# Round 2: internals

Method: I evaluated the implicits numerically in the container (scripts in /tmp/r/int2_*.py). I ran radial scans at every station, took the hot wall from the actual tca gas side at 1 mm z steps over 40 angles, and labelled 0.4 mm 3D grids at each ring, the head and disc, the lip and the duct entry (solid, coolant, gas, ambient), then looked for coolant cells touching ambient or gas. Renders: `int2_lcring`, `int2_head` (y:0).

## Round-1 status
- INT-1 **partly fixed**: no port breaks through, but the hot wall under every port is thin (INT-14).
- INT-2 **fixed**: full grooves at all stations, the head end and the lip.
- INT-3 **partly fixed**: there is a head-end collector and a lip collector, but the bell-top ring is still a dead end (INT-16).
- INT-4 **fixed**: the lower ring (R 228, a 50) overlaps the skin by 20 mm. No cell leaks anywhere on the rings, the upper ring included (contact about 2 mm).
- INT-5 **fixed**: no powerhead material at r < 169 mm, z 1905–1984 mm.
- INT-6 **fixed**: 361 elements, outer ring at r 141 mm. Every bore opens into the dome.
- INT-7 **fixed**: the face plate is let into the disc over r 171–173 mm.
- INT-8 **partly fixed**: the slots are closed but the roof is only 2 mm (INT-17).
- INT-9 **mostly fixed**: the bore stays sealed. Bore 7,850 mm² → cavity 5,390 mm² open meridian area → ports 1,810 mm² → channels 1,320 (up) + 3,500 (down) mm². The ports are now the choke, which is acceptable.
- INT-10 **fixed** (as a closed tap; its line is solid).
- INT-11 **partly fixed**: the dome wall is at least 15 mm (at z 2110 mm; ≥ 20 mm was asked). The inlet ends in a solid ox turbopump, which is acceptable because the turbine is not modelled.

## New findings
- **INT-14 major: ports leave 0.33–0.6 mm of hot wall.** `r_in = r_gas + 5` is a radial offset. On a sloped wall the round end comes within 5·cos α of the wall: 0.33 mm at z 1650, 0.48 mm at z 1433, 0.62 mm at z 1150, each over a Ø 8 mm spot at all 36 port angles. Fix: offset along the normal (`r_gas + (1 + 4)/cos α`), or end the capsule at the groove's outer face.
- **INT-15 major: the 10 mm grooves leave a 1 mm hot wall with no lands under it.** The grooves start at n = 1, so they remove the lands over 10 mm of length. The span bending stress is about Δp·b²/(2t²): with Δp ≈ 20–40 MPa (coolant against expansion gas at 1152/1435 mm and at the lip) that is about 1–2 GPa, against roughly 200 MPa for a copper alloy at 800 K. Fix: start the grooves at n ≈ 1 + d − 1, so the lands keep supporting the hot wall, or make them about 2–3 mm long axially (σ ∝ b²).
- **INT-16 major: the coolant circuit breaks the full-flow cycle.** The up-leg return goes from the head collector straight into the fuel-rich annulus. That puts regen outlet methane into the turbine exhaust that feeds the injector, so it bypasses the fuel preburner and turbine (UNKNOWN §7 says return → preburner). Nothing carries methane to the preburner. The "thin line back to the powerhead" is a solid tube (`small_lines`, no `wall`, not cut from the ring), so the bell-top ring, the throat ring and the upper chamber ring are all closed plenums. The down-leg (1435 → 1152 → bell) therefore leaves only through the 72 Ø2 lip dump holes (226 mm², choke against 9,570 mm² of channels). That dumps about 10 kg/s (≈ 5–8 % of the fuel) and leaves the ε 2–5 channels at about 15 m/s. Fix: hollow the lines and cut them into the rings, and route the return to the preburner (disc lobe), not to the annulus.
- **INT-17 minor: thin powerhead roofs.** The slots are roofed by 2 mm over r 182–225 mm (18 mm span), and the annulus by 10 mm over a 60–80 mm span. At about 35 MPa both are overstressed by 5× or more. Lower the slots and annulus by about 10 mm, or add a web.
- **INT-18 note: the lip closeout over the groove is 2.3–2.5 mm (z 31–41 mm).** The channels overlap the lip groove by only 1 mm in z (they start at 40, the groove spans 31–41).
- **INT-19 note: throat channels are 745 mm² in all.** Full-flow methane (about 150 kg/s at about 250 kg/m³) would run at roughly 800 m/s. That follows from UNKNOWN §1, but deeper throat channels or a regen bypass would make it consistent.
- **INT-20 note: the channels and the gas side use different polylines.** The channels follow `gas_side(60)` and the tca follows `gas_side(40)`, so the bell hot wall varies from 1.16 to 1.2 mm. Use one polyline.
- Section caps still show the round-1 triangle artefacts (INT-13).

**Verdict:** the topology is much better: every channel is fed, nothing leaks, and the disc and injector are fixed. It is still not physically consistent: thin walls at the ports and grooves (INT-14, 15) and a cycle-level routing error (INT-16) remain.
