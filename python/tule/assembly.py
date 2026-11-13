"""Assemblies: named, colored parts grouped in a tree."""

from __future__ import annotations

from typing import Iterator, Union

from OCP.BRep import BRep_Builder
from OCP.TopoDS import TopoDS_Compound

from .errors import TuleError
from .geom import Vec
from .shape import Shape

Child = Union[Shape, "Assembly", "Instances"]


def _pair(c) -> bool:
    return isinstance(c, tuple) and len(c) == 2 and isinstance(c[0], str)


class Assembly:
    """A group of parts (Shapes) and sub-assemblies.

    ``Assembly(base.named("base"), arm.named("arm", "#4a8"), name="gripper")``.
    Transforms apply to every part. ``a.parts()`` lists ``(path, shape)``;
    ``a["arm"]`` finds a part by name or path. A part placed many times is one child:
    ``Assembly(hull, tile.placed(at=centers).named("tiles"))`` (``tule.Instances``; ``parts()`` lists its copies).
    """

    def __init__(self, *children: Child, name: str | None = None):
        flat = []
        for c in children:
            if isinstance(c, (list, tuple)):
                # (path, shape) pairs, as parts() lists them, give their shape
                flat.extend(x[1] if _pair(x) else x for x in ([c] if _pair(c) else c))
            else:
                flat.append(c)
        from .instances import Instances

        for c in flat:
            if isinstance(c, Instances) and getattr(c.body, "kind", None) == "implicit":
                raise TuleError("copies of an implicit have no surface to show or export yet", op="assembly",
                                hint="mesh the body once at the resolution you want: lattice.placed(T).to_mesh(cell=0.4)")
            if getattr(c, "kind", None) == "implicit":
                raise TuleError("an implicit has no surface to show or export yet", op="assembly",
                                hint="mesh it at the resolution you want: Assembly(base, lattice.to_mesh(cell=0.4), …)")
            if not (isinstance(c, (Shape, Assembly, Instances)) or getattr(c, "kind", None) == "mesh"):
                raise TypeError(f"assembly children must be Shapes, Meshes, Assemblies or placed copies, got {type(c).__name__}")
        self.children: list[Child] = flat
        self.name = name

    def named(self, name: str, color: str | None = None, material=None) -> "Assembly":
        """The same parts under a new name; ``color`` paints every part (a purchased part in one color),
        ``material`` makes every part of it (``"steel"``; see ``Shape.with_material``)."""
        if color is None and material is None:
            return Assembly(*self.children, name=name)
        return Assembly(*self._map(lambda s: s.named(s.name, color, material)).children, name=name)

    def with_material(self, material=None, *, metal=None, roughness=None, color=None) -> "Assembly":
        """Every part in this material (see ``Shape.with_material``)."""
        return Assembly(*self._map(lambda s: s.with_material(material, metal=metal, roughness=roughness, color=color)).children,
                        name=self.name)

    def parts(self, prefix: str = "") -> list[tuple[str, Shape]]:
        """Every part with its path (``"gripper/finger_l"``); unnamed parts are ``part1``, ``part2``, …
        Placed copies (``part.placed(...)``) are listed one by one: ``"gripper/pin[3]"``."""
        out = []
        for path, c in self._leaves(prefix):
            if hasattr(c, "leaves"):  # Instances: each copy, moved
                out.extend(c._copies(path))
            else:
                out.append((path, c))
        return out

    def _leaves(self, prefix: str = "", _count: list | None = None) -> list:
        """``[(path, part or Instances)]``: placed copies stay one entry (what outputs write once)."""
        out = []
        base = f"{prefix}{self.name}/" if self.name else prefix
        # an unnamed sub-assembly shares its parent's folder, so it continues the parent's numbering
        n = _count if _count is not None and not self.name else [0]
        for c in self.children:
            if isinstance(c, Assembly):
                out.extend(c._leaves(base, n))
            else:
                n[0] += 1
                out.append((base + (c.name or f"part{n[0]}"), c))
        return out

    def __iter__(self) -> Iterator[Child]:
        return iter(self.children)

    def __len__(self) -> int:
        return len(self.children)

    def __getitem__(self, key: str) -> Shape:
        for path, part in self._leaves():  # a group of copies by its name first
            if path == key or path.endswith("/" + key) or part.name == key:
                return part
        for path, part in self.parts():
            if path == key or path.endswith("/" + key) or part.name == key:
                return part
        names = ", ".join(p for p, _ in self.parts())
        raise TuleError(f"no part named {key!r}", op="assembly", hint=f"parts: {names}")

    def clashes(self, ignore=(), min_volume: float = 1e-3) -> list[tuple[str, str, float]]:
        """Parts that overlap: ``[(a, b, overlap volume in mm³)]``, largest first. Touching is not a
        clash; ``ignore`` lists pairs of names to skip (a press-fit pin in its bore):
        ``ignore=[("pin", "proximal")]``. Through a motion: ``tule.clearance``."""
        from .clearance import _check

        r = _check(self, ignore, below=0, gaps=False, min_volume=min_volume)
        return [(p.a, p.b, p.overlap) for p in r.pairs if p.overlapping]

    def gaps(self, below: float = 1.0, ignore=()) -> list[tuple[str, str, float]]:
        """Parts closer than ``below`` mm: ``[(a, b, gap in mm)]``, smallest first (0: touching or
        overlapping; ``clashes`` says by how much). ``ignore`` as for ``clashes``; through a motion:
        ``tule.clearance``."""
        from .clearance import _check

        r = _check(self, ignore, below=below, volumes=False)
        return sorted(((p.a, p.b, p.gap) for p in r.pairs if p.gap < below), key=lambda t: t[2])

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
        for path, p in self.parts():
            if not hasattr(p, "wrapped"):
                raise TuleError(f"{path} is a mesh: no B-rep compound of this assembly", op="assembly",
                                hint="meshes join B-rep parts in assemblies, outputs and plates, but not in B-rep "
                                     "operations; part.to_mesh() the B-rep parts to combine them")
            b.Add(c, p.wrapped)
        return Shape(c, name=self.name)

    @property
    def bounds(self):
        """Axis-aligned bounds of every part: the union of the parts' (cached) bounds."""
        boxes = []
        for _, p in self._leaves():
            try:
                boxes.append(p.bounds)
            except TuleError:  # an empty part has no bounds; the others still do
                pass
        if not boxes:
            return self.to_shape().bounds
        return Vec(*map(min, zip(*(lo for lo, _ in boxes)))), Vec(*map(max, zip(*(hi for _, hi in boxes))))

    @property
    def size(self):
        lo, hi = self.bounds
        return hi - lo

    @property
    def volume(self) -> float:
        return sum(p.volume for _, p in self._leaves())

    def placed(self, transforms=None, *, at=None, rotate=None):
        """This assembly placed many times (``tule.Instances``): ``fin.placed(rotate=[(0, 0, a) for a in (0, 90, 180, 270)])``."""
        from .instances import Instances

        return Instances(self, transforms, at=at, rotate=rotate)

    def export_step(self, path: str) -> str:
        from .io import export_step

        return export_step(self, path)

    def export_gltf(self, path: str) -> str:
        from .io import export_gltf

        return export_gltf(self, path)

    def __repr__(self) -> str:
        n = f" {self.name!r}" if self.name else ""
        leaves = self._leaves()
        copies = sum(len(p) for _, p in leaves if hasattr(p, "leaves"))
        k = sum(1 for _, p in leaves if not hasattr(p, "leaves"))
        return f"<Assembly{n}: {k} parts>" if not copies else f"<Assembly{n}: {k} parts and {copies:,} placed copies>"
