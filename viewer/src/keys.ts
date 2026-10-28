// Keys of lines: their anchors as data.js and lines.js hold them (mapping.save_map rounds both
// alike), so the same line of a run has the same key on every map and after every reload.

import type { Line } from "./types";

type Anchors = Pick<Line, "lat1" | "lon1" | "lat2" | "lon2">;

/** "lat1,lon1,lat2,lon2": a line within its run (the profile requests and links to a line use it too). */
export const lineCoords = (l: Anchors) => [l.lat1, l.lon1, l.lat2, l.lon2].join(",");

/** A line among the lines of several runs: "<run id>|lat1,lon1,lat2,lon2". */
export const runLineKey = (runId: string, coords: string) => runId + "|" + coords;
export const lineKey = (l: Line, currentRun: string) => runLineKey(l.run ? l.run.info.id : currentRun, lineCoords(l));
