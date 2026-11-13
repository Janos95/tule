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
