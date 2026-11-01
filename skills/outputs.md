# Outputs: what the viewer shows

The viewer renders `/workspace/outputs/`: one folder per output, holding geometry files and an optional `output.json`. Write there and the user sees it within a second.

```
outputs/
  bracket/         bracket.step                       a part: one file, no JSON needed
  gripper/         output.json base.step finger.step  an assembly: names, colors
  bracket-topopt/  output.json frame_0000.stl …       a sequence: frames with values, a slider and a plot
  bracket-loads/   output.json bracket.step           an overlay: colored faces, labels, arrows
```

Formats: STEP (B-rep: faces, edges, names, colors and tags travel inside) and STL (meshes). Prefer STEP for anything you model: the viewer and `tule screenshot --labels` then show face ids and tags.

## Writing them

`tule.outputs` writes the folders atomically (temporary name, then rename; `output.json` last), so the viewer never sees half a file:

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
```

Any tool may write the files directly instead (a solver writing STL frames, say): write to a temporary name in the same folder and `os.replace` it, and write `output.json` last.

## `output.json`

Optional; without it, one file is a part, numbered files are a sequence, several files an assembly.

```json
{"version": 1, "kind": "part", "file": "bracket.step", "title": "Bracket v2"}
{"version": 1, "kind": "assembly", "parts": [{"file": "base.step", "name": "base", "color": "#888"}]}
{"version": 1, "kind": "sequence", "status": "running",
 "frames": [{"file": "frame_0000.stl", "label": "iteration 0", "values": {"compliance": 41.2}}]}
{"version": 1, "kind": "overlay", "file": "bracket.step",
 "faces": {"12": {"role": "load", "label": "500 N"}, "3": {"severity": "warning", "label": "r < tool"}},
 "arrows": [{"at": [0, 0, 18], "dir": [0, 0, -1], "label": "500 N"}]}
```

- **Overlay faces** are keyed by face id (`"12"` is `f12`). Give a `role` (`load`, `fixed`, `keep`, `avoid` or your own word) or a `severity` (`error`, `warning`, `info`, `ok`), and a short `label`. Unmentioned faces are drawn gray. An arrow points along `dir` and ends at `at`.
- **Sequences**: set `"status": "running"` while frames are coming and `"done"` (or `"failed"`) at the end; `values` are plotted.

## Looking at outputs yourself

```sh
tule screenshot -o view.png                              # the most recently changed output
tule screenshot -o view.png --output bracket --labels    # f12 · load on every visible face
tule screenshot -o top.png --output bracket --view top   # iso front back left right top bottom
tule inspect outputs/bracket/bracket.step                # faces, ids, types, areas, tags
```

Read the PNG. The command also prints which faces are labeled, too small to label, or hidden from that view. A sequence renders its last frame; an overlay its colors, labels and arrows.

Keep `outputs/` for results the user should see; delete stale outputs (`rm -r outputs/old`) so the list stays meaningful. Outputs are not committed to git (see `.gitignore`), but snapshots keep their history.
