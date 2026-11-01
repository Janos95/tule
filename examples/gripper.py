"""A two-finger parallel gripper as an assembly: the viewer's Parts panel lists
its parts (base, carriage, the jaws with their pads), and hides, isolates or
highlights them. Run it on a Tule machine; it writes outputs/gripper/."""
from tule import *

# the base plate with four mounting holes and a slot the jaws ride in
base = rect(90, 40, center=True).round(4).extrude(8)
for x in (-35, 35):
    for y in (-12, 12):
        base -= cylinder(2.2, 30, center=True).at(x, y, 0)
base -= box(70, 8, 4, center=True).at(0, 0, 7)

# the carriage (actuator body) on top
carriage = box(36, 28, 16, center=True).at(0, 0, 16)
carriage = carriage.fillet(carriage.edges.parallel(Z), 3)

def jaw(side: int) -> Assembly:
    """One jaw: a finger standing up from the slot, and a rubber pad on its inner face."""
    finger = box(10, 24, 44, center=True).at(side * 26, 0, 30)
    finger = finger.fillet(finger.edges.parallel(Y), 1.5)
    pad = box(3, 20, 18, center=True).at(side * (26 - 6.5), 0, 40)
    name = "left jaw" if side < 0 else "right jaw"
    return Assembly(finger.named("finger", "#5b7fa6"), pad.named("pad", "#2b2b2b"), name=name)

gripper = Assembly(
    base.named("base", "#9aa4ae"),
    carriage.named("carriage", "#d0893b"),
    jaw(-1),
    jaw(+1),
    name="gripper",
)

for path, part in gripper.parts():
    print(f"{path:<28} volume {part.volume:8.1f} mm³")
outputs.assembly("gripper", gripper, title="Parallel gripper, 2 jaws")
