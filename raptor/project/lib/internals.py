"""Stage 4: internals. ALL INFERRED: no public evidence shows Raptor's channels, injector or manifolds
(raptor/UNKNOWN.md). The numbers are plausible engineering from the literature cited there.

* Regenerative cooling: a copper-alloy liner with milled channels under a closed-out jacket (channel
  wall, UNKNOWN §1–2: the outside of the bell is smooth, no tubes). 360 channels from the injector face
  down to 1.35 m, 1 mm hot wall, 1 mm lands, so 0.96 mm wide at the throat and at most 2.7 mm; they split
  into 720 (0.8 mm lands) down to 0.78 m and into 1440 to the lip, so no channel is wider than 3.2 mm
  under the hot wall (ROUND-3 INT-23). The outside of the
  sea-level bell has no ribs or bands (KNOWN §5), so the closeout carries the hoop load itself: nothing
  in the channel layout has to line up with external stiffeners.
* Coolant circuit (round 1, ROUND-1-internals INT-1…4, 9, 10): every ring station has a collector groove
  that cuts across all channels just outside the 1 mm hot wall; radial ports run from the groove (never
  through the hot wall) out into the hollow ring. Methane arrives through the transfer duct in the lower
  chamber ring (1.435 m, inlet) and splits:
    up:   360 channels → mixing groove under the upper chamber ring (1.83 m, a closed equalising ring) →
          head-end collector at the injector face (1.975 m) → 48 ports into the coolant return annulus in
          the base disc → fuel-rich preburner → fuel turbopump turbine → fuel-rich gas annulus → injector;
    down: 360 channels → bell-top collector (1.15 m), outlet to the bell-top ring, which returns through the
          hollow thin line up to the coolant return annulus; part of it feeds the 720 bell channels
          (bifurcation at the groove), which run down to a lip collector and leave through 72 small dump
          holes as a film at the exit (a dump fraction: the one open assumption of the circuit).
  Grooves start 2.6 mm out so the lands under the 1 mm hot wall stay; ports start along the wall normal.
  The throat ring is a bleed / instrumentation tap (ports from its own groove, the thin line to the disc).
* Main injector: 361 gas-gas coaxial elements on 10 rings at 14 mm pitch inside the ox-rich dome's
  footprint (r ≤ 141, INT-6; UNKNOWN §4: ≈ 400): ox posts from the ox-rich dome through a fuel cavity,
  each in a face hole that leaves a 1.7 mm annulus for the fuel-rich gas. The plates are let 2 mm into
  the disc all round (INT-7).
* Powerhead: an ox-rich hot-gas dome under the ox turbopump, a fuel-rich annular manifold in the base
  disc fed from the disc lobe under the fuel turbopump, and radial slots into the fuel cavity (UNKNOWN §7).
"""
import math

from tule import sdf

from lib import contour as C
from lib.common import disc, lathe, polar
from lib.envelope import DISC_Z, RINGS
from lib.pumps import FTP_X, FTP_Y

Z_SPLIT = RINGS["bell-top ring"][0]  # channel bifurcation at the bell-top ring
INJ = dict(face=(C.FACE_Z, C.FACE_Z + 15), cavity=(C.FACE_Z + 15, C.FACE_Z + 35), back=(C.FACE_Z + 35, C.FACE_Z + 50))
POST_TOP = C.FACE_Z + 62


def wall() -> sdf.Wall:
    g = C.gas_side(60)[::-1]  # z increasing
    return sdf.Wall(g[:, 0], g[:, 1])


SPLIT_1, SPLIT_2 = 1350, 780   # channel bifurcations (ROUND-3 INT-23): gas radius 210 mm and 450 mm
UPPER_DEPTH = [(SPLIT_1, 2.6), (1450, 2.6), (C.THROAT_Z, 2.2), (1660, 3.0), (C.FACE_Z, 4.0)]


def channels() -> sdf.Implicit:
    """360 channels from the head down to 1.35 m (≤ 2.7 mm wide), 720 down to 0.78 m (1.0–3.2 mm), 1440 to the
    lip (1.2–2.0 mm). Each split overlaps 2 mm, the finer set phased so two channels sit under each coarser one."""
    w = wall()
    upper = w.channels(360, land=1.0, offset=1.0, z=(SPLIT_1, C.FACE_Z - 6), depth=UPPER_DEPTH)
    mid = w.channels(720, land=0.8, offset=1.0, z=(SPLIT_2, SPLIT_1 + 2), depth=[(SPLIT_2, 3.0), (SPLIT_1 + 2, 2.6)], phase=0.25)
    lower = w.channels(1440, land=0.8, offset=1.2, depth=3.0, z=(40, SPLIT_2 + 2), phase=0.125)
    return upper | mid | lower


HEAD_Z = C.FACE_Z - 10      # head-end collector
LIP_Z = 36                  # lip collector (bell dump)
# Collector grooves start 2.6 mm out (ROUND-2 INT-15): the 1 mm hot wall keeps its lands underneath and the
# groove overlaps the outer 0.6–2.4 mm of every channel, so it still joins them all.
G_BASE, G_HEIGHT = 2.6, 5.0
LIP_BASE, LIP_HEIGHT = 3.2, 2.2
PORT_R = 3.5


def collectors() -> sdf.Implicit:
    """Full-circumference grooves at every ring station, the head end and the lip: they join all channels."""
    w = wall()
    rings = w.bands([z for z, R, a in RINGS.values()] + [HEAD_Z], width=10, height=G_HEIGHT, base=G_BASE)
    lip = w.bands([LIP_Z], width=10, height=LIP_HEIGHT, base=LIP_BASE)
    return rings | lip


def _port_start(w, z: float, base: float) -> float:
    """Radius where a radial port of radius PORT_R starts so its round end stays at or outside normal
    offset ``base`` on the sloped wall (ROUND-2 INT-14: offsets along the normal, not the radius)."""
    s = abs(float(w.r_at(z + 1.0) - w.r_at(z - 1.0))) / 2.0          # dr/dz
    cos = 1.0 / math.sqrt(1.0 + s * s)
    return float(w.r_at(z)) + base / cos + PORT_R + PORT_R * s + 0.3


def manifold_ports() -> sdf.Implicit:
    """48 radial ports per ring, from the collector groove (never through the hot wall) out to the ring
    cavity; 48 from the head-end collector into the coolant return annulus in the disc; 72 dump holes at
    the lip."""
    w = wall()
    out = None
    for name, (z, R, a) in RINGS.items():
        port = sdf.capsule((_port_start(w, z, G_BASE), 0, z), (R, 0, z), PORT_R).repeat_polar(48, overlap=False)
        out = port if out is None else out | port
    out = out | sdf.capsule((_port_start(w, HEAD_Z, G_BASE), 0, HEAD_Z), (240, 0, HEAD_Z), PORT_R).repeat_polar(48, overlap=False)
    r_lip = float(w.r_at(LIP_Z))
    dump = sdf.capsule((r_lip - 2.0, 0, LIP_Z - 4), (r_lip + LIP_BASE + 1.0, 0, LIP_Z), 0.7).repeat_polar(72, overlap=False)
    return out | dump


def ring_cavities() -> sdf.Implicit:
    out = None
    for z, R, a in RINGS.values():
        cav = sdf.torus(R, a - 5).at(0, 0, z)
        out = cav if out is None else out | cav
    return out


def _element_rings():
    """(radius, count) of the injector element rings: a centre element and 10 rings at 14 mm pitch, all
    inside the ox-rich dome (r ≤ 141 < 165)."""
    rings = [(0.0, 1)]
    for k in range(1, 11):
        r = 13.5 * k + 6
        rings.append((r, max(6, round(2 * math.pi * r / 14))))
    return rings


def element_count() -> int:
    return sum(n for _, n in _element_rings())


def _at_elements(make) -> sdf.Implicit:
    out = None
    for r, n in _element_rings():
        one = make().at(r, 0, 0)
        body = one if n == 1 else one.repeat_polar(n)
        out = body if out is None else out | body
    return out


def injector():
    """(the injector with its holes and posts, the same without them: plates only, for coarse cells)."""
    f0, f1 = INJ["face"]
    b0, b1 = INJ["back"]
    face, back = disc(C.RC + 3, f0, f1), disc(C.RC + 3, b0, b1)
    holes = _at_elements(lambda: sdf.cylinder(5.5, f1 - f0 + 2).at(0, 0, f0 - 1))
    posts = _at_elements(lambda: sdf.cylinder(3.8, POST_TOP - f0 - 1).at(0, 0, f0 + 1))
    bores = _at_elements(lambda: sdf.cylinder(2.4, POST_TOP - f0 + 2).at(0, 0, f0 - 1))
    full = ((face - holes) | back | posts) - bores
    plain = face | back
    return full, plain


def powerhead_cavities() -> sdf.Implicit:
    """The voids inside the powerhead (base disc, valve body): injector bay, ox-rich dome, fuel-rich
    annular manifold with its feed from the disc lobe and the radial slots into the fuel cavity."""
    bay = disc(C.RC + 1, C.FACE_Z - 1, INJ["back"][1])
    # the ox-rich dome covers every element (INT-6), keeps ≥ 20 mm of wall (INT-11) and is fed from the ox
    # turbopump's turbine above
    dome = lathe([(0, INJ["back"][1] - 1), (150, INJ["back"][1] - 1), (146, 2060), (138, 2110), (116, 2190), (60, 2216), (0, 2220)])
    inlet = disc(62, 2200, 2330)
    # ROUND-2 INT-16/17, the full-flow fuel side: heated methane from the head-end collector and the bell-top
    # ring (thin line) gathers in a coolant return annulus, runs out to the fuel-rich preburner (the can under
    # the lobe), whose gas rises to the fuel turbopump's turbine; the turbine exhaust comes back through the
    # disc into a separate fuel-rich gas annulus and the radial slots into the injector's fuel cavity.
    coolant = sdf.rect(80, 36).at(230, 1962).revolve()                  # r 190–270, z 1944–1980
    to_preburner = sdf.tube([polar(250, 26, 1962), polar(392, 24, 1962), polar(392, 24, 1900)], r=18, bend=30)
    x, y = polar(455, 18, 0)[:2]
    riser = sdf.tube([(x, y, 1900), (FTP_X, FTP_Y, 2030)], r=32)          # preburner gas up to the turbine
    # the gas annulus and exhaust rise into the hub on top of the disc (ROUND-3 INT-17, INT-22: larger passages)
    gas = sdf.rect(70, 46).at(220, 2017).revolve()                       # r 185–255, z 1994–2040
    exhaust = sdf.tube([(FTP_X - 100, FTP_Y - 60, 2040), (FTP_X - 100, FTP_Y - 60, 2012), (235, 0, 2014)], r=24, bend=30)
    slots = sdf.box(60, 30, 16, center=True).at(C.RC + 25, 0, 2010).repeat_polar(16)
    return bay | dome | inlet | coolant | to_preburner | riser | gas | exhaust | slots
