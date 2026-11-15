# Round 2: powerhead, turbopumps, preburners, ducts and plumbing

Renders (`/private/tmp/claude-501/review/ph2/`): `ph2_front012` (az -90, el 7, fov 5, 1.688 mm/px, same frame as a
crop of 012 at x 150, y 100), `ph2_back`, `ph2_az35/45/60`, `ph2_p10/p20` (perspective, az -10/-20, for 007 and 026),
`ph2_026/026b`, `ph2_valve`, `ph2_canTop`, z-sections at 1900 and 2050.

## Round-1 findings
| ID | Status | Evidence |
|---|---|---|
| PH-1 duct path | **mostly fixed** | 012: the leg now runs about 40 mm left of the photo (x ≈ +490 at z 2050 down to the ring at the front right); 004 matches. In `ph2_p10` the leg looks vertical, as in 007 and 026. See PH2-1. |
| PH-2 FTP volute height | **fixed** | 012: volute z 2214–2358 (photo about 2200–2320), cap top 2456 (photo 2442), short neck. The volute top is about 30–40 mm high (minor). |
| PH-3 valve cluster | **partly fixed** | Block x 155–395, z 2085–2305 (photo x +108…+471, z 2075–2345). Still about 2/3 of the width, and the round bolted valve face (012 crop x 505–600, Ø ≈ 160 mm, x ≈ +300…+470) is missing. |
| PH-4 duct Ø | **fixed** | `DUCT_R` 54. Measured width in 012: 60 px in the render against 65–70 px in the photo. |
| PH-5 can gap/flare | **fixed** (new issue PH2-3) | The flare r 122 now reaches z 1912, which overlaps the disc underside (1904). |
| PH-6 elbow | **fixed** | The elbow turns toward the axis and the front with a domed end, as in 012. |
| PH-7 silver line | **partly fixed** | Present, Ø 34 mm, ends in the actuator/block. It leaves the column at z 2400 but 012 shows it at port level, z ≈ 2470–2490 (crop x 430, y 360). Its run is x +153→+300; the photo has +167→+360. |
| PH-8 flange height | **fixed** | Flange z 2170, arch top ≈ 2203 (photo 2152–2175 / 2210). |

## New findings
**PH2-1 minor: the duct's vertical leg and top run are not far enough forward (-y).**
- In 007 (crop x 900–1350, y 150–850) and 026, the leg drops outside the disc's left silhouette.
- In `ph2_p10` / `ph2_026b` the leg overlaps the disc edge or the column by about one leg diameter.
- 026 shows a long horizontal top run. The discharge leaves the volute's outer (+x) side and passes under the volute, and the silver flange sits at the volute's front edge. In the model the discharge is a short stub from the volute front, with the flange about 100 mm inside the volute silhouette.
- **Fix:** move the leg plane from y ≈ -270 to about -330…-350. Put the flange at about (590, -240, 2165) and lengthen the discharge from the outer side of the volute.

**PH2-2 minor: the FTP and the preburner can are not stacked.**
- The can is at y +141 and the FTP at y +30.
- In 012, 007 and 026 the cap and the can's centre line are vertically aligned. 012 has both at x ≈ +465. In 007 and 026 they are also aligned left to right.
- No model azimuth other than ±90 lines them up (`ph2_az35/45/60`, `ph2_p10`: the can sits 60–110 mm right of the cap).
- In 007 and 026 the cap also clears the column's right edge, but in `ph2_p10` it overlaps it.
- **Fix:** move the FTP (and the disc lobe centre) to about y +100…+140. Move both about +40 mm in x (cap centre at x ≈ 460 in 012).

**PH2-3 minor: the can flare pokes out of the disc lobe.**
- The lobe is a circle of r 235 at (455, 0). The flare edge reaches y 263, so it sticks out about 28 mm beyond the lobe edge (`ph2_canTop`).
- 026 shows the flare fully under the lobe.
- Fixing PH2-2 or reducing the flare to r ≈ 105 cures it.

**PH2-4 note: tessellation artefacts.**
- The valve block (box ∩ sphere, smooth 14) has torn spikes at its edges and around the stem (`ph2_valve`).
- The can's swellings show jagged facets.
- Both are visible at review distance; the meshing resolution should be raised.

**PH2-5 note: the silver line's column end.**
- The line starts at r 200, where the housing is r ≈ 188, so its end sits on the housing surface with no boss.
- The photo shows a fitting/boss (012 crop x 420–450, y 345–385). Add one when it is raised to port level (PH-7).

No parts float or cut through each other badly. The duct, discharge, block, can and lines all connect.

## Verdict
Much closer: the major round-1 issues (duct path, FTP height, duct Ø) are fixed. What remains is minor: placement in y (duct leg, FTP over the can) and the width of the valve cluster.
