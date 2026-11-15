# Round 3 (final): exterior (envelope + powerhead)

**Renders.** All are in `/private/tmp/claude-501/review/ext3/`:
- `ext3_m012_ph` and `ext3_m012_neck`: az -90, el 7, fov 5. These are pixel-matched to crops of the 012 left panel (`../ext3_012_ph.png`: x 250–900, y 300–860, ×1.5; `../ext3_012_neck.png`: ×1.6).
- `ext3_front`, `ext3_ph`, `ext3_neck` (ducts hidden), `ext3_top`, `ext3_back`, `ext3_p20`, `ext3_az45_60`.
- `ext3_026_vs`: az -30, el -4, fov 34, side by side with 026. Placing the column ports puts 026 at about az -30.

## Status of the round-2 findings
| ID | Status | Evidence |
|---|---|---|
| ENV-1 flare | **partly fixed** | The flare is there (r 241 → 258, z 1350–1386). Its top edge stops outside the lower ring's underside: at r 258 the torus underside is at z ≈ 1395. That leaves a flat lip of about 9 mm (`ext3_neck`, `ext3_m012_neck`). 012 shows a smooth fillet. Fix: end the flare inside the tube, e.g. (1372, 246), (1395, 255). |
| ENV-8 | **fixed** | 45 nuts, Ø 24, top at 2090. The nut band matches 012 to within about 15 mm. |
| ENV-12 | **fixed** | The rim and its flat underside show in `ext3_m012_neck`. The radii match 012. |
| ENV-13 | **fixed** | a 39, R 262. The tube height matches 012. The gap to the disc is 39 mm against about 30 in the photo (note). |
| ENV-14 | **fixed** | The gimbal ring now reads as a cylinder with a chamfered top. |
| PH-3 | **fixed** | The cluster spans x 445–735 in the crop (photo 440–770). The round face is Ø ≈ 160 with its centre at about x +330 (photo about +365). |
| PH-7 | **fixed** | The line is at z 2480. Its bend is about 25 mm left of where the photo has it. |
| PH2-1 | **partly fixed** | See below. |
| PH2-2 | **fixed** | The cap sits over the can in `ext3_p20`, `ext3_az45_60` and `ext3_026_vs`, as in 026 and 007. |
| PH2-3 | **fixed** | The flare's edge is at y 246. The lobe's edge is at y 345 (`ext3_top`). |
| PH2-5 | **partly fixed** | The boss is there, but it is Ø 56 and stands only about 9 mm proud. The fitting in 012 is about 95 mm across (crop x 480–570). |

## Remaining and new findings
**PH2-1 (minor, still open): transfer duct.**
- **In 026, against `ext3_026_vs`:** the photo's top run is horizontal at flange height, and its vertical leg falls just outside the left edge of the disc. In the model, the duct slants down across the front of the disc, and the leg sits about one diameter inside the disc's silhouette.
- **In 012:** the model's upper leg is about 80 mm too far +x (photo centre x ≈ 520 at image z 1970, model ≈ 600), because of the outward kink to x 620. The photo shows the flange face-on, with the duct leaving it toward the camera.
- **What both views suggest:**
  - The top run goes forward from the flange, to about (580, -480, 2167).
  - The leg then drops while coming back in y, about (560, -480, 2050) → (240, -300, 1500), into the ring.
  - The y values are uncertain by about ±100 mm because 026 is a close, wide-angle shot.
- **Discharge:** it is still a short stub off the volute's front-left. In 026 it is a tapered horn running along the volute's front from its outer (+x) side.

**PH3-1 (minor): the discharge flange is too small.**
- 012's bolt ring is about Ø 190 mm (crop x 800–970). The model's is Ø 136–140.
- At that size the duct elbow hides it in the front view. Use r ≈ 95.

**PH3-2 (note): small lines touch the upper chamber ring.**
- Line b passes through the middle of the ring tube: at z 1826 it is at r ≈ 258, against a tube centre at 262.
- Line a grazes the tube's outer edge (294 < 301).
- Fix: route b at r ≥ 312, or end it on the ring.

**PH3-3 (note): nuts and valve block.** The disc nuts reach 5 mm into the underside of the valve block (block bottom 2085, nut tops 2090). This is harmless.

**PH2-4 (note, unchanged): tessellation.** Spikes on the block's edges and facets on the can are still visible (`ext3_ph`).

No regressions. No floating parts (front, back and top views): the block rests on the nuts and the column, the can on the lobe, and the duct joins the flange and the ring.

**Verdict: close. The envelope is essentially done (one minor flare lip); the powerhead has minor items left, mainly where the transfer duct's top run and leg sit (PH2-1).**
