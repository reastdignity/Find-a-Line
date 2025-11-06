# Find a Line
This is an application that finds locations with significant highline potential based on topographic validity. It works by extracting DEM data from .tiff files and evalutating the potential of each raster pixel by applying a custom "maximum elevation formula". 

Most of the landmass on Earth has been measured by NASA radar systems at a granularity of 30 metres (see *Learn More* below). There are some areas that have a higher topographic granularity available, but 30 metres is plenty good enough for finding lines 100m and longer, smaller lines next to these lines, and general areas that have a high potential.

**Limitations and future scope**
- This does not take into account vegetation and other factors that may make rigging difficult, though this would be possible by analyzing DSM data
- It would be nice to overlay with property maps
- The math behind line dynamics is very nuanced, somewhat complex, and generally not available; we are using a simple formula here
- Please be aware that our formula **does not** take into account the additional height required for a safe mainline failure.

## Maximum Elevation Formula
Due to deflection, the further an athlete travels from the nearest anchor the lower elevation required. The maximum safe elevation for a hypothetical 0 metre highline is 6 metres lower than the anchors- this is to account for a leash fall. From there we require 15cm more clearance for every metre longer the line is. This slope was calculated based on 155kg athlete walking a median stretch webbing tensioned to 2kN.

The safe elevation at any given point on a highline can be described as below.

<img width="387" height="25" alt="e\index{c}⩽ min{e\index{a1},e\index{a2}}-0.15(min{d\index{a1},d\index{a2}})-6" src="https://github.com/user-attachments/assets/934ce642-292b-414e-8606-2816852c1835" />

- a<sub>1</sub> = anchor 1
- a<sub>2</sub> = anchor 2
- c = current point
- d = distance from the current point to an anchor
- e = elevation

This can also be visualised as a slope below the line that is always exactly 6 metres below the line at maximum deflection.
<img width="1361" height="224" alt="visualisation" src="https://github.com/user-attachments/assets/5147f16f-768c-4a63-89b6-105c0c6ea8bd" />

## Algorithm Breakdown
- We first extract raster pixels of significant slope to use as a starting point for promising anchors.
- For every promising anchor, we calculate the distance to every other promising anchor. If the distance matches our criteria, it's added to a list of promising lines.
- For each promising line, we "draw" a line using a DDA (Digital Differential Analyzer) algorithm. This allows us to intercept every raster pixel between the two anchors.
- For each raster pixel that exists along this line, the maximum acceptable elevation is calculated using our formula, and the pixel's actual elevation is subtracted from this value to retrieve the elevation difference. A **positive** elevation difference implies that the pixel's elevation is **lower** than our required maximum.
- If every pixel that has a positive elevation difference, the line is added to a list of valid lines
- The list of valid lines is then converted into coordinates and diplayed on a map

## Example Run
This is how the map looks after valid lines have been found (2500-5000m lines in southern Washington).
<img width="1440" height="900" alt="south wa big lines" src="https://github.com/user-attachments/assets/37fa09be-8726-465f-bb57-f1a76d3332a6" />


And here is an example of one such gap found using this tool.
<img width="1404" height="356" alt="Screenshot 2025-11-03 at 4 14 58 PM" src="https://github.com/user-attachments/assets/e44f01cf-f3a1-4a7d-b629-f00bec73779e" />

## Learn More
- [Tension calculator](https://www.balancecommunity.com/pages/tension-calculator)
- [A good resource for finding DEM data for this project](https://portal.opentopography.org/raster?opentopoID=OTSDEM.032021.4326.2)
