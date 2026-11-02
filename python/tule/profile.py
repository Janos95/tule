"""2D profiles in the xy plane: build them, combine them, round them, sweep them.

A Profile is one or more planar faces at z = 0. Outlines may run in either
direction; 2D Booleans (``| - & ^``) and ``round`` / ``chamfer`` return new
profiles. ``extrude``, ``revolve`` and ``loft`` (in :mod:`tule.sweeps`) turn
them into solids.
"""

from __future__ import annotations

import math
from typing import Iterable, Sequence

from OCP.BRep import BRep_Tool
from OCP.BRepAlgoAPI import BRepAlgoAPI_Common, BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse
from OCP.BRepBuilderAPI import (
    BRepBuilderAPI_MakeEdge,
    BRepBuilderAPI_MakeFace,
    BRepBuilderAPI_MakePolygon,
    BRepBuilderAPI_MakeWire,
    BRepBuilderAPI_Transform,
)
from OCP.BRepFilletAPI import BRepFilletAPI_MakeFillet2d
from OCP.BRepGProp import BRepGProp
from OCP.BRepTools import BRepTools
from OCP.ChFi2d import ChFi2d_IsDone
from OCP.gp import gp_Ax2, gp_Circ, gp_Dir, gp_Elips, gp_Pnt, gp_Trsf, gp_Vec
from OCP.GProp import GProp_GProps
from OCP.ShapeUpgrade import ShapeUpgrade_UnifySameDomain

from OCP.TopoDS import TopoDS_Compound
from OCP.BRep import BRep_Builder

from ._occ import cast, explore, from_list, shape_list, static, subshapes
from .errors import OCC_ERRORS, TuleError
from .geom import Vec, from_pnt
from .selection import edge_type

Point2 = Sequence[float]


def _compound(faces) -> TopoDS_Compound:
    c = TopoDS_Compound()
    b = BRep_Builder()
    b.MakeCompound(c)
    for f in faces:
        b.Add(c, f)
    return c


def _unify(shape):
    u = ShapeUpgrade_UnifySameDomain(shape, True, True, False)
    u.Build()
    return u.Shape()


def _polygon_face(points: Sequence[Point2]):
    if len(points) < 3:
        raise TuleError("a polygon needs at least 3 points", op="polygon")
    poly = BRepBuilderAPI_MakePolygon()
    last = None
    for p in points:
        q = (float(p[0]), float(p[1]))
        if last is not None and abs(q[0] - last[0]) < 1e-12 and abs(q[1] - last[1]) < 1e-12:
            continue
        poly.Add(gp_Pnt(q[0], q[1], 0))
        last = q
    poly.Close()
    if not poly.IsDone():
        raise TuleError("could not build the polygon outline", op="polygon", hint="check for repeated points")
    mk = BRepBuilderAPI_MakeFace(poly.Wire(), True)
    if not mk.IsDone():
        raise TuleError("could not make a face from the outline", op="polygon",
                        hint="self-intersecting outlines are not supported; split them into simple polygons and union them")
    return mk.Face()


class Profile:
    """An immutable planar region in the xy plane."""

    __slots__ = ("_s",)

    def __init__(self, shape):
        self._s = shape

    @property
    def wrapped(self):
        return self._s

    @property
    def faces(self) -> list:
        return list(subshapes(self._s, "face"))

    # ------------------------------------------------------------ measurements
    @property
    def area(self) -> float:
        p = GProp_GProps()
        static(BRepGProp, "SurfaceProperties")(self._s, p)
        return p.Mass()

    @property
    def bounds(self) -> tuple[Vec, Vec]:
        from OCP.Bnd import Bnd_Box
        from OCP.BRepBndLib import BRepBndLib

        b = Bnd_Box()
        static(BRepBndLib, "AddOptimal")(self._s, b, False, False)
        lo, hi = from_pnt(b.CornerMin()), from_pnt(b.CornerMax())
        return Vec(lo.x, lo.y, 0), Vec(hi.x, hi.y, 0)

    @property
    def points(self) -> list[tuple[float, float]]:
        """Corner points of all loops (vertices of the region's edges)."""
        return [(v.x, v.y) for v in (from_pnt(static(BRep_Tool, "Pnt")(x)) for x in subshapes(self._s, "vertex"))]

    def __repr__(self) -> str:
        return f"<Profile: {len(self.faces)} face(s), area {self.area:.6g}>"

    # ------------------------------------------------------------ 2D Booleans
    def _boolean(self, cls, other: "Profile", name: str) -> "Profile":
        op = cls(self._s, other._s)
        op.Build()
        if not op.IsDone():
            raise TuleError("2D Boolean failed", op=name)
        res = op.Shape()
        faces = list(subshapes(res, "face"))
        if not faces:
            return Profile(_compound([]))
        return Profile(_unify(_compound(faces)))

    def __or__(self, other: "Profile") -> "Profile":
        return self._boolean(BRepAlgoAPI_Fuse, other, "union")

    __add__ = __or__

    def __sub__(self, other: "Profile") -> "Profile":
        return self._boolean(BRepAlgoAPI_Cut, other, "difference")

    def __and__(self, other: "Profile") -> "Profile":
        return self._boolean(BRepAlgoAPI_Common, other, "intersection")

    def __xor__(self, other: "Profile") -> "Profile":
        return (self | other) - (self & other)

    # ------------------------------------------------------------ transforms
    def _trsf(self, t: gp_Trsf) -> "Profile":
        return Profile(BRepBuilderAPI_Transform(self._s, t, True).Shape())

    def at(self, x: float = 0, y: float = 0) -> "Profile":
        t = gp_Trsf()
        t.SetTranslation(gp_Vec(float(x), float(y), 0))
        return self._trsf(t)

    translate = at

    def rotate(self, degrees: float, about: Point2 = (0, 0)) -> "Profile":
        from OCP.gp import gp_Ax1

        t = gp_Trsf()
        t.SetRotation(gp_Ax1(gp_Pnt(about[0], about[1], 0), gp_Dir(0, 0, 1)), math.radians(degrees))
        return self._trsf(t)

    def scale(self, x: float, y: float | None = None) -> "Profile":
        if y is None or y == x:
            t = gp_Trsf()
            t.SetScale(gp_Pnt(0, 0, 0), float(x))
            return self._trsf(t)
        from OCP.BRepBuilderAPI import BRepBuilderAPI_GTransform
        from OCP.gp import gp_GTrsf, gp_Mat

        g = gp_GTrsf()
        g.SetVectorialPart(gp_Mat(float(x), 0, 0, 0, float(y), 0, 0, 0, 1))
        return Profile(BRepBuilderAPI_GTransform(self._s, g, True).Shape())

    def mirror(self, nx: float = 1, ny: float = 0) -> "Profile":
        from OCP.gp import gp_Ax2 as Ax2

        t = gp_Trsf()
        t.SetMirror(Ax2(gp_Pnt(0, 0, 0), gp_Dir(nx, ny, 0)))
        return self._trsf(t)

    # ------------------------------------------------------------ corners
    def _corners(self, face):
        """Vertices of a face where two edges meet at a real corner (not tangent)."""
        out = []
        for v in subshapes(face, "vertex"):
            edges = [e for e in subshapes(face, "edge") if v in subshapes(e, "vertex")]
            if len(edges) != 2:
                continue
            if all(edge_type(e) == "line" for e in edges):
                d = []
                for e in edges:
                    a, b = [from_pnt(static(BRep_Tool, "Pnt")(x)) for x in subshapes(e, "vertex")]
                    p = from_pnt(static(BRep_Tool, "Pnt")(v))
                    other = b if (a - p).length < 1e-9 else a
                    d.append((other - p).normalized())
                cosang = d[0].dot(d[1])
                if cosang < -0.9999:  # straight through: not a corner
                    continue
            out.append(v)
        return out

    def _corner_op(self, size, op: str, points: Sequence[Point2] | None = None) -> "Profile":
        faces = []
        for face in self.faces:
            mk = BRepFilletAPI_MakeFillet2d(face)
            corners = self._corners(face)
            for v in corners:
                p = from_pnt(static(BRep_Tool, "Pnt")(v))
                r = size
                if points is not None:
                    r = 0.0
                    for q, rq in points:
                        if abs(q[0] - p.x) < 1e-6 and abs(q[1] - p.y) < 1e-6:
                            r = rq
                            break
                if r <= 0:
                    continue
                try:
                    if op == "round":
                        mk.AddFillet(v, float(r))
                    else:
                        es = [e for e in subshapes(face, "edge") if v in subshapes(e, "vertex")]
                        mk.AddChamfer(es[0], es[1], float(r), float(r))
                except OCC_ERRORS:
                    pass
            try:
                mk.Build()
                ok = mk.IsDone()
            except OCC_ERRORS:
                ok = False
            if not ok:
                raise TuleError(
                    f"could not {op} corners with {size:g}",
                    op=op,
                    hint="the size is likely larger than an adjacent edge allows; try a smaller value",
                )
            faces.append(mk.Shape())
        return Profile(_compound([cast(f) for f in faces]) if len(faces) != 1 else cast(faces[0]))

    def round(self, r: float) -> "Profile":
        """Round every corner (convex and concave) with radius ``r``."""
        return self._corner_op(r, "round")

    def chamfer(self, d: float) -> "Profile":
        """Bevel every corner by ``d``."""
        return self._corner_op(d, "chamfer")

    # ------------------------------------------------------------ sweeps
    def extrude(self, h: float = 1, center: bool = False, **kw):
        from .sweeps import extrude

        return extrude(self, h, center=center, **kw)

    def revolve(self, angle: float = 360):
        from .sweeps import revolve

        return revolve(self, angle)


# ---------------------------------------------------------------- constructors


def polygon(points: Sequence[Sequence[float]], holes: Iterable[Sequence[Sequence[float]]] = ()) -> Profile:
    """A polygon from ``(x, y)`` points; ``(x, y, r)`` rounds that corner with radius ``r``.

    ``holes`` are further point lists cut out of the outline.
    """
    pts = [tuple(p) for p in points]
    face = _polygon_face([p[:2] for p in pts])
    prof = Profile(face)
    for h in holes:
        prof = prof - Profile(_polygon_face([tuple(p)[:2] for p in h]))
    rounded = [((p[0], p[1]), p[2]) for p in pts if len(p) > 2 and p[2]]
    if rounded:
        prof = prof._corner_op(max(r for _, r in rounded), "round", rounded)
    return prof


def rect(w: float, h: float | None = None, center: bool = False) -> Profile:
    """A ``w`` × ``h`` rectangle from the origin, or centered."""
    h = w if h is None else h
    x0, y0 = (-w / 2, -h / 2) if center else (0.0, 0.0)
    return Profile(_polygon_face([(x0, y0), (x0 + w, y0), (x0 + w, y0 + h), (x0, y0 + h)]))


def circle(r: float, center: Point2 = (0, 0)) -> Profile:
    """A circle of radius ``r``."""
    c = gp_Circ(gp_Ax2(gp_Pnt(center[0], center[1], 0), gp_Dir(0, 0, 1)), float(r))
    e = BRepBuilderAPI_MakeEdge(c).Edge()
    w = BRepBuilderAPI_MakeWire(e).Wire()
    return Profile(BRepBuilderAPI_MakeFace(w, True).Face())


def ellipse(rx: float, ry: float) -> Profile:
    """An ellipse with semi-axes ``rx`` (along x) and ``ry`` (along y)."""
    if rx >= ry:
        el = gp_Elips(gp_Ax2(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1), gp_Dir(1, 0, 0)), float(rx), float(ry))
    else:
        el = gp_Elips(gp_Ax2(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1), gp_Dir(0, 1, 0)), float(ry), float(rx))
    e = BRepBuilderAPI_MakeEdge(el).Edge()
    return Profile(BRepBuilderAPI_MakeFace(BRepBuilderAPI_MakeWire(e).Wire(), True).Face())


def regular_polygon(n: int, r: float) -> Profile:
    """A regular ``n``-gon with circumradius ``r`` and a corner on +x."""
    pts = [(r * math.cos(2 * math.pi * i / n), r * math.sin(2 * math.pi * i / n)) for i in range(n)]
    return polygon(pts)


def slot(length: float, width: float) -> Profile:
    """A stadium along x, ``length`` overall and ``width`` wide, centered."""
    if length <= width:
        return circle(width / 2)
    r = width / 2
    body = rect(length - width, width, center=True)
    return body | circle(r, (-(length / 2 - r), 0)) | circle(r, ((length / 2 - r), 0))


# fonts-dejavu-core on Tule machines; elsewhere a font file's path, or a name the system knows
FONT_DIR = "/usr/share/fonts/truetype/dejavu"
FONTS = {"sans": "DejaVuSans", "serif": "DejaVuSerif", "mono": "DejaVuSansMono"}


def text(s: str, size: float = 10, font: str = "sans", bold: bool = False, align: str = "left") -> Profile:
    """Text as a profile, on the x axis from the origin (``align`` left, center or right of it).

    ``size`` is the font size in mm (capitals are about 0.73 × size tall). ``font``: sans,
    serif, mono, or a .ttf/.otf path. Extrude it for raised lettering, or cut it for engraving::

        plate = box(80, 20, 3) + text("MK4 TOOLS", 8, bold=True).at(4, 6).extrude(1).at(0, 0, 3)
    """
    import os

    from OCP.Font import Font_FontAspect
    from OCP.Graphic3d import Graphic3d_HTA_CENTER, Graphic3d_HTA_LEFT, Graphic3d_HTA_RIGHT, Graphic3d_VTA_BOTTOM
    from OCP.gp import gp_Ax3
    from OCP.NCollection import NCollection_String
    from OCP.StdPrs import StdPrs_BRepFont, StdPrs_BRepTextBuilder

    halign = {"left": Graphic3d_HTA_LEFT, "center": Graphic3d_HTA_CENTER, "right": Graphic3d_HTA_RIGHT}.get(align)
    if halign is None:
        raise TuleError(f"align is left, center or right, not {align!r}", op="text")
    if not s.strip():
        raise TuleError("text needs some characters", op="text")
    if font not in FONTS and not os.path.isfile(font):
        raise TuleError(f"no font {font!r}", op="text", hint="use sans, serif, mono, or the path of a .ttf/.otf file")
    f = StdPrs_BRepFont()
    name = FONTS.get(font, font)
    path = font if os.path.isfile(font) else os.path.join(FONT_DIR, name + ("-Bold" if bold else "") + ".ttf")
    ok = f.Init(NCollection_String(path), float(size), 0) if os.path.isfile(path) else \
        f.Init(NCollection_String(name), Font_FontAspect.Font_FA_Bold if bold else Font_FontAspect.Font_FA_Regular, float(size))
    if not ok:
        raise TuleError(f"no font {font!r}", op="text", hint="use sans, serif, mono, or the path of a .ttf/.otf file")
    shape = StdPrs_BRepTextBuilder().Perform(f, NCollection_String(s), gp_Ax3(), halign, Graphic3d_VTA_BOTTOM)
    if shape.IsNull():
        raise TuleError(f"could not make text {s!r}", op="text")
    return Profile(shape)

