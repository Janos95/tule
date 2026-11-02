# Drawings: shop drawings from the model

`outputs.drawing` writes a dimensioned drawing (`.pdf` and `.svg`, `dxf="top"` adds that view at 1:1) from the part itself. Don't hand-draw one with reportlab or monkey-patch `tule.drawing`; say what only you know with the arguments below, then look at the PDF in the viewer.

```python
from tule import *

plate = box(80, 40, 6) - cylinder(2.75, 20, center=True).at(10, 10, 0)
plate = plate - cylinder(2.75, 20, center=True).at(70, 10, 0)
outputs.drawing("plate", plate.named("plate"), views=("front", "top", "right", "iso"),
                dims=[("top", (0, 0, 6), (10, 10, 6), {"along": "h"}),
                      ("top", (10, 10, 6), (70, 10, 6), {"tol": "±0.05"})],
                info={"number": "PL-1", "material": "6061-T6", "rev": "A"},
                notes=["Break edges 0.3"], sheet="A4")
```

## What you get without asking

- Views by ISO first-angle projection (`projection="third"` too), hidden lines dashed (`hidden=False`), an isometric view in a free spot.
- Overall sizes, each once; center lines.
- Hole and slot callouts in standard form, counted, in the view that looks into them: `4× ⌀6.6 THRU`, `⌴ ⌀11 ↧6.5` (counterbore), `⌵ ⌀12 × 90°`, `slot 5.5 × 11.5 THRU`. THRU only when the hole is open at both ends; a hole that ends in a slit, pocket or bore gets its depth `↧`.
- Each callout goes on the side of its view where its leader crosses the fewest lines. Leaders never cross each other and no text touches a line or another text.
- The largest ISO 5455 scale that fits the sheet (2:1, 1:1, 1:2, ...).

## Your dimensions

`dims` items are `(view, p1, p2[, options])`: two 3D points (or one-item selections) measured in that view. Options: `"along": "h" | "v" | "aligned"`, `"tol": "±0.05"`, `"text": "40"`, `"offset": mm`.

## One feature: pick it by its faces

`(view, selection[, options])` with cylindrical faces or circular edges gives that feature its own callout in that view. Options: `"tol"`, `"side"` (`"top"`, `"bottom"`, `"left"`, `"right"`) and `"text"` (replaces the callout; `"\n"` between lines). Only the faces on the view's axis are taken, so `faces.cylindrical(r=...)` may match others too.

```python
from tule import *

block = box(40, 20, 30) - box(1, 22, 21).at(19.5, -1, 10)              # split by a slit
block -= cylinder(2.25, 21).rotate(y=90).at(-1, 10, 24)                # clearance, into the slit
block -= cylinder(1.6, 12).rotate(y=90).at(20.5, 10, 24)               # tap drill, blind
for x in (8, 32):
    block -= cylinder(2.25, 32).at(x, 4, -1)                           # mounting, same size
outputs.drawing("clamp", block.named("clamp"), views=("front", "top", "left", "right", "iso"),
                dims=[("left", block.faces.cylindrical(r=2.25), {"tol": "+0.1/0", "side": "top"}),
                      ("right", block.faces.cylindrical(r=1.6), {"text": "M4×0.7-6H ↧12"})],
                callouts={"top": "bottom"}, sheet="A3")
```

Here only the clearance hole gets `+0.1/0`; the mounting holes stay `2× ⌀4.5 THRU`. A picked circle that is no hole or slot (a boss, an arc) gets `⌀` or `R`.

`callouts="top"` puts every callout on that side; `{"right": "top"}` does it for one view. Leave both out unless a drawing shows you a reason.

## Tolerances by printed text

`tol={"⌀22": "+0.05/0", "88": "±0.1", "slot 5.5": "+0.1/0", "↧6.5": "±0.1"}` puts a tolerance on every size printed that way. Values: `"±0.1"`, `"+0.05/0"` (stacked), fits like `"H7"`. A key that matches nothing, or callouts of two different features, is an error: then pick the feature in `dims`.

## Sections

`("front", point)` in `views` draws the front view as section A-A through that point (a 3D point or a one-item selection): the material between the plane and the viewer is removed, cut faces are hatched, and the cut line with arrows and letters is drawn on a view that sees the plane edge-on. More sections get B-B, C-C. Holes the plane cuts are called out in the section.

```python
from tule import *

part = box(60, 30, 20) - cylinder(4, 30).rotate(x=90).at(20, 35, 10) - cylinder(3, 12).at(45, 15, 8)
outputs.drawing("section", part.named("block"), views=(("front", (30, 15, 10)), "top", "right"))
```

## Scale and sheet

Leave `scale` out: the default is the largest standard scale that fits. `sheet="A3"` (to `"A0"`) gives room for more views or a larger scale. A fixed scale that does not fit is an error naming the scales that fit (e.g. `scale="1.5:1"`, any ratio is allowed) and the next sheet. Forcing `1:1` on a small part makes it tiny; don't, unless the shop asks.

## Title block, notes, revisions

`info`: `number`, `rev`, `material`, `finish`, `tolerance` (default ISO 2768-m; `""` drops it), `drawn`, `date`, `checked`, `approved`; other keys get their own rows. `revisions=[("A", "First issue"), ("B", "Slots longer", "2026-11-02")]`; `notes=[...]` is a numbered NOTES block. Nothing is dated unless you say so: the same part gives the same bytes.
