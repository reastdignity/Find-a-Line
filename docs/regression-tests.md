# Regression tests

The search is deterministic: for the same data and code, every number below is reproduced exactly. Run the tests that cover what you changed and compare the output with the expected values.

## How to run
`tests/regression.py` runs the searches one after another, with runs and maps in a temporary folder, and compares the results with `tests/regression.json`:

```
uv run tests/regression.py                # quick set (R13)
uv run tests/regression.py --set pl       # sets: quick, pl, search, sources, water
uv run tests/regression.py R1 R7          # chosen tests
uv run tests/regression.py --all          # everything (about 4 min with a filled cache)
uv run tests/regression.py R1 --update --write-doc   # store new results, rewrite the table below
```

- Each test gets its own temporary data folder whose `cache` links to the real cache; `--cache <temp>/cache` uses another one (an empty folder tests the downloads).
- Compared: the source name, `Found N potential anchors`, `Anchors with an open direction: N`, `Checked pixel by pixel: N`, the lines and spots of `lines.csv`, `Filled ... km²` with its source, `sea X km²`, and lines/spots with `water_m > 0`; only the values stored for a test are checked.
- The table below is generated from `tests/regression.json` (`--write-doc`); edit the JSON, not the table. Each test's log stays in the folder printed at the end.
- The offline tests (`uv run pytest`, ~25 s) need no network and no cache: a synthetic canyon and a 600 × 600 m clip of Żabi Koń (`tests/data`, cut by `make_fixture.py`).

## When a number differs
1. A difference after a code change that should not affect results is a regression.
2. If the change is intended, check a few lines on the map (profile, orthophoto), then update the expected values (`--update --write-doc`) in the same commit.
3. The remote data can change too: geoportal.gov.pl publishes new sheets (a newer sheet in the index replaces the cached one), IGN republishes surveys, OpenStreetMap coastlines are edited, Nominatim names change. If a test fails without a code change, check the data first (`python findaline.py cache status`, then `cache update`, or the search on a scratch `--data-dir`) and re-baseline.
4. The exact centre matters: grid snapping and the circle mask make a centre moved by a fraction of a metre change a few lines at the edge. Always use the coordinates below as written.

## Search tests
Baseline: 2026-10-23, commit `26c02a3`; open anchors and checked pairs from the numba search (2026-10-24), which gave the same lines and spots everywhere. Polish tests (R1–R5, R13) re-baselined on 2026-10-25 after the switch to geoportal.gov.pl sheet files (EVRF2007, newest survey) with the working grid aligned to their pixels. Defaults unless listed: radius 1000 m, resolution of the source, slope ≥45°, lines 30–1000 m, anchors at most 5% / 50 m apart in height, `--nmpt mark`, `--model formula`, `--water fill`, `--fill-gaps on`.

<!-- regression-table:start -->
| ID | Place / what it covers | Arguments | Anchors | Open anchors / checked pairs | Lines / spots | Also check |
| --- | --- | --- | --- | --- | --- | --- |
| R1 | Żabi Koń, Poland: geoportal source, defaults | `--lat 50.1559508 --lon 19.7562722` | 8 900 | 1 261 / 13 929 | 1 262 / 104 | source `geoportal.gov.pl`; Spot 1: 102 m, clearance 8.0 m, 50.156507, 19.755129 → 50.155960, 19.756268; no coastline lookup (inland) |
| R2 | Dolina Bolechowicka, Poland: 1 m grid | `--lat 50.1528584 --lon 19.7846212 --resolution 1 --min-length 15 --max-length 2000 --slope-threshold 60` | 4 503 | 437 / 2 340 | 334 / 19 | `Coastline: no sea in the area` |
| R3 | as R2 with the sag estimator | R2 + `--model sag` | 4 503 | 2 060 / 90 196 | 12 009 / 102 |  |
| R4 | Dolina Będkowska, Poland: 1 m grid, many anchors | `--lat 50.1733209 --lon 19.7409174 --resolution 1 --min-length 15 --max-length 2000 --slope-threshold 60` | 20 460 | 2 926 / 37 361 | 1 972 / 68 | no gaps (the WCS data had 0.05 km² filled from FABDEM) |
| R5 | as R4 with the sag estimator | R4 + `--model sag` | 20 460 | 10 485 / 1 322 582 | 52 693 / 454 | map ~150 MB |
| R6 | Dingli cliffs, Malta 2018: sea filling, lines over water | `--lat 35.8565 --lon 14.3830` | 12 865 | 4 958 / 218 849 | 10 928 / 366 | sea 0.42 km²; 344 spots over water |
| R7 | as R6 with Malta 2012: georeference correction, gap filling from 2018 | R6 + `--source malta2012` | 10 550 | 3 389 / 65 564 | 6 783 / 381 | `Filled 0.19 km² ... from Malta 2018 (MSDI)`; sea 0.43 km²; 376 spots over water |
| R8 | Dingli – zatoka (centre over the sea) | `--lat 35.848778 --lon 14.383533 --slope-threshold 60` | 10 697 | 6 646 / 262 091 | 11 328 / 423 | sea 1.94 km²; 367 spots over water |
| R9 | Montserrat, Spain: IGN WCS (ASCII grid) | `--lat 41.5930 --lon 1.8100 --min-length 40` | 27 401 | 20 979 / 549 299 | 86 029 / 3 993 | source `IGN Spain (MDT05/MDS05)` |
| R10 | Los Gigantes, Tenerife: Canary Islands source, sea at 0 m in the data | `--lat 28.2640 --lon -16.8460 --radius 800 --min-length 40` | 14 711 | 11 654 / 196 868 | 34 697 / 2 050 | source `IGN Canary Islands (MDT05/MDS05)`; 15 002 lines with `water_m > 0` |
| R11 | Meteora, Greece: Europe-wide FABDEM + Copernicus, 30 m | `--lat 39.7217 --lon 21.6306 --radius 2000 --min-length 60` | 257 | 257 / 887 | 451 / 82 | grouping distance 60 m |
| R12 | Meteora with NASADEM (needs `OPENTOPOGRAPHY_API_KEY`) | R11 + `--source nasadem` | 433 | 433 / 2 137 | 930 / 159 |  |
| R13 | Quick smoke test, Kasprowy Wierch (~15 s with cache) | `--northing 152074 --easting 571448 --radius 500 --resolution 5 --slope-threshold 50 --min-length 50 --max-length 400` | 2 796 | 2 396 / 8 628 | 562 / 138 | `Filled 0.03 km² ... from FABDEM + Copernicus DEM`; PL-1992 input; the filled gaps are on the Slovak side; with `--cache` on an empty folder it also tests the geoportal sheet download (index WFS, ASCII grid sheets, NMPT at 0.5 m) |
| R14 | R1's circle as a polygon file, computed in tiles of 600 m: same lines as R1 | `--area tests/data/r1_circle.geojson --tile-size 600` | 7 558 | 1 100 / 13 358 | 1 262 / 104 | anchors, open anchors and checked pairs are those of the first tile; must equal R1 in lines and spots |
<!-- regression-table:end -->

Suggested sets:
- Any change: R13.
- Line search, clearance, grouping: R1, R2, R3.
- Data sources, download, mosaics: R1, R7, R9, R10, R11, R13 (with `--cache` on an empty folder for download code).
- Water, coastline, gap filling: R6, R7, R8, R10, R13.

## Component checks
**Sea mask (coastline sources).** On a 2 m grid in EPSG:32633 with radius 1000 m around 35.8565, 14.3830, `coast.sea_mask` gives 0.433 km² of sea with both `world` and `overpass` (identical pixels) and 0.440 km² with `copernicus`. Against the LiDAR shoreline there, 90% of the OSM coastline is within 13 m, of the Copernicus mask within 70 m.

**Data checks for a new source or download path.** Learned with geoportal.gov.pl (2026-10-24/25):
- *Coverage at coarse resolutions*: a server that downsamples may lose data. The EVRF2007 WCS at 5 m returned 41% valid pixels on the Kasprowy tile, where its 1 m data was complete. Compare the valid share of a 5 m mosaic with a 1 m one.
- *Which survey a service serves*: compare the difference between two services with the difference between candidate source files over the same 200×200 m window (difference of differences); a residual median of 0.00 m and p5–p95 within about ±0.2 m identifies them (KRON86 WCS = 2012–2018 scans, EVRF2007 WCS = 2023–2025).
- *Half-pixel alignment*: geoportal sheets have pixel centres on whole metres (`xllcenter`); windows and grids must be shifted by 0.5 m, otherwise every comparison shows spurious ±0.5 m differences on slopes.
- *Exactness at native resolution*: with the grid aligned (`grid_origin`), R2 and R4 at 1 m gave the same anchors, open anchors and checked pairs from the sheet files as from the native WCS.

**Malta 2012 georeference.** With `MALTA_2012_SHIFT` applied, the 2012 DTM matches Copernicus GLO-30 with a median absolute difference of about 0.5 m and the 2018 DTM with 0.19 m (radius 1500 m, 10 m grid, Dingli). Without the shift it is 9 m. Do the same shift search (move one model ±200 m, minimise the median absolute difference) for any new national source.

**Pair search.** `linesearch.py` must find exactly the lines of the plain search (every pair within the length and height-difference limits checked with `helpers.evaluate_line`). After changing it, compare `lines.csv` of R1 and R3 with `--nmpt reject` against a run of the plain search: sorted, the files must be identical. The plain search is in git history before `linesearch.py` was added (`helpers.find_valid_lines`); run that version from a `git archive` copy with `--cache-dir` pointing at the real cache.

**Sag estimator.** Defaults (2.5 kN, 5% at 10 kN, 70 g/m, 80 kg), span 100 m, no height difference: unstretched length 98.768 m, sag without athlete 0.339 m, sag with the athlete at the maximum of the envelope 5.521 m; span 1000 m: envelope maximum 71.18 m. These match an independent spring-mass chain (200 segments, minimised energy) to the millimetre. The TypeScript copy (`viewer/src/sag.ts`) must give the same values to 1e-6 m: `tests/test_viewer.py` runs it with Node for spans 30, 100, 300 (dh −40), 1000 m and 100 m with dh 100, and compares the grouping (`viewer/src/grouping.ts`) with `group_lines` on 2000 random lines.

**Browser.** For the same settings, the spot count shown by the map panel equals the one printed by Python (grouping in JS mirrors `group_lines`). Clicking a line leaves one line plus its thin variants; closing the popup or "Show all lines" restores all; "Zoom to line" fits the line above the profile panel. Check with a copy of a small map plus a test script, in headless Edge (`--dump-dom`); the viewer exposes the Leaflet map as `window.FINDALINE_MAP`, so a script can find the drawn lines and fire `click` on one.

**Web app.** `tests/test_web.py` checks the guards (Host, token, refused fields and paths, only orthophoto tiles under `/cache/`) and runs a job of the Żabi Koń clip through the queue with real worker processes (queued → downloading → ready → computing → done). For the page itself: start `findaline.py web --no-browser --port 8799 --data-dir <scratch>` (with the real cache linked as in the regression script), put a test page in `viewer/dist` that loads a stub of `EventSource` before `app.js` and a test script after it (the open event stream keeps `--virtual-time-budget` from ending), and remove both afterwards. Checked 2026-10-25: a click on the map sets the centre, *Estimate* shows the source and the download, *Start* queues the job and switches to the queue; *Draw* shows the Geoman toolbar.
