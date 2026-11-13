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
outputs.part("bracket", part)
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
``"avoid"``, or another word), ``label`` (short text), ``value`` (a number to
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

A field may instead carry its own surface: values at the vertices of a
triangle mesh (a finite-element mesh's boundary), drawn in place of the
part's tessellation, so slots and blades thinner than any grid show as they
are::

        {"label": "von Mises stress", "unit": "MPa", "file": "von-mises-stress.npy",   # (n,) or (n, 3)
         "mesh": {"vertices": "surface.vertices.npy",       # float32 (n, 3)
                  "triangles": "surface.triangles.npy",     # int32 (m, 3)
                  "faces": "surface.faces.npy"},            # optional: int32 (m,), B-rep face id per triangle
         "min": 0, "max": 31, "deform": 50, "animate": true}

Fields may share one mesh. Lines are drawn where the face id changes (the
part's edges). ``"animate": true`` (with ``deform``) swings the shape back and
forth through the vectors: a vibration or buckling mode. The viewer's slider
scales ``deform``.
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
from .mesh import MeshFaces
from .io import Mesh, export_gltf, export_step, export_stl, write_atomic, write_stl_mesh
from .selection import Selection
from .shape import Shape

VERSION = 1



def _color(c) -> str:
    """A part's color as the viewer reads it: "#rrggbb", or "#rrggbbaa" when see-through."""
    from .io import color_hex, parse_rgba
    return color_hex(parse_rgba(c))

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


def picture(item: Union[Shape, Assembly, Mesh], path: str, view: str = "iso", size: int = 1200,
            caption: str = "", labels: bool = False) -> str:
    """A PNG of a shape, assembly or triangle ``Mesh`` (a topopt result's ``.mesh``) for documents
    (an assembly guide's steps, ``![](step-1.png)`` in a ``.md`` beside it; a drawing): shaded like
    ``tule screenshot``, on white, without axes. ``view``: iso, front, back, left, right, top, bottom."""
    try:
        from tule_cli.machine import items_to_parts
        from tule_cli.render import render
    except ImportError:
        raise TuleError("picture needs the tule CLI (it is on every Tule machine)", op="picture") from None
    import io as _io

    if isinstance(item, Assembly):
        items = [(n.rsplit("/", 1)[-1], s) for n, s in item.parts()]
    elif isinstance(item, Mesh):
        if not item.triangles:
            raise TuleError("the mesh is empty", op="picture")
        items = [("mesh", item)]
    else:
        items = [(item.name or "part", item)]
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


def _not_empty(item, what: str):
    if getattr(item, "kind", None) == "implicit":
        raise TuleError(f"{item.name or what} is an implicit: it has no surface until it is meshed", op="outputs",
                        hint="choose the resolution: body.to_mesh(cell=0.4) (smaller cells, finer detail)")
    if isinstance(item, Mesh) and not item.triangles:
        raise TuleError(f"{item.name or what} is an empty mesh", op="outputs", hint="check mesh.triangles after each step")
    if isinstance(item, Shape) and not item.faces:
        raise TuleError(f"{item.name or what} is empty (no faces)", op="outputs",
                        hint="the operation that made it left nothing: check len(part.faces) after each step")


def _write_geometry(path: str, item: Union[Shape, Assembly]) -> str:
    _not_empty(item, os.path.basename(path))
    ext = os.path.splitext(path)[1].lower()
    if isinstance(item, Mesh):  # PLY: regions, name, color and tags in one file
        if ext == ".stl":
            return item.export_stl(path)
        if ext != ".ply":
            raise TuleError(f"a mesh is written as PLY, not {ext}", op="outputs", hint="leave out fmt= for meshes")
        return item.export_ply(path)
    if ext in (".step", ".stp"):
        return export_step(item, path)
    if ext == ".stl":
        return export_stl(item, path)
    if ext in (".glb", ".gltf"):
        return export_gltf(item, path if ext == ".glb" else path[:-5] + ".glb")
    raise TuleError(f"unsupported format {ext!r}", op="outputs", hint="use step, stl or glb (ply for meshes)")


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


def part(name: str, shape: Union[Shape, Mesh], fmt: str | None = None, title: str | None = None) -> str:
    """Write one part: ``outputs/<name>/<name>.<fmt>``. Returns the folder.

    ``shape`` is a Shape (STEP unless ``fmt`` says otherwise) or a ``Mesh`` (STL, keeping its
    regions, name, color and tags). An implicit is meshed first: ``body.to_mesh(cell=0.4)``.
    """
    if isinstance(shape, Assembly):
        raise TuleError(f"{name}: this is an assembly", op="outputs", hint=f'use outputs.assembly("{name}", …)')
    _not_empty(shape, name)  # before the folder: an empty part leaves no empty output behind
    folder = _folder(name)
    if isinstance(shape, Mesh):
        fmt = "stl" if fmt == "stl" else "ply"  # a mesh is PLY (regions, name, color, tags in one file)
    fmt = "glb" if fmt == "gltf" else fmt or "step"  # a binary glTF is written either way
    fname = f"{_slug(name.rsplit('/', 1)[-1])}.{fmt}"  # named after the output, so its path stays put when the label changes
    _write_geometry(os.path.join(folder, fname), shape)
    meta = {"kind": "part", "file": fname}
    if title:
        meta["title"] = title
    if shape.name:
        meta["name"] = shape.name
    if shape.color:
        meta["color"] = _color(shape.color)
    _write_json(folder, meta)
    return folder


def assembly(name: str, item: Union[Assembly, list, tuple], fmt: str = "step", title: str | None = None) -> str:
    """Write an assembly: one file per part plus ``output.json`` listing names and colors."""
    asm = item if isinstance(item, Assembly) else Assembly(*item, name=name)
    for path, shp in asm.parts():
        if isinstance(shp, Mesh):
            _not_empty(shp, path)
        elif not shp.faces:
            raise TuleError(f"{path} is empty (no faces)", op="outputs",
                            hint="the operation that made it left nothing: check len(part.faces) after each step")
    fmt = "glb" if fmt == "gltf" else fmt
    folder = _folder(name)
    parts = []
    used = set()
    for path, shp in asm.parts():
        base = _slug(path.split("/", 1)[-1].replace("/", "_"))
        fname = f"{base}.{'ply' if isinstance(shp, Mesh) else fmt}"
        i = 2
        while fname in used:
            fname = f"{base}_{i}.{'ply' if isinstance(shp, Mesh) else fmt}"
            i += 1
        used.add(fname)
        _write_geometry(os.path.join(folder, fname), shp)
        entry = {"file": fname, "name": path}
        if shp.color:
            entry["color"] = _color(shp.color)
        parts.append(entry)
    meta: dict[str, Any] = {"kind": "assembly", "parts": parts}
    if title or asm.name:
        meta["title"] = title or asm.name
    _write_json(folder, meta)
    return folder


def plate(name: str, parts, *, bed=None, gap: float = 5.0, arrange: bool = True,
          settings: dict | None = None, title: str | None = None, estimate: bool | str = False) -> str:
    """A print plate: ``outputs/<name>/<name>.3mf`` to open in the slicer and print, shown as an
    assembly of the parts where they sit on the bed. Returns the folder. The 3MF carries every
    part (Download also offers each part's file): no separate STLs are needed.

    ``parts``: Shapes, ``(name, shape)`` or ``(name, shape, settings)``, or an Assembly, each
    oriented to print (flat face down); ``("pin", pin, {"copies": 4})`` puts four on the plate.
    ``bed``, ``gap``, ``arrange`` and ``settings`` (``layer_height``, ``walls``, ``infill`` %,
    ``supports``, ``brim`` mm, ``material``: PLA, PETG, …) as for ``export_3mf``. ``bed``: the
    printer's (256 × 256 × 256 mm without one); parts that don't fit on one go onto more plates
    (``<name>-2.3mf``, …, shown side by side; it says so on stderr, ``output.json``'s ``"plates"`` lists them).

    ``estimate=True`` slices the plate with PrusaSlicer's command line (``prusa-slicer``; raises
    with the install command when it is missing) and shows the print time and filament (g)
    in the outputs list; ``output.json``'s ``"plate"`` has them and the printer for a BOM or report
    script. True is a generic i3-class printer; name the user's for its speeds, accelerations
    and bed (from its maker's default profile): ``"bambu-p1s"``, ``"bambu-x1c"``, ``"bambu-a1"``,
    ``"prusa-mk4"``, ``"prusa-core-one"``, ``"ender-3"``, ``"voron-2.4"``. What settings leave
    out is the profile's: 0.2 mm layers, 2 walls, 15 % infill, no supports or brim.
    """
    from .io import _duration, _plates, _print_settings, _printer, _prusa_slicer, _slice_estimate, _write_3mf

    base = _print_settings(settings)
    printer = _printer(estimate)
    if printer:
        _prusa_slicer()  # missing: say so before anything is written
    whose = None  # the bed's printer, named in the errors when it does not fit
    if bed is None:
        bed = printer[1].bed if printer else (256, 256, 256)
        whose = printer[1].label if printer and printer[0] != "generic" else None
    plates = _plates(parts, bed, gap, arrange, whose, base)  # more than one when they don't fit on one
    folder = _folder(name)
    stem = _slug(name.rsplit("/", 1)[-1])
    paths = [_write_3mf(pl, os.path.join(folder, f"{stem}.3mf" if i == 0 else f"{stem}-{i + 1}.3mf"), base)
             for i, pl in enumerate(plates)]
    fact = None  # the outputs list's few words: the print time and filament, once sliced
    total: dict[str, int] = {}
    for pl in plates:
        for n, c in {n: copies for n, _, _, _, copies, _ in pl}.items():
            total[n] = total.get(n, 0) + c
    summary: dict[str, Any] = {"file": os.path.basename(paths[0]), "parts": total}
    if len(plates) > 1:
        summary["plates"] = [{"file": os.path.basename(x), "parts": {n: c for n, _, _, _, c, _ in pl}}
                             for x, pl in zip(paths, plates)]
        b = tuple(bed)
        each = "; ".join(f"{d['file']}: " + ", ".join(f"{n} ×{c}" if c > 1 else n for n, c in d["parts"].items())
                         for d in summary["plates"])
        print(f"tule: outputs.plate {name!r}: the parts do not fit on one {b[0]:g} × {b[1]:g} mm bed, so they go onto "
              f"{len(plates)} plates, a 3MF each ({each})", file=sys.stderr, flush=True)
    if printer:
        ests = [_slice_estimate(x, base, bed, printer=printer[1]) for x in paths]
        est = {k: (sum(e[k] for e in ests) if all(isinstance(e.get(k), (int, float)) for e in ests) else v)
               for k, v in ests[0].items()}  # the plates' totals
        summary["printer"] = printer[0]
        summary["estimate"] = est
        for d, e in zip(summary.get("plates", []), ests):
            d["estimate"] = e
        fact = _duration(est["print_time_s"]) + (f" on {len(plates)} plates" if len(plates) > 1 else "")
        if "filament_g" in est:
            g = est["filament_g"]
            fact += f" · {g:.1f} g" if g < 10 else f" · {g:.0f} g"
    # the view: the plates side by side, each part's file once, its copies placed again ("pin ×4/2")
    W = float(tuple(bed)[0])
    entries, files, used, seen = [], {}, set(), {}
    for p_i, pl in enumerate(plates):
        for n, shp, _, _, _, _ in pl:
            if p_i:
                shp = shp.at(p_i * (W + 30), 0, 0)
            k = seen[n] = seen.get(n, 0) + 1
            copies = total[n]
            if n not in files:
                base_name = _slug(n.replace("/", "_"))
                ext = "ply" if isinstance(shp, Mesh) else "step"
                fname, i = f"{base_name}.{ext}", 2
                while fname in used:
                    fname, i = f"{base_name}_{i}.{ext}", i + 1
                used.add(fname)
                _write_geometry(os.path.join(folder, fname), shp)
                files[n] = (fname, shp.bounds[0])
            fname, first = files[n]
            entry: dict[str, Any] = {"file": fname, "name": f"{name}/{n}" if copies == 1 else f"{name}/{n} ×{copies}/{k}"}
            if shp.color:
                entry["color"] = _color(shp.color)
            if copies > 1:
                entry["copies"] = copies
                if k > 1:
                    lo = shp.bounds[0]
                    entry["transform"] = [1, 0, 0, round(lo.x - first.x, 5), 0, 1, 0, round(lo.y - first.y, 5), 0, 0, 1, 0, 0, 0, 0, 1]
            entries.append(entry)
    meta: dict[str, Any] = {"kind": "assembly", "parts": entries, "title": title or name, "plate": summary}
    if fact:
        meta["fact"] = fact
    _write_json(folder, meta)
    return folder


class Sequence:
    """Frames of a process (an optimization, a simulation) the viewer can scroll and play.

    Each ``add`` writes one frame file; ``output.json`` (``"status": "running"``) follows
    at most once a second, and ``done()`` (the end of a ``with`` block) writes it finished.
    ``fps``: frames per second when the
    viewer plays it (4 when left out); a motion sampled every 0.05 s plays in real time at 20.

    A motion: make the parts once and move them per frame (``.at``, ``.rotate``); a driven part
    from contact with ``touch``; check every frame with ``clearance(output_or_frames)``, not a
    loop of ``distance`` calls (``tule docs Clearance``).
    """

    WARN_TOTAL = 50 << 20  # new geometry per sequence before the warning (and 1/50 of it per frame)

    def __init__(self, name: str, title: str | None = None, fmt: str | None = None, fps: float | None = None):
        if fps is not None and not 0 < float(fps) <= 60:
            raise TuleError(f"fps is frames per second when played, between 0 and 60, got {fps}", op="sequence")
        self.fps = fps
        self.name = name
        self.title = title
        self._written = self._added = 0.0
        atexit.register(lambda: self._written < self._added and self._flush())  # frames of the last second
        self.fmt = "glb" if fmt == "gltf" else fmt  # a binary glTF is written either way
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
        elif isinstance(shape, Shape) and self.fmt is None:
            # one part, as outputs.part writes it (STEP, with its color: an STL draws faceted and gray)
            fname = None
            placed = [self._place(shape.name or self.name, shape)]
        elif isinstance(shape, (list, tuple, Assembly)) and self.fmt is None:
            # an assembly's frame: each part's file is written once; a part that only moved
            # (.at, .rotate: the same solid) costs its placement, so a motion study stays small
            asm = shape if isinstance(shape, Assembly) else Assembly(*shape, name=self.name)
            fname = None
            placed = [self._place(path.split("/", 1)[-1], part) for path, part in asm.parts()]
        elif shape is not None:
            if isinstance(shape, (list, tuple)):  # with a format given: one file per frame
                shape = Assembly(*shape, name=self.name)
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
            entry["color"] = _color(part.color)
        if not loc.IsIdentity():
            t = loc.Transformation()
            entry["transform"] = [round(t.Value(r, c), 7 if c < 4 else 5) for r in (1, 2, 3) for c in (1, 2, 3, 4)] + [0, 0, 0, 1]
        return entry

    def done(self, status: str = "done") -> str:
        self.status = status
        return self._flush()

    def _flush(self) -> str:
        meta: dict[str, Any] = {"kind": "sequence", "frames": self.frames, "status": self.status}
        if self.fps:
            meta["fps"] = float(self.fps)
        if self.title:
            meta["title"] = self.title
        self._written = time.monotonic()
        return _write_json(self.folder, meta)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.done("failed" if exc_type else "done")


def _brep_hash(shape) -> str:
    """The solid's geometry as text, hashed: equal for two shapes made the same way, whether or not one was
    meshed since (a copy without the mesh, and without the 0/1 flag lines that a check or a mesh changes)."""
    import hashlib
    import io
    import re

    from OCP.BRepBuilderAPI import BRepBuilderAPI_Copy
    from OCP.BRepTools import BRepTools

    copy = BRepBuilderAPI_Copy(shape, True, False).Shape()
    f = io.BytesIO()
    BRepTools.Write_s(copy, f)
    return hashlib.sha1(re.sub(rb"(?m)^[01]{7}\r?$", b"", f.getvalue())).hexdigest()


def sequence(name: str, title: str | None = None, fmt: str | None = None, fps: float | None = None) -> Sequence:
    return Sequence(name, title, fmt, fps)


sequence.__doc__ = Sequence.__doc__


# ---------------------------------------------------------------- overlay

def _face_entries(shape: Shape, data) -> dict:
    """Normalize a ``{face id or face selection: value}`` mapping into overlay face entries."""
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise TuleError("faces is a {face id: value} dict", op="overlay",
                        hint='{part.faces["load"]: {"role": "load", "label": "500 N"}, 12: 1.8}')
    nfaces = shape.regions if isinstance(shape, Mesh) else len(shape.faces)
    faces: dict[str, dict] = {}
    for k, v in data.items():
        if isinstance(k, (Selection, MeshFaces)):  # part.faces["load"]: every face of the selection
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


FIELD_KEYS = {"label", "values", "origin", "step", "mesh", "unit", "min", "max", "deform", "note", "animate"}


def _npy(a: np.ndarray) -> bytes:
    buf = io.BytesIO()
    np.save(buf, a)
    return buf.getvalue()


def _same(a: np.ndarray, b: np.ndarray) -> bool:
    """The same mesh array (fields on one mesh share its files)."""
    return a is b or (a.shape == b.shape and bool(np.array_equal(a, b)))


def _fields(fields) -> list[tuple[dict, dict]]:
    """Checks overlay fields; returns (output.json entry, {file name: array}) pairs. A field is a
    grid of nodes (``origin``, ``step``) or values at the vertices of a surface ``mesh``."""
    out, files = [], set()
    meshes: list[tuple] = []  # (vertices, triangles, faces, file names): each mesh written once

    def name(base):
        fname, i = f"{base}.npy", 2
        while fname in files:
            fname, i = f"{base}-{i}.npy", i + 1
        files.add(fname)
        return fname

    for f in fields or ():
        on_mesh = isinstance(f, dict) and "mesh" in f
        need = {"label", "values", "mesh"} if on_mesh else {"label", "values", "origin", "step"}
        if not isinstance(f, dict) or not need <= set(f) or set(f) - FIELD_KEYS or (on_mesh and {"origin", "step"} & set(f)):
            raise TuleError("a field is a dict with label, values and either origin and step (a grid) or mesh "
                            "(unit, min, max, deform, note, animate optional)", op="overlay",
                            hint='{"label": "temperature", "unit": "°C", "values": grid, "origin": (0, 0, 0), "step": 0.5}; '
                                 'or {"label": "stress", "values": per_vertex, "mesh": (vertices, triangles, face_ids)}')
        v = np.asarray(f["values"], dtype=np.float32)
        own: dict[str, np.ndarray] = {}
        entry: dict[str, Any] = {"label": str(f["label"])}
        if on_mesh:
            mesh = f["mesh"]
            if not isinstance(mesh, (list, tuple)) or len(mesh) not in (2, 3):
                raise TuleError(f"field {f['label']!r}: mesh is (vertices (n, 3), triangles (m, 3)[, B-rep face id per triangle])",
                                op="overlay")
            P = np.asarray(mesh[0], dtype=np.float64)
            T = np.asarray(mesh[1])
            F = np.asarray(mesh[2]) if len(mesh) == 3 and mesh[2] is not None else None
            if P.ndim != 2 or P.shape[1] != 3 or T.ndim != 2 or T.shape[1] != 3 or not len(T) or \
                    (F is not None and F.shape != (len(T),)) or T.min() < 0 or T.max() >= len(P):
                raise TuleError(f"field {f['label']!r}: mesh is (vertices (n, 3), triangles (m, 3) into them[, (m,) face ids])",
                                op="overlay", hint=f"got {P.shape}, {T.shape}" + (f", {F.shape}" if F is not None else ""))
            if v.shape not in ((len(P),), (len(P), 3)):
                raise TuleError(f"field {f['label']!r}: values must be one per vertex, ({len(P)},) or ({len(P)}, 3); got {v.shape}",
                                op="overlay")
            known = next((m for m in meshes if _same(m[0], P) and _same(m[1], T) and
                          (m[2] is None if F is None else m[2] is not None and _same(m[2], F))), None)
            if known is None:
                prefix = _slug("surface" if not meshes else f"surface-{len(meshes) + 1}")
                names = {"vertices": name(f"{prefix}.vertices"), "triangles": name(f"{prefix}.triangles")}
                own[names["vertices"]] = np.ascontiguousarray(P, dtype="<f4")
                own[names["triangles"]] = np.ascontiguousarray(T, dtype="<i4")
                if F is not None:
                    names["faces"] = name(f"{prefix}.faces")
                    own[names["faces"]] = np.ascontiguousarray(F, dtype="<i4")
                known = (P, T, F, names)
                meshes.append(known)
            entry["mesh"] = dict(known[3])
        else:
            if v.ndim not in (3, 4) or (v.ndim == 4 and v.shape[3] != 3) or min(v.shape[:3]) < 2:
                raise TuleError(f"field {f['label']!r}: values must be a grid of nodes, (nx, ny, nz) or (nx, ny, nz, 3); got {v.shape}",
                                op="overlay", hint="a number per grid node, or a vector (a displacement)")
            if not float(f["step"]) > 0 or len(f["origin"]) != 3:
                raise TuleError(f"field {f['label']!r}: origin is a point, step a positive length", op="overlay")
        if not np.isfinite(v).all():
            raise TuleError(f"field {f['label']!r} has NaN or infinite values", op="overlay")
        vector = v.ndim == (2 if on_mesh else 4)
        mag = np.linalg.norm(v, axis=-1) if vector else v
        fname = name(_slug(str(f["label"])).lower())
        own[fname] = np.ascontiguousarray(v, dtype="<f4")
        entry["file"] = fname
        if not on_mesh:
            entry["origin"] = [float(x) for x in f["origin"]]
            entry["step"] = float(f["step"])
        if f.get("unit"):
            entry["unit"] = str(f["unit"])
        entry["min"] = float(f["min"]) if f.get("min") is not None else float(mag.min())
        entry["max"] = float(f["max"]) if f.get("max") is not None else float(mag.max())
        if f.get("deform"):
            if not vector:
                raise TuleError(f"field {f['label']!r}: only a vector field can deform the shape", op="overlay")
            entry["deform"] = float(f["deform"])
        if f.get("animate"):
            if not entry.get("deform"):
                raise TuleError(f"field {f['label']!r}: animate swings the shape through its deformation: give deform too",
                                op="overlay")
            entry["animate"] = True
        if f.get("note"):
            entry["note"] = str(f["note"])
        out.append((entry, own))
    return out


def overlay(name: str, part: Shape, faces=None, *, arrows=(), title: str | None = None,
            scale: dict | None = None, fields=(), fact: str | None = None) -> str:
    """Write a part with per-face data: ``outputs/<name>/<part>.step`` plus an overlay ``output.json``.

    ``faces`` maps a face id or a face selection (``part.faces["load"]``) to a number, a label, or a
    dict (``severity``, ``role``, ``label``, ``value``). ``arrows`` are dicts ``{"at": point, "dir": vector, "label": text}``.
    ``scale`` names the values' color scale: ``{"label", "unit", "min", "max"}``
    (all optional). ``fields``: values on the surface, dicts
    ``{"label", "values", "unit", "min", "max", "deform", "note", "animate"}`` with either a grid of
    nodes (values (nx, ny, nz) or (nx, ny, nz, 3), ``"origin"``, ``"step"``) or a surface ``"mesh"``:
    ``(vertices (n, 3), triangles (m, 3), B-rep face id per triangle)`` with values (n,) or (n, 3)
    (``tule.fea`` writes both kinds). ``fact``: the result in a few words, for the outputs list.
    The format is described at the top of this module. Returns the folder.
    """
    _not_empty(part, name)
    if not isinstance(part, (Shape, Mesh)):
        raise TuleError(f"an overlay needs a Shape or a Mesh, got {type(part).__name__}", op="overlay")
    if scale is not None:
        if not isinstance(scale, dict) or set(scale) - {"label", "unit", "min", "max"}:
            raise TuleError("scale is a dict with label, unit, min and max (all optional)", op="overlay",
                            hint='{"label": "wall thickness", "unit": "mm", "min": 0, "max": 3}')
        scale = {k: (float(v) if k in ("min", "max") else str(v)) for k, v in scale.items() if v is not None}
    _not_empty(part, name)
    entries = _face_entries(part, faces)
    grids = _fields(fields)
    folder = _folder(name)
    fname = f"{_slug(name.rsplit('/', 1)[-1])}.{'ply' if isinstance(part, Mesh) else 'step'}"
    _write_geometry(os.path.join(folder, fname), part)
    for _, own in grids:
        for fn, arr in own.items():
            write_atomic(os.path.join(folder, fn), _npy(arr))
    meta: dict[str, Any] = {"kind": "overlay", "file": fname}
    if title:
        meta["title"] = title
    if fact:
        meta["fact"] = str(fact)
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
    _write_json(folder, meta)
    return folder


# ---------------------------------------------------------------- drawings

def drawing(name: str, item: Union[Shape, Assembly, list, tuple, dict], views=None, dims=(),
            title: str | None = None, info: dict | None = None, notes=None, tol: dict | None = None,
            revisions=None, callouts=None, sheet: str | None = None, scale=None, hidden: bool = True,
            projection: str = "first", dxf: str | None = None, sheets=None, datums: dict | None = None,
            table=None, explode=None, bom=None) -> str:
    """Write a dimensioned shop drawing: ``outputs/<name>/<name>.pdf`` (every sheet), one
    ``.svg`` per sheet (``<name>.svg``, ``<name>-2.svg``, ...) and the model as STEP, shown in 3D
    (several parts side by side). Returns the folder.

    ``item``: a part, or ``{"B-20": b20, "B-30": b30}``: each on its own sheets, titled by its
    name; ``dims``, ``datums``, ``tol`` or ``info`` keyed by part name go to that part (``info`` may
    also hold shared facts beside the parts' own: ``{"tolerance": "ISO 2768-m", "B-40": {"number": "B40"}}``).

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
    each other or a dimension line, and no text touches a line or another text. A dimension's
    text sits on its line or just past an arrow; crowded dimensions stack outward.
    ``callouts="top"`` or ``{"right": "top"}`` puts a view's callouts on that side;
    ``"none"`` leaves them to another view.

    ``dims`` adds yours: ``("top", p1, p2)`` measures two 3D points (or one-item
    selections) in that view; a fourth item ``{"along": "h" | "v" | "aligned",
    "tol": "±0.05" | "basic", "text": "40", "label": "ROOT", "offset": mm, "side": "top"}``
    (``text`` replaces the value, ``label`` follows it: ``2.86 ROOT``). A feature picked
    by its faces or circular edges gets its own callout, in the view given or the best one:
    ``("right", part.faces.cylindrical(r=3.25), {"tol": "+0.1/0", "side": "top", "text":
    "M6×1-6H ↧12", "frame": "position ⌀0.1 M A B C"})`` (``text`` replaces the callout,
    ``"\\n"`` between lines; ``label`` follows it); on a slot ``"width": "+0.1/0"`` and
    ``"length": True`` dimension it. A concave arc (pocket corner, fillet) gets ``R3 ↧14``, a boss ``⌀``/``R``. A planar
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
    0.3", "Anodize clear, MIL-A-8625 Type II"]``: a NOTES block on sheet 1, one numbered note
    per item (numbering typed in is dropped; an item or line that only continues the one
    before is joined to it), 3.5 mm text; more than 12 lines go on A4 portrait notes sheets
    at the end. ``table=[{"Part": "B-20", "L": 20}, ...]`` (or lists, the header first) adds
    a last sheet with the table; it, or several parts, takes the notes there.

    ``explode=1`` draws an Assembly as an assembly drawing: one view (iso; ``views="front"``),
    the parts moved apart, a numbered balloon on each distinct part (copies: same geometry,
    names differing by a number, are one item, numbered in part order), its leader to a dot
    on the part, crossing no other, and the parts list (Item, Name, Qty) on the title block.
    The factor stacks the parts along the axis that parts them with least travel: a part
    stays above the one it rests on, of two interlocked (a shaft in a bore) the one listed
    later goes up, and one hidden under a lid is uncovered; ``2`` doubles the gaps, ``0``
    keeps them in place; ``{"shaft": (-80, 0, 0)}`` moves just those parts (name, path, or
    name without its number). ``bom={"cam": {"material": "PLA", "source": "printed"}}`` adds
    columns (``"name"`` replaces the name). No dims, datums or tol.

    ``sheet``: ``"A4"`` to ``"A0"``; left out, the smallest of A4, A3, A2 that takes the views.
    ``scale``: left out, 1:1 when it fits (2:1 and up only for a small part), else the
    largest ISO reduction; ``"1.5:1"`` or any ratio when you need one, and the error for one
    that does not fit says what is too big and names the largest that does. ``sheets=[{"views":
    ("front", "top"), "sheet": "A3"}, {"views": (("front", point), "iso"), "callouts": "none"}]``
    lays the drawing out over several sheets (for each part). ``dxf="top"`` also writes that
    view at 1:1 as ``<name>.dxf`` (``<name>-<part>.dxf`` each) for laser or waterjet cutting.
    """
    from .drawing import write as write_drawing

    if isinstance(item, dict) and isinstance(info, dict) and any(isinstance(info.get(k), dict) for k in item):
        # shared facts and each part's own: {"tolerance": "ISO 2768-m", "Motor block": {"number": "EB-01"}}
        shared = {k: v for k, v in info.items() if k not in item}
        info = {k: {**shared, **(info.get(k) or {})} for k in item}
    each = isinstance(item, dict) and isinstance(info, dict) and info and set(info) <= set(item)
    info = {k: _info(v) for k, v in info.items()} if each else _info(info)
    folder = _folder(name)
    facts = write_drawing(folder, _slug(name.rsplit("/", 1)[-1]), item, views=views, dims=dims, title=title, info=info,
                          sheet=sheet, scale=scale, hidden=hidden, projection=projection, dxf_view=dxf, notes=notes,
                          tol=tol, revisions=revisions, callouts=callouts, sheets=sheets, datums=datums, table=table,
                          explode=explode, bom=bom)
    # the model beside its drawing, so the output shows it in 3D too; several parts side by side
    parts, x = [], None
    for stem, it in facts["stems"]:
        shape = Assembly(*it) if isinstance(it, (list, tuple)) else it
        _write_geometry(os.path.join(folder, stem + ".step"), shape)
        entry: dict[str, Any] = {"file": stem + ".step", "name": stem}
        if getattr(shape, "color", None):
            entry["color"] = _color(shape.color)
        lo, hi = (shape.to_shape() if isinstance(shape, Assembly) else shape).bounds
        if x is not None:
            entry["transform"] = [1, 0, 0, round(x - lo.x, 5), 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]
        x = (hi.x if x is None else x + hi.x - lo.x) + max(10.0, 0.15 * (hi.x - lo.x))
        parts.append(entry)
    meta: dict[str, Any] = {"kind": "part", "file": parts[0]["file"]} if len(parts) == 1 else {"kind": "assembly", "parts": parts}
    meta["title"] = facts["title"]
    _write_json(folder, meta)
    return folder
