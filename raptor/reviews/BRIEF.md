# Review brief (shared by every reviewer)

You review one subsystem of a CAD model of a Raptor 3 sea-level engine built in Tule from implicit
bodies. You do not edit the model: you compare it with the evidence and report differences.

## What to read
- `raptor/KNOWN.md`: published and measured facts (heights in mm above the nozzle exit, 1.688 mm/px in
  `evidence/img/012`), `raptor/UNKNOWN.md`: what is inferred, `raptor/evidence/INDEX.md`: every image.
- Images: `raptor/evidence/img/NNN_*.jpg|png|webp` (open them with the Read tool). Internal reference only.
- Model source: `raptor/project/lib/*.py` and `raptor/project/src/raptor.py` (frame: mm, z = 0 at the
  nozzle exit plane, z up; the fuel turbopump is on +x; the front of photo 012 = camera on -y).

## How to look at the model
The model is built on a local Docker machine. Render it with the software renderer:

```sh
docker exec tule-dev-raptor sh -c 'cd /workspace && tule screenshot -o /tmp/r/NAME.png --azimuth -90 --elevation 7 --fit --size 1000x1200 --no-edges'
docker cp tule-dev-raptor:/tmp/r/NAME.png /private/tmp/claude-501/review/NAME.png    # then Read it
```

Options: `--azimuth` (degrees about +z from +x; -90 = from -y, the 012 view), `--elevation`, `--distance`
(mm), `--target X,Y,Z`, `--target-part NAME` (frame one part, e.g. "fuel turbopump"), `--hide NAME`,
`--section y:0` (cutaway: keeps y >= 0; `z:1555` across the throat, add `:flip` for the other side),
`--size WxH`, `--no-edges` (compare with photos this way). `tule inspect outputs/raptor` lists the parts.
Make the camera match each photo you compare with (estimate the photo's azimuth and elevation) and put
the render and the photo side by side in your head; check proportions with numbers where you can
(heights in mm, ratio to the 1.30 m exit diameter).

## What to report
A list of findings, most important first. Each finding:
- `ID` (e.g. ENV-1), severity: **major** (wrong shape, missing part, proportion off > 5 %), **minor**
  (small offset, detail), **note**.
- What differs, where (part name, height in mm), and **which image(s) show it** (file name and, if useful,
  the pixel region), and what the model should do instead (numbers where you can).
- Do not report the internals' absence from photos, colours/materials beyond gross errors, or markings
  (none are modelled on purpose).
End with a one-line verdict: how close the subsystem is (e.g. "close: minor findings only").
Write the report to `raptor/reviews/ROUND-N-<subsystem>.md` and return it as your final answer too.
