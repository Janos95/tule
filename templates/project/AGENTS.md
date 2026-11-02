<!-- Generated from skills/{index,loop,modeling,outputs}.md by tools/build_agents_md.py; edit those. More: tule docs <topic>. -->

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
- Parts to 3D print go on a plate: `outputs.plate(name, parts, settings={"layer_height": 0.2, "walls": 3, "infill": 15, "supports": False, "brim": 5})` lays them out, each as you oriented it (flat face down), in one 3MF that Bambu Studio, OrcaSlicer and PrusaSlicer open with every part named and these settings on it: the user picks printer and filament and slices. `bed=(256, 256)` is the default: pass their printer's. `("pin", pin, {"copies": 4})`: four of it. The 3MF carries every part (Download offers each part's file too): write no STLs beside it. `estimate=True` adds print time and filament (PrusaSlicer's CLI on a generic profile, or their printer's, which sets the bed too: `estimate="bambu-p1s"`; if missing, its error gives the install command): don't fetch another slicer.
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

---

# The loop: edit, build, look, iterate

Work so the user can follow along and nothing is lost.

## One iteration

1. **Edit** a script in `src/` (keep parameters at the top). Rename the placeholder `src/part.py` for your part (`git mv src/part.py src/bracket.py`). It writes its output with `tule.outputs`, named like the script (`src/part.py` → `outputs.part("part", …)`; other files into `outputs.folder()`).
2. **Build**: `tule build` runs the scripts whose inputs changed (`tule build part` for one); the viewer updates live. Fix `TuleError`s from their message and hint (`tule docs occt`). Nothing rebuilds by itself: keeping outputs current is your job.
3. **Look**: `tule screenshot -o /tmp/view.png --labels`, then read the PNG (your own checks stay in `/tmp`, not `work/`). Check other sides (`--view top|front|right`). Claim a feature only once you have seen it.
4. **Check numbers**: `tule inspect outputs/<name>/<file>.step` (size, volume, faces, tags), or print measurements in the script (`part.size`, `part.volume`, `part.is_valid`).
5. **Iterate** until it matches the request; then save (`tule save -m "…"`) and tell the user what to look at in the viewer. Don't end a turn with work unsaved.

Faces are `f<id>` alike in the viewer, screenshots and `part.face(id)`. When the user says "the face on the left" or picks one in the viewer, confirm the id with a labeled screenshot before changing it.

## Project layout

```
inputs/    from outside: supplier STEP, scans, datasheets, customer specs; scripts only read it
src/       src/<name>.py makes outputs/<name>/: a part, an assembly, a BOM, a study;
           a folder is a group: src/checks/fit.py makes outputs/checks/fit/
lib/       only code and parameters the scripts import (from lib.params import WALL)
work/      done by hand from the design: supplier quotes, part comparisons, reviews
outputs/   made by tule build; never edit it by hand
```

A script may read inputs and other outputs (`import_step("outputs/bracket/bracket.step")`): `tule build` records what each script read, builds what it reads first, and reruns a script only when something it read changed. A script may write only its own output.

Before you show the user anything, `tule build --check`: it lists what is out of date or failed (with why and how long its last run took), outputs whose script is gone, and work to check again. Rebuild what you changed and what reads it: `tule build plate --dependents`, `tule build 'plate-*'`, or everything but a long run, `tule build --skip study`. Leave long runs until the design settles, and say so. An output some machine already built from the same files is downloaded instead of run (the build cache); `--force` runs it anyway.

## Work done by hand

Work that is not a script (finding suppliers for the bracket, comparing bearings, a design review) is never re-run by `tule build`. Save its result in `work/` (`work/suppliers/bracket.json`; a JSON list of records shows as a table) and record what it was based on:

```sh
tule based-on work/suppliers/bracket.json outputs/bracket
```

When `outputs/bracket` changes, `tule build` asks to check it again. Redo the work, or if it still holds, run `tule based-on work/suppliers/bracket.json` again. A choice it leads to (which bearing) goes in code, `lib/params.py`.

## Saving

`/workspace` is a git repository. Save when something works, with a message that says what changed:

```sh
tule save -m "Bracket: 4 M5 holes, 2 mm fillets"
```

`tule save` commits, merges what other machines saved, and pushes. If it reports a conflict, open the listed files, resolve the `<<<<<<<` sections (keep both sides' intent), and `tule save` again; for a binary file pick one side (`git checkout --ours <file>` or `--theirs`). Files over 5 MB go to Git LFS by themselves; if a save is refused for a large file, run the command in the error.

For **variants** the user compares side by side, put the model in a function in `lib/` and make a script per variant (`src/bracket-thin.py` calls `make(wall=1.5)`). A branch (`git switch -c thin-wall`) is for trying something you may throw away; `tule save` on it saves the branch, not main.

Old revisions live in git history (`git show <rev>:src/bracket.py`): never copy them to archive folders. Autosaves keep everything every few seconds, but only on this machine's copy; a save is what other machines and later sessions get. `tule snapshot` autosaves now.

## Long jobs

`tule build` goes on when the SSH command that started it ends (a timeout, a dropped connection); `tule build study` again waits for it (log: `outputs/.build/build.log`); `tule build --stop` ends it. Write progress as a sequence output.

The machine stops after 30 minutes without SSH sessions or traffic; a background job does not keep it alive, so keep a session open during long runs. Everything in `/workspace` is autosaved when it stops; save before you leave work for another machine or session.

## Housekeeping

- `pip install`, or `apt-get update && apt-get install -y prusa-slicer`; list them in `tule.toml` (`[packages] pip = [...]`, `apt = [...]`) to keep them after a restart.
- Scratch and big generated data go to `/tmp`. From your computer, write a file without quoting: `tule ssh -p <name> -- 'cat > src/a.py' < a.py`.
- Several load cases or variants of one study: one overlay output with a field each, not an output per case.
- Put notes for the next session (decisions, open questions) in `NOTES.md` and save it.

---

# Modeling with `tule`

`tule` is a thin Python layer over OpenCascade: immutable values, selection queries, tags that survive every operation.

```python
from tule import *

plate = rect(80, 40, center=True).round(5).extrude(6)
plate = plate.tag("mount", plate.faces["bottom"])      # tags survive later operations
boss = circle(9).extrude(12).at(0, 0, 6)
part = (plate | boss).fillet(boss.edges["bottom"], 2)  # the boss's edge, found after the union
part -= cylinder(3.2, 50, center=True)                 # through hole
part = part.tag("load", part.faces.top()).named("bracket", "#4a8")
print(part, part.tags, part.size, round(part.mass(0.0027), 1), "g")
outputs.part("bracket", part)
```

## Style

- Operations return new values; nothing changes in place. Units: mm.
- Parameters are variables at the top of the script (shared ones in `lib/params.py`); derive the rest.
- `from tule import *` gives everything below; `tule docs Shape.fillet` documents a call.

## Building blocks

- Solids: `box(x, y, z, center=)`, `cube`, `cylinder(r, h, center=)`, `cone(r, h, r2)`, `sphere(r)`, `torus(R, r)`, `wedge`, `polyhedron(points, faces)`.
- Profiles (xy plane): `rect(w, h, center=)`, `circle(r)`, `ellipse`, `regular_polygon(n, r)`, `slot(length, width)`, `polygon([(x, y), (x, y, r), ...])` (a third value rounds that corner), `spline(points, tol=)` (a smooth outline), `text("MK4", size, bold=)`. Combine with `| - & ^`; `.round(r)`, `.chamfer(d)`, `.at(x, y)`, `.rotate(deg)`, `.mirror()`.
- Sweeps: `profile.extrude(h, center=, twist=, scale=)`, `profile.revolve(angle)` (x: radius, y: z), `loft([p1, p2], heights=[0, 30])`, `sweep(profile, [(x, y, z), …])` along a smooth 3D path (faces `start`, `end`), `pipe(points, r, wall=)` for tendons, cables, tubes; points `(x, y, z, r)`: straight, that corner an arc of radius r (a pulley).
- Booleans: `a | b` union, `a - b` difference, `a & b` intersection; `union(a, b, c)`.
- Transforms: `.at(x, y, z)`, `.rotate(x=, y=, z=)` (degrees), `.mirror(nx, ny, nz)`, `.scale(f)`.
- Features: `.fillet(edges, r)`, `.chamfer(edges, d)`, `.shell(faces_to_remove, thickness)`.
- Measure: `.volume`, `.area`, `.bounds`, `.size`, `.center_of_mass`, `.mass(density)`, `.is_valid`, `a.distance(b)` (0: touching), `a.closest(b)`: `(gap, pa, pb)`.
- Clearance: `r = clearance(frames, contacts=[("pin", "link")])`: an assembly or each pose of a motion (Assemblies, or their `outputs.sequence`); each overlap, or gap under `below=0.5` mm, exact at its worst frame. `print(r)`, `r.show("arm-clearance")`, `assert r.ok`. Gears: step by a fraction of a tooth.
- Gears: `spur_gear(module, teeth, width, bore=, backlash=)`, `internal_gear(...)`; `pair = gear_pair(module, z1, z2)` checks interference; `pair.place(gear2, angle=)` meshes gear 2 at `pair.distance`. Bevel: `pair = bevel_pair(module, z1, z2, width, shaft_angle=90)` meshes `pair.gear1`, `pair.gear2`; `pair.place(bevel_gear(..., mating_teeth=z1), turn=)`. Cycloidal: `d = cycloidal_drive(pins, pin_circle_d, pin_d, eccentricity, width)` has `d.disc` (pins − 1 lobes), `d.pins`, `d.place(d.disc, crank_angle)`.
- Sheet metal (flanges, flats): `tule docs sheetmetal`.
- Parts: `.named("name", "#4a8")`, `Assembly(a, b, name=)`; files: `export_step`, `export_stl`, `import_step`.

## Selecting faces and edges

Select with queries on the current shape, never by index:

```python
from tule import *

part = box(60, 40, 20, center=True)
part = part.fillet(part.edges.parallel(Z), 4)  # the four vertical edges
part -= cylinder(3, 50, center=True).at(20, 10, 0)
top = part.faces.planar().normal(+Z)           # planar faces facing up
hole = part.faces.cylindrical(r=3)             # by radius
rim = part.edges.circles(r=3).top()            # the hole's top edge
print(top.ids, hole.info())                    # stable ids (f12 in the viewer) and facts
part = part.chamfer(rim, 0.5)                  # ids change: select again after each operation
```

Queries: `planar()`, `cylindrical(r=)`, `curved()`, `normal(dir)`, `largest(n)`, `top()`/`bottom()` (along an axis), `within(lo, hi)`, `nearest(point)`, `sort_by(axis)`, `filter(fn)`; edges also `lines()`, `circles(r=)`, `parallel(axis)`, `perpendicular(axis)`, `on(faces)`, `longest()`. Combine with `| & -`. `part.face(12)` is `f12` in `tule screenshot --labels` and `tule inspect`.

## Roles and tags

- **Roles**: primitives and features name faces: `box(...).faces["top"]` (`top bottom left right front back`), `side`, `fillet`, `chamfer`, shell `inner`. After a Boolean, `part.faces["top"]` is not defined.
- **Tags** are names you give: `part.tag("load", part.faces.top())`, then `part.faces["load"]`. They flow through every operation into STEP files, the viewer (`f12 · load`) and analysis. Tag loads, fixed and mating faces.

## Drawings

`outputs.drawing("plate", part)` writes a shop drawing (PDF and SVG, `dxf=` for cutting) with views, overall sizes and hole, counterbore and slot callouts from the model. Don't hand-draw one; `tule docs drawings`: dimensions, tolerances, sections, scale.

## Errors

Failures raise `TuleError` with `op`, `faces`, `edges`, `hint` and data such as `max_radius`:

```python
from tule import *

part = box(20, 20, 4)
try:
    part = part.fillet(part.edges.parallel(Z), 15)
except TuleError as e:
    print(e)  # …; largest radius that works ≈ 9.99
    part = part.fillet(part.edges.parallel(Z), 0.9 * e.data["max_radius"])
```

Read the message, change the input, rerun (`tule docs occt`).

---

# Outputs: what the viewer shows

The viewer renders `/workspace/outputs/` (and `work/`, `inputs/`): one folder per output: geometry files, an optional `output.json`, other files. `tule build` runs `src/<name>.py`, which writes `outputs/<name>/`; the user sees it at once.

```
outputs/
  bracket/         bracket.step                       a part: one file, no JSON needed
  gripper/         output.json base.step finger.step  an assembly: names, colors
  bracket-topopt/  output.json frame_0000.stl …       a sequence: frames with values, a slider and a plot
  bracket-loads/   output.json bracket.step           an overlay: colored faces, labels, arrows
  print/           output.json lid.step print.3mf     a print plate: one 3MF, every part (no STLs)
```

Geometry: STEP (faces, names, colors, tags travel inside; prefer it) or STL (meshes).

## Writing them

`tule.outputs` writes folders atomically (under `tule build` one per script; here several):

```python
from tule import *

part = box(40, 20, 5).named("plate", "#4a8")
part = part.tag("load", part.faces["top"]).tag("fixed", part.faces["left"])
outputs.part("plate", part)                              # outputs/plate/plate.step
outputs.assembly("stack", [part, box(10).at(0, 0, 5).named("block", "#d33")])
with outputs.sequence("grow", "Growing a block") as seq: # status "running" until the block ends
    for i in range(1, 4):
        seq.add(box(10, 10, i * 5), label=f"step {i}", height=i * 5)
with outputs.sequence("tilt") as seq:  # motion: make parts once, move them per frame (.at, .rotate):
    for a in range(0, 91, 10):         # a frame then stores placements only
        seq.add([part, part.rotate(y=-a).at(40, 0, 5)], label=f"{a} deg", angle=a)
outputs.plate("print", [part, ("pin", box(10), {"copies": 4})], settings={"walls": 3})
outputs.overlay("plate-loads", part, title="Load case 1",
                faces={part.faces["load"]: {"role": "load", "label": "200 N"},
                       part.faces["fixed"]: {"role": "fixed", "label": "clamped"}},
                arrows=[{"at": (20, 10, 5), "dir": (0, 0, -1), "label": "200 N"}])
```

Other tools write into `outputs.folder()` directly: temporary name, `os.replace`, `output.json` last.

## `output.json`

Optional: without it one file is a part, numbered files a sequence, several files an assembly.

```json
{"version": 1, "kind": "part", "file": "bracket.step", "title": "Bracket v2"}
{"version": 1, "kind": "assembly", "parts": [{"file": "base.step", "name": "base", "color": "#888"}]}
{"version": 1, "kind": "sequence", "status": "running",
 "frames": [{"file": "frame_0000.stl", "label": "iteration 0", "values": {"compliance": 41.2}}]}
{"version": 1, "kind": "overlay", "file": "bracket.step",
 "faces": {"12": {"role": "load", "label": "500 N"}, "3": {"severity": "warning", "label": "wall 0.6 mm"},
           "7": {"value": 1.8}},
 "scale": {"label": "wall thickness", "unit": "mm", "min": 0, "max": 3},
 "arrows": [{"at": [0, 0, 18], "dir": [0, 0, -1], "label": "500 N"}],
 "info": {"material": "aluminium 6061", "mass": "20.8 g"}}
```

- **Overlay faces** are keyed by face id (`"12"` is `f12`). Give a `role` (`load`, `fixed`, `keep`, `avoid` or your own word) or a `severity` (`error`, `warning`, `info`, `ok`), and a short `label`. An arrow points along `dir` and ends at `at`.
- **Values:** a face with only a number `value` is colored on a blue (low) to red (high) scale, from `scale.min` to `scale.max` (default: the values' range); `scale.label` and `scale.unit` name it in the legend. Stress over the whole surface: `tule docs fea`.
- **Info** (any kind): a flat object of facts (units in text: `"20.8 g"`), shown under the view.
- **Plates**: `"plate": {"parts": {"pin": 4}, "printer": "bambu-p1s", "estimate": {"print_time_s", "filament_g", …}}` (`estimate=True` or the user's printer, `estimate="bambu-p1s"`: `tule docs outputs.plate`).
- **Sequences**: `"status": "running"` while frames come, then `"done"` (or `"failed"`). A frame is a `file`, or `"parts": [{"file", "name", "color", "transform"}]` (4x4, row-major).

## Tables, charts, documents, images

Other files get their own view: `.csv`, `.tsv` or a `.json` list of records: a table; `*.vl.json`: a Vega-Lite chart; `.md`: a document (`![](mass.vl.json)` draws a chart, table or image in place; `outputs.picture(asm, f"{d}/step-1.png", view="front")` draws a model); PDF (a drawing: `tule docs drawings`), text, images; anything else: a download.

```python
import json
from tule import outputs
d = outputs.folder("study")  # under tule build: outputs.folder()
with open(f"{d}/results.csv", "w") as f:
    f.write("t_mm,mass_g\n3,25.9\n5,43.2\n")
with open(f"{d}/mass.vl.json", "w") as f:
    json.dump({"data": {"url": "results.csv"}, "mark": "line",
               "encoding": {"x": {"field": "t_mm", "type": "quantitative"},
                            "y": {"field": "mass_g", "type": "quantitative"}}}, f)
```

No HTML or network: chart data is a file beside it or inline `values`. A value naming an output (a BOM's `part` column) links to it.

## Looking at outputs yourself

```sh
tule screenshot -o /tmp/v.png --output bracket --labels      # f12 · load on every visible face
tule screenshot -o /tmp/top.png --output bracket --view top  # iso front back left right top bottom
```

Read the PNG; it lists faces labeled, too small to label, or hidden. A sequence shows its last frame (`--frame 3`: the third).

Outputs are not in git (`tule build` remakes them).
