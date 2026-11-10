"""Topology optimization: where should material go, given loads and supports on tagged faces.

```python
from tule import *

problem = topopt.Problem(
    part,                                          # the design domain, a Shape
    fixed=part.faces["bolt"],                      # faces held in place
    loads={part.faces["load"]: (0, 0, -500)},      # total force on each face set (N)
    volume_fraction=0.3,                           # keep 30 % of the domain
    keep=[boss],                                   # regions that stay solid (Shapes)
)
problem.show("bracket-bc")                         # fixed faces, loads, arrows in the viewer
result = topopt.optimize(problem, output="bracket-topopt")   # a sequence output, frame by frame
print(result)                                      # compliance, volume, iterations, time
outputs.part("bracket-optimized", result.mesh)     # the smoothed isosurface as STL
solid = result.to_shape()                          # a Shape (a closed triangulated solid) for Booleans
```

**Method.** Density-based SIMP: minimize compliance (the work done by the
loads, ``f·u``) under a volume constraint. Element stiffness is
``E·(Emin + ρ^p (1 - Emin))``, a density filter of radius ``filter_radius``
sets the minimum feature size, and optimality-criteria (OC) updates move
the densities. (MMA, for several constraints, slots in as another
``method``.)

**Solver.** Matrix-free 8-node hexahedral elements on a regular voxel grid:
every element is the same cube, so ``K·u`` is a gather of 24 dofs per
element, one product with the 24×24 reference stiffness scaled by the
element's ``E(ρ)``, and a scatter-add, all array operations. Conjugate
gradients are preconditioned by a geometric multigrid V-cycle: factor-2
coarsening with trilinear transfer, coarse operators that are the exact
Galerkin products ``PᵀKP`` built element by element (one 24×24 matrix per
coarse element, from the 8 children), damped Jacobi smoothing, and a
sparse direct solve on the coarsest grid. The CG iteration count stays
flat as the grid gets finer (``tools/topopt_bench.py``: 9–11 per solve at
2,300, 18,000 and 147,000 elements for the same design). With
``continuation`` the design is first optimized on a grid twice as coarse,
then its densities are prolonged to the fine grid and refined.

**Devices.** All kernels are written against an array module ``xp``:
NumPy on CPU machines, CuPy on GPU machines (``device="auto"`` picks CuPy
when it imports and sees a GPU). Voxelization, boundary conditions and
meshing run on the host.

**Units.** mm, N, MPa (``E`` defaults to aluminium, 70 GPa), so compliance
is in N·mm (mJ) and displacements in mm.
"""

from __future__ import annotations

import math
import time
import warnings
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

import numpy as np

from .errors import TuleError
from .geom import Vec, VecLike, vec
from .io import Mesh
from .selection import Faces
from .shape import Shape

# ---------------------------------------------------------------- array backend


def backend(device: str = "auto"):
    """The array module for ``device``: ``"cpu"`` (NumPy), ``"gpu"`` (CuPy), or ``"auto"``."""
    if device not in ("auto", "cpu", "gpu"):
        raise TuleError(f"unknown device {device!r}", op="topopt", hint='use "auto", "cpu" or "gpu"')
    if device in ("auto", "gpu"):
        try:
            import cupy  # type: ignore

            if cupy.cuda.runtime.getDeviceCount() > 0:
                return cupy
            if device == "gpu":
                raise TuleError("no GPU on this machine (CuPy sees none)", op="topopt",
                                hint='start a GPU machine (tule up --gpu …), or use device="cpu" or "auto"')
        except TuleError:
            raise
        except Exception as e:  # no CuPy, no driver, no GPU
            if device == "gpu":
                raise TuleError("no GPU backend available", op="topopt",
                                hint=f"install cupy for this machine's CUDA version ({e.__class__.__name__}: {e})") from None
    return np


def _host(a) -> np.ndarray:
    """A NumPy copy of an array from any backend."""
    get = getattr(a, "get", None)
    return get() if get is not None else np.asarray(a)


# ---------------------------------------------------------------- the reference element

# local node a = (i, j, k) corner offsets; dof 3a + c
CORNERS = [(i, j, k) for i in (0, 1) for j in (0, 1) for k in (0, 1)]


def element_stiffness(nu: float) -> np.ndarray:
    """The 24×24 stiffness of a unit cube of trilinear hexahedral elasticity with E = 1."""
    lam = nu / ((1 + nu) * (1 - 2 * nu))
    mu = 1 / (2 * (1 + nu))
    D = np.zeros((6, 6))
    D[:3, :3] = lam
    D[:3, :3] += 2 * mu * np.eye(3)
    D[3:, 3:] = mu * np.eye(3)
    g = [0.5 - 0.5 / math.sqrt(3), 0.5 + 0.5 / math.sqrt(3)]
    K = np.zeros((24, 24))
    for xi in g:
        for eta in g:
            for zeta in g:
                q = (xi, eta, zeta)
                dN = np.zeros((8, 3))
                for a, c in enumerate(CORNERS):
                    f = [q[d] if c[d] else 1 - q[d] for d in range(3)]
                    s = [1.0 if c[d] else -1.0 for d in range(3)]
                    dN[a] = [s[0] * f[1] * f[2], f[0] * s[1] * f[2], f[0] * f[1] * s[2]]
                B = np.zeros((6, 24))
                for a in range(8):
                    x, y, z = dN[a]
                    B[0, 3 * a], B[1, 3 * a + 1], B[2, 3 * a + 2] = x, y, z
                    B[3, 3 * a], B[3, 3 * a + 1] = y, x
                    B[4, 3 * a + 1], B[4, 3 * a + 2] = z, y
                    B[5, 3 * a], B[5, 3 * a + 2] = z, x
                K += B.T @ D @ B / 8
    return K


# ---------------------------------------------------------------- matrix-free operators


class _Level:
    """One grid of the multigrid hierarchy and its operator, applied element by element.

    The finest level is matrix-free with the single reference matrix ``KE``
    scaled per element (``coef``, E(ρ)·h); coarser levels keep one Galerkin
    24×24 matrix per element (``Ke``, shape ``(nx, ny, nz, 24, 24)``). Node
    fields are component-first, ``(3, nx+1, ny+1, nz+1)``, so the gathered
    element vectors ``(24, elements)`` and the scattered blocks are contiguous.
    """

    def __init__(self, xp, free, KE=None, coef=None, Ke=None):
        self.xp = xp
        self.KE, self.coef, self.Ke = KE, coef, Ke
        self.shape = tuple(coef.shape if Ke is None else Ke.shape[:3])
        self.free = free  # (3, nx+1, ny+1, nz+1), 1 = free dof
        self.diag = None

    def gather(self, u):
        """Element dof vectors ``(24, elements)``, dof ``3a + c`` for corner ``a``, component ``c``."""
        nx, ny, nz = self.shape
        ue = self.xp.stack([u[:, i:i + nx, j:j + ny, k:k + nz] for (i, j, k) in CORNERS])
        return ue.reshape(24, -1)

    def scatter(self, fe):
        xp = self.xp
        nx, ny, nz = self.shape
        fe = fe.reshape(8, 3, nx, ny, nz)
        y = xp.zeros(self.free.shape, dtype=fe.dtype)
        for a, (i, j, k) in enumerate(CORNERS):
            y[:, i:i + nx, j:j + ny, k:k + nz] += fe[a]
        return y

    def K(self, u):
        ue = self.gather(u)
        if self.Ke is None:
            fe = (self.KE @ ue) * self.coef.reshape(1, -1)
        else:
            fe = (self.Ke.reshape(-1, 24, 24) @ ue.T[:, :, None])[:, :, 0].T
        return self.scatter(fe)

    def A(self, u):
        """The stiffness with fixed (and empty) dofs replaced by the identity."""
        return self.free * self.K(self.free * u) + (1 - self.free) * u

    def element_diagonals(self):
        xp = self.xp
        if self.Ke is None:
            return xp.diag(self.KE)[:, None] * self.coef.reshape(1, -1)
        idx = xp.arange(24)
        return self.Ke.reshape(-1, 24, 24)[:, idx, idx].T

    def make_diag(self):
        xp = self.xp
        d = self.scatter(self.element_diagonals())
        stiff = d > 1e-30 * float(d.max() if d.size else 1)
        self.free = self.free * stiff  # dofs no element touches are not unknowns
        self.diag = xp.where(self.free > 0, d, 1.0)

    def coarsen(self, P):
        """The Galerkin operator on the grid twice as coarse, element by element.

        Trilinear prolongation inside a coarse element only uses that
        element's 8 nodes, so ``Pᵀ K P`` is a sum over each coarse element's
        8 children of ``P_cᵀ K_child P_c`` (``P`` holds the 8 ``P_c``).
        """
        xp = self.xp
        Kc = None
        for c, (i, j, k) in enumerate(CORNERS):
            Pc = P[c]
            if self.Ke is None:
                G = Pc.T @ self.KE @ Pc
                term = self.coef[i::2, j::2, k::2][..., None, None] * G
            else:
                term = Pc.T @ self.Ke[i::2, j::2, k::2] @ Pc
            if Kc is None:
                Kc = term
            else:
                Kc += term  # in place: these are the biggest arrays of a solve
        # a coarse dof is fixed when any fine dof it interpolates to is: supports that fall between
        # coarse nodes (a bolt hole) must not vanish, or the coarse problem floats
        free = (_restrict(1.0 - self.free, xp) <= 1e-12).astype(self.free.dtype)
        return _Level(xp, free, Ke=Kc)


def _child_prolongations() -> np.ndarray:
    """``P[c]`` (24×24): the dofs of child ``c`` of a coarse element from the coarse element's dofs."""
    P = np.zeros((8, 24, 24))
    for c, off in enumerate(CORNERS):
        for a, corner in enumerate(CORNERS):
            t = [(off[d] + corner[d]) / 2 for d in range(3)]  # the child's node, in coarse-element coordinates
            for b, cb in enumerate(CORNERS):
                w = np.prod([t[d] if cb[d] else 1 - t[d] for d in range(3)])
                for comp in range(3):
                    P[c, 3 * a + comp, 3 * b + comp] = w
    return P


def _prolong(c, xp):
    """Trilinear interpolation of node fields ``(3, ...)`` to the grid twice as fine (separable)."""
    for axis in (1, 2, 3):
        n = c.shape[axis]
        shape = list(c.shape)
        shape[axis] = 2 * n - 1
        f = xp.zeros(shape, dtype=c.dtype)
        ev = [slice(None)] * c.ndim
        od = [slice(None)] * c.ndim
        lo = [slice(None)] * c.ndim
        hi = [slice(None)] * c.ndim
        ev[axis], od[axis], lo[axis], hi[axis] = slice(0, None, 2), slice(1, None, 2), slice(0, -1), slice(1, None)
        f[tuple(ev)] = c
        f[tuple(od)] = 0.5 * (c[tuple(lo)] + c[tuple(hi)])
        c = f
    return c


def _restrict(f, xp):
    """The transpose of ``_prolong``."""
    for axis in (1, 2, 3):
        ev = [slice(None)] * f.ndim
        od = [slice(None)] * f.ndim
        ev[axis], od[axis] = slice(0, None, 2), slice(1, None, 2)
        r = f[tuple(ev)].copy()
        o = 0.5 * f[tuple(od)]
        lo = [slice(None)] * f.ndim
        hi = [slice(None)] * f.ndim
        lo[axis], hi[axis] = slice(0, -1), slice(1, None)
        r[tuple(lo)] += o
        r[tuple(hi)] += o
        f = r
    return f


def _sparse_stiffness(level: _Level):
    """The coarsest operator as a sparse matrix (on the host), fixed dofs as identity rows."""
    from scipy.sparse import coo_matrix, diags

    if hasattr(level, "assemble"):  # a level that stores only its voxels with material (tule.fea)
        return level.assemble()
    free = _host(level.free).reshape(-1)
    nx, ny, nz = level.shape
    if level.Ke is None:
        Ke = _host(level.coef).reshape(-1, 1, 1) * _host(level.KE)[None]
    else:
        Ke = _host(level.Ke).reshape(-1, 24, 24)
    Ny, Nz = ny + 1, nz + 1
    nn = (nx + 1) * Ny * Nz
    i, j, k = np.meshgrid(np.arange(nx), np.arange(ny), np.arange(nz), indexing="ij")
    nodes = np.stack([((i + a) * Ny + (j + b)) * Nz + (k + c) for (a, b, c) in CORNERS]).reshape(8, -1)
    dofs = (np.arange(3)[None, :, None] * nn + nodes[:, None, :]).reshape(24, -1).T  # (elements, 24)
    n = 3 * nn
    on = np.nonzero(np.abs(Ke).reshape(len(Ke), -1).max(axis=1) > 0)[0]
    rows = np.broadcast_to(dofs[on, :, None], (len(on), 24, 24)).reshape(-1)
    cols = np.broadcast_to(dofs[on, None, :], (len(on), 24, 24)).reshape(-1)
    vals = Ke[on].reshape(-1) * free[rows] * free[cols]
    A = coo_matrix((vals, (rows, cols)), shape=(n, n)).tocsc() + diags(1.0 - free)
    return A.tocsc()


_P_CHILD = _child_prolongations()


class _Multigrid:
    """A geometric multigrid V-cycle preconditioner: damped Jacobi smoothing, a sparse direct coarsest solve.

    The coarsest solve runs on the host (SciPy's SuperLU); on a GPU only its
    small right-hand side and solution cross the bus.
    """

    def __init__(self, xp, KE, coef, free, levels: int, sweeps: int = 2, omega: float = 0.6, fine: "_Level | None" = None):
        from scipy.sparse.linalg import splu

        self.xp, self.sweeps, self.omega = xp, sweeps, omega
        self.levels = [fine or _Level(xp, free, KE=KE, coef=coef)]  # fine: a level of its own (tule.fea's cut cells)
        P = xp.asarray(_P_CHILD)
        for _ in range(levels - 1):
            self.levels.append(self.levels[-1].coarsen(P))
        for lv in self.levels:
            lv.make_diag()
        A = _sparse_stiffness(self.levels[-1])
        try:
            self.coarse = splu(A)
        except RuntimeError:  # singular (a floating piece): regularize slightly
            from scipy.sparse import identity

            self.coarse = splu((A + 1e-9 * abs(A.diagonal()).max() * identity(A.shape[0])).tocsc())

    @property
    def free(self):
        return self.levels[0].free

    def apply(self, r, l: int = 0):
        xp, lv = self.xp, self.levels[l]
        if l == len(self.levels) - 1:
            return xp.asarray(self.coarse.solve(_host(r).reshape(-1))).reshape(r.shape)
        w = self.omega / lv.diag
        z = w * r
        for _ in range(self.sweeps - 1):
            z = z + w * (r - lv.A(z))
        res = r - lv.A(z)
        nxt = self.levels[l + 1]
        rc = nxt.free * _restrict(lv.free * res, xp)
        z = z + lv.free * _prolong(self.apply(rc, l + 1), xp)
        for _ in range(self.sweeps):
            z = z + w * (r - lv.A(z))
        return z


def _pcg(A, M, b, x, tol: float, maxiter: int, xp):
    r = b - A(x)
    bn = float(xp.sqrt(xp.sum(b * b)))
    if bn == 0:
        return x * 0, 0
    z = M(r)
    p = z
    rz = float(xp.sum(r * z))
    for it in range(1, maxiter + 1):
        Ap = A(p)
        alpha = rz / float(xp.sum(p * Ap))
        x = x + alpha * p
        r = r - alpha * Ap
        if float(xp.sqrt(xp.sum(r * r))) <= tol * bn:
            return x, it
        z = M(r)
        rz_new = float(xp.sum(r * z))
        p = z + (rz_new / rz) * p
        rz = rz_new
    return x, maxiter


# ---------------------------------------------------------------- the density filter


class _Filter:
    """Linear (cone) density filter on the element grid, restricted to the design domain."""

    def __init__(self, xp, mask, radius_elems: float):
        self.xp = xp
        R = max(radius_elems, 1.0 + 1e-9)
        r = int(math.ceil(R)) - 1 if float(R).is_integer() else int(math.floor(R))
        self.offsets = []
        for a in range(-r, r + 1):
            for b in range(-r, r + 1):
                for c in range(-r, r + 1):
                    d = math.sqrt(a * a + b * b + c * c)
                    if R - d > 0:
                        self.offsets.append((a, b, c, R - d))
        self.r = r
        self.mask = mask
        self.fft = None
        if len(self.offsets) > 64:  # big kernels: convolve by FFT (same result, zero padding)
            k = np.zeros((2 * r + 1,) * 3)
            for a, b, c, w in self.offsets:
                k[a + r, b + r, c + r] = w
            self.size = tuple(n + 2 * r for n in mask.shape)
            self.fft = xp.fft.rfftn(xp.asarray(k), self.size)
        self.Hs = self._conv(mask)
        self.Hs = xp.where(mask > 0, self.Hs, 1.0)

    def _conv(self, x):
        xp, r = self.xp, self.r
        nx, ny, nz = x.shape
        if self.fft is not None:
            full = xp.fft.irfftn(xp.fft.rfftn(x * self.mask, self.size) * self.fft, self.size)
            return full[r:r + nx, r:r + ny, r:r + nz] * self.mask
        p = xp.zeros((nx + 2 * r, ny + 2 * r, nz + 2 * r), dtype=x.dtype)
        p[r:r + nx, r:r + ny, r:r + nz] = x * self.mask
        out = xp.zeros_like(x)
        for a, b, c, w in self.offsets:
            out += w * p[r + a:r + a + nx, r + b:r + b + ny, r + c:r + c + nz]
        return out * self.mask

    def __call__(self, x):
        return self._conv(x) / self.Hs

    def transpose(self, g):
        return self._conv(g / self.Hs)


# ---------------------------------------------------------------- voxels


def voxelize(shape: Shape, origin: VecLike, voxel: float, dims: Sequence[int]) -> np.ndarray:
    """Which element centers of a grid lie inside ``shape``: a boolean array of ``dims``.

    Each column of centers along z is intersected with the B-rep once
    (``IntCurvesFace_ShapeIntersector``); every stretch between two crossings
    is classified by one call to ``BRepClass3d_SolidClassifier``.
    """
    from OCP.BRepClass3d import BRepClass3d_SolidClassifier
    from OCP.gp import gp_Dir, gp_Lin, gp_Pnt
    from OCP.IntCurvesFace import IntCurvesFace_ShapeIntersector
    from OCP.TopAbs import TopAbs_IN

    o = vec(origin)
    nx, ny, nz = dims
    zc = o.z + (np.arange(nz) + 0.5) * voxel
    inside = np.zeros((nx, ny, nz), dtype=bool)
    lo, hi = shape.bounds
    if hi.x < o.x or hi.y < o.y or lo.x > o.x + nx * voxel or lo.y > o.y + ny * voxel:
        return inside
    si = IntCurvesFace_ShapeIntersector()
    si.Load(shape.wrapped, 1e-7)
    cl = BRepClass3d_SolidClassifier(shape.wrapped)
    z0, z1 = min(lo.z, o.z) - 1.0, max(hi.z, o.z + nz * voxel) + 1.0
    tol = 1e-9 * max(1.0, z1 - z0)
    for i in range(nx):
        x = o.x + (i + 0.5) * voxel
        if x < lo.x or x > hi.x:
            continue
        for j in range(ny):
            y = o.y + (j + 0.5) * voxel
            if y < lo.y or y > hi.y:
                continue
            si.Perform(gp_Lin(gp_Pnt(x, y, z0), gp_Dir(0, 0, 1)), 0.0, z1 - z0)
            ws = sorted({round(z0 + si.WParameter(n), 12) for n in range(1, si.NbPnt() + 1)})
            bounds = [z0] + ws + [z1]
            for a, b in zip(bounds[:-1], bounds[1:]):
                if b - a <= tol:
                    continue
                k0, k1 = np.searchsorted(zc, a, "right"), np.searchsorted(zc, b, "left")
                if k1 <= k0:
                    continue
                cl.Perform(gp_Pnt(x, y, 0.5 * (a + b)), 1e-7)
                if cl.State() == TopAbs_IN:
                    inside[i, j, k0:k1] = True
    return inside


@dataclass(frozen=True)
class _Grid:
    origin: Vec
    voxel: float
    dims: tuple  # elements (nx, ny, nz), padded for multigrid
    domain: np.ndarray  # bool (nx, ny, nz)
    keep: np.ndarray
    void: np.ndarray
    fixed: np.ndarray  # bool nodes (nx+1, ny+1, nz+1)
    force: np.ndarray  # (nx+1, ny+1, nz+1, 3)
    levels: int

    @property
    def elements(self) -> int:
        return int(self.domain.sum())


def _face_samples(shape: Shape, face_ids: Iterable[int], spacing: float) -> np.ndarray:
    m = shape.tessellate(min(0.05, spacing / 4), 0.2)
    sel = np.isin(m.face_ids, list(face_ids))
    V = m.positions.astype(np.float64)[m.indices[sel].astype(np.int64)]
    if not len(V):
        return np.zeros((0, 3))
    a, b, c = V[:, 0], V[:, 1], V[:, 2]
    L = np.max([np.linalg.norm(b - a, axis=1), np.linalg.norm(c - b, axis=1), np.linalg.norm(a - c, axis=1)], axis=0)
    N = np.maximum(1, np.ceil(L / spacing)).astype(np.int64)
    pts = []
    for n in np.unique(N):  # a lattice of (n + 1)(n + 2) / 2 points per triangle, for all triangles of that n at once
        i, j = np.meshgrid(np.arange(n + 1), np.arange(n + 1), indexing="ij")
        ok = i + j <= n
        u, v = i[ok] / n, j[ok] / n
        k = N == n
        A = a[k]
        pts.append((A[:, None] + u[None, :, None] * (b[k] - A)[:, None] + v[None, :, None] * (c[k] - A)[:, None]).reshape(-1, 3))
    return np.concatenate(pts)


def _face_nodes(shape: Shape, faces: Faces, origin: Vec, h: float, node_active: np.ndarray) -> np.ndarray:
    """Grid nodes on or next to ``faces``: a bool node array."""
    ids = shape._resolve(faces, "face").ids
    pts = _face_samples(shape, ids, h / 2)
    N = np.array(node_active.shape)
    out = np.zeros(node_active.shape, dtype=bool)
    if len(pts) == 0:
        return out
    q = (pts - np.array(origin)) / h
    base = np.floor(q).astype(np.int64)
    best_d = np.full(len(pts), np.inf)
    best = np.zeros((len(pts), 3), dtype=np.int64)
    cands = []
    for c in CORNERS:
        idx = base + np.array(c)
        ok = np.all((idx >= 0) & (idx < N), axis=1)
        idx = np.clip(idx, 0, N - 1)
        act = ok & node_active[idx[:, 0], idx[:, 1], idx[:, 2]]
        d = np.linalg.norm(q - idx, axis=1)
        d = np.where(act, d, np.inf)
        cands.append((idx, d))
        better = d < best_d
        best_d[better] = d[better]
        best[better] = idx[better]
    for idx, d in cands:
        take = d <= 0.6
        out[idx[take, 0], idx[take, 1], idx[take, 2]] = True
    near = best_d <= 1.5
    out[best[near, 0], best[near, 1], best[near, 2]] = True
    return out


# ---------------------------------------------------------------- the problem


def _force_label(f: Vec) -> str:
    mag = f.length
    for name, d in (("X", (1, 0, 0)), ("Y", (0, 1, 0)), ("Z", (0, 0, 1))):
        if abs(abs(f.dot(d)) - mag) < 1e-9 * max(mag, 1):
            return f"{mag:g} N {'+' if f.dot(d) > 0 else '-'}{name}"
    return f"{mag:g} N ({f.x:g}, {f.y:g}, {f.z:g})"


@dataclass(frozen=True)
class Problem:
    """A compliance problem on a design domain: supports, loads, a volume budget, fixed regions.

    ``fixed`` are faces of ``domain`` held in place; ``loads`` maps face
    selections of ``domain`` to the total force on them (N, spread over the
    grid nodes on those faces); ``volume_fraction`` is the share of the
    domain to keep; ``keep`` and ``void`` are Shapes whose voxels stay solid
    or empty. ``E`` (MPa) and ``nu`` are the material.
    """

    domain: Shape
    fixed: Faces
    loads: Any  # {Faces: force}; stored as a tuple of (Faces, Vec)
    volume_fraction: float = 0.3
    keep: Any = ()
    void: Any = ()
    E: float = 70_000.0
    nu: float = 0.3

    def __post_init__(self):
        if not isinstance(self.domain, Shape):
            raise TuleError(f"the domain must be a Shape, got {type(self.domain).__name__}", op="topopt")
        if not isinstance(self.fixed, Faces) or not self.fixed:
            raise TuleError("fixed must be a non-empty face selection of the domain", op="topopt",
                            hint='e.g. fixed=part.faces["bolt"] after part = part.tag("bolt", ...)')
        object.__setattr__(self, "fixed", self.domain._resolve(self.fixed, "face"))
        loads = self.loads.items() if isinstance(self.loads, Mapping) else self.loads
        norm = []
        for faces, force in loads:
            if not isinstance(faces, Faces) or not faces:
                raise TuleError("each load needs a non-empty face selection", op="topopt",
                                hint='loads={part.faces["load"]: (0, 0, -500)}')
            f = vec(force)
            if f.length == 0:
                raise TuleError("a load force is zero", op="topopt", hint="give the total force in N, e.g. (0, 0, -500)")
            norm.append((self.domain._resolve(faces, "face"), f))
        if not norm:
            raise TuleError("no loads given", op="topopt", hint='loads={part.faces["load"]: (0, 0, -500)}')
        object.__setattr__(self, "loads", tuple(norm))
        if not 0 < self.volume_fraction < 1:
            raise TuleError("volume_fraction must be between 0 and 1", op="topopt", hint="0.2 to 0.4 is typical")
        for name in ("keep", "void"):
            v = getattr(self, name)
            v = (v,) if isinstance(v, Shape) else tuple(v or ())
            if not all(isinstance(s, Shape) for s in v):
                raise TuleError(f"{name} must be Shapes", op="topopt", hint=f"{name}=[cylinder(6, 10).at(...)]")
            object.__setattr__(self, name, v)

    # -- viewing the setup
    def show(self, name: str, title: str | None = None) -> str:
        """Write the boundary conditions as an ``overlay`` output: fixed and load faces, force arrows."""
        from . import outputs
        from .selection import face_area, face_center

        faces: dict[int, dict] = {}
        for i in self.fixed.ids:
            faces[i] = {"role": "fixed", "label": "fixed"}
        arrows = []
        for sel, f in self.loads:
            label = _force_label(f)
            for i in sel.ids:
                faces[i] = {"role": "load", "label": label}
            w = [face_area(x) for x in sel.items]
            c = sum((np.array(face_center(x)) * a for x, a in zip(sel.items, w)), np.zeros(3)) / max(sum(w), 1e-30)
            arrows.append({"at": c.tolist(), "dir": list(f.normalized()), "label": label})
        return outputs.overlay(name, self.domain, faces, arrows=arrows,
                               title=title or f"Topology optimization setup, volume fraction {self.volume_fraction:g}")

    # -- discretization
    def _grid(self, voxel: float, levels: int) -> _Grid:
        lo, hi = self.domain.bounds
        size = np.array(hi - lo)
        mult = 2 ** (levels - 1)
        dims = tuple(int(max(1, math.ceil(s / voxel - 1e-9))) for s in size)
        dims = tuple(int(math.ceil(d / mult) * mult) for d in dims)
        origin = Vec(*lo)
        domain = voxelize(self.domain, origin, voxel, dims)
        if not domain.any():
            raise TuleError("the domain has no voxels", op="topopt", hint=f"use a smaller voxel than {voxel:g} mm")
        keep = np.zeros_like(domain)
        void = np.zeros_like(domain)
        for s in self.keep:
            keep |= voxelize(s, origin, voxel, dims)
        for s in self.void:
            void |= voxelize(s, origin, voxel, dims)
        keep &= domain
        void &= domain & ~keep
        node_active = np.zeros(tuple(d + 1 for d in dims), dtype=bool)
        for (i, j, k) in CORNERS:
            node_active[i:i + dims[0], j:j + dims[1], k:k + dims[2]] |= domain
        fixed = _face_nodes(self.domain, self.fixed, origin, voxel, node_active)
        if not fixed.any():
            raise TuleError("no grid nodes found on the fixed faces", op="topopt", faces=self.fixed.ids,
                            hint="use a smaller voxel so the faces are resolved")
        force = np.zeros(node_active.shape + (3,))
        for sel, f in self.loads:
            nodes = _face_nodes(self.domain, sel, origin, voxel, node_active)
            n = int(nodes.sum())
            if n == 0:
                raise TuleError("no grid nodes found on a load's faces", op="topopt", faces=sel.ids,
                                hint="use a smaller voxel so the faces are resolved")
            force[nodes] += np.array(f) / n
        return _Grid(origin, voxel, dims, domain, keep, void, fixed, force, levels)


# ---------------------------------------------------------------- the result


@dataclass(frozen=True)
class Result:
    """The optimized design: densities on the voxel grid, the history, and a smooth isosurface."""

    problem: Problem
    density: np.ndarray  # (nx, ny, nz) element densities, 0 outside the domain
    origin: Vec
    voxel: float
    history: tuple  # one dict per iteration: compliance, volume, change, cg_iterations, voxel
    mesh: Mesh
    seconds: float
    device: str

    @property
    def compliance(self) -> float:
        return self.history[-1]["compliance"]

    @property
    def volume_fraction(self) -> float:
        return self.history[-1]["volume"]

    @property
    def iterations(self) -> int:
        return len(self.history)

    def to_shape(self) -> Shape:
        """The isosurface as a closed, triangulated solid Shape (one planar face per triangle)."""
        return mesh_to_shape(self.mesh)

    def __str__(self) -> str:
        h = self.history
        cg = [x["cg_iterations"] for x in h]
        return (f"topopt: compliance {self.compliance:.4g} N·mm (from {h[0]['compliance']:.4g}), volume fraction "
                f"{self.volume_fraction:.3f} (target {self.problem.volume_fraction:g}), {self.iterations} iterations, "
                f"{self.density.size} voxels of {self.voxel:.3g} mm, CG {min(cg)}–{max(cg)} per solve, "
                f"{self.seconds:.1f} s on {self.device}; mesh {self.mesh.triangles} triangles")

    __repr__ = __str__


# ---------------------------------------------------------------- isosurface


def isosurface(density: np.ndarray, origin: VecLike, voxel: float, level: float | None = None,
               smooth: bool = True) -> Mesh:
    """A closed triangle mesh around the voxels whose density exceeds ``level``.

    Element densities are samples at the element centers; the field is
    padded with zeros so the surface is closed, blurred slightly and meshed
    with marching cubes, then Taubin-smoothed (``smooth``). Without a
    ``level`` it is chosen so the solid keeps the design's volume
    (``sum(density)`` voxels). The one place the mesher lives, so a sharper
    one (dual contouring) can replace it.
    """
    from scipy.ndimage import gaussian_filter
    from skimage.measure import marching_cubes

    d = np.pad(np.asarray(density, dtype=np.float64), 1)
    if smooth:
        d = gaussian_filter(d, 0.6, mode="constant")
    if level is None:
        want = float(np.sum(density))
        vals = np.sort(d.reshape(-1))[::-1]
        k = int(min(max(round(want), 1), len(vals) - 1))
        level = float(np.clip(0.5 * (vals[k - 1] + vals[k]), 0.05, 0.95)) if want > 0 else 0.5
    if d.max() <= level or d.min() >= level:
        return Mesh(np.zeros((0, 3), np.float32), np.zeros((0, 3), np.float32), np.zeros((0, 3), np.uint32),
                    np.zeros(0, np.uint32), [])
    with warnings.catch_warnings():  # scikit-image 0.26 sets array shapes, deprecated in NumPy 2.5
        warnings.simplefilter("ignore", DeprecationWarning)
        verts, tris, _, _ = marching_cubes(d, level, allow_degenerate=False)
    P = np.array(origin, dtype=np.float64) + (verts - 0.5) * voxel  # padded sample i sits at center of element i-1
    T = tris.astype(np.int64)
    if smooth:
        P = _taubin(P, T, 10)
    # outward winding: positive signed volume
    t = P[T]
    vol = np.einsum("ij,ij->i", t[:, 0], np.cross(t[:, 1], t[:, 2])).sum() / 6
    if vol < 0:
        T = T[:, ::-1]
    N = _vertex_normals(P, T)
    return Mesh(P.astype(np.float32), N.astype(np.float32), T.astype(np.uint32), np.zeros(len(T), np.uint32), [])


def _taubin(P: np.ndarray, T: np.ndarray, iterations: int, lam: float = 0.5, mu: float = -0.53) -> np.ndarray:
    from scipy.sparse import coo_matrix

    n = len(P)
    i = np.concatenate([T[:, 0], T[:, 1], T[:, 2], T[:, 1], T[:, 2], T[:, 0]])
    j = np.concatenate([T[:, 1], T[:, 2], T[:, 0], T[:, 0], T[:, 1], T[:, 2]])
    W = coo_matrix((np.ones(len(i)), (i, j)), shape=(n, n)).tocsr()
    W.data[:] = 1.0
    deg = np.asarray(W.sum(axis=1)).reshape(-1, 1)
    deg[deg == 0] = 1
    for _ in range(iterations):
        for f in (lam, mu):
            P = P + f * (W @ P / deg - P)
    return P


def _vertex_normals(P: np.ndarray, T: np.ndarray) -> np.ndarray:
    fn = np.cross(P[T[:, 1]] - P[T[:, 0]], P[T[:, 2]] - P[T[:, 0]])
    N = np.zeros_like(P)
    for c in range(3):
        np.add.at(N, T[:, c], fn)
    ln = np.linalg.norm(N, axis=1, keepdims=True)
    return N / np.maximum(ln, 1e-30)


def mesh_to_shape(mesh: Mesh) -> Shape:
    """A closed triangle mesh as a solid Shape (shared edges, one planar face per triangle)."""
    from OCP.BRep import BRep_Builder
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeShapeOnMesh
    from OCP.BRepCheck import BRepCheck_Analyzer
    from OCP.gp import gp_Pnt
    from OCP.Poly import Poly_Triangle, Poly_Triangulation
    from OCP.TopoDS import TopoDS_Shell, TopoDS_Solid

    from ._occ import explore

    P, T = mesh.positions.astype(np.float64), mesh.indices.astype(np.int64)
    if len(T) == 0:
        raise TuleError("the mesh is empty", op="mesh_to_shape")
    # every edge must be shared by exactly two triangles for a closed solid
    e = np.sort(np.concatenate([T[:, [0, 1]], T[:, [1, 2]], T[:, [2, 0]]]), axis=1)
    _, counts = np.unique(e, axis=0, return_counts=True)
    if np.any(counts != 2):
        raise TuleError("the mesh is not closed and manifold", op="mesh_to_shape",
                        hint=f"{int(np.sum(counts != 2))} edges are not shared by exactly two triangles; "
                        "export the mesh (outputs.part) instead")
    tri = Poly_Triangulation(len(P), len(T), False)
    for i, p in enumerate(P, 1):
        tri.SetNode(i, gp_Pnt(*p))
    for i, (a, b, c) in enumerate(T, 1):
        tri.SetTriangle(i, Poly_Triangle(int(a) + 1, int(b) + 1, int(c) + 1))
    mk = BRepBuilderAPI_MakeShapeOnMesh(tri)  # one planar face per triangle, edges shared
    mk.Build()
    faces = list(explore(mk.Shape(), "face"))
    if not faces:
        raise TuleError("could not build faces from the mesh", op="mesh_to_shape")
    builder = BRep_Builder()
    shell = TopoDS_Shell()
    builder.MakeShell(shell)
    for f in faces:
        builder.Add(shell, f)
    shell.Closed(True)
    solid = TopoDS_Solid()
    builder.MakeSolid(solid)
    builder.Add(solid, shell)
    s = Shape(solid)
    if s.volume < 0:
        s = Shape(solid.Reversed())
    if not BRepCheck_Analyzer(s.wrapped).IsValid():
        raise TuleError("the mesh solid is not valid", op="mesh_to_shape", hint="export the mesh (outputs.part) instead")
    return s


# ---------------------------------------------------------------- the optimizer


def _auto_voxel(problem: Problem, elements: int) -> float:
    lo, hi = problem.domain.bounds
    size = np.array(hi - lo)
    v = problem.domain.volume
    h = (v / elements) ** (1 / 3)
    return float(min(h, size.max() / 4))


def _levels_for(dims: Sequence[int], max_coarse_dofs: int = 3000, min_coarse_elements: int = 4) -> int:
    """Multigrid levels for a grid: coarsen by 2 until the coarsest grid is small, but not so coarse
    (fewer than ``min_coarse_elements`` across) that it loses the part's shape."""
    levels = 1
    d = np.array(dims, dtype=float)
    while True:
        nodes = np.prod(np.ceil(d / 2 ** (levels - 1)) + 1)
        if 3 * nodes <= max_coarse_dofs or np.min(np.ceil(d / 2 ** levels)) < min_coarse_elements or levels >= 6:
            return levels
        levels += 1


def _oc(x, dc, dv, design, apply_bounds, volume_of, target: float, move: float, xp):
    """One optimality-criteria update: the bisection on the multiplier hits the volume target."""
    l1, l2 = 0.0, 1e9 * float(xp.max(-dc) + 1e-30) / float(xp.max(dv) + 1e-30) + 1e-30
    ratio = xp.sqrt(xp.maximum(-dc, 0) / xp.maximum(dv, 1e-30))
    lo, hi = xp.maximum(x - move, 0.0), xp.minimum(x + move, 1.0)
    xnew = x
    for _ in range(80):
        lmid = 0.5 * (l1 + l2)
        xnew = xp.clip(x * ratio / math.sqrt(lmid), lo, hi)
        xnew = apply_bounds(xp.where(design, xnew, x))
        if volume_of(xnew) > target:
            l1 = lmid
        else:
            l2 = lmid
        if (l2 - l1) <= 1e-6 * (l1 + l2):
            break
    return xnew


EMIN = 1e-6  # void stiffness, relative to E
UPDATES = {"oc": _oc}  # "mma" later: same inputs plus more constraints


def optimize(
    problem: Problem,
    *,
    voxel: float | None = None,
    iterations: int = 60,
    penal: float = 3.0,
    filter_radius: float | None = None,
    continuation: bool = True,
    output: str | None = "topopt",
    frames: int = 40,
    device: str = "auto",
    method: str = "oc",
    move: float = 0.2,
    tol: float = 0.01,
    cg_tol: float = 1e-5,
    verbose: bool = False,
) -> Result:
    """Minimize compliance for ``problem``; returns a ``Result``.

    ``voxel`` is the element edge (mm; default: about 20,000 elements in the
    domain). ``iterations`` is the total budget (the run stops early when no
    density changes by more than ``tol``). ``filter_radius`` (mm, default
    2 voxels) is the minimum feature size. With ``continuation`` the first
    part of the run is on voxels twice as big. ``output`` names a
    ``sequence`` output that grows by one frame (an isosurface) per iteration,
    up to ``frames`` frames; ``None`` writes nothing. ``device``: ``"auto"``,
    ``"cpu"`` or ``"gpu"``.
    """
    if method not in UPDATES:
        raise TuleError(f"unknown method {method!r}", op="topopt", hint=f"available: {', '.join(UPDATES)}")
    xp = backend(device)
    dev = "gpu" if xp is not np else "cpu"
    t_start = time.time()
    h = float(voxel) if voxel else _auto_voxel(problem, 20_000)
    if h <= 0:
        raise TuleError("voxel must be positive", op="topopt")
    rfilter = float(filter_radius) if filter_radius else 2.0 * h
    stages = [(h, iterations)]
    lo, hi = problem.domain.bounds
    if continuation and min(np.array(hi - lo)) / (2 * h) >= 4 and iterations >= 10:
        n0 = int(round(iterations * 0.6))
        stages = [(2 * h, n0), (h, iterations - n0)]
    KE = element_stiffness(problem.nu)
    seq = None
    if output:
        from . import outputs

        seq = outputs.sequence(output, f"Topology optimization, volume fraction {problem.volume_fraction:g}")
    stride = max(1, int(math.ceil(iterations / max(frames, 1))))
    history: list[dict] = []
    x_prev = None
    grid_prev = None
    try:
        for si, (hs, n_it) in enumerate(stages):
            dims0 = tuple(int(max(1, math.ceil(s / hs - 1e-9))) for s in np.array(hi - lo))
            levels = _levels_for(dims0)
            g = problem._grid(hs, levels)
            if verbose:
                print(f"topopt: {g.elements} elements of {hs:.3g} mm in a {g.dims[0]}×{g.dims[1]}×{g.dims[2]} grid, "
                      f"{levels} multigrid levels, {dev}")
            domain = xp.asarray(g.domain.astype(np.float64))
            keep = xp.asarray(g.keep)
            void = xp.asarray(g.void)
            design = xp.asarray(g.domain & ~g.keep & ~g.void)
            free = xp.asarray(np.repeat((~g.fixed)[None], 3, axis=0).astype(np.float64))
            f = xp.asarray(np.moveaxis(g.force, -1, 0)) * free
            filt = _Filter(xp, domain, max(rfilter, 1.5 * hs) / hs)
            n_dom = float(g.domain.sum())
            KEd = xp.asarray(KE)
            target = problem.volume_fraction

            def bounds(x):
                return xp.where(keep, 1.0, xp.where(void, 0.0, x)) * domain

            def phys(x):
                return bounds(filt(x))

            def volume_of(x):
                return float(xp.sum(phys(x))) / n_dom

            if x_prev is None:
                x = bounds(domain * target)
            else:  # prolong: every fine element takes its parent's density
                xc = x_prev
                xf = np.repeat(np.repeat(np.repeat(xc, 2, 0), 2, 1), 2, 2)
                xf = xf[:g.dims[0], :g.dims[1], :g.dims[2]]
                pad = [(0, max(0, g.dims[d] - xf.shape[d])) for d in range(3)]
                xf = np.pad(xf, pad, mode="edge")
                x = bounds(xp.asarray(xf) * domain)
            u = xp.zeros(free.shape)
            Emin = EMIN
            for it in range(n_it):
                xphys = phys(x)
                coef = problem.E * hs * (Emin + (1 - Emin) * xphys ** penal) * domain
                mg = _Multigrid(xp, KEd, coef, free, levels)
                lv = mg.levels[0]
                u, cg_it = _pcg(lv.A, mg.apply, f * lv.free, u * lv.free, cg_tol, 500, xp)
                ue = lv.gather(u)
                ce = xp.sum((KEd @ ue) * ue, axis=0).reshape(coef.shape)
                c = float(xp.sum(coef * ce))
                dc_phys = -penal * problem.E * hs * (1 - Emin) * xphys ** (penal - 1) * ce * domain
                # kept and void voxels do not follow x (phys overrides them): nothing of theirs is filtered back
                passive = keep | void
                dc = filt.transpose(xp.where(passive, 0.0, dc_phys))
                dv = filt.transpose(xp.where(passive, 0.0, domain / n_dom))
                xnew = UPDATES[method](x, dc, dv, design, bounds, volume_of, target, move, xp)
                change = float(xp.max(xp.abs(xnew - x)))
                x = xnew
                vol = float(xp.sum(xphys)) / n_dom
                rec = {"compliance": c, "volume": vol, "change": change, "cg_iterations": int(cg_it), "voxel": hs}
                history.append(rec)
                if verbose:
                    print(f"  it {len(history):3d}  compliance {c:.5g}  volume {vol:.3f}  change {change:.3f}  CG {cg_it}")
                last_stage = si == len(stages) - 1
                done = change < tol or it == n_it - 1
                if seq is not None and (len(history) - 1) % stride == 0 and not (last_stage and done):
                    m = isosurface(_host(xphys)[: g.dims[0], : g.dims[1], : g.dims[2]], g.origin, hs, level=0.5, smooth=False)
                    if m.triangles:
                        seq.add(m, label=f"iteration {len(history)}", compliance=c, volume=vol, change=change)
                if change < tol:
                    break
            x_prev = _host(x)
            grid_prev = g
        xphys = _host(phys(x))
        mesh = isosurface(xphys, grid_prev.origin, grid_prev.voxel, smooth=True)
        if seq is not None:
            last = history[-1]
            seq.add(mesh, label=f"result ({len(history)} iterations)", compliance=last["compliance"],
                    volume=last["volume"], change=last["change"])
            seq.done()
    except BaseException:
        if seq is not None:
            seq.done("failed")
        raise
    return Result(problem, xphys, grid_prev.origin, grid_prev.voxel, tuple(history), mesh, time.time() - t_start, dev)
