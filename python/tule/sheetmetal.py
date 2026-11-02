"""Sheet metal: parts bent from one sheet, their flat patterns, bend tables and cut files.

```python
from tule import *

tray = rect(120, 80).sheet(1.5, bend_radius=1.5, k=0.44)        # a sheet part: t, R and K stay on it
tray = tray.flange(tray.faces["side"], 20)                       # four 20 mm walls, corner reliefs
tray -= cylinder(2.5, 10).at(60, 40, -1)                         # cuts are ordinary Booleans
flat = tray.unfold()                                             # the blank, bend lines on its top face
print(flat.size, [b.label for b in flat.bends])                  # UP 90° R1.5, ...
outputs.drawing("tray-flat", flat, dxf="top")                    # bend lines, bend table, layered DXF
```

The model is the formed part; the flat pattern is derived from it, so a design
change is made once. Conventions:

* The sheet starts as a profile in the xy plane extruded by ``t`` along +z. Its
  bottom face is the *reference side*; it continues around every bend. In the
  flat pattern the reference side is at z = 0 and the top side faces +z, so a
  bend is UP when it folds toward +z (the viewer looking at the DXF).
* ``flange(edge, length)``: the flange's outside face is flush with the edge it
  is put on (material inside: the profile is the outside footprint) and
  ``length`` is measured on the outside, from the virtual sharp (where the
  outside faces of sheet and flange meet) to the flange tip: for 90° this is
  the outside height. Positive ``angle`` folds toward the top side.
* Bend allowance ``BA = angle · (R + K·t)`` (angle in radians, ``R`` the inside
  radius), outside setback ``OSSB = (R + t) · tan(angle / 2)``, bend deduction
  ``BD = 2 · OSSB − BA``: the flat is the sum of the outside lengths less one BD
  per bend.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Sequence

from OCP.BRep import BRep_Tool
from OCP.BRepAdaptor import BRepAdaptor_Curve, BRepAdaptor_Surface
from OCP.BRepAlgoAPI import BRepAlgoAPI_Common, BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse, BRepAlgoAPI_Splitter
from OCP.BRepBuilderAPI import (BRepBuilderAPI_MakeEdge, BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakePolygon,
                                BRepBuilderAPI_Transform)
from OCP.BRepClass3d import BRepClass3d_SolidClassifier
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder, BRepPrimAPI_MakePrism, BRepPrimAPI_MakeRevol
from OCP.BRepTools import BRepTools, BRepTools_WireExplorer
from OCP.GeomAbs import GeomAbs_Circle, GeomAbs_Cylinder, GeomAbs_Line, GeomAbs_Plane
from OCP.gp import gp_Ax1, gp_Ax2, gp_Ax3, gp_Dir, gp_Pnt, gp_Trsf, gp_Vec
from OCP.ShapeUpgrade import ShapeUpgrade_UnifySameDomain
from OCP.TopAbs import TopAbs_IN, TopAbs_REVERSED

from ._occ import cast, kind_of, shape_list, static, subshapes
from .errors import OCC_ERRORS, TuleError
from .geom import Vec
from .selection import Edges, Selection, edge_length, face_area

if TYPE_CHECKING:  # pragma: no cover
    from .profile import Profile
    from .shape import Shape

REF = "sheet:ref"  # the group holding the reference (bottom) side of the first panel


# ---------------------------------------------------------------- values


@dataclass(frozen=True)
class SheetMetal:
    """What a sheet part remembers: thickness ``t``, default inside bend radius, K-factor.

    A flat pattern (``flat=True``) also carries its ``bends``."""

    thickness: float
    radius: float
    k: float
    flat: bool = False
    bends: tuple = ()
    frame: tuple = (1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0)  # moves since sheet(), 3×4

    def _trsf(self) -> gp_Trsf:
        t = gp_Trsf()
        t.SetValues(*self.frame)
        return t


@dataclass(frozen=True)
class Bend:
    """One bend of a sheet part, as the shop needs it.

    ``direction`` is ``"up"`` (toward the flat pattern's top side, +z) or ``"down"``; ``angle``
    is the bend angle in degrees (90 for a right-angle flange, 0 would be flat); ``radius`` the
    inside radius. ``line`` is the bend center line in the flat pattern ``((x0, y0), (x1, y1))``,
    ``zone`` the two tangent lines that bound the bend region there.
    """

    id: str
    direction: str
    angle: float
    radius: float
    k: float
    thickness: float
    line: tuple
    zone: tuple = field(default=(), repr=False)

    @property
    def allowance(self) -> float:
        """BA = angle · (R + K·t): the developed length of the bend on the neutral axis."""
        return math.radians(self.angle) * (self.radius + self.k * self.thickness)

    @property
    def setback(self) -> float:
        """OSSB = (R + t) · tan(angle / 2): from a tangent line to the outside virtual sharp."""
        return (self.radius + self.thickness) * math.tan(math.radians(self.angle) / 2)

    @property
    def deduction(self) -> float:
        """BD = 2 · OSSB − BA: subtract it from the sum of the outside flange lengths."""
        return 2 * self.setback - self.allowance

    @property
    def length(self) -> float:
        (x0, y0), (x1, y1) = self.line
        return math.hypot(x1 - x0, y1 - y0)

    @property
    def label(self) -> str:
        """``UP 90° R1.5``: what goes beside the bend line."""
        return f"{self.direction.upper()} {_num(self.angle, 3)}° R{_num(self.radius, 3)}"

    def row(self) -> dict:
        return {"bend": self.id, "direction": self.direction, "angle": round(self.angle, 3),
                "radius": round(self.radius, 3), "k": round(self.k, 3), "thickness": round(self.thickness, 3),
                "allowance": round(self.allowance, 3), "deduction": round(self.deduction, 3),
                "length": round(self.length, 3)}


def _num(v: float, digits: int = 2) -> str:
    s = f"{round(float(v), digits):.{digits}f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


# ---------------------------------------------------------------- making a sheet


def sheet(profile: "Profile", thickness: float, bend_radius: float | None = None, k: float = 0.44) -> "Shape":
    """A sheet part: ``profile`` (xy plane) extruded by ``thickness`` along +z.

    ``bend_radius`` (inside radius, default ``thickness``) and ``k`` (K-factor: where the neutral
    axis lies, as a fraction of ``t`` from the inside; 0.44 suits air-bent mild steel with R ≈ t,
    ask the shop for theirs) are remembered for ``flange`` and ``unfold``. The bottom face is the
    reference side; faces get the roles ``top``, ``bottom`` and ``side`` (the sheet's edges).
    """
    t = float(thickness)
    if not t > 0:
        raise TuleError(f"thickness must be positive, got {thickness}", op="sheet")
    r = t if bend_radius is None else float(bend_radius)
    if not r > 0:
        raise TuleError(f"bend_radius must be positive, got {bend_radius}", op="sheet",
                        hint=f"a sharp bend still has an inside radius; use at least {_num(0.5 * t)} (t/2), "
                             f"commonly {_num(t)} (t)")
    if not 0 <= k <= 1:
        raise TuleError(f"k is a fraction of the thickness (0 to 1), got {k}", op="sheet",
                        hint="typical K-factors are 0.3 to 0.5; 0.44 for air bending steel with R ≈ t")
    part = profile.extrude(t)
    groups = dict(part._groups)
    groups[REF] = tuple(part.faces["bottom"].items)
    from .shape import Shape

    return Shape(part.wrapped, groups, part._alias, part.name, part.color, sm=SheetMetal(t, r, float(k)))


def moved(sm: SheetMetal, t: gp_Trsf) -> SheetMetal | None:
    """Sheet data after a transform: a formed part keeps it; a flat pattern keeps its bend lines
    through moves within the xy plane and is a plain plate after any other."""
    if not sm.flat:
        m = t.Multiplied(sm._trsf())
        return replace(sm, frame=tuple(m.Value(i, j) for i in (1, 2, 3) for j in (1, 2, 3, 4)))
    z = _apply_vec(t, Vec(0, 0, 1))
    if z.z < 1 - 1e-9 or abs(_apply(t, Vec(0, 0, 0)).z) > 1e-9 or abs(t.ScaleFactor() - 1) > 1e-12:
        return None

    def p2(x, y):
        q = _apply(t, Vec(x, y, 0))
        return (round(q.x, 9), round(q.y, 9))

    bends = tuple(replace(b, line=tuple(p2(*p) for p in b.line), zone=tuple(tuple(p2(*p) for p in z_) for z_ in b.zone))
                  for b in sm.bends)
    return replace(sm, bends=bends)


def _need(part: "Shape", op: str) -> SheetMetal:
    sm = part._sm
    if sm is None:
        raise TuleError("this shape is not a sheet part", op=op,
                        hint="make it with sheet(profile, thickness) or profile.sheet(thickness); "
                             "Booleans keep it a sheet part when it is their first operand")
    return sm


# ---------------------------------------------------------------- small geometry helpers


def _v(p) -> Vec:
    return Vec(p.X(), p.Y(), p.Z())


def _pnt(p) -> gp_Pnt:
    return gp_Pnt(float(p[0]), float(p[1]), float(p[2]))


def _dir(p) -> gp_Dir:
    return gp_Dir(float(p[0]), float(p[1]), float(p[2]))


def _vertex_pnt(v) -> Vec:
    return _v(static(BRep_Tool, "Pnt")(v))


def _plane(f):
    """(point, outward normal) of a planar face."""
    s = BRepAdaptor_Surface(f)
    pl = s.Plane()
    n = _v(pl.Axis().Direction())
    if not pl.Position().Direct():  # a left-handed frame: the surface normal is the other way
        n = -n
    if f.Orientation() == TopAbs_REVERSED:
        n = -n
    return _v(pl.Location()), n


def _cyl(f):
    """(axis point, unit axis, radius, direct) of a cylindrical face."""
    c = BRepAdaptor_Surface(f).Cylinder()
    ax = c.Axis()
    return _v(ax.Location()), _v(ax.Direction()), c.Radius(), c.Position().Direct()


def _ftype(f) -> str:
    t = BRepAdaptor_Surface(f).GetType()
    return "plane" if t == GeomAbs_Plane else "cylinder" if t == GeomAbs_Cylinder else "other"


def _radial(p: Vec, a: Vec, d: Vec) -> Vec:
    q = p - a
    return (q - d * q.dot(d)).normalized()


def _normal(f, p: Vec) -> Vec | None:
    """Outward normal of a plane or cylinder face at (or near) ``p``."""
    typ = _ftype(f)
    if typ == "plane":
        return _plane(f)[1]
    if typ == "cylinder":
        a, d, r, direct = _cyl(f)
        n = _radial(p, a, d) * (1 if direct else -1)
        return -n if f.Orientation() == TopAbs_REVERSED else n
    return None


def _edge_mid(e) -> Vec:
    c = BRepAdaptor_Curve(e)
    return _v(c.Value((c.FirstParameter() + c.LastParameter()) / 2))


def _edge_ends(e) -> tuple[Vec, Vec]:
    c = BRepAdaptor_Curve(e)
    return _v(c.Value(c.FirstParameter())), _v(c.Value(c.LastParameter()))


def _is_line(e) -> bool:
    return BRepAdaptor_Curve(e).GetType() == GeomAbs_Line


def _line_dir(e) -> Vec:
    d = BRepAdaptor_Curve(e).Line().Direction()
    return Vec(d.X(), d.Y(), d.Z())


def _volume(s) -> float:
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps

    p = GProp_GProps()
    static(BRepGProp, "VolumeProperties")(s, p)
    return p.Mass()


def _area(s) -> float:
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps

    p = GProp_GProps()
    static(BRepGProp, "SurfaceProperties")(s, p)
    return p.Mass()


def _common_volume(a, b) -> float:
    op = BRepAlgoAPI_Common(a, b)
    op.Build()
    return _volume(op.Shape()) if op.IsDone() else 0.0


def _rotation(a: Vec, d: Vec, angle: float) -> gp_Trsf:
    t = gp_Trsf()
    t.SetRotation(gp_Ax1(_pnt(a), _dir(d)), angle)
    return t


def _translation(v: Vec) -> gp_Trsf:
    t = gp_Trsf()
    t.SetTranslation(gp_Vec(*v))
    return t


def _apply(t: gp_Trsf, p: Vec) -> Vec:
    return _v(_pnt(p).Transformed(t))


def _apply_vec(t: gp_Trsf, v: Vec) -> Vec:
    return _v(gp_Vec(*v).Transformed(t))


# ---------------------------------------------------------------- the reference side


class _Skin:
    """The reference side of a sheet part, walked from the seed face across tangent edges.

    ``planes``: face index -> transform into the flat (z = 0); ``bends``: face index -> bend
    data relative to the plane it was reached from; ``top``: the planar faces of the other side.
    """

    def __init__(self, part: "Shape", sm: SheetMetal, op: str):
        self.part, self.sm, self.op = part, sm, op
        self.faces = part._sub("face")
        self.edges = part._sub("edge")
        self.edge_faces: dict[int, list[int]] = {}
        self.face_edges: list[list[int]] = []
        for fi, f in enumerate(self.faces):
            es = []
            for e in subshapes(f, "edge"):
                ei = self.edges.index(e)
                if ei < 0:
                    continue
                es.append(ei)
                lst = self.edge_faces.setdefault(ei, [])
                if fi not in lst:
                    lst.append(fi)
            self.face_edges.append(es)
        self.planes: dict[int, gp_Trsf] = {}
        self.bends: dict[int, dict] = {}
        self._walk()
        self._find_top()

    # -- the walk
    def _seed(self) -> int:
        cands = [self.faces.index(f) for f in self.part._groups.get(REF, ()) if kind_of(f) == "face"]
        cands = [i for i in cands if i >= 0 and _ftype(self.faces[i]) == "plane"]
        if not cands:
            raise TuleError("the sheet's reference (bottom) face is gone", op=self.op,
                            hint="an operation removed the first panel's bottom face; keep part of the base sheet")
        return max(cands, key=lambda i: face_area(self.faces[i]))

    def _same_side(self, a: int, b: int, e: int) -> bool:
        m = _edge_mid(self.edges[e])
        na, nb = _normal(self.faces[a], m), _normal(self.faces[b], m)
        return na is not None and nb is not None and na.dot(nb) > math.cos(math.radians(0.5))

    def _walk(self):
        seed = self._seed()
        p0, n0 = _plane(self.faces[seed])
        back = self.sm._trsf().Inverted()  # the part as it was made: the base sheet on z = 0
        if abs(_apply(back, p0).z) < 1e-6 and _apply_vec(back, n0).z < -1 + 1e-9:
            self.planes[seed] = back
        else:
            self.planes[seed] = self._place(p0, n0)
        queue = [seed]
        self._bfs(queue)

    @staticmethod
    def _place(p0: Vec, n0: Vec) -> gp_Trsf:
        top = -n0  # the top side, toward +z in the flat
        place = gp_Trsf()
        axis = top.cross(Vec(0, 0, 1))
        if axis.length > 1e-9:
            place = _rotation(Vec(0, 0, 0), axis.normalized(), math.acos(max(-1.0, min(1.0, top.z))))
        elif top.z < 0:
            place = _rotation(Vec(0, 0, 0), Vec(1, 0, 0), math.pi)
        z = _apply(place, p0).z
        return _translation(Vec(0, 0, -z)).Multiplied(place)

    def _bfs(self, queue):
        while queue:
            fi = queue.pop(0)
            for ei in self.face_edges[fi]:
                for gi in self.edge_faces.get(ei, ()):
                    if gi == fi or gi in self.planes or gi in self.bends:
                        continue
                    if not self._same_side(fi, gi, ei):
                        continue
                    typ = _ftype(self.faces[gi])
                    if fi in self.planes:
                        if typ == "plane":
                            self.planes[gi] = self.planes[fi]
                        elif typ == "cylinder":
                            self.bends[gi] = self._bend(fi, gi, ei)
                        else:
                            self._unsupported(gi)
                        queue.append(gi)
                    else:  # from a bend
                        if typ != "plane":
                            raise TuleError("two bends follow each other without a flat between them", op=self.op,
                                            faces=self.part._ids("face", [self.faces[gi]]),
                                            hint="unfold supports bends joined by flat panels; leave a flat of at "
                                                 "least t between bends")
                        self.planes[gi] = self._across(fi, ei)
                        queue.append(gi)

    def _unsupported(self, gi):
        raise TuleError(f"the sheet has a {BRepAdaptor_Surface(self.faces[gi]).GetType()} face where a flat or a "
                        "cylindrical bend should be", op=self.op, faces=self.part._ids("face", [self.faces[gi]]),
                        hint="sheet parts unfold from flat panels joined by cylindrical bends (flange); "
                             "fillets, cones and freeform faces cannot be flattened")

    def _bend(self, fi: int, gi: int, ei: int) -> dict:
        _f, g, e = self.faces[fi], self.faces[gi], self.edges[ei]
        a, d, r, _ = _cyl(g)
        if not _is_line(e) or abs(_line_dir(e).dot(d)) < 1 - 1e-6:
            raise TuleError("a bend meets a flat along a curved or skewed edge", op=self.op,
                            faces=self.part._ids("face", [g]),
                            hint="bends must run straight along their axis; cut odd shapes after flanging")
        t, k = self.sm.thickness, self.sm.k
        m = _edge_mid(e)
        rho_e = _radial(m, a, d)
        n_mid = _normal(g, m)
        convex = n_mid.dot(rho_e) > 0  # the reference side outside: folds toward the top side
        R = r - t if convex else r
        if R <= 1e-9:
            raise TuleError("a bend's inside radius is zero or negative", op=self.op, faces=self.part._ids("face", [g]))
        # the angle around the axis, positive into the bend face
        cen = BRepAdaptor_Surface(g)
        u0, u1, v0, v1 = static(BRepTools, "UVBounds")(g)
        inside = _v(cen.Value((u0 + u1) / 2, (v0 + v1) / 2))
        rho_c = _radial(inside, a, d)
        ds = d if rho_e.cross(rho_c).dot(d) >= 0 else -d
        w = ds.cross(rho_e)  # tangent of the bend at e, pointing into it (lies in f's plane)
        info = {"a": a, "d": d, "ds": ds, "r": r, "R": R, "up": convex, "m": m, "rho": rho_e, "w": w,
                "T": self.planes[fi], "from": fi, "nf": R + k * t}
        angles = [self.phi(info, _vertex_pnt(v)) for v in subshapes(g, "vertex")]
        info["angle"] = max(angles)
        return info

    @staticmethod
    def phi(info, p: Vec) -> float:
        rp = _radial(p, info["a"], info["d"])
        return math.atan2(info["rho"].cross(rp).dot(info["ds"]), info["rho"].dot(rp))

    def _across(self, ci: int, ei: int) -> gp_Trsf:
        """The flat transform of a plane reached from bend ``ci`` over edge ``ei``."""
        b = self.bends[ci]
        e = self.edges[ei]
        if not _is_line(e):
            raise TuleError("a bend meets a flat along a curved edge", op=self.op,
                            faces=self.part._ids("face", [self.faces[ci]]))
        x = _edge_mid(e)
        ph = self.phi(b, x)
        if abs(ph) < 1e-9:
            return b["T"]
        target = b["m"] + b["d"] * (x - b["m"]).dot(b["d"])
        best = None
        for s in (1, -1):
            rot = _rotation(b["a"], b["d"], s * ph)
            err = (_apply(rot, x) - target).length
            if best is None or err < best[0]:
                best = (err, rot)
        move = _translation(b["w"] * (ph * b["nf"]))
        return b["T"].Multiplied(move.Multiplied(best[1]))

    def _find_top(self):
        t = self.sm.thickness
        self.top: set[int] = set()
        refs = [(fi, *_plane(self.faces[fi])) for fi in self.planes]
        for fi, f in enumerate(self.faces):
            if fi in self.planes or fi in self.bends or _ftype(f) != "plane":
                continue
            p, n = _plane(f)
            for _, pr, nr in refs:
                if n.dot(nr) < -1 + 1e-9 and abs((p - pr).dot(nr) + t) < 1e-6 * max(1.0, t):
                    self.top.add(fi)
                    break

    # -- flat pattern
    def flat_point(self, info, p: Vec) -> Vec:
        q = info["m"] + info["d"] * (p - info["m"]).dot(info["d"])
        return _apply(info["T"], q + info["w"] * (self.phi(info, p) * info["nf"]))


# ---------------------------------------------------------------- flanges


@dataclass
class _EdgeSpec:
    a: Vec        # a point of the edge on the reference side
    d: Vec        # unit direction along it
    lo: float     # extent along d from a
    hi: float
    top: Vec      # the top side's direction (normal of the panel)
    out: Vec      # outward, in the panel's plane, away from the material


def _edge_spec(part: "Shape", skin: _Skin, e, other) -> _EdgeSpec | None:
    """The sheet edge along line ``e`` between a skin face and the edge face ``other``."""
    ei = skin.edges.index(e)
    fs = skin.edge_faces.get(ei, [])
    t = skin.sm.thickness
    for fi in fs:
        if fi == other or (other is not None and other not in fs):
            continue
        rest = [g for g in fs if g != fi]
        if len(rest) != 1:
            continue
        g = rest[0]
        if fi in skin.planes:
            top, shift = -_plane(skin.faces[fi])[1], Vec(0, 0, 0)
        elif fi in skin.top:
            top = _plane(skin.faces[fi])[1]
            shift = -top * t
        else:
            continue
        if _ftype(skin.faces[g]) != "plane":
            return None
        out = _plane(skin.faces[g])[1]
        if abs(out.dot(top)) > 1e-6:
            return None
        p, q = _edge_ends(e)
        d = (q - p).normalized()
        return _EdgeSpec(p + shift, d, 0.0, (q - p).length, top, out)
    return None


def _resolve_edges(part: "Shape", where, skin: _Skin, op: str) -> list[_EdgeSpec]:
    if not isinstance(where, Selection):
        raise TuleError("pass the edges to flange as a selection: part.edges... or edge faces part.faces...", op=op)
    if where.kind not in ("edge", "face"):
        raise TuleError("flange takes edges or faces", op=op)
    sel = part._resolve(where, where.kind)
    if not sel.items:
        raise TuleError("nothing selected to flange", op=op)
    specs: list[_EdgeSpec] = []
    hint = ("select straight edges where the sheet's top or bottom face meets an edge face "
            "(part.edges...), or the flat edge faces themselves (part.faces['side'] on a fresh sheet)")
    if sel.kind == "edge":
        for e in sel.items:
            sp = _edge_spec(part, skin, e, None) if _is_line(e) else None
            if sp is None:
                raise TuleError("this edge is not a straight edge of the sheet", op=op,
                                edges=part._ids("edge", [e]), hint=hint)
            specs.append(sp)
    else:
        for f in sel.items:
            fi = skin.faces.index(f)
            if _ftype(f) != "plane" or fi in skin.planes or fi in skin.top:
                raise TuleError("this face is not a flat edge face of the sheet", op=op,
                                faces=part._ids("face", [f]), hint=hint)
            found = []
            for e in subshapes(f, "edge"):
                if not _is_line(e):
                    continue
                ei = skin.edges.index(e)
                if any(g in skin.planes for g in skin.edge_faces.get(ei, ()) if g != fi):
                    sp = _edge_spec(part, skin, skin.edges[ei], fi)
                    if sp is not None and edge_length(e) > skin.sm.thickness * 1.01:
                        found.append(sp)
            if not found:
                raise TuleError("this face has no straight edge on the sheet's bottom face", op=op,
                                faces=part._ids("face", [f]), hint=hint)
            base = found[0]
            if any(abs(s.d.dot(base.d)) < 1 - 1e-9 or (s.a - base.a).cross(base.d).length > 1e-6 for s in found[1:]):
                raise TuleError("this edge face runs along more than one sheet edge (it merged with a bend or flange "
                                "beside it)", op=op, faces=part._ids("face", [f]),
                                hint="select the straight edge instead: part.edges.on(face) ... or flange all edges "
                                     "in one call on the fresh sheet")
            ts = [(p - base.a).dot(base.d) for s in found for p in (s.a + s.d * s.lo, s.a + s.d * s.hi)]
            specs.append(_EdgeSpec(base.a, base.d, min(ts), max(ts), base.top, base.out))
    return specs


def _current(part: "Shape", skin: _Skin, spec: _EdgeSpec, op: str) -> tuple[float, float]:
    """The extent of ``spec``'s line on the part as it is now (earlier flanges shorten it)."""
    ts = []
    tol = 1e-6 * max(1.0, spec.hi - spec.lo)
    for fi in skin.planes:
        for ei in skin.face_edges[fi]:
            e = skin.edges[ei]
            if not _is_line(e) or abs(_line_dir(e).dot(spec.d)) < 1 - 1e-9:
                continue
            p, q = _edge_ends(e)
            if (p - spec.a).cross(spec.d).length > tol * 10:
                continue
            others = [g for g in skin.edge_faces.get(ei, ()) if g != fi]
            if not any(_ftype(skin.faces[g]) == "plane" and _plane(skin.faces[g])[1].dot(spec.out) > 1 - 1e-6
                       for g in others):
                continue
            s0, s1 = sorted(((p - spec.a).dot(spec.d), (q - spec.a).dot(spec.d)))
            if s1 > spec.lo + tol and s0 < spec.hi - tol:
                ts += [max(s0, spec.lo), min(s1, spec.hi)]
    if not ts:
        raise TuleError("the edge to flange is gone: an earlier flange or cut took it", op=op,
                        hint="flange each edge once; select edges on the current part")
    return min(ts), max(ts)


def _box(frame: gp_Trsf, x0, x1, y0, y1, z0, z1):
    lo = (min(x0, x1), min(y0, y1), min(z0, z1))
    b = BRepPrimAPI_MakeBox(gp_Pnt(*lo), abs(x1 - x0), abs(y1 - y0), abs(z1 - z0)).Shape()
    return BRepBuilderAPI_Transform(b, frame, True).Shape()


def flange(part: "Shape", where, length: float, angle: float = 90, radius: float | None = None,
           inset: float | Sequence[float] = 0, relief: str = "rect") -> "Shape":
    """Bend a flange up from each selected sheet edge. See :meth:`Shape.flange`."""
    from .shape import Shape, new_feature_id

    op = "flange"
    sm = _need(part, op)
    if sm.flat:
        raise TuleError("this is a flat pattern; flange the formed part", op=op)
    t = sm.thickness
    R = sm.radius if radius is None else float(radius)
    if not R > 0:
        raise TuleError(f"radius must be positive, got {radius}", op=op)
    a = math.radians(abs(float(angle)))
    if not 0 < abs(angle) < 180:
        raise TuleError(f"angle must be between -180 and 180 degrees and not 0, got {angle}", op=op,
                        hint="positive folds toward the top side, negative toward the bottom (reference) side")
    if relief not in ("rect", "obround"):
        raise TuleError(f"relief is 'rect' or 'obround', got {relief!r}", op=op)
    ins = (float(inset), float(inset)) if isinstance(inset, (int, float)) else tuple(map(float, inset))
    if len(ins) != 2 or min(ins) < 0:
        raise TuleError("inset is a distance ≥ 0, or two of them (at the edge's lower and upper end)", op=op)
    ossb = (R + t) * math.tan(a / 2)
    L = float(length)
    if L <= ossb + 1e-9:
        raise TuleError(f"a {_num(L)} mm flange is too short for a {_num(abs(angle))}° bend with inside radius "
                        f"{_num(R)} in {_num(t)} mm sheet: the bend alone takes {_num(ossb)} mm of it", op=op,
                        hint=f"make the flange longer than {_num(ossb)} mm (outside length; a press brake wants "
                             f"about {_num(ossb + 2 * t)} mm or more), or use a smaller radius",
                        min_length=round(ossb, 6))
    # edges are found as lines on the shape the selection was made on (an earlier stage of this part is
    # fine: its faces may since have merged with a bend's end), then on this part as it is now
    owner = where.owner if isinstance(where, Selection) and getattr(where.owner, "_sm", None) is not None else part
    specs = _resolve_edges(owner, where, _Skin(owner, owner._sm, op), op)
    fid = new_feature_id()
    out = part
    for spec in specs:
        out = _one_flange(out, spec, L, a, angle > 0, R, ins, relief, fid)
    alias = dict(out._alias)
    for role in ("flange", "bend", "tip"):
        if f"#{fid}.{role}" in out._groups:
            alias[role] = f"#{fid}.{role}"
    return Shape(out.wrapped, out._groups, alias, out.name, out.color, sm=out._sm)


def _one_flange(part: "Shape", spec0: _EdgeSpec, L: float, a: float, up: bool, R: float, inset, relief: str,
                fid: int) -> "Shape":
    from .shape import Shape, _boolean

    op = "flange"
    sm = part._sm
    t = sm.thickness
    skin = _Skin(part, sm, op)
    s0, s1 = _current(part, skin, spec0, op)
    # the lower end in the user's sense: the one with the lower x, then y, then z
    p_lo, p_hi = spec0.a + spec0.d * spec0.lo, spec0.a + spec0.d * spec0.hi
    flip = tuple(round(c, 9) for c in p_lo) > tuple(round(c, 9) for c in p_hi)
    ia, ib = (inset[1], inset[0]) if flip else inset
    span = (s0 + ia, s1 - ib)
    if span[1] - span[0] < t:
        raise TuleError(f"the flange would be {_num(span[1] - span[0])} mm wide", op=op,
                        hint="a smaller inset, or a longer edge")
    ossb = (R + t) * math.tan(a / 2)
    # local frame: x outward, z toward the side the flange folds to, the edge along y
    zdir = spec0.top if up else -spec0.top
    origin = spec0.a if up else spec0.a + spec0.top * t
    ax3 = gp_Ax3(_pnt(origin), _dir(zdir), _dir(spec0.out))
    to_world = gp_Trsf()
    to_world.SetTransformation(ax3)
    to_world = to_world.Inverted()
    ydir = zdir.cross(spec0.out)
    y0, y1 = sorted(((spec0.a + spec0.d * s - origin).dot(ydir) for s in span))
    # which local y end is which end of the span
    lo_end_s = span[0] if (spec0.a + spec0.d * span[0] - origin).dot(ydir) <= (spec0.a + spec0.d * span[1] - origin).dot(ydir) else span[1]
    lo, hi = part.bounds
    big = (hi - lo).length + 10 * (L + t)
    W = to_world
    wr = t  # relief width
    ends = []
    for y_end, sgn in ((y0, -1.0), (y1, 1.0)):
        s_here = lo_end_s if sgn < 0 else (span[0] + span[1] - lo_end_s)
        if abs(s_here - s0) < 1e-9 or abs(s_here - s1) < 1e-9:  # at the end of the edge
            kind = _end_kind(part, skin, W, y_end, sgn, ossb, t)
            if kind == "relief":  # the panel goes on beyond the edge: the relief goes inside the span
                y_end -= sgn * wr
        else:
            kind = "relief"
        ends.append((y_end, sgn, kind))
    y0, y1 = ends[0][0], ends[1][0]
    if y1 - y0 < t:
        raise TuleError(f"the flange would be {_num(y1 - y0)} mm wide after its reliefs", op=op,
                        hint="a smaller inset, or a longer edge")
    cuts = []
    # nothing may be missing from the bend zone: a hole there was cut before flanging, or another bend is there
    zone = _box(W, -ossb, 0, y0, y1, 0, t)
    full = ossb * (y1 - y0) * t
    have = _common_volume(part.wrapped, zone)
    if have < full * (1 - 1e-4) - 1e-9:
        if _touches_bend(skin, zone):
            raise TuleError(f"this flange's bend ({_num(ossb)} mm deep from the edge) runs into the bend of an "
                            "earlier flange: the panel between them is too narrow", op=op,
                            hint="a smaller angle or radius, a wider panel, or an inset so the flanges do not "
                                 "share a stretch of panel", min_panel=round(2 * ossb, 6))
        raise TuleError("something was cut from the sheet where this flange's bend goes "
                        f"({_num(full - have)} mm³ missing within {_num(ossb)} mm of the edge)", op=op,
                        hint="cut holes and notches that cross a bend after flanging; or inset the flange past them")
    cuts.append(_box(W, -ossb, t, y0, y1, -0.5 * t, 1.5 * t))
    for y_end, sgn, kind in ends:
        if kind == "corner":  # an earlier flange's bend ends here: both stop at each other's bend
            cuts.append(_box(W, -ossb, t, y_end, y_end + sgn * big, -big, big))
        elif kind == "relief":
            depth = ossb + wr / 2
            if relief == "rect":
                cuts.append(_box(W, -depth, t, y_end, y_end + sgn * wr, -0.5 * t, 1.5 * t))
            else:
                cuts.append(_box(W, -ossb, t, y_end, y_end + sgn * wr, -0.5 * t, 1.5 * t))
                cyl = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(-ossb, y_end + sgn * wr / 2, -0.5 * t), gp_Dir(0, 0, 1)),
                                               wr / 2, 2 * t).Shape()
                cuts.append(BRepBuilderAPI_Transform(cyl, W, True).Shape())
    trimmed = _boolean(BRepAlgoAPI_Cut, op,
                       [part], [Shape(c) for c in cuts])
    trimmed = Shape(trimmed.wrapped, trimmed._groups, trimmed._alias, part.name, part.color, sm=part._sm)
    # the bend: the panel's end section turned about the bend axis; then the flat flange
    sec = BRepBuilderAPI_MakePolygon(gp_Pnt(-ossb, y0, 0), gp_Pnt(-ossb, y1, 0), gp_Pnt(-ossb, y1, t),
                                     gp_Pnt(-ossb, y0, t), True).Wire()
    face = BRepBuilderAPI_MakeFace(sec, True).Face()
    rev = BRepPrimAPI_MakeRevol(face, gp_Ax1(gp_Pnt(-ossb, 0, t + R), gp_Dir(0, -1, 0)), a)
    rev.Build()
    end_face = cast(rev.LastShape())
    d = gp_Vec(math.cos(a), 0, math.sin(a)) * (L - ossb)
    plate = BRepPrimAPI_MakePrism(end_face, d)
    plate.Build()
    tip = cast(plate.LastShape())
    bend_s = Shape(BRepBuilderAPI_Transform(rev.Shape(), W, True).Shape())
    bend_s = bend_s._with_roles({"bend": list(bend_s._sub("face"))}, fid)
    plate_w = BRepBuilderAPI_Transform(plate.Shape(), W, True)
    plate_s = Shape(plate_w.Shape())
    tip_w = plate_w.ModifiedShape(tip)
    plate_s = plate_s._with_roles({"flange": list(plate_s._sub("face")), "tip": [tip_w]}, fid)
    new = _volume(bend_s.wrapped) + _volume(plate_s.wrapped)
    clash = _common_volume(trimmed.wrapped, bend_s.wrapped) + _common_volume(trimmed.wrapped, plate_s.wrapped)
    if clash > 1e-6 * new + 1e-9:
        raise TuleError(f"the flange would overlap the part ({_num(clash)} mm³)", op=op,
                        hint="a shorter flange, a smaller angle, or an inset; flanges at a corner of one panel "
                             "get their relief automatically, other flanges must not run into each other")
    out = _boolean(BRepAlgoAPI_Fuse, op, [trimmed], [bend_s, plate_s])
    return Shape(out.wrapped, out._groups, out._alias, part.name, part.color, sm=part._sm)


def _touches_bend(skin: _Skin, zone) -> bool:
    from OCP.BRepExtrema import BRepExtrema_DistShapeShape

    for bi in skin.bends:
        d = BRepExtrema_DistShapeShape(zone, skin.faces[bi])
        d.Perform()
        if d.IsDone() and d.Value() < 1e-6:
            return True
    return False


def _end_kind(part, skin: _Skin, W: gp_Trsf, y_end: float, sgn: float, ossb: float, t: float) -> str:
    """What is beyond a flange's end that is at the end of its edge: ``corner`` (an earlier bend),
    ``relief`` (the panel goes on) or ``free``."""
    p = _apply(W, Vec(0, y_end, 0))
    tol = 1e-6 * max(1.0, ossb)
    for bi in skin.bends:
        f = skin.faces[bi]
        for v in subshapes(f, "vertex"):
            if (_vertex_pnt(v) - p).length < max(tol * 10, 1e-6):
                return "corner"
    probe = _apply(W, Vec(-ossb / 2, y_end + sgn * min(t, ossb) / 2, t / 2))
    c = BRepClass3d_SolidClassifier(part.wrapped, _pnt(probe), 1e-7)
    return "relief" if c.State() == TopAbs_IN else "free"


# ---------------------------------------------------------------- unfolding


def _flat_wire(pieces: list[list[Vec]]):
    """A closed wire in z = 0 from pieces of mapped boundary: 2 points a line, more a B-spline through them."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeWire
    from OCP.Approx import Approx_ParametrizationType
    from OCP.GeomAbs import GeomAbs_Shape
    from OCP.GeomAPI import GeomAPI_PointsToBSpline

    try:  # OCP 8
        from OCP.collections import Array1_gp_Pnt as Array1OfPnt  # type: ignore
    except ImportError:  # OCP 7
        from OCP.TColgp import TColgp_Array1OfPnt as Array1OfPnt  # type: ignore

    pieces = [pc for pc in pieces if (pc[-1] - pc[0]).length > 1e-9 or len(pc) > 2]
    for i in range(1, len(pieces)):  # snap each piece's start onto the previous end
        pieces[i][0] = pieces[i - 1][-1]
    if pieces:
        pieces[-1][-1] = pieces[0][0]
    mk = BRepBuilderAPI_MakeWire()
    for pc in pieces:
        pts = [pc[0]] + [q for a, q in zip(pc, pc[1:]) if (q - a).length > 1e-9]
        if len(pts) < 2:
            continue
        if len(pts) == 2:
            e = BRepBuilderAPI_MakeEdge(gp_Pnt(pts[0].x, pts[0].y, 0), gp_Pnt(pts[1].x, pts[1].y, 0)).Edge()
        else:
            arr = Array1OfPnt(1, len(pts))
            for i, q in enumerate(pts):
                arr.SetValue(i + 1, gp_Pnt(q.x, q.y, 0))
            fit = GeomAPI_PointsToBSpline(arr, Approx_ParametrizationType.Approx_ChordLength, 3, 5,
                                          GeomAbs_Shape.GeomAbs_C2, 1e-4)
            e = BRepBuilderAPI_MakeEdge(fit.Curve()).Edge()
        mk.Add(e)
    return mk.Wire()


def _bend_flat_face(skin: _Skin, info: dict, f):
    """The developed region of one bend face in the flat (z = 0): straight where the bend's boundary
    runs along or around its axis, a B-spline through mapped points where a cut crosses it."""

    def wire_pieces(w):
        pieces: list[list[Vec]] = []
        ex = BRepTools_WireExplorer(w, f)
        while ex.More():
            e = ex.Current()
            c = BRepAdaptor_Curve(e)
            t0, t1 = c.FirstParameter(), c.LastParameter()
            straight = c.GetType() == GeomAbs_Line or (
                c.GetType() == GeomAbs_Circle and abs(_v(c.Circle().Axis().Direction()).dot(info["d"])) > 1 - 1e-9)
            n = 1 if straight else 24
            ts = [t0 + (t1 - t0) * i / n for i in range(n + 1)]
            if e.Orientation() == TopAbs_REVERSED:
                ts = ts[::-1]
            pieces.append([skin.flat_point(info, _v(c.Value(u))) for u in ts])
            ex.Next()
        return pieces

    outer = static(BRepTools, "OuterWire")(f)
    face = BRepBuilderAPI_MakeFace(_flat_wire(wire_pieces(outer)), True).Face()
    for w in subshapes(f, "wire"):
        if w.IsSame(outer):
            continue
        hole = BRepBuilderAPI_MakeFace(_flat_wire(wire_pieces(w)), True).Face()
        cut = BRepAlgoAPI_Cut(face, hole)
        cut.Build()
        fs = list(subshapes(cut.Shape(), "face"))
        if fs:
            face = fs[0]
    return face


def _same_bend(a: dict, b: dict) -> bool:
    if abs(a["d"].dot(b["d"])) < 1 - 1e-9:
        return False
    if (b["a"] - a["a"]).cross(a["d"]).length > 1e-6:
        return False
    return abs(a["R"] - b["R"]) < 1e-6 and a["up"] == b["up"]


def unfold(part: "Shape") -> "Shape":
    """The flat pattern of a sheet part. See :meth:`Shape.unfold`."""
    from .shape import Shape

    op = "unfold"
    sm = _need(part, op)
    if sm.flat:
        return part
    t, k = sm.thickness, sm.k
    skin = _Skin(part, sm, op)
    if len(part._sub("solid")) != 1:
        raise TuleError(f"a sheet part is one piece; this one has {len(part._sub('solid'))}", op=op,
                        hint="a cut went all the way across; check part.solids")
    # refold check: the formed volume from the reference side alone
    flat_faces, expect_area, expect_vol = [], 0.0, 0.0
    for fi, T in skin.planes.items():
        f = skin.faces[fi]
        A = face_area(f)
        expect_vol += A * t
        expect_area += A
        flat_faces.append(BRepBuilderAPI_Transform(f, T, True).Shape())
    for bi, info in skin.bends.items():
        f = skin.faces[bi]
        A = face_area(f)
        expect_vol += A * t * (info["R"] + t / 2) / info["r"]
        ff = _bend_flat_face(skin, info, f)
        expect_area += _area(ff)
        flat_faces.append(ff)
    vol = part.volume
    if abs(vol - expect_vol) > 5e-3 * vol + t ** 3:
        raise TuleError(f"this is not a sheet of uniform thickness {_num(t)}: its volume is {_num(vol)} mm³, "
                        f"the sheet found from its bottom side would be {_num(expect_vol)} mm³", op=op,
                        hint="material was added that is not sheet (a union with a solid), or a cut left the "
                             "sheet's sides misaligned; keep extra parts as separate shapes")
    # the flat: every face in the plane, fused into one (a part without bends is its one face)
    if len(flat_faces) == 1:
        joined_faces = flat_faces[0]
    else:
        fuse = BRepAlgoAPI_Fuse()
        fuse.SetArguments(shape_list(flat_faces[:1]))
        fuse.SetTools(shape_list(flat_faces[1:]))
        fuse.SetFuzzyValue(1e-5)
        fuse.Build()
        if not fuse.IsDone():
            raise TuleError("could not join the unfolded faces", op=op,
                            hint="check the part with part.is_valid; flanges that meet in the flat need a gap")
        joined_faces = fuse.Shape()
    u = ShapeUpgrade_UnifySameDomain(joined_faces, True, True, False)
    u.Build()
    faces = list(subshapes(u.Shape(), "face"))
    area = sum(_area(f) for f in faces)
    if area < expect_area * (1 - 1e-4) - 1e-6:
        raise TuleError(f"the flat pattern overlaps itself ({_num(expect_area - area)} mm² twice)", op=op,
                        hint="flanges that meet in the flat need a gap or relief: shorten or inset them")
    if len(faces) != 1:
        raise TuleError(f"the flat pattern came out in {len(faces)} pieces", op=op,
                        hint="every panel must hang on the base sheet through bends")
    # edges the bends join are inside the flat; any other edge lost in the union is two pieces touching
    joined = 0.0
    for bi in skin.bends:
        for ei in skin.face_edges[bi]:
            if any(g in skin.planes for g in skin.edge_faces.get(ei, ())):
                joined += edge_length(skin.edges[ei])
    perimeter = sum(edge_length(e) for f in flat_faces for e in subshapes(f, "edge"))
    lost = perimeter - 2 * joined - sum(edge_length(e) for e in subshapes(faces[0], "edge"))
    if lost > 1e-4 * perimeter + 1e-6:
        raise TuleError(f"parts of the flat pattern touch along {_num(lost / 2)} mm where they must be cut apart",
                        op=op, hint="leave a gap: inset the flange there, or cut a slot between the pieces")
    flat_face = faces[0]
    prism = BRepPrimAPI_MakePrism(flat_face, gp_Vec(0, 0, t))
    prism.Build()
    solid = prism.Shape()
    bottom = cast(prism.FirstShape())
    # bends: one per bend axis
    groups: list[list[dict]] = []
    for bi, info in skin.bends.items():
        for g in groups:
            if _same_bend(g[0], info):
                g.append({**info, "face": bi})
                break
        else:
            groups.append([{**info, "face": bi}])
    bends = []
    for g in groups:
        b0 = g[0]
        vs = [(_vertex_pnt(v) - b0["m"]).dot(b0["d"]) for x in g for v in subshapes(skin.faces[x["face"]], "vertex")]
        ang = max(x["angle"] for x in g)
        BA = ang * b0["nf"]

        def at(v, s):
            return _apply(b0["T"], b0["m"] + b0["d"] * v + b0["w"] * s)

        lo_v, hi_v = min(vs), max(vs)
        line = tuple((round(p.x, 9), round(p.y, 9)) for p in (at(lo_v, BA / 2), at(hi_v, BA / 2)))
        zone = tuple(tuple((round(p.x, 9), round(p.y, 9)) for p in (at(lo_v, s), at(hi_v, s))) for s in (0, BA))
        bends.append(Bend("", "up" if b0["up"] else "down", math.degrees(ang), b0["R"], k, t, line, zone))
    bends.sort(key=lambda b: (-round((b.line[0][1] + b.line[1][1]) / 2, 3), round((b.line[0][0] + b.line[1][0]) / 2, 3)))
    bends = [replace(b, id=f"B{i + 1}") for i, b in enumerate(bends)]
    # bend lines as edges on the top face, tagged bend_up / bend_down
    tools = []
    for b in bends:
        (x0, y0), (x1, y1) = b.line
        tools.append(BRepBuilderAPI_MakeEdge(gp_Pnt(x0, y0, t), gp_Pnt(x1, y1, t)).Edge())
    groups_ = {REF: (bottom,)}
    if tools:
        try:
            sp = BRepAlgoAPI_Splitter()
            sp.SetArguments(shape_list([solid]))
            sp.SetTools(shape_list(tools))
            sp.Build()
            if sp.IsDone() and len(subshapes(sp.Shape(), "solid")) == 1:
                s2 = sp.Shape()
                solids = list(subshapes(s2, "solid"))
                solid = solids[0]
                groups_ = {REF: tuple(f for f in subshapes(solid, "face")
                                      if _ftype(f) == "plane" and abs(_plane(f)[0].z) < 1e-9
                                      and _plane(f)[1].z < -0.5)}
        except OCC_ERRORS:
            pass
    flat = Shape(solid, groups_, {}, part.name, part.color, sm=replace(sm, flat=True, bends=tuple(bends)))
    for direction in ("up", "down"):
        es = [e for e in flat._sub("edge") if _on_bend_line(e, [b for b in bends if b.direction == direction], t)]
        if es:
            flat = flat.tag(f"bend_{direction}", Edges(flat, es))
    return flat


def _on_bend_line(e, bends, t) -> bool:
    if not _is_line(e):
        return False
    p, q = _edge_ends(e)
    if abs(p.z - t) > 1e-7 or abs(q.z - t) > 1e-7:
        return False
    for b in bends:
        (x0, y0), (x1, y1) = b.line
        a, c = Vec(x0, y0, t), Vec(x1, y1, t)
        d = (c - a).normalized()
        if all((x - a).cross(d).length < 1e-6 for x in (p, q)):
            return True
    return False


def bends(part: "Shape") -> list[Bend]:
    sm = _need(part, "bends")
    if sm.flat:
        return list(sm.bends)
    return list(part.unfold()._sm.bends)


def bend_table(part: "Shape") -> list[dict]:
    """One dict per bend: bend, direction, angle, radius, k, thickness, allowance, deduction, length."""
    return [b.row() for b in bends(part)]


def bend_csv(part: "Shape") -> str:
    rows = bend_table(part)
    if not rows:
        return "bend,direction,angle,radius,k,thickness,allowance,deduction,length\n"
    head = list(rows[0])
    return ",".join(head) + "\n" + "".join(",".join(str(r[h]) for h in head) + "\n" for r in rows)


# ---------------------------------------------------------------- DXF


# layer: (ACI color, linetype)
LAYERS = {"CUT": (7, "CONTINUOUS"), "BEND_UP": (3, "DASHDOT"), "BEND_DOWN": (1, "DASHDOT"), "BEND_NOTES": (8, "CONTINUOUS")}


def _flat(part: "Shape", op: str, name: str | None = None) -> "Shape":
    """``part.unfold()``, its error naming the part (``name`` when it has none): what a drawing or
    cut file of it says."""
    _need(part, op)
    try:
        return part.unfold()
    except TuleError as e:
        name = part.name or name
        who = f"sheet part {name!r}" if name else "the sheet part"
        raise TuleError(f"{who} does not unfold: {e.message}", op=op, faces=e.faces, edges=e.edges,
                        hint=e.hint, **e.data) from None


def _wire_prim(w, f, tol: float) -> tuple:
    """One loop of the flat's bottom face as a closed outline: a CIRCLE for a round hole, else a
    closed polyline, arcs as bulges, other curves (cuts across a bend) as points ``tol`` apart."""
    from OCP.GCPnts import GCPnts_TangentialDeflection

    edges = []
    ex = BRepTools_WireExplorer(w, f)
    while ex.More():
        edges.append(ex.Current())
        ex.Next()
    if len(edges) == 1:
        c = BRepAdaptor_Curve(edges[0])
        if c.GetType() == GeomAbs_Circle and c.LastParameter() - c.FirstParameter() > 2 * math.pi - 1e-9:
            o = c.Circle().Location()
            return ("circle", o.X(), o.Y(), c.Circle().Radius())
    pts: list = []
    bulges: list = []
    for e in edges:  # each edge from its start in the loop's direction: (point, bulge) pairs
        c = BRepAdaptor_Curve(e)
        t0, t1 = c.FirstParameter(), c.LastParameter()
        rev = e.Orientation() == TopAbs_REVERSED
        typ = c.GetType()
        if typ == GeomAbs_Line:
            run = [(t1 if rev else t0, 0.0)]
        elif typ == GeomAbs_Circle:
            ccw = (c.Circle().Axis().Direction().Z() > 0) != rev
            run = [(t1 if rev else t0, math.tan((t1 - t0) / 4) * (1 if ccw else -1))]
        else:
            g = GCPnts_TangentialDeflection(c, 0.1, tol)
            ts = sorted({t0, t1, *(g.Parameter(i) for i in range(1, g.NbPoints() + 1))})
            run = [(t_, 0.0) for t_ in (ts[:0:-1] if rev else ts[:-1])]
        for t_, bulge in run:
            q = c.Value(t_)
            if pts and math.dist(pts[-1], (q.X(), q.Y())) < 1e-9:
                bulges[-1] = bulge
                continue
            pts.append((q.X(), q.Y()))
            bulges.append(bulge)
    if len(pts) > 1 and math.dist(pts[0], pts[-1]) < 1e-9:
        pts.pop()
        bulges.pop()
    return ("poly", tuple(pts), True, tuple(bulges))


def flat_dxf(part: "Shape", notes: bool = False) -> str:
    """The flat pattern as an R12 DXF in mm (``$INSUNITS`` 4) for a laser or waterjet shop: the
    outline and every hole as closed outlines (CIRCLE, closed POLYLINE) on CUT, bend lines on
    BEND_UP / BEND_DOWN (dash-dot), no text. ``notes=True`` adds their labels (``B1 UP 90° R1.5``)
    as TEXT on BEND_NOTES; the bend table and the drawing carry them anyway."""
    from .drawing import to_dxf_entities

    flat = _flat(part, "export_dxf")
    prims = []
    for f in flat._sub("face"):
        if _ftype(f) == "plane" and abs(_plane(f)[0].z) < 1e-9 and _plane(f)[1].z < -0.5:
            outer = static(BRepTools, "OuterWire")(f)
            for w in [outer] + [w for w in subshapes(f, "wire") if not w.IsSame(outer)]:
                prims.append(("CUT", _wire_prim(w, f, 0.01)))
    texts = []
    for b in flat._sm.bends:
        layer = "BEND_UP" if b.direction == "up" else "BEND_DOWN"
        (x0, y0), (x1, y1) = b.line
        prims.append((layer, ("line", x0, y0, x1, y1)))
        if not notes:
            continue
        ang = math.degrees(math.atan2(y1 - y0, x1 - x0))
        if ang <= -90 or ang > 90:
            ang -= 180 * (1 if ang > 0 else -1)
            (x0, y0), (x1, y1) = (x1, y1), (x0, y0)
        h = max(1.0, min(3.5, b.length / 12))
        L = b.length
        nx, ny = -(y1 - y0) / L, (x1 - x0) / L
        tx, ty = x0 + (x1 - x0) * 0.1 + nx * 0.6 * h, y0 + (y1 - y0) * 0.1 + ny * 0.6 * h
        texts.append(("BEND_NOTES", tx, ty, h, ang, f"{b.id} {b.label}".replace("°", "%%d")))
    used = {x[0] for x in prims} | {x[0] for x in texts} | {"CUT"}
    return to_dxf_entities(prims, texts, {k: v for k, v in LAYERS.items() if k in used})


def export_dxf(part: "Shape", path: str, notes: bool = False) -> str:
    """Write a sheet part's flat pattern as a DXF for cutting (see :func:`flat_dxf`). Returns the path."""
    from .io import write_atomic

    return write_atomic(path, flat_dxf(part, notes))
