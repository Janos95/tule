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
"""

from __future__ import annotations

import math
from typing import Callable, Iterable, Sequence

import numpy as np

from .errors import TuleError
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


class Implicit:
    """An implicit body: a signed-distance expression (negative inside), its bounds, name, color,
    tags (as predicates applied when meshed) and history. ``kind == "implicit"``."""

    kind = "implicit"

    def __init__(self, expr, bounds=None, *, name: str | None = None, color: str | None = None,
                 tags: dict | None = None, history: Sequence[str] = ()):
        self.expr = _e(expr)
        self.bounds = _bounds(bounds)
        self.name = name
        self.color = color
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
    def named(self, name: str, color: str | None = None) -> "Implicit":
        if color is not None:
            from .io import parse_rgba
            parse_rgba(color)
        out = self._with(self.expr, self.bounds)
        out.name, out.color = name, color or self.color
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

    def mirror(self, nx: float | VecLike = 1, ny: float = 0, nz: float = 0, origin: VecLike = (0, 0, 0)) -> "Implicit":
        n = np.array(vec(nx) if not isinstance(nx, (int, float)) else (nx, ny, nz), dtype=float)
        n /= np.linalg.norm(n)
        R = np.eye(3) - 2 * np.outer(n, n)
        c = np.array(vec(origin), dtype=float)
        return self._affine(R, c - R @ c)

    # meshing
    def to_mesh(self, cell: float | None = None, bounds=None) -> "object":
        """This body as a closed ``tule.Mesh`` (Fidget: JIT evaluation and dual contouring, sharp edges
        kept). ``cell`` (mm) is the smallest octree cell: detail under it is lost (default: a 128th of
        the largest size). ``bounds`` (lo, hi) clips it; an unbounded body (a bare lattice) needs them."""
        from .mesh import Mesh

        b = _bounds(bounds) if bounds is not None else self.bounds
        if b is None:
            raise TuleError("this body is unbounded (a lattice or a plane on its own)", op="to_mesh",
                            hint="intersect it with a bounded body (body & sdf.gyroid(8, 1)) or give bounds=((x0, y0, z0), (x1, y1, z1))")
        e = self.expr
        if bounds is not None:  # clipped to the bounds asked for (a bare lattice, or a piece of a body)
            e = max(e, box_between(*b).expr)
        else:  # a surface that runs past the body's own bounds is closed just outside them, not left open
            g = 1.5 * (float(cell) if cell else float(np.max(np.subtract(b[1], b[0]))) / 128)
            e = max(e, box_between(tuple(np.subtract(b[0], g)), tuple(np.add(b[1], g))).expr)
        lo, hi = np.array(b[0], dtype=float), np.array(b[1], dtype=float)
        size = float((hi - lo).max())
        cell = float(cell) if cell else size / 128
        if cell <= 0:
            raise TuleError("cell must be > 0 mm", op="to_mesh")
        center = (lo + hi) / 2
        half = size / 2 + 2 * cell
        depth = int(math.ceil(math.log2(2 * half / cell)))
        fg = _fidget()
        if depth > 12 or (fg is None or _has_grid(e)) and (2 * half / cell) ** 3 > 2.5e8:
            raise TuleError(f"cells of {cell:g} mm across {size:.4g} mm are too many", op="to_mesh",
                            hint=f"a larger cell (≥ {size / 2000:.2g} mm), or mesh pieces separately and join the meshes")
        if fg is not None and not _has_grid(e):
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
            raise TuleError("the body has no surface inside its bounds", op="to_mesh",
                            hint="is it empty, or all inside? check body.value(points); a finer cell for thin walls")
        t = P[T]
        if np.einsum("ij,ij->i", t[:, 0], np.cross(t[:, 1], t[:, 2])).sum() < 0:
            T = T[:, ::-1]
        m = Mesh(P, T, name=self.name, color=self.color, welded=True)
        if fg is not None and not _has_grid(e):
            m = _clean(e, m, cell)
        m.history = self.history + (how,)
        for tag, fn in self._tags.items():
            try:
                m = m.tag(tag, fn)
            except TuleError as err:
                raise TuleError(f"tag {tag!r} selects nothing on the mesh", op="to_mesh",
                                hint="check the predicate against mesh.faces (bounds, tol=)") from err
        return m


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
