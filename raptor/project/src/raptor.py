"""Raptor 3 (sea level), built only from implicit bodies (tule.sdf), meshed part by part.

Stages: outer envelope (lib/envelope.py) → turbopumps and preburners (lib/pumps.py) → ducts and lines
(lib/plumbing.py) → internals (lib/internals.py: INFERRED, not from photos). Evidence: raptor/KNOWN.md,
raptor/UNKNOWN.md, raptor/evidence/INDEX.md (internal reference only; no markings are modelled).

Sections that show the internals (the viewer's Section button, or tule screenshot --section):
  y:0       lengthwise through the axis: liner, channels (meshed at 0.3 mm in a band |y| < 3 mm),
            injector posts, the hot-gas dome and manifolds
  z:1555    across the throat: the ring of 360 channels (band 1549–1561 mm)
  z:1992    across the injector face: the coaxial elements (band 1988–1996 mm)
"""
import time

from tule import Assembly, outputs

from lib import contour as C
from lib import envelope as E
from lib import internals as I
from lib import plumbing as P
from lib import pumps as U
from lib.common import BELL, BRONZE, COPPER, DUCT, HOUSING, INTERNAL, SATIN, STEEL, mesh

t0 = time.time()
CELL = 3.0           # coarse cell for big smooth parts (mm)
FINE = 0.3           # cell in the section bands (the channels are 0.96 mm wide at the throat)
Y_BAND = ((-720, -3, 0), (720, 3, 2000), FINE)
THROAT_BAND = ((-260, -260, C.THROAT_Z - 6), (260, 260, C.THROAT_Z + 6), FINE)
FACE_BAND = ((-180, -180, C.FACE_Z + 3), (180, 180, C.FACE_Z + 11), 0.4)
INJ_Y_BAND = ((-180, -6, C.FACE_Z - 2), (180, 6, I.POST_TOP + 2), 0.4)


def log(what):
    print(f"  {time.time() - t0:6.1f} s  {what}", flush=True)


# ---------------------------------------------------------------- 1. envelope + 4. internals of the chamber
tca = E.thrust_chamber_solid()
ports = I.manifold_ports()
cooled = tca - I.channels() - I.collectors() - ports
chamber = mesh(cooled, "thrust chamber and bell", BELL, CELL, simplify=0.03, fine=[Y_BAND, THROAT_BAND], rest=tca)  # grooves and ports only where the cell resolves the 1 mm hot wall
log(f"thrust chamber: {chamber.triangles:,} triangles")

rings = E.manifold_rings()
duct = P.transfer_duct()
cav = I.ring_cavities()
duct_bore = P.transfer_duct_bore()
ring_parts = []
for name, body in rings.items():
    hollow = body - cav - duct_bore - ports - P.line_a_bore()
    ring_parts.append(mesh(hollow, name, HOUSING, 1.5, simplify=0.15))
log("manifold rings")

disc_body = E.base_disc()
col = E.column_and_top()
powerhead = (disc_body | col["hub"] | col["valve body"] | col["ox turbopump housing"]) - I.powerhead_cavities() - E.chamber_seat() - P.line_a_bore()
powerhead_m = mesh(powerhead, "powerhead (disc, valve body, ox turbopump housing)", HOUSING, 2.0, simplify=0.2,
                   fine=[((-720, -4, 1900), (720, 4, 2600), 0.6), ((180, 0, 1890), (520, 260, 2060), 1.0)], rest=(disc_body | col["hub"] | col["valve body"] | col["ox turbopump housing"]) - E.chamber_seat())
log(f"powerhead: {powerhead_m.triangles:,} triangles")

inj_full, inj_plain = I.injector()
injector = mesh(inj_full, "main injector (inferred)", COPPER, 2.0, simplify=0.03, fine=[FACE_BAND, INJ_Y_BAND], rest=inj_plain)
log(f"injector: {injector.triangles:,} triangles, {I.element_count()} elements")

top = [
    mesh(col["valve flange"], "valve flange", SATIN, 1.2, simplify=0.1),
    mesh(col["disc bolts"], "disc bolts", SATIN, 1.0, simplify=0.1),
    mesh(col["top flange"], "top flange", SATIN, 1.5, simplify=0.1),
    mesh(col["studs"], "flange studs", STEEL, 1.0, simplify=0.1),
    mesh(col["gimbal ring"], "gimbal ring", BRONZE, 1.5, simplify=0.1),
    mesh(col["gimbal sleeve"], "gimbal sleeve", STEEL, 1.5, simplify=0.1),
    mesh(col["lox inlet"], "lox inlet", SATIN, 1.5, simplify=0.1),
]
log("top stack")

# ---------------------------------------------------------------- 2. turbopumps and preburners
ftp = U.fuel_turbopump()
pumps = [
    mesh(U.ox_ports(), "ox turbopump ports", HOUSING, 1.5, simplify=0.15),
    mesh(ftp["fuel turbopump"], "fuel turbopump", HOUSING, 2.0, simplify=0.2),
    mesh(ftp["fuel inlet flange"], "fuel inlet flange", SATIN, 1.5, simplify=0.1),
    mesh(ftp["fuel inlet cap"], "fuel inlet", SATIN, 1.5, simplify=0.1),
    mesh(ftp["fuel discharge"], "fuel discharge", DUCT, 1.5, simplify=0.15),
    mesh(U.valve_block(), "main fuel valve", SATIN, 1.5, simplify=0.15),
    mesh(U.preburner_can(), "fuel-side preburner can", DUCT, 2.0, simplify=0.2),
    mesh(U.tvc_clevises(), "actuator clevises", SATIN, 1.2, simplify=0.1),
]
log("turbopumps")

# ---------------------------------------------------------------- 3. ducts and lines
plumbing = [
    mesh(duct - cav, "transfer duct", DUCT, 1.5, simplify=0.15),
    mesh(P.small_lines(), "small lines", DUCT, 1.0, simplify=0.1),
]
log("plumbing")

engine = Assembly(
    Assembly(chamber, *ring_parts, name="thrust chamber"),
    Assembly(powerhead_m, injector, *top, name="powerhead"),
    Assembly(*pumps, name="turbopumps"),
    Assembly(*plumbing, name="ducts"),
    name="Raptor 3 (implicit)",
)
outputs.assembly("raptor", engine)
n = sum(p.triangles for p in [chamber, powerhead_m, injector, *ring_parts, *top, *pumps, *plumbing])
log(f"written: {n:,} triangles")
