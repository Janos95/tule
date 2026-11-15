# Round 3: internals

Method: I evaluated the implicits in the container (`/tmp/r/int3_*.py`). Checks: a 1 mm connected-component labelling of every void in the powerhead (x −322…712, z 1884–2068); 0.25–0.5 mm coolant-adjacency grids at each ring, the head, the lip, line a and the preburner can; a normal hot-wall scan from z 45 to 1980 over one port pitch; angular land scans; and flow areas taken from the source. Renders: `int3_tee` (y:191), `int3_head` (y:0). The powerhead mesh has no cavities outside |y| < 4 mm, so the passages at 14–40° appear in no render.

## Round-2 status
- INT-14 **fixed**: the hot wall is at least 0.97 mm (normal) everywhere, ports included.
- INT-15 **fixed**: the lands stay under every groove (gaps of 1.4–2.0 mm). The exception is z 1152–1154, where the 360 and 720 channel patterns overlap and leave a 2 mm axial band with no lands (acceptable).
- INT-16 **fixed in topology**. The coolant (rings → ports → head annulus, line a, to-preburner leg) is one component, sealed from the fuel-rich gas, the injector cavity, the ox dome and the outside air. The walls are ≥ 18 mm to the gas annulus and 30 mm to air. The to-preburner leg and the riser are 21 mm apart: they are joined only through the preburner can, which is solid (as the turbine is). The exhaust and gas annulus feed the slots and the cavity. The riser and the exhaust both end blind in the solid fuel turbopump. **One open hole remains (INT-21).**
- INT-17 **partly fixed**: the powerhead roof over the gas annulus is still 4 mm (z 2016–2020). The 30 mm above it is the separate "disc bolts" collar, which reads as a bolted part. Make it part of the powerhead body.
- INT-11 **fixed**: the dome wall is at least 24.7 mm (z 2120).
- INT-20 and INT-13 are unchanged. The cap artefacts are visible in `int3_head`.

## New findings
- **INT-21 major: line a's bore breaks out to air** over z ≈ 1191–1265 at θ 30°, r 373–386. `line_a_bore` runs straight from (382, 30°, 1152) to (345, 34°, 1420) and skips `LINE_A[-1]` (372, 30°, 1175), which the tube bends through. The bore then leaves the 14 mm tube on its outboard side. It is visible as a slit in `int3_tee`. Fix: pass the bore through every LINE_A point, with the same `bend` as the tube.
- **INT-22 major: the fuel-side passages are 3–15× too small for full flow.** Assumptions: about 175 kg/s CH₄, regen outlet ρ ≈ 170 kg/m³, turbine exhaust ρ ≈ 100 kg/m³ (fuel-rich gas at about 900 K).

  | Passage | Area | Velocity at full flow |
  |---|---|---|
  | Exhaust (Ø22) | 380 mm² | ≈ 5,000 m/s; choked, it passes only about 18 kg/s |
  | Gas annulus | 1,080 mm² per side | ≈ 900 m/s |
  | To-preburner leg | 1,257 mm² | ≈ 820 m/s |
  | Head ports | 1,847 mm² | ≈ 510 m/s |
  | Riser | 3,217 mm² | ≈ 530 m/s |
  | Slots | 3,456 mm² | ≈ 560 m/s |

  For comparison, the ox inlet is 12,076 mm² (≈ 220 m/s). Size the fuel-side passages for about 10,000 mm² each, e.g. a Ø 110 mm (or kidney-shaped) exhaust port in the lobe and a deeper gas annulus with larger slots.
- **INT-23 major: the 360 upper channels grow to 4.2 mm wide** (2.7 mm at z 1350, 3.5 at 1250, 4.2 at 1160) under a 1 mm hot wall. With Δp ≈ 40 MPa, σ ≈ Δp·b²/2t² ≈ 350 MPa. Just below the split the 720 channels are 1.65 mm wide, so the bifurcation is on the wrong side. Bifurcate at the lower chamber ring, where the channels are 1.95 mm wide (the inlet groove can feed both). Bifurcate again at r ≈ 450 so the lip is not at 4.4 mm.
- **INT-24 minor: line a (bore 254 mm²) plus the lip dumps (111 mm²) cap the down-leg** at roughly 10–15 kg/s. That is consistent only if the bell-side split is meant to be small; state it.
- INT-18 and INT-19 are not re-checked and still stand.

**Verdict:** the topology is now right (separate, sealed coolant, fuel-gas and ox circuits), but line a's bore leaks to air and the fuel-side passages and the wide bell-top channels are not physically sized: not yet consistent, three major findings.
