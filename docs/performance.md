# Performance of the public site under many jobs

Measured 2026-10-31 on the development PC (12 cores / 24 threads, Windows 11, NTFS, the cache on a
local SSD). The public worker runs in a container with `WORKER_CPUS` (default 3) and
`WORKER_MEMORY` (default 8g); "3 cores" below is this PC restricted to 3 physical cores (CPU
affinity and `--threads 3`), which is what `search.line_threads` gives the worker there.

The question: one worker runs one job downloading and one computing at a time
(`jobs.Scheduler`, `running` keyed by phase), the quick lane beside them, and claims jobs first in,
first out. What limits it when many people queue jobs?

## How to measure again

| Script | What it does |
| --- | --- |
| `tools/perf_profile.py --out <dir> --name <n> [--cores N] -- <findaline options> --data-dir <scratch>` | Runs a search in its own process, follows the progress bars and samples CPU time and memory every 0.1 s: a table per stage (wall s, CPU s, cores busy, peak memory) and a timeline chart |
| `tools/perf_download.py --out <dir> --osm-from cache/osm --threads 2 4 8 -- <findaline options>` | The download phase (`search.download_data`) on an empty cache (only the OSM themes and Nominatim answers copied), with `sources.DOWNLOAD_THREADS` set |
| `tools/perf_sqlite.py --out <dir> --jobs 1 4 8 16 --pages 30 [--burst]` | `jobs.sqlite` with the real `jobs.Jobs` under K phase processes, the worker, the quick lane and M open pages |

The searches ran on cached data (a scratch data folder whose `cache` is a junction to the real one)
with `--ortho never`, one at a time. Their results equal the regression table: R13 566 lines / 143
spots, R9 85 671 / 3 972, Gozo 403 026 / 2 734 (the run of 2026-10-28).

## 1. Cores used by one job

| Job | Size | 24 threads: wall | mean cores | 3 cores: wall | mean cores | peak memory |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| small: R13 Kasprowy Wierch | 5 m, r 500 m, 566 lines | 1.9 s | 3.5 | 1.7 s | 1.1 | 0.2 GB |
| medium: R9 Montserrat | 5 m, r 1 km, 85 671 lines | 15.7 s | 2.3 | 16.8 s | 1.2 | 1.0 GB |
| large: Gozo coast ±100 m, 1 m | 9.65 km², 6 tiles, 403 026 lines | 129-131 s | 2.3 | 134-135 s | 1.2 | 5.6 GB |

(R4, the 1 m Dolina Będkowska, takes 3 s and behaves like R13.) Times are of the search alone; the
worker adds a spawned process per phase (about 1 s of imports each).

**The line search is not the problem.** It is the only parallel code (numba `prange` in
`linesearch.py`) and uses the cores well (21-22 of 24, 3 of 3), but it is 6 % of the Gozo job on 24
threads and 12 % on 3 cores; on R9 it takes 0.6 s of 16. Everything else runs on one core, so 24
threads make the large job only 5 % faster than 3 cores. On 3 cores a job keeps 1.2 cores busy on
average: **the worker's three cores are mostly idle while one job computes.**

![Gozo on 24 threads](perf/gozo-24.png)

![Gozo on 3 cores](perf/gozo-3.png)

![R9 on 3 cores](perf/r9-3.png)

Where the large job spends its time (24 threads, all 6 tiles together; cProfile for the parts
between the bars):

| Stage | Wall s | Cores | Where |
| --- | ---: | ---: | --- |
| gap filling (`sources.fill_gaps`) | 44 | 1 | 19 s in `scipy.ndimage.median` over the whole tile, 7 s own numpy, 11 s the FABDEM/Copernicus mosaic |
| anchor prominence and drop (`terrain.py`) | 20 | 1 | union-find in plain `@njit` (12 s) and an `argsort` of every pixel (5 s) |
| mosaics (`build_mosaic`: terrain, surface, water mask) | 9 | 1 | half of it `rasterio.warp.reproject` with GDAL's one thread |
| saving results and the map (`runs.save_results`, `mapping.save_map`) | 14 | 1 | CSV, GeoJSON, `json` of 403 026 lines |
| grouping, coordinates, tile files | 9 | 1-1.5 | |
| filling sea and lakes, sea mask | 8 | 1 | |
| line search (free directions, pairs, checking) | 7 | 18-22 | numba `parallel=True` |

R9 (a circle) has a different profile: line profiles 7.1 s (one `line_profile` per line, all 85 671
lines of a circle), saving 5 s, line search 0.6 s.

**A cheap fix found on the way.** `fill_gaps` calls `ndimage.median(values, ring, index=...)` on the
whole tile; scipy sorts every pixel, label 0 included. Passing only the ring pixels
(`ndimage.median(diff[ring > 0], ring[ring > 0], index=...)`) gives identical medians: on a
7 600 × 7 600 tile with 2 958 gaps, 10.5 s → 0.15 s. That is about 15 % of the Gozo job, with
the same lines (the regression checksums would not move).

## 2. Downloads

The download phase (`search.download_data`) does only network and file work: the layers one after
another (terrain, then surface, then the water mask), each with a pool of `DOWNLOAD_THREADS = 4`.
It keeps 0.1-0.3 cores busy (1.8 at 8 threads on Malta's WCS).

| Area, service | 2 threads | 4 threads (today) | 8 threads |
| --- | ---: | ---: | ---: |
| Gozo coast, Malta WCS (50 tiles, 196 MB received) | 66 s | 25 s | 15 s |
| Jura r 2 km, geoportal.gov.pl sheets (18 sheets, 662 MB) | 285 s | 181 s | 121 s |
| two Jura areas at once, 4 threads each (17 + 18 sheets, 1 250 MB) | | 286 s and 315 s | |

- Each connection is slow: about 1 MB/s per geoportal sheet (earlier measurements gave 17 MB/s,
  docs/roadmap.md: the service varies), and Malta's WCS spends most of a request generating the
  tile. More threads help both, almost linearly up to 8.
- Two jobs downloading from geoportal at the same time got no more in total (1 250 MB in 315 s,
  4.0 MB/s) than one job with 8 threads (5.5 MB/s): the service (or the path to it) is the limit,
  not the number of jobs.
- The terrain and surface layers are downloaded one after the other, so a job waits for the slowest
  sheet of each layer twice.
- With one download slot, a slow download blocks everyone: the next job's download starts only when
  this one ends, even if it needs a different service or nothing at all (cached data).

Is it safe to run two download phases at once? Yes: each download writes its own `.part` file and
renames it (`cache.py`), `index.sqlite` is in WAL mode, and Nominatim and Overpass are already
limited across processes (`osm._ask`, `osm._overpass_turn`). What is missing is a lock per tile:
two jobs over the same area download the same tiles twice (correct, but wasted), and no limit per
host across jobs.

## 3. SQLite

| Database | Written by | Settings |
| --- | --- | --- |
| `jobs.sqlite` | worker (events, claims, heartbeat, quick lane), web (jobs, favourites, reports, usage) | WAL, `timeout=60`, a new connection per operation |
| `accounts.sqlite` | web (sign-ins, sessions: `seen`) | WAL |
| `audit.sqlite` | web and worker, one row per change | WAL |
| `cache/<top>/index.sqlite` | downloads, one row per tile | WAL |
| `cache/osm/nominatim.sqlite` | Nominatim answers | rollback journal (no WAL) |
| `cache/osm/nominatim-rate.sqlite` | pyrate-limiter's bucket | its own |

`jobs.sqlite` under load (`tools/perf_sqlite.py`, 30 s each; a phase writes a progress event every
0.3 s and an info event every 2 s, the worker its heartbeat every 5 s and its claims every 0.5 s,
the quick lane `quick_claim` every 0.05 s, each page reads its events and `worker_alive` every
0.5 s):

| Jobs at once | Pages | Events/s | event write, median / p99 | page read, median / p99 | errors |
| ---: | ---: | ---: | --- | --- | ---: |
| 1 | 30 | 4 | 2.3 / 18 ms | 14 / 22 ms | 0 |
| 4 | 30 | 17 | 6.6 / 60 ms | 6.8 / 23 ms | 0 |
| 16 | 30 | 66 | 5.1 / 117 ms | 4.4 / 24 ms | 0 |
| 4 | 100 | 17 | 12 / 85 ms | 17 / 175 ms | 0 |
| 4 | 400 | 17 | 11 / 86 ms | 122 / 307 ms | 0 |
| 4, no pauses | 30 | 429 | 5.0 / 114 ms | 17 / 30 ms | 0 |

- **SQLite is enough** for one machine, as decided (docs/roadmap.md: Postgres only with a second
  machine). The writes of 16 jobs at once are 66 events/s, a sixth of what four writers without
  pauses reach on this PC (429/s); no `database is locked` anywhere.
- **The limit is the web process, not the database:** every open page polls on its own (two
  queries every 0.5 s, each on a new connection with `PRAGMA journal_mode`). At 400 pages the
  reads queue behind the GIL of one process (median 122 ms) while the writes stay at 11 ms. The
  web container has one CPU (`WEB_CPUS=1`).
- The idle quick lane opens a write transaction (`BEGIN IMMEDIATE`) 20 times a second, the
  busiest writer when nothing happens; harmless now, but it competes with every other write.

## Conclusions

1. **The worker wastes most of its cores.** A computing job keeps 1.2 of 3 cores busy, because only
   the line search is parallel. Even with more cores the job would barely be faster (Gozo: 129 s
   on 24 threads, 135 s on 3).
2. **Throughput is one computing job at a time**, and a large job blocks small ones (first in, first
   out, no fairness between users): behind a Gozo job (2-3 minutes with cached data), every small
   search waits.
3. **Downloads are limited by the services**, not by the worker; one slot means a slow service
   blocks the downloads of all other jobs.
4. **SQLite fits**; the web's polling per page is what grows with many users.

## Recommendations (ranked by gain against effort)

| # | Change | Gain | Effort |
| --- | --- | --- | --- |
| 1 | `fill_gaps`: the median over the ring pixels only | ~15 % of a large job, less memory; identical results | a few lines |
| 2 | Two compute slots (and two or three download slots) per worker, admitted by memory: a tile's peak is about 85 bytes per pixel (Gozo's 65 Mpx tile 5.6 GB), so a slot is taken only when the job's largest tile fits into what remains of `WORKER_MEMORY` | ~1.8× jobs per hour on 3 cores; small jobs no longer wait for a large one | `Scheduler.running` per slot, a memory estimate in `plan`; medium |
| 3 | Fair claiming: the oldest job of the user with the fewest running jobs (round robin, already in docs/roadmap.md) | small jobs of other users are not stuck behind one user's queue | one SQL query; small |
| 4 | The tiles of a large area in parallel processes (each tile is independent until the merge) | large jobs ~N× faster with N tiles in memory | medium (memory as in 2) |
| 5 | Parallel single-core stages: `reproject(num_threads=...)`, the prominence's `argsort` (numba parallel or per band), profiles of a circle in numba `prange`, writing `lines.csv`/`lines.geojson`/`data.js` in parallel | 10-25 % each where they dominate | small to medium each |
| 6 | Downloads: one pool for both layers, 8 threads per job, a limit per host across all jobs (a file lock or a semaphore in SQLite, like Overpass), a lock per tile so two jobs never fetch the same tile | shorter downloads, politer to the services | small to medium |
| 7 | The web: one poller per process reading new events every 0.5 s and handing them to the open streams, instead of a query per page; the quick lane waits longer when idle | hundreds of open pages on one CPU | small |

The measurements apply to this PC; the VPS's cores are slower or faster per core, but the shape
(one busy core most of the time) does not depend on it.
