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

**Fields** (overlay): values on the part's surface, not per face (stress,
displacement), from a grid of nodes in a NumPy ``.npy`` file next to the part::

      "fields": [
        {"label": "von Mises stress", "unit": "MPa", "file": "von-mises-stress.npy",
         "origin": [x, y, z], "step": 0.5, "min": 0, "max": 31,   # node (i, j, k) at origin + step·(i, j, k)
         "note": "peak 40.2 MPa"},                                # optional: a line in the legend
        {"label": "displacement", "unit": "mm", "file": "displacement.npy", "origin": [x, y, z], "step": 0.5,
         "min": 0, "max": 0.21, "deform": 50}                     # a vector field: shape moved 50 × the vectors
      ]

The file holds little-endian float32 values, shape ``(nx, ny, nz)`` (a number
per node) or ``(nx, ny, nz, 3)`` (a vector, colored by its length). The
viewer and ``tule screenshot`` interpolate it (trilinear) at the part's
surface and color it on the scale from ``min`` to ``max``. The first field
is shown; the legend switches between them. Nodes outside the part should
carry the nearest inside value.

**Info** (any kind): ``"info": {"material": "aluminium 6061", "mass": "20.8 g"}``
are facts about the output, shown as rows under the view. Values are text,
numbers or booleans.
"""

from __future__ import annotations

import io
import json
import os
import re
from typing import Any, Union

import numpy as np

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


FIELD_KEYS = {"label", "values", "origin", "step", "unit", "min", "max", "deform", "note"}


def _fields(fields) -> list[tuple[dict, np.ndarray]]:
    """Checks overlay fields; returns (output.json entry, float32 grid) pairs."""
    out, files = [], set()
    for f in fields or ():
        if not isinstance(f, dict) or not {"label", "values", "origin", "step"} <= set(f) or set(f) - FIELD_KEYS:
            raise TuleError("a field is a dict with label, values, origin and step (unit, min, max, deform, note optional)",
                            op="overlay", hint='{"label": "temperature", "unit": "°C", "values": grid, "origin": (0, 0, 0), "step": 0.5}')
        v = np.asarray(f["values"], dtype=np.float32)
        if v.ndim not in (3, 4) or (v.ndim == 4 and v.shape[3] != 3) or min(v.shape[:3]) < 2:
            raise TuleError(f"field {f['label']!r}: values must be a grid of nodes, (nx, ny, nz) or (nx, ny, nz, 3); got {v.shape}",
                            op="overlay", hint="a number per grid node, or a vector (a displacement)")
        if not np.isfinite(v).all():
            raise TuleError(f"field {f['label']!r} has NaN or infinite values", op="overlay")
        if not float(f["step"]) > 0 or len(f["origin"]) != 3:
            raise TuleError(f"field {f['label']!r}: origin is a point, step a positive length", op="overlay")
        mag = np.linalg.norm(v, axis=3) if v.ndim == 4 else v
        base = _slug(str(f["label"])).lower()
        fname, i = f"{base}.npy", 2
        while fname in files:
            fname, i = f"{base}-{i}.npy", i + 1
        files.add(fname)
        entry: dict[str, Any] = {"label": str(f["label"]), "file": fname,
                                 "origin": [float(x) for x in f["origin"]], "step": float(f["step"])}
        if f.get("unit"):
            entry["unit"] = str(f["unit"])
        entry["min"] = float(f["min"]) if f.get("min") is not None else float(mag.min())
        entry["max"] = float(f["max"]) if f.get("max") is not None else float(mag.max())
        if f.get("deform"):
            if v.ndim != 4:
                raise TuleError(f"field {f['label']!r}: only a vector field can deform the shape", op="overlay")
            entry["deform"] = float(f["deform"])
        if f.get("note"):
            entry["note"] = str(f["note"])
        out.append((entry, np.ascontiguousarray(v, dtype="<f4")))
    return out


def overlay(name: str, part: Shape, faces=None, *, arrows=(), title: str | None = None,
            scale: dict | None = None, info: dict | None = None, fields=()) -> str:
    """Write a part with per-face data: ``outputs/<name>/<part>.step`` plus an overlay ``output.json``.

    ``faces`` maps a face id or a face selection (``part.faces["load"]``) to a number, a label, or a
    dict (``severity``, ``role``, ``label``, ``value``). ``arrows`` are dicts ``{"at": point, "dir": vector, "label": text}``.
    ``scale`` names the values' color scale: ``{"label", "unit", "min", "max"}``
    (all optional). ``info``: facts shown under the view. ``fields``: values on the surface from a
    grid of nodes, dicts ``{"label", "values": (nx, ny, nz) or (nx, ny, nz, 3) array, "origin": point,
    "step": spacing, "unit", "min", "max", "deform", "note"}`` (``tule.fea`` writes them).
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
    grids = _fields(fields)
    folder = _folder(name)
    fname = f"{_slug(part.name or name)}.step"
    _write_geometry(os.path.join(folder, fname), part)
    for entry, v in grids:
        buf = io.BytesIO()
        np.save(buf, v)
        write_atomic(os.path.join(folder, entry["file"]), buf.getvalue())
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
    if grids:
        meta["fields"] = [e for e, _ in grids]
    if info:
        meta["info"] = info
    _write_json(folder, meta)
    return folder
