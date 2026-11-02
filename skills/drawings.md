# Drawings: shop drawings from the model

`outputs.drawing` writes a dimensioned drawing (`.pdf`, one `.svg` per sheet, `dxf="top"` adds that view at 1:1) from the part itself. Don't hand-draw one or monkey-patch `tule.drawing`; say what only you know with the arguments below, then look at the PDF.

## What you get without asking

- ISO first-angle views (`projection="third"`), hidden lines dashed (`hidden=False`), an iso view in a free spot.
- Overall sizes once; center lines; extension lines with the standard gap.
- Hole, slot and counterbore callouts in standard form, counted, once per drawing in the view that shows them best: `4× ⌀6.6 THRU`, `⌴ ⌀11 ↧6.5`, `⌵ ⌀12 × 90°`, `slot 5.5 × 11.5 THRU`.
- Leaders cross no leader or dimension line; no text touches a line.
- Scale 1:1 if it fits A4, A3 or A2 (`sheet=` fixes one), else the largest ISO 5455 scale; stderr says what kept it from 1:1. A fixed `scale` that does not fit is an error naming what is too big and the largest scale that fits. Text is 3.5 mm.
- The title is `title=`, else `info["part"]`, else the shape's name.

## Dimensions, features, GD&T

```python
from tule import *

p = box(90, 70, 10) - cylinder(15, 30, center=True).at(45, 40, 0)
for x in (12, 78):
    p -= cylinder(2.75, 30, center=True).at(x, 12, 0)
    p -= slot(15.5, 5.5).extrude(30).at(x, 40, -10)
outputs.drawing("plate", p, title="Bore plate",
    datums={"A": p.faces.bottom(), "B": p.faces.normal((-1, 0, 0)).largest()},
    dims=[(p.faces.top(), {"frame": "flatness 0.05"}),
          (p.faces.cylindrical(r=15), {"tol": "H7", "frame": ["perpendicular ⌀0.02 A", "position ⌀0.05 A B"]}),
          (p.faces.cylindrical(r=2.75).within((0, 0, -1), (90, 25, 11)), {"frame": "position ⌀0.1 M A B"}),
          (p.faces.cylindrical(r=2.75).within((0, 30, -1), (90, 50, 11)), {"width": "+0.1/0", "length": True}),
          ("top", (0, 0, 10), (45, 40, 10), {"along": "h", "tol": "basic", "side": "bottom"})],
    info={"number": "BP-1", "material": "6061-T6", "rev": "A"})
```

- Linear: `(view, p1, p2[, opts])`, 3D points or one-item selections. Opts: `along` (`"h"`, `"v"`, `"aligned"`), `tol`, `text` (replaces the value), `label` (follows it: `2.86 ROOT`), `offset` (mm), `side`.
- A feature: `([view,] faces[, opts])`. Cylindrical faces or circular edges give a hole or slot its own callout, in `view` or the best one. Opts: `tol`, `side` (`"top"`, `"bottom"`, `"left"`, `"right"`), `text` (replaces it; `"\n"` between lines), `label`, `frame`. On a slot, `width` (a tolerance or `True`) dimensions across it and `length` along it. A concave arc (pocket corner, fillet) gets `4× R3 ↧14`; a boss gets `⌀`/`R`.
- A planar face (`faces.top()`, `.largest()`) takes only `frame`.
- `frame`: a string or a list. Form: `flatness`, `straightness`, `circularity`, `cylindricity`. With datums: `parallel`, `perpendicular`, `angularity`, `position`, `concentricity`, `symmetry`, `runout`, `total runout`, `profile`. Write `⌀` for a cylindrical zone and `M`/`L` for MMC/LMC, e.g. `"position ⌀0.1 M A B C"`.
- `datums={"A": faces}`: a planar face gets a datum triangle; a hole or bore hangs its tag under its callout. A frame must name defined datums.
- `"tol": "basic"` boxes a theoretically exact size.
- `faces.cylindrical(r=...)` matches every such face; narrow it with `.within(lo, hi)`.

## Tolerances by printed text

`tol={"⌀22": "+0.05/0", "88": "±0.1", "slot 5.5": "+0.1/0", "↧6.5": "±0.1"}` tolerances every size printed that way. Values: `"±0.1"`, `"+0.05/0"`, fits like `"H7"`. A key that matches nothing, or two different features, is an error; pick the feature in `dims` instead.

## Sections and sheets

`("front", point)` in `views` draws section A-A through that point. Cut faces are hatched and the cut line goes on a view seeing the plane edge-on. Letters skip datum letters.

```python
from tule import *

part = box(60, 30, 20) - cylinder(4, 30).rotate(x=90).at(20, 35, 10) - cylinder(3, 12).at(45, 15, 8)
outputs.drawing("block", part, title="Block",
                sheets=[{"views": ("front", "top", "right")},
                        {"views": (("front", (30, 15, 10)), "iso"), "callouts": "none", "sheet": "A4"}])
```

`sheets` takes a list of `{"views", "sheet", "scale", "callouts"}`. This gives one multi-page PDF titled by the drawing, with "A3 1/2" in each title block. `callouts`: a side for all, `{"top": "bottom"}` per view, or `"none"` (per view too) to leave a view's automatic callouts to another.

## Several parts, a table

`outputs.drawing("family", {"B-40": b40, "B-60": b60}, dims={"B-60": [...]}, table=[{"Part": "B-40", "L": 40}, ...])` draws each part on its own sheets, titled by its name; `dims`, `datums`, `tol`, `info` keyed by part name go to that part. `table` (dicts, or lists, header first) adds a last sheet; it takes the notes, as it does for several parts.

## Title block, notes, revisions

`info`: `part`, `number`, `rev`, `material`, `finish`, `tolerance` (default ISO 2768-m; `""` drops it), `drawn`, `date`, `checked`, `approved`; other keys get rows. `revisions=[("A", "First issue"), ("B", "Slots longer", "2026-11-02")]`. `notes=[...]` makes a numbered NOTES block, on sheet 1. Nothing is dated unless you say so: same part, same bytes.

Sheet metal: a flat (`part.unfold()`) draws as its top view with dash-dot bend lines and a bend table, and `dxf=` writes it with CUT and BEND layers (`tule docs sheetmetal`).
