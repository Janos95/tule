"""Write results to ``/workspace/outputs/`` in the format the viewer renders.

One folder per output, holding geometry files and an optional
``output.json``. Files are written atomically (temporary name, then rename)
and ``output.json`` last, so the viewer never sees half an output.

```python
from tule import outputs
outputs.part("bracket", part)                       # outputs/bracket/bracket.step
outputs.assembly("gripper", gripper)                # one file per part + output.json
seq = outputs.sequence("bracket-topopt", "Topology optimization")
for i, (verts, tris, c) in enumerate(steps):
    seq.add(mesh=(verts, tris), label=f"iteration {i}", compliance=c)
seq.done()
outputs.overlay("bracket-loads", part,                # colored faces and arrows on a part
                faces={part.faces["load"]: {"role": "load", "label": "500 N"}},
                arrows=[{"at": (0, 0, 18), "dir": (0, 0, -1), "label": "500 N"}])
outputs.overlay("bracket-walls", part,                # numbers on faces, colored on a scale
                faces={12: 1.8, 13: 0.9}, scale={"label": "wall thickness", "unit": "mm", "min": 0, "max": 3})
outputs.part("bracket", part, info={"material": "aluminium 6061", "mass": "20.8 g"})  # facts, any kind
outputs.drawing("bracket-drawing", part, info={"material": "6061"}, dxf="top")  # PDF + SVG (+ DXF)
```

Nothing here is required: any tool may write the files and JSON directly.

**Overlay** (``kind: "overlay"``): data shown on the faces of one part,
for boundary conditions, checks, or any per-face values. The faces are
B-rep face ids, the same as ``part.faces`` and the viewer's ``f12``::

    {
      "version": 1,
      "kind": "overlay",
      "file": "bracket.step",                 # the part, in the same folder
      "title": "Loads and supports",          # optional
      "faces": {                              # face id (string) -> what to show
        "12": {"severity": "warning", "label": "wall 0.6 mm"},
        "4":  {"role": "fixed", "label": "fixed"},
        "7":  {"role": "load", "label": "500 N -Z"},
        "9":  {"value": 0.31}
      },
      "arrows": [                             # optional: vectors to draw (loads)
        {"at": [x, y, z], "dir": [0, 0, -1], "label": "500 N"}
      ],
      "scale": {"label": "wall thickness", "unit": "mm", "min": 0, "max": 3},  # optional: for "value"s
    }

Every key of a face entry is optional: ``severity`` (``"error"``,
``"warning"``, ``"info"``, ``"ok"``), ``role`` (``"fixed"``, ``"load"``, ``"keep"``,
``"void"``, or another word), ``label`` (short text), ``value`` (a number to
color by). ``dir`` is a
unit vector; ``at`` is in model units (mm). Faces not listed have no data.
A face with only a ``value`` is colored on a blue-to-red scale from
``scale.min`` to ``scale.max`` (default: the values' range), with a legend
bar labelled ``scale.label`` in ``scale.unit``.

**Info** (any kind): ``"info": {"material": "aluminium 6061", "mass": "20.8 g"}``
are facts about the output, shown as rows under the view. Values are text,
numbers or booleans.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Union

from .assembly import Assembly
from .errors import TuleError
from .io import Mesh, export_gltf, export_step, export_stl, write_atomic, write_stl_mesh
from .selection import Selection
from .shape import Shape

VERSION = 1


def root() -> str:
    """The outputs folder: ``$TULE_OUTPUTS``, else ``/workspace/outputs`` if ``/workspace`` exists, else ``./outputs``."""
    env = os.environ.get("TULE_OUTPUTS")
    if env:
        return env
    if os.path.isdir("/workspace"):
        return "/workspace/outputs"
    return os.path.abspath("outputs")


def _slug(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", name.strip()).strip("-.")
    if not s:
        raise TuleError(f"invalid output name {name!r}", op="outputs", hint="use letters, digits, '-', '_' or '.'")
    return s


def _folder(name: str) -> str:
    slug = _slug(name)
    target = os.environ.get("TULE_BUILD_TARGET")
    if target and slug != target:
        raise TuleError(f"src/{target}.py writes only outputs/{target}/, not outputs/{slug}/", op="outputs",
                        hint=f"name the output {target!r}, or make {slug!r} in its own script, src/{slug}.py")
    d = os.path.join(root(), slug)
    os.makedirs(d, exist_ok=True)
    return d


def folder(name: str | None = None) -> str:
    """The folder to write an output's files into (a CSV table, a chart): ``outputs/<name>/``.

    Under ``tule build`` the name defaults to the script's own (``src/bom.py`` → ``bom``)
    and the folder is a staging one that replaces ``outputs/bom/`` when the script succeeds.
    """
    name = name or os.environ.get("TULE_BUILD_TARGET")
    if not name:
        raise TuleError("which output?", op="outputs.folder", hint="outputs.folder(\"bom\"); under tule build it defaults to the script's name")
    return _folder(name)


def _write_geometry(path: str, item: Union[Shape, Assembly]) -> str:
    ext = os.path.splitext(path)[1].lower()
    if ext in (".step", ".stp"):
        return export_step(item, path)
    if ext == ".stl":
        return export_stl(item, path)
    if ext in (".glb", ".gltf"):
        return export_gltf(item, path if ext == ".glb" else path[:-5] + ".glb")
    raise TuleError(f"unsupported format {ext!r}", op="outputs", hint="use step, stl or glb")


def _write_json(folder: str, data: dict) -> str:
    data = {"version": VERSION, **data}
    return write_atomic(os.path.join(folder, "output.json"), json.dumps(data, indent=2) + "\n")


def _info(info) -> dict | None:
    """Checks ``info``: a flat mapping of names to text, numbers or booleans."""
    if info is None:
        return None
    if not isinstance(info, dict):
        raise TuleError(f"info must be a dict, got {type(info).__name__}", op="outputs",
                        hint='{"material": "aluminium 6061", "mass": "20.8 g"}')
    out = {}
    for k, v in info.items():
        if hasattr(v, "item"):  # numpy scalars
            v = v.item()
        if not isinstance(v, (str, int, float, bool)):
            raise TuleError(f"info[{k!r}] is a {type(v).__name__}", op="outputs",
                            hint="use text, a number or a boolean (format units into text: '20.8 g')")
        out[str(k)] = v
    return out


def _mesh_arrays(mesh) -> tuple:
    if isinstance(mesh, Mesh):
        return mesh.positions, mesh.indices
    return mesh[0], mesh[1]


def part(name: str, shape: Union[Shape, Mesh], fmt: str | None = None, title: str | None = None,
         info: dict | None = None) -> str:
    """Write one part: ``outputs/<name>/<name>.<fmt>``. Returns the folder.

    ``shape`` is a Shape (STEP unless ``fmt`` says otherwise) or a ``Mesh``
    (always STL), such as a topology optimization result. ``info``: facts
    shown under the view (``{"mass": "20.8 g"}``).
    """
    info = _info(info)
    folder = _folder(name)
    if isinstance(shape, Mesh):
        fname = f"{_slug(name)}.stl"
        P, I = _mesh_arrays(shape)
        write_stl_mesh(os.path.join(folder, fname), P, I)
        meta: dict[str, Any] = {"kind": "part", "file": fname}
        if title:
            meta["title"] = title
        if info:
            meta["info"] = info
        _write_json(folder, meta)
        return folder
    fmt = fmt or "step"
    fname = f"{_slug(shape.name or name)}.{fmt}"
    _write_geometry(os.path.join(folder, fname), shape)
    meta = {"kind": "part", "file": fname}
    if title:
        meta["title"] = title
    if shape.name:
        meta["name"] = shape.name
    if shape.color:
        meta["color"] = shape.color
    if info:
        meta["info"] = info
    _write_json(folder, meta)
    return folder


def assembly(name: str, item: Union[Assembly, list, tuple], fmt: str = "step", title: str | None = None,
             info: dict | None = None) -> str:
    """Write an assembly: one file per part plus ``output.json`` listing names and colors."""
    info = _info(info)
    asm = item if isinstance(item, Assembly) else Assembly(*item, name=name)
    folder = _folder(name)
    parts = []
    used = set()
    for path, shp in asm.parts():
        base = _slug(path.split("/", 1)[-1].replace("/", "_"))
        fname = f"{base}.{fmt}"
        i = 2
        while fname in used:
            fname = f"{base}_{i}.{fmt}"
            i += 1
        used.add(fname)
        _write_geometry(os.path.join(folder, fname), shp)
        entry = {"file": fname, "name": path}
        if shp.color:
            entry["color"] = shp.color
        parts.append(entry)
    meta: dict[str, Any] = {"kind": "assembly", "parts": parts}
    if title or asm.name:
        meta["title"] = title or asm.name
    if info:
        meta["info"] = info
    _write_json(folder, meta)
    return folder


class Sequence:
    """Frames of a process (an optimization, a simulation) the viewer can scroll and play.

    Each ``add`` writes one frame file and rewrites ``output.json`` with
    ``"status": "running"``; ``done()`` marks it finished.
    """

    def __init__(self, name: str, title: str | None = None, fmt: str = "stl"):
        self.name = name
        self.title = title
        self.fmt = fmt
        self.folder = _folder(name)
        self.frames: list[dict] = []
        self.status = "running"
        self._flush()

    def add(self, shape: Shape | None = None, *, mesh: tuple | None = None, label: str | None = None, **values: float) -> str:
        """Add a frame from a Shape, a ``Mesh`` or raw ``mesh=(positions, triangles)``; ``values`` are plotted."""
        i = len(self.frames)
        if isinstance(shape, Mesh):
            shape, mesh = None, shape
        if mesh is not None:
            fname = f"frame_{i:04d}.stl"
            P, I = _mesh_arrays(mesh)
            write_stl_mesh(os.path.join(self.folder, fname), P, I)
        elif shape is not None:
            fname = f"frame_{i:04d}.{self.fmt}"
            _write_geometry(os.path.join(self.folder, fname), shape)
        else:
            raise TuleError("add a shape or a mesh", op="sequence")
        frame: dict[str, Any] = {"file": fname}
        if label is not None:
            frame["label"] = label
        if values:
            frame["values"] = {k: float(v) for k, v in values.items()}
        self.frames.append(frame)
        self._flush()
        return fname

    def done(self, status: str = "done") -> str:
        self.status = status
        return self._flush()

    def _flush(self) -> str:
        meta: dict[str, Any] = {"kind": "sequence", "frames": self.frames, "status": self.status}
        if self.title:
            meta["title"] = self.title
        return _write_json(self.folder, meta)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.done("failed" if exc_type else "done")


def sequence(name: str, title: str | None = None, fmt: str = "stl") -> Sequence:
    return Sequence(name, title, fmt)


# ---------------------------------------------------------------- overlay

def _face_entries(shape: Shape, data) -> dict:
    """Normalize a ``{face id or face selection: value}`` mapping into overlay face entries."""
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise TuleError("faces is a {face id: value} dict", op="overlay",
                        hint='{part.faces["load"]: {"role": "load", "label": "500 N"}, 12: 1.8}')
    nfaces = len(shape.faces)
    faces: dict[str, dict] = {}
    for k, v in data.items():
        if isinstance(k, Selection):  # part.faces["load"]: every face of the selection
            if k.kind != "face":
                raise TuleError(f"overlay keys must be faces, got {k.kind}s", op="overlay")
            for i in shape._resolve(k, "face").ids:
                faces.update(_face_entries(shape, {i: v}))
            continue
        i = int(k)
        if not 0 <= i < nfaces:
            raise TuleError(f"face id {i} is not a face of this part", op="overlay",
                            hint=f"face ids run from 0 to {nfaces - 1}; pass the part the data was made for")
        if isinstance(v, dict):
            entry = dict(v)
        elif isinstance(v, str):
            entry = {"label": v}
        elif isinstance(v, (int, float)):
            entry = {"value": float(v)}  # the viewer labels it with the scale's unit
        else:
            raise TuleError(f"cannot show a {type(v).__name__} on a face", op="overlay",
                            hint="use a number, a label, or a dict with severity/role/label/value")
        faces[str(i)] = entry
    return faces


def overlay(name: str, part: Shape, faces=None, *, arrows=(), title: str | None = None,
            scale: dict | None = None, info: dict | None = None) -> str:
    """Write a part with per-face data: ``outputs/<name>/<part>.step`` plus an overlay ``output.json``.

    ``faces`` maps a face id or a face selection (``part.faces["load"]``) to a number, a label, or a
    dict (``severity``, ``role``, ``label``, ``value``). ``arrows`` are dicts ``{"at": point, "dir": vector, "label": text}``.
    ``scale`` names the values' color scale: ``{"label", "unit", "min", "max"}``
    (all optional). ``info``: facts shown under the view.
    The format is described at the top of this module. Returns the folder.
    """
    if not isinstance(part, Shape):
        raise TuleError(f"an overlay needs a Shape, got {type(part).__name__}", op="overlay")
    info = _info(info)
    if scale is not None:
        if not isinstance(scale, dict) or set(scale) - {"label", "unit", "min", "max"}:
            raise TuleError("scale is a dict with label, unit, min and max (all optional)", op="overlay",
                            hint='{"label": "wall thickness", "unit": "mm", "min": 0, "max": 3}')
        scale = {k: (float(v) if k in ("min", "max") else str(v)) for k, v in scale.items() if v is not None}
    entries = _face_entries(part, faces)
    folder = _folder(name)
    fname = f"{_slug(part.name or name)}.step"
    _write_geometry(os.path.join(folder, fname), part)
    meta: dict[str, Any] = {"kind": "overlay", "file": fname}
    if title:
        meta["title"] = title
    meta["faces"] = entries
    if arrows:
        out = []
        for a in arrows:
            d: dict[str, Any] = {"at": [float(x) for x in a["at"]], "dir": [float(x) for x in a["dir"]]}
            if a.get("label"):
                d["label"] = str(a["label"])
            out.append(d)
        meta["arrows"] = out
    if scale:
        meta["scale"] = scale
    if info:
        meta["info"] = info
    _write_json(folder, meta)
    return folder


# ---------------------------------------------------------------- drawings

def drawing(name: str, item: Union[Shape, Assembly, list, tuple], views=("front", "top", "right", "iso"), dims=(),
            title: str | None = None, info: dict | None = None, sheet: str = "A4", scale=None,
            hidden: bool = True, projection: str = "first", dxf: str | None = None) -> str:
    """Write a dimensioned 2D drawing: ``outputs/<name>/<name>.pdf`` and ``.svg``. Returns the folder.

    Views (``front top right left bottom back iso``) are projected with hidden lines
    (dashed; ``hidden=False`` drops them) and placed by ``projection`` (``"first"``
    angle, ISO, or ``"third"``). Overall sizes and hole diameters (``4× ⌀5.5``) are
    dimensioned automatically; ``dims`` adds yours: ``("front", p1, p2)`` measures two
    3D points in that view (``{"along": "h" | "v" | "aligned", "text": ..., "offset": mm}``
    as a fourth item), ``("top", part.edges.circles(r=3))`` a diameter. ``info`` rows go
    in the title block (``{"material": "S235"}``). ``scale``: ``"1:2"`` (default: the
    largest standard scale that fits ``sheet``, A4 to A0). ``dxf="top"`` also writes
    that view at 1:1 as ``<name>.dxf`` for laser or waterjet cutting.
    """
    from .drawing import write as write_drawing

    info = _info(info)
    folder = _folder(name)
    write_drawing(folder, _slug(name), item, views=views, dims=dims, title=title, info=info, sheet=sheet,
                  scale=scale, hidden=hidden, projection=projection, dxf_view=dxf)
    return folder
