# Outputs: what the viewer shows

The viewer renders `/workspace/outputs/` (and `work/`, `inputs/`): one folder per output, holding geometry files, an optional `output.json`, and other files (tables, charts, documents, images). `tule build` runs `src/<name>.py`, which writes `outputs/<name>/`; the user sees it within a second.

```
outputs/
  bracket/         bracket.step                       a part: one file, no JSON needed
  gripper/         output.json base.step finger.step  an assembly: names, colors
  bracket-topopt/  output.json frame_0000.stl …       a sequence: frames with values, a slider and a plot
  bracket-loads/   output.json bracket.step           an overlay: colored faces, labels, arrows
```

Geometry: STEP (faces, names, colors and tags travel inside; prefer it) or STL (meshes).

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
with outputs.sequence("tilt") as seq:     # motion: make parts once, move them per frame (.at, .rotate):
    for a in range(0, 91, 10):            # a frame then stores placements only
        seq.add([part, part.rotate(y=-a).at(40, 0, 5)], label=f"{a} deg", angle=a)
outputs.overlay("plate-loads", part, title="Load case 1",
                faces={part.faces["load"]: {"role": "load", "label": "200 N"},
                       part.faces["fixed"]: {"role": "fixed", "label": "clamped"}},
                arrows=[{"at": (20, 10, 5), "dir": (0, 0, -1), "label": "200 N"}])
```

Other tools may write into `outputs.folder()` directly: temporary name, `os.replace`, `output.json` last.

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

- **Overlay faces** are keyed by face id (`"12"` is `f12`). Give a `role` (`load`, `fixed`, `keep`, `avoid` or your own word) or a `severity` (`error`, `warning`, `info`, `ok`), and a short `label`. An arrow points along `dir` and ends at `at`.
- **Values:** a face with only a number `value` is colored on a blue (low) to red (high) scale, from `scale.min` to `scale.max` (default: the values' range); `scale.label` and `scale.unit` name it in the legend. Stress over the whole surface: `tule docs fea`.
- **Info** (any kind): a flat object of facts (units in text: `"20.8 g"`), shown under the view.
- **Sequences**: set `"status": "running"` while frames are coming and `"done"` (or `"failed"`) at the end; `values` are plotted. A frame is a `file`, or `"parts": [{"file", "name", "color", "transform"}]` (4x4, row-major).

## Tables, charts, documents, images

Other files get their own view: `.csv`, `.tsv` or a `.json` list of records: a table; `*.vl.json`: a Vega-Lite chart; `.md`: a document (`![](mass.vl.json)` draws a chart, table or image in place; `outputs.picture(asm, f"{d}/step-1.png", view="front")` draws a model); PDF (a drawing: `outputs.drawing`, see `tule docs modeling`), text, images; anything else: a download.

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

No HTML or network: chart data is a file beside it or inline `values`. A value naming an output (a BOM's `part` column) links to it.

## Looking at outputs yourself

```sh
tule screenshot -o /tmp/view.png                             # the most recently changed output
tule screenshot -o /tmp/view.png --output bracket --labels   # f12 · load on every visible face
tule screenshot -o /tmp/top.png --output bracket --view top  # iso front back left right top bottom
tule inspect outputs/bracket/bracket.step                # faces, ids, types, areas, tags
```

Read the PNG. It also lists faces labeled, too small to label, or hidden. A sequence renders its last frame (`--frame 3` another).

Outputs are not in git (`tule build` remakes them).
