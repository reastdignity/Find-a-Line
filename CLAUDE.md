# Find-a-Line

Finds possible highlines in elevation data: anchors on steep pixels, every pair checked for clearance under the webbing, results shown on an interactive Leaflet map. User-facing docs: [README.md](README.md) (reference) and [setup-for-regular-folk.md](setup-for-regular-folk.md) (step-by-step guide). Plans, decisions already made, their order and open checks: [docs/roadmap.md](docs/roadmap.md); read it before proposing a design.

## Run it
- Python 3.12+ (scipy 1.18 needs it). Dependencies in `pyproject.toml`, pinned in `uv.lock` (`uv sync`, `uv run findaline.py ...`; the user's `.venv` is git-ignored). For a session environment outside the repo: `UV_PROJECT_ENVIRONMENT=<dir> uv sync --frozen` (uv itself can be pip-installed into any venv: `python -m uv`). After changing dependencies run `uv lock` and the full regression set.
- Tests: `pytest` (offline, ~30 s: synthetic canyon + Żabi Koń clip in `tests/data`, units, the viewer against Python, the web app) and `python tests/regression.py` (real searches, see docs/regression-tests.md). `tests/test_viewer.py` is skipped without Node and `npm install` in `viewer/`.
- Before adding a dependency, check the current docs of the version that gets installed (Starlette 1.x: no `add_event_handler`, use `lifespan`; its TestClient wants `httpx2`, not `httpx`) and run the tests with deprecation warnings visible.
- `python findaline.py --lat 50.1559508 --lon 19.7562722` (Żabi Koń, Poland). `--help` lists everything. Other commands (first argument): `cache status|update`, `osm download|update` (update.py), `web` (web.py).
- `python findaline.py web --no-browser --port 8799 --data-dir <scratch>` runs the web app for tests (start it in the background). Headless Edge waits for the open event stream: stub `EventSource` in a test page (see docs/regression-tests.md) or use `--timeout` for screenshots.
- `--no-plot` is required when running without a desktop session (the matplotlib window blocks). `--yes` skips the confirmation questions; without a terminal they default to "no".
- Long runs: start them in the background and wait for the notification. Each search has its own `work/<id>/` and downloads write unique `.part` files, so searches may overlap, but tests run one after another (each search uses all cores).
- Data folder (`--data-dir`, `FINDALINE_DATA`, default the repo): `cache/`, `runs/`, `work/`; layout and index in `cache.py`. A cache without the current `cache/LAYOUT` is refused: no migration code, move or delete the user's folders by hand.
- Without a terminal, progress is printed as `[Download] ...` / `[Compute] ...` lines every 10 s; `FINDALINE_PROGRESS=bars|text` forces a mode.

## Code map
| File | What it does |
| --- | --- |
| `findaline.py` | Command line only: options from `params.py`, messages, questions, the list of spots, the matplotlib window; `cache`/`osm`/`web` as first argument go to update.py / web.py |
| `params.py` | `SearchParams` (pydantic: fields, help, groups, validation) and `CliOptions`; argparse is generated from them |
| `search.py` | The search as a library: `plan_search`, `estimate_search`, `download_data` (cache only, for the web worker), `prepare_data` (step 1), `find_lines` (2-4), `save_run` (5), `run_search`; messages via `on_event`, questions via `confirm` |
| `area.py` | Search areas (`Circle`, `Polygon`): grid bounds, mask, shapes for maps and orthophoto; `from_spec` builds `--area` (coast, cliffs, file, osm:name) |
| `sources.py` | Data sources (`SOURCES`), tiled download (WCS tiles, 1° tiles, geoportal sheets), mosaics, water and gap filling |
| `cache.py` | Data folder (`cache/`, `runs/`, `work/`), cache layout, `index.sqlite` per top-level folder |
| `osm.py` | OSM themes in 0.25° tiles (`coastline`, `cliff`) from Overpass or the extracts, cached Nominatim answers |
| `coast.py` | Sea mask: OSM coastline (theme tiles from Overpass or extracts, or the world file) or the Copernicus water mask |
| `grid.py` | Working grid (metric CRS: the source's, or the UTM zone of the centre; edges on the source's pixel edges, `Source.grid_origin`) |
| `helpers.py` | Anchors, line evaluation (DDA), line profiles |
| `linesearch.py` | The pair search (numba): free directions per anchor, bucketed pairs, early-exit line check |
| `sag.py` | Sag estimator (elastic catenary + point load) and envelope table for `--model sag` |
| `mapping.py` | Coordinates, grouping, matplotlib map; `save_map` writes a run's `map.html` + `data.js` for the viewer |
| `viewer/` | The result viewer (TypeScript, Preact, Leaflet; `src/`), bundled by esbuild into `dist/` (committed); copied to `runs/_viewer/` by every run |
| `web.py` | `findaline.py web`: FastAPI on 127.0.0.1 (Host check, session token, CSP), job queue in `jobs.sqlite`, worker processes (download, then compute), SSE events; the page is `viewer/src/app` |
| `update.py` | `findaline.py cache status/update` and `osm download/update`: which cached data can change, checks, runs on changed data |
| `osmextract.py` | Geofabrik OSM extracts: download, replication diffs (pyosmium), theme features, theme tiles built from extracts |
| `orthophoto.py` | Orthophoto tile cache (XYZ tiles requested from a WMS in EPSG:4326) |
| `runs.py` | Saved runs (`runs/<date>_<place>/`), `runs/index.html`, `runs/runs.js`, run names |
| `progress.py` | Two tqdm bars (Download / Compute); listeners (`progress.subscribe`) for the web worker |

## Things that must stay in sync
- `mapping.group_lines` (Python) and `groupLines` (`viewer/src/grouping.ts`): the same greedy algorithm with the same cell index; the browser must produce the same groups. Values used for grouping are passed to the viewer unrounded.
- `sag.py` and `viewer/src/sag.ts`: the same solver; results match to 1e-6 m. `tests/test_viewer.py` checks both pairs with Node, and that `viewer/dist` is built from `viewer/src`.
- The run data written by `mapping.save_map` (`data.js`, `lines.js`), the list in `runs.write_index` (`runs.js`) and their types in `viewer/src/types.ts`.
- `params.py` help texts and the options table in README.md describe the same options. The web form is generated from `SearchParams` too (`web.schema`): a new field that takes a file path or a secret must be `cli_only`, or the web would accept it.
- Tiled areas (`search.search_lines`): a line belongs to the tile whose core holds its first anchor (smaller x, then y); the overlap is `--max-length` + 2 pixels so slopes at the edge match one grid. R14 and `tests/test_offline.py::test_area_in_tiles` check that tiles give exactly the lines of one grid (inland, no water: water and gap filling work per tile and may differ at tile borders).
- `helpers.max_safe_elevation` defines the clearance limit; `evaluate_line` and `line_profile` use it. `linesearch._line_ok` repeats it in numba (formula and sag table) and `find_valid_lines` derives the weakest per-anchor limit from it (`gain`, `cap`, `offset`): change all three together. The kernels may only pass too much, never too little (EPS); `evaluate_line` makes the final decision, so results equal the plain pixel-by-pixel search.

## Pitfalls learned the hard way
- **numba** compiles `linesearch.py` on the first run (a few seconds, cached in `__pycache__`). Inside `@njit` code only numpy/math and module constants work; keep the float operations in `_line_ok` in the same order as `max_safe_elevation`.
- **The viewer** works from `file://`: no `fetch`, so a run's data is a script (`data.js`) and the list of runs too (`runs.js`); everything it needs (Leaflet, its CSS and marker images) is in the bundle, no CDN. After changing `viewer/src`: `cd viewer; npm install` (once), `npm run check` (types), `npm run build`, and commit `dist/`. Users need no Node.
- **Pipes and heredocs**: piping the script's output into `head` kills it before the map is saved. Complex multi-line edits are safer as a Python script written to the scratchpad than as a bash heredoc.
- **geoportal.gov.pl data comes from sheet files**, not WCS (`SheetTiles`): the EVRF2007 WCS takes 30–50 s per ASCII tile (generating the text, not downsampling, is the cost), its own downsampling leaves holes at coarse resolutions (Tatras at 5 m), its GeoTIFF output is an 8-bit picture, and the KRON86 coverages stop at 2019. Index answers are cached per 0.25° tile in `cache/poland/sheet-index/`. The sheet files on opendata.geoportal.gov.pl download at ~17 MB/s; the geoportal pages give no request limits (technical contact service.desk@gugik.gov.pl). Sheet attributes and coverage are in docs/roadmap.md ("To check").
- **Half-pixel grid alignment**: data whose pixel centres lie on whole metres (geoportal sheets: `xllcenter`) must get `grid_origin=0.5`, otherwise the mosaic interpolates it bilinearly half a pixel off and smooths cliff edges (R2 at 1 m lost 40% of its lines). Comparing rasters from two services needs the same care.
- **Telling surveys apart**: two services resample differently, so compare their difference with the difference of the candidate source files (difference of differences); a residual around 0 shows which survey each serves (used for KRON86 vs EVRF2007).
- **WCS services differ**: axis labels (`x/y` vs `E/N`), multipart responses (coverage part may be `image/*` or `application/*`), formats (some GeoTIFFs are whole metres; ASCII grids keep centimetres), server limits (~2000×2000 px), interpolation support.
- **Check georeferencing of every new source** against Copernicus DEM (shift search, see regression doc). Malta 2012 is off by (-56 m E, -196 m N), corrected by `MALTA_2012_SHIFT`.
- **Water/gap filling order**: sea zone first, then gaps on land outside it, then water. Gap offsets are matched on gentle slopes only and limited to ±3 m from the overall offset; no anchors at gap seams. Cliffs can be vertical: high land next to water is normal.
- **Public services are flaky**: Overpass is often overloaded and slow (30–70 s per 0.25° theme tile; the world file in `cache/osm/water-polygons/` is used instead when present; inland areas skip the coastline); the OpenTopography demo key `demoapikeyot2022` has a shared limit of 50 calls/day; Nominatim allows 1 request/s.
- Grid snapping and the circle mask make results sensitive to the exact centre: a centre 0.4 m away changes a few lines at the edge.
- **Updates** (`update.py`): a dataset that can change needs a TTL (`cache.TTL_DAYS`, `Layer.ttl_days` from `/live` in the folder) and a way back from its path to its tile (`Layer.tile_of`), otherwise `cache update` skips it. `osm.THEME_TAGS` defines a theme for both Overpass and the extracts: a new theme is one entry there, and the extracts must be taken again (`osm update` or a new download).
- **pyosmium**: the extracts are read in three filtered passes (tagged objects, ways of relations, nodes by id) instead of `with_locations()`, whose node index for Poland would take gigabytes; Poland (2.1 GB) takes about 10 s. Geofabrik keeps dated extracts (`<region>-YYMMDD.osm.pbf`), handy for testing the diffs. The OSM world water polygons are regenerated daily, so they are only checked after a year (a check means a 900 MB download).
- **viewer/dist is committed**: the global `dist/` rule of `.gitignore` has an exception (`!viewer/dist/`), and `.gitattributes` marks it `-text`, so git never changes its line endings (the test compares it byte for byte with a fresh build). Start scripts keep their line endings (`*.sh`/`*.command` LF, `*.bat` CRLF).
- **The web worker** runs each phase in a `spawn` process; its stdout becomes `info` events, and `progress.TEXT_INTERVAL` is set to infinity there (progress goes as events). A tiled area stopped by cancelling or closing the app resumes on the next start of the same search. The page is drawn again on every queue event: form fields must not take their value from state that changes only on `change` (text fields: `onInput`; number fields: `defaultValue`), or typing is reset.
- **Shell on this PC**: `cat > file` without a here-document waits for input forever; nested quotes in `python - <<EOF` scripts break easily (write the script with the Write tool instead). Stop only test browsers: filter `msedge.exe` by the `--user-data-dir` of the test (PowerShell `Get-CimInstance Win32_Process`), never all Edge processes of the user.

## Verifying changes
- Follow [docs/regression-tests.md](docs/regression-tests.md): run the affected locations and compare with the expected numbers.
- Map/JS changes: `tests/test_viewer.py`, then generate a small map (the Żabi Koń clip in `tests/data`, offline), append a test `<script>` to a copy and run headless Edge (`msedge --headless --dump-dom` for checks, `--screenshot` for a look; `window.FINDALINE_MAP` is the Leaflet map). Synthetic `mousemove` works; headless screenshots don't keep hover state. "Other runs" across coordinate systems: reproject the clip to UTM (rasterio) and run it as a second run.
- Cache update code: a scratch `--data-dir`, `checked_at` aged by hand in its `index.sqlite`, then `cache status` / `cache update` (see the regression doc).
- New download code: test on an empty cache (`tests/regression.py --cache <temp>/cache`, or a scratch `--data-dir`), so the real cache is not touched.
- New data source or download path: also check the share of valid pixels at the coarsest resolution (5 m) and compare a few tiles with the source files (see the regression doc, "Data checks for a new source").
- The Polish set (R13, R1–R5) takes about 3 min with an empty geoportal cache; run the tests one after another, never two searches at once.

## Conventions
- Code, comments, docs and commit messages in English; conversation with the user in Polish.
- Match the existing style (docstrings, comment density). Commit messages end with the `Co-Authored-By` line.
- Work happens on the `rework-webui-websources` branch (local, ahead of `main`); nothing is pushed unless the user asks.
- Generated data (`cache/`, `runs/`, `work/`, `findaline_map.html`) is git-ignored. The user is the only user: no migration or backward-compatibility code for old caches or runs.
