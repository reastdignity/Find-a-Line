# Finding and adding a data source

How to research a country's elevation data and decide how Find-a-Line gets it. Learned with Poland, Malta, Spain (2026-10-23/25), with Switzerland, Slovakia and Greece (2026-10-25) and with the USA (2026-10-26/30; see the roadmap, "New data sources"). The mechanics of adding a `Source` are in README.md, "Adding a country"; the data checks after it are in docs/regression-tests.md, "Data checks for a new source".

## 1. What to find out
For the terrain model (DTM, ground only) and the surface model (DSM, trees and buildings) separately:

| Question | Why it matters | Bad answers seen |
| --- | --- | --- |
| Resolution and coverage; survey years; is a newer cycle under way? | 1–2 m is the useful range; a partial newer cycle means versions per area | Slovakia: 0.5 m 2nd cycle in 16 of 73 lots |
| The program's own status map and plans (coverage now, next year) | where the data really is, what is restricted or in progress; links for the roadmap | USGS 3DEP: FY26 status and FY27 partnership maps; "some areas are restricted" (tribal lands) |
| Does a newer survey replace a whole tile, or only part of it? | "newest file per tile" is right only for whole tiles | USA: a 2020 project a 10 MB sliver of a tile whose 2019 file covers it all |
| Is there a seamless product built from the surveys? | one file per place, but it may lag the surveys and use another CRS | USGS S1M: 20% of CONUS, Albers, older than the newest projects |
| File layout: tiled or COG, file size | tiled files can be read in windows over HTTP, so huge files are no obstacle | USA: 10–450 MB tiles, Alaska 900 MB cells, all tiled |
| Licence and who may get it | must allow automatic download (and, for a public site, redistribution) | Greece: public bodies only |
| CRS and **vertical datum**, as the files name them | heights shown to users; gap filling against Copernicus; the working grid | Slovakia WCS: ellipsoidal heights, 43 m above sea level; Slovakia's TIFFs: a `LOCAL_CS` only; Alaska's IfSAR: Albers on "GRS80, datum not specified" |
| Value precision (cm, mm, whole metres) | whole metres make slopes and clearances coarse | Malta 2018 and Spain's WCS GeoTIFF: whole metres |
| Pixel alignment (edges or centres on whole metres) | `grid_origin`; half a pixel off smooths cliff edges | Poland's sheets: centres on whole metres (`grid_origin` 0.5) |
| Access methods (below), with the source's own recommendations, fair use, rate limits | how to download without being blocked | swisstopo: "use the download service", no bots |
| Does a DSM exist and is it reachable the same way? | without it, lines over trees are not marked | Slovakia: DSM only in 155 GB ZIPs |
| Orthophoto WMS/WMTS: CRS (EPSG:4326 or 3857), terms, watermarks | the map background, offline tiles | Greece: watermark; EPSG:900913 gives blank images |

Where to look: the national mapping agency's geoportal and its "download" or "open data" pages; the INSPIRE geoportal of the country (CSW search for "Elevation"); data.europa.eu with a country filter; the JRC survey of open LiDAR in Europe (publications.jrc.ec.europa.eu, JRC126223); OpenTopography's catalogue for small LiDAR sets. Read the terms page itself, and the licence files inside downloads (Slovakia's ZIPs carry a PDF).

## 2. Ways to get the data, best first

| Way | Use when | Pros | Cons | In the code |
| --- | --- | --- | --- | --- |
| **Files per sheet/tile, found through an index** (WFS sheet index, STAC API) | the source publishes ready files | fast static downloads (Poland 17 MB/s, Switzerland 40 MB/s), full precision, versions visible (year in the name), what sources recommend for offline use | whole sheets even for a small area; versions to choose and prune; an index to query (cache its answers) | `SheetTiles` (Poland), `StacTiles` (Switzerland); both `IndexedTiles` |
| **Windows of big tiled files over HTTP** (GDAL `/vsicurl/`, range requests) | files of hundreds of MB that are tiled or COG | only the blocks over the area (El Capitan: 15 MB instead of 560 MB), full precision, no conversion, exact pixels | a block per request round (about 2 s per 1 km); versions must be composed when surveys cover parts of tiles; each file's CRS must be checked | `TnmBlocks` (USA, through the TNM API; composed newest first), `DegreeBlocks` (1° files at a fixed URL) |
| **Files on a fixed grid** (URL from the tile name) | tiles on a known grid, one version | simplest, no index | no version information | `DegreeTiles` (Copernicus, FABDEM) |
| **WCS 2.0** | no files, or files only as huge archives | any box, small requests | the server generates every tile (slow: Poland 30–50 s per 2 km); server-side downsampling may lose data or average oddly; size limits (~2000 px, Slovakia fails at 20 km); 404 beyond the coverage (clip to DescribeCoverage); GeoTIFF output may be 8-bit or whole metres (prefer ASCII grid or float GeoTIFF; ask for DEFLATE) | `WcsTiles` with `fetch_res` = native resolution |
| **Our own hosted copy** (when the licence allows redistribution) | only huge archives (one compressed file per country) or e-mail exports | small tiles on our terms, one mechanism for DTM and DSM | download, convert and host once; keep it up to date; attribution | a tile layer on a fixed grid (planned for Slovakia) |
| Rendered WMS images, hillshades, viewer tiles | never for elevation | – | pictures, not heights | – |

Rules of thumb:
- Ask for the native resolution and downsample locally (`fetch_res`); never trust server-side downsampling without checking the share of valid pixels at 5 m.
- Clip requests to the coverage extent; a `missing` tile at the edge of the data is suspicious (Malta's north coast of Gozo was lost this way).
- Compressed archives (ZIP Deflate/Deflate64) cannot be read in parts; a TIFF inside may still be tiled, so after one extraction it reads fast. Python's `zipfile` cannot read Deflate64: use 7-Zip.
- Huge ZIPs: read the central directory with range requests first (the end of the file, then the ZIP64 record): the compression method of each member (8 = Deflate: Python's `zipfile` and GDAL `/vsizip/` read it; 9 = Deflate64: neither does, extract with 7-Zip) and the sizes inside. Extract a huge member once instead of reading it through `/vsizip/`: for Slovakia's 122 GB TIFF GDAL managed about 7 MB/s, `zipfile` inflates 400 MB/s (13 min), and the plain file reads in any order.
- Before downloading whole files, open one with rasterio through `/vsicurl/` and look at `block_shapes` and the `LAYOUT=COG` tag: a tiled file needs no download, only windows (`TnmBlocks`, `DegreeBlocks`). Old 3DEP files with the IFD at the end still read fine.
- Versions that cover parts of tiles: compose newest first, read an older file only while holes remain, and put a key of the set of files into the cached file's name, so a new survey over a block makes it read again and the older file can be deleted. "Newest file per tile" (Poland, Switzerland) is right only when a survey replaces whole tiles.
- Work in the CRS of the files when they are metric (`Source.crs`, or `Source.local_crs` for a CRS per zone such as NAD83 / UTM): blocks on the files' own pixels are copied onto the grid as they are; anything else is resampled and smooths cliff edges.
- Compare CRSs by their parameters, not by name: a file may name only the ellipsoid of the expected datum (`sources._same_crs`); check the first real file, not only the documentation.
- Fill the gaps from data with the same licence where there is any (the USA: the USGS 1/3″ DEM, not FABDEM, keeps a run public domain). A source may fill from several, in turn (`Source.more_fallbacks`).
- Names, descriptions and notes of sources are printed to the console: keep them in characters of cp1250 (a Windows console), e.g. "1/3 arc-second", not ″.
- When more than one product would be needed (a gap filler, a second region, a seamless model), present the options with a recommendation and let the user decide before writing it.
- For own hosting: COG tiles of 1 km in the source grid, LERC_ZSTD with `MAX_Z_ERROR=0.005` (5 mm) was the smallest and fastest (Slovak DSM 0.5 m: 5.5 MB per km², 0.2 s per tile); a JSON index with checksums; attribution and licence file next to the data.

## 3. Quick tests before writing code
Scripts from the 2026-10-25 research lived in the session scratchpad; the commands are short enough to redo:
- **STAC**: `GET <api>/collections/<id>/items?bbox=W,S,E,N&limit=100` and the `HEAD` of one asset (size, checksum, server).
- **WCS**: `GetCapabilities`, `DescribeCoverage` (axis labels, extent, CRS), then a 500 × 500 m `GetCoverage` at native resolution: check content type, pixel size and origin, value precision (fraction of values on whole centimetres), nodata, and time per request; try 2 km and larger to find the size limit; request one box crossing the coverage edge to see the 404.
- **Archives**: read a ZIP's central directory with HTTP range requests (the last 64 kB, then the directory) to list members, compression methods and sizes, and fetch small members (licence, info, `.tfw`) without downloading the archive.
- **Index APIs** (USGS TNM Access: `/api/v1/products?datasets=<name>&bbox=W,S,E,N&max=…&offset=…`): list the products at a few known places (climbing areas), note how many files a tile has and their sizes; for coverage, sample a lattice of points over the country (a point counts where a gap filler exists, so sea and neighbours fall out) and compare with the program's status map.
- **Window reads**: open a remote file with `rasterio.open("/vsicurl/<url>")` under `GDAL_DISABLE_READDIR_ON_OPEN=EMPTY_DIR`: CRS, transform (pixel edges on whole units?), block shapes, compression, nodata, and the time to read a 1 km window.
- **Orthophoto**: one `GetMap` in EPSG:4326 (WMS 1.3.0 axis order lat, lon) and, for WMTS, one tile in EPSG:3857; look at the image (watermarks, blank tiles).
- Then the data checks of docs/regression-tests.md: shift against Copernicus, valid pixels at 5 m, a cached tile against the source file, the grid alignment.

## 4. After adding a source
- A regression test in a characteristic place (cliffs, coast, border), with the coverage and `empty_tiles` values stored; a pytest against a fake server for new download code (see `tests/test_sources.py`).
- Attribution in the map (`OrthoService.attribution`, the source's `note`) and in README's "Data" section; the licence in the roadmap's licence list.
- Update policy (cache.py `TTL_DAYS`): versioned releases never, "live" services after their interval, sheet indexes after 180 days.
- `cache status` and `cache update <folder>` on a scratch data folder with the index rows aged by hand (`checked_at` in `cache/<top>/index.sqlite`, with a time zone, e.g. `2024-01-01T00:00:00+00:00`).
- New download code that uses GDAL's network access: read one window inside the Docker image too (HTTPS certificates of the slim image), and a job through the web app's queue.
- The whole regression set: the other countries' checksums must not change.
