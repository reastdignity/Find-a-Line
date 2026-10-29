// The mini maps of C's cards: an SVG (no Leaflet) with the run's area, or its box, and its lines in
// the map's colours (style.ts). Loaded when the card comes into view, a few at a time; a run with
// more than MAX_LINES lines shows its box and the count only (its lines.js would be large). A search
// still running shows the area of its request (a circle or the drawn areas) when it has one.

import type { Geometry, GeoJSON, Position } from "geojson";
import { useEffect, useRef, useState } from "preact/hooks";
import { Icon } from "../../ui/icons";
import { COLORS } from "../../style";
import { loadLines } from "../../others";
import { loadRunList } from "../../result/load";
import { api } from "../api";

export const MAX_LINES = 3000;  // more: the outline and "N lines", without loading lines.js
const AT_ONCE = 3;              // thumbnails loaded at the same time
const W = 320, H = 180, MARGIN = 10;

export type ThumbSource =
  | {kind: "run"; id: string; lines: number}
  | {kind: "job"; id: string}
  | {kind: "none"};

interface Shape {
  rings: Position[][];  // outlines, [lon, lat]
  lines: {a: Position; b: Position; over: boolean}[];
  mode: "lines" | "outline" | "placeholder";
  label: string;
}

// ---- loading, a few at a time ----------------------------------------------------------------

let running = 0;
const waiting: (() => void)[] = [];

function queued<T>(task: () => Promise<T>): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const begin = () => {
      running++;
      task().then(resolve, reject).finally(() => {
        running--;
        waiting.shift()?.();
      });
    };
    if (running < AT_ONCE) begin(); else waiting.push(begin);
  });
}

const shapes = new Map<string, Promise<Shape>>();

/** The rings of a GeoJSON object's polygons. */
function ringsOf(g: GeoJSON | Geometry | null | undefined, out: Position[][] = []): Position[][] {
  if (!g) return out;
  switch (g.type) {
    case "Polygon": out.push(...g.coordinates); break;
    case "MultiPolygon": g.coordinates.forEach((p) => out.push(...p)); break;
    case "GeometryCollection": g.geometries.forEach((x) => ringsOf(x, out)); break;
    case "Feature": ringsOf(g.geometry, out); break;
    case "FeatureCollection": g.features.forEach((f) => ringsOf(f.geometry, out)); break;
  }
  return out;
}

/** A circle of `radius` metres as a ring. */
function circle(lat: number, lon: number, radius: number): Position[] {
  const dLat = radius / 111_320, dLon = dLat / Math.max(0.01, Math.cos(lat * Math.PI / 180));
  return Array.from({length: 49}, (_, k) => {
    const a = k / 48 * 2 * Math.PI;
    return [lon + dLon * Math.cos(a), lat + dLat * Math.sin(a)];
  });
}

const box = (b: number[]): Position[] => [[b[0], b[1]], [b[2], b[1]], [b[2], b[3]], [b[0], b[3]], [b[0], b[1]]];
const count = (n: number) => n.toLocaleString("en-GB") + (n === 1 ? " line" : " lines");

async function runShape(id: string, lines: number): Promise<Shape> {
  if (lines > MAX_LINES) {
    const listed = (await loadRunList()).find((r) => r.id === id);
    return {rings: listed?.bbox ? [box(listed.bbox)] : [], lines: [], mode: listed?.bbox ? "outline" : "placeholder",
            label: count(lines)};
  }
  const loaded = await loadLines(id, "/runs/");
  return {rings: ringsOf(loaded.area), lines: loaded.lines.map((d) => ({a: [d[1], d[0]], b: [d[3], d[2]], over: d[13]})),
          mode: "lines", label: loaded.lines.length ? "" : "no lines"};
}

async function jobShape(id: string): Promise<Shape> {
  const {request} = await api.job(id);
  const p = request?.params || {};
  const rings = ringsOf(request?.geometry);
  if (typeof p.lat === "number" && typeof p.lon === "number") {
    rings.push(circle(p.lat, p.lon, Number(p.radius ?? 1000)));
  }
  return {rings, lines: [], mode: rings.length ? "outline" : "placeholder", label: ""};
}

function shapeOf(source: ThumbSource): Promise<Shape> | null {
  if (source.kind === "none") return null;
  const key = `${source.kind}:${source.id}:${source.kind === "run" ? source.lines : ""}`;
  let found = shapes.get(key);
  if (!found) {
    found = queued(() => source.kind === "run" ? runShape(source.id, source.lines) : jobShape(source.id));
    shapes.set(key, found);
    found.catch(() => shapes.delete(key));  // tried again next time
  }
  return found;
}

// ---- drawing ---------------------------------------------------------------------------------

const TILE = 256, MAX_ZOOM = 17;  // OpenTopoMap's tiles, the run maps' first base layer (layers.ts)
export const THUMB_CREDIT = "Mini maps: © OpenStreetMap contributors, SRTM | map style © OpenTopoMap (CC-BY-SA)";

/** Web Mercator pixels of a point at zoom 0 (one 256 px tile for the world). */
function mercator([lon, lat]: Position): [number, number] {
  const s = Math.sin(Math.max(-85.0511, Math.min(85.0511, lat)) * Math.PI / 180);
  return [(lon + 180) / 360 * TILE, (0.5 - Math.log((1 + s) / (1 - s)) / (4 * Math.PI)) * TILE];
}

/** The shape in the thumbnail's frame (Web Mercator fitted into W × H, as the map's tiles), with the
 *  OpenTopoMap tiles under it: the largest whole zoom at which it fits, the tiles scaled to fill. */
function draw(shape: Shape) {
  const points: Position[] = shape.rings.flat();
  shape.lines.forEach((l) => points.push(l.a, l.b));
  if (!points.length) return null;
  let [x0, y0, x1, y1] = [Infinity, Infinity, -Infinity, -Infinity];
  for (const p of points) {
    const [x, y] = mercator(p);
    x0 = Math.min(x0, x); x1 = Math.max(x1, x); y0 = Math.min(y0, y); y1 = Math.max(y1, y);
  }
  const fit = Math.min((W - 2 * MARGIN) / Math.max(x1 - x0, 1e-9), (H - 2 * MARGIN) / Math.max(y1 - y0, 1e-9));
  const zoom = Math.max(0, Math.min(MAX_ZOOM, Math.floor(Math.log2(fit))));
  const scale = fit / 2 ** zoom > 2 ? 2 ** (zoom + 1) : fit;  // past the tiles' zoom: at most twice their size
  const cx = (x0 + x1) / 2 * scale, cy = (y0 + y1) / 2 * scale;  // the centre in pixels of `scale`
  const px = (p: Position): [number, number] => { const [x, y] = mercator(p); return [x * scale - cx + W / 2, y * scale - cy + H / 2]; };
  const xy = (p: Position) => px(p).map((v) => v.toFixed(1)).join(" ");
  const area = shape.rings.map((r) => "M" + r.map(xy).join("L") + "Z").join("");
  const path = (over: boolean) => shape.lines.filter((l) => l.over === over).map((l) => `M${xy(l.a)}L${xy(l.b)}`).join("");
  const size = TILE * scale / 2 ** zoom, n = 2 ** zoom;  // a tile's size in the frame
  const tiles: {href: string; x: number; y: number; size: number}[] = [];
  for (let ty = Math.floor((cy - H / 2) / size); ty <= Math.floor((cy + H / 2) / size); ty++) {
    if (ty < 0 || ty >= n) continue;
    for (let tx = Math.floor((cx - W / 2) / size); tx <= Math.floor((cx + W / 2) / size); tx++) {
      const wrapped = ((tx % n) + n) % n;
      tiles.push({href: `https://${"abc"[(wrapped + ty) % 3]}.tile.opentopomap.org/${zoom}/${wrapped}/${ty}.png`,
                  x: tx * size - cx + W / 2, y: ty * size - cy + H / 2, size});
    }
  }
  return {area, clean: path(false), over: path(true), tiles};
}

/** Whether an element has come near the viewport (once). */
function useSeen(ref: {current: Element | null}): boolean {
  const [seen, setSeen] = useState(false);
  useEffect(() => {
    if (seen || !ref.current) return;
    if (typeof IntersectionObserver !== "function") { setSeen(true); return; }
    const observer = new IntersectionObserver((entries) => {
      if (entries.some((e) => e.isIntersecting)) { setSeen(true); observer.disconnect(); }
    }, {rootMargin: "200px"});
    observer.observe(ref.current);
    // A page that draws no frames (a headless browser) gets no observations: look once by hand
    const timer = setTimeout(() => {
      const r = ref.current?.getBoundingClientRect();
      if (r && r.bottom > -200 && r.top < innerHeight + 200 && r.width > 0) setSeen(true);
    }, 1000);
    return () => { observer.disconnect(); clearTimeout(timer); };
  }, [seen]);
  return seen;
}

/** A card's mini map. */
export function Thumb({source, class: cls = ""}: {source: ThumbSource; class?: string}) {
  const ref = useRef<HTMLDivElement>(null);
  const seen = useSeen(ref);
  const [shape, setShape] = useState<Shape | null>(null);
  const [failed, setFailed] = useState(false);
  const key = source.kind === "none" ? "none" : `${source.kind}:${source.id}:${source.kind === "run" ? source.lines : ""}`;
  useEffect(() => {
    setShape(null);
    setFailed(false);
    if (!seen) return;
    let live = true;
    shapeOf(source)?.then((s) => { if (live) setShape(s); }, () => { if (live) setFailed(true); });
    return () => { live = false; };
  }, [seen, key]);
  const drawn = shape && draw(shape);
  const mode = drawn ? shape!.mode : "placeholder";
  return <div ref={ref} class={`c-thumb ${cls}`} data-thumb={shape ? mode : failed ? "failed" : "loading"}
              data-lines={shape ? shape.lines.length : undefined} aria-hidden="true">
    {drawn ? <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="xMidYMid slice">
      <g class="c-thumb-tiles">{drawn.tiles.map((t) => <image key={t.href} href={t.href} x={t.x} y={t.y}
                                                              width={t.size + 0.5} height={t.size + 0.5} />)}</g>
      {drawn.area && <path class="c-thumb-area" d={drawn.area} />}
      {drawn.clean && <path class="c-thumb-line" d={drawn.clean} stroke={COLORS.clean} />}
      {drawn.over && <path class="c-thumb-line" d={drawn.over} stroke={COLORS.over} />}
    </svg> : <span class="c-thumb-empty"><Icon name={source.kind === "job" ? "clock" : "layers"} /></span>}
    {shape?.label && <span class="c-thumb-label">{shape.label}</span>}
  </div>;
}
