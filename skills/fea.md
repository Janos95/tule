# Stress, deflection, vibration and buckling (`tule.fea`)

Linear analysis of one part: fixed faces and loads from **tags**, a material by name. Units: mm, N, MPa, g, Hz.

```python
from tule import *

part = import_step("outputs/bracket/bracket.step")         # tagged "mount" (bolted down) and "load"
r = fea.static(part, "PETG", fixed="mount",
               loads=[fea.force("load", (0, 0, -30))],      # 30 N in total, spread over the faces
               accel=(0, 0, -3 * fea.g),                    # plus its own weight at 3 g (mm/s²)
               dofs=60_000)                                 # a quick look; the default is 300,000
print(r)
r.show("bracket-fea")                                      # outputs/bracket-fea: stress and displacement
assert r.imbalance < 1e-3 and r.safety_factor > 2
```

```sh
tule screenshot --output bracket-fea -o stress.png
tule screenshot --output bracket-fea -o disp.png --field displacement     # on the shape, exaggerated
```

Read both PNGs. In the viewer, buttons switch fields, hovering shows the value, and a slider sets the exaggeration.

## Reading the numbers

- **Peak**: the highest stress on the surface, and where. At a sharp inside corner or the edge of a clamped face it is a singularity that grows as the mesh shrinks; a peak in a smooth region is real.
- **99.5th percentile**: 0.5 % of the surface area is above it. It holds steady as the mesh changes: judge a design by it. Red on the scale starts there.
- `r.safety_factor` = yield / peak. Printed plastics: aim for 2 to 4 on the percentile. A peak at a sharp corner wants a fillet.
- **Reactions** must balance the loads. If not, a support is missing or a load sits on fixed faces.
- **Convergence**: run again with `dofs` doubled; if the percentile or max displacement moves over 5 %, double again.

## Inputs

- `fixed`: a tag, a selection (`part.faces.cylindrical(r=2.6)`), or a list. Clamped; `fea.fix("base", "z")` holds z only.
- `loads`: `fea.force(faces, (fx, fy, fz))` total N by area; `fea.pressure(faces, p)` MPa into the faces; `fea.moment(faces, (mx, my, mz))` N·mm about their centroid.
- `accel`: mm/s² on the part's own mass. `fea.g` = 9810.
- Materials: `"PLA"`, `"PETG"`, `"ABS"`, `"nylon PA12"`, `"aluminium 6061-T6"`, `"steel S235"`, `"stainless 304"` (aliases `"aluminum"`, `"steel"`), or `fea.Material("PC", E=2400, nu=0.37, density=1.2, yield_strength=60)`.

## The mesh: voxels or tetrahedra

`mesh="auto"` measures the thinnest wall and narrowest slot (`fea.thinnest(part)`, under a second) and picks:
- **voxels** for chunky parts: a grid sized from `dofs` (or `voxel=0.5` mm), cut cells integrated exactly. Fast; CuPy on a GPU.
- **quadratic tetrahedra** (gmsh) when a wall is under 3 voxels or a slot under 2: blades, flexures, ribs, sheet. Faces thinner than the overall size get elements of half their thickness. `mesh="tet"` forces it, `size=0.5` sets the element size. 300,000 DOFs: 10–30 s.

Same result either way: `max_stress`, `stress_p995`, `safety_factor`, `max_displacement`, `reaction`, `mass`, `stress_at(points)`, `displacement_at(points)`, `show()`. On tetrahedra the fields are drawn on the mesh's own surface, so 0.3 mm slots stay open.

## Vibration modes and buckling (tetrahedra)

```python
from tule import *

stage = box(30, 10, 10) | box(0.5, 20, 10).at(2, 10, 0) | box(0.5, 20, 10).at(27.5, 10, 0) | box(30, 10, 10).at(0, 30, 0)
stage = stage.tag("base", stage.faces.nearest((15, 0, 5))).tag("top", stage.faces.nearest((15, 40, 5)))
md = fea.modes(stage, "aluminium", fixed="base", n=4, dofs=30_000)   # default dofs 100,000; needs a density
print(md)                     # Hz, and which way each mode moves the mass (effective mass %)
md.show("stage-modes")        # a field per mode, swinging in the viewer
bk = fea.buckling(stage, "aluminium", fixed="base", loads=[fea.force("top", (0, -100, 0))], dofs=30_000)
print(bk.factor)              # the loads × factor buckle it; inf: none found (say, in tension)
```

`md.frequencies` (Hz) and `md.shapes` ((n, nodes, 3), largest motion 1). Keep the first mode above about 2× any excitation (motor steps, servo bandwidth); a free part has six 0 Hz modes, so fix it. Buckling is linear (Euler) for the perfect part: real parts buckle sooner, so want a factor over 3. If the stress at that load passes yield, it yields first (the printout says so).

## Limits

Linear only: small displacements, no plasticity, no contact. A flexure whose travel closes a slot touches there: compare `max_displacement` with the gap from `fea.thinnest`. Bolted and press-fit joints are modelled as clamps.
