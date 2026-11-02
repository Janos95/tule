<!-- Generated from skills/{index,loop,modeling,outputs}.md by tools/build_agents_md.py; edit those. More: tule docs <topic>. -->

# Tule: start here

Tule gives you, the agent, engineering computers: Linux machines with OpenCascade, the `tule` Python modeling API and engineering libraries installed, a project `/workspace` on each (autosaved continuously; `tule save` merges and saves it in git), and a web viewer where the user watches `/workspace/outputs/` live.

## Two ways in

- **The Tule connector (MCP)** manages resources: projects (list, create, rename, delete), machines (start several per project: Tule cloud, a GPU, the user's computers; stop, status), saves, autosaves, SSH and API keys, usage. It never does the work: no shell, no files.
- **The `tule` CLI and SSH** do the work: run Python on the machine, read and write files, render and inspect results. Use them whenever you can run commands.

Typical session: find or create the project with the connector (or `tule up -p <name>`), then `tule ssh -p <name>` and work in `/workspace`. In a chat app without a shell, the connector alone can manage projects and link the viewer, but cannot model.

## On the machine

- Work on the machine, not on your own computer: files written locally are not in the project and the user never sees them. Write a file with `tule ssh -p <name> -- 'cat > src/bracket.py' <<'EOF'` … `EOF`, or edit it on the machine. Give the user viewer links (`tule open -p <name> --print`; `?o=<output>` opens one output) for results, never paths on your computer.
- `/workspace` is the project and the only thing that persists (a git repository; `tule save` saves it). Everything else is rebuilt from the image at every boot.
- `src/<name>.py` makes `outputs/<name>/`; `tule build` runs the scripts whose inputs changed (nothing runs by itself). The viewer shows outputs as soon as they appear.
- Look at your own work: `tule screenshot -o /tmp/view.png --labels`, then read the PNG (to view it on your own computer: `tule ssh -p <name> -- tule screenshot --labels -o - > /tmp/view.png`). `tule inspect <file>` lists faces with ids, types, areas and tags.
- You are root: `pip install`, background jobs and any tool are fine. Extra packages for every boot go in `tule.toml`.

## Topics

Read one with `tule docs <topic>` (on the machine) or the connector's `guide(topic)`.

| topic | read it when |
|---|---|
| `setup` | logging in, keys, starting machines, GPUs, SSH |
| `loop` | the working loop: edit, build, look, iterate; project layout; saving and merging; long jobs |
| `modeling` | writing geometry with `tule` (values, operators, selections, tags) |
| `outputs` | what the viewer shows and how to write it |
| `occt` | an operation failed: fillets, Booleans, shells, tolerances |
| `annotations` | the user drew on the model in the viewer |
| `topopt` | topology optimization from loads and fixed faces |
| `golden-paths` | tested end-to-end recipes to copy from |

---

# The loop: edit, build, look, iterate

How to work on a Tule machine so that the user can follow along and nothing is lost.

## One iteration

1. **Edit** a script in `src/` (keep parameters at the top). A new project's `src/part.py` is a placeholder: rename it for your part (`git mv src/part.py src/bracket.py`) rather than leaving it beside yours. It writes its output with `tule.outputs`, named like the script (`src/part.py` → `outputs.part("part", …)`; other files into `outputs.folder()`).
2. **Build**: `tule build` runs the scripts whose inputs changed (`tule build part` for one); the viewer updates live. Fix `TuleError`s from their message and hint (`tule docs occt`). Nothing rebuilds by itself: keeping outputs current is your job.
3. **Look**: `tule screenshot -o /tmp/view.png --labels`, then read the PNG (your own checks stay in `/tmp`, not `work/`). Check other sides with `--view top|front|right|bottom`. Do not claim a feature is there until you have seen it.
4. **Check numbers**: `tule inspect outputs/<name>/<file>.step` (size, volume, faces, tags), or print measurements in the script (`part.size`, `part.volume`, `part.is_valid`).
5. **Iterate** until it matches the request; then save (`tule save -m "…"`) and tell the user what to look at in the viewer. Don't end a turn with work unsaved.

Faces are named `f<id>` the same way in the viewer, in screenshots and in `part.face(id)`. When the user says "the face on the left" or picks one in the viewer, confirm the id with a labeled screenshot before changing it.

## Project layout

```
inputs/    from outside: supplier STEP, scans, datasheets, customer specs; scripts only read it
src/       src/<name>.py makes outputs/<name>/: a part, an assembly, a BOM, a study
lib/       code and parameters the scripts share (from lib.params import WALL)
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

`tule save` commits everything and merges what other machines saved, then pushes. If it reports a conflict, open the listed files, resolve the `<<<<<<<` sections (keep both sides' intent), and `tule save` again; for a binary file pick one side (`git checkout --ours <file>` or `--theirs`). Plain `git commit` works too; the next `tule save` pushes it. Files over 5 MB go to Git LFS by themselves; if a save is refused for a large file, run the command in the error.

For **variants** the user compares side by side, put the model in a function in `lib/` and make a script per variant (`src/bracket-thin.py` calls `make(wall=1.5)`). A branch (`git switch -c thin-wall`) is for trying something you may throw away; `tule save` on it saves the branch, not main.

Autosaves keep everything (including outputs and unsaved work) every few seconds, but only on this machine's copy; a save is what other machines and later sessions get. `tule snapshot` autosaves now, e.g. before a risky change.

## Long jobs

The SSH session may end; jobs should not depend on it. Run long builds in the background, log to a file, and write progress as a sequence output:

```sh
nohup tule build study > study.log 2>&1 &
tail -n 20 study.log          # check back later
```

The machine stops after 30 minutes without SSH sessions or traffic; a job running in the background does not keep it alive by itself, so keep a session open (or check in) during long runs. Everything in `/workspace` is autosaved when it stops; save before you leave work for another machine or session.

## Housekeeping

- Install packages with `pip install`; add them to `tule.toml` (`[packages] pip = [...]`) so they are there after a restart.
- Keep big generated data under `/workspace` only if it must persist; scratch goes to `/tmp`.
- Put notes for the next session (decisions, open questions) in `NOTES.md` and save it.

---

# Modeling with `tule`

`tule` is a thin, opinionated Python layer over OpenCascade: immutable values, operators, selection queries, and tags that survive every operation.

```python
from tule import *

plate = rect(80, 40, center=True).round(5).extrude(6)
plate = plate.tag("mount", plate.faces["bottom"])        # tags survive later operations
boss = circle(9).extrude(12).at(0, 0, 6)
part = (plate | boss).fillet(boss.edges["bottom"], 2)    # the boss's edge, found after the union
part -= cylinder(3.2, 50, center=True)                   # through hole
part = part.tag("load", part.faces.top()).named("bracket", "#4a8")
print(part, part.tags, part.size, round(part.mass(0.0027), 1), "g")
outputs.part("bracket", part)
```

## Style

- Every operation returns a new value; nothing is modified in place. Units are yours (mm by convention).
- Parameters are plain Python variables at the top of the script (shared ones in `lib/params.py`); derive everything else from them.
- `from tule import *` gives everything below; `help(box)` documents each call.

## Building blocks

- Solids: `box(x, y, z, center=)`, `cube`, `cylinder(r, h, center=)`, `cone(r, h, r2)`, `sphere(r)`, `torus(R, r)`, `wedge`, `polyhedron(points, faces)`.
- Profiles (xy plane): `rect(w, h, center=)`, `circle(r)`, `ellipse`, `regular_polygon(n, r)`, `slot(length, width)`, `polygon([(x, y), (x, y, r), ...])` (a third value rounds that corner). `text("MK4", size, bold=, align=)` for lettering (extrude it, or cut it to engrave). Combine with `| - & ^`; `.round(r)`, `.chamfer(d)`, `.at(x, y)`, `.rotate(deg)`, `.mirror()`.
- Sweeps: `profile.extrude(h, center=, twist=, scale=)`, `profile.revolve(angle)` (x is the radius, y becomes z), `loft([p1, p2], heights=[0, 30])`.
- Booleans: `a | b` union, `a - b` difference, `a & b` intersection; `union(a, b, c)` for many.
- Transforms: `.at(x, y, z)`, `.rotate(x=, y=, z=)` (degrees), `.mirror(nx, ny, nz)`, `.scale(f)`.
- Features: `.fillet(edges, r)`, `.chamfer(edges, d)`, `.shell(faces_to_remove, thickness)`.
- Measure: `.volume`, `.area`, `.bounds`, `.size`, `.center_of_mass`, `.mass(density)`, `.is_valid`.
- Parts: `.named("name", "#4a8")`, `Assembly(a, b, name=)`; files: `export_step`, `export_stl`, `import_step`.

## Selecting faces and edges

Select with queries on the current shape, never by index guesses:

```python
from tule import *

part = box(60, 40, 20, center=True)
part = part.fillet(part.edges.parallel(Z), 4)                 # the four vertical edges
part -= cylinder(3, 50, center=True).at(20, 10, 0)
top = part.faces.planar().normal(+Z)                          # planar faces facing up
hole = part.faces.cylindrical(r=3)                            # by radius
rim = part.edges.circles(r=3).top()                           # the hole's top edge
print(top.ids, hole.info())                                   # stable ids (f12 in the viewer) and facts
print(part.edges.parallel(X).longest(), part.faces.nearest((30, 0, 0)))
part = part.chamfer(rim, 0.5)                                 # ids change: select again after each operation
```

Queries: `planar()`, `cylindrical(r=)`, `curved()`, `normal(dir)`, `largest(n)`, `top()`/`bottom()` (along an axis), `within(lo, hi)`, `nearest(point)`, `sort_by(axis)`, `filter(fn)`; edges also `lines()`, `circles(r=)`, `parallel(axis)`, `perpendicular(axis)`, `on(faces)`, `longest()`. Combine with `| & -`. Seam edges are never selected. `part.face(12)` is face `f12` as shown by `tule screenshot --labels` and `tule inspect`.

## Roles and tags

- **Roles**: primitives and features name their faces: `box(...).faces["top"]` (`top bottom left right front back`), `cylinder(...).faces["side"]`, extrusions `top/bottom/side`, `fillet`, `chamfer`, shell `inner`. A role resolves through later operations when you select on the feature itself: `part.fillet(boss.edges["bottom"], 2)`. After a Boolean, `part.faces["top"]` is not defined (it would be ambiguous).
- **Tags** are names you give: `part.tag("load", part.faces.top())`, then `part.faces["load"]`. They flow through every operation, are written into STEP files, show in the viewer (`f12 · load`) and are what analysis (`tule.topopt`) reads. Tag what matters: loads, fixed faces, mating faces, cosmetic faces.

## Errors

Failures raise `TuleError` with `op`, `faces`, `edges`, `hint` and data such as `max_radius`:

```python
from tule import *

part = box(20, 20, 4)
try:
    part = part.fillet(part.edges.parallel(Z), 15)
except TuleError as e:
    print(e)                                                  # fillet failed: could not fillet with 15; edges e…; largest radius that works ≈ 9.99
    part = part.fillet(part.edges.parallel(Z), 0.9 * e.data["max_radius"])
```

Read the message, change the input, rerun. See `tule docs occt` for what usually goes wrong.

---

# Outputs: what the viewer shows

The viewer renders `/workspace/outputs/` (and `work/`, `inputs/`): one folder per output, holding geometry files, an optional `output.json`, and any other files (tables, charts, documents, images: see below). `tule build` runs `src/<name>.py`, which writes `outputs/<name>/`; the user sees it within a second.

```
outputs/
  bracket/         bracket.step                       a part: one file, no JSON needed
  gripper/         output.json base.step finger.step  an assembly: names, colors
  bracket-topopt/  output.json frame_0000.stl …       a sequence: frames with values, a slider and a plot
  bracket-loads/   output.json bracket.step           an overlay: colored faces, labels, arrows
```

Geometry: STEP (faces, names, colors and tags travel inside; prefer it: the viewer and `tule screenshot --labels` show face ids and tags) or STL (meshes).

## Writing them

`tule.outputs` writes the folders atomically, so the viewer never sees half a file (one output per script under `tule build`; here several):

```python
from tule import *

part = box(40, 20, 5).named("plate", "#4a8")
part = part.tag("load", part.faces["top"]).tag("fixed", part.faces["left"])
outputs.part("plate", part)                                    # outputs/plate/plate.step
outputs.assembly("stack", [part, box(10).at(0, 0, 5).named("block", "#d33")])
with outputs.sequence("grow", "Growing a block") as seq:       # status "running" until the block ends
    for i in range(1, 4):
        seq.add(box(10, 10, i * 5), label=f"step {i}", height=i * 5)
outputs.overlay("plate-loads", part, title="Load case 1",
                faces={part.faces["load"]: {"role": "load", "label": "200 N"},
                       part.faces["fixed"]: {"role": "fixed", "label": "clamped"}},
                arrows=[{"at": (20, 10, 5), "dir": (0, 0, -1), "label": "200 N"}])
outputs.overlay("plate-walls", part, title="Wall thickness",    # numbers on faces: a color scale
                faces={i: 1.0 + 0.5 * i for i in part.faces.ids},
                scale={"label": "wall thickness", "unit": "mm"})
outputs.part("plate-info", part, info={"mass": "10.8 g"})       # facts, any kind
```

Any tool may write the files directly into `outputs.folder()` instead (a solver's STL frames): temporary name, then `os.replace`; `output.json` last.

## `output.json`

Optional; without it, one file is a part, numbered files are a sequence, several files an assembly.

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

- **Overlay faces** are keyed by face id (`"12"` is `f12`). Give a `role` (`load`, `fixed`, `keep`, `avoid` or your own word) or a `severity` (`error`, `warning`, `info`, `ok`), and a short `label`. Unmentioned faces are drawn gray. An arrow points along `dir` and ends at `at`.
- **Values:** a face with only a number `value` is colored on a blue (low) to red (high) scale, from `scale.min` to `scale.max` (default: the values' range); `scale.label` and `scale.unit` name it in the legend. Use it for anything per face: wall thickness, draft angle, a score.
- **Info** (any kind): a flat object of facts (units in text: `"20.8 g"`), shown under the view.
- **Sequences**: set `"status": "running"` while frames are coming and `"done"` (or `"failed"`) at the end; `values` are plotted.

## Tables, charts, documents, images

Other files get their own view: `.csv`, `.tsv` or a `.json` list of records: a table; `*.vl.json`: a Vega-Lite chart; `.md`: a document (`![](mass.vl.json)` draws a chart, table or image in place); PDF, text, images; anything else: a download.

```python
import json
from tule import outputs
d = outputs.folder("study")              # under tule build: outputs.folder()
with open(f"{d}/results.csv", "w") as f:
    f.write("t_mm,mass_g\n3,25.9\n5,43.2\n")
with open(f"{d}/mass.vl.json", "w") as f:
    json.dump({"data": {"url": "results.csv"}, "mark": "line",
               "encoding": {"x": {"field": "t_mm", "type": "quantitative"},
                            "y": {"field": "mass_g", "type": "quantitative"}}}, f)
```

No HTML or network: chart data is a file beside it or inline `values`; no styling. A value naming an output (a BOM's `part` column) links to it.

## Looking at outputs yourself

```sh
tule screenshot -o /tmp/view.png                             # the most recently changed output
tule screenshot -o /tmp/view.png --output bracket --labels   # f12 · load on every visible face
tule screenshot -o /tmp/top.png --output bracket --view top  # iso front back left right top bottom
tule inspect outputs/bracket/bracket.step                # faces, ids, types, areas, tags
```

Read the PNG. It also prints which faces are labeled, too small to label, or hidden. A sequence renders its last frame.

Outputs are not in git (`tule build` remakes them).
