# Bodies: B-rep, meshes, implicits

A part is one of three kinds (`.kind`). Each keeps its own operators; nothing converts by itself.

| kind | made by | good for | loses |
|---|---|---|---|
| `Shape` "brep" | `box`, `cylinder`, sketches, STEP | exact faces, fillets, drawings, STEP | — |
| `Mesh` "mesh" | `shape.to_mesh(tol)`, `body.to_mesh(cell)`, `Mesh.load("x.stl")`, topopt | anything you have triangles of; prints | exact surfaces: STL/3MF out, no STEP |
| `sdf.Implicit` "implicit" | `tule.sdf`: fields, lattices, blends | organic shapes, TPMS lattices, smooth unions, shells | nothing until meshed |

`a | b` of two kinds raises and says which conversion to write. Convert on purpose, once, where it is stated:

- `shape.to_mesh(0.01)`: triangles within 0.01 mm of the faces; faces become regions, tags carry over.
- `body.to_mesh(cell)`: Fidget meshes the field (detail under a cell is lost; sharp edges keep). Default cell from the bounds.
- `shape.to_implicit(cell)`, `mesh.to_implicit(cell)`: a sampled signed distance (edges round to a cell).
- `mesh.to_brep()`: one planar face per triangle (up to 20,000): for STEP from a simple mesh, not for fillets.

## Implicit bodies

```python
from tule import sdf, outputs

block = sdf.box(40, 30, 12)                            # corner at the origin, like tule.box
boss = sdf.cylinder(8, 20).at(20, 15, 0)
body = block.union(boss, smooth=4) - sdf.cylinder(3, 40).at(20, 15, -5)   # smooth= blends (mm)
lattice = (sdf.box(40, 30, 12) & sdf.gyroid(10, thickness=1.2))          # a 1.2 mm gyroid sheet
shell = sdf.sphere(15).shell(1.5)                      # hollow, 1.5 mm wall (inward)
blob = sdf.field(lambda x, y, z: sdf.length(x, y, z * 1.6) - 10 + 0.8 * sdf.sin(x) * sdf.sin(y),
                 bounds=((-14, -14, -10), (14, 14, 10)))
m = body.tag("mount", lambda f: f.bottom()).to_mesh(0.4)   # tags on an implicit: a function of the mesh's faces
print(m)
outputs.part("blended", m)
```

Drawing a part from stations, as a loft drawing does: `sdf.field(lambda x, y, z: ..., bounds)` with `sdf.interp(x, stations, values)` (a station table), `sdf.smooth_min(a, b, r)` / `smooth_max` (fillets on raw fields), `sdf.slab(x, lo, hi)` (a station range). `examples/blackbird.py` builds an SR-71 that way: 26 parts, one assembly.

Primitives: `box`, `cube`, `sphere`, `cylinder`, `capsule`, `torus`, `plane`; `gyroid`, `schwarz_p`, `diamond(period, thickness)` (without thickness: a solid half, e.g. `.sheet(t)` later). Math on `sdf.x, y, z`: `+ - * /`, `abs`, `sdf.sin/cos/sqrt/exp/min/max/clamp/mix/length`. Booleans `| - &`, or `.union/.difference/.intersection(..., smooth=r)`. `.offset(d)`, `.shell(t)`, `.sheet(t)`, `.clip(lo, hi)`, `.at`, `.rotate`, `.scale`, `.mirror`. `.value(points)`, `.contains(points)`.

## Meshes

```python
from tule import *

plate = box(60, 40, 6)
plate = plate.fillet(plate.edges.parallel(Z), 4).to_mesh(0.02)    # B-rep -> mesh, 0.02 mm
lattice = (sdf.box(50, 30, 20) & sdf.gyroid(8, 1.0)).at(5, 5, 6).to_mesh(0.4)
part = (plate | lattice).named("lattice block", "#4a8")       # manifold3d: always closed, regions kept
part = part.tag("base", part.faces.bottom())
print(part.faces.top().area, part.faces.normal((1, 0, 0)).ids, part.volume, part.is_closed)
outputs.part("lattice_block", part)
outputs.plate("print", [part])
```

- Regions: a B-rep face, or triangles between creases (35°) on a plain mesh. `mesh.faces` selects them: `.top()`, `.bottom()`, `.normal(dir)`, `.within(lo, hi)`, `.nearest(p)`, `.largest(n)`, `.where(lambda c, n: ...)` (triangle centers, normals), `| & -`. A tag cuts regions so it is always whole regions.
- `Mesh.load("scan.stl")` (STL, OBJ), `Mesh(points, triangles)`; `.export_stl`, `.export_3mf`.
- `.simplify(tol)`, `.refine(edge)`, `.pieces()`, `.at/.rotate/.scale/.mirror`, `| - &`.
- The viewer draws only feature edges of a mesh; `outputs.picture`, plates, assemblies take meshes like shapes.

## FEA on meshes

`fea.static(mesh, ...)` takes a closed mesh with tags as for a shape. `mesh="auto"` uses voxels for meshes over 20,000 triangles (gmsh tetrahedra on a fine Fidget mesh take minutes); `mesh="tet"` asks for them anyway. An implicit is meshed first: `fea.static(body.to_mesh(cell), ...)`.
