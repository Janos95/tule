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
def test_free_modes_of_a_beam(tmp_path, monkeypatch):
    monkeypatch.setenv("TULE_OUTPUTS", str(tmp_path))
    L, al = 200.0, fea.material("aluminium 6061-T6")
    md = fea.modes(box(L, 20, 10), al, fixed=None, n=4, dofs=20_000, verbose=False)
    # Euler-Bernoulli, free-free, bending across the 10 mm
    f1 = 4.730 ** 2 / (2 * math.pi * L ** 2) * math.sqrt(al.E * 10 ** 2 / 12 / (al.density * 1e-9))
    assert md.frequencies[0] == pytest.approx(f1, rel=0.03)  # the six rigid-body modes are left out
    assert md.frequencies[1] == pytest.approx(2 * f1, rel=0.05)  # across the 20 mm
    lines = str(md).splitlines()
    assert "bends in the x-z plane" in lines[1] and "bends in the x-y plane" in lines[2]
    assert "twists about x" in lines[4]  # after the second x-z bending mode
    assert md.motion[0]["bend x-z"] > 0.9 and md.motion[3]["twist x"] > 0.5  # the shares, as printed
    assert set(md.motion[0]) == {"bend x-y", "bend x-z", "twist x", "stretch x", "other"}
    meta = json.load(open(os.path.join(md.show("free-beam", labels=["a", "b", "c", "d"]), "output.json")))
    assert [f["label"] for f in meta["fields"]] == ["a", "b", "c", "d"]
    with pytest.raises(TuleError, match="labels"):
        md.show("x", labels=["a"])


@needs_gmsh
def test_under_resolved_walls_warn_even_quietly(capsys):
    # a 2 mm plate on a small budget: under one element through its thickness
    md = fea.modes(box(300, 300, 2), "aluminium 6061-T6", fixed=None, n=1, dofs=3_000, verbose=False)
    assert md.warnings and "through their thickness" in md.warnings[0] and "too stiff" in md.warnings[0]
    assert "fea: warning: walls down to" in capsys.readouterr().out
    assert "  warning: walls down to" in str(md) and not any("through their" in n for n in md.notes)


@needs_gmsh
def test_mesh_keeps_near_the_dof_budget():
    p = box(150, 30, 6)
    for x in (20, 50, 80, 110, 130):
        p = p - cylinder(2, 6).at(x, 15, 0)
    m = fea._tet_mesh(p, 20_000, None, lambda *a: None, [])
    assert 3 * len(m.nodes) <= 1.3 * 20_000


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


@needs_gmsh
def test_bearing_patch_and_free_parts_on_tetrahedra():
    # a lug: a pin's bearing against the same force spread evenly over (about) the loaded half of the bore
    b = box(80, 30, 5)
    lug = b.tag("root", b.faces["left"]) - cylinder(5, 15).at(60, 15, -5)
    lug = lug.tag("bore", lug.faces.cylindrical(r=5))
    kw = dict(fixed="root", mesh="tet", dofs=40_000, verbose=False)
    br = fea.static(lug, STEEL, loads=[fea.bearing("bore", (1000, 0, 0))], **kw)
    half = fea.static(lug, STEEL, loads=[fea.force("bore", (1000, 0, 0), at=(65, 15, 2.5), r=5 * math.sqrt(2))], **kw)
    assert np.allclose(br.applied, (1000, 0, 0), atol=1e-9) and br.warnings == ()
    assert br.stress_p995 == pytest.approx(half.stress_p995, rel=0.12) and 2.2 < br.stress_p995 / 10 < 3.6
    far, near = br.stress_at([(54.8, 15, 2.5), (65.2, 15, 2.5)])
    assert far < 0.35 * near
    # a force on a patch of the top of a cantilever: a point load at 60 mm (the mesh is refined there)
    bm = beam()
    bm = bm.tag("top", bm.faces["top"])
    r = fea.static(bm, STEEL, fixed="root", loads=[fea.force("top", (0, 0, -100), at=(60, 5, 10), r=3)], mesh="tet",
                   dofs=25_000, verbose=False)
    assert np.allclose(r.applied, (0, 0, -100), atol=1e-9)
    assert -r.displacement_at([(100, 5, 5)])[0, 2] == pytest.approx(100 * 60 ** 2 * 240 / (6 * STEEL.E * 1e4 / 12), rel=0.01)
    assert r.warnings == ()
    hard = fea.static(bm, STEEL, fixed="root", loads=[fea.force("top", (0, 0, -1000), at=(60, 5, 10), r=0.5)], mesh="tet",
                      dofs=25_000, verbose=False)
    assert any("past yield" in w for w in hard.warnings)  # about 1000 MPa under the patch
    # a bar pulled at both ends, held by nothing
    r = fea.static(beam(), STEEL, loads=[fea.force("tip", (2000, 0, 0)), fea.force("root", (-2000, 0, 0))], balanced=True,
                   mesh="tet", dofs=20_000, verbose=False)
    stretch = r.displacement_at([(100, 5, 5)])[0, 0] - r.displacement_at([(0, 5, 5)])[0, 0]
    assert stretch == pytest.approx(2000 * 100 / (STEEL.E * 100), rel=1e-4)
    assert r.stress_p995 == pytest.approx(20, rel=1e-4) and np.linalg.norm(r.reaction) < 1e-4 and r.warnings == ()
    with pytest.raises(TuleError, match="do not balance"):
        fea.static(beam(), STEEL, loads=[fea.force("tip", (2000, 0, 0))], balanced=True, mesh="tet", dofs=20_000, verbose=False)
    two = box(10, 10, 10) | box(10, 10, 10).at(20, 0, 0)
    two = two.tag("a", two.faces.nearest((0, 5, 5))).tag("b", two.faces.nearest((30, 5, 5)))
    with pytest.raises(TuleError, match="one piece"):
        fea.static(two, STEEL, loads=[fea.force("a", (-1, 0, 0)), fea.force("b", (1, 0, 0))], balanced=True, mesh="tet",
                   dofs=5_000, verbose=False)


# ---------------------------------------------------------------- large displacements


def test_nonlinear_tangent_is_consistent():
    m = cube_mesh(2, 4.0)
    model = tetfe.Model(m)
    E, nu = 1000.0, 0.3
    f0, K0, (det0, e0) = model.tangent(np.zeros((model.n, 3)), E, nu)
    assert abs(K0 - model.stiffness(E, nu)).max() < 1e-12 * abs(K0).max() and abs(f0).max() == 0 and (det0, e0) == (1, 0)
    rng = np.random.default_rng(1)
    u, v = 0.2 * rng.standard_normal((model.n, 3)), rng.standard_normal((model.n, 3))
    f, K, _ = model.tangent(u, E, nu)
    h = 1e-6
    fd = (model.tangent(u + h * v, E, nu, stiffness=False)[0] - model.tangent(u - h * v, E, nu, stiffness=False)[0]) / (2 * h)
    assert np.abs(fd.reshape(-1) - K @ v.reshape(-1)).max() < 1e-8 * np.abs(fd).max()  # the tangent is f's derivative
    assert abs(K - K.T).max() < 1e-12 * abs(K).max()
    th = 0.7  # a rigid rotation strains nothing
    R = np.array([[np.cos(th), -np.sin(th), 0], [np.sin(th), np.cos(th), 0], [0, 0, 1]])
    ur = m.nodes @ R.T - m.nodes
    assert np.abs(model.tangent(ur, E, nu, stiffness=False)[0]).max() < 1e-9 and np.abs(model.cauchy(ur, E, nu)).max() < 1e-9
    assert model.rotation(0.01 * ur).max() == pytest.approx(0.01 * np.sin(th), rel=1e-9)  # small-rotation angle
    free = np.ones(3 * model.n, bool)
    free[::7] = False
    idx = np.flatnonzero(free)
    blocks = model.tangent(u, E, nu, blocks=True)[1]
    assert abs(model.reducer(free)(blocks) - K[idx][:, idx]).max() == 0
    # the stability check: a negative Rayleigh quotient of A against B proves negative stiffness
    import scipy.sparse as sp

    n = 400
    B = sp.identity(n, format="csr")
    pos, neg = sp.diags(np.linspace(1, 3, n)).tocsr(), sp.diags(np.linspace(-0.5, 3, n)).tocsr()
    assert fea._softest(pos, B, lambda x: x, n) >= 1 - 1e-9 and fea._softest(neg, B, lambda x: x, n) < -0.3


def test_move_and_nonlinear_inputs(monkeypatch):
    with pytest.raises(TuleError, match="directions"):
        fea.move("top", (1, 0, 0), "w")
    with pytest.raises(TuleError, match="zero in the directions"):
        fea.move("top", (1, 0, 0), "y")
    mv = fea.move("tip", (2, 0, 0), "x")
    assert mv.dirs == "x" and mv.label() == "moved (2, 0, 0) mm in x"
    say = lambda *a: None  # noqa: E731
    monkeypatch.setattr(tetfe, "find_gmsh", lambda: "/usr/bin/gmsh")
    assert fea._choose_mesh(beam(), "auto", None, None, 300_000, say, need_tet="nonlinear=True") == "tet"
    for kw in (dict(mesh="voxel"), dict(voxel=1.0)):
        with pytest.raises(TuleError, match="nonlinear=True runs on tetrahedra"):
            fea.static(beam(), STEEL, fixed="root", loads=[fea.force("tip", (0, 0, -1))], nonlinear=True, verbose=False, **kw)
        with pytest.raises(TuleError, match="fea.move runs on tetrahedra"):
            fea.static(beam(), STEEL, fixed=["root", mv], verbose=False, **kw)
    with pytest.raises(TuleError, match="steps= is for nonlinear"):
        fea.static(beam(), STEEL, fixed="root", loads=[fea.force("tip", (0, 0, -1))], steps=4, verbose=False)
    with pytest.raises(TuleError, match="needs the part held"):
        fea.static(beam(), STEEL, loads=[fea.force("tip", (1, 0, 0)), fea.force("root", (-1, 0, 0))], balanced=True,
                   nonlinear=True, verbose=False)
    with pytest.raises(TuleError, match="fea.move is for fea.static"):
        fea.modes(beam(), STEEL, fixed=["root", mv], verbose=False)


def elastica(alpha):
    """The tip of a cantilever under a dead end load P (PL²/EI = alpha), from the elastica
    θ'' = -α cos θ, θ(0) = 0, θ'(1) = 0 by shooting (Bisshopp and Drucker's problem): (x, y) / L."""
    from scipy.integrate import solve_ivp
    from scipy.optimize import brentq

    def shoot(k):
        rhs = lambda s, y: [y[1], -alpha * math.cos(y[0]), math.cos(y[0]), math.sin(y[0])]  # noqa: E731
        return solve_ivp(rhs, (0, 1), [0, k, 0, 0], rtol=1e-11, atol=1e-12).y[:, -1]

    y = shoot(brentq(lambda k: shoot(k)[1], 0, alpha))
    return y[2], y[3]


@needs_gmsh
def test_large_deflection_of_a_cantilever():
    # Bisshopp and Drucker: tip deflection at PL²/EI = 1, 2, 5 is 0.3017, 0.4935, 0.7138 L down, 0.0564, 0.1606, 0.3876 L in
    L, h = 100.0, 2.5  # slender (strains under 6 %, where St. Venant-Kirchhoff bends like a beam)
    bar = box(L, h, h)
    bar = bar.tag("root", bar.faces["left"]).tag("tip", bar.faces["right"])
    EI = NU0.E * h ** 4 / 12
    for alpha in (1, 2, 5):
        r = fea.static(bar, NU0, fixed="root", loads=[fea.force("tip", (0, 0, -alpha * EI / L ** 2))], nonlinear=True,
                       dofs=7_000, verbose=False)
        x, y = elastica(alpha)
        d = r.displacement_at([(L, h / 2, h / 2)])[0]
        assert -d[2] / L == pytest.approx(y, rel=0.01) and -d[0] / L == pytest.approx(1 - x, rel=0.01)
        assert r.nonlinear and r.steps[-1].fraction == 1 and all("strains reach" in w for w in r.warnings)
        assert r.linear_max_displacement == pytest.approx(alpha * L / 3, rel=0.01)  # what linear theory says
    assert len(r.steps) >= 6 and all(a.fraction < b.fraction for a, b in zip(r.steps, r.steps[1:]))
    assert r.steps[-1].reaction.z == pytest.approx(5 * EI / L ** 2, rel=1e-6)
    path = [-r.displacement_at([(L, h / 2, h / 2)], step=i)[0, 2] for i in range(len(r.steps))]
    assert all(a < b for a, b in zip(path, path[1:])) and path[-1] == pytest.approx(-d[2])
    assert "geometrically nonlinear" in str(r) and "linear theory" in str(r)


@needs_gmsh
def test_small_loads_nonlinear_is_linear():
    kw = dict(fixed="root", loads=[fea.force("tip", (0, -20, -100))], mesh="tet", dofs=10_000, verbose=False)
    r = fea.static(beam(), STEEL, nonlinear=True, **kw)
    lin = fea.static(beam(), STEEL, **kw)
    assert r.max_displacement == pytest.approx(r.linear_max_displacement, rel=1e-3)  # on the same mesh
    assert r.max_displacement == pytest.approx(lin.max_displacement, rel=0.01)  # on another
    assert r.stress_p995 == pytest.approx(lin.stress_p995, rel=0.02) and r.imbalance < 1e-6
    assert len(r.steps) == 4 and max(s.iterations for s in r.steps) <= 2  # 4 steps (the path), each converged at once


@needs_gmsh
def test_flexure_driven_by_a_displacement(tmp_path, monkeypatch):
    # a parallel flexure's stage pushed 1.5 mm (10 % of its 15 mm blades): free to drop (y) it shortens by
    # 3/5 d²/L and the force stays linear; held at its height the blades stretch and it stiffens hard
    monkeypatch.setenv("TULE_OUTPUTS", str(tmp_path))
    d, L = 1.5, 15.0
    kw = dict(dofs=40_000, verbose=False)
    lin = fea.static(flexure(), "PETG", fixed=["base", fea.move("top", (d, 0, 0), "x")], **kw)
    free = fea.static(flexure(), "PETG", fixed=["base", fea.move("top", (d, 0, 0), "x")], nonlinear=True, **kw)
    held = fea.static(flexure(), "PETG", fixed=["base", fea.move("top", (d, 0, 0))], nonlinear=True, steps=5, **kw)
    assert isinstance(lin, fea.TetResult) and any("nonlinear=True" in w for w in lin.warnings)  # rotations of 0.15 rad
    k_beam = 2 * 12 * 2100 * (10 * 0.4 ** 3 / 12) / L ** 3  # two guided blades, 1.19 N/mm
    assert lin.move_forces[0].x / d == pytest.approx(k_beam, rel=0.15) and lin.move_forces[0].y == 0
    assert np.allclose(np.add(lin.reaction, lin.move_forces[0]), 0, atol=1e-5) and lin.imbalance < 1e-5  # the base holds it
    shortening = -free.displacement_at([(15, 35, 5)])[0, 1]
    assert shortening == pytest.approx(0.6 * d ** 2 / L, rel=0.1) and lin.displacement_at([(15, 35, 5)])[0, 1] == pytest.approx(0, abs=1e-3)
    assert free.move_forces[0].x == pytest.approx(lin.move_forces[0].x, rel=0.04)
    assert held.move_forces[0].x > 4 * lin.move_forces[0].x and held.move_forces[0].y > 20  # the blades in tension
    assert [round(s.fraction, 9) for s in held.steps] == [0.2, 0.4, 0.6, 0.8, 1.0]
    assert all(np.diff([0] + [s.move_forces[0].x for s in held.steps], 2) > 0)  # stiffening: each step takes more
    assert held.displacement_at([(15, 35, 5)], step=1)[0, 0] == pytest.approx(0.4 * d, rel=1e-6)
    assert held.warnings == () and "it takes" in str(held) and "force on each fea.move" in str(held)
    folder = held.show("flexure-nl")
    meta = json.load(open(os.path.join(folder, "output.json")))
    assert meta["fields"][1]["deform"] == 1.0  # drawn at its true shape
    with pytest.raises(TuleError, match="step= is for a nonlinear result"):
        lin.displacement_at([(15, 35, 5)], step=0)


@needs_gmsh
def test_buckling_under_a_force_is_an_error():
    col = box(5, 5, 100)
    col = col.tag("bottom", col.faces["bottom"]).tag("top", col.faces["top"])
    P = math.pi ** 2 * STEEL.E * (5 ** 4 / 12) / (4 * 100 ** 2)  # Euler, fixed-free: 2570 N
    with pytest.raises(TuleError, match="did not converge past 5") as e:
        fea.static(col, STEEL, fixed="bottom", loads=[fea.force("top", (0, 0, -2 * P))], nonlinear=True, dofs=8_000,
                   verbose=False)
    assert 0.49 < e.value.data["last_fraction"] <= 0.51 and "buckling" in str(e.value) and "fea.move" in str(e.value)
    assert e.value.result.steps[-1].fraction == e.value.data["last_fraction"]  # the last converged step
    assert e.value.result.steps[-1].reaction.z == pytest.approx(2 * P * e.value.data["last_fraction"], rel=1e-6)


@needs_gmsh
def test_a_path_past_a_stability_limit_is_refused(monkeypatch):
    # the stability check after the last step (LOBPCG on the tangent) and its bisection to the step where it failed
    calls = iter([-1.0, 1.0, -1.0])  # the end: unstable; step 2 (of 4): stable; step 3: not
    monkeypatch.setattr(fea, "_softest", lambda *a, **k: next(calls))
    with pytest.raises(TuleError, match="between 50 % and 75 % of the loads") as e:
        fea.static(beam(), STEEL, fixed="root", loads=[fea.force("tip", (0, 0, -100))], nonlinear=True, steps=4, mesh="tet",
                   dofs=5_000, verbose=False)
    assert e.value.result.steps[-1].fraction == 0.5 and len(e.value.result.steps) == 2
