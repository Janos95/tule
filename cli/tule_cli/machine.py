"""Commands for the machine (they work on a laptop too, for local files).

    tule screenshot -o view.png [--output NAME | FILE] [--view iso] [--labels] [--size 1200]
    tule inspect FILE [--json]
    tule docs [topic]
    tule snapshot          (on the machine: asks the local tule-agent to save /workspace now)

Outputs live in $TULE_OUTPUTS, else $TULE_WORKSPACE/outputs, else
/workspace/outputs if /workspace exists, else ./outputs.
"""

from __future__ import annotations

import json
import io
import os
import re
import socket
import struct
import subprocess
import sys

GEOMETRY = (".step", ".stp", ".stl")
SOCKET = os.environ.get("TULE_AGENT_SOCKET", "/run/tule/agent.sock")
DOC_ORDER = ["index", "setup", "loop", "modeling", "outputs", "drawings", "sheetmetal", "occt", "annotations", "topopt", "golden-paths"]


from . import CliError


# ---------------------------------------------------------------- where things are


def outputs_root() -> str:
    if os.environ.get("TULE_OUTPUTS"):
        return os.environ["TULE_OUTPUTS"]
    if os.environ.get("TULE_WORKSPACE"):
        return os.path.join(os.environ["TULE_WORKSPACE"], "outputs")
    if os.path.isdir("/workspace"):
        return "/workspace/outputs"
    return os.path.abspath("outputs")


def on_machine() -> bool:
    return bool(os.environ.get("TULE_WORKSPACE")) or os.path.isdir("/workspace")


def _newest_mtime(path: str) -> float:
    if os.path.isfile(path):
        return os.stat(path).st_mtime
    t = 0.0
    for d, _, files in os.walk(path):
        for f in files:
            try:
                t = max(t, os.stat(os.path.join(d, f)).st_mtime)
            except OSError:
                pass
    return t


def find_output(name: str | None, path: str | None) -> str:
    """A file or output folder: FILE as given, --output NAME in the outputs folder, or the newest output."""
    if path:
        if os.path.exists(path):
            return path
        if os.sep not in path.rstrip(os.sep):  # tule screenshot motion: an output's name
            name = path.rstrip(os.sep)
        else:
            raise CliError(f"{path}: no such file or folder")
    root = outputs_root()
    if name:
        for cand in [os.path.join(root, name)] + [os.path.join(root, name + ext) for ext in GEOMETRY]:
            if os.path.exists(cand):
                return cand
        have = ", ".join(sorted(os.listdir(root))) if os.path.isdir(root) else "none"
        raise CliError(f"no output {name!r} in {root} (outputs: {have})")
    if not os.path.isdir(root):
        raise CliError(f"no outputs folder at {root}; pass a file, or write an output first (tule docs outputs)")
    entries = [os.path.join(root, e) for e in os.listdir(root) if not e.startswith(".")]
    entries = [e for e in entries if os.path.isdir(e) or e.lower().endswith(GEOMETRY)]
    if not entries:
        raise CliError(f"{root} is empty; write an output first (tule docs outputs)")
    return max(entries, key=_newest_mtime)


# ---------------------------------------------------------------- loading


def read_stl(path: str):
    """(n, 3, 3) float32 triangles from a binary or ASCII STL."""
    import numpy as np

    with open(path, "rb") as f:
        data = f.read()
    if len(data) >= 84:
        n = struct.unpack_from("<I", data, 80)[0]
        if 84 + 50 * n == len(data):
            rec = np.frombuffer(data, dtype=np.dtype([("n", "<f4", 3), ("v", "<f4", (3, 3)), ("a", "<u2")]), count=n, offset=84)
            return rec["v"].astype(np.float32)
    pts = [list(map(float, line.split()[1:4])) for line in data.decode(errors="replace").splitlines()
           if line.strip().startswith("vertex")]
    return np.asarray(pts, dtype=np.float32).reshape(-1, 3, 3)


def smooth_normals(tri, crease_deg: float = 35.0):
    """Per-corner normals for a triangle soup: averaged over corners at the same point, except across creases."""
    import numpy as np

    tri = np.asarray(tri, dtype=np.float64)
    fn = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])  # area-weighted
    unit = fn / np.maximum(np.linalg.norm(fn, axis=1, keepdims=True), 1e-30)
    P = tri.reshape(-1, 3)
    q = np.round(P / (1e-6 * (np.ptp(P, axis=0).max() or 1.0))).astype(np.int64)
    _, vid = np.unique(q, axis=0, return_inverse=True)
    vid = vid.reshape(-1)
    acc = np.zeros((vid.max() + 1, 3))
    np.add.at(acc, vid, np.repeat(fn, 3, axis=0))
    vn = acc[vid]
    vn /= np.maximum(np.linalg.norm(vn, axis=1, keepdims=True), 1e-30)
    corner = np.repeat(unit, 3, axis=0)
    keep = (vn * corner).sum(1) > np.cos(np.radians(crease_deg))
    return np.where(keep[:, None], vn, corner).astype(np.float32)


def _need_tule():
    try:
        import tule  # noqa: F401
    except ImportError:
        raise CliError("this needs the tule modeling package (it is on every Tule machine; locally: pip install -e python)") from None


def load_items(path: str) -> list[tuple[str, object]]:
    """[(name, Shape or STL triangles)] for a STEP/STL file."""
    ext = os.path.splitext(path)[1].lower()
    base = os.path.splitext(os.path.basename(path))[0]
    if ext == ".stl":
        return [(base, read_stl(path))]
    if ext not in (".step", ".stp"):
        raise CliError(f"{path}: unsupported file type {ext or '(none)'}; use STEP, STL or a PDF"
                       + ("; read an image directly" if ext in (".png", ".jpg", ".jpeg", ".svg") else ""))
    _need_tule()
    from tule import Assembly, import_step

    item = import_step(path)
    if isinstance(item, Assembly):
        return [(n.rsplit("/", 1)[-1], s) for n, s in item.parts()]
    return [(item.name or base, item)]


def to_parts(path: str, color: str | None = None, name: str | None = None):
    return items_to_parts(load_items(path), color, name)


def items_to_parts(items, color: str | None = None, name: str | None = None):
    """Meshes to draw for [(name, Shape or STL triangles)]."""
    import numpy as np

    from .render import Part

    out = []
    for nm, item in items:
        if isinstance(item, np.ndarray):  # STL
            out.append(Part(name or nm, item.reshape(-1, 3), smooth_normals(item),
                            np.arange(3 * len(item)).reshape(-1, 3), np.zeros(len(item), np.int64),
                            _rgb(color), {}, 0))
            continue
        from tule import tessellate

        if not item.faces:
            print(f"tule: {name or nm} is empty (no faces); not drawn", file=sys.stderr)
            continue
        lo, hi = item.bounds
        diag = max((hi - lo).length, 1e-6)
        m = tessellate(item, tolerance=diag / 2000, angular=0.2)
        tags: dict[int, list[str]] = {}
        for t in item.tags:
            try:
                ids = item.faces[t].ids
            except Exception:  # an edge-only tag
                continue
            for i in ids:
                tags.setdefault(i, []).append(t)
        out.append(Part(name or nm, m.positions, m.normals, m.indices, m.face_ids,
                        _rgb(color or item.color), tags, len(item.faces)))
    return out


def _rgb(c):
    if not c:
        return None
    if isinstance(c, (list, tuple)):
        return tuple(float(v) for v in c[:3])
    h = c.lstrip("#")
    if len(h) == 3:
        h = "".join(ch * 2 for ch in h)
    try:
        return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    except ValueError:
        return None


def _numbered(files: list[str]) -> bool:
    return len(files) > 1 and all(re.search(r"\d+\.\w+$", f) for f in files)


SEVERITY = {"error": (0.86, 0.16, 0.13), "warning": (0.95, 0.60, 0.10), "info": (0.20, 0.50, 0.90), "ok": (0.20, 0.68, 0.33)}
ROLES = {"fixed": (0.20, 0.45, 0.90), "load": (0.93, 0.42, 0.10), "keep": (0.55, 0.40, 0.80), "avoid": (0.50, 0.50, 0.50)}
PALETTE = [(0.10, 0.62, 0.62), (0.80, 0.30, 0.55), (0.55, 0.65, 0.15), (0.45, 0.35, 0.75), (0.85, 0.65, 0.10)]


# the viewer's value scale (web/src/overlay.js), low to high
RAMP = [(0x3b, 0x4c, 0xc0), (0x6f, 0x92, 0xf3), (0xaa, 0xc7, 0xfd), (0xf2, 0xe5, 0xa0), (0xf7, 0xa8, 0x89), (0xe2, 0x69, 0x52), (0xb4, 0x04, 0x26)]


def _ramp(t: float) -> tuple:
    x = min(1.0, max(0.0, t)) * (len(RAMP) - 1)
    i = min(len(RAMP) - 2, int(x))
    return tuple((a + (b - a) * (x - i)) / 255 for a, b in zip(RAMP[i], RAMP[i + 1]))


def _is_value(spec: dict) -> bool:
    v = spec.get("value")
    return isinstance(v, (int, float)) and not isinstance(v, bool) and not spec.get("severity") and spec.get("role") is None


def _overlay_color(spec: dict) -> tuple:
    if spec.get("color"):
        return _rgb(spec["color"]) or PALETTE[0]
    if spec.get("severity") in SEVERITY:
        return SEVERITY[spec["severity"]]
    role = spec.get("role")
    if role in ROLES:
        return ROLES[role]
    return PALETTE[sum(map(ord, str(role or ""))) % len(PALETTE)]


def _placed(parts, transform):
    """Parts moved by an output.json "transform" (16 numbers or a 4x4, row-major)."""
    import dataclasses

    import numpy as np

    if transform is None:
        return parts
    m = np.asarray(transform, dtype=np.float64).reshape(4, 4)
    R, t = m[:3, :3], m[:3, 3]
    return [dataclasses.replace(p, positions=(p.positions @ R.T + t).astype(np.float32),
                                normals=(p.normals @ R.T).astype(np.float32)) for p in parts]


RAMP_RGB = [tuple(c / 255 for c in rgb) for rgb in RAMP]


def ramp_array(t):
    """The ramp's colors at t in [0, 1] (an array): (n, 3)."""
    import numpy as np

    stops = np.asarray(RAMP_RGB)
    x = np.clip(np.nan_to_num(np.asarray(t, dtype=np.float64), nan=0.5), 0, 1) * (len(stops) - 1)
    i = np.minimum(len(stops) - 2, x.astype(int))
    return stops[i] + (stops[i + 1] - stops[i]) * (x - i)[:, None]


def pick_field(fields: list, want: str | None, jpath: str) -> dict:
    """The field to show: the first, or the one whose label (or 1-based number) is ``want``."""
    if not want:
        return fields[0]
    labels = [str(f.get("label", "")) for f in fields]
    if want.isdigit() and 1 <= int(want) <= len(fields):
        return fields[int(want) - 1]
    for f, label in zip(fields, labels):
        if want.lower() in label.lower():
            return f
    raise CliError(f"{jpath}: no field {want!r}; fields: {', '.join(labels)}")


def load_field(path: str, spec: dict, jpath: str):
    """A field's grid from its .npy file: (nx, ny, nz) or (nx, ny, nz, 3) floats."""
    import numpy as np

    try:
        grid = np.load(path, allow_pickle=False)
    except (OSError, ValueError) as e:
        raise CliError(f"{path}: not a NumPy .npy file ({e})") from None
    ok = grid.ndim == 3 or (grid.ndim == 4 and grid.shape[3] == 3)
    if not ok or not isinstance(spec.get("origin"), list) or len(spec["origin"]) != 3 or not float(spec.get("step") or 0) > 0:
        raise CliError(f"{jpath}: field {spec.get('label')!r} needs a (nx, ny, nz[, 3]) grid, an origin and a step "
                       f"(got shape {grid.shape})")
    return grid.astype(np.float64)


def surface_field(spec: dict, file_in, jpath: str, part=None):
    """A field on its own surface mesh as a render Part (vertices split per B-rep face, so edges stay
    sharp; moved by ``deform`` × the vectors), the value per vertex (a vector's length), and deform."""
    import numpy as np

    from .render import Part

    def arr(name, what):
        try:
            return np.load(file_in(str(name)), allow_pickle=False)
        except (OSError, ValueError, TypeError) as e:
            raise CliError(f"{jpath}: field {spec.get('label')!r}: cannot read its {what} {name!r} ({e})") from None

    mesh = spec["mesh"]
    P = arr(mesh.get("vertices"), "vertices").astype(np.float64)
    T = arr(mesh.get("triangles"), "triangles").astype(np.int64)
    F = arr(mesh["faces"], "faces").astype(np.int64) if mesh.get("faces") else np.zeros(len(T), np.int64)
    V = arr(spec.get("file"), "values").astype(np.float64)
    if P.ndim != 2 or P.shape[1] != 3 or T.ndim != 2 or T.shape[1] != 3 or F.shape != (len(T),) or \
            V.shape not in ((len(P),), (len(P), 3)) or (len(T) and (T.min() < 0 or T.max() >= len(P))):
        raise CliError(f"{jpath}: field {spec.get('label')!r}: a surface is vertices (n, 3), triangles (m, 3), faces (m,) "
                       f"and values (n,) or (n, 3); got {P.shape}, {T.shape}, {F.shape}, {V.shape}")
    deform = float(spec.get("deform") or 0) if V.ndim == 2 else 0.0
    if deform:
        P = P + deform * V
    mag = np.linalg.norm(V, axis=1) if V.ndim == 2 else V
    # a vertex per (vertex, face): smooth inside a face, sharp across its edges
    key, inv = np.unique(np.stack([T.reshape(-1), np.repeat(F, 3)], 1), axis=0, return_inverse=True)
    inv = inv.reshape(-1, 3)
    pos = P[key[:, 0]]
    fn = np.cross(pos[inv[:, 1]] - pos[inv[:, 0]], pos[inv[:, 2]] - pos[inv[:, 0]])
    nrm = np.zeros_like(pos)
    for c in range(3):
        np.add.at(nrm, inv[:, c], fn)
    nrm /= np.maximum(np.linalg.norm(nrm, axis=1, keepdims=True), 1e-30)
    out = Part(part.name if part else "field", pos.astype(np.float32), nrm.astype(np.float32), inv, F,
               part.color if part else None, dict(part.tags) if part else {}, part.faces if part else int(F.max(initial=0) + 1))
    return out, mag[key[:, 0]], deform


def refine(part, longest: float, cap: int = 400_000):
    """Splits the triangles of a render Part until no edge is longer than ``longest`` (or the mesh has
    ``cap`` triangles), so a field sampled at the vertices shows as it is on a coarse mesh (a flat face
    is two triangles). Split edges are shared: no cracks."""
    import numpy as np

    P, N, T, F = (part.positions.astype(np.float64), part.normals.astype(np.float64),
                       part.indices.astype(np.int64).reshape(-1, 3), part.face_ids.astype(np.int64))
    while len(T) < cap:
        a, b = T, np.roll(T, -1, axis=1)  # edge k of a triangle: corner k to corner k+1
        long_ = np.linalg.norm(P[a] - P[b], axis=2) > longest
        if not long_.any():
            break
        lo, hi = np.minimum(a, b)[long_], np.maximum(a, b)[long_]
        keys, inv = np.unique(lo * len(P) + hi, return_inverse=True)
        i, j = keys // len(P), keys % len(P)
        mid = np.full(T.shape, -1)
        mid[long_] = len(P) + inv
        nm = N[i] + N[j]
        P = np.vstack([P, (P[i] + P[j]) / 2])
        N = np.vstack([N, nm / np.maximum(np.linalg.norm(nm, axis=1, keepdims=True), 1e-12)])
        out, faces = [], []
        for t, m, f in zip(T, mid, F):
            k = int((m >= 0).sum())
            if k == 0:
                tris = [t]
            elif k == 3:
                tris = [(t[0], m[0], m[2]), (m[0], t[1], m[1]), (m[2], m[1], t[2]), (m[0], m[1], m[2])]
            else:
                r = int(np.argmax(m >= 0)) if k == 1 else int(np.argmin(m >= 0))  # the split (k=1) or kept (k=2) edge
                c = [t[(r + q) % 3] for q in range(3)]
                e = [m[(r + q) % 3] for q in range(3)]
                if k == 1:
                    tris = [(c[0], e[0], c[2]), (e[0], c[1], c[2])]
                else:  # edges 1 and 2 split, edge 0 (c0-c1) kept
                    tris = [(c[0], c[1], e[1]), (c[0], e[1], e[2]), (e[2], e[1], c[2])]
            out += tris
            faces += [f] * len(tris)
        T, F = np.array(out, dtype=np.int64), np.array(faces, dtype=np.int64)
    part.positions, part.normals = P.astype(np.float32), N.astype(np.float32)
    part.indices, part.face_ids = T.astype(np.uint32), F.astype(part.face_ids.dtype)
    return part


def sample_grid(grid, origin, step: float, points):
    """Trilinear interpolation of a node grid at points (clamped to the grid), as the viewer does."""
    import numpy as np

    N = np.array(grid.shape[:3])
    q = np.clip((np.asarray(points, dtype=np.float64) - np.asarray(origin, dtype=np.float64)) / step, 0, N - 1)
    b = np.minimum(np.floor(q).astype(np.int64), np.maximum(N - 2, 0))
    t = q - b
    out = 0
    for c in ((0, 0, 0), (0, 0, 1), (0, 1, 0), (0, 1, 1), (1, 0, 0), (1, 0, 1), (1, 1, 0), (1, 1, 1)):
        w = np.prod([t[:, d] if c[d] else 1 - t[:, d] for d in range(3)], axis=0)
        idx = np.minimum(b + np.array(c), N - 1)
        v = grid[idx[:, 0], idx[:, 1], idx[:, 2]]
        out = out + (w[:, None] * v if v.ndim == 2 else w * v)
    return out


def scene(target: str, frame: int | None = None, field: str | None = None):
    """(parts, overlay or None, caption) for a file or an output folder (its output.json, or inferred);
    a sequence shows its last frame, or ``frame`` (negative counts from the end); ``field`` picks an
    overlay's field by label (default: the first)."""
    import numpy as np

    from .render import Overlay

    if os.path.isfile(target):
        return to_parts(target), None, os.path.basename(target)
    meta = None
    jpath = os.path.join(target, "output.json")
    if os.path.exists(jpath):
        try:
            with open(jpath) as f:
                meta = json.load(f)
        except ValueError as e:
            raise CliError(f"{jpath}: not valid JSON ({e})") from None
    folder = os.path.basename(os.path.normpath(target))
    files = sorted(f for f in os.listdir(target) if f.lower().endswith(GEOMETRY) and not f.startswith("."))
    kind = (meta or {}).get("kind")
    overlay = None

    def file_in(name):
        p = os.path.join(target, name)
        if not os.path.exists(p):
            raise CliError(f"{jpath} names {name!r}, which is not in {target}")
        return p

    if kind == "assembly":
        parts = []
        for e in meta.get("parts", []):
            parts += _placed(to_parts(file_in(e["file"]), e.get("color"), (e.get("name") or "").rsplit("/", 1)[-1] or None),
                             e.get("transform"))
        caption = f"{folder} (assembly, {len(parts)} parts)"
    elif kind == "sequence":
        frames = meta.get("frames") or []
        if not frames:
            raise CliError(f"{jpath}: the sequence has no frames yet")
        # counted from 1, as the viewer's slider (7 / 16); -1 is the last
        n = len(frames)
        i = n - 1 if frame is None else frame - 1 if 1 <= frame <= n else n + frame if -n <= frame <= -1 else None
        if i is None:
            raise CliError(f"{folder} has frames 1 to {n}, not {frame}")
        fr = frames[i]
        if fr.get("parts"):  # a motion frame: parts placed
            parts = [q for e in fr["parts"] for q in _placed(to_parts(file_in(e["file"]), e.get("color"), e.get("name")), e.get("transform"))]
        else:
            parts = to_parts(file_in(fr["file"]))
        vals = ", ".join(f"{k} {v:.4g}" for k, v in (fr.get("values") or {}).items())
        caption = f"{folder}: frame {i + 1} of {len(frames)} ({meta.get('status', 'done')})" + (f", {fr['label']}" if fr.get("label") else "") + (f", {vals}" if vals else "")
    elif kind in ("part", "overlay"):
        name = meta.get("file") or (files[0] if files else None)
        if not name:
            raise CliError(f"{jpath}: no \"file\" and no geometry in {target}")
        parts = to_parts(file_in(name), meta.get("color"))
        caption = f"{folder}/{name}"
        if kind == "overlay":
            overlay = Overlay()
            faces = {k: (v if isinstance(v, dict) else {"label": str(v)}) for k, v in (meta.get("faces") or {}).items()}
            vals = [f["value"] for f in faces.values() if _is_value(f)]
            sc = meta.get("scale") if isinstance(meta.get("scale"), dict) else {}
            lo = float(sc["min"]) if isinstance(sc.get("min"), (int, float)) else min(vals, default=0.0)
            hi = float(sc["max"]) if isinstance(sc.get("max"), (int, float)) else max(vals, default=1.0)
            unit = f" {sc['unit']}" if sc.get("unit") else ""
            for fid, spec in faces.items():
                try:
                    key = (0, int(str(fid).lstrip("f")))
                except ValueError:
                    raise CliError(f"{jpath}: face key {fid!r} is not a face id (use \"12\" for f12)") from None
                if _is_value(spec):
                    t = (spec["value"] - lo) / (hi - lo) if hi > lo else 0.5
                    overlay.faces[key] = {"color": _ramp(t), "label": spec.get("label") or f"{spec['value']:.3g}{unit}"}
                    continue
                overlay.faces[key] = {"color": _overlay_color(spec), "label": spec.get("label") or spec.get("role") or spec.get("severity") or ""}
            for a in meta.get("arrows") or []:
                if "at" not in a or "dir" not in a:
                    raise CliError(f"{jpath}: every arrow needs \"at\" and \"dir\" (got {a})")
                overlay.arrows.append(a)
            caption += f" (overlay, {len(overlay.faces)} faces, {len(overlay.arrows)} arrows)"
            fields = [f for f in meta.get("fields") or [] if isinstance(f, dict)]
            if fields and isinstance(fields[0], dict) and isinstance(pick_field(fields, field, jpath).get("mesh"), dict):
                spec = pick_field(fields, field, jpath)
                surf, mag, deform = surface_field(spec, file_in, jpath, parts[0] if parts else None)
                flo, fhi = float(spec.get("min", mag.min())), float(spec.get("max", mag.max()))
                surf.colors = ramp_array((mag - flo) / (fhi - flo) if fhi > flo else np.full(len(mag), 0.5))
                parts = [surf] + parts[1:]
                overlay.scale = {"label": spec.get("label") or "value", "unit": spec.get("unit") or "",
                                 "min": flo, "max": fhi, "note": spec.get("note") or "", "deform": deform, "colors": RAMP_RGB}
                others = [f.get("label") for f in fields if f is not spec]
                caption += f" · {spec.get('label')}" + (f" (also: {', '.join(others)}; --field)" if others else "")
            elif fields:
                spec = pick_field(fields, field, jpath)
                grid = load_field(file_in(str(spec.get("file"))), spec, jpath)
                deform = float(spec.get("deform") or 0) if grid.ndim == 4 else 0.0
                for p in parts:
                    refine(p, 1.5 * float(spec["step"]))
                    v = sample_grid(grid, spec["origin"], float(spec["step"]), p.positions)
                    mag = np.linalg.norm(v, axis=1) if v.ndim == 2 else v
                    flo, fhi = float(spec.get("min", mag.min())), float(spec.get("max", mag.max()))
                    p.colors = ramp_array((mag - flo) / (fhi - flo) if fhi > flo else np.full(len(mag), 0.5))
                    if deform:
                        p.positions = (p.positions + deform * v).astype(np.float32)
                overlay.scale = {"label": spec.get("label") or "value", "unit": spec.get("unit") or "",
                                 "min": flo, "max": fhi, "note": spec.get("note") or "", "deform": deform, "colors": RAMP_RGB}
                others = [f.get("label") for f in fields if f is not spec]
                caption += f" · {spec.get('label')}" + (f" (also: {', '.join(others)}; --field)" if others else "")
            elif vals:
                overlay.scale = {"label": sc.get("label") or "value", "unit": sc.get("unit") or "",
                                 "min": lo, "max": hi, "colors": RAMP_RGB}
    elif kind is None:
        if not files:
            raise CliError(f"{target}: no STEP or STL files in this output")
        if _numbered(files):
            parts = to_parts(os.path.join(target, files[-1]))
            caption = f"{folder}/{files[-1]} (last of {len(files)} frames)"
        else:
            parts = [p for f in files for p in to_parts(os.path.join(target, f))]
            caption = folder if len(files) > 1 else f"{folder}/{files[0]}"
    else:
        raise CliError(f"{jpath}: unknown kind {kind!r} (part, assembly, sequence, overlay)")
    if meta and meta.get("title"):
        caption = f"{meta['title']}: {caption}"
    return parts, overlay, caption


# ---------------------------------------------------------------- commands


def remote(args, argv: list[str], out: str | None = None):
    """Runs a machine command on project args.project's machine over tule ssh (from your own
    computer); its stdout goes to the file out (a PNG) or to ours."""
    import shlex

    cmd = [sys.executable, "-m", "tule_cli", "ssh", "-p", args.project, "--", shlex.join(argv)]
    if out is None:
        sys.exit(subprocess.call(cmd))
    with open(out, "wb") as f:
        code = subprocess.call(cmd, stdout=f)
    if code:
        os.remove(out)
        sys.exit(code)
    print(f"tule: wrote {out} (from {args.project}'s machine)", file=sys.stderr)


def _pdf_page(args):
    import shutil
    import subprocess
    import tempfile

    if not os.path.isfile(args.file):
        raise CliError(f"no such file: {args.file}")
    if not shutil.which("pdftoppm"):
        raise CliError("rendering a PDF needs pdftoppm (apt-get install poppler-utils)")
    page = args.frame or 1
    with tempfile.TemporaryDirectory() as d:
        r = subprocess.run(["pdftoppm", "-png", "-f", str(page), "-l", str(page), "-scale-to", str(args.size), "-singlefile",
                            args.file, os.path.join(d, "p")], capture_output=True, text=True)
        if r.returncode:
            raise CliError(f"cannot render page {page} of {args.file}: {r.stderr.strip()[-300:]}")
        data = open(os.path.join(d, "p.png"), "rb").read()
    if args.o == "-":
        sys.stdout.buffer.write(data)
        sys.stdout.flush()
        out = "the PNG to stdout"
    else:
        out = os.path.abspath(args.o)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "wb") as f:
            f.write(data)
    print(f"tule: wrote {out}: page {page} of {args.file} (--frame N for another page)", file=sys.stderr)


def cmd_screenshot(args):
    if args.project:  # from your own computer: render on the machine, write the PNG here
        argv = ["tule", "screenshot", *([args.file] if args.file else []), "-o", "-", "--view", args.view, "--size", str(args.size)]
        argv += (["--output", args.output] if args.output else []) + (["--labels"] if args.labels else [])
        argv += ["--frame", str(args.frame)] if args.frame is not None else []
        argv += ["--field", args.field] if args.field else []
        return remote(args, argv, None if args.o == "-" else args.o)
    if args.output and not args.file and os.path.splitext(args.output)[1].lower() in (".pdf", ".png", ".jpg", ".jpeg", ".svg"):
        args.file, args.output = os.path.join(outputs_root(), args.output), None  # --output drawing/drawing.pdf
    if args.file and args.file.lower().endswith(".pdf"):  # a drawing: its page as the sheet looks
        return _pdf_page(args)
    try:
        from .render import VIEWS, render
    except ImportError as e:
        raise CliError(f"tule screenshot needs numpy and Pillow ({e.name} is missing): pip install numpy pillow") from None
    if args.view not in VIEWS:
        raise CliError(f"unknown view {args.view!r}; use one of {', '.join(VIEWS)}")
    target = find_output(args.output, args.file)
    parts, overlay, caption = scene(target, args.frame, args.field)
    import numpy as np

    P = np.concatenate([p.positions for p in parts])
    size = P.max(0) - P.min(0)
    caption += " · size " + " x ".join(_g(v) for v in size)
    img, rep = render(parts, args.view, args.size, args.labels, overlay, caption)
    if args.o == "-":  # to stdout, e.g. to your own computer: tule ssh -p bracket -- tule screenshot -o - > view.png
        out = "the PNG to stdout"
        buf = io.BytesIO()
        img.save(buf, "PNG")
        sys.stdout.buffer.write(buf.getvalue())
        sys.stdout.flush()
    else:
        out = os.path.abspath(args.o)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        tmp = out + ".tmp.png"
        img.save(tmp)
        os.replace(tmp, out)
    lines = [f"tule: wrote {out} ({img.width}×{img.height}, view {args.view}): {target}"]
    if (args.labels or overlay is not None) and not any(p.faces for p in parts):
        lines.append("  no labels: a mesh (STL) has no B-rep faces; write STEP to get face ids")
    elif args.labels or overlay is not None:
        lines.append(f"  labeled: {_ids(rep['labeled'])}")
        if rep["small"]:
            lines.append(f"  visible but too small to label: {_ids(rep['small'])}")
    hidden = rep["hidden"]
    if len(hidden) > 12:  # a count, and the faces that matter by name
        tagged = rep.get("hidden_tagged") or []
        lines.append(f"  {len(hidden)} faces not visible from {args.view}" + (f", tagged: {_ids(tagged)}" if tagged else "")
                     + " (try another --view)")
    elif hidden:
        lines.append(f"  not visible from {args.view}: {_ids(hidden)} (try another --view)")
    print("\n".join(lines), file=sys.stderr if args.o == "-" else sys.stdout)


def _ids(names: list[str]) -> str:
    return ", ".join(names) if names else "none"


def _g(x: float) -> str:
    if 1e4 <= abs(x) < 1e12:
        return f"{x:.0f}"
    s = f"{x:.4g}"
    return "0" if s in ("-0", "0") else s


def _vec(v) -> str:
    return "(" + ", ".join(_g(c) for c in v) + ")"


def _dir(v) -> str:
    for i, a in enumerate("xyz"):
        if abs(abs(v[i]) - 1) < 1e-6:
            return ("+" if v[i] > 0 else "-") + a
    return _vec(v)


def inspect_file(path: str) -> list[dict]:
    import numpy as np

    out = []
    for name, item in load_items(path):
        if isinstance(item, np.ndarray):
            tri = item.astype(np.float64)
            vol = float(np.einsum("ij,ij->i", tri[:, 0], np.cross(tri[:, 1], tri[:, 2])).sum() / 6)
            P = tri.reshape(-1, 3)
            out.append({"name": name, "kind": "mesh", "triangles": len(tri), "bounds": [P.min(0).tolist(), P.max(0).tolist()],
                        "size": (P.max(0) - P.min(0)).tolist(), "volume": abs(vol)})
            continue
        lo, hi = item.bounds
        faces = item.faces.info()
        tag_faces = {}
        for t in item.tags:
            try:
                tag_faces[t] = [f"f{i}" for i in item.faces[t].ids]
            except Exception:
                tag_faces[t] = [f"e{i}" for i in item.edges[t].ids]
        out.append({
            "name": name, "kind": "solid", "color": item.color, "tags": tag_faces,
            "bounds": [list(lo), list(hi)], "size": list(hi - lo),
            "volume": item.volume, "area": item.area, "valid": item.is_valid,
            "solids": len(item.solids), "edges": len(item.edges), "vertices": len(item.vertices),
            "faces": faces,
        })
    return out


def cmd_inspect(args):
    if args.project:
        return remote(args, ["tule", "inspect", *args.file, *(["--json"] if args.json else [])])
    files = []
    for path in args.file:
        if os.path.isdir(path):
            found = [os.path.join(path, f) for f in sorted(os.listdir(path)) if f.lower().endswith(GEOMETRY)]
            if not found:
                raise CliError(f"{path}: no STEP or STL files")
            files += found
        elif os.path.exists(path):
            files.append(path)
        else:
            raise CliError(f"{path}: no such file")
    result = {f: inspect_file(f) for f in files}
    if args.json:
        print(json.dumps(result, indent=1, default=float))
        return
    for f, parts in result.items():
        print(f"{f}: {len(parts)} part{'s' if len(parts) != 1 else ''}")
        for p in parts:
            if p["kind"] == "mesh":
                print(f"  mesh {p['name']!r}: {p['triangles']} triangles, size {' x '.join(_g(v) for v in p['size'])}, "
                      f"bounds {_vec(p['bounds'][0])}..{_vec(p['bounds'][1])}, volume {_g(p['volume'])}")
                continue
            tags = "; ".join(f"{t} {', '.join(ids)}" for t, ids in p["tags"].items()) or "none"
            print(f"  part {p['name']!r} color {p['color'] or 'none'}")
            print(f"    size {' x '.join(_g(v) for v in p['size'])}  bounds {_vec(p['bounds'][0])}..{_vec(p['bounds'][1])}")
            print(f"    volume {_g(p['volume'])}  area {_g(p['area'])}  {p['solids']} solid(s)  {'valid' if p['valid'] else 'INVALID (check the last operation)'}")
            print(f"    {len(p['faces'])} faces, {p['edges']} edges, {p['vertices']} vertices; tags: {tags}")
            for fc in p["faces"]:
                extra = ""
                if "normal" in fc:
                    extra = f"normal {_dir(fc['normal'])}"
                if "radius" in fc:
                    extra = f"r {_g(fc['radius'])}"
                t = f"  [{', '.join(fc['tags'])}]" if fc.get("tags") else ""
                print(f"    f{fc['id']:<3} {fc['type']:<8} area {_g(fc['area']):<8} center {_vec(fc['center']):<22} {extra}{t}")


def skills_dir() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    for d in (os.environ.get("TULE_SKILLS"), "/opt/tule/skills", os.path.join(here, "..", "..", "skills"),
              os.path.join(here, "skills")):  # the last: in the installed CLI (web/build.mjs puts them there)
        if d and os.path.isdir(d):
            return os.path.normpath(d)
    raise CliError("cannot find the agent instructions (skills/); set TULE_SKILLS to the folder")


def topics(d: str) -> list[str]:
    names = [f[:-3] for f in os.listdir(d) if f.endswith(".md")]
    return sorted(names, key=lambda n: (DOC_ORDER.index(n) if n in DOC_ORDER else len(DOC_ORDER), n))


def summary(path: str) -> str:
    """The first line after the title."""
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                return line
    return ""


def _sig(name: str, obj) -> str:
    import inspect

    class Ann(str):  # string annotations (from __future__ import annotations) shown as written
        def __repr__(self):
            return self.strip("'\"")

    if isinstance(obj, property) or not callable(obj):
        return name
    try:
        sig = inspect.signature(obj)
    except (TypeError, ValueError):
        return name
    ann = lambda a: Ann(a) if isinstance(a, str) else a  # noqa: E731
    params = [p.replace(annotation=ann(p.annotation)) for p in sig.parameters.values() if p.name != "self"]
    return name + str(sig.replace(parameters=params, return_annotation=ann(sig.return_annotation)))


def _first(obj) -> str:
    import inspect

    doc = inspect.getdoc(obj.fget if isinstance(obj, property) else obj) or ""
    return doc.split("\n\n")[0].replace("\n", " ")


def _lookup(name: str):
    """The library object a name like ``Shape.fillet``, ``fea.static`` or ``FaceSelection`` means, or None."""
    import importlib
    import inspect
    import sys

    try:
        tule = importlib.import_module("tule")
    except ImportError:
        return None
    path = name.removeprefix("tule.").split(".")
    if path[0].startswith("_"):
        return None
    try:
        obj = importlib.import_module("tule." + path[0])
    except ImportError:
        obj = getattr(tule, path[0], None)
    if obj is None:  # a class the library returns but does not export (FaceSelection, Step)
        obj = next((getattr(m, path[0]) for n, m in sorted(sys.modules.items()) if n.startswith("tule.") and m
                    and inspect.isclass(getattr(m, path[0], None)) and getattr(m, path[0]).__module__ == n), None)
    for part in path[1:]:
        if obj is None:
            break
        obj = inspect.getattr_static(obj, part, None) if inspect.isclass(obj) else getattr(obj, part, None)
    return None if obj is tule else obj


def reference(name: str) -> str | None:
    """Signatures and docs of a library name (``Shape``, ``Shape.fillet``, ``outputs``,
    ``fea.solve``): a function in full, a class or module as a list of its public members."""
    import inspect

    obj = _lookup(name)
    if obj is None:
        return None
    if isinstance(obj, (staticmethod, classmethod)):
        obj = obj.__func__
    title = _sig(name, obj)
    if not (inspect.isclass(obj) or inspect.ismodule(obj)):
        doc = inspect.getdoc(obj.fget if isinstance(obj, property) else obj) or ""
        return f"{title}\n\n{doc}\n" if doc else f"{title}\n"
    lines = [("class " if inspect.isclass(obj) else "") + title, "", inspect.getdoc(obj) or "", ""]
    if inspect.ismodule(obj):
        members = [(n, v) for n, v in vars(obj).items() if not n.startswith("_")
                   and (inspect.isclass(v) or inspect.isfunction(v)) and getattr(v, "__module__", "") == obj.__name__]
    else:
        members = [(n, v) for n, v in vars(obj).items() if not n.startswith("_")
                   and (callable(v) or isinstance(v, (property, staticmethod, classmethod)))]
        for base in obj.__mro__[1:]:
            if base.__module__.startswith("tule"):
                members += [(n, v) for n, v in vars(base).items() if not n.startswith("_") and n not in dict(members)
                            and (callable(v) or isinstance(v, (property, staticmethod, classmethod)))]
    for n, v in members:
        if isinstance(v, (staticmethod, classmethod)):
            v = v.__func__
        lines.append(f"  {_sig(n, v)}")
        if _first(v):
            lines.append(f"      {_first(v)}")
    return "\n".join(lines).rstrip() + "\n"


def _close_names(name: str) -> list[str]:
    """Library names like ``name`` (FaceSelection: Faces, Selection)."""
    import difflib
    import inspect
    import sys

    if _lookup("Shape") is None:
        return []
    have = {n for m, mod in list(sys.modules.items()) if m.startswith("tule.") and mod
            for n, v in vars(mod).items() if not n.startswith("_") and (inspect.isclass(v) or inspect.isfunction(v))
            and getattr(v, "__module__", "") == m}
    low = {n.lower(): n for n in have}
    parts = [p for p in _camel_parts(name) if len(p) > 3]
    hits = [low[p.lower()] for p in parts if p.lower() in low]
    return list(dict.fromkeys(hits + difflib.get_close_matches(name, sorted(have), n=3, cutoff=0.6)))[:4]


def _camel_parts(name: str) -> list[str]:
    import re

    return re.findall(r"[A-Z][a-z]+s?|[a-z]+", name) + [w + "s" for w in re.findall(r"[A-Z][a-z]+", name)]


def _no_member(owner: str, name: str) -> str:
    import difflib

    obj = _lookup(owner)
    have = [n for n in dir(obj) if not n.startswith("_")
            and (getattr(getattr(obj, n), "__module__", None) or "tule").startswith("tule")]
    close = difflib.get_close_matches(name, have, n=3, cutoff=0.6)
    return (f"{owner} has no {name!r}" + (f"; close: {', '.join(close)}" if close else "")
            + f" (tule docs {owner} lists what it has)")


def cmd_docs(args):
    d = skills_dir()
    names = topics(d)
    if not args.topic:
        print("tule docs <topic>:")
        for n in names:
            print(f"  {n:<13} {summary(os.path.join(d, n + '.md'))}")
        print("or a library name for its signatures and docs: tule docs Shape, tule docs outputs.sequence, tule docs tule.fea")
        return
    if args.topic not in names:
        ref = reference(args.topic)
        if ref is None and "." in args.topic and _lookup(args.topic.rsplit(".", 1)[0]) is not None:
            raise CliError(_no_member(*args.topic.rsplit(".", 1)))
        if ref is None:
            close = _close_names(args.topic.split(".")[0])
            raise CliError(f"no topic {args.topic!r}" + (f"; library names close to it: {', '.join(close)}" if close else "")
                           + f"; topics: {', '.join(names)}, or a library name (tule docs Shape.fillet)")
        sys.stdout.write(ref)
        return
    with open(os.path.join(d, args.topic + ".md")) as f:
        sys.stdout.write(f.read())


def local_request(msg: dict, timeout: float = 900) -> dict:
    """One request to the tule-agent on this machine (line-delimited JSON over its Unix socket)."""
    if not os.path.exists(SOCKET):
        raise CliError(f"no tule-agent socket at {SOCKET}: is tule-agent running? (it is the machine's entrypoint)")
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(SOCKET)
        s.sendall(json.dumps(msg).encode() + b"\n")
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    except OSError as e:
        raise CliError(f"tule-agent at {SOCKET}: {e}") from None
    finally:
        s.close()
    try:
        r = json.loads(buf)
    except ValueError:
        raise CliError(f"tule-agent at {SOCKET} sent no answer") from None
    if r.get("error"):
        raise CliError(f"tule-agent: {r['error']}")
    return r


def snapshot_here(reason: str = "manual"):
    r = local_request({"op": "snapshot", "reason": reason})
    print(f"tule: snapshot {r['snapshot']}" + ("" if r.get("changed", True) else " (nothing changed since)"))


def add_commands(add):
    """Registers the machine commands with the CLI's `add(name, fn, help, project)`."""
    sp = add("screenshot", cmd_screenshot, "render an output or a STEP/STL file to PNG (the agent's eyes)", project=False)
    sp.add_argument("file", nargs="?", help="a STEP/STL file, an output folder (default: the newest output), or a PDF drawing")
    sp.add_argument("-o", default="view.png", help="the PNG to write, or - for stdout (default: view.png)")
    sp.add_argument("--output", help="an output's name in the outputs folder")
    sp.add_argument("--view", default="iso", help="iso (default), front, back, left, right, top, bottom")
    sp.add_argument("--labels", action="store_true", help="label visible faces with their ids and tags (f12 · load)")
    sp.add_argument("--size", type=int, default=1200, help="image width in pixels (default 1200)")
    sp.add_argument("--frame", type=int, help="a sequence's frame, from 1 as in the viewer (default: the last; -2 the one before); a PDF's page")
    sp.add_argument("--page", type=int, dest="frame", help="a PDF's page, from 1 (a datasheet's drawing)")
    sp.add_argument("-p", "--project", help="from your own computer: render on this project's machine, write the PNG here")
    sp.add_argument("--field", help="an overlay field to color by, by label (default: the first; e.g. displacement)")
    sp = add("inspect", cmd_inspect, "parts, bounds, volume, faces with ids, types, areas and tags", project=False)
    sp.add_argument("file", nargs="+", help="STEP/STL files or output folders")
    sp.add_argument("--json", action="store_true", help="print JSON instead of text")
    sp.add_argument("-p", "--project", help="from your own computer: inspect files on this project's machine")
    sp = add("docs", cmd_docs, "the agent instructions: tule docs lists topics, tule docs <topic> prints one", project=False)
    sp.add_argument("topic", nargs="?")
