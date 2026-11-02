"""Thin helpers over OCP that hide naming differences between OCP versions.

OCP 7.x exposes static methods with an ``_s`` suffix (``BRep_Tool.Surface_s``);
OCP 8 keeps the suffix for most classes but not for the ``TopoDS`` casts, and
moves the ``TopTools`` collections into ``OCP.collections``. Everything in tule
goes through these helpers so either version works.
"""

from __future__ import annotations

from typing import Iterable, Iterator

from OCP.TopAbs import (
    TopAbs_COMPOUND,
    TopAbs_EDGE,
    TopAbs_FACE,
    TopAbs_SHELL,
    TopAbs_SOLID,
    TopAbs_VERTEX,
    TopAbs_WIRE,
)
from OCP.TopExp import TopExp_Explorer
from OCP.TopoDS import TopoDS, TopoDS_Shape

try:  # OCP 7.x
    from OCP.TopTools import TopTools_ListOfShape as _ListOfShape  # type: ignore
except ImportError:  # OCP 8
    from OCP.collections import List_TopoDS_Shape as _ListOfShape  # type: ignore


def static(cls, name: str):
    """Return the static method ``name`` of ``cls`` whatever the OCP version calls it."""
    fn = getattr(cls, name + "_s", None)  # prefer the static form when both exist
    if fn is None:
        fn = getattr(cls, name)
    return fn


KINDS = {
    "vertex": TopAbs_VERTEX,
    "edge": TopAbs_EDGE,
    "wire": TopAbs_WIRE,
    "face": TopAbs_FACE,
    "shell": TopAbs_SHELL,
    "solid": TopAbs_SOLID,
    "compound": TopAbs_COMPOUND,
}

_CAST = {
    TopAbs_VERTEX: "Vertex",
    TopAbs_EDGE: "Edge",
    TopAbs_WIRE: "Wire",
    TopAbs_FACE: "Face",
    TopAbs_SHELL: "Shell",
    TopAbs_SOLID: "Solid",
    TopAbs_COMPOUND: "Compound",
}

_KIND_OF = {v: k for k, v in KINDS.items()}


def kind_of(shape: TopoDS_Shape) -> str:
    return _KIND_OF.get(shape.ShapeType(), "shape")


def cast(shape: TopoDS_Shape):
    """Downcast a TopoDS_Shape to its concrete type (TopoDS_Face, ...)."""
    name = _CAST.get(shape.ShapeType())
    return static(TopoDS, name)(shape) if name else shape


class ShapeSet:
    """An ordered set of sub-shapes compared with ``IsSame`` (orientation ignored)."""

    __slots__ = ("_buckets", "_items")

    def __init__(self, items: Iterable = ()):
        self._buckets: dict[int, list] = {}
        self._items: list = []
        for s in items:
            self.add(s)

    def index(self, s) -> int:
        """0-based position of ``s``, or -1."""
        for i, t in self._buckets.get(hash(s), ()):
            if t.IsSame(s):
                return i
        return -1

    def __contains__(self, s) -> bool:
        return self.index(s) >= 0

    def add(self, s) -> bool:
        if s in self:
            return False
        self._buckets.setdefault(hash(s), []).append((len(self._items), s))
        self._items.append(s)
        return True

    def __iter__(self):
        return iter(self._items)

    def __len__(self) -> int:
        return len(self._items)

    def __getitem__(self, i):
        return self._items[i]


def explore(shape: TopoDS_Shape, kind: str) -> Iterator:
    exp = TopExp_Explorer(shape, KINDS[kind])
    while exp.More():
        yield cast(exp.Current())
        exp.Next()


def subshapes(shape: TopoDS_Shape, kind: str) -> ShapeSet:
    """Unique sub-shapes of one kind, in a stable (exploration) order."""
    return ShapeSet(explore(shape, kind))


def shape_list(items: Iterable) -> _ListOfShape:
    lst = _ListOfShape()
    for s in items:
        lst.Append(s)
    return lst


def from_list(lst) -> list:
    """An OCCT list of shapes as a Python list of downcast shapes.

    Making a Python iterator over an OCCT list costs milliseconds (OCP 8), and
    history lookups return mostly empty or one-element lists, so those skip it.
    """
    n = lst.Size()
    if n == 0:
        return []
    if n <= 2:
        return [cast(lst.First())] if n == 1 else [cast(lst.First()), cast(lst.Last())]
    return [cast(s) for s in lst]
