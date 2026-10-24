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
- Rough scale: the coast of Malta and Gozo is about 190 km, so a 100 m band is 20–40 km² at 1 m. Expect hours of computing, which is fine for memory with tiles.

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
