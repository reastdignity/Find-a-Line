"""
Slovak ALS elevation models as small tiles on Hugging Face (run by hand, not by the app).

ÚGKK SR publishes the 1 m terrain model (DMR 5.0) and surface model (DMP 1.0) of the
whole country only as one ZIP each with one huge TIFF inside (about 155–200 GB), which
cannot be read in parts over the network. This script turns such a TIFF into 2 km COG
tiles in its own grid (S-JTSK [JTSK03] EPSG:8353, Baltic 1957 heights EPSG:8357) and
uploads them to a public Hugging Face dataset, from which sources.GridTiles downloads
the tiles a search needs. Licence CC BY 4.0, author ÚGKK SR (the licence PDF in the ZIPs).

    python tools/sk_tiles.py download dmp1 --work \\\\nas\\share\\sk     # the ZIP (resumable)
    python tools/sk_tiles.py cut dmp1 --work \\\\nas\\share\\sk          # tiles + index (resumable)
    python tools/sk_tiles.py upload dmp1 --work \\\\nas\\share\\sk --repo user/name   # needs HF_TOKEN

Layout in the dataset: <product>/1m/<block>/<E>_<N>.tif, where E, N are the tile's lower
left corner in km divided by 2 (E = floor(x / 2000)) and <block> groups 10 x 10 tiles
(E // 10, N // 10) so no folder holds more than 100 files; <product>/1m/index.json lists
the tiles with their sha256. The TIFF is read straight from the ZIP (GDAL /vsizip/),
2 km rows at a time from the top, so the ZIP is read mostly forwards and never unpacked.
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
CHUNK = 25          # tiles read at once along a row (50 km: 400 MB of float32)
MAX_Z_ERROR = 0.005  # metres
HEADERS = {"User-Agent": "Find-a-Line/1.0 (highline finder; tiles for a public mirror)"}


def tile_folder(product: str) -> str:
    return f"{product}/1m"


def tile_name(i: int, j: int) -> str:
    """Path of tile (i, j) inside the product folder (the naming of sources.GridTiles)."""
    return grid_tile_name(i, j, BLOCK)


def download(product: str, work: str):
    """Download the ZIP into the work folder, resuming a partial file."""
    url = PRODUCTS[product]["zip"]
    path = os.path.join(work, os.path.basename(url))
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


def cut(product: str, work: str):
    """Cut the TIFF inside the ZIP into 2 km tiles (skips tiles already written)."""
    spec = PRODUCTS[product]
    source = f"/vsizip/{os.path.join(work, os.path.basename(spec['zip']))}/{spec['member']}"
    out = os.path.join(work, "tiles", tile_folder(product))
    index_path = os.path.join(out, "index.json")
    index = json.load(open(index_path, encoding="utf-8")) if os.path.exists(index_path) else {"tiles": {}}
    start = time.time()
    with rio.open(source) as src:
        res = src.res[0]
        assert abs(res - 1.0) < 1e-9, f"expected 1 m, got {res}"
        left, bottom, right, top = src.bounds
        # The tile grid must fall on pixel edges (whole metres here)
        assert abs(left - round(left)) < 1e-3 and abs(top - round(top)) < 1e-3, src.bounds
        nodata = src.nodata
        rows = range(math.ceil(top / TILE_M) - 1, math.floor(bottom / TILE_M) - 1, -1)  # from the top
        cols = range(math.floor(left / TILE_M), math.ceil(right / TILE_M))
        print(f"{source}: {src.width} x {src.height} px, nodata {nodata}, {len(rows)} x {len(cols)} tiles", flush=True)
        for n, j in enumerate(rows):
            # A 2 km row of the raster, 50 km at a time from the left (mostly forwards in the ZIP)
            row0 = round(top - (j + 1) * TILE_M)
            for group in [cols[k:k + CHUNK] for k in range(0, len(cols), CHUNK)]:
                todo = [i for i in group if f"{i}_{j}" not in index["tiles"]]
                if not todo:
                    continue
                c0 = round((group[0] * TILE_M - left) / res)
                width = len(group) * TILE_M
                part = src.read(1, window=Window(c0, row0, width, TILE_M), boundless=True, fill_value=nodata,
                                masked=True).astype(np.float32).filled(np.nan)
                for i in todo:
                    tile = np.ascontiguousarray(part[:, (i - group[0]) * TILE_M:(i - group[0] + 1) * TILE_M])
                    if not np.isfinite(tile).any():
                        index["tiles"][f"{i}_{j}"] = None  # no data: not uploaded
                        continue
                    path = os.path.join(out, tile_name(i, j))
                    os.makedirs(os.path.dirname(path), exist_ok=True)
                    _write(path, tile, src.crs, from_origin(i * TILE_M, (j + 1) * TILE_M, res, res))
                    index["tiles"][f"{i}_{j}"] = _sha256(path)
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


def upload(product: str, work: str, repo: str):
    """Upload the tiles and the index (resumable; needs a write token in HF_TOKEN)."""
    from huggingface_hub import HfApi
    token = os.environ.get("HF_TOKEN")
    if not token:
        sys.exit("Set HF_TOKEN to a Hugging Face token with write access")
    api = HfApi(token=token)
    api.create_repo(repo, repo_type="dataset", exist_ok=True)
    root = os.path.join(work, "tiles")
    with open(os.path.join(root, "README.md"), "w", encoding="utf-8") as f:
        f.write(CARD)
    # With hf_xet (installed with huggingface_hub 2.x) a large folder goes in several commits,
    # and running the same call again resumes it
    api.upload_folder(repo_id=repo, repo_type="dataset", folder_path=root,
                      allow_patterns=[f"{tile_folder(product)}/**", "README.md"],
                      commit_message=f"{product}: 2 km tiles")
    print(f"uploaded {product} to https://huggingface.co/datasets/{repo}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("step", choices=["download", "cut", "upload"])
    parser.add_argument("product", choices=list(PRODUCTS))
    parser.add_argument("--work", required=True, help="working folder (ZIP and tiles; e.g. a NAS share)")
    parser.add_argument("--repo", help="Hugging Face dataset, user/name (upload)")
    args = parser.parse_args()
    os.makedirs(args.work, exist_ok=True)
    if args.step == "download":
        download(args.product, args.work)
    elif args.step == "cut":
        cut(args.product, args.work)
    else:
        if not args.repo:
            sys.exit("--repo user/name is needed for the upload")
        upload(args.product, args.work, args.repo)


if __name__ == "__main__":
    main()
