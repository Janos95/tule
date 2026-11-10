# Topology optimization (`tule.topopt`)

Find the stiffest shape for given loads and supports within a design space (SIMP on a voxel grid).

## Run a study

1. Model the design space and the regions that must stay (bolt bosses, mating faces). Tag the faces: `fixed` (bolted, clamped), `load` (where forces act).
2. `p = topopt.Problem(part, fixed=part.faces["fixed"], loads={part.faces["load"]: (0, 0, -500)}, volume_fraction=0.3, keep=[boss], void=[...])`: N in total per face set; `keep` and `void` are Shapes that stay solid or empty; `E` (MPa) and `nu` are the material.
3. `p.show("bracket-bc")` writes the supports and loads as an overlay. Get a yes from the user before a long study (`tule docs golden-paths`, "bracket from load cases").
4. `r = topopt.optimize(p, output="bracket-topopt")` writes a sequence while it iterates (a frame per iteration; compliance and volume plotted, so the user watches it converge). `print(r)`.
5. `outputs.part("bracket-optimized", r.mesh)` (the smoothed result, STL); `r.to_shape()` is a solid to keep modeling with (it can fail on a ragged result: catch `TuleError`).

- `voxel=` sets the element size (default: about 20,000 elements), `iterations=60`.
- `device="auto"` uses the GPU on a GPU machine (`tule docs setup`).
- Long studies: `tule build` goes on if the SSH command ends (`tule docs loop`).
- Signatures: `tule docs tule.topopt`.
