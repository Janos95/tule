# Implicit parts: revolved profiles, repeats, walls, tubes, big parts

Building blocks in `tule.sdf` for turned and ducted parts (nozzles, manifolds, heat exchangers) and for fine detail inside a big part. Basics of implicit bodies: `tule docs bodies`.

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

- Shading: `to_mesh` stores the field's exact normals and the viewer uses them. A mesh changed afterwards (`simplify`) loses them: `m = m.simplify(0.1).refine(10 * cell)` (long slivers streak), then `Mesh(m.points, m.indices, normals=body.normals(m.points))`. For outlines meant to be smooth, `sdf.spline(...).revolve()` rather than a polygon, whose corners show.
