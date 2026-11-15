# Round 2 review: outer envelope

**Method.** I rendered the model with `env2_front`, `env2_el0`, `env2_chamber` (ducts hidden) and `env2_top`, all at az -90. I measured 012's left panel with gridded crops, row-by-row edge maps and column grey profiles. Scale is 1.688 mm/px, z = (1935 − y)·1.688, axis x 471. I cross-checked with 013 and 014.

## Round-1 findings

| ID | Status | Evidence |
|---|---|---|
| ENV-1 cone | **fixed** (fillet still missing) | 012 right edge: r 309 / 292 / 272 / 252 at z 1197 / 1227 / 1268 / 1308. Model: 300 / 288 / 273 / 257. The ledge on the bell-top ring is now there (r 304 → 350). The concave flare under the lower ring is still absent: the cone runs straight into the bottom of the tube. |
| ENV-2 neck/skirt | **partly** | See ENV-12. The neck is right: 012 gives r 149–152 at z 1520–1560, the model 149–151. Round 1 was wrong to put the waist at z 1500. The photo widens to about 165–175 at z 1504–1515, where the model has 162 at 1500, which is fine. |
| ENV-3 lower ring | **fixed** | Model z 1385–1485, a 50. 012: y 1053–1118 gives z 1379–1489, about 101 mm tall at the left extreme. |
| ENV-4 disc | **fixed** | Model z 1904–2020. 012: y 740–805 gives z 1907–2018. Left reach 304 mm. |
| ENV-5 top flange | **fixed** | Plate measures about 0.49 m in both the photo and the render, at z 2593–2628. Spacer is Ø 0.43 (photo) against Ø 0.42 (model). Stud length is about 90 against 85. Housing top is 2555. Cap, sleeve and ring heights match: 2875 / 2783 / 2628 against 2880 / 2790 / 2628. |
| ENV-6 OTP waist | **fixed** | 012 y 536–607: r ≈ 154. Model: 158. |
| ENV-7 throat ring | **fixed** | 012 column profile 20 mm inside the extreme: 57 mm tall. A tube with a = 29 gives 55. |
| ENV-8 disc bolts | **partly** | 012 y 697–740: a collar at z 2018–2043 (model 2018–2048, ok). The nuts stand at z 2043–2090, about 24 mm across, roughly 45 of them. Model: 40 bolts, Ø 16, z 2048–2070. Use about Ø 24, height about 42 (top ≈ 2090). |
| ENV-9 bell steps | **not fixed** (not in scope this round) | 014 y 2100–2200 still shows two thin steps under the ring. The model has only the single ring-seat hump. |

## New findings

**ENV-12 (major): the skirt under the throat ring is too slim and has no rim.**
- 012 left edge, rows y 980–1012: r 177 / 169 / 165 / 162 at z 1612 / 1592 / 1578 / 1572.
- Then a sharp drop to 150 by z 1565–1558. That drop is the rim seen in 013, where the flat underside of the rim is visible, and in 014 at x 978, y 1555.
- Model: 172 / 160 / 152 / 149 at z 1618 / 1605 / 1585 / 1555. It tapers smoothly, so the skirt is 10–16 mm too thin at z 1570–1605.
- Fix: hold r ≈ 172 → 163 from z 1615 to 1570. Step in to r 150 over z 1570–1563 so the rim has a flat underside. Keep the current neck below that.

**ENV-13 (minor): the upper chamber ring tube is too thin.**
- 012 at x 310 (about 29 mm inside the extreme x 293): dark from y 828 to 874, which is 78 mm tall. Centre y 851, z 1830.
- OD 0.60 m (x 290–645), as in the model.
- Model a = 30. A tube that thin would be about 60 mm tall at that offset.
- Fix: a ≈ 38–40 and R ≈ 262, which keeps the OD at 0.60, centre z 1830. The ring top then sits at about 1870, closing the 48 mm gap under the disc to the 30 mm seen in 012 (disc bottom y 805, ring top y 823).

**ENV-14 (minor): the gimbal ring is too conical.**
- 012 (y 290–378): the ring is close to a cylinder, r ≈ 138–145, with a chamfer at the top.
- Model: a cone from r 153 at its base to 128 at its top. It reads as a strongly flared cone in `env2_top`.
- Fix: about r 145 from z 2628 to 2760, then a chamfer to r 130 at 2790.

**ENV-15 (note):** the bell, the chamber band (r 225 at z 1681–1789 from the left/right mean), the ring positions, the LOX cap and the overall height (2950) still match.

Verdict: close. Seven of eight round-1 findings are fixed or nearly fixed. One major item remains (the skirt rim under the throat ring), plus minor tube, bolt and gimbal-ring proportions.
