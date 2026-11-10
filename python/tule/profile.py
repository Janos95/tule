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
from OCP.BRepCheck import BRepCheck_Analyzer
from OCP.ChFi2d import ChFi2d_IsDone
from OCP.gp import gp_Ax2, gp_Circ, gp_Dir, gp_Elips, gp_Pnt, gp_Trsf, gp_Vec
from OCP.GProp import GProp_GProps
from OCP.ShapeUpgrade import ShapeUpgrade_UnifySameDomain

from OCP.TopoDS import TopoDS_Compound
from OCP.BRep import BRep_Builder

from ._occ import cast, static, subshapes
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


def _region(wire):
    """The region a closed planar wire bounds, whichever way the wire runs."""
    from OCP.ShapeAnalysis import ShapeAnalysis

    face = BRepBuilderAPI_MakeFace(wire, True).Face()
    if not ShapeAnalysis.IsOuterBound_s(face):  # wound the other way: its material would be outside it
        face = BRepBuilderAPI_MakeFace(cast(wire.Reversed()), True).Face()
    return face


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
        ends = [(e, subshapes(e, "vertex")) for e in subshapes(face, "edge")]  # each edge's vertices, once
        out = []
        for v in subshapes(face, "vertex"):
            edges = [e for e, vs in ends if any(v.IsSame(x) for x in vs)]
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
            wires = subshapes(face, "wire")
            if len(wires) > 1:
                # OpenCascade's 2D fillet leaves a face with holes invalid: the outline and each hole are
                # done on their own (a hole's corners round as its own outline's), then the holes cut out
                outer = static(BRepTools, "OuterWire")(face)
                parts = [Profile(BRepBuilderAPI_MakeFace(w, True).Face())._corner_op(size, op, points)
                         for w in [outer] + [w for w in wires if not w.IsSame(outer)]]
                done = parts[0]
                for hole in parts[1:]:
                    done = done - hole
                faces += [f for f in done.faces]
                continue
            mk = BRepFilletAPI_MakeFillet2d(face)
            bad = False
            added = 0
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
                added += 1
                try:
                    if op == "round":
                        mk.AddFillet(v, float(r))
                    else:
                        es = [e for e in subshapes(face, "edge") if v in subshapes(e, "vertex")]
                        mk.AddChamfer(es[0], es[1], float(r), float(r))
                    bad = bad or mk.Status() != ChFi2d_IsDone  # a corner it could not do: not skipped quietly
                except OCC_ERRORS:
                    bad = True
            if not added:  # no corner here (a round hole): the face as it is
                faces.append(face)
                continue
            try:
                mk.Build()
                ok = not bad and mk.IsDone() and BRepCheck_Analyzer(mk.Shape()).IsValid()
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
        return self._largest(r, "round")

    def _largest(self, size: float, op: str) -> "Profile":
        """``op`` on every corner; when ``size`` does not fit, the error says the largest that does."""
        try:
            return self._corner_op(size, op)
        except TuleError:
            pass
        lo, hi = 0.0, float(size)
        for _ in range(10):
            mid = (lo + hi) / 2
            try:
                self._corner_op(mid, op)
                lo = mid
            except TuleError:
                hi = mid
        word = "radius" if op == "round" else "distance"
        raise TuleError(f"could not {op} corners with {size:g}", op=op,
                        hint=f"largest {word} that works ≈ {lo:.3g}" if lo > 0 else "no size works: a corner's edges are too short",
                        **({"max_radius": round(lo, 6)} if op == "round" else {"max_distance": round(lo, 6)}))

    def chamfer(self, d: float) -> "Profile":
        """Bevel every corner by ``d``."""
        return self._largest(d, "chamfer")

    def offset(self, d: float, round: bool = True) -> "Profile":
        """The region grown by ``d`` (shrunk when negative), holes shrinking as it grows: a rim
        band is ``p.offset(2) - p``, a wall of 1.5 mm ``p - p.offset(-1.5)``. Corners grown
        outward are rounded (``round=False``: sharp, the edges extended)."""
        from functools import reduce

        from OCP.BRepOffsetAPI import BRepOffsetAPI_MakeOffset
        from OCP.GeomAbs import GeomAbs_Arc, GeomAbs_Intersection

        if d == 0:
            return self
        kind = GeomAbs_Arc if round else GeomAbs_Intersection

        def grown(wire, by):
            # the region inside one outline, offset; None when it vanishes
            try:
                mk = BRepOffsetAPI_MakeOffset(_region(wire), kind)
                mk.Perform(float(by))
                ok = mk.IsDone()
            except OCC_ERRORS:
                ok = False
            regions = [Profile(_region(w)) for w in subshapes(mk.Shape(), "wire")] if ok else []
            regions = [r for r in regions if r.area > 1e-9]
            return reduce(lambda a, b: a ^ b, regions) if regions else None  # nested ones cancel

        out = []
        for face in self.faces:
            outer = BRepTools.OuterWire_s(face)
            region = grown(outer, d)
            if region is None:
                continue
            holes = [grown(w, -d) for w in subshapes(face, "wire") if not w.IsSame(outer)]
            for hole in holes:  # a hole shrinks as the region grows, and may close
                if hole is not None:
                    region = region - hole
            out.append(region)
        if not out or sum(p.area for p in out) <= 1e-12:
            raise TuleError(f"offsetting the profile by {d:g} leaves nothing", op="offset",
                            hint="shrink by less than half its narrowest width")
        return reduce(lambda a, b: a | b, out)

    # ------------------------------------------------------------ sweeps
    def extrude(self, h: float = 1, center: bool = False, **kw):
        from .sweeps import extrude

        return extrude(self, h, center=center, **kw)

    def sheet(self, thickness: float, bend_radius: float | None = None, k: float = 0.44):
        """A sheet part of this outline; see :func:`tule.sheet`."""
        from .sheetmetal import sheet

        return sheet(self, thickness, bend_radius, k)

    def revolve(self, angle: float = 360):
        from .sweeps import revolve

        return revolve(self, angle)


# ---------------------------------------------------------------- constructors


def polygon(points: Sequence[Sequence[float]], holes: Iterable[Sequence[Sequence[float]]] = ()) -> Profile:
    """A polygon from ``(x, y)`` points; ``(x, y, r)`` rounds that corner with radius ``r``.

    ``holes`` are further point lists cut out of the outline.
    """
    pts = [tuple(p) for p in points]
    if len(pts) > 200:  # a sampled curve: every point an edge, heavy STEP files and slow motion studies
        import sys

        print(f"tule: polygon with {len(pts)} points: a sampled curve (a cam, an airfoil) is lighter and smooth "
              "as spline(points, tol=0.01)", file=sys.stderr, flush=True)
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
    if not (w > 0 and h > 0):
        raise TuleError("w and h must be positive", op="rect", hint=f"got w={w}, h={h}")
    x0, y0 = (-w / 2, -h / 2) if center else (0.0, 0.0)
    return Profile(_polygon_face([(x0, y0), (x0 + w, y0), (x0 + w, y0 + h), (x0, y0 + h)]))


def circle(r: float, center: Point2 = (0, 0)) -> Profile:
    """A circle of radius ``r``."""
    if not r > 0:
        raise TuleError("r must be positive", op="circle", hint=f"got r={r}")
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


def spline(points: Sequence[Point2], closed: bool = True, tol: float | None = None) -> Profile:
    """A smooth outline: one B-spline curve through the ``(x, y)`` points (no corners, no facets).

    ``closed=True`` joins the last point back to the first smoothly (periodic,
    no seam corner); with ``closed=False`` the points must end on the first
    one, which becomes a corner. ``tol`` lets the curve pass within ``tol`` of
    the points instead of through each, keeping only as many as it needs, so
    a dense sampling (``[f(t) for t in ts]``) becomes a light spline. The
    curve is stored as edges of at most a quarter turn each (smooth where
    they meet), so area and volume measure right.
    """
    import numpy as np
    from OCP.Geom2dAPI import Geom2dAPI_InterCurveCurve
    from OCP.GeomAPI import GeomAPI
    from OCP.gp import gp_Pln

    try:
        pts = np.array([(float(p[0]), float(p[1])) for p in points])
    except (TypeError, IndexError, ValueError):
        raise TuleError("spline points are (x, y) pairs", op="spline") from None
    size = float(np.ptp(pts, axis=0).max()) if len(pts) else 0.0
    eps = 1e-9 * max(size, 1.0)
    if len(pts):
        keep = np.concatenate([[True], np.hypot(*np.diff(pts, axis=0).T) > eps])
        pts = pts[keep]
    ends_meet = len(pts) > 1 and math.dist(pts[0], pts[-1]) <= eps
    if not closed and not ends_meet:
        raise TuleError("an open curve does not bound a region", op="spline",
                        hint="pass closed=True for a smooth closed outline, or end on the first point")
    if ends_meet:
        pts = pts[:-1]
    if len(pts) < 3:
        raise TuleError(f"a spline outline needs at least 3 distinct points, not {len(pts)}", op="spline")
    if np.linalg.svd(pts - pts.mean(axis=0), compute_uv=False)[-1] <= eps:
        raise TuleError("the spline points lie on a line", op="spline")
    x, y = pts[:, 0], pts[:, 1]
    if np.dot(x, np.roll(y, -1)) < np.dot(y, np.roll(x, -1)):  # counter-clockwise, so the face's normal is +z
        pts = np.vstack([pts[:1], pts[:0:-1]])
    if tol is not None and tol <= 0:
        raise TuleError("tol must be positive", op="spline")

    curve = _spline_through(pts, closed, tol)
    seam = curve.Value(curve.FirstParameter())
    hits = Geom2dAPI_InterCurveCurve(curve, 1e-7 * max(size, 1.0))
    at_seam = [math.hypot(p.X() - seam.X(), p.Y() - seam.Y()) <= 1e-6 * max(size, 1.0)
               for p in (hits.Point(i + 1) for i in range(hits.NbPoints()))]
    if hits.NbSegments() or at_seam.count(False) or at_seam.count(True) > 1:  # the seam itself is always listed once
        p = next((hits.Point(i + 1) for i, s in enumerate(at_seam) if not s), seam)
        raise TuleError(f"the spline crosses itself near ({p.X():.4g}, {p.Y():.4g})", op="spline",
                        hint="reorder the points so they run once around the outline; a curve wiggling "
                             "between close points can loop: thin them out or pass tol=")
    c3 = static(GeomAPI, "To3d")(curve, gp_Pln())
    cuts = _quarter_turns(c3)
    wire = BRepBuilderAPI_MakeWire()
    for a, b in zip(cuts[:-1], cuts[1:]):
        piece = c3.Copy() if len(cuts) > 2 else c3  # each edge keeps only its own poles
        if len(cuts) > 2:
            piece.Segment(a, b)
        wire.Add(BRepBuilderAPI_MakeEdge(piece).Edge())
    mk = BRepBuilderAPI_MakeFace(wire.Wire(), True)
    if not mk.IsDone():
        raise TuleError("could not make a face from the spline", op="spline")
    return Profile(mk.Face())


def _quarter_turns(curve) -> list[float]:
    """Parameters that cut ``curve`` into pieces turning at most a quarter turn each.

    OpenCascade integrates area and volume with a fixed number of points per
    edge, so a wavy curve measures wrong as one edge but right in such pieces.
    """
    import numpy as np

    u0, u1 = curve.FirstParameter(), curve.LastParameter()
    us = np.linspace(u0, u1, 16 * curve.NbKnots() + 1)
    d = []
    for u in us:
        p, v = gp_Pnt(), gp_Vec()
        curve.D1(float(u), p, v)
        d.append(complex(v.X(), v.Y()))
    d = np.array(d)
    turn = np.concatenate([[0], np.cumsum(np.abs(np.angle(d[1:] / d[:-1])))])
    n = max(1, math.ceil(turn[-1] / (math.pi / 2) - 1e-6))
    cuts = np.interp(np.linspace(0, turn[-1], n + 1), turn, us)
    return [u0, *map(float, cuts[1:-1]), u1]


def _spline_through(pts, closed, tol):
    """The 2D B-spline through ``pts`` (periodic when ``closed``, else from the first point back
    to it); with ``tol``, through as few of them as keep all within ``tol``."""
    import numpy as np
    from OCP.Geom2dAPI import Geom2dAPI_Interpolate, Geom2dAPI_ProjectPointOnCurve
    from OCP.gp import gp_Pnt2d

    try:
        from OCP.collections import HArray1_gp_Pnt2d as HArray  # OCP 8
    except ImportError:  # pragma: no cover
        from OCP.TColgp import TColgp_HArray1OfPnt2d as HArray

    def through(sub):
        ring = sub if closed else np.vstack([sub, sub[:1]])
        arr = HArray(1, len(ring))  # kept in a variable: the interpolation holds a handle to it
        for i, p in enumerate(ring):
            arr.SetValue(i + 1, gp_Pnt2d(float(p[0]), float(p[1])))
        it = Geom2dAPI_Interpolate(arr, closed, 1e-9)
        it.Perform()
        if not it.IsDone():
            raise TuleError("could not fit a spline through the points", op="spline",
                            hint="points too close together or doubling back")
        return it.Curve()

    n = len(pts)
    if tol is None:
        return through(pts)
    chosen = np.unique(np.linspace(0, n - 1, min(n, 16)).round().astype(int))
    while True:
        curve = through(pts[chosen])
        if len(chosen) == n:
            return curve
        dev = np.zeros(n)
        for i in np.setdiff1d(np.arange(n), chosen):
            proj = Geom2dAPI_ProjectPointOnCurve(gp_Pnt2d(*map(float, pts[i])), curve)
            dev[i] = proj.LowerDistance() if proj.NbPoints() else math.inf
        if dev.max() <= tol:
            return curve
        # add the worst point of every stretch between chosen points that strays past tol
        bounds = np.append(chosen, n)
        extra = [a + int(np.argmax(dev[a:b])) for a, b in zip(bounds[:-1], bounds[1:]) if dev[a:b].max() > tol]
        chosen = np.union1d(chosen, extra)


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

