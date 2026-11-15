# Raptor: unknowns and modelling estimates

Nothing below is published for Raptor. Each item gives (a) what is known or visible, (b) a modelling estimate, and (c) its basis. Treat all numbers as plausible placeholders, not facts. Measured external dimensions are in `KNOWN.md` §4.

**General references:**
- [H&H] Huzel & Huang, *Design of Liquid Propellant Rocket Engines*, NASA SP-125 (2nd ed. 1971; AIAA rev. ed. 1992). https://ntrs.nasa.gov/citations/19710019929
- [S&B] Sutton & Biblarz, *Rocket Propulsion Elements*, 9th ed.
- [RS-25] RS-25/SSME figures as summarised in https://en.wikipedia.org/wiki/RS-25 : about 390 milled coolant channels in the NARloy-Z main combustion chamber (MCC) liner, a brazed stainless tube-wall nozzle, two-stage hot-gas turbines on both high-pressure pumps, and a 0.26 m throat.

## 1. Regenerative cooling channels

- **Visible**: the outer surfaces are smooth (no tube corrugations) on the chamber and the R3 bell (`014`, `012`). Copper is in the regen circuit (Musk, https://x.com/elonmusk/status/2121648438144024943), so the liner is a copper alloy (in the GRCop / NARloy class) inside a structural jacket. Close-up `013` shows print layer lines on the outer jacket and manifolds.
- **Channel count, chamber/throat**: **estimate 300–400 axial channels**. Basis: RS-25 MCC has about 390 channels at similar throat size (0.26 m vs Raptor ≈ 0.22–0.24 m) and higher heat flux per unit area with hydrogen. Methane cooling at 300–350 bar needs a similar or higher channel density. A good modelling default is **360 channels at the throat**, a number that splits cleanly in two for nozzle bifurcation.
- **Channel cross-section (copper liner)**:
  - Throat: about 1.0 mm wide × 2–3 mm deep, with lands about 1.0 mm.
  - Cylindrical chamber: about 1.5–2.0 mm wide × 3–5 mm deep.
  - Hot-gas wall thickness: about 0.7–1.0 mm.
  - Basis: [H&H] ch. 4 regen-design examples; RS-25 MCC channels (≈1 mm wide at the throat, with depth increasing away from the throat). Aspect ratio 2–5 is typical of milled copper liners.
- **Nozzle extension**: channels widen with radius or bifurcate. **Estimate 720 channels** (2 × 360) beyond area ratio ~5–8, about 2–4 mm wide × 3–6 mm deep, with a stainless or Inconel structural closeout. Basis: [H&H] channel bifurcation practice; Vulcain-2-style channel-wall (sandwich) nozzles.
- **Coolant direction**: Raptor diagrams (`107`, `132`, `134`) show methane entering a manifold partway down the nozzle and flowing both ways or counter-flow to the injector end. Model a coolant inlet manifold at the bell attach torus (about 1.15 m above the exit, `KNOWN.md` §4) and a return at the chamber top.

## 2. Channel-wall vs tube-wall nozzle

- **Visible**: both the R2 (`140`) and the R3 (`014`) bells have a smooth continuous outer skin with no tube scalloping, unlike RS-25 (`146`) and F-1 tube-wall nozzles.
- **Estimate**: **channel-wall (milled or printed channels with a brazed or welded closeout jacket)** over the whole sea-level bell. Confidence is moderate to high. RVac 3 is probably the same over its regen-cooled portion. Whether RVac 3 has an uncooled radiative extension is unknown; `031` does not resolve it.

## 3. Wall thicknesses (structural)

No public values exist. Outer-vs-gas-side comparisons from `KNOWN.md` §4:
- **Throat region**: outer Ø ≈ 0.36 m vs gas-side ≈ 0.22–0.24 m, so the radial build-up is ≈ 55–70 mm. That covers the liner, channels and jacket, plus the torus manifolds and printed structure around them. Suggested split: hot wall 1 mm + channel 2.5 mm + closeout 3–5 mm + structural jacket or ribs. The rest is manifold or flange.
- **Chamber**: outer Ø 0.45 m. Gas-side Ø is estimated at 0.33–0.38 m, using a contraction ratio Ac/At ≈ 2.5–3 typical of high-pc engines [H&H]. The resulting radial build-up is ≈ 35–60 mm.
- **Bell**: total wall about 6–12 mm near the top, thinning to 4–6 mm near the lip, plus a rolled lip stiffener. Basis: channel-wall nozzles at ε ≈ 34; no external stiffener bands are visible, so the closeout jacket must carry the hoop load itself.

## 4. Main injector

- **Known**: coaxial swirl elements (stated for Raptor 2, https://en.wikipedia.org/wiki/SpaceX_Raptor). Both propellants arrive as gas (FFSC). No main-chamber igniter on R2 (same source). Musk lists "main chamber spark igniters" among items internalised in R3 (https://x.com/elonmusk/status/2096292014824247360), so R3 apparently does have spark igniters, now integrated.
- **Element count**: **estimate 300–500 gas-gas coaxial elements**. A modelling default is about 400 on concentric rings over a face of about 0.33–0.36 m diameter.
- **Basis**: RS-25 main injector has about 600 coaxial elements on a 0.45 m-class face (widely cited from the Rocketdyne SSME orientation material). Vulcain 2 has 500+ coaxial elements. Ox-rich staged-combustion engines (RD-170/180) use a few hundred swirl elements per chamber. Raptor's face is smaller than SSME's but its pc is higher.
- **Element pitch**: about 12–16 mm.
- **Layout**: an outer row of film-cooling or fuel-rich elements. The community fact sheet mentions "head end and throat film cooling" (`132`).

## 5. Turbopumps and turbines

- **Known**: 1 oxygen and 1 methane turbopump; ox-rich turbine on the OTP, fuel-rich turbine on the FTP (https://en.wikipedia.org/wiki/SpaceX_Raptor). External OTP envelope ≈ Ø 0.42 m, on the axis under the LOX inlet. The FTP is off-axis (centre ≈ 0.41 m) (`KNOWN.md` §4).
- **Estimates**:
  - OTP: single-stage centrifugal pump (possibly with an inducer), single-stage axial impulse turbine.
  - FTP: two impeller stages. The community fact sheet says "two CH₄ impellers"; methane's low density makes two stages plausible. Turbine single-stage or two-stage.
  - Basis: RD-170 OTP has a single-stage turbine; RS-25 HPOTP and HPFTP have two-stage turbines; [H&H] ch. 6.
- **Blade counts (no data)**: turbine rotor about 50–90 blades, pitch-line diameter about 0.15–0.25 m; inducer 3–4 blades; impeller 6–8 main blades plus splitters. These are typical of 50–100 MW-class turbopumps ([H&H] ch. 6; IPD/SSME practice).
- **Shaft speeds**: the community fact sheet gives an OX shaft at ~20,000 rpm and a CH₄ shaft at ~35,000 rpm (`132`). Unverified.

## 6. Preburners

- **Known**: one ox-rich and one fuel-rich preburner (Wikipedia, above). They have torch igniters (R2-era statement). Hot oxygen-rich parts use SX500 superalloy (https://www.teslarati.com/spacex-superalloys-raptor-rocket-engine-elon-musk/).
- **Community claim (unverified)**: the ox preburner is a "transverse pintle type" and the fuel preburner a "coaxial type", per `132`.
- **Estimates**:
  - Ox-rich preburner: a short can of about 0.20–0.25 m inner diameter, directly under the OTP turbine on the engine axis. Turbine inlet temperature about 700–800 K. The community fact sheet's peak of 815 K agrees with [H&H] ox-rich practice.
  - Fuel-rich preburner: about 0.15–0.20 m inner diameter, under the FTP. It is probably the vertical bulbous duct seen on the fuel side (Ø ≈ 0.19 m, 1.36–2.0 m above the exit). That identification is uncertain.

## 7. Manifold and duct routing inside the R3 integrated structure

- **Visible externally**: the LOX inlet sits on the axis above the gimbal flange. The fuel inlet (blue cap) is on the FTP block. One large S-duct (Ø ≈ 0.13–0.15 m) runs from the FTP discharge down to a torus on the chamber. A vertical duct (Ø ≈ 0.19 m) is on the fuel side. There are four torus rings (chamber ×3 at 1.45/1.65/1.84 m above the exit, plus 1 at the bell top at 1.15 m).
- **Hidden (estimate from the cycle and the community diagram `132`)**:
  - The ox-rich gas from the OTP turbine goes straight down the axis into the injector "hot ox manifold" (a dome). There is no external duct, consistent with nothing visible.
  - The fuel-rich gas from the FTP turbine crosses horizontally through the base disc (y ≈ 740–790 in `012`, 1.97 m above the exit, 1.0 m wide) into an annular hot-fuel manifold around the injector. This explains why the base disc is asymmetric toward the FTP side.
  - The methane coolant feed most likely enters through the S-duct into the 1.45 m torus (inlet manifold near area ratio ~3–5). The coolant splits up and down; the return goes into the 1.84 m torus, then to the fuel preburner. The 1.15 m bell-top torus is the nozzle-extension coolant manifold or turnaround.
  - Secondary lines (purge, sensing, autogenous pressurisation, igniter feeds) are internal passages in the printed housings. Model them only as small bosses or ports.

## 8. Other gaps

- **Gimbal**: the range is unpublished (Raptor commonly ≈ ±15°, unsourced). The bearing type and location (assumed at the top flange, about 2.65 m above the exit) are unknown. Actuator clevises are visible at y ≈ 640–680 in `012` (≈ 2.2 m above the exit, on the left of the powerhead).
- **Nozzle contour**: the exact bell shape (Rao parabola parameters) is not published. Fit a Rao optimum contour with throat Ø 0.23 m, ε ≈ 34, length ≈ 1.55 m (≈ 75–80 % bell), entrance angle θn ≈ 30–33°, exit angle θe ≈ 8–10°. Match the visible profile in `012`: Ø 0.71 m at 0.73 m below the bell attach ring, rising to 1.30 m at the lip.
- **Chamber length, injector face to throat**: L* ≈ 0.8–1.0 m is typical for LOX/CH₄ [H&H]. With At ≈ 0.042 m², chamber volume is ≈ 0.035–0.042 m³. With a gas-side chamber Ø ≈ 0.35 m, the cylindrical length plus convergent comes to about 0.35–0.45 m, consistent with the visible ≈ 0.39 m.
- **RVac 3 dimensions**: only `031` (a skirt camera view) was found. Use RVac exit Ø 2.4 m (Wikipedia, R2-era) as the default.
