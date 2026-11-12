# Stress, deflection, vibration and buckling (`tule.fea`)

Analysis of one part: fixed faces and loads from **tags**, a material by name. Units: mm, N, MPa, g, Hz.

```python
from tule import *

part = import_step("outputs/bracket/bracket.step")         # tagged "mount" (bolted down) and "load"
r = fea.static(part, "PETG", fixed="mount",
               loads=[fea.force("load", (0, 0, -30))],      # 30 N in total, spread over the faces
               accel=(0, 0, -3 * fea.g),                    # plus its own weight at 3 g (mm/s²)
               dofs=60_000)                                 # quick; default 300,000
print(r)
r.show("bracket-fea")                                      # outputs/bracket-fea: stress and displacement
assert r.imbalance < 1e-3 and r.safety_factor > 2
```

```sh
tule screenshot --output bracket-fea -o stress.png
tule screenshot --output bracket-fea -o disp.png --field displacement     # on the shape, exaggerated
```

Read both PNGs. In the viewer, buttons switch fields, hover shows values, a slider sets the exaggeration.

## Reading the numbers

- **Peak**: the highest stress on the surface, and where. At a sharp inside corner or the edge of a clamped face it is a singularity that grows as the mesh shrinks; a peak in a smooth region is real.
- **99.5th percentile**: 0.5 % of the surface area is above it; steady as the mesh changes: judge a design by it.
- `r.safety_factor` = yield / peak. Printed plastics: aim for 2 to 4 on the percentile. A peak at a sharp corner wants a fillet.
- **Reactions** must balance the loads. If not, a support is missing or a load sits on fixed faces.
- `r.warnings`: red flags (printed too): an unconverged solve, a singular peak, a point load, a weak support, a large deflection. Resolve them before trusting the numbers.
- **Convergence**: `fea.converge(part, ...)` takes `static`'s arguments, solves at three budgets up to `dofs` and prints the spread; `.converged`, `.result`.

## Inputs

- `fixed`: a tag, a selection (`part.faces.cylindrical(r=2.6)`), or a list. Clamped; `fea.fix("base", "z")` holds z only. `fea.move("top", (2, 0, 0))` drives faces by 2 mm: the force it takes is `r.move_forces`.
- `loads`: `fea.force(faces, (fx, fy, fz))` total N by area (`at=(x, y, z), r=4`: only within r of the point); `fea.pressure(faces, p)` MPa into the faces; `fea.moment(faces, (mx, my, mz))` N·mm about their centroid; `fea.bearing(bore, (fx, fy, 0))` a pin's cosine pressure on the half it pushes (`fea.force` on a whole bore pulls its far side too).
- `balanced=True` instead of `fixed`: a free part whose loads balance (a link pulled at both ends).
- `accel`: mm/s² on the part's own mass. `fea.g` = 9810.
- Materials: `"PLA"`, `"PETG"`, `"ABS"`, `"nylon PA12"`, `"aluminium 6061-T6"`, `"steel S235"`, `"stainless 304"` (aliases `"aluminum"`, `"steel"`), or `fea.Material("PC", E=2400, nu=0.37, density=1.2, yield_strength=60)`.

## The mesh: voxels or tetrahedra

`mesh="auto"` measures the thinnest wall and narrowest slot (`fea.thinnest(part)`) and picks:
- **voxels** for chunky parts: a grid sized from `dofs` (or `voxel=0.5` mm). Fast; CuPy on a GPU.
- **quadratic tetrahedra** (gmsh, built in: no install) when a wall is under 3 voxels or a slot under 2: blades, flexures, ribs, sheet. `mesh="tet"` forces it, `size=0.5` sets the element size. 300,000 DOFs: 10–30 s.

Same result either way: `max_stress`, `stress_p995`, `safety_factor`, `max_displacement`, `reaction`, `mass`, `stress_at(points)`, `displacement_at(points)`, `show()`.

## Modes, buckling, large deflection (tetrahedra)

```python
from tule import *

stage = box(30, 10, 10) | box(0.5, 20, 10).at(2, 10, 0) | box(0.5, 20, 10).at(27.5, 10, 0) | box(30, 10, 10).at(0, 30, 0)
stage = stage.tag("base", stage.faces.nearest((15, 0, 5))).tag("top", stage.faces.nearest((15, 40, 5)))
md = fea.modes(stage, "aluminium", fixed="base", n=4, dofs=30_000)   # default dofs 100,000; needs a density
print(md)                     # Hz, and which way each mode moves the mass (effective mass %)
bk = fea.buckling(stage, "aluminium", fixed="base", loads=[fea.force("top", (0, -100, 0))], dofs=30_000)
print(bk.factor)              # the loads × factor buckle it; inf: none found (tension)
nl = fea.static(stage, "PETG", fixed=["base", fea.move("top", (3, 0, 0), "x")],
               nonlinear=True, dofs=30_000)   # printed: pushed 3 mm along x, free in y and z
for s in nl.steps: print(s.fraction, s.move_forces[0].x)   # force vs stroke
```

`md.frequencies` (Hz), `md.shapes`; `md.show(name, labels=[...])` swings them. `fixed=None`: a free part (on cords); rigid-body modes dropped, each mode named (bends in x-y, twists…). Keep the first mode above 2× any excitation. Buckling is linear (Euler) for the perfect part: want a factor over 3, and check yield at that load (printed).

`nonlinear=True` when a part moves over ~10 % of its span, turns over ~0.15 rad (a warning says so), or a strip held at both ends bends about its thickness (it stretches and stiffens). Loads and moves go on in steps (`steps=10`, else adaptive), Newton in each; the loads keep their direction. Drive a flexure by `fea.move`, not a force. Buckling or snap-through under a load is a `TuleError` naming the last converged fraction (`err.result`).

## Limits

Small strains, no plasticity, no contact. A flexure whose travel closes a slot touches there: compare `max_displacement` with the gap from `fea.thinnest`. Bolted and press-fit joints are modelled as clamps.
