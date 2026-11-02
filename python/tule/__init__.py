"""tule: pythonic solid modeling on OpenCascade, built for agents.

```python
from tule import *

plate = rect(80, 40, center=True).round(5).extrude(6)
boss  = circle(9).extrude(12).at(0, 0, 6)
part  = (plate | boss).fillet(boss.edges["bottom"], 2)
part -= cylinder(3.2, 50, center=True)
outputs.part("bracket", part.named("bracket", "#4a8"))
```
"""

from . import drawing, fea, outputs, topopt
from .assembly import Assembly
from .errors import TuleError
from .gears import GearPair, gear_pair, internal_gear, spur_gear
from .geom import ORIGIN, Vec, X, Y, Z
from .io import Mesh, export_gltf, export_step, export_stl, import_step, tessellate
from .primitives import box, cone, cube, cylinder, polyhedron, sphere, torus, wedge
from .profile import Profile, circle, ellipse, polygon, rect, regular_polygon, slot, text
from .selection import Edges, Faces, Selection, Vertices
from .shape import Shape, difference, intersection, union
from .sweeps import extrude, loft, pipe, revolve, sweep

__version__ = "0.1.0"

__all__ = [
    "Assembly", "Edges", "Faces", "GearPair", "Mesh", "ORIGIN", "Profile", "Selection", "Shape",
    "TuleError", "Vec", "Vertices", "X", "Y", "Z", "box", "circle", "cone", "cube", "cylinder",
    "difference", "drawing", "ellipse", "export_gltf", "export_step", "export_stl", "extrude",
    "fea", "gear_pair", "import_step", "internal_gear", "intersection", "loft", "outputs", "pipe", "sweep",
    "polygon", "polyhedron", "rect", "regular_polygon", "revolve", "slot", "sphere", "spur_gear",
    "tessellate", "text", "topopt", "torus", "union", "wedge",
]
