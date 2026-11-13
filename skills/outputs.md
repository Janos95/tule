# Outputs: what the viewer shows

The viewer shows `/workspace/outputs/` (and `work/`, `inputs/`): a folder per output: geometry, an optional `output.json`, other files. `src/<name>.py` writes `outputs/<name>/`; the user sees it at once.

```
outputs/
  bracket/         bracket.step                       a part: one file, no JSON
  gripper/         output.json base.step finger.step  an assembly: names, colors
  bracket-topopt/  output.json frame_0000.stl …       a sequence: frames, a slider, a plot
  bracket-loads/   output.json bracket.step           an overlay: colored faces, labels, arrows
  print/           output.json lid.step print.3mf     a print plate: one 3MF (no STLs)
```

Geometry: STEP (faces, names, colors, tags travel inside; prefer it), STL.

## Writing them

`tule.outputs` writes folders atomically (under `tule build` one per script):

```python
from tule import *

part = box(40, 20, 5).named("plate", "#4a8")
part = part.tag("load", part.faces["top"]).tag("fixed", part.faces["left"])
outputs.part("plate", part)                              # outputs/plate/plate.step
outputs.assembly("stack", [part, box(10).at(0, 0, 5).named("block", "#d33")])
with outputs.sequence("tilt") as seq:  # "running" until the block ends; motion: make parts once,
    for a in range(0, 91, 10):         # move them per frame (.at, .rotate): a frame stores placements
        seq.add([part, part.rotate(y=-a).at(40, 0, 5)], label=f"{a} deg", angle=a)
outputs.plate("print", [part, ("pin", box(10), {"copies": 4})], settings={"walls": 3})
outputs.overlay("plate-loads", part, title="Load case 1",
                faces={part.faces["load"]: {"role": "load", "label": "200 N"},
                       part.faces["fixed"]: {"role": "fixed", "label": "clamped"}},
                arrows=[{"at": (20, 10, 5), "dir": (0, 0, -1), "label": "200 N"}])
```

Other tools write into `outputs.folder()`, `output.json` last.

A long list: group early (`src/checks/fit.py` makes `outputs/checks/fit/`, folded under "checks"); a number orders: `src/01-print.py` writes `outputs/01-print/`, shown first as "print".

## `output.json`

Optional: without it one file is a part, numbered files a sequence, several an assembly.

```json
{"version": 1, "kind": "part", "file": "bracket.step", "title": "Bracket v2"}
{"version": 1, "kind": "assembly", "parts": [{"file": "base.step", "name": "base", "color": "#888"}]}
{"version": 1, "kind": "sequence", "status": "running",
 "frames": [{"file": "frame_0000.stl", "label": "iteration 0", "values": {"compliance": 41.2}}]}
{"version": 1, "kind": "overlay", "file": "bracket.step",
 "faces": {"12": {"role": "load", "label": "500 N"}, "3": {"severity": "warning", "label": "wall 0.6 mm"},
           "7": {"value": 1.8}},
 "scale": {"label": "wall thickness", "unit": "mm", "min": 0, "max": 3},
 "arrows": [{"at": [0, 0, 18], "dir": [0, 0, -1], "label": "500 N"}]}
```

- **Overlay faces** are keyed by face id (`"12"` is `f12`). Give a `role` (`load`, `fixed`, `keep`, `avoid`, …) or a `severity` (`error`, `warning`, `info`, `ok`), and a short `label`. An arrow points along `dir` and ends at `at`.
- **Values:** a face with only a number `value` is colored blue (low) to red (high), from `scale.min` to `scale.max` (default: their range), named by `scale.label` and `scale.unit`; stress: `tule docs fea`.
- **See-through** parts: a color with opacity, `.named("window", "#8da0aa66")`.
- **Plates**: `"plate": {"parts": {"pin": 4}, "printer": …, "estimate": …}` (`tule docs outputs.plate`); a part's `{"down": "base"}` lays that tagged face down.
- **Sequences**: `"status": "running"` while frames come, then `"done"` (or `"failed"`). A frame is a `file`, or `"parts": [{"file", "name", "color", "transform"}]` (4x4, row-major).

## Tables, charts, documents, images

Other files get their own view: `.csv`, `.tsv` or a `.json` list of records: a table; `*.vl.json`: a Vega-Lite chart; `.md`: a document (`![](mass.vl.json)` draws a chart, table or image in place; `outputs.picture(asm, f"{d}/step-1.png", view="front")`); PDF (a drawing: `tule docs drawings`), text, images; anything else: a download.

```python
import json
from tule import outputs
d = outputs.folder("study")  # under tule build: outputs.folder()
open(f"{d}/results.csv", "w").write("t_mm,mass_g\n3,25.9\n5,43.2\n")
q = {"type": "quantitative"}
json.dump({"data": {"url": "results.csv"}, "mark": "line",
           "encoding": {"x": {"field": "t_mm", **q}, "y": {"field": "mass_g", **q}}}, open(f"{d}/mass.vl.json", "w"))
```

No HTML or network: chart data is a file beside it or inline `values`. A value naming an output (a BOM's `part` column) links to it. In a folder, a `README.md` (or `00-…md`) opens first. Write for the user: rounded numbers.

## Looking at outputs yourself

```sh
tule screenshot -o /tmp/v.png --output bracket --labels --view top  # iso front back left right top bottom
tule screenshot -o /tmp/c.png --output ship --target-part flaps --azimuth 30 --elevation 15
```

It lists faces labeled (f12 · load), too small or hidden. Sequences: the last frame (`--frame 3`). Charts: `tule screenshot outputs/study/mass.vl.json`.

Cameras as the viewer's (`-h`): azimuth from +X about +Z, front -90, `--distance` mm or fit `--target-part` (part, group, copy `tiles[17]`), `--target=X,Y,Z`, `--hide P`; it prints a viewer link `?cam=…` (Copy view link: `--cam=…`). Batches: `--cameras cams.json -o dir/`.

Outputs are not in git (`tule build` remakes them).
