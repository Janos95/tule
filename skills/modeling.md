# Modeling with `tule`

`tule` is a thin Python layer over OpenCascade: immutable values, selection queries, tags that survive every operation.

```python
from tule import *

plate = rect(80, 40, center=True).round(5).extrude(6)
plate = plate.tag("mount", plate.faces["bottom"])      # tags survive later operations
boss = circle(9).extrude(12).at(0, 0, 6)
part = (plate | boss).fillet(boss.edges["bottom"], 2)  # the boss's edge, found after the union
part -= cylinder(3.2, 50, center=True)                 # through hole
part = part.tag("load", part.faces.top()).named("bracket", "#4a8")
print(part, part.tags, part.size, round(part.mass(0.0027), 1), "g")
outputs.part("bracket", part)
```

## Style

- Operations return new values; nothing changes in place. Units: mm.
- Parameters are variables at the top of the script (shared ones in `lib/params.py`); derive the rest.
- `from tule import *` gives everything below; `tule docs Shape.fillet` documents a call.

## Building blocks

- Solids: `box(x, y, z, center=)`, `cube`, `cylinder(r, h, center=)`, `cone(r, h, r2)`, `sphere(r)`, `torus(R, r)`, `wedge`, `polyhedron(points, faces)`.
- Profiles (xy plane): `rect(w, h, center=)`, `circle(r)`, `ellipse`, `regular_polygon(n, r)`, `slot(length, width)`, `polygon([(x, y), (x, y, r), ...])` (a third value rounds that corner), `spline(points, tol=)` (a smooth outline), `text("MK4", size, bold=)`. Combine with `| - & ^`; `.round(r)`, `.chamfer(d)`, `.at(x, y)`, `.rotate(deg)`, `.mirror()`.
- Sweeps: `profile.extrude(h, center=, twist=, scale=)`, `profile.revolve(angle)` (x: radius, y: z), `loft([p1, p2], heights=[0, 30])`, `sweep(profile, [(x, y, z), …])` along a smooth 3D path (faces `start`, `end`), `pipe(points, r, wall=)` for tendons, cables, tubes; points `(x, y, z, r)`: straight, that corner an arc of radius r (a pulley).
- Booleans: `a | b` union, `a - b` difference, `a & b` intersection; `union(a, b, c)`.
- Transforms: `.at(x, y, z)`, `.rotate(x=, y=, z=)` (degrees), `.mirror(nx, ny, nz)`, `.scale(f)`.
- Features: `.fillet(edges, r)`, `.chamfer(edges, d)`, `.shell(faces_to_remove, thickness)`.
- Measure: `.volume`, `.area`, `.bounds`, `.size`, `.center_of_mass`, `.mass(density)`, `.is_valid`, `a.distance(b)` (0: touching), `a.closest(b)`: `(gap, pa, pb)`.
- Clearance: `r = clearance(frames, contacts=[("pin", "link")])`: an assembly or each pose of a motion (Assemblies, or their `outputs.sequence`); each overlap, or gap under `below=0.5` mm, exact at its worst frame. `print(r)`, `r.show("arm-clearance")`, `assert r.ok`. Gears: step by a fraction of a tooth.
- Gears: `spur_gear(module, teeth, width, bore=, backlash=)`, `internal_gear(...)`; `pair = gear_pair(module, z1, z2)` checks interference; `pair.place(gear2, angle=)` meshes gear 2 at `pair.distance`. Bevel: `pair = bevel_pair(module, z1, z2, width, shaft_angle=90)` meshes `pair.gear1`, `pair.gear2`; `pair.place(bevel_gear(..., mating_teeth=z1), turn=)`. Cycloidal: `d = cycloidal_drive(pins, pin_circle_d, pin_d, eccentricity, width)` has `d.disc` (pins − 1 lobes), `d.pins`, `d.place(d.disc, crank_angle)`.
- Sheet metal (flanges, flats): `tule docs sheetmetal`.
- Parts: `.named("name", "#4a8")`, `Assembly(a, b, name=)`; files: `export_step`, `export_stl`, `import_step`.

## Selecting faces and edges

Select with queries on the current shape, never by index:

```python
from tule import *

part = box(60, 40, 20, center=True)
part = part.fillet(part.edges.parallel(Z), 4)  # the four vertical edges
part -= cylinder(3, 50, center=True).at(20, 10, 0)
top = part.faces.planar().normal(+Z)           # planar faces facing up
hole = part.faces.cylindrical(r=3)             # by radius
rim = part.edges.circles(r=3).top()            # the hole's top edge
print(top.ids, hole.info())                    # stable ids (f12 in the viewer) and facts
part = part.chamfer(rim, 0.5)                  # ids change: select again after each operation
```

Queries: `planar()`, `cylindrical(r=)`, `curved()`, `normal(dir)`, `largest(n)`, `top()`/`bottom()` (along an axis), `within(lo, hi)`, `nearest(point)`, `sort_by(axis)`, `filter(fn)`; edges also `lines()`, `circles(r=)`, `parallel(axis)`, `perpendicular(axis)`, `on(faces)`, `longest()`. Combine with `| & -`. `part.face(12)` is `f12` in `tule screenshot --labels` and `tule inspect`.

## Roles and tags

- **Roles**: primitives and features name faces: `box(...).faces["top"]` (`top bottom left right front back`), `side`, `fillet`, `chamfer`, shell `inner`. After a Boolean, `part.faces["top"]` is not defined.
- **Tags** are names you give: `part.tag("load", part.faces.top())`, then `part.faces["load"]`. They flow through every operation into STEP files, the viewer (`f12 · load`) and analysis. Tag loads, fixed and mating faces.

## Drawings

`outputs.drawing("plate", part)` writes a shop drawing (PDF and SVG, `dxf=` for cutting) with views, overall sizes and hole, counterbore and slot callouts from the model. Don't hand-draw one; `tule docs drawings`: dimensions, tolerances, sections, scale.

## Errors

Failures raise `TuleError` with `op`, `faces`, `edges`, `hint` and data such as `max_radius`:

```python
from tule import *

part = box(20, 20, 4)
try:
    part = part.fillet(part.edges.parallel(Z), 15)
except TuleError as e:
    print(e)  # …; largest radius that works ≈ 9.99
    part = part.fillet(part.edges.parallel(Z), 0.9 * e.data["max_radius"])
```

Read the message, change the input, rerun (`tule docs occt`).
