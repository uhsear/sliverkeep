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
PASS  a part whose area equals the threshold is kept for its size  <-- pinned defect
...
PASS  points that lie outside the parts layer's extent refuse the run, with both extents shown  <-- pinned defect
...
PASS  a feature whose MultiPolygon is empty had nothing dropped and stays  <-- pinned defect
...
PASS  the README's first demo run prints exactly what it quotes
PASS  and the second, with the status filter and the mask
...
PASS  and the refusal prints exactly what the README quotes
...
PASS  --out naming a hard link to an input file is a usage error  <-- pinned defect
...
PASS  check() and raises() really do record a failure  <-- pinned defect
PASS  refusal() and exits() report a call that did neither
PASS  a run with a failure exits 1
--------------------------------------------------------------------
173 assertions, 0 failed
```

The full run prints all 173 assertions. Each `...` line above is where this block is cut.

## Requirements

Python 3.9 or newer and nothing else. No `arcpy`, no third-party package, no network, no
database. Every layer is a GeoJSON file.

The self-test prints 173 assertions, 0 failed, on Windows (Python 3.13 and 3.9.25) and on Ubuntu
(Python 3.12.3). The counts are equal on all three. The demo runs below give the same output on
all of them, except for line endings.

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
| `--out` | off | The path for the cleaned layer. The file is written only with `--apply`. Without it, the run prints a `Check only` line. |
| `--apply` | off | Actually write `--out`. Without it, nothing is written. |
| `--self-test` | off | Run the assertions and exit. |

There is no default threshold, because no single threshold is right for every layer. 10,000 m2
is one hectare, or about 2.47 acres.

Every flag must be spelled in full. The parser sets `allow_abbrev=False`, so `--ap` is an error
and never a short form of `--apply`. The self-test pins `--ap` and `--poi` to exit 64.

## What it does

Each polygon part of each feature gets one verdict:

| Verdict | When |
|---|---|
| `KEEP_SIZE` | The part's area is at or over `--min-area`. |
| `KEEP_POINTS` | The part is under `--min-area`, and at least one address point lies inside it and outside the mask. |
| `DROP` | The part is under `--min-area`, and no such point lies inside it. |

Only parts under the threshold are tested against the points. A hole is part of its polygon and
is never dropped on its own. A point in a hole is not inside that part.

### The demo layers

The demo layers are synthetic. The parts layer is one MultiPolygon with six parts. One part is
1.10x under the threshold and one is 1.10x over it. One small pocket holds a live address, and
two shards lie along the edge of the removed area. One address is retired, and one lies in the
removed area.

The layers are not files in this repository. `demo_layers()` in `sliverkeep.py` builds them.
The next command writes them to the current directory:

```
python -c "import json, sliverkeep as s; [json.dump(d, open(n, 'w')) for n, d in zip(('parts.geojson', 'addresses.geojson', 'removed.geojson'), s.demo_layers())]"
```

The self-test builds the same layers and compares the output of the first, second and refusal
runs below with this text, line by line. The `Check only` line is also checked.

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
part's edge or vertex counts as inside the part. In both cases the tool takes the answer that
keeps the part, because a wrongly kept shard costs a few square metres, and a wrongly dropped
pocket costs a household.

**The output.** With `--out` and `--apply`, the tool writes the parts layer again without the
`DROP` parts. Every feature keeps its `id`, its properties and any foreign members. A Polygon
stays a Polygon and a MultiPolygon stays a MultiPolygon. A feature whose every part is dropped
leaves the collection. A feature that never held a part, such as one with a null geometry or an
empty MultiPolygon, stays as it came. The coordinates are copied as they are: the tool does not
round them and does not rewind them. The tool builds the whole output text before it opens
`--out`, so a document that cannot be written, such as one with a `NaN` in a foreign member,
exits 2 and leaves an existing `--out` file as it was.

```
$ python sliverkeep.py parts.geojson --points addresses.geojson --min-area 10000 --mask removed.geojson --status-field LIFECYCLE --status-value CURRENT --out kept.geojson
...
Check only. kept.geojson was not written. Re-run with --apply.
```

**Printing.** A point id or a status value can hold any text. The tool prints it as Python
`ascii()` escapes: a Cyrillic letter prints as `\u0414`, and an escape code prints as `\x1b`.
The report is then plain ASCII, so it cannot fail on a console code page and cannot move the
cursor.

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

**A value that matches some points but misses others by case.** This run does not refuse,
because the data may really hold both spellings. It names the points it did not count. Here
`a-101` was written `Current`, and its pocket is dropped:

```
$ python sliverkeep.py parts.geojson --points mixed.geojson --min-area 10000 --mask removed.geojson --status-field LIFECYCLE --status-value CURRENT
sliverkeep: parts.geojson 1 feature(s), 6 part(s); mixed.geojson 5 point(s)
status    LIFECYCLE = 'CURRENT' matches 3 of 5 point(s); not counted, differs only in case: 'Current' 1
...
VERDICT: keep 2 part(s), drop 4 sliver(s) totalling 11301.5 m2.
```

`mixed.geojson` is `addresses.geojson` with the first `LIFECYCLE` changed to `Current`:

```
python -c "import json; d = json.load(open('addresses.geojson')); d['features'][0]['properties']['LIFECYCLE'] = 'Current'; json.dump(d, open('mixed.geojson', 'w'))"
```

**Points that cannot reach the parts.** When some part is under the threshold, at least one
evidence point must lie inside the bounding box of the whole parts layer. A layer written
latitude first, or a point layer for another area, passes the longitude and latitude range
check, but no point could reach any part. Without this refusal, every small part is dropped, and
the run still exits 0. `swapped.geojson` is `addresses.geojson` with each coordinate pair
reversed:

```
$ python sliverkeep.py parts.geojson --points swapped.geojson --min-area 10000 --out kept.geojson --apply
sliverkeep: parts.geojson 1 feature(s), 6 part(s); swapped.geojson 5 point(s)
REFUSED   no evidence point lies inside the extent of the parts layer
      parts extent: longitude 10.00000 to 10.05040, latitude 0.00000 to 0.02000
      point extent: longitude 0.00020 to 0.03000, latitude 10.00000 to 10.05020
      A layer written latitude first, or a point layer for another area, would drop every small part. Nothing was written.

VERDICT: REFUSED
```

```
python -c "import json; d = json.load(open('addresses.geojson')); [f['geometry']['coordinates'].reverse() for f in d['features']]; json.dump(d, open('swapped.geojson', 'w'))"
```

The check uses only the points the status filter keeps. It is skipped when no part is under the
threshold, because the points then change nothing.

The tool also refuses these:

- a status field that no point carries;
- a point layer with no points;
- a `--mask` file with no polygon in it, because an empty mask excludes nothing;
- a run in which every part would be dropped, because the result would be an empty layer.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | The parts were classified. With `--out --apply`, the file was written. |
| 2 | A file could not be read or written, is not JSON, is nested too deep to read, is not GeoJSON the tool can use, has point properties that are not an object, or is not in longitude and latitude. |
| 3 | Refused. Nothing was written. |
| 64 | Usage error, including a flag the parser does not know, such as `--ap`, and an `--out` that names an input file. |

argparse exits 2 on a flag error by default. Here, 2 means a bad file, so the parser exits 64
instead. A scheduled job can then tell "the command line is wrong" from "the input is bad".

`--out` may not name the parts file, the point file or the mask file. The tool compares files
with `os.path.samefile`. The self-test pins a hard link to an input and a path with a `..`
segment as that input. The tool also stops a run when `--out` is the point file or the mask file.

## The alternatives

The Esri tool for this job is Eliminate Polygon Part (Data Management). It deletes parts or
holes under an area, or under a percentage of the feature's outer area, or both. By default it
deletes only parts that other parts contain completely (`CONTAINED_ONLY`). If every part of a
feature is under the size, it keeps the largest one. It is fast, it runs inside Pro, and for
noise that is clearly smaller than every real part, it is the right tool.

Eliminate (Data Management) merges each selected polygon into the neighbour with the longest
shared border or the largest area. You choose which polygons it eliminates with a selection. The
Esri page says that selection comes from Select Layer By Attribute or Select Layer By Location.
Select Layer By Location has a `Contains` relationship, which selects the polygons that contain a
selecting feature. So a no-code path exists inside Pro: select the small polygons, remove from
the selection the ones that contain an address point, and run Eliminate. When the evidence is one
clean point layer and you want each dropped part merged into a neighbour, that path is the right
one, and sliverkeep does not replace it.

On GitHub, AppGeo/sliver-remover is a JavaScript function that removes thin spikes from one
ring, by angle and distance. zhangyhrs/ArcGIS-Sliver-Polygon-Tools is an ArcMap toolbox that
finds slivers with scale and shape conditions (area, width, compactness and others). It then
merges the slivers you have reviewed into a neighbour. Its manual review step is the right
instinct. andreytyu/eliminate-sliver-polygons, an arcpy Eliminate reproduction marked under
construction, and saeedrajaee/Sliver, a notebook that deletes very small polygons, also work
from the polygon layer. I searched GitHub on 2026-10-13 with `gh search repos` for "sliver
polygon", "eliminate sliver polygons" and "remove slivers polygon". Those four were the
relevant results, and none of their readmes or code that I read takes a point layer. That search
is not proof that no such tool exists.

The Esri pages describe none of these checks, and I found none of them in the tools above:

- a status filter that is checked against the values the field holds, so that a typo refuses
  the run instead of removing the evidence;
- a mask that stops an address in the removed ground from counting as evidence;
- a report of the size gap, so that a threshold drawn through a continuum is said in words.

Those three checks, and a plain-text report that runs without `arcpy`, are what sliverkeep adds
to the selection rule. It does not merge, does not compute shape metrics, and does not replace a
size threshold. It decides only which parts under your threshold to keep.

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
- A ring that crosses itself, such as a bow tie, gets a signed area in which the two lobes
  partly cancel. An invalid part can then look smaller than it is. The tool does not check ring
  topology.
- It tests every point against every small part, after a bounding-box check, with no spatial
  index. The self-test does not time it. One run on the Windows workstation took 11.6 to
  12.3 s in three tries, with 300 square parts of 0.0003 degrees and 300,000 uniform random
  points, no mask and no status filter. An earlier audit run measured 14.9 to 19.9 s on the same
  machine. Expect tens of seconds at this size, and more as parts times points grows. The run
  prints no progress.
- GeoJSON only. No geodatabase, no shapefile, no SDE connection. Reading a geodatabase needs
  `arcpy`, and the tool would then stop running anywhere else.
- No projected CRS. A coordinate outside -180 to 180 longitude or -90 to 90 latitude is
  refused, which catches a file that Features To JSON wrote with `KEEP_INPUT_SR` from a
  projected layer. A geographic CRS other than WGS 84, such as NAD83, is not detected: the tool
  does not read the `crs` member and treats the coordinates as WGS 84.
- The extent refusal catches longitude and latitude swapped in one layer. It does not catch
  them swapped in both layers. The areas are then wrong, and nothing reports an error.
- With no `--status-field`, every point is evidence, including retired ones, and the run does
  not warn. The tool also does not notice a status column that holds one value on every point.
  [idreuse](https://github.com/uhsear/idreuse) refuses that case.
- A status value is compared as text. A numeric property `1.0` does not match
  `--status-value 1`, and `True` matches where `true` does not. The run is then refused and lists
  the values it saw, so it is safe but can surprise you.
- A `Point` or `Polygon` with `"coordinates": []` is an empty geometry that RFC 7946 section 3.1
  allows a reader to treat as null. The tool refuses it with exit 2. A MultiPolygon with `[]` is
  accepted, so the two are not consistent.
- A parts layer with no polygon at all, such as an empty FeatureCollection, reports
  `keep 0 part(s), drop 0 sliver(s)` and exits 0. With `--apply` it writes an equivalent empty
  layer, because the tool cannot tell an empty export from a deliberate empty input.
- It does not rewind rings. RFC 7946 section 3.1.6 asks for counter-clockwise exterior rings
  and clockwise holes. The output keeps whatever winding the input had. Use
  [ringwind](https://github.com/uhsear/ringwind) to check or fix the winding.
- It does not merge a dropped part into a neighbour. A dropped part is removed. If the parts
  must tile a surface without gaps, use Eliminate instead.
- It reads all three files into memory.
- `--out` is written in place, not through a temporary file. A full disk or a killed run can
  leave a partial file. The input files are never opened for writing.
- Nothing is written without `--apply`, with one exception. `--self-test` makes a temporary
  directory for its fixtures and removes it again. A refused run writes nothing at all.
- A file name is printed as given. A name with a character outside the console code page can
  still raise `UnicodeEncodeError` when the output is piped. Point ids and status values are
  escaped and cannot.
- It opens no socket and imports no network module. There is no network path to test.
- Branch coverage of `sliverkeep.py --self-test` is 100% (842 statements, 200 branches),
  measured with `coverage run --branch`. No line is excluded.

## Sources

- Esri, Eliminate Polygon Part (Data Management), ArcGIS Pro tool reference:
  https://doc.esri.com/en/arcgis-pro/latest/tool-reference/data-management/eliminate-polygon-part.html
- Esri, Eliminate (Data Management), ArcGIS Pro tool reference. The usage notes say the selection
  comes from Select Layer By Attribute or Select Layer By Location:
  https://doc.esri.com/en/arcgis-pro/latest/tool-reference/data-management/eliminate.html
- Esri, Select Layer By Location (Data Management), ArcGIS Pro tool reference. The `Contains`
  relationship:
  https://doc.esri.com/en/arcgis-pro/latest/tool-reference/data-management/select-layer-by-location.html
- Esri, Features To JSON (Conversion), ArcGIS Pro tool reference:
  https://doc.esri.com/en/arcgis-pro/latest/tool-reference/conversion/features-to-json.html
- RFC 7946, The GeoJSON Format, sections 3.1, 3.1.1, 3.1.6, 3.2 and 4:
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
- [idreuse](https://github.com/uhsear/idreuse) - refuse a status column that holds one value on
  every row. That is a related check: it finds a status column that cannot separate a live
  record from a retired one, where sliverkeep refuses a status value that matches no point.
