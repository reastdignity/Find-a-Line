# Roadmap

Planned features and the decisions already made for them. Nothing here is implemented yet.

1. **Several points at once**: lines from several places shown together on one map.
2. **Irregular search areas**: e.g. the whole coast of Malta within 100 m of the shore, or a polygon drawn on the map.
3. **Running searches from the browser**: no terminal needed, built so it can later become a public website.
4. **Cache layout and updates**: versions and metadata for cached data, updating it when the services publish new data, a local OpenStreetMap copy.

## Decisions
| Topic | Decision |
| --- | --- |
| Several points | `--point LAT,LON[,NAME]`, repeatable; every point is a separate run. Every map can also show the lines of other saved runs. |
| Telling runs apart on the map | Only the search areas (circles/polygons) get a colour per run; lines look the same as today. |
| Area sources | Band along the sea coast, band around OSM cliffs, GeoJSON/KML/GPX file, OSM object by name (Nominatim), polygon drawn on the map. |
| What the area limits | Anchors only: both anchors must be inside the area, the webbing may pass outside it (e.g. over a bay wider than the band). |
| Large areas | One run and one map per area, computed tile by tile inside; an interrupted run resumes from the last finished tile. |
| Web application | FastAPI + job queue + separate worker (option A below). |
| Result viewer | A static viewer (JS/CSS + per-run JSON data) replaces the folium-generated `map.html` in stage 3. |

## Order
Chosen to keep the cost and the risk of errors low: a safety net first, foundations before what builds on them, nothing built twice.

1. **Safety net** (done 2026-10-25): `tests/regression.py` runs the searches of `tests/regression.json` and compares the numbers (all 13 in about 3 min; the table in docs/regression-tests.md is generated from the JSON); `pytest` runs the offline tests (synthetic canyon, Żabi Koń clip) in about 25 s. `pyproject.toml` + `uv.lock` replace `requirements.txt`; the regression set gives the same numbers with the locked, newer library versions (numpy 2.5, rasterio 1.5, shapely 2.2). The start scripts come with stage 3.
2. **Cache layout and index** (done 2026-10-25, `cache.py`, `osm.py`): new paths with editions and tilings, `cache/LAYOUT`, `index.sqlite` per top-level folder (replaces the `.missing` files), `--data-dir` / `FINDALINE_DATA` (replacing `--cache-dir` and `--runs-dir`), `work/<id>/` per search (deleted unless the search crashed), the data used recorded in `run.json`, OSM themes `coastline` and `cliff` from Overpass, cached Nominatim answers. The old cache was deleted and downloaded again (only the OSM world file was moved), so old maps load the orthophoto online. `--refresh` stays until the update commands (step 6).
3. **Stage 0** (done 2026-10-25): `params.py` (pydantic schema, argparse generated), `search.py` (`plan_search`, `estimate_search`, `prepare_data`, `find_lines`, `save_run`, `run_search` with `on_event` and `confirm`), `area.py` (`Circle`, `Polygon`, masks instead of `center_row`/`radius_px`), progress listeners (`progress.subscribe`); `findaline.py` is the command line only. All 13 regression tests give the same numbers. The only visible change: `--ortho auto|always|never` replaces `--no-ortho-cache`. Left for later: messages printed inside `sources.py`, `coast.py` and `orthophoto.py` still go to stdout, not to `on_event`; `WcsTiles.tiles` picks the tiles under the circle inscribed in the grid, which must use the area's shape once polygons arrive (stage 2).
4. **Stage 1, minimal** (done 2026-10-25): repeatable `--point LAT,LON[,NAME]`, each point its own search, work folder and run; a point without lines does not stop the others (they are listed at the end). The "Other runs" panel and `lines.js` are not built for the folium map; they come with the stage 3 viewer and its per-run data format.
5. **Profiling, then stage 2**: measure where the time goes (downloading, gap/water filling, profiles, map) first. Profiled 2026-10-25 with cached data (cProfile): the line search itself is quick (R9: 3 s of 50 s); the time went to per-line Python work, line profiles (15 s, half of it rounding), grouping (10 s, every group scanned all lines), saving `lines.csv`/`lines.geojson` (12 s) and the folium map (7 s, 4 s of it compiling Jinja templates), plus 5 s of geoportal index queries in every Polish search. Done: exact rounding through `tolist()`, grouping with a grid of cells (only lines near the representative are tested), index answers cached per 0.25° tile; `lines.csv` and the maps of all 13 tests are byte-identical, R9 50 → 20 s, R4 8.6 → 3 s. Still to do for large areas: saving the results (see "Very large areas").

   **Stage 2 done 2026-10-25**: `--area coast|cliffs|<file>|osm:<name>` with `--buffer` and `--within`; tiles with an overlap of `--max-length` (+2 pixels), sized from the free memory (`--tile-size` to override; rounded to 500 m and kept in `progress.json` so a resumed run uses the same tiles); every finished tile saved as `tiles/<k>.pkl.gz` (not `.npz`: the results are dicts with profiles), an unfinished run of the same search is found and resumed automatically; confirmation with km², tiles and download; in maps of areas only the representatives have a profile, detailed orthophoto for `--ortho-detail` (50) groups. Data tiles (WCS, sheets) are picked under the area outline. Checked: R1 as a polygon gives exactly R1's 1262 lines, as one grid and in 15 tiles (R14), and after an interruption the resumed run too. Decided: the coast band covers both sides of the coastline (anchors at sea are flat anyway). Changed later the same day: `--within osm:<name>` no longer clips the band to the land of the named area (that lost the islets off Gozo's coast, the sea side and cliff edges: 2240 of 2305 lines at Dwejra); it takes the coastline or cliffs within `--max-length` of the area and reports the pieces up to twice as far; a box still clips. The app's estimate draws the area on the map. Differences from the plan: none of the area sources was left out. Then areas: GeoJSON file first, then `coast` and `cliffs` (they use the OSM themes from step 2), tiling and resuming last, with the tests ready (a polygon equal to the circle, a tiled area equal to one grid).
6. **Cache updates** (done 2026-10-25, `update.py`, `osmextract.py`): `findaline.py cache status [FOLDER]` (size, fetch and use dates, what is stale, OSM extracts, runs on changed data) and `cache update [FOLDER] [--all] [--bbox|--point]`; `--update stale|all` of a search replaces `--refresh`. Intervals in `cache.TTL_DAYS`: live tiles 730 days, sheet index 180, Overpass theme tiles 365, extracts 30, world water polygons 365 (the server regenerates them daily, so "conditional at every update" would mean 900 MB each time), missing 90; releases never. A file downloaded again with the same sha256 keeps its fetch time. geoportal.gov.pl: the index tile is asked again, newer surveys of the cached godła are downloaded and the older files deleted at once (also in a search). `findaline.py osm download REGION` (Geofabrik id or name; the extract goes into the country's folder) and `osm update` (replication diffs with pyosmium, `osmium` package); theme tiles are built from the extracts when their outlines cover the tile whole (border tiles need both countries), else Overpass, and are rebuilt after every download or update. Runs record the paths of the files they used (`data[].paths` in `run.json`); a run is on changed data when one of them was fetched later or deleted. Checked: the Malta extract gives exactly the Overpass features and geometries (coastline and cliff tiles); an extract from 2026-10-01 caught up with 22 diffs in 16 s; a cached older sheet of M-34-64-D-a-1-1 was replaced by the 2024 one by `cache update` and by a search, and the run on it was listed as changed.
7. **Stage 3** (local web application) with the TypeScript viewer, the "Other runs" panel and polygon drawing. The viewer's grouping and sag must match Python (checked with Node, as `Sag` is today). Docker and CI are not part of it (decided 2026-10-25). Decided: the viewer replaces the folium map for the command line too; esbuild, `viewer/dist` committed (users need no Node); Preact for the UI; order 3a viewer, 3b `lines.js` and "Other runs", 3c server.

   **3a done 2026-10-25** (`viewer/`): TypeScript + Preact + Leaflet, bundled by esbuild into `viewer/dist` (committed, copied to `runs/_viewer/`); a run's `map.html` loads it with `data.js` (`window.FINDALINE_RUN`: lines with profiles, area, orthophoto, grouping, rigging) and `../runs.js`. Everything of the folium map is there (layers with the cached orthophoto, search area, grouping panel, draggable popups, focus on a line, profile with the sag estimator, run panel); folium, branca and xyzservices are gone. `groupLines` now uses the cell index of `group_lines` (the old JS compared every pair). `tests/test_viewer.py`: sag to 1e-6 m, the same groups as Python, `dist` up to date.

   **3b done 2026-10-25**: every run also writes `lines.js` (all its lines without profiles, its CRS and area: `window.FINDALINE_LINES[id]`); `runs.js` lists runs without their data paths and outlines but with a box (`bbox`). The "Other runs" panel lists the runs with `lines.js`, those within 20 km first (box distance), the rest folded; a checked run adds its lines in its own colour (dashed over trees/buildings) and its area, loaded with a `<script>` tag. Decided: all lines of the other runs, grouped together with the current run's; when a shown run has another CRS, all lines are grouped in a local plane around the current run (otherwise in the grid CRS, exactly like Python); the choice is kept in `localStorage`. A line of another run has no profile; its popup links to its own map. Checked: the Żabi Koń clip as one run in EPSG:2180 and one reprojected to UTM 34N merges into the same spots (43 → 43), a third run nearby adds 4.

   **3c done 2026-10-25** (`web.py`, `viewer/src/app`): `findaline.py web` (plus `start.bat`, `start.command`, `start.sh`) serves the app on 127.0.0.1:8765. The page: the map (click a centre, draw polygons/rectangles with Leaflet-Geoman, an uploaded GeoJSON/KML/GPX shown on the map, or coast/cliffs/osm:name with buffer and within), the form generated from `SearchParams` (cli_only fields left out), *Estimate* (`estimate_search`), *Start*; a queue with live progress (SSE) and cancelling; the list of runs. Jobs are rows of `jobs.sqlite` in the data folder with their events; each phase runs in its own `spawn` process: `search.download_data` (new: the layers, the water mask and the coastline of the area, no grids) and then the search, one computing and one downloading at a time; a drawn area is saved in the job folder and named after the place. Guards: Host must be 127.0.0.1/localhost, changing requests need the session token of the page, CSP without inline scripts, no file paths from the web, `/cache/` serves orthophoto tiles only. Maps are served from `/runs/` and link back to the app. Added later: the *OSM* tab (regions at the map centre from the Geofabrik index, download and update as queue jobs); theme tiles count as covered when what the extracts leave out is open sea. Not done: a page of its own for the results (the run maps are used), stopping a running job other than by killing its process, limits on area and time per job (the user confirms the estimate; limits come with the public version), KML/GPX are read by GDAL (no defusedxml).
8. **Docker and CI** (taken out of stage 3, later): the `Dockerfile` and GitHub Actions on three platforms (see "Installation and platforms").
9. **Stage 4** (public website): last, after the licences (FABDEM non-commercial, Malta) are checked and Overpass/Nominatim have own instances or caching.

Rules for every step:
- A commit is either a refactoring or a behaviour change, never both; only then do the regression tests tell something.
- The highest risk is in step 5 (tile borders, duplicate lines) and step 7 (viewer matching Python); step 1 must be done before them.

## Stage 0: refactoring
- `findaline.main(args)` becomes a library function `run_search(params, on_progress)`. One parameter schema (pydantic) is shared by the CLI, the API and the browser form, with types, defaults, limits and help texts in one place. argparse is generated from it.
- Area abstraction: `Circle(center, radius)` and `Polygon(geometry)`, both giving a boolean mask on the grid. Everything that now uses `center_row`, `center_col` and `radius_px` (anchor search, plots, orthophoto cache, run metadata) uses the mask instead.
- `progress.py` reports to a callback as well as to tqdm, so the server can forward progress.
- `run_search` is split into a download part (all tiles of the area into the cache) and a compute part, so the worker can download the data of the next job while the current one computes (stage 3).

## Stage 1: several points, overlaying runs
- `--point` may be given many times; the runs are computed one after another with the same parameters. `--lat/--lon`, `--northing/--easting` stay as the single-point form.
- Built in the stage 3 viewer (see Order): every run also writes `lines.js` (`window.FINDALINE_LINES[...] = {...}`), so a map opened from disk can load other runs with a `<script>` tag. It holds the spot representatives with their popup data, without profiles.
- Map panel "Other runs" (also in the stage 3 viewer): a checkbox per saved run (nearby ones first) adds its lines and its search area, drawn in the run's own colour. Clicking such a line shows its popup and a link to open it in its own map (with the profile).
- Tests: R1 and R13 unchanged; two points computed in one call, each with its own run.

## Stage 2: irregular areas
**Engine**
- The grid covers the bounding box of the area. Anchors are searched only inside the mask; the pair search is unchanged.
- Tiles: squares of about 2 km with an overlap equal to `--max-length`. A line belongs to the tile that holds its first anchor, so there are no duplicates. Each finished tile is saved in the run folder (`tiles/<i>.npz`), and a restarted run skips those.
- Before starting, the script shows the area (km²), the number of tiles, the download size and an estimated time, and asks for confirmation.
- Rough scale: the coast of Malta and Gozo is about 190 km, so a 100 m band is 20–40 km² at 1 m. With the numba search the line search takes minutes; downloading the data dominates.
- Orthophoto cache: the overview (up to zoom 16) covers the whole area, the detail strip (up to zoom 21) only the N best groups in the `--group-by` order (a parameter, about 50); the other groups load the orthophoto online.

**Area sources (`--area ...`)**
- `coast --buffer 100`: land within the given distance of the OSM coastline (world water polygons file, else the `coastline` OSM theme), limited to a country/island or a bounding box.
- `cliffs --buffer 100`: OSM `natural=cliff` (optionally `climbing=crag`) from the OSM themes of the cache, buffered.
- `file.geojson|kml|gpx`: polygons as they are; points and lines buffered by `--buffer`.
- `osm:"Gozo"`: polygon of a named object from Nominatim (`polygon_geojson=1`).
- Polygons drawn on the map arrive as GeoJSON (stage 3, or exported to a file before then).

**Tests**: R1 with an equivalent polygon gives the same lines as the circle. A polygon split into tiles gives the same lines as one grid. Resume after interrupting.

## Stage 3: web application (local)
- `findaline.py web` (decided instead of a separate `findaline_web.py`; plus `start.bat`, `start.command`, `start.sh`) starts FastAPI on `127.0.0.1` and opens the browser. Locally, the API, the queue (SQLite) and the worker run in one process tree.
- Pages: map for clicking points and drawing polygons (Leaflet-Geoman); parameter form generated from the schema; live Download/Compute progress (Server-Sent Events); list of runs; result viewer.
- Static viewer: one JS/CSS bundle shared by all runs. Data per run comes as JSON (lines, groups, profiles), plus `lines.js` for `file://`. Grouping and the `Sag` port move from the Jinja templates in `mapping.py` into this bundle, and stay in sync with Python as today.
- One job computes at a time (it uses all cores and much RAM); the next job may already download its data meanwhile. The others wait in the queue and can be cancelled. Each job has its own `work/<job-id>/`; concurrent downloads need WAL mode in `index.sqlite` and a lock so the same tile is not fetched twice.

## Stage 4: public website (later)
- Docker Compose: reverse proxy with TLS (Caddy), API, worker(s), Postgres, Redis, object storage for results.
- Accounts (to be decided: invitations, OAuth, or anonymous with a quota), per-user quotas, admin view of jobs.

## New data sources
Researched 2026-10-25 (how: docs/data-sources.md). Order: Slovakia (DSM needs a hosted copy, see below), then Switzerland (done first, 2026-10-26: it needed no hosting); Greece has no usable national source. The USA was researched on 2026-10-26 (below); nothing is planned for it yet.

### Switzerland (swisstopo): sheet-like files from STAC
- **Data**: swissALTI3D (DTM, 0.5 m and 2 m) and swissSURFACE3D Raster (DSM, 0.5 m), whole country and Liechtenstein; 1 km tiles as COG GeoTIFF float32 (mm), LV95 EPSG:2056 with LN02 heights; pixel edges on whole metres (`grid_origin` 0); about 26 MB (0.5 m) or 1 MB (2 m) per tile. Licence: open government data, commercial use allowed, "© swisstopo" required.
- **Access**: no elevation WCS exists. swisstopo recommends its download service for offline use: the STAC API `https://data.geo.admin.ch/api/stac/v1` (collections `ch.swisstopo.swissalti3d`, `ch.swisstopo.swisssurface3d-raster`; items `swissalti3d_<year>_<E>-<N>`, bbox in WGS84, 100 items per page). Files on S3/CloudFront, about 40 MB/s, sha256 in the item (`file:checksum`). Fair use: no bulk bot traffic, exponential backoff with jitter.
- **Versions**: every tile exists per survey year (2019, 2024, 2025...), not every year for every tile; DSM tiles of neighbouring areas may be of different years.
- **Plan**: a layer on the `SheetTiles` pattern: STAC answers cached per 0.25° tile (policy `sheet-index`), the newest year per tile, older years deleted when a newer one is downloaded; the 2 m files for working grids of 2 m and more, 0.5 m below. Orthophoto: SWISSIMAGE WMTS in EPSG:3857 (`https://wmts.geo.admin.ch/1.0.0/ch.swisstopo.swissimage/default/current/3857/{z}/{x}/{y}.jpeg`: the map's own tiling, CDN), with a lower default `--ortho-detail` to respect the fair-use terms (decided).
- **Tests**: a regression test with the DSM (e.g. the cliffs of Lauterbrunnen), the data checks of a new source (shift against Copernicus, coverage at 5 m, a tile against its file); pytest with a fake STAC (newest year, deleting older ones).
- **Done 2026-10-26** (`sources.StacTiles`, source `swisstopo`): the index logic of `SheetTiles` became a base class, `IndexedTiles`, shared by both. An item holds a COG and an XYZ zip at each resolution (`gsd`); only the COGs are used, stored as published in `switzerland/<product>/<res>m/<year>_<E>-<N>.tif` and checked against `file:checksum` (sha256 multihash `1220…`). Files: float32 metres, nodata -9999, pixel edges on whole metres, DTM values finer than mm, DSM in cm; 2 m tile 1.3 MB, 0.5 m 17–20 MB, about 0.5 s each from CloudFront. A 0.25° index tile in the Alps is about 1300 items in 14 pages (9 s), so it is cached as planned. At Lauterbrunnen the DTM has 2019 and 2025 surveys per tile, the DSM 2022. Data checks: no shift against Copernicus GLO-30 (DSM) or FABDEM (DTM) in a ±200 m search, 100% valid pixels at 2 and 5 m, a 2 m mosaic identical to its files. SWISSIMAGE WMTS has imagery up to zoom 20 (21 answers 400): `OrthoService` got `url=None` (base tiles only, cached up to its last zoom, enlarged by the map above it) and `detail` (20 groups, circles too; `--ortho-detail` overrides). Regression tests R19 (2 m) and R20 (1 m: the 0.5 m terrain files). Since the default resolution became 1 m (2026-10-26) a default search uses the 0.5 m terrain files (about 37 MB per km² with the DSM instead of 18); the 2 m files serve `--resolution 2` and coarser. `cache update` replaced a 2019 tile by its 2025 survey and deleted the older file.

### Slovakia (ÚGKK SR): own hosted tiles of DTM and DSM
- **Data**: DMR 5.0 (DTM) and DMP 1.0 (DSM), 1 m, whole country (ALS 2017–2023); DMR 6.0 / DMP 2.0, 0.5 m, 2nd cycle (16 of 73 lots in 10/2026, not the Tatras). Licence CC BY 4.0, author ÚGKK SR (checked: the licence PDF inside the ZIPs says only this). Heights in S-JTSK [JTSK03] EPSG:8353 with Bpv EPSG:8357, or ETRS89-TM34 EPSG:3046 with ellipsoidal heights (about 43 m higher).
- **Access as published**:
  - an INSPIRE WCS for the DTM only (`https://inspirews.skgeodesy.sk/geoserver/el/ows`, coverage `el__EL.GridCoverage`, EPSG:3046 ellipsoidal heights, cm, 2 km in about 5 s, 404 beyond the coverage, server-side scaling unreliable); no WCS for the DSM;
  - static ZIPs on `opendata.skgeodesy.sk`: one compressed TIFF per country or lot (DMP 1.0 155 GB, DMR 5.0 198 GB, a 2nd-cycle lot 7–14 GB): no random access, unusable on demand;
  - MAPKA export up to 400 km², delivered by e-mail: manual only.
- **Decided**: host our own copy of both DTM and DSM as tiles (heights in Bpv, one mechanism, no dependence on the WCS); the app reads them as a fixed grid of 2 km tiles (`sources.GridTiles`; 2 km rather than 1 km keeps each product at about 13k files, far below Hugging Face's 100k per repository), converted by `tools/sk_tiles.py` (download, cut straight from the ZIP, upload; the user's NAS as working space only). Hosting (decided 2026-10-25): a public Hugging Face dataset only, no copy on the user's NAS (no maintenance, no load on the home uplink, FABDEM already comes from Hugging Face); tiles in subfolders (fewer than 10k files per folder, 100k per repository), licence `cc-by-4.0` with ÚGKK SR as author in the dataset card.
- **Trial (LOT04 DMP 2.0, 0.5 m)**: the ZIP (9.1 GB) downloaded in 12.4 min (12 MB/s); 7-Zip extracted the TIFF (Deflate64, which Python's zipfile cannot read) in 17 s; the TIFF inside is tiled (128 px, LZW). Cut into 1 km COG tiles in the source grid: 918 tiles with data, 4.94 GB, 4 min. Encodings compared on 30 tiles: LERC_ZSTD with a maximum error of 5 mm gave 5.5 MB per tile in 0.2 s (float32 DEFLATE: 9.4 MB, lossless; int32 cm DEFLATE: 5.8 MB but 15 s). Estimate for the whole country: about 65–70 GB per model at 1 m, about 265 GB per model at 0.5 m. Script: a `tools/` converter still to be written (download, 7-Zip, cut, index JSON with checksums, upload).
- **Orthophoto**: WMS `https://zbgisws.skgeodesy.sk/zbgis_ortofoto_wms/service.svc/get?`, layer `1`, EPSG:4326, CC BY 4.0.
- **Tests**: a regression test on the Slovak side of the Tatras with the DSM; the data checks of a new source (shift against Copernicus and against the WCS DTM, coverage); pytest for the tile layer against a fake server.
- **Changed 2026-10-26: a local copy first, Hugging Face later.** The tiles are kept on the user's disk until the upload (`C:\Users\Krzysiek\findaline-mirror\slovakia`, laid out as the dataset, with its card); the ZIPs are downloaded to C: (`C:\Users\Krzysiek\findaline-work\sk-zips`), cut there and then kept on the NAS (`Z:\Udostępnione tymczasowo\krzysiek\Find-a-Line\sk-zips`). Whether the upload goes to a dataset repository or a Storage Bucket (as FABDEM's mirror) is decided at upload time; the layout and `index.json` suit both. 1 m (1st cycle) only; the 0.5 m lots of the 2nd cycle later. Checked: both national ZIPs are plain Deflate (DMR 5.0: 198 GB with a 157 GB TIFF and a 47 GB `.ovr`; DMP 1.0: 155 GB, TIFF 122 GB), so GDAL `/vsizip/` reads them without 7-Zip, in full-width 2 km bands so the ZIP is read forwards only.
- **Done (code) 2026-10-26**: source `slovakia` (`sources.GridTiles` with `mirror=`, EPSG:8353, ZBGIS orthophoto WMS, which has imagery at zoom 20 and more); `FINDALINE_MIRROR` names the folder above `slovakia/`, and a search copies the tiles it needs into `cache/slovakia/dmr5/5.0/...` and `dmp1/1.0/...` (static releases), checking each against the sha256 of the mirror's `index.json`; a tile not in the index yet is an error, never "missing". Without a copy set up, `--source auto` uses `europe` and says why. Moving to Hugging Face: cut, upload, add the base to `sources.SLOVAK_TILES`.

### Greece: no national source that can be downloaded
- The Hellenic Cadastre has a 5 m DEM (2007–09), a 2 m DEM (2014–16) and a 0.8 m DSM of 58 towns, given only to public bodies on request (national security rules). No WCS, download portal or index. A new LiDAR survey (1 m DTM/DSM, about 47% of the country, planned 2024–2025) had no distribution announced.
- **Decided**: keep FABDEM/Copernicus 30 m; add the Cadastre orthophoto WMS as the map background despite its watermark (`http://gis.ktimanet.gr/wms/wmsopen/wmsserver.aspx`, layer `BASEMAP`, EPSG:4326; EPSG:900913 returns blank images). The ArcGIS tiles behind maps.ktimatologio.gr (no watermark) are not a published service: not used.
- **To do outside the code**: watch whether the new LiDAR is published (expect sheet files on the EGSA87 grid: the Polish pattern); write to the Cadastre's Geospatial Information Department for the 2 m DEM of chosen places (non-commercial use).

### United States (USGS 3DEP): researched 2026-10-26, not planned yet
**Summary**
- **Terrain (DTM): yes, at 1 m.** USGS (3D Elevation Program, 3DEP) publishes free 1 m ground elevation for nearly all of the USA, in the public domain. Each 10 km tile is a file, and a newer survey of the same place is another file. It could be added like the Polish or Swiss data.
- **Surface (DSM: trees, buildings): no national 1 m model.** The choice is a ready 2 m model from Microsoft's Planetary Computer, or a model computed from the USGS point clouds. The Planetary Computer model covers older surveys only and is missing in places such as Yosemite. Computing our own is exact, but means large downloads and new code.
- **States** mostly deliver their laser scans to USGS, so the national terrain already holds them. A few states (Utah, Kentucky) also publish a DSM, each in its own format.
- **Orthophoto**: USGS imagery tiles (NAIP, public domain) are cached up to zoom 16.

Checked at Yosemite (El Capitan), Moab and Boulder (Flatirons) with the TNM API, HTTP range reads and a few image requests. No data was compared with Copernicus yet.

**Terrain (DTM), national: USGS 3DEP.** All 3DEP products are public domain; credit "USGS 3DEP" anyway.
- *1 m DEM per project* (TNM dataset `Digital Elevation Model (DEM) 1 meter`):
  - Tiles are 10×10 km in UTM / NAD83 with NAVD88 heights, float32.
  - Files are `https://prd-tnm.s3.amazonaws.com/StagedProducts/Elevation/1m/Projects/<project>/TIFF/USGS_1M_<zone>_x<E>y<N>_<project>.tif`, 10–285 MB each.
  - One tile can exist in several projects, that is, surveys: Moab 3, Boulder 2 (2020 and 2023). These are versions, as with the Polish sheets.
  - Newer files are COGs: GDAL ghost header, 512 px blocks, LZW with the floating-point predictor, overviews. Older ones have their IFD at the end of the file (Yosemite 2019), so range reads need one more request.
  - The index is the TNM Access API: `https://tnmaccess.nationalmap.gov/api/v1/products?datasets=<name>&bbox=W,S,E,N`. It answers JSON with title, publication date, size, download URL and box.
- *OPR DEM* (`Original Product Resolution (OPR) Digital Elevation Model (DEM)`, `.../Elevation/OPR/Projects/...`):
  - Each project's own resolution and tiling: at Yosemite, 1.5 km tiles of about 12 MB.
  - Smaller files, but the CRS and units differ between projects.
- *Seamless 1 m DEM (S1M)*, TNM dataset `Seamless 1-m DEM (S1M)`, new in 2025:
  - 10 km COG tiles in EPSG:6350 with NAVD88 (GEOID18) heights, built from the best 1 m DEMs of each place.
  - Voids wider than 10 m are filled from the 1/3″ or 1/9″ DEMs. There are no versions to choose between.
  - "Limited availability": the API returned nothing at any of the three places. To watch.
- *ImageServer* `https://elevation.nationalmap.gov/arcgis/rest/services/3DEPElevation/ImageServer`:
  - Best available data, 1 m where it exists, in any CRS, float32 TIFF or LERC, up to 8000×8000 px per request.
  - `exportImage` of 2×2 km at 1 m in EPSG:26911 took 11.5 s (17 MB).
  - No version information. Its mosaicking and resampling are not checked. USGS also announced WCS and WMS interfaces for it.
- *1/3″ (about 10 m) seamless DEM*: all of CONUS, Hawaii and the territories; the natural gap filler instead of FABDEM.
- *Possible way in*: an `IndexedTiles` layer over the TNM API, with the newest project per tile, so that `cache update` finds newer projects. Window reads of the COGs (range requests) would spare downloading a 250 MB tile for a small circle. The alternative is `WcsTiles`-like requests to the ImageServer. Decide after the data checks.

**Surface (DSM): nothing ready nationally at 1 m.**
- *Planetary Computer* `3dep-lidar-dsm` (STAC `https://planetarycomputer.microsoft.com/api/stac/v1`):
  - 2 m COGs made with PDAL from the 3DEP point clouds, noise removed.
  - The STAC search needs no account. The files (Azure blobs) need a free SAS token from the Planetary Computer API.
  - Projects of 2012–2022 only. Found at Moab (UT Southern QL1 2018) and Boulder (2013, older than the 2020/2023 terrain). Not found at Yosemite (2019), Smith Rock or New River Gorge.
  - The items give no CRS or vertical datum.
  - Related collections: `3dep-lidar-dtm` (SMRF ground) and `3dep-lidar-hag` (height above ground).
- *Point clouds* (TNM dataset `Lidar Point Cloud (LPC)`):
  - 1 km LAZ tiles of 200–500 MB at Yosemite and Boulder, wherever the 1 m DTM is.
  - A DSM would be computed by us: the highest first return per 1 m pixel, noise classes excluded, cached as small tiles with the LAZ deleted.
  - Costs: a new dependency (laspy + lazrs) and 1–2 GB of downloads for a 1 km circle.
  - The EPT copy on AWS (`s3://usgs-lidar-public`, Web Mercator, readable by octree node) lags behind: Yosemite 2019 and the Colorado 2023 project are missing.
- *OpenTopography*: 3DEP point clouds and the 1 m DEM by API. The 1 m DEM is for academic users only. Free keys allow 50 calls per day (200 for academics), and a 1 m request is limited to about 250 km².
- *Copernicus GLO-30 as the surface over a 1 m DTM*: its 30 m pixels on cliff rims would make obstacles everywhere. Not usable.

**State sources**
- Most state lidar is flown with 3DEP (state partners) and ends up in the national DTM. States add mainly DSMs and sometimes a finer resolution, each through its own portal and format.
- Examples:
  - Utah UGRC: statewide acquisitions 2016–2020 with DTM and first-return DSM at 0.5–1 m; Wasatch Front 2013–14 at 0.5 m in 2 km `.img` tiles.
  - Washington DNR lidar portal (`lidarportal.dnr.wa.gov`): projects with DTM and DSM, interactive download.
  - Kentucky KyFromAbove: AWS open data with a STAC API, 2 ft DEM, COPC point clouds, public domain with attribution.
  - Tennessee (`lidar.tn.gov`): `.img` DEMs and `.zlas` point clouds.
  - NOAA Digital Coast: coastal lidar and DEMs.
- A state source would make sense only for a statewide DSM in a ready format (Utah, Kentucky).

**Orthophoto**
- USGS `https://basemap.nationalmap.gov/arcgis/rest/services/USGSImageryOnly/MapServer/tile/{z}/{y}/{x}`: EPSG:3857, USDA NAIP, public domain. The cached tiles answer up to zoom 16; zoom 18 answered 404.
- For more detail: NAIP 0.6 m from the USDA ImageServer (exportImage), not checked.

**To check before deciding**
- Georeferencing and heights against Copernicus (NAVD88 against EGM2008), see docs/regression-tests.md, "Data checks for a new source".
- ImageServer pixels against a 1 m file.
- Choosing between the projects of one tile.
- Range reads of the files that are not COGs.
- When S1M reaches the climbing areas.
- How often lines in US places pass over trees or buildings, that is, how much a missing DSM would change the results.

## Cache layout and updates
The top level of `cache/` stays one folder per country or per global dataset, so a user can free space by deleting a folder. Everything about that country or dataset, its metadata included, is inside it.

**Decisions**
| Topic | Decision |
| --- | --- |
| Location | `cache/`, `runs/` and `work/` stay in the repository folder; `--data-dir` / `FINDALINE_DATA` moves all three together (maps reach cached orthophoto tiles by a relative path from `runs/`). |
| Path | `<country or global dataset>/<dataset>/<edition>/<tiling>/<tile>`. |
| Edition | Versioned releases use their version (`2018`, `2021`, `v1-2`): content never changes, a new release goes into a new folder. Services that always serve the current state (IGN, WMS orthophotos) use `live`: an update overwrites the tile, its dates are kept in the index. |
| Tiling | CRS, resolution, tile size and format (e.g. `epsg2180-1m-2000px-tif`); a format change matters because IGN GeoTIFFs are whole metres and ASCII grids keep centimetres. Corrections applied when tiles are placed on the grid (`MALTA_2012_SHIFT`) are not part of it. |
| Metadata | `index.sqlite` in every top-level folder, one row per file keyed by its path (which names dataset, edition and tiling): status (`ok`, `missing`), fetched/checked/last-used times, ETag, Last-Modified, sha256, date of the source data, processing version. Replaces the `.missing` files. URLs are stored without secrets (no OpenTopography key). A row without a file means missing; a file without a row is adopted. |
| Updates | Never during a search. `--update stale|all` or `findaline cache update [folder] [--point/--bbox]`; `findaline cache status` lists folders with size, last use and stale tiles. Missing tiles are re-checked by `update` like any other (after 90 days). Checks never stop a search: offline or on errors the cached data is used with a warning. |
| OSM | Themes (coastline, cliffs, crags...) in `osm/themes/<theme>/<tile>.geojson` on a fixed 0.25° grid, filled from Overpass by default. `findaline osm download <country>` adds a Geofabrik extract in `<country>/osm-extract/`; when present, themes are built from it and `findaline osm update` applies the daily diffs. Fallbacks as today: world water polygons, then Overpass, then the Copernicus mask. |
| Runs | Each run records the data it used (`data` in `run.json`: folder, number of files, oldest and newest fetch time, survey years of sheets); `cache status` lists runs computed on data that has changed since. |

**Layout**
```
cache/
  LAYOUT                                   # layout version; a mismatch tells the user to delete the cache
  poland/        index.sqlite, nmt/<year>_<sheet file>.tif, nmpt/<year>_<sheet file>.tif (sheets never change: no edition level), ortho/live/wm256/{z}/{x}/{y}.jpg, osm-extract/
  malta/         index.sqlite, dtm/2018/epsg32633-1m-2000px-tif/<i>_<j>.tif, dtm/2012/epsg25833-..., dsm/..., ortho/2018/wm256/...
  spain/         index.sqlite, mdt05/live/epsg25830-.../, mdt05/live/epsg4083-.../, mds05/live/..., ortho/live/...
  copernicus30/  index.sqlite, 2021/dem/, 2021/wbm/       # the AWS bucket serves the 2021 release (since 2022-05-09)
  fabdem/        index.sqlite, v1-2/
  nasadem/       index.sqlite, v1/
  osm/           index.sqlite, water-polygons/<date>/, themes/<theme>/<tile>.geojson, nominatim.sqlite
runs/
work/<id>/                                 # per search, deleted unless it crashed
```

**How each dataset is checked**
| Policy | Datasets | Check |
| --- | --- | --- |
| `static` | Malta 2012/2018, FABDEM, Copernicus, NASADEM (OpenTopography allows 50 calls/day) | never; a new release is a code change |
| `conditional` | world water polygons, S3/HTTP files | `If-None-Match` / `If-Modified-Since` |
| `sheet-index` | geoportal.gov.pl NMT/NMPT | the sheet index WFS (below): a newer sheet of the same godło exists |
| `ttl` | WMS without dates, Overpass tiles, `missing` | age of the last check |

When a server gives no dates, the tile is downloaded again and only its check time changes if the sha256 is the same.

**geoportal.gov.pl sheet index** (checked 2026-10-24): WFS services `PZGIK/NumerycznyModelTerenu{KRON86,EVRF2007}/WFS/Skorowidze`, `PZGIK/NumerycznyModelPokryciaTerenu{KRON86,EVRF2007}/WFS/Skorowidze` and `PZGIK/ORTO/WFS/Skorowidze` have one layer per year (`gugik:SkorowidzNMT2024`, `SkorowidzNMPT2026`, `SkorowidzOrtofomapy2025`...). A sheet has `godlo`, `akt_rok` (year only), `nr_zglosz`, `char_przestrz` (1 m, 5 m...), `uklad_h`, `zrodlo_danych` and `url_do_pobrania`. The NMT/NMPT cache holds sheet files, which never change: a new survey is a new file. An update asks the index for the sheets over the cached area and downloads the ones that are newer than the cached sheet of the same godło; the older file can then be pruned. The orthophoto (WMS tiles) stays `ttl`, or the orthophoto index can mark tiles over a newer sheet as stale. The KRON86 indexes end in 2019; newer surveys are only in the EVRF2007 ones (see "To check").

**Moving to it**: the existing folders are moved by hand once (no migration code), `cache/osm/coastline` is deleted, saved maps get the new orthophoto paths. README, regression-tests.md (`--update` instead of `--refresh`) and setup-for-regular-folk.md are updated.

## Installation and platforms
The goal is Windows, Linux and macOS (Intel and Apple Silicon), installable by people with little technical knowledge.

- **One lock file, two ways to run it.** `pyproject.toml` + `uv.lock` (uv) replace `requirements.txt`; both ways below install exactly the same versions.
  - **Users, R&D, Windows:** install uv with one command, then double-click `start.bat` / `start.command` (macOS) / `start.sh` (Linux). uv downloads the right Python by itself, so there is no system Python, PATH, venv or PEP 668 trouble (Debian 12 ships Python 3.11, the project needs 3.12+). setup-for-regular-folk.md shrinks to two steps.
  - **Docker (maintainer, server):** multi-stage `Dockerfile`. The build stage uses the official uv image and `uv sync --frozen`; the runtime stage is slim, has no uv and runs as non-root. `docker compose up` runs the local version (one container, SQLite); stage 4 adds Postgres/Redis to the same image. Keep the data folder (`cache/`, `runs/`, `work/`) in a Docker volume, not a bind-mounted Windows folder (memmap is much slower there), and give Docker Desktop enough RAM and cores.
- **Platform rules for the code:**
  - The worker is a separate process and uses the `spawn` start method on every OS, so Linux behaves like Windows/macOS. (Python 3.14 changes the Linux default to `forkserver`.)
  - Each job gets its own work folder, and memmapped files are closed before they are deleted: Windows cannot delete or overwrite an open file.
  - `pathlib` for paths; `webbrowser` to open the browser.
- **CI**: GitHub Actions on `windows-latest`, `macos-latest` and `ubuntu-latest` runs a small search on a fixture raster (no network) and starts the web server, plus a Docker build.
- **macOS:** a downloaded `start.command` may need right-click → Open the first time (Gatekeeper) and can lose its executable bit when unzipped; the guide covers both.
- **Resources** are shown before a search: the cache needs several GB (the OSM water polygons alone are 1.2 GB), large areas need a lot of RAM.
- Not planned: signed installers (PyInstaller/Briefcase), which mean 300–500 MB packages, SmartScreen/Gatekeeper warnings without paid signing, and antivirus false positives. The public website (stage 4) is the no-install option.

## Language
Python stays. It has the best geospatial stack (GDAL/rasterio, pyproj, shapely, scipy), binary wheels for all three platforms, the working code and the regression baselines. Other languages considered:
- **Rust:** fast, a single binary, can compile to WASM. But GDAL needs a system library, which loses the easy install, and it would be a full rewrite.
- **Go:** weak geospatial and numeric ecosystem.
- **C++:** native GDAL but the hardest to build and maintain on three platforms.
- **TypeScript/Node:** no usable GDAL or numeric stack at this scale.

Speeding up:
- Done (2026-10-24): the pair search is in numba (`linesearch.py`): free directions per anchor, bucketed pairs, early-exit line check. Same lines as before, the pair search is about 150–750× faster (R4: 201 s on 23 processes → 0.3 s; R5 with the sag model: 283 s → 2 s).
- Now the time goes to downloading, gap/water filling, profiles and the map. Profile these before large areas.
- If ever needed: a **Rust extension (PyO3/maturin)**; the same code could be compiled to WASM for the viewer, ending the duplicated `Sag`/`groupLines` code in Python and JS.

The static viewer (stage 3) is written in **TypeScript**.

## Web architecture: options considered
| | Pros | Cons |
| --- | --- | --- |
| **A. FastAPI + queue + worker (chosen)** | Typed validation with limits (pydantic); OpenAPI docs; heavy computing never blocks the server; the same code runs locally (SQLite queue) and publicly (Postgres/Redis); easy to add endpoints. | Accounts, CSRF and rate limiting come from extra libraries; more dependencies. |
| B. Django + Celery / Django-Q | Best security defaults (accounts, CSRF, sessions, headers), admin panel, migrations. | Heavy for a single-purpose tool; live progress needs Channels or polling; most work up front. |
| C. Flask + HTMX | Least code at the start. | Security assembled from extensions, easy to miss something; long jobs still need a queue; grows messy. |
| D. Computing in the browser (Pyodide/WASM) | No server cost, nothing to trust. | Large rewrite (Web Workers instead of multiprocessing), slower; WCS services may block CORS (a proxy is needed anyway); large areas impossible. |

## Security
These apply from stage 3 on, the local version included.
- **Abuse of computing power is the main risk.** Every request goes through `estimate_search` with limits on area, pairs, minimum resolution and time. Jobs have a timeout and can be killed; the queue has per-user limits; the public version adds rate limiting.
- **No file paths over the web.** `--elevation-file`, `--surface-file` and `--data-dir` are CLI only. Results are addressed by UUID, with no directory listing.
- **No user-supplied URLs.** The server talks to a fixed list of services only (SSRF).
- **Uploads**: size limit, vertex limit, KML/GPX parsed with `defusedxml` (XXE).
- **XSS / CSP**: names from Nominatim and user input are escaped. The static viewer allows a strict Content-Security-Policy without inline scripts.
- **The local server is reachable by any website the user visits** (CSRF, DNS rebinding). The server checks the `Host` and `Origin` headers and requires a session token.
- **Secrets** (OpenTopography key) stay in server environment variables and never reach results or logs.
- **Public deployment**: HTTPS only, security headers, non-root read-only containers, pinned dependencies checked with `pip-audit`, audit log.
- **No shell anywhere**: the worker calls `run_search()` directly.

## To check
- **Obstacles lowered by averaging the DSM** (measured 2026-10-26 at Lauterbrunnen, 0.5 → 1 m): the highest 0.5 m value of a pixel is above its mean by 0.25 m (median) and 7.7 m (99th percentile), so single treetops come out lower than they are; at 2 m and coarser more so. Taking the maximum of the DSM instead does not work: the clearance limit comes from the anchor heights in the averaged DTM, and anchors lie on cliff edges, where the maximum of a pixel is the rim (up to 189 m above the mean on vertical walls); the pixels next to every anchor and along every wall would become obstacles, so nearly every line from a cliff would be marked as over trees, and single wrong DSM points would spread over whole pixels. To try instead: the maximum of the obstacle height above the ground (DSM − DTM on the data's grid), added to the averaged DTM; on bare rock it is about 0, tree crowns keep their height. Costs: both models on one fine grid first (Polish NMPT 0.5 m, NMT 1 m), mixed pixels on slopes and forest edges, changed results over trees. Measure on the regression places before deciding.
- **Hugging Face for the Slovak tiles**: dataset repository or Storage Bucket (FABDEM's mirror is a bucket); the docs disagree on a soft cap per public repository (300 GB or 1 TB) and on limits for anonymous downloads; check before uploading (about 140 GB at 1 m, about 530 GB with the 0.5 m cycle).
- **geoportal.gov.pl sheet files instead of WCS** (done 2026-10-25, `SheetTiles`: newest sheet of 0.5–1 m per godło, cached as GeoTIFF, working grid aligned to the sheet pixels, offline from the cached sheets; the Polish regression tests now take 3 min instead of 15). Findings: the EVRF2007 NMT index covers all of Poland (63 465 sheets 1:5000 of ≤ 1 m in PL-1992, plus PL-2000 sheets in some cities), the NMPT about 85% (53 846, all laser scans, `.asc`); the newest NMT sheet is `.asc` for 94% of the sheets, `.xyz.zip` for most others; in 56% of the sheets it is labelled "aerial photos", but in a sample these held the values of the earlier laser scan (4 of 5 identical, 1 within ±3 cm). Before: the EVRF2007 WCS spends 30–50 s generating each 2 km ASCII tile (75 MB, 55 s in all; four parallel requests take as long as one), while the sheet files linked from the WFS indexes (`url_do_pobrania` on `opendata.geoportal.gov.pl`) download at about 17 MB/s: a 1 m NMT sheet of about 2.3×2.3 km (37 MB) in 2 s, roughly 30× faster per km². It is also the download path the geoportal pages describe, and the cache would then match the units GUGiK updates (sheet, year, `nr_zglosz`). To check first: whether the EVRF2007 indexes cover all of Poland with 1 m sheets, the sheet formats (`.asc`, `.xyz.zip`, `.zip`), the NMPT sheets, and choosing between sheets of different years and sources (laser scan vs aerial photos). The geoportal pages give no limits for parallel requests; technical contact: service.desk@gugik.gov.pl.
- **geoportal.gov.pl height system** (done 2026-10-24: switched to EVRF2007 at native resolution, averaged locally, Polish tests re-baselined; the server's own downsampling left most of the Tatras empty at 5 m): the sources used the KRON86 coverages (`DTM_PL-KRON86-NH_TIFF`, `DSM_PL-KRON86-NH`), whose sheet indexes end in 2019. The WCS also has `DTM_PL-EVRF2007-NH` (ASCII grid only) and `DSM_PL-EVRF2007-NH`. Checked on 2026-10-24 by comparing 200×200 m windows of both coverages with the sheet files (difference of differences, so the resampling of the WCS cancels out): the difference between the two coverages equals the difference between the newest KRON86 and the newest EVRF2007 sheet (residual median 0.00 m, p5–p95 about ±0.2 m) at Żabi Koń (2013 vs 2023), Kasprowy (2012 vs 2025), Sokoliki (2012 vs 2025) and Gdańsk (2018 vs 2024); in Warsaw the EVRF2007 coverage matches the 2025 sheet within ±0.1 m. So **the KRON86 coverage holds surveys up to 2019 and the EVRF2007 one the newest**, 5–13 years newer in these places. Before step 2: switch NMT and NMPT to EVRF2007 (NMT only as ASCII grid: `fmt="image/x-aaigrid"`) and re-baseline the Polish regression tests. To watch: some newest sheets come from aerial photos, not laser scanning (Sokoliki 2025); at Żabi Koń the 2024 "aerial photo" sheet holds exactly the values of the 2023 laser scan.
**Before going public**
- **Data licences** (checked 2026-10-24):
  - FABDEM: CC BY-NC-SA 4.0 (non-commercial only; stated for V1-0, the V1-2 record at data.bris.ac.uk could not be read).
  - Malta 2018: the INSPIRE records say no conditions on access and use, and the data portal lists it as a High Value Dataset (Regulation 2023/138, which requires CC BY 4.0 or more permissive). Malta 2012: the MSDI records were reported as CC BY-NC-ND 4.0, the INSPIRE ones as no conditions; unresolved.
  - IGN: CC BY 4.0 (Orden FOM/2807/2015), attribution with product, date and "CC BY 4.0".
  - swisstopo: open government data, free use including commercial, "© swisstopo" required; the download service asks for no bulk bot traffic (backoff with jitter, few detailed orthophoto tiles).
  - GUGiK: free for any use; the source must be stated (geoportal terms § 3.2).
  - ÚGKK SR (Slovakia, ALS models and ZBGIS orthophoto): CC BY 4.0, author ÚGKK SR (the licence PDF in the ZIPs; the WMS capabilities say the same).
  - OSM: ODbL, attribution.
  - Esri World Imagery: Esri Master License Agreement; the layer is not meant for offline tile export (there is a separate "for Export" layer), so it stays online only.
- **Public services:** the public Nominatim and Overpass instances forbid heavy use. A public site needs caching or its own instances.
- **Accounts** (stage 4): a decision, not a check; to be made when stage 4 starts.

## Resampling to the working resolution (measured 2026-10-26)
- **What is cached**: only the data as the services deliver it (Polish sheets at 1 m, Malta tiles at `fetch_res` 1 m, Spain at 5 m, swisstopo files at 0.5 and 2 m); the same files serve every working resolution. The two fixed conversions on download (MDS05's 5.0045 m grid to 5 m, Malta 2018 served at 1 m by the WCS) do not depend on the chosen resolution. Bringing the data onto the working grid happens in every search, into `work/<id>/` (`nmt.npy`, `nmpt.npy`, masks), deleted when the search ends.
- **Updates**: `cache update` / `--update` replaces source files only, so nothing derived needs to be invalidated; the next search builds its mosaics from the new files. Saved runs record the files they used (`run.json`), and `cache status` lists the runs whose files were fetched again or replaced since; they are not recomputed automatically.
- **Cost** (cached data, numba warm, one process; time of `build_mosaic`, split into tiles copied onto the grid and tiles resampled):

  | Search | Without downloads | Resampling | Share |
  | --- | --- | --- | --- |
  | Żabi Koń r 1 km, 1 m (default) | 1.7 s | 0.04 s (water mask only) | 2.5% |
  | Żabi Koń r 1 km, 2 m | 0.8 s | 0.28 s | 35% |
  | Będkowska r 1 km, 1 m (R4) | 1.3 s | 0.04 s | 3% |
  | Lauterbrunnen r 400 m, 1 m (R20) | 1.3 s | 0.17 s | 13% |
  | Lauterbrunnen r 400 m, 2 m (R19) | 0.3 s | 0.09 s | 24% |
  | Dingli r 1 km, 1 m (sea filling) | 16.3 s | 0.04 s | 0.2% |
  | Montserrat r 1 km, 5 m (R9) | 17.2 s | 0.01 s | 0% |
  | R1 circle as an area in 600 m tiles, 1 m (R14) | 7.7 s | 0.45 s (water mask) | 6% |

  At the default 1 m the Polish and Maltese data is already on the grid and only copied; Switzerland averages its 0.5 m files (13%). At 2 m the share is larger only because the whole search takes under a second. The start of the process and the imports (a few seconds) are not counted, so the share of the time a user waits is smaller still.
- **Decided**: no cache of resampled tiles: it would save at most a few tenths of a second per search.
- **Possible optimisation for large areas**: in an area computed in tiles (R14) building the mosaics takes about a third of the time (2.1 s of 7.7 s), all of it copying: every tile, with its overlap, reads the same sheets again.

## Very large areas (measured 2026-10-26, Switzerland)
- **What happened**: the first tile (8 km core, 1 m) of a search over Switzerland gave 12.49 million lines in 79 529 spots (about 157 lines per spot). The worker held about 18.6 GB, grouping took 7 min 18 s, and then the tile file (`tiles/<k>.pkl.gz`, 836 MB) was written for about 11 minutes on one core with no progress at all: gzip at level 9. On a tile of Gozo (690 000 lines, 141 MB pickled) level 1 gives 53 MB in 1.6 s, level 6 44 MB in 4.6 s, level 9 42 MB in 37.7 s; reading takes about 0.35 s at every level, pickling 1.7 s. Threads would not help much: pickling holds the GIL, and at level 1 compressing takes about as long as pickling.
- **Done**: tile files at level 1, pickled in chunks of 100 000 lines with progress for saving and loading; the step shown while a tile is saved or loaded; step 5 counts lines instead of files (`lines.csv`, `lines.geojson`, `data.js`, `lines.js` are written in chunks, with the same text as before).
- **Still open: the size of the results.** Per line (Gozo, 237 040 lines): `lines.csv` 140 B, `lines.geojson` 610 B, `lines.js` 154 B, `data.js` about 195 B, plus about 2.4 KB per profile of a representative. All lines of that one Swiss tile: about 14 GB of results; only the representatives: about 270 MB. A `data.js` of gigabytes is beyond what a browser can parse (V8 strings end at about 0.5–1 GB), so the map of such a run does not open. Saving also builds every row in memory (tens of GB for millions of lines).
- **Options**:
  - (a) **Lines as numpy columns** instead of one dict per line (about 100 B instead of about 1.5 KB per line): lowers the peak of a tile and makes tile files fast. Care needed: the order of `group_lines` must stay that of `groupLines` (ties included: a stable `np.lexsort`), `round` of Python and `np.round` differ (convert with `tolist()` before rounding, as the profiles do), `None` of `clearance_nmpt` becomes NaN and must be written back as `None`, profiles are kept apart (index → profile).
  - (b) **Only the representatives saved** above a number of lines, each with its group size and the ranges of length and clearance. Lost in the map: grouping again with another distance or order, filters applied before grouping, the thin variants, the view without groups; at tile borders the greedy grouping needs a band of all lines, or groups there may differ a little from one grid (R14 would need a new definition).
  - (c) **A viewer served by the app for large runs**: all lines stay in a compact file on disk; the page asks for the lines in view, variants, profiles (`/api/profile` exists) and exports when needed. Grouping of the visible part only is quick but may differ at the edges of the view from grouping the whole run; groups for the run's own distance would be computed once when saving. This reverses, for large runs only, the decision that the viewer works from `file://` for the command line too (small runs keep the static `data.js`), and frees such runs from keeping `grouping.ts` in step with Python.
- **Proposed order**: (a) first, as the base of both; then (c); (b) only if (c) is dropped.

## Open pits and other terraced walls (looked at 2026-10-25, Bełchatów)
- The Bełchatów and Szczerców pits (200–250 m deep, walls of benches, about 20° overall) gave lines of 1.1 km at most, all near the bottom: the rims have no pixels of 45° or more, and with 5.5 kN and 70 g/m the sag is about 45 m at 1 km, 114 m at 2 km, 207 m at 3 km and 475 m at 5 km, deeper than the pits for lines from rim to rim. With 10 kN and 45° lines of up to 1.9 km appear across the Bełchatów pit.
- Considered, not chosen for now: anchors on edges (the terrain drops by X m within Y m) instead of the slope of one pixel, so that the rim of a terraced wall is an anchor. *Measure* in the map (two points, the profile from the run's data, anchors above the ground) was built instead, to see why a line expected there is missing.
