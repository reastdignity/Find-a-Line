# Finding and adding a data source

How to research a country's elevation data and decide how Find-a-Line gets it. Learned with Poland, Malta, Spain (2026-10-23/25), and with Switzerland, Slovakia and Greece (2026-10-25, see the roadmap, "New data sources"). The mechanics of adding a `Source` are in README.md, "Adding a country"; the data checks after it are in docs/regression-tests.md, "Data checks for a new source".

## 1. What to find out
For the terrain model (DTM, ground only) and the surface model (DSM, trees and buildings) separately:

| Question | Why it matters | Bad answers seen |
| --- | --- | --- |
| Resolution and coverage; survey years; is a newer cycle under way? | 1–2 m is the useful range; a partial newer cycle means versions per area | Slovakia: 0.5 m 2nd cycle in 16 of 73 lots |
| Licence and who may get it | must allow automatic download (and, for a public site, redistribution) | Greece: public bodies only |
| CRS and **vertical datum** | heights shown to users; gap filling against Copernicus | Slovakia WCS: ellipsoidal heights, 43 m above sea level |
| Value precision (cm, mm, whole metres) | whole metres make slopes and clearances coarse | Malta 2018 and Spain's WCS GeoTIFF: whole metres |
| Pixel alignment (edges or centres on whole metres) | `grid_origin`; half a pixel off smooths cliff edges | Poland's sheets: centres on whole metres (`grid_origin` 0.5) |
| Access methods (below), with the source's own recommendations, fair use, rate limits | how to download without being blocked | swisstopo: "use the download service", no bots |
| Does a DSM exist and is it reachable the same way? | without it, lines over trees are not marked | Slovakia: DSM only in 155 GB ZIPs |
| Orthophoto WMS/WMTS: CRS (EPSG:4326 or 3857), terms, watermarks | the map background, offline tiles | Greece: watermark; EPSG:900913 gives blank images |

Where to look: the national mapping agency's geoportal and its "download" or "open data" pages; the INSPIRE geoportal of the country (CSW search for "Elevation"); data.europa.eu with a country filter; the JRC survey of open LiDAR in Europe (publications.jrc.ec.europa.eu, JRC126223); OpenTopography's catalogue for small LiDAR sets. Read the terms page itself, and the licence files inside downloads (Slovakia's ZIPs carry a PDF).

## 2. Ways to get the data, best first

| Way | Use when | Pros | Cons | In the code |
| --- | --- | --- | --- | --- |
| **Files per sheet/tile, found through an index** (WFS sheet index, STAC API) | the source publishes ready files | fast static downloads (Poland 17 MB/s, Switzerland 40 MB/s), full precision, versions visible (year in the name), what sources recommend for offline use | whole sheets even for a small area; versions to choose and prune; an index to query (cache its answers) | `SheetTiles` (Poland); a STAC variant for Switzerland |
| **Files on a fixed grid** (URL from the tile name) | tiles on a known grid, one version | simplest, no index | no version information | `DegreeTiles` (Copernicus, FABDEM) |
| **WCS 2.0** | no files, or files only as huge archives | any box, small requests | the server generates every tile (slow: Poland 30–50 s per 2 km); server-side downsampling may lose data or average oddly; size limits (~2000 px, Slovakia fails at 20 km); 404 beyond the coverage (clip to DescribeCoverage); GeoTIFF output may be 8-bit or whole metres (prefer ASCII grid or float GeoTIFF; ask for DEFLATE) | `WcsTiles` with `fetch_res` = native resolution |
| **Our own hosted copy** (when the licence allows redistribution) | only huge archives (one compressed file per country) or e-mail exports | small tiles on our terms, one mechanism for DTM and DSM | download, convert and host once; keep it up to date; attribution | a tile layer on a fixed grid (planned for Slovakia) |
| Rendered WMS images, hillshades, viewer tiles | never for elevation | – | pictures, not heights | – |

Rules of thumb:
- Ask for the native resolution and downsample locally (`fetch_res`); never trust server-side downsampling without checking the share of valid pixels at 5 m.
- Clip requests to the coverage extent; a `missing` tile at the edge of the data is suspicious (Malta's north coast of Gozo was lost this way).
- Compressed archives (ZIP Deflate/Deflate64) cannot be read in parts; a TIFF inside may still be tiled, so after one extraction it reads fast. Python's `zipfile` cannot read Deflate64: use 7-Zip.
- For own hosting: COG tiles of 1 km in the source grid, LERC_ZSTD with `MAX_Z_ERROR=0.005` (5 mm) was the smallest and fastest (Slovak DSM 0.5 m: 5.5 MB per km², 0.2 s per tile); a JSON index with checksums; attribution and licence file next to the data.

## 3. Quick tests before writing code
Scripts from the 2026-10-25 research lived in the session scratchpad; the commands are short enough to redo:
- **STAC**: `GET <api>/collections/<id>/items?bbox=W,S,E,N&limit=100` and the `HEAD` of one asset (size, checksum, server).
- **WCS**: `GetCapabilities`, `DescribeCoverage` (axis labels, extent, CRS), then a 500 × 500 m `GetCoverage` at native resolution: check content type, pixel size and origin, value precision (fraction of values on whole centimetres), nodata, and time per request; try 2 km and larger to find the size limit; request one box crossing the coverage edge to see the 404.
- **Archives**: read a ZIP's central directory with HTTP range requests (the last 64 kB, then the directory) to list members, compression methods and sizes, and fetch small members (licence, info, `.tfw`) without downloading the archive.
- **Orthophoto**: one `GetMap` in EPSG:4326 (WMS 1.3.0 axis order lat, lon) and, for WMTS, one tile in EPSG:3857; look at the image (watermarks, blank tiles).
- Then the data checks of docs/regression-tests.md: shift against Copernicus, valid pixels at 5 m, a cached tile against the source file, the grid alignment.

## 4. After adding a source
- A regression test in a characteristic place (cliffs, coast, border), with the coverage and `empty_tiles` values stored; a pytest against a fake server for new download code (see `tests/test_sources.py`).
- Attribution in the map (`OrthoService.attribution`, the source's `note`) and in README's "Data" section; the licence in the roadmap's licence list.
- Update policy (cache.py `TTL_DAYS`): versioned releases never, "live" services after their interval, sheet indexes after 180 days.
