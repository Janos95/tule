"""The thrust chamber's contours (mm; z = 0 at the nozzle exit plane, up the engine axis).

MEASURED (outer): from evidence/img/012 (left panel, 1.688 mm/px, axis x = 481, exit plane y = 1935),
the mean of the left and right silhouette where neither side is hidden (raptor/KNOWN.md §4).
INFERRED (gas side): a Rao-style bell for a 0.22 m throat and an area ratio of 34.3 (KNOWN.md §2),
a 0.34 m chamber and L* of about 0.9 m (UNKNOWN.md §8). Nothing on the gas side is published.
"""
import math

import numpy as np

# ---------------------------------------------------------------- measured outside (012, mean of both sides)
BELL_OUTER = [  # (z, r) of the bell's outer skin, from the lip up to just under the collar
    (0, 650), (59, 645), (127, 635), (194, 625), (262, 615), (329, 600), (397, 584), (464, 564),
    (532, 545), (599, 525), (667, 504), (734, 483), (802, 459), (869, 433), (937, 405), (1004, 377),
    (1072, 353), (1089, 348),
]
EXIT_R = 650.0          # KNOWN §2: exit Ø 1.30 m (the scale reference)
HEIGHT = 2950.0         # KNOWN §4: top of the LOX inlet cap, H/De = 2.27
THROAT_Z = 1555.0       # KNOWN §4: narrowest outer section 1.55 m above the exit
THROAT_OUTER_R = 149   # narrowest outer section of the neck (012, 013, 014; ROUND-1 ENV-2)
CHAMBER_OUTER_R = 223.0  # KNOWN §4: chamber band Ø 0.45 m, z 1680–1790

# ---------------------------------------------------------------- inferred gas side
RT = 110.0              # throat radius: Ø 0.22 m (ε = 34.3 with a 644 mm inner exit radius)
RC = 170.0              # chamber radius: Ø 0.34 m (contraction ratio 2.4)
FACE_Z = 1985.0         # injector face: L* ≈ 0.9 m with the chamber above
CONV_DEG = 30.0         # convergent half angle
THETA_N, THETA_E = 28.0, 8.0   # bell entrance and exit angles: the largest pair that keeps the gas side inside 012's outer bell (6–18 mm wall)
EXIT_IN_R = 644.0       # 6 mm lip wall


def _bezier(p0, p1, p2, n):
    t = np.linspace(0, 1, n)[:, None]
    return (1 - t) ** 2 * p0 + 2 * (1 - t) * t * p1 + t ** 2 * p2


def gas_side(n_bell: int = 40) -> np.ndarray:
    """(z, r) of the gas-side wall from the injector face to the exit, z decreasing (face first)."""
    pts = []
    # chamber cylinder, then a 1.5 RC-ish round into the 30° convergent, then the 1.5 RT upstream throat arc
    a = math.radians(CONV_DEG)
    r_up = 1.5 * RT
    # upstream throat arc: centre at (THROAT_Z + 0, RT + r_up)... in (axial s, r) with s = THROAT_Z - z
    # arc from angle -(90+30)° to -90° around centre (s=0, r=RT+r_up)
    arc_up = [(-r_up * math.sin(t), RT + r_up - r_up * math.cos(t)) for t in np.linspace(a, 0, 8)]
    s_conv_end, r_conv_end = arc_up[0]
    # straight convergent up to the chamber radius, entered by a round of radius R2
    R2 = 0.8 * RC
    r_round_lo = RC - R2 * (1 - math.cos(a))
    s_round_lo = s_conv_end - (r_round_lo - r_conv_end) / math.tan(a)
    round_ = [(s_round_lo - R2 * math.sin(a) + R2 * math.sin(t), RC - R2 + R2 * math.cos(t)) for t in np.linspace(0, a, 6)]
    s_face = THROAT_Z - FACE_Z
    pts += [(s_face, RC)] + round_ + arc_up
    # downstream throat arc (0.382 RT) to THETA_N, then a quadratic bezier to the exit
    r_dn = 0.382 * RT
    tn, te = math.radians(THETA_N), math.radians(THETA_E)
    arc_dn = [(r_dn * math.sin(t), RT + r_dn - r_dn * math.cos(t)) for t in np.linspace(0, tn, 8)][1:]
    pts += arc_dn
    N = np.array(arc_dn[-1])
    E = np.array([THROAT_Z - 0.0, EXIT_IN_R])
    # control point: where the entrance and exit tangents meet
    m1, m2 = math.tan(tn), math.tan(te)
    c1, c2 = N[1] - m1 * N[0], E[1] - m2 * E[0]
    sx = (c2 - c1) / (m1 - m2)
    Q = np.array([sx, m1 * sx + c1])
    bell = _bezier(N, Q, E, n_bell)[1:]
    pts += [tuple(p) for p in bell]
    P = np.array(pts)
    return np.column_stack([THROAT_Z - P[:, 0], P[:, 1]])


if __name__ == "__main__":
    g = gas_side()
    outer = np.array(BELL_OUTER)
    for z, r in g[::3]:
        o = np.interp(z, outer[:, 0], outer[:, 1]) if z <= 1089 else float("nan")
        print(f"z {z:7.1f} r_gas {r:6.1f}  outer {o:6.1f}  wall {o - r:6.1f}")
