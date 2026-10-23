"""
Highline sag estimator.

The webbing is an elastic catenary (own weight, axial stiffness from the stretch
at 10 kN). Its unstretched length follows from the pretension; the athlete is a
point load at a given horizontal position. This follows the approach of the
Highline Sag Calculator (sagcalc.krw.sk); the solver is our own (Newton's method
with a numerical Jacobian). The same algorithm runs in the interactive map
(SAG_SCRIPT in mapping.py), so keep both in sync.

Conventions: x is horizontal distance from anchor 1, y is the drop below anchor 1
(positive down), dh is how much lower anchor 2 is than anchor 1. Forces in N,
lengths in m.
"""

import math
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

import numpy as np

from progress import progress

G = 9.80665
ENVELOPE_POINTS = 40  # athlete positions along the line (interior points)


@dataclass
class Rigging:
    pretension: float = 2.5      # kN, horizontal tension without the athlete
    stretch: float = 5.0         # % at 10 kN
    webbing_weight: float = 70   # g/m
    athlete_mass: float = 80     # kg
    fall_zone: float = 3.0       # m below the sagged webbing that must stay free

    @property
    def H0(self) -> float:
        return self.pretension * 1e3

    @property
    def EA(self) -> float:
        return 1e4 / (self.stretch / 100)

    @property
    def w(self) -> float:
        return max(self.webbing_weight / 1e3 * G, 1e-6)

    @property
    def P(self) -> float:
        return self.athlete_mass * G


def segment(H: float, V: float, s: float, w: float, EA: float) -> Tuple[float, float, float]:
    """End of an elastic catenary segment of unstretched length s.

    H is the horizontal tension, V the upward force at the start of the segment.
    Returns (x, drop, V at the end).
    """
    Ve = V - w * s
    x = H * s / EA + H / w * (math.asinh(V / H) - math.asinh(Ve / H))
    y = (V * s - 0.5 * w * s * s) / EA + (math.hypot(H, V) - math.hypot(H, Ve)) / w
    return x, y, Ve


def _newton(f, x0: Sequence[float], scale: Sequence[float], iterations: int = 60,
            tol: float = 1e-9) -> Optional[List[float]]:
    """Damped Newton's method with a forward-difference Jacobian."""
    x = np.array(x0, dtype=float)
    scale = np.asarray(scale, dtype=float)
    try:
        r = np.asarray(f(x))
    except (ValueError, OverflowError, ZeroDivisionError):
        return None
    for _ in range(iterations):
        norm = np.linalg.norm(r)
        if norm < tol:
            return x.tolist()
        J = np.empty((len(r), len(x)))
        for j in range(len(x)):
            h = 1e-7 * max(abs(x[j]), scale[j])
            xh = x.copy()
            xh[j] += h
            J[:, j] = (np.asarray(f(xh)) - r) / h
        try:
            step = np.linalg.solve(J, -r)
        except np.linalg.LinAlgError:
            return None
        lam = 1.0
        while lam > 1e-4:  # backtracking: accept the first step that reduces the residual
            xn = x + lam * step
            try:
                rn = np.asarray(f(xn))
                if np.all(np.isfinite(rn)) and np.linalg.norm(rn) < norm:
                    break
            except (ValueError, OverflowError, ZeroDivisionError):
                pass
            lam /= 2
        else:
            return x.tolist() if norm < 1e-6 else None
        x, r = xn, rn
    return x.tolist() if np.linalg.norm(r) < 1e-6 else None


def unstretched(span: float, dh: float, rig: Rigging) -> Tuple[float, float]:
    """Unstretched length of the webbing and the upward force at anchor 1 at pretension."""
    H, w, EA = rig.H0, rig.w, rig.EA
    chord = math.hypot(span, dh)
    guess = [0.5 * w * chord + H * dh / span, chord / (1 + H / EA)]

    def residual(v):
        x, y, _ = segment(H, v[0], v[1], w, EA)
        return [x - span, y - dh]

    V, L0 = _newton(residual, guess, [H, span])
    return L0, V


def unloaded_curve(span: float, dh: float, rig: Rigging, n: int = 50) -> List[Tuple[float, float]]:
    """Webbing without the athlete: [(x, drop below anchor 1)]."""
    L0, V = unstretched(span, dh, rig)
    return [segment(rig.H0, V, L0 * k / n, rig.w, rig.EA)[:2] for k in range(n + 1)]


def loaded(span: float, dh: float, rig: Rigging, x_athlete: float,
           L0: float, guess: Optional[List[float]] = None) -> Optional[dict]:
    """Webbing with the athlete at horizontal position x_athlete.

    Unknowns: horizontal tension H, upward force at anchor 1 V and the unstretched
    length u from anchor 1 to the athlete. Without a guess the load is applied in
    steps (continuation), which keeps Newton's method in its convergence region.
    """
    w, EA = rig.w, rig.EA

    def residual_for(P):
        def residual(v):
            H, V, u = v
            x1, y1, Ve = segment(H, V, u, w, EA)
            x2, y2, _ = segment(H, Ve - P, L0 - u, w, EA)
            return [x1 + x2 - span, y1 + y2 - dh, x1 - x_athlete]
        return residual

    scale = [rig.H0, rig.H0, span]
    if guess is None:
        _, V0 = unstretched(span, dh, rig)
        guess = [rig.H0, V0, L0 * x_athlete / span]
        for k in range(1, 9):
            guess = _newton(residual_for(rig.P * k / 8), guess, scale)
            if guess is None:
                return None
    else:
        guess = _newton(residual_for(rig.P), guess, scale)
        if guess is None:
            return None
    H, V, u = guess
    _, y1, _ = segment(H, V, u, w, EA)
    return {"H": H, "V": V, "u": u, "sag": y1 - dh / span * x_athlete, "drop": y1, "solution": guess}


def loaded_curve(span: float, dh: float, rig: Rigging, solution: dict, L0: float,
                 n: int = 50) -> List[Tuple[float, float]]:
    """Shape of the loaded webbing: [(x, drop below anchor 1)]."""
    H, V, u = solution["solution"]
    w, EA = rig.w, rig.EA
    k1 = max(1, round(n * u / L0))
    points = [segment(H, V, u * k / k1, w, EA)[:2] for k in range(k1 + 1)]
    x1, y1, Ve = segment(H, V, u, w, EA)
    k2 = max(1, n - k1)
    for k in range(1, k2 + 1):
        x, y, _ = segment(H, Ve - rig.P, (L0 - u) * k / k2, w, EA)
        points.append((x1 + x, y1 + y))
    return points


def envelope(span: float, dh: float, rig: Rigging,
             points: int = ENVELOPE_POINTS) -> Tuple[np.ndarray, np.ndarray]:
    """Sag below the chord under the athlete, for athlete positions along the line.

    Returns (fractions of the span 0..1, sag in m); zero at the anchors.
    Positions are solved from the middle outwards, each from its neighbour.
    """
    L0, _ = unstretched(span, dh, rig)
    f = np.linspace(0, 1, points + 2)
    sag = np.zeros_like(f)
    mid = len(f) // 2
    first = loaded(span, dh, rig, f[mid] * span, L0)
    if first is None:
        raise ValueError(f"sag estimator did not converge for span {span:.0f} m")
    sag[mid] = first["sag"]
    for direction in (-1, 1):
        guess = first["solution"]
        for k in range(mid + direction, 0 if direction < 0 else len(f) - 1, direction):
            result = loaded(span, dh, rig, f[k] * span, L0, guess) or loaded(span, dh, rig, f[k] * span, L0)
            if result is None:
                raise ValueError(f"sag estimator did not converge for span {span:.0f} m")
            sag[k] = result["sag"]
            guess = result["solution"]
    return f, sag


class EnvelopeTable:
    """Precomputed sag envelopes for the line search (--model sag).

    Envelopes depend on the span and on the height difference; they are computed
    for spans on a geometric grid and for a few ratios dh/span, and interpolated.
    """
    RATIOS = np.array([-1.0, -0.6, -0.3, -0.15, 0.0, 0.15, 0.3, 0.6, 1.0])
    SPAN_STEP = 1.05

    def __init__(self, rig: Rigging, min_span: float, max_span: float):
        self.rig = rig
        n = max(2, math.ceil(math.log(max_span / min_span) / math.log(self.SPAN_STEP)) + 1)
        self.spans = min_span * self.SPAN_STEP ** np.arange(n)
        self.fractions = np.linspace(0, 1, ENVELOPE_POINTS + 2)
        self.sag = np.empty((len(self.spans), len(self.RATIOS), len(self.fractions)))
        progress.compute.start("sag estimator table", len(self.spans), unit="length")
        for i, span in enumerate(self.spans):
            for j, ratio in enumerate(self.RATIOS):
                self.sag[i, j] = envelope(span, ratio * span, rig)[1]
            progress.compute.update(1)
        progress.compute.finish()

    def __call__(self, span: float, dh: float, t: np.ndarray) -> np.ndarray:
        """Sag below the chord at fractions t of a line with this span and height difference."""
        si = np.interp(math.log(span), np.log(self.spans), np.arange(len(self.spans)))
        ri = np.interp(dh / span, self.RATIOS, np.arange(len(self.RATIOS)))
        i0, j0 = min(int(si), len(self.spans) - 2), min(int(ri), len(self.RATIOS) - 2)
        a, b = si - i0, ri - j0
        table = ((1 - a) * (1 - b) * self.sag[i0, j0] + a * (1 - b) * self.sag[i0 + 1, j0]
                 + (1 - a) * b * self.sag[i0, j0 + 1] + a * b * self.sag[i0 + 1, j0 + 1])
        return np.interp(t, self.fractions, table)
