# tule (Python)

Pythonic solid modeling on OpenCascade, built for agents: immutable shapes,
operators, 2D profiles, B-rep features, and selections that survive later
operations.

```sh
pip install -e python        # needs cadquery-ocp (OCP 7.8+ or 8)
python -m pytest python      # the test suite
```

```python
from tule import *

plate = rect(80, 40, center=True).round(5).extrude(6)
plate = plate.tag("mount", plate.faces["bottom"])          # tags survive every later operation
boss = circle(9).extrude(12).at(0, 0, 6)

part = (plate | boss).fillet(boss.edges["bottom"], 2)      # the boss's bottom edge, found after the union
part -= cylinder(3.2, 50, center=True)                     # through hole
part = part.shell(part.faces["mount"], 1.5)                # open from below, 1.5 mm walls
part = part.tag("load", part.faces.top()).named("bracket", "#4a8")

outputs.part("bracket", part)                              # outputs/bracket/bracket.step (+ output.json)
```

## Shapes

- Primitives: `box(x, y=x, z=x, center=False)`, `cube`, `cylinder(r, h, center=False, r2=None)`,
  `cone(r, h, r2=0)`, `sphere(r)`, `torus(R, r)`, `wedge(dx, dy, dz, ltx)`,
  `polyhedron(points, faces)`.
- Booleans: `a | b` (also `+`, `sum([...])`), `a - b`, `a & b`, `a ^ b`; `union(*s)`,
  `difference(a, *s)`, `intersection(*s)`. Coplanar faces are merged after each Boolean.
- Transforms return new shapes: `at(x, y, z)` / `translate`, `rotate(x, y, z, about=)` (degrees,
  x then y then z), `rotate_about(axis, deg, origin)`, `scale(f)` or `scale(fx, fy, fz)`,
  `mirror(nx, ny, nz, origin=)`, `transform(matrix)`.
- Features: `fillet(edges, r)`, `chamfer(edges, d)`, `shell(faces_to_remove, thickness)`.
- Metadata: `named(name, color)`, `colored(color)`; `Assembly(*parts, name=)` with `parts()`,
  `a["name"]`, `at/rotate/mirror`.
- Measure: `volume`, `area`, `bounds`, `size`, `center_of_mass`, `mass(density)`, `is_valid`.

## Profiles and sweeps

- `polygon(points, holes=())` (a point `(x, y, r)` rounds that corner), `rect(w, h, center=)`,
  `circle(r)`, `ellipse(rx, ry)`, `regular_polygon(n, r)`, `slot(length, width)`.
- 2D Booleans `| - & ^`, `round(r)`, `chamfer(d)`, `at`, `rotate`, `scale`, `mirror`, `area`, `bounds`.
- `extrude(p, h, center=False, twist=0, scale=1)` (or `p.extrude(h)`), `revolve(p, angle=360)`
  (the profile's x is the radius, y becomes z), `loft(profiles, heights, ruled=True)`.

## Selecting faces and edges

`shape.faces`, `shape.edges` and `shape.vertices` are selections. Narrow them with queries:

```python
part.faces.planar().normal(+Z)      # planar faces facing up
part.faces.cylindrical(r=3.2)       # the hole wall
part.faces.top()                    # highest face(s) along +Z (any axis: top(X))
part.edges.parallel(Z)              # straight vertical edges
part.edges.circles(r=9)
part.edges.on(part.faces.top())     # edges bounding the top face
part.edges.longest(), .within(lo, hi), .nearest(point), .sort_by(axis)
a | b, a & b, a - b                 # combine selections
sel.ids, sel.info()                 # stable ids (f12, e3) and facts, for agents
```

**Roles.** Primitives and sweeps name their faces: `box(...).faces["top"]` (and `bottom`,
`left`, `right`, `front`, `back`), cylinders and extrusions `top`, `bottom`, `side`, revolves
`top`, `bottom`, `side` (`start`, `end` for partial angles), fillets `fillet`, chamfers
`chamfer`, shells `inner`. `edges["top"]` gives the edges of that face group. Roles belong to
the shape that made them and carry through unary operations, but not through Booleans: after
`part = plate | boss`, use `boss.edges["bottom"]` and tule finds those edges in `part` through
OpenCascade's history. If they were consumed, you get an error saying so.

**Tags** are names you choose, and they survive every operation, including Booleans and
STEP export/import: `s.tag("load", s.faces.top())`, then `part.faces["load"]`.

## Errors

`TuleError` carries `op`, `faces`, `edges`, `hint` and data such as `max_radius`:

```
fillet failed: could not fillet with 7; edges e0, …, e11; largest radius that works ≈ 5
```

## Files

- `export_step(shape_or_assembly, path)` writes names, colors and tags (as named faces);
  `import_step(path)` reads them back (a Shape, or an Assembly).
- `export_stl` (binary), `export_gltf` (.glb, one colored mesh per part).
- `tessellate(shape)` → `Mesh(positions, normals, indices, face_ids, edges)`: normals from the
  surfaces, a B-rep face id per triangle, B-rep edges as polylines (seams skipped).

## Outputs for the viewer

`tule.outputs` writes `outputs/<name>/` folders the viewer renders (see DESIGN.md):

```python
outputs.part("bracket", part)
outputs.assembly("gripper", gripper)
with outputs.sequence("bracket-topopt", "Topology optimization") as seq:
    for i, (verts, tris, c) in enumerate(steps):
        seq.add(mesh=(verts, tris), label=f"iteration {i}", compliance=c)
```

The folder is `$TULE_OUTPUTS`, else `/workspace/outputs` when `/workspace` exists, else
`./outputs`. Files are written atomically and `output.json` last.
