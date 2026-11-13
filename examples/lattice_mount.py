"""A printable mount from two kinds of body: a B-rep base and lug (exact holes, tags) joined by an
implicit lattice strut (an open gyroid in a capsule, blended into solid pads), checked with FEA.

Each conversion is written out where it happens: the B-rep parts become meshes at 0.02 mm, the strut
is meshed by Fidget at 0.5 mm cells; manifold3d unions the meshes and keeps the B-rep tags.
Writes outputs/lattice-mount (the part), outputs/lattice-mount-fea (stress) and outputs/lattice-mount-plate.
"""
import time

from tule import *

t0 = time.time()

# B-rep: base plate with two bolt holes, and a lug with a pin hole (exact cylinders, tagged)
base = box(80, 40, 6)
base = base.fillet(base.edges.parallel(Z), 5)
for y in (10, 30):
    base -= cylinder(3.2, 20, center=True).at(10, y, 0)
base = base.tag("bolt", base.faces.cylindrical(r=3.2))

lug = box(16, 16, 40).at(64, 12, 0)
lug -= cylinder(4, 30, center=True).rotate(x=90).at(72, 20, 32)
lug = lug.tag("pin", lug.faces.cylindrical(r=4))

# implicit: an open gyroid strut from the plate to the lug, blended into solid pads at both ends
a, b = (24, 20, 4), (57, 20, 27)
strut = sdf.capsule(a, b, 9) & sdf.gyroid(8, thickness=1.6)
pads = sdf.sphere(7).at(*a) | sdf.sphere(7).at(*b)
strut = strut.union(pads, smooth=3).clip((0, 0, 0), (80, 40, 50))

# convert, each on purpose, and join
part = base.to_mesh(0.02) | lug.to_mesh(0.02) | strut.to_mesh(0.5)
part = part.named("lattice mount", "#3a8fb7")
print(part, f"\n  {part.volume / 1000:.1f} cm³, tags {part.tags}, closed: {part.is_closed} ({time.time() - t0:.1f} s)")
outputs.part("lattice-mount", part)

# FEA: bolts fixed, 80 N down on the pin (PETG)
r = fea.static(part, "PETG", fixed="bolt", loads=[fea.force("pin", (0, 0, -80))])
print(r)
r.show("lattice-mount-fea")

outputs.plate("lattice-mount-plate", [part], settings={"walls": 3, "infill": 100})
print(f"done in {time.time() - t0:.0f} s")
