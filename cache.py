"""
The data folder and the index of downloaded files.

Data folder (--data-dir or FINDALINE_DATA; default: the folder of the scripts):
    cache/     downloaded data (below)
    runs/      saved runs (runs.py)
    work/      working grids of the searches in progress, one folder per search,
               deleted when the search ends without an error

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
"""

import hashlib
import os
import re
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional

LAYOUT = 1
SCHEMA = """CREATE TABLE IF NOT EXISTS files (
    path TEXT PRIMARY KEY,      -- relative to the top-level folder, with /
    status TEXT NOT NULL,       -- ok | missing
    url TEXT, fetched_at TEXT, checked_at TEXT, last_used_at TEXT,
    etag TEXT, last_modified TEXT, sha256 TEXT, source_date TEXT, processing INTEGER)"""


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


def remove_work(path: str):
    try:
        shutil.rmtree(path)
    except OSError as e:  # e.g. a memory-mapped grid still open on Windows
        print(f"Could not delete the work folder {path}: {e}")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def clean_url(url: Optional[str]) -> Optional[str]:
    """The URL without API keys."""
    return re.sub(r"((?:api_?key|key|token)=)[^&]+", r"\1...", url, flags=re.I) if url else url


def _split(cache_dir: str, path: str):
    rel = os.path.relpath(path, cache_dir).replace("\\", "/")
    top, _, inner = rel.partition("/")
    return top, inner


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
        rows = [(inner, status, clean_url(url), now, now, now, etag, last_modified,
                 _sha256(path) if status == "ok" else None, source_date, processing)
                for path, inner in items]
        with _connect(cache_dir, top) as db:
            db.executemany("INSERT OR REPLACE INTO files VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
        db.close()


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
