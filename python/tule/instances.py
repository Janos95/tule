"""One body placed many times: its geometry is stored, meshed and drawn once.

``part.placed(transforms)`` (a Shape, Mesh, Implicit or Assembly) is an ``Instances``: the body
and N placements, each a 4×4 matrix (row-major, as ``Shape.transform`` takes). It goes into an
``Assembly`` like a part. ``outputs`` writes the body's file once with the placements in
``output.json`` (or a ``.npy`` beside it for many), the machine meshes it once, and the viewer
draws it with one instanced draw call: 18,000 heat-shield tiles cost one tile and 18,000 matrices.
"""

from __future__ import annotations

import numpy as np

from .errors import TuleError
from .geom import Vec, vec


def _matrices(transforms) -> np.ndarray:
    """(N, 4, 4) float64 from (N, 4, 4), (N, 3, 4), (N, 16), (N, 12) or a list of matrices."""
    if isinstance(transforms, Instances):
        return transforms.transforms
    try:
        a = np.asarray(transforms, dtype=np.float64)
    except (TypeError, ValueError):
        raise TuleError("transforms must be numbers: (N, 4, 4) matrices, row-major", op="placed",
                        hint="placed(at=centers) for positions only, or a list of 4×4 numpy arrays") from None
    if a.ndim == 2 and a.shape[1] in (12, 16):
        a = a.reshape(len(a), a.shape[1] // 4, 4)
    if a.ndim == 2 and a.shape in ((4, 4), (3, 4)):
        a = a[None]
    if a.ndim != 3 or a.shape[1:] not in ((4, 4), (3, 4)):
        raise TuleError(f"transforms must be (N, 4, 4) matrices, got shape {a.shape}", op="placed",
                        hint="positions are placed(at=points) with points (N, 3); rotations: rotate=(N, 3) degrees")
    out = np.zeros((len(a), 4, 4))
    out[:, :3] = a[:, :3]
    out[:, 3, 3] = 1
    if not np.isfinite(out).all():
        raise TuleError("transforms contain NaN or infinity", op="placed")
    return out


def _rotations(deg) -> np.ndarray:
    """(N, 3) degrees -> (N, 3, 3): about x, then y, then z (as ``part.rotate``)."""
    r = np.radians(np.asarray(deg, dtype=np.float64))
    c, s = np.cos(r), np.sin(r)
    one, zero = np.ones(len(r)), np.zeros(len(r))
    Rx = np.stack([one, zero, zero, zero, c[:, 0], -s[:, 0], zero, s[:, 0], c[:, 0]], 1).reshape(-1, 3, 3)
    Ry = np.stack([c[:, 1], zero, s[:, 1], zero, one, zero, -s[:, 1], zero, c[:, 1]], 1).reshape(-1, 3, 3)
    Rz = np.stack([c[:, 2], -s[:, 2], zero, s[:, 2], c[:, 2], zero, zero, zero, one], 1).reshape(-1, 3, 3)
    return Rz @ Ry @ Rx


def _per_copy(x, n: int | None, what: str) -> np.ndarray:
    a = np.asarray(x, dtype=np.float64)
    if a.ndim == 1:
        a = a[None]
    if a.ndim != 2 or a.shape[1] not in (2, 3):
        raise TuleError(f"{what} must be (N, 3), got shape {a.shape}", op="placed")
    if a.shape[1] == 2:
        a = np.hstack([a, np.zeros((len(a), 1))])
    if n is not None and len(a) not in (1, n):
        raise TuleError(f"{what} has {len(a)} rows for {n} copies", op="placed", hint="one row per copy, or one for all")
    return a


def _move(op: str, args) -> np.ndarray:
    """The 4×4 of a part's move (at, rotate, mirror, ...), as a Mesh makes it."""
    from .mesh import _axis_matrix

    M = np.eye(4)
    if op == "at":
        M[:3, 3] = args[0]
        return M
    if op == "rotate":
        (x, y, z), about = args
        R = _rotations([[x, y, z]])[0]
    elif op == "rotate_about":
        axis, deg, about = args
        R = _axis_matrix(np.array(vec(axis)), deg)
    else:  # mirror
        n, about = args
        n = np.asarray(n, dtype=np.float64) / max(np.linalg.norm(n), 1e-300)
        R = np.eye(3) - 2 * np.outer(n, n)
    c = np.array(vec(about), dtype=np.float64)
    M[:3, :3] = R
    M[:3, 3] = c - R @ c
    return M


class Instances:
    """A body (Shape, Mesh, Implicit or Assembly) placed N times; ``kind == "instances"``.

    ``part.placed(T)`` with T (N, 4, 4) matrices, or ``part.placed(at=points, rotate=degrees)``:
    each copy is the part rotated (about x, then y, then z, as ``part.rotate``) and then moved to its
    point (``at``: (N, 3); ``rotate``: (N, 3), or one row for all). With both, the matrix comes first:
    ``part.transform(T[i]).rotate(*rotate[i]).at(*at[i])``.

    It is a part of an ``Assembly`` (``Assembly(hull, tiles, engines)``), and ``outputs.part`` /
    ``outputs.assembly`` write the body once with its placements. ``.at``, ``.rotate``, ``.mirror`` move
    every copy; ``.named`` names the group; ``.to_mesh(...)`` meshes the body once (an implicit's copies);
    ``bounds``, ``volume``, ``len``; ``.parts()`` lists the copies as moved bodies (``"tiles[17]"``),
    which is what B-rep operations, clearance and plates use.
    """

    kind = "instances"

    def __init__(self, body, transforms=None, *, at=None, rotate=None, name: str | None = None, color: str | None = None):
        from .assembly import Assembly

        if isinstance(body, Instances):  # copies of copies: one level, the matrices multiplied
            inner = body
            body, base = inner.body, inner.transforms
            name, color = name or inner.name, color or inner.color
        else:
            base = None
        if not (isinstance(body, Assembly) or getattr(body, "kind", None) in ("brep", "mesh", "implicit")):
            raise TypeError(f"placed copies of a Shape, Mesh, Implicit or Assembly, not {type(body).__name__}")
        n = None
        T = _matrices(transforms) if transforms is not None else None
        if T is not None:
            n = len(T)
        A = _per_copy(at, n, "at") if at is not None else None
        n = n if n is not None else (len(A) if A is not None and len(A) > 1 else None)
        R = _per_copy(rotate, n, "rotate") if rotate is not None else None
        n = n if n is not None else (len(R) if R is not None else (len(A) if A is not None else None))
        if n is None:
            raise TuleError("where are the copies?", op="placed",
                            hint="part.placed(matrices) with (N, 4, 4), or part.placed(at=points, rotate=degrees)")
        M = T if T is not None else np.broadcast_to(np.eye(4), (n, 4, 4)).copy()
        if R is not None:
            Rm = np.broadcast_to(np.eye(4), (n, 4, 4)).copy()
            Rm[:, :3, :3] = _rotations(np.broadcast_to(R, (n, 3)))
            M = Rm @ M
        if A is not None:
            M = M.copy()
            M[:, :3, 3] += np.broadcast_to(A, (n, 3))
        if base is not None:  # inner copies, then these placements
            M = (M[:, None] @ base[None]).reshape(-1, 4, 4)
        det = np.linalg.det(M[:, :3, :3]) if len(M) else np.zeros(0)
        if len(M) and np.abs(det).min() < 1e-12:
            i = int(np.abs(det).argmin())
            raise TuleError(f"transform {i} flattens the part (its determinant is 0)", op="placed")
        if color is not None:
            from .io import parse_rgba
            parse_rgba(color)
        self.body = body
        self._T = M
        self._T.setflags(write=False)
        self.name = name if name is not None else getattr(body, "name", None)
        self.color = color if color is not None else getattr(body, "color", None)

    # ------------------------------------------------------------ what it is
    @property
    def transforms(self) -> np.ndarray:
        """The placements: (N, 4, 4), row-major (read-only)."""
        return self._T

    def __len__(self) -> int:
        return len(self._T)

    def __repr__(self) -> str:
        n = f" {self.name!r}" if self.name else ""
        return f"<Instances{n}: {len(self):,} copies of {self.body!r}>"

    def _with(self, T=None, name=None, color=None, body=None) -> "Instances":
        out = Instances.__new__(Instances)
        out.body = self.body if body is None else body
        out._T = self._T if T is None else T
        out._T.setflags(write=False)
        out.name = self.name if name is None else name
        out.color = self.color if color is None else color
        return out

    def named(self, name: str, color: str | None = None) -> "Instances":
        """The group's name (the viewer's part, ``"tiles[17]"`` for one copy) and, optionally, a color for all."""
        if color is not None:
            from .io import parse_rgba
            parse_rgba(color)
        return self._with(name=name, color=color)

    def colored(self, color: str) -> "Instances":
        return self.named(self.name, color)

    # ------------------------------------------------------------ moving every copy
    def _moved(self, M: np.ndarray) -> "Instances":
        return self._with(T=M[None] @ self._T)

    def at(self, x=0, y=0, z=0) -> "Instances":
        v = vec(x) if not isinstance(x, (int, float)) else Vec(x, y, z)
        return self._moved(_move("at", (tuple(v),)))

    translate = at

    def rotate(self, x=0, y=0, z=0, about=(0, 0, 0)) -> "Instances":
        return self._moved(_move("rotate", ((x, y, z), about)))

    def rotate_about(self, axis_dir, degrees: float, origin=(0, 0, 0)) -> "Instances":
        return self._moved(_move("rotate_about", (axis_dir, degrees, origin)))

    def mirror(self, nx=1, ny=0, nz=0, origin=(0, 0, 0)) -> "Instances":
        n = vec(nx) if not isinstance(nx, (int, float)) else Vec(nx, ny, nz)
        return self._moved(_move("mirror", (tuple(n), origin)))

    def transform(self, matrix) -> "Instances":
        """Every copy moved by a 4×4 (or 3×4) matrix."""
        return self._moved(_matrices(matrix)[0])

    def to_mesh(self, *args, **kwargs) -> "Instances":
        """The body meshed once (``body.to_mesh(...)``: a tolerance for a B-rep, ``cell=`` for an implicit), the same copies."""
        from .assembly import Assembly

        if isinstance(self.body, Assembly):
            raise TuleError("to_mesh of copies of an assembly", op="placed", hint="mesh the assembly's parts, then place it")
        return self._with(body=self.body.to_mesh(*args, **kwargs))

    # ------------------------------------------------------------ measures
    @property
    def bounds(self) -> tuple[Vec, Vec]:
        """Bounds of every copy (each copy's box moved: a little loose where copies are turned)."""
        b = self.body.bounds
        if b is None:
            raise TuleError("the body is unbounded", op="placed", hint="intersect it with a bounded body first")
        lo, hi = np.asarray(b[0], dtype=np.float64), np.asarray(b[1], dtype=np.float64)
        C = np.array([[(lo, hi)[i][0], (lo, hi)[j][1], (lo, hi)[k][2]] for i in (0, 1) for j in (0, 1) for k in (0, 1)])
        P = np.einsum("nij,cj->nci", self._T[:, :3, :3], C) + self._T[:, None, :3, 3]
        P = P.reshape(-1, 3)
        return Vec(*P.min(0)), Vec(*P.max(0))

    @property
    def size(self):
        lo, hi = self.bounds
        return hi - lo

    @property
    def volume(self) -> float:
        return float(self.body.volume * np.abs(np.linalg.det(self._T[:, :3, :3])).sum())

    # ------------------------------------------------------------ the copies, one by one
    def copy(self, i: int):
        """Copy ``i`` as a moved body (a Shape copy shares the solid: a placed B-rep costs nothing)."""
        return _placed(self.body, self._T[i])

    def parts(self, prefix: str = "") -> list:
        """Every copy as ``(path, moved part)``: ``"tiles[0]"``, … (an assembly's parts under each)."""
        return self._copies(prefix + (self.name or "copy"))

    def _copies(self, stem: str) -> list:
        from .assembly import Assembly

        out = []
        for i in range(len(self)):
            p = f"{stem}[{i}]"
            c = self.copy(i)
            if isinstance(c, Assembly):
                out.extend((p + "/" + (q.split("/", 1)[-1] if c.name else q), s) for q, s in c.parts())
            else:
                out.append((p, c.named(p.rsplit("/", 1)[-1], self.color or c.color)))
        return out

    def leaves(self) -> list:
        """``[(path below the group, Shape or Mesh, (N, 4, 4))]``: what a writer stores once with its placements."""
        from .assembly import Assembly

        if isinstance(self.body, Assembly):
            return [(q.split("/", 1)[-1] if self.body.name else q, s, T) for q, s, T in _assembly_leaves(self.body, self._T)]
        return [("", self.body, self._T)]


def _assembly_leaves(asm, outer: np.ndarray):
    """(path, part, transforms) of an assembly placed at ``outer``: its parts at ``outer``; its copies at outer × theirs."""
    out = []
    for path, child in asm._leaves():
        if isinstance(child, Instances):
            for sub, body, T in child.leaves():
                full = (outer[:, None] @ T[None]).reshape(-1, 4, 4)
                out.append((f"{path}/{sub}" if sub else path, body, full))
        else:
            out.append((path, child, outer))
    return out


def _rigid(M: np.ndarray) -> bool:
    R = M[:3, :3]
    return abs(np.linalg.det(R) - 1) < 1e-9 and np.allclose(R @ R.T, np.eye(3), atol=1e-9)


def _placed(body, M: np.ndarray):
    """``body`` moved by the 4×4 ``M``."""
    from .assembly import Assembly

    if isinstance(body, Assembly):
        return body._map(lambda s: _placed(s, M))
    if isinstance(body, Instances):
        return body._moved(M)
    kind = getattr(body, "kind", None)
    if kind == "brep" and _rigid(M):  # a placement: the same solid, located (no copy of its geometry)
        from OCP.gp import gp_Trsf

        t = gp_Trsf()
        t.SetValues(*M[0], *M[1], *M[2])
        return body._apply_trsf(t)
    if kind == "implicit":
        return body._affine(M[:3, :3], M[:3, 3], scale=abs(float(np.linalg.det(M[:3, :3]))) ** (1 / 3))
    return body.transform(M)
