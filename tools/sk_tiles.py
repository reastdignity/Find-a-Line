"""
Slovak ALS elevation models as small tiles on Hugging Face (run by hand, not by the app).

ÚGKK SR publishes the 1 m terrain model (DMR 5.0) and surface model (DMP 1.0) of the
whole country only as one ZIP each with one huge TIFF inside (about 155–200 GB), which
cannot be read in parts over the network. This script turns such a TIFF into 2 km COG
tiles in its own grid (S-JTSK [JTSK03] EPSG:8353, Baltic 1957 heights EPSG:8357) and
writes them into a folder laid out as a Hugging Face dataset. Until it is uploaded, that
folder is a local mirror: sources.GridTiles reads it through FINDALINE_MIRROR (the folder
above it, see sources.mirror_urls), later it downloads the same tiles from Hugging Face.
Licence CC BY 4.0, author ÚGKK SR (the licence PDF in the ZIPs).

    python tools/sk_tiles.py download dmp1 --zips C:\\sk-zips                    # the ZIP (resumable)
    python tools/sk_tiles.py extract dmp1 --zips C:\\sk-zips                     # the TIFF beside it
    python tools/sk_tiles.py cut dmp1 --zips C:\\sk-zips --out C:\\mirror\\slovakia  # tiles + index (resumable)
    python tools/sk_tiles.py upload dmp1 --out C:\\mirror\\slovakia --repo user/name  # needs HF_TOKEN

--work DIR stands for both --zips DIR and --out DIR/tiles.

Layout in the dataset: <product>/1m/<block>/<E>_<N>.tif, where E, N are the tile's lower
left corner in km divided by 2 (E = floor(x / 2000)) and <block> groups 10 x 10 tiles
(E // 10, N // 10) so no folder holds more than 100 files; <product>/1m/index.json lists
the tiles with their sha256 (None: no data there). The TIFF is read in 2 km bands of the
full width from the top. Extract it first (122–157 GB): GDAL can read it straight from the
ZIP (/vsizip/; the national ZIPs are plain Deflate, forwards only), but at about 7 MB/s
(DMP 1.0: 9 min per band, 15 h in all) against 400 MB/s for Python's zipfile, and from the
plain file the band takes a fraction of that; cut uses the extracted TIFF when it is there.
Encoding: LERC_ZSTD with a maximum error of 5 mm (5.5 MB per km² at 0.5 m in the trial
on lot 04, against 9.4 MB lossless; the surveys are accurate to centimetres).
"""

import argparse
import datetime
import hashlib
import json
import math
import os
import sys
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

import numpy as np
import rasterio as rio
import requests
from rasterio.transform import from_origin
from rasterio.windows import Window

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sources import grid_tile_name  # noqa: E402

BASE = "https://opendata.skgeodesy.sk/static/LLS"
PRODUCTS = {
    "dmr5": {"zip": f"{BASE}/DMR5/DMR5_0_sjtsk03_bpv.zip", "member": "dmr5_0/dmr5_jtsk03.tif",
             "title": "DMR 5.0 digital terrain model (ground)"},
    "dmp1": {"zip": f"{BASE}/DMP1/DMP1_0_sjtsk03_bpv.zip", "member": "dmp1_0/dmp1_jtsk03.tif",
             "title": "DMP 1.0 digital surface model (with vegetation and buildings)"},
}
TILE_M = 2000
BLOCK = 10          # tiles per folder side
MAX_Z_ERROR = 0.005  # metres
# GDAL's block cache keeps the TIFF blocks a band shares with the previous one (a block
# row of the whole country is a few hundred MB), so they are not read again
CACHE_MB = 4096
WRITE_THREADS = os.cpu_count() or 4  # LERC and the COG driver release the GIL
HEADERS = {"User-Agent": "Find-a-Line/1.0 (highline finder; tiles for a public mirror)"}


def tile_folder(product: str) -> str:
    return f"{product}/1m"


def tile_name(i: int, j: int) -> str:
    """Path of tile (i, j) inside the product folder (the naming of sources.GridTiles)."""
    return grid_tile_name(i, j, BLOCK)


def download(product: str, zips: str):
    """Download the ZIP into the folder zips, resuming a partial file."""
    url = PRODUCTS[product]["zip"]
    path = os.path.join(zips, os.path.basename(url))
    total = int(requests.head(url, headers=HEADERS, timeout=60).headers["Content-Length"])
    done = os.path.getsize(path) if os.path.exists(path) else 0
    if done == total:
        print(f"{path}: complete ({total / 1e9:.1f} GB)")
        return path
    start, last = time.time(), time.time()
    with requests.get(url, headers={**HEADERS, "Range": f"bytes={done}-"}, stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(path, "ab") as f:
            for chunk in r.iter_content(chunk_size=8 << 20):
                f.write(chunk)
                done += len(chunk)
                if time.time() - last > 30:
                    last = time.time()
                    print(f"  {done / 1e9:.1f} of {total / 1e9:.1f} GB, {done / total:.1%}, "
                          f"{(time.time() - start) / 60:.0f} min", flush=True)
    print(f"{path}: {done / 1e9:.1f} GB in {(time.time() - start) / 60:.0f} min")
    return path


def extracted(product: str, zips: str) -> str:
    """Path of the TIFF extracted beside the ZIP."""
    return os.path.join(zips, os.path.basename(PRODUCTS[product]["member"]))


def extract(product: str, zips: str):
    """Extract the TIFF from the ZIP into the folder zips (through a .part file)."""
    spec = PRODUCTS[product]
    path = extracted(product, zips)
    with zipfile.ZipFile(os.path.join(zips, os.path.basename(spec["zip"]))) as archive:
        total = archive.getinfo(spec["member"]).file_size
        if os.path.exists(path) and os.path.getsize(path) == total:
            print(f"{path}: already extracted ({total / 1e9:.1f} GB)")
            return path
        start, last, done = time.time(), time.time(), 0
        with archive.open(spec["member"]) as src, open(path + ".part", "wb") as dst:
            while chunk := src.read(64 << 20):
                dst.write(chunk)
                done += len(chunk)
                if time.time() - last > 30:
                    last = time.time()
                    print(f"  {done / 1e9:.1f} of {total / 1e9:.1f} GB, {(time.time() - start) / 60:.0f} min",
                          flush=True)
    os.replace(path + ".part", path)
    print(f"{path}: {total / 1e9:.1f} GB in {(time.time() - start) / 60:.0f} min")
    return path


def _write(path: str, band: np.ndarray, crs, transform):
    tmp = path + ".part"
    with rio.open(tmp, "w", driver="COG", width=band.shape[1], height=band.shape[0], count=1, dtype="float32",
                  crs=crs, transform=transform, nodata=np.nan, compress="LERC_ZSTD", max_z_error=MAX_Z_ERROR,
                  blocksize=512, overview_resampling="average") as dst:
        dst.write(band, 1)
    os.replace(tmp, path)


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def cut(product: str, zips: str, root: str, rows_limit: Optional[int] = None):
    """Cut the TIFF (extracted, or else inside the ZIP) into 2 km tiles under root (skips
    rows already written; rows_limit stops after that many rows, for a timing trial)."""
    spec = PRODUCTS[product]
    source = extracted(product, zips)
    if not os.path.exists(source):
        source = f"/vsizip/{os.path.join(zips, os.path.basename(spec['zip']))}/{spec['member']}"
    out = os.path.join(root, tile_folder(product))
    index_path = os.path.join(out, "index.json")
    index = {"tiles": {}}
    if os.path.exists(index_path):
        with open(index_path, encoding="utf-8") as f:
            index = json.load(f)
    start = time.time()
    with rio.Env(GDAL_CACHEMAX=CACHE_MB, GDAL_NUM_THREADS="ALL_CPUS"), rio.open(source) as src, \
            ThreadPoolExecutor(max_workers=WRITE_THREADS) as pool:
        res = src.res[0]
        assert abs(res - 1.0) < 1e-9, f"expected 1 m, got {res}"
        left, bottom, right, top = src.bounds
        # The tile grid must fall on pixel edges (whole metres here)
        assert abs(left - round(left)) < 1e-3 and abs(top - round(top)) < 1e-3, src.bounds
        nodata = src.nodata
        rows = range(math.ceil(top / TILE_M) - 1, math.floor(bottom / TILE_M) - 1, -1)  # from the top
        cols = range(math.floor(left / TILE_M), math.ceil(right / TILE_M))
        print(f"{source}: {src.width} x {src.height} px, blocks {src.block_shapes[0]}, nodata {nodata}, "
              f"{len(rows)} x {len(cols)} tiles", flush=True)
        for n, j in enumerate(rows):
            if rows_limit is not None and n >= rows_limit:
                break
            todo = [i for i in cols if f"{i}_{j}" not in index["tiles"]]
            if not todo:
                continue
            # A 2 km band of the full width: the ZIP is read forwards only
            # (rasterio's boundless reads go through a warped VRT: the window is clipped here instead)
            row0 = round(top - (j + 1) * TILE_M)
            c0 = round((cols[0] * TILE_M - left) / res)
            band = np.full((TILE_M, len(cols) * TILE_M), np.nan, dtype=np.float32)
            r_in, c_in = max(row0, 0), max(c0, 0)
            r_end, c_end = min(row0 + TILE_M, src.height), min(c0 + len(cols) * TILE_M, src.width)
            part = src.read(1, window=Window(c_in, r_in, c_end - c_in, r_end - r_in), masked=True)
            band[r_in - row0:r_end - row0, c_in - c0:c_end - c0] = part.astype(np.float32).filled(np.nan)
            del part

            def save(i):
                tile = np.ascontiguousarray(band[:, (i - cols[0]) * TILE_M:(i - cols[0] + 1) * TILE_M])
                if not np.isfinite(tile).any():
                    return None  # no data: not uploaded
                path = os.path.join(out, tile_name(i, j))
                os.makedirs(os.path.dirname(path), exist_ok=True)
                _write(path, tile, src.crs, from_origin(i * TILE_M, (j + 1) * TILE_M, res, res))
                return _sha256(path)

            for i, digest in zip(todo, pool.map(save, todo)):
                index["tiles"][f"{i}_{j}"] = digest
            del band
            index.update(product=product, title=spec["title"], source=spec["zip"], crs="EPSG:8353",
                         heights="EPSG:8357 (Baltic 1957)", tile_m=TILE_M, resolution=res, block=BLOCK,
                         encoding=f"COG LERC_ZSTD, max_z_error {MAX_Z_ERROR} m", licence="CC-BY-4.0",
                         attribution="ÚGKK SR", created=datetime.date.today().isoformat())
            os.makedirs(out, exist_ok=True)
            with open(index_path + ".part", "w", encoding="utf-8") as f:
                json.dump(index, f)
            os.replace(index_path + ".part", index_path)
            done = sum(v is not None for v in index["tiles"].values())
            print(f"  row {n + 1}/{len(rows)}: {done} tiles with data, {(time.time() - start) / 60:.0f} min",
                  flush=True)
    write_card(root)
    print(f"done: {sum(v is not None for v in index['tiles'].values())} tiles in {(time.time() - start) / 60:.0f} min")


CARD = """---
license: cc-by-4.0
pretty_name: Slovakia ALS elevation models (ÚGKK SR) as 2 km tiles
tags: [geospatial, elevation, lidar, slovakia]
---
# Slovakia ALS elevation models as 2 km tiles

Tiles of the digital terrain model **DMR 5.0** and digital surface model **DMP 1.0** of the
Slovak airborne laser scanning project (1st cycle, 2017–2023), 1 m, made from the files
published by **ÚGKK SR** (Úrad geodézie, kartografie a katastra Slovenskej republiky) at
https://www.geoportal.sk/en/zbgis/als/ under **CC BY 4.0**
(https://creativecommons.org/licenses/by/4.0/). Source: © ÚGKK SR.

Changes made: the national rasters (S-JTSK [JTSK03] EPSG:8353, Baltic 1957 heights EPSG:8357)
were cut into 2 km × 2 km Cloud Optimized GeoTIFFs on the same pixel grid and stored with
LERC compression with a maximum error of 5 mm. Values are not otherwise changed.

Layout: `<product>/1m/<E//10>_<N//10>/<E>_<N>.tif`, where E = floor(x / 2000) and
N = floor(y / 2000) of the tile's lower left corner; `<product>/1m/index.json` lists every tile
with its sha256. Made for Find-a-Line (a highline finder); not endorsed by ÚGKK SR.
"""


def write_card(root: str):
    """The dataset card (README.md) next to the product folders: the licence and attribution
    travel with the tiles, in the local mirror as on Hugging Face."""
    with open(os.path.join(root, "README.md"), "w", encoding="utf-8") as f:
        f.write(CARD)


def upload(product: str, root: str, repo: str):
    """Upload the tiles and the index (resumable; needs a write token in HF_TOKEN).

    To a dataset repository, as decided in the roadmap; a Storage Bucket (like FABDEM's)
    is still open: then only the destination of this call changes, not the layout."""
    from huggingface_hub import HfApi
    token = os.environ.get("HF_TOKEN")
    if not token:
        sys.exit("Set HF_TOKEN to a Hugging Face token with write access")
    api = HfApi(token=token)
    api.create_repo(repo, repo_type="dataset", exist_ok=True)
    write_card(root)
    # With hf_xet (installed with huggingface_hub 2.x) a large folder goes in several commits,
    # and running the same call again resumes it
    api.upload_folder(repo_id=repo, repo_type="dataset", folder_path=root,
                      allow_patterns=[f"{tile_folder(product)}/**", "README.md"],
                      commit_message=f"{product}: 2 km tiles")
    print(f"uploaded {product} to https://huggingface.co/datasets/{repo}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("step", choices=["download", "extract", "cut", "upload"])
    parser.add_argument("product", choices=list(PRODUCTS))
    parser.add_argument("--zips", help="folder of the ZIPs (download, cut)")
    parser.add_argument("--out", help="folder of the tiles, laid out as the dataset (cut, upload)")
    parser.add_argument("--work", help="the same as --zips WORK --out WORK/tiles")
    parser.add_argument("--rows", type=int, help="cut: stop after this many rows (a timing trial)")
    parser.add_argument("--repo", help="Hugging Face dataset, user/name (upload)")
    args = parser.parse_args()
    zips = args.zips or args.work
    out = args.out or (os.path.join(args.work, "tiles") if args.work else None)
    if args.step in ("download", "extract", "cut") and not zips:
        sys.exit("--zips (or --work) is needed")
    if args.step in ("cut", "upload") and not out:
        sys.exit("--out (or --work) is needed")
    if args.step == "download":
        os.makedirs(zips, exist_ok=True)
        download(args.product, zips)
    elif args.step == "extract":
        extract(args.product, zips)
    elif args.step == "cut":
        cut(args.product, zips, out, args.rows)
    else:
        if not args.repo:
            sys.exit("--repo user/name is needed for the upload")
        upload(args.product, out, args.repo)


if __name__ == "__main__":
    main()
