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
outputs.overlay("bracket-dfm", part, dfm.cnc(part))  # per-face findings on the part
```

Nothing here is required: any tool may write the files and JSON directly.

**Overlay** (``kind: "overlay"``): data shown on the faces of one part,
for DFM findings, boundary conditions, or any per-face values. The faces are
B-rep face ids, the same as ``part.faces`` and the viewer's ``f12``::

    {
      "version": 1,
      "kind": "overlay",
      "file": "bracket.step",                 # the part, in the same folder
      "title": "DFM cnc-3axis: 2 errors",     # optional
      "faces": {                              # face id (string) -> what to show
        "12": {"severity": "error", "label": "internal corner r1 < tool r3"},
        "4":  {"role": "fixed", "label": "fixed"},
        "7":  {"role": "load", "label": "500 N -Z"},
        "9":  {"value": 0.31, "label": "0.31"}
      },
      "arrows": [                             # optional: vectors to draw (loads)
        {"at": [x, y, z], "dir": [0, 0, -1], "label": "500 N"}
      ],
      "findings": [                           # optional: the full list, for a panel
        {"check": "corner_radius", "severity": "error", "label": "...", "message": "...",
         "faces": [12, 13], "edges": [31], "tags": ["pocket"], "values": {"radius": 1.0, "tool_radius": 3.0}}
      ],
      "summary": "DFM cnc-3axis (...): NOT OK, 2 errors, ..."   # optional, the report as text
    }

Every key of a face entry is optional: ``severity`` (``"error"``,
``"warning"``, ``"info"``), ``role`` (``"fixed"``, ``"load"``, ``"keep"``,
``"void"``, or another word), ``label`` (short text), ``value`` (a number to
color by). When several findings share a face, its entry has the worst
severity and their labels joined with ``"; "``, worst first. ``dir`` is a
unit vector; ``at`` is in model units (mm). Faces not listed have no data.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Union

from .assembly import Assembly
from .errors import TuleError
from .io import Mesh, export_gltf, export_step, export_stl, write_atomic, write_stl_mesh
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
    d = os.path.join(root(), _slug(name))
    os.makedirs(d, exist_ok=True)
    return d


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


def _mesh_arrays(mesh) -> tuple:
    if isinstance(mesh, Mesh):
        return mesh.positions, mesh.indices
    return mesh[0], mesh[1]


def part(name: str, shape: Union[Shape, Mesh], fmt: str | None = None, title: str | None = None) -> str:
    """Write one part: ``outputs/<name>/<name>.<fmt>``. Returns the folder.

    ``shape`` is a Shape (STEP unless ``fmt`` says otherwise) or a ``Mesh``
    (always STL), such as a topology optimization result.
    """
    folder = _folder(name)
    if isinstance(shape, Mesh):
        fname = f"{_slug(name)}.stl"
        P, I = _mesh_arrays(shape)
        write_stl_mesh(os.path.join(folder, fname), P, I)
        meta: dict[str, Any] = {"kind": "part", "file": fname}
        if title:
            meta["title"] = title
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
    _write_json(folder, meta)
    return folder


def assembly(name: str, item: Union[Assembly, list, tuple], fmt: str = "step", title: str | None = None) -> str:
    """Write an assembly: one file per part plus ``output.json`` listing names and colors."""
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

_RANK = {"error": 0, "warning": 1, "info": 2}


def _face_entries(shape: Shape, data) -> tuple[dict, list | None, str | None]:
    """Normalize a Report, findings, or a ``{face id: value}`` mapping into overlay face entries."""
    from .dfm import Finding, Report

    nfaces = len(shape.faces)
    summary = None
    if isinstance(data, Report):
        summary = str(data)
        data = data.findings
    if data is None:
        return {}, None, summary

    def check(i: int, what: str) -> int:
        if not 0 <= i < nfaces:
            raise TuleError(f"{what} {i} is not a face of this part", op="overlay",
                            hint=f"face ids run from 0 to {nfaces - 1}; pass the part the data was made for")
        return i

    if isinstance(data, dict):
        faces: dict[str, dict] = {}
        for k, v in data.items():
            i = check(int(k), "face id")
            if isinstance(v, dict):
                entry = dict(v)
            elif isinstance(v, str):
                entry = {"label": v}
            elif isinstance(v, (int, float)):
                entry = {"value": float(v), "label": f"{v:.3g}"}
            else:
                raise TuleError(f"cannot show a {type(v).__name__} on a face", op="overlay",
                                hint="use a number, a label, or a dict with severity/role/label/value")
            faces[str(i)] = entry
        return faces, None, summary
    findings = list(data)
    if not all(isinstance(f, Finding) for f in findings):
        raise TuleError("expected a dfm Report, a list of findings, or a {face id: value} dict", op="overlay")
    per_face: dict[int, list] = {}
    for f in sorted(findings, key=lambda f: _RANK.get(f.severity, 3)):
        for i in f.faces:
            per_face.setdefault(check(i, "a finding refers to face"), []).append(f)
    faces = {}
    for i in sorted(per_face):
        fs = per_face[i]
        labels = list(dict.fromkeys(f.label for f in fs))
        faces[str(i)] = {"severity": fs[0].severity, "label": "; ".join(labels)}
    return faces, [f.to_dict() for f in findings], summary


def overlay(name: str, part: Shape, faces=None, *, arrows=(), title: str | None = None) -> str:
    """Write a part with per-face data: ``outputs/<name>/<part>.step`` plus an overlay ``output.json``.

    ``faces`` is a ``dfm`` Report, a list of findings, or a mapping from face
    id to a number, a label, or a dict (``severity``, ``role``, ``label``,
    ``value``). ``arrows`` are dicts ``{"at": point, "dir": vector, "label": text}``.
    The format is described at the top of this module. Returns the folder.
    """
    if not isinstance(part, Shape):
        raise TuleError(f"an overlay needs a Shape, got {type(part).__name__}", op="overlay")
    entries, findings, summary = _face_entries(part, faces)
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
    if findings is not None:
        meta["findings"] = findings
    if summary:
        meta["summary"] = summary
    _write_json(folder, meta)
    return folder
