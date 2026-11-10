"""Selections of faces, edges and vertices, with queries that filter further.

A selection belongs to a shape (its ``owner``). Selections that came from a
feature role or a tag also remember the group keys they came from, so they can
be resolved in later shapes after Booleans and other operations: pass
``boss.edges["bottom"]`` to ``part.fillet(...)`` and tule finds those edges in
``part`` through OCCT's history.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Callable, Iterable

from OCP.BRep import BRep_Tool
from OCP.BRepAdaptor import BRepAdaptor_Curve, BRepAdaptor_Surface
from OCP.BRepGProp import BRepGProp
from OCP.BRepLProp import BRepLProp_SLProps
from OCP.BRepTools import BRepTools
from OCP.GeomAbs import (
    GeomAbs_BSplineSurface,
    GeomAbs_Circle,
    GeomAbs_Cone,
    GeomAbs_Cylinder,
    GeomAbs_Line,
    GeomAbs_Plane,
    GeomAbs_Sphere,
    GeomAbs_Torus,
)
from OCP.GProp import GProp_GProps
from OCP.TopAbs import TopAbs_REVERSED

from ._occ import ShapeSet, static, subshapes
from .errors import TuleError
from .geom import Vec, VecLike, from_pnt, vec

if TYPE_CHECKING:  # pragma: no cover
    from .shape import Shape


_SURFACE_TYPES = {
    GeomAbs_Plane: "plane",
    GeomAbs_Cylinder: "cylinder",
    GeomAbs_Cone: "cone",
    GeomAbs_Sphere: "sphere",
    GeomAbs_Torus: "torus",
    GeomAbs_BSplineSurface: "bspline",
}

_CURVE_TYPES = {GeomAbs_Line: "line", GeomAbs_Circle: "circle"}


# ---------------------------------------------------------------- face / edge facts


def face_center(f) -> Vec:
    p = GProp_GProps()
    static(BRepGProp, "SurfaceProperties")(f, p)
    return from_pnt(p.CentreOfMass())


def face_area(f) -> float:
    p = GProp_GProps()
    static(BRepGProp, "SurfaceProperties")(f, p)
    return p.Mass()


def face_type(f) -> str:
    return _SURFACE_TYPES.get(BRepAdaptor_Surface(f).GetType(), "other")


def face_normal(f) -> Vec:
    """Outward normal of a face (at its parametric middle)."""
    umin, umax, vmin, vmax = static(BRepTools, "UVBounds")(f)
    surf = BRepAdaptor_Surface(f)
    props = BRepLProp_SLProps(surf, (umin + umax) / 2, (vmin + vmax) / 2, 1, 1e-7)
    if not props.IsNormalDefined():
        return Vec(0, 0, 0)
    n = props.Normal()
    v = Vec(n.X(), n.Y(), n.Z())
    if f.Orientation() == TopAbs_REVERSED:
        v = -v
    return v


def face_radius(f) -> float | None:
    s = BRepAdaptor_Surface(f)
    t = s.GetType()
    if t == GeomAbs_Cylinder:
        return s.Cylinder().Radius()
    if t == GeomAbs_Sphere:
        return s.Sphere().Radius()
    return None


def edge_type(e) -> str:
    return _CURVE_TYPES.get(BRepAdaptor_Curve(e).GetType(), "other")


def edge_center(e) -> Vec:
    p = GProp_GProps()
    static(BRepGProp, "LinearProperties")(e, p)
    if p.Mass() == 0:  # degenerate edge
        c = BRepAdaptor_Curve(e)
        return from_pnt(c.Value((c.FirstParameter() + c.LastParameter()) / 2))
    return from_pnt(p.CentreOfMass())


def edge_length(e) -> float:
    p = GProp_GProps()
    static(BRepGProp, "LinearProperties")(e, p)
    return p.Mass()


def edge_direction(e) -> Vec | None:
    c = BRepAdaptor_Curve(e)
    if c.GetType() != GeomAbs_Line:
        return None
    d = c.Line().Direction()
    return Vec(d.X(), d.Y(), d.Z())


def edge_radius(e) -> float | None:
    c = BRepAdaptor_Curve(e)
    if c.GetType() == GeomAbs_Circle:
        return c.Circle().Radius()
    return None


def is_degenerate(e) -> bool:
    return bool(static(BRep_Tool, "Degenerated")(e))


def _angle_deg(a: Vec, b: Vec) -> float:
    la, lb = a.length, b.length
    if not la or not lb:
        return 180.0
    c = max(-1.0, min(1.0, a.dot(b) / (la * lb)))
    return math.degrees(math.acos(c))


# ---------------------------------------------------------------- selections


class Item:
    """One face, edge or vertex as ``filter`` sees it: ``center``, ``type`` and the rest of its
    ``info()`` as attributes (None when this item has none, e.g. ``normal`` of a curved face),
    the OCCT shape as ``wrapped``. ``center_of_mass`` is ``center``. Any other name is an
    AttributeError that lists the ones there are."""

    ALIASES = {"center_of_mass": "center", "centroid": "center"}

    def __init__(self, wrapped, info: dict, known: Iterable[str] = ()):
        self.wrapped = wrapped
        self._info = info
        self._known = tuple(dict.fromkeys([*info, *known]))

    def __getattr__(self, k):
        if k.startswith("_"):
            raise AttributeError(k)
        k = self.ALIASES.get(k, k)
        if k not in self._known:
            raise AttributeError(f"a selection item has no {k!r}; it has "
                                 f"{', '.join(self._known + ('wrapped',))}")
        v = self._info.get(k)
        return Vec(*v) if k in ("center", "normal") and v is not None else v

    def __dir__(self):
        return [*self._known, "wrapped", *self.ALIASES]

    def __repr__(self):
        return f"Item({self._info})"


class Selection:
    """An ordered selection of sub-shapes of one kind belonging to a shape."""

    kind = "shape"
    ITEM_KEYS: tuple[str, ...] = ("center",)  # what filter's items have (some None on some items)

    def __init__(self, owner: "Shape", items: Iterable, keys: tuple[str, ...] = ()):
        self.owner = owner
        self.items = list(ShapeSet(items))
        self.keys = tuple(keys)

    # -- container protocol
    def __len__(self) -> int:
        return len(self.items)

    def __iter__(self):
        """Each item as a selection of one (``for f in part.faces: f.id, f.info()``)."""
        return (self._new([x]) for x in self.items)

    def __bool__(self) -> bool:
        return bool(self.items)

    def _new(self, items, keys=()):
        return type(self)(self.owner, items, keys)

    def __getitem__(self, i):
        if isinstance(i, str):
            return self.owner._named(self.kind, i)
        if isinstance(i, slice):
            return self._new(self.items[i])
        if isinstance(i, Selection):
            return self.owner._resolve(i, self.kind)
        if isinstance(i, (list, tuple)):
            return self._new([self.items[k] for k in i])
        return self._new([self.items[i]])

    # -- set operations
    def __or__(self, other: "Selection"):
        return self._new(list(self.items) + list(self._same_owner(other).items))

    __add__ = __or__

    def __and__(self, other: "Selection"):
        o = ShapeSet(self._same_owner(other).items)
        return self._new([s for s in self.items if s in o])

    def __sub__(self, other: "Selection"):
        o = ShapeSet(self._same_owner(other).items)
        return self._new([s for s in self.items if s not in o])

    def _same_owner(self, other: "Selection") -> "Selection":
        if other.owner is self.owner:
            return other
        return self.owner._resolve(other, self.kind)

    # -- generic filters
    def filter(self, pred: Callable) -> "Selection":
        """Keep the items for which ``pred(item)`` is true. ``item`` has what ``info()`` lists
        (``center``, ``type``, ``area`` or ``length``, ``radius``, ``normal``) and ``.wrapped``."""
        return self._new([s for s, d in zip(self.items, self.info()) if pred(Item(s, d, self.ITEM_KEYS))])

    def info(self) -> list[dict]:
        return [{"center": tuple(round(c, 6) for c in self._center(s))} for s in self.items]

    def _keep(self, pred: Callable) -> "Selection":
        return self._new([s for s in self.items if pred(s)])

    def _center(self, s) -> Vec:  # pragma: no cover - overridden
        raise NotImplementedError

    def sort_by(self, axis: VecLike = (0, 0, 1), reverse: bool = False) -> "Selection":
        a = vec(axis)
        return self._new(sorted(self.items, key=lambda s: self._center(s).dot(a), reverse=reverse))

    def _extreme(self, axis: VecLike, sign: float, tol: float) -> "Selection":
        if not self.items:
            return self._new([])
        a = vec(axis)
        vals = [(self._center(s).dot(a) * sign, s) for s in self.items]
        best = max(v for v, _ in vals)
        return self._new([s for v, s in vals if v >= best - tol])

    def top(self, axis: VecLike = (0, 0, 1), tol: float = 1e-6) -> "Selection":
        """Items whose center is highest along ``axis``."""
        return self._extreme(axis, 1.0, tol)

    def bottom(self, axis: VecLike = (0, 0, 1), tol: float = 1e-6) -> "Selection":
        """Items whose center is lowest along ``axis``."""
        return self._extreme(axis, -1.0, tol)

    def within(self, lo: VecLike, hi: VecLike) -> "Selection":
        """Items whose center lies in the axis-aligned box ``lo``..``hi``."""
        lo, hi = vec(lo), vec(hi)

        def inside(s):
            c = self._center(s)
            return all(lo[i] - 1e-9 <= c[i] <= hi[i] + 1e-9 for i in range(3))

        return self._keep(inside)

    def nearest(self, point: VecLike, n: int = 1) -> "Selection":
        p = vec(point)
        return self._new(sorted(self.items, key=lambda s: (self._center(s) - p).length)[:n])

    # -- ids and description
    @property
    def ids(self) -> list[int]:
        """Stable ids of the items in the owner (``f3`` is face id 3)."""
        return self.owner._ids(self.kind, self.items)

    @property
    def id(self) -> int:
        """The id of a selection of one (``for f in part.faces: f.id``)."""
        if len(self.items) != 1:
            raise TuleError(f"id is one {self.kind}'s; this selection has {len(self.items)}", op="select",
                            hint="ids lists them all; for f in part.faces gives one at a time")
        return self.ids[0]

    def one(self):
        if len(self.items) != 1:
            raise TuleError(f"expected exactly one {self.kind}, found {len(self.items)}", op="select")
        return self.items[0]

    def __repr__(self) -> str:
        prefix = {"face": "f", "edge": "e", "vertex": "v"}.get(self.kind, "s")
        ids = ", ".join(f"{prefix}{i}" for i in self.ids[:12])
        more = "" if len(self.items) <= 12 else f", … ({len(self.items)} total)"
        return f"<{type(self).__name__} [{ids}{more}]>"


class Faces(Selection):
    kind = "face"
    ITEM_KEYS = ("id", "type", "area", "center", "normal", "radius", "tags")

    def _center(self, s) -> Vec:
        return face_center(s)

    def of_type(self, *types: str) -> "Faces":
        """Faces whose surface is one of ``plane``, ``cylinder``, ``cone``, ``sphere``, ``torus``, ``bspline``."""
        return self._keep(lambda f: face_type(f) in types)

    def planar(self) -> "Faces":
        return self.of_type("plane")

    def cylindrical(self, r: float | None = None, tol: float = 1e-6) -> "Faces":
        f = self.of_type("cylinder")
        if r is None:
            return f
        return f._keep(lambda x: abs((face_radius(x) or 0) - r) <= tol)

    def curved(self) -> "Faces":
        return self._keep(lambda f: face_type(f) != "plane")

    def normal(self, direction: VecLike, tol_deg: float = 1.0) -> "Faces":
        """Planar faces whose outward normal points along ``direction``."""
        d = vec(direction)
        return self.planar()._keep(lambda f: _angle_deg(face_normal(f), d) <= tol_deg)

    def largest(self, n: int = 1) -> "Faces":
        return self._new(sorted(self.items, key=face_area, reverse=True)[:n])

    def smallest(self, n: int = 1) -> "Faces":
        return self._new(sorted(self.items, key=face_area)[:n])

    @property
    def edges(self) -> "Edges":
        """All edges bounding these faces."""
        out = []
        for f in self.items:
            out.extend(e for e in subshapes(f, "edge") if not is_degenerate(e))
        return Edges(self.owner, out)

    @property
    def area(self) -> float:
        return sum(face_area(f) for f in self.items)

    def info(self) -> list[dict]:
        """One dict per face: id, type, area, center, normal (planar), radius."""
        out = []
        for i, f in zip(self.ids, self.items):
            d = {"id": i, "type": face_type(f), "area": round(face_area(f), 6),
                 "center": tuple(round(c, 6) for c in face_center(f))}
            if d["type"] == "plane":
                d["normal"] = tuple(round(c, 6) for c in face_normal(f))
            r = face_radius(f)
            if r is not None:
                d["radius"] = round(r, 6)
            tags = self.owner._tags_of(f)
            if tags:
                d["tags"] = tags
            out.append(d)
        return out


class Edges(Selection):
    kind = "edge"
    ITEM_KEYS = ("id", "type", "length", "center", "radius")

    def __init__(self, owner, items, keys=()):
        seams = owner._seams() if hasattr(owner, "_seams") else ()
        super().__init__(owner, [e for e in items if not is_degenerate(e) and e not in seams], keys)

    def _center(self, s) -> Vec:
        return edge_center(s)

    def lines(self) -> "Edges":
        return self._keep(lambda e: edge_type(e) == "line")

    def circles(self, r: float | None = None, tol: float = 1e-6) -> "Edges":
        e = self._keep(lambda x: edge_type(x) == "circle")
        if r is None:
            return e
        return e._keep(lambda x: abs((edge_radius(x) or 0) - r) <= tol)

    def parallel(self, axis: VecLike, tol_deg: float = 1.0) -> "Edges":
        """Straight edges parallel to ``axis`` (either direction)."""
        a = vec(axis)

        def ok(e):
            d = edge_direction(e)
            return d is not None and min(_angle_deg(d, a), _angle_deg(-d, a)) <= tol_deg

        return self._keep(ok)

    def perpendicular(self, axis: VecLike, tol_deg: float = 1.0) -> "Edges":
        """Edges lying in planes perpendicular to ``axis`` (straight or circular)."""
        a = vec(axis)

        def ok(e):
            d = edge_direction(e)
            if d is not None:
                return abs(_angle_deg(d, a) - 90.0) <= tol_deg
            c = BRepAdaptor_Curve(e)
            if c.GetType() == GeomAbs_Circle:
                n = c.Circle().Axis().Direction()
                return min(_angle_deg(Vec(n.X(), n.Y(), n.Z()), a), _angle_deg(-Vec(n.X(), n.Y(), n.Z()), a)) <= tol_deg
            return False

        return self._keep(ok)

    def on(self, faces: "Faces") -> "Edges":
        """Edges that bound any of ``faces``."""
        fs = faces if faces.owner is self.owner else self.owner._resolve(faces, "face")
        bounding = ShapeSet()
        for f in fs.items:
            for e in subshapes(f, "edge"):
                bounding.add(e)
        return self._keep(lambda e: e in bounding)

    def longest(self, n: int = 1) -> "Edges":
        return self._new(sorted(self.items, key=edge_length, reverse=True)[:n])

    def shortest(self, n: int = 1) -> "Edges":
        return self._new(sorted(self.items, key=edge_length)[:n])

    @property
    def length(self) -> float:
        return sum(edge_length(e) for e in self.items)

    def info(self) -> list[dict]:
        out = []
        for i, e in zip(self.ids, self.items):
            d = {"id": i, "type": edge_type(e), "length": round(edge_length(e), 6),
                 "center": tuple(round(c, 6) for c in edge_center(e))}
            r = edge_radius(e)
            if r is not None:
                d["radius"] = round(r, 6)
            out.append(d)
        return out


class Vertices(Selection):
    kind = "vertex"

    def _center(self, s) -> Vec:
        return from_pnt(static(BRep_Tool, "Pnt")(s))

    @property
    def points(self) -> list[Vec]:
        return [self._center(v) for v in self.items]


SELECTION_TYPES = {"face": Faces, "edge": Edges, "vertex": Vertices}
