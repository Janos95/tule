"""Assemblies: named, colored parts grouped in a tree."""

from __future__ import annotations

from typing import Iterator, Union

from OCP.BRep import BRep_Builder
from OCP.TopoDS import TopoDS_Compound

from .errors import TuleError
from .shape import Shape

Child = Union[Shape, "Assembly"]


class Assembly:
    """A group of parts (Shapes) and sub-assemblies.

    ``Assembly(base.named("base"), arm.named("arm", "#4a8"), name="gripper")``.
    Transforms apply to every part. ``a.parts()`` lists ``(path, shape)``;
    ``a["arm"]`` finds a part by name or path.
    """

    def __init__(self, *children: Child, name: str | None = None):
        flat = []
        for c in children:
            if isinstance(c, (list, tuple)):
                flat.extend(c)
            else:
                flat.append(c)
        for c in flat:
            if not isinstance(c, (Shape, Assembly)):
                raise TypeError(f"assembly children must be Shapes or Assemblies, got {type(c).__name__}")
        self.children: list[Child] = flat
        self.name = name

    def parts(self, prefix: str = "") -> list[tuple[str, Shape]]:
        """Every part with its path (``"gripper/finger_l"``); unnamed parts are ``part1``, ``part2``, …"""
        out = []
        base = f"{prefix}{self.name}/" if self.name else prefix
        n = 0
        for c in self.children:
            if isinstance(c, Assembly):
                out.extend(c.parts(base))
            else:
                n += 1
                out.append((base + (c.name or f"part{n}"), c))
        return out

    def __iter__(self) -> Iterator[Child]:
        return iter(self.children)

    def __len__(self) -> int:
        return len(self.children)

    def __getitem__(self, key: str) -> Shape:
        for path, part in self.parts():
            if path == key or path.endswith("/" + key) or part.name == key:
                return part
        names = ", ".join(p for p, _ in self.parts())
        raise TuleError(f"no part named {key!r}", op="assembly", hint=f"parts: {names}")

    def _map(self, fn) -> "Assembly":
        return Assembly(*[c._map(fn) if isinstance(c, Assembly) else fn(c) for c in self.children], name=self.name)

    def at(self, x=0, y=0, z=0) -> "Assembly":
        return self._map(lambda s: s.at(x, y, z))

    translate = at

    def rotate(self, x=0, y=0, z=0, about=(0, 0, 0)) -> "Assembly":
        return self._map(lambda s: s.rotate(x, y, z, about))

    def mirror(self, nx=1, ny=0, nz=0, origin=(0, 0, 0)) -> "Assembly":
        return self._map(lambda s: s.mirror(nx, ny, nz, origin))

    def to_shape(self) -> Shape:
        """All parts as one compound (no Boolean union)."""
        c = TopoDS_Compound()
        b = BRep_Builder()
        b.MakeCompound(c)
        for _, p in self.parts():
            b.Add(c, p.wrapped)
        return Shape(c, name=self.name)

    @property
    def bounds(self):
        return self.to_shape().bounds

    def export_step(self, path: str) -> str:
        from .io import export_step

        return export_step(self, path)

    def export_gltf(self, path: str) -> str:
        from .io import export_gltf

        return export_gltf(self, path)

    def __repr__(self) -> str:
        n = f" {self.name!r}" if self.name else ""
        return f"<Assembly{n}: {len(self.parts())} parts>"
