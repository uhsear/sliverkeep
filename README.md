# sliverkeep

Drop the sliver parts from a GeoJSON polygon layer, but keep every part that holds an address
point. Refuse a status filter that matches no point.

A team needed a service-area polygon for a field app's geofence: the whole jurisdiction minus the
cities inside it. They subtracted the city polygons from the outer boundary. The two layers had
been digitized separately and their shared edges did not agree, so the subtraction threw off
shards along every shared edge. Most of the parts in the result were shards, and the smallest
was a few square feet.

The first cleanup was the usual one: drop every part under an area threshold. A review of that
cut found two problems. The largest part it dropped and the smallest part it kept were only
1.10x apart in size, so the threshold did not fall in a gap. It cut through a continuum. And
the dropped parts held about a dozen live address points. Some of them were real
unincorporated pockets that a city's annexations had pinched off from the rest. The geofence
would have put those households outside the service area.

The fix was to drop a small part only when no live address lies inside it. That fix has a
second trap. "Live" is a filter on a status column. If the filter value is spelled differently
from the data (a change of case, or a renamed domain code), the filter selects no points. The
rescue test then rescues nothing, every small part is dropped, and nothing reports an error.

```
$ python sliverkeep.py --self-test
sliverkeep self-test: no network, no credentials
--------------------------------------------------------------------
PASS  a one-degree cell at the equator has its exact spherical area
PASS  area does not depend on winding: clockwise gives the same number
...
PASS  a point on an edge counts as inside a part  <-- pinned defect
PASS  and as outside the mask, so it stays evidence  <-- pinned defect
PASS  a vertex counts as the boundary
PASS  a point in a hole is not in the part  <-- pinned defect
...
PASS  a projected easting and northing is refused  <-- pinned defect
...
PASS  an unclosed ring is refused  <-- pinned defect
...
PASS  a status value that matches nothing refuses the run  <-- pinned defect
PASS  and the refusal names the value
PASS  and lists the values the field does hold, with counts
PASS  and says when the miss is only a difference of case  <-- pinned defect
PASS  one value of two that matches nothing still refuses, and only that value is named  <-- pinned defect
...
PASS  and the two are 1.10x apart, as in the story
PASS  parts at or over the threshold are kept for their size
PASS  a small pocket holding a live address is kept  <-- pinned defect
PASS  and names the address that saved it
PASS  a small part whose only address is retired is dropped
PASS  an address inside the mask is not evidence  <-- pinned defect
...
PASS  a threshold drawn through a continuum is said in words  <-- pinned defect
...
PASS  a second run under another filter does not inherit the first run's evidence  <-- pinned defect
...
PASS  a run that would drop every part is refused  <-- pinned defect
...
PASS  a unique prefix --ap of --apply is REFUSED by the parser  <-- pinned defect
PASS  a prefix --poi of --points is refused too
PASS  a parser error exits 64, not the 2 an unreadable file exits  <-- pinned defect
...
PASS  --out without --apply writes nothing at all  <-- pinned defect
...
PASS  a status filter that matches nothing exits 3  <-- pinned defect
PASS  and writes nothing, even with --apply  <-- pinned defect
PASS  and prints no classification at all
PASS  a mask with no polygon is refused, not read as no mask  <-- pinned defect
PASS  --out naming an input file is a usage error  <-- pinned defect
PASS  and the input is untouched
...
PASS  importing the module defines main and runs nothing
PASS  check() and raises() really do record a failure  <-- pinned defect
PASS  refusal() and exits() report a call that did neither
PASS  a run with a failure exits 1
--------------------------------------------------------------------
134 assertions, 0 failed
```

The full run prints all 134 assertions. Each `...` line above is where this block is cut.

## Requirements

Python 3.9 or newer and nothing else. No `arcpy`, no third-party package, no network, no
database. Every layer is a GeoJSON file.

The self-test prints 134 assertions, 0 failed, on Windows (Python 3.13 and 3.9.25) and on Ubuntu
(Python 3.12.3). The demo runs below give the same output on both hosts, except for line
endings.

Every layer must be in WGS 84 longitude and latitude, which is what RFC 7946 section 4 requires
of GeoJSON. In ArcGIS Pro, the Features To JSON tool writes this when you set `geoJSON` to
`GEOJSON` and `outputToWGS84` to `WGS84`. A coordinate outside the longitude and latitude
ranges is refused with exit 2. That catches a file left in a projected CRS.

```
git clone https://github.com/uhsear/sliverkeep.git
```

## Usage

```
python sliverkeep.py --self-test
python sliverkeep.py parts.geojson --points addresses.geojson --min-area 10000
python sliverkeep.py parts.geojson --points addresses.geojson --min-area 10000 \
    --mask removed.geojson --status-field LIFECYCLE --status-value CURRENT
python sliverkeep.py parts.geojson --points addresses.geojson --min-area 10000 \
    --mask removed.geojson --status-field LIFECYCLE --status-value CURRENT \
    --out kept.geojson --apply
```

| Flag | Default | What it does |
|---|---|---|
| `PARTS` | none | The polygon layer to clean. Required unless `--self-test`. |
| `--points` | none | The address points. Required. |
| `--min-area` | none | Parts under this many square metres are slivers, unless a point lies inside them. Required. |
| `--mask` | off | Polygons whose points are not evidence: the ground the parts layer was cut from. |
| `--status-field` | off | A property on the points to filter on. Needs `--status-value`. |
| `--status-value` | off | A value of `--status-field` that keeps a point. Repeat it for more than one value. |
| `--sample` | `10` | How many parts to list under each heading. `0` lists none. |
| `--out` | off | Write the cleaned layer to this path. Needs `--apply`. |
| `--apply` | off | Actually write. Without it, nothing is written. |
| `--self-test` | off | Run the assertions and exit. |

There is no default threshold, because no single threshold is right for every layer. 10,000 m2
is one hectare, or about 2.47 acres.

Every flag must be spelled in full. The parser sets `allow_abbrev=False`, so `--ap` is an error
and never a short form of `--apply`.

## What it does

Each polygon part of each feature gets one verdict:

| Verdict | When |
|---|---|
| `KEEP_SIZE` | The part's area is at or over `--min-area`. |
| `KEEP_POINTS` | The part is under `--min-area`, and at least one address point lies inside it and outside the mask. |
| `DROP` | The part is under `--min-area`, and no such point lies inside it. |

Only parts under the threshold are tested against the points. A hole is part of its polygon and
is never dropped on its own. A point in a hole is not inside that part.

The demo files are synthetic. The parts layer is one MultiPolygon with six parts. One part is
1.10x under the threshold and one is 1.10x over it. One small pocket holds a live address, and
two shards lie along the edge of the removed area.

```
$ python sliverkeep.py parts.geojson --points addresses.geojson --min-area 10000
sliverkeep: parts.geojson 1 feature(s), 6 part(s); addresses.geojson 5 point(s)
threshold parts under 10000.0 m2 are slivers unless an address point lies inside them
parts classified: 6
  KEEP_SIZE    2
  KEEP_POINTS  3
  DROP         1

kept for the points inside them, smallest first:
  feature 0 part 5             99.1 m2  1 point(s): a-104
  feature 0 part 3           1982.7 m2  1 point(s): a-101
  feature 0 part 2           9120.5 m2  1 point(s): a-103

dropped, largest first:
  feature 0 part 4             99.1 m2

size gap: the largest part under the threshold is 9120.5 m2 and the smallest part over it is 10037.5 m2, 1.10x apart. Size alone does not separate them; the points are deciding.

VERDICT: keep 5 part(s), drop 1 sliver(s) totalling 99.1 m2.
```

This run has no status filter and no mask, so every point is evidence. Two of the three kept
parts are kept for the wrong reason: `a-103` is a retired address, and `a-104` lies in the area
that the subtraction removed. Name the live status and the mask, and both of those parts go:

```
$ python sliverkeep.py parts.geojson --points addresses.geojson --min-area 10000 --mask removed.geojson --status-field LIFECYCLE --status-value CURRENT
sliverkeep: parts.geojson 1 feature(s), 6 part(s); addresses.geojson 5 point(s)
status    LIFECYCLE = 'CURRENT' matches 4 of 5 point(s)
mask      1 polygon(s). A point inside them is not evidence.
threshold parts under 10000.0 m2 are slivers unless an address point lies inside them
parts classified: 6
  KEEP_SIZE    2
  KEEP_POINTS  1
  DROP         3

kept for the points inside them, smallest first:
  feature 0 part 3           1982.7 m2  1 point(s): a-101

dropped, largest first:
  feature 0 part 2           9120.5 m2
  feature 0 part 4             99.1 m2
  feature 0 part 5             99.1 m2  (1 point(s) inside, all in the mask)

size gap: the largest part under the threshold is 9120.5 m2 and the smallest part over it is 10037.5 m2, 1.10x apart. Size alone does not separate them; the points are deciding.
points inside a small part but also inside the mask, not counted as evidence: 1

VERDICT: keep 3 part(s), drop 3 sliver(s) totalling 9318.8 m2.
```

The pocket at 1,982.7 m2 is a fifth of the size of a part that was dropped, and it is kept. A
size-only filter at any threshold that drops the 9,120.5 m2 part also drops the pocket.

The size-gap line is printed on every run that has parts on both sides of the threshold. When the
two nearest parts are less than 2x apart, the line says that size alone does not separate them.
That is the 1.10x finding from the story, reported on every run instead of found in review.

**The mask.** A point inside a mask polygon is never evidence. In the story, the mask is the
city layer: an address inside a city does not make a shard on the city's edge part of the
unincorporated area. A point exactly on the mask's edge stays evidence. A point exactly on a
part's edge counts as inside the part. In both cases the tool takes the answer that keeps the
part, because a wrongly kept shard costs a few square metres, and a wrongly dropped pocket costs
a household.

**The output.** With `--out` and `--apply`, the tool writes the parts layer again without the
`DROP` parts. Every feature keeps its `id`, its properties and any foreign members. A Polygon
stays a Polygon and a MultiPolygon stays a MultiPolygon. A feature whose every part is dropped
leaves the collection. The coordinates are copied as they are: the tool does not round them and
does not rewind them.

```
$ python sliverkeep.py parts.geojson --points addresses.geojson --min-area 10000 --mask removed.geojson --status-field LIFECYCLE --status-value CURRENT --out kept.geojson
...
Check only. kept.geojson was not written. Re-run with --apply.
```

## Refusals

A refusal prints the reason, writes nothing (even with `--apply`) and exits 3.

**A status value that matches no point.** This is the second trap from the story.

```
$ python sliverkeep.py parts.geojson --points addresses.geojson --min-area 10000 --mask removed.geojson --status-field LIFECYCLE --status-value Current --out kept.geojson --apply
sliverkeep: parts.geojson 1 feature(s), 6 part(s); addresses.geojson 5 point(s)
REFUSED   the status value 'Current' matches no point
      LIFECYCLE = 'Current' matches 0 of 5 point(s)
      values seen in LIFECYCLE: 'CURRENT' 4, 'RETIRED' 1
      'CURRENT' differs from 'Current' only in case
      Without the points this value should keep, every part they hold would be dropped. Nothing was written.

VERDICT: REFUSED
```

The values are compared after trimming whitespace, and the case must match. Every value you name
must match at least one point. With `--status-value CURRENT --status-value CURREN`, the run is
refused and names `'CURREN'`, even though `CURRENT` matches. A typo in one value of two removes
evidence just as silently as a typo in the only value.

The tool also refuses these:

- a status field that no point carries;
- a point layer with no points;
- a `--mask` file with no polygon in it, because an empty mask excludes nothing;
- a run in which every part would be dropped, because the result would be an empty layer.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | The parts were classified. With `--out --apply`, the file was written. |
| 2 | A file could not be read or written, is not JSON, is not GeoJSON the tool can use, or is not in longitude and latitude. |
| 3 | Refused. Nothing was written. |
| 64 | Usage error, including a flag the parser does not know, such as `--ap`. |

argparse exits 2 on a flag error by default. Here, 2 means a bad file, so the parser exits 64
instead. A scheduled job can then tell "the command line is wrong" from "the input is bad".

## The alternatives

The Esri tool for this job is Eliminate Polygon Part (Data Management). It deletes parts or
holes under an area, or under a percentage of the feature's outer area, or both. By default it
deletes only parts that other parts contain completely (`CONTAINED_ONLY`). If every part of a
feature is under the size, it keeps the largest one. It is fast, it runs inside Pro, and for
noise that is clearly smaller than every real part, it is the right tool.

Eliminate (Data Management) merges each selected polygon into the neighbour with the longest
shared border or the largest area. You choose which polygons it eliminates with a selection.

On GitHub, AppGeo/sliver-remover removes sliver bits from a polygon.
zhangyhrs/ArcGIS-Sliver-Polygon-Tools is an ArcMap toolbox that finds slivers with scale and
shape conditions (area, width, compactness and others). It then merges the slivers you have
reviewed into a neighbour. Its manual review step is the right instinct.

All of them decide from the polygon layer alone: size, shape or neighbours. Neither Esri page
describes a point layer as an input. None of them can know that a small part has a house in it.
sliverkeep adds that one test. It does not merge, does not compute shape metrics, and does not
replace a size threshold. It decides only which parts under your threshold to keep.

## Limits

- Areas are spherical, on a sphere with the WGS 84 semi-major axis (6,378,137 m). The
  formula is the line integral from Chamberlain and Duquette, "Some Algorithms for Polygons on
  a Sphere" (JPL, 2007). The self-test checks it against the closed-form area of a one-degree
  cell. Against the WGS 84 ellipsoid (pyproj `Geod`), the spherical area of a 0.001-degree
  square was 0.67% high at the equator, 0.00% at 45 degrees and 0.63% low at 80 degrees. Set
  the threshold with more margin than that.
- The point-in-polygon test treats longitude and latitude as plane coordinates. For parts the
  size of a sliver or a pocket, that is exact enough. A part that crosses the antimeridian is
  not handled.
- A point on an edge is detected only when it is exactly on the edge, in floating point. A
  point a billionth of a degree off the edge is inside or outside by ray casting.
- It tests every point against every small part, after a bounding-box check, with no spatial
  index. On the Windows workstation, 300 small parts against 300,000 points took about 10
  seconds. The time grows with parts times points.
- GeoJSON only. No geodatabase, no shapefile, no SDE connection. Reading a geodatabase needs
  `arcpy`, and the tool would then stop running anywhere else.
- No projected CRS. A coordinate outside -180 to 180 longitude or -90 to 90 latitude is
  refused, which catches a file that Features To JSON wrote with `KEEP_INPUT_SR` from a
  projected layer. A geographic CRS other than WGS 84, such as NAD83, is not detected: the tool
  does not read the `crs` member and treats the coordinates as WGS 84.
- It does not rewind rings. RFC 7946 section 3.1.6 asks for counter-clockwise exterior rings
  and clockwise holes. The output keeps whatever winding the input had. Use
  [ringwind](https://github.com/uhsear/ringwind) to check or fix the winding.
- It does not merge a dropped part into a neighbour. A dropped part is removed. If the parts
  must tile a surface without gaps, use Eliminate instead.
- It reads all three files into memory.
- Nothing is written without `--apply`. `--out` cannot name an input file. A refused run writes
  nothing at all.
- It opens no socket and imports no network module. There is no network path to test.
- Branch coverage of `sliverkeep.py --self-test` is 100% (704 statements, 184 branches),
  measured with `coverage run --branch`.

## Sources

- Esri, Eliminate Polygon Part (Data Management), ArcGIS Pro tool reference:
  https://doc.esri.com/en/arcgis-pro/latest/tool-reference/data-management/eliminate-polygon-part.html
- Esri, Eliminate (Data Management), ArcGIS Pro tool reference:
  https://doc.esri.com/en/arcgis-pro/latest/tool-reference/data-management/eliminate.html
- Esri, Features To JSON (Conversion), ArcGIS Pro tool reference:
  https://doc.esri.com/en/arcgis-pro/latest/tool-reference/conversion/features-to-json.html
- RFC 7946, The GeoJSON Format, sections 3.1.1, 3.1.6 and 4:
  https://www.rfc-editor.org/rfc/rfc7946.html
- R. G. Chamberlain and W. H. Duquette, "Some Algorithms for Polygons on a Sphere", Jet
  Propulsion Laboratory, 2007.

## Contributing

Open an issue or pull request on GitHub.

## Author

Built by [Asir Khan](https://www.linkedin.com/in/asir-khan-310317264/).

## License

MIT.

## Related

Other single-file tools in this portfolio that pair with this one:

- [ringwind](https://github.com/uhsear/ringwind) - name every GeoJSON ring wound against the
  convention you declared. Run it on the output of this tool before a geofence reads it.
- [geocodesift](https://github.com/uhsear/geocodesift) - find the records stacked on one
  coordinate in a geocoded batch. Run it on the address points before you trust them as
  evidence.
- [idreuse](https://github.com/uhsear/idreuse) - the same refusal of a status column that cannot
  separate a live record from a retired one.
- ng911topo (private, in progress) - find response-area overlaps and coverage gaps, with a
  sliver-width gate that reports what it suppressed.
