# Stress and deflection (`tule.fea`)

Linear static analysis of one part: fixed faces and loads from **tags**, a material by name. It returns von Mises stress, displacement, a safety factor, reactions and mass, and draws them in the viewer. Units: mm, N, MPa, g.

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
tule screenshot --output bracket-fea -o stress.png                        # von Mises stress (the first field)
tule screenshot --output bracket-fea -o disp.png --field displacement     # on the shape, exaggerated
```

Read both PNGs. In the viewer, buttons in the strip switch between the fields, and hovering the part shows the value under the pointer.

## Reading the numbers

```
fea.static: bracket in PETG (E 2100 MPa, yield 50 MPa), 14,839 voxels of 0.969 mm, 61,503 DOFs, CG 53 iterations, 5.3 s on cpu
  von Mises stress: peak 2.065 MPa at (-0.593, -4.66, 15.4) mm; 99.5th percentile 1.439 MPa (surface)
  safety factor against yield: 24.2 on the peak, 34.7 on the 99.5th percentile
  max displacement 0.01406 mm at (2.41, 8.67, 17.8) mm
  loads (0, 0, -30.29) N, reactions (4.724e-07, 3.252e-07, 30.29) N: balanced to 4.4e-06 %
  mass 9.793 g
```

- **Peak** is the highest stress on the surface, and where it is. At a sharp inside corner, or at the edge of a clamped face, linear elasticity has a singularity: the peak there grows as the voxels shrink and means little. A peak in a smooth region is real.
- **99.5th percentile**: 0.5 % of the surface area is above it. It ignores those few spikes and holds steady as the mesh changes, so judge a design by it. In the viewer, red on the stress scale starts there, and the legend gives the peak.
- `r.safety_factor` is yield / peak, the conservative number. Printed plastics are weaker across layers and creep, so aim for 2 to 4 on the percentile. If the peak sits at a sharp corner, add a fillet there rather than arguing with the number.
- **Reactions** must balance the loads (gravity included). If they don't, something is wrong: a support is missing, or a load sits on fixed faces (a note says so).
- **Mesh convergence**: run again with `dofs` doubled. If the 99.5th percentile or the max displacement moves by more than about 5 %, double it once more.

## Inputs

- `fixed`: a tag, a selection (`part.faces.cylindrical(r=2.6)`), or a list of them. Faces are clamped. `fea.fix("base", "z")` holds them in z only, so the faces can slide on a plane. Hold the part against every rigid motion.
- `loads`:
  - `fea.force(faces, (fx, fy, fz))`: total N, spread over the faces by area.
  - `fea.pressure(faces, p)`: MPa, pushing into the faces.
  - `fea.moment(faces, (mx, my, mz))`: a couple in N·mm, about the faces' centroid (a servo's torque on its horn).
- `accel`: mm/s² on the part's own mass. `fea.g` = 9810.
- Materials:
  - built in: `"PLA"`, `"PETG"`, `"ABS"`, `"nylon PA12"`, `"aluminium 6061-T6"`, `"steel S235"`, `"stainless 304"` (with aliases such as `"aluminum"` and `"steel"`);
  - or your own: `fea.Material("PC", E=2400, nu=0.37, density=1.2, yield_strength=60)` (MPa, g/cm³).
- Mesh: cubic voxels over the bounding box, sized from `dofs`; or set `voxel=0.5` (mm). Voxels cut by the surface are integrated exactly over their inside, so curved faces are not stair-stepped. Keep walls at least 3 voxels thick: a note warns when they are thinner.
- Speed: 300,000 DOFs take 20–40 s on one core and under 3 GB of memory. `device="auto"` uses CuPy on a GPU machine. Run big studies in the background (`tule docs loop`).

## Using the result

- `r.stress_at(points)` and `r.displacement_at(points)` return values at points.
- `r.stress` and `r.displacement` are node grids, with `r.origin` and `r.voxel`.
- `r.max_displacement_at` gives where the largest displacement is.

`r.show(name, deform=50)` overrides the exaggeration of the drawn shape. Any tool can draw its own fields with `outputs.overlay(..., fields=[...])` (`help(outputs)`).

Linear only: no contact, no plasticity, and no buckling. Displacements should be small against the part. For a thin wall in compression, check buckling separately.
