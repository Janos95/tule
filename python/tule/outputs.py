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
```

Nothing here is required: any tool may write the files and JSON directly.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Union

from .assembly import Assembly
from .errors import TuleError
from .io import export_gltf, export_step, export_stl, write_atomic, write_stl_mesh
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


def part(name: str, shape: Shape, fmt: str = "step", title: str | None = None) -> str:
    """Write one part: ``outputs/<name>/<name>.<fmt>``. Returns the folder."""
    folder = _folder(name)
    fname = f"{_slug(shape.name or name)}.{fmt}"
    _write_geometry(os.path.join(folder, fname), shape)
    meta: dict[str, Any] = {"kind": "part", "file": fname}
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
        """Add a frame from a Shape or a raw ``mesh=(positions, triangles)``; ``values`` are plotted."""
        i = len(self.frames)
        if mesh is not None:
            fname = f"frame_{i:04d}.stl"
            write_stl_mesh(os.path.join(self.folder, fname), mesh[0], mesh[1])
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


def overlay(name: str, shape: Shape, faces: dict | None = None, arrows: list | None = None,
            title: str | None = None) -> str:
    """Show facts on a part's faces: boundary conditions, DFM findings, what the user marked.

    ``faces`` maps a face selection (``part.faces["load"]``) or a face id to
    ``{"role": "load" | "fixed" | ..., "label": "500 N"}`` or
    ``{"severity": "error" | "warning" | "info" | "ok", "label": "..."}`` (a
    plain string is a label). ``arrows`` are ``{"at": (x, y, z), "dir":
    (dx, dy, dz), "label": "500 N"}``: an arrow along ``dir`` ending at ``at``.
    Faces not mentioned are drawn gray. Returns the folder.
    """
    folder = _folder(name)
    fname = f"{_slug(shape.name or name)}.step"
    out: dict[str, dict] = {}
    for key, spec in (faces or {}).items():
        spec = {"label": spec} if isinstance(spec, str) else dict(spec)
        if isinstance(key, Selection):
            if key.kind != "face":
                raise TuleError(f"overlay keys must be faces, got {key.kind}s", op="overlay")
            ids = shape._resolve(key, "face").ids
        elif isinstance(key, int):
            if not 0 <= key < len(shape.faces):
                raise TuleError(f"no face f{key}", op="overlay", hint=f"face ids are 0..{len(shape.faces) - 1}")
            ids = [key]
        else:
            raise TuleError(f"overlay keys are face selections or face ids, got {key!r}", op="overlay")
        for i in ids:
            out[str(i)] = spec
    arr = []
    for a in arrows or []:
        if "at" not in a or "dir" not in a:
            raise TuleError("an arrow needs 'at' and 'dir'", op="overlay",
                            hint='{"at": (x, y, z), "dir": (0, 0, -1), "label": "500 N"}')
        arr.append({"at": [float(v) for v in a["at"]], "dir": [float(v) for v in a["dir"]],
                    **({"label": a["label"]} if a.get("label") else {})})
    export_step(shape, os.path.join(folder, fname))
    meta: dict[str, Any] = {"kind": "overlay", "file": fname, "faces": out, "arrows": arr}
    if title:
        meta["title"] = title
    _write_json(folder, meta)
    return folder
