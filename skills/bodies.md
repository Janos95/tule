# Bodies: B-rep, meshes, implicits

A part is one of three kinds (`.kind`). Each keeps its own operators; nothing converts by itself.

| kind | made by | good for | loses |
|---|---|---|---|
| `Shape` "brep" | `box`, `cylinder`, sketches, STEP | exact faces, fillets, drawings, STEP | — |
| `Mesh` "mesh" | `shape.to_mesh(tol)`, `body.to_mesh(cell)`, `Mesh.load("x.ply")`, topopt | anything you have triangles of; prints | exact surfaces: PLY/STL/3MF out, no STEP |
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

### Revolved parts, repeats, walls, tubes, big parts

```python
from tule import sdf

bell = sdf.spline([(0, 0), (60, 0), (60, 40), (25, 110), (0, 110)]).revolve()   # a 2D profile (u = radius, v = z)
wall = sdf.Wall([0, 300, 420, 900], [220, 220, 130, 420], samples=8)           # a revolved contour r(z)
liner = wall.solid(0, 6)                                                       # 6 mm out from the contour
cooled = liner - wall.channels(200, land=1.0, depth=[(0, 3), (420, 2), (900, 4)], offset=1.0)  # narrow at the throat
bolts = sdf.cylinder(4, 12).at(250, 0, 0).repeat_polar(96)                     # 96 bolts, one copy's expression
duct = sdf.tube([(0, 0, 0), (0, 0, 200), (150, 0, 300)], r=30, wall=3, bend=80)  # hollow, open, a bent pipe
m = cooled.to_mesh(4, fine=[((-500, -8, -10), (500, 8, 910), 0.5)], rest=liner)  # 0.5 mm only in a band at y = 0
```

- 2D: `sdf.polygon(points)`, `sdf.spline(points)` (closed; open with `width=`), `sdf.polyline(points, width)`, `sdf.rect(w, h, radius=)`, `sdf.circle(r)`; booleans, `.offset`, `.shell`, `.at(u, v)`, `.rotate(deg)`; then `.revolve(degrees=360)` or `.extrude(h)`. `sdf.wedge(deg)` cuts a sector.
- `body.repeat_polar(n)` (model the copy on +x), `body.repeat_linear(step, n)`: domain repetition, one expression however many; `overlap=True` when copies reach into their neighbours' cells.
- `sdf.Wall(z, r)`: coordinates that follow a revolved contour (`.n` normal offset, `.s` length along it, `.t` distance around, `.theta`); `.solid(inner, outer)`, `.channels(count, width= or land=, depth, offset)`, `.ribs(count, width, height, base)`, `.bands(at, width, height)`. Values: a number, a table `[(z, v), …]` or a function of z.
- `sdf.tube(points, r, wall=, bend= or spline=True, ends="flat")`: ducts, manifolds, lines; `r` per point tapers.
- Fine detail on a big part: `to_mesh(cell, fine=[(lo, hi, cell), …], rest=body_without_the_detail)` meshes boxes at their own cell (closed pieces that meet face to face). A section through the band shows the detail: the viewer's **Section** button (or `?section=y:0`), `tule screenshot --section y:0`.

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
- `Mesh.load("scan.ply")` (PLY, STL, OBJ), `Mesh(points, triangles)`; `.export_ply` (Tule's mesh format: keeps regions, name, color), `.export_stl` (slicers), `.export_3mf`.
- `.simplify(tol)`, `.refine(edge)`, `.pieces()`, `.at/.rotate/.scale/.mirror`, `| - &`.
- The viewer draws only feature edges of a mesh; `outputs.picture`, plates, assemblies take meshes like shapes.

## FEA on meshes

`fea.static(mesh, ...)` takes a closed mesh with tags as for a shape. `mesh="auto"` uses voxels for meshes over 20,000 triangles (gmsh tetrahedra on a fine Fidget mesh take minutes); `mesh="tet"` asks for them anyway. An implicit is meshed first: `fea.static(body.to_mesh(cell), ...)`.

## Many copies

A part repeated (tiles, engines, fasteners): `part.placed(...)` stores, meshes and draws it once. Any kind; an implicit's copies `.to_mesh(cell)` first.

```python
import numpy as np
from tule import *

tile = regular_polygon(6, 10).extrude(3).named("tile", "#333")
a = np.radians(np.arange(0, 360, 5))
tiles = tile.placed(at=np.c_[100 * np.cos(a), 100 * np.sin(a), 0 * a], rotate=np.c_[0 * a, 0 * a, np.degrees(a)])
fins = box(30, 4, 60).placed(rotate=[(0, 0, k) for k in (0, 90, 180, 270)]).named("fins")
outputs.assembly("ring", Assembly(tiles.named("tiles"), fins))
```

`placed(T)` takes (N, 4, 4) matrices; `at=` (N, 3), `rotate=` degrees as `part.rotate` (one row for all). `.at/.rotate` move every copy; `bounds`, `volume`, `len`; `asm.parts()` lists each copy (`tiles[3]`). `output.json`: `"transforms"`, a list of 16 numbers each, or a `.npy` for many.
