# Find a Line
This is an application that finds topographically valid highlines in Poland. It downloads elevation data from [geoportal.gov.pl](https://www.geoportal.gov.pl) (GUGiK) and evaluates the potential of each raster pixel by applying a custom "maximum elevation formula".

The whole of Poland has been scanned with airborne LiDAR and is published by GUGiK as a free terrain model (NMT, 1 metre grid) and a surface model including trees and buildings (NMPT, 0.5 metre grid). This is much more detailed than global datasets (NASADEM is 30 metres), which makes it possible to find short lines as well as long ones.

**Limitations and future scope**
- Data is only available for Poland. Areas outside the country are treated as "no data" and lines never cross them.
- Trees and buildings are taken into account through NMPT (on by default, `--nmpt off` to skip it). Other factors that make rigging difficult are not.
- It would be nice to overlay with property maps.
- The math behind line dynamics is very nuanced, somewhat complex, and generally not available; we are using a simple formula.
- Because our formula always takes into account the elevation of the lowest anchor, the lines may be off-level, or required to be rigged shorter than shown on the map.
- Please be aware that our formula **does not** take into account the additional height required for a safe mainline failure.

## Maximum Elevation Formula
Due to deflection, the further an athlete travels from the nearest anchor the lower elevation required. The maximum safe elevation for a hypothetical 0 metre highline is 6 metres lower than the anchors- this is to account for a leash fall. From there we require 15cm more clearance for every metre longer the line is. This slope was calculated based on 155kg athlete walking a median stretch webbing tensioned at 2kN.

The formula for safe elevation at any given point on a highline can be described as

<img width="387" height="25" alt="e\index{c}⩽ min{e\index{a1},e\index{a2}}-0.15(min{d\index{a1},d\index{a2}})-6" src="https://github.com/user-attachments/assets/934ce642-292b-414e-8606-2816852c1835" />

- a<sub>1</sub> = anchor 1
- a<sub>2</sub> = anchor 2
- c = current point
- d = distance from the current point to an anchor (metres)
- e = elevation (metres)

This can also be visualised as a slope below the line that is always exactly 6 metres below the line at maximum deflection.
<img width="1361" height="224" alt="visualisation" src="https://github.com/user-attachments/assets/5147f16f-768c-4a63-89b6-105c0c6ea8bd" />

## Algorithm Breakdown
- Elevation data for a circle around the given point is downloaded from the GUGiK WCS service in tiles and resampled to the working resolution (`--resolution`). Tiles are cached, so a second run over the same area does not download anything.
- Slope is computed from the terrain model (or read from your own slope raster, `--slope-file`). Pixels steeper than `--slope-threshold` degrees become promising anchors.
- For every promising anchor, we find every other anchor whose distance matches our length criteria (using a KD-tree, so pairs that are too far apart are never considered).
- For each such pair, we "draw" a line using a DDA (Digital Differential Analyzer) algorithm. This allows us to intercept every raster pixel between the two anchors.
- For each raster pixel that exists along this line, the maximum acceptable elevation is calculated using our formula, and the pixel's actual elevation is subtracted from this value to retrieve the elevation difference (clearance). A **positive** clearance implies that the pixel's elevation is **lower** than our required maximum.
- If every pixel has a positive clearance, the line is added to a list of valid lines.
- The same check is repeated against the surface model (treetops, roofs), unless `--nmpt off`. In `mark` mode lines that fail it are kept but drawn in orange; in `reject` mode they are dropped.
- The list of valid lines is then converted into GPS coordinates, printed, and displayed on a map.

## Getting Started
New to the terminal? Follow the [step-by-step setup guide](setup-for-regular-folk.md) for macOS and Windows.

Install the requirements (`pip install -r requirements.txt`) and run the script with the centre of the area you want to search:

```
python findaline.py --lat 49.2320 --lon 19.9817
```

No data files are needed; everything is downloaded automatically into the `cache` folder.

The search centre can be given in either of two coordinate systems:
- **WGS84** (`--lat`, `--lon`), as shown in Google Maps or a GPS. Example: `--lat 49.2320 --lon 19.9817`
- **PL-1992 / EPSG:2180** (`--northing`, `--easting`), in metres. Note that geoportal.gov.pl labels the northing as **X** and the easting as **Y**. Example: `--northing 152074 --easting 571448`

### Options
| Option | Default | Description |
| --- | --- | --- |
| `--radius` | 1000 | Search radius around the centre, in metres |
| `--resolution` | 2 | Working resolution in metres (1 is the native NMT resolution) |
| `--min-length` / `--max-length` | 30 / 1000 | Line length constraints, in metres |
| `--slope-threshold` | 45 | Minimum terrain slope (degrees) for an anchor |
| `--slope-file` | | Your own slope raster in degrees (GeoTIFF or ASC) instead of computing it from NMT |
| `--nmpt` | `mark` | `mark` or `reject` lines that pass too close to trees or buildings; `off` skips the check and the NMPT download |
| `--processes` | CPU count - 1 | Number of parallel processes |
| `--cache-dir` | `cache` | Folder for downloaded data |
| `--refresh` | | Download data again even if it is cached |
| `--group-distance` | 20 | Group similar lines: both anchors within this many metres |
| `--group-by` | `clearance` | Line shown for each group: `clearance` (largest) or `longest` |
| `--all-lines` | | List every line instead of one line per group |
| `--html` | `findaline_map.html` | Interactive map file |
| `--no-html` / `--no-plot` | | Skip the interactive map / the matplotlib window |
| `-y`, `--yes` | | Do not ask for confirmation before long runs |

Run `python findaline.py --help` for the full list.

### Results
A steep rock face produces many near-identical lines (anchors shifted by a metre or two). Such lines are grouped into one spot: two lines belong to the same group when both of their anchors are within `--group-distance` metres of each other. Each group is shown as one line, the one with the largest clearance (or the longest one, `--group-by longest`). Lines over trees/buildings are grouped separately from clear lines.

- The coordinates are printed in the terminal (WGS84, latitude and longitude), one line per group with the number of similar lines. Use `--all-lines` to list every line.
- An interactive map (`findaline_map.html`) can be opened in any web browser. You can switch between OpenTopoMap, OpenStreetMap and the geoportal orthophoto, and click a line to see its length, anchor elevations and clearance. The panel in the bottom-left corner turns grouping on and off, sets the grouping distance and the line shown for each group. Clicking a group shows all of its lines.
- A static map is shown in a separate window (close it to finish the program), with one line per group.

### Run time and data size
With the defaults (1 km radius at 2 m, NMPT on) the first run downloads about 100 MB and takes a minute or two, mostly downloading NMPT, which is much slower than NMT. Larger areas grow quickly: a 20 km radius at 2 m is about 1.5 GB for NMT alone, and in steep terrain potentially billions of anchor pairs. Before downloading and before evaluating, the script prints an estimate and asks for confirmation if the run looks long. If it is too slow, compromise by doing at least one of the following:
- Use a smaller `--radius`
- Use a coarser `--resolution` (5 or 10 metres is good for a first look at a large area)
- Raise `--slope-threshold`
- Change your minimum or maximum line lengths

### Example Run
This is how the map looks after valid lines have been found (2500-5000m lines in southern Washington, from an earlier version of this tool that used NASADEM data).
<img width="1440" height="900" alt="south wa big lines" src="https://github.com/user-attachments/assets/37fa09be-8726-465f-bb57-f1a76d3332a6" />


And here is an example of one such location found using this tool.
<img width="1404" height="356" alt="Screenshot 2025-11-03 at 4 14 58 PM" src="https://github.com/user-attachments/assets/e44f01cf-f3a1-4a7d-b629-f00bec73779e" />

## Data
- Terrain: NMT, WCS service `DigitalTerrainModelFormatTIFF`, height system PL-KRON86-NH.
- Trees and buildings: NMPT, WCS service `DigitalSurfaceModel`, same height system.
- Both are downsampled on the server by averaging, so at coarse resolutions small objects such as single trees appear lower than they are.
- Source: Główny Urząd Geodezji i Kartografii, [geoportal.gov.pl](https://www.geoportal.gov.pl). The data is free to use.

## Learn More
- [I'm a non-technical user, how do I run this?](setup-for-regular-folk.md)
- [ISA calculators](https://www.slackline.app)
- [BC Tension calculator](https://www.balancecommunity.com/pages/tension-calculator)
- [Geoportal elevation data (NMT/NMPT)](https://www.geoportal.gov.pl/pl/dane/numeryczny-model-terenu-nmt/)
