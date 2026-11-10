import json
import os

import numpy as np
import pytest

from tule import *
from tule import topopt



def _dense_stiffness(level) -> np.ndarray:
    """The coarsest operator as a dense matrix (on the host), fixed dofs as identity rows."""
    free = topopt._host(level.free).reshape(-1)
    nx, ny, nz = level.shape
    if level.Ke is None:
        Ke = topopt._host(level.coef).reshape(-1, 1, 1) * topopt._host(level.KE)[None]
    else:
        Ke = topopt._host(level.Ke).reshape(-1, 24, 24)
    Ny, Nz = ny + 1, nz + 1
    nn = (nx + 1) * Ny * Nz
    i, j, k = np.meshgrid(np.arange(nx), np.arange(ny), np.arange(nz), indexing="ij")
    nodes = np.stack([((i + a) * Ny + (j + b)) * Nz + (k + c) for (a, b, c) in topopt.CORNERS]).reshape(8, -1)
    dofs = (np.arange(3)[None, :, None] * nn + nodes[:, None, :]).reshape(24, -1).T  # (elements, 24)
    n = 3 * nn
    A = np.zeros(n * n)
    on = np.nonzero(np.abs(Ke).reshape(len(Ke), -1).max(axis=1) > 0)[0]
    flat = (dofs[on, :, None] * n + dofs[on, None, :]).reshape(-1)
    np.add.at(A, flat, Ke[on].reshape(-1))
    A = A.reshape(n, n) * free[:, None] * free[None, :]
    A[np.diag_indices(n)] += 1 - free
    return A

def cantilever():
    b = box(40, 10, 20)
    return b.tag("fixed", b.faces["left"]).tag("load", b.faces["right"])


def problem(**kw):
    b = cantilever()
    return topopt.Problem(b, fixed=b.faces["fixed"], loads={b.faces["load"]: (0, 0, -100)}, volume_fraction=0.3, **kw)


# ---------------------------------------------------------------- the solver


def test_element_stiffness():
    KE = topopt.element_stiffness(0.3)
    assert np.allclose(KE, KE.T)
    w = np.linalg.eigvalsh(KE)
    assert np.sum(np.abs(w) < 1e-10) == 6 and w.min() > -1e-10  # six rigid-body modes, otherwise positive
    t = np.tile([1.0, 0, 0], 8)
    assert np.allclose(KE @ t, 0)


def grid_level(rng, shape=(4, 2, 4)):
    KE = topopt.element_stiffness(0.3)
    coef = rng.uniform(0.01, 1, shape)
    free = np.ones((3,) + tuple(n + 1 for n in shape))
    free[:, 0] = 0  # clamp x = 0
    return topopt._Level(np, free, KE=KE, coef=coef)


def test_matrix_free_operator_matches_assembled_matrix():
    rng = np.random.default_rng(0)
    lv = grid_level(rng)
    A = _dense_stiffness(lv)
    u = rng.normal(size=lv.free.shape)
    assert np.allclose(lv.A(u).reshape(-1), A @ u.reshape(-1))
    assert np.allclose(A, A.T)


def test_coarse_operator_is_galerkin():
    rng = np.random.default_rng(1)
    lv = grid_level(rng)
    lv.free[:] = 1  # compare the unconstrained operators
    coarse = lv.coarsen(topopt._P_CHILD)
    coarse.free[:] = 1
    n_c = coarse.free.size
    P = np.stack([topopt._prolong(e.reshape(coarse.free.shape), np).reshape(-1) for e in np.eye(n_c)], axis=1)
    R = np.stack([topopt._restrict(e.reshape(lv.free.shape), np).reshape(-1) for e in np.eye(lv.free.size)], axis=1)
    assert np.allclose(R, P.T)
    A = _dense_stiffness(lv)
    assert np.allclose(_dense_stiffness(coarse), P.T @ A @ P)


def test_cg_iterations_stay_flat_as_the_grid_gets_finer():
    p = problem()
    its = []
    for voxel in (1.25, 0.625):  # 4,096 and 32,768 elements, 2 and 3 multigrid levels
        r = topopt.optimize(p, voxel=voxel, iterations=1, continuation=False, output=None)
        its.append(r.history[0]["cg_iterations"])
    assert its[1] <= its[0] + 3, its


def test_voxelize_matches_volume():
    c = cylinder(10, 20)
    h = 1.0
    inside = topopt.voxelize(c, (-10, -10, 0), h, (20, 20, 20))
    assert inside.sum() * h**3 == pytest.approx(c.volume, rel=0.03)
    assert not inside[0, 0].any() and inside[10, 10].all()


# ---------------------------------------------------------------- the optimization


def test_cantilever_compliance_decreases_and_volume_hits_target(tmp_path, monkeypatch):
    monkeypatch.setenv("TULE_OUTPUTS", str(tmp_path))
    p = problem()
    r = topopt.optimize(p, voxel=1.25, iterations=24, output="cantilever")
    c = [h["compliance"] for h in r.history]
    assert c[-1] < 0.5 * c[0]
    assert c[-1] <= min(c[:12]) + 1e-9  # the refined design is better than anything early on
    assert r.volume_fraction == pytest.approx(0.3, abs=0.005)
    assert np.mean(r.density[r.density > 0] > 0.5) > 0  # some solid
    assert 0.1 < np.mean(r.density) < 0.5
    # the sequence output
    meta = json.load(open(tmp_path / "cantilever" / "output.json"))
    assert meta["kind"] == "sequence" and meta["status"] == "done"
    assert len(meta["frames"]) >= 10
    assert set(meta["frames"][-1]["values"]) == {"compliance", "volume", "change"}
    assert all(os.path.exists(tmp_path / "cantilever" / f["file"]) for f in meta["frames"])
    # the mesh is closed, outward, and about the design's volume
    m = r.mesh
    t = m.positions[m.indices].astype(np.float64)
    vol = np.einsum("ij,ij->i", t[:, 0], np.cross(t[:, 1], t[:, 2])).sum() / 6
    assert vol == pytest.approx(0.3 * cantilever().volume, rel=0.15)
    s = r.to_shape()
    assert s.is_valid and s.volume == pytest.approx(vol, rel=1e-3)
    assert "compliance" in str(r) and "volume fraction 0.300" in str(r)


def test_keep_and_void_regions():
    keep = box(6, 10, 6).at(16, 0, 6)
    void = box(8, 10, 8).at(24, 0, 0)
    r = topopt.optimize(problem(keep=[keep], void=void), voxel=2.0, iterations=6, continuation=False, output=None)
    org = np.array(r.origin)
    centers = lambda lo, hi: tuple(slice(int(round((lo[d] - org[d]) / r.voxel)), int(round((hi[d] - org[d]) / r.voxel)))
                                   for d in range(3))
    assert np.allclose(r.density[centers((16, 0, 6), (22, 10, 12))], 1)
    assert np.allclose(r.density[centers((24, 0, 0), (32, 10, 8))], 0)
    assert r.density[centers((0, 0, 0), (8, 10, 20))].mean() > 0.05


def test_problem_validation_and_show(tmp_path, monkeypatch):
    monkeypatch.setenv("TULE_OUTPUTS", str(tmp_path))
    b = cantilever()
    with pytest.raises(TuleError, match="fixed"):
        topopt.Problem(b, fixed=b.faces["fixed"] - b.faces["fixed"], loads={b.faces["load"]: (0, 0, -1)})
    with pytest.raises(TuleError, match="volume_fraction"):
        topopt.Problem(b, fixed=b.faces["fixed"], loads={b.faces["load"]: (0, 0, -1)}, volume_fraction=1.5)
    with pytest.raises(TuleError, match="another shape"):
        other = box(1)
        topopt.Problem(b, fixed=other.faces["top"], loads={b.faces["load"]: (0, 0, -1)})
    p = problem()
    folder = p.show("cantilever-bc")
    meta = json.load(open(os.path.join(folder, "output.json")))
    assert meta["kind"] == "overlay"
    assert meta["faces"] == {str(b.faces["fixed"].ids[0]): {"role": "fixed", "label": "fixed"},
                             str(b.faces["load"].ids[0]): {"role": "load", "label": "100 N -Z"}}
    assert meta["arrows"] == [{"at": [40.0, 5.0, 10.0], "dir": [0.0, 0.0, -1.0], "label": "100 N -Z"}]


def test_device_selection():
    assert topopt.backend("cpu") is np
    try:
        import cupy  # noqa: F401
    except ImportError:
        assert topopt.backend("auto") is np
        with pytest.raises(TuleError, match="GPU"):
            topopt.backend("gpu")
