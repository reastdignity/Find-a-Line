# Find-a-Line

Finds possible highlines in elevation data: anchors on steep pixels, every pair checked for clearance under the webbing, results shown on an interactive Leaflet map. User-facing docs: [README.md](README.md) (reference) and [setup-for-regular-folk.md](setup-for-regular-folk.md) (step-by-step guide).

## Run it
- Python 3.12+ (scipy 1.18 needs it). `pip install -r requirements.txt`. There is no virtual environment in the repo; create one outside it.
- `python findaline.py --lat 50.1559508 --lon 19.7562722` (Żabi Koń, Poland). `--help` lists everything.
- `--no-plot` is required when running without a desktop session (the matplotlib window blocks). `--yes` skips the confirmation questions; without a terminal they default to "no".
- Long runs: start them in the background and wait for the notification. Do not run two searches at the same time: they share `cache/work/*.npy`.
- Without a terminal, progress is printed as `[Download] ...` / `[Compute] ...` lines every 10 s; `FINDALINE_PROGRESS=bars|text` forces a mode.

## Code map
| File | What it does |
| --- | --- |
| `findaline.py` | CLI and the pipeline (steps 1-5) |
| `sources.py` | Data sources (`SOURCES`), tiled download + cache (WCS tiles, 1° tiles, geoportal sheets), mosaics, water and gap filling |
| `coast.py` | Sea mask: OSM coastline (Overpass or the world file) or the Copernicus water mask |
| `grid.py` | Working grid (metric CRS: the source's, or the UTM zone of the centre) |
| `helpers.py` | Anchors, line evaluation (DDA), line profiles |
| `linesearch.py` | The pair search (numba): free directions per anchor, bucketed pairs, early-exit line check |
| `sag.py` | Sag estimator (elastic catenary + point load) and envelope table for `--model sag` |
| `mapping.py` | Coordinates, grouping, matplotlib map, HTML map (all browser JS lives here as Jinja templates) |
| `orthophoto.py` | Orthophoto tile cache (XYZ tiles requested from a WMS in EPSG:4326) |
| `runs.py` | Saved runs (`runs/<date>_<place>/`), `runs/index.html`, `runs/runs.js`, run names |
| `progress.py` | Two tqdm bars (Download / Compute) |

## Things that must stay in sync
- `mapping.group_lines` (Python) and `groupLines` (JS in `GROUPING_SCRIPT`): the same greedy algorithm; the browser must produce the same groups. Values used for grouping are passed to JS unrounded.
- `sag.py` and the `Sag` JS object in `mapping.py`: the same solver; results match to 1e-6 m. Check with Node after changes (see docs/regression-tests.md).
- `helpers.max_safe_elevation` defines the clearance limit; `evaluate_line` and `line_profile` use it. `linesearch._line_ok` repeats it in numba (formula and sag table) and `find_valid_lines` derives the weakest per-anchor limit from it (`gain`, `cap`, `offset`): change all three together. The kernels may only pass too much, never too little (EPS); `evaluate_line` makes the final decision, so results equal the plain pixel-by-pixel search.

## Pitfalls learned the hard way
- **numba** compiles `linesearch.py` on the first run (a few seconds, cached in `__pycache__`). Inside `@njit` code only numpy/math and module constants work; keep the float operations in `_line_ok` in the same order as `max_safe_elevation`.
- **Jinja templates** in `mapping.py`: never write `{#` in JS; use `tojavascript` (not `tojson`) for options that contain `JsCode`; inside the Python string, JS escapes need double backslashes.
- **Pipes and heredocs**: piping the script's output into `head` kills it before the map is saved. Complex multi-line edits are safer as a Python script written to the scratchpad than as a bash heredoc.
- **geoportal.gov.pl data comes from sheet files**, not WCS (`SheetTiles`): the EVRF2007 WCS takes 30–50 s per ASCII tile, its own downsampling leaves holes at coarse resolutions (Tatras at 5 m), its GeoTIFF output is an 8-bit picture, and the KRON86 coverages stop at 2019. Sheet files have pixel centres on whole metres (`xllcenter`); compare them with anything else only after aligning the half pixel.
- **WCS services differ**: axis labels (`x/y` vs `E/N`), multipart responses (coverage part may be `image/*` or `application/*`), formats (some GeoTIFFs are whole metres; ASCII grids keep centimetres), server limits (~2000×2000 px), interpolation support.
- **Check georeferencing of every new source** against Copernicus DEM (shift search, see regression doc). Malta 2012 is off by (-56 m E, -196 m N), corrected by `MALTA_2012_SHIFT`.
- **Water/gap filling order**: sea zone first, then gaps on land outside it, then water. Gap offsets are matched on gentle slopes only and limited to ±3 m from the overall offset; no anchors at gap seams. Cliffs can be vertical: high land next to water is normal.
- **Public services are flaky**: Overpass is often overloaded (the world file in `cache/osm/` is used instead when present; inland areas skip the coastline); the OpenTopography demo key `demoapikeyot2022` has a shared limit of 50 calls/day; Nominatim allows 1 request/s.
- Grid snapping and the circle mask make results sensitive to the exact centre: a centre 0.4 m away changes a few lines at the edge.

## Verifying changes
- Follow [docs/regression-tests.md](docs/regression-tests.md): run the affected locations and compare with the expected numbers.
- Map/JS changes: generate a small map (Kasprowy test), append a test `<script>` to a copy and run headless Edge (`msedge --headless --dump-dom` for checks, `--screenshot` for a look). Synthetic `mousemove` works; headless screenshots don't keep hover state.
- New download code: test with `--refresh` on a scratch `--cache-dir`, so the real cache is not touched.

## Conventions
- Code, comments, docs and commit messages in English; conversation with the user in Polish.
- Match the existing style (docstrings, comment density). Commit messages end with the `Co-Authored-By` line.
- Work happens on the `rework-webui-websources` branch (local, ahead of `main`); nothing is pushed unless the user asks.
- Generated data (`cache/`, `runs/`, `findaline_map.html`) is git-ignored. The user is the only user: no migration or backward-compatibility code for old caches or runs.
