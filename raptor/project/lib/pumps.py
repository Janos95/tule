"""Stage 2: turbopumps and preburners.

* Ox turbopump: on the axis under the LOX inlet (KNOWN §4: Ø 0.42 m, centre 2.42 m); its housing is in
  envelope.column_and_top(). Here: its round port bosses (three on the front-left in 012 / 026) and the
  ox-rich preburner, which is inside the column under the turbine (inferred: no duct is visible, UNKNOWN §7).
* Fuel turbopump: off axis on +x (KNOWN §4: envelope 0.52 × 0.44 m, centre 0.41 m out, 2.22 m up; inlet with
  a cap on top, a volute discharging toward the front, 026 / 007), with the main fuel valve block between it
  and the column (the silver blocks in 012, x 520–700).
* Fuel-rich preburner: the vertical bulbous body under the fuel turbopump (KNOWN §4: Ø 0.19 m, 0.37–0.56 m
  off axis, 1.36–2.0 m up). Its identity is inferred (UNKNOWN §6); it is modelled as a can with three
  swellings that turns its hot gas back up into the disc lobe.
"""
from tule import sdf

from lib.common import disc, lathe, polar

FTP_X, FTP_Y = 460.0, 120.0  # turbopump axis (vertical): stacked over the preburner can, cap clear of the column
                             # (012, 007, 026; ROUND-2 PH2-2)


def ox_ports() -> sdf.Implicit:
    """Round bolted port bosses on the ox turbopump housing (012: three on the front at y 410–470)."""
    out = None
    for deg, z, r in ((-150, 2470, 46), (-110, 2480, 52), (-70, 2470, 46), (150, 2460, 40)):
        boss = sdf.cylinder(r, 70).rotate(y=90).at(170, 0, z).rotate(z=deg)
        cap = sdf.cylinder(r * 0.72, 12).rotate(y=90).at(238, 0, z).rotate(z=deg)
        b = boss | cap
        out = b if out is None else out | b
    return out


def fuel_turbopump() -> dict:
    """The fuel turbopump: turbine casing on the disc lobe, the volute right under the inlet cap (012 x +270…+640,
    z 2210–2345; 026, 007: a scroll about as wide as the ox column; ROUND-1 PH-2), a short neck and the cap."""
    x, y = FTP_X, FTP_Y
    out = {}
    casing = lathe([(0, 2017), (150, 2017), (168, 2050), (172, 2130), (160, 2200), (120, 2240), (0, 2240)])
    volute = sdf.torus(150, 66).at(0, 0, 2280)
    # the volute grows toward its tongue: a second, offset torus blended in
    swell = sdf.torus(138, 78).at(0, -26, 2280)
    body = casing.union(volute, swell, smooth=30)
    neck = lathe([(0, 2300), (92, 2300), (84, 2350), (86, 2378), (0, 2378)])
    out["fuel turbopump"] = body.union(neck, smooth=16).at(x, y, 0)
    out["fuel inlet flange"] = disc(104, 2376, 2398).at(x, y, 0)
    out["fuel inlet cap"] = lathe([(0, 2398), (82, 2398), (82, 2448), (74, 2456), (0, 2456)]).at(x, y, 0)
    # discharge: from the volute down and toward the front (-y), its flange 0.58 m out at z 2170 (PH-8)
    # from the volute's outer side, a long run to the flange at its front edge (026; ROUND-2 PH2-1)
    out["fuel discharge"] = sdf.tube([(x + 95, y - 150, 2262), (590, -150, 2215), (590, -226, 2167)],
                                     r=56, bend=50).union(sdf.cylinder(95, 24).rotate(x=90).at(590, -216, 2167),
                                                           smooth=6)
    return out


def valve_block() -> sdf.Implicit:
    """The main valve cluster between the column and the fuel turbopump (012: x +108…+471, z 2075–2345;
    027/029: a large housing fills that space; ROUND-1 PH-3), its actuator, and the bent line from the ox
    column (PH-7: 012, 026, 004)."""
    c = (290, -150, 2195)
    blk = sdf.box(320, 150, 220, center=True).at(*c)  # x 130–450 (PH-3)
    blk = blk.intersection(sdf.sphere(205).at(*c), smooth=14)
    actuator = sdf.cylinder(48, 110).at(c[0] - 60, c[1], c[2] + 100)
    face = sdf.cylinder(80, 22).rotate(x=90).at(c[0] + 40, c[1] - 70, c[2] - 10)   # the round bolted valve face
    stem = sdf.cylinder(34, 90).rotate(x=90).at(c[0] - 90, c[1] - 70, c[2] - 30)
    # the bent silver line from the ox column at port level (z ≈ 2480) to the valve top (PH-7), with a boss
    line = sdf.tube([polar(200, -40, 2480), (365, -205, 2480), (365, -210, 2300)], r=17, bend=45)
    boss = sdf.cylinder(28, 30).rotate(y=90).at(190, 0, 2480).rotate(z=-40)
    return blk | actuator | face | stem | line | boss


def preburner_can() -> sdf.Implicit:
    """The fuel-side vertical body (inferred fuel-rich preburner), Ø 0.19 m with swellings, 1.36–2.0 m up."""
    x, y = polar(455, 18, 0)[:2]
    prof = [(0, 1912), (105, 1912), (100, 1895), (96, 1880), (100, 1840), (84, 1790), (98, 1730), (96, 1650),
            (80, 1600), (90, 1540), (86, 1470), (70, 1420), (0, 1420)]
    can = lathe(prof).at(x, y, 0)
    # a flared top under the disc (PH-5); the bottom elbow curls inward and down, closed by a domed end (026)
    x2, y2 = polar(398, 9, 0)[:2]  # toward the axis and the front (PH-6)
    elbow = sdf.tube([(x, y, 1460), (x, y, 1400), (x2, y2, 1340)], r=[70, 64, 58], bend=60)
    cap = sdf.sphere(58).at(x2, y2, 1340)
    return can.union(elbow, smooth=12) | cap


def tvc_clevises() -> sdf.Implicit:
    """Actuator clevis lugs on the column (012: y 640–680 on the left, ≈ 2.2 m up), two at 90° apart."""
    out = None
    for deg in (135, 225):
        lug = sdf.box(70, 46, 70, center=True).at(205, 0, 2165)
        lug = lug - sdf.cylinder(14, 80).rotate(x=90).at(225, 0, 2165).translate(0, 40, 0)
        pair = (lug.at(0, 30, 0) | lug.at(0, -30, 0)).rotate(z=deg)
        out = pair if out is None else out | pair
    return out
