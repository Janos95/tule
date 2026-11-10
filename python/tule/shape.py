"""The immutable Shape: an OCCT shape plus named face/edge groups.

Groups are how tags and feature provenance survive modeling. Each group is a
key (``"tag:load"`` for a tag, ``"#7.top"`` for the top faces of feature 7)
mapped to sub-shapes. Every operation rebuilds the groups from the OCCT
builder's history (``Modified``), so a group always refers to
sub-shapes of the current shape.

Names a user can type are *aliases*: ``boss.faces["top"]`` looks up the alias
``top`` of ``boss``. Tags travel with the geometry through every operation;
feature roles (``top``, ``side``, ``fillet``, …) are aliases of the shape that
created them and of shapes derived from it by unary operations (transforms,
fillets, …), but not of Boolean results. To use a feature's role after a
Boolean, pass the feature's selection: ``part.fillet(boss.edges["bottom"], 2)``.
"""

from __future__ import annotations

import itertools
import math
from typing import TYPE_CHECKING, Callable, Iterable, Sequence

from OCP.BRepAlgoAPI import BRepAlgoAPI_Common, BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse
from OCP.BRepBndLib import BRepBndLib
from OCP.BRepBuilderAPI import BRepBuilderAPI_GTransform, BRepBuilderAPI_Transform
from OCP.BRepCheck import BRepCheck_Analyzer
from OCP.BRepFilletAPI import BRepFilletAPI_MakeChamfer, BRepFilletAPI_MakeFillet
from OCP.BRepGProp import BRepGProp
from OCP.BRepOffsetAPI import BRepOffsetAPI_MakeThickSolid
from OCP.Bnd import Bnd_Box
from OCP.gp import gp_Ax2, gp_GTrsf, gp_Mat, gp_Pnt, gp_Trsf, gp_XYZ
from OCP.GProp import GProp_GProps
from OCP.ShapeUpgrade import ShapeUpgrade_UnifySameDomain


from OCP.TopLoc import TopLoc_Location
from OCP.TopoDS import TopoDS_Shape

from ._occ import ShapeSet, cast, from_list, kind_of, shape_list, static, subshapes
from .errors import OCC_ERRORS, TuleError
from .geom import Vec, VecLike, from_pnt, gdir, pnt, translation, vec, xyz_rotation
from .selection import SELECTION_TYPES, Edges, Faces, Selection, Vertices

if TYPE_CHECKING:  # pragma: no cover
    from .assembly import Assembly

_ids = itertools.count(1)


def new_feature_id() -> int:
    return next(_ids)


Groups = dict  # str -> tuple of sub-shapes


def carry(
    groups: Groups,
    result: TopoDS_Shape,
    modified: Callable[[TopoDS_Shape], list],
) -> Groups:
    """Map every group onto ``result`` using an operation's ``Modified`` history.

    A sub-shape survives if it is still in ``result`` (``IsSame``); a modified
    one is replaced by its images that are in ``result``; anything else was
    consumed. Membership in the result is the authority, not ``IsDeleted``:
    OCCT's fillet and chamfer builders report every edge and vertex without
    images as deleted, including the untouched ones that are still there.
    """
    targets = {k: subshapes(result, k) for k in ("face", "edge", "vertex")}
    out: Groups = {}
    for key, items in groups.items():
        found = ShapeSet()
        for s in items:
            kind = kind_of(s)
            target = targets.get(kind)
            if target is None:
                continue
            try:
                mods = modified(s)
            except OCC_ERRORS:
                mods = []
            for m in [*mods, s]:
                i = target.index(m) if kind_of(m) == kind else -1
                if i >= 0:
                    found.add(target[i])
        out[key] = tuple(found)
    return out


def _merge_groups(*shapes: "Shape") -> Groups:
    merged: dict[str, ShapeSet] = {}
    for sh in shapes:
        for k, items in sh._groups.items():
            st = merged.setdefault(k, ShapeSet())
            for s in items:
                st.add(s)
    return {k: tuple(v) for k, v in merged.items()}


def _tag_aliases(*shapes: "Shape") -> dict:
    out = {}
    for sh in shapes:
        for name, key in sh._alias.items():
            if key.startswith("tag:"):
                out[name] = key
    return out


class _Moved:
    """A moved shape's groups, moved when first read: a motion study's frames never read them."""

    __slots__ = ("groups", "loc")

    def __init__(self, groups, loc: TopLoc_Location):
        if isinstance(groups, _Moved):  # moved again: one placement for both
            groups, loc = groups.groups, loc.Multiplied(groups.loc)
        self.groups, self.loc = groups, loc

    def resolve(self) -> Groups:
        return {k: tuple(cast(s.Moved(self.loc)) for s in v) for k, v in self.groups.items()}


def _move_bounds(lohi, t: gp_Trsf, cache: dict):
    """Bounds of a moved shape from its bounds, when the move keeps boxes exact (a turn by 90°)."""
    m = t.VectorialPart()
    if any(min(abs(m.Value(i, j)), abs(abs(m.Value(i, j)) - 1)) > 1e-12 for i in (1, 2, 3) for j in (1, 2, 3)):
        return
    lo, hi = lohi
    pts = []
    for x in (lo.x, hi.x):
        for y in (lo.y, hi.y):
            for z in (lo.z, hi.z):
                p = gp_Pnt(x, y, z).Transformed(t)
                pts.append((p.X(), p.Y(), p.Z()))
    cache["bounds"] = (Vec(*map(min, zip(*pts))), Vec(*map(max, zip(*pts))))


class Shape:
    """An immutable solid (or compound) with tags, provenance and metadata."""

    __slots__ = ("_s", "_g", "_alias", "name", "color", "_cache", "_sm")

    def __init__(
        self,
        shape: TopoDS_Shape,
        groups: Groups | None = None,
        alias: dict | None = None,
        name: str | None = None,
        color: str | None = None,
        sm=None,
    ):
        self._s = shape
        self._g = groups if isinstance(groups, _Moved) else dict(groups or {})
        self._alias: dict = dict(alias or {})
        self.name = name
        self.color = color
        self._cache: dict = {}
        self._sm = sm  # sheet metal: thickness, bend radius, K-factor (tule.sheetmetal)

    # ------------------------------------------------------------ basics
    @property
    def _groups(self) -> Groups:
        if isinstance(self._g, _Moved):
            self._g = self._g.resolve()
        return self._g

    @property
    def wrapped(self) -> TopoDS_Shape:
        """The underlying OCCT shape."""
        return self._s

    def _derive(self, shape, groups, alias=None, keep_meta=True) -> "Shape":
        return Shape(
            shape,
            groups,
            self._alias if alias is None else alias,
            self.name if keep_meta else None,
            self.color if keep_meta else None,
            self._sm,
        )

    def named(self, name: str, color: str | None = None) -> "Shape":
        """The same shape with a part name and (optionally) a color like ``"#4a8"``; with opacity
        (``"#rrggbbaa"``, ``"#8da0aa66"``) it is drawn see-through: glass, covers, housings."""
        return self._renamed(name, color or self.color)

    def colored(self, color: str) -> "Shape":
        return self._renamed(self.name, color)

    def _renamed(self, name, color) -> "Shape":
        if color is not None:
            from .io import parse_rgba
            parse_rgba(color)  # a wrong color fails here, not when the part is written
        out = Shape(self._s, self._g, self._alias, name, color, self._sm)
        out._cache = self._cache  # the same solid: its faces, bounds, flat pattern
        return out

    def __repr__(self) -> str:
        n = f" {self.name!r}" if self.name else ""
        nf = len(self._sub("face"))
        try:
            v = f", volume {self.volume:.6g}"
        except Exception:  # pragma: no cover
            v = ""
        return f"<Shape{n}: {nf} faces{v}>"

    # ------------------------------------------------------------ topology
    def _sub(self, kind: str) -> ShapeSet:
        key = ("sub", kind)
        if key not in self._cache:
            self._cache[key] = subshapes(self._s, kind)
        return self._cache[key]

    def _seams(self) -> ShapeSet:
        """Seam edges (an edge closing a periodic face onto itself, e.g. a cylinder's seam)."""
        key = ("seams",)
        if key not in self._cache:
            from OCP.BRep import BRep_Tool

            seams = ShapeSet()
            for f in self._sub("face"):
                for e in subshapes(f, "edge"):
                    if static(BRep_Tool, "IsClosed")(e, f):
                        seams.add(e)
            self._cache[key] = seams
        return self._cache[key]

    @property
    def faces(self) -> Faces:
        return Faces(self, self._sub("face"))

    @property
    def edges(self) -> Edges:
        return Edges(self, self._sub("edge"))

    @property
    def vertices(self) -> Vertices:
        return Vertices(self, self._sub("vertex"))

    @property
    def solids(self) -> list["Shape"]:
        return [Shape(s) for s in self._sub("solid")]

    def _ids(self, kind: str, items) -> list[int]:
        sub = self._sub(kind)
        return [sub.index(s) for s in items]

    def face(self, i: int) -> Faces:
        """The face with stable id ``i`` (as shown in the viewer: ``f12``)."""
        return Faces(self, [self._sub("face")[i]])

    def edge(self, i: int) -> Edges:
        return Edges(self, [self._sub("edge")[i]])

    # ------------------------------------------------------------ groups, tags
    @property
    def tags(self) -> list[str]:
        return sorted(n for n, k in self._alias.items() if k.startswith("tag:"))

    @property
    def roles(self) -> list[str]:
        return sorted(n for n, k in self._alias.items() if not k.startswith("tag:"))

    def _group_items(self, key: str, kind: str) -> list:
        items = [s for s in self._groups.get(key, ()) if kind_of(s) == kind]
        if kind == "edge":
            items += [s for s in self._groups.get(key + ":edges", ()) if kind_of(s) == kind]
            if not items:
                for f in self._groups.get(key, ()):
                    if kind_of(f) == "face":
                        items.extend(subshapes(f, "edge"))
        elif kind == "vertex" and not items:
            for s in self._groups.get(key, ()):
                items.extend(subshapes(s, "vertex"))
        return items

    def _named(self, kind: str, name: str) -> Selection:
        key = self._alias.get(name)
        if key is None:
            avail = ", ".join(sorted(self._alias)) or "none"
            raise TuleError(
                f"this shape has no {kind} group named {name!r}",
                op="select",
                hint=f"available names: {avail}; a feature's roles do not carry through Booleans, "
                f"so select on the feature itself (e.g. boss.{kind}s[{name!r}]) or tag it",
            )
        keys = (key,)
        if kind == "edge" and key + ":edges" in self._groups:
            keys = (key + ":edges",)
        return SELECTION_TYPES[kind](self, self._group_items(key, kind), keys)

    def _resolve(self, sel: Selection, kind: str | None = None) -> Selection:
        """Find a selection made on another shape in this shape."""
        kind = kind or sel.kind
        cls = SELECTION_TYPES[kind]
        if sel.owner is self:
            return sel if sel.kind == kind else cls(self, sel.items, sel.keys)
        if sel.keys and all(k in self._groups or k.removesuffix(":edges") in self._groups for k in sel.keys):
            items = []
            for k in sel.keys:
                if k.endswith(":edges"):
                    items += [s for s in self._groups.get(k, ()) if kind_of(s) == kind]
                else:
                    items += self._group_items(k, kind)
            if not items:
                raise TuleError(
                    f"the selected {kind}s no longer exist in this shape",
                    op="select",
                    hint="an earlier operation consumed them; select on the current shape instead",
                )
            return cls(self, items, sel.keys)
        target = self._sub(kind)
        found = [s for s in sel.items if s in target]
        if found and len(found) == len(sel.items):
            return cls(self, found)
        raise TuleError(
            f"this {kind} selection belongs to another shape and cannot be found here",
            op="select",
            hint="select on this shape (e.g. part.edges...), use a feature's role selection, or tag it",
        )

    def _canonical(self, s):
        """The occurrence of sub-shape ``s`` in this shape (with its orientation here)."""
        sub = self._sub(kind_of(s))
        i = sub.index(s)
        return sub[i] if i >= 0 else None

    def _tags_of(self, s) -> list[str]:
        return sorted(n for n, k in self._alias.items() if k.startswith("tag:") and s in ShapeSet(self._groups.get(k, ())))

    def tag(self, name: str, selection: Selection) -> "Shape":
        """Tag faces or edges with a name that survives later operations.

        ``plate.tag("load", plate.faces.top())`` then ``part.faces["load"]``.
        """
        sel = self._resolve(selection, selection.kind)
        key = f"tag:{name}"
        groups = dict(self._groups)
        groups[key] = tuple(ShapeSet(list(groups.get(key, ())) + list(sel.items)))
        alias = dict(self._alias)
        alias[name] = key
        return Shape(self._s, groups, alias, self.name, self.color, self._sm)

    def _with_roles(self, roles: dict[str, Iterable], fid: int | None = None) -> "Shape":
        """Attach feature roles (``{"top": faces, ...}``) as groups and aliases."""
        fid = fid or new_feature_id()
        groups = dict(self._groups)
        alias = dict(self._alias)
        for role, items in roles.items():
            items = [self._canonical(x) for x in ShapeSet(items)]
            items = [x for x in items if x is not None]
            key = f"#{fid}.{role}"
            groups[key] = tuple(items)
            faces = [s for s in items if kind_of(s) == "face"]
            if faces:
                edges = ShapeSet()
                for f in faces:
                    for e in subshapes(f, "edge"):
                        edges.add(e)
                groups[key + ":edges"] = tuple(edges)
            if role not in alias or not alias[role].startswith("tag:"):
                alias[role] = key
        return Shape(self._s, groups, alias, self.name, self.color, self._sm)

    # ------------------------------------------------------------ Booleans
    def __or__(self, other: "Shape") -> "Shape":
        return union(self, other)

    __add__ = __or__

    def __radd__(self, other):
        if other == 0:  # sum([...])
            return self
        return union(other, self)

    def __sub__(self, other: "Shape") -> "Shape":
        return difference(self, other)

    def __and__(self, other: "Shape") -> "Shape":
        return intersection(self, other)

    def __xor__(self, other: "Shape") -> "Shape":
        return union(self, other) - intersection(self, other)

    # ------------------------------------------------------------ transforms
    def _apply_trsf(self, t: gp_Trsf, copy: bool = False) -> "Shape":
        if copy:
            b = BRepBuilderAPI_Transform(self._s, t, True)
            res = b.Shape()
            out = self._derive(res, carry(self._groups, res, lambda s: from_list(b.Modified(s))))
        else:  # a rigid move: the same solid, placed (as BRepBuilderAPI_Transform does), its groups when read
            loc = TopLoc_Location(t)
            out = self._derive(self._s.Moved(loc), _Moved(self._g, loc))
            if "bounds" in self._cache:
                _move_bounds(self._cache["bounds"], t, out._cache)
        if self._sm is not None:
            from .sheetmetal import moved

            out._sm = moved(self._sm, t)
        return out

    def at(self, x: float | VecLike = 0, y: float = 0, z: float = 0) -> "Shape":
        """Translate by ``(x, y, z)``; ``s.at((1, 2, 3))`` works too."""
        v = vec(x) if isinstance(x, (tuple, list, Vec)) else Vec(x, y, z)
        return self._apply_trsf(translation(v))

    translate = at

    def rotate(self, x: float = 0, y: float = 0, z: float = 0, about: VecLike = (0, 0, 0)) -> "Shape":
        """Rotate by degrees about x, then y, then z, around ``about``."""
        return self._apply_trsf(xyz_rotation(x, y, z, about))

    def rotate_about(self, axis_dir: VecLike, degrees: float, origin: VecLike = (0, 0, 0)) -> "Shape":
        from .geom import rotation

        return self._apply_trsf(rotation(axis_dir, degrees, origin))

    def scale(self, x: float, y: float | None = None, z: float | None = None, about: VecLike = (0, 0, 0)) -> "Shape":
        """Uniform ``scale(f)`` or per-axis ``scale(fx, fy, fz)``."""
        if self._sm is not None and not (x == 1 and y in (None, 1) and z in (None, 1)):
            return Shape(self._s, self._groups, self._alias, self.name, self.color).scale(x, y, z, about)
        if y is None and z is None:
            t = gp_Trsf()
            t.SetScale(pnt(about), float(x))
            return self._apply_trsf(t, copy=True)
        y = x if y is None else y
        z = x if z is None else z
        g = gp_GTrsf()
        g.SetVectorialPart(gp_Mat(float(x), 0, 0, 0, float(y), 0, 0, 0, float(z)))
        a = vec(about)
        g.SetTranslationPart(gp_XYZ(a[0] * (1 - x), a[1] * (1 - y), a[2] * (1 - z)))
        b = BRepBuilderAPI_GTransform(self._s, g, True)
        res = b.Shape()
        return self._derive(res, carry(self._groups, res, lambda s: from_list(b.Modified(s))))

    def mirror(self, nx: float | VecLike = 1, ny: float = 0, nz: float = 0, origin: VecLike = (0, 0, 0)) -> "Shape":
        """Mirror across the plane through ``origin`` with normal ``(nx, ny, nz)``."""
        n = vec(nx) if isinstance(nx, (tuple, list, Vec)) else Vec(nx, ny, nz)
        t = gp_Trsf()
        t.SetMirror(gp_Ax2(pnt(origin), gdir(n)))
        return self._apply_trsf(t, copy=True)

    def transform(self, matrix: Sequence[Sequence[float]]) -> "Shape":
        """Apply a 3x3, 3x4 or 4x4 (affine) matrix."""
        m = [list(map(float, r)) for r in matrix]
        if len(m) == 3 and len(m[0]) == 3:
            m = [r + [0.0] for r in m]
        # a rotation with one scale (a mirror too) keeps surfaces as they are; anything else (a stretch along
        # one axis, a shear) needs the general transform. SetValues would quietly make the matrix orthogonal.
        a = [r[:3] for r in m[:3]]
        det = (a[0][0] * (a[1][1] * a[2][2] - a[1][2] * a[2][1]) - a[0][1] * (a[1][0] * a[2][2] - a[1][2] * a[2][0])
               + a[0][2] * (a[1][0] * a[2][1] - a[1][1] * a[2][0]))
        if abs(det) < 1e-12:
            raise TuleError("transform: the matrix flattens the shape (its determinant is 0)", op="transform")
        s2 = abs(det) ** (2 / 3)
        aat = [[sum(a[i][k] * a[j][k] for k in range(3)) for j in range(3)] for i in range(3)]
        similar = all(abs(aat[i][j] - (s2 if i == j else 0.0)) <= 1e-9 * s2 for i in range(3) for j in range(3))
        if similar:
            t = gp_Trsf()
            t.SetValues(*m[0][:4], *m[1][:4], *m[2][:4])
            return self._apply_trsf(t, copy=True)
        g = gp_GTrsf()
        g.SetVectorialPart(gp_Mat(*m[0][:3], *m[1][:3], *m[2][:3]))
        g.SetTranslationPart(gp_XYZ(m[0][3], m[1][3], m[2][3]))
        b = BRepBuilderAPI_GTransform(self._s, g, True)
        res = b.Shape()
        return self._derive(res, carry(self._groups, res, lambda s: from_list(b.Modified(s))))

    # ------------------------------------------------------------ features
    def fillet(self, edges: Selection, radius: float) -> "Shape":
        """Round ``edges`` with ``radius``. New faces get the role ``fillet``."""
        return self._edge_feature("fillet", edges, radius)

    def chamfer(self, edges: Selection, distance: float) -> "Shape":
        """Bevel ``edges`` by ``distance``. New faces get the role ``chamfer``."""
        return self._edge_feature("chamfer", edges, distance)

    def _edge_feature(self, op: str, edges: Selection, size: float) -> "Shape":
        sel = self._resolve(edges, "edge")
        if not sel.items:
            raise TuleError("no edges selected", op=op)
        if fold := self._folding_arc(sel.items, size):
            e, r = fold
            word = "radius" if op == "fillet" else "distance"
            raise TuleError(f"cannot {op} with {size:g}: an edge runs around a convex {r:.3g} mm round, "
                            f"and a {word} that big folds over itself", op=op, edges=self._ids("edge", [e]),
                            hint=f"use a {word} below {r:.3g}, or make that rounded corner of the profile bigger",
                            **({"max_radius": round(r, 6)} if op == "fillet" else {"max_distance": round(r, 6)}))

        def attempt(r: float):
            mk = BRepFilletAPI_MakeFillet(self._s) if op == "fillet" else BRepFilletAPI_MakeChamfer(self._s)
            bad = []
            for e in sel.items:
                try:
                    mk.Add(float(r), e)
                except OCC_ERRORS as x:  # this edge cannot take one at all (not between exactly two faces)
                    bad.append((e, x))
            if bad:
                raise TuleError(f"cannot {op} {len(bad)} of the edges: {bad[0][1]}", op=op,
                                edges=self._ids("edge", [e for e, _ in bad]),
                                hint="leave these edges out (sel - part.edges[...]); an edge where more than two faces "
                                     "meet comes from tools touching along it: overlap them a little instead")
            try:
                mk.Build()
            except OCC_ERRORS:
                return None
            if not mk.IsDone():
                return None
            res = mk.Shape()
            if res.IsNull() or not BRepCheck_Analyzer(res).IsValid():
                return None
            return mk

        mk = attempt(size)
        if mk is None:
            lo, hi = 0.0, float(size)
            for _ in range(10):
                mid = (lo + hi) / 2
                if attempt(mid) is not None:
                    lo = mid
                else:
                    hi = mid
            hint = (
                f"largest {('radius' if op == 'fillet' else 'distance')} that works ≈ {lo:.3g}"
                if lo > 0
                else "no size works for this edge set; try fewer edges or fillet them in separate steps"
            )
            raise TuleError(f"could not {op} with {size:g}", op=op, edges=sel.ids, hint=hint,
                            **({"max_radius": round(lo, 6)} if op == "fillet" else {"max_distance": round(lo, 6)}))
        res = mk.Shape()
        groups = carry(self._groups, res, lambda s: from_list(mk.Modified(s)))
        new_faces = []
        for e in sel.items:
            new_faces.extend(f for f in from_list(mk.Generated(e)) if kind_of(f) == "face")
        out = self._derive(res, groups)
        return out._with_roles({op: new_faces})

    def _folding_arc(self, edges, size: float):
        """(edge, arc radius) for a circular edge along a convex round smaller than ``size``.

        Rolling a fillet that big around it makes a surface that folds over
        itself; OpenCascade still returns a "valid" solid, with slivers that mesh open.
        """
        from OCP.BRepAdaptor import BRepAdaptor_Curve, BRepAdaptor_Surface
        from OCP.BRepClass3d import BRepClass3d_SolidClassifier
        from OCP.GeomAbs import GeomAbs_Circle, GeomAbs_Plane
        from OCP.TopAbs import TopAbs_IN, TopAbs_REVERSED

        arcs = []
        for e in edges:
            c = BRepAdaptor_Curve(e)
            if c.GetType() == GeomAbs_Circle and c.Circle().Radius() <= size * (1 + 1e-9):
                arcs.append((e, c.Circle()))
        if not arcs:
            return None
        faces_of: dict[int, list] = {}
        want = ShapeSet(e for e, _ in arcs)
        for f in subshapes(self._s, "face"):
            for x in subshapes(f, "edge"):
                if (i := want.index(x)) >= 0:
                    faces_of.setdefault(i, []).append(f)
        for i, (e, circ) in enumerate(arcs):
            axis = circ.Axis().Direction()
            for f in faces_of.get(i, ()):
                s = BRepAdaptor_Surface(f)
                if s.GetType() != GeomAbs_Plane:
                    continue
                n = s.Plane().Axis().Direction()
                if abs(n.Dot(axis)) < 1 - 1e-6:
                    continue
                if f.Orientation() == TopAbs_REVERSED:
                    n.Reverse()
                r = circ.Radius()
                c = circ.Location()
                step = min(r, size) * 0.5
                p = gp_Pnt(c.X() - n.X() * step, c.Y() - n.Y() * step, c.Z() - n.Z() * step)
                if BRepClass3d_SolidClassifier(self._s, p, 1e-7).State() == TopAbs_IN:  # material at the center: convex
                    return e, r
        return None

    def shell(self, remove: Selection | None, thickness: float, inward: bool = True) -> "Shape":
        """Hollow the solid: remove the ``remove`` faces and keep walls of ``thickness``.

        Inner faces get the role ``inner``.
        """
        faces = self._resolve(remove, "face").items if remove is not None else []
        mk = BRepOffsetAPI_MakeThickSolid()
        off = -abs(thickness) if inward else abs(thickness)
        try:
            mk.MakeThickSolidByJoin(self._s, shape_list(faces), off, 1e-4)
            mk.Build()
            ok = mk.IsDone()
        except OCC_ERRORS:
            ok = False
        if not ok:
            raise TuleError(
                f"could not shell with thickness {thickness:g}",
                op="shell",
                faces=self._ids("face", faces),
                hint="try a smaller thickness, remove different faces, or shell before filleting",
            )
        res = mk.Shape()
        groups = carry(self._groups, res, lambda s: from_list(mk.Modified(s)))
        inner = []
        for f in self._sub("face"):
            inner.extend(g for g in from_list(mk.Generated(f)) if kind_of(g) == "face")
        return self._derive(res, groups)._with_roles({"inner": inner})

    # ------------------------------------------------------------ sheet metal
    @property
    def sheet_metal(self):
        """``SheetMetal(thickness, radius, k)`` for a sheet part (``sheet(...)``), else None."""
        return self._sm

    def flange(self, edges: Selection, length: float, angle: float = 90, radius: float | None = None,
               inset: float | Sequence[float] = 0, relief: str = "rect") -> "Shape":
        """Bend a flange from each selected edge of a sheet part (``sheet(profile, t)``).

        ``edges``: straight edges of the sheet, or its flat edge faces (``part.faces["side"]``).
        ``length`` is the outside length, from the virtual sharp to the tip (for 90°: the outside
        height); the flange's outside face is flush with the edge (the profile is the outside
        footprint). ``angle`` > 0 folds toward the top side, < 0 toward the bottom. ``radius``:
        inside radius (default: the sheet's). ``inset``: how far the flange stays back from the
        edge's ends (one value, or two: lower then upper end by x, y, z); a flange that stops
        short of the panel's end gets a ``relief`` cut (``"rect"`` or ``"obround"``, t wide,
        reaching t/2 past the bend). Where a flange meets an earlier one at a corner of the
        panel, both stop at each other's bend (a square corner relief). New faces get the roles
        ``flange``, ``bend`` and ``tip`` (the end faces: flange them for a return).
        """
        from .sheetmetal import flange

        return flange(self, edges, length, angle, radius, inset, relief)

    def unfold(self) -> "Shape":
        """The flat pattern of a sheet part: a plate ``t`` thick in the xy plane, reference (bottom)
        side at z = 0, up-bends folding toward +z. Its ``bends`` give each bend's direction,
        angle, inside radius, allowance and line; the lines are edges of the top face, tagged
        ``bend_up`` / ``bend_down``. Holes and cuts on the part, also across bends, are in it."""
        if "unfold" not in self._cache:
            from .sheetmetal import unfold

            self._cache["unfold"] = unfold(self)
        flat = self._cache["unfold"]  # shared with renamed copies of this part: under this one's name and color
        return flat if (flat.name, flat.color) == (self.name, self.color) else flat._renamed(self.name, self.color)

    @property
    def bends(self) -> list:
        """The bends of a sheet part or flat pattern (``Bend``: id, direction, angle, radius, k,
        allowance, deduction, line in the flat), in reading order of the flat (B1 top left)."""
        from .sheetmetal import bends

        return bends(self)

    def bend_table(self) -> list[dict]:
        """The bend table for the shop: one dict per bend (bend, direction, angle, radius, k,
        thickness, allowance, deduction, length)."""
        from .sheetmetal import bend_table

        return bend_table(self)

    def dfm(self, min_flange: float | None = None, hole_to_bend: float | None = None,
            hole_to_edge: float | None = None):
        """Check a sheet part (or its flat) against the rules of a laser-and-brake shop; returns a
        ``SheetCheck`` (``ok``, ``issues``; ``print`` it). Measured in the flat pattern, in mm:

        * ``min_flange``: from each bend line to the outline across it, on both sides, met by at
          least half the bend's length. Default ``2t + BA/2``: an outside flange length of
          ``OSSB + 2t`` (4t at R = t), the least a press brake's die can hold.
        * ``hole_to_bend``: from each hole, slot or cutout, and each notch in the outline, to a bend
          line within the bend's extent. Default ``2t + R``: closer, the cut distorts in the bend.
        * ``hole_to_edge``: from each hole to the outline and to the next hole. Default ``t``
          (laser portals want bridges of 1–1.5 t).

        Give your shop's values for the material and thickness to override. Violations are results,
        not errors."""
        from .sheetmetal import dfm

        return dfm(self, min_flange, hole_to_bend, hole_to_edge)

    # ------------------------------------------------------------ measurements
    @property
    def volume(self) -> float:
        p = GProp_GProps()
        static(BRepGProp, "VolumeProperties")(self._s, p)
        return p.Mass()

    @property
    def area(self) -> float:
        p = GProp_GProps()
        static(BRepGProp, "SurfaceProperties")(self._s, p)
        return p.Mass()

    def distance(self, other: "Shape") -> float:
        """The smallest gap to ``other``; 0 when they touch or overlap."""
        return self.closest(other)[0]

    def closest(self, other: "Shape") -> tuple[float, Vec, Vec]:
        """``(gap, point on self, point on other)``: the closest points. Touching or overlapping: 0 and
        a point where they meet (inside both when one holds the other)."""
        from .clearance import _extrema

        gap, p, q = _extrema(self._s, other._s, threads=True)
        return gap, Vec(*p), Vec(*q)

    @property
    def center_of_mass(self) -> Vec:
        p = GProp_GProps()
        static(BRepGProp, "VolumeProperties")(self._s, p)
        return from_pnt(p.CentreOfMass())

    @property
    def center(self) -> Vec:
        """The middle of the bounds (``center_of_mass`` is the centroid)."""
        lo, hi = self.bounds
        return (lo + hi) * 0.5

    @property
    def bounds(self) -> tuple[Vec, Vec]:
        """Tight axis-aligned bounds ``(lo, hi)``."""
        lohi = self._cache.get("bounds")
        if lohi is None:  # exact bounds of a vendor board take seconds: once per shape
            b = Bnd_Box()
            static(BRepBndLib, "AddOptimal")(self._s, b, False, False)
            if b.IsVoid():
                raise TuleError(f"{self.name or 'the shape'} is empty: it has no bounds", op="bounds",
                                hint="the operation that made it left nothing: check len(part.faces) after each step")
            lohi = self._cache["bounds"] = (from_pnt(b.CornerMin()), from_pnt(b.CornerMax()))
        return lohi

    @property
    def size(self) -> Vec:
        lo, hi = self.bounds
        return hi - lo

    def mass(self, density: float) -> float:
        """Mass for a density in mass per unit volume (e.g. g/mm³: aluminium ≈ 0.0027)."""
        return self.volume * density

    @property
    def is_valid(self) -> bool:
        return BRepCheck_Analyzer(self._s).IsValid()

    # ------------------------------------------------------------ export
    def export_step(self, path: str) -> str:
        from .io import export_step

        return export_step(self, path)

    def export_stl(self, path: str, tolerance: float = 0.05, angular: float = 0.2) -> str:
        from .io import export_stl

        return export_stl(self, path, tolerance, angular)

    def export_dxf(self, path: str, notes: bool = False, bends: bool = False) -> str:
        """A sheet part's flat pattern as a DXF to cut from, in mm: closed outlines on layer CUT, nothing
        else (``bends=True`` adds bend lines on BEND_UP / BEND_DOWN, ``notes=True`` their labels)."""
        from .sheetmetal import export_dxf

        return export_dxf(self, path, notes, bends)

    def export_gltf(self, path: str) -> str:
        from .io import export_gltf

        return export_gltf(self, path)

    def tessellate(self, tolerance: float = 0.05, angular: float = 0.2):
        from .io import tessellate

        return tessellate(self, tolerance, angular)


# ---------------------------------------------------------------- Booleans


def _as_shape(x) -> Shape:
    if isinstance(x, Shape):
        return x
    from .assembly import Assembly

    if isinstance(x, Assembly):
        return x.to_shape()
    raise TypeError(f"expected a Shape, got {type(x).__name__}")


def _flatten(shapes) -> list[Shape]:
    out = []
    for s in shapes:
        if isinstance(s, (list, tuple)):
            out.extend(_flatten(s))
        else:
            out.append(_as_shape(s))
    return out


def _boolean(op_cls, op_name: str, args: list[Shape], tools: list[Shape], simplify: bool = True) -> Shape:
    op = op_cls()
    op.SetArguments(shape_list([a._s for a in args]))
    op.SetTools(shape_list([t._s for t in tools]))
    op.SetRunParallel(True)
    try:
        op.Build()
    except OCC_ERRORS as e:  # pragma: no cover - OCCT internal failure
        raise TuleError(str(e), op=op_name) from None
    has_errors = getattr(op, "HasErrors", None)
    if not op.IsDone() or (has_errors is not None and has_errors()):
        raise TuleError(
            "OpenCascade reported an error",
            op=op_name,
            hint="check that inputs are valid solids (s.is_valid); nearly coincident faces can fail, "
            "try moving one input by a small amount",
        )
    inputs = args + tools
    try:
        res = op.Shape()
        groups = carry(_merge_groups(*inputs), res, lambda s: from_list(op.Modified(s)))
    except OCC_ERRORS as e:
        raise TuleError(f"{type(e).__name__}: {e}", op=op_name,
                        hint="coincident or tangent faces: move one input by 0.01 mm, or make the tool a little bigger") from None
    if simplify:
        try:  # merging coplanar faces can fail on odd geometry: the result is right without it
            u = ShapeUpgrade_UnifySameDomain(res, True, True, False)
            u.Build()
            res2 = u.Shape()
            hist = u.History()
            groups = carry(groups, res2, lambda s: from_list(hist.Modified(s)))
            res = res2
        except OCC_ERRORS:
            pass
    first = args[0]
    return Shape(res, groups, _tag_aliases(*inputs), first.name, first.color, first._sm)


def union(*shapes) -> Shape:
    """Union of any number of shapes (``union(a, b, c)`` or ``union([a, b, c])``)."""
    s = _flatten(shapes)
    if len(s) == 1:
        return s[0]
    return _boolean(BRepAlgoAPI_Fuse, "union", s[:1], s[1:])


def difference(first, *others) -> Shape:
    """``first`` minus all ``others``."""
    o = _flatten(others)
    f = _as_shape(first)
    if not o:
        return f
    return _boolean(BRepAlgoAPI_Cut, "difference", [f], o)


def intersection(*shapes) -> Shape:
    s = _flatten(shapes)
    out = s[0]
    for t in s[1:]:
        out = _boolean(BRepAlgoAPI_Common, "intersection", [out], [t])
    return out
