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
5. **Profiling, then stage 2**: measure where the time goes (downloading, gap/water filling, profiles, map) first. Profiled 2026-10-25 with cached data (cProfile): the line search itself is quick (R9: 3 s of 50 s); the time went to per-line Python work, line profiles (15 s, half of it rounding), grouping (10 s, every group scanned all lines), saving `lines.csv`/`lines.geojson` (12 s) and the folium map (7 s, 4 s of it compiling Jinja templates), plus 5 s of geoportal index queries in every Polish search. Done: exact rounding through `tolist()`, grouping with a grid of cells (only lines near the representative are tested), index answers cached per 0.25° tile; `lines.csv` and the maps of all 13 tests are byte-identical, R9 50 → 20 s, R4 8.6 → 3 s. Still to do for large areas: saving the results.

   **Stage 2 done 2026-10-25**: `--area coast|cliffs|<file>|osm:<name>` with `--buffer` and `--within`; tiles with an overlap of `--max-length` (+2 pixels), sized from the free memory (`--tile-size` to override; rounded to 500 m and kept in `progress.json` so a resumed run uses the same tiles); every finished tile saved as `tiles/<k>.pkl.gz` (not `.npz`: the results are dicts with profiles), an unfinished run of the same search is found and resumed automatically; confirmation with km², tiles and download; in maps of areas only the representatives have a profile, detailed orthophoto for `--ortho-detail` (50) groups. Data tiles (WCS, sheets) are picked under the area outline. Checked: R1 as a polygon gives exactly R1's 1262 lines, as one grid and in 15 tiles (R14), and after an interruption the resumed run too. Decided: the coast band covers both sides of the coastline (anchors at sea are flat anyway), but `--within` clips it (the OSM outline of Gozo is the land). Differences from the plan: none of the area sources was left out. Then areas: GeoJSON file first, then `coast` and `cliffs` (they use the OSM themes from step 2), tiling and resuming last, with the tests ready (a polygon equal to the circle, a tiled area equal to one grid).
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
- **geoportal.gov.pl sheet files instead of WCS** (done 2026-10-25, `SheetTiles`: newest sheet of 0.5–1 m per godło, cached as GeoTIFF, working grid aligned to the sheet pixels, offline from the cached sheets; the Polish regression tests now take 3 min instead of 15). Findings: the EVRF2007 NMT index covers all of Poland (63 465 sheets 1:5000 of ≤ 1 m in PL-1992, plus PL-2000 sheets in some cities), the NMPT about 85% (53 846, all laser scans, `.asc`); the newest NMT sheet is `.asc` for 94% of the sheets, `.xyz.zip` for most others; in 56% of the sheets it is labelled "aerial photos", but in a sample these held the values of the earlier laser scan (4 of 5 identical, 1 within ±3 cm). Before: the EVRF2007 WCS spends 30–50 s generating each 2 km ASCII tile (75 MB, 55 s in all; four parallel requests take as long as one), while the sheet files linked from the WFS indexes (`url_do_pobrania` on `opendata.geoportal.gov.pl`) download at about 17 MB/s: a 1 m NMT sheet of about 2.3×2.3 km (37 MB) in 2 s, roughly 30× faster per km². It is also the download path the geoportal pages describe, and the cache would then match the units GUGiK updates (sheet, year, `nr_zglosz`). To check first: whether the EVRF2007 indexes cover all of Poland with 1 m sheets, the sheet formats (`.asc`, `.xyz.zip`, `.zip`), the NMPT sheets, and choosing between sheets of different years and sources (laser scan vs aerial photos). The geoportal pages give no limits for parallel requests; technical contact: service.desk@gugik.gov.pl.
- **geoportal.gov.pl height system** (done 2026-10-24: switched to EVRF2007 at native resolution, averaged locally, Polish tests re-baselined; the server's own downsampling left most of the Tatras empty at 5 m): the sources used the KRON86 coverages (`DTM_PL-KRON86-NH_TIFF`, `DSM_PL-KRON86-NH`), whose sheet indexes end in 2019. The WCS also has `DTM_PL-EVRF2007-NH` (ASCII grid only) and `DSM_PL-EVRF2007-NH`. Checked on 2026-10-24 by comparing 200×200 m windows of both coverages with the sheet files (difference of differences, so the resampling of the WCS cancels out): the difference between the two coverages equals the difference between the newest KRON86 and the newest EVRF2007 sheet (residual median 0.00 m, p5–p95 about ±0.2 m) at Żabi Koń (2013 vs 2023), Kasprowy (2012 vs 2025), Sokoliki (2012 vs 2025) and Gdańsk (2018 vs 2024); in Warsaw the EVRF2007 coverage matches the 2025 sheet within ±0.1 m. So **the KRON86 coverage holds surveys up to 2019 and the EVRF2007 one the newest**, 5–13 years newer in these places. Before step 2: switch NMT and NMPT to EVRF2007 (NMT only as ASCII grid: `fmt="image/x-aaigrid"`) and re-baseline the Polish regression tests. To watch: some newest sheets come from aerial photos, not laser scanning (Sokoliki 2025); at Żabi Koń the 2024 "aerial photo" sheet holds exactly the values of the 2023 laser scan.
**Before going public**
- **Data licences** (checked 2026-10-24):
  - FABDEM: CC BY-NC-SA 4.0 (non-commercial only; stated for V1-0, the V1-2 record at data.bris.ac.uk could not be read).
  - Malta 2018: the INSPIRE records say no conditions on access and use, and the data portal lists it as a High Value Dataset (Regulation 2023/138, which requires CC BY 4.0 or more permissive). Malta 2012: the MSDI records were reported as CC BY-NC-ND 4.0, the INSPIRE ones as no conditions; unresolved.
  - IGN: CC BY 4.0 (Orden FOM/2807/2015), attribution with product, date and "CC BY 4.0".
  - GUGiK: free for any use; the source must be stated (geoportal terms § 3.2).
  - OSM: ODbL, attribution.
  - Esri World Imagery: Esri Master License Agreement; the layer is not meant for offline tile export (there is a separate "for Export" layer), so it stays online only.
- **Public services:** the public Nominatim and Overpass instances forbid heavy use. A public site needs caching or its own instances.
- **Accounts** (stage 4): a decision, not a check; to be made when stage 4 starts.
