# Golden paths: tested end-to-end recipes

Each recipe runs as written (they are tested). Adapt the numbers; keep the steps.

## Bracket from load cases

"A bracket bolted to a wall with 4 × M5, carrying 500 N downwards at a boss."

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
outputs.overlay("bracket-loads", part, title=f"{LOAD_N} N down at the boss, 4 x M5 bolted",
                faces={part.faces["load"]: {"role": "load", "label": f"{LOAD_N} N"},
                       part.faces["fixed"]: {"role": "fixed", "label": "M5"}},
                arrows=[{"at": (0, BOSS_R - 1, T + BOSS_H), "dir": (0, 0, -1), "label": f"{LOAD_N} N"}])
```

```sh
python main.py
tule screenshot -o loads.png --output bracket-loads      # read it: load and bolts where the user meant?
tule inspect outputs/bracket/bracket.step                # tags: load f.., fixed f.., f.., f.., f..
```

Tell the user to check `bracket-loads` in the viewer; once confirmed, commit, then optimize (`tule docs topopt`).

## Run a study on a GPU

1. Switch the project to a GPU machine: connector `machines.resize` to `gpu-<type>` (e.g. `gpu-rtx4090`), or `tule up --machine gpu-rtx4090`. It costs the user's RunPod balance: say so and stop it when done.
2. Check it: `nvidia-smi` and `python -c "import cupy; print(cupy.cuda.runtime.getDeviceCount())"`.
3. Write the study so it also runs on CPU (CuPy if there is a GPU, NumPy otherwise), and report progress as a sequence output. `study.py`:

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
nohup python study.py > study.log 2>&1 &
tail -f study.log                                        # the viewer plots stress and mass per frame
```

4. When it is done: commit, `tule up --machine cpu-1` (or `tule down`) so the GPU stops billing, and summarize the result with numbers.
