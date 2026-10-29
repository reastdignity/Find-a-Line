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
| Public hosting (stage 4, 2026-10-27; changed 2026-10-29) | A VPS with Ubuntu 26.04 LTS that runs nothing else on the web; nginx, certbot and fail2ban from Ubuntu's packages on the host (not in compose: the reviewed nginx configuration carries over, nginx sees the visitors' own addresses, fail2ban bans on the host, the packages get Ubuntu's security updates), the web service on `127.0.0.1:8765` only. IPv4 only at first (no AAAA record). Replaces the home server behind SWAG (decided 2026-10-27) and the Caddyfile planned for deployments without it (Caddy would need a plugin build for rate limits and has no connection limit). The local mode (`start.bat`, no sign-in, 127.0.0.1) stays as it is. |
| Accounts (stage 4) | OAuth with GitHub and Google through Authlib; a new account is `pending` until an admin approves it. An admin can lift the limits of a user (`users.unlimited`) without making them admin. |
| Storage (stage 4) | SQLite (WAL) and the local disk on one machine; no Postgres, Redis or object storage (only with a second machine). Schema versions with `PRAGMA user_version`; whether a change gets an automatic migration is decided per change. |
| Runs in public (stage 4) | Private to their owner, shared by an unguessable link (renewable, revocable); an admin can mark a run as a public demo. |
| Orthophoto in public (stage 4) | Ordinary users get a cache limited per job; above the limit the map loads the orthophoto online. |
| Own code or library (stage 4) | Popular, maintained libraries first (table under "Stage 4"); the job queue and the Nominatim cache stay our own. |

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
8. **Docker and CI** (taken out of stage 3; planned 2026-10-27 as part of stage 4, section 4 below): three branches, `cgroup-limits` (memory and cores from the cgroup), `ci` (GitHub Actions on Linux and Windows, macOS if it passes without changes; `pip-audit`, `npm audit`, `docker build`) and `docker` (multi-stage image, `compose.yaml` with `web` and `worker` from one image, hardened containers).
9. **Stage 4** (public website, decided 2026-10-27; done 2026-10-28, what remains is on the server: "Stage 4", *Open on the server*): the sections below, one branch each, in this order (see "Stage 4: public website"). The licences (FABDEM, Malta) are settled in section 6; Overpass is replaced by the Geofabrik extracts and Nominatim/Overpass get shared rate limits instead of own instances (see "Deferred").

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

## Stage 4: public website
Decided 2026-10-27 (decisions in the table at the top). Supersedes the earlier sketch (Caddy, Postgres, Redis, object storage for results): one machine with SQLite and the local disk behind a reverse proxy (SWAG at first; since 2026-10-29 nginx on a VPS); Postgres/Redis only with a second machine.

Why it is much work: today the security rests on listening on 127.0.0.1 and on one user. The page token stops CSRF but authenticates nobody; jobs, events and runs are shared (`/runs` serves the whole folder, `run.json` and pickles included); the estimate only advises and the worker answers "yes" to every question; circles are never tiled; NaN/Inf pass validation; `osm:<name>` and `within` call Nominatim/Overpass in the server process before any estimate; uploads are read by GDAL with any driver (an OGR VRT can read server files); server paths, exception texts and possibly the OpenTopography key leak into events and errors; run ids can collide between users.

Threat model: anonymous scanners; a malicious signed-in user (CPU, RAM and disk exhaustion, other users' data, abuse of Nominatim, Overpass and the data services); XSS and CSRF through foreign content, login CSRF included; after a possible RCE in a container, reaching the host, the provider's private network or its metadata service (the home LAN, while the plan was a home server).

**Sections** (one branch each, merged into `main` in order; the lines of `lines.csv` and their checksums must not change: new limits in `search.py` are off by default and only the web app turns them on):
1. `web-hardening` (both modes, no accounts): queue and worker moved to `jobs.py`; finite values and sane ranges in `SearchParams`; drawn areas as a validated geometry field (no GDAL, no files in `work/`); KML/GPX/GeoJSON read in the browser (`/api/area-file` and `python-multipart` gone); URLs in errors without query strings; unique run ids; viewer without inline styles (`style-src 'self'`); OSM extracts of countries and smaller only; temporary files from `tempfile`.
2. `web-worker`: `PRAGMA user_version` helper; SSE through `sse-starlette`; atomic job claim; `findaline.py worker` as its own process with a lease, cancelling by a column, killing the process tree; a work folder per job; estimates and profiles in a worker process ("quick" lane); time limits per phase (`limits.py`); housekeeping.
3. `web-public-mode` (public mode without accounts: everything but the page answers 401): `--public` with `PublicSettings`, `TrustedHostMiddleware`, `Origin` check, trusted proxy, security headers; `Limits` on request values; optional limits in `plan_search`/`_lines` before costly work (pixels, tiles, anchors, lines, the estimate in MB and km²); no server paths in public events.
4. Docker and CI: `cgroup-limits`, `ci`, `docker` (item 8 above).
5. `web-accounts`: sign-in with GitHub and Google (Authlib), own revocable sessions; jobs belong to their user (FastAPI dependencies, a test that every non-public route has one); run files behind access control (a `StaticFiles` subclass with a whitelist); share links `/s/<token>/`; demo runs and the anonymous page; quotas and rate limits with three account levels (user, user without limits, admin); admin tab; audit log, privacy and about pages.
6. `public-licences` (licence and attribution per source, in the run panel, the map and `ATTRIBUTION.txt`) and `public-osm-limits` (Nominatim 1 request/s and Overpass one at a time across processes; Nominatim cache with expiry of failed answers).
7. `deploy-home` (done 2026-10-28): SWAG proxy config (`client_max_body_size`, `limit_req`, SSE without buffering, gzip, masked `/s/<token>` in the log), a fail2ban jail, separate Docker networks for `web` and `worker`; `docs/deployment.md` (SWAG, firewall incl. `DOCKER-USER` rules against the LAN, OAuth registration, backups with `sqlite3 .backup` + `restic`); README "Public site". Also: the OpenTopography key as a Docker secret of the worker, the common User-Agent for orthophoto requests, `libexpat1` in the image (the first local build: rasterio's and osmium's wheels need it). Checked locally with Docker Desktop and SWAG's own image (`nginx -t`, headers, gzip, SSE, masking, `limit_req`, `fail2ban-regex`, a backup and restore); the firewall, DNS, certificates, the real OAuth apps and a real ban wait for the server.
8. `security-review` before opening the ports (done 2026-10-28): `/security-review`, `pip-audit`, `npm audit`, OWASP ZAP baseline, a second account trying to reach other users' data, a dynamic test of the compose stack behind SWAG's own image. The findings were fixed on two branches: `security-fixes` (the application) and `security-deploy` (the deployment: the accounts in a volume of the web service only and numba's cache in each service's tmpfs, a backup that works with the Docker root closed to uid 10001 and reports failures, no AAAA record until SWAG sees IPv6 addresses, the slow-client timeouts of our server block (they do not cover HTTP/1.1 headers, an optional SWAG-wide setting does), no secrets in nginx's error log, `Upgrade` refused, no swap, the stock ufw accepts of `before.rules` closed to the containers, CI actions pinned to commits, `deploy/` with LF line endings, disk and cgroup v2 in the docs). SSL Labs and the headers wait for the go-live.
9. `deploy-vps` (2026-10-29): the deployment moved from the home server behind SWAG to a VPS with Ubuntu 26.04 and nginx on the host (decision in the table at the top): `deploy/nginx` (the SWAG configuration carried over: limits, SSE, gzip, the masked log; now also its own TLS settings, HSTS once at the server level, `client_header_timeout` for every server, which closes the HTTP/1.1 gap of SWAG's default server, other names refused on 80 and 443), `deploy/fail2ban` for the host's fail2ban, the web service published on `127.0.0.1:8765` with the fixed gateway `172.30.0.1` of the network `web` as the trusted proxy (the network `findaline-proxy` and `compose.override.example.yaml` gone), the firewall without a LAN (the provider's private network and its metadata service instead), docs/deployment.md rewritten (SSH first). `tests/nginx_check.sh` runs the nginx and fail2ban files on Ubuntu 26.04's packages in CI, and CI checks that the published port's connections arrive from the gateway.

**Open on the server** (stage 4 is done in the repository; docs/deployment.md has the steps):
- The VPS: SSH with a key only, `ufw`, the `DOCKER-USER` block of `after.rules` and the rule of `before.rules`, then the container test of section 2 (the metadata service and the provider's private network blocked).
- DNS (an A record only, no AAAA), nginx and the certificate from certbot (`certbot renew --dry-run`), fail2ban; the OAuth apps of GitHub and Google with the real callbacks.
- restic: the repository, the first backup, the timer with its failure notice, a restore test.
- After go-live: SSL Labs, securityheaders.com, the checks of section 12 (headers, gzip, the masked log, SSE, the limits and the jail from outside, the visitors' real addresses, memory, sign-in), a real ban.
- CI: the `docker` job runs on GitHub only after the first push; check that it passes.

**Libraries of this stage**
| For | Library | Section |
| --- | --- | --- |
| KML/GPX/GeoJSON in the browser | `@tmcw/togeojson` (bundled) | 1 |
| structure of a drawn geometry (RFC 7946) | `geojson-pydantic`, plus own vertex and depth limits | 1 |
| event stream | `sse-starlette` (keepalive, disconnects, `send_timeout`) | 2 |
| settings and `Limits` from env and Docker secrets | `pydantic-settings` | 2 |
| Host check | `TrustedHostMiddleware` (Starlette) | 3 |
| direct imports from Starlette | `starlette` as an explicit dependency, ≥ 1.3.1 (CVE fixes) | 3 |
| OAuth GitHub and Google | `authlib` (≥ 1.8, client on `httpx2`), `itsdangerous` | 5 |
| run files with access control | `StaticFiles` subclass (Starlette) | 5 |
| rate limits in the web process | `limits` (memory) | 5 |
| Nominatim 1/s, Overpass one at a time, across processes | `pyrate-limiter` (`SQLiteBucket`) and `filelock` | 6 |
| off-site backups | `restic` (tool) | 7 |

FastAPI and Starlette stay pinned in `uv.lock` and are raised deliberately (FastAPI is 0.x and releases often). Own code stays where libraries fit worse: sessions (must be revocable, which a signed cookie is not; FastAPI-Users is in maintenance mode and needs SQLAlchemy), CSRF (`starlette-csrf` has no release since 2023), the job queue, the Nominatim cache, migrations (`user_version`), the admin tab, the audit log, reading the cgroup.

**Migrations of this stage** (each in its own commit):
| Data | Change | Migration |
| --- | --- | --- |
| `jobs.sqlite` | new columns (`worker_id`, `cancel_requested`, `user_id`), tables, indexes | automatic on opening; job history kept |
| `nominatim.sqlite` | time of each answer (failed searches expire) | automatic; existing rows count as successful |
| existing runs | new id format (suffix), `lines.geojson` without paths | none: old ids keep working, old files stay |
| existing runs in public mode | owner table `runs` | automatic: at start runs without a row belong to the first admin, private; nothing is deleted |
| `cache/` | no layout change (`LAYOUT` stays 1) | not needed |

**Deferred**
- An audit log viewer in the UI (the admin reads the log with `sqlite3`), a round-robin queue between users, overriding single limits per user, `--max-spots`.
- CI on macOS, if Linux and Windows suffice.
- A healthcheck of the worker in compose.yaml: its lease in `jobs.sqlite` (the `workers` table) renewed within `LEASE` (decided 2026-10-28: later).
- LRU eviction of the cache down to a size cap (decided 2026-10-28: later): today only the free-disk limit of new searches protects the disk, and an administrator deletes cache folders by hand.
- An allowlist of schemes and hosts for download URLs taken from upstream indexes (STAC `href` and `next`, geoportal's `url_do_pobrania`, the replication URLs of the extracts), so an index that is wrong or compromised cannot point the worker elsewhere (decided 2026-10-28: later).
- Renovate or Dependabot for the pinned image digests (Dockerfile) and the actions pinned to commits (CI) (decided 2026-10-28: later).
- A CVE scan of the image in CI (trivy, its action pinned to a commit) (decided 2026-10-28: later).
- IPv6 for the site (an AAAA record): nginx on the host already sees IPv6 visitors' own addresses, but the limit zones must count per /64 and fail2ban ban the /64 first (decided 2026-10-28, again 2026-10-29: later; docs/deployment.md, section 2, *IPv6 for the site later*).
- Deleting one's own account (decided 2026-10-28: later): the account, its sessions, jobs and runs with their files, and an audit entry; until then users ask the contact of the privacy page.
- Very large runs (numpy columns, a viewer served by the app; see "Very large areas"); this stage only limits the lines and compresses.
- **Own Nominatim and Overpass instances.** Used for: Nominatim for `osm:<name>`, run names and `country_code`; Overpass for the `coastline`/`cliff` theme tiles, which the Geofabrik extracts already replace. Needed when the traffic exceeds the public instances' policies despite the cache, quotas and limiters, or for areas without extracts. Cost (estimates): Nominatim is PostgreSQL+PostGIS, for the planet ≥ 64 GB RAM and about 900 GB NVMe, a country much less; Overpass for a region tens of GB of disk and a few GB of RAM; both need updating with diffs. A cheaper step first: `country_code` from the outlines of the Geofabrik index (to check at borders where the outlines overlap).

## Lines list, group choice and favourites (done 2026-10-28)

- **List of lines** in the map (top right): one row per spot or line as the map shows them (after the filters), sorted by any value (`list.ts`), optionally only those in the view; the sort order is kept in the browser.
- **Another line of a group**: the user picks any member (thin variants clicked, ◀ ▶ in the popup, the group's row unfolded in the list); only the line shown changes, never the members, so `groupLines` still matches `mapping.group_lines`. Decided: the choice lasts until the map is closed (groups change with the distance and the filters, so a stored choice would be ambiguous); a favourite is the lasting way. `map.html#line=lat1,lon1,lat2,lon2` opens a line.
- **Favourites** (decided: on the server, per account, not in the browser's storage): table `favourites` of `jobs.sqlite` (migration 8, automatic), marked from a map served by the app (it gets the token of its changes from the GET of its favourites: a map is a static page without one, and `runs.js` could be included by a foreign page), listed in the app's tab *Favourites*. The map sends the line's values with it and the server keeps them (`FavouriteLine`: the user's own data, shown only to them), so the tab needs no run's `lines.csv`; the line itself is not checked against the run (at most `FAVOURITES_MAX` = 10000 per user bounds the rows). Maps from disk and share links have none. Priority of the line shown in a group: chosen in the session, then a favourite, then the representative. Export as GeoJSON/CSV in the browser (the columns of `lines.csv`).

## UI prototypes B and C (built 2026-10-29, branch `ui-prototypes`, not merged)

Users found the app hard to navigate.
- **Review:** ten workflows, a diagnosis and three proposals with mockups (A guided steps, B map-first, C dashboard), in a private artifact (https://claude.ai/artifact/Lqu4f7ZfGctPjYbgzz2nNT).
- **Built:** B and C, both in one build next to the old app, chosen with `?ui=b` / `?ui=c` (remembered in the browser; `?ui=old` goes back). Since 2026-10-29 D is the default (the user), the old app stays at `?ui=old`.
- **Shared by both:**
  - one results panel: summary with legend, filter chips, an unfolded list sorted by clearance, line details with next/previous spot, profile with tap-and-drag, Share/Download/Compare/Measure;
  - a search panel: modes Point · Area · Along cliffs · File, presets (`params.PRESETS`), human labels and topics (`params.LABELS`, `TOPICS`), a live estimate instead of the Estimate button;
  - `GET /api/usage` for the quota meters.
- **Decided:**
  - `map.html` stays unchanged until one proposal wins.
  - New searches in B and C start with the "Typical highline" preset (30–150 m, Δh ≤ 10 m).
  - No search field "clearance ≥ x" (the results' filter does it).
  - No GPX export yet (to be compared with libraries first).
- **D (2026-10-29):** the user prefers C's home and new search with B's way of working with a finished run, above all the profile docked at the bottom; `?ui=d` combines them (B's result view is now `result/view/ResultWorkspace.tsx`, shared by B and D). B and C stay for comparison.
- **Open:**
  - The choice between B, C and D (D is the user's current preference).
  - Then:
    - the winner's result view in `map.html` (from disk and share links), removing the copies noted in `result/model.ts`;
    - the old app removed;
    - the orthophoto in B's results;
    - a place-name search box;
    - the old Admin/OSM panels in the new look.
  - Small fixes noticed (2026-10-29):
    - Done: on phones (B, C, D) a selected row leaves the sheet at half the map, and at full it stays below the top bar (`Sheet` measured its parent, which in the result is `display: contents`, so it had no height and grew with its content; it measures its positioned ancestor now, `top` keeps room for the bar).
    - Done: the map's controls in the dark theme, one set of rules in `ui/ui.css` for every prototype instead of copies per shell (D had none; Leaflet's light grey of focused and disabled buttons and the white layers box are covered).
    - Done: the B/C shell tests' rare failure was a canvas redraw after leaving a result ("clearRect of undefined": Leaflet binds the redraw when it asks for the frame); `ResultMap`'s renderers skip a redraw without a canvas.
    - Dropped (the user, 2026-10-29): one OpenTopoMap tile per mini map.
    - The tile usage policies of OpenTopoMap (and OSM) for the mini maps on the public site (see "To check").
  - Running the prototypes from the worktree with the usual data: `uv run findaline.py web --data-dir <main checkout>` (without it the worktree's own empty data folder is used).

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
- **Changed 2026-10-26: a local copy first, Hugging Face later.** The tiles are kept on the user's disk until the upload (`C:\Users\Krzysiek\findaline-mirror\slovakia`, laid out as the dataset, with its card); the ZIPs are downloaded to C: (`C:\Users\Krzysiek\findaline-work\sk-zips`), cut there and then kept on the NAS (`Z:\Udostępnione tymczasowo\krzysiek\Find-a-Line\sk-zips`). Whether the upload goes to a dataset repository or a Storage Bucket (as FABDEM's mirror) is decided at upload time; the layout and `index.json` suit both. 1 m (1st cycle) only; the 0.5 m lots of the 2nd cycle later. Checked: both national ZIPs are plain Deflate (DMR 5.0: 198 GB with a 157 GB TIFF and a 47 GB `.ovr`; DMP 1.0: 155 GB, TIFF 122 GB), so GDAL `/vsizip/` reads them without 7-Zip. Measured 2026-10-27: too slowly. The TIFF inside is LZW in 128 px blocks; through `/vsizip/` a 128 px block row of the country took 15–20 s and a 2 km band 9 min (one core, about 7 MB/s; 15 h for DMP 1.0), and opening it 4 min. Python's `zipfile` inflates the same member at 400 MB/s, so `sk_tiles.py extract` unpacks the TIFF beside the ZIP (DMP 1.0: 13 min) and `cut` reads the plain file, writing a band's tiles in parallel: 30 s per band. Downloads: about 8–10 MB/s per ZIP from opendata.skgeodesy.sk (DMP 1.0 in 5.3 h, DMR 5.0 in about 6 h); two at once are not faster in total.
- **Done (code) 2026-10-26**: source `slovakia` (`sources.GridTiles` with `mirror=`, EPSG:8353, ZBGIS orthophoto WMS, which has imagery at zoom 20 and more); `FINDALINE_MIRROR` names the folder above `slovakia/`, and a search copies the tiles it needs into `cache/slovakia/dmr5/5.0/...` and `dmp1/1.0/...` (static releases), checking each against the sha256 of the mirror's `index.json`; a tile not in the index yet is an error, never "missing". Without a copy set up, `--source auto` uses `europe` and says why. Moving to Hugging Face: cut, upload, add the base to `sources.SLOVAK_TILES`.
- **Done (data) 2026-10-27**: both products cut into the local mirror, 12 826 tiles with data each on the same 2 km grid (DMP 1.0 73 GB, 5.7 MB per tile; DMR 5.0 61 GB, 4.8 MB), 34 min per product from the extracted TIFF; both ZIPs kept on the NAS (copies checked by size and 33 samples), the extracted TIFFs deleted. The TIFFs name their CRS only (`LOCAL_CS`, DMP "S-JTSK [JTSK03] / Krovak East North", DMR "S_JTSK_JTSK03_Krovak_East_North"; DMR's pixel 0.999999999998 m), so the tiles get EPSG:8353 and exact 1 m pixels. Data checks passed (regression doc: no shift against Copernicus/FABDEM, heights above sea level, 100% valid at 1 and 5 m, tiles within 5 mm of the source). R21 (Súľovské skaly, skipped without `FINDALINE_MIRROR`), the ZBGIS orthophoto in the map and a job in the web app checked. Still open: the Tatras test (the DSM over the dwarf pine) and the upload.

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
  - **Docker (maintainer, server):** multi-stage `Dockerfile`. The build stage uses the official uv image and `uv sync --frozen`; the runtime stage is slim, has no uv and runs as non-root. `docker compose up` runs the public version: `web` and `worker` from the same image, SQLite in one data volume (stage 4; no Postgres/Redis on one machine). Keep the data folder (`cache/`, `runs/`, `work/`) in a Docker volume, not a bind-mounted Windows folder (memmap is much slower there), and give Docker Desktop enough RAM and cores.
- **Platform rules for the code:**
  - The worker is a separate process and uses the `spawn` start method on every OS, so Linux behaves like Windows/macOS. (Python 3.14 changes the Linux default to `forkserver`.)
  - Each job gets its own work folder, and memmapped files are closed before they are deleted: Windows cannot delete or overwrite an open file.
  - `pathlib` for paths; `webbrowser` to open the browser.
- **CI**: GitHub Actions on `ubuntu-latest` and `windows-latest` (`macos-latest` too if it passes without changes; else deferred) runs `pytest` (fixture rasters, no network, the web app) and the viewer checks, plus a Docker build and `pip-audit` / `npm audit`. Done 2026-10-27 (`.github/workflows/ci.yml`, not run yet: nothing is pushed): Linux and Windows with Python 3.13 and Node 24, the audits in their own job; macOS left out (deferred); the Docker build job comes with the `docker` branch.
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
| **A. FastAPI + queue + worker (chosen)** | Typed validation with limits (pydantic); OpenAPI docs; heavy computing never blocks the server; the same code runs locally and publicly (SQLite queue on one machine; Postgres/Redis only with a second one); easy to add endpoints. | Accounts, CSRF and rate limiting come from extra libraries; more dependencies. |
| B. Django + Celery / Django-Q | Best security defaults (accounts, CSRF, sessions, headers), admin panel, migrations. | Heavy for a single-purpose tool; live progress needs Channels or polling; most work up front. |
| C. Flask + HTMX | Least code at the start. | Security assembled from extensions, easy to miss something; long jobs still need a queue; grows messy. |
| D. Computing in the browser (Pyodide/WASM) | No server cost, nothing to trust. | Large rewrite (Web Workers instead of multiprocessing), slower; WCS services may block CORS (a proxy is needed anyway); large areas impossible. |

## Security
These apply from stage 3 on, the local version included. *Not yet* marks what stage 3 does not do (checked 2026-10-27) and names the stage-4 section that implements it.
- **Abuse of computing power is the main risk.** Every request goes through `estimate_search` with limits on area, pairs, minimum resolution and time. Jobs have a timeout and can be killed (since section 2: time limits per phase and for estimates and profiles, `FINDALINE_LIMIT_*`, none locally; the worker kills a job's whole process tree); the queue has per-user limits; the public version adds rate limiting. Since section 3 the worker enforces `Limits` (the public site's defaults in `settings.PUBLIC`): request values (`web.check_request`), and before the costly work the size of a `within` box or Nominatim area (before Overpass), the area, the pixels of a grid or tile, the tiles, the download, the anchors and the lines (`search.plan_search`, `check_estimate`, `_lines`); orthophoto over `ortho_minutes` is not cached. *Not yet*: per-user limits and rate limiting (section 5).
- **No file paths over the web.** `--elevation-file`, `--surface-file` and `--data-dir` are CLI only. Results are addressed by unguessable ids (a random part of 64 bits since 2026-10-27; older runs keep their ids), with no directory listing; `lines.geojson` carries no list of cache files and drawn areas no paths. Since section 5 the public site serves only the listed files of a user's own runs (`web.RunFiles`; `run.json`, `progress.json` and tiles never) and share links `/s/<token>/` whose token is stored as its sha256; locally `/runs` is still the whole folder. Since section 3 the public worker's events, errors and answers pass `jobs.Redact` (the data folder becomes relative, other absolute paths `<path>`), and an unexpected exception shows "internal error, ref <job>" while its traceback goes to the log.
- **No user-supplied URLs.** The server talks to a fixed list of services only (SSRF).
- **Area files**: KML/GPX/GeoJSON are read in the browser (`@tmcw/togeojson`, the browser's XML parser; at most 5 MB); the server takes no files, only a geometry checked by `geojson-pydantic` with vertex, depth and range limits and by shapely (`area.check_geometry`), so GDAL never reads data from the web and `defusedxml` is not needed. The request body is limited to 2 MB by nginx (`client_max_body_size`, section 7).
- **XSS / CSP**: names from Nominatim and user input are escaped, run ids in links too. The static viewer allows a strict Content-Security-Policy without inline scripts or styles (`style-src 'self'`: popup colours are set through the CSSOM, `runs/index.html` uses `_viewer/index.css`).
- **The local server is reachable by any website the user visits** (CSRF, DNS rebinding). The server checks the `Host` (`TrustedHostMiddleware`) and, for changes, the `Origin` headers and requires a session token.
- **Secrets** (OpenTopography key) stay in server environment variables or Docker secrets (the key: the worker's only, since section 7) and never reach results or logs. Errors of requests report their URLs without query strings (`cache.clean_error`).
- **Public deployment**: HTTPS only, security headers, non-root read-only containers, pinned dependencies checked with `pip-audit`, audit log. Since section 3: `--public` (only the public host, `Origin` required for changes, `X-Forwarded-*` from the proxy only, since section 5 sign-in with GitHub/Google through Authlib, own revocable sessions with a CSRF token each, every route behind a FastAPI dependency (`current_user`, `require_admin`, `run_access`), jobs and runs per user); the headers of `web.SECURITY_HEADERS` in both modes (CSP with `base-uri 'none'` and `form-action 'self'`, COOP and CORP `same-origin`, `Permissions-Policy`, `Referrer-Policy: strict-origin`: the origin only, also to our own pages, so a share link's token never leaves in a Referer, while the tile servers still get the one they ask for). Since sections 4, 5 and 7: non-root read-only containers, the audit log, the site behind SWAG (rate limits per address, a fail2ban jail, HSTS, logs without share tokens) in a network of its own, containers kept from the LAN by `DOCKER-USER` rules (docs/deployment.md); since section 9 behind nginx on the host of a VPS (the same limits, jail and log; TLS settings of its own, other names refused), the web service on the host's loopback only, the containers kept from the host, the provider's private network and its metadata service. Since section 8: the accounts and sessions in a volume only the web service mounts, numba's compiled code in each service's own tmpfs (neither service loads what the other could plant; the worker still writes the runs, `data.js` included: an accepted risk), the host closed to the containers in ufw's `before.rules` too, nginx's error log without share tokens or sign-in codes.
- **No shell anywhere**: the worker calls `run_search()` directly.

## To check
- **Obstacles lowered by averaging the DSM** (measured 2026-10-26 at Lauterbrunnen, 0.5 → 1 m): the highest 0.5 m value of a pixel is above its mean by 0.25 m (median) and 7.7 m (99th percentile), so single treetops come out lower than they are; at 2 m and coarser more so. Taking the maximum of the DSM instead does not work: the clearance limit comes from the anchor heights in the averaged DTM, and anchors lie on cliff edges, where the maximum of a pixel is the rim (up to 189 m above the mean on vertical walls); the pixels next to every anchor and along every wall would become obstacles, so nearly every line from a cliff would be marked as over trees, and single wrong DSM points would spread over whole pixels. To try instead: the maximum of the obstacle height above the ground (DSM − DTM on the data's grid), added to the averaged DTM; on bare rock it is about 0, tree crowns keep their height. Costs: both models on one fine grid first (Polish NMPT 0.5 m, NMT 1 m), mixed pixels on slopes and forest edges, changed results over trees. Measure on the regression places before deciding.
- **Hugging Face for the Slovak tiles**: dataset repository or Storage Bucket (FABDEM's mirror is a bucket); the docs disagree on a soft cap per public repository (300 GB or 1 TB) and on limits for anonymous downloads; check before uploading (134 GB at 1 m as cut, 2 × 12 826 files plus two indexes; about 530 GB with the 0.5 m cycle).
- **geoportal.gov.pl sheet files instead of WCS** (done 2026-10-25, `SheetTiles`: newest sheet of 0.5–1 m per godło, cached as GeoTIFF, working grid aligned to the sheet pixels, offline from the cached sheets; the Polish regression tests now take 3 min instead of 15). Findings: the EVRF2007 NMT index covers all of Poland (63 465 sheets 1:5000 of ≤ 1 m in PL-1992, plus PL-2000 sheets in some cities), the NMPT about 85% (53 846, all laser scans, `.asc`); the newest NMT sheet is `.asc` for 94% of the sheets, `.xyz.zip` for most others; in 56% of the sheets it is labelled "aerial photos", but in a sample these held the values of the earlier laser scan (4 of 5 identical, 1 within ±3 cm). Before: the EVRF2007 WCS spends 30–50 s generating each 2 km ASCII tile (75 MB, 55 s in all; four parallel requests take as long as one), while the sheet files linked from the WFS indexes (`url_do_pobrania` on `opendata.geoportal.gov.pl`) download at about 17 MB/s: a 1 m NMT sheet of about 2.3×2.3 km (37 MB) in 2 s, roughly 30× faster per km². It is also the download path the geoportal pages describe, and the cache would then match the units GUGiK updates (sheet, year, `nr_zglosz`). To check first: whether the EVRF2007 indexes cover all of Poland with 1 m sheets, the sheet formats (`.asc`, `.xyz.zip`, `.zip`), the NMPT sheets, and choosing between sheets of different years and sources (laser scan vs aerial photos). The geoportal pages give no limits for parallel requests; technical contact: service.desk@gugik.gov.pl.
- **geoportal.gov.pl height system** (done 2026-10-24: switched to EVRF2007 at native resolution, averaged locally, Polish tests re-baselined; the server's own downsampling left most of the Tatras empty at 5 m): the sources used the KRON86 coverages (`DTM_PL-KRON86-NH_TIFF`, `DSM_PL-KRON86-NH`), whose sheet indexes end in 2019. The WCS also has `DTM_PL-EVRF2007-NH` (ASCII grid only) and `DSM_PL-EVRF2007-NH`. Checked on 2026-10-24 by comparing 200×200 m windows of both coverages with the sheet files (difference of differences, so the resampling of the WCS cancels out): the difference between the two coverages equals the difference between the newest KRON86 and the newest EVRF2007 sheet (residual median 0.00 m, p5–p95 about ±0.2 m) at Żabi Koń (2013 vs 2023), Kasprowy (2012 vs 2025), Sokoliki (2012 vs 2025) and Gdańsk (2018 vs 2024); in Warsaw the EVRF2007 coverage matches the 2025 sheet within ±0.1 m. So **the KRON86 coverage holds surveys up to 2019 and the EVRF2007 one the newest**, 5–13 years newer in these places. Before step 2: switch NMT and NMPT to EVRF2007 (NMT only as ASCII grid: `fmt="image/x-aaigrid"`) and re-baseline the Polish regression tests. To watch: some newest sheets come from aerial photos, not laser scanning (Sokoliki 2025); at Żabi Koń the 2024 "aerial photo" sheet holds exactly the values of the 2023 laser scan.
**Before going public**
- **Data licences** (checked 2026-10-24; FABDEM, Copernicus and NASADEM 2026-10-27). Done 2026-10-27 (`public-licences`): `sources.Credit` per source and per filling layer (water mask, FABDEM, Copernicus) and for OSM; every run records the credits of the data it used (`run.json` `attribution`, `data[].credit`), shows them in the run panel and the map's attribution line and writes `ATTRIBUTION.txt`. Orthophotos keep their own attribution in the map (licences not recorded yet).
  - FABDEM V1-2: CC BY-NC-SA 4.0. The [data.bris.ac.uk record](https://data.bris.ac.uk/data/dataset/s5hqmjcdj8yo2ibzi9b4ew3sn) says so in its description, ships the CC BY-NC-SA 4.0 text as `license.txt`, and its `readme.txt` (also in the Hugging Face mirror) explains it: no commercial use, adaptations under the same licence, plus the Copernicus notice and disclaimer. Only the record's licence field (a CKAN choice) says "Non-Commercial Government Licence for public sector information"; both are non-commercial. Commercial use: fabdem@fathom.global.
  - Copernicus DEM GLO-30 (and its water body mask): [licence](https://docs.sentinel-hub.com/api/latest/static/files/data/dem/resources/license/License-COPDEM-30.pdf) free for any use; adapted data needs the notice "produced using Copernicus WorldDEM-30 © DLR e.V. 2010-2014 and © Airbus Defence and Space GmbH 2014-2018 provided under COPERNICUS by the European Union and ESA; all rights reserved" and the liability disclaimer (Art. 6).
  - NASADEM: NASA open data without restriction (EOSDIS data use policy); citation NASA JPL (2020), doi:10.5067/MEaSUREs/NASADEM/NASADEM_HGT.001; OpenTopography asks to be acknowledged.
  - Malta 2018: the INSPIRE records say no conditions on access and use, and the data portal lists it as a High Value Dataset (Regulation 2023/138, which requires CC BY 4.0 or more permissive). Malta 2012: the MSDI records were reported as CC BY-NC-ND 4.0, the INSPIRE ones as no conditions; unresolved.
  - IGN: CC BY 4.0 (Orden FOM/2807/2015), attribution with product, date and "CC BY 4.0".
  - swisstopo: open government data, free use including commercial, "© swisstopo" required; the download service asks for no bulk bot traffic (backoff with jitter, few detailed orthophoto tiles).
  - GUGiK: free for any use; the source must be stated (geoportal terms § 3.2).
  - ÚGKK SR (Slovakia, ALS models and ZBGIS orthophoto): CC BY 4.0, author ÚGKK SR (the licence PDF in the ZIPs; the WMS capabilities say the same).
  - OSM: ODbL, attribution.
  - Esri World Imagery: Esri Master License Agreement; the layer is not meant for offline tile export (there is a separate "for Export" layer), so it stays online only.
- **FABDEM licence** (settled 2026-10-27): CC BY-NC-SA 4.0 for V1-2 (see above); the description of `europe` says so. For a public site: non-commercial only, the credit and licence shown with every result made from it (run panel, map, `ATTRIBUTION.txt`), and those results shared under the same licence.
- **Tile policies for a public site, to check**: Esri World Imagery (terms of use for a public, non-commercial web app, attribution, request volume) and the OSM tile servers (tile usage policy: valid `Referer`/User-Agent, no heavy use, attribution; possibly another tile provider).
- **Public services:** the public Nominatim and Overpass instances forbid heavy use. Decided for stage 4: the Nominatim cache, shared rate limiters (section 6), Geofabrik extracts instead of Overpass on the server; own instances deferred (see "Stage 4", Deferred).
- **Accounts** (stage 4): decided 2026-10-27 (OAuth with admin approval, see Decisions).

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
- **Done meanwhile (2026-10-27)**: limits in the search that keep only prominent lines (next section), so a mountain area need not produce millions of lines; (a)-(c) stay open for searches without them.

## Prominent lines (done 2026-10-27)
In steep mountains a user wants the outstanding lines, not every line. New values per line, all saved (`lines.csv`, `data.js`), shown in the popup and filtered in the map, and the same as search limits (all off by default):
- **Heights of the webbing above the terrain** (`height_max`, `height_mean`, `high_pct` of `--high-above` 20 m): the webbing of the clearance model (decided: with `--model sag` the chord minus the sag under the athlete, so the values follow the rigging; with the formula `min anchor − 0.15 × distance`); computed in the same pass as the clearance, so free. `--min-height`, `--min-mean-height`, `--min-high-share` drop lines right after the final check, before they become Python objects. `--group-by height` shows the highest line of a group.
- **Angle to the wall** at both anchors (Horn gradient): `--max-wall-angle`, applied to pairs before their pixels are checked.
- **Prominence and drop of the anchors** (`terrain.py`): prominence is the height above the level at which the anchor's rock joins terrain wider than `--prominence-span` (300 m; bounded, so tiles give one grid's values: grids and tile overlaps grow by that margin); drop is the height above the lowest terrain within `--drop-radius` (50 m). `--min-prominence` (one anchor), `--min-prominence-both`, `--min-drop` act on the anchors before the pairs, which saves the most. Union-find over all pixels: under 1 s at 2601 × 2601 px, about 20 s on a random 8000 × 8000 grid (worst case).
- **Measured** at Montserrat (R9, 5 m, cached data, whole search with the map):

  | Limit | Lines | Spots | Pairs checked | Time | data.js |
  | --- | --- | --- | --- | --- | --- |
  | none | 85 671 | 3 972 | 549 299 | 15.2 s | 107 MB |
  | `--min-height 50` | 72 739 | 3 006 | 549 299 | 13.2 s | 97 MB |
  | `--min-mean-height 30` | 73 638 | 3 054 | 549 299 | 13.4 s | 97 MB |
  | `--min-high-share 80` | 68 500 | 2 900 | 549 299 | 12.5 s | 92 MB |
  | `--max-wall-angle 45` | 58 778 | 3 034 | 286 739 | 10.5 s | 73 MB |
  | `--min-prominence 10` | 1 963 | 215 | 19 945 | 1.8 s | 3 MB |
  | `--min-prominence-both 10` | 7 | 2 | 218 | 1.3 s | 0 MB |
  | `--min-drop 50` | 73 403 | 3 245 | 408 375 | 13.0 s | 93 MB |
  | `--min-height 50 --min-prominence 10` | 1 581 | 169 | 19 945 | 1.8 s | 3 MB |

  Montserrat's lines are high anyway (85% reach 50 m), so the height limits remove little there; the prominence of an anchor (towers) cuts the lines 40-fold. At Żabi Koń, a tower joined to the hillside by a high saddle, the prominence is mostly 0–6 m (17 m at most) and the drop (25–50 m) shows its exposure better: neither measure alone describes every "prominent" rock.
- **Later**: `--max-spots N` (the N best spots instead of thresholds), access (distance of the anchors from an OSM path or road, a new `highway` theme), skipping low pairs already in `_line_ok` if pairs ever become the cost.

## Open pits and other terraced walls (looked at 2026-10-25, Bełchatów)
- The Bełchatów and Szczerców pits (200–250 m deep, walls of benches, about 20° overall) gave lines of 1.1 km at most, all near the bottom: the rims have no pixels of 45° or more, and with 5.5 kN and 70 g/m the sag is about 45 m at 1 km, 114 m at 2 km, 207 m at 3 km and 475 m at 5 km, deeper than the pits for lines from rim to rim. With 10 kN and 45° lines of up to 1.9 km appear across the Bełchatów pit.
- Considered, not chosen for now: anchors on edges (the terrain drops by X m within Y m) instead of the slope of one pixel, so that the rim of a terraced wall is an anchor. *Measure* in the map (two points, the profile from the run's data, anchors above the ground) was built instead, to see why a line expected there is missing.
