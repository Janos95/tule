# Golden paths: tested end-to-end recipes

Each recipe runs as written (they are tested). Adapt the numbers; keep the steps.

## Bracket from load cases

"A bracket bolted to a wall with 4 × M5, carrying 500 N downwards at a boss."

`src/bracket.py`:

```python
from tule import *

W, D, T = 80, 40, 6            # plate, mm
BOSS_R, BOSS_H = 9, 12
HOLE_R = 2.75                  # M5 clearance
LOAD_N = 500

plate = rect(W, D, center=True).round(5).extrude(T)
boss = circle(BOSS_R).extrude(BOSS_H).at(0, 0, T)
part = (plate | boss).fillet(boss.edges["bottom"], 2)
for x in (-W / 2 + 8, W / 2 - 8):
    for y in (-D / 2 + 8, D / 2 - 8):
        part -= cylinder(HOLE_R, 50, center=True).at(x, y, 0)
part = part.tag("load", part.faces.top()).tag("fixed", part.faces.cylindrical(r=HOLE_R))
part = part.named("bracket", "#4a8")
print(part, "mass (g, aluminium):", round(part.mass(0.0027), 1))
outputs.part("bracket", part)
```

`src/bracket-loads.py` shows the loads on the built bracket:

```python
from tule import *

LOAD_N, HOLE_R = 500, 2.75     # in a project: in lib/params.py, shared with src/bracket.py
part = import_step("outputs/bracket/bracket.step")
load, fixed = part.faces.top(), part.faces.cylindrical(r=HOLE_R)
outputs.overlay("bracket-loads", part, title=f"{LOAD_N} N down at the boss, 4 x M5 bolted",
                faces={load: {"role": "load", "label": f"{LOAD_N} N"},
                       fixed: {"role": "fixed", "label": "M5"}},
                arrows=[{"at": (0, 0, part.size[2]), "dir": (0, 0, -1), "label": f"{LOAD_N} N"}])
```

```sh
tule build                                                # bracket first: bracket-loads reads it
tule screenshot -o /tmp/loads.png --output bracket-loads  # read it: load and bolts where the user meant?
tule inspect outputs/bracket/bracket.step                 # tags: load f.., fixed f.., f.., f.., f..
```

Tell the user to check `bracket-loads` in the viewer; once confirmed, `tule save`, then optimize (`tule docs topopt`).

## Motion study, checked

"A pinion drives a wheel: does it run free?" Check every motion (and assembly) before presenting it.

`src/gears-motion.py` makes each part once and moves it per frame:

```python
from tule import *

pair = gear_pair(1.5, 20, 30, backlash=(0.05, 0.05))
pinion = spur_gear(1.5, 20, 8, bore=6, backlash=0.05).named("pinion", "#4a8")
wheel = spur_gear(1.5, 30, 8, bore=6, backlash=0.05).named("wheel", "#48d")
shaft = cylinder(3, 20).at(0, 0, -6).named("shaft", "#aaa")
plate = (box(70, 40, 4).at(-15, -20, -6) - cylinder(3, 20, center=True)).named("plate", "#888")
with outputs.sequence("gears-motion", "One tooth of mesh") as seq:
    for i in range(13):                       # a fraction of a tooth per frame
        t = i * 360 / 20 / 12
        seq.add([plate, shaft, pinion.rotate(z=t), pair.place(wheel, turn=t)], label=f"{t:.1f} deg", angle=t)
```

`src/gears-clearance.py` checks every frame of it:

```python
from tule import *

r = clearance("outputs/gears-motion", contacts=[("shaft", "*"), ("pinion", "wheel")], below=0.5)
print(r)                    # worst first: clashes, gaps under 0.5 mm, then the contacts you listed
r.show("gears-clearance")   # the worst frame with its faces marked, and the table
assert r.ok, r.clashes
```

```sh
tule build                                               # gears-motion first: gears-clearance reads it
tule screenshot -o /tmp/c.png --output gears-clearance   # read it
```

List what is meant to touch (meshing gears, a pin in its bore, a slider on its rail) in `contacts`. Fix every clash, then show the user `gears-motion` and the smallest gap.

## Run a study on a GPU

1. `tule save`, then load the project on a GPU machine too (it starts from the latest save): `tule up --machine gpu-rtx4090`, or the connector's `machine_start` with `gpu-<type>`. It costs the user's RunPod balance: say so and stop it when done.
2. Check it: `nvidia-smi` and `python -c "import cupy; print(cupy.cuda.runtime.getDeviceCount())"`.
3. Write the study so it also runs on CPU (CuPy if there is a GPU, NumPy otherwise), and report progress as a sequence output. `src/plate-study.py`:

```python
try:
    import cupy as xp
except ImportError:                                       # CPU machine: same code on NumPy
    import numpy as xp
from tule import *

N = 1_000_000                                             # sampled load cases
F = xp.random.normal(500, 60, N)                          # N
angle = xp.radians(xp.random.uniform(-15, 15, N))
with outputs.sequence("plate-study", "Plate thickness vs. 99.9 % stress") as seq:
    for t in (4, 5, 6, 8):
        part = rect(80, 40, center=True).round(5).extrude(t).named("plate", "#4a8")
        sigma = 6 * (F * xp.cos(angle)) * 40 / (40 * t**2)   # MPa: bending at the wall, 40 mm lever
        p999 = float(xp.percentile(sigma, 99.9))
        seq.add(part, label=f"t = {t} mm", stress_mpa=p999, mass_g=part.mass(0.0027))
        print(t, round(p999, 1), flush=True)
```

```sh
tule build plate-study      # goes on if this command ends; run it again to follow it. The viewer plots stress and mass per frame
```

4. When it is done: `tule save` on it, `tule down --machine gpu-rtx4090` so the GPU stops billing, and summarize the result with numbers. Its outputs are in the build cache: after a `tule save` on the other machine, `tule build study` there downloads them instead of running the study.
