"""Triangle meshes as parts: ``tule.Mesh``, one of Tule's three kinds of geometry.

A Mesh holds vertices, triangles, a *region* id per triangle and tags (name → regions).
Regions play the part B-rep faces play: the viewer's ``f12``, selections, FEA loads. A mesh
made from a B-rep (``part.to_mesh()``) keeps one region per B-rep face and every tag; any
other mesh starts with regions split at creases (edges sharper than 35°).

```python
from tule import *

m = part.to_mesh(0.05)                          # B-rep -> mesh (tessellated within 0.05 mm)
m = m | lattice.to_mesh(cell=0.4)               # union with an implicit's mesh (manifold3d)
m = m.tag("load", m.faces.top())                # a tag: whole regions (split where needed)
m = m.tag("mount", m.faces.within((0, 0, 0), (5, 40, 40)))
outputs.part("bracket", m.named("bracket", "#4a8"))
```

Booleans (``|``, ``-``, ``&``) run on manifold3d and keep regions and tags. Operators never
mix kinds: convert first (``part.to_mesh()``, ``body.to_mesh(cell=…)``). ``history`` lists
what the mesh went through and what each step lost.
"""

from __future__ import annotations

import math
import os
import struct
from typing import Callable, Iterable, Sequence

import numpy as np

from .errors import TuleError
from .geom import Vec, VecLike, vec

CREASE = 35.0  # degrees: sharper edges are features (drawn, and region borders)
MAX_BREP_TRIANGLES = 20_000


# ---------------------------------------------------------------- mesh helpers (also used by the viewer's agent)


def tri_normals(P: np.ndarray, T: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Unit normals and areas of triangles."""
    if not len(T):
        return np.zeros((0, 3)), np.zeros(0)
    cr = np.cross(P[T[:, 1]] - P[T[:, 0]], P[T[:, 2]] - P[T[:, 0]])
    ln = np.linalg.norm(cr, axis=1)
    return cr / np.maximum(ln, 1e-300)[:, None], 0.5 * ln


def weld(P: np.ndarray, T: np.ndarray, tol: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Vertices within ``tol`` merged; degenerate triangles dropped: (P, T, kept triangle indices)."""
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components
    from scipy.spatial import cKDTree

    P = np.asarray(P, dtype=np.float64).reshape(-1, 3)
    T = np.asarray(T, dtype=np.int64).reshape(-1, 3)
    if not len(P):
        return P, T, np.arange(len(T))
    if tol > 0:
        pairs = cKDTree(P).query_pairs(tol, output_type="ndarray")
        n = len(P)
        g = coo_matrix((np.ones(len(pairs)), (pairs[:, 0], pairs[:, 1])), shape=(n, n))
        _, label = connected_components(g, directed=False)
    else:  # exactly equal coordinates only (an STL's copies of one vertex)
        _, label = np.unique(P, axis=0, return_inverse=True)
        label = label.reshape(-1)
    F = label[T]
    keep = np.flatnonzero((F[:, 0] != F[:, 1]) & (F[:, 1] != F[:, 2]) & (F[:, 0] != F[:, 2]))
    used, inv = np.unique(F[keep], return_inverse=True)
    V = np.zeros((label.max() + 1, 3))
    V[label] = P
    return V[used], inv.reshape(-1, 3), keep


def edge_table(T: np.ndarray):
    """Undirected edges: (a, b) vertex pairs (k, 2), and for each the triangles on it (k, 2; -1 for a
    boundary edge), plus the count of triangles on each (an edge in more than two is non-manifold)."""
    m = len(T)
    e = np.concatenate([T[:, [0, 1]], T[:, [1, 2]], T[:, [2, 0]]])
    t = np.tile(np.arange(m), 3)
    s = np.sort(e, axis=1)
    key = s[:, 0] * (int(T.max(initial=0)) + 1) + s[:, 1]
    order = np.argsort(key, kind="stable")
    key, s, t = key[order], s[order], t[order]
    first = np.flatnonzero(np.r_[True, key[1:] != key[:-1]])
    count = np.diff(np.r_[first, len(key)])
    tris = np.full((len(first), 2), -1, dtype=np.int64)
    tris[:, 0] = t[first]
    two = count >= 2
    tris[two, 1] = t[first[two] + 1]
    return s[first], tris, count


def _degenerate(A: np.ndarray) -> np.ndarray:
    """Triangles of next to no area (a mesher's slivers), whose normals mean nothing."""
    return A < 1e-4 * float(np.median(A)) if len(A) else np.zeros(0, bool)


def crease_regions(P: np.ndarray, T: np.ndarray, angle: float = CREASE) -> np.ndarray:
    """A region id per triangle: triangles joined across edges flatter than ``angle``."""
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components

    if not len(T):
        return np.zeros(0, np.uint32)
    N, A0 = tri_normals(P, T)
    _, tris, count = edge_table(T)
    inner = (count == 2)
    a, b = tris[inner, 0], tris[inner, 1]
    deg = _degenerate(A0)  # a zero-area sliver's normal is noise: never a crease beside one
    smooth = (np.einsum("ij,ij->i", N[a], N[b]) > math.cos(math.radians(angle))) | deg[a] | deg[b]
    a, b = a[smooth], b[smooth]
    g = coo_matrix((np.ones(len(a)), (a, b)), shape=(len(T), len(T)))
    _, lab = connected_components(g, directed=False)
    # slivers (a mesher's bevels along sharp edges, folds) join the neighbour they share most edges with
    _, A = tri_normals(P, T)
    ta, tb = tris[inner, 0], tris[inner, 1]
    for _ in range(3):
        area = np.bincount(lab, weights=A)
        small = area < 2e-3 * A.sum()
        la, lb = lab[ta], lab[tb]
        cross = la != lb
        if not small.any() or not cross.any():
            break
        pa, pb = np.concatenate([la[cross], lb[cross]]), np.concatenate([lb[cross], la[cross]])
        keep = small[pa] & ~small[pb]
        if not keep.any():
            keep = small[pa] & (area[pb] > area[pa])
            if not keep.any():
                break
        pa, pb = pa[keep], pb[keep]
        pair, cnt = np.unique(np.stack([pa, pb], 1), axis=0, return_counts=True)
        best = np.lexsort((-cnt, pair[:, 0]))
        pair = pair[best]
        first = np.r_[True, pair[1:, 0] != pair[:-1, 0]]
        to = np.arange(len(area))
        to[pair[first, 0]] = pair[first, 1]
        lab = to[lab]
        _, lab = np.unique(lab, return_inverse=True)
    # number regions by size, largest first, so ids are stable for the same mesh
    sizes = np.bincount(lab)
    rank = np.empty_like(sizes)
    rank[np.argsort(-sizes, kind="stable")] = np.arange(len(sizes))
    return rank[lab].astype(np.uint32)


def feature_edges(P: np.ndarray, T: np.ndarray, F: np.ndarray | None = None, angle: float = CREASE) -> np.ndarray:
    """Line segments (k, 2, 3) worth drawing: region borders, creases over ``angle``, open edges."""
    if not len(T):
        return np.zeros((0, 2, 3))
    N, A = tri_normals(P, T)
    e, tris, count = edge_table(T)
    a, b = tris[:, 0], tris[:, 1]
    draw = count != 2
    two = ~draw
    deg = _degenerate(A)
    draw[two] = (np.einsum("ij,ij->i", N[a[two]], N[b[two]]) < math.cos(math.radians(angle))) \
        & ~deg[a[two]] & ~deg[b[two]]
    if F is not None:
        F = np.asarray(F)
        draw[two] |= F[a[two]] != F[b[two]]
    return P[e[draw]]


def shaded(P: np.ndarray, T: np.ndarray, F: np.ndarray | None = None, angle: float = CREASE):
    """For drawing: vertices split at creases and region borders, with smooth normals elsewhere:
    (positions (n, 3), normals (n, 3), triangles (m, 3)) with the same triangle order."""
    P = np.asarray(P, dtype=np.float64)
    T = np.asarray(T, dtype=np.int64)
    if not len(T):
        return P[:0], P[:0], T
    N, A = tri_normals(P, T)
    F = np.zeros(len(T), np.int64) if F is None else np.asarray(F, dtype=np.int64)
    corner_v = T.reshape(-1)
    corner_t = np.repeat(np.arange(len(T)), 3)
    order = np.argsort(corner_v, kind="stable")
    cv, ct = corner_v[order], corner_t[order]
    starts = np.flatnonzero(np.r_[True, cv[1:] != cv[:-1]])
    k = np.diff(np.r_[starts, len(cv)])  # corners per vertex
    # every pair (i, j) of corners at the same vertex: k² pairs per vertex
    rep = np.repeat(np.arange(len(starts)), k * k)
    off = np.arange(len(rep)) - np.repeat(np.cumsum(k * k) - k * k, k * k)
    kk = k[rep]
    i = starts[rep] + off // kk
    j = starts[rep] + off % kk
    ti, tj = ct[i], ct[j]
    near = (F[ti] == F[tj]) & (np.einsum("ij,ij->i", N[ti], N[tj]) > math.cos(math.radians(angle)))
    acc = np.zeros((len(cv), 3))
    w = (N[tj] * A[tj, None])[near]
    np.add.at(acc, i[near], w)
    acc /= np.maximum(np.linalg.norm(acc, axis=1), 1e-300)[:, None]
    corner_n = np.empty((len(corner_v), 3))
    corner_n[order] = acc
    # one vertex per (vertex, normal): corners with the same smoothed normal share it
    q = np.round(corner_n * 4096).astype(np.int64)
    key = np.stack([corner_v, q[:, 0], q[:, 1], q[:, 2]], 1)
    uniq, first, inv = np.unique(key, axis=0, return_index=True, return_inverse=True)
    return P[corner_v[first]], corner_n[first], inv.reshape(-1, 3)


def read_stl(path: str) -> tuple[np.ndarray, np.ndarray | None, bytes]:
    """(triangles (n, 3, 3) float32, attribute words or None, header) from a binary or ASCII STL."""
    with open(path, "rb") as f:
        data = f.read()
    if len(data) >= 84:
        n = struct.unpack_from("<I", data, 80)[0]
        if 84 + 50 * n == len(data):
            rec = np.frombuffer(data, dtype=np.dtype([("n", "<f4", 3), ("v", "<f4", (3, 3)), ("a", "<u2")]), count=n, offset=84)
            return rec["v"].astype(np.float32), rec["a"].copy(), data[:80]
    pts = [list(map(float, line.split()[1:4])) for line in data.decode(errors="replace").splitlines()
           if line.strip().startswith("vertex")]
    return np.asarray(pts, dtype=np.float32).reshape(-1, 3, 3), None, b""


REGION_HEADER = b"tule mesh: region id per triangle in the attribute word"


_PLY_TYPES = {"char": "i1", "int8": "i1", "uchar": "u1", "uint8": "u1", "short": "i2", "int16": "i2",
              "ushort": "u2", "uint16": "u2", "int": "i4", "int32": "i4", "uint": "u4", "uint32": "u4",
              "float": "f4", "float32": "f4", "double": "f8", "float64": "f8"}


def write_ply(path: str, P, T, F=None, meta: dict | None = None) -> str:
    """Binary PLY: shared float32 vertices, triangles with a ``region`` per face, and ``meta`` (name,
    color, tags, history) as one JSON comment line in the header: one self-contained file."""
    import json

    from .io import write_atomic

    P = np.asarray(P, dtype="<f4").reshape(-1, 3)
    T = np.asarray(T, dtype="<i4").reshape(-1, 3)
    head = ["ply", "format binary_little_endian 1.0", "comment made by tule"]
    if meta:
        head.append("comment tule " + json.dumps(meta, separators=(",", ":")))
    head += [f"element vertex {len(P)}", "property float x", "property float y", "property float z",
             f"element face {len(T)}", "property list uchar int vertex_indices"]
    face = [("n", "u1"), ("v", "<i4", (3,))]
    if F is not None:
        head.append("property int region")
        face.append(("region", "<i4"))
    head.append("end_header")
    rec = np.zeros(len(T), dtype=face)
    rec["n"], rec["v"] = 3, T
    if F is not None:
        rec["region"] = np.asarray(F, dtype=np.int64)
    return write_atomic(path, ("\n".join(head) + "\n").encode() + P.tobytes() + rec.tobytes())


def read_ply(path: str):
    """(points, triangles, regions or None, meta dict) from a PLY, binary or ASCII; polygons are
    split into fans; extra properties (normals, colors) are skipped."""
    import json

    with open(path, "rb") as f:
        data = f.read()
    end = data.find(b"end_header")
    if not data.startswith(b"ply") or end < 0:
        raise TuleError(f"{os.path.basename(path)} is not a PLY file", op="Mesh.load")
    body = data[data.index(b"\n", end) + 1:]
    fmt, meta, elements = "ascii", {}, []
    for line in data[:end].decode("latin-1").splitlines():
        w = line.split()
        if not w:
            continue
        if w[0] == "format":
            fmt = w[1]
        elif w[0] == "comment" and len(w) > 1 and w[1] == "tule":
            try:
                meta = json.loads(line.split("tule", 1)[1])
            except ValueError:
                meta = {}
        elif w[0] == "element":
            elements.append((w[1], int(w[2]), []))
        elif w[0] == "property" and elements:
            elements[-1][2].append(("list", w[2], w[3], w[4]) if w[1] == "list" else (w[1], w[2]))
    if fmt == "binary_big_endian":
        raise TuleError("big-endian PLY", op="Mesh.load", hint="save it as binary little-endian or ASCII PLY")
    P, T, F = np.zeros((0, 3)), np.zeros((0, 3), np.int64), None
    tokens = iter(body.split()) if fmt == "ascii" else None
    pos = 0
    for name, n, props in elements:
        fixed = all(p[0] != "list" for p in props)
        lists = [p for p in props if p[0] == "list"]
        if fmt != "ascii" and (fixed or (len(lists) == 1 and props[0][0] == "list" or True)):
            # try a fixed layout first: every face a triangle (what nearly every writer makes)
            dt = []
            for i, p in enumerate(props):
                if p[0] == "list":
                    dt += [(f"n{i}", "<" + _PLY_TYPES[p[1]]), (f"p{i}", "<" + _PLY_TYPES[p[2]], (3,))]
                else:
                    dt.append((p[1], "<" + _PLY_TYPES[p[0]]))
            dt = np.dtype(dt)
            rec = np.frombuffer(body, dtype=dt, count=n, offset=pos) if n else np.zeros(0, dt)
            ok = all((rec[f"n{i}"] == 3).all() for i, p in enumerate(props) if p[0] == "list")
            if ok:
                pos += n * dt.itemsize
                cols = {p[3] if p[0] == "list" else p[1]: rec[f"p{i}"] if p[0] == "list" else rec[p[1]]
                        for i, p in enumerate(props)}
            else:  # polygons: walk the records
                cols, pos = _ply_walk(body, pos, n, props)
        else:
            cols = _ply_ascii(tokens, n, props)
        if name == "vertex":
            P = np.stack([np.asarray(cols[k], dtype=np.float64) for k in "xyz"], 1)
        elif name == "face":
            faces = cols.get("vertex_indices", cols.get("vertex_index"))
            T, keep = _fans(faces)
            if "region" in cols:
                F = np.asarray(cols["region"], dtype=np.int64)[keep]
    return P, T, F, meta


def _fans(faces):
    """Triangles from faces given as an (n, 3) array or a list of index lists; and which face each came from."""
    if isinstance(faces, np.ndarray) and faces.ndim == 2:
        return faces.astype(np.int64), np.arange(len(faces))
    tris, src = [], []
    for i, f in enumerate(faces):
        for k in range(1, len(f) - 1):
            tris.append((f[0], f[k], f[k + 1]))
            src.append(i)
    return np.asarray(tris, dtype=np.int64).reshape(-1, 3), np.asarray(src, dtype=np.int64)


def _ply_walk(body, pos, n, props):
    cols = {(p[3] if p[0] == "list" else p[1]): [] for p in props}
    for _ in range(n):
        for p in props:
            if p[0] == "list":
                c = np.frombuffer(body, "<" + _PLY_TYPES[p[1]], 1, pos)[0]
                pos += np.dtype(_PLY_TYPES[p[1]]).itemsize
                cols[p[3]].append(np.frombuffer(body, "<" + _PLY_TYPES[p[2]], int(c), pos).tolist())
                pos += int(c) * np.dtype(_PLY_TYPES[p[2]]).itemsize
            else:
                cols[p[1]].append(np.frombuffer(body, "<" + _PLY_TYPES[p[0]], 1, pos)[0])
                pos += np.dtype(_PLY_TYPES[p[0]]).itemsize
    return cols, pos


def _ply_ascii(tokens, n, props):
    cols = {(p[3] if p[0] == "list" else p[1]): [] for p in props}
    for _ in range(n):
        for p in props:
            if p[0] == "list":
                c = int(next(tokens))
                cols[p[3]].append([int(next(tokens)) for _ in range(c)])
            else:
                cols[p[1]].append(float(next(tokens)))
    return cols


def write_stl(path: str, P, T, F=None) -> str:
    """A binary STL; with ``F`` each triangle's region id goes in its attribute word (slicers ignore it)."""
    from .io import write_atomic

    P = np.asarray(P, dtype=np.float64).reshape(-1, 3)
    I = np.asarray(T, dtype=np.int64).reshape(-1, 3)
    tri = P[I]
    n, _ = tri_normals(P, I)
    rec = np.zeros(len(I), dtype=[("n", "<f4", 3), ("v", "<f4", (3, 3)), ("a", "<u2")])
    rec["n"] = n
    rec["v"] = tri
    head = b"tule"
    if F is not None and len(I):
        F = np.asarray(F, dtype=np.int64)
        if F.max(initial=0) < 32768:  # bit 15 clear: never read as a color
            rec["a"] = F
            head = REGION_HEADER
    data = head.ljust(80, b" ") + struct.pack("<I", len(I)) + rec.tobytes()
    return write_atomic(path, data)


def _read_obj(path: str) -> tuple[np.ndarray, np.ndarray]:
    V, T = [], []
    with open(path, errors="replace") as fh:
        for line in fh:
            if line.startswith("v "):
                V.append([float(x) for x in line.split()[1:4]])
            elif line.startswith("f "):
                idx = [int(w.split("/")[0]) for w in line.split()[1:]]
                idx = [i - 1 if i > 0 else len(V) + i for i in idx]
                for k in range(1, len(idx) - 1):  # fan
                    T.append([idx[0], idx[k], idx[k + 1]])
    return np.asarray(V, dtype=np.float64).reshape(-1, 3), np.asarray(T, dtype=np.int64).reshape(-1, 3)


# ---------------------------------------------------------------- selections


class MeshFaces:
    """A selection of a mesh's triangles, with the B-rep ``Faces`` vocabulary where it makes sense.

    ``mesh.faces["load"]`` (a tag), ``mesh.faces[3]`` (region 3), ``.top()``, ``.bottom()``,
    ``.normal(dir)``, ``.within(lo, hi)``, ``.nearest(point)``, ``.where(fn)``, ``.largest()``;
    combine with ``|``, ``&``, ``-``. ``ids`` are the regions it touches."""

    kind = "face"

    def __init__(self, owner: "Mesh", mask: np.ndarray, keys: tuple = ()):
        self.owner = owner
        self.mask = np.asarray(mask, dtype=bool)
        self.keys = tuple(keys)  # tag names it came from: found again in meshes derived from the owner

    # basics
    def __len__(self) -> int:
        return int(self.mask.sum())

    def __bool__(self) -> bool:
        return bool(self.mask.any())

    def __repr__(self) -> str:
        return f"<MeshFaces: {len(self)} triangles in regions {self.ids[:12]}{' …' if len(self.ids) > 12 else ''}>"

    def _new(self, mask, keys=()) -> "MeshFaces":
        return MeshFaces(self.owner, mask, keys)

    def _same(self, other) -> "MeshFaces":
        if not isinstance(other, MeshFaces):
            raise TuleError(f"cannot combine mesh faces with {type(other).__name__}", op="select")
        return other if other.owner is self.owner else self.owner._resolve(other)

    def __or__(self, other):
        o = self._same(other)
        return self._new(self.mask | o.mask, self.keys + o.keys if self.keys and o.keys else ())

    def __and__(self, other):
        return self._new(self.mask & self._same(other).mask)

    def __sub__(self, other):
        return self._new(self.mask & ~self._same(other).mask)

    # facts
    @property
    def ids(self) -> list[int]:
        return sorted(set(np.unique(self.owner.face_ids[self.mask]).tolist()))

    @property
    def area(self) -> float:
        return float(self.owner._tri()[1][self.mask].sum())

    @property
    def center(self) -> Vec:
        c, a = self.owner._centers(), self.owner._tri()[1]
        w = a[self.mask]
        return Vec(*(c[self.mask] * w[:, None]).sum(0) / max(w.sum(), 1e-300))

    @property
    def normal_mean(self) -> Vec:
        """The area-weighted mean normal (a flat patch's normal)."""
        n, a = self.owner._tri()
        v = (n[self.mask] * a[self.mask, None]).sum(0)
        return Vec(*(v / max(np.linalg.norm(v), 1e-300)))

    # queries (each keeps a subset of this selection)
    def where(self, fn: Callable) -> "MeshFaces":
        """Triangles for which ``fn(centers (k, 3), normals (k, 3))`` is True (a bool array)."""
        idx = np.flatnonzero(self.mask)
        keep = np.asarray(fn(self.owner._centers()[idx], self.owner._tri()[0][idx]), dtype=bool).reshape(-1)
        if len(keep) != len(idx):
            raise TuleError("where(fn): fn must return one bool per triangle", op="select",
                            hint="lambda c, n: c[:, 2] > 10   (c: centers, n: normals, both (k, 3))")
        m = np.zeros_like(self.mask)
        m[idx[keep]] = True
        return self._new(m)

    def top(self, axis: VecLike = (0, 0, 1), tol: float | None = None) -> "MeshFaces":
        """Triangles facing ``axis`` (within 60°) within ``tol`` of the highest point along it
        (default: 0.1 % of the mesh's size)."""
        a = np.array(vec(axis), dtype=np.float64)
        a /= max(np.linalg.norm(a), 1e-300)
        idx = np.flatnonzero(self.mask)
        if not len(idx):
            return self
        P, T = self.owner._P, self.owner._T
        h = (P[T[idx]] @ a).max(1)  # each triangle's highest corner
        tol = 1e-3 * self.owner._diag() if tol is None else float(tol)
        lo = (P[T[idx]] @ a).min(1)
        n = self.owner._tri()[0][idx] @ a
        keep = (lo >= h.max() - tol) & (n > 0.5)
        if not keep.any():  # a rounded top: the triangles nearest the top
            keep = h >= h.max() - tol
        m = np.zeros_like(self.mask)
        m[idx[keep]] = True
        return self._new(m)

    def bottom(self, axis: VecLike = (0, 0, 1), tol: float | None = None) -> "MeshFaces":
        return self.top(-vec(axis), tol)

    def normal(self, direction: VecLike, tol_deg: float = 5.0) -> "MeshFaces":
        """Triangles whose normal is within ``tol_deg`` of ``direction``."""
        d = np.array(vec(direction), dtype=np.float64)
        d /= max(np.linalg.norm(d), 1e-300)
        c = math.cos(math.radians(tol_deg))
        return self.where(lambda _, n: n @ d >= c)

    def within(self, lo: VecLike, hi: VecLike) -> "MeshFaces":
        """Triangles whose centers are in the box ``lo``–``hi``."""
        lo, hi = np.array(vec(lo)), np.array(vec(hi))
        return self.where(lambda c, _: np.all((c >= lo) & (c <= hi), axis=1))

    def nearest(self, point: VecLike, n: int = 1) -> "MeshFaces":
        """The ``n`` regions nearest ``point`` (whole regions, like picking a face)."""
        idx = np.flatnonzero(self.mask)
        if not len(idx):
            return self
        d = np.linalg.norm(self.owner._centers()[idx] - np.array(vec(point)), axis=1)
        regions = []
        for t in idx[np.argsort(d)]:
            r = int(self.owner.face_ids[t])
            if r not in regions:
                regions.append(r)
                if len(regions) == n:
                    break
        return self._new(self.mask & np.isin(self.owner.face_ids, regions))

    def largest(self, n: int = 1) -> "MeshFaces":
        """The ``n`` largest regions (by the area selected in each)."""
        F = self.owner.face_ids[self.mask].astype(np.int64)
        if not len(F):
            return self
        area = np.bincount(F, weights=self.owner._tri()[1][self.mask])
        keep = np.argsort(-area)[:n]
        return self._new(self.mask & np.isin(self.owner.face_ids, keep))

    def regions(self) -> "MeshFaces":
        """Grown to whole regions: every region this selection touches."""
        return self._new(np.isin(self.owner.face_ids, self.ids), self.keys)


# ---------------------------------------------------------------- the Mesh


def _f32(a):
    return np.ascontiguousarray(a, dtype=np.float32)


class Mesh:
    """A triangle-mesh part: vertices, triangles, a region per triangle, tags, name and color.

    ``Mesh(points, triangles)`` (optionally ``face_ids=`` regions and ``tags=``), ``Mesh.load(path)``
    (STL, OBJ), ``part.to_mesh(tolerance)`` (from a B-rep) or ``body.to_mesh(cell=…)`` (from an implicit).
    Meshes from ``tessellate`` (for drawing) keep each B-rep face's own vertices; booleans weld them.
    """

    kind = "mesh"

    def __init__(self, *args, face_ids=None, edges=None, normals=None, tags=None, name: str | None = None,
                 color: str | None = None, history: Sequence[str] = (), welded: bool | None = None, **kw):
        # Mesh(points, triangles[, face_ids]) or, as tessellate builds it, Mesh(positions, normals, indices, face_ids, edges)
        args = list(args)
        if "positions" in kw or "indices" in kw:
            P, T = kw.pop("positions"), kw.pop("indices")
            normals = kw.pop("normals", normals)
            face_ids = kw.pop("face_ids", face_ids)
        elif len(args) >= 3 and np.asarray(args[1]).dtype.kind == "f":
            P, normals, T = args[0], args[1], args[2]
            face_ids = args[3] if len(args) > 3 else face_ids
            edges = args[4] if len(args) > 4 else edges
        else:
            P = kw.pop("points", args[0] if args else None)
            T = kw.pop("triangles", args[1] if len(args) > 1 else None)
            if len(args) > 2:
                face_ids = args[2]
        if kw:
            raise TypeError(f"Mesh() got unexpected arguments {sorted(kw)}")
        if P is None or T is None:
            raise TuleError("a Mesh needs points and triangles", op="Mesh", hint="Mesh(points (n, 3), triangles (m, 3))")
        self._P = np.asarray(P, dtype=np.float64).reshape(-1, 3)
        self._T = np.asarray(T, dtype=np.int64).reshape(-1, 3)
        if len(self._T) and (self._T.min() < 0 or self._T.max() >= len(self._P)):
            raise TuleError("triangle indices out of range", op="Mesh", hint=f"0 to {len(self._P) - 1}")
        self._F = None if face_ids is None else np.asarray(face_ids, dtype=np.uint32).reshape(-1)
        if self._F is not None and len(self._F) != len(self._T):
            if len(self._F) == 0:
                self._F = None
            else:
                raise TuleError(f"face_ids has {len(self._F)} entries for {len(self._T)} triangles", op="Mesh")
        self._N = None if normals is None else np.asarray(normals, dtype=np.float32).reshape(-1, 3)
        self._edges = edges
        self._tags: dict[str, frozenset] = {k: frozenset(int(i) for i in v) for k, v in (tags or {}).items()}
        self.name = name
        self.color = color
        self.history: tuple[str, ...] = tuple(history)
        self._welded_flag = welded
        self._cache: dict = {}

    # ------------------------------------------------------------ the arrays (and the older tessellation API)
    @property
    def positions(self) -> np.ndarray:
        """Vertex positions, float32 (n, 3)."""
        if "p32" not in self._cache:
            self._cache["p32"] = _f32(self._P)
        return self._cache["p32"]

    @positions.setter
    def positions(self, value) -> None:
        """Move the vertices in place (same count); everything measured from them is recomputed."""
        P = np.asarray(value, dtype=np.float64).reshape(-1, 3)
        if P.shape != self._P.shape:
            raise TuleError("positions: the same number of vertices", op="Mesh", hint=f"({len(self._P)}, 3)")
        self._P, self._cache = P, {}

    @property
    def points(self) -> np.ndarray:
        """Vertex positions, float64 (n, 3)."""
        return self._P

    @property
    def indices(self) -> np.ndarray:
        """Triangles as vertex indices, uint32 (m, 3)."""
        if "t32" not in self._cache:
            self._cache["t32"] = np.ascontiguousarray(self._T, dtype=np.uint32)
        return self._cache["t32"]

    @property
    def normals(self) -> np.ndarray:
        """A normal per vertex (float32): the surface's (from a B-rep), else area-weighted from the triangles."""
        if self._N is None or len(self._N) != len(self._P):
            n, a = self._tri()
            N = np.zeros_like(self._P)
            for c in range(3):
                np.add.at(N, self._T[:, c], n * a[:, None])
            self._N = _f32(N / np.maximum(np.linalg.norm(N, axis=1), 1e-300)[:, None])
        return self._N

    @property
    def face_ids(self) -> np.ndarray:
        """A region id per triangle (uint32): B-rep face ids for a mesh of a B-rep, else regions split at creases."""
        if self._F is None:
            P, T, keep = self._weld_arrays()
            F = np.zeros(len(self._T), np.uint32)
            if len(keep):
                F[keep] = crease_regions(P, T)
            self._F = F
        return self._F

    @property
    def edges(self) -> list:
        """Lines worth drawing: [(id, (k, 3) float32 polyline)]: the B-rep's edges for a tessellation,
        else region borders and creases (never every triangle edge)."""
        if self._edges is None:
            P, T, keep = self._weld_arrays()
            segs = feature_edges(P, T, self.face_ids[keep])
            self._edges = [(i, s.astype(np.float32)) for i, s in enumerate(segs)]
        return self._edges

    @property
    def triangles(self) -> int:
        """How many triangles (``indices`` holds them)."""
        return int(len(self._T))

    def tessellate(self, tolerance: float = 0.05, angular: float = 0.2) -> "Mesh":
        """A mesh is its own tessellation (for code written for B-rep parts)."""
        return self

    # ------------------------------------------------------------ derived facts
    def _tri(self):
        if "tri" not in self._cache:
            self._cache["tri"] = tri_normals(self._P, self._T)
        return self._cache["tri"]

    def _centers(self) -> np.ndarray:
        if "cen" not in self._cache:
            self._cache["cen"] = self._P[self._T].mean(1) if len(self._T) else np.zeros((0, 3))
        return self._cache["cen"]

    def _diag(self) -> float:
        if not len(self._P):
            return 1.0
        return float(np.linalg.norm(self._P.max(0) - self._P.min(0))) or 1.0

    def _weld_arrays(self):
        """(P, T, kept triangles) with coincident vertices merged (tessellations keep a face's own)."""
        if "weld" not in self._cache:
            if self._welded_flag:
                self._cache["weld"] = (self._P, self._T, np.arange(len(self._T)))
            else:
                self._cache["weld"] = weld(self._P, self._T, 1e-7 * self._diag())
        return self._cache["weld"]

    def _manifold(self):
        """This mesh as a manifold3d Manifold (for ray casts; built once)."""
        if "manifold" not in self._cache:
            self._cache["manifold"] = _to_manifold([self])[0]
        return self._cache["manifold"]

    @property
    def is_closed(self) -> bool:
        """Watertight: every edge has as many triangles running one way along it as the other. Usually
        two; four where two solids touch along an edge (an STL cannot keep their vertices apart)."""
        P, T, _ = self._weld_arrays()
        if not len(T):
            return False
        e = np.concatenate([T[:, [0, 1]], T[:, [1, 2]], T[:, [2, 0]]]).astype(np.int64)
        n = int(e.max()) + 1
        fwd = np.where(e[:, 0] < e[:, 1], e[:, 0] * n + e[:, 1], e[:, 1] * n + e[:, 0])
        sign = np.where(e[:, 0] < e[:, 1], 1, -1)
        keys, inv = np.unique(fwd, return_inverse=True)
        return bool((np.bincount(inv.reshape(-1), weights=sign, minlength=len(keys)) == 0).all())

    @property
    def volume(self) -> float:
        if not len(self._T):
            return 0.0
        t = self._P[self._T]
        return float(np.einsum("ij,ij->i", t[:, 0], np.cross(t[:, 1], t[:, 2])).sum() / 6)

    @property
    def area(self) -> float:
        return float(self._tri()[1].sum())

    @property
    def bounds(self) -> tuple[Vec, Vec]:
        if not len(self._P):
            raise TuleError("the mesh is empty", op="bounds")
        used = self._P[np.unique(self._T)] if len(self._T) else self._P
        return Vec(*used.min(0)), Vec(*used.max(0))

    @property
    def size(self) -> Vec:
        lo, hi = self.bounds
        return hi - lo

    @property
    def center(self) -> Vec:
        lo, hi = self.bounds
        return (lo + hi) * 0.5

    @property
    def center_of_mass(self) -> Vec:
        t = self._P[self._T]
        v = np.einsum("ij,ij->i", t[:, 0], np.cross(t[:, 1], t[:, 2])) / 6
        c = (t.sum(1) / 4 * v[:, None]).sum(0) / max(v.sum(), 1e-300)
        return Vec(*c)

    def mass(self, density: float) -> float:
        """Mass in g for ``density`` in g/cm³."""
        return self.volume * density / 1000

    @property
    def regions(self) -> int:
        return int(self.face_ids.max()) + 1 if len(self._T) else 0

    def _region_areas(self) -> np.ndarray:
        return np.bincount(self.face_ids.astype(np.int64), weights=self._tri()[1], minlength=self.regions)

    # ------------------------------------------------------------ naming, tags, selections
    def _derive(self, P=None, T=None, F=None, tags=None, history=None, name=..., color=..., N=None, edges=None, welded=None):
        return Mesh(self._P if P is None else P, self._T if T is None else T,
                    face_ids=self._F if F is None and P is None and T is None else F,
                    normals=N, edges=edges, tags=self._tags if tags is None else tags,
                    name=self.name if name is ... else name, color=self.color if color is ... else color,
                    history=self.history if history is None else history,
                    welded=self._welded_flag if welded is None else welded)

    def named(self, name: str, color: str | None = None) -> "Mesh":
        """The same mesh with a part name and (optionally) a color like ``"#4a8"``."""
        if color is not None:
            from .io import parse_rgba
            parse_rgba(color)
        out = Mesh(self._P, self._T, face_ids=self._F, normals=self._N, edges=self._edges, tags=self._tags,
                   name=name, color=color or self.color, history=self.history, welded=self._welded_flag)
        out._cache = self._cache
        return out

    def colored(self, color: str) -> "Mesh":
        return self.named(self.name, color)

    @property
    def tags(self) -> list[str]:
        return sorted(self._tags)

    @property
    def faces(self) -> MeshFaces:
        """Every triangle, to select from: ``mesh.faces.top()``, ``mesh.faces["load"]``."""
        return _AllFaces(self, np.ones(len(self._T), dtype=bool))

    def face(self, i: int) -> MeshFaces:
        """Region ``i`` (as the viewer shows it: ``f12``)."""
        if not isinstance(i, (int, np.integer)) or not 0 <= i < self.regions:
            raise TuleError(f"no region {i!r}: this mesh has regions 0 to {self.regions - 1}", op="select")
        return MeshFaces(self, self.face_ids == i)

    def _tagged(self, name: str) -> MeshFaces:
        if name not in self._tags:
            raise TuleError(f"this mesh has no tag {name!r}", op="select",
                            hint=f"tags: {', '.join(self.tags) or 'none'}; mesh = mesh.tag({name!r}, mesh.faces...)")
        return MeshFaces(self, np.isin(self.face_ids, list(self._tags[name])), (name,))

    def _resolve(self, sel, kind: str | None = None) -> MeshFaces:
        """Find a selection made on another mesh (an earlier version of this one) in this one."""
        if isinstance(sel, MeshFaces):
            if sel.owner is self:
                return sel
            if sel.keys and all(k in self._tags for k in sel.keys):
                out = self._tagged(sel.keys[0])
                for k in sel.keys[1:]:
                    out = out | self._tagged(k)
                return out
            if len(sel.mask) == len(self._T):  # the same triangles (named, moved, retagged)
                return MeshFaces(self, sel.mask, sel.keys)
            raise TuleError("this selection belongs to another mesh and cannot be found here", op="select",
                            hint="tag it before the operation that changed the triangles (tags survive Booleans)")
        raise TuleError(f"a mesh's faces are selected with mesh.faces, not {type(sel).__name__}", op="select",
                        hint="mesh.faces.top(), mesh.faces['tag'], mesh.faces.within(lo, hi)")

    def _split(self, mask: np.ndarray) -> "Mesh":
        """Regions split so ``mask`` covers whole regions (tags of a split region keep both halves)."""
        F = self.face_ids.astype(np.int64).copy()
        mask = np.asarray(mask, dtype=bool)
        touched = np.unique(F[mask])
        whole = np.unique(F[~mask])
        partial = np.intersect1d(touched, whole)
        if not len(partial):
            return self
        nxt = int(F.max(initial=-1)) + 1
        new_of = {}
        for r in partial.tolist():
            new_of[r] = nxt
            F[mask & (F == r)] = nxt
            nxt += 1
        tags = {k: frozenset(set(v) | {new_of[r] for r in v if r in new_of}) for k, v in self._tags.items()}
        out = self._derive(F=F.astype(np.uint32), tags=tags)
        out._P, out._T = self._P, self._T
        out._cache = {k: v for k, v in self._cache.items() if k in ("tri", "cen", "weld", "p32", "t32")}
        out._N = self._N
        return out

    def tag(self, name: str, selection) -> "Mesh":
        """Name a selection (``mesh.faces.top()``, or a function of ``mesh.faces``) so FEA, overlays and
        later meshes find it. Regions it cuts through are split, so a tag is always whole regions."""
        sel = selection(self.faces) if callable(selection) and not isinstance(selection, MeshFaces) else selection
        sel = self._resolve(sel)
        if not sel:
            raise TuleError(f"tag {name!r}: the selection is empty", op="tag",
                            hint="check it: len(mesh.faces.top()) etc.; a larger tol= or a wider box")
        out = self._split(sel.mask)
        regions = set(np.unique(out.face_ids[sel.mask]).tolist())
        tags = dict(out._tags)
        tags[name] = frozenset(set(tags.get(name, ())) | regions)
        out._tags = tags
        return out

    # ------------------------------------------------------------ transforms
    def _moved(self, M: np.ndarray) -> "Mesh":
        M = np.asarray(M, dtype=np.float64)
        P = self._P @ M[:3, :3].T + M[:3, 3]
        flip = np.linalg.det(M[:3, :3]) < 0
        T = self._T[:, ::-1] if flip else self._T
        N = None
        if self._N is not None and not flip and abs(abs(np.linalg.det(M[:3, :3])) - 1) < 1e-9:
            N = _f32(self._N @ M[:3, :3].T)
        out = Mesh(P, T, face_ids=self._F, normals=N, tags=self._tags, name=self.name, color=self.color,
                   history=self.history, welded=self._welded_flag)
        return out

    def at(self, x: float | VecLike = 0, y: float = 0, z: float = 0) -> "Mesh":
        v = vec(x) if not isinstance(x, (int, float)) else Vec(x, y, z)
        M = np.eye(4)
        M[:3, 3] = tuple(v)
        return self._moved(M)

    translate = at

    def rotate(self, x: float = 0, y: float = 0, z: float = 0, about: VecLike = (0, 0, 0)) -> "Mesh":
        """Rotate by degrees about x, then y, then z (as ``Shape.rotate``)."""
        R = np.eye(3)
        for ax, deg in ((0, x), (1, y), (2, z)):
            if deg:
                R = _axis_matrix(np.eye(3)[ax], deg) @ R
        return self._about(R, about)

    def rotate_about(self, axis_dir: VecLike, degrees: float, origin: VecLike = (0, 0, 0)) -> "Mesh":
        return self._about(_axis_matrix(np.array(vec(axis_dir)), degrees), origin)

    def scale(self, x: float, y: float | None = None, z: float | None = None, about: VecLike = (0, 0, 0)) -> "Mesh":
        y = x if y is None else y
        z = x if z is None else z
        return self._about(np.diag([x, y, z]).astype(float), about)

    def mirror(self, nx: float | VecLike = 1, ny: float = 0, nz: float = 0, origin: VecLike = (0, 0, 0)) -> "Mesh":
        n = np.array(vec(nx) if not isinstance(nx, (int, float)) else (nx, ny, nz), dtype=np.float64)
        n /= max(np.linalg.norm(n), 1e-300)
        return self._about(np.eye(3) - 2 * np.outer(n, n), origin)

    def transform(self, matrix) -> "Mesh":
        """A 4×4 (or 3×4) affine matrix."""
        M = np.eye(4)
        A = np.asarray(matrix, dtype=np.float64)
        M[: A.shape[0], : A.shape[1]] = A
        return self._moved(M)

    def _about(self, R: np.ndarray, about) -> "Mesh":
        c = np.array(vec(about), dtype=np.float64)
        M = np.eye(4)
        M[:3, :3] = R
        M[:3, 3] = c - R @ c
        return self._moved(M)

    # ------------------------------------------------------------ Booleans (manifold3d)
    def __or__(self, other):
        return mesh_boolean("union", [self, other])

    __add__ = __or__

    def __radd__(self, other):
        if other == 0:
            return self
        return mesh_boolean("union", [other, self])

    def __sub__(self, other):
        return mesh_boolean("difference", [self, other])

    def __and__(self, other):
        return mesh_boolean("intersection", [self, other])

    # ------------------------------------------------------------ mesh operations
    def simplify(self, tolerance: float) -> "Mesh":
        """Fewer triangles: edges collapsed where the surface moves less than ``tolerance`` mm
        (manifold3d; region borders kept)."""
        man, table = _to_manifold([self])
        out = _from_manifold(man.simplify(float(tolerance)), table, [self], name=self.name, color=self.color)
        out.history = self.history + (f"simplified within {tolerance:g} mm ({self.triangles:,} → {out.triangles:,} triangles)",)
        return out

    def refine(self, edge: float) -> "Mesh":
        """Triangles split until no edge is longer than ``edge`` mm (same shape, finer)."""
        man, table = _to_manifold([self])
        out = _from_manifold(man.refine_to_length(float(edge)), table, [self], name=self.name, color=self.color)
        out.history = self.history + (f"refined to edges ≤ {edge:g} mm",)
        return out

    def pieces(self) -> list["Mesh"]:
        """The separate solids (connected pieces), largest first."""
        man, table = _to_manifold([self])
        parts = [_from_manifold(p, table, [self], name=self.name, color=self.color) for p in man.decompose()]
        return sorted(parts, key=lambda m: -abs(m.volume))

    def to_brep(self) -> "object":
        """A B-rep Shape with flat faces (coplanar triangles merged into one face). Exact for
        polyhedra; a curved mesh stays faceted. Up to 20,000 triangles (``simplify`` first)."""
        from .shape import Shape
        from .topopt import mesh_to_shape

        P, T, keep = self._weld_arrays()
        if len(T) > MAX_BREP_TRIANGLES:
            raise TuleError(f"{len(T):,} triangles is too many for a B-rep (one face each before merging)", op="to_brep",
                            hint=f"mesh.simplify(tolerance) to under {MAX_BREP_TRIANGLES:,} first, or keep it a mesh "
                                 "(meshes print, export, analyse and show like B-rep parts)")
        if not self.is_closed:
            raise TuleError("only a closed mesh makes a solid", op="to_brep", hint="mesh.is_closed; a Boolean with manifold3d repairs small gaps")
        s = mesh_to_shape(_Arrays(P, T))
        try:
            from OCP.ShapeUpgrade import ShapeUpgrade_UnifySameDomain

            u = ShapeUpgrade_UnifySameDomain(s.wrapped, True, True, True)
            u.Build()
            s = Shape(u.Shape())
        except Exception:  # noqa: BLE001 - the faceted solid is still right
            pass
        return s.named(self.name, self.color) if self.name or self.color else s

    def to_implicit(self, cell: float = 0.5, band: float | None = None):
        """A signed-distance implicit sampled on a grid of ``cell`` mm (trilinear between samples):
        loses detail under a cell, regions and tags."""
        from .sdf import from_mesh

        return from_mesh(self, cell, band)

    def to_mesh(self, *_, **__) -> "Mesh":
        return self

    # ------------------------------------------------------------ files
    @staticmethod
    def load(path: str, name: str | None = None) -> "Mesh":
        """Read a PLY, OBJ or STL. A PLY Tule wrote keeps its regions, name, color, tags and history;
        an STL Tule wrote keeps its regions."""
        path = os.fspath(path)
        ext = os.path.splitext(path)[1].lower()
        F, meta = None, {}
        if ext == ".ply":
            P, T, F, meta = read_ply(path)
            P, T, keep = weld(P, T, 0.0)
            F = F[keep] if F is not None else None
        elif ext == ".stl":
            tri, attr, head = read_stl(path)
            P0, T0 = tri.reshape(-1, 3), np.arange(3 * len(tri)).reshape(-1, 3)
            ours = attr is not None and head.startswith(REGION_HEADER)  # Tule wrote it: copies are exact
            P, T, keep = weld(P0, T0, 0.0 if ours else 1e-6 * max(1.0, float(np.abs(P0).max(initial=1))))
            if ours:
                F = attr[keep]
        elif ext == ".obj":
            P, T = _read_obj(path)
        else:
            raise TuleError(f"cannot read {ext or path!r} as a mesh", op="Mesh.load", hint="PLY, OBJ or STL; STEP: import_step")
        base = os.path.splitext(os.path.basename(path))[0]
        return Mesh(P, T, face_ids=F, tags=meta.get("tags"), name=name or meta.get("name") or base, color=meta.get("color"),
                    history=tuple(meta.get("history", ())) or (f"read from {os.path.basename(path)}",), welded=True)

    def _meta(self) -> dict:
        from .io import color_hex, parse_rgba

        return {k: v for k, v in (("name", self.name), ("color", self.color and color_hex(parse_rgba(self.color))),
                                  ("tags", {t: sorted(int(i) for i in r) for t, r in self._tags.items()}),
                                  ("history", list(self.history))) if v}

    def export_ply(self, path: str) -> str:
        """Write a binary PLY: shared vertices, a region per triangle, and the name, color, tags and
        history in its header (what ``Mesh.load`` reads back; Tule's own format for meshes)."""
        return write_ply(os.fspath(path), self._P, self._T, self.face_ids, self._meta())

    def export_stl(self, path: str) -> str:
        """Write a binary STL for a slicer (regions in the attribute words; no name, color or tags:
        ``export_ply`` or ``export_3mf`` keep those)."""
        return write_stl(os.fspath(path), self._P, self._T, self.face_ids)

    def export_3mf(self, path: str, **kw) -> str:
        from .io import export_3mf

        return export_3mf([self], path, **kw)

    def export_step(self, path: str) -> str:
        raise TuleError("a mesh has no exact surfaces for STEP", op="export_step",
                        hint="mesh.to_brep().export_step(path) for a faceted solid (up to 20,000 triangles), "
                             "or export_ply / export_3mf / export_stl")

    def __repr__(self) -> str:
        n = f" {self.name!r}" if self.name else ""
        closed = "closed" if self.is_closed else "open"
        tags = f", {len(self._tags)} tags" if self._tags else ""
        hist = f" ({'; '.join(self.history[-2:])})" if self.history else ""
        return f"<Mesh{n}: {self.triangles:,} triangles, {closed}, {self.regions} regions{tags}{hist}>"


class _AllFaces(MeshFaces):
    """``mesh.faces``: also indexed by tag or region id."""

    def __getitem__(self, key):
        if isinstance(key, str):
            return self.owner._tagged(key)
        if isinstance(key, (int, np.integer)):
            return self.owner.face(int(key))
        raise TuleError(f"mesh.faces[{key!r}]", op="select", hint="a tag name or a region id")


class _Arrays:
    """Positions and indices for ``topopt.mesh_to_shape``."""

    def __init__(self, P, T):
        self.positions, self.indices = P, T


def _axis_matrix(axis, degrees: float) -> np.ndarray:
    a = np.asarray(axis, dtype=np.float64)
    a = a / max(np.linalg.norm(a), 1e-300)
    t = math.radians(degrees)
    K = np.array([[0, -a[2], a[1]], [a[2], 0, -a[0]], [-a[1], a[0], 0]])
    return np.eye(3) + math.sin(t) * K + (1 - math.cos(t)) * K @ K


# ---------------------------------------------------------------- manifold3d


def _manifold3d():
    try:
        import manifold3d
    except ImportError:
        raise TuleError("mesh Booleans need manifold3d", op="mesh", hint="pip install manifold3d (it is in the Tule image)") from None
    return manifold3d


def _to_manifold(meshes: list[Mesh]):
    """Each mesh as a manifold3d Manifold with its regions as face ids; ``table`` maps each
    input's original id to its index in ``meshes``."""
    m3 = _manifold3d()
    mans, table = [], {}
    for k, m in enumerate(meshes):
        P, T, keep = m._weld_arrays()
        F = m.face_ids[keep]
        rid = int(m3.Manifold.reserve_ids(1))  # an original id of its own: face ids then survive (as_original resets them)
        man = m3.Manifold(m3.Mesh(vert_properties=np.array(P, dtype=np.float32), tri_verts=np.array(T, dtype=np.uint32),
                                  face_id=np.array(F, dtype=np.uint32), run_original_id=np.array([rid], dtype=np.uint32),
                                  run_index=np.array([0, 3 * len(T)], dtype=np.uint32)))
        st = man.status()
        if st != m3.Error.NoError:
            what = m.name or f"mesh {k + 1}"
            raise TuleError(f"{what} is not a closed manifold mesh ({str(st).split('.')[-1]})", op="mesh",
                            hint="Booleans need closed solids: check mesh.is_closed; meshes of B-reps and implicits are; "
                                 "an STL with holes needs repairing first")
        table[rid] = k
        mans.append(man)
    return (mans[0] if len(mans) == 1 else mans), table


def _from_manifold(man, table: dict, inputs: list[Mesh], name=None, color=None) -> Mesh:
    o = man.to_mesh()
    V = np.asarray(o.vert_properties, dtype=np.float64)[:, :3]
    T = np.asarray(o.tri_verts, dtype=np.int64).reshape(-1, 3)
    fid = np.asarray(o.face_id, dtype=np.int64) if o.face_id is not None and len(o.face_id) else np.zeros(len(T), np.int64)
    runs = np.asarray(o.run_index, dtype=np.int64)
    ids = np.asarray(o.run_original_id, dtype=np.int64)
    src = np.full(len(T), -1, dtype=np.int64)
    for r, oid in enumerate(ids):
        src[runs[r] // 3: runs[r + 1] // 3] = table.get(int(oid), -1)
    key = src * (1 << 32) + fid
    uniq, F = np.unique(key, return_inverse=True)
    F = F.reshape(-1)
    tags: dict[str, set] = {}
    for k, m in enumerate(inputs):
        for name_, regions in m._tags.items():
            want = np.isin(uniq, [k * (1 << 32) + r for r in regions])
            if want.any():
                tags.setdefault(name_, set()).update(np.flatnonzero(want).tolist())
    return Mesh(V, T, face_ids=F.astype(np.uint32), tags=tags, name=name, color=color, welded=True)


def mesh_boolean(op: str, meshes: list) -> Mesh:
    """``union``, ``difference`` or ``intersection`` of meshes, with manifold3d; regions and tags kept."""
    for m in meshes:
        if not isinstance(m, Mesh):
            raise _mixed(meshes[0], m, op)
    m3 = _manifold3d()
    mans, table = _to_manifold(list(meshes))
    mans = mans if isinstance(mans, list) else [mans]
    kind = {"union": m3.OpType.Add, "difference": m3.OpType.Subtract, "intersection": m3.OpType.Intersect}[op]
    res = m3.Manifold.batch_boolean(mans, kind)
    first = meshes[0]
    out = _from_manifold(res, table, list(meshes), first.name, first.color)
    others = ", ".join(o.name or "a mesh" for o in meshes[1:])
    out.history = first.history + (f"{op} with {others} (manifold3d)",)
    if not out.triangles:
        raise TuleError(f"the {op} is empty", op="mesh", hint="do the meshes overlap where you expect? check their bounds")
    return out


def _mixed(a, b, op: str) -> TuleError:
    ka, kb = getattr(a, "kind", type(a).__name__), getattr(b, "kind", type(b).__name__)
    how = {"brep": "part.to_mesh(tolerance=0.05)", "implicit": "body.to_mesh(cell=0.5)", "mesh": "the mesh"}
    return TuleError(f"cannot {op} a {ka} and a {kb}: the kinds differ", op=op,
                     hint=f"convert one explicitly so the loss is your choice, e.g. {how.get(ka, 'a')} {'|' if op == 'union' else '-'} "
                          f"{how.get(kb, 'b')} (both meshes), or mesh.to_implicit(cell=…) for smooth blends with implicits")
