"""Implicit bodies (signed distance fields): ``tule.Implicit``, one of Tule's three kinds of geometry.

A body is an expression over ``x, y, z`` (mm), negative inside, built from primitives, booleans
(sharp, or ``smooth=`` blends), offsets, shells, TPMS lattices and any math you write.
Nothing is meshed until ``to_mesh(cell=…)``, which runs Fidget (JIT-compiled, dual contouring:
sharp edges stay sharp).

```python
from tule import sdf

body = sdf.box(60, 30, 20, center=True)
body = sdf.union(body, sdf.sphere(14).at(30, 0, 10), smooth=4)   # a 4 mm blend
infill = body & sdf.gyroid(period=8, thickness=0.8)              # a gyroid lattice inside it
part = body.shell(1.6) | infill                                  # a 1.6 mm skin around the lattice
wave = sdf.field(lambda x, y, z: z - 2 * sdf.sin(x / 5), bounds=((-20, -20, -5), (20, 20, 5)))
mesh = part.to_mesh(cell=0.3)                                    # a tule.Mesh (Fidget)
```

Bounds travel with the expression (unions grow them, intersections shrink them); TPMS fields
and planes are unbounded until intersected with something bounded.

Revolved and repeated parts, features that follow a wall, tubes, and big parts with fine detail:

```python
bell = sdf.spline([(0, 0), (60, 0), (60, 40), (25, 110), (0, 110)]).revolve()  # a 2D profile: u = radius, v = z
nut = (sdf.rect(20, 8, radius=1) - sdf.circle(3)).extrude(5)                  # 2D booleans, then a 3D body
bolts = sdf.cylinder(4, 12).at(250, 0, 0).repeat_polar(96)                    # 96 copies, one copy's expression
row = sdf.sphere(3).repeat_linear((10, 0, 0), 20)
wall = sdf.Wall([0, 300, 420, 900], [220, 220, 130, 420], samples=8)          # a revolved contour r(z)
liner = wall.solid(0, 6)                                                      # 0 to 6 mm out along its normal
cooled = liner - wall.channels(200, land=1.0, depth=[(0, 3), (420, 2), (900, 4)], offset=1.0)
duct = sdf.tube([(0, 0, 0), (0, 0, 200), (150, 0, 300)], r=30, wall=3, bend=80)
mesh = cooled.to_mesh(4, fine=[((-500, -8, -10), (500, 8, 910), 0.5)], rest=liner)
```

* 2D regions (``Region``): ``polygon``, ``spline`` (closed, or an open stroke with ``width``),
  ``polyline(points, width)``, ``rect(w, h, radius=)``, ``circle``; booleans, ``offset``, ``shell``,
  ``at``, ``rotate``, ``mirror``; ``revolve(degrees)`` about z (u is the radius) or ``extrude(h)``.
  ``wedge(degrees)`` is a sector through the z axis (a cutaway: ``body - sdf.wedge(90)``).
* Domain repetition: ``body.repeat_polar(n)`` (model the copy on +x) and ``body.repeat_linear(step, n)``
  cost one copy's evaluation; ``overlap=True`` also checks the neighbours for copies that reach over.
* ``Wall(z, r)``: coordinates on a revolved contour (``n`` normal offset, ``s`` length along it,
  ``t`` distance around, ``theta``) and bodies in them: ``solid``, ``channels`` (width, or ``land``
  so they narrow with the circumference at a throat), ``ribs``, ``bands``. Sizes are a number, a
  station table ``[(z, value), …]`` or a function of the z expression. The contour is r over z
  (one radius per height); ``n`` is the first-order normal distance (exact on straight segments).
* ``tube(points, r, wall=, bend= | spline=True)``: a round tube along a 3D path, tapered when ``r``
  has one radius per point, hollow and open-ended with ``wall``.
* ``to_mesh(cell, fine=[(lo, hi, cell), …], rest=)``: banded meshing. Each box is meshed at its own
  cell, the rest at ``cell`` (from ``rest`` when given: the part without the hidden detail), each a
  closed piece; regions larger than one octree (4096 cells) are split the same way.
"""

from __future__ import annotations

import builtins
import math
from typing import Callable, Iterable, Sequence

import numpy as np

from .errors import TuleError
from .look import Looks, with_look
from .geom import Vec, VecLike, vec

# ---------------------------------------------------------------- expressions


class Expr:
    """A node of an expression over x, y, z. Arithmetic and the functions here build new nodes."""

    __slots__ = ("op", "args", "value")

    def __init__(self, op: str, args: tuple = (), value=0.0):
        self.op = op
        self.args = args
        self.value = value

    # arithmetic
    def __add__(self, o):
        o = _e(o)
        if o.op == "const" and o.value == 0:
            return self
        return _fold("add", self, o)

    __radd__ = __add__

    def __sub__(self, o):
        o = _e(o)
        if o.op == "const" and o.value == 0:
            return self
        return _fold("sub", self, o)

    def __rsub__(self, o):
        return _fold("sub", _e(o), self)

    def __mul__(self, o):
        o = _e(o)
        if o.op == "const" and o.value == 1:
            return self
        return _fold("mul", self, o)

    __rmul__ = __mul__

    def __truediv__(self, o):
        o = _e(o)
        if o.op == "const":
            return self * (1.0 / o.value)
        return _fold("div", self, o)

    def __rtruediv__(self, o):
        return _fold("div", _e(o), self)

    def __neg__(self):
        if self.op == "const":
            return Expr("const", (), -self.value)
        return Expr("neg", (self,))

    def __pos__(self):
        return self

    def __abs__(self):
        return Expr("abs", (self,))

    def __mod__(self, o):
        return _fold("mod", self, _e(o))

    def __pow__(self, p):
        if p == 2:
            return Expr("square", (self,))
        if p == 1:
            return self
        if p == 0.5:
            return Expr("sqrt", (self,))
        if isinstance(p, int) and p > 2:
            out = self
            for _ in range(p - 1):
                out = out * self
            return out
        return exp(log(self) * p)

    def __bool__(self):
        raise TypeError("an expression has no truth value: use sdf.min/max/where-style math, not if/and/or")

    def __repr__(self) -> str:
        return f"<Expr {self.op}{' ' + format(self.value, 'g') if self.op == 'const' else ''}: {_count(self)} nodes>"


def _e(v) -> Expr:
    if isinstance(v, Expr):
        return v
    if isinstance(v, (int, float, np.floating, np.integer)):
        return Expr("const", (), float(v))
    raise TypeError(f"expected a number or an expression, got {type(v).__name__}")


_FOLD = {"add": lambda a, b: a + b, "sub": lambda a, b: a - b, "mul": lambda a, b: a * b, "div": lambda a, b: a / b,
         "min": min, "max": max, "mod": lambda a, b: a % b, "atan2": math.atan2}


def _fold(op, a, b) -> Expr:
    if a.op == "const" and b.op == "const" and op in _FOLD:
        return Expr("const", (), float(_FOLD[op](a.value, b.value)))
    return Expr(op, (a, b))


def _unary(op):
    np_f = {"sqrt": math.sqrt, "sin": math.sin, "cos": math.cos, "tan": math.tan, "asin": math.asin, "acos": math.acos,
            "atan": math.atan, "exp": math.exp, "ln": math.log, "floor": math.floor, "ceil": math.ceil}.get(op)

    def f(a):
        a = _e(a)
        if a.op == "const" and np_f is not None:
            return Expr("const", (), float(np_f(a.value)))
        return Expr(op, (a,))

    f.__name__ = op
    return f


sqrt, sin, cos, tan = _unary("sqrt"), _unary("sin"), _unary("cos"), _unary("tan")
asin, acos, atan, exp, floor, ceil = _unary("asin"), _unary("acos"), _unary("atan"), _unary("exp"), _unary("floor"), _unary("ceil")
log = _unary("ln")


def atan2(y, x) -> Expr:
    return _fold("atan2", _e(y), _e(x))


def min(*v) -> Expr:  # noqa: A001 - sdf.min, as math
    vals = list(v[0]) if len(v) == 1 and not isinstance(v[0], (Expr, int, float)) else list(v)
    out = _e(vals[0])
    for o in vals[1:]:
        out = _fold("min", out, _e(o))
    return out


def max(*v) -> Expr:  # noqa: A001
    vals = list(v[0]) if len(v) == 1 and not isinstance(v[0], (Expr, int, float)) else list(v)
    out = _e(vals[0])
    for o in vals[1:]:
        out = _fold("max", out, _e(o))
    return out


def clamp(v, lo, hi) -> Expr:
    return max(min(v, hi), lo)


def mix(a, b, t) -> Expr:
    """a + (b - a) t."""
    return _e(a) + (_e(b) - _e(a)) * t


def interp(t, xs, ys) -> Expr:
    """A station table as a field: piecewise linear in ``t`` through ``(xs[i], ys[i])``, constant
    beyond the ends (as ``numpy.interp``). ``r = sdf.interp(x, [0, 2, 9], [0, 0.4, 0.86])``."""
    xs, ys = [float(v) for v in xs], [float(v) for v in ys]
    if len(xs) != len(ys) or len(xs) < 1 or any(b <= a for a, b in zip(xs, xs[1:])):
        raise TuleError("interp(t, xs, ys): xs increasing, as many ys", op="sdf.interp",
                        hint="sdf.interp(x, [0, 2, 9], [0, 0.4, 0.86])")
    out = _e(ys[0])
    for (ta, va), (tb, vb) in zip(zip(xs, ys), zip(xs[1:], ys[1:])):
        if vb != va:
            out = out + (vb - va) / (tb - ta) * clamp(t - ta, 0, tb - ta)
    return out


def smooth_min(a, b, k: float) -> Expr:
    """Union of two fields with a fillet of about ``k`` (polynomial smooth minimum)."""
    if k <= 0:
        return min(a, b)
    h = max(k - abs(_e(a) - b), 0) / k
    return min(a, b) - h * h * (k / 4)


def smooth_max(a, b, k: float) -> Expr:
    """Intersection of two fields, its edge rounded by about ``k``."""
    return -smooth_min(-_e(a), -_e(b), k)


def slab(t, lo: float, hi: float) -> Expr:
    """``lo <= t <= hi`` as a field (a station range: ``sdf.slab(x, 3.6, 13.0)``)."""
    return max(lo - _e(t), _e(t) - hi)


def length(*v) -> Expr:
    out = None
    for c in v:
        out = _e(c) ** 2 if out is None else out + _e(c) ** 2
    return sqrt(out)


x, y, z = Expr("x"), Expr("y"), Expr("z")
_X, _Y, _Z = x, y, z  # for code whose arguments are called x, y, z


def _nodes(root: Expr) -> list[Expr]:
    """Every node once, arguments before the nodes that use them."""
    order, seen, stack = [], set(), [(root, False)]
    while stack:
        n, done = stack.pop()
        if done:
            order.append(n)
            continue
        if id(n) in seen:
            continue
        seen.add(id(n))
        stack.append((n, True))
        for a in n.args:
            if id(a) not in seen:
                stack.append((a, False))
    return order


def _count(root: Expr) -> int:
    return len(_nodes(root))


def _has_grid(root: Expr) -> bool:
    return any(n.op == "grid" for n in _nodes(root))


def _ops(root: Expr) -> list[tuple]:
    """The node list the Fidget bindings take: (op, a, b, value)."""
    order = _nodes(root)
    index = {id(n): i for i, n in enumerate(order)}
    out = []
    for n in order:
        a = index[id(n.args[0])] if len(n.args) > 0 else -1
        b = index[id(n.args[1])] if len(n.args) > 1 else -1
        out.append((n.op, a, b, float(n.value) if n.op == "const" else 0.0))
    return out


def _remap(root: Expr, X: Expr, Y: Expr, Z: Expr) -> Expr:
    """The expression with x, y, z replaced."""
    memo: dict[int, Expr] = {}
    for n in _nodes(root):
        if n.op == "x":
            r = X
        elif n.op == "y":
            r = Y
        elif n.op == "z":
            r = Z
        elif not n.args:
            r = n
        else:
            r = Expr(n.op, tuple(memo[id(a)] for a in n.args), n.value)
        memo[id(n)] = r
    return memo[id(root)]


# ---------------------------------------------------------------- evaluation (Fidget, or NumPy)

_NP2 = {"add": np.add, "sub": np.subtract, "mul": np.multiply, "div": np.divide, "min": np.minimum, "max": np.maximum,
        "atan2": np.arctan2, "mod": np.mod}
_NP1 = {"neg": np.negative, "abs": np.abs, "sqrt": np.sqrt, "square": np.square, "recip": np.reciprocal, "sin": np.sin,
        "cos": np.cos, "tan": np.tan, "asin": np.arcsin, "acos": np.arccos, "atan": np.arctan, "exp": np.exp, "ln": np.log,
        "floor": np.floor, "ceil": np.ceil, "round": np.round}


def _fidget():
    try:
        import tule_fidget
    except ImportError:
        return None
    return tule_fidget


_warned = []


def _note_numpy(why: str):
    if not _warned:
        _warned.append(1)
        import sys

        print(f"tule.sdf: {why}: evaluating with NumPy and meshing by marching cubes (slower, edges rounded)", file=sys.stderr)


def _np_eval(root: Expr, P: np.ndarray) -> np.ndarray:
    out = np.empty(len(P))
    step = 1 << 20
    order = _nodes(root)
    with np.errstate(all="ignore"):
        for s in range(0, len(P), step):
            Q = P[s:s + step]
            memo: dict[int, np.ndarray] = {}
            for n in order:
                if n.op in ("x", "y", "z"):
                    v = Q[:, "xyz".index(n.op)]
                elif n.op == "const":
                    v = np.full(len(Q), n.value)
                elif n.op == "grid":
                    v = n.value.sample(np.stack([memo[id(a)] for a in n.args], 1))
                elif n.op in _NP1:
                    v = _NP1[n.op](memo[id(n.args[0])])
                else:
                    v = _NP2[n.op](memo[id(n.args[0])], memo[id(n.args[1])])
                memo[id(n)] = v
            out[s:s + step] = memo[id(root)]
    return out


def evaluate(root: Expr, points, grad: bool = False) -> np.ndarray:
    """Values (n,), or values and gradients (n, 4), at points (n, 3)."""
    P = np.ascontiguousarray(np.asarray(points, dtype=np.float64).reshape(-1, 3))
    fg = _fidget()
    if fg is not None and not _has_grid(root):
        raw = fg.eval(_ops(root), P.astype(np.float32).tobytes(), grad)
        return np.frombuffer(raw, np.float32).astype(np.float64).reshape((-1, 4) if grad else (-1,))
    if fg is None:
        _note_numpy("the tule_fidget module is not installed")
    v = _np_eval(root, P)
    if not grad:
        return v
    e = 1e-4 * float(np.max(np.abs(P), initial=1.0) or 1.0)
    g = [(_np_eval(root, P + e * d) - _np_eval(root, P - e * d)) / (2 * e) for d in np.eye(3)]
    return np.stack([v, *g], 1)


# ---------------------------------------------------------------- sampled fields (from meshes)


class GridField:
    """Signed distances sampled on a regular grid; trilinear between samples, and outside the grid
    the nearest border value plus the distance to the grid (still positive, still about a distance)."""

    def __init__(self, values: np.ndarray, origin, cell: float):
        self.values = np.asarray(values, dtype=np.float32)
        self.origin = np.asarray(origin, dtype=np.float64)
        self.cell = float(cell)

    def sample(self, P: np.ndarray) -> np.ndarray:
        from scipy.ndimage import map_coordinates

        u = (P - self.origin) / self.cell
        hi = np.array(self.values.shape) - 1
        c = np.clip(u, 0, hi)
        v = map_coordinates(self.values, c.T, order=1, mode="nearest")
        return v + np.linalg.norm((u - c) * self.cell, axis=1)


def _inside(P: np.ndarray, T: np.ndarray, xs: np.ndarray, ys: np.ndarray, zs: np.ndarray) -> np.ndarray:
    """Inside a closed mesh at every grid point (len(xs), len(ys), len(zs)): rays along z through each
    (x, y) column, crossings counted below each z."""
    nx, ny = len(xs), len(ys)
    A, B, C = P[T[:, 0]], P[T[:, 1]], P[T[:, 2]]
    lo, hi = np.minimum(np.minimum(A, B), C), np.maximum(np.maximum(A, B), C)
    i0, i1 = np.searchsorted(xs, lo[:, 0]), np.searchsorted(xs, hi[:, 0], "right")
    j0, j1 = np.searchsorted(ys, lo[:, 1]), np.searchsorted(ys, hi[:, 1], "right")
    ni, nj = np.maximum(i1 - i0, 0), np.maximum(j1 - j0, 0)
    cnt = ni * nj
    t = np.repeat(np.arange(len(T)), cnt)
    off = np.arange(len(t)) - np.repeat(np.cumsum(cnt) - cnt, cnt)
    ii = i0[t] + off // np.maximum(nj[t], 1)
    jj = j0[t] + off % np.maximum(nj[t], 1)
    px, py = xs[ii], ys[jj]
    a, b, c = A[t], B[t], C[t]
    d = (b[:, 0] - a[:, 0]) * (c[:, 1] - a[:, 1]) - (c[:, 0] - a[:, 0]) * (b[:, 1] - a[:, 1])
    with np.errstate(all="ignore"):
        w1 = ((px - a[:, 0]) * (c[:, 1] - a[:, 1]) - (c[:, 0] - a[:, 0]) * (py - a[:, 1])) / d
        w2 = ((b[:, 0] - a[:, 0]) * (py - a[:, 1]) - (px - a[:, 0]) * (b[:, 1] - a[:, 1])) / d
    hit = (d != 0) & (w1 >= 0) & (w2 >= 0) & (w1 + w2 <= 1)
    zh = (a[:, 2] + w1 * (b[:, 2] - a[:, 2]) + w2 * (c[:, 2] - a[:, 2]))[hit]
    col = (ii * ny + jj)[hit]
    order = np.lexsort((zh, col))
    col, zh = col[order], zh[order]
    starts = np.searchsorted(col, np.arange(nx * ny + 1))
    inside = np.zeros((nx * ny, len(zs)), dtype=bool)
    for k, zk in enumerate(zs):  # crossings below zk in each column: parity
        below = np.zeros(len(col), dtype=np.int64)
        below[zh < zk] = 1
        cs = np.concatenate([[0], np.cumsum(below)])
        inside[:, k] = ((cs[starts[1:]] - cs[starts[:-1]]) % 2) == 1
    return inside.reshape(nx, ny, len(zs))


def _point_tri_dist(p, a, b, c):
    """Distances from points to triangles (row by row), exact (Ericson's closest point)."""
    ab, ac, ap = b - a, c - a, p - a
    d1, d2 = np.einsum("ij,ij->i", ab, ap), np.einsum("ij,ij->i", ac, ap)
    bp = p - b
    d3, d4 = np.einsum("ij,ij->i", ab, bp), np.einsum("ij,ij->i", ac, bp)
    cp = p - c
    d5, d6 = np.einsum("ij,ij->i", ab, cp), np.einsum("ij,ij->i", ac, cp)
    va, vb, vc = d3 * d6 - d5 * d4, d5 * d2 - d1 * d6, d1 * d4 - d3 * d2
    with np.errstate(all="ignore"):
        den = va + vb + vc
        v, w = vb / den, vc / den
        q = a + ab * v[:, None] + ac * w[:, None]  # inside the face
        t = d1 / (d1 - d3)
        q = np.where(((vc <= 0) & (d1 >= 0) & (d3 <= 0))[:, None], a + ab * t[:, None], q)
        t = d2 / (d2 - d6)
        q = np.where(((vb <= 0) & (d2 >= 0) & (d6 <= 0))[:, None], a + ac * t[:, None], q)
        t = (d4 - d3) / ((d4 - d3) + (d5 - d6))
        q = np.where(((va <= 0) & (d4 - d3 >= 0) & (d5 - d6 >= 0))[:, None], b + (c - b) * t[:, None], q)
    q = np.where(((d1 <= 0) & (d2 <= 0))[:, None], a, q)
    q = np.where(((d3 >= 0) & (d4 <= d3))[:, None], b, q)
    q = np.where(((d6 >= 0) & (d5 <= d6))[:, None], c, q)
    q = np.nan_to_num(q, nan=0.0)
    return np.linalg.norm(p - q, axis=1)


MAX_GRID = 40_000_000


def from_mesh(mesh, cell: float = 0.5, band: float | None = None) -> "Implicit":
    """A closed mesh as a signed-distance implicit sampled every ``cell`` mm (exact within a few
    cells of the surface, approximate further out). Loses detail under a cell, regions and tags."""
    from scipy.spatial import cKDTree

    if not mesh.is_closed:
        raise TuleError("only a closed mesh has an inside", op="to_implicit", hint="mesh.is_closed; mesh Booleans repair small gaps")
    P, T, _ = mesh._weld_arrays()
    cell = float(cell)
    pad = 3 * cell + (band or 0)
    lo, hi = P.min(0) - pad, P.max(0) + pad
    dims = np.ceil((hi - lo) / cell).astype(int) + 1
    if np.prod(dims) > MAX_GRID:
        need = cell * (np.prod(dims) / MAX_GRID) ** (1 / 3)
        raise TuleError(f"a {dims[0]}×{dims[1]}×{dims[2]} grid is too large", op="to_implicit",
                        hint=f"a larger cell (about {need:.2g} mm) or a smaller part")
    jit = 7.3e-4 * cell  # rays a hair off round coordinates: never through a vertex or along an edge
    xs, ys, zs = (lo[i] + np.arange(dims[i]) * cell + jit * (i + 1) for i in range(3))
    inside = _inside(P, T, xs, ys, zs)
    # distances: to samples on the triangles (about cell / 3 apart), exact near the surface
    A, B, C = P[T[:, 0]], P[T[:, 1]], P[T[:, 2]]
    L = np.max([np.linalg.norm(B - A, axis=1), np.linalg.norm(C - B, axis=1), np.linalg.norm(A - C, axis=1)], axis=0)
    n = np.clip(np.ceil(L / (cell / 3)), 1, 50).astype(int)
    pts, owner = [], []
    for k in np.unique(n):
        sel = np.flatnonzero(n == k)
        u, v = np.meshgrid(np.arange(k + 1), np.arange(k + 1), indexing="ij")
        keep = u + v <= k
        u, v = u[keep] / k, v[keep] / k
        q = A[sel, None] + (B - A)[sel, None] * u[None, :, None] + (C - A)[sel, None] * v[None, :, None]
        pts.append(q.reshape(-1, 3))
        owner.append(np.repeat(sel, len(u)))
    pts, owner = np.concatenate(pts), np.concatenate(owner)
    tree = cKDTree(pts)
    G = np.stack(np.meshgrid(xs, ys, zs, indexing="ij"), -1).reshape(-1, 3)
    d, _ = tree.query(G, k=1, workers=-1)
    near = np.flatnonzero(d < 2.5 * cell)
    if len(near):
        _, idx = tree.query(G[near], k=12, workers=-1)
        cand = owner[idx]  # (k, 12) candidate triangles
        best = np.full(len(near), np.inf)
        for j in range(cand.shape[1]):
            t = cand[:, j]
            best = np.minimum(best, _point_tri_dist(G[near], A[t], B[t], C[t]))
        d[near] = best
    vals = np.where(inside.reshape(-1), -d, d).reshape(tuple(dims))
    gf = GridField(vals, lo + jit * np.array([1, 2, 3]), cell)
    e = Expr("grid", (_X, _Y, _Z), gf)
    hist = mesh.history + (f"sampled as a signed distance every {cell:g} mm (detail under a cell, regions and tags lost)",)
    return Implicit(e, (Vec(*P.min(0)), Vec(*P.max(0))), name=mesh.name, color=mesh.color, history=hist)


# ---------------------------------------------------------------- the Implicit


Bounds = "tuple[Vec, Vec] | None"


def _bounds(b):
    if b is None:
        return None
    lo, hi = b
    lo, hi = vec(lo), vec(hi)
    if any(h < l for l, h in zip(lo, hi)):
        raise TuleError(f"bounds {tuple(lo)} – {tuple(hi)}: lo must be below hi", op="sdf")
    return (lo, hi)


def _grow(b, d: float):
    if b is None:
        return None
    lo, hi = b
    return (lo - Vec(d, d, d), hi + Vec(d, d, d)) if d > 0 else b


class Implicit(Looks):
    """An implicit body: a signed-distance expression (negative inside), its bounds, name, color,
    tags (as predicates applied when meshed) and history. ``kind == "implicit"``."""

    kind = "implicit"

    def __init__(self, expr, bounds=None, *, name: str | None = None, color: str | None = None,
                 tags: dict | None = None, history: Sequence[str] = (), material=None):
        self.expr = _e(expr)
        self.bounds = _bounds(bounds)
        self.name = name
        self.color = with_look(None, color, material) if material is not None else color
        self._tags: dict[str, Callable] = dict(tags or {})
        self.history = tuple(history)

    def _with(self, expr, bounds, history=None, tags=None) -> "Implicit":
        return Implicit(expr, bounds, name=self.name, color=self.color, tags=self._tags if tags is None else tags,
                        history=self.history if history is None else history)

    def __repr__(self) -> str:
        n = f" {self.name!r}" if self.name else ""
        b = (f"{tuple(round(v, 3) for v in self.bounds[0])} to {tuple(round(v, 3) for v in self.bounds[1])}"
             if self.bounds else "unbounded")
        g = ", sampled" if _has_grid(self.expr) else ""
        hist = f" ({'; '.join(self.history[-2:])})" if self.history else ""
        return f"<Implicit{n}: {_count(self.expr)} nodes{g}, {b}{hist}>"

    # names, tags
    def named(self, name: str, color: str | None = None, material=None) -> "Implicit":
        out = self._with(self.expr, self.bounds)
        out.name, out.color = name, with_look(self.color, color, material)
        return out

    def _looking(self, color) -> "Implicit":
        out = self._with(self.expr, self.bounds)
        out.color = color
        return out

    def colored(self, color: str) -> "Implicit":
        return self.named(self.name, color)

    @property
    def tags(self) -> list[str]:
        return sorted(self._tags)

    def tag(self, name: str, select: Callable) -> "Implicit":
        """A tag for the mesh this becomes: ``select(mesh.faces)`` picks the triangles when meshed,
        e.g. ``body.tag("mount", lambda f: f.bottom())``. Predicates see the mesh where it ends up."""
        if not callable(select):
            raise TuleError("an implicit has no faces yet: tag it with a function of the mesh's faces", op="tag",
                            hint='body.tag("mount", lambda f: f.bottom()) or lambda f: f.within(lo, hi)')
        tags = dict(self._tags)
        tags[name] = select
        return self._with(self.expr, self.bounds, tags=tags)

    # evaluation
    def value(self, points) -> np.ndarray:
        """Signed distance at points (n, 3): negative inside."""
        return evaluate(self.expr, points)

    def gradient(self, points) -> np.ndarray:
        """The field's gradient (n, 3) at points (the outward normal on the surface)."""
        return evaluate(self.expr, points, grad=True)[:, 1:]

    def contains(self, points) -> np.ndarray:
        return self.value(points) < 0

    # booleans
    def _other(self, o, op) -> "Implicit":
        if not isinstance(o, Implicit):
            from .mesh import _mixed

            raise _mixed(self, o, op)
        return o

    def __or__(self, o):
        return union(self, self._other(o, "union"))

    __add__ = __or__

    def __sub__(self, o):
        return difference(self, self._other(o, "difference"))

    def __and__(self, o):
        return intersection(self, self._other(o, "intersection"))

    def union(self, *others, smooth: float = 0) -> "Implicit":
        return union(self, *others, smooth=smooth)

    def difference(self, *others, smooth: float = 0) -> "Implicit":
        return difference(self, *others, smooth=smooth)

    def intersection(self, *others, smooth: float = 0) -> "Implicit":
        return intersection(self, *others, smooth=smooth)

    # modifiers
    def offset(self, d: float) -> "Implicit":
        """Grown by ``d`` mm (shrunk if negative): rounds outside corners when growing."""
        return self._with(self.expr - d, _grow(self.bounds, d), self.history)

    def shell(self, thickness: float, inward: bool = True) -> "Implicit":
        """A wall ``thickness`` thick along the surface, inside it (or outside: ``inward=False``)."""
        t = float(thickness)
        e = max(self.expr, -(self.expr + t)) if inward else max(self.expr - t, -self.expr)
        return self._with(e, self.bounds if inward else _grow(self.bounds, t))

    def sheet(self, thickness: float) -> "Implicit":
        """The surface thickened to ``thickness`` mm, half each side (a TPMS sheet lattice)."""
        return self._with(abs(self.expr) - float(thickness) / 2, _grow(self.bounds, float(thickness) / 2))

    def clip(self, lo: VecLike, hi: VecLike) -> "Implicit":
        """Only what is inside the box ``lo``–``hi`` (bounds an unbounded field)."""
        b = box_between(lo, hi)
        return intersection(self, b)

    # transforms
    def _affine(self, A: np.ndarray, t: np.ndarray, scale: float = 1.0) -> "Implicit":
        """Moved by p -> A p + t (A orthogonal times ``scale``): the field at q is f(A⁻¹(q - t)) · scale."""
        Ai = np.linalg.inv(A)
        b = -Ai @ t
        X, Y, Z = (sum((float(Ai[r, c]) * v for c, v in enumerate((_X, _Y, _Z)) if Ai[r, c] != 0), Expr("const", (), 0.0))
                   + float(b[r]) for r in range(3))
        e = _remap(self.expr, X, Y, Z)
        if scale != 1:
            e = e * scale
        bounds = None
        if self.bounds is not None:
            lo, hi = np.array(self.bounds[0]), np.array(self.bounds[1])
            corners = np.array([[(lo, hi)[i][0], (lo, hi)[j][1], (lo, hi)[k][2]] for i in (0, 1) for j in (0, 1) for k in (0, 1)])
            q = corners @ A.T + t
            bounds = (Vec(*q.min(0)), Vec(*q.max(0)))
        return self._with(e, bounds)

    def at(self, x: float | VecLike = 0, y: float = 0, z: float = 0) -> "Implicit":
        v = vec(x) if not isinstance(x, (int, float)) else Vec(x, y, z)
        return self._affine(np.eye(3), np.array(tuple(v), dtype=float))

    translate = at

    def rotate(self, x: float = 0, y: float = 0, z: float = 0, about: VecLike = (0, 0, 0)) -> "Implicit":
        """Rotate by degrees about x, then y, then z (as ``Shape.rotate``)."""
        from .mesh import _axis_matrix

        R = np.eye(3)
        for ax, deg in ((0, x), (1, y), (2, z)):
            if deg:
                R = _axis_matrix(np.eye(3)[ax], deg) @ R
        c = np.array(vec(about), dtype=float)
        return self._affine(R, c - R @ c)

    def rotate_about(self, axis_dir: VecLike, degrees: float, origin: VecLike = (0, 0, 0)) -> "Implicit":
        from .mesh import _axis_matrix

        R = _axis_matrix(np.array(vec(axis_dir)), degrees)
        c = np.array(vec(origin), dtype=float)
        return self._affine(R, c - R @ c)

    def scale(self, s: float, about: VecLike = (0, 0, 0)) -> "Implicit":
        """Uniform scale (distances stay distances)."""
        c = np.array(vec(about), dtype=float)
        return self._affine(np.eye(3) * s, c - s * c, scale=float(s))

    def placed(self, transforms=None, *, at=None, rotate=None):
        """This part placed many times, stored and drawn once (``tule.Instances``): ``(N, 4, 4)``
        matrices, or ``at=`` (N, 3) points and ``rotate=`` (N, 3) degrees (x, then y, then z; one row for all)."""
        from .instances import Instances

        return Instances(self, transforms, at=at, rotate=rotate)

    def mirror(self, nx: float | VecLike = 1, ny: float = 0, nz: float = 0, origin: VecLike = (0, 0, 0)) -> "Implicit":
        n = np.array(vec(nx) if not isinstance(nx, (int, float)) else (nx, ny, nz), dtype=float)
        n /= np.linalg.norm(n)
        R = np.eye(3) - 2 * np.outer(n, n)
        c = np.array(vec(origin), dtype=float)
        return self._affine(R, c - R @ c)

    # meshing
    def to_mesh(self, cell: float | None = None, bounds=None, fine=None, rest: "Implicit | None" = None) -> "object":
        """This body as a closed ``tule.Mesh`` (Fidget: JIT evaluation and dual contouring, sharp edges
        kept). ``cell`` (mm) is the smallest octree cell: detail under it is lost (default: a 128th of
        the largest size). ``bounds`` (lo, hi) clips it; an unbounded body (a bare lattice) needs them.

        ``fine=[(lo, hi, cell), …]``: boxes meshed at their own (smaller) cell, the rest at ``cell``
        (banded meshing: 1 mm channels at a cutaway plane of a 3 m part). Each box and the rest are
        closed pieces that meet face to face; a region too large for one octree is split the same way.
        ``rest``: another body meshed outside the fine boxes instead of this one (the same part without its
        hidden detail, e.g. the wall without its cooling channels, so the coarse cells don't chew on them)."""
        from .mesh import Mesh

        b = _bounds(bounds) if bounds is not None else self.bounds
        if b is None:
            raise TuleError("this body is unbounded (a lattice or a plane on its own)", op="to_mesh",
                            hint="intersect it with a bounded body (body & sdf.gyroid(8, 1)) or give bounds=((x0, y0, z0), (x1, y1, z1))")
        lo, hi = np.array(b[0], dtype=float), np.array(b[1], dtype=float)
        size = float((hi - lo).max())
        cell = float(cell) if cell else size / 128
        if cell <= 0:
            raise TuleError("cell must be > 0 mm", op="to_mesh")
        e = self.expr
        if rest is not None and not fine:
            raise TuleError("rest= is what is meshed outside the fine= boxes: give fine= too", op="to_mesh")
        closing = None
        if bounds is not None:  # clipped to the bounds asked for (a bare lattice, or a piece of a body)
            closing = box_between(*b).expr
        else:  # a surface that runs past the body's own bounds is closed just outside them, not left open
            g = 1.5 * cell
            closing = box_between(tuple(lo - g), tuple(hi + g)).expr
        e = max(e, closing)
        e_rest = e if rest is None else max(self._other(rest, "to_mesh rest").expr, closing)
        fg = _fidget()
        sampled = fg is None or _has_grid(e)
        pieces = []  # (expr, lo, hi, cell)
        taken = None
        for f in fine or ():
            try:
                flo, fhi, fc = np.array(vec(f[0]), dtype=float), np.array(vec(f[1]), dtype=float), float(f[2])
            except (TypeError, IndexError, ValueError):
                raise TuleError("fine: a list of (lo, hi, cell) boxes", op="to_mesh",
                                hint="fine=[((-2000, -15, -100), (2000, 15, 3500), 0.6)]") from None
            flo, fhi = np.maximum(flo, lo), np.minimum(fhi, hi)
            if np.any(fhi <= flo) or fc <= 0:
                continue
            fb = box_between(tuple(flo), tuple(fhi)).expr
            pe = max(e, fb) if taken is None else max(e, fb, -taken)
            pieces.append((pe, flo, fhi, fc))
            taken = fb if taken is None else min(taken, fb)
        pieces.append((e_rest if taken is None else max(e_rest, -taken), lo, hi, cell))
        chunks = []
        for pe, plo, phi, pc in pieces:
            # an octree is at most 2^12 cells across (or 2.5e8 samples for marching cubes): split bigger regions
            limit = 600.0 if sampled else 3800.0
            n = np.maximum(1, np.ceil((phi - plo) / (pc * limit))).astype(int)
            if (n > 1).any() and (not fine and bounds is None and n.prod() > 64 or n.prod() > 512):
                raise TuleError(f"cells of {pc:g} mm across {float((phi - plo).max()):.4g} mm are too many", op="to_mesh",
                                hint=f"a larger cell (≥ {float((phi - plo).max()) / 4000:.2g} mm), or fine= boxes only where the detail is")
            edges = [np.linspace(plo[i], phi[i], n[i] + 1) for i in range(3)]
            for i in range(n[0]):
                for j in range(n[1]):
                    for k in range(n[2]):
                        clo = np.array([edges[0][i], edges[1][j], edges[2][k]])
                        chi = np.array([edges[0][i + 1], edges[1][j + 1], edges[2][k + 1]])
                        ce = pe if n.prod() == 1 else max(pe, box_between(tuple(clo), tuple(chi)).expr)
                        chunks.append((ce, clo, chi, pc))
        Ps, Ts, hows, off = [], [], [], 0
        for ce, clo, chi, cc in chunks:
            got = self._mesh_piece(ce, clo, chi, cc, fg)
            if got is None:
                continue
            P, T, how = got
            Ps.append(P)
            Ts.append(T + off)
            off += len(P)
            hows.append(how)
        if not Ps:
            raise TuleError("the body has no surface inside its bounds", op="to_mesh",
                            hint="is it empty, or all inside? check body.value(points); a finer cell for thin walls")
        m = Mesh(np.vstack(Ps), np.vstack(Ts), name=self.name, color=self.color, welded=True)
        how = hows[0] if len(hows) == 1 else (f"meshed in {len(hows)} closed pieces (cells "
                                              f"{', '.join(sorted({f'{c[3]:g}' for c in chunks}, key=float))} mm), {hows[-1].split(' (')[0]}")
        m.history = self.history + (how,)
        for tag, fn in self._tags.items():
            try:
                m = m.tag(tag, fn)
            except TuleError as err:
                raise TuleError(f"tag {tag!r} selects nothing on the mesh", op="to_mesh",
                                hint="check the predicate against mesh.faces (bounds, tol=)") from err
        return m

    def _mesh_piece(self, e, lo, hi, cell, fg):
        """(points, triangles, how) of the closed surface of ``e`` in the box lo–hi, or None if empty there."""
        from .mesh import Mesh

        center = (lo + hi) / 2
        half = float((hi - lo).max()) / 2 + 2 * cell
        depth = builtins.max(1, int(math.ceil(math.log2(2 * half / cell))))
        if fg is not None and not _has_grid(e):
            if depth > 12:
                raise TuleError(f"cells of {cell:g} mm across {2 * half:.4g} mm are too many", op="to_mesh")
            vb, tb = fg.mesh(_ops(e), [float(c) for c in center], float(half), depth)
            P = np.frombuffer(vb, np.float32).reshape(-1, 3).astype(np.float64)
            T = np.frombuffer(tb, np.uint32).reshape(-1, 3).astype(np.int64)
            got = 2 * half / 2 ** depth
            how = f"meshed by Fidget (dual contouring, cells {got:.3g} mm: detail under a cell lost)"
        else:
            if fg is None:
                _note_numpy("the tule_fidget module is not installed")
            P, T = _marching(e, lo - 2 * cell, hi + 2 * cell, cell)
            how = f"meshed by marching cubes (sampled field, cells {cell:.3g} mm: detail under a cell lost, edges rounded)"
        if not len(T):
            return None
        t = P[T]
        if np.einsum("ij,ij->i", t[:, 0], np.cross(t[:, 1], t[:, 2])).sum() < 0:
            T = T[:, ::-1]
        if fg is not None and not _has_grid(e):
            m = _clean(e, Mesh(P, T, welded=True), cell)
            P, T = m._P, m._T
        return P, T, how


def _clean(e: Expr, m, cell: float):
    """Dual contouring's leftovers fixed: coincident vertices welded, and the small
    flaps folded back over their neighbours (normal against the field's gradient) untangled: their
    vertices moved to their neighbours' mean, then back onto the surface along the gradient."""
    from scipy.sparse import coo_matrix

    from .mesh import Mesh, tri_normals, weld

    # coincident vertices merged and the triangles that collapse with them dropped (not manifold3d's
    # simplify: even at a tiny tolerance it collapses edges along a thin rim and bites into it)
    P, T, _ = weld(m._P, m._T, 1e-6 * cell)
    P = P.copy()
    # back-to-back triangle pairs on the same three vertices (a flat flap hanging off an edge): they
    # enclose nothing, so dropping both leaves the solid as it was
    key = np.sort(T, axis=1)
    _, inv, cnt = np.unique(key, axis=0, return_inverse=True, return_counts=True)
    T = T[cnt[inv.reshape(-1)] == 1]
    used, T = np.unique(T, return_inverse=True)
    P, T = P[used], T.reshape(-1, 3)
    n = len(P)
    i = np.concatenate([T[:, 0], T[:, 1], T[:, 2], T[:, 1], T[:, 2], T[:, 0]])
    j = np.concatenate([T[:, 1], T[:, 2], T[:, 0], T[:, 0], T[:, 1], T[:, 2]])
    W = coo_matrix((np.ones(len(i)), (i, j)), shape=(n, n)).tocsr()
    W.data[:] = 1.0
    deg = np.maximum(np.asarray(W.sum(1)).ravel(), 1)
    def unit(g):
        return g / np.maximum(np.linalg.norm(g, axis=1, keepdims=True), 1e-12)

    for _ in range(8):
        N, A = tri_normals(P, T)
        g = evaluate(e, P[T].mean(1), grad=True)[:, 1:]
        gv = unit(evaluate(e, P, grad=True)[:, 1:])
        # a vertex within a cell of a sharp edge (a chine, a trailing edge) stays where Fidget put it:
        # the gradient turns there, and averaging would round the edge off. Six probes a cell away.
        smooth = np.ones(len(P), bool)
        for d in np.vstack([np.eye(3), -np.eye(3)]) * cell:
            smooth &= np.einsum("ij,ij->i", gv, unit(evaluate(e, P + d, grad=True)[:, 1:])) > 0.9
        flat = smooth[T].all(1)
        # a flap: its normal against the gradient at its middle and at every corner (inside a knife
        # edge the middle alone can read the far side)
        against = (np.einsum("ij,ij->i", N, g) < 0) & (np.einsum("tkj,tj->tk", gv[T], N) < 0).all(1)
        bad = against & (A > 1e-12 * cell * cell) & flat
        if not bad.any():
            break
        v = np.unique(T[bad])
        v = np.unique(np.concatenate([v, W[v].indices]))  # and their neighbours
        v = v[smooth[v]]
        P[v] = (W[v] @ P) / deg[v][:, None]
        for _ in range(3):  # Newton steps back to the surface, at most half a cell each
            fg = evaluate(e, P[v], grad=True)
            gg = fg[:, 1:]
            g2 = (gg * gg).sum(1)
            step = np.nan_to_num((fg[:, 0] / np.maximum(g2, 1e-12))[:, None] * gg, nan=0.0, posinf=0.0, neginf=0.0)
            ln = np.linalg.norm(step, axis=1)
            step[g2 < 0.04] = 0.0
            step *= np.minimum(1.0, 0.5 * cell / np.maximum(ln, 1e-12))[:, None]
            P[v] -= step
    return Mesh(P, T, name=m.name, color=m.color, welded=True)


def _marching(e: Expr, lo, hi, cell: float):
    """A mesh by marching cubes on samples every ``cell`` (for sampled fields, or without Fidget)."""
    import warnings

    from skimage.measure import marching_cubes

    dims = np.ceil((hi - lo) / cell).astype(int) + 1
    axes = [lo[i] + np.arange(dims[i]) * cell for i in range(3)]
    G = np.stack(np.meshgrid(*axes, indexing="ij"), -1).reshape(-1, 3)
    v = evaluate(e, G).reshape(tuple(dims))
    v = np.pad(v, 1, constant_values=np.abs(v).max() + cell)  # closed at the border
    if v.min() >= 0 or v.max() <= 0:
        return np.zeros((0, 3)), np.zeros((0, 3), np.int64)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        verts, tris, _, _ = marching_cubes(v.astype(np.float32), 0.0, allow_degenerate=False)
    return lo + (verts - 1) * cell, tris.astype(np.int64)


# ---------------------------------------------------------------- booleans


def _bodies(items) -> list[Implicit]:
    out = []
    for b in items:
        if isinstance(b, (list, tuple)):
            out.extend(_bodies(b))
        elif isinstance(b, Implicit):
            out.append(b)
        else:
            from .mesh import _mixed

            raise _mixed(Implicit(0.0), b, "combine")
    if not out:
        raise TuleError("no bodies given", op="sdf")
    return out


def _smin(a: Expr, b: Expr, k: float) -> Expr:
    """Polynomial smooth minimum: blends over about ``k`` mm."""
    if k <= 0:
        return min(a, b)
    h = max(k - abs(a - b), 0.0) / k
    return min(a, b) - h * h * (k * 0.25)


def _join_tags(bodies):
    tags = {}
    for b in bodies:
        tags.update(b._tags)
    return tags


def _box_union(bs):
    if any(b is None for b in bs):
        return None
    return (Vec(*np.min([tuple(b[0]) for b in bs], 0)), Vec(*np.max([tuple(b[1]) for b in bs], 0)))


def _box_meet(bs):
    known = [b for b in bs if b is not None]
    if not known:
        return None
    lo, hi = np.max([tuple(b[0]) for b in known], 0), np.min([tuple(b[1]) for b in known], 0)
    return (Vec(*lo), Vec(*np.maximum(hi, lo)))


def union(*bodies, smooth: float = 0) -> Implicit:
    """All of them; ``smooth`` blends where they meet over about that many mm (a fillet-like seam)."""
    bs = _bodies(bodies)
    e = bs[0].expr
    for b in bs[1:]:
        e = _smin(e, b.expr, float(smooth))
    bounds = _grow(_box_union([b.bounds for b in bs]), float(smooth) / 2)
    hist = bs[0].history + ((f"smooth union ({smooth:g} mm)",) if smooth else ())
    return Implicit(e, bounds, name=bs[0].name, color=bs[0].color, tags=_join_tags(bs), history=hist)


def intersection(*bodies, smooth: float = 0) -> Implicit:
    """Where all of them are; ``smooth`` rounds the seams."""
    bs = _bodies(bodies)
    e = bs[0].expr
    for b in bs[1:]:
        e = -_smin(-e, -b.expr, float(smooth))
    out = Implicit(e, _box_meet([b.bounds for b in bs]), name=bs[0].name, color=bs[0].color, tags=_join_tags(bs),
                   history=bs[0].history)
    return out


def difference(first, *others, smooth: float = 0) -> Implicit:
    """``first`` without the others; ``smooth`` rounds the cut edges."""
    bs = _bodies([first, *others])
    e = bs[0].expr
    for b in bs[1:]:
        e = -_smin(-e, b.expr, float(smooth))
    return Implicit(e, bs[0].bounds, name=bs[0].name, color=bs[0].color, tags=_join_tags(bs), history=bs[0].history)


# ---------------------------------------------------------------- primitives


def _pos(op: str, **sizes):
    for k, v in sizes.items():
        if not (isinstance(v, (int, float)) and v > 0):
            raise TuleError(f"sdf.{op}: {k} must be > 0, got {v!r}", op=f"sdf.{op}")


def _box_expr(c, h) -> Expr:
    qx, qy, qz = abs(_X - c[0]) - h[0], abs(_Y - c[1]) - h[1], abs(_Z - c[2]) - h[2]
    outside = length(max(qx, 0.0), max(qy, 0.0), max(qz, 0.0))
    return outside + min(max(qx, qy, qz), 0.0)


def box(x: float = 1, y: float | None = None, z: float | None = None, center: bool = False) -> Implicit:
    """A box from the origin to (x, y, z) (``center=True``: centered on it), as ``tule.box``."""
    y = x if y is None else y
    z = x if z is None else z
    _pos("box", x=x, y=y, z=z)
    h = np.array([x, y, z], dtype=float) / 2
    c = np.zeros(3) if center else h
    return Implicit(_box_expr(c, h), (Vec(*(c - h)), Vec(*(c + h))))


def box_between(lo: VecLike, hi: VecLike) -> Implicit:
    lo, hi = np.array(vec(lo), dtype=float), np.array(vec(hi), dtype=float)
    return Implicit(_box_expr((lo + hi) / 2, (hi - lo) / 2), (Vec(*lo), Vec(*hi)))


def cube(size: float = 1, center: bool = False) -> Implicit:
    return box(size, size, size, center)


def sphere(r: float = 1) -> Implicit:
    """A sphere of radius ``r`` at the origin."""
    _pos("sphere", r=r)
    return Implicit(length(_X, _Y, _Z) - r, (Vec(-r, -r, -r), Vec(r, r, r)))


def cylinder(r: float = 1, h: float = 1, center: bool = False) -> Implicit:
    """A cylinder along z from z = 0 to ``h`` (``center=True``: centered), as ``tule.cylinder``."""
    _pos("cylinder", r=r, h=h)
    zc = 0.0 if center else h / 2
    dr, dz = length(_X, _Y) - r, abs(_Z - zc) - h / 2
    e = length(max(dr, 0.0), max(dz, 0.0)) + min(max(dr, dz), 0.0)
    return Implicit(e, (Vec(-r, -r, zc - h / 2), Vec(r, r, zc + h / 2)))


def capsule(a: VecLike, b: VecLike, r: float) -> Implicit:
    """A rod of radius ``r`` from ``a`` to ``b`` with round ends (struts, ribs)."""
    _pos("capsule", r=r)
    a, b = np.array(vec(a), dtype=float), np.array(vec(b), dtype=float)
    ab = b - a
    L2 = float(ab @ ab) or 1e-300
    px, py, pz = _X - a[0], _Y - a[1], _Z - a[2]
    t = clamp((px * ab[0] + py * ab[1] + pz * ab[2]) / L2, 0.0, 1.0)
    e = length(px - t * ab[0], py - t * ab[1], pz - t * ab[2]) - r
    return Implicit(e, (Vec(*(np.minimum(a, b) - r)), Vec(*(np.maximum(a, b) + r))))


def torus(R: float = 1, r: float = 0.25) -> Implicit:
    """A torus about z: ring radius ``R``, tube radius ``r``."""
    _pos("torus", R=R, r=r)
    return Implicit(length(length(_X, _Y) - R, _Z) - r, (Vec(-R - r, -R - r, -r), Vec(R + r, R + r, r)))


def plane(normal: VecLike = (0, 0, 1), point: VecLike = (0, 0, 0)) -> Implicit:
    """The half-space behind a plane (``normal`` points out of it). Unbounded: intersect it."""
    n = np.array(vec(normal), dtype=float)
    n /= np.linalg.norm(n)
    p = np.array(vec(point), dtype=float)
    return Implicit((_X - p[0]) * n[0] + (_Y - p[1]) * n[1] + (_Z - p[2]) * n[2])


# TPMS: the field over (2π / period) and its mean gradient on the surface, so values are about mm
_TPMS = {
    "gyroid": (lambda X, Y, Z: sin(X) * cos(Y) + sin(Y) * cos(Z) + sin(Z) * cos(X), 1.53),
    "schwarz_p": (lambda X, Y, Z: cos(X) + cos(Y) + cos(Z), 1.31),
    "diamond": (lambda X, Y, Z: sin(X) * sin(Y) * sin(Z) + sin(X) * cos(Y) * cos(Z) + cos(X) * sin(Y) * cos(Z)
                + cos(X) * cos(Y) * sin(Z), 1.49),
}


def _tpms(kind: str, period: float, thickness: float | None) -> Implicit:
    _pos(kind, period=period)
    f, grad = _TPMS[kind]
    k = 2 * math.pi / period
    e = f(_X * k, _Y * k, _Z * k) / (k * grad)  # about a distance near the surface
    body = Implicit(e)
    if thickness is not None:
        _pos(kind, thickness=thickness)
        body = body.sheet(thickness)
        body.bounds = None
    return body


def gyroid(period: float, thickness: float | None = None) -> Implicit:
    """A gyroid lattice with cells ``period`` mm across. With ``thickness``: sheets that thick
    (mm, about); without: the solid on one side of the surface. Unbounded: ``body & gyroid(…)``."""
    return _tpms("gyroid", period, thickness)


def schwarz_p(period: float, thickness: float | None = None) -> Implicit:
    """Schwarz P (primitive) lattice; as ``gyroid``."""
    return _tpms("schwarz_p", period, thickness)


def diamond(period: float, thickness: float | None = None) -> Implicit:
    """Schwarz D (diamond) lattice; as ``gyroid``."""
    return _tpms("diamond", period, thickness)


def field(fn: Callable, bounds=None) -> Implicit:
    """Any body written as math: ``fn(x, y, z)`` with ``sdf.sin``, ``sdf.sqrt``, ``sdf.min``, …
    returns the field (negative inside; a true distance blends and offsets best).
    ``bounds=((x0, y0, z0), (x1, y1, z1))`` where it lives (needed to mesh it)."""
    e = fn(_X, _Y, _Z)
    if not isinstance(e, (Expr, int, float)):
        raise TuleError(f"field(fn): fn returned a {type(e).__name__}", op="sdf.field",
                        hint="use sdf.sin, sdf.sqrt, sdf.min, … on x, y, z (not math.* or numpy)")
    return Implicit(e, bounds)


# ---------------------------------------------------------------- 2D profiles, revolve, extrude


def _step(t, at_zero: float = 0.5) -> Expr:
    """1 where t > 0, 0 where t < 0, ``at_zero`` at 0 (then a ramp a micrometre wide): exact away from
    it, and a constant over any interval that does not cross it, so Fidget still prunes."""
    return clamp(_e(t) * 1e6 + at_zero, 0.0, 1.0)


class Region:
    """A 2D region in the plane: an expression over ``x`` (here: u) and ``y`` (v), negative inside,
    with its 2D bounds. ``revolve()`` turns it about the z axis with u as the radius and v as the
    height; ``extrude(h)`` sweeps it along z. Booleans, ``offset``, ``shell`` and moves work as on bodies."""

    __slots__ = ("expr", "bounds")

    def __init__(self, expr, bounds=None):
        self.expr = _e(expr)
        self.bounds = None if bounds is None else (tuple(map(float, bounds[0])), tuple(map(float, bounds[1])))

    def __repr__(self) -> str:
        b = f"{self.bounds[0]} to {self.bounds[1]}" if self.bounds else "unbounded"
        return f"<Region: {_count(self.expr)} nodes, {b}>"

    def value(self, points) -> np.ndarray:
        """Signed distance at 2D points (n, 2)."""
        P = np.asarray(points, dtype=float).reshape(-1, 2)
        return evaluate(self.expr, np.column_stack([P, np.zeros(len(P))]))

    def contains(self, points) -> np.ndarray:
        return self.value(points) < 0

    # booleans
    def _join(self, others, kind: str, smooth: float) -> "Region":
        rs = [self, *others]
        for r in rs:
            if not isinstance(r, Region):
                raise TuleError(f"a 2D region and a {type(r).__name__} cannot be combined", op=f"region.{kind}",
                                hint="revolve() or extrude() the region first, or make both 2D (sdf.polygon, sdf.rect)")
        e, k = rs[0].expr, float(smooth)
        for r in rs[1:]:
            e = _smin(e, r.expr, k) if kind == "union" else -_smin(-e, r.expr if kind == "difference" else -r.expr, k)
        bs = [r.bounds for r in rs]
        if kind == "union":
            b = None if any(v is None for v in bs) else (tuple(np.min([v[0] for v in bs], 0)), tuple(np.max([v[1] for v in bs], 0)))
        elif kind == "difference":
            b = rs[0].bounds
        else:
            known = [v for v in bs if v is not None]
            b = (tuple(np.max([v[0] for v in known], 0)), tuple(np.min([v[1] for v in known], 0))) if known else None
        return Region(e, b)

    def union(self, *others, smooth: float = 0) -> "Region":
        return self._join(others, "union", smooth)

    def intersection(self, *others, smooth: float = 0) -> "Region":
        return self._join(others, "intersection", smooth)

    def difference(self, *others, smooth: float = 0) -> "Region":
        return self._join(others, "difference", smooth)

    __or__ = __add__ = lambda self, o: self.union(o)
    __and__ = lambda self, o: self.intersection(o)
    __sub__ = lambda self, o: self.difference(o)

    def offset(self, d: float) -> "Region":
        """Grown by ``d`` mm (shrunk if negative)."""
        b = self.bounds
        return Region(self.expr - d, None if b is None else (tuple(v - max(d, 0) for v in b[0]), tuple(v + max(d, 0) for v in b[1])))

    def shell(self, thickness: float, inward: bool = True) -> "Region":
        """A band ``thickness`` wide along the outline, inside it (or outside: ``inward=False``)."""
        t = float(thickness)
        if inward:
            return Region(max(self.expr, -(self.expr + t)), self.bounds)
        return Region(max(self.expr - t, -self.expr), self.offset(t).bounds)

    def at(self, u: float = 0, v: float = 0) -> "Region":
        b = self.bounds
        return Region(_remap(self.expr, _X - u, _Y - v, _Z),
                      None if b is None else ((b[0][0] + u, b[0][1] + v), (b[1][0] + u, b[1][1] + v)))

    def rotate(self, degrees: float) -> "Region":
        """Turned counterclockwise about the origin."""
        c, s = math.cos(math.radians(degrees)), math.sin(math.radians(degrees))
        b = self.bounds
        if b is not None:
            q = np.array([[u, v] for u in (b[0][0], b[1][0]) for v in (b[0][1], b[1][1])]) @ np.array([[c, s], [-s, c]])
            b = (tuple(q.min(0)), tuple(q.max(0)))
        return Region(_remap(self.expr, c * _X + s * _Y, -s * _X + c * _Y, _Z), b)

    def mirror(self) -> "Region":
        """Mirrored across the v axis (u -> -u)."""
        b = self.bounds
        return Region(_remap(self.expr, -_X, _Y, _Z), None if b is None else ((-b[1][0], b[0][1]), (-b[0][0], b[1][1])))

    # into 3D
    def revolve(self, degrees: float = 360, start: float = 0) -> Implicit:
        """The region turned about the z axis: u is the distance from the axis, v the height (a lathe
        profile; only u >= 0 counts). ``degrees`` < 360: a wedge from ``start`` counterclockwise from +x."""
        e = _remap(self.expr, length(_X, _Y), _Z, _Z)
        if self.bounds is None:
            b = None
        else:
            R = builtins_max(abs(self.bounds[0][0]), abs(self.bounds[1][0]))
            b = (Vec(-R, -R, self.bounds[0][1]), Vec(R, R, self.bounds[1][1]))
        body = Implicit(e, b, history=(f"revolved {degrees:g}°",))
        if degrees < 360:
            body = body & wedge(degrees, start)
        return body

    def extrude(self, h: float, center: bool = False) -> Implicit:
        """The region swept along z from 0 to ``h`` (``center=True``: centered)."""
        _pos("extrude", h=h)
        z0 = -h / 2 if center else 0.0
        d = self.expr
        e = max(d, slab(_Z, z0, z0 + h))
        b = None if self.bounds is None else (Vec(*self.bounds[0], z0), Vec(*self.bounds[1], z0 + h))
        return Implicit(e, b)


import builtins as _builtins  # noqa: E402

builtins_max, builtins_min = _builtins.max, _builtins.min


def wedge(degrees: float, start: float = 0) -> Implicit:
    """The space between two half-planes through the z axis, from angle ``start`` turning
    ``degrees`` counterclockwise (from +x). Unbounded: intersect it (a cutaway: ``body - wedge(90)``)."""
    if not 0 < degrees < 360:
        raise TuleError(f"wedge: degrees must be between 0 and 360, got {degrees:g}", op="sdf.wedge")
    a0, a1 = math.radians(start), math.radians(start + degrees)
    d0 = math.sin(a0) * _X - math.cos(a0) * _Y  # positive on the near side of the start plane
    d1 = math.cos(a1) * _Y - math.sin(a1) * _X  # negative inside the end plane
    e = max(d0, d1) if degrees <= 180 else min(d0, d1)
    return Implicit(e)


def circle(r: float) -> Region:
    """A disc of radius ``r`` at the origin (2D)."""
    _pos("circle", r=r)
    return Region(length(_X, _Y) - r, ((-r, -r), (r, r)))


def rect(w: float, h: float, center: bool = True, radius: float = 0) -> Region:
    """A ``w`` x ``h`` rectangle (2D), centered on the origin (``center=False``: from it), with
    corners rounded to ``radius``."""
    _pos("rect", w=w, h=h)
    r = float(radius)
    if r < 0 or 2 * r > builtins_min(w, h):
        raise TuleError(f"rect: radius {r:g} must be between 0 and half the smaller side", op="sdf.rect")
    cu, cv = (0.0, 0.0) if center else (w / 2, h / 2)
    qx, qy = abs(_X - cu) - (w / 2 - r), abs(_Y - cv) - (h / 2 - r)
    e = length(max(qx, 0.0), max(qy, 0.0)) + min(max(qx, qy), 0.0) - r
    return Region(e, ((cu - w / 2, cv - h / 2), (cu + w / 2, cv + h / 2)))


def _points2(points, op: str, least: int) -> np.ndarray:
    P = np.asarray(points, dtype=float)
    if P.ndim != 2 or P.shape[1] != 2 or len(P) < least:
        raise TuleError(f"{op}: give at least {least} points (u, v)", op=f"sdf.{op}",
                        hint=f"sdf.{op}([(0, 0), (40, 0), (40, 10), (0, 30)])")
    return P


def _segments_d2(P: np.ndarray, closed: bool):
    """Per segment: the squared distance from (x, y) to it, and its (a, b)."""
    n = len(P)
    out = []
    for i in range(n if closed else n - 1):
        a, b = P[i], P[(i + 1) % n]
        e = b - a
        L2 = float(e @ e)
        if L2 == 0:
            continue
        wx, wy = _X - a[0], _Y - a[1]
        t = clamp((wx * e[0] + wy * e[1]) / L2, 0.0, 1.0)
        out.append(((wx - e[0] * t) ** 2 + (wy - e[1] * t) ** 2, a, b))
    return out


def polygon(points) -> Region:
    """A closed polygon (2D) through ``points`` (u, v), either winding; its exact signed distance
    (nearest edge, inside by crossings). A lathe profile: ``sdf.polygon([(0, 0), (40, 0), (30, 50), (0, 50)]).revolve()``."""
    P = _points2(points, "polygon", 3)
    if np.allclose(P[0], P[-1]):
        P = P[:-1]
    segs = _segments_d2(P, True)
    d2 = min(*[s[0] for s in segs]) if len(segs) > 1 else segs[0][0]
    sign = None
    for _, a, b in segs:
        if a[1] == b[1]:
            continue  # a level edge never crosses a horizontal ray
        e = b - a
        s1, s2 = _step(_Y - a[1], 1.0), _step(b[1] - _Y, 0.0)  # y >= a.y, y < b.y: a vertex counts once
        # x of the crossing vs the point, as a signed length: e × w over |e|
        s3 = _step((e[0] * (_Y - a[1]) - e[1] * (_X - a[0])) / math.hypot(*e))
        flip = s1 * s2 * s3 + (1 - s1) * (1 - s2) * (1 - s3)
        f = 1 - 2 * flip
        sign = f if sign is None else sign * f
    return Region(sqrt(d2) * sign, (tuple(P.min(0)), tuple(P.max(0))))


def _catmull(P: np.ndarray, closed: bool, samples: int) -> np.ndarray:
    """Points along a centripetal Catmull–Rom spline through P, ``samples`` per span."""
    n = len(P)
    if closed:
        Q = np.vstack([P[-1], P, P[0], P[1]])
    else:
        Q = np.vstack([2 * P[0] - P[1], P, 2 * P[-1] - P[-2]])
    out = []
    spans = n if closed else n - 1
    for i in range(spans):
        p0, p1, p2, p3 = Q[i], Q[i + 1], Q[i + 2], Q[i + 3]
        t0 = 0.0
        t1 = t0 + builtins_max(np.linalg.norm(p1 - p0), 1e-9) ** 0.5
        t2 = t1 + builtins_max(np.linalg.norm(p2 - p1), 1e-9) ** 0.5
        t3 = t2 + builtins_max(np.linalg.norm(p3 - p2), 1e-9) ** 0.5
        for t in np.linspace(t1, t2, samples, endpoint=False):
            a1 = (t1 - t) / (t1 - t0) * p0 + (t - t0) / (t1 - t0) * p1
            a2 = (t2 - t) / (t2 - t1) * p1 + (t - t1) / (t2 - t1) * p2
            a3 = (t3 - t) / (t3 - t2) * p2 + (t - t2) / (t3 - t2) * p3
            b1 = (t2 - t) / (t2 - t0) * a1 + (t - t0) / (t2 - t0) * a2
            b2 = (t3 - t) / (t3 - t1) * a2 + (t - t1) / (t3 - t1) * a3
            out.append((t2 - t) / (t2 - t1) * b1 + (t - t1) / (t2 - t1) * b2)
    if not closed:
        out.append(P[-1])
    return np.array(out)


def spline(points, closed: bool = True, samples: int = 12, width: float | None = None) -> Region:
    """A smooth curve (2D, centripetal Catmull–Rom) through ``points``: closed, the region it
    encloses; open (``closed=False``), a stroke ``width`` wide along it. ``samples`` per span."""
    P = _points2(points, "spline", 3 if closed else 2)
    Q = _catmull(P, closed, int(samples))
    return polygon(Q) if closed else polyline(Q, width if width is not None else 0)


def polyline(points, width: float) -> Region:
    """A stroke ``width`` wide (round ends and corners) along the open polyline ``points`` (2D)."""
    P = _points2(points, "polyline", 2)
    _pos("polyline", width=width)
    segs = _segments_d2(P, False)
    d2 = min(*[s[0] for s in segs]) if len(segs) > 1 else segs[0][0]
    w = width / 2
    return Region(sqrt(d2) - w, (tuple(P.min(0) - w), tuple(P.max(0) + w)))


def revolve(region: Region, degrees: float = 360, start: float = 0) -> Implicit:
    """``region.revolve(…)``: a 2D profile (u = radius, v = height) turned about the z axis."""
    return region.revolve(degrees, start)


def extrude(region: Region, h: float, center: bool = False) -> Implicit:
    return region.extrude(h, center)


# ---------------------------------------------------------------- domain repetition


def _polar_expr(e: Expr, count: int, overlap: bool) -> Expr:
    sector = 2 * math.pi / count
    rho = length(_X, _Y)
    a = atan2(_Y, _X)
    k = floor(a / sector + 0.5)
    a0 = a - k * sector  # in [-sector/2, sector/2]: the copy on +x
    shifts = (0.0, -sector, sector) if overlap else (0.0,)
    out = None
    for s in shifts:
        ai = a0 + s
        f = _remap(e, rho * cos(ai), rho * sin(ai), _Z)
        out = f if out is None else min(out, f)
    return out


def _repeat_polar(body: Implicit, count: int, overlap: bool = False) -> Implicit:
    if not (isinstance(count, int) and count >= 1):
        raise TuleError(f"repeat_polar: count must be a whole number >= 1, got {count!r}", op="repeat_polar")
    if count == 1:
        return body
    b = None
    if body.bounds is not None:
        lo, hi = body.bounds
        R = builtins_max(math.hypot(u, v) for u in (lo[0], hi[0]) for v in (lo[1], hi[1]))
        b = (Vec(-R, -R, lo[2]), Vec(R, R, hi[2]))
    return body._with(_polar_expr(body.expr, count, overlap), b,
                      history=body.history + (f"{count} copies about z",))


def _repeat_linear(body: Implicit, step: VecLike, count: int, overlap: bool = False) -> Implicit:
    if not (isinstance(count, int) and count >= 1):
        raise TuleError(f"repeat_linear: count must be a whole number >= 1, got {count!r}", op="repeat_linear")
    d = np.array(vec(step), dtype=float)
    L2 = float(d @ d)
    if L2 == 0:
        raise TuleError("repeat_linear: step must not be zero", op="repeat_linear")
    if count == 1:
        return body
    t = (_X * d[0] + _Y * d[1] + _Z * d[2]) / L2
    k = clamp(floor(t + 0.5), 0, count - 1)
    out = None
    for s in ((0, -1, 1) if overlap else (0,)):
        ks = clamp(k + s, 0, count - 1) if s else k
        f = _remap(body.expr, _X - ks * d[0], _Y - ks * d[1], _Z - ks * d[2])
        out = f if out is None else min(out, f)
    b = None
    if body.bounds is not None:
        lo, hi = np.array(body.bounds[0]), np.array(body.bounds[1])
        end = d * (count - 1)
        b = (Vec(*np.minimum(lo, lo + end)), Vec(*np.maximum(hi, hi + end)))
    return body._with(out, b, history=body.history + (f"{count} copies along {tuple(round(v, 3) for v in d)}",))


def repeat_polar(body: Implicit, count: int, overlap: bool = False) -> Implicit:
    """``count`` copies of ``body`` turned evenly about the z axis, still one expression of the
    same size (domain repetition): model the copy that sits on +x. The copies must each stay inside
    their sector; ``overlap=True`` also checks the two neighbours (3x the work) for copies that reach over."""
    return _repeat_polar(body, count, overlap)


def repeat_linear(body: Implicit, step: VecLike, count: int, overlap: bool = False) -> Implicit:
    """``count`` copies of ``body``, each ``step`` (x, y, z) further on, as one expression of the same
    size. As ``repeat_polar``: each copy stays within half a step of its place unless ``overlap=True``."""
    return _repeat_linear(body, step, count, overlap)


Implicit.repeat_polar = lambda self, count, overlap=False: _repeat_polar(self, count, overlap)
Implicit.repeat_linear = lambda self, step, count, overlap=False: _repeat_linear(self, step, count, overlap)
Implicit.repeat_polar.__doc__ = repeat_polar.__doc__
Implicit.repeat_linear.__doc__ = repeat_linear.__doc__


# ---------------------------------------------------------------- wall coordinates on a revolved contour


def _station(v, t: Expr, op: str) -> Expr:
    """A number, a station table [(t, value), …] (piecewise linear), or a function of the Expr t."""
    if callable(v):
        return _e(v(t))
    if isinstance(v, (int, float)):
        return _e(float(v))
    pts = np.asarray(v, dtype=float)
    if pts.ndim != 2 or pts.shape[1] != 2:
        raise TuleError(f"{op}: give a number, a table [(z, value), …] or a function of z", op=op)
    return interp(t, pts[:, 0], pts[:, 1])


class Wall:
    """A revolved wall contour, its radius ``r(z)`` from the axis given at stations (z increasing:
    a chamber, a throat, a nozzle). It gives coordinates that follow the wall:

    * ``n``: signed distance from the contour along its normal, positive away from the axis;
    * ``s``: length along the contour from its first station; ``theta``: angle about z (radians);
    * ``t``: the distance around (``rho · angle``), for features of a given width.

    and builds bodies in them: ``solid(inner, outer)`` (a liner, a jacket), ``channels(count, width,
    depth, offset)`` (regenerative cooling channels that follow the contour and narrow at the throat
    with it) and ``ribs(count, width, height)`` on the outside. Widths, depths and offsets are a
    number, a station table ``[(z, value), …]`` or a function of the z expression."""

    def __init__(self, z, r, samples: int = 0):
        z, r = np.asarray(z, dtype=float), np.asarray(r, dtype=float)
        if samples:  # smooth through the stations
            Q = _catmull(np.column_stack([z, r]), False, int(samples))
            z, r = Q[:, 0], Q[:, 1]
        if len(z) < 2 or len(z) != len(r) or np.any(np.diff(z) <= 0):
            raise TuleError("Wall(z, r): two or more stations, z increasing, a radius each", op="sdf.Wall",
                            hint="sdf.Wall([0, 300, 420, 900], [220, 220, 130, 420])")
        self.z, self.r = z, r
        dz, dr = np.diff(z), np.diff(r)
        seg_c = dz / np.hypot(dz, dr)  # cos of the wall's slope per segment
        c = np.concatenate([[seg_c[0]], (seg_c[:-1] + seg_c[1:]) / 2, [seg_c[-1]]])
        self._c = c
        self.length = float(np.hypot(dz, dr).sum())
        self._s = np.concatenate([[0.0], np.cumsum(np.hypot(dz, dr))])
        self.rho = length(_X, _Y)
        self.theta = atan2(_Y, _X)
        self.radius = interp(_Z, z, r)
        self.n = (self.rho - self.radius) * interp(_Z, z, c)
        self.s = interp(_Z, z, self._s)
        self.t = self.rho * self.theta

    def __repr__(self) -> str:
        return f"<Wall: {len(self.z)} stations, z {self.z[0]:g}–{self.z[-1]:g}, r {self.r.min():g}–{self.r.max():g} mm>"

    def r_at(self, z) -> np.ndarray:
        """The contour's radius at heights ``z`` (numbers)."""
        return np.interp(z, self.z, self.r)

    def _span(self, z):
        z0, z1 = (self.z[0], self.z[-1]) if z is None else (float(z[0]), float(z[1]))
        return z0, z1

    def _box(self, z0, z1, out):
        R = float(self.r.max() + out)
        return (Vec(-R, -R, z0), Vec(R, R, z1))

    def solid(self, inner=0.0, outer=1.0, z=None) -> Implicit:
        """The wall between normal offsets ``inner`` and ``outer`` (mm; negative is toward the axis),
        from ``z[0]`` to ``z[1]`` (default: the whole contour), cut square to the axis at the ends."""
        z0, z1 = self._span(z)
        lo, hi = _station(inner, _Z, "Wall.solid"), _station(outer, _Z, "Wall.solid")
        e = max(lo - self.n, self.n - hi, slab(_Z, z0, z1))
        out = builtins_max(_station_max(outer), 0.0) + 1.0
        return Implicit(e, self._box(z0, z1, out), history=(f"wall {z0:g}–{z1:g}",))

    def channels(self, count: int, width=None, depth=1.0, offset=1.0, z=None, land=None, phase: float = 0.0) -> Implicit:
        """``count`` channels around the wall, each ``width`` wide (or ``land``: the rib left between
        channels, so they widen and narrow with the circumference), from normal offset ``offset``
        outward ``depth`` deep, running from ``z[0]`` to ``z[1]``. Subtract them from a ``solid``.
        One expression whatever the count (domain repetition about z)."""
        if not (isinstance(count, int) and count >= 1):
            raise TuleError("channels: count must be a whole number >= 1", op="Wall.channels")
        if (width is None) == (land is None):
            raise TuleError("channels: give width= or land=, one of them", op="Wall.channels",
                            hint="width=1.2 (mm), a table [(z, w), …], or land=1.0 (mm of rib between channels)")
        z0, z1 = self._span(z)
        off = _station(offset, _Z, "Wall.channels")
        dep = _station(depth, _Z, "Wall.channels")
        sector = 2 * math.pi / count
        if land is not None:
            mid = self.radius + off + dep / 2
            w = mid * sector - _station(land, _Z, "Wall.channels")
        else:
            w = _station(width, _Z, "Wall.channels")
        a = self.theta - math.radians(phase)
        a0 = a - floor(a / sector + 0.5) * sector
        t = self.rho * a0
        e = max(abs(t) - w / 2, off - self.n, self.n - off - dep, slab(_Z, z0, z1))
        out = _station_max(offset) + _station_max(depth) + 1.0
        return Implicit(e, self._box(z0, z1, out), history=(f"{count} channels",))

    def ribs(self, count: int, width, height, base=0.0, z=None, phase: float = 0.0) -> Implicit:
        """``count`` ribs standing on the wall at normal offset ``base``, ``height`` tall and ``width``
        wide (numbers, tables or functions of z), as one expression. Union them with a ``solid``."""
        z0, z1 = self._span(z)
        b, h, w = (_station(v, _Z, "Wall.ribs") for v in (base, height, width))
        sector = 2 * math.pi / count
        a = self.theta - math.radians(phase)
        a0 = a - floor(a / sector + 0.5) * sector
        e = max(abs(self.rho * a0) - w / 2, b - self.n, self.n - b - h, slab(_Z, z0, z1))
        out = _station_max(base) + _station_max(height) + 1.0
        return Implicit(e, self._box(z0, z1, out), history=(f"{count} ribs",))

    def bands(self, at, width, height, base=0.0) -> Implicit:
        """Hoops around the wall: at heights ``at`` (a list of z), ``width`` along z, ``height`` out
        from normal offset ``base``."""
        zs = [float(v) for v in at]
        b, h = _station(base, _Z, "Wall.bands"), _station(height, _Z, "Wall.bands")
        dz = min(*[abs(_Z - zc) for zc in zs]) if len(zs) > 1 else abs(_Z - zs[0])
        e = max(dz - float(width) / 2, b - self.n, self.n - b - h)
        out = _station_max(base) + _station_max(height) + 1.0
        return Implicit(e, self._box(builtins_min(zs) - width, builtins_max(zs) + width, out), history=(f"{len(zs)} bands",))


def _station_max(v) -> float:
    if callable(v):
        return 50.0  # unknown: a margin for the bounds
    if isinstance(v, (int, float)):
        return float(v)
    return float(np.max(np.asarray(v, dtype=float)[:, 1]))


# ---------------------------------------------------------------- tubes along 3D paths


def _fillet_corners(P: np.ndarray, bend: float, samples: int = 8) -> np.ndarray:
    """The polyline with each inner corner replaced by an arc of radius ``bend`` (pipe bends)."""
    out = [P[0]]
    for i in range(1, len(P) - 1):
        a, b, c = P[i - 1], P[i], P[i + 1]
        u, v = a - b, c - b
        lu, lv = np.linalg.norm(u), np.linalg.norm(v)
        if lu == 0 or lv == 0:
            continue
        u, v = u / lu, v / lv
        cosang = float(np.clip(u @ v, -1, 1))
        ang = math.acos(cosang)  # the angle between the legs
        if ang > math.pi - 1e-3:
            out.append(b)
            continue
        tlen = bend / math.tan(ang / 2)
        tlen = builtins_min(tlen, 0.5 * lu, 0.5 * lv)
        r = tlen * math.tan(ang / 2)
        p0, p1 = b + u * tlen, b + v * tlen
        bis = (u + v) / np.linalg.norm(u + v)
        center = b + bis * (r / math.sin(ang / 2))
        e0, e1 = p0 - center, p1 - center
        turn = math.pi - ang
        for k in range(samples + 1):
            s = k / samples
            # slerp between e0 and e1
            w0 = math.sin((1 - s) * turn) / math.sin(turn)
            w1 = math.sin(s * turn) / math.sin(turn)
            out.append(center + w0 * e0 + w1 * e1)
    out.append(P[-1])
    return np.array(out)


def tube(points, r, wall: float | None = None, spline: bool = False, bend: float | None = None,
         samples: int = 8, ends: str = "flat") -> Implicit:
    """A tube of radius ``r`` along a 3D path through ``points`` (ducts, manifolds, lines):
    straight between them, or a smooth curve (``spline=True``, Catmull–Rom), or straight with
    each corner bent to radius ``bend``. ``r`` may be one radius or one per point (tapers).
    ``wall``: hollow, that thick, open at flat ends. ``ends="flat"`` or ``"round"``."""
    P = np.asarray(points, dtype=float)
    if P.ndim != 2 or P.shape[1] != 3 or len(P) < 2:
        raise TuleError("tube: give two or more points (x, y, z)", op="sdf.tube",
                        hint="sdf.tube([(0, 0, 0), (0, 0, 100), (80, 0, 160)], r=12, bend=40)")
    radii = np.broadcast_to(np.asarray(r, dtype=float), (len(P),)).copy()
    if np.any(radii <= 0):
        raise TuleError("tube: r must be > 0", op="sdf.tube")
    if ends not in ("flat", "round"):
        raise TuleError(f"tube: ends is 'flat' or 'round', not {ends!r}", op="sdf.tube")
    if spline or bend:
        s_old = np.concatenate([[0], np.cumsum(np.linalg.norm(np.diff(P, axis=0), axis=1))])
        Q = _catmull(P, False, int(samples)) if spline else _fillet_corners(P, float(bend), int(samples))
        s_new = np.concatenate([[0], np.cumsum(np.linalg.norm(np.diff(Q, axis=0), axis=1))])
        radii = np.interp(s_new / builtins_max(s_new[-1], 1e-12) * s_old[-1], s_old, radii)
        P = Q
    keep = np.concatenate([[True], np.linalg.norm(np.diff(P, axis=0), axis=1) > 1e-9])
    P, radii = P[keep], radii[keep]
    if len(P) < 2:
        raise TuleError("tube: the points coincide", op="sdf.tube")

    def field(shrink: float, cap: bool) -> Expr:
        out = None
        last = len(P) - 2
        for i in range(len(P) - 1):
            a, b = P[i], P[i + 1]
            ab = b - a
            L2 = float(ab @ ab)
            px, py, pz = _X - a[0], _Y - a[1], _Z - a[2]
            tt = (px * ab[0] + py * ab[1] + pz * ab[2]) / L2
            t = clamp(tt, 0.0, 1.0)
            ra, rb = radii[i] - shrink, radii[i + 1] - shrink
            rr = ra if ra == rb else ra + (rb - ra) * t
            d = length(px - t * ab[0], py - t * ab[1], pz - t * ab[2]) - rr
            if cap and i == 0:
                d = max(d, -tt * math.sqrt(L2))  # flat at the start
            if cap and i == last:
                d = max(d, (tt - 1) * math.sqrt(L2))  # flat at the end
            out = d if out is None else min(out, d)
        return out

    flat = ends == "flat"
    e = field(0.0, flat)
    if wall is not None:
        _pos("tube", wall=wall)
        if wall >= radii.min():
            raise TuleError(f"tube: wall {wall:g} must be thinner than the radius ({radii.min():g})", op="sdf.tube")
        e = max(e, -field(float(wall), False))
    R = float(radii.max())
    return Implicit(e, (Vec(*(P.min(0) - R)), Vec(*(P.max(0) + R))), history=(f"tube along {len(P)} points",))
