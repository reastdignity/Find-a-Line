"""
The data folder and the index of downloaded files.

Data folder (--data-dir or FINDALINE_DATA; default: the folder of the scripts):
    cache/     downloaded data (below)
    runs/      saved runs (runs.py)
    work/      working grids of the searches in progress, one folder per search,
               deleted when the search ends without an error; the web worker's are
               work/jobs/<job>/<phase>

Cache layout: <country or global dataset>/<dataset>/<edition>/<tiling>/<tile>, e.g.
    poland/nmt/<year>_<sheet file>.tif           (sheets never change: no edition)
    malta/dtm/2018/epsg32633-1m-2000px-tif/12_34.tif
    spain/mdt05/live/epsg25830-5m-2000px-asc/1_2.tif   ("live": the current state of a service)
    copernicus30/2021/dem/N50E019.tif
    osm/water-polygons/<date>/..., osm/themes/<theme>/<tile>.geojson, osm/nominatim.sqlite
Deleting a top-level folder (a country or a global dataset) removes everything about it,
its index included. cache/LAYOUT holds the layout version; a cache of another layout
is not read (delete it: everything is downloaded again when needed).

Every top-level folder has an index.sqlite with one row per file: its status
("ok", or "missing" when the service has no data there), when it was fetched,
checked and last used, ETag / Last-Modified, sha256, the date of the source data and
the processing version. URLs are stored without API keys. A row without its file
counts as not downloaded; a file without a row is adopted.

Updates (update.py, and --update of a search) never happen on their own. Data that
can change is checked again after its interval (TTL_DAYS); versioned releases
(Copernicus 2021, FABDEM v1-2, Malta 2018...) never change and are never checked.
A file downloaded again with the same content keeps its fetch time (only the check
time changes), so the fetch time says when the data last changed.
"""

import contextlib
import hashlib
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional

LAYOUT = 1
USER_AGENT = "Find-a-Line/1.0 (highline finder)"  # of every request (http_headers)
# Days after which `update stale` checks data again
TTL_DAYS = {
    "live": 730,         # tiles of services that serve the current state (IGN Spain, WMS orthophotos)
    "sheet-index": 180,  # answers of the geoportal.gov.pl sheet index (new surveys)
    "osm-theme": 365,    # OSM theme tiles from Overpass (tiles built from an extract follow the extract)
    "osm-extract": 30,   # Geofabrik extracts (daily diffs)
    "water-polygons": 365,  # OSM water polygons of the world (regenerated daily, about 900 MB)
}
MISSING_TTL_DAYS = 90    # "missing" tiles of data that can change
SCHEMA = """CREATE TABLE IF NOT EXISTS files (
    path TEXT PRIMARY KEY,      -- relative to the top-level folder, with /
    status TEXT NOT NULL,       -- ok | missing
    url TEXT, fetched_at TEXT, checked_at TEXT, last_used_at TEXT,
    etag TEXT, last_modified TEXT, sha256 TEXT, source_date TEXT, processing INTEGER)"""


@contextlib.contextmanager
def replacing(path: str, mode: str = "wb", **kwargs):
    """A file that replaces `path` once it is written completely: written under a unique
    temporary name next to it (tempfile, so searches, downloads and the web worker never
    share one) and moved over `path` at the end; removed if writing fails."""
    tmp = tempfile.NamedTemporaryFile(mode, dir=os.path.dirname(path) or ".", prefix=os.path.basename(path) + ".",
                                      suffix=".part", delete=False, **kwargs)
    try:
        with tmp as f:
            yield f
        os.chmod(tmp.name, 0o644)  # tempfile creates it readable by its owner only (POSIX)
        os.replace(tmp.name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp.name)
        raise


def _statements(script: str) -> Iterable[str]:
    """The SQL statements of a script, one by one (executescript would commit before it runs)."""
    current = ""
    for part in script.split(";"):
        current += part + ";"
        if sqlite3.complete_statement(current):
            if current.strip(" \t\r\n;"):
                yield current
            current = ""


def migrate(db: sqlite3.Connection, steps: List) -> int:
    """Bring a database of the data folder to its newest schema; returns its version.

    The schema version is PRAGMA user_version: steps[0] makes version 1, steps[1]
    version 2 and so on. Each step is an SQL script or a callable taking the connection.
    The missing steps run in one transaction (BEGIN IMMEDIATE, so two processes opening
    the database at once migrate it once); if one fails, the database stays as it was.
    A step is never changed once released: a change of the schema is a new step.
    """
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if version >= len(steps):
        return version
    isolation, db.isolation_level = db.isolation_level, None  # our own BEGIN / COMMIT
    try:
        db.execute("BEGIN IMMEDIATE")
        try:
            version = db.execute("PRAGMA user_version").fetchone()[0]  # another process may have migrated
            for number, step in enumerate(steps[version:], version + 1):
                if callable(step):
                    step(db)
                else:
                    for statement in _statements(step):
                        db.execute(statement)
                db.execute(f"PRAGMA user_version = {number:d}")
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise
    finally:
        db.isolation_level = isolation
    return max(version, len(steps))


def data_dir(arg: Optional[str] = None) -> str:
    """The data folder: --data-dir, else FINDALINE_DATA, else the folder of the scripts."""
    return os.path.abspath(arg or os.environ.get("FINDALINE_DATA") or os.path.dirname(os.path.abspath(__file__)))


def check_layout(cache_dir: str):
    """Stop with a clear message when the cache has another layout; mark a new cache."""
    marker = os.path.join(cache_dir, "LAYOUT")
    if os.path.isdir(cache_dir) and os.listdir(cache_dir):
        found = open(marker).read().strip() if os.path.exists(marker) else "none"
        if found != str(LAYOUT):
            sys.exit(f"The cache in {cache_dir} has another layout ({found}, expected {LAYOUT}). "
                     "Delete that folder (everything is downloaded again when needed), or keep only "
                     "what you need after moving it to the new layout (see cache.py).")
        return
    os.makedirs(cache_dir, exist_ok=True)
    with open(marker, "w") as f:
        f.write(f"{LAYOUT}\n")


def new_work(data: str) -> str:
    """A fresh work folder for one search."""
    path = os.path.join(data, "work", f"{datetime.now():%Y%m%d-%H%M%S}-{os.getpid()}")
    os.makedirs(path, exist_ok=True)
    return path


def remove_stale_parts(cache_dir: str, days: float = 1.0) -> int:
    """Delete the temporary files of downloads (*.part: replacing, sources._part, orthophoto tiles)
    not written to for `days`: left by a process killed while writing. Returns their number."""
    limit, removed = time.time() - days * 86400, 0
    for folder, _, names in os.walk(cache_dir):
        for name in names:
            if name.endswith(".part"):
                path = os.path.join(folder, name)
                with contextlib.suppress(OSError):  # gone meanwhile, or still open (Windows)
                    if os.path.getmtime(path) < limit:
                        os.remove(path)
                        removed += 1
    return removed


def remove_work(path: str):
    try:
        shutil.rmtree(path)
    except OSError as e:  # e.g. a memory-mapped grid still open on Windows
        print(f"Could not delete the work folder {path}: {e}")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def http_headers() -> Dict[str, str]:
    """Headers of every request to a data service: one User-Agent naming the app.

    With FINDALINE_CONTACT set (a public site) it carries the operator's contact too, as
    the Nominatim and Overpass usage policies ask of an application with many users.
    """
    contact = " ".join(os.environ.get("FINDALINE_CONTACT", "").split())  # no line breaks in a header
    return {"User-Agent": f"{USER_AGENT[:-1]}; contact: {contact})" if contact else USER_AGENT}


def clean_url(url: Optional[str]) -> Optional[str]:
    """The URL without API keys."""
    return re.sub(r"((?:api_?key|key|token)=)[^&]+", r"\1...", url, flags=re.I) if url else url


def clean_error(error) -> str:
    """The message of an error (or a text) with the query strings of its URLs cut off.

    requests puts the whole URL into its errors ("... for url: https://...?API_Key=..."),
    and these messages reach the terminal, job events and the web page; a key or token
    in a query must not. A query is "?" followed by text with "=" and no spaces.
    """
    return re.sub(r"\?[^\s'\"()<>?]*=[^\s'\"()<>]*", "?...", str(error))


def _split(cache_dir: str, path: str):
    rel = os.path.relpath(path, cache_dir).replace("\\", "/")
    top, _, inner = rel.partition("/")
    return top, inner


def relative(cache_dir: str, path: str) -> str:
    """A path in the cache as stored in runs and listings: relative, with /."""
    return os.path.relpath(path, cache_dir).replace("\\", "/")


def _connect(cache_dir: str, top: str) -> sqlite3.Connection:
    os.makedirs(os.path.join(cache_dir, top), exist_ok=True)
    db = sqlite3.connect(os.path.join(cache_dir, top, "index.sqlite"), timeout=60)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute(SCHEMA)
    return db


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def record(cache_dir: str, path: str, status: str = "ok", url: Optional[str] = None,
           etag: Optional[str] = None, last_modified: Optional[str] = None,
           source_date: Optional[str] = None, processing: int = 1):
    """Note a downloaded file (or that the service has no data for it)."""
    record_many(cache_dir, [path], status, url, etag, last_modified, source_date, processing)


def record_many(cache_dir: str, paths: Iterable[str], status: str = "ok", url: Optional[str] = None,
                etag: Optional[str] = None, last_modified: Optional[str] = None,
                source_date: Optional[str] = None, processing: int = 1):
    by_top: Dict[str, List[str]] = {}
    for path in paths:
        top, inner = _split(cache_dir, path)
        by_top.setdefault(top, []).append((path, inner))
    now = _now()
    for top, items in by_top.items():
        with _connect(cache_dir, top) as db:
            rows = []
            for path, inner in items:
                sha = _sha256(path) if status == "ok" else None
                old = db.execute("SELECT status, sha256, fetched_at, last_used_at FROM files WHERE path = ?",
                                 (inner,)).fetchone()
                # The same content again: only the check time changes
                fetched = old[2] if old and old[0] == status and old[1] == sha and old[2] else now
                rows.append((inner, status, clean_url(url), fetched, now, (old and old[3]) or now, etag,
                             last_modified, sha, source_date, processing))
            db.executemany("INSERT OR REPLACE INTO files VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
        db.close()


def checked(cache_dir: str, paths: Iterable[str]):
    """Note that files were checked and are still current."""
    now = _now()
    by_top: Dict[str, List[str]] = {}
    for path in paths:
        top, inner = _split(cache_dir, path)
        by_top.setdefault(top, []).append(inner)
    for top, inners in by_top.items():
        with _connect(cache_dir, top) as db:
            db.executemany("UPDATE files SET checked_at = ? WHERE path = ?", [(now, i) for i in inners])
        db.close()


def forget(cache_dir: str, paths: Iterable[str]):
    """Delete files and their rows (e.g. a sheet replaced by a newer survey)."""
    for path in paths:
        if os.path.exists(path):
            os.remove(path)
        top, inner = _split(cache_dir, path)
        with _connect(cache_dir, top) as db:
            db.execute("DELETE FROM files WHERE path = ?", (inner,))
        db.close()


def info(cache_dir: str, path: str) -> Optional[Dict]:
    """The index row of a file, as a dict (None if it has none)."""
    top, inner = _split(cache_dir, path)
    with _connect(cache_dir, top) as db:
        db.row_factory = sqlite3.Row
        row = db.execute("SELECT * FROM files WHERE path = ?", (inner,)).fetchone()
    db.close()
    return dict(row) if row else None


def rows(cache_dir: str, top: str) -> List[Dict]:
    """All index rows of a top-level folder; "path" is relative to cache_dir."""
    if not os.path.exists(os.path.join(cache_dir, top, "index.sqlite")):
        return []
    with _connect(cache_dir, top) as db:
        db.row_factory = sqlite3.Row
        found = [dict(r) for r in db.execute("SELECT * FROM files")]
    db.close()
    for row in found:
        row["path"] = f"{top}/{row['path']}"
    return found


def age_days(timestamp: Optional[str]) -> float:
    """Days since an index time (infinite for none)."""
    if not timestamp:
        return float("inf")
    return (datetime.now(timezone.utc) - datetime.fromisoformat(timestamp)) / timedelta(days=1)


def stale(row: Optional[Dict], ttl: Optional[int], update: Optional[str]) -> bool:
    """Whether an update ("stale" or "all") checks a file of an index row again.

    ttl: days between checks of data that can change; None for data that never changes.
    """
    if not update or ttl is None or row is None:
        return False
    if update == "all":
        return True
    limit = MISSING_TTL_DAYS if row["status"] == "missing" else ttl
    return age_days(row["checked_at"]) > limit


def due(cache_dir: str, path: str, processing: int = 1, update: Optional[str] = None,
        ttl: Optional[int] = None) -> bool:
    """Whether a file has to be downloaded: not there yet, or to be checked again (stale)."""
    if state(cache_dir, path, processing) is None:
        return True
    return stale(info(cache_dir, path), ttl, update)


def state(cache_dir: str, path: str, processing: int = 1) -> Optional[str]:
    """"ok", "missing" (the service has no data there) or None (to be downloaded)."""
    top, inner = _split(cache_dir, path)
    with _connect(cache_dir, top) as db:
        row = db.execute("SELECT status, processing FROM files WHERE path = ?", (inner,)).fetchone()
        if row and row[1] is not None and row[1] != processing:
            result = None  # made by another version of the processing: fetch again
        elif os.path.exists(path):
            if row is None:  # a file without a row is adopted
                mtime = datetime.fromtimestamp(os.path.getmtime(path), timezone.utc).isoformat(timespec="seconds")
                db.execute("INSERT INTO files (path, status, fetched_at, checked_at, processing) "
                           "VALUES (?, 'ok', ?, ?, ?)", (inner, mtime, mtime, processing))
            result = "ok"
        else:
            result = "missing" if row and row[0] == "missing" else None
    db.close()
    return result


def touch(cache_dir: str, paths: Iterable[str]) -> Dict[str, Optional[str]]:
    """Mark files as used now; return the range of their fetch times (oldest, newest)."""
    by_top: Dict[str, List[str]] = {}
    for path in paths:
        top, inner = _split(cache_dir, path)
        by_top.setdefault(top, []).append(inner)
    now, fetched = _now(), []
    for top, inners in by_top.items():
        with _connect(cache_dir, top) as db:
            db.executemany("UPDATE files SET last_used_at = ? WHERE path = ?", [(now, i) for i in inners])
            for inner in inners:
                row = db.execute("SELECT fetched_at FROM files WHERE path = ?", (inner,)).fetchone()
                if row and row[0]:
                    fetched.append(row[0])
        db.close()
    return {"oldest": min(fetched, default=None), "newest": max(fetched, default=None)}
