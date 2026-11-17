"""tule.sdf: implicit bodies, their fields, meshing and conversions."""
import math

import numpy as np
import pytest

from tule import *
from tule import sdf
from tule.sdf import x, y, z

fidget = pytest.importorskip("tule_fidget")


def test_fields_are_signed_distances():
    s = sdf.sphere(10)
    assert s.value([(0, 0, 0), (10, 0, 0), (13, 0, 0)]) == pytest.approx([-10, 0, 3], abs=1e-4)
    b = sdf.box(10, 20, 30)  # corner at the origin, like tule.box
    assert b.value([(5, 10, 15), (12, 10, 15)]) == pytest.approx([-5, 2], abs=1e-4)
    assert list(b.contains([(1, 1, 1), (-1, 1, 1)])) == [True, False]
    g = s.gradient([(0, 0, 20)])[0]
    assert g == pytest.approx([0, 0, 1], abs=1e-4)


def test_fidget_and_numpy_agree():
    body = (sdf.box(20, 20, 10) | sdf.sphere(6).at(20, 20, 10)).union(sdf.torus(8, 2), smooth=3) - sdf.cylinder(3, 40).at(10, 10, -5)
    e = body.rotate(z=30).at(1, 2, 3).expr
    P = np.random.default_rng(0).uniform(-20, 40, (2000, 3))
    assert sdf.evaluate(e, P) == pytest.approx(sdf._np_eval(e, P), abs=1e-3)


def test_booleans_transforms_and_kind():
    a, b = sdf.box(10), sdf.sphere(4).at(10, 10, 10)
    assert a.kind == "implicit"
    assert (a | b).contains([(12, 10, 10)])[0] and not (a - b).contains([(9, 9, 9)])[0]
    assert (a & b).contains([(9, 9, 9)])[0] and not (a & b).contains([(1, 1, 1)])[0]
    soft = a.union(b, smooth=4)
    assert soft.value([(10, 10, 6.5)])[0] < (a | b).value([(10, 10, 6.5)])[0]  # the blend adds material
    assert sdf.sphere(2).at(5, 0, 0).rotate(z=90).value([(0, 5, 0)])[0] == pytest.approx(-2, abs=1e-4)
    assert sdf.sphere(2).scale(3).value([(0, 0, 0)])[0] == pytest.approx(-6, abs=1e-4)
    with pytest.raises(TuleError, match="mesh"):
        a | box(3)


def test_mesh_volume_and_tags():
    c = sdf.cylinder(10, 20).tag("top", lambda f: f.top())
    m = c.to_mesh(0.5)
    assert m.kind == "mesh" and m.is_closed
    assert m.volume == pytest.approx(math.pi * 100 * 20, rel=0.01)
    assert m.faces["top"].area == pytest.approx(math.pi * 100, rel=0.03)
    assert "Fidget" in " ".join(m.history)


def test_tpms_sheet_thickness():
    g = sdf.gyroid(10, thickness=1.0)
    rng = np.random.default_rng(1)
    P = rng.uniform(0, 10, (20000, 3))
    v = g.value(P)
    surf = P[np.abs(v) < 0.02]
    # stepping along the gradient from the mid-surface reaches the wall about thickness/2 away
    raw = sdf.gyroid(10).value(surf)
    assert np.abs(raw).max() < 0.6
    cube = (sdf.box(20) & sdf.gyroid(10, 1.0)).to_mesh(0.5)
    # a gyroid has about 3.1 / period of surface per volume: a 1 mm sheet fills about 31 %
    assert cube.is_closed and cube.volume / 8000 == pytest.approx(0.31, abs=0.04)


def test_field_and_from_shapes():
    f = sdf.field(lambda X, Y, Z: sdf.length(X, Y, Z) - 5, bounds=((-6, -6, -6), (6, 6, 6)))
    assert f.value([(0, 0, 0)])[0] == pytest.approx(-5, abs=1e-4)
    imp = box(10).to_implicit(0.5)
    assert imp.value([(5, 5, 5)])[0] == pytest.approx(-5, abs=0.3)
    assert imp.value([(15, 5, 5)])[0] == pytest.approx(5, abs=0.3)


def test_numpy_fallback(monkeypatch):
    monkeypatch.setattr(sdf, "_fidget", lambda: None)
    m = sdf.sphere(10).to_mesh(0.5)
    assert m.is_closed and m.volume == pytest.approx(4 / 3 * math.pi * 1000, rel=0.02)


def test_station_tables_fillets_and_slabs():
    t = sdf.interp(x, [0, 2, 9], [0, 0.4, 0.86])
    P = [(-1, 0, 0), (1, 0, 0), (5.5, 0, 0), (20, 0, 0)]
    assert sdf.evaluate(t, P) == pytest.approx(np.interp([-1, 1, 5.5, 20], [0, 2, 9], [0, 0.4, 0.86]), abs=1e-5)
    assert sdf.evaluate(sdf.smooth_min(x, y, 1.0), [(0, 0, 0), (3, 0, 0)]) == pytest.approx([-0.25, 0], abs=1e-6)
    assert sdf.evaluate(sdf.smooth_max(x, y, 1.0), [(0, 0, 0)]) == pytest.approx([0.25], abs=1e-6)
    assert sdf.evaluate(sdf.slab(x, 1, 3), [(2, 0, 0), (0, 0, 0)]) == pytest.approx([-1, 1])
    with pytest.raises(TuleError, match="increasing"):
        sdf.interp(x, [0, 0], [1, 2])


def test_knife_edges_stay_sharp():
    # a thin lens-section blade (a chine): its edge must keep its line, not get bitten by the cleanup
    w = sdf.interp(x, [0, 60], [0, 20])
    s = sdf.clamp(abs(y) / sdf.max(w, 1e-3), 0, 1)
    blade = sdf.field(lambda X, Y, Z: sdf.max(abs(Y) - w, abs(Z) - 0.8 * (1 - s * s) - 0.15, -X, X - 60),
                      ((-1, -21, -2), (61, 21, 2)))
    m = blade.to_mesh(0.2)
    P = m.points
    # the outline, slice by slice: the widest point in each 1 mm of x is on the rim line (a bite would
    # pull it inward by up to a few cells)
    bins = np.floor(P[:, 0]).astype(int)
    for b in range(5, 59):
        sel = bins == b
        assert np.abs(P[sel, 1]).max() == pytest.approx((b + 0.5) / 3, abs=0.3), b
    assert m.is_closed and np.abs(blade.value(P)).max() < 0.2


def test_a_surface_past_its_bounds_is_closed_at_them():
    from tule import sdf

    slab_ = sdf.field(lambda x, y, z: abs(z) - 1, ((-5, -5, -2), (5, 5, 2)))   # infinite in x and y
    m = slab_.to_mesh(0.5)
    assert m.is_closed
    (lo, hi) = m.bounds
    assert -5.8 < lo[0] < -5 and 5 < hi[0] < 5.8


# ---------------------------------------------------------------- profiles, revolve, repetition, walls, tubes


def test_polygon_is_a_signed_distance_either_winding():
    sq = [(0, 0), (10, 0), (10, 10), (0, 10)]
    for pts in (sq, sq[::-1]):
        p = sdf.polygon(pts)
        assert p.value([(5, 5), (15, 5), (-1, -1), (5, 0)]) == pytest.approx([-5, 5, math.sqrt(2), 0], abs=1e-4)
    L = sdf.polygon([(0, 0), (20, 0), (20, 5), (5, 5), (5, 20), (0, 20)])  # concave: an L
    assert L.value([(10, 10), (2, 2), (10, 2.5)]) == pytest.approx([5, -2, -2.5], abs=1e-4)
    # NumPy agrees with Fidget, and the sign holds on rays through vertices
    P = np.random.default_rng(3).uniform(-5, 25, (3000, 3))
    P[:500, 1] = 5.0  # level with two vertices
    assert sdf.evaluate(L.expr, P) == pytest.approx(sdf._np_eval(L.expr, P), abs=1e-3)


def test_rect_circle_spline_polyline_and_region_booleans():
    r = sdf.rect(10, 4, radius=1)
    assert r.value([(0, 0), (6, 0), (5, 2)]) == pytest.approx([-2, 1, math.sqrt(2) - 1], abs=1e-4)
    assert sdf.rect(10, 4, center=False).value([(5, 2)])[0] == pytest.approx(-2, abs=1e-4)
    ring = sdf.circle(10) - sdf.circle(6)
    assert list(ring.contains([(8, 0), (3, 0), (11, 0)])) == [True, False, False]
    assert sdf.circle(10).shell(2).contains([(9, 0), (7, 0)]).tolist() == [True, False]
    grown = sdf.circle(10).offset(2)
    assert grown.bounds == ((-12, -12), (12, 12)) and sdf.circle(10).offset(-2).bounds == ((-10, -10), (10, 10))
    blob = sdf.spline([(0, 0), (10, 0), (10, 10), (0, 10)])  # a smooth closed curve through the corners
    assert blob.contains([(5, 5)])[0] and not blob.contains([(5, -4)])[0]
    stroke = sdf.polyline([(0, 0), (10, 0), (10, 10)], width=2)
    assert stroke.value([(5, 0), (5, 3), (10, 5)]) == pytest.approx([-1, 2, -1], abs=1e-4)
    moved = sdf.circle(2).at(5, 1).rotate(90)
    assert moved.value([(-1, 5)])[0] == pytest.approx(-2, abs=1e-4)
    with pytest.raises(TuleError, match="2D"):
        sdf.circle(3) | sdf.sphere(2)


def test_revolve_a_lathe_profile_and_a_wedge():
    cone = sdf.polygon([(0, 0), (40, 0), (30, 50), (0, 50)]).revolve()
    m = cone.to_mesh(0.5)
    assert m.is_closed
    assert m.volume == pytest.approx(math.pi * 50 / 3 * (40 ** 2 + 40 * 30 + 30 ** 2), rel=0.01)
    # a washer from a rectangle off the axis: a distance away from the axis too
    washer = sdf.rect(4, 2).at(10, 0).revolve()
    assert washer.value([(0, 10, 0), (0, 13, 0), (0, 0, 0)]) == pytest.approx([-1, 1, 8], abs=1e-4)
    quarter = washer.to_mesh(0.2).volume / 4
    assert sdf.rect(4, 2).at(10, 0).revolve(90).to_mesh(0.2).volume == pytest.approx(quarter, rel=0.03)
    assert sdf.rect(6, 6).at(10, 0).extrude(5).to_mesh(0.3).volume == pytest.approx(180, rel=0.02)


def test_polar_and_linear_repetition_stay_one_expression():
    bolt = sdf.cylinder(6, 10).at(100, 0, 0)
    ring = bolt.repeat_polar(40)
    assert sdf._count(ring.expr) < 3 * sdf._count(bolt.expr)  # not 40 copies of the tree
    assert ring.to_mesh(0.5).volume == pytest.approx(40 * math.pi * 36 * 10, rel=0.02)
    a = 2 * math.pi * 7 / 40  # the 8th copy
    assert ring.value([(100 * math.cos(a), 100 * math.sin(a), 5)])[0] == pytest.approx(-5, abs=1e-3)
    row = sdf.sphere(3).repeat_linear((10, 0, 0), 20)
    assert row.to_mesh(0.3).volume == pytest.approx(20 * 4 / 3 * math.pi * 27, rel=0.02)
    assert row.value([(190, 0, 0), (200, 0, 0), (-10, 0, 0)]) == pytest.approx([-3, 7, 7], abs=1e-4)
    # a copy reaching into its neighbour's cell needs overlap=True to be whole
    wide = sdf.box(14, 2, 2, center=True)
    assert wide.repeat_linear((10, 0, 0), 3, overlap=True).value([(16, 0, 0)])[0] < 0
    assert sdf.sphere(1).repeat_polar(1).value([(0, 0, 0)])[0] == pytest.approx(-1, abs=1e-5)


def test_wall_channels_follow_the_contour_and_narrow_at_the_throat():
    w = sdf.Wall([0, 200, 300, 350, 400, 800], [200, 200, 150, 110, 130, 400], samples=6)
    assert w.r_at(0) == pytest.approx(200) and w.r_at(800) == pytest.approx(400)
    liner = w.solid(0, 6)
    ch = w.channels(60, land=2.0, depth=2.5, offset=1.5, z=(10, 790))
    body = liner - ch
    for zc in (100, 350, 700):
        r = float(w.r_at(zc))
        mid = r + (1.5 + 1.25) / math.cos(math.atan(np.gradient(w.r, w.z)[np.searchsorted(w.z, zc)]))
        assert body.value([(mid, 0, zc)])[0] > 0  # a channel on +x (phase 0), inside the wall
        assert body.value([(r + 0.75, 0, zc)])[0] < 0  # the hot wall under it
        a = math.pi / 60  # half a pitch on: the land between two channels
        assert body.value([(mid * math.cos(a), mid * math.sin(a), zc)])[0] < 0
    # the channel's width at the wall: the pitch less the land, so narrow at the throat
    def width(zc):
        r = float(w.r_at(zc)) + 2.75
        ang = np.linspace(-0.06, 0.06, 4001)
        v = ch.value(np.column_stack([r * np.cos(ang), r * np.sin(ang), np.full_like(ang, zc)]))
        return float((v < 0).sum()) / len(ang) * 0.12 * r
    assert width(350) == pytest.approx(2 * math.pi * (110 + 2.75) / 60 - 2, abs=0.25)
    assert width(700) > 2.5 * width(350)
    fixed = w.channels(60, width=[(0, 3.0), (800, 1.0)], depth=2, offset=1)
    assert fixed.value([(float(w.r_at(400)) + 2, 0, 400)])[0] < 0
    ribs = w.ribs(24, width=8, height=10, base=6)
    assert ribs.value([(float(w.r_at(600)) + 10, 0, 600)])[0] < 0
    with pytest.raises(TuleError, match="width"):
        w.channels(60, depth=2)


def test_tube_along_a_bent_path_hollow_and_open():
    path = [(0, 0, 0), (0, 0, 100), (80, 0, 160)]
    t = sdf.tube(path, r=12, wall=2, bend=30)
    assert t.value([(0, 0, 50), (0, 11, 50), (0, 14, 50)]) == pytest.approx([10, -1, 2], abs=1e-3)  # bore, wall, outside
    m = t.to_mesh(0.8)
    assert m.is_closed
    L = 100 + 100  # about: the legs less the bend's cut corner
    assert m.volume == pytest.approx(math.pi * (144 - 100) * L, rel=0.12)
    solid = sdf.tube(path, r=[10, 10, 5])  # tapered
    assert solid.value([(80 - 0.8, 0, 160 - 0.6)])[0] == pytest.approx(-1, abs=0.2)  # a flat end, 1 mm in
    assert sdf.tube(path, r=5, spline=True).contains([(0, 0, 50)])[0]
    with pytest.raises(TuleError, match="wall"):
        sdf.tube(path, r=3, wall=4)


def test_banded_meshing_fine_boxes_and_rest():
    w = sdf.Wall([0, 300], [100, 100])
    liner = w.solid(0, 5)
    body = liner - w.channels(40, width=3, depth=2, offset=1.5, z=(10, 290))
    m = body.to_mesh(4, fine=[((-200, -6, -10), (200, 6, 310), 0.5)], rest=liner)
    assert m.is_closed and "pieces" in m.history[-1]
    # the channels exist in the fine band only: the band's surface is much finer
    coarse_only = liner.to_mesh(4)
    assert m.volume == pytest.approx(coarse_only.volume, rel=0.02)
    band = m.faces.within((-200, -6, 0), (200, 6, 300))
    assert len(band) > 2000
    with pytest.raises(TuleError, match="fine"):
        liner.to_mesh(4, rest=liner)


def _folded(body, m):
    """Share of the surface area whose triangles face against the field's gradient."""
    P, T = m.points, m._T
    A, B, C = P[T[:, 0]], P[T[:, 1]], P[T[:, 2]]
    n = np.cross(B - A, C - A)
    g = sdf.evaluate(body.expr, (A + B + C) / 3, grad=True)[:, 1:]
    area = np.linalg.norm(n, axis=1)
    return area[np.einsum("ij,ij->i", n, g) < 0].sum() / area.sum()


def test_meshes_barely_fold_where_cell_sizes_change():
    # dual contouring's octree merges cells on smooth stretches; Fidget triangulates each edge's polygon
    # so it doesn't fold where a merged cell meets smaller ones (about 1.5 % of the area folded before)
    for body in (sdf.box(120, 80, 60) | sdf.sphere(45).at(60, 0, 30), sdf.cylinder(40, 90).rotate(x=17),
                 sdf.torus(120, 30).rotate(x=11)):
        m = body.to_mesh(2.0)
        assert m.is_closed and _folded(body, m) < 1e-3


def test_faces_at_round_numbers_mesh_without_slivers():
    # a face at z = 2300 on a grid of 2 mm cells would lie on the cells' corners, where its values are zero
    # up to rounding, of either sign: the mesh would fold into the face in triangles with no area
    body = sdf.polygon([(0, 2300), (92, 2300), (92, 2378), (0, 2378)]).revolve().at(460, 120, 0)
    m = body.to_mesh(2.0)
    P, T = m.points, m._T
    area = np.linalg.norm(np.cross(P[T[:, 1]] - P[T[:, 0]], P[T[:, 2]] - P[T[:, 0]]), axis=1)
    assert m.is_closed and area.min() > 1e-6
    assert len(np.unique(P.round(6), axis=0)) == len(P)


def test_meshed_bodies_carry_the_fields_normals(tmp_path):
    from tule.mesh import shaded
    m = sdf.cylinder(30, 50).to_mesh(1.5)
    N = m._exact_normals
    assert N is not None and N.shape == m.points.shape
    side = np.abs(m.points[:, 2] - 25) < 20
    radial = m.points[side, :2] / np.linalg.norm(m.points[side, :2], axis=1, keepdims=True)
    assert np.einsum("ij,ij->i", N[side, :2], radial).min() > 0.999  # exact, not averaged from facets
    m.export_ply(tmp_path / "c.ply")
    back = Mesh.load(tmp_path / "c.ply")
    assert back._exact_normals is not None and len(back._exact_normals) == len(back.points)
    moved = m.at(10, 0, 0).rotate(x=90)  # rotated with the mesh: the side's normals stay radial about the new axis
    side_n = moved._exact_normals[side]
    assert np.allclose(side_n[:, 1], 0, atol=1e-3) and np.allclose(np.linalg.norm(side_n, axis=1), 1, atol=1e-5)
    # shading uses them on smooth surfaces and keeps the flat normal across the rim's crease
    V, Ns, I = shaded(m.points, m._T, None, normals=N)
    top = np.abs(V[:, 2] - 50) < 1e-6
    rim_side = (np.abs(V[:, 2] - 50) < 1e-6) & (np.linalg.norm(V[:, :2], axis=1) > 29.5)
    assert np.allclose(Ns[top & ~rim_side, 2], 1, atol=1e-3)
    A = np.linalg.norm(np.cross(V[I[:, 1]] - V[I[:, 0]], V[I[:, 2]] - V[I[:, 0]]), axis=1)
    seen = np.zeros(len(V), bool)
    seen[I[A > 0.05 * np.median(A)].reshape(-1)] = True  # slivers (no area, nothing to see) keep their own normal
    wall = seen & (V[:, 2] > 3) & (V[:, 2] < 47)  # two cells clear of the rims, where dual contouring tilts real facets
    radial = V[wall, :2] / np.linalg.norm(V[wall, :2], axis=1, keepdims=True)
    assert np.einsum("ij,ij->i", Ns[wall, :2], radial).min() > 0.999


def test_normals_after_simplify():
    c = sdf.cylinder(30, 50)
    m = c.to_mesh(1.5).simplify(0.05).refine(5)  # (a ruled side simplifies to rim-to-rim triangles)
    assert m._exact_normals is None  # the points moved
    N = c.normals(m.points)
    side = np.abs(m.points[:, 2] - 25) < 20
    radial = m.points[side, :2] / np.linalg.norm(m.points[side, :2], axis=1, keepdims=True)
    assert np.isfinite(N).all() and np.einsum("ij,ij->i", N[side, :2], radial).min() > 0.999
    assert Mesh(m.points, m.indices, normals=N)._exact_normals is not None


def test_normals_on_a_polygon_lathe_never_vanish():
    # a polygon's distance is sqrt(d²): exactly on an edge its derivative is 0, not the normal
    body = sdf.polygon([(0, 0), (40, 0), (40, 30), (25, 50), (0, 50)]).revolve()
    P = np.array([[40.0, 0, 10], [0, -40.0, 20], [28.0, 0, 0]])
    N = body.normals(P)
    assert np.allclose(N[0], [1, 0, 0], atol=1e-3) and np.allclose(N[1], [0, -1, 0], atol=1e-3)
    assert np.allclose(N[2], [0, 0, -1], atol=1e-3)


def _corner_deviation(m):
    """Largest angle (degrees) between a corner's normal and its triangle's facet, over triangles with area."""
    from tule.mesh import tri_normals
    P, T = m.points, m._T
    Fn, A = tri_normals(P, T)
    ok = A > 0.05 * np.median(A)
    d = np.einsum("ijk,ik->ij", m.normals[T].astype(float), Fn)[ok]
    return np.degrees(np.arccos(np.clip(d, -1, 1)))


def test_shade_splits_normals_at_creases(tmp_path):
    # a vertex on a crease has the field's normal of one side; the other side's triangles shaded toward it
    # (dark spikes off every crease in a renderer). shade gives each side its own vertex and normal.
    body = sdf.cylinder(30, 50) | sdf.box(70, 20, 20).at(-35, -10, 15)  # 90° rims, concave union creases
    m = body.to_mesh(1.5)
    assert np.percentile(_corner_deviation(m), 99.5) > 60  # one normal per vertex: half the rim is wrong
    s = body.shade(m)
    dev = _corner_deviation(s)
    assert np.percentile(dev, 99.5) < 25 and len(s.points) > len(m.points)
    assert s.triangles == m.triangles and s.is_closed and s.volume == pytest.approx(m.volume, rel=1e-9)
    # smooth surfaces keep the field's exact normals (one vertex each, no split)
    side = (np.abs(s.points[:, 2] - 25) < 8) & (np.linalg.norm(s.points[:, :2], axis=1) > 29.5) & (s.points[:, 1] > 15)
    radial = s.points[side, :2] / np.linalg.norm(s.points[side, :2], axis=1, keepdims=True)
    assert side.sum() > 20 and np.einsum("ij,ij->i", s.normals[side, :2], radial).min() > 0.999
    # a PLY keeps the split (copies of a vertex with different normals stay apart when read back)
    s.export_ply(tmp_path / "s.ply")
    back = Mesh.load(tmp_path / "s.ply")
    assert len(back.points) == len(s.points) and back.is_closed
    assert np.percentile(_corner_deviation(back), 99.5) < 25


def test_shade_keeps_gentle_curvature_smooth():
    # a torus has no creases: nothing is split, and every normal is the field's
    body = sdf.torus(40, 12)
    m = body.to_mesh(1.0).simplify(0.05)
    s = body.shade(m)
    assert len(s.points) == len(m.points)
    assert np.allclose(s.normals, body.normals(s.points), atol=1e-4)


def test_meshes_have_no_folds_and_crease_vertices_on_their_creases():
    # dual contouring folds triangles over (dents on smooth surfaces, notches at creases) and leaves crease
    # vertices a little off the crease (ragged teeth); to_mesh relaxes the folds out and moves those onto it
    for body in (sdf.torus(120, 30).rotate(x=11), sdf.box(120, 80, 60) | sdf.sphere(45).at(60, 0, 30),
                 sdf.cylinder(30, 50) | sdf.cylinder(12, 80).rotate(x=90).at(0, 40, 25)):
        m = body.to_mesh(2.0)
        # (what is left: a few slivers flat in a face along a concave crease, under a mm² in all)
        assert m.is_closed and _folded(body, m) < 5e-5
    # straddling triangles: a triangle across a crease has its centre well inside or outside the body
    body = sdf.polygon([(0, 0), (60, 0), (60, 20), (40, 45), (0, 45)]).revolve().rotate(x=13, y=7)
    m = body.to_mesh(2.0)
    C = m.points[m._T].mean(1)
    assert np.percentile(np.abs(body.value(C)), 99.9) < 0.05 * 2.0


def test_a_gentle_kink_is_an_edge_and_shades_as_one():
    # a cone meeting a slightly steeper one (9.5°, as under the chamber's lower ring): dual contouring puts
    # vertices on the kink, but the fold relax used to drag them off it (its planes didn't pin a gentle
    # kink), so triangles crossed it and smooth shading smeared it into a ragged band as wide as each one
    prof = [(0, 0), (60, 0), (56, 10), (58.5, 21), (63, 32), (55, 36), (0, 36)]
    body = sdf.polygon(prof).revolve()
    zk = 21.0
    m = body.to_mesh(1.0)
    P, T = m.points, m._T
    C = P[T].mean(1)
    zt = P[T][:, :, 2]
    near = (np.abs(np.linalg.norm(C[:, :2], axis=1) - 58.5) < 3) & (np.abs(C[:, 2] - zk) < 3)
    assert near.sum() > 500 and not (near & (zt.min(1) < zk - 0.1) & (zt.max(1) > zk + 0.1)).any()
    # an edge chain along it, so shade can split it there (asked to, under its default 15°): each side's
    # triangles then keep their own normals (not the mean of both)
    s = body.shade(m, angle=5)
    P, T, N = s.points, s._T, s.normals.astype(float)
    C = P[T].mean(1)
    k = np.abs(C[:, 2] - zk) < 3
    err = []
    for j in range(3):
        G = body.normals(P[T[k, j]] + 0.25 * (C[k] - P[T[k, j]]))
        err.append(np.degrees(np.arccos(np.clip(np.einsum("ij,ij->i", N[T[k, j]], G), -1, 1))))
    assert np.percentile(np.max(err, 0), 90) < 2


def test_band_seams_meet_on_the_surface():
    # a fine band and the coarse rest are meshed as closed pieces: their outlines on the shared plane are
    # moved onto the body's own outline, and the pieces joined (a union) into one closed solid: no inner
    # caps, no crack between the two outlines, the seam on the surface
    body = sdf.cylinder(40, 60).rotate(x=8)
    m = body.to_mesh(2.0, fine=[((-60, -60, 25), (60, 60, 35), 0.4)])
    assert m.is_closed and "joined into one solid" in m.history[-1]
    assert len(m.pieces()) == 1 and abs(m.volume / (math.pi * 40**2 * 60) - 1) < 2e-3
    from tule.mesh import tri_normals
    P, T = m.points, m._T
    Fn, A = tri_normals(P, T)
    for z in (25.0, 35.0):
        on = np.abs(P[:, 2] - z) < 1e-6
        assert on.sum() > 100 and np.abs(body.value(P[on])).max() < 1e-4
        # the caps where the pieces met are gone: what is left on the plane is the sliver between the two
        # outlines' chords (hundredths of a mm wide), on the surface
        cap = (np.abs(P[T, 2] - z) < 1e-6).all(1) & (np.abs(Fn[:, 2]) > 0.99)
        assert A[cap].sum() < 0.005 * math.pi * 40**2
        assert np.abs(body.value(P[T[cap]].mean(1))).max() < 0.2


def test_banded_pieces_join_without_bridging_the_band_detail():
    # the coarse rest (no channels) and the fine band (channels) are joined into one closed solid; a rest
    # vertex dual contouring placed past the band's face would fill part of a channel once joined
    w = sdf.Wall([0, 300], [100, 100])
    liner = w.solid(0, 5)
    body = liner - w.channels(40, width=3, depth=2, offset=1.5, z=(10, 290))
    m = body.to_mesh(4, fine=[((-200, -6, -10), (200, 6, 310), 0.5)], rest=liner)
    assert m.is_closed and "joined into one solid" in m.history[-1]
    # one solid: its other shells are the channels' ends inside the band, sealed by the rest (voids)
    shells = m.pieces()
    assert shells[0].volume > 0 and all(p.volume < 0 for p in shells[1:])
    P, T = m.points, m._T
    C = P[T].mean(1)
    size = np.linalg.norm(P[T] - np.roll(P[T], 1, axis=1), axis=2).max(1)
    inside = (np.abs(C[:, 1]) < 5.5) & (C[:, 2] > 12) & (C[:, 2] < 288)
    # in the band the surface is the body's (channels and all): no triangle across a channel
    assert inside.sum() > 2000 and (np.abs(body.value(C[inside])) < 0.1 * size[inside] + 0.02).all()
    # shading uses the field the mesh is of (the rest's outside the band): to_mesh's tidy is kept
    s = body.shade(m)
    assert len(s.indices) == len(T) and np.allclose(np.unique(s.points, axis=0), np.unique(P, axis=0))


def test_tol_is_a_distance_whatever_the_octree():
    # Fidget merges cells where the surface stays within tol (mm): a body meshed inside larger bounds
    # (a larger octree, same cell) merges about as much; a smaller tol keeps more triangles, all near it
    body = sdf.torus(30, 9).rotate(x=17)
    a = body.to_mesh(0.5, tol=0.05, bounds=((-45, -45, -45), (45, 45, 45)))
    b = body.to_mesh(0.5, tol=0.05, bounds=((-90, -90, -90), (90, 90, 90)))
    assert a.triangles == pytest.approx(b.triangles, rel=0.15)
    fine = body.to_mesh(0.5, tol=0.005, bounds=((-45, -45, -45), (45, 45, 45)))
    assert fine.triangles > 1.5 * a.triangles
    for m, tol in ((a, 0.05), (fine, 0.005)):
        C = m.points[m._T].mean(1)
        assert np.abs(body.value(C)).mean() < tol
