# Topology optimization (`tule.topopt`)

Find the stiffest shape for given loads and supports within a design space. (Placeholder: `tule.topopt` v0 is being built; this topic will document its API.)

## What it will do

- SIMP on a hex grid: a design domain (a `Shape`), non-design regions to keep, fixed faces and loads read from **tags** (`part.tag("fixed", ...)`, `part.tag("load", ...)`), a target volume fraction.
- It writes a `sequence` output while it iterates (one frame per iteration with compliance and volume values, so the user watches it converge in the viewer) and returns a smoothed result as a `Shape` you can keep modeling with.
- On a GPU machine a CUDA solver runs the same problem faster (`tule docs setup` for GPUs).

## Prepare for it now

1. Model the design space and the parts that must stay (bolt bosses, mating faces).
2. Tag the boundary conditions on faces: `fixed` (bolted, clamped), `load` (where forces act), with the load values in your script.
3. Show them to the user as an overlay output with arrows and get a yes before running a study (`tule docs golden-paths`, "bracket from load cases").
4. Long studies: `tule build` goes on if the SSH command ends (`tule docs loop`).
