# Tule: start here

Tule gives you, the agent, engineering computers: Linux machines with OpenCascade, the `tule` Python modeling API and engineering libraries installed, a project `/workspace` on each (autosaved continuously; `tule save` merges and saves it in git), and a web viewer where the user watches `/workspace/outputs/` live.

## Two ways in

- **The Tule connector (MCP)** manages resources: projects (list, create, rename, delete), machines (start several per project: Tule cloud, a GPU, the user's computers; stop, status), saves, autosaves, SSH and API keys, usage. It never does the work: no shell, no files.
- **The `tule` CLI and SSH** do the work: run Python on the machine, read and write files, render and inspect results. Use them whenever you can run commands.

Typical session: find or create the project with the connector (or `tule up -p <name>`), then `tule ssh -p <name>` and work in `/workspace`. No `tule` command yet: run the setup line from the connector's `connect_info` first. In a chat app without a shell, the connector alone can manage projects and link the viewer, but cannot model.

## On the machine

- Work on the machine, not on your own computer: files written locally are not in the project and the user never sees them. Write a file with `tule ssh -p <name> -- 'cat > src/bracket.py' <<'EOF'` … `EOF`, or edit it on the machine. Give the user viewer links (`tule open -p <name> --print`; `?o=<output>` opens one output) for results, never paths on your computer.
- `/workspace` is the project and the only thing that persists (a git repository; `tule save` saves it). Everything else is rebuilt from the image at every boot.
- `src/<name>.py` makes `outputs/<name>/`; `tule build` runs the scripts whose inputs changed (nothing runs by itself). The viewer shows outputs as soon as they appear.
- Look at your own work: `tule screenshot -o /tmp/view.png --labels`, then read the PNG (on your own computer: `tule screenshot -p <name> --labels -o /tmp/view.png`). `tule screenshot inputs/datasheet.pdf --page 3` shows a page of any PDF (a drawing, a datasheet). `tule inspect <file>` lists faces with ids, types, areas and tags.
- Dimensions of a purchased part (servo, bearing, motor, a drill to hold) come from its datasheet or the vendor's CAD (search for it; a community model, or a printed fit gauge, if there is none): note the source next to the number, in `lib/` when several scripts use it, read what the number measures (screw shank or head), and check the interface (hole pattern, edge distance) in a screenshot before you say it fits.
- Write for the user you have: to someone new to it, print and assembly steps in plain words (what to pick in their slicer, which screw to buy), each term explained or left out.
- Parts to 3D print go on a plate: `outputs.plate(name, parts, settings={"layer_height": 0.2, "walls": 3, "infill": 15, "supports": False, "brim": 5})` lays them out, each as you oriented it (flat face down), in one 3MF that Bambu Studio, OrcaSlicer and PrusaSlicer open with every part named and these settings on it: the user picks printer and filament and slices. `bed=(256, 256)` is the default: pass their printer's. `("pin", pin, {"copies": 4})`: four of it. The 3MF carries every part (Download offers each part's file too): write no STLs beside it. `estimate=True` adds print time and filament (PrusaSlicer's CLI on a generic profile; if missing, its error gives the install command): don't fetch another slicer.
- Say only what the user can see: before you reply, `tule build --check` and the viewer link for each result you mention; a job still running is "running", not a result.
- Never copy the user's Tule login (`~/.config/tule`, its token) onto a machine or into a script. To see what the viewer shows, `tule screenshot` (`--frame N` for a sequence).
- You are root: `pip install`, background jobs and any tool are fine. Extra packages for every boot go in `tule.toml`.

## Topics

Read one with `tule docs <topic>` (on the machine) or the connector's `guide(topic)`. `tule docs Shape`, `tule docs outputs.sequence` print the library's signatures and docstrings: read those, not the source.

| topic | read it when |
|---|---|
| `setup` | logging in, keys, starting machines, GPUs, SSH |
| `loop` | the working loop: edit, build, look, iterate; project layout; saving and merging; long jobs |
| `modeling` | writing geometry with `tule` (values, operators, selections, tags) |
| `outputs` | what the viewer shows and how to write it |
| `drawings` | shop drawings: dimensions, tolerances, callouts, sections |
| `sheetmetal` | bent sheet parts: flanges, flat patterns, bend tables, DXF for the laser |
| `occt` | an operation failed: fillets, Booleans, shells, tolerances |
| `annotations` | the user drew on the model in the viewer |
| `fea` | stress, deflection and safety factor of a part under loads |
| `topopt` | topology optimization from loads and fixed faces |
| `golden-paths` | tested end-to-end recipes to copy from |
