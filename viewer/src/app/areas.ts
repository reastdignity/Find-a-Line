// How many separate areas a search covers: shapes that overlap or touch are one area.
// Polygons count as they are; points and lines of an uploaded file are grown by the
// buffer, as the search grows them (area.from_spec), so two of them closer than twice
// the buffer join. Distances are in metres on a local plane (enough at these sizes).

import type { FeatureCollection, Geometry, Position } from "geojson";

type XY = [number, number];
interface Part {
  rings: XY[][];    // polygon: outer ring and holes; line: one open ring; point: one point
  polygon: boolean;
  box: [number, number, number, number];
}

function parts(geometry: Geometry | null, plane: (p: Position) => XY): Part[] {
  if (!geometry) return [];
  const part = (rings: XY[][], polygon: boolean): Part => {
    const all = rings.flat();
    return {rings, polygon, box: [Math.min(...all.map((p) => p[0])), Math.min(...all.map((p) => p[1])),
                                  Math.max(...all.map((p) => p[0])), Math.max(...all.map((p) => p[1]))]};
  };
  switch (geometry.type) {
    case "Polygon": return [part(geometry.coordinates.map((r) => r.map(plane)), true)];
    case "MultiPolygon": return geometry.coordinates.map((p) => part(p.map((r) => r.map(plane)), true));
    case "LineString": return [part([geometry.coordinates.map(plane)], false)];
    case "MultiLineString": return geometry.coordinates.map((l) => part([l.map(plane)], false));
    case "Point": return [part([[plane(geometry.coordinates)]], false)];
    case "MultiPoint": return geometry.coordinates.map((p) => part([[plane(p)]], false));
    case "GeometryCollection": return geometry.geometries.flatMap((g) => parts(g, plane));
  }
}

/** Inside a polygon with holes (even-odd over all its rings). */
function inside(p: XY, rings: XY[][]): boolean {
  let odd = false;
  for (const ring of rings) {
    for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
      const [xi, yi] = ring[i], [xj, yj] = ring[j];
      if ((yi > p[1]) !== (yj > p[1]) && p[0] < (xj - xi) * (p[1] - yi) / (yj - yi) + xi) odd = !odd;
    }
  }
  return odd;
}

function pointSegment(p: XY, a: XY, b: XY): number {
  const dx = b[0] - a[0], dy = b[1] - a[1], len2 = dx * dx + dy * dy;
  const t = len2 ? Math.max(0, Math.min(1, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / len2)) : 0;
  return Math.hypot(p[0] - a[0] - t * dx, p[1] - a[1] - t * dy);
}

function cross(a: XY, b: XY, c: XY): number {
  return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0]);
}

function segmentSegment(a: XY, b: XY, c: XY, d: XY): number {
  const d1 = cross(a, b, c), d2 = cross(a, b, d), d3 = cross(c, d, a), d4 = cross(c, d, b);
  if (((d1 > 0 && d2 < 0) || (d1 < 0 && d2 > 0)) && ((d3 > 0 && d4 < 0) || (d3 < 0 && d4 > 0))) return 0;
  return Math.min(pointSegment(a, c, d), pointSegment(b, c, d), pointSegment(c, a, b), pointSegment(d, a, b));
}

function segments(part: Part): [XY, XY][] {
  const out: [XY, XY][] = [];
  for (const ring of part.rings) {
    if (ring.length === 1) out.push([ring[0], ring[0]]);
    for (let i = 1; i < ring.length; i++) out.push([ring[i - 1], ring[i]]);
  }
  return out;
}

/** The distance between two parts: 0 when they overlap or one holds the other. */
function distance(a: Part, b: Part, limit: number): number {
  const gap = Math.max(a.box[0] - b.box[2], b.box[0] - a.box[2], a.box[1] - b.box[3], b.box[1] - a.box[3]);
  if (gap > limit) return gap;
  if (b.polygon && inside(a.rings[0][0], b.rings)) return 0;
  if (a.polygon && inside(b.rings[0][0], a.rings)) return 0;
  let best = Infinity;
  const sb = segments(b);
  for (const [p, q] of segments(a)) {
    for (const [r, s] of sb) {
      best = Math.min(best, segmentSegment(p, q, r, s));
      if (best <= limit) return best;
    }
  }
  return best;
}

/** The number of separate areas of these shapes (buffer: metres around points and lines). */
export function countAreas(collection: FeatureCollection | null, buffer: number): number {
  if (!collection || !collection.features.length) return 0;
  const lat0 = collection.features.flatMap((f) => parts(f.geometry, (p) => [p[0], p[1]]))
    .map((p) => (p.box[1] + p.box[3]) / 2)[0] ?? 0;
  const kx = 111320 * Math.cos(lat0 * Math.PI / 180), ky = 110540;
  const all = collection.features.flatMap((f) => parts(f.geometry, (p) => [p[0] * kx, p[1] * ky]));
  const root = all.map((_, i) => i);
  const find = (i: number): number => root[i] === i ? i : (root[i] = find(root[i]));
  for (let i = 0; i < all.length; i++) {
    for (let j = i + 1; j < all.length; j++) {
      if (find(i) === find(j)) continue;
      const grow = (all[i].polygon ? 0 : buffer) + (all[j].polygon ? 0 : buffer);
      if (distance(all[i], all[j], grow) <= grow) root[find(i)] = find(j);
    }
  }
  return new Set(all.map((_, i) => find(i))).size;
}
