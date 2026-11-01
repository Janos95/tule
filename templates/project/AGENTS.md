# Working in a Tule project

You are on a Tule machine: a Linux computer for engineering work with
OpenCascade, the `tule` modeling API and engineering libraries installed.
`/workspace` (this folder) is the project and the only thing that is saved:
it is snapshotted continuously, so anything outside it is gone after the
machine stops. The user watches `outputs/` in the Tule viewer, live.

## The loop

1. Edit Python in `/workspace` (start from `main.py`).
2. Run it: `python main.py`.
3. Write results to `outputs/<name>/` (`tule.outputs` does it for you).
4. Look at them: `tule screenshot -o view.png --output <name>` and read the PNG,
   or ask the user what they see in the viewer.
5. Iterate. Commit code that works (`git commit`); use branches for variants.

## Modeling with `tule`

```python
from tule import *

plate = rect(80, 40, center=True).round(5).extrude(6)
plate = plate.tag("mount", plate.faces["bottom"])        # tags survive later operations
boss  = circle(9).extrude(12).at(0, 0, 6)
part  = (plate | boss).fillet(boss.edges["bottom"], 2)    # the boss's edge, found after the union
part -= cylinder(3.2, 50, center=True)
part  = part.shell(part.faces["mount"], 1.5)
outputs.part("bracket", part.named("bracket", "#4a8"))
```

- Values are immutable; operators combine them: `|` union, `-` difference,
  `&` intersection.
- Select with queries, not strings: `part.faces.planar().normal(+Z)`,
  `part.edges.parallel(Z)`, `.top()`, `.longest()`, `.nearest(p)`.
  `.ids` and `.info()` give stable ids (`f12` in the viewer) and facts.
- Feature roles (`boss.faces["side"]`, `box(...).faces["top"]`) resolve in
  later shapes. Tag what analysis needs: `part.tag("load", part.faces.top())`.
- Errors are `TuleError`s that say what failed, where and what would work
  (for a fillet: the largest radius that fits). Read them and adjust.
- `help(tule)` and `tule docs <topic>` have the reference.

## Outputs

One folder per output under `outputs/`:

- a part: one STEP or STL file (`outputs.part(name, shape)`);
- an assembly: several files plus `output.json` with names and colors
  (`outputs.assembly(name, [a, b, c])`);
- a sequence: numbered frames with values, e.g. an optimization
  (`seq = outputs.sequence(name)`, `seq.add(shape, label=..., compliance=...)`).

Files are written atomically; the viewer never sees half a file.

## The machine

- You are root; `pip install` and `apt-get install` work (registries only).
  Packages listed in `tule.toml` are installed at every boot.
- Everything in `/workspace` is saved; `tule snapshot` saves now.
- Long jobs: run them in the background (`nohup python job.py > job.log &`)
  and check the log; the machine stops after 30 idle minutes without an SSH
  session.
