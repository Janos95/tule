# Round 1: powerhead, turbopumps, preburners, ducts and plumbing

Renders: `ph_front` (az -90, el 7), `ph_back` (az 90), `ph_007` (az -45, el -3), `ph_026` / `ph_a-30` / `ph_az0`
(az -40..-15, powerhead close-ups), `ph_plan` (top). 012 coordinates are original pixels (1.688 mm/px, axis x = 481,
z = (1935 - y) × 1.688).

## Findings

**PH-1 major: the transfer duct follows the wrong path.** (`plumbing._path`)
- In 012 the duct comes off the flange at about (+640, z 2160), turns down and runs almost straight:
  - x ≈ +420 at z 1916 (px 730, 800)
  - x ≈ +320 at z 1663 (px 670, 950)
  - x ≈ +220 at z 1494 (px 610, 1050)
- It joins the lower chamber ring at the front, slightly right of the axis (x ≈ +110, z ≈ 1460, about az -65), and continues left as the ring. 004 shows the same.
- 007 and 026 (camera roughly from +x/-y) see the same leg as a long straight vertical drop with sharp bends at the top and bottom. So the leg lies roughly in a vertical plane at y ≈ -250…-300.
- The model is different:
  - The duct swings out to r ≈ 500–560 at y ≈ -460…-490 and crosses the front to the -x side.
  - It enters the ring at az -125 (x -150).
  - In the 012 view the descending leg is 250–350 mm too far left (x +104 at z 1840 and -37 at z 1620, against about +370 and +300 in the photo).
- **Fix:** route it about (580,-130,2160) → (620,-280,2160) → (600,-280,2050) → (420,-275,1900) → (300,-270,1640) → (200,-265,1500) → into the ring at polar(246,-65,1462). Use a tight bend at the top and bottom.

**PH-2 major: the fuel turbopump volute is too low and the inlet neck is a thin stalk.** (`pumps.fuel_turbopump`)
- In 012 the dark volute housing is directly under the blue cap (px x 640–860, y 345–425): x +270…+640, **z 2210–2345**.
- The cap: centre x ≈ +460, Ø ≈ 0.16 m, z 2380–2448.
- 026 and 007 show a large scroll volute about as wide as the ox-pump column, with the cap sitting directly on it and the duct leaving it tangentially at the volute's height.
- The model has the volute torus at z 2033–2157, a Ø 0.17 m neck from 2185 to 2320, and the cap at 2342–2402. From every side view the FTP reads as a narrow post, and the duct hides the volute.
- **Fix:**
  - Put the volute (scroll) at about z 2210–2350, R about 150–190.
  - Put the turbine casing below it (2017–2200).
  - Make the neck about 30 mm.
  - Put the cap at about 2380–2450.
  - Raise the discharge to about 2180–2220.

**PH-3 major: the main valve cluster is about half the visible size.** (`pumps.valve_block`)
- In SN1 (012 px x 545–760, y 540–700) the silver cluster spans x +108…+471 and z 2075–2345, about 0.36 × 0.27 m.
- In production SN20 (012 right panel, 027, 029) the same space is a large dark housing that bridges from the column to the FTP at z ≈ 2150–2330.
- The model block is 0.17 × 0.15 m (x 155–325, z 2085–2235). The space between the column and the FTP looks empty.
- **Fix:** widen it to about x 110–470 and raise it to about 2340, or model the SN20 bridge housing.

**PH-4 major: the transfer duct is too thick.**
- Measured across the slanted leg in 012 (rows y 880–1000, 64–72 px horizontal at a 63° slope): **Ø ≈ 0.10–0.11 m**. 026 gives the same ratio to the preburner can (can/duct ≈ 2.0, against 1.4 in the model).
- The model has `DUCT_R` = 70 (Ø 0.14 m), taken from the KNOWN figure of 0.13–0.15 m, which looks overestimated.
- **Fix:** `DUCT_R` ≈ 52–55.

**PH-5 minor: the preburner can floats and lacks its top flare.**
- The `preburner_can` profile ends at z 1925, but the disc's underside is at 1933, so there is an 8 mm gap.
- 026 and 012 show the can's top flaring into the underside of the disc lobe, to about 1.4× the can diameter.
- **Fix:** extend the can to 1940 and flare its top to r ≈ 120.

**PH-6 minor: the preburner can's bottom elbow points the wrong way.**
- It turns radially inward and slightly toward +y.
- 012 shows it turning toward the axis (left). 026 sees its domed end face-on from the front-right, so it also turns toward -y.
- **Fix:** aim the elbow at about az -20…-40 from the can, not +18°.

**PH-7 minor: the silver line from the ox column is missing.**
- A bent silver line (Ø ≈ 30–40 mm) leaves the ox column at about z 2400 (port level, right side), arcs over and drops into the valve cluster.
- It shows in 012 (px 600–710, y 460–560), 026, 004, 029 and 027 (SN20 too).
- **Fix:** add a tube of r ≈ 16 from polar(200,-60,2420) up and over to the valve block top.

**PH-8 minor: the discharge flange and duct top are about 50 mm low.**
- 012 has the flange centre at z ≈ 2160–2175 and the arch top at z ≈ 2210. The model has 2120.
- This follows from PH-2.

**PH-9 note: these parts are fine as they are.**
- The ox turbopump housing, port bosses, stud flange, gimbal/LOX stack, TVC clevises, base disc outline (0.30 / 0.69 m) and the can's position and swellings match 012 / 026 within a few percent.
- The FTP's plan position (x 420) is consistent with 012.

## Verdict
The components are all present, but the subsystem is not yet close: the duct routing (PH-1) and the size and height of the fuel-pump/valve block (PH-2, PH-3) are visibly wrong in every photo.
