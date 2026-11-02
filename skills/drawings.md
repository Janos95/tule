# Drawings: shop drawings from the model

`outputs.drawing` writes a dimensioned drawing (`.pdf`, one `.svg` per sheet, `dxf="top"` adds that view at 1:1) from the part, or an exploded assembly drawing. Don't hand-draw one or monkey-patch `tule.drawing`; say what only you know with the arguments below, then look at the PDF.

## What you get without asking

- First-angle views (`projection="third"`), hidden lines dashed (`hidden=False`), an iso view.
- Overall sizes once; center lines.
- Hole, slot and counterbore callouts, counted, once in the view that shows them best: `4× ⌀6.6 THRU`, `⌴ ⌀11 ↧6.5`, `⌵ ⌀12 × 90°`, `slot 5.5 × 11.5 THRU`.
- Leaders cross no leader or dimension line; no text touches a line.
- Scale 1:1 if it fits A4, A3 or A2 (`sheet=` fixes one), else the largest ISO 5455 scale; stderr says why not 1:1. A `scale` that does not fit is an error naming the largest that does. Text is 3.5 mm.
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

- Linear: `(view, p1, p2[, opts])`, 3D points or one-item selections. Opts: `along` (`"h"`, `"v"`, `"aligned"`), `tol`, `text` (replaces the value), `label` (`2.86 ROOT`), `offset` (mm), `side`.
- A feature: `([view,] faces[, opts])`: cylindrical faces or circular edges give a hole or slot its own callout, in `view` or the best one. Opts: `tol`, `side`, `text` (replaces it; `"\n"` between lines), `label`, `frame`; on a slot `width` (a tolerance or `True`) and `length`. A concave arc gets `4× R3 ↧14`, a boss `⌀`/`R`. A planar face takes only `frame`.
- `frame`: a string or a list. Form: `flatness`, `straightness`, `circularity`, `cylindricity`. With datums: `parallel`, `perpendicular`, `angularity`, `position`, `concentricity`, `symmetry`, `runout`, `total runout`, `profile`. `⌀` for a cylindrical zone, `M`/`L` for MMC/LMC: `"position ⌀0.1 M A B C"`.
- `datums={"A": faces}`: a datum triangle on a planar face, a tag under a hole's callout. `"tol": "basic"` boxes an exact size.
- `faces.cylindrical(r=...)` matches every such face; narrow it with `.within(lo, hi)`.

## Tolerances by printed text

`tol={"⌀22": "+0.05/0", "88": "±0.1", "slot 5.5": "+0.1/0"}` tolerances every size printed that way (`"±0.1"`, `"+0.05/0"`, `"H7"`). A key that matches nothing, or two features, is an error; pick the feature in `dims`.

## Sections and sheets

`("front", point)` in `views` draws section A-A through that point, hatched, the cut line on a view seeing it edge-on. `sheets=[{"views": ("front", "top")}, {"views": (("front", (30, 15, 10)), "iso"), "callouts": "none", "sheet": "A4"}]`: one PDF, each sheet `{"views", "sheet", "scale", "callouts"}`. `callouts`: a side, `{"top": "bottom"}` per view, or `"none"` to leave a view's callouts to another.

## Several parts, a table

`outputs.drawing("family", {"B-40": b40, "B-60": b60}, dims={"B-60": [...]}, table=[{"Part": "B-40", "L": 40}, ...])`: each part on its own sheets; `dims`, `datums`, `tol`, `info` keyed by part name go to it. `table` (dicts, or lists, header first) adds a last sheet, which takes the notes.

## Assembly drawings

```python
from tule import *

pins = [cylinder(3, 20).at(x, 20, 5).named(f"pin{x}") for x in (20, 60)]
outputs.drawing("stand", Assembly(box(80, 40, 5).named("base"), *pins), explode=1,
                bom={"base": {"material": "PLA"}, "pin": {"source": "ISO 2338 6m6"}})
```

One iso view (`views="front"`), the parts apart, a numbered balloon per distinct part, the parts list (Item, Name, Qty, `bom` columns) on the title block. `explode`: a factor (`2` doubles the gaps, `0`: in place) stacks parts along the axis that parts them with least travel, a part above the one it rests on, of two interlocked (shaft in bore) the one listed later; `{"shaft": (-80, 0, 0)}` moves just those. Copies (same geometry, `pin20`, `pin60`) are one item, numbered in part order. Leaders never cross.

## Title block, notes, revisions

`info`: `part`, `number`, `rev`, `material`, `finish`, `tolerance` (default ISO 2768-m; `""` drops it), `drawn`, `date`, `checked`, `approved`; other keys get rows. `revisions=[("A", "First issue"), ("B", "Slots longer", "2026-11-02")]`. `notes=[...]`: one note per item, numbered on sheet 1 (typed numbers dropped, a line continuing the one before joined); more than 12 lines go on A4 portrait notes sheets. Nothing is dated unless you say so.

Sheet metal: a flat (`part.unfold()`) draws as its top view with dash-dot bend lines and a bend table, and `dxf=` writes it with CUT and BEND layers (`tule docs sheetmetal`).
