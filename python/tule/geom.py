"""Small vector helpers and axis constants."""

from __future__ import annotations

import math
from typing import Sequence, Union

from OCP.gp import gp_Ax1, gp_Dir, gp_Pnt, gp_Trsf, gp_Vec

VecLike = Union[Sequence[float], "Vec"]


class Vec(tuple):
    """An immutable 3D vector: ``Vec(1, 2, 3)``, ``+X``, ``-Z``, ``2 * Y``."""

    def __new__(cls, x: float = 0.0, y: float = 0.0, z: float = 0.0):
        return super().__new__(cls, (float(x), float(y), float(z)))

    x = property(lambda self: self[0])
    y = property(lambda self: self[1])
    z = property(lambda self: self[2])

    def __add__(self, o):  # type: ignore[override]
        o = vec(o)
        return Vec(self[0] + o[0], self[1] + o[1], self[2] + o[2])

    def __sub__(self, o):
        o = vec(o)
        return Vec(self[0] - o[0], self[1] - o[1], self[2] - o[2])

    def __mul__(self, k):  # type: ignore[override]
        return Vec(self[0] * k, self[1] * k, self[2] * k)

    __rmul__ = __mul__

    def __neg__(self):
        return Vec(-self[0], -self[1], -self[2])

    def __pos__(self):
        return self

    def dot(self, o) -> float:
        o = vec(o)
        return self[0] * o[0] + self[1] * o[1] + self[2] * o[2]

    def cross(self, o) -> "Vec":
        o = vec(o)
        return Vec(
            self[1] * o[2] - self[2] * o[1],
            self[2] * o[0] - self[0] * o[2],
            self[0] * o[1] - self[1] * o[0],
        )

    @property
    def length(self) -> float:
        return math.sqrt(self.dot(self))

    def normalized(self) -> "Vec":
        n = self.length
        return self * (1.0 / n) if n else self

    def __repr__(self) -> str:
        return f"Vec({self[0]:g}, {self[1]:g}, {self[2]:g})"


X = Vec(1, 0, 0)
Y = Vec(0, 1, 0)
Z = Vec(0, 0, 1)
ORIGIN = Vec(0, 0, 0)


def vec(v: VecLike) -> Vec:
    if isinstance(v, Vec):
        return v
    v = tuple(v)
    if len(v) == 2:
        return Vec(v[0], v[1], 0.0)
    return Vec(*v)


def pnt(v: VecLike) -> gp_Pnt:
    v = vec(v)
    return gp_Pnt(*v)


def gvec(v: VecLike) -> gp_Vec:
    v = vec(v)
    return gp_Vec(*v)


def gdir(v: VecLike) -> gp_Dir:
    v = vec(v)
    return gp_Dir(*v)


def from_pnt(p) -> Vec:
    return Vec(p.X(), p.Y(), p.Z())


def axis(origin: VecLike = ORIGIN, direction: VecLike = Z) -> gp_Ax1:
    return gp_Ax1(pnt(origin), gdir(direction))


def translation(v: VecLike) -> gp_Trsf:
    t = gp_Trsf()
    t.SetTranslation(gvec(v))
    return t


def rotation(axis_dir: VecLike, degrees: float, origin: VecLike = ORIGIN) -> gp_Trsf:
    t = gp_Trsf()
    t.SetRotation(axis(origin, axis_dir), math.radians(degrees))
    return t


def xyz_rotation(x: float = 0, y: float = 0, z: float = 0, origin: VecLike = ORIGIN) -> gp_Trsf:
    """Rotate about x, then y, then z (degrees), about ``origin``."""
    t = gp_Trsf()
    for d, a in ((X, x), (Y, y), (Z, z)):
        if a:
            t = rotation(d, a, origin).Multiplied(t)
    return t


