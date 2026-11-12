"""tule.fea against hand calculations, on small grids."""
import json
import math
import os

import numpy as np
import pytest

from tule import *
from tule import fea

STEEL = fea.Material("test steel", E=200_000, nu=0.3, density=7.85, yield_strength=250)


def beam(L=100.0, B=10.0, H=10.0):
    b = box(L, B, H)
    return b.tag("root", b.faces["left"]).tag("tip", b.faces["right"])


# ---------------------------------------------------------------- materials and inputs


def test_materials_by_name_and_explicit():
    assert fea.material("PETG") is fea.MATERIALS["PETG"]
    assert fea.material("aluminum") is fea.material("Aluminium 6061-T6")
    assert fea.material("steel").E == 210_000 and fea.material("stainless").yield_strength == 215
    assert set(fea.MATERIALS) == {"PLA", "PETG", "ABS", "nylon PA12", "aluminium 6061-T6", "steel S235", "stainless 304"}
    assert fea.material(STEEL) is STEEL
    with pytest.raises(TuleError, match="unknown material") as e:
        fea.material("unobtainium")
    assert "PETG" in e.value.hint
    with pytest.raises(TuleError, match="nu"):
        fea.Material("bad", E=1000, nu=0.5, density=1, yield_strength=10)


def test_input_errors():
    b = beam()
    with pytest.raises(TuleError, match="no faces tagged 'base'") as e:
        fea.static(b, STEEL, fixed="base", loads=[fea.force("tip", (0, 0, -1))], verbose=False)
    assert "root" in e.value.hint
    with pytest.raises(TuleError, match="no loads"):
        fea.static(b, STEEL, fixed="root", verbose=False)
    with pytest.raises(TuleError, match="zero"):
        fea.force("tip", (0, 0, 0))
    with pytest.raises(TuleError, match="directions"):
        fea.fix("root", "w")
    with pytest.raises(TuleError, match="loads are"):
        fea.static(b, STEEL, fixed="root", loads=[(0, 0, -1)], verbose=False)


# ---------------------------------------------------------------- hand calculations


@pytest.mark.parametrize("voxel", [2.5, 2.3])  # on the grid, and with the tip face inside a voxel
def test_cantilever_matches_beam_theory(voxel):
    L, B, H, F = 100.0, 10.0, 10.0, 100.0
    r = fea.static(beam(L, B, H), STEEL, fixed="root", loads=[fea.force("tip", (0, 0, -F))], voxel=voxel, verbose=False)
    I = B * H ** 3 / 12
    tip = r.displacement_at([(L, B / 2, H / 2)])[0]
    assert -tip[2] == pytest.approx(F * L ** 3 / (3 * STEEL.E * I), rel=0.03)  # Euler–Bernoulli
    for x in (20, 50, 75):  # bending stress on the top surface: M c / I
        assert r.stress_at([(x, B / 2, H)])[0] == pytest.approx(F * (L - x) * (H / 2) / I, rel=0.03)
    root = F * L * (H / 2) / I  # 60 MPa at the root, where the clamp adds a local peak
    assert r.stress_p995 == pytest.approx(root, rel=0.15)
    assert r.max_stress >= r.stress_p995
    assert r.safety_factor == pytest.approx(STEEL.yield_strength / r.max_stress)
    # equilibrium
    assert np.allclose(r.applied, (0, 0, -F)) and np.allclose(r.reaction, (0, 0, F), atol=1e-4 * F)
    assert r.imbalance < 1e-6
    assert r.mass == pytest.approx(L * B * H * STEEL.density / 1000, rel=0.01)


def test_bar_in_tension():
    # held on three faces in one direction each: free to contract, so a plain uniaxial stress
    L, A, F = 60.0, 100.0, 2000.0
    b = beam(L)
    r = fea.static(b, STEEL, fixed=(fea.fix("root", "x"), fea.fix(b.faces["bottom"], "z"), fea.fix(b.faces["front"], "y")),  # a tuple works as a list does
                   loads=[fea.force("tip", (F, 0, 0))], voxel=2.0, verbose=False)
    assert r.stress_p995 == pytest.approx(F / A, rel=0.02)
    assert r.stress_at([(L / 2, 5, 5)])[0] == pytest.approx(F / A, rel=0.01)
    assert r.displacement_at([(L, 5, 5)])[0][0] == pytest.approx(F * L / (STEEL.E * A), rel=0.01)
    assert np.allclose(r.reaction, (-F, 0, 0), atol=1e-4 * F)


def test_self_weight_and_reactions():
    b = box(40, 20, 8)
    b = b.tag("floor", b.faces["bottom"])
    a = 5 * fea.g
    r = fea.static(b, STEEL, fixed="floor", accel=(0, 0, -a), voxel=2.0, verbose=False)
    weight = r.mass / 1000 * a / 1000  # kg · m/s²
    assert r.mass == pytest.approx(b.volume * STEEL.density / 1000, rel=0.01)
    assert r.applied[2] == pytest.approx(-weight, rel=0.01)
    assert r.reaction[2] == pytest.approx(weight, rel=1e-4)
    assert r.imbalance < 1e-6


def test_torsion_of_a_round_shaft():
    R, Ln, T = 5.0, 40.0, 1000.0
    s = cylinder(R, Ln)
    s = s.tag("base", s.faces["bottom"]).tag("end", s.faces["top"])
    r = fea.static(s, STEEL, fixed="base", loads=[fea.moment("end", (0, 0, T))], voxel=0.8, verbose=False)
    tau = 2 * T / (math.pi * R ** 3)  # T r / J at the surface
    assert np.linalg.norm(r.applied) < 1e-6 * T  # a couple: no net force
    assert np.linalg.norm(r.reaction) < 1e-4 * T
    at = [(R * math.cos(t), R * math.sin(t), Ln / 2) for t in np.linspace(0, 2 * math.pi, 12, endpoint=False)]
    assert np.mean(r.stress_at(at)) == pytest.approx(math.sqrt(3) * tau, rel=0.08)  # von Mises of pure shear (5 % high at 6 voxels per radius)
    G = STEEL.E / (2 * (1 + STEEL.nu))
    twist = r.displacement_at([(R, 0, Ln)])[0][1] / R  # the end's rotation (rad)
    assert twist == pytest.approx(T * Ln / (G * math.pi * R ** 4 / 2), rel=0.06)


def test_pressure_matches_force():
    b = beam(40)
    f = fea.static(b, STEEL, fixed="root", loads=[fea.force("tip", (-500, 0, 0))], voxel=2.0, verbose=False)
    p = fea.static(b, STEEL, fixed="root", loads=[fea.pressure("tip", 5.0)], voxel=2.0, verbose=False)
    assert np.allclose(p.applied, f.applied, atol=1e-6) and p.stress_p995 == pytest.approx(f.stress_p995, rel=1e-6)


# ---------------------------------------------------------------- output


def test_show_writes_fields_and_screenshot_draws_them(tmp_path, monkeypatch):
    monkeypatch.setenv("TULE_OUTPUTS", str(tmp_path))
    r = fea.static(beam(), "PLA", fixed="root", loads=[fea.force("tip", (0, 0, -20))], voxel=2.5, verbose=False)
    text = str(r)
    assert "peak" in text and "99.5th percentile" in text and "PLA" in text and "safety factor" in text
    folder = r.show("beam-fea")
    meta = json.load(open(os.path.join(folder, "output.json")))
    assert meta["kind"] == "overlay" and meta["fact"] == f"safety {r.material.yield_strength / r.stress_p995:.2g}"
    roles = sorted(f["role"] for f in meta["faces"].values())
    assert roles == ["fixed", "load"]
    stress, disp = meta["fields"]
    assert stress["label"] == "von Mises stress" and stress["unit"] == "MPa" and stress["max"] >= r.stress_p995
    assert disp["unit"] == "mm" and disp["deform"] > 0 and disp["max"] == pytest.approx(r.max_displacement)
    g = np.load(os.path.join(folder, stress["file"]))
    assert g.dtype == np.float32 and g.ndim == 3 and g.max() == pytest.approx(r.stress.max(), rel=1e-5)
    assert np.load(os.path.join(folder, disp["file"])).shape[3] == 3
    assert "info" not in meta and meta["fact"].startswith("safety ")

    from tule_cli.machine import scene
    from tule_cli.render import render

    parts, overlay, caption = scene(folder, field="displacement")
    assert overlay.scale["label"] == "displacement" and overlay.scale["deform"] == disp["deform"]
    assert parts[0].colors.shape == (len(parts[0].positions), 3)
    assert "also: von Mises stress" in caption
    parts, overlay, _ = scene(folder)
    assert overlay.scale["label"] == "von Mises stress" and overlay.scale["max"] == stress["max"]
    img, _ = render(parts, overlay=overlay, size=400)
    px = np.asarray(img.convert("RGB")).reshape(-1, 3).astype(int)
    assert ((px[:, 0] - px[:, 2] > 80) & (px[:, 1] < 120)).sum() > 50  # the root is red on the ramp


def test_overlay_fields_are_checked(tmp_path, monkeypatch):
    monkeypatch.setenv("TULE_OUTPUTS", str(tmp_path))
    part = box(10, 10, 10)
    grid = np.fromfunction(lambda i, j, k: i + 0.0, (6, 6, 6))
    folder = outputs.overlay("temp", part, {}, fields=[{"label": "temperature", "unit": "°C", "values": grid,
                                                        "origin": (0, 0, 0), "step": 2.0}])
    (entry,) = json.load(open(os.path.join(folder, "output.json")))["fields"]
    assert entry == {"label": "temperature", "file": "temperature.npy", "origin": [0.0, 0.0, 0.0], "step": 2.0,
                     "unit": "°C", "min": 0.0, "max": 5.0}
    assert np.array_equal(np.load(os.path.join(folder, "temperature.npy")), grid.astype(np.float32))
    with pytest.raises(TuleError, match="grid of nodes"):
        outputs.overlay("bad", part, {}, fields=[{"label": "t", "values": np.zeros((4, 4)), "origin": (0, 0, 0), "step": 1}])
    with pytest.raises(TuleError, match="only a vector field"):
        outputs.overlay("bad", part, {}, fields=[{"label": "t", "values": grid, "origin": (0, 0, 0), "step": 1, "deform": 2}])


# ---------------------------------------------------------------- bearing, patches, free parts, warnings


def lug(L=80.0, W=30.0, t=5.0, d=10.0, x=60.0):
    """A plate held at its left end with a hole for a pin at `x`."""
    b = box(L, W, t)
    p = b.tag("root", b.faces["left"]) - cylinder(d / 2, 3 * t).at(x, W / 2, -t)
    return p.tag("bore", p.faces.cylindrical(r=d / 2))


def test_bearing_is_a_cosine_on_the_loaded_half():
    p, F, R, t = lug(), 1000.0, 5.0, 5.0
    pts, w, nrm, _ = fea._surface(p.tessellate(0.05, 0.2), 0.3, p.faces["bore"].ids)
    v, area, projected = fea._load_vectors(fea.bearing("bore", (F, 0, 0)), pts, w, nrm)
    assert np.allclose(v.sum(0), (F, 0, 0), atol=1e-9 * F)  # exactly the force
    far = pts[:, 0] < 60 - 1e-6
    assert np.abs(v[far]).sum() < 2e-3 * F  # the far half carries nothing (bar facets at ±90°)
    theta = np.arctan2(pts[:, 1] - 15, pts[:, 0] - 60)
    mid = np.abs(theta) < 1.4
    p0 = 2 * F / (math.pi * R * t)  # p0 cos θ over the half: ∫ p0 cos² θ R t dθ = F
    assert np.abs(np.linalg.norm(v[mid], axis=1) / w[mid] - p0 * np.cos(theta[mid])).max() < 0.06 * p0
    assert projected == pytest.approx(2 * R * t, rel=1e-3) and area == pytest.approx(math.pi * R * t, rel=0.02)
    with pytest.raises(TuleError, match="along the bore's axis") as e:
        fea._load_vectors(fea.bearing("bore", (F, 0, 50)), pts, w, nrm)
    assert "shoulder" in e.value.hint
    top = fea._surface(p.tessellate(0.05, 0.2), 1.0, p.faces.nearest((40, 15, 5)).ids)
    with pytest.raises(TuleError, match="bore or a pin"):
        fea._load_vectors(fea.bearing("x", (F, 0, 0)), top[0], top[1], top[2])
    half = pts[:, 0] < 60
    with pytest.raises(TuleError, match="no part of these faces"):
        fea._load_vectors(fea.bearing("x", (F, 0, 0)), pts[half], w[half], nrm[half])


def test_bearing_on_a_lug_against_a_uniform_half():
    p, F = lug(), 1000.0
    kw = dict(fixed="root", voxel=0.6, verbose=False)
    br = fea.static(p, STEEL, loads=[fea.bearing("bore", (F, 0, 0))], **kw)
    # reference: the same force spread evenly over (about) the loaded half: a patch around the bore's +x side
    half = fea.static(p, STEEL, loads=[fea.force("bore", (F, 0, 0), at=(65, 15, 2.5), r=5 * math.sqrt(2))], **kw)
    whole = fea.static(p, STEEL, loads=[fea.force("bore", (F, 0, 0))], **kw)  # wrong: pulls the far side too
    assert np.allclose(br.applied, (F, 0, 0)) and br.imbalance < 1e-6 and br.warnings == ()
    assert br.stress_p995 == pytest.approx(half.stress_p995, rel=0.12)
    assert br.max_displacement == pytest.approx(half.max_displacement, rel=0.1)
    net = F / (2 * 10 * 5)  # net-section stress beside the hole; a pin-loaded lug's Kt there is about 2.5 to 3.5
    assert 2.2 < br.stress_p995 / net < 3.6
    far, near = br.stress_at([(54.8, 15, 2.5), (65.2, 15, 2.5)])
    assert far < 0.35 * near  # the pin does not touch the far side
    assert whole.stress_p995 < 0.6 * br.stress_p995  # a force on the whole bore understates it


def test_force_on_a_patch():
    L, B, H, F, a = 100.0, 10.0, 10.0, 100.0, 60.0
    b = beam(L, B, H)
    b = b.tag("top", b.faces["top"])
    r = fea.static(b, STEEL, fixed="root", loads=[fea.force("top", (0, 0, -F), at=(a, B / 2, H), r=3)], voxel=2.0, verbose=False)
    assert np.allclose(r.applied, (0, 0, -F), atol=1e-9 * F)
    tip = F * a ** 2 * (3 * L - a) / (6 * STEEL.E * B * H ** 3 / 12)  # a point load at a: the tip still rotates
    assert -r.displacement_at([(L, B / 2, H / 2)])[0, 2] == pytest.approx(tip, rel=0.02)
    assert r.warnings == () and "within 3 mm of (60, 5, 10) mm" in r.loads[0][0].label()
    with pytest.raises(TuleError, match="catches none of the loaded faces") as e:
        fea.static(b, STEEL, fixed="root", loads=[fea.force("top", (0, 0, -F), at=(50, 5, 30), r=3)], voxel=2.0, verbose=False)
    assert "20 mm away" in str(e.value)
    with pytest.raises(TuleError, match="both at= and r="):
        fea.force("top", (0, 0, -F), at=(50, 5, 10))
    with pytest.raises(TuleError, match="positive"):
        fea.force("top", (0, 0, -F), at=(50, 5, 10), r=0)
    tiny = fea.static(b, STEEL, fixed="root", loads=[fea.force("top", (0, 0, -F), at=(a, B / 2, H), r=0.4)], voxel=2.0, verbose=False)
    assert len(tiny.warnings) == 1 and "under 2 voxels across: a point load" in tiny.warnings[0]


def test_balanced_free_parts():
    L, A, F = 60.0, 100.0, 2000.0
    r = fea.static(beam(L), STEEL, loads=[fea.force("tip", (F, 0, 0)), fea.force("root", (-F, 0, 0))], balanced=True,
                   voxel=2.0, verbose=False)
    stretch = r.displacement_at([(L, 5, 5)])[0, 0] - r.displacement_at([(0, 5, 5)])[0, 0]
    assert stretch == pytest.approx(F * L / (STEEL.E * A), rel=1e-3)
    assert r.stress_at([(L / 2, 5, 5)])[0] == pytest.approx(F / A, rel=1e-3) and r.stress_p995 == pytest.approx(F / A, rel=0.01)
    assert np.linalg.norm(r.reaction) < 1e-5 * F and r.warnings == ()
    assert np.abs(r.displacement_at([(L / 2, 5, 5)])).max() < 1e-6  # relative to its mean position: the middle stays
    assert "free part (balanced=True)" in str(r)
    # a shaft twisted by opposite couples at its ends
    R, Ln, T = 5.0, 40.0, 1000.0
    s = cylinder(R, Ln)
    s = s.tag("base", s.faces["bottom"]).tag("end", s.faces["top"])
    r = fea.static(s, STEEL, loads=[fea.moment("end", (0, 0, T)), fea.moment("base", (0, 0, -T))], balanced=True,
                   voxel=0.8, verbose=False)
    twist = (r.displacement_at([(R, 0, Ln)])[0][1] - r.displacement_at([(R, 0, 0)])[0][1]) / R
    assert twist == pytest.approx(T * Ln / (STEEL.E / 2.6 * math.pi * R ** 4 / 2), rel=0.05)
    # the loads must balance
    with pytest.raises(TuleError, match=r"do not balance: net force \(2000, 0, 0\) N") as e:
        fea.static(beam(L), STEEL, loads=[fea.force("tip", (F, 0, 0))], balanced=True, voxel=2.0, verbose=False)
    assert "accel=(-4.246e+07, 0, 0)" in e.value.hint
    with pytest.raises(TuleError, match=r"net moment \(0, -1.2e\+05, 0\)"):
        fea.static(beam(L), STEEL, loads=[fea.force("tip", (0, 0, F)), fea.force("root", (0, 0, -F))], balanced=True,
                   voxel=2.0, verbose=False)
    with pytest.raises(TuleError, match="leave out fixed"):
        fea.static(beam(L), STEEL, fixed="root", loads=[fea.force("tip", (F, 0, 0))], balanced=True, verbose=False)
    with pytest.raises(TuleError, match="nothing holds the part") as e:
        fea.static(beam(L), STEEL, loads=[fea.force("tip", (F, 0, 0))], verbose=False)
    assert "balanced=True" in e.value.hint


def test_link_pulled_by_two_pins():
    lk = box(100, 20, 5) - cylinder(4, 20).at(10, 10, -5) - cylinder(4, 20).at(90, 10, -5)
    bores = lk.faces.cylindrical(r=4)
    lk = lk.tag("a", bores.nearest((10, 10, 2))).tag("b", bores.nearest((90, 10, 2)))
    r = fea.static(lk, STEEL, loads=[fea.bearing("a", (-1000, 0, 0)), fea.bearing("b", (1000, 0, 0))], balanced=True,
                   voxel=0.7, verbose=False)
    assert r.stress_at([(50, 10, 2.5)])[0] == pytest.approx(1000 / (20 * 5), rel=0.02)  # the shank: F / A
    assert min(abs(r.max_stress_at.x - 10), abs(r.max_stress_at.x - 90)) < 6  # at a hole


def test_warnings(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TULE_OUTPUTS", str(tmp_path))
    b = beam()
    # the clamp's corner peak: how far over 2x it lands depends on how the voxels fall (2.5x on macOS, 1.95x on Linux)
    (w,) = fea._peak_warning(250.0, (0, 10, 10), 100.0, "fixed", 500.0)
    assert "2.5× the 99.5th percentile, at the edge of a fixed face" in w and "safety factor 5" in w
    assert fea._peak_warning(150.0, (0, 0, 0), 100.0, "fixed", 500.0) == []
    hard = fea.static(b, STEEL, fixed="root", loads=[fea.force("tip", (0, 0, -1e5), at=(100, 5, 5), r=1)], voxel=2.5)
    assert any("past yield" in x for x in hard.warnings) and any("point load" in x for x in hard.warnings)
    assert capsys.readouterr().out.count("fea: warning:") == len(hard.warnings)  # printed once each, when solved
    assert "WARNING: the " in str(hard)
    assert json.load(open(os.path.join(hard.show("beam-warn"), "output.json")))["fact"].startswith("safety ")
    assert "residual of 1.0e-03" in fea._common_warnings(1e-3, 1e-6, 0.0, 0.0)[0]
    assert fea._common_warnings(1e-8, 1e-6, 1e-7, 0.0) == []
    assert "0.5 %" in fea._common_warnings(0.0, 1e-6, 5e-3, 0.0)[0]


def test_converge_reports_the_spread(capsys):
    c = fea.converge(beam(), STEEL, fixed="root", loads=[fea.force("tip", (0, 0, -100))], dofs=40_000, verbose=False)
    assert len(c.results) == 3 and c.results[0].dofs < c.results[1].dofs < c.result.dofs
    assert c.converged and c.spread("max_displacement") < 0.05
    assert "converged (within 5 %)" in str(c)
    # a block on a small foot: it tilts on the voxelized clamp, differently at each size
    p = box(40, 20, 10).at(0, 0, 2) | box(3, 3, 2.5).at(18.5, 8.5, 0)
    p = p.tag("foot", p.faces.nearest((20, 10, 0))).tag("top", p.faces.nearest((20, 10, 12)))
    c = fea.converge(p, STEEL, fixed="foot", loads=[fea.force("top", (0, 0, -100))], dofs=60_000, mesh="voxel", verbose=False)
    assert not c.converged and "NOT converged" in str(c)
    with pytest.raises(TuleError, match="sets the mesh"):
        fea.converge(beam(), STEEL, fixed="root", loads=[fea.force("tip", (0, 0, -1))], voxel=1.0)
