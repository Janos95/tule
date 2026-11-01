"""Topology optimization of a wall bracket: two bolt holes on the left, a pin hole on the right pulled down.

Writes outputs/bracket-bc (the setup as an overlay), outputs/bracket-topopt (a sequence, one frame per
iteration) and outputs/bracket-optimized (the smoothed result, STL). About 20,000 voxels.
"""
import time

from tule import *

ROT = dict(x=90)                                     # holes run along Y: make them along Z, then turn them

domain = box(120, 30, 60)
bolts = [(12, 12), (12, 48)]                        # (x, z) of the two bolt holes
pin = (108, 30)
for x, z in bolts + [pin]:
    domain -= cylinder(5, 40, center=True).rotate(**ROT).at(x, 15, z)
domain = domain.tag("bolt", domain.faces.cylindrical(r=5).within((0, 0, 0), (30, 30, 60)))
domain = domain.tag("pin", domain.faces.cylindrical(r=5).within((90, 0, 0), (120, 30, 60)))

rings = [(cylinder(10, 30, center=True) - cylinder(5, 40, center=True)).rotate(**ROT).at(x, 15, z) for x, z in bolts + [pin]]

problem = topopt.Problem(
    domain,
    fixed=domain.faces["bolt"],
    loads={domain.faces["pin"]: (0, 0, -500)},       # 500 N down on the pin
    volume_fraction=0.2,
    keep=rings,                                      # material around the holes stays
)
problem.show("bracket-bc")

t = time.time()
result = topopt.optimize(problem, output="bracket-topopt", verbose=True)
print(result)
outputs.part("bracket-optimized", result.mesh)
try:
    solid = result.to_shape()
    print("solid:", solid, "valid:", solid.is_valid, f"({time.time() - t:.1f} s in all)")
except TuleError as e:
    print("no solid:", e)
