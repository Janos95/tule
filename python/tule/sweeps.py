"""Profiles swept into solids: extrude, revolve, loft.

Results carry face roles: ``extrude`` → ``bottom``, ``top``, ``side``;
``revolve`` → ``side`` (and ``start``/``end`` for partial angles);
``loft`` → ``bottom``, ``top``, ``side``.
"""

from __future__ import annotations

import math
from typing import Sequence

import numpy as np

from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
from OCP.BRepOffsetAPI import BRepOffsetAPI_ThruSections
from OCP.BRepPrimAPI import BRepPrimAPI_MakePrism, BRepPrimAPI_MakeRevol
from OCP.BRepTools import BRepTools
from OCP.gp import gp_Ax1, gp_Dir, gp_Pnt, gp_Trsf, gp_Vec


from ._occ import cast, from_list, kind_of, static, subshapes
from .errors import OCC_ERRORS, TuleError
from .profile import Profile
from .shape import Shape


def _faces_of(shape) -> list:
    if shape is None or shape.IsNull():
        return []
    return list(subshapes(shape, "face")) if kind_of(shape) != "face" else [shape]


def extrude(profile: Profile, h: float = 1, center: bool = False, twist: float = 0, scale: float = 1) -> Shape:
    """Sweep ``profile`` along +z by ``h`` (from z = 0, or centered).

    ``twist`` (degrees, counter-clockwise seen from above) and ``scale``
    (size of the top relative to the bottom) loft between sections; they
    support profiles without holes.
    """
    if h == 0:
        raise TuleError("extrusion height must not be zero", op="extrude")
    if not scale > 0:
        raise TuleError("scale must be positive", op="extrude",
                        hint=f"got {scale}; to a point: cone(), or a small scale such as 0.01")
    src = profile.wrapped
    if center:
        t = gp_Trsf()
        t.SetTranslation(gp_Vec(0, 0, -h / 2))
        src = BRepBuilderAPI_Transform(src, t, True).Shape()
    if twist or scale != 1:
        z0 = -h / 2 if center else 0.0
        n = max(1, int(math.ceil(abs(twist) / 5.0))) if twist else 1
        sections = []
        for i in range(n + 1):
            f = i / n
            p = profile.scale(1 + (scale - 1) * f) if scale != 1 else profile
            p = p.rotate(twist * f) if twist else p
            sections.append((p, z0 + h * f))
        return _loft([p for p, _ in sections], [z for _, z in sections], ruled=not twist)
    mk = BRepPrimAPI_MakePrism(src, gp_Vec(0, 0, float(h)))
    mk.Build()
    if not mk.IsDone():
        raise TuleError("could not extrude the profile", op="extrude")
    s = mk.Shape()
    bottom = _faces_of(mk.FirstShape())
    top = _faces_of(mk.LastShape())
    side = []
    for e in subshapes(src, "edge"):
        side.extend(f for f in from_list(mk.Generated(e)) if kind_of(f) == "face")
    if h < 0:
        bottom, top = top, bottom
    return Shape(s)._with_roles({"bottom": bottom, "top": top, "side": side})


def revolve(profile: Profile, angle: float = 360) -> Shape:
    """Sweep ``profile`` around the z axis. The profile's x is the radius (x ≥ 0), its y becomes z.

    A partial ``angle`` starts in the xz plane, runs counter-clockwise seen
    from above, and gets end faces ``start`` and ``end``.
    """
    if not angle or abs(angle) > 360:
        raise TuleError("the angle must be between -360 and 360 degrees, not 0", op="revolve", hint=f"got {angle}")
    lo, hi = profile.bounds
    if lo.x < -1e-9:
        raise TuleError("a revolved profile must lie at x ≥ 0", op="revolve",
                        hint=f"the profile reaches x = {lo.x:g}; move it with .at(x, 0)")
    t = gp_Trsf()
    t.SetRotation(gp_Ax1(gp_Pnt(0, 0, 0), gp_Dir(1, 0, 0)), math.pi / 2)  # y -> z
    src = BRepBuilderAPI_Transform(profile.wrapped, t, True).Shape()
    full = abs(angle) >= 360
    ax = gp_Ax1(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1))
    mk = BRepPrimAPI_MakeRevol(src, ax) if full else BRepPrimAPI_MakeRevol(src, ax, math.radians(angle))
    mk.Build()
    if not mk.IsDone():
        raise TuleError("could not revolve the profile", op="revolve")
    s = mk.Shape()
    from .selection import face_normal, face_type

    base = Shape(s)
    caps = []
    if not full:
        caps = [c for c in (base._canonical(f) for f in _faces_of(mk.FirstShape()) + _faces_of(mk.LastShape())) if c is not None]
    roles = {"side": [], "top": [], "bottom": []}
    for f in base._sub("face"):
        if any(f.IsSame(c) for c in caps):
            continue
        n = face_normal(f) if face_type(f) == "plane" else None
        if n is not None and n.z > 0.9999:
            roles["top"].append(f)
        elif n is not None and n.z < -0.9999:
            roles["bottom"].append(f)
        else:
            roles["side"].append(f)
    roles = {k: v for k, v in roles.items() if v}
    if not full:
        roles["start"] = _faces_of(mk.FirstShape())
        roles["end"] = _faces_of(mk.LastShape())
    return base._with_roles(roles)


def _placed_wire(profile: Profile, z: float):
    faces = profile.faces
    if len(faces) != 1:
        raise TuleError("loft sections must be single regions", op="loft")
    face = faces[0]
    if len(list(subshapes(face, "wire"))) > 1:
        raise TuleError("loft sections cannot have holes", op="loft", hint="cut holes after lofting")
    w = static(BRepTools, "OuterWire")(face)
    t = gp_Trsf()
    t.SetTranslation(gp_Vec(0, 0, float(z)))
    return cast(BRepBuilderAPI_Transform(w, t, True).Shape())


def _loft(profiles: Sequence[Profile], heights: Sequence[float], ruled: bool = True) -> Shape:
    if len(profiles) < 2 or len(profiles) != len(heights):
        raise TuleError("loft needs at least two profiles and one height per profile", op="loft")
    mk = BRepOffsetAPI_ThruSections(True, ruled, 1e-6)
    for p, z in zip(profiles, heights):
        mk.AddWire(_placed_wire(p, z))
    try:
        mk.Build()
        ok = mk.IsDone()
    except OCC_ERRORS:
        ok = False
    if not ok:
        raise TuleError("could not loft the sections", op="loft",
                        hint="sections with very different shapes or point counts can fail; try similar outlines")
    s = mk.Shape()
    first, last = _faces_of(mk.FirstShape()), _faces_of(mk.LastShape())
    # bottom is the lower end: the last section when the heights go down (extrude(-h, scale=...))
    roles = {"bottom": first, "top": last} if heights[-1] >= heights[0] else {"bottom": last, "top": first}
    roles["side"] = [f for f in subshapes(s, "face") if not any(f.IsSame(c) for c in roles["bottom"] + roles["top"])]
    return Shape(s)._with_roles(roles)


def loft(profiles: Sequence[Profile], heights: Sequence[float], ruled: bool = True) -> Shape:
    """Join profiles placed at z = ``heights[i]`` with ruled (or smooth) sides and flat caps."""
    return _loft(profiles, heights, ruled)


def _path(path, smooth: bool):
    """A wire from a Shape (an edge or wire) or 3D points: a smooth spline through them, else a polyline."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeEdge, BRepBuilderAPI_MakePolygon, BRepBuilderAPI_MakeWire
    from OCP.TopoDS import TopoDS

    if isinstance(path, Shape):
        k = kind_of(path.wrapped)
        if k == "wire":
            return cast(path.wrapped)
        edges = [path.wrapped] if k == "edge" else list(subshapes(path.wrapped, "edge"))
        mk = BRepBuilderAPI_MakeWire()
        for e in edges:
            mk.Add(static(TopoDS, "Edge")(e))
        if not mk.IsDone():
            raise TuleError("the path's edges do not join into one line", op="sweep")
        return mk.Wire()
    if hasattr(path, "ShapeType"):  # an OCP edge or wire
        return _path(Shape(path), smooth)
    pts = [tuple(float(c) for c in p) for p in path]
    if len(pts) < 2 or any(len(p) not in (3, 4) for p in pts):
        raise TuleError("a path is two or more (x, y, z) points (or (x, y, z, r): a corner of radius r), or an edge or wire", op="sweep")
    if any(len(p) == 4 for p in pts):
        return _rounded_path([p if len(p) == 4 else (*p, 0.0) for p in pts])
    pts = [p for i, p in enumerate(pts) if i == 0 or math.dist(p, pts[i - 1]) > 1e-9]
    if smooth and len(pts) > 2:
        from OCP.GeomAPI import GeomAPI_Interpolate

        try:
            from OCP.collections import HArray1_gp_Pnt  # OCP 8
        except ImportError:  # pragma: no cover
            from OCP.TColgp import TColgp_HArray1OfPnt as HArray1_gp_Pnt

        arr = HArray1_gp_Pnt(1, len(pts))  # kept in a variable: the interpolation holds a handle to it
        for i, p in enumerate(pts):
            arr.SetValue(i + 1, gp_Pnt(*p))
        it = GeomAPI_Interpolate(arr, False, 1e-7)
        it.Perform()
        if not it.IsDone():
            raise TuleError("could not fit a smooth path through the points", op="sweep", hint="points too close together or doubling back")
        curve = it.Curve()
        return BRepBuilderAPI_MakeWire(BRepBuilderAPI_MakeEdge(curve).Edge()).Wire()
    poly = BRepBuilderAPI_MakePolygon()
    for p in pts:
        poly.Add(gp_Pnt(*p))
    return poly.Wire()


def _rounded_path(pts):
    """Straight segments through (x, y, z, r) points, each corner rounded with radius r by an arc
    tangent to both sides: a tendon around a pulley, a cable over a guide."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeEdge, BRepBuilderAPI_MakeWire
    from OCP.GC import GC_MakeArcOfCircle

    P = [np.array(p[:3]) for p in pts]
    if any(np.linalg.norm(P[i + 1] - P[i]) < 1e-9 for i in range(len(P) - 1)):
        raise TuleError("two points of the path are the same", op="sweep")
    corners = {}  # point index -> (arc start, arc end)
    for i in range(1, len(P) - 1):
        r = pts[i][3]
        d1 = (P[i] - P[i - 1]) / np.linalg.norm(P[i] - P[i - 1])
        d2 = (P[i + 1] - P[i]) / np.linalg.norm(P[i + 1] - P[i])
        turn = math.acos(max(-1.0, min(1.0, float(d1 @ d2))))
        if r <= 0 or turn < 1e-9:
            continue
        if turn > math.pi - 1e-6:
            raise TuleError(f"the path turns straight back at point {i}", op="sweep")
        t = r * math.tan(turn / 2)
        corners[i] = (P[i] - d1 * t, P[i] + d2 * t, d1)
    for i in range(len(P) - 1):
        a = corners[i][1] if i in corners else P[i]
        b = corners[i + 1][0] if i + 1 in corners else P[i + 1]
        if (b - a) @ (P[i + 1] - P[i]) < -1e-9:
            raise TuleError(f"the corner radii at points {i} and {i + 1} need more room than the "
                            f"{np.linalg.norm(P[i + 1] - P[i]):.3g} mm between them", op="sweep",
                            hint="make the radii smaller or the points further apart")
    pnt = lambda v: gp_Pnt(*map(float, v))  # noqa: E731
    mk = BRepBuilderAPI_MakeWire()
    start = P[0]
    for i in range(1, len(P)):
        end = corners[i][0] if i in corners else P[i]
        if np.linalg.norm(end - start) > 1e-9:
            mk.Add(BRepBuilderAPI_MakeEdge(pnt(start), pnt(end)).Edge())
        if i in corners:
            a, b, d1 = corners[i]
            mk.Add(BRepBuilderAPI_MakeEdge(GC_MakeArcOfCircle(pnt(a), gp_Vec(*map(float, d1)), pnt(b)).Value()).Edge())
            start = b
        else:
            start = P[i]
    if not mk.IsDone():
        raise TuleError("the path's segments do not join", op="sweep")
    return mk.Wire()


def _placed(shape, start, tangent):
    """A profile (wire or face in the xy plane) set square to the path at its start."""
    from OCP.gp import gp_Ax3

    t = gp_Trsf()
    z = gp_Dir(*tangent)
    x = gp_Dir(1, 0, 0) if abs(tangent[0]) < 0.9 else gp_Dir(0, 1, 0)
    x = gp_Dir(gp_Vec(z).Crossed(gp_Vec(x)).Crossed(gp_Vec(z)))  # a path going up +z keeps the profile as extrude does
    t.SetTransformation(gp_Ax3(gp_Pnt(*start), z, x), gp_Ax3())
    return cast(BRepBuilderAPI_Transform(shape, t, True).Shape())


def _why(path_wire, profile) -> str:
    """What in the path most likely stops a sweep: a corner where two of its pieces meet, or its tightest bend."""
    from OCP.BRepAdaptor import BRepAdaptor_Curve
    from OCP.BRepBndLib import BRepBndLib
    from OCP.Bnd import Bnd_Box
    from OCP.BRepTools import BRepTools_WireExplorer
    from OCP.BRepLProp import BRepLProp_CLProps

    try:
        box = Bnd_Box()
        BRepBndLib.Add_s(profile, box)
        lo, hi = box.CornerMin(), box.CornerMax()
        size = max(abs(lo.X()), abs(lo.Y()), abs(hi.X()), abs(hi.Y()))  # the profile's reach from the path
        edges = []
        ex = BRepTools_WireExplorer(path_wire)
        while ex.More():
            edges.append(BRepAdaptor_Curve(ex.Current()))
            ex.Next()

        def d1(c, u):
            p, v = gp_Pnt(), gp_Vec()
            c.D1(u, p, v)
            return p, v

        for a, b in zip(edges, edges[1:]):
            p, va = d1(a, a.LastParameter())
            _, vb = d1(b, b.FirstParameter())
            if va.Magnitude() > 1e-12 and vb.Magnitude() > 1e-12:
                ang = math.degrees(va.Angle(vb))
                if ang > 1:
                    return (f": the path turns {ang:.0f}° at ({p.X():.4g}, {p.Y():.4g}, {p.Z():.4g}), where two of its "
                            "pieces meet without a shared tangent (round it: a point (x, y, z, r))")
        tight, where = math.inf, None
        for c in edges:
            u0, u1 = c.FirstParameter(), c.LastParameter()
            for k in range(41):
                u = u0 + (u1 - u0) * k / 40
                pr = BRepLProp_CLProps(c, u, 2, 1e-7)
                if pr.IsTangentDefined():
                    kap = pr.Curvature()
                    if kap > 1e-12 and 1 / kap < tight:
                        p = pr.Value()
                        tight, where = 1 / kap, (p.X(), p.Y(), p.Z())
        if where and tight < size:
            return (f": the path bends to radius {tight:.3g} mm at ({where[0]:.4g}, {where[1]:.4g}, {where[2]:.4g}), "
                    f"less than the profile's {size:.3g} mm reach")
    except Exception:  # a diagnosis only: never in the way of the error itself
        pass
    return ""


def _sweep_face(face, path_wire, start, tangent):
    """A face with holes swept along the path in one go (a tube): cutting the swept hole out of
    the swept outline instead fails on some splines, as the two surfaces run parallel."""
    from OCP.BRepOffsetAPI import BRepOffsetAPI_MakePipe
    from OCP.GeomFill import GeomFill_Trihedron

    try:
        mk = BRepOffsetAPI_MakePipe(path_wire, _placed(face, start, tangent), GeomFill_Trihedron.GeomFill_IsCorrectedFrenet, False)
        mk.Build()
        out = Shape(mk.Shape()) if mk.IsDone() else None
    except OCC_ERRORS:
        return None
    return out if out is not None and out.is_valid and out.volume > 0 else None


def _sweep_wire(wire, path_wire, start, tangent, roles_out=None):
    """One closed profile wire (in the xy plane) swept along the path: a solid."""
    from OCP.BRepOffsetAPI import BRepOffsetAPI_MakePipeShell
    from OCP.BRepBuilderAPI import BRepBuilderAPI_TransitionMode

    placed = _placed(wire, start, tangent)
    mk = BRepOffsetAPI_MakePipeShell(path_wire)
    mk.SetMode(False)  # corrected Frenet: the profile does not twist about the path
    mk.SetTransitionMode(BRepBuilderAPI_TransitionMode.BRepBuilderAPI_RoundCorner)
    mk.Add(placed, False, False)
    try:
        mk.Build()
        ok = mk.IsDone() and mk.MakeSolid()
    except OCC_ERRORS:
        ok = False
    if not ok:
        raise TuleError("could not sweep the profile along the path" + _why(path_wire, wire), op="sweep",
                        hint="a bend tighter than the profile's size folds it onto itself: smooth the path or make the profile smaller")
    if roles_out is not None:
        roles_out["start"] = _faces_of(mk.FirstShape())
        roles_out["end"] = _faces_of(mk.LastShape())
    return mk.Shape()


def sweep(profile: Profile, path, smooth: bool = True) -> Shape:
    """Sweep ``profile`` along a 3D ``path``: points ``[(x, y, z), ...]`` (a smooth spline through them,
    or a polyline with rounded corners with ``smooth=False``), points ``(x, y, z, r)`` (straight
    segments, that corner an arc of radius ``r``: a tendon around a pulley), or an edge or wire.

    The profile's xy plane is set square to the path at its start, its origin on the path, and is
    carried along without twisting (a path going up +z places it as ``extrude`` would). Faces: ``start``, ``end``, ``side``. A tube: a profile with a hole.
    """
    from OCP.BRepAdaptor import BRepAdaptor_CompCurve

    pw = _path(path, smooth)
    c = BRepAdaptor_CompCurve(pw)
    p0, v0 = gp_Pnt(), gp_Vec()
    c.D1(c.FirstParameter(), p0, v0)
    if v0.Magnitude() < 1e-12:
        raise TuleError("the path has no direction at its start", op="sweep")
    v0.Normalize()
    start, tangent = (p0.X(), p0.Y(), p0.Z()), (v0.X(), v0.Y(), v0.Z())
    faces = profile.faces
    if not faces:
        raise TuleError("the profile is empty", op="sweep")
    roles: dict = {}
    out = None
    for face in faces:
        wires = list(subshapes(face, "wire"))
        outer = static(BRepTools, "OuterWire")(face)
        solid = _sweep_face(face, pw, start, tangent) if len(wires) > 1 else None
        if solid is None:
            solid = Shape(_sweep_wire(outer, pw, start, tangent, roles))
            for w in wires:
                if not w.IsSame(outer):
                    solid = solid - Shape(_sweep_wire(w, pw, start, tangent))
        out = solid if out is None else out | solid
    if not out.faces or out.volume <= 0:
        raise TuleError("sweeping the profile along the path left nothing", op="sweep",
                        hint="a bend tighter than the profile folds it onto itself: smooth the path, "
                             "make the profile smaller, or try smooth=False")
    p1, v1 = gp_Pnt(), gp_Vec()
    c.D1(c.LastParameter(), p1, v1)
    start_caps = _caps(out, p0, v0)
    end_caps = _caps(out, p1, v1) if v1.Magnitude() > 1e-12 else []
    caps = start_caps + end_caps
    side = [f for f in out._sub("face") if not any(f.IsSame(x) for x in caps)]
    return out._with_roles({"start": start_caps, "end": end_caps, "side": side})


def _caps(shape: Shape, at, direction) -> list:
    """The planar faces of shape lying in the plane through ``at`` square to ``direction`` (a sweep's end)."""
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.GeomAbs import GeomAbs_SurfaceType

    d = gp_Dir(direction)
    out = []
    for f in shape._sub("face"):
        srf = BRepAdaptor_Surface(f)
        if srf.GetType() != GeomAbs_SurfaceType.GeomAbs_Plane:
            continue
        pln = srf.Plane()
        if abs(pln.Axis().Direction().Dot(d)) > 1 - 1e-6 and pln.Distance(at) < 1e-6 * max(1.0, abs(at.X()) + abs(at.Y()) + abs(at.Z())):
            out.append(f)
    return out


def pipe(points, r: float, wall: float | None = None, smooth: bool = True) -> Shape:
    """A round rod of radius ``r`` along 3D points (a tendon, a cable, a wire); with ``wall`` a tube."""
    from .profile import circle

    p = circle(r)
    if wall is not None:
        if not 0 < wall < r:
            raise TuleError("a tube's wall must be between 0 and r", op="pipe")
        p = p - circle(r - wall)
    return sweep(p, points, smooth)
