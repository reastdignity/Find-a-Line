# Regression tests

The search is deterministic: for the same data and code, every number below is reproduced exactly. Run the tests that cover what you changed and compare the output with the expected values.

## How to run
`tests/regression.py` runs the searches one after another, with runs and maps in a temporary folder, and compares the results with `tests/regression.json`:

```
uv run tests/regression.py                # quick set (R13)
uv run tests/regression.py --set pl       # sets: quick, pl, search, sources, water, network
uv run tests/regression.py R1 R7          # chosen tests
uv run tests/regression.py --all          # everything (about 4 min with a filled cache, plus R17's ~7 min)
uv run tests/regression.py --all --browser   # also count the spots of every map in headless Edge
uv run tests/regression.py R1 --update --write-doc   # store new results, rewrite the table below
```

- Each test gets its own temporary data folder whose `cache` links to the real cache; `--cache <temp>/cache` uses another one (an empty folder tests the downloads).
- Compared (only the values stored for a test):
  - the counts: the source name, `Found N potential anchors`, `Anchors with an open direction: N`, `Checked pixel by pixel: N`, the lines and spots of `lines.csv`, `Filled ... km²` with its source, `sea X km²`, lines/spots with `water_m > 0`;
  - the content: the sha256 of `lines.csv`, with totals that show what changed when it differs (length in km, smallest/mean/largest clearance, lines over trees). The counts alone missed a swap of length and anchor height (2026-10-25): same lines and spots, `length_km` 95.55 → 1034.51;
  - the data: `Elevation data: X% of the search area` (before any filling, first tile of an area) and the tiles without data (`N of M tiles without data`): a tile lost by the download shows here, whatever the terrain (the north coast of Gozo, 2026-10-25);
  - `same_as`: `lines.csv` identical to another test's (R14 in tiles = R1 as one grid).
- Always checked: the map data (`data.js` has every line; profiles for every line of a circle and for the line shown for each group of an area; `lines.js` the same lines); with `check_profiles` (R16) that profiles computed again from the cache (`search.profile_of_line`) equal the saved ones; with `--browser` that the map shows the spots of `lines.csv`.
- Each test records its time (`seconds`); one more than twice as slow (and 5 s slower) gets a warning, not a failure.
- R17 (`kind: osm`, set `network`) builds the coastline and cliff tiles of Malta from the Geofabrik extract and fetches them from Overpass: the features and geometries must be the same. It is skipped without the extract (`findaline.py osm download malta`) or when Overpass does not answer.
- After upgrading libraries (`uv lock`), checksums may change with identical lines: compare two log folders' `lines.csv` files to see what changed, then re-baseline.
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
| R1 | Żabi Koń, Poland: geoportal source, defaults at 2 m | `--lat 50.1559508 --lon 19.7562722 --resolution 2` | 8 900 | 1 261 / 13 929 | 1 248 / 104 | source `geoportal.gov.pl`; data 100%; Spot 1: 102 m, clearance 8.0 m, 50.156507, 19.755129 → 50.155960, 19.756268; no coastline lookup (inland) |
| R2 | Dolina Bolechowicka, Poland: 1 m grid | `--lat 50.1528584 --lon 19.7846212 --resolution 1 --min-length 15 --max-length 2000 --slope-threshold 60` | 4 503 | 437 / 2 340 | 333 / 19 | data 100%; `Coastline: no sea in the area` |
| R3 | as R2 with the sag estimator | R2 + `--model sag` | 4 503 | 2 060 / 90 196 | 11 989 / 101 | data 100% |
| R4 | Dolina Będkowska, Poland: 1 m grid, many anchors | `--lat 50.1733209 --lon 19.7409174 --resolution 1 --min-length 15 --max-length 2000 --slope-threshold 60` | 20 460 | 2 926 / 37 361 | 1 965 / 68 | data 100%; no gaps (the WCS data had 0.05 km² filled from FABDEM) |
| R5 | as R4 with the sag estimator | R4 + `--model sag` | 20 460 | 10 485 / 1 322 582 | 52 660 / 456 | data 100%; map ~150 MB |
| R6 | Dingli cliffs, Malta 2018: sea filling, lines over water | `--lat 35.8565 --lon 14.3830 --resolution 2` | 12 865 | 4 958 / 218 849 | 10 923 / 364 | data 93.21%; sea 0.42 km²; 342 spots over water; 10 829 lines with `water_m > 0` |
| R7 | as R6 with Malta 2012: georeference correction, gap filling from 2018 | R6 + `--source malta2012` | 10 550 | 3 389 / 65 564 | 6 774 / 382 | data 87.3%; `Filled 0.19 km² ... from Malta 2018 (MSDI)`; sea 0.43 km²; 377 spots over water; 6 761 lines with `water_m > 0` |
| R8 | Dingli – zatoka (centre over the sea) | `--lat 35.848778 --lon 14.383533 --slope-threshold 60 --resolution 2` | 10 697 | 6 646 / 262 091 | 11 308 / 416 | data 51.94%; sea 1.94 km²; 364 spots over water; 10 853 lines with `water_m > 0` |
| R9 | Montserrat, Spain: IGN WCS (ASCII grid) | `--lat 41.5930 --lon 1.8100 --min-length 40` | 27 401 | 20 979 / 549 299 | 85 671 / 3 972 | source `IGN Spain (MDT05/MDS05)`; data 100% |
| R10 | Los Gigantes, Tenerife: Canary Islands source, sea at 0 m in the data | `--lat 28.2640 --lon -16.8460 --radius 800 --min-length 40` | 14 711 | 11 654 / 196 868 | 34 652 / 2 053 | source `IGN Canary Islands (MDT05/MDS05)`; data 100%; 676 spots over water; 15 008 lines with `water_m > 0` |
| R11 | Meteora, Greece: Europe-wide FABDEM + Copernicus, 30 m | `--lat 39.7217 --lon 21.6306 --radius 2000 --min-length 60` | 257 | 257 / 887 | 451 / 81 | data 100%; grouping distance 60 m |
| R12 | Meteora with NASADEM (needs `OPENTOPOGRAPHY_API_KEY`) | R11 + `--source nasadem` | 433 | 433 / 2 137 | 920 / 152 | data 100% |
| R13 | Quick smoke test, Kasprowy Wierch (~15 s with cache) | `--northing 152074 --easting 571448 --radius 500 --resolution 5 --slope-threshold 50 --min-length 50 --max-length 400` | 2 796 | 2 396 / 8 628 | 566 / 143 | data 97.64%; `Filled 0.03 km² ... from FABDEM + Copernicus DEM`; PL-1992 input; the filled gaps are on the Slovak side; with `--cache` on an empty folder it also tests the geoportal sheet download (index WFS, ASCII grid sheets, NMPT at 0.5 m) |
| R14 | R1's circle as a polygon file, computed in tiles of 600 m: same lines as R1 | `--area tests/data/r1_circle.geojson --tile-size 600 --resolution 2` | 7 558 | 1 100 / 0 | 1 248 / 104 | data 100%; lines.csv identical to R1; anchors, open anchors and checked pairs are those of the first tile; must equal R1 in lines and spots |
| R15 | Ta' Għammar, north coast of Gozo: a tile at the northern edge of the Malta coverage | `--lat 36.07967 --lon 14.22867 --radius 300 --resolution 1 --min-length 20` | 15 056 | 708 / 5 134 | 606 / 26 | data 74.72%; sea 0.10 km²; 26 spots over water; 606 lines with `water_m > 0`; the edge tile must come from Malta 2018: when it was lost (404 beyond the coverage) FABDEM filled the land, leaving 19 anchors and no lines |
| R16 | North coast of Gozo as a coast area in tiles, with the sag model: sea, gap filling at tile borders, profiles of the lines shown for groups (no group of this area joins two tiles, so the profiles computed after the tiles are not exercised) | `--area coast --buffer 100 --within 36.06,14.20,36.085,14.27 --model sag --tile-size 800 --min-length 20 --resolution 2` | 5 130 | 3 750 / 32 577 | 25 906 / 657 | data 53%; `Filled 0.19 km² ... from FABDEM + Copernicus DEM`; sea 0.77 km²; 617 spots over water; 25 178 lines with `water_m > 0`; 3 profiles computed again |
| R17 | Gozo and Malta: coastline and cliff tiles from the Malta extract equal those from Overpass | extract `malta`, box [14.15, 35.78, 14.6, 36.1] | | | | 93 coastline features; 217 cliff features, the same from Overpass |
| R18 | Fungus Rock, Dwejra (Gozo): coast within a named islet takes the Gozo coast within --max-length too, so lines between the island and the islet are found | `--area coast --buffer 100 --within osm:"Fungus Rock" --max-length 300 --resolution 1` | 33 956 | 2 797 / 23 402 | 2 226 / 38 | data 40.45%; sea 0.31 km²; 36 spots over water; 2 201 lines with `water_m > 0`; 1 583 lines with an anchor in osm:Fungus Rock; before 2026-10-25 within clipped the band to the land of the named area: no line reached the islet |
| R19 | Lauterbrunnen, Switzerland: swisstopo STAC tiles (2 m terrain, 0.5 m surface averaged to 2 m) | `--lat 46.5960 --lon 7.9070 --radius 400 --min-length 50 --max-length 400 --resolution 2` | 5 775 | 928 / 1 002 | 222 / 27 | source `swisstopo (swissALTI3D / swissSURFACE3D)`; data 100%; first run on an empty cache: 18 s (4 DTM tiles 5 MB, 4 DSM tiles 70 MB); all lines cross the forest under the west wall (over trees) |
| R20 | Lauterbrunnen at the default resolution (1 m: the 0.5 m swissALTI3D files) | `--lat 46.5960 --lon 7.9070 --radius 400 --min-length 50 --max-length 400` | 27 951 | 1 886 / 10 090 | 2 259 / 40 | source `swisstopo (swissALTI3D / swissSURFACE3D)`; data 100%; downloads the 0.5 m terrain tiles (about 80 MB) next to R19's 2 m ones |
| R21 | Súľovské skaly, Slovakia: our own ÚGKK SR tiles (DMR 5.0 / DMP 1.0, 1 m) from the local mirror | `--lat 49.1700 --lon 18.5750 --radius 400 --min-length 50 --max-length 400` | 62 991 | 2 556 / 7 196 | 1 653 / 23 | source `ÚGKK SR (DMR 5.0 / DMP 1.0)`; data 100%; skipped without FINDALINE_MIRROR; --source auto picks Slovakia (the centre is in SK); first run on an empty cache: 9 s, 2 DTM tiles 12 MB and 2 DSM tiles 14 MB copied from the mirror; forest everywhere: all lines over trees |
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

**Tiles at the edge of a coverage.** Malta's WCS answers 404 to a request reaching beyond the coverage (2026-10-25: the north coast of Gozo was "missing" and filled from FABDEM). `tests/test_sources.py` checks the clipping offline with a fake WCS (an edge tile gets the data inside the coverage on its own pixel grid, a tile outside stays missing); R15 checks it on the real service.

**Profiles on demand.** `tests/test_web.py::test_profile_on_demand`: a profile computed again on a small grid (`search.profile_of_line`) equals the one the search saved. Checked 2026-10-25 on five random lines of a Gozo coast area (water and gap filling): identical, 0.4–0.5 s each.

**Malta 2012 georeference.** With `MALTA_2012_SHIFT` applied, the 2012 DTM matches Copernicus GLO-30 with a median absolute difference of about 0.5 m and the 2018 DTM with 0.19 m (radius 1500 m, 10 m grid, Dingli). Without the shift it is 9 m. Do the same shift search (move one model ±200 m, minimise the median absolute difference) for any new national source.

**swisstopo** (2026-10-26, Lauterbrunnen, radius 1500 m, 10 m grid): best shift (0, 0) for swissSURFACE3D against Copernicus GLO-30 (median |d| 5.1 m in steep terrain, median d 0.14 m) and (10 m, 0) for swissALTI3D against FABDEM (4.96 against 4.98 m unshifted: within FABDEM's 30 m); 100% valid pixels at 1, 2 and 5 m for both models; a 2 m mosaic is identical to the 2 m files, and a 1 m mosaic (the default since 2026-10-26) equals the mean of each 2×2 block of the 0.5 m files to 0.00006 m (float32), for the DTM and the DSM. The averaging lowers the DSM: within a 1 m pixel the highest 0.5 m value is above the mean by 0.25 m (median), 7.7 m (99th percentile) and up to 189 m on vertical walls. `tests/test_sources.py::test_stac_tiles` checks the layer against a fake STAC (pages, newest survey, resolution per grid, checksum, update and pruning, offline). `cache update switzerland --all` after replacing the cached 2025 survey of 2635-1160 by its 2019 one: the index was asked again, 2025 downloaded and 2019 deleted.

**Slovakia (ÚGKK SR, our own tiles)** (2026-10-27, Súľovské skaly and Lomnický štít, radius 1500 m, 10 m grid, through the app's path mirror → cache → mosaic): best shift (0, 10 m) and (0, 0) for DMP 1.0 against Copernicus GLO-30 (median |d| 3.0 and 3.9 m, median d +2.7 and +1.0 m), (10 m, 0) and (0, 0) for DMR 5.0 against FABDEM (2.7 and 4.4 m, median d −0.7 and +0.6 m: heights above sea level, not the WCS's ellipsoidal ones about 43 m higher); 100% valid pixels at 1 and 5 m. 40 random tiles of each product against the source TIFF: within 5.00 mm (LERC's `max_z_error`), the same nodata; 10 empty tiles of the index are empty in the source; both indexes complete (DMP 102 × 214, DMR 103 × 216 entries) with the same 12 826 tiles with data. The national TIFFs name their CRS only (a `LOCAL_CS`): tiles carrying it could not be reprojected, so the cutter writes EPSG:8353 (`tests/test_sources.py` cuts a raster with ÚGKK's CRS).

**Pair search.** `linesearch.py` must find exactly the lines of the plain search (every pair within the length and height-difference limits checked with `helpers.evaluate_line`). After changing it, compare `lines.csv` of R1 and R3 with `--nmpt reject` against a run of the plain search: sorted, the files must be identical. The plain search is in git history before `linesearch.py` was added (`helpers.find_valid_lines`); run that version from a `git archive` copy with `--cache-dir` pointing at the real cache.

**Sag estimator.** Defaults (2.5 kN, 5% at 10 kN, 70 g/m, 80 kg), span 100 m, no height difference: unstretched length 98.768 m, sag without athlete 0.339 m, sag with the athlete at the maximum of the envelope 5.521 m; span 1000 m: envelope maximum 71.18 m. These match an independent spring-mass chain (200 segments, minimised energy) to the millimetre. The TypeScript copy (`viewer/src/sag.ts`) must give the same values to 1e-6 m: `tests/test_viewer.py` runs it with Node for spans 30, 100, 300 (dh −40), 1000 m and 100 m with dh 100, and compares the grouping (`viewer/src/grouping.ts`) with `group_lines` on 2000 random lines.

**Browser.** For the same settings, the spot count shown by the map panel equals the one printed by Python (grouping in JS mirrors `group_lines`). Clicking a line leaves one line plus its thin variants; closing the popup or "Show all lines" restores all; "Zoom to line" fits the line above the profile panel. Check with a copy of a small map plus a test script, in headless Edge (`--dump-dom`); the viewer exposes the Leaflet map as `window.FINDALINE_MAP`, so a script can find the drawn lines and fire `click` on one.

**Cache updates.** On a scratch data folder (2026-10-25):
- *Theme tiles from an extract*: `findaline.py osm download malta`, then `osm.theme` over Gozo: the tiles the extract covers are identical to Overpass tiles (same ids and geometries: coastline 33 and 9, cliff 128 and 29 features; Poland's Jura tiles 704 and 488); border tiles (Tatras with Poland only) still come from Overpass.
- *Diffs*: put an older dated extract in place (`malta-261001.osm.pbf`) and run `osm update`: 22 diffs in 16 s, the header ends at the newest sequence.
- *Stale checks*: set `checked_at` of the rows to an old date in `cache/<top>/index.sqlite`; `cache status` lists them, `cache update` checks them ("0 of N changed" when the content is the same), a second run has nothing to check.
- *Newer sheets*: delete the newest cached sheet of a godło and download an older survey of it (`SheetTiles._download_and_record` with a sheet from the index JSON); `cache update poland --all` and a search with `--update stale` both download the newest and delete the older file, and `cache status` lists the run made on the older one.

**Other runs.** The Żabi Koń clip as run A (EPSG:2180), the same clip reprojected to UTM 34N as run B and a smaller run C nearby: on A's map, checking C adds its spots (43 → 47), checking B adds none (its lines join A's groups); the choice is still there when the map is opened again.

**Web app.** `tests/test_web.py` checks the guards (Host, token, refused fields and paths, only orthophoto tiles under `/cache/`) and runs a job of the Żabi Koń clip through the queue with real worker processes (queued → downloading → ready → computing → done). For the page itself: start `findaline.py web --no-browser --port 8799 --data-dir <scratch>` (with the real cache linked as in the regression script), put a test page in `viewer/dist` that loads a stub of `EventSource` before `app.js` and a test script after it (the open event stream keeps `--virtual-time-budget` from ending), and remove both afterwards. Checked 2026-10-25: a click on the map sets the centre, *Estimate* shows the source and the download, *Start* queues the job and switches to the queue; *Draw* shows the Geoman toolbar.
