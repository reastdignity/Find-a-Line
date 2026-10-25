// Terrain profile of the clicked line, in a panel along the bottom of the map:
// terrain, trees/buildings, water, the safety limit of the search, the sag
// estimator (webbing, envelope, fall zone, athlete under the cursor).

import { useEffect, useMemo, useRef, useState } from "preact/hooks";
import * as Sag from "./sag";
import { COLORS } from "./style";
import type { Line, Rigging } from "./types";

export const PROFILE = {terrain: "#8a7656", terrainFill: "#e3d8c3", trees: "#3f8f35",
                        treesFill: "rgba(76,153,60,0.45)", limit: "#d62728", envelope: "#6a3d9a",
                        fallFill: "rgba(214,39,40,0.12)", athlete: "#222", water: "#2f6fc0",
                        waterFill: "rgba(47,111,192,0.45)"};
const RIGGING: [keyof Rigging, string, number][] = [
  ["pretension", "kN pretension", 0.1], ["stretch", "% stretch @ 10 kN", 0.5],
  ["webbing_weight", "g/m webbing", 5], ["athlete_mass", "kg athlete", 5], ["fall_zone", "m fall zone", 0.5]];
type LayerName = "webbing" | "athlete" | "envelope" | "fall";
const LAYERS: [LayerName, string, string][] = [
  ["webbing", "webbing", "height:3px;background:" + COLORS.clean],
  ["athlete", "athlete under cursor", "height:2px;background:" + PROFILE.athlete],
  ["envelope", "sag envelope", "border-top:2px dotted " + PROFILE.envelope + ";height:0"],
  ["fall", "fall zone", "height:8px;background:" + PROFILE.fallFill + ";border:1px solid " + PROFILE.limit]];
const HEIGHT = 170, M = {l: 58, r: 20, t: 18, b: 24};

function Swatch({style}: {style: string}) {
  return <span class="fal-swatch" style={style}></span>;
}

function niceStep(span: number, count: number): number {
  const raw = span / count, mag = Math.pow(10, Math.floor(Math.log10(raw)));
  for (const step of [1, 2, 5, 10]) if (step * mag >= raw) return step * mag;
  return 10 * mag;
}

export interface ProfileProps {
  line: Line;
  model: "formula" | "sag";
  slope: number;
  leashFall: number;
  rigging: Rigging;                      // initial values (the run's)
  onClose(): void;
  onHover(at: [number, number] | null): void;   // (lat, lon) under the cursor
}

export function ProfilePanel(props: ProfileProps) {
  const {line: l, model} = props;
  const p = l.prof!;
  const [rigging, setRigging] = useState<Rigging>({...props.rigging});
  const [show, setShow] = useState<Record<LayerName, boolean>>(
    {webbing: true, athlete: true, envelope: true, fall: true});
  const [equal, setEqual] = useState(false);
  const [width, setWidth] = useState(800);
  const [hover, setHover] = useState<number | null>(null);  // distance from A1 under the cursor
  const chartRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const measure = () => setWidth(chartRef.current?.clientWidth || 800);
    measure();
    window.addEventListener("resize", measure);
    return () => window.removeEventListener("resize", measure);
  }, []);
  useEffect(() => setHover(null), [l]);

  const est = useMemo(() => Sag.estimate(l.len, l.e1 - l.e2, rigging), [l, rigging]);
  const W = width, pw = W - M.l - M.r, ph = HEIGHT - M.t - M.b;
  const color = l.over ? COLORS.over : COLORS.clean;
  const chord = (d: number) => l.e1 + (l.e2 - l.e1) * d / l.len;
  const fall = rigging.fall_zone;
  const envelopeAt = (d: number) => chord(d) - est!.sagAt(d / l.len);
  const limit = (d: number) => {
    if (model !== "sag") return Math.min(l.e1, l.e2) - props.slope * Math.min(d, l.len - d) - props.leashFall;
    return est ? envelopeAt(d) - fall : NaN;
  };

  // Vertical range: everything that is drawn
  const dsamples: number[] = [];
  for (let q = 0; q <= 40; q++) dsamples.push(l.len * q / 40);
  let values: (number | null)[] = p.z.concat(p.s || [], [l.e1, l.e2]);
  dsamples.forEach((d) => {
    values.push(limit(d));
    if (est) values.push(envelopeAt(d) - (show.fall ? fall : 0));
  });
  const finiteValues = values.filter((v): v is number => v !== null && isFinite(v));
  let zmin = Math.min(...finiteValues), zmax = Math.max(...finiteValues);
  const pad = Math.max((zmax - zmin) * 0.08, 1);
  zmin -= pad; zmax += pad;
  let sx = pw / l.len, sy = ph / (zmax - zmin), xoff = 0;
  if (equal) {  // same metres per pixel on both axes
    sx = sy = Math.min(sx, sy);
    xoff = (pw - l.len * sx) / 2;
  }
  const zmid = (zmin + zmax) / 2;
  const X = (d: number) => M.l + xoff + d * sx;
  const Y = (z: number) => M.t + ph / 2 - (z - zmid) * sy;
  const visibleMin = zmid - ph / 2 / sy, visibleMax = zmid + ph / 2 / sy;
  const pt = (d: number, z: number) => X(d).toFixed(1) + "," + Y(z).toFixed(1);
  const base = (d: number) => X(d).toFixed(1) + "," + (HEIGHT - M.b);

  const grid = [];
  const yStep = niceStep(visibleMax - visibleMin, 4);
  for (let z = Math.ceil(visibleMin / yStep) * yStep; z <= visibleMax; z += yStep) {
    grid.push(<line x1={M.l} x2={W - M.r} y1={Y(z)} y2={Y(z)} stroke="#eee" />);
    grid.push(<text x={M.l - 6} y={Y(z) + 4} text-anchor="end" fill="#777">{z.toFixed(0) + " m"}</text>);
  }
  const xStep = niceStep(l.len, 6);
  for (let d = 0; d <= l.len + 0.01; d += xStep) {
    grid.push(<text x={X(d)} y={HEIGHT - 6} text-anchor="middle" fill="#777">{d.toFixed(0) + " m"}</text>);
  }

  const ground: string[] = [], top: string[] = [], bottom: string[] = [];
  for (let i = 0; i < p.d.length; i++) if (p.z[i] !== null) ground.push(pt(p.d[i], p.z[i]!));
  if (p.s) {
    for (let i = 0; i < p.d.length; i++) {
      if (p.s[i] === null || p.z[i] === null) continue;
      top.push(pt(p.d[i], Math.max(p.s[i]!, p.z[i]!)));
      bottom.unshift(pt(p.d[i], p.z[i]!));
    }
  }
  // Water (sea and lakes): each run of water points, from the surface down
  const water: string[] = [];
  if (p.w) {
    let run: number[] = [];
    const flush = () => {
      if (run.length > 1) {
        water.push(run.map((j) => pt(p.d[j], p.z[j]!)).concat(
          [base(p.d[run[run.length - 1]]), base(p.d[run[0]])]).join(" "));
      }
      run = [];
    };
    for (let i = 0; i < p.d.length; i++) {
      if (p.w[i] && p.z[i] !== null) run.push(i); else flush();
    }
    flush();
  }

  // Smallest clearance over terrain and over trees/buildings (from the search)
  const minMarker = (min: [number, number], stroke: string, label: string, below: boolean) => {
    const topZ = limit(min[0]), bottomZ = topZ - min[1];
    if (!isFinite(topZ)) return null;
    const right = min[0] > l.len / 2;  // label on the inner side, so it is not cut off
    return <g>
      <line x1={X(min[0])} x2={X(min[0])} y1={Y(topZ)} y2={Y(bottomZ)} stroke={stroke} stroke-width={2} />
      <text x={X(min[0]) + (right ? -4 : 4)} y={below ? Y(bottomZ) + 12 : Y(topZ) - 4} fill={stroke}
            text-anchor={right ? "end" : "start"}>{label + " " + min[1].toFixed(1) + " m"}</text>
    </g>;
  };

  let summary = "Sag estimator did not converge for these values.";
  if (est) {
    const k = est.sag.indexOf(Math.max(...est.sag));
    summary = "Sag estimator: max sag " + est.sag[k].toFixed(2) + " m below the chord with the athlete at " +
      (est.f[k] * l.len).toFixed(0) + " m (line tension " + (est.H[k] / 1e3).toFixed(1) +
      " kN); without the athlete " + Math.max(...est.unloaded.map((e) =>
        e[1] - (l.e1 - l.e2) * e[0] / l.len)).toFixed(2) + " m.";
  }

  // Under the cursor: readout, the loaded webbing, the point on the map
  let readout = summary, athlete: Sag.Athlete | null = null;
  if (hover !== null) {
    const d = hover;
    let lo = 0, hi = p.d.length - 1;  // nearest terrain sample
    while (hi - lo > 1) { const mid = (lo + hi) >> 1; if (p.d[mid] < d) lo = mid; else hi = mid; }
    const k = (d - p.d[lo] < p.d[hi] - d) ? lo : hi;
    const z = p.z[k], s = p.s ? p.s[k] : null;
    readout = d.toFixed(0) + " m from A1";
    if (z !== null) readout += " · terrain " + z.toFixed(1) + " m";
    if (p.w && p.w[k]) readout += " (water)";
    if (p.g && p.g[k]) readout += " (filled gap)";
    if (s !== null && s !== undefined) readout += " · trees/buildings " + s.toFixed(1) + " m";
    const lim = limit(d);
    if (isFinite(lim)) {
      readout += " · safety limit " + lim.toFixed(1) + " m";
      if (z !== null) readout += " · clearance " + (lim - z).toFixed(1) + " m";
      if (s !== null && s !== undefined && z !== null)
        readout += " (over trees/buildings " + (lim - Math.max(s, z)).toFixed(1) + " m)";
    }
    athlete = est && show.athlete && d > 0.01 * l.len && d < 0.99 * l.len ? est.athlete(d) : null;
    if (athlete) {
      readout += " · athlete here: sag " + athlete.sag.toFixed(2) + " m, tension " +
        (athlete.H / 1e3).toFixed(1) + " kN";
      if (z !== null) readout += ", " + (chord(d) - athlete.sag - z).toFixed(1) + " m above terrain";
    }
  }

  const onMove = (e: MouseEvent) => {
    const box = (e.currentTarget as SVGElement).ownerSVGElement!.getBoundingClientRect();
    const d = Math.max(0, Math.min(l.len, (e.clientX - box.left - M.l - xoff) / sx));
    setHover(d);
    const t = d / l.len;
    props.onHover([l.lat1 + (l.lat2 - l.lat1) * t, l.lon1 + (l.lon2 - l.lon1) * t]);
  };
  const onLeave = () => { setHover(null); props.onHover(null); };

  const setRig = (key: keyof Rigging, input: HTMLInputElement) => {
    const v = parseFloat(input.value);
    if (isFinite(v) && (v > 0 || (key !== "pretension" && key !== "stretch" && v >= 0))) {
      setRigging({...rigging, [key]: v});
    } else input.value = String(rigging[key]);
  };
  const dh = Math.abs(l.e1 - l.e2);

  return <div class="fal-profile" onClick={(e) => e.stopPropagation()}>
    <div class="fal-profile-head">
      <div>
        <b class="fal-profile-title">{"Profile: " + l.len.toFixed(0) + " m, A1 " + l.e1.toFixed(1) + " m → A2 " +
          l.e2.toFixed(1) + " m (Δh " + dh.toFixed(1) + " m), min. clearance " + l.clr.toFixed(1) + " m"}</b>
        <span class="fal-profile-legend">
          <span><Swatch style={"height:8px;background:" + PROFILE.terrainFill + ";border:1px solid " + PROFILE.terrain} /> terrain</span>
          <span><Swatch style={"height:8px;background:" + PROFILE.treesFill} /> trees/buildings</span>
          <span><Swatch style={"height:8px;background:" + PROFILE.waterFill} /> water</span>
          <span><Swatch style={"border-top:2px dashed " + PROFILE.limit + ";height:0"} /> safety limit
            ({model === "sag" ? "sag model" : "formula"})</span>
        </span>
      </div>
      <div>
        <button class="fal-scale" onClick={() => setEqual(!equal)}>
          {equal ? "Auto scale" : "1:1 scale (now vertical ×" + (sy / sx).toFixed(1) + ")"}</button>{" "}
        <button class="fal-close" title="Close" onClick={props.onClose}>×</button>
      </div>
    </div>
    <div class="fal-profile-controls">
      <span class="fal-rigging">{RIGGING.map(([key, label, step]) =>
        <label><input type="number" min={0} step={step} value={rigging[key]}
                      onChange={(e) => setRig(key, e.currentTarget)} /> {label}</label>)}</span>
      <span class="fal-layers">{LAYERS.map(([key, label, style]) =>
        <label><input type="checkbox" checked={show[key]}
                      onChange={(e) => setShow({...show, [key]: e.currentTarget.checked})} />{" "}
          <Swatch style={style} /> {label}</label>)}</span>
    </div>
    <div class="fal-profile-chart" ref={chartRef}>
      <svg width={W} height={HEIGHT} font-size="11">
        {grid}
        {est && show.fall && <polygon fill={PROFILE.fallFill} stroke="none" points={
          dsamples.map((d) => pt(d, envelopeAt(d))).concat(
            dsamples.map((d) => pt(d, envelopeAt(d) - fall)).reverse()).join(" ")} />}
        {p.s && <polygon points={top.concat(bottom).join(" ")} fill={PROFILE.treesFill} stroke="none" />}
        <polygon points={ground.concat([base(l.len), base(0)]).join(" ")} fill={PROFILE.terrainFill}
                 stroke={PROFILE.terrain} stroke-width={1} />
        {water.map((points) =>
          <polygon points={points} fill={PROFILE.waterFill} stroke={PROFILE.water} stroke-width={1} />)}
        <polyline points={dsamples.map((d) => pt(d, limit(d))).join(" ")} fill="none" stroke={PROFILE.limit}
                  stroke-width={1.5} stroke-dasharray="6 4" />
        {est && show.envelope && <polyline points={dsamples.map((d) => pt(d, envelopeAt(d))).join(" ")}
                                           fill="none" stroke={PROFILE.envelope} stroke-width={1.5}
                                           stroke-dasharray="2 3" />}
        {show.webbing && <polyline fill="none" stroke={color} stroke-width={2.5} points={
          (est ? est.unloaded.map((e) => pt(e[0], l.e1 - e[1])) : [pt(0, l.e1), pt(l.len, l.e2)]).join(" ")} />}
        {athlete && <polyline class="fal-loaded" fill="none" stroke={PROFILE.athlete} stroke-width={1.5}
                              points={athlete.curve.map((e) => pt(e[0], l.e1 - e[1])).join(" ")} />}
        {athlete && hover !== null && <circle class="fal-athlete" r={4} fill={PROFILE.athlete} cx={X(hover)}
                                              cy={Y(chord(hover) - athlete.sag)} />}
        {minMarker(p.min, PROFILE.terrain, "min. clearance", true)}
        {p.min_nmpt && p.min_nmpt[1] < p.min[1] - 0.05 &&
          minMarker(p.min_nmpt, PROFILE.trees, "over trees", false)}
        {([[0, l.e1, "A1", "start"], [l.len, l.e2, "A2", "end"]] as [number, number, string, string][]).map(
          ([d, z, name, anchor]) => <g>
            <circle cx={X(d)} cy={Y(z)} r={4} fill={color} />
            <text x={X(d) + (anchor === "start" ? 6 : -6)} y={Y(z) - 7} text-anchor={anchor}
                  font-weight="bold">{name + " " + z.toFixed(1) + " m"}</text>
          </g>)}
        {hover !== null && <line class="fal-cursor" x1={X(hover)} x2={X(hover)} y1={M.t} y2={HEIGHT - M.b}
                                 stroke="#333" stroke-dasharray="2 2" />}
        <rect class="fal-hover" x={M.l} y={M.t} width={pw} height={ph} fill="transparent"
              onMouseMove={onMove} onMouseLeave={onLeave} />
      </svg>
    </div>
    <div class="fal-profile-readout">{readout}</div>
  </div>;
}
