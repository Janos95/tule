"""Stage 1: the outer envelope. Thrust chamber and bell (one revolved wall around the inferred gas
side), the four manifold rings, the powerhead base disc, the central column and the top stack.

Heights and radii are mm in the common frame; each value names its evidence (raptor/KNOWN.md §4 and
evidence/img/NNN). The gas side inside the wall is inferred (lib/contour.py).
"""
import numpy as np
from tule import sdf

from lib import contour as C
from lib.common import disc, lathe, ring, bolt_circle

# ---------------------------------------------------------------- thrust chamber: outer skin (measured)
# above the bell the left side of 012 is hidden by the transfer duct and the right by the fuel-side duct:
# the cone between the bell-top ring and the throat follows 013/014 and the narrowest outer section (KNOWN §4)
# round 1 (ROUND-1-envelope ENV-1/2): a flat ledge on the bell-top ring, a straight cone r 300 → 230 between
# 1.19 and 1.36 m with a concave flare into the lower ring (012, 014), a concave hourglass neck r ≈ 149, and a
# skirt r 177 → 162 under the throat ring ending in a sharp rim at 1.565 m (012, 013, 014; ROUND-2 ENV-12)
UPPER_OUTER = [
    (1089, 348), (1104, 356), (1122, 384), (1140, 380), (1168, 368), (1180, 345), (1186, 304),
    (1350, 241), (1372, 246), (1395, 255), (1420, 205), (1470, 182), (1500, 162), (1530, 151),
    (1555, C.THROAT_OUTER_R), (1563, 150), (1567, 162), (1578, 165), (1592, 169), (1612, 177), (1640, 200), (1672, 223),
    (1790, C.CHAMBER_OUTER_R), (1830, 240), (1870, 258), (1905, 250), (1925, 215), (1960, 196), (1985, 190),
]

# the four manifold rings (KNOWN §4 table and §5): (z, ring radius R, tube radius a)
RINGS = {
    "bell-top ring": (1152, 382, 30),     # OD 0.83 m at 1.15 m (y 1240–1270 in 012)
    "lower chamber ring": (1435, 228, 50),  # OD 0.56 m, 110 mm tall, centre 1.435 m (012 y 1053–1118; ENV-3)
    "throat ring": (1648, 207, 29),        # OD 0.47 m at 1.65 m, tube ≈ 58 mm (012, 013; ENV-7)
    "upper chamber ring": (1826, 262, 39),  # OD 0.60 m, tube ≈ 78 mm tall (012 y 820–870; ROUND-2 ENV-13)
}

DISC_Z = (1904, 2020)       # powerhead base disc, y 739–807 in 012 (ENV-4)
DISC_LEFT, DISC_RIGHT = 300, 690   # it reaches 0.30 m to the left (-x) and 0.69 m to the right (+x)


def outer_points() -> list:
    """(r, z) of the outside from the lip up to the top of the thrust chamber."""
    pts = [(r, z) for z, r in C.BELL_OUTER] + [(r, z) for z, r in UPPER_OUTER[1:]]
    return pts


def thrust_chamber_solid() -> sdf.Implicit:
    """The chamber, throat and bell as one revolved wall: the measured outside, the inferred gas side
    inside (without cooling channels: lib/internals.py cuts them). Built in wall coordinates (two
    sdf.Wall contours, r over z), which is far cheaper to evaluate than the same outline as a polygon."""
    outer = sdf.Wall([z for z, r in C.BELL_OUTER + UPPER_OUTER[1:]], [r for z, r in C.BELL_OUTER + UPPER_OUTER[1:]])
    g = C.gas_side()[::-1]
    gas = sdf.Wall(g[:, 0], g[:, 1])
    body = sdf.Implicit(sdf.max(outer.n, -gas.n, sdf.slab(sdf.z, 0.0, C.FACE_Z)),
                        ((-C.EXIT_R - 5, -C.EXIT_R - 5, 0), (C.EXIT_R + 5, C.EXIT_R + 5, C.FACE_Z)))
    lip = ring(C.EXIT_R - 4, 7, 9)  # a slight rolled lip at the exit (014)
    return body | lip


def chamber_seat() -> sdf.Implicit:
    """The space the thrust chamber's top takes up inside the base disc (ROUND-1 INT-5): cut from the disc so
    the disc never fills the chamber or covers its channels."""
    pts = C.BELL_OUTER + UPPER_OUTER[1:]
    outer = sdf.Wall([z for z, r in pts], [r for z, r in pts])
    R = max(r for z, r in UPPER_OUTER) + 5
    return sdf.Implicit(sdf.max(outer.n, sdf.slab(sdf.z, 1800.0, C.FACE_Z)), ((-R, -R, 1800), (R, R, C.FACE_Z)))


def manifold_rings() -> dict:
    return {name: ring(R, a, z) for name, (z, R, a) in RINGS.items()}


def base_disc() -> sdf.Implicit:
    """The powerhead base disc: round about the axis, with a lobe under the fuel turbopump (+x)."""
    z0, z1 = DISC_Z
    lobe_r = 235
    shape = sdf.circle(DISC_LEFT).union(sdf.circle(lobe_r).at(DISC_RIGHT - lobe_r, 110), smooth=260)  # lobe under the pump
    plate = shape.offset(-12).extrude(z1 - z0 - 24).at(0, 0, z0 + 12).offset(12)  # rounded edges
    return plate


def column_and_top() -> dict:
    """The central column above the disc (main valve body, ox turbopump housing), the stud flange and
    the gimbal / LOX inlet stack (012 left panel, y 185–700; 026)."""
    out = {}
    # main valve body between disc and turbopump: y 610–740 in 012
    out["valve body"] = lathe([(0, 2010), (182, 2010), (182, 2040), (178, 2060), (160, 2120), (158, 2190), (175, 2236), (0, 2236)])
    out["valve flange"] = disc(205, 2092, 2112) | bolt_circle(28, 192, 2112, 12, 9)
    # the bolt ring sits on a raised hub on the disc (012 y 700–740, z 2044–2105; ENV-8), part of the powerhead
    # body: it roofs the fuel-rich gas annulus (ROUND-3 INT-17)
    out["hub"] = lathe([(176, DISC_Z[1] - 2), (345, DISC_Z[1] - 2), (340, 2034), (312, 2040), (306, 2048), (176, 2048)])
    out["disc bolts"] = bolt_circle(45, 282, 2048, 24, 42, phase=4.0)  # ≈ 45 nuts, 24 mm, up to 2.09 m
    # ox turbopump housing: Ø 0.42 m, waist r ≈ 158 at 2.25–2.36 m below its ports, top at 2.555 m (012; ENV-5/6)
    out["ox turbopump housing"] = lathe([(0, 2236), (162, 2236), (158, 2255), (158, 2350), (180, 2385),
                                         (205, 2430), (211, 2500), (206, 2545), (196, 2555), (0, 2555)])
    # spacer Ø 0.42 m (2555–2593) and top flange ≈ 0.48 m across (2593–2628), squarish with round corners (012, 026)
    spacer = disc(210, 2553, 2595)
    plate = sdf.rect(470, 470, radius=110).intersection(sdf.circle(248)).extrude(35).at(0, 0, 2593)
    out["top flange"] = spacer | plate
    out["studs"] = bolt_circle(16, 205, 2628, 18, 85, phase=11.25) | bolt_circle(16, 205, 2628, 30, 10, phase=11.25)
    # gimbal and LOX inlet stack: dark bronze ring (y 280–330), satin sleeve (y 240–280), inlet cap (y 185–240)
    out["gimbal ring"] = lathe([(0, 2628), (144, 2628), (144, 2766), (130, 2790), (0, 2790)])  # near cylinder, chamfered top (ENV-14)
    out["gimbal sleeve"] = disc(118, 2790, 2880)
    out["lox inlet"] = lathe([(0, 2880), (112, 2880), (112, 2935), (104, 2950), (0, 2950)])
    return out
