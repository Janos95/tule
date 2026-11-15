# Raptor 3 (sea level) in implicit bodies: report

The model is the Tule project in `raptor/project`. Its one output, `raptor`, is built only from `tule.sdf`
implicit bodies, which are meshed part by part. The build takes about 170 s and gives 2.0 M triangles in 24 parts,
grouped as thrust chamber, powerhead, turbopumps and ducts.

Open it in the viewer and press **Section** (or add `?section=y:0`) to see the cutaway. Three sections show the internals:
- `y:0` runs lengthwise through the axis and shows the liner, the channels, the collector grooves, the injector posts, the dome and the manifolds.
- `z:1555` cuts across the throat and shows the ring of 360 channels.
- `z:1992` cuts across the injector face and shows the elements.

These regions are meshed at 0.3–0.4 mm; everything else is at 1–3 mm. `tule screenshot --section …` draws the same cut.

Renders of the final build are in `renders/`: the front view matching photo 012, and the y:0 and throat sections.

Evidence is in `KNOWN.md`, `UNKNOWN.md` and `evidence/INDEX.md`. The 66 images are internal reference only: they
are not committed (`raptor/.gitignore`), and INDEX lists their source URLs. No markings or branding are modelled.

## What matches the evidence

The reviewers measured these against photo 012 (left panel, 1.688 mm/px) and cross-checked with 013, 014, 026, 007, 004 and 029. Rounds 2–3 measured each item within about 5 %, most within 10–15 mm.

- **Overall:**
  - Height 2950 mm.
  - Exit Ø 1.30 m.
  - The bell profile is within ±5 mm, with no ribs or bands on the bell (KNOWN §5).
- **Thrust chamber:**
  - The bell-top ring is OD 0.83 m at 1.15 m, with its ledge.
  - The straight cone runs from r 300 to r 240 between 1.19 and 1.35 m, with a flare into the lower ring.
  - The lower chamber ring is OD 0.56 m and 110 mm tall, at 1.435 m.
  - The hourglass neck is r 149.
  - The skirt runs from r 177 to r 162 with a sharp rim under the throat ring.
  - The throat ring is OD 0.47 m at 1.65 m.
  - The chamber band is Ø 0.45 m.
  - The upper chamber ring is OD 0.60 m with a 78 mm tube, at 1.83 m.
- **Powerhead:**
  - The base disc spans z 1904–2020, reaches 0.30 m to −x and 0.69 m to +x, and has a lobe under the fuel pump.
  - The hub carries 45 nuts.
  - The ox turbopump housing is Ø 0.42 m with a waist of r 158 and its port bosses.
  - The 0.21 m spacer and the 0.48 m squarish top flange sit at 2.59–2.63 m, with 16 studs.
  - The gimbal ring, the sleeve and the LOX inlet are at the top.
- **Turbopumps and plumbing:**
  - The fuel turbopump's volute is at z 2210–2350, right under its inlet cap, and the pump is stacked over the preburner can.
  - The valve cluster spans x 130–450, with a round valve face and the bent line from the ox column at port level.
  - The fuel-side preburner can has its swellings, flared top and front-turning elbow.
  - The Ø 0.11 m transfer duct drops in one slant across the front into the lower ring.

## What is inferred (labelled in `lib/internals.py` and `lib/contour.py`)

- **Gas side.** Chamber r 170, throat r 110 at 1.555 m, a 30° convergent and a Rao-type bell inside the measured outside. The wall is 6–18 mm in the bell.
- **Cooling.** A milled-channel liner under a closeout, with a 1 mm hot wall:
  - 360 channels from the head down to 1.35 m, with 1 mm lands, so 0.96 × 2.2 mm at the throat;
  - they split into 720 down to 0.78 m, then 1440 down to the lip. No channel is wider than 3.2 mm.
  - Full collector grooves (starting 2.6 mm out, so the lands under the hot wall stay) sit at the four ring stations, the head end and the lip.
  - The four rings are hollow, with 48 radial ports each, started along the wall normal.
- **Coolant circuit (full flow).**
  - Methane enters through the transfer duct into the lower chamber ring.
  - Upward: chamber channels → equalising groove at the upper ring → head-end collector → coolant return annulus in the disc → fuel-rich preburner (the can) → riser to the fuel turbopump's turbine → exhaust → fuel-rich gas annulus in the hub → 16 slots → injector fuel cavity.
  - Downward: chamber channels → bell-top collector → bell-top ring → hollow line a back to the return annulus.
  - The bell channels are fed from the bell-top groove and dump through 72 Ø 1.4 mm holes at the lip.
  - The throat ring is a closed tap.
- **Injector and dome.**
  - 361 gas-gas coaxial elements on 10 rings at 14 mm pitch, inside an ox-rich dome (≥ 20 mm wall) fed from the ox turbine above.
  - The face and back plates are let into the disc.
  - The chamber's top sits in a seat cut into the disc, so the disc never fills the chamber.

## Review rounds (`reviews/`)

Each subsystem was reviewed three times; the internals were reviewed for physical consistency. Round-1 findings: 5 major for the envelope, 4 for the powerhead, 6 for the internals; all were fixed or reduced to minor. The round-3 exterior findings were applied after round 3; that last build was checked by render only, with no review round after it. The round-3 internals findings INT-21 and INT-23 were fixed, and INT-17 and INT-22 partly.

## Remaining findings

- **PH2-1 (minor):** the transfer duct's top run and vertical leg may sit about 100 mm too far back (+y) compared with the close wide-angle shot 026. That photo's perspective leaves ±100 mm of doubt, so the 012/004 fit was kept.
- **PH2-5 / PH3-3 / ENV-9 (minor):**
  - The fitting where the bent line leaves the ox column is smaller than in 012.
  - The disc nuts touch the valve block's underside.
  - The two thin steps under the bell-top ring in 014 are not modelled.
- **INT-22 (major, partly addressed):** the fuel-side passages inside the measured 116 mm disc are still 2–5× smaller in area than full methane flow (about 140–175 kg/s) needs. These are the coolant return, the leg to the preburner and the turbine exhaust. The real engine very likely carries this flow in passages the photos don't show. This is the main physical shortcut of the internals.
- **INT-24 (note):** the downward leg (line a and the lip dump) is meant to carry a small share of the coolant. The model does not set the split.
- **INT-18/19 (notes):** see the round-2 and round-3 internals reports:
  - The ox dome and turbine are only blind cavities.
  - The upper ring is only touching the chamber skin over about 2 mm.
- **Meshing:**
  - Dual contouring leaves small spikes on sharp box edges (the valve block) and facets on the can.
  - The software renderer's section caps show triangle artefacts in thick walls. They are display-only, not voids.

## Tule changes made for this

On the branch `fix/viewer-clip-and-smoothness`.

**`tule.sdf`:**
- Region 2D profiles (polygon, spline, polyline, rect, circle, wedge) with revolve and extrude.
- Polar and linear domain repetition.
- `sdf.Wall`: wall coordinates with channels, ribs, bands and solid.
- `sdf.tube` along polylines or splines, with fillets, hollow walls and tapers.
- Banded meshing: `to_mesh(fine=[…], rest=…)`, with large regions split into chunks.
- A faster `_clean`.

**Viewer and screenshots:**
- The viewer has a section plane with hatched caps (Section button, S key, `?section=`).
- `tule screenshot --section` draws the same cut.
- Fix: `tule screenshot --elevation 0` was dropped as if no elevation had been given.

**Known issue left:** `tule inspect` crashes on this output (`'_AllFaces' object has no attribute 'info'`).
