"""Primitive solids. Each comes with face roles: ``box(...).faces["top"]``.

Planar faces are named by their outward normal (``top`` +Z, ``bottom`` -Z,
``right`` +X, ``left`` -X, ``back`` +Y, ``front`` -Y); curved faces are
``side`` (cylinders, cones) or ``surface`` (spheres, tori).
"""

from __future__ import annotations

from typing import Sequence

from OCP.BRepBuilderAPI import BRepBuilderAPI_MakePolygon, BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakeSolid, BRepBuilderAPI_Sewing
from OCP.BRepPrimAPI import (
    BRepPrimAPI_MakeBox,
    BRepPrimAPI_MakeCone,
    BRepPrimAPI_MakeCylinder,
    BRepPrimAPI_MakeSphere,
    BRepPrimAPI_MakeTorus,
    BRepPrimAPI_MakeWedge,
)
from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt
from OCP.TopoDS import TopoDS

from ._occ import static, subshapes
from .errors import TuleError
from .geom import VecLike, vec
from .selection import face_normal, face_type
from .shape import Shape

_NORMAL_ROLES = [
    ((0, 0, 1), "top"),
    ((0, 0, -1), "bottom"),
    ((1, 0, 0), "right"),
    ((-1, 0, 0), "left"),
    ((0, 1, 0), "back"),
    ((0, -1, 0), "front"),
]


def _roles(shape, curved_role: str = "side") -> dict:
    roles: dict[str, list] = {}
    for f in subshapes(shape, "face"):
        if face_type(f) == "plane":
            n = face_normal(f)
            for d, name in _NORMAL_ROLES:
                if n.dot(d) > 0.9999:
                    roles.setdefault(name, []).append(f)
                    break
            else:
                roles.setdefault("planar", []).append(f)
        else:
            roles.setdefault(curved_role, []).append(f)
    return roles


def _make(shape, curved_role: str = "side") -> Shape:
    s = Shape(shape)
    return s._with_roles(_roles(shape, curved_role))


def _positive(op: str, **sizes: float):
    """A clear error for a size that is zero or negative (OpenCascade's own says little or nothing)."""
    bad = {k: v for k, v in sizes.items() if not v > 0}
    if bad:
        raise TuleError(f"{', '.join(bad)} must be positive", op=op,
                        hint="got " + ", ".join(f"{k}={v}" for k, v in sizes.items()))


def box(x: float = 1, y: float | None = None, z: float | None = None, center: bool = False) -> Shape:
    """A box of size ``x`` × ``y`` × ``z`` (``box(2)`` is a cube), from the origin or centered."""
    y = x if y is None else y
    z = x if z is None else z
    if min(x, y, z) <= 0:
        raise TuleError("box sizes must be positive", op="box", hint=f"got {x}, {y}, {z}")
    o = gp_Pnt(-x / 2, -y / 2, -z / 2) if center else gp_Pnt(0, 0, 0)
    return _make(BRepPrimAPI_MakeBox(o, float(x), float(y), float(z)).Shape())


def cube(size: float = 1, center: bool = False) -> Shape:
    return box(size, size, size, center)


def cylinder(r: float = 1, h: float = 1, center: bool = False, r2: float | None = None) -> Shape:
    """A cylinder along +Z of radius ``r`` and height ``h`` (``r2`` makes it a cone frustum)."""
    if r2 is not None and r2 != r:
        return cone(r, h, r2=r2, center=center)
    _positive("cylinder", r=r, h=h)
    z0 = -h / 2 if center else 0.0
    ax = gp_Ax2(gp_Pnt(0, 0, z0), gp_Dir(0, 0, 1))
    return _make(BRepPrimAPI_MakeCylinder(ax, float(r), float(h)).Shape())


def cone(r: float = 1, h: float = 1, r2: float = 0.0, center: bool = False) -> Shape:
    """A cone along +Z from radius ``r`` at the bottom to ``r2`` at the top."""
    _positive("cone", h=h)
    if r < 0 or r2 < 0 or r == r2 == 0:
        raise TuleError("a cone's radii must be ≥ 0 and not both 0", op="cone", hint=f"got r={r}, r2={r2}")
    if r == r2:
        return cylinder(r, h, center=center)
    z0 = -h / 2 if center else 0.0
    ax = gp_Ax2(gp_Pnt(0, 0, z0), gp_Dir(0, 0, 1))
    return _make(BRepPrimAPI_MakeCone(ax, float(r), float(r2), float(h)).Shape())


def sphere(r: float = 1) -> Shape:
    """A sphere of radius ``r`` at the origin."""
    _positive("sphere", r=r)
    return _make(BRepPrimAPI_MakeSphere(gp_Pnt(0, 0, 0), float(r)).Shape(), "surface")


def torus(R: float = 1, r: float = 0.25) -> Shape:
    """A torus around +Z: ring radius ``R``, tube radius ``r``."""
    _positive("torus", R=R, r=r)
    ax = gp_Ax2(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1))
    return _make(BRepPrimAPI_MakeTorus(ax, float(R), float(r)).Shape(), "surface")


def wedge(dx: float, dy: float, dz: float, ltx: float) -> Shape:
    """A wedge: a ``dx`` × ``dy`` × ``dz`` box whose top face is ``ltx`` long in x."""
    return _make(BRepPrimAPI_MakeWedge(float(dx), float(dy), float(dz), float(ltx)).Shape())


def polyhedron(points: Sequence[VecLike], faces: Sequence[Sequence[int]]) -> Shape:
    """A closed polyhedron from points and faces (lists of point indices, CCW seen from outside)."""
    pts = [vec(p) for p in points]
    sew = BRepBuilderAPI_Sewing(1e-6)
    for f in faces:
        poly = BRepBuilderAPI_MakePolygon()
        for i in f:
            poly.Add(gp_Pnt(*pts[i]))
        poly.Close()
        sew.Add(BRepBuilderAPI_MakeFace(poly.Wire(), True).Face())
    sew.Perform()
    shell = static(TopoDS, "Shell")(sew.SewedShape())
    solid = BRepBuilderAPI_MakeSolid(shell).Solid()
    s = Shape(solid)
    if s.volume < 0:
        s = Shape(solid.Reversed())
    return s._with_roles(_roles(s.wrapped))
