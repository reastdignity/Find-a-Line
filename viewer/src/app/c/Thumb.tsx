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

/** The shape in the thumbnail's frame: an equirectangular projection fitted into W × H. */
function draw(shape: Shape) {
  const points: Position[] = shape.rings.flat();
  shape.lines.forEach((l) => points.push(l.a, l.b));
  if (!points.length) return null;
  let [w, s, e, n] = [Infinity, Infinity, -Infinity, -Infinity];
  for (const [x, y] of points) { w = Math.min(w, x); e = Math.max(e, x); s = Math.min(s, y); n = Math.max(n, y); }
  const k = Math.cos((s + n) / 2 * Math.PI / 180);
  const width = Math.max((e - w) * k, 1e-9), height = Math.max(n - s, 1e-9);
  const scale = Math.min((W - 2 * MARGIN) / width, (H - 2 * MARGIN) / height);
  const x0 = (W - width * scale) / 2, y0 = (H - height * scale) / 2;
  const xy = ([x, y]: Position) => `${(x0 + (x - w) * k * scale).toFixed(1)} ${(y0 + (n - y) * scale).toFixed(1)}`;
  const area = shape.rings.map((r) => "M" + r.map(xy).join("L") + "Z").join("");
  const path = (over: boolean) => shape.lines.filter((l) => l.over === over).map((l) => `M${xy(l.a)}L${xy(l.b)}`).join("");
  return {area, clean: path(false), over: path(true)};
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
    {drawn ? <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="xMidYMid meet">
      {drawn.area && <path class="c-thumb-area" d={drawn.area} />}
      {drawn.clean && <path class="c-thumb-line" d={drawn.clean} stroke={COLORS.clean} />}
      {drawn.over && <path class="c-thumb-line" d={drawn.over} stroke={COLORS.over} />}
    </svg> : <span class="c-thumb-empty"><Icon name={source.kind === "job" ? "clock" : "layers"} /></span>}
    {shape?.label && <span class="c-thumb-label">{shape.label}</span>}
  </div>;
}
