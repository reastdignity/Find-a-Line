# Roadmap

Planned features and the decisions already made for them. Nothing here is implemented yet.

1. **Several points at once**: lines from several places shown together on one map.
2. **Irregular search areas**: e.g. the whole coast of Malta within 100 m of the shore, or a polygon drawn on the map.
3. **Running searches from the browser**: no terminal needed, built so it can later become a public website.

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

## Stage 0: refactoring
- `findaline.main(args)` becomes a library function `run_search(params, on_progress)`. One parameter schema (pydantic) is shared by the CLI, the API and the browser form, with types, defaults, limits and help texts in one place. argparse is generated from it.
- Area abstraction: `Circle(center, radius)` and `Polygon(geometry)`, both giving a boolean mask on the grid. Everything that now uses `center_row`, `center_col` and `radius_px` (anchor search, plots, orthophoto cache, run metadata) uses the mask instead.
- `progress.py` reports to a callback as well as to tqdm, so the server can forward progress.

## Stage 1: several points, overlaying runs
- `--point` may be given many times; the runs are computed one after another with the same parameters. `--lat/--lon`, `--northing/--easting` stay as the single-point form.
- Every run also writes `lines.js` (`window.FINDALINE_LINES[...] = {...}`), so a map opened from disk can load other runs with a `<script>` tag. It holds the spot representatives with their popup data, without profiles.
- Map panel "Other runs": a checkbox per saved run (nearby ones first) adds its lines and its search area, drawn in the run's own colour. Clicking such a line shows its popup and a link to open it in its own map (with the profile).
- Tests: R1 and R13 unchanged; two points within one map in headless Edge.

## Stage 2: irregular areas
**Engine**
- The grid covers the bounding box of the area. Anchors are searched only inside the mask; the pair search is unchanged.
- Tiles: squares of about 2 km with an overlap equal to `--max-length`. A line belongs to the tile that holds its first anchor, so there are no duplicates. Each finished tile is saved in the run folder (`tiles/<i>.npz`), and a restarted run skips those.
- Before starting, the script shows the area (km²), the number of tiles, the download size and an estimated time, and asks for confirmation.
- Rough scale: the coast of Malta and Gozo is about 190 km, so a 100 m band is 20–40 km² at 1 m. With the numba search the line search takes minutes; downloading the data dominates.

**Area sources (`--area ...`)**
- `coast --buffer 100`: land within the given distance of the OSM coastline (`coast.py`, world water polygons file), limited to a country/island or a bounding box.
- `cliffs --buffer 100`: OSM `natural=cliff` (optionally `climbing=crag`) from Overpass, buffered.
- `file.geojson|kml|gpx`: polygons as they are; points and lines buffered by `--buffer`.
- `osm:"Gozo"`: polygon of a named object from Nominatim (`polygon_geojson=1`).
- Polygons drawn on the map arrive as GeoJSON (stage 3, or exported to a file before then).

**Tests**: R1 with an equivalent polygon gives the same lines as the circle. A polygon split into tiles gives the same lines as one grid. Resume after interrupting.

## Stage 3: web application (local)
- `findaline_web.py` (plus `start.bat`) starts FastAPI on `127.0.0.1` and opens the browser. Locally, the API, the queue (SQLite) and the worker run in one process tree.
- Pages: map for clicking points and drawing polygons (Leaflet-Geoman); parameter form generated from the schema; live Download/Compute progress (Server-Sent Events); list of runs; result viewer.
- Static viewer: one JS/CSS bundle shared by all runs. Data per run comes as JSON (lines, groups, profiles), plus `lines.js` for `file://`. Grouping and the `Sag` port move from the Jinja templates in `mapping.py` into this bundle, and stay in sync with Python as today.
- One job runs at a time (the worker shares `cache/work`); the others wait in the queue and can be cancelled.

## Stage 4: public website (later)
- Docker Compose: reverse proxy with TLS (Caddy), API, worker(s), Postgres, Redis, object storage for results.
- Accounts (to be decided: invitations, OAuth, or anonymous with a quota), per-user quotas, admin view of jobs.

## Installation and platforms
The goal is Windows, Linux and macOS (Intel and Apple Silicon), installable by people with little technical knowledge.

- **One lock file, two ways to run it.** `pyproject.toml` + `uv.lock` (uv) replace `requirements.txt`; both ways below install exactly the same versions.
  - **Users, R&D, Windows:** install uv with one command, then double-click `start.bat` / `start.command` (macOS) / `start.sh` (Linux). uv downloads the right Python by itself, so there is no system Python, PATH, venv or PEP 668 trouble (Debian 12 ships Python 3.11, the project needs 3.12+). setup-for-regular-folk.md shrinks to two steps.
  - **Docker (maintainer, server):** multi-stage `Dockerfile`. The build stage uses the official uv image and `uv sync --frozen`; the runtime stage is slim, has no uv and runs as non-root. `docker compose up` runs the local version (one container, SQLite); stage 4 adds Postgres/Redis to the same image. Keep `cache/` in a Docker volume, not a bind-mounted Windows folder (memmap is much slower there), and give Docker Desktop enough RAM and cores.
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
- Done (2026-10-24): the pair search is in numba (`linesearch.py`): free directions per anchor, bucketed pairs, early-exit line check. Same lines as before, step 3 is about 150–750× faster (R4: 201 s on 23 processes → 0.3 s; R5 with the sag model: 283 s → 2 s).
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
- **No file paths over the web.** `--elevation-file`, `--surface-file`, `--cache-dir` and `--runs-dir` are CLI only. Results are addressed by UUID, with no directory listing.
- **No user-supplied URLs.** The server talks to a fixed list of services only (SSRF).
- **Uploads**: size limit, vertex limit, KML/GPX parsed with `defusedxml` (XXE).
- **XSS / CSP**: names from Nominatim and user input are escaped. The static viewer allows a strict Content-Security-Policy without inline scripts.
- **The local server is reachable by any website the user visits** (CSRF, DNS rebinding). The server checks the `Host` and `Origin` headers and requires a session token.
- **Secrets** (OpenTopography key) stay in server environment variables and never reach results or logs.
- **Public deployment**: HTTPS only, security headers, non-root read-only containers, pinned dependencies checked with `pip-audit`, audit log.
- **No shell anywhere**: the worker calls `run_search()` directly.

## To check before going public
- **Data licences:**
  - FABDEM is CC BY-NC-SA (non-commercial only).
  - Malta Planning Authority LiDAR terms need checking.
  - IGN is CC BY 4.0, GUGiK data are open, OSM is ODbL (attribution).
  - Esri imagery tiles have their own terms.
- **Public services:** the public Nominatim and Overpass instances forbid heavy use. A public site needs caching or its own instances.
