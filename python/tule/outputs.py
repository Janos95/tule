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
outputs.plate("print", [base, lid], settings={"layer_height": 0.2, "walls": 3})  # print.3mf + the plate in 3D
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

import atexit
import io
import json
import os
import sys
import time
from pathlib import Path
import re
from typing import Any, Union

import numpy as np

from .assembly import Assembly
from .errors import TuleError
from .io import Mesh, _plate, export_3mf, export_gltf, export_step, export_stl, write_atomic, write_stl_mesh
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


def _output_name(name: str) -> str:
    """An output's name: "bracket", or "checks/fit" in a group (made by src/checks/fit.py)."""
    parts = name.strip().strip("/").split("/")
    if len(parts) > 2:
        raise TuleError(f"invalid output name {name!r}", op="outputs", hint="one group at most: 'checks/fit'")
    return "/".join(_slug(p) for p in parts)


def _folder(name: str) -> str:
    slug = _output_name(name)
    target = os.environ.get("TULE_BUILD_TARGET")
    if target and "/" in target and slug == target.rsplit("/", 1)[1]:
        slug = target  # src/checks/fit.py may call its output "fit"
    if target and slug != target:
        raise TuleError(f"src/{target}.py writes only outputs/{target}/, not outputs/{slug}/", op="outputs",
                        hint=f"name the output {target!r}, or make {slug!r} in its own script, src/{slug}.py")
    d = os.path.join(root(), slug)
    os.makedirs(d, exist_ok=True)
    return d


def picture(item: Union[Shape, Assembly], path: str, view: str = "iso", size: int = 1200,
            caption: str = "", labels: bool = False) -> str:
    """A PNG of a shape or assembly for documents (an assembly guide's steps, ``![](step-1.png)``
    in a ``.md`` beside it; a drawing): shaded like ``tule screenshot``, on white, without axes.
    ``view``: iso, front, back, left, right, top, bottom."""
    try:
        from tule_cli.machine import items_to_parts
        from tule_cli.render import render
    except ImportError:
        raise TuleError("picture needs the tule CLI (it is on every Tule machine)", op="picture") from None
    import io as _io

    items = [(n.rsplit("/", 1)[-1], s) for n, s in item.parts()] if isinstance(item, Assembly) else [(item.name or "part", item)]
    img, _ = render(items_to_parts(items), view, size, labels, None, caption, plain=True)
    buf = _io.BytesIO()
    img.save(buf, "PNG")
    return write_atomic(str(path), buf.getvalue())


class Folder(str):
    """A folder path: a str that also works as a Path (``outputs.folder() / "bom.csv"``, ``.iterdir()``, ``.glob("*.stl")``)."""

    def __truediv__(self, other) -> Path:
        return Path(self) / other

    def __getattr__(self, name):
        return getattr(Path(self), name)


def folder(name: str | None = None) -> "Folder":
    """The folder to write an output's files into (a CSV table, a chart): ``outputs/<name>/``.

    Under ``tule build`` the name defaults to the script's own (``src/bom.py`` → ``bom``)
    and the folder is a staging one that replaces ``outputs/bom/`` when the script succeeds.
    """
    name = name or os.environ.get("TULE_BUILD_TARGET")
    if not name:
        raise TuleError("which output?", op="outputs.folder", hint="outputs.folder(\"bom\"); under tule build it defaults to the script's name")
    return Folder(_folder(name))


def _write_geometry(path: str, item: Union[Shape, Assembly]) -> str:
    if isinstance(item, Shape) and not item.faces:
        raise TuleError(f"{item.name or os.path.basename(path)} is empty (no faces)", op="outputs",
                        hint="the operation that made it left nothing: check len(part.faces) after each step")
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
    if isinstance(shape, Assembly):
        raise TuleError(f"{name}: this is an assembly", op="outputs", hint=f'use outputs.assembly("{name}", …)')
    info = _info(info)
    folder = _folder(name)
    if isinstance(shape, Mesh):
        fname = f"{_slug(name.rsplit('/', 1)[-1])}.stl"
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
    fname = f"{_slug(name.rsplit('/', 1)[-1])}.{fmt}"  # named after the output, so its path stays put when the label changes
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
    for path, shp in asm.parts():
        if not shp.faces:
            raise TuleError(f"{path} is empty (no faces)", op="outputs",
                            hint="the operation that made it left nothing: check len(part.faces) after each step")
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


def plate(name: str, parts, *, bed=None, gap: float = 5.0, arrange: bool = True,
          settings: dict | None = None, title: str | None = None, info: dict | None = None,
          estimate: bool | str = False) -> str:
    """A print plate: ``outputs/<name>/<name>.3mf`` to open in the slicer and print, shown as an
    assembly of the parts where they sit on the bed. Returns the folder. The 3MF carries every
    part (Download also offers each part's file): no separate STLs are needed.

    ``parts``: Shapes, ``(name, shape)`` or ``(name, shape, settings)``, or an Assembly, each
    oriented to print (flat face down); ``("pin", pin, {"copies": 4})`` puts four on the plate.
    ``bed``, ``gap``, ``arrange`` and ``settings`` (``layer_height``, ``walls``, ``infill`` %,
    ``supports``, ``brim`` mm, ``material``: PLA, PETG, …) as for ``export_3mf``; the settings
    also show under the view. ``bed``: the printer's (256 × 256 × 256 mm without one). ``info``:
    more facts.

    ``estimate=True`` slices the plate with PrusaSlicer's command line (``prusa-slicer``; raises
    with the install command when it is missing) and shows the print time and filament (g, m)
    under the view; ``output.json``'s ``"plate"`` has them and the printer for a BOM or report
    script. True is a generic i3-class printer; name the user's for its speeds, accelerations
    and bed (from its maker's default profile): ``"bambu-p1s"``, ``"bambu-x1c"``, ``"bambu-a1"``,
    ``"prusa-mk4"``, ``"prusa-core-one"``, ``"ender-3"``, ``"voron-2.4"``. What settings leave
    out is the profile's: 0.2 mm layers, 2 walls, 15 % infill, no supports or brim.
    """
    from .io import _duration, _print_settings, _printer, _prusa_slicer, _slice_estimate, _write_3mf

    info = _info(info)
    base = _print_settings(settings)
    printer = _printer(estimate)
    if printer:
        _prusa_slicer()  # missing: say so before anything is written
    whose = None  # the bed's printer, named in the errors when it does not fit
    if bed is None:
        bed = printer[1].bed if printer else (256, 256, 256)
        whose = printer[1].label if printer and printer[0] != "generic" else None
    placed = _plate(parts, bed, gap, arrange, whose)
    folder = _folder(name)
    stem = _slug(name.rsplit("/", 1)[-1])
    path = _write_3mf(placed, os.path.join(folder, f"{stem}.3mf"), base)
    shown: dict[str, Any] = {"bed": " × ".join(f"{float(b):g}" for b in tuple(bed)[:2]) + " mm"}
    for k, v in base.items():
        shown[k.replace("_", " ")] = (False if k == "brim" and not v else f"{v:g} mm" if k in ("layer_height", "brim")
                                      else f"{v:g} %" if k == "infill" else v)
    own = list(dict.fromkeys(n for n, _, o, _, _ in placed if o))
    if own:
        shown["own settings"] = ", ".join(own)
    summary: dict[str, Any] = {"file": os.path.basename(path), "parts": {}}
    for n, *_, copies in placed:
        summary["parts"][n] = copies
    if printer:
        est = _slice_estimate(path, base, bed, printer=printer[1])
        summary["printer"] = printer[0]
        summary["estimate"] = est
        shown["print time"] = _duration(est["print_time_s"])
        if "filament_g" in est:
            g, m = est["filament_g"], est.get("filament_m")
            shown["filament"] = f"{g:.1f} g" if g < 10 else f"{g:.0f} g"
            if m is not None:
                shown["filament"] += f" ({m:.2f} m)" if m < 10 else f" ({m:.1f} m)"
        shown["estimate"] = f"{printer[1].label}, PrusaSlicer"
    # the view: each part's file once, its copies placed again ("pin ×4/2")
    entries, files, used = [], {}, set()
    for n, shp, _, k, copies in placed:
        if n not in files:
            base_name = _slug(n.replace("/", "_"))
            fname, i = f"{base_name}.step", 2
            while fname in used:
                fname, i = f"{base_name}_{i}.step", i + 1
            used.add(fname)
            _write_geometry(os.path.join(folder, fname), shp)
            files[n] = (fname, shp.bounds[0])
        fname, first = files[n]
        entry: dict[str, Any] = {"file": fname, "name": f"{name}/{n}" if copies == 1 else f"{name}/{n} ×{copies}/{k}"}
        if shp.color:
            entry["color"] = shp.color
        if copies > 1:
            entry["copies"] = copies
            if k > 1:
                lo = shp.bounds[0]
                entry["transform"] = [1, 0, 0, round(lo.x - first.x, 5), 0, 1, 0, round(lo.y - first.y, 5), 0, 0, 1, 0, 0, 0, 0, 1]
        entries.append(entry)
    meta: dict[str, Any] = {"kind": "assembly", "parts": entries, "title": title or name,
                            "info": {**shown, **(info or {})}, "plate": summary}
    _write_json(folder, meta)
    return folder


class Sequence:
    """Frames of a process (an optimization, a simulation) the viewer can scroll and play.

    Each ``add`` writes one frame file; ``output.json`` (``"status": "running"``) follows
    at most once a second, and ``done()`` (the end of a ``with`` block) writes it finished.
    ``info``: facts shown under the view, as for a part.
    """

    WARN_TOTAL = 50 << 20  # new geometry per sequence before the warning (and 1/50 of it per frame)

    def __init__(self, name: str, title: str | None = None, fmt: str | None = None, info: dict | None = None):
        self.name = name
        self.title = title
        self.info = info
        self._written = self._added = 0.0
        atexit.register(lambda: self._written < self._added and self._flush())  # frames of the last second
        self.fmt = fmt
        self.folder = _folder(name)
        self.frames: list[dict] = []
        self._parts: list[tuple] = []  # (geometry, name, color, file, geometry hash): each part's file, written once
        self._bytes = 0  # written so far, for the warning when frames carry their geometry each time
        self._warned = False
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
        elif isinstance(shape, (list, tuple, Assembly)) and self.fmt is None:
            # an assembly's frame: each part's file is written once; a part that only moved
            # (.at, .rotate: the same solid) costs its placement, so a motion study stays small
            asm = shape if isinstance(shape, Assembly) else Assembly(*shape, name=self.name)
            fname = None
            placed = [self._place(path.split("/", 1)[-1], part) for path, part in asm.parts()]
        elif shape is not None:
            fmt = self.fmt or "stl"
            fname = f"frame_{i:04d}.{fmt}"
            _write_geometry(os.path.join(self.folder, fname), shape)
        else:
            raise TuleError("add a shape or a mesh", op="sequence")
        frame: dict[str, Any] = {"file": fname} if fname else {"parts": placed}
        if label is not None:
            frame["label"] = label
        if values:
            frame["values"] = {k: float(v) for k, v in values.items()}
        self.frames.append(frame)
        self._added = time.monotonic()
        self._check_size(fname, placed if fname is None else None)
        if len(self.frames) == 1 or time.monotonic() - self._written >= 1.0:  # not a rewrite per frame
            self._flush()
        return fname or ""

    def _check_size(self, fname, placed):
        """Warns once when the frames carry their geometry every time (a motion study that rebuilds its
        parts per pose): hundreds of MB that the viewer and autosaves must carry."""
        new = [fname] if fname else [e["file"] for e in placed or () if e.get("new")]
        for e in placed or ():
            e.pop("new", None)
        for f in new:
            try:
                self._bytes += os.path.getsize(os.path.join(self.folder, f))
            except OSError:
                pass
        n = len(self.frames)
        if n == 1:
            self._first = self._bytes  # the first frame writes every part: what comes after is the question
        later = self._bytes - getattr(self, "_first", 0)
        if self._warned or n < 3 or later < self.WARN_TOTAL or later / (n - 1) < self.WARN_TOTAL / 50:
            return
        self._warned = True
        how = ("make each part once and move it per frame (part.rotate(...).at(...)): a moved part costs only its placement"
               if fname is None else "add a list of parts (or an Assembly) per frame, each made once and moved, not one merged shape")
        print(f"tule: sequence {self.name!r}: {self._bytes >> 20} MB after {n} frames, about {later / (n - 1) / 2**20:.1f} MB "
              f"of new geometry per frame; {how}", file=sys.stderr, flush=True)

    def _place(self, name: str, part: Shape) -> dict:
        """A frame's entry for one part: its file (written the first time this solid appears) and placement."""
        from OCP.TopLoc import TopLoc_Location

        loc = part.wrapped.Location()
        base = part.wrapped.Located(TopLoc_Location())
        fname = next((f for g, n, c, f, _ in self._parts if g.IsPartner(base) and n == name and c == part.color), None)
        sig = None
        if fname is None:  # the same solid made again (cylinder(12, 1) each frame): the same geometry, moved
            sig = _brep_hash(base)
            fname = next((f for _, n, c, f, h in self._parts if h == sig and n == name and c == part.color), None)
        if fname is None:
            stem, used = _slug(name), {p[3] for p in self._parts}
            fname, k = f"{stem}.step", 2
            while fname in used:
                fname, k = f"{stem}_{k}.step", k + 1
            _write_geometry(os.path.join(self.folder, fname), Shape(base, name=name, color=part.color))
            self._parts.append((base, name, part.color, fname, sig))
            entry: dict[str, Any] = {"file": fname, "name": name, "new": True}
        else:
            entry = {"file": fname, "name": name}
        if part.color:
            entry["color"] = part.color
        if not loc.IsIdentity():
            t = loc.Transformation()
            entry["transform"] = [round(t.Value(r, c), 7 if c < 4 else 5) for r in (1, 2, 3) for c in (1, 2, 3, 4)] + [0, 0, 0, 1]
        return entry

    def done(self, status: str = "done") -> str:
        self.status = status
        return self._flush()

    def _flush(self) -> str:
        meta: dict[str, Any] = {"kind": "sequence", "frames": self.frames, "status": self.status}
        if self.title:
            meta["title"] = self.title
        if self.info:
            meta["info"] = self.info
        self._written = time.monotonic()
        return _write_json(self.folder, meta)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.done("failed" if exc_type else "done")


def _brep_hash(shape) -> str:
    """The solid's geometry as text, hashed: equal for two shapes made the same way."""
    import hashlib
    import io

    from OCP.BRepTools import BRepTools

    f = io.BytesIO()
    BRepTools.Write_s(shape, f)
    return hashlib.sha1(f.getvalue()).hexdigest()


def sequence(name: str, title: str | None = None, fmt: str | None = None, info: dict | None = None) -> Sequence:
    return Sequence(name, title, fmt, info)


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
    fname = f"{_slug(name.rsplit('/', 1)[-1])}.step"
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


# ---------------------------------------------------------------- drawings

def drawing(name: str, item: Union[Shape, Assembly, list, tuple], views=None, dims=(),
            title: str | None = None, info: dict | None = None, notes=None, tol: dict | None = None,
            revisions=None, callouts=None, sheet: str | None = None, scale=None, hidden: bool = True,
            projection: str = "first", dxf: str | None = None, sheets=None, datums: dict | None = None) -> str:
    """Write a dimensioned shop drawing: ``outputs/<name>/<name>.pdf`` (every sheet) and one
    ``.svg`` per sheet (``<name>.svg``, ``<name>-2.svg``, ...). Returns the folder.

    Views (``front top right left bottom back iso``) are projected with hidden lines
    (dashed; ``hidden=False`` drops them) and placed by ``projection`` (``"first"``
    angle, ISO, or ``"third"``). ``("front", point)`` in ``views`` draws that view as a
    section A-A through ``point`` (a 3D point or a one-item selection): material between the
    plane and the viewer removed, cut faces hatched, the plane marked on a view that sees it
    edge-on. Further sections get B, C, ... More: ``tule docs drawings``.

    Automatic: overall sizes, and callouts for holes and slots, counted, in standard form,
    once per drawing, in the view that shows them best: ``4× ⌀6.6 THRU`` (only when nothing is beyond either
    end; a hole into a slit or pocket gets its depth ``↧``) / ``⌴ ⌀11 ↧6.5`` counterbore,
    ``⌵ ⌀12 × 90°`` countersink, ``4× slot 5.5 × 11.5 THRU`` (width × overall length). Each
    callout goes on the side where its leader crosses the fewest lines; leaders never cross
    each other or a dimension line, and no text touches a line or another text.
    ``callouts="top"`` or ``{"right": "top"}`` puts a view's callouts on that side;
    ``"none"`` leaves them to another view.

    ``dims`` adds yours: ``("top", p1, p2)`` measures two 3D points (or one-item
    selections) in that view; a fourth item ``{"along": "h" | "v" | "aligned",
    "tol": "±0.05" | "basic", "text": "40", "offset": mm, "side": "top"}``. A feature picked
    by its faces or circular edges gets its own callout, in the view given or the best one:
    ``("right", part.faces.cylindrical(r=3.25), {"tol": "+0.1/0", "side": "top", "text":
    "M6×1-6H ↧12", "frame": "position ⌀0.1 M A B C"})`` (``text`` replaces the callout,
    ``"\\n"`` between lines); on a slot ``"width": "+0.1/0"`` and ``"length": True`` dimension
    it. A concave arc (pocket corner, fillet) gets ``R3 ↧14``, a boss ``⌀``/``R``. A planar
    face takes ``{"frame": ["flatness 0.05", "parallel 0.05 A"]}``. ``datums={"A":
    part.faces.bottom(), "B": holes}`` draws datum feature symbols; frames name them.

    ``tol`` tolerances a size by its printed text: ``{"⌀22": "+0.05/0", "88": "±0.1",
    "slot 5.5": "+0.1/0", "↧6.5": "±0.1"}``; values ``"±0.1"``, ``"+0.05/0"`` (stacked),
    ``"H7"``. A key that matches nothing, or more than one callout, is an error naming them:
    then tolerance the one feature in ``dims``.

    ``title`` (else ``info["part"]``, else the shape's name) heads the title block and is the
    PDF's title. ``info`` fills the title block: ``number`` (part number), ``rev``, ``material``,
    ``finish``, ``tolerance`` (general tolerances, default ISO 2768-m; ``""`` drops it),
    ``drawn``, ``date``, ``checked``, ``approved``; other keys get rows of their own.
    Nothing is dated unless you say so: the same drawing writes the same bytes.
    ``revisions=[("A", "First issue"), ("B", "Slots 2 mm longer", "2026-11-02")]``
    draws a revision table; the last is the current rev. ``notes=["Break all edges
    0.3", "Anodize clear, MIL-A-8625 Type II"]``: a numbered NOTES block.

    ``sheet``: ``"A4"`` to ``"A0"``; left out, the smallest of A4, A3, A2 that takes the views.
    ``scale``: left out, 1:1 when it fits (2:1 and up only for a small part), else the
    largest ISO reduction; ``"1.5:1"`` or any ratio when you need one, and the error for one
    that does not fit says what is too big and names the largest that does. ``sheets=[{"views":
    ("front", "top"), "sheet": "A3"}, {"views": (("front", point), "iso"), "callouts": "none"}]``
    lays the drawing out over several sheets. ``dxf="top"`` also writes that
    view at 1:1 as ``<name>.dxf`` for laser or waterjet cutting.
    """
    from .drawing import write as write_drawing

    info = _info(info)
    folder = _folder(name)
    write_drawing(folder, _slug(name.rsplit("/", 1)[-1]), item, views=views, dims=dims, title=title, info=info,
                  sheet=sheet, scale=scale, hidden=hidden, projection=projection, dxf_view=dxf, notes=notes,
                  tol=tol, revisions=revisions, callouts=callouts, sheets=sheets, datums=datums)
    return folder
