"""Profiles swept into solids: extrude, revolve, loft.

Results carry face roles: ``extrude`` → ``bottom``, ``top``, ``side``;
``revolve`` → ``side`` (and ``start``/``end`` for partial angles);
``loft`` → ``bottom``, ``top``, ``side``.
"""

from __future__ import annotations

import math
from typing import Sequence

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
    roles = {"bottom": _faces_of(mk.FirstShape()), "top": _faces_of(mk.LastShape())}
    caps = set()
    for f in roles["bottom"] + roles["top"]:
        caps.add(hash(f))
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
    pts = [tuple(float(c) for c in p) for p in path]
    if len(pts) < 2 or any(len(p) != 3 for p in pts):
        raise TuleError("a path is two or more (x, y, z) points, or an edge or wire", op="sweep")
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


def _sweep_wire(wire, path_wire, start, tangent, roles_out=None):
    """One closed profile wire (in the xy plane) swept along the path: a solid."""
    from OCP.BRepOffsetAPI import BRepOffsetAPI_MakePipeShell
    from OCP.BRepBuilderAPI import BRepBuilderAPI_TransitionMode
    from OCP.gp import gp_Ax3

    t = gp_Trsf()
    z = gp_Dir(*tangent)
    x = gp_Dir(1, 0, 0) if abs(tangent[0]) < 0.9 else gp_Dir(0, 1, 0)
    x = gp_Dir(gp_Vec(z).Crossed(gp_Vec(x)).Crossed(gp_Vec(z)).Reversed())
    t.SetTransformation(gp_Ax3(gp_Pnt(*start), z, x), gp_Ax3())
    placed = cast(BRepBuilderAPI_Transform(wire, t, True).Shape())
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
        raise TuleError("could not sweep the profile along the path", op="sweep",
                        hint="a bend tighter than the profile's size folds it onto itself: smooth the path or make the profile smaller")
    if roles_out is not None:
        roles_out["start"] = _faces_of(mk.FirstShape())
        roles_out["end"] = _faces_of(mk.LastShape())
    return mk.Shape()


def sweep(profile: Profile, path, smooth: bool = True) -> Shape:
    """Sweep ``profile`` along a 3D ``path``: points ``[(x, y, z), ...]`` (a smooth spline through them,
    or a polyline with rounded corners with ``smooth=False``) or an edge or wire.

    The profile's xy plane is set square to the path at its start, its origin on the path, and is
    carried along without twisting. Faces: ``start``, ``end``, ``side``. A tube: a profile with a hole.
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
        solid = Shape(_sweep_wire(outer, pw, start, tangent, roles))
        for w in wires:
            if not w.IsSame(outer):
                solid = solid - Shape(_sweep_wire(w, pw, start, tangent))
        out = solid if out is None else out | solid
    if len(faces) == 1 and len(list(subshapes(faces[0], "wire"))) == 1:
        side = [f for f in out._sub("face") if not any(f.IsSame(c) for c in roles["start"] + roles["end"])]
        return out._with_roles({"start": roles["start"], "end": roles["end"], "side": side})
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
