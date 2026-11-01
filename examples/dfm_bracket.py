"""DFM checks of a bracket: a plate with a pocket, a deep slot, bolt holes and a cross hole.

Checks it for 3-axis milling from above and for FDM printing, writes the findings as overlays
(outputs/bracket-cnc, outputs/bracket-fdm), then applies the fixes the CNC report asks for and checks again.
"""
from tule import *


def bracket(corner: float = 0.0, cross_hole: bool = True, slot_width: float = 4.0):
    plate = rect(80, 50, center=True).round(5).extrude(20)
    plate = plate.tag("mount", plate.faces["bottom"])
    pocket = rect(50, 26, center=True)
    slot = rect(slot_width, 20, center=True)
    if corner:
        pocket = pocket.round(corner)
        slot = slot.round(min(corner, slot_width / 2 - 1e-3))
    part = plate - pocket.extrude(8).at(0, 0, 12)                         # a pocket, 8 deep
    part -= slot.extrude(18).at(-33, 0, 2)                                # a narrow slot, 18 deep
    part -= cylinder(2.5, 50, center=True).at(33, 18, 0)                  # bolt holes, Ø5
    part -= cylinder(2.5, 50, center=True).at(33, -18, 0)
    if cross_hole:
        part -= cylinder(3, 100, center=True).rotate(y=90).at(0, 0, 5)    # a cross hole along X
    part = part.tag("pocket", part.faces.planar().normal(+Z).within((-25, -13, 11.9), (25, 13, 12.1)))
    return part.named("bracket", "#4a8")


part = bracket()
cnc = dfm.cnc(part, axis=-Z, tool_radius=3, min_wall=1.0)
print(cnc, "\n")
outputs.overlay("bracket-cnc", part, cnc, title="CNC 3-axis from above, Ø6 end mill")

fdm = dfm.fdm(part, up=+Z, max_overhang=45, min_wall=0.8, max_bridge=10)
print(fdm, "\n")
outputs.overlay("bracket-fdm", part, fdm, title="FDM, built along +Z")

# what the CNC report asks for: corners rounded above the tool radius, a slot wide enough for the
# tool, no cross hole from this side; the bolt holes stay (drilled)
better = bracket(corner=3.3, cross_hole=False, slot_width=8)
print(dfm.cnc(better, axis=-Z, tool_radius=3, min_wall=1.0))
