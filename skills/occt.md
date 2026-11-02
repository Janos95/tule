# OpenCascade pitfalls and how errors read

`tule` runs on OpenCascade (OCCT, through OCP). Most failures have a few causes; this is what they look like and what to do.

## How errors read

Every failure is a `TuleError`: `<op> failed: <what>; edges e3, e7; faces f2; <hint>`. Its fields: `e.op`, `e.edges`, `e.faces` (ids as in `tule screenshot --labels`), `e.hint`, `e.data` (e.g. `max_radius`), `e.to_json()`. Look before changing anything: `tule screenshot --labels` on the last good output shows those face ids; `part.edges.info()` gives each edge's id, type, length and center.

## Fillets and chamfers

- **Too big**: the radius must fit the faces next to the edge. The error gives the largest radius that works (`e.data["max_radius"]`); use less, or fillet fewer edges.
- **Edges meeting at a corner** with different radii, or a mix of convex and concave edges at one vertex, often fail together: fillet them in separate calls, biggest radius first.
- **Order**: fillet after the Booleans that create the edges, and select the edges on the feature that made them (`boss.edges["bottom"]`) or by query on the current shape.
- **Fillet, then shell**: shelling a filleted body fails when a fillet radius is smaller than the wall thickness (the inner offset collapses). Shell first, or make fillets larger than the wall.

## Booleans

- **Coincident faces** (two faces in the same plane, a cutter ending exactly at a face) are the classic failure or produce slivers. Make cutters pass through: `cylinder(3.2, 50, center=True)` instead of exactly the part's height; overlap unions by a little (0.01 to 1 mm) instead of touching.
- **Tangent contact** (a cylinder touching a plane along a line) is fragile: overlap or separate.
- Inputs must be valid solids: check `a.is_valid` and `len(a.solids)`. A result with 2 solids means the parts did not touch.
- The result is simplified (coplanar faces merged), so face ids change after every operation: select again, or tag before.

## Tolerances and scale

- Model in millimetres at real size; features under about 0.001 mm (OCCT's precision is 1e-7, its fuzzy defaults around 1e-4) or parts over a few kilometres misbehave.
- Avoid nearly equal dimensions that should be equal: compute them from one parameter.
- `part.is_valid` false after an operation: the last operation is the culprit; change its inputs rather than continuing.

## Imported STEP files

Supplier files may be shells, loose faces or slightly open. Check `part.is_valid`, `len(part.solids)`, `part.volume > 0` (and `tule inspect file.step`). Sew and fix with OCP directly:

```python
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeSolid, BRepBuilderAPI_Sewing
from OCP.ShapeFix import ShapeFix_Shape
from OCP.TopAbs import TopAbs_SHELL
from OCP.TopExp import TopExp_Explorer
from OCP.TopoDS import TopoDS
from tule import *

loose = import_step("outputs/bracket/bracket.step")      # your file in inputs/
sew = BRepBuilderAPI_Sewing(1e-3)                         # close gaps up to 0.001 mm
sew.Add(loose.wrapped)
sew.Perform()
solid = BRepBuilderAPI_MakeSolid()
shells = TopExp_Explorer(sew.SewedShape(), TopAbs_SHELL)
while shells.More():
    solid.Add(TopoDS.Shell(shells.Current()))
    shells.Next()
fix = ShapeFix_Shape(solid.Solid())
fix.Perform()
part = Shape(fix.Shape())                                 # names and tags are not carried over
print(part.is_valid, len(part.solids), round(part.volume, 1))
```

## Seam edges and periodic faces

A full cylinder, cone, sphere or torus is one face closed onto itself along a seam edge. `tule` never selects seam edges (they are not real edges: filleting one fails), and the viewer does not draw them. A hole's rim is `part.edges.circles(r=...)`, its wall `part.faces.cylindrical(r=...)`.

## When to drop to OCP

`part.wrapped` is the `TopoDS_Shape`; `Shape(topods_shape)` wraps one back (without tags). In OCP, static methods mostly carry an `_s` suffix (`BRepGProp.VolumeProperties_s`), a few do not (`TopoDS.Shell`); `dir()` tells. build123d's source is a good reference for OCCT edge cases.
