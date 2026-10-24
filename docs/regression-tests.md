# Regression tests

The search is deterministic: for the same data and code, every number below is reproduced exactly. Run the tests that cover what you changed and compare the output with the expected values.

## How to run
Keep test runs out of the real history and map:

```
python findaline.py <arguments> --no-plot --yes --runs-dir <temp>/runs --html <temp>/latest.html
```

- `<temp>` is any folder outside the repository. The data cache (`cache/`) can be shared; use `--cache-dir <temp>/cache` (with `--refresh` where noted) only when testing download code.
- Compare the lines printed by the script: `Found N potential anchors`, `Anchors with an open direction: N`, `Checked pixel by pixel: N`, `Found N valid lines in M spots`, and the `Filled ... km²` / `filling sea and lakes ... sea X km²` lines.
- Counts of lines over water come from `lines.csv` of the run (`water_m > 0`; "spots" = rows with `representative == True`).

## When a number differs
1. A difference after a code change that should not affect results is a regression.
2. If the change is intended, check a few lines on the map (profile, orthophoto), then update the table in the same commit.
3. The remote data can change too: geoportal.gov.pl and IGN republish surveys, OpenStreetMap coastlines are edited, Nominatim names change. If a test fails without a code change, check the data first (`--refresh` on a scratch cache) and re-baseline.
4. The exact centre matters: grid snapping and the circle mask make a centre moved by a fraction of a metre change a few lines at the edge. Always use the coordinates below as written.

## Search tests
Baseline: 2026-10-23, commit `26c02a3`; open anchors and checked pairs from the numba search (2026-10-24), which gave the same lines and spots everywhere. Defaults unless listed: radius 1000 m, resolution of the source, slope ≥45°, lines 30–1000 m, anchors at most 5% / 50 m apart in height, `--nmpt mark`, `--model formula`, `--water fill`, `--fill-gaps on`.

| ID | Place / what it covers | Arguments | Anchors | Open anchors / checked pairs | Lines / spots | Also check |
| --- | --- | --- | --- | --- | --- | --- |
| R1 | Żabi Koń, Poland: geoportal source, defaults | `--lat 50.1559508 --lon 19.7562722` | 8 371 | 1 000 / 9 627 | 691 / 66 | Spot 1: 102 m, clearance 4.6 m, 50.156485, 19.755093 → 50.155991, 19.756290; no coastline lookup (inland) |
| R2 | Dolina Bolechowicka, Poland: 1 m grid | `--lat 50.1528584 --lon 19.7846212 --resolution 1 --min-length 15 --max-length 2000 --slope-threshold 60` | 4 035 | 411 / 1 674 | 401 / 19 | `Coastline: no sea in the area` |
| R3 | as R2 with the sag estimator | R2 + `--model sag` | 4 035 | 1 841 / 69 301 | 9 188 / 100 | |
| R4 | Dolina Będkowska, Poland: gap filling from FABDEM | `--lat 50.1733209 --lon 19.7409174 --resolution 1 --min-length 15 --max-length 2000 --slope-threshold 60` | 18 756 | 2 278 / 22 346 | 1 175 / 47 | `Filled 0.05 km² of gaps ... from FABDEM + Copernicus DEM` |
| R5 | as R4 with the sag estimator | R4 + `--model sag` | 18 756 | 9 735 / 1 158 235 | 42 052 / 357 | map ~150 MB |
| R6 | Dingli cliffs, Malta 2018: sea filling, lines over water | `--lat 35.8565 --lon 14.3830` | 12 865 | 4 958 / 218 849 | 10 928 / 366 | sea 0.42 km²; 344 spots over water |
| R7 | as R6 with Malta 2012: georeference correction, gap filling from 2018 | R6 + `--source malta2012` | 10 550 | 3 389 / 65 564 | 6 783 / 381 | `Filled 0.19 km² ... from Malta 2018`; sea 0.43 km²; 376 spots over water |
| R8 | Dingli – zatoka (centre over the sea) | `--lat 35.848778 --lon 14.383533 --slope-threshold 60` | 10 697 | 6 646 / 262 091 | 11 328 / 423 | sea 1.94 km²; 366 spots over water |
| R9 | Montserrat, Spain: IGN WCS (ASCII grid) | `--lat 41.5930 --lon 1.8100 --min-length 40` | 27 401 | 20 979 / 549 299 | 86 029 / 3 993 | source `IGN Spain (MDT05/MDS05)` |
| R10 | Los Gigantes, Tenerife: Canary Islands source, sea at 0 m in the data | `--lat 28.2640 --lon -16.8460 --radius 800 --min-length 40` | 14 711 | 11 654 / 196 868 | 34 697 / 2 050 | source `IGN Canary Islands`; 15 002 lines with `water_m > 0` |
| R11 | Meteora, Greece: Europe-wide FABDEM + Copernicus, 30 m | `--lat 39.7217 --lon 21.6306 --radius 2000 --min-length 60` | 257 | 257 / 887 | 451 / 82 | grouping distance 60 m |
| R12 | Meteora with NASADEM (needs `OPENTOPOGRAPHY_API_KEY`) | R11 + `--source nasadem` | 433 | 433 / 2 137 | 930 / 159 | |
| R13 | Quick smoke test, Kasprowy Wierch (~1 min with cache) | `--northing 152074 --easting 571448 --radius 500 --resolution 5 --slope-threshold 50 --min-length 50 --max-length 400` | | | 2 370 / 8 838 | PL-1992 input; with `--refresh --cache-dir <temp>/cache` it also tests the geoportal WCS download (NMT GeoTIFF, NMPT multipart ASCII) |

Suggested sets:
- Any change: R13.
- Line search, clearance, grouping: R1, R2, R3.
- Data sources, download, mosaics: R1, R7, R9, R10, R11 (and R13 with `--refresh`).
- Water, coastline, gap filling: R4, R6, R7, R8, R10.

## Component checks
**Sea mask (coastline sources).** On a 2 m grid in EPSG:32633 with radius 1000 m around 35.8565, 14.3830, `coast.sea_mask` gives 0.433 km² of sea with both `world` and `overpass` (identical pixels) and 0.440 km² with `copernicus`. Against the LiDAR shoreline there, 90% of the OSM coastline is within 13 m, of the Copernicus mask within 70 m.

**Malta 2012 georeference.** With `MALTA_2012_SHIFT` applied, the 2012 DTM matches Copernicus GLO-30 with a median absolute difference of about 0.5 m and the 2018 DTM with 0.19 m (radius 1500 m, 10 m grid, Dingli). Without the shift it is 9 m. Do the same shift search (move one model ±200 m, minimise the median absolute difference) for any new national source.

**Pair search.** `linesearch.py` must find exactly the lines of the plain search (every pair within the length and height-difference limits checked with `helpers.evaluate_line`). After changing it, compare `lines.csv` of R1 and R3 with `--nmpt reject` against a run of the plain search: sorted, the files must be identical. The plain search is in git history before `linesearch.py` was added (`helpers.find_valid_lines`); run that version from a `git archive` copy with `--cache-dir` pointing at the real cache.

**Sag estimator.** Defaults (2.5 kN, 5% at 10 kN, 70 g/m, 80 kg), span 100 m, no height difference: unstretched length 98.768 m, sag without athlete 0.339 m, sag with the athlete at the maximum of the envelope 5.521 m; span 1000 m: envelope maximum 71.18 m. These match an independent spring-mass chain (200 segments, minimised energy) to the millimetre. The JavaScript copy in `mapping.py` must give the same values to 1e-6 m: extract the `var Sag = (function() {...})();` block from a generated map and run it with Node for spans 30, 100, 300 (dh −40), 1000 m and 100 m with dh 100.

**Browser.** For the same settings, the spot count shown by the map panel equals the one printed by Python (grouping in JS mirrors `group_lines`). Clicking a line leaves one line plus its thin variants; closing the popup or "Show all lines" restores all; "Zoom to line" fits the line above the profile panel. Check with a copy of a small map plus a test script, in headless Edge (`--dump-dom`).
