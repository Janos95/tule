# Round 1 review: outer envelope

Scope: `lib/envelope.py`, `lib/contour.py` (bell, cone, throat, chamber, 4 rings, base disc, column, top stack).
Method: renders `env_front.png` (az -90, el 7) and `env_chamber.png` (az -90, el 2, ducts hidden). I re-ran the row
silhouette on 012 (threshold 110) and read gridded crops of 012 (left panel), 013 and 014. The 012 scale is
1.688 mm/px, z = (1935 - y) x 1.688. Axis: x 481 for the bell. The chamber rings are centred at x 468-472 in 012, so I
used about 471 for the chamber and 470 for the powerhead.

## Findings

**ENV-1 (major): the cone between the bell-top ring and the lower chamber ring is too fat.**
- Model (`UPPER_OUTER`): r 352/338 at z 1175/1200, 298 at 1270, 258 at 1350, 222 at 1430. The cone leaves the ring at
  its inner edge, so the ring has no visible top ledge.
- 012: right edge x 656-666 at y 1190-1200 gives r ≈ 300-310 at z 1195-1240. Right edge x 616 at y 1121 gives
  r ≈ 236 at z 1375.
- 014 (crop x 680-1720, y 1300-2220, scaled by the 0.83 m ring): r ≈ 289 at the cone base and r ≈ 225 at the cone top.
- Both photos show a flat ledge on top of the bell-top ring, about 50-60 mm wide, inside its OD. They also show a short
  concave fillet where the cone tucks under the lower ring.
- Fix: a straight cone from r ≈ 300 at z 1185 to r ≈ 230 at z 1380, then a concave flare into the underside of the
  lower ring.

**ENV-2 (major): the neck between the throat ring and the lower ring has the wrong shape and is too wide low down.**
- Model: a plain taper, r 182 at z 1600, 175 at 1555, 190 at 1500, 222 at 1430.
- 013 and 014 show a flared skirt under the throat ring with a sharp lower rim. Below it is a concave hourglass neck
  that stays narrow down to the top of the lower ring.
- 012 (crop x 300-660, y 930-1140):
  - Skirt: left edge x ≈ 374 at y 976-1007, so r ≈ 165 at z 1565-1615 (the model has 175-182 there).
  - Neck: left edge x ≈ 386 at y 1022-1045, so r ≈ 143 with axis 471 (≈ 160 with axis 481) at z 1500-1540.
- 013 and 014 give a neck/ring ratio of about 0.70, i.e. Ø ≈ 0.32-0.34 m.
- Fix: neck r ≈ 155-165 from z 1490 to 1550, the skirt rim at z ≈ 1565 (r ≈ 168), and keep the narrowest section down
  near z 1500 rather than 1555. The model's r 190 at z 1500 is 15-30 mm too wide.

**ENV-3 (major): the lower chamber ring tube is too thin and sits too high.**
- 012: the ring spans y 1053-1118, i.e. 65 px ≈ 110 mm tall. It sits near lens height, so the projected height is
  about the tube Ø. Its centre is z 1434. In 012 and 014 it is visibly the fattest ring.
- Model: tube Ø 68 mm, centre z 1462.
- The OD of 0.55-0.56 m is confirmed (014: 0.551 m).
- Fix: a ≈ 50, R ≈ 228, z ≈ 1435. Its bottom (z ≈ 1385) is where the cone in ENV-1 should end.

**ENV-4 (major): the base disc is too thin.**
- 012 (crop x 220-920, y 700-1320): the plate runs from y ≈ 739 to 807, i.e. z 1904-2020, about 115 mm thick.
- Model `DISC_Z` (1933, 2017) gives 84 mm. Move the bottom to z ≈ 1905.
- Its extent (0.305 m left / 0.70 m right of x 470) matches the model.

**ENV-5 (major): the top flange is too narrow and the flange stack is about 30 mm too high.**
- 012 (crop x 280-640, y 160-760):
  - Flange plate: y 378-399 (z 2593-2628), 284 px ≈ 0.48 m wide.
  - A lighter spacer below it: Ø ≈ 0.42 m, z 2555-2593.
  - The dark OTP housing ends at z ≈ 2555.
  - Stud tops are at z ≈ 2715, so about 85 mm of stud shows above the plate.
- Model: the plate is 400 mm square (seen edge-on from -y), at z 2622-2658. The housing top is 2622 and the studs are
  52 mm long.
- Fix:
  - Plate ≈ 480 across at z 2593-2628, with a Ø 420 spacer at z 2555-2593.
  - Housing top at 2555.
  - Gimbal ring base at ≈ 2630, studs ≈ 85 mm long.

**ENV-6 (minor): the OTP housing waist is too wide.**
- 012: at y 539-603 (z 2250-2355) the column is 187 px ≈ 316 mm wide, so r ≈ 158.
- Model: r 176-186 at z 2290-2350.
- The upper housing (r ≈ 209 at z 2420-2600) matches.

**ENV-7 (minor): the throat ring tube is a little thin.**
- 012: y 939-979 is 68 mm tall, which gives a tube Ø ≈ 55-60 mm after a small ellipse allowance.
- 013 also shows a plump tube.
- Model a = 24 (Ø 48). Use a ≈ 29 with R ≈ 207 to keep the OD at 0.47.

**ENV-8 (minor): the disc bolt ring sits too low and is too short.**
- 012: the nut row is at y 688-724 (z 2044-2105) on a raised collar, at r ≥ 260.
- Model: 18 mm bolts at z 2017.

**ENV-9 (minor): the steps on the bell under the ring are missing.**
- 014 shows two thin circumferential steps about 25 mm apart just under the bell-top ring (z ≈ 1030-1080).
- The model is smooth from the lip up to the hump under the ring.

**ENV-10 (note): these parts check out.**

| Part | Model | Photo |
|---|---|---|
| Exit Ø | 1.30 m | 1.30 m |
| Bell profile | z 0-1089 | 012 mean silhouette, within ±5 mm |
| Bell-top ring | z 1152, OD 0.824 | z 1150, OD 0.827 |
| Upper chamber ring | z 1826, OD 0.592 | z 1831, OD 0.598 |
| Throat ring | z 1648, OD 0.472 | z 1648, OD 0.471 |
| Chamber band | Ø 0.446 at z 1680-1790 | Ø 0.435 at z 1681-1789 |
| LOX cap | Ø 0.224, top 2950 | Ø 0.223, top 2938-2950 |
| Gimbal sleeve | r 118 | r 123 |
| Bronze ring | r 128-150 | r 138 |
| Stud circle | r 196 | r ≈ 190 |
| Overall height | 2950 | 2950 |

**ENV-11 (note, tooling):**
- `tule inspect outputs/raptor` crashes with `'_AllFaces' object has no attribute 'info'`.
- `tule screenshot --elevation 0` gives elevation 30, as if 0 were treated as unset. Use 0.5 or above.

Verdict: the bell, the chamber band, three of the four ring positions and the overall height are right. The section
from the bell-top ring to the throat ring (cone, neck, lower ring) is about 25-40 mm too wide in radius and lacks the
skirt and fillet details. The disc and top flange need re-proportioning. The envelope is moderately close, with 5 major
findings, all local.
