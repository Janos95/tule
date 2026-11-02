"""Start here: src/part.py makes outputs/part/ (the viewer shows it). Run it with `tule build`."""
from tule import *

plate = rect(80, 40, center=True).round(5).extrude(6)
boss = circle(9).extrude(12).at(0, 0, 6)
part = (plate | boss).fillet(boss.edges["bottom"], 2)
part -= cylinder(3.2, 50, center=True)

print(part, "volume:", round(part.volume, 1))
outputs.part("part", part.named("part", "#4a8"))
