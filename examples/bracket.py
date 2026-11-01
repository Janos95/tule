"""The bracket from DESIGN.md: a rounded plate, a filleted boss, a through hole, hollowed from below."""
from tule import *

plate = rect(80, 40, center=True).round(5).extrude(6)
plate = plate.tag("mount", plate.faces["bottom"])          # tags survive every later operation
boss = circle(9).extrude(12).at(0, 0, 6)

part = (plate | boss).fillet(boss.edges["bottom"], 2)      # the boss's bottom edge, found after the union
part -= cylinder(3.2, 50, center=True)                     # through hole
part = part.shell(part.faces["mount"], 1.5)                # open from below, 1.5 mm walls
part = part.tag("load", part.faces.top()).named("bracket", "#4a8")

print(part, "tags:", part.tags, "valid:", part.is_valid)
print("mass in aluminium (g):", round(part.mass(0.0027), 1))
outputs.part("bracket", part)
