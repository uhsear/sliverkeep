#!/usr/bin/env python
"""Drop sliver parts from a GeoJSON polygon layer, but keep every part that holds an address point.

Refuses a status filter that matches no point, because a filter that matches
nothing leaves no evidence, and with no evidence every small part is dropped.

Subtract one boundary layer from another and the edges that were digitized
apart throw off shards: thin parts a few square feet across. The usual cleanup
drops every part under an area threshold. Size is the only test it applies,
so a real pocket of land that happens to be small goes out with the shards.
If people live in that pocket, their addresses are now outside the polygon.

The rule here is the one size alone cannot apply: a part under the threshold
is dropped only when no address point lies inside it. A point that also lies
inside a mask polygon (the area the subtraction removed) is not evidence,
because that ground was never meant to be in the result.

    python sliverkeep.py --self-test
    python sliverkeep.py parts.geojson --points addresses.geojson --min-area 10000
    python sliverkeep.py parts.geojson --points addresses.geojson --min-area 10000 \\
        --mask removed.geojson --status-field LIFECYCLE --status-value CURRENT
    python sliverkeep.py parts.geojson --points addresses.geojson --min-area 10000 \\
        --out kept.geojson --apply

Nothing is written without --apply.

Exit codes: 0 the parts were classified, 2 a file could not be read or
written, 3 refused, 64 usage error.
"""

from __future__ import print_function

import argparse
import contextlib
import io
import json
import math
import os
import runpy
import shutil
import sys
import tempfile

# =============================================================================
# CONFIGURATION. Deliberately not flags. Change here, not at the call site.
# =============================================================================

# The sphere the areas are measured on, in metres. This is the WGS 84
# semi-major axis, used as a sphere radius. A spherical area differs from the
# WGS 84 ellipsoidal one by under 1%: measured against pyproj's Geod, +0.67%
# at the equator, 0.00% at 45 degrees and -0.63% at 80 degrees. That is far
# inside the margin any sliver threshold is chosen with.
EARTH_RADIUS_M = 6378137.0

# When the largest part under the threshold and the smallest part over it are
# closer than this ratio, the run says that size alone does not separate them.
# A real sliver population sits orders of magnitude under the real parts. Two
# parts within 2x of each other on either side of the line mean the line was
# drawn through a continuum, and only the point test is deciding anything.
SIZE_GAP_WARN = 2.0

# How many parts are listed under each heading. The count is the finding; the
# sample is there so somebody can open one part and agree.
DEFAULT_SAMPLE = 10

# How many distinct status values a refusal lists.
LIFECYCLE_VALUES_SHOWN = 10

# =============================================================================
# End of CONFIGURATION.
# =============================================================================

KEEP_SIZE = "KEEP_SIZE"
KEEP_POINTS = "KEEP_POINTS"
DROP = "DROP"

VERDICTS = (KEEP_SIZE, KEEP_POINTS, DROP)

INSIDE = "inside"
OUTSIDE = "outside"
BOUNDARY = "boundary"


class InputError(ValueError):
    """A file that parsed but cannot be used. Exit 2, like an unreadable one."""


class Refused(Exception):
    """The run cannot give a sound answer, so it gives none. Exit 3."""

    def __init__(self, reason, details):
        Exception.__init__(self, reason)
        self.reason = reason
        self.details = list(details)


class Part(object):
    """One polygon part: an exterior ring and its holes, from one feature."""

    def __init__(self, feature, index, rings):
        self.feature = feature
        self.index = index
        self.rings = rings
        self.area = polygon_area(rings)
        self.bbox = bbox_of(rings[0])
        self.verdict = None
        self.evidence = []
        self.masked = 0

    @property
    def label(self):
        return "feature %d part %d" % (self.feature, self.index)


class Point(object):
    """One address point, reduced to what the rescue test reads."""

    def __init__(self, index, label, x, y, properties):
        self.index = index
        self.label = label
        self.x = x
        self.y = y
        self.properties = properties


class Report(object):
    """Everything one run decided, in the order describe() prints it."""

    def __init__(self, parts, min_area, points_total, points_used,
                 status_line, mask_count, gap):
        self.parts = parts
        self.min_area = min_area
        self.points_total = points_total
        self.points_used = points_used
        self.status_line = status_line
        self.mask_count = mask_count
        self.gap = gap

    def count(self, verdict):
        return sum(1 for p in self.parts if p.verdict == verdict)


# ------------------------------------------------------------------ geometry

def ring_area(ring):
    """Area a closed lon/lat ring bounds on the sphere, in square metres.

    The line-integral form from Chamberlain and Duquette, "Some Algorithms for
    Polygons on a Sphere" (JPL, 2007): each edge adds its longitude span
    times (2 + sin lat1 + sin lat2). The sign follows the winding, so the
    absolute value is taken and the winding never matters.
    """
    total = 0.0
    for (x1, y1), (x2, y2) in zip(ring, ring[1:]):
        total += (math.radians(x2 - x1)
                  * (2.0 + math.sin(math.radians(y1))
                     + math.sin(math.radians(y2))))
    return abs(total) * EARTH_RADIUS_M * EARTH_RADIUS_M / 2.0


def polygon_area(rings):
    """Exterior area less the holes. A hole is never a part of its own."""
    return max(0.0, ring_area(rings[0]) - sum(ring_area(r) for r in rings[1:]))


def bbox_of(ring):
    xs = [p[0] for p in ring]
    ys = [p[1] for p in ring]
    return (min(xs), min(ys), max(xs), max(ys))


def in_bbox(x, y, box):
    return box[0] <= x <= box[2] and box[1] <= y <= box[3]


def on_segment(x, y, a, b):
    """True when (x, y) lies exactly on the segment a-b."""
    if (b[0] - a[0]) * (y - a[1]) - (b[1] - a[1]) * (x - a[0]) != 0:
        return False
    return (min(a[0], b[0]) <= x <= max(a[0], b[0])
            and min(a[1], b[1]) <= y <= max(a[1], b[1]))


def ring_position(x, y, ring):
    """INSIDE, OUTSIDE or BOUNDARY of one closed ring, by ray casting."""
    inside = False
    for a, b in zip(ring, ring[1:]):
        if on_segment(x, y, a, b):
            return BOUNDARY
        if (a[1] > y) != (b[1] > y):
            if x < (b[0] - a[0]) * (y - a[1]) / (b[1] - a[1]) + a[0]:
                inside = not inside
    return INSIDE if inside else OUTSIDE


def in_polygon(x, y, rings, boundary_counts):
    """Whether a point is in a polygon with holes.

    A point exactly on an edge is ambiguous, so the caller says which answer
    is the safe one. For a part it is "inside": keeping a part is the error
    that loses nothing. For the mask it is "outside": a point on the mask's
    edge stays evidence, for the same reason.
    """
    where = ring_position(x, y, rings[0])
    if where == OUTSIDE:
        return False
    if where == BOUNDARY:
        return boundary_counts
    for hole in rings[1:]:
        where = ring_position(x, y, hole)
        if where == INSIDE:
            return False
        if where == BOUNDARY:
            return boundary_counts
    return True


# ------------------------------------------------------------ GeoJSON reading

def features_of(doc, path):
    """(feature, geometry) pairs from any GeoJSON object a layer can be.

    feature is None when the document is a bare geometry.
    """
    if not isinstance(doc, dict):
        raise InputError("%s: not a GeoJSON object" % path)
    kind = doc.get("type")
    if kind == "FeatureCollection":
        features = doc.get("features")
        if not isinstance(features, list):
            raise InputError("%s: FeatureCollection has no features list"
                             % path)
        for i, feature in enumerate(features):
            if not isinstance(feature, dict) or feature.get("type") != "Feature":
                raise InputError("%s: features[%d] is not a Feature" % (path, i))
        return [(f, f.get("geometry")) for f in features]
    if kind == "Feature":
        return [(doc, doc.get("geometry"))]
    return [(None, doc)]


def check_position(pos, where):
    """(lon, lat) of one GeoJSON position, refused when it is not lon/lat."""
    if (not isinstance(pos, list) or len(pos) < 2
            or not all(isinstance(v, (int, float)) and not isinstance(v, bool)
                       for v in pos[:2])):
        raise InputError("%s: %r is not a position" % (where, pos))
    x, y = float(pos[0]), float(pos[1])
    # RFC 7946 section 4: GeoJSON coordinates are WGS 84 longitude and
    # latitude. A layer exported in its projected CRS has values in the
    # hundreds of thousands, and an area computed from them would be noise.
    if not (-180.0 <= x <= 180.0 and -90.0 <= y <= 90.0):
        raise InputError("%s: %r is not longitude/latitude. Export the layer "
                         "in WGS 84 (EPSG:4326)." % (where, pos))
    return (x, y)


def check_ring(ring, where):
    """One linear ring as (lon, lat) tuples, refused when it is not closed."""
    if not isinstance(ring, list) or len(ring) < 4:
        raise InputError("%s: a linear ring needs four or more positions"
                         % where)
    out = [check_position(p, where) for p in ring]
    if out[0] != out[-1]:
        raise InputError("%s: ring is not closed, its first and last "
                         "positions differ" % where)
    return out


def polygons_of(geometry, where):
    """The polygons (lists of rings) a geometry holds. None holds none."""
    if geometry is None:
        return []
    kind = geometry.get("type") if isinstance(geometry, dict) else None
    coords = geometry.get("coordinates") if kind else None
    if kind == "Polygon":
        polys = [coords]
    elif kind == "MultiPolygon":
        polys = coords
    else:
        raise InputError("%s: a %s is not a polygon" % (where, kind))
    if not isinstance(polys, list) or not all(
            isinstance(p, list) and p for p in polys):
        raise InputError("%s: polygon coordinates are malformed" % where)
    return [[check_ring(r, where) for r in poly] for poly in polys]


def points_of(geometry, where):
    """The positions a Point or MultiPoint holds. None holds none."""
    if geometry is None:
        return []
    kind = geometry.get("type") if isinstance(geometry, dict) else None
    coords = geometry.get("coordinates") if kind else None
    if kind == "Point":
        return [check_position(coords, where)]
    if kind == "MultiPoint":
        if not isinstance(coords, list):
            raise InputError("%s: MultiPoint coordinates are malformed"
                             % where)
        return [check_position(p, where) for p in coords]
    raise InputError("%s: a %s is not a point" % (where, kind))


def load_parts(doc, path):
    """Every polygon part in the layer, numbered by feature and part."""
    parts = []
    for fi, (_feature, geometry) in enumerate(features_of(doc, path)):
        where = "%s feature %d" % (path, fi)
        for pi, rings in enumerate(polygons_of(geometry, where)):
            parts.append(Part(fi, pi, rings))
    return parts


def load_points(doc, path):
    """Every address point, and the count of features with no geometry."""
    points, empty = [], 0
    for fi, (feature, geometry) in enumerate(features_of(doc, path)):
        positions = points_of(geometry, "%s feature %d" % (path, fi))
        if not positions:
            empty += 1
        props = (feature or {}).get("properties") or {}
        fid = (feature or {}).get("id")
        label = "#%d" % fi if fid is None else str(fid)
        for x, y in positions:
            points.append(Point(fi, label, x, y, props))
    return points, empty


def load_mask(doc, path):
    """The mask polygons, each a list of rings."""
    mask = []
    for fi, (_feature, geometry) in enumerate(features_of(doc, path)):
        mask.extend(polygons_of(geometry, "%s feature %d" % (path, fi)))
    return mask


# --------------------------------------------------------------- the decision

def status_key(value):
    """How a status cell is compared: trimmed text, case kept. null is null."""
    return "null" if value is None else str(value).strip()


def filter_status(points, field, values):
    """The points the status filter keeps, and the line that reports it.

    Every value named must match at least one point. A filter value that
    matches nothing is a typo or a renamed domain code, never a fact about the
    data, and the points it should have kept are the evidence that saves a
    real pocket. So the run is refused rather than continued with less.
    """
    if not points:
        raise Refused(
            "the point layer holds no points, so no part could be kept for "
            "the points it holds",
            ["Every part under the threshold would have been dropped. "
             "Nothing was written."])
    if not field:
        return points, None
    wanted = [status_key(v) for v in values]
    seen = {}
    for p in points:
        if field in p.properties:
            key = status_key(p.properties[field])
            seen[key] = seen.get(key, 0) + 1
    kept = [p for p in points
            if field in p.properties
            and status_key(p.properties[field]) in wanted]
    unmatched = [v for v in wanted if v not in seen]
    shown = " or ".join("'%s'" % v for v in wanted)
    line = "%s = %s matches %d of %d point(s)" % (field, shown, len(kept),
                                                  len(points))
    if unmatched:
        details = [line]
        if not seen:
            details.append("no point carries a property named '%s'" % field)
        else:
            ranked = sorted(seen.items(), key=lambda kv: (-kv[1], kv[0]))
            details.append("values seen in %s: %s" % (field, ", ".join(
                "'%s' %d" % kv for kv in ranked[:LIFECYCLE_VALUES_SHOWN])))
            for v in unmatched:
                for s in sorted(seen):
                    if s.lower() == v.lower():
                        details.append("'%s' differs from '%s' only in case"
                                       % (s, v))
        details.append("Without the points this value should keep, every "
                       "part they hold would be dropped. Nothing was written.")
        raise Refused("the status value %s matches no point"
                      % " and ".join("'%s'" % v for v in unmatched), details)
    return kept, line


def size_gap(parts, min_area):
    """(largest area under the threshold, smallest area at or over it)."""
    under = [p.area for p in parts if p.area < min_area]
    over = [p.area for p in parts if p.area >= min_area]
    if not under or not over:
        return None
    return (max(under), min(over))


def decide(parts, points, mask, min_area):
    """Give every part a verdict. Only parts under the threshold are tested.

    Raises Refused when every part would be dropped: an empty result is never
    a cleanup, and writing one would erase the layer.
    """
    for part in parts:
        # A second run over the same parts must not inherit the first run's
        # evidence, or a part one filter kept stays kept under the next.
        part.evidence, part.masked = [], 0
        if part.area >= min_area:
            part.verdict = KEEP_SIZE
            continue
        for pt in points:
            if not in_bbox(pt.x, pt.y, part.bbox):
                continue
            if not in_polygon(pt.x, pt.y, part.rings, True):
                continue
            if any(in_polygon(pt.x, pt.y, m, False) for m in mask):
                part.masked += 1
            else:
                part.evidence.append(pt.label)
        part.verdict = KEEP_POINTS if part.evidence else DROP
    if parts and all(p.verdict == DROP for p in parts):
        raise Refused(
            "every part is under the threshold and holds no point, so the "
            "result would be empty",
            ["%d part(s), the largest %.1f m2, threshold %.1f m2"
             % (len(parts), max(p.area for p in parts), min_area),
             "Lower --min-area or check the point layer. Nothing was "
             "written."])


def run(parts, points, mask, min_area, status_field=None, status_values=()):
    """The whole decision, from loaded layers to a Report. No I/O."""
    if not (min_area > 0 and math.isfinite(min_area)):
        raise ValueError("--min-area must be a positive number, got %r"
                         % min_area)
    used, status_line = filter_status(points, status_field, status_values)
    decide(parts, used, mask, min_area)
    return Report(parts, min_area, len(points), len(used), status_line,
                  len(mask), size_gap(parts, min_area))


def describe(report, sample):
    """The lines a run prints, in order."""
    lines = []
    if report.status_line:
        lines.append("status    %s" % report.status_line)
    if report.mask_count:
        lines.append("mask      %d polygon(s). A point inside them is not "
                     "evidence." % report.mask_count)
    lines.append("threshold parts under %.1f m2 are slivers unless an address "
                 "point lies inside them" % report.min_area)
    lines.append("parts classified: %d" % len(report.parts))
    for verdict in VERDICTS:
        lines.append("  %-12s %d" % (verdict, report.count(verdict)))

    kept = sorted((p for p in report.parts if p.verdict == KEEP_POINTS),
                  key=lambda p: p.area)
    if kept and sample:
        lines.append("")
        lines.append("kept for the points inside them, smallest first:")
        for p in kept[:sample]:
            lines.append("  %-20s %12.1f m2  %d point(s): %s"
                         % (p.label, p.area, len(p.evidence),
                            ", ".join(p.evidence[:3])
                            + (" ..." if len(p.evidence) > 3 else "")))
    dropped = sorted((p for p in report.parts if p.verdict == DROP),
                     key=lambda p: -p.area)
    if dropped and sample:
        lines.append("")
        lines.append("dropped, largest first:")
        for p in dropped[:sample]:
            note = ("  (%d point(s) inside, all in the mask)" % p.masked
                    if p.masked else "")
            lines.append("  %-20s %12.1f m2%s" % (p.label, p.area, note))

    if report.gap:
        under, over = report.gap
        lines.append("")
        if under <= 0:
            lines.append("size gap: every part under the threshold has no "
                         "area at all")
        else:
            ratio = over / under
            text = ("size gap: the largest part under the threshold is %.1f "
                    "m2 and the smallest part over it is %.1f m2, %.2fx apart."
                    % (under, over, ratio))
            if ratio < SIZE_GAP_WARN:
                text += (" Size alone does not separate them; the points "
                         "are deciding.")
            lines.append(text)

    masked = sum(p.masked for p in report.parts)
    if masked:
        lines.append("points inside a small part but also inside the mask, "
                     "not counted as evidence: %d" % masked)

    lines.append("")
    keep = len(report.parts) - report.count(DROP)
    lines.append("VERDICT: keep %d part(s), drop %d sliver(s) totalling %.1f "
                 "m2." % (keep, report.count(DROP),
                          sum(p.area for p in dropped)))
    return lines


def filtered_doc(doc, parts):
    """The parts layer with every DROP part removed. The input is not changed.

    Every member the input carried (ids, properties, foreign members) is kept,
    and each geometry keeps its type. A feature whose every part was dropped
    leaves the collection. Coordinates are copied, never rewound or rounded.
    """
    keep = {}
    for p in parts:
        if p.verdict != DROP:
            keep.setdefault(p.feature, []).append(p.index)

    def geometry_for(fi, geometry):
        if geometry is None:
            return None
        kept = keep.get(fi, [])
        if not kept:
            return False
        out = dict(geometry)
        if geometry["type"] == "Polygon":
            out["coordinates"] = geometry["coordinates"]
        else:
            out["coordinates"] = [geometry["coordinates"][i] for i in kept]
        return out

    kind = doc.get("type")
    if kind == "FeatureCollection":
        out = dict(doc)
        out["features"] = []
        for fi, feature in enumerate(doc["features"]):
            geometry = geometry_for(fi, feature.get("geometry"))
            if geometry is False:
                continue
            copy = dict(feature)
            copy["geometry"] = geometry
            out["features"].append(copy)
        return out
    if kind == "Feature":
        out = dict(doc)
        out["geometry"] = geometry_for(0, doc.get("geometry"))
        return out
    return geometry_for(0, doc)


# ------------------------------------------------------------------ self-test

def self_test():
    """Assertions over the decision core and the CLI. No network."""
    passed = [0]
    failed = []

    def check(cond, label):
        if cond:
            passed[0] += 1
            print("PASS  %s" % label)
        else:
            failed.append(label)
            print("FAIL  %s" % label)

    def raises(fn, label, exc_type=ValueError):
        try:
            fn()
        except exc_type:
            check(True, label)
        except Exception as exc:
            check(False, "%s (wrong exception %r)" % (label, exc))
        else:
            check(False, "%s (no error raised)" % label)

    def refusal(fn):
        """The Refused a call raises, or None when it raises nothing."""
        try:
            fn()
        except Refused as exc:
            return exc
        return None

    def exits(fn):
        """The exit code a call ends with, or None when it does not exit."""
        with contextlib.redirect_stderr(io.StringIO()):
            try:
                fn()
            except SystemExit as exc:
                return exc.code
        return None

    def box(x0, y0, w, h):
        """A closed counter-clockwise rectangle ring, as GeoJSON lists."""
        return [[x0, y0], [x0 + w, y0], [x0 + w, y0 + h], [x0, y0 + h],
                [x0, y0]]

    def tup(ring):
        return [tuple(p) for p in ring]

    def pt(label, x, y, **props):
        return Point(0, label, x, y, props)

    print("sliverkeep self-test: no network, no credentials")
    print("-" * 68)

    # --- area
    one = ring_area(tup(box(0, 0, 1, 1)))
    exact = (EARTH_RADIUS_M ** 2 * math.radians(1.0)
             * math.sin(math.radians(1.0)))
    check(abs(one - exact) < 1e-3,
          "a one-degree cell at the equator has its exact spherical area")
    check(abs(ring_area(tup(box(0, 0, 1, 1))[::-1]) - one) < 1e-6,
          "area does not depend on winding: clockwise gives the same number")
    small = ring_area(tup(box(10, 0, 0.001, 0.001)))
    check(12300 < small < 12500,
          "a 0.001 degree square at the equator is about 12,390 m2")
    check(ring_area(tup(box(10, 60, 0.001, 0.001))) < small * 0.51,
          "the same square at 60 degrees north is half the area, as it is")
    holed = [tup(box(0, 0, 0.01, 0.01)), tup(box(0.004, 0.004, 0.002, 0.002))]
    check(abs(polygon_area(holed) - (ring_area(holed[0])
                                     - ring_area(holed[1]))) < 1e-6,
          "a hole is subtracted from its part's area")
    check(polygon_area([tup(box(0, 0, 0.001, 0.001)),
                        tup(box(0, 0, 0.002, 0.002))]) == 0.0,
          "a hole larger than its exterior cannot make the area negative")

    # --- point in polygon
    sq = [tup(box(0, 0, 1, 1))]
    check(in_polygon(0.5, 0.5, sq, False), "a point in the middle is inside")
    check(not in_polygon(1.5, 0.5, sq, True), "a point past the edge is outside")
    check(not in_polygon(0.5, 1.5, sq, True),
          "a point above the ring is outside")
    check(in_polygon(1.0, 0.5, sq, True),
          "a point on an edge counts as inside a part  <-- pinned defect")
    check(not in_polygon(1.0, 0.5, sq, False),
          "and as outside the mask, so it stays evidence  <-- pinned defect")
    check(in_polygon(0.0, 0.0, sq, True), "a vertex counts as the boundary")
    donut = [tup(box(0, 0, 1, 1)), tup(box(0.25, 0.25, 0.5, 0.5))]
    check(not in_polygon(0.5, 0.5, donut, True),
          "a point in a hole is not in the part  <-- pinned defect")
    check(in_polygon(0.1, 0.1, donut, True),
          "a point between the hole and the edge is in the part")
    check(in_polygon(0.25, 0.5, donut, True),
          "a point on a hole's edge is on the part's boundary, so inside")
    check(not in_polygon(0.25, 0.5, donut, False),
          "and outside the mask when the hole is in the mask")
    tri = [[(0.0, 0.0), (2.0, 0.0), (1.0, 2.0), (0.0, 0.0)]]
    check(in_polygon(1.0, 1.0, tri, False) and not in_polygon(1.9, 1.5, tri, False),
          "a slanted edge splits inside from outside correctly")
    check(in_bbox(0.5, 0.5, (0, 0, 1, 1)) and not in_bbox(2, 0.5, (0, 0, 1, 1)),
          "the bounding box prefilter passes inside and rejects outside")
    check(not on_segment(3.0, 0.0, (0.0, 0.0), (2.0, 0.0)),
          "a collinear point past the segment's end is not on it")

    # --- GeoJSON reading
    where = "t"
    raises(lambda: check_position([200.0, 10.0], where),
           "a longitude of 200 is refused as not lon/lat  <-- pinned defect",
           InputError)
    raises(lambda: check_position([500000.0, 1700000.0], where),
           "a projected easting and northing is refused  <-- pinned defect",
           InputError)
    raises(lambda: check_position([10.0, 91.0], where),
           "a latitude of 91 is refused", InputError)
    raises(lambda: check_position([True, 1.0], where),
           "a JSON true is not a coordinate", InputError)
    raises(lambda: check_position([1.0], where),
           "a position with one number is refused", InputError)
    raises(lambda: check_position("1,2", where),
           "a string is not a position", InputError)
    check(check_position([10, 20, 5], where) == (10.0, 20.0),
          "a position with a Z value reads its first two numbers")
    raises(lambda: check_ring([[0, 0], [1, 0], [1, 1], [0, 1]], where),
           "an unclosed ring is refused  <-- pinned defect", InputError)
    raises(lambda: check_ring([[0, 0], [1, 0], [0, 0]], where),
           "a ring of three positions is refused", InputError)
    raises(lambda: check_ring("ring", where),
           "a ring that is not a list is refused", InputError)
    check(polygons_of(None, where) == [], "a null geometry holds no polygon")
    raises(lambda: polygons_of({"type": "Point", "coordinates": [0, 0]},
                               where),
           "a Point in the parts layer is refused", InputError)
    raises(lambda: polygons_of({"coordinates": []}, where),
           "a geometry with no type is refused", InputError)
    raises(lambda: polygons_of("x", where),
           "a geometry that is not an object is refused", InputError)
    raises(lambda: polygons_of({"type": "MultiPolygon", "coordinates": [[]]},
                               where),
           "a MultiPolygon with an empty polygon is refused", InputError)
    raises(lambda: polygons_of({"type": "Polygon", "coordinates": 5}, where),
           "Polygon coordinates that are not a list are refused", InputError)
    check(len(polygons_of({"type": "MultiPolygon",
                           "coordinates": [[box(0, 0, 1, 1)],
                                           [box(2, 2, 1, 1)]]}, where)) == 2,
          "a MultiPolygon holds one polygon per part")
    check(points_of(None, where) == [], "a null geometry holds no point")
    check(points_of({"type": "MultiPoint", "coordinates": [[1, 2], [3, 4]]},
                    where) == [(1.0, 2.0), (3.0, 4.0)],
          "a MultiPoint holds each of its positions")
    raises(lambda: points_of({"type": "Polygon", "coordinates": []}, where),
           "a Polygon in the points layer is refused", InputError)
    raises(lambda: points_of({"type": "MultiPoint", "coordinates": 3}, where),
           "a MultiPoint whose coordinates are not a list is refused",
           InputError)
    raises(lambda: features_of([], where), "a JSON array is not GeoJSON",
           InputError)
    raises(lambda: features_of({"type": "FeatureCollection"}, where),
           "a FeatureCollection with no features list is refused", InputError)
    raises(lambda: features_of({"type": "FeatureCollection",
                                "features": [{"type": "Point"}]}, where),
           "a collection member that is not a Feature is refused", InputError)
    raises(lambda: features_of({"type": "FeatureCollection",
                                "features": [5]}, where),
           "a collection member that is not an object is refused", InputError)
    bare = {"type": "Point", "coordinates": [1, 2]}
    check(features_of(bare, where) == [(None, bare)],
          "a bare geometry is read as one layer of one shape")
    pts, empty = load_points({"type": "FeatureCollection", "features": [
        {"type": "Feature", "id": "a-1", "properties": None,
         "geometry": {"type": "Point", "coordinates": [1, 2]}},
        {"type": "Feature", "properties": {},
         "geometry": None}]}, where)
    check(len(pts) == 1 and pts[0].label == "a-1" and pts[0].properties == {}
          and empty == 1,
          "a point takes its feature id, null properties read as none, and "
          "a null geometry is counted")
    pts, empty = load_points(bare, where)
    check(pts[0].label == "#0" and empty == 0,
          "a bare point with no id is labelled by its position in the file")

    # --- status filter
    pop = [pt("p1", 0, 0, LIFECYCLE="CURRENT"), pt("p2", 0, 0, LIFECYCLE="CURRENT "),
           pt("p3", 0, 0, LIFECYCLE="RETIRED"), pt("p4", 0, 0, LIFECYCLE=None),
           pt("p5", 0, 0)]
    kept, line = filter_status(pop, "LIFECYCLE", ["CURRENT"])
    check([p.label for p in kept] == ["p1", "p2"],
          "a status value matches with surrounding whitespace trimmed")
    check(line == "LIFECYCLE = 'CURRENT' matches 2 of 5 point(s)",
          "and the line reports how many of all the points it kept")
    kept, line = filter_status(pop, "LIFECYCLE", ["CURRENT", "RETIRED"])
    check(len(kept) == 3, "two status values keep the points of either")
    kept, line = filter_status(pop, "LIFECYCLE", ["null"])
    check([p.label for p in kept] == ["p4"],
          "a JSON null is the value 'null', and a missing property is no value")
    kept, line = filter_status(pop, None, [])
    check(len(kept) == 5 and line is None,
          "with no status field every point is evidence")
    exc = refusal(lambda: filter_status(pop, "LIFECYCLE", ["Current"]))
    check(exc is not None,
          "a status value that matches nothing refuses the run"
          "  <-- pinned defect")
    details = exc.details if exc else []
    check(exc is not None and "'Current'" in exc.reason,
          "and the refusal names the value")
    check(any("'CURRENT' 2" in d for d in details),
          "and lists the values the field does hold, with counts")
    check(any("'CURRENT' differs from 'Current' only in case" in d
              for d in details),
          "and says when the miss is only a difference of case"
          "  <-- pinned defect")
    exc = refusal(lambda: filter_status(pop, "LIFECYCLE", ["CURRENT", "CURREN"]))
    check(exc is not None and "'CURREN'" in exc.reason
          and "'CURRENT'" not in exc.reason,
          "one value of two that matches nothing still refuses, and only "
          "that value is named  <-- pinned defect")
    check(exc is not None and not any("only in case" in d
                                      for d in exc.details),
          "and no case hint is printed when there is no case near-miss")
    exc = refusal(lambda: filter_status(pop, "LIFE", ["CURRENT"]))
    check(exc is not None and any(
        "no point carries a property named 'LIFE'" in d for d in exc.details),
          "a status field no point carries refuses the run, and says so")
    exc = refusal(lambda: filter_status([], None, []))
    check(exc is not None and "holds no points" in exc.reason,
          "an empty point layer refuses the run  <-- pinned defect")

    # --- the decision: the story, at small scale
    # Threshold 10,000 m2. A big part, the smallest part over the line, the
    # largest part under it (1.10x smaller), a small pocket with a live
    # address in it, and a small shard whose only address is in the mask.
    big = Part(0, 0, [tup(box(10.0, 0.0, 0.01, 0.01))])
    over = Part(0, 1, [tup(box(10.02, 0.0, 0.00081, 0.001))])
    under = Part(0, 2, [tup(box(10.03, 0.0, 0.000736, 0.001))])
    pocket = Part(0, 3, [tup(box(10.04, 0.0, 0.0003, 0.0003))])
    shard = Part(0, 4, [tup(box(10.05, 0.0, 0.0002, 0.00005))])
    parts = [big, over, under, pocket, shard]
    check(over.area >= 10000 > under.area,
          "the fixture puts one part each side of the threshold")
    check(abs(over.area / under.area - 1.10) < 0.01,
          "and the two are 1.10x apart, as in the story")
    points = [pt("live-1", 10.04015, 0.00015, LIFECYCLE="CURRENT"),
              pt("city-1", 10.0501, 0.00002, LIFECYCLE="CURRENT"),
              pt("gone-1", 10.0302, 0.00005, LIFECYCLE="RETIRED"),
              pt("far-1", 50.0, 50.0, LIFECYCLE="CURRENT"),
              pt("box-1", 10.0501, 0.00004, LIFECYCLE="CURRENT")]
    # The mask covers the shard and its two addresses; box-1 is in the
    # shard's bounding box too, so it reaches the mask test.
    mask = [[tup(box(10.0499, -0.0001, 0.0004, 0.0003))]]
    report = run(parts, points, mask, 10000.0, "LIFECYCLE", ["CURRENT"])
    check(big.verdict == KEEP_SIZE and over.verdict == KEEP_SIZE,
          "parts at or over the threshold are kept for their size")
    check(pocket.verdict == KEEP_POINTS,
          "a small pocket holding a live address is kept  <-- pinned defect")
    check(pocket.evidence == ["live-1"], "and names the address that saved it")
    check(under.verdict == DROP,
          "a small part whose only address is retired is dropped")
    check(shard.verdict == DROP and shard.masked == 2,
          "an address inside the mask is not evidence  <-- pinned defect")
    check(report.points_total == 5 and report.points_used == 4,
          "the report counts all the points and the points the filter kept")
    check(report.gap == (under.area, over.area),
          "the size gap is the largest part under and smallest over the line")
    check(report.count(KEEP_SIZE) + report.count(KEEP_POINTS)
          + report.count(DROP) == len(parts),
          "every part gets exactly one verdict")

    lines = describe(report, DEFAULT_SAMPLE)
    text = "\n".join(lines)
    check("1.10x apart. Size alone does not separate them" in text,
          "a threshold drawn through a continuum is said in words"
          "  <-- pinned defect")
    check("feature 0 part 3" in text and "live-1" in text,
          "the kept pocket is listed with the address that kept it")
    check("(2 point(s) inside, all in the mask)" in text,
          "a dropped part with masked addresses says so")
    check("not counted as evidence: 2" in text,
          "the masked points are counted in a line of their own")
    check("status    LIFECYCLE = 'CURRENT' matches 4 of 5 point(s)" in text,
          "the status line is printed")
    check(lines[-1].startswith("VERDICT: keep 3 part(s), drop 2 sliver(s)"),
          "the verdict line counts kept and dropped parts")
    quiet = describe(report, 0)
    check(not any("dropped, largest first" in ln or "kept for the points" in ln
                  for ln in quiet),
          "--sample 0 lists no part, only the counts")

    # The same parts, run again under other filters.
    run(parts, points, [], 10000.0, "LIFECYCLE", ["RETIRED"])
    check(pocket.verdict == DROP and under.verdict == KEEP_POINTS,
          "a second run under another filter does not inherit the first run's "
          "evidence  <-- pinned defect")
    run(parts, points, [], 10000.0)
    check(shard.verdict == KEEP_POINTS and shard.masked == 0,
          "with no mask, an address in the shard keeps it")
    many = Part(0, 9, [tup(box(10.04, 0.0, 0.0003, 0.0003))])
    many.verdict = KEEP_POINTS
    many.evidence = ["a", "b", "c", "d"]
    wide = Report([many], 10000.0, 4, 4, None, 0, None)
    wide_text = "\n".join(describe(wide, 5))
    check("a, b, c ..." in wide_text,
          "a part held by more than three points lists three and an ellipsis")
    check("status" not in wide_text and "mask" not in wide_text
          and "size gap" not in wide_text,
          "no status, mask or gap line is printed when there is none")

    wide_gap = Report([], 10000.0, 0, 0, None, 0, (100.0, 1000000.0))
    check("10000.00x apart." in "\n".join(describe(wide_gap, 1))
          and "Size alone" not in "\n".join(describe(wide_gap, 1)),
          "a wide size gap is reported without the warning")
    zero_gap = Report([], 10000.0, 0, 0, None, 0, (0.0, 20000.0))
    check("has no area at all" in "\n".join(describe(zero_gap, 1)),
          "a gap whose largest small part has no area does not divide by 0")
    check(size_gap([big], 10000.0) is None,
          "there is no size gap when no part is under the threshold")

    wedge = Part(0, 0, [[(20.0, 0.0), (20.001, 0.0), (20.0, 0.001),
                         (20.0, 0.0)]])
    run([big, wedge], [pt("corner", 20.0009, 0.0009)], [], 10000.0)
    check(wedge.verdict == DROP,
          "a point inside a part's bounding box but outside the part is not "
          "evidence")
    exc = refusal(lambda: run([under, shard], [pt("far", 50.0, 50.0)], [],
                              10000.0))
    check(exc is not None and "result would be empty" in exc.reason,
          "a run that would drop every part is refused  <-- pinned defect")
    raises(lambda: run(parts, points, [], 0.0),
           "a threshold of zero is a usage error")
    raises(lambda: run(parts, points, [], float("nan")),
           "a NaN threshold is a usage error  <-- pinned defect")
    raises(lambda: run(parts, points, [], float("inf")),
           "an infinite threshold is a usage error")
    empty_report = run([], points, [], 10.0)
    check(empty_report.parts == [] and empty_report.gap is None,
          "a parts layer with no parts classifies nothing and refuses nothing")

    # --- the output document
    holed_mp = {"type": "MultiPolygon",
                "coordinates": [[box(10.0, 0.0, 0.01, 0.01)],
                                [box(10.05, 0.0, 0.0002, 0.00005)],
                                [box(10.04, 0.0, 0.0003, 0.0003)]]}
    coll = {"type": "FeatureCollection", "name": "layer", "features": [
        {"type": "Feature", "id": 7, "properties": {"k": 1},
         "geometry": holed_mp},
        {"type": "Feature", "properties": {},
         "geometry": {"type": "Polygon",
                      "coordinates": [box(10.06, 0.0, 0.0001, 0.0001)]}},
        {"type": "Feature", "properties": {"note": "no shape"},
         "geometry": None}]}
    lp = load_parts(coll, "c")
    run(lp, [pt("live", 10.04015, 0.00015)], [], 10000.0)
    out = filtered_doc(coll, lp)
    check([p.verdict for p in lp] == [KEEP_SIZE, DROP, KEEP_POINTS, DROP],
          "the collection's parts are classified across features")
    check(out["features"][0]["geometry"]["coordinates"]
          == [holed_mp["coordinates"][0], holed_mp["coordinates"][2]],
          "a dropped part leaves its MultiPolygon, the kept parts stay in order")
    check(out["features"][0]["id"] == 7
          and out["features"][0]["properties"] == {"k": 1}
          and out["name"] == "layer",
          "ids, properties and foreign members are carried through")
    check(len(out["features"]) == 2
          and out["features"][1]["properties"] == {"note": "no shape"},
          "a feature whose only part is dropped leaves the collection, and a "
          "feature with no geometry stays")
    check(len(coll["features"][0]["geometry"]["coordinates"]) == 3,
          "the input document is not changed")
    single = {"type": "Feature", "properties": {},
              "geometry": {"type": "Polygon",
                           "coordinates": [box(10.0, 0.0, 0.01, 0.01)]}}
    sp = load_parts(single, "s")
    run(sp, [pt("x", 50.0, 50.0)], [], 10000.0)
    check(filtered_doc(single, sp)["geometry"]["type"] == "Polygon",
          "a Feature stays a Feature, and a kept Polygon stays a Polygon")
    geom = single["geometry"]
    gp = load_parts(geom, "g")
    run(gp, [pt("x", 50.0, 50.0)], [], 10000.0)
    check(filtered_doc(geom, gp) == geom,
          "a bare geometry comes back as a bare geometry")
    nogeom = {"type": "Feature", "properties": {}, "geometry": None}
    check(filtered_doc(nogeom, [])["geometry"] is None,
          "a Feature with no geometry comes back with none")

    # --- the parser
    a = _parse(["p.geojson", "--points", "a.geojson", "--min-area", "5"])
    check(a.apply is False, "--apply defaults to OFF")
    check(exits(lambda: _parse(["p.geojson", "--points", "a.geojson",
                                "--min-area", "5", "--ap"])) == 64,
          "a unique prefix --ap of --apply is REFUSED by the parser"
          "  <-- pinned defect")
    check(exits(lambda: _parse(["p.geojson", "--poi", "a.geojson",
                                "--min-area", "5"])) == 64,
          "a prefix --poi of --points is refused too")
    check(exits(lambda: _parse(["--min-area", "many"])) == 64,
          "a parser error exits 64, not the 2 an unreadable file exits"
          "  <-- pinned defect")

    # --- the CLI, against files in a temporary directory
    tmp = tempfile.mkdtemp(prefix="sliverkeep-")
    try:
        def put(name, doc):
            path = os.path.join(tmp, name)
            with open(path, "w") as handle:
                if isinstance(doc, str):
                    handle.write(doc)
                else:
                    json.dump(doc, handle)
            return path

        def cli(argv):
            buf, ebuf = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(buf):
                with contextlib.redirect_stderr(ebuf):
                    code = main(argv)
            return code, buf.getvalue(), ebuf.getvalue()

        parts_path = put("parts.geojson", coll)
        pts_path = put("points.geojson", {
            "type": "FeatureCollection", "features": [
                {"type": "Feature", "id": "live-1",
                 "properties": {"LIFECYCLE": "CURRENT"},
                 "geometry": {"type": "Point",
                              "coordinates": [10.04015, 0.00015]}},
                {"type": "Feature", "id": "city-1",
                 "properties": {"LIFECYCLE": "CURRENT"},
                 "geometry": {"type": "Point",
                              "coordinates": [10.0501, 0.00002]}},
                {"type": "Feature", "properties": {}, "geometry": None}]})
        mask_path = put("mask.geojson", {
            "type": "Feature", "properties": {},
            "geometry": {"type": "Polygon", "coordinates": [
                box(10.0499, -0.0001, 0.0004, 0.0003)]}})
        out_path = os.path.join(tmp, "kept.geojson")
        base = [parts_path, "--points", pts_path, "--min-area", "10000"]

        code, out_text, _ = cli(base)
        check(code == 0 and "KEEP_POINTS  2" in out_text,
              "a CLI run classifies the parts and exits 0")
        check("1 with no geometry, ignored" in out_text,
              "a point feature with no geometry is counted, not hidden")
        check(not os.path.exists(out_path), "a run with no --out writes nothing")

        code, out_text, _ = cli(base + ["--out", out_path])
        check(code == 0 and "Check only" in out_text
              and not os.path.exists(out_path),
              "--out without --apply writes nothing at all  <-- pinned defect")

        code, out_text, _ = cli(base + ["--mask", mask_path, "--status-field",
                                        "LIFECYCLE", "--status-value", "CURRENT",
                                        "--out", out_path, "--apply"])
        with open(out_path) as handle:
            written = json.load(handle)
        check(code == 0 and "wrote %s" % out_path in out_text,
              "--out with --apply writes the kept parts")
        check(written["features"][0]["geometry"]["coordinates"]
              == [holed_mp["coordinates"][0], holed_mp["coordinates"][2]],
              "the written file holds exactly the kept parts")
        check("mask      1 polygon(s)" in out_text,
              "the mask is counted in the report")

        os.remove(out_path)
        code, out_text, err_text = cli(base + ["--status-field", "LIFECYCLE",
                                               "--status-value", "Current",
                                               "--out", out_path, "--apply"])
        check(code == 3 and "REFUSED" in out_text
              and "VERDICT: REFUSED" in out_text,
              "a status filter that matches nothing exits 3  <-- pinned defect")
        check(not os.path.exists(out_path),
              "and writes nothing, even with --apply  <-- pinned defect")
        check("KEEP_SIZE" not in out_text,
              "and prints no classification at all")

        empty_mask = put("empty-mask.geojson",
                         {"type": "FeatureCollection", "features": []})
        code, out_text, _ = cli(base + ["--mask", empty_mask])
        check(code == 3 and "mask holds no polygon" in out_text,
              "a mask with no polygon is refused, not read as no mask"
              "  <-- pinned defect")

        code, _, err_text = cli(base + ["--out", parts_path, "--apply"])
        check(code == 64 and "input" in err_text,
              "--out naming an input file is a usage error  <-- pinned defect")
        with open(parts_path) as handle:
            check(json.load(handle) == coll, "and the input is untouched")

        code, _, err_text = cli(base + ["--out", tmp, "--apply"])
        check(code == 2 and "could not write" in err_text,
              "an --out that cannot be written exits 2")

        code, _, err_text = cli([parts_path, "--points", pts_path])
        check(code == 64 and "--min-area" in err_text,
              "a run with no threshold is a usage error, there is no default")
        code, _, err_text = cli([parts_path, "--min-area", "5"])
        check(code == 64 and "--points" in err_text,
              "a run with no point layer is a usage error")
        code, _, err_text = cli(["--points", pts_path, "--min-area", "5"])
        check(code == 64, "a run with no parts layer is a usage error")
        code, _, err_text = cli(base + ["--status-field", "LIFECYCLE"])
        check(code == 64 and "together" in err_text,
              "a status field with no value is a usage error")
        code, _, err_text = cli(base + ["--status-value", "CURRENT"])
        check(code == 64, "a status value with no field is a usage error")
        code, _, err_text = cli(base + ["--apply"])
        check(code == 64 and "--apply needs --out" in err_text,
              "--apply with no --out is a usage error")
        code, _, err_text = cli(base + ["--sample", "-1"])
        check(code == 64, "a negative --sample is a usage error")
        code, _, err_text = cli([parts_path, "--points", pts_path,
                                 "--min-area", "-5"])
        check(code == 64 and "positive" in err_text,
              "a negative threshold is a usage error")

        code, _, err_text = cli([os.path.join(tmp, "absent.geojson"),
                                 "--points", pts_path, "--min-area", "5"])
        check(code == 2, "a parts file that does not exist exits 2")
        bad = put("bad.geojson", "{not json")
        code, _, err_text = cli([bad, "--points", pts_path, "--min-area", "5"])
        check(code == 2 and "bad.geojson" in err_text,
              "a file that is not JSON exits 2 and names the file")
        proj = put("projected.geojson", {"type": "Polygon", "coordinates": [
            box(500000.0, 1700000.0, 100.0, 100.0)]})
        code, _, err_text = cli([proj, "--points", pts_path, "--min-area", "5"])
        check(code == 2 and "EPSG:4326" in err_text,
              "a projected parts layer exits 2 and says to export in WGS 84"
              "  <-- pinned defect")
        bom = os.path.join(tmp, "bom.geojson")
        with io.open(bom, "w", encoding="utf-8-sig") as handle:
            handle.write(json.dumps(coll))
        code, out_text, _ = cli([bom, "--points", pts_path,
                                 "--min-area", "10000"])
        check(code == 0 and "KEEP_POINTS  2" in out_text,
              "a file with a UTF-8 BOM reads the same as one without")
        slivers = put("slivers.geojson", {"type": "Polygon", "coordinates": [
            box(10.06, 0.0, 0.0001, 0.0001)]})
        far_path = put("far.geojson", {"type": "Point",
                                       "coordinates": [50.0, 50.0]})
        code, out_text, _ = cli([slivers, "--points", far_path,
                                 "--min-area", "10000"])
        check(code == 3 and "result would be empty" in out_text,
              "a run that would empty the layer exits 3")
        check("no geometry" not in out_text,
              "and a point layer with every geometry present reports none "
              "missing")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # --- importing the module runs nothing
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        loaded = runpy.run_path(os.path.abspath(__file__),
                                run_name="sliverkeep_imported")
    check(buf.getvalue() == "" and "main" in loaded,
          "importing the module defines main and runs nothing")

    # --- the harness itself
    mark = len(failed)
    saved = sys.stdout
    sys.stdout = io.StringIO()
    try:
        check(False, "probe: a false condition must be recorded as a failure")
        raises(lambda: None, "probe: a call that raises nothing must fail")
        raises(lambda: [][0], "probe: the wrong exception must fail")
        probe_refusal = refusal(lambda: None)
        probe_exits = exits(lambda: None)
        probe_summary = _summary(3, ["probe"])
    finally:
        sys.stdout = saved
    probe = failed[mark:]
    del failed[mark:]
    check(len(probe) == 3,
          "check() and raises() really do record a failure  <-- pinned defect")
    check(probe_refusal is None and probe_exits is None,
          "refusal() and exits() report a call that did neither")
    check(probe_summary == 1, "a run with a failure exits 1")

    print("-" * 68)
    return _summary(passed[0] + len(failed), failed)


def _summary(total, failed):
    """Print the footer and return the self-test's exit code."""
    if failed:
        print("%d assertions, %d failed" % (total, len(failed)))
        for f in failed:
            print("  FAILED: %s" % f)
        return 1
    print("%d assertions, 0 failed" % total)
    return 0


# ----------------------------------------------------------------------- cli

def read_json(path):
    # utf-8-sig, because some exporters write a UTF-8 BOM and json.load
    # refuses one.
    with io.open(path, "r", encoding="utf-8-sig") as handle:
        try:
            return json.load(handle)
        except ValueError as exc:
            # json's own message has no file name in it.
            raise InputError("%s: not JSON: %s" % (path, exc))


def same_file(a, b):
    return (os.path.normcase(os.path.realpath(a))
            == os.path.normcase(os.path.realpath(b)))


class _Parser(argparse.ArgumentParser):
    """argparse exits 2 on a bad flag, and 2 here means an unreadable file.
    A scheduled job must be able to tell the two apart, so this exits 64."""

    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(64, "%s: error: %s\n" % (self.prog, message))


def _parse(argv):
    ap = _Parser(
        prog="sliverkeep.py",
        description="Drop sliver parts from a GeoJSON polygon layer, but keep "
                    "every part that holds an address point.",
        epilog="All layers are GeoJSON in WGS 84 longitude/latitude. Nothing "
               "is written without --apply.",
        allow_abbrev=False,
    )
    ap.add_argument("parts", nargs="?", metavar="PARTS",
                    help="the polygon layer to clean, as GeoJSON")
    ap.add_argument("--points", help="the address points, as GeoJSON")
    ap.add_argument("--min-area", dest="min_area", type=float,
                    help="parts under this many square metres are slivers "
                         "unless an address point lies inside them. Required.")
    ap.add_argument("--mask",
                    help="polygons, as GeoJSON, whose points are not evidence: "
                         "the ground the parts layer was cut from")
    ap.add_argument("--status-field", dest="status_field",
                    help="a property on the points to filter on")
    ap.add_argument("--status-value", dest="status_values", action="append",
                    default=[],
                    help="a value of --status-field that keeps a point. "
                         "Repeat for more than one. Every value must match "
                         "at least one point, or the run is refused.")
    ap.add_argument("--sample", type=int, default=DEFAULT_SAMPLE,
                    help="how many parts to list under each heading "
                         "(default %d)" % DEFAULT_SAMPLE)
    ap.add_argument("--out", help="path for the cleaned GeoJSON")
    ap.add_argument("--apply", action="store_true",
                    help="write --out. Without this nothing is written.")
    ap.add_argument("--self-test", dest="self_test", action="store_true",
                    help="run the offline assertions and exit")
    return ap.parse_args(argv)


def _usage(message):
    print("error: %s" % message, file=sys.stderr)
    return 64


def main(argv=None):
    args = _parse(sys.argv[1:] if argv is None else argv)

    if args.self_test:
        return self_test()

    if not args.parts:
        return _usage("a parts layer is required. Use --self-test to verify "
                      "the tool without one.")
    if not args.points:
        return _usage("--points is required: the address points are the "
                      "evidence that keeps a small part.")
    if args.min_area is None:
        return _usage("--min-area is required. There is no default, because "
                      "no threshold is right for every layer.")
    if bool(args.status_field) != bool(args.status_values):
        return _usage("--status-field and --status-value go together.")
    if args.sample < 0:
        return _usage("--sample cannot be negative.")
    if args.apply and not args.out:
        return _usage("--apply needs --out.")
    if args.out and any(same_file(args.out, p) for p in
                        (args.parts, args.points, args.mask) if p):
        return _usage("--out names an input file. Write the result "
                      "somewhere else.")

    try:
        parts_doc = read_json(args.parts)
        parts = load_parts(parts_doc, args.parts)
        points, no_geometry = load_points(read_json(args.points), args.points)
        mask = (load_mask(read_json(args.mask), args.mask)
                if args.mask else [])
    except (IOError, OSError, ValueError) as exc:
        # ValueError covers both a file that is not JSON and an InputError:
        # a layer that parsed but is not usable is an unreadable input too.
        print("error: %s" % exc, file=sys.stderr)
        return 2

    features = len(features_of(parts_doc, args.parts))
    print("sliverkeep: %s %d feature(s), %d part(s); %s %d point(s)"
          % (args.parts, features, len(parts), args.points, len(points)))
    if no_geometry:
        print("points    %d with no geometry, ignored" % no_geometry)

    try:
        if args.mask and not mask:
            raise Refused(
                "the mask holds no polygon, so it would exclude nothing",
                ["%s was named as the mask and is empty. Every point inside "
                 "it would have counted as evidence. Nothing was written."
                 % args.mask])
        report = run(parts, points, mask, args.min_area, args.status_field,
                     args.status_values)
    except Refused as exc:
        print("REFUSED   %s" % exc.reason)
        for d in exc.details:
            print("      %s" % d)
        print("")
        print("VERDICT: REFUSED")
        return 3
    except ValueError as exc:
        return _usage(str(exc))

    for line in describe(report, args.sample):
        print(line)

    if args.out:
        if args.apply:
            try:
                with io.open(args.out, "w", encoding="utf-8") as handle:
                    json.dump(filtered_doc(parts_doc, parts), handle)
            except (IOError, OSError) as exc:
                print("error: could not write %s: %s" % (args.out, exc),
                      file=sys.stderr)
                return 2
            print("\nwrote %s" % args.out)
        else:
            print("\nCheck only. %s was not written. Re-run with --apply."
                  % args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
