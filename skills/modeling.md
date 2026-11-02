# Modeling with `tule`

`tule` is a thin, opinionated Python layer over OpenCascade: immutable values, operators, selection queries, and tags that survive every operation.

```python
from tule import *

plate = rect(80, 40, center=True).round(5).extrude(6)
plate = plate.tag("mount", plate.faces["bottom"])        # tags survive later operations
boss = circle(9).extrude(12).at(0, 0, 6)
part = (plate | boss).fillet(boss.edges["bottom"], 2)    # the boss's edge, found after the union
part -= cylinder(3.2, 50, center=True)                   # through hole
part = part.tag("load", part.faces.top()).named("bracket", "#4a8")
print(part, part.tags, part.size, round(part.mass(0.0027), 1), "g")
outputs.part("bracket", part)
```

## Style

- Every operation returns a new value; nothing is modified in place. Units are yours (mm by convention).
- Parameters are plain Python variables at the top of the script (shared ones in `lib/params.py`); derive everything else from them.
- `from tule import *` gives everything below; `help(box)` documents each call.

## Building blocks

- Solids: `box(x, y, z, center=)`, `cube`, `cylinder(r, h, center=)`, `cone(r, h, r2)`, `sphere(r)`, `torus(R, r)`, `wedge`, `polyhedron(points, faces)`.
- Profiles (xy plane): `rect(w, h, center=)`, `circle(r)`, `ellipse`, `regular_polygon(n, r)`, `slot(length, width)`, `polygon([(x, y), (x, y, r), ...])` (a third value rounds that corner). `text("MK4", size, bold=, align=)` for lettering (extrude it, or cut it to engrave). Combine with `| - & ^`; `.round(r)`, `.chamfer(d)`, `.at(x, y)`, `.rotate(deg)`, `.mirror()`.
- Sweeps: `profile.extrude(h, center=, twist=, scale=)`, `profile.revolve(angle)` (x is the radius, y becomes z), `loft([p1, p2], heights=[0, 30])`.
- Booleans: `a | b` union, `a - b` difference, `a & b` intersection; `union(a, b, c)` for many.
- Transforms: `.at(x, y, z)`, `.rotate(x=, y=, z=)` (degrees), `.mirror(nx, ny, nz)`, `.scale(f)`.
- Features: `.fillet(edges, r)`, `.chamfer(edges, d)`, `.shell(faces_to_remove, thickness)`.
- Measure: `.volume`, `.area`, `.bounds`, `.size`, `.center_of_mass`, `.mass(density)`, `.is_valid`, `a.distance(b)` (the gap; 0 if touching). `assembly.clashes(ignore=[("pin", "link")])` lists overlapping parts `(a, b, mm³)`: run it on every frame of a motion study.
- Parts: `.named("name", "#4a8")`, `Assembly(a, b, name=)`; files: `export_step`, `export_stl`, `import_step`.

## Selecting faces and edges

Select with queries on the current shape, never by index guesses:

```python
from tule import *

part = box(60, 40, 20, center=True)
part = part.fillet(part.edges.parallel(Z), 4)                 # the four vertical edges
part -= cylinder(3, 50, center=True).at(20, 10, 0)
top = part.faces.planar().normal(+Z)                          # planar faces facing up
hole = part.faces.cylindrical(r=3)                            # by radius
rim = part.edges.circles(r=3).top()                           # the hole's top edge
print(top.ids, hole.info())                                   # stable ids (f12 in the viewer) and facts
print(part.edges.parallel(X).longest(), part.faces.nearest((30, 0, 0)))
part = part.chamfer(rim, 0.5)                                 # ids change: select again after each operation
```

Queries: `planar()`, `cylindrical(r=)`, `curved()`, `normal(dir)`, `largest(n)`, `top()`/`bottom()` (along an axis), `within(lo, hi)`, `nearest(point)`, `sort_by(axis)`, `filter(fn)`; edges also `lines()`, `circles(r=)`, `parallel(axis)`, `perpendicular(axis)`, `on(faces)`, `longest()`. Combine with `| & -`. Seam edges are never selected. `part.face(12)` is face `f12` as shown by `tule screenshot --labels` and `tule inspect`.

## Roles and tags

- **Roles**: primitives and features name their faces: `box(...).faces["top"]` (`top bottom left right front back`), `cylinder(...).faces["side"]`, extrusions `top/bottom/side`, `fillet`, `chamfer`, shell `inner`. A role resolves through later operations when you select on the feature itself: `part.fillet(boss.edges["bottom"], 2)`. After a Boolean, `part.faces["top"]` is not defined (it would be ambiguous).
- **Tags** are names you give: `part.tag("load", part.faces.top())`, then `part.faces["load"]`. They flow through every operation, are written into STEP files, show in the viewer (`f12 · load`) and are what analysis (`tule.topopt`) reads. Tag what matters: loads, fixed faces, mating faces, cosmetic faces.

## Errors

Failures raise `TuleError` with `op`, `faces`, `edges`, `hint` and data such as `max_radius`:

```python
from tule import *

part = box(20, 20, 4)
try:
    part = part.fillet(part.edges.parallel(Z), 15)
except TuleError as e:
    print(e)                                                  # fillet failed: could not fillet with 15; edges e…; largest radius that works ≈ 9.99
    part = part.fillet(part.edges.parallel(Z), 0.9 * e.data["max_radius"])
```

Read the message, change the input, rerun. See `tule docs occt` for what usually goes wrong.
