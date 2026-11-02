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

    def named(self, name: str, color: str | None = None) -> "Assembly":
        """The same parts under a new name; ``color`` paints every part (a purchased part in one color)."""
        if color is None:
            return Assembly(*self.children, name=name)
        return Assembly(*self._map(lambda s: s.named(s.name, color)).children, name=name)

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

    def clashes(self, ignore=(), min_volume: float = 1e-3) -> list[tuple[str, str, float]]:
        """Parts that overlap: ``[(a, b, overlap volume in mm³)]``, largest first (for every frame of a
        motion study). Touching is not a clash; ``ignore`` lists pairs of names to skip (a press-fit pin
        in its bore): ``ignore=[("pin", "proximal")]``."""
        from OCP.Bnd import Bnd_Box
        from OCP.BRepBndLib import BRepBndLib

        from ._occ import static

        parts = self.parts()
        skip = {frozenset(p) for p in ignore}
        boxes = []
        for _, s in parts:
            b = Bnd_Box()
            static(BRepBndLib, "Add")(s.wrapped, b, True)
            boxes.append(b)
        out = []
        for i in range(len(parts)):
            for j in range(i + 1, len(parts)):
                (pa, a), (pb, b) = parts[i], parts[j]
                if boxes[i].IsOut(boxes[j]):
                    continue
                if frozenset((pa, pb)) in skip or frozenset((pa.rsplit("/", 1)[-1], pb.rsplit("/", 1)[-1])) in skip:
                    continue
                try:
                    v = (a & b).volume
                except TuleError:
                    v = float("nan") if a.distance(b) == 0 else 0.0  # the Boolean failed: they touch or overlap
                if v != v or v > min_volume:
                    out.append((pa, pb, v))
        return sorted(out, key=lambda t: -(t[2] if t[2] == t[2] else float("inf")))

    def gaps(self, below: float = 1.0, ignore=()) -> list[tuple[str, str, float]]:
        """Parts closer than ``below`` mm: ``[(a, b, gap in mm)]``, smallest first (0: touching or
        overlapping; ``clashes`` says by how much). Run it on every frame of a motion study for the
        running clearance; ``ignore`` as for ``clashes``."""
        from OCP.Bnd import Bnd_Box
        from OCP.BRepBndLib import BRepBndLib

        from ._occ import static

        parts = self.parts()
        skip = {frozenset(p) for p in ignore}
        boxes = []
        for _, s in parts:
            b = Bnd_Box()
            static(BRepBndLib, "Add")(s.wrapped, b, True)
            b.Enlarge(below / 2)
            boxes.append(b)
        out = []
        for i in range(len(parts)):
            for j in range(i + 1, len(parts)):
                (pa, a), (pb, b) = parts[i], parts[j]
                if boxes[i].IsOut(boxes[j]):
                    continue
                if frozenset((pa, pb)) in skip or frozenset((pa.rsplit("/", 1)[-1], pb.rsplit("/", 1)[-1])) in skip:
                    continue
                d = a.distance(b)
                if d < below:
                    out.append((pa, pb, d))
        return sorted(out, key=lambda t: t[2])

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

    @property
    def size(self):
        return self.to_shape().size

    @property
    def volume(self) -> float:
        return sum(p.volume for _, p in self.parts())

    def export_step(self, path: str) -> str:
        from .io import export_step

        return export_step(self, path)

    def export_gltf(self, path: str) -> str:
        from .io import export_gltf

        return export_gltf(self, path)

    def __repr__(self) -> str:
        n = f" {self.name!r}" if self.name else ""
        return f"<Assembly{n}: {len(self.parts())} parts>"
