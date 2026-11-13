"""SR-71 Blackbird at 1:144, built the way its loft drawings are: every part is drawn from its own
station tables (FS fuselage station aft from the nose, BL butt line out from the centreline, WL water
line up from the wing plane; metres, full scale), made as an implicit body, meshed by Fidget, and the
parts placed in one assembly with the joint gaps a real airframe has.

Parts: nose section, forward fuselage (with chines and canopy fairing), aft fuselage, pitot boom,
canopy glazing, wing (with its fillets into the fuselage and nacelles), inboard and outboard elevons,
and per side: inlet spike, inlet cowl, nacelle, ejector (slotted tail feathers), afterburner nozzle,
turbine face, fin base and all-moving rudder.
"""
import math
import time

from tule import *
from tule import sdf
from tule.sdf import clamp, sqrt

SCALE = 1000 / 144          # full-scale metres -> model millimetres
GAP = 0.03                  # joint gap between parts (0.2 mm at 1:144)
HINGE_GAP = 0.018           # around the elevons
EDGE = 0.032                # half-thickness kept at leading and trailing edges (0.45 mm thick at 1:144)


# ---------------------------------------------------------------- drawing helpers
def pwl(t, pts):
    """A station table [(station, value), ...] as a field."""
    return sdf.interp(t, *zip(*pts))


smin, smax = sdf.smooth_min, sdf.smooth_max


def between(x, lo, hi):
    """The station range lo .. hi, half a joint gap taken off each cut face."""
    return sdf.slab(x, lo + GAP / 2, hi - GAP / 2)


# ---------------------------------------------------------------- fuselage lofts
FUSE_R = [(0.0, 0.0), (0.8, 0.17), (2.0, 0.37), (4.0, 0.58), (6.5, 0.74), (9.0, 0.83), (12.0, 0.86),
          (21.0, 0.86), (24.5, 0.80), (27.5, 0.60), (30.0, 0.32), (31.4, 0.0)]
FUSE_WL = 0.15
FLAT = 0.8                  # the belly is flatter than the back: lower semi-axis / upper
CHINE = [(0.25 + 14.3 * (i / 28), 3.3 * (i / 28) ** 0.62) for i in range(29)]   # (FS, BL) of the chine edge
CHINE_T = 0.12
# canopy fairing and dorsal spine: height above the 85 % line of the body, FS 4.8 .. 25
FAIR_H = [(4.8, 0.0), (5.25, 0.30), (6.0, 0.47), (7.6, 0.50), (9.4, 0.45), (10.6, 0.33), (13.0, 0.20),
          (20.0, 0.14), (25.0, 0.0)]
FAIR_W = 0.46
FUSE_SECTIONS = [("nose section", -0.1, 3.6), ("forward fuselage", 3.6, 13.0), ("aft fuselage", 13.0, 31.6)]


def fuse_body(x, y, z):
    dz = z - FUSE_WL
    dz = sdf.max(dz, 0) + sdf.min(dz, 0) / FLAT
    return sdf.length(y, dz) - pwl(x, FUSE_R)


def fairing(x, y, z):
    top = FUSE_WL + 0.85 * pwl(x, FUSE_R) + pwl(x, FAIR_H) * (1 - (y / FAIR_W) * (y / FAIR_W))
    return sdf.max(z - top, abs(y) - FAIR_W, FUSE_WL - z, 4.8 - x, x - 25.0)


def chine(x, y, z):
    w = pwl(x, CHINE)
    s = clamp(abs(y) / sdf.max(w, 1e-3), 0, 1)
    return sdf.max(abs(y) - w, abs(z) - CHINE_T * (1 - s * s) * clamp(x / 2.5, 0.25, 1) - EDGE, 0.25 - x, x - 13.0)


def fuselage(x, y, z):
    return smin(smin(fuse_body(x, y, z), chine(x, y, z), 0.35), fairing(x, y, z), 0.15)


def window_mask(x, y, z):
    """Where the glazing is: windscreen, the pilot's side windows, the RSO's small windows."""
    sill = FUSE_WL + 0.85 * pwl(x, FUSE_R) + 0.16
    windscreen = sdf.max(between(x, 5.2, 6.15), sill - z)
    pilot = sdf.max(between(x, 6.25, 7.5), sill - z, 0.09 - abs(y))
    rso = sdf.max(between(x, 8.55, 9.15), sill + 0.06 - z, 0.16 - abs(y))
    return sdf.min(windscreen, pilot, rso)


def glazing(x, y, z):
    return sdf.max(fuselage(x, y, z) - 0.018, window_mask(x, y, z))


def pitot(x, y, z):
    r = pwl(x, [(-1.9, 0.0), (-1.75, 0.03), (-0.2, 0.04), (0.35, 0.08)])
    return sdf.max(sdf.length(y, z - FUSE_WL) - r, -1.9 - x, x - 0.35)


# ---------------------------------------------------------------- wing
NAC_BL, NAC_WL = 4.45, 0.0
WING_LE = [(bl, fs) for fs, bl in CHINE if bl >= 2.4] + [(5.25, 12.2), (6.0, 14.4), (6.8, 17.1), (7.7, 20.3), (8.47, 23.5)]
WING_TE = [(0.0, 29.6), (3.3, 29.6), (5.6, 29.2), (8.47, 26.9)]
WING_TC = 0.03
ROOT_HALF = 0.13            # the wing box is no deeper than this inboard (half-depth)
TIP = 8.47
CORR_A, CORR_P = 0.014, 0.33  # corrugation depth and pitch (0.1 mm, 2.3 mm at 1:144)
DROOP = 0.16                 # conical camber: outer leading edge droop at the tip
ELEVONS = [("inboard elevon", 1.05, 3.12, 1.55), ("outboard elevon", 5.85, 8.05, 1.35)]  # BL from, to; chord


def wing_raw(x, y, z):
    b = abs(y)
    le, te = pwl(b, WING_LE), pwl(b, WING_TE)
    c = te - le
    s = clamp((x - le) / c, 0, 1)
    k = clamp(1 - s / 0.35, 0, 1)
    z0 = -DROOP * clamp((b - 5.8) / (TIP - 5.8), 0, 1) * k * k
    half = sdf.min(2 * WING_TC * (x - le) * (te - x) / c, ROOT_HALF) + EDGE
    # chordwise skin corrugations on the inner and outer wing panels (they take the heat growth)
    panel = sdf.max(sdf.min(b - 1.0, 3.15 - b), sdf.min(b - 5.9, 8.0 - b))
    fade = clamp(sdf.min(panel, x - 17.5, te - 0.5 - x) / 0.25, 0, 1)
    half = half + CORR_A * fade * sdf.cos(2 * math.pi * b / CORR_P)
    plan = smax(smax(b - TIP, x - te, 0.5), le - x, 0.4)
    return sdf.max(abs(z - z0) - half, plan)


def elevon_region(name, x, y, z):
    _, lo, hi, chord = next(e for e in ELEVONS if e[0] == name)
    b = abs(y)
    hinge = pwl(b, WING_TE) - chord
    return sdf.max(hinge - x, lo - b, b - hi)


def nac_r(x):
    return pwl(x, [(11.6, 1.00), (12.6, 1.12), (14.0, 1.22), (16.0, 1.27), (26.5, 1.27), (29.8, 1.14), (32.3, 1.10)])


def nac_outer(side, x, y, z):
    r = sdf.length(y - side * NAC_BL, z - NAC_WL)
    return sdf.max(r - nac_r(x), x - 32.3, 11.6 - x)


def wing(x, y, z):
    w = wing_raw(x, y, z)
    body = fuse_body(x, y, z)
    nl, nr = nac_outer(1, x, y, z), nac_outer(-1, x, y, z)
    w = smin(smin(smin(w, body, 0.3), nl, 0.25), nr, 0.25)          # fillets into fuselage and nacelles
    w = sdf.max(w, 0.03 - body, 0.02 - nl, 0.02 - nr)                # ... but not the bodies themselves
    w = sdf.max(w, -sdf.max(x - 13.0 - GAP / 2, abs(y) - 4.45))      # the chine ahead of FS 13 is the fuselage's
    for name, *_ in ELEVONS:
        w = sdf.max(w, HINGE_GAP / 2 - elevon_region(name, x, y, z))
    return w


def elevon(name):
    return lambda x, y, z: sdf.max(wing_raw(x, y, z), elevon_region(name, x, y, z) + HINGE_GAP / 2)


# ---------------------------------------------------------------- propulsion
SPIKE = [(9.6, 0.0), (11.9, 0.55), (12.8, 0.70), (13.8, 0.76), (15.5, 0.66)]
INLET_BORE = [(11.6, 0.95), (12.4, 1.02), (14.5, 1.05), (16.0, 0.9)]


def nacelle_piece(side, lo, hi):
    yc = side * NAC_BL

    def f(x, y, z):
        r = sdf.length(y - yc, z - NAC_WL)
        shell = sdf.max(r - nac_r(x), between(x, lo, hi))
        duct = sdf.max(r - pwl(x, INLET_BORE), x - 15.5)
        bore = sdf.max(r - (nac_r(x) - 0.07), 30.2 - x)
        return sdf.max(shell, -duct, -bore)
    return f


def ejector(side):
    yc = side * NAC_BL

    def f(x, y, z):
        r = sdf.length(y - yc, z - NAC_WL)
        ring = sdf.max(r - nac_r(x), (nac_r(x) - 0.07) - r, between(x, 29.8, 32.3))
        theta = sdf.atan2(z - NAC_WL, y - yc)
        slots = sdf.max(abs(sdf.sin(12 * theta)) * r - 0.018, 31.5 - x)  # gaps between the tail feathers
        return sdf.max(ring, -slots)
    return f


def afterburner(side):
    yc = side * NAC_BL
    return lambda x, y, z: (lambda r: sdf.max(r - 1.0, 0.82 - r, x - 30.9, 30.2 - x))(sdf.length(y - yc, z - NAC_WL))


def turbine_face(side):
    yc = side * NAC_BL

    def f(x, y, z):
        r = sdf.length(y - yc, z - NAC_WL)
        cone = r - 0.45 * clamp((30.4 - x) / 0.6, 0, 1)                  # the tail cone in the middle
        return sdf.min(sdf.max(r - 1.06, abs(x - 29.9) - 0.05), sdf.max(cone, 29.8 - x))
    return f


def spike(side):
    yc = side * NAC_BL
    return lambda x, y, z: sdf.max(sdf.length(y - yc, z - NAC_WL) - pwl(x, SPIKE), x - 15.5, 9.6 - x)


FIN_ROOT, FIN_TIP, FIN_H, FIN_TC, FIN_CANT = (25.6, 30.2), (28.4, 30.65), 2.6, 0.05, 15
RUDDER_Z = 0.32              # the fixed base below, the all-moving rudder above


def fin_local(x, y, z):
    s = clamp(z / FIN_H, 0, 1)
    le = FIN_ROOT[0] + (FIN_TIP[0] - FIN_ROOT[0]) * s
    te = FIN_ROOT[1] + (FIN_TIP[1] - FIN_ROOT[1]) * s
    half = 2 * FIN_TC * (x - le) * (te - x) / (te - le) + EDGE
    plan = smax(smax(z - FIN_H, le - x, 0.35), x - te, 0.2)
    return sdf.max(abs(y) - half, plan, -0.5 - z)


def fin_piece(side, lo, hi):
    body = sdf.field(lambda x, y, z: sdf.max(fin_local(x, y, z), between(z, lo, hi)),
                     ((25.3, -0.4, -0.6), (30.9, 0.4, 2.7)))
    return body.rotate(x=side * FIN_CANT).at(0, side * NAC_BL, NAC_WL + 1.20)


# ---------------------------------------------------------------- parts
def make(name, f, bounds, color, cell=0.015):
    t = time.time()
    body = f if isinstance(f, sdf.Implicit) else sdf.field(f, bounds)
    m = body.scale(SCALE).to_mesh(cell * SCALE)
    try:
        m = m.simplify(0.006)
    except TuleError:
        print(f"  ({name}: not manifold, left unsimplified)")
    m = m.named(name, color)
    print(f"  {name}: {m.triangles:,} triangles, {time.time() - t:.1f} s")
    return m


INK, SKIN, PANEL, FLAP, TI, BURNT, DARK, GLASS, PROBE = (
    "#202329", "#262a31", "#2c3038", "#31353e", "#6e675f", "#9b6a43", "#121418", "#41607dd9", "#8d9198")

t0 = time.time()
parts = []
for i, (name, lo, hi) in enumerate(FUSE_SECTIONS):
    parts.append(make(name, lambda x, y, z, lo=lo, hi=hi: sdf.max(fuselage(x, y, z), between(x, lo, hi)),
                      ((lo - 0.1, -3.5, -1.0), (hi + 0.1, 3.5, 1.9)), SKIN if i != 1 else INK, cell=0.01))
parts.append(make("pitot boom", pitot, ((-2.0, -0.2, -0.1), (0.5, 0.2, 0.4)), PROBE, cell=0.008))
parts.append(make("canopy glazing", glazing, ((5.0, -0.7, 0.5), (9.4, 0.7, 1.7)), GLASS, cell=0.01))
parts.append(make("wing", wing, ((11.8, -8.6, -0.7), (29.8, 8.6, 0.7)), PANEL, cell=0.01))
for name, lo, hi, chord in ELEVONS:
    for side, tag in ((1, "left"), (-1, "right")):
        yb = (side * lo, side * hi)
        parts.append(make(f"{name} {tag}", lambda x, y, z, n=name, s=side: sdf.max(elevon(n)(x, y, z), -s * y),
                          ((25.0, min(yb) - 0.1, -0.4), (29.8, max(yb) + 0.1, 0.4)), FLAP, cell=0.01))
for side, tag in ((1, "left"), (-1, "right")):
    yc = side * NAC_BL
    lo, hi = (11.4, yc - 1.4, -1.4), (32.5, yc + 1.4, 1.4)
    parts += [
        make(f"inlet spike {tag}", spike(side), ((9.5, yc - 0.9, -0.9), (15.6, yc + 0.9, 0.9)), DARK),
        make(f"inlet cowl {tag}", nacelle_piece(side, 11.6, 16.0), (lo, (16.2, *hi[1:])), SKIN),
        make(f"nacelle {tag}", nacelle_piece(side, 16.0, 29.8), ((15.8, *lo[1:]), (30.0, *hi[1:])), INK, cell=0.02),
        make(f"ejector {tag}", ejector(side), ((29.6, *lo[1:]), hi), TI, cell=0.012),
        make(f"afterburner nozzle {tag}", afterburner(side), ((30.0, yc - 1.1, -1.1), (31.0, yc + 1.1, 1.1)), BURNT),
        make(f"turbine face {tag}", turbine_face(side), ((29.7, yc - 1.1, -1.1), (30.5, yc + 1.1, 1.1)), DARK),
        make(f"fin base {tag}", fin_piece(side, -0.6, RUDDER_Z), None, SKIN, cell=0.008),
        make(f"rudder {tag}", fin_piece(side, RUDDER_Z, FIN_H + 0.1), None, PANEL, cell=0.008),
    ]

blackbird = Assembly(*parts, name="SR-71 Blackbird 1:144")
print(f"{len(parts)} parts, {sum(p.triangles for p in parts):,} triangles, {time.time() - t0:.0f} s")
outputs.assembly("blackbird", blackbird)
