"""Clearance through a motion: which parts overlap, touch or come close, and in which frame.

```python
r = clearance(frames, contacts=[("pin", "wheel")], below=0.5)   # Assemblies, or a written sequence
print(r)                                                       # a table, worst first
r.show("motion-clearance")                                     # the worst frame, marked, in the viewer
assert r.ok, r.clashes
```

Each distinct solid is meshed once (a part moved with ``.at`` or ``.rotate`` keeps its solid) and
placed per frame. Pairs whose boxes come within reach are measured mesh to mesh (FCL), and the
closest frames of each pair again on the B-rep: the gap between the faces the meshes put closest
(and any others their meshes allow to be closer), an overlap's volume by a Boolean. The reported
numbers are the B-rep's.
"""

from __future__ import annotations

import fnmatch
import json
import math
import os
import re
import time
from dataclasses import dataclass, field

import numpy as np
from OCP.gp import gp_Pnt, gp_Trsf
from OCP.TopLoc import TopLoc_Location

from ._occ import static, subshapes
from .errors import OCC_ERRORS, TuleError

TOL = 0.02  # mesh deflection asked for, mm; each face's mesh says how far it strays (``slack``)
MIN_VOLUME = 1e-3  # mm³ in common: less is touching
REFINE = 3  # frames per pair measured on the B-rep


def _fcl():
    try:
        import fcl
    except ImportError:
        raise TuleError("clearance checks need python-fcl (mesh distances)", op="clearance",
                        hint="pip install python-fcl") from None
    return fcl


# ---------------------------------------------------------------- placements

def _matrix(t: gp_Trsf) -> np.ndarray:
    return np.array([[t.Value(r, c) for c in (1, 2, 3, 4)] for r in (1, 2, 3)] + [[0.0, 0.0, 0.0, 1.0]])


def _trsf(m: np.ndarray) -> gp_Trsf:
    u, _, vt = np.linalg.svd(m[:3, :3])  # a written sequence rounds its matrices: back to a rotation
    r = u @ vt
    t = gp_Trsf()
    t.SetValues(*r[0], m[0, 3], *r[1], m[1, 3], *r[2], m[2, 3])
    return t


def _inverse(m: np.ndarray) -> np.ndarray:
    out = np.eye(4)
    out[:3, :3] = m[:3, :3].T
    out[:3, 3] = -m[:3, :3].T @ m[:3, 3]
    return out


def _split(s):
    """A placed shape as its solid (at the origin) and its placement."""
    return s.Located(TopLoc_Location()), _matrix(s.Location().Transformation())


def _frames(frames):
    """``(label, [(path, solid, placement)])`` per frame, from Assemblies, a Sequence or its folder."""
    from .assembly import Assembly
    from .outputs import Sequence

    if isinstance(frames, Sequence):
        files = {f: g for g, _, _, f, _ in frames._parts}
        yield from _placed(frames.frames, files.__getitem__)
        return
    if isinstance(frames, (str, os.PathLike)):
        yield from _written(str(frames))
        return
    if isinstance(frames, Assembly):
        frames = [frames]
    items = frames.items() if isinstance(frames, dict) else ((None, f) for f in frames)
    for label, f in items:
        if isinstance(f, (list, tuple)):
            f = Assembly(*f)
        if not isinstance(f, Assembly):
            raise TuleError(f"a frame is an Assembly or a list of parts, got {type(f).__name__}", op="clearance",
                            hint="clearance([Assembly(base, arm.rotate(z=a)) for a in range(0, 360, 5)])")
        yield label, [(p, *_split(s.wrapped)) for p, s in f.parts()]


def _written(path: str):
    from .io import import_step
    from .outputs import root

    d = path if os.path.isdir(path) else os.path.join(root(), path)
    try:
        with open(os.path.join(d, "output.json")) as f:
            meta = json.load(f)
    except (OSError, ValueError):
        raise TuleError(f"no sequence at {d}", op="clearance", hint="pass outputs/<name> of a sequence written with outputs.sequence") from None
    if meta.get("kind") != "sequence":
        raise TuleError(f"{d} is a {meta.get('kind')}, not a sequence", op="clearance")
    solids = {}

    def load(file):
        if file not in solids:
            solids[file] = import_step(os.path.join(d, file)).wrapped
        return solids[file]

    yield from _placed(meta["frames"], load)


def _placed(frames, load):
    for i, fr in enumerate(frames):
        if not fr.get("parts"):
            raise TuleError(f"frame {i} is one shape, not placed parts", op="clearance",
                            hint="add a list of parts (or an Assembly) per frame: seq.add([base, arm.rotate(z=a)])")
        parts = []
        for e in fr["parts"]:
            solid, m = _split(load(e["file"]))
            if e.get("transform"):
                m = np.array(e["transform"], dtype=float).reshape(4, 4) @ m
            parts.append((e["name"], solid, m))
        yield fr.get("label"), parts


# ---------------------------------------------------------------- solids

class _Solid:
    """One distinct solid at the origin: its mesh and FCL model, per-face models for the B-rep step."""

    def __init__(self, shape, fcl):
        self.shape = shape
        self.points, self.tris, self.face_of, self.defl = _mesh(shape)
        self.slack = 1.5 * self.defl.max(initial=0.0) + 5e-5  # how far a mesh gap may be off, from this side
        self.empty = not len(self.tris)
        if not self.empty:
            self.lo, self.hi = self.points.min(0), self.points.max(0)
            self.corners = _corners(self.lo[None], self.hi[None])[0]
            self.model = _model(fcl, self.points, self.tris)
            self.obj = fcl.CollisionObject(self.model)
        self.sig = None  # the geometry's hash, once asked for
        self._faces = None
        self._models: dict[int, object] = {}
        self._classifier = None

    def faces(self):
        """The B-rep faces, their mesh boxes ``(lo, hi)`` (inf: not meshed) and triangles."""
        if self._faces is None:
            occ = list(subshapes(self.shape, "face"))
            lo, hi = np.full((len(occ), 3), np.inf), np.full((len(occ), 3), -np.inf)
            order = np.argsort(self.face_of, kind="stable")
            ids, starts = np.unique(self.face_of[order], return_index=True)
            tris = dict(zip(ids, np.split(order, starts[1:])))
            for f, t in tris.items():
                p = self.points[self.tris[t].ravel()]
                lo[f], hi[f] = p.min(0), p.max(0)
            self._faces = (occ, lo, hi, tris)
        return self._faces

    def face_model(self, fcl, f: int):
        """An FCL model of one face's mesh, made when first asked for."""
        if f not in self._models:
            used, local = np.unique(self.tris[self.faces()[3][f]], return_inverse=True)
            self._models[f] = _model(fcl, self.points[used], local.reshape(-1, 3).astype(np.int32))
        return self._models[f]

    def inside(self, p) -> bool:
        from OCP.BRepClass3d import BRepClass3d_SolidClassifier
        from OCP.TopAbs import TopAbs_IN

        if self._classifier is None:
            self._classifier = BRepClass3d_SolidClassifier(self.shape)
        self._classifier.Perform(gp_Pnt(*map(float, p)), 1e-7)
        return self._classifier.State() == TopAbs_IN


def _mesh(shape):
    """Points, triangles, each triangle's face and each face's deflection (how far its mesh strays)."""
    from OCP.BRep import BRep_Tool
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Copy
    from OCP.BRepMesh import BRepMesh_IncrementalMesh

    copy = BRepBuilderAPI_Copy(shape, False, False).Shape()  # meshed apart: the part's own exports keep their mesh
    BRepMesh_IncrementalMesh(copy, TOL, False, 0.3, True)
    points, tris, face_of, defl, n = [], [], [], [], 0
    for i, f in enumerate(subshapes(copy, "face")):
        loc = TopLoc_Location()
        tri = static(BRep_Tool, "Triangulation")(f, loc)
        defl.append(tri.Deflection() if tri is not None else 0.0)
        if tri is None or not tri.NbTriangles():
            continue
        t = loc.Transformation()
        points += [tri.Node(j).Transformed(t).Coord() for j in range(1, tri.NbNodes() + 1)]
        tris += [[k + n - 1 for k in tri.Triangle(j).Get()] for j in range(1, tri.NbTriangles() + 1)]
        face_of += [i] * tri.NbTriangles()
        n = len(points)
    return (np.array(points, dtype=np.float64).reshape(-1, 3), np.array(tris, dtype=np.int32).reshape(-1, 3),
            np.array(face_of, dtype=np.int64), np.array(defl))


def _model(fcl, points, tris):
    m = fcl.BVHModel()
    m.beginModel(len(points), len(tris))
    m.addSubModel(points, tris)
    m.endModel()
    return m


def _corners(lo, hi):
    """The 8 corners of boxes ``(n, 3)``: ``(n, 8, 3)``."""
    bits = np.array([[(k >> a) & 1 for a in range(3)] for k in range(8)], dtype=bool)
    return np.where(bits[None], hi[:, None], lo[:, None])


def _boxes(corners, m):
    """Boxes of corners ``(n, 8, 3)`` moved by ``m`` (one 4×4, or ``(n, 4, 4)``)."""
    if m.ndim == 2:
        w = corners @ m[:3, :3].T + m[:3, 3]
    else:
        w = np.einsum("nij,nkj->nki", m[:, :3, :3], corners) + m[:, None, :3, 3]
    return w.min(1), w.max(1)


def _apart(alo, ahi, blo, bhi):
    """How far apart boxes are (the largest gap along an axis; negative: they overlap): ``(na, nb)``."""
    return np.maximum(blo[None] - ahi[:, None], alo[:, None] - bhi[None]).max(2)


class _Solids(list):
    """The distinct solids, found by their TShape; a part made again each frame by its geometry."""

    def __init__(self, fcl):
        super().__init__()
        self.fcl = fcl
        self._buckets: dict[int, list] = {}
        self._by_path: dict[str, int] = {}

    def key(self, path: str, shape) -> int:
        bucket = self._buckets.setdefault(hash(shape), [])
        k = next((k for s, k in bucket if s.IsSame(shape)), None)
        if k is None:
            prev = self._by_path.get(path)
            if prev is not None:
                from .outputs import _brep_hash

                if self[prev].sig is None:
                    self[prev].sig = _brep_hash(self[prev].shape)
                if _brep_hash(shape) == self[prev].sig:
                    k = prev
            if k is None:
                k = len(self)
                self.append(_Solid(shape, self.fcl))
            bucket.append((shape, k))
        self._by_path[path] = k
        return k


# ---------------------------------------------------------------- measuring

def _narrow(fcl, a: _Solid, b: _Solid, rel):
    """The mesh gap with a at the origin and b at ``rel``, how many triangle pairs cross (the depth's
    stand-in; inf: one holds the other, the gap then 0) and the closest or a crossing pair of faces."""
    ob = fcl.CollisionObject(b.model, fcl.Transform(rel[:3, :3], rel[:3, 3]))
    res = fcl.DistanceResult()
    d = fcl.distance(a.obj, ob, fcl.DistanceRequest(), res)
    seed = (a.face_of[res.b1], b.face_of[res.b2])
    if d > 0:
        blo, bhi = _boxes(b.corners[None], rel)
        if (np.all(blo >= a.lo) and np.all(bhi <= a.hi) and a.inside(rel[:3, :3] @ b.points[0] + rel[:3, 3])) or (
                np.all(blo <= a.lo) and np.all(bhi >= a.hi) and b.inside(_inverse(rel)[:3] @ np.append(a.points[0], 1))):
            return 0.0, math.inf, seed
        return float(d), 0, seed
    hits = fcl.CollisionResult()
    fcl.collide(a.obj, ob, fcl.CollisionRequest(num_max_contacts=1000), hits)
    return 0.0, len(hits.contacts), seed


def _extrema(a, b, threads=False):
    """The B-rep gap and closest points between two shapes (solids or faces): 0 when one holds the other."""
    from OCP.BRepExtrema import BRepExtrema_DistShapeShape

    d = BRepExtrema_DistShapeShape()
    d.SetMultiThread(threads)  # worth it for whole parts, not for a pair of faces
    d.LoadS1(a)
    d.LoadS2(b)
    d.Perform()
    if not d.IsDone():
        raise TuleError("could not measure the distance", op="distance")
    if d.InnerSolution():  # OCCT says one holds the other; it is sometimes wrong (a pin in a clearance bore)
        return _held(a, b, threads)
    return max(0.0, d.Value()), np.array(d.PointOnShape1(1).Coord()), np.array(d.PointOnShape2(1).Coord())


def _held(a, b, threads):
    """The gap between the boundaries, unless a point of one boundary is inside the other (then 0)."""
    from OCP.BRepClass3d import BRepClass3d_SolidClassifier
    from OCP.BRepExtrema import BRepExtrema_DistShapeShape
    from OCP.TopAbs import TopAbs_IN, TopAbs_SOLID
    from OCP.TopExp import TopExp_Explorer

    def faces(s):
        from OCP.BRep import BRep_Builder
        from OCP.TopAbs import TopAbs_FACE
        from OCP.TopoDS import TopoDS_Compound

        c, bld = TopoDS_Compound(), BRep_Builder()
        bld.MakeCompound(c)
        ex = TopExp_Explorer(s, TopAbs_FACE)
        while ex.More():
            bld.Add(c, ex.Current())
            ex.Next()
        return c

    d = BRepExtrema_DistShapeShape()
    d.SetMultiThread(threads)
    d.LoadS1(faces(a))
    d.LoadS2(faces(b))
    d.Perform()
    p, q = np.array(d.PointOnShape1(1).Coord()), np.array(d.PointOnShape2(1).Coord())
    if not d.IsDone() or d.Value() < 1e-7:
        return 0.0, p, q

    def inside(s, x):
        ex = TopExp_Explorer(s, TopAbs_SOLID)
        while ex.More():
            c = BRepClass3d_SolidClassifier(ex.Current(), gp_Pnt(*x), 1e-7)
            if c.State() == TopAbs_IN:
                return True
            ex.Next()
        return False

    if inside(a, q) or inside(b, p):  # boundaries apart, one inside the other
        return 0.0, q, q
    return d.Value(), p, q


def _closest(fcl, a: _Solid, b: _Solid, rel, seed):
    """The B-rep gap with a at the origin and b at ``rel``, and its points: first between the faces the
    meshes put closest (``seed``), then between any other pair of faces whose boxes, then meshes,
    allow them closer, nearest first."""
    loc = TopLoc_Location(_trsf(rel))
    aocc, alo, ahi, _ = a.faces()
    bocc, blo, bhi, _ = b.faces()
    best = _extrema(aocc[seed[0]], bocc[seed[1]].Moved(loc))
    if best[0] < 1e-7:
        return (0.0, *best[1:])
    ok = np.isfinite(blo[:, 0])
    wlo, whi = np.full_like(blo, np.inf), np.full_like(bhi, -np.inf)
    wlo[ok], whi[ok] = _boxes(_corners(blo[ok], bhi[ok]), rel)
    err = 1.5 * (a.defl[:, None] + b.defl[None, :]) + 1e-4  # how far a face pair's meshes may be off
    least = _apart(alo, ahi, wlo, whi) - err
    least[seed] = math.inf
    tf = fcl.Transform(rel[:3, :3], rel[:3, 3])
    near = []
    for i, j in zip(*np.nonzero(least < best[0])):
        d = fcl.distance(fcl.CollisionObject(a.face_model(fcl, i)), fcl.CollisionObject(b.face_model(fcl, j), tf))
        if max(d, 0.0) - err[i, j] < best[0]:
            near.append((max(d, 0.0) - err[i, j], i, j))
    for low, i, j in sorted(near):
        if low >= best[0] or best[0] < 1e-7:
            break
        found = _extrema(aocc[i], bocc[j].Moved(loc))
        if found[0] < best[0]:
            best = found
    return (0.0 if best[0] < 1e-7 else best[0]), best[1], best[2]


def _common(a, b, min_volume):
    """Volume, center and size of what two shapes have in common (nan: the Boolean failed)."""
    from OCP.Bnd import Bnd_Box
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Common
    from OCP.BRepBndLib import BRepBndLib
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps

    try:
        op = BRepAlgoAPI_Common(a, b)
        if not op.IsDone():
            return math.nan, None, 0.0
        p = GProp_GProps()
        static(BRepGProp, "VolumeProperties")(op.Shape(), p)
        v = p.Mass()
        if v <= min_volume:
            return v, None, 0.0
        box = Bnd_Box()
        static(BRepBndLib, "Add")(op.Shape(), box, True)
        size = 0.5 * box.CornerMin().Distance(box.CornerMax())
        return v, np.array(p.CentreOfMass().Coord()), size
    except OCC_ERRORS:
        return math.nan, None, 0.0


def _exact(fcl, a: _Solid, b: _Solid, rel, narrow, volumes, gaps, min_volume):
    """``(gap, overlap, point on a, point on b, size of the overlap)`` with a at the origin, b at ``rel``,
    from what the meshes found there (``_narrow``). ``volumes``: measure overlaps (else they are a gap
    of 0); ``gaps``: measure gaps (else inf)."""
    mesh_gap, crossing, seed = narrow
    bshape = b.shape.Located(TopLoc_Location(_trsf(rel)))

    def overlap():
        v, c, size = _common(a.shape, bshape, min_volume)
        if v <= min_volume:
            return None
        if c is None:  # the Boolean failed: say where, roughly
            blo, bhi = _boxes(b.corners[None], rel)
            c = 0.5 * (np.maximum(a.lo, blo[0]) + np.minimum(a.hi, bhi[0]))
        return 0.0, v, c, c, size

    if mesh_gap == 0 and volumes:  # the meshes cross, or one holds the other
        found = overlap()
        if found or not gaps:
            return found or (math.inf, 0.0, None, None, 0.0)
    g, p, q = _extrema(a.shape, bshape, True) if crossing == math.inf else _closest(fcl, a, b, rel, seed)
    if g == 0 and volumes and mesh_gap > 0:  # an overlap thinner than the meshes can see
        found = overlap()
        if found:
            return found
    return g, 0.0, p, q, 0.0


# ---------------------------------------------------------------- results

@dataclass(frozen=True)
class Pair:
    """Two parts where they come closest (or overlap most) through the frames.

    ``gap`` in mm (0: touching or overlapping), ``overlap`` the volume in common in mm³ (nan: the
    Boolean failed), both measured on the B-rep at ``frame`` (0-based; ``label`` its label), the
    frame the meshes rank worst (frames closer than the meshes can tell apart, a few hundredths of
    a mm, may swap). ``points`` on a and on b (an overlap's center twice); ``contact``: listed as
    expected to touch. ``frames``: every frame where they come within ``below`` or overlap, by the meshes.
    """

    a: str
    b: str
    gap: float
    overlap: float
    frame: int
    label: str | None
    points: tuple
    contact: bool
    frames: tuple = field(repr=False)
    _size: float = field(default=0.0, repr=False, compare=False)

    @property
    def overlapping(self) -> bool:
        return not self.overlap <= 0  # nan: the Boolean failed, so they do

    @property
    def clash(self) -> bool:
        """They overlap and are not a listed contact."""
        return self.overlapping and not self.contact

    @property
    def status(self) -> str:
        """``clash``, ``touch`` or ``gap``; ``contact`` for listed contacts."""
        return "contact" if self.contact else "clash" if self.overlapping else "touch" if self.gap == 0 else "gap"

    def _key(self):
        return (self.contact, -(math.inf if self.overlap != self.overlap else self.overlap), self.gap)

    def _amount(self) -> str:
        if self.overlapping:
            return "overlap ?" if self.overlap != self.overlap else f"{self.overlap:.4g} mm³ overlap"
        return "touching" if self.gap == 0 else f"{self.gap:.4g} mm"

    def _where(self) -> str:
        return f"frame {self.frame}" + (f" ({self.label})" if self.label not in (None, "") else "")


def _pt(p) -> str:
    return "(" + ", ".join(f"{round(v, 6) + 0.0:.4g}" for v in p) + ")"


class Clearance:
    """What ``clearance`` found: ``pairs`` worst first (clashes by volume, then gaps, then contacts),
    ``clashes`` (overlaps not listed as contacts), ``ok`` (none). ``print(r)`` is a table;
    ``r.show(name)`` puts it in the viewer."""

    def __init__(self, pairs, frames, paths, solids, below, seconds):
        self.pairs: list[Pair] = pairs
        self.below = below
        self.seconds = seconds
        self._frames = frames  # (label, part ids, solid keys, placements) per frame
        self._paths = paths
        self._solids = solids

    @property
    def clashes(self) -> list[Pair]:
        return [p for p in self.pairs if p.clash]

    @property
    def ok(self) -> bool:
        return not self.clashes

    @property
    def worst(self) -> Pair | None:
        return self.pairs[0] if self.pairs else None

    def __iter__(self):
        return iter(self.pairs)

    def __len__(self) -> int:
        return len(self.pairs)

    def _summary(self) -> str:
        n = {s: sum(p.status == s for p in self.pairs) for s in ("clash", "touch", "gap", "contact")}
        words = [f"{n['clash']} clash{'es' if n['clash'] != 1 else ''}"]
        if n["touch"]:
            words.append(f"{n['touch']} touching")
        words.append(f"{n['gap']} under {self.below:g} mm")
        if n["contact"]:
            words.append(f"{n['contact']} contact{'s' if n['contact'] != 1 else ''} as listed")
        return ", ".join(words)

    def __str__(self) -> str:
        lines = [f"clearance: {len(self._paths)} parts, {len(self._frames)} frame{'s' if len(self._frames) != 1 else ''}: "
                 f"{self._summary()} ({self.seconds:.1f} s)"]
        if self.pairs:
            w = max(len(f"{p.a} × {p.b}") for p in self.pairs[:25])
            for p in self.pairs[:25]:
                at = _pt(p.points[0]) if p.overlapping or p.gap == 0 else f"{_pt(p.points[0])} → {_pt(p.points[1])}"
                lines.append(f"  {p.status:<8} {p.a + ' × ' + p.b:<{w}}  {p._amount():<18} {p._where():<20} at {at}")
            if len(self.pairs) > 25:
                lines.append(f"  … and {len(self.pairs) - 25} more (r.pairs)")
        return "\n".join(lines)

    __repr__ = __str__

    def records(self) -> list[dict]:
        """The pairs as table rows."""
        return [{"status": p.status, "a": p.a, "b": p.b, "gap (mm)": round(p.gap, 6),
                 "overlap (mm³)": None if p.overlap != p.overlap else round(p.overlap, 6),
                 "frame": p.frame, "label": p.label, "on a": _pt(p.points[0]), "on b": _pt(p.points[1])}
                for p in self.pairs]

    def show(self, name: str, frame: int | None = None, title: str | None = None) -> str:
        """Write an overlay output: the worst pair's frame (or ``frame``) with the faces of each pair
        at their worst there marked (red: clash, amber: gap or touching, green: contact), arrows at
        the gaps and overlaps, and ``clearance.json``, the table. Returns the folder."""
        from . import outputs
        from .io import write_atomic
        from .shape import Shape
        from OCP.BRep import BRep_Builder
        from OCP.TopoDS import TopoDS_Compound

        if frame is None:
            frame = self.pairs[0].frame if self.pairs else 0
        label, ids, keys, M = self._frames[frame]
        placed = [self._solids[k].shape.Located(TopLoc_Location(_trsf(m))) for k, m in zip(keys, M)]
        comp = TopoDS_Compound()
        b = BRep_Builder()
        b.MakeCompound(comp)
        for s in placed:
            b.Add(comp, s)
        everything = subshapes(comp, "face")
        sev = {"clash": "error", "gap": "warning", "touch": "warning", "contact": "ok"}
        faces, arrows = {}, []
        for p in self.pairs:
            if p.frame != frame:
                continue
            for path, at, other in ((p.a, p.points[0], p.b), (p.b, p.points[1], p.a)):
                i = ids.index(self._paths.index(path))
                local = (_inverse(M[i]) @ np.append(at, 1))[:3]
                own = list(subshapes(placed[i], "face"))
                for f in self._near_faces(keys[i], local, p._size):
                    faces[everything.index(own[f])] = {"severity": sev[p.status],
                                                       "label": f"{p.status} with {other}: {p._amount()}"}
            if p.overlapping and not p.contact:
                arrows.append({"at": p.points[0], "dir": (0, 0, -1), "label": f"{p.a} × {p.b}: {p._amount()}"})
            elif p.gap > 0 and not p.contact:
                arrows.append({"at": p.points[1], "dir": (np.asarray(p.points[1]) - p.points[0]) / p.gap,
                               "label": f"{p.a} × {p.b}: {p._amount()}"})
        folder = outputs.folder(name)
        write_atomic(os.path.join(folder, "clearance.json"), json.dumps(self.records(), indent=1, ensure_ascii=False) + "\n")
        worst = self.pairs[0] if self.pairs else None
        info = {"parts": len(self._paths), "frames": len(self._frames), "found": self._summary(),
                "shown": f"frame {frame}" + (f" ({label})" if label not in (None, "") else "")}
        if worst:
            info["worst"] = f"{worst.a} × {worst.b}: {worst.status}, {worst._amount()}, {worst._where()}"
        if title is None:
            n = len(self.clashes)
            title = f"Clearance: {n} clash{'es' if n != 1 else ''}" if n else "Clearance: no clashes"
        return outputs.overlay(name, Shape(comp, name=name), faces, arrows=arrows, title=title, info=info)

    def _near_faces(self, key, at, size) -> list[int]:
        """Faces of a solid at a point (within ``size`` of it: an overlap's faces)."""
        from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeVertex
        from OCP.BRepExtrema import BRepExtrema_DistShapeShape

        s = self._solids[key]
        occ, lo, hi, _ = s.faces()
        reach = max(size, s.slack + 1e-3)
        out = np.nonzero(np.all(lo - reach <= at, 1) & np.all(at <= hi + reach, 1))[0]
        if size:
            return list(out)
        v = BRepBuilderAPI_MakeVertex(gp_Pnt(*map(float, at))).Vertex()
        on = [f for f in out if BRepExtrema_DistShapeShape(v, occ[f]).Value() < 1e-4]
        return on or list(out[:1])


# ---------------------------------------------------------------- the check

def _pairs_rule(pairs, what):
    rules = []
    for p in pairs:
        if not (isinstance(p, (list, tuple)) and len(p) == 2 and all(isinstance(x, str) for x in p)):
            raise TuleError(f"{what} lists pairs of part names, got {p!r}", op="clearance",
                            hint=f'{what}=[("pin", "link"), ("bolt*", "*")]: a name, a path or a pattern')
        rules.append(p)

    def names(path):
        path = re.sub(r"#\d+$", "", path)
        return (path, path.rsplit("/", 1)[-1])

    def match(a, b):
        na, nb = names(a), names(b)
        hit = lambda ns, pat: any(fnmatch.fnmatchcase(n, pat) for n in ns)
        return any((hit(na, p) and hit(nb, q)) or (hit(na, q) and hit(nb, p)) for p, q in rules)

    return match


def clearance(frames, ignore=(), contacts=(), below: float = 0.5) -> Clearance:
    """Check that a mechanism clears itself: every pair of parts that overlaps or comes within
    ``below`` mm, at its worst frame, with the gap or overlap volume measured on the B-rep.

    ``frames``: the poses of one mechanism, parts matched by path: Assemblies (a list, a generator,
    a ``{label: Assembly}`` dict, or one for a static check), a sequence from ``outputs.sequence``
    (its frames' labels come along), or a written one's folder (``"outputs/arm-motion"``).
    ``contacts``: pairs expected to touch (gear meshes, a pin in its bore, a slider on its rail),
    reported but not clashes. ``ignore``: pairs not checked. Both take names, paths or patterns:
    ``[("pin", "link"), ("bolt*", "*")]``. Returns a :class:`Clearance`: ``print(r)``,
    ``r.show(name)``, ``r.ok``, ``r.clashes``, ``r.pairs``.
    """
    return _check(frames, ignore, contacts, below)


def _check(frames, ignore=(), contacts=(), below=0.5, volumes=True, gaps=True, min_volume=MIN_VOLUME) -> Clearance:
    fcl = _fcl()
    start = time.monotonic()
    skip, touch = _pairs_rule(ignore, "ignore"), _pairs_rule(contacts, "contacts")
    solids = _Solids(fcl)
    paths: list[str] = []
    pid: dict[str, int] = {}
    rule: dict[tuple, bool | None] = {}  # (a, b) -> None: ignored, else whether a contact
    stored = []
    near: dict[tuple, list] = {}  # (a, b) -> [(mesh gap, crossing, frame, pose key)]
    cache: dict[tuple, tuple] = {}
    for fi, (label, parts) in enumerate(_frames(frames)):
        ids, keys, ms, seen = [], [], [], {}
        for path, shape, m in parts:
            seen[path] = n = seen.get(path, 0) + 1
            if n > 1:
                path = f"{path}#{n}"  # two parts of one name
            if path not in pid:
                pid[path] = len(paths)
                paths.append(path)
            ids.append(pid[path])
            keys.append(solids.key(path, shape))
            ms.append(m)
        M = np.array(ms).reshape(-1, 4, 4)
        stored.append((label, ids, keys, M))
        live = [i for i, k in enumerate(keys) if not solids[k].empty]  # a part without faces cannot clash
        if len(live) < 2:
            continue
        lo, hi = _boxes(np.array([solids[keys[i]].corners for i in live]), M[live])
        reach = below + 2 * max(solids[keys[i]].slack for i in live)
        for u, v in zip(*np.nonzero(np.triu(_apart(lo, hi, lo, hi) <= reach, 1))):
            i, j = live[u], live[v]
            if ids[i] > ids[j]:
                i, j = j, i
            pair = (ids[i], ids[j])
            if pair not in rule:
                a, b = paths[pair[0]], paths[pair[1]]
                rule[pair] = None if skip(a, b) else touch(a, b)
            if rule[pair] is None:
                continue
            rel = _inverse(M[i]) @ M[j]
            ck = (keys[i], keys[j], (np.round(rel[:3], 6) + 0.0).tobytes())
            if ck not in cache:
                cache[ck] = _narrow(fcl, solids[keys[i]], solids[keys[j]], rel)
            gap, crossing, _ = cache[ck]
            if gap <= below + solids[keys[i]].slack + solids[keys[j]].slack:
                near.setdefault(pair, []).append((gap, crossing, fi, ck))

    pairs = []
    for (ia, ib), seen in near.items():
        over = [s for s in seen if s[0] == 0]
        if over and volumes:
            ranked = sorted(over, key=lambda s: -s[1])
        else:  # the frames whose mesh gap is within the meshes' error of the smallest
            ranked = sorted(seen, key=lambda s: s[0])
            ka, kb = ranked[0][3][:2]
            ranked = [s for s in ranked if s[0] <= ranked[0][0] + 2 * (solids[ka].slack + solids[kb].slack)]
        picks, poses = [], set()
        for s in ranked:
            if s[3] not in poses:
                poses.add(s[3])
                picks.append(s)
            if len(picks) == REFINE:
                break
        best = None
        for _, _, fi, ck in picks:
            _, ids, keys, M = stored[fi]
            i, j = ids.index(ia), ids.index(ib)
            g, v, p, q, size = _exact(fcl, solids[keys[i]], solids[keys[j]], _inverse(M[i]) @ M[j], cache[ck],
                                      volumes, gaps, min_volume)
            found = (-(math.inf if v != v else v), g, fi, p, q, v, size)
            if best is None or found[:2] < best[:2]:
                best = found
        _, g, fi, p, q, v, size = best
        if not (g < below or g == 0 or not v <= 0):
            continue
        label, _, _, M = stored[fi]
        mi = M[stored[fi][1].index(ia)]
        world = lambda x: tuple(float(c) + 0.0 for c in mi[:3, :3] @ x + mi[:3, 3])
        within = tuple(sorted({s[2] for s in seen if s[0] < below or s[0] == 0}))
        pairs.append(Pair(paths[ia], paths[ib], g, v, fi, label, (world(p), world(q)), bool(rule[(ia, ib)]),
                          within, size))
    pairs.sort(key=lambda p: (*p._key(), pid[p.a], pid[p.b]))
    return Clearance(pairs, stored, paths, solids, below, time.monotonic() - start)
