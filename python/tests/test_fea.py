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
    r = fea.static(b, STEEL, fixed=[fea.fix("root", "x"), fea.fix(b.faces["bottom"], "z"), fea.fix(b.faces["front"], "y")],
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
    assert meta["kind"] == "overlay"
    roles = sorted(f["role"] for f in meta["faces"].values())
    assert roles == ["fixed", "load"]
    stress, disp = meta["fields"]
    assert stress["label"] == "von Mises stress" and stress["unit"] == "MPa" and stress["max"] >= r.stress_p995
    assert disp["unit"] == "mm" and disp["deform"] > 0 and disp["max"] == pytest.approx(r.max_displacement)
    g = np.load(os.path.join(folder, stress["file"]))
    assert g.dtype == np.float32 and g.ndim == 3 and g.max() == pytest.approx(r.stress.max(), rel=1e-5)
    assert np.load(os.path.join(folder, disp["file"])).shape[3] == 3
    assert "safety factor" in meta["info"]

    from tule_cli.machine import scene
    from tule_cli.render import render

    parts, overlay, caption = scene(folder, "displacement")
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
