// Sag estimator: the same algorithm as sag.py (keep both in sync; tests/test_viewer.py
// compares them to 1e-6 m). Elastic catenary segments, the athlete as a point load.

import type { Rigging } from "./types";

const G = 9.80665, POINTS = 40;

interface Constants { H0: number; EA: number; w: number; P: number }
type Vec = number[];

function constants(r: Rigging): Constants {
  return {H0: r.pretension * 1e3, EA: 1e4 / (r.stretch / 100),
          w: Math.max(r.webbing_weight / 1e3 * G, 1e-6), P: r.athlete_mass * G};
}

/** (dx, dy, V at the end) of a segment of unstretched length s with tension H and V at its start. */
function segment(H: number, V: number, s: number, w: number, EA: number): [number, number, number] {
  const Ve = V - w * s;
  return [H * s / EA + H / w * (Math.asinh(V / H) - Math.asinh(Ve / H)),
          (V * s - 0.5 * w * s * s) / EA + (Math.hypot(H, V) - Math.hypot(H, Ve)) / w, Ve];
}

function norm(r: Vec): number { return Math.sqrt(r.reduce((a, v) => a + v * v, 0)); }
function finite(r: Vec): boolean { return r.every((v) => isFinite(v)); }

function solveLinear(A: number[][], b: Vec): Vec | null {  // Gaussian elimination with partial pivoting
  const n = b.length, M = A.map((row, i) => row.concat([b[i]]));
  for (let c = 0; c < n; c++) {
    let p = c;
    for (let r = c + 1; r < n; r++) if (Math.abs(M[r][c]) > Math.abs(M[p][c])) p = r;
    if (Math.abs(M[p][c]) < 1e-300) return null;
    const tmp = M[c]; M[c] = M[p]; M[p] = tmp;
    for (let r = c + 1; r < n; r++) {
      const k = M[r][c] / M[c][c];
      for (let j = c; j <= n; j++) M[r][j] -= k * M[c][j];
    }
  }
  const x = new Array<number>(n);
  for (let i = n - 1; i >= 0; i--) {
    let s = M[i][n];
    for (let j = i + 1; j < n; j++) s -= M[i][j] * x[j];
    x[i] = s / M[i][i];
  }
  return x;
}

function newton(f: (x: Vec) => Vec, x0: Vec, scale: Vec): Vec | null {  // damped, forward-difference Jacobian
  let x = x0.slice(), r = f(x);
  if (!finite(r)) return null;
  for (let it = 0; it < 60; it++) {
    const n = norm(r);
    if (n < 1e-9) return x;
    const J: number[][] = r.map(() => []);
    for (let j = 0; j < x.length; j++) {
      const h = 1e-7 * Math.max(Math.abs(x[j]), scale[j]), xh = x.slice();
      xh[j] += h;
      const rh = f(xh);
      for (let i = 0; i < r.length; i++) J[i][j] = (rh[i] - r[i]) / h;
    }
    const step = solveLinear(J, r.map((v) => -v));
    if (!step) return null;
    let lam = 1, xn: Vec = x, rn: Vec = r;
    while (lam > 1e-4) {
      xn = x.map((v, k) => v + lam * step[k]);
      rn = f(xn);
      if (finite(rn) && norm(rn) < n) break;
      lam /= 2;
    }
    if (lam <= 1e-4) return n < 1e-6 ? x : null;
    x = xn; r = rn;
  }
  return norm(r) < 1e-6 ? x : null;
}

function unstretched(span: number, dh: number, c: Constants): {L0: number; V: number} | null {
  const chord = Math.hypot(span, dh);
  const v = newton((v) => {
    const e = segment(c.H0, v[0], v[1], c.w, c.EA);
    return [e[0] - span, e[1] - dh];
  }, [0.5 * c.w * chord + c.H0 * dh / span, chord / (1 + c.H0 / c.EA)], [c.H0, span]);
  return v && {L0: v[1], V: v[0]};
}

interface Loaded { H: number; sag: number; solution: Vec }

function loaded(span: number, dh: number, c: Constants, x: number, L0: number, guess?: Vec | null): Loaded | null {
  const residualFor = (P: number) => (v: Vec) => {
    const a = segment(v[0], v[1], v[2], c.w, c.EA), b = segment(v[0], a[2] - P, L0 - v[2], c.w, c.EA);
    return [a[0] + b[0] - span, a[1] + b[1] - dh, a[0] - x];
  };
  const scale = [c.H0, c.H0, span];
  let g: Vec | null;
  if (!guess) {
    const u = unstretched(span, dh, c);
    if (!u) return null;
    g = [c.H0, u.V, L0 * x / span];
    for (let k = 1; k <= 8 && g; k++) g = newton(residualFor(c.P * k / 8), g, scale);
  } else g = newton(residualFor(c.P), guess, scale);
  if (!g) return null;
  const y1 = segment(g[0], g[1], g[2], c.w, c.EA)[1];
  return {H: g[0], sag: y1 - dh / span * x, solution: g};
}

function loadedCurve(c: Constants, s: Loaded, L0: number, n: number): [number, number][] {
  const [H, V, u] = s.solution;
  const k1 = Math.max(1, Math.round(n * u / L0)), pts: [number, number][] = [];
  for (let k = 0; k <= k1; k++) {
    const p = segment(H, V, u * k / k1, c.w, c.EA);
    pts.push([p[0], p[1]]);
  }
  const end = segment(H, V, u, c.w, c.EA), k2 = Math.max(1, n - k1);
  for (let k = 1; k <= k2; k++) {
    const p = segment(H, end[2] - c.P, (L0 - u) * k / k2, c.w, c.EA);
    pts.push([end[0] + p[0], end[1] + p[1]]);
  }
  return pts;
}

export interface Athlete { sag: number; H: number; curve: [number, number][] }

export interface Estimate {
  L0: number;
  unloaded: [number, number][];   // (x, drop below anchor 1) of the webbing without the athlete
  f: number[];                    // fractions of the span where the envelope is computed
  sag: number[];                  // sag below the chord with the athlete at f
  H: number[];                    // horizontal tension, N
  sagAt(t: number): number;       // envelope at fraction t (linear interpolation)
  athlete(x: number): Athlete | null;  // loaded webbing with the athlete at x
}

/** Everything the profile needs for one line: the unloaded webbing and the envelope. */
export function estimate(span: number, dh: number, rigging: Rigging): Estimate | null {
  const c = constants(rigging), u = unstretched(span, dh, c);
  if (!u) return null;
  const unloadedPts: [number, number][] = [];
  for (let k = 0; k <= 50; k++) {
    const p = segment(c.H0, u.V, u.L0 * k / 50, c.w, c.EA);
    unloadedPts.push([p[0], p[1]]);
  }
  const n = POINTS + 2, f: number[] = [], sag: number[] = [], H: number[] = [];
  const solutions: (Vec | null)[] = [], mid = Math.floor(n / 2);
  for (let k = 0; k < n; k++) { f.push(k / (n - 1)); sag.push(0); H.push(c.H0); solutions.push(null); }
  const first = loaded(span, dh, c, f[mid] * span, u.L0);
  if (!first) return null;
  sag[mid] = first.sag; H[mid] = first.H; solutions[mid] = first.solution;
  for (let dir = -1; dir <= 1; dir += 2) {
    let guess = first.solution;
    for (let k = mid + dir; dir < 0 ? k > 0 : k < n - 1; k += dir) {
      const r = loaded(span, dh, c, f[k] * span, u.L0, guess) || loaded(span, dh, c, f[k] * span, u.L0);
      if (!r) return null;
      sag[k] = r.sag; H[k] = r.H; solutions[k] = r.solution; guess = r.solution;
    }
  }
  return {
    L0: u.L0, unloaded: unloadedPts, f, sag, H,
    sagAt(t) {
      const i = Math.min(n - 2, Math.floor(t * (n - 1))), a = t * (n - 1) - i;
      return sag[i] * (1 - a) + sag[i + 1] * a;
    },
    athlete(x) {
      const t = x / span, i = Math.max(1, Math.min(n - 2, Math.round(t * (n - 1))));
      const s = loaded(span, dh, c, x, u.L0, solutions[i]) || loaded(span, dh, c, x, u.L0);
      return s && {sag: s.sag, H: s.H, curve: loadedCurve(c, s, u.L0, 60)};
    },
  };
}
