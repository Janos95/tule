# Raptor: known facts and measurements

Every line has a source. "SpaceX" means an official SpaceX or Elon Musk post or slide. "Community" means fan or analyst work: useful, but not authoritative. Image files referenced as `evidence/img/...` are listed in `evidence/INDEX.md`.

## 1. Published performance and mass

| Item | R1 | R2 | R3 | Source |
|---|---|---|---|---|
| Sea-level thrust (design, Aug 2024) | 185 tf | 230 tf | 280 tf | SpaceX, https://x.com/SpaceX/status/1819795288116330594 and https://x.com/SpaceX/status/1819772716339339664 |
| Sea-level thrust (as flown, V3 / Flight 12, 2026) | – | 230 tf | 250 tf (2,452 kN) | Wikipedia infobox, https://en.wikipedia.org/wiki/SpaceX_Raptor ; Flight 12 coverage, https://spaceq.ca/starship-flight-12-validates-v3-reentry-systems-despite-early-booster-termination/ |
| Vacuum (RVac) thrust | 200 tf | 258 tf | 306 tf (design) / 275 tf (as flown) | SpaceX Apr-2024 slide (`evidence/img/025_…webp`, via https://starship-spacex.fandom.com/wiki/Raptor_Engine); 275 tf: https://en.wikipedia.org/wiki/SpaceX_Raptor |
| Isp (sea-level engine, quoted in vacuum) | 350 s | 347 s | 350 s | SpaceX posts above. Dan Huot confirms this is the *vacuum* Isp of the sea-level engine: https://x.com/danhuot/status/1819797151804776752 |
| Isp at sea level | 327 s (2018 figure) | | ~332 s (community estimate) | https://en.wikipedia.org/wiki/SpaceX_Raptor ; community: `evidence/img/132_…jpg` (https://x.com/mcrs987/status/1877855892793696589) |
| RVac Isp | 380 s target ("Raptor 3/4 vacuum … Isp of 380") | | | https://x.com/elonmusk/status/1819781279828636041 ; https://en.wikipedia.org/wiki/SpaceX_Raptor |
| Engine mass | 2,080 kg | 1,630 kg | 1,525 kg | SpaceX, https://x.com/SpaceX/status/1819795288116330594 , https://x.com/SpaceX/status/1819772716339339664 (confirmed: R2 1,630 kg, R3 1,525 kg) |
| Engine + vehicle-side commodities and hardware | 3,630 kg | 2,875 kg | 1,720 kg | same SpaceX posts |
| Thrust/weight (infobox) | 88.94 | 141.1 | 163.9 | https://en.wikipedia.org/wiki/SpaceX_Raptor |
| Chamber pressure | 250 bar design (2016) | ~300 bar ("roughly a 300-bar", 2018); 330 bar reached Aug 2020 | 350 bar reached May 2023 (at 269 tf) | https://en.wikipedia.org/wiki/SpaceX_Raptor ; https://x.com/elonmusk/status/1295495834998513664 ; https://x.com/elonmusk/status/1657249739925258240 (trace: `evidence/img/154_…jpg`) |
| Throttle range | 40–100 % | | | https://en.wikipedia.org/wiki/SpaceX_Raptor |

- **Propellants**: liquid oxygen and liquid methane. Mixture ratio O/F = 3.6 (78 % O₂, 22 % CH₄). Source: https://en.wikipedia.org/wiki/SpaceX_Raptor
- **Mass flow**: 650 kg/s in total (LOX 510, CH₄ 140) at 2.23 MN and 350 s. Source: https://en.wikipedia.org/wiki/SpaceX_Raptor (note a). A community R3 figure is 843 kg/s at 280 tf: https://x.com/mcrs987/status/1877855892793696589
- **Cycle**: full-flow staged combustion (FFSC). An oxygen-rich preburner and turbine drive the oxygen turbopump; a fuel-rich preburner and turbine drive the methane turbopump. Both propellants enter the main chamber as gas. Source: https://en.wikipedia.org/wiki/SpaceX_Raptor
- **Counts**: 2 turbopumps (1 oxygen, 1 methane), 2 preburners (1 ox-rich, 1 fuel-rich), 1 combustion chamber. Source: https://en.wikipedia.org/wiki/SpaceX_Raptor (infobox "Pumps 2"). The community R3 fact sheet says "1 oxygen preburner, transverse pintle type; 1 fuel preburner, coaxial type; two LOX impellers, two CH₄ impellers": https://x.com/mcrs987/status/1877855892793696589 (unverified).
- **Main injector**: coaxial swirl injectors (stated for Raptor 2). Source: https://en.wikipedia.org/wiki/SpaceX_Raptor
- **Cooling**: regeneratively cooled chamber and nozzle, including the RVac nozzle extension. Source: https://en.wikipedia.org/wiki/SpaceX_Raptor . The chamber liner contains copper: Musk explained a green flame as "burning copper from depleting fuel in the regenerative cooling circuit", https://x.com/elonmusk/status/2121648438144024943
- **Materials**: SX500 superalloy (in-house, Inconel-like) for hot oxygen-rich turbopump and manifold parts. Source: https://www.teslarati.com/spacex-superalloys-raptor-rocket-engine-elon-musk/
- **Vehicle counts**: Super Heavy V3 has 33 × R3 (https://x.com/SpaceX/status/2105061657558786148). Ship V3 has 3 sea-level + 3 vacuum R3 (https://www.teslaoracle.com/2026/03/09/starship-flight-12-spacex-to-test-raptor-3-engines-on-the-v3-booster-19-for-the-first-time/).

## 2. Published dimensions

| Item | Value | Source / note |
|---|---|---|
| Length (sea-level Raptor) | 3.1 m | https://en.wikipedia.org/wiki/SpaceX_Raptor (ref: SpaceX Starship page). Probably an R1/R2 number; the lineup (`001`) shows R2 ≈ 3.13 m if De = 1.3 m, which matches. **R3 is shorter: about 2.95 m (see §4).** |
| Diameter / sea-level nozzle exit | 1.3 m | https://en.wikipedia.org/wiki/SpaceX_Raptor (Musk 2017 IAC; Gaynor 2018). R1, R2 and R3 have identical exit width in pixels in the lineup photo `001`, so 1.3 m is taken to hold for R3 as well. |
| RVac nozzle exit | 2.4 m | https://en.wikipedia.org/wiki/SpaceX_Raptor (Gaynor 2018). The often-quoted 2.3 m was **not** found in any primary source. |
| Expansion ratio, sea level | 34.34 (current); 40 (2016 plan) | https://en.wikipedia.org/wiki/SpaceX_Raptor |
| Expansion ratio, RVac | 200 (2016 plan) | https://en.wikipedia.org/wiki/SpaceX_Raptor |
| Throat diameter | **no published value** | See §4 for a measured outer diameter and a computed gas-side estimate. |

## 3. Raptor 3 design changes (statements)

- No engine heat shield is needed; secondary flow paths were internalised and regenerative cooling was added for exposed components: "simplify the Raptor engine, internalize secondary flow paths and add regenerative cooling for exposed components", https://x.com/elonmusk/status/1819597689283121225 . SpaceX: "eliminating the need for engine heatshields", https://x.com/SpaceX/status/1819772716339339664
- Sensors and controllers sit inside the engine's thermal protection, so individual engine shrouds are unnecessary. Source: https://en.wikipedia.org/wiki/SpaceX_Raptor
- Items moved inside the structure: "secondary propellant & purge lines, wiring for power & sensing, sensors, avionics boxes, main chamber spark igniters. Also, many bolted flanges were replaced with welded joints." Source: https://x.com/elonmusk/status/2096292014824247360
- Heavy use of 3D metal printing ("SpaceX has the most advanced 3D metal printing technology in the world"): https://x.com/elonmusk/status/1819795653972865460 . Print layer lines are visible in close-ups (`013`), and an early large bolted flange was later removed: https://www.construction-physics.com/p/how-spacex-streamlined-the-raptor
- Smaller lines are bundled into one common umbilical: https://www.construction-physics.com/p/how-spacex-streamlined-the-raptor
- Removing the booster engine shielding saved about 907 kg (2,000 lb) per engine at vehicle level. The shielding is reduced to protection around the main valves, TVC actuators and primary inlets. Source: https://www.nasaspaceflight.com/2026/05/super-heavy-block-3-booster-future/ (read through a search snippet; the page blocks automated fetching)
- The redesigned ignition system applies to all variants: https://en.wikipedia.org/wiki/SpaceX_Raptor
- Electric TVC actuators: **claimed only by community sources** ("Electric TVC motors", `evidence/img/132_…jpg`, https://x.com/mcrs987/status/1877855892793696589). No primary source was found.

## 4. Pixel measurements (Raptor 3 SN1)

**Image:** `evidence/img/012_r3-side-pair-sn1-and-sn20.png`, left panel (2048×2048 file; the left panel spans x ≈ 0–960). Source: https://www.construction-physics.com/p/how-spacex-streamlined-the-raptor

**Method:**
- For each row I took the leftmost and rightmost pixel darker than grey level 110, which separates the black engine from the light siding (script `prof.py`, run with `python -I` under /private/tmp).
- I read the features on gridded, zoomed crops.
- I cross-checked against `001` (lineup, distant camera) and `025` (SpaceX render).

**Scale:**
- The exit diameter De is the widest row of the bell lip: x = 112…881 at y ≈ 1925, so **De = 770 px**.
- Assuming De = 1.30 m, the scale is **1.688 mm/px**.
- Exit plane: y ≈ 1935 ± 15. The lip's lower ellipse edge is at y ≈ 2000, which puts the camera about 7° above the exit plane.

**Engine axis:** x ≈ 481 at the bell top and x ≈ 460–465 on the central powerhead column. I used x = 470 ± 10 for the powerhead and 481 for the chamber and bell; the difference comes from perspective and slight yaw.

| Quantity | Pixels (in 012) | Ratio to De | Metres (De = 1.30 m) | Notes |
|---|---|---|---|---|
| Overall height (LOX inlet cap top y≈185 → exit plane y≈1935) | 1750 | 2.27 | **2.95 m** | Lineup `001`: top y≈392 → exit y≈2080, De = 738 px → 2.29. Render `025`: 557/244 = 2.28. |
| Nozzle exit diameter | 770 | 1 | 1.30 (reference) | Outer lip |
| Bell attach ring (torus at bell top), y 1240–1270 | OD 489 | 0.635 | 0.83 m OD, centre 1.15 m above exit | |
| Bell just below the ring (y 1300) | 419 | 0.54 | 0.71 m | Start of the visible smooth bell |
| Narrowest visible section (throat region outer jacket), y ≈ 1000–1030 | 2×(481−373) = 216 | 0.28 | **0.36 m outer**, 1.55 m above exit | Partly hidden by the S-duct. This is the *outside* of the structure, not the gas-side throat. |
| Gas-side throat diameter (computed, not measured) | – | – | **≈0.22–0.24 m** | At = F/(Cf·pc) = 2.75 MN / (1.75 × 35 MPa) → Dt ≈ 0.24 m. ε = 34.34 with an inner exit of about 1.27 m → Dt ≈ 0.22 m. RS-25 for comparison: 0.26 m throat (https://en.wikipedia.org/wiki/RS-25) |
| Nozzle length, throat (y≈1020) → exit | 915 | 1.19 | **1.55 m** | About 75–80 % of an equivalent 15° cone for ε ≈ 34; ±0.05 m |
| Chamber outer diameter (cylindrical band, y 870–940) | 2×(481−347) = 268 | 0.35 | **0.45 m**, 1.70 m above exit | Outer jacket |
| Chamber length (injector face to throat) | ~ y 790 → 1020 = 230 | 0.30 | **≈0.39 m** visible from the top disc to the throat | The injector face is hidden inside the powerhead disc, so the real chamber is probably 0.45–0.6 m from face to throat. |
| Manifold torus rings on the chamber | y 820–870 (OD ≈350 px), 940–975 (OD ≈280), 1040–1110 (OD ≈332) | 0.45 / 0.36 / 0.43 | 0.59 / 0.47 / 0.56 m OD at 1.84 / 1.65 / 1.45 m above exit | Regen coolant and hot-gas manifolds (function inferred) |
| Powerhead base disc (dark plate), y 740–790 | x 290–880 = 590 | 0.77 | 1.0 m wide, 1.97 m above exit; extends 0.30 m left / 0.69 m right of the axis | Asymmetric: it sits under the fuel-pump side |
| **Overall width at the top (powerhead incl. fuel turbopump)** | x 291–904 = 613 | 0.80 | **1.03 m** | Lineup `001`: 531–569 px / 738 → 0.72–0.77 De (0.94–1.0 m). Render: 0.75 De. |
| Oxygen turbopump (central column, on axis), housing y 390–610 | Ø ≈ 250 | 0.32 | Ø ≈ 0.42 m, centre ≈ 2.42 m above exit, 0 m offset | Identified as the OTP by its on-axis position under the LOX inlet (layout per the community diagram `132`) |
| Top mounting flange with studs, y 330–380 | Ø 265 | 0.34 | Ø 0.45 m at ≈ 2.65 m | Gimbal/thrust interface |
| LOX inlet (with blue protective cap), y 185–240 | Ø 130 | 0.17 | Ø 0.22 m, top at 2.95 m | |
| Fuel turbopump assembly (right side), envelope x 560–870, y 490–750 | 310 × 260 | – | 0.52 m wide × 0.44 m tall; **centre about 0.41 m off-axis, 2.22 m above exit** | Includes valve block and inlet cap. Discharge flange centre (815, 645): 0.58 m off-axis, 2.18 m above exit, Ø ≈ 0.15 m |
| Vertical bulbous duct on the fuel side, x 690–800, y 750–1130 | Ø ≈ 110 | 0.14 | Ø 0.19 m, 0.37–0.56 m off-axis, spans 1.36–2.0 m above exit | Probably the fuel-rich preburner / hot-gas transfer or fuel discharge duct (function uncertain) |
| S-duct from the FTP discharge, wrapping to the front torus at y≈1075 | Ø ≈ 75–90 | – | Ø ≈ 0.13–0.15 m | |

**Uncertainty:**
- Perspective: the camera height is unknown, and the top of the engine is about 1.6 m above the lens axis, so tops are slightly foreshortened. All three sources agree on H/De within ±1 %, so I estimate ±3 % on ratios.
- Exit diameter: if the real De differs from 1.30 m, all metric values scale with it (De could be 1.25–1.35 m).
- Hidden features: the injector face, the gas-side throat and any internal splits are **not** visible. The axis position adds ±10 px (±17 mm) to off-axis offsets.
- The fuel turbopump might not lie exactly in the image plane. If it is rotated toward the camera, its true offset is larger than 0.41 m. View `004` (from the other side) is consistent with a roughly in-plane position.
- SN1 (Aug 2024) differs from production engines (SN20 in `012` right panel and `027`–`029`): the large bolted flange at y ≈ 700–730 is gone and the powerhead outline is smoother. Proportions of the bell and chamber look identical.

## 5. Nozzle jacket ribs and bands

- **R3 sea-level nozzle: 0 axial ribs and 0 stiffener bands on the bell.** The bell is a smooth matte black surface from the attach ring to the lip. Images: `014` (best, 2394×3840), `004`, `012`, `020`.
- The only circumferential features are at the bell top: the attach torus (about 0.83 m OD) and a narrow 1–2 step lip just below it (`014` shows two thin lines about 25 mm apart under the torus), plus a slight rolled lip at the exit.
- **R2 nozzle (`140`)**: also no axial ribs. There is one faint circumferential seam about 90 % of the way down the bell and a clamp/flange band at the top.
- The chamber region has 3 torus rings (see the table in §4) and 1 ring at the bell top, so **4 circumferential manifold rings** in total between the powerhead and the bell (`012`, `013`).
- RVac 3 (`031`): no ribs visible, but the image is low resolution and in flame.
