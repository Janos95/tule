"""tule.fea on quadratic tetrahedra (tule.tetfe) against hand calculations, on small meshes.

The element, quadrature, thickness rays and output format need nothing extra; meshing needs
the gmsh binary (``apt-get install gmsh``; ``$TULE_GMSH`` points at another), else those tests skip.
"""
import json
import math
import os

import numpy as np
import pytest

from tule import *
from tule import fea, tetfe

STEEL = fea.Material("test steel", E=200_000, nu=0.3, density=7.85, yield_strength=250)
NU0 = fea.Material("test steel, nu 0", E=200_000, nu=0.0, density=7.85, yield_strength=250)  # beam = plate theory

needs_gmsh = pytest.mark.skipif(not tetfe.find_gmsh(), reason="gmsh is not installed (apt-get install gmsh, or set TULE_GMSH)")


def beam(L=100.0, B=10.0, H=10.0):
    b = box(L, B, H)
    return b.tag("root", b.faces["left"]).tag("tip", b.faces["right"])


def flexure(t=0.4, gap=0.35, L=15.0, H=10.0):
    """Two blades `t` thick between a base and a top block, with a tongue from the base between
    them across wire-EDM gaps `gap` wide: a parallel-guided stage, cut from one part."""
    base, top = box(30, 10, H), box(30, 10, H).at(0, 10 + L, 0)
    p = base | top | box(t, L, H).at(2, 10, 0) | box(t, L, H).at(28 - t, 10, 0) \
        | box(28 - 2 * t - 2 - 2 * gap, L - gap, H).at(2 + t + gap, 10, 0)
    return p.tag("base", p.faces.nearest((15, 0, 5))).tag("top", p.faces.nearest((15, 20 + L, 5)))


# ---------------------------------------------------------------- the element (no gmsh)


def cube_mesh(n=2, size=1.0):
    """A cube of n³ cells, 6 straight quadratic tets each (a hand-made TetMesh)."""
    g = np.linspace(0, size, n + 1)
    P = np.array([(x, y, z) for x in g for y in g for z in g])
    idx = lambda i, j, k: (i * (n + 1) + j) * (n + 1) + k  # noqa: E731
    tets = []
    for i in range(n):
        for j in range(n):
            for k in range(n):
                c = [idx(i + a, j + b, k + d) for a in (0, 1) for b in (0, 1) for d in (0, 1)]
                for t in ((0, 1, 3, 7), (0, 3, 2, 7), (0, 2, 6, 7), (0, 6, 4, 7), (0, 4, 5, 7), (0, 5, 1, 7)):
                    tets.append([c[v] for v in t])
    tets = np.array(tets)
    mids, nodes = {}, list(map(tuple, P))
    full = []
    for t in tets:
        row = list(t)
        for a, b in tetfe.TET_EDGES:
            key = tuple(sorted((t[a], t[b])))
            if key not in mids:
                mids[key] = len(nodes)
                nodes.append(tuple((P[t[a]] + P[t[b]]) / 2))
            row.append(mids[key])
        full.append(row)
    nodes = np.array(nodes)
    full = np.array(full)
    _, det = tetfe._jacobians(nodes, full, tetfe.GP4)
    flip = det[:, 0] < 0  # orient every tet positively
    full[flip] = full[flip][:, [0, 2, 1, 3, 6, 5, 4, 7, 9, 8]]
    return tetfe.TetMesh(nodes, full, np.zeros((0, 6), np.int64), np.zeros(0, np.int64), size / n, size / n, 0.0)


def test_shape_functions_and_quadrature():
    L = np.random.default_rng(1).dirichlet(np.ones(4), 20)
    N = tetfe.shape(L)
    assert np.allclose(N.sum(1), 1) and np.allclose(tetfe.shape_grad(L).sum(1), 0)
    # the conical rule integrates degree-5 monomials exactly: ∫ x^a y^b z^c = a! b! c! / (a + b + c + 3)!
    Lq, w = tetfe.GP27, tetfe.W27
    for a, b, c in ((0, 0, 0), (2, 1, 0), (1, 1, 3), (5, 0, 0), (2, 2, 1)):
        exact = math.factorial(a) * math.factorial(b) * math.factorial(c) / math.factorial(a + b + c + 3)
        assert (w * Lq[:, 1] ** a * Lq[:, 2] ** b * Lq[:, 3] ** c).sum() == pytest.approx(exact, rel=1e-12)


def test_patch_test_and_mass():
    m = cube_mesh(2, 4.0)
    model = tetfe.Model(m)
    A = np.array([[1e-3, 2e-4, 0], [-1e-4, 3e-4, 5e-4], [2e-4, 0, -2e-3]])
    u = m.nodes @ A.T  # a linear field: constant strain, so every element's stress is exact
    sig = model.nodal(model.gauss_stress(u, 1000.0, 0.25))
    e = 0.5 * (A + A.T)
    eps = np.array([e[0, 0], e[1, 1], e[2, 2], 2 * e[0, 1], 2 * e[1, 2], 2 * e[2, 0]])
    assert np.allclose(sig, tetfe.D_matrix(1000.0, 0.25) @ eps, atol=1e-12)
    K = model.stiffness(1000.0, 0.25)
    assert abs(K - K.T).max() < 1e-9 * abs(K).max()
    rigid = np.tile([1.0, -2.0, 0.5], len(m.nodes))
    assert np.abs(K @ rigid).max() < 1e-9 * abs(K).max()
    M = model.mass(2.0)
    assert (np.ones(3 * len(m.nodes)) @ M @ np.ones(3 * len(m.nodes))) / 3 == pytest.approx(2.0 * 64, rel=1e-12)
    assert model.volume == pytest.approx(64, rel=1e-12)


def test_thickness_finds_blades_and_slots():
    th = fea.thinnest(flexure())
    assert th.min_wall == pytest.approx(0.4, abs=1e-6) and th.min_gap == pytest.approx(0.35, abs=1e-6)
    assert th.seconds < 5
    plate = box(60, 40, 5) - cylinder(4, 20).at(30, 20, -5)  # a hole is not a thin wall (its chords are inside it)
    th = fea.thinnest(plate)
    assert th.min_wall == pytest.approx(5, rel=1e-6) and th.min_gap == pytest.approx(8, rel=0.01)


def test_auto_picks_tetrahedra_for_thin_walls(monkeypatch):
    say = lambda *a: None  # noqa: E731
    monkeypatch.setattr(tetfe, "find_gmsh", lambda: "/usr/bin/gmsh")
    assert fea._choose_mesh(flexure(), "auto", None, None, 300_000, say) == "tet"
    assert fea._choose_mesh(beam(), "auto", None, None, 300_000, say) == "voxel"
    assert fea._choose_mesh(flexure(), "auto", 0.2, None, 300_000, say) == "voxel"  # voxel= asks for voxels
    monkeypatch.setattr(tetfe, "find_gmsh", lambda: None)
    assert fea._choose_mesh(flexure(), "auto", None, None, 300_000, say) == "voxel"
    with pytest.raises(TuleError, match="mesh="):
        fea._choose_mesh(beam(), "hex", None, None, 300_000, say)


def test_surface_fields_in_outputs_and_screenshot(tmp_path, monkeypatch):
    monkeypatch.setenv("TULE_OUTPUTS", str(tmp_path))
    part = box(10, 10, 10)
    P = np.array([(0, 0, 0), (10, 0, 0), (0, 10, 0), (10, 10, 0)], float)
    T = np.array([(0, 1, 2), (1, 3, 2)])
    mesh = (P, T, np.array([4, 4]))
    folder = outputs.overlay("surf", part, {}, fields=[
        {"label": "temperature", "unit": "°C", "values": P[:, 0], "mesh": mesh},
        {"label": "motion", "values": np.ones((4, 3)), "mesh": mesh, "deform": 0.5, "animate": True}])
    a, b = json.load(open(os.path.join(folder, "output.json")))["fields"]
    assert a == {"label": "temperature", "file": "temperature.npy", "unit": "°C", "min": 0.0, "max": 10.0,
                 "mesh": {"vertices": "surface.vertices.npy", "triangles": "surface.triangles.npy", "faces": "surface.faces.npy"}}
    assert b["mesh"] == a["mesh"] and b["deform"] == 0.5 and b["animate"] is True  # one mesh, written once
    assert np.load(os.path.join(folder, "surface.triangles.npy")).dtype == np.dtype("<i4")
    with pytest.raises(TuleError, match="one per vertex"):
        outputs.overlay("bad", part, {}, fields=[{"label": "t", "values": np.zeros(3), "mesh": mesh}])
    with pytest.raises(TuleError, match="give deform"):
        outputs.overlay("bad", part, {}, fields=[{"label": "t", "values": np.zeros((4, 3)), "mesh": mesh, "animate": True}])
    with pytest.raises(TuleError, match="either origin and step"):
        outputs.overlay("bad", part, {}, fields=[{"label": "t", "values": np.zeros(4), "mesh": mesh, "step": 1}])

    from tule_cli.machine import scene

    parts, overlay, caption = scene(folder, field="motion")
    assert overlay.scale["deform"] == 0.5 and np.allclose(parts[0].positions[:, 2], 0.5)  # moved by 0.5 × (1, 1, 1)
    assert len(parts[0].indices) == 2 and set(parts[0].face_ids) == {4}


# ---------------------------------------------------------------- analyses (gmsh)


@needs_gmsh
def test_cantilever_on_tetrahedra():
    L, B, H, F = 100.0, 10.0, 10.0, 100.0
    r = fea.static(beam(L, B, H), STEEL, fixed="root", loads=[fea.force("tip", (0, 0, -F))], mesh="tet",
                   dofs=25_000, verbose=False)
    I = B * H ** 3 / 12
    assert isinstance(r, fea.TetResult) and r.dofs < 40_000
    assert -r.displacement_at([(L, B / 2, H / 2)])[0, 2] == pytest.approx(F * L ** 3 / (3 * STEEL.E * I), rel=0.01)
    assert r.stress_at([(L / 2, B / 2, H)])[0] == pytest.approx(F * (L / 2) * (H / 2) / I, rel=0.02)
    assert r.imbalance < 1e-6 and r.reaction.z == pytest.approx(F, rel=1e-6)
    assert r.mass == pytest.approx(78.5) and 2 < r.safety_factor < 6
    assert "quadratic tetrahedra" in str(r)


@needs_gmsh
def test_flexure_blades_are_resolved():
    # guided by a stiff top block: k = 2 × 12 E I / L³; the blades' roots in the blocks add a few %
    r = fea.static(flexure(), NU0, fixed="base", loads=[fea.force("top", (1, 0, 0))], dofs=60_000, verbose=False)
    assert isinstance(r, fea.TetResult)  # auto: 0.4 mm blades and 0.35 mm gaps are under the voxels
    k = 1 / r.displacement_at([(15, 30, 5)])[0, 0]
    assert k == pytest.approx(24 * NU0.E * (10 * 0.4 ** 3 / 12) / 15 ** 3, rel=0.08)
    assert r.max_stress_at.x < 3 or r.max_stress_at.x > 27  # in a blade, not the tongue: the slots are open


@needs_gmsh
def test_plate_with_a_hole():
    W, t, d = 100.0, 5.0, 10.0
    b = box(200, W, t)
    b = b.tag("left", b.faces["left"]).tag("right", b.faces["right"])
    plate = b - cylinder(d / 2, 3 * t).at(100, W / 2, -t)
    r = fea.static(plate, NU0, fixed="left", loads=[fea.pressure("right", -10.0)], mesh="tet", dofs=40_000, verbose=False)
    kt = (2 + (1 - d / W) ** 3) / (1 - d / W)  # Heywood, on the gross section: 3.03
    assert r.max_stress / 10 == pytest.approx(kt, rel=0.04)
    assert math.hypot(r.max_stress_at.x - 100, r.max_stress_at.y - W / 2) == pytest.approx(d / 2, abs=0.2)


@needs_gmsh
def test_tetrahedra_agree_with_voxels_on_a_chunky_part():
    br = box(60, 30, 10) | box(10, 30, 40)
    br = br.tag("mount", br.faces.nearest((30, 15, 0))).tag("load", br.faces.nearest((5, 15, 40)))
    kw = dict(fixed="mount", loads=[fea.force("load", (300, 0, 0))], verbose=False)
    v = fea.static(br, STEEL, mesh="voxel", dofs=150_000, **kw)  # voxels converge slower: 40k is 9 % stiff
    t = fea.static(br, STEEL, mesh="tet", dofs=40_000, **kw)
    assert t.max_displacement == pytest.approx(v.max_displacement, rel=0.03)
    assert t.reaction.x == pytest.approx(v.reaction.x, rel=1e-6)


@needs_gmsh
def test_modes_of_a_cantilever(tmp_path, monkeypatch):
    monkeypatch.setenv("TULE_OUTPUTS", str(tmp_path))
    L = 150.0
    md = fea.modes(beam(L), STEEL, fixed="root", n=3, dofs=20_000, verbose=False)
    f1 = 1.875104 ** 2 / (2 * math.pi * L ** 2) * math.sqrt(STEEL.E * (10 * 10 ** 3 / 12) / (STEEL.density * 1e-9 * 100))
    assert md.frequencies[0] == pytest.approx(f1, rel=0.01) and md.frequencies[1] == pytest.approx(f1, rel=0.01)
    assert md.frequencies[2] > 5 * f1  # the second bending pair (6.27 f1) or torsion
    assert md.shapes.shape == (3, len(md.mesh.nodes), 3) and np.abs(np.linalg.norm(md.shapes, axis=2).max(1) - 1).max() < 1e-6
    assert md.effective_mass[0][1:].sum() > 0.5  # mode 1 swings the beam sideways: about 60 % of its mass
    assert "mode 1" in str(md)
    folder = md.show("beam-modes")
    meta = json.load(open(os.path.join(folder, "output.json")))
    assert [f["label"].split(":")[0] for f in meta["fields"]] == ["mode 1", "mode 2", "mode 3"]
    assert all(f["animate"] and f["deform"] > 0 and f["mesh"] == meta["fields"][0]["mesh"] for f in meta["fields"])


@needs_gmsh
def test_euler_buckling():
    col = box(5, 5, 100)
    col = col.tag("bottom", col.faces["bottom"]).tag("top", col.faces["top"])
    bk = fea.buckling(col, STEEL, fixed="bottom", loads=[fea.force("top", (0, 0, -1000))], n=2, dofs=20_000, verbose=False)
    P = math.pi ** 2 * STEEL.E * (5 ** 4 / 12) / (4 * 100 ** 2)  # fixed-free: 2570 N
    assert bk.factor * 1000 == pytest.approx(P, rel=0.01) and bk.factors[1] * 1000 == pytest.approx(P, rel=0.01)
    pulled = fea.buckling(col, STEEL, fixed="bottom", loads=[fea.force("top", (0, 0, 1000))], n=2, dofs=20_000, verbose=False)
    assert pulled.factor == math.inf and "reversed, the loads buckle it at 2.57 ×" in str(pulled)  # in tension


@needs_gmsh
def test_tet_show_writes_surface_fields(tmp_path, monkeypatch):
    monkeypatch.setenv("TULE_OUTPUTS", str(tmp_path))
    r = fea.static(flexure(), "aluminium", fixed="base", loads=[fea.force("top", (5, 0, 0))], dofs=30_000, verbose=False)
    folder = r.show("flexure-fea")
    meta = json.load(open(os.path.join(folder, "output.json")))
    stress, disp = meta["fields"]
    assert stress["mesh"] == disp["mesh"] and disp["deform"] > 0
    V = np.load(os.path.join(folder, stress["mesh"]["vertices"]))
    F = np.load(os.path.join(folder, stress["mesh"]["faces"]))
    assert np.load(os.path.join(folder, stress["file"])).shape == (len(V),)
    assert set(np.unique(F)) == set(range(len(r.part.faces)))  # every B-rep face, by its id

    from tule_cli.machine import scene
    from tule_cli.render import render

    parts, overlay, _ = scene(folder)
    img, _ = render(parts, "top", 400, False, overlay)
    px = np.asarray(img.convert("RGB")).reshape(-1, 3).astype(int)
    assert ((px[:, 2] - px[:, 0] > 80)).sum() > 1000  # mostly blue: low stress away from the blades


@needs_gmsh
def test_loose_pieces_are_refused():
    two = box(10, 10, 10) | box(10, 10, 10).at(20, 0, 0)
    two = two.tag("fixed", two.faces.nearest((0, 5, 5))).tag("load", two.faces.nearest((30, 5, 5)))
    with pytest.raises(TuleError, match="touch no fixed face"):
        fea.static(two, STEEL, fixed="fixed", loads=[fea.force("load", (1, 0, 0))], mesh="tet", dofs=5_000, verbose=False)
