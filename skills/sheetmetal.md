# Sheet metal: bent parts, flat patterns, bend tables

Model the formed part; the flat pattern, its DXF and the bend table come from it, so a change (a bigger fan, +10 mm width) is made once.

```python
from tule import *

t = 1.5
tray = rect(120, 80).sheet(t, bend_radius=1.5, k=0.44)  # outside footprint; t, R, K stay on the part
tray = tray.flange(tray.faces["side"], 30)              # four walls 30 mm tall outside, corner reliefs
tray -= cylinder(2.5, 10).at(10, 10, -1)                # cuts are Booleans, before or after flanging
tray -= cylinder(12, 10).rotate(x=90).at(60, 5, 16)     # a fan hole in the front wall
for i in range(6):
    tray -= slot(40, 3).extrude(10).at(60, 25 + 6 * i, -1)
flat = tray.unfold()                                    # the blank: a plate t thick in the xy plane
print(flat.size, [b.label for b in flat.bends])         # UP 90° R1.5 ...
print(tray.bend_table()[0])                             # bend, direction, angle, radius, k, allowance, ...
outputs.part("tray", tray.named("tray", "#9ab"))
outputs.drawing("tray-flat", flat, dxf="top", info={"material": "DC01", "number": "TR-1"})
```

`outputs.drawing` of a flat draws its top view with dash-dot bend lines labeled `B1 UP 90° R1.5`, the overall blank size, hole and slot callouts and a bend table, and writes `tray-flat-bends.csv`; `dxf=` writes the flat with layers CUT (outline, holes), BEND_UP and BEND_DOWN (dash-dot) and BEND_NOTES. `flat.export_dxf(path)` writes the same file. A drawing of the formed part also gets the bend table.

## Conventions

- `sheet(profile, t, bend_radius=t, k=0.44)` (or `profile.sheet(...)`): the profile in xy extruded by t along +z. Its bottom is the reference side; the flat keeps it at z = 0 and where the base was made, so `flat` lines up with your profile's coordinates.
- `flange(edges, length, angle=90, radius=, inset=0, relief="rect")`: `edges` are straight edges of the sheet or its edge faces (`part.faces["side"]` on a fresh sheet, `s.faces.normal(-Y)`; a selection made on an earlier stage of the part works). The flange's outside face is flush with the edge: the profile is the outside footprint. `length` is outside, from the virtual sharp to the tip (90°: the outside height). `angle` > 0 folds toward the top side (UP in the flat), < 0 down.
- Where two flanges meet at a square corner, each stops at the other's bend: a square corner relief, a gap of R between the walls. `inset=5` (or `(a, b)`, lower then upper end by x, y, z) stops a flange short; it gets reliefs t wide, reaching t/2 past the bend (`relief="obround"` rounds them).
- New faces get roles `flange`, `bend`, `tip`: `part.flange(part.faces["tip"], 8)` adds a return lip.
- Bend allowance `BA = angle·(R + K·t)` (radians), outside setback `OSSB = (R + t)·tan(angle/2)`, deduction `BD = 2·OSSB − BA`: blank = sum of outside lengths − one BD per bend. `flat.bends` give each bend's `direction`, `angle`, `radius`, `allowance`, `deduction` and `line` in the flat; the lines are edges tagged `bend_up` / `bend_down`.
- K depends on material, tooling and R/t (0.33 to 0.5): ask the shop for theirs and their inside radius, then set them once on `sheet(...)`.

## What fails, and what to do

- A flange shorter than its bend (`min_length` in the error), flanges that would run into each other or a panel too narrow for two bends: shorten, change the angle, or inset.
- A hole cut before flanging inside a bend zone: cut it after flanging (holes across bends unfold point by point).
- Flanges meeting at a corner that is not 90° (a hexagon): inset them so they do not meet.
- `unfold` checks the part is one sheet of thickness t (a union with a solid is not) and that the flat does not overlap or touch itself.
- Not built: hems, jogs, louvers, lances, closed or mitered corners, flanges on curved edges, rolled (conical, cylindrical) sheets.
