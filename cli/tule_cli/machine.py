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
import os
import re
import socket
import struct
import sys

GEOMETRY = (".step", ".stp", ".stl")
SOCKET = os.environ.get("TULE_AGENT_SOCKET", "/run/tule/agent.sock")
DOC_ORDER = ["index", "setup", "loop", "modeling", "outputs", "occt", "annotations", "dfm", "topopt", "golden-paths"]


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
        if not os.path.exists(path):
            raise CliError(f"{path}: no such file or folder")
        return path
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
        raise CliError(f"{path}: unsupported file type {ext or '(none)'}; use STEP or STL")
    _need_tule()
    from tule import Assembly, import_step

    item = import_step(path)
    if isinstance(item, Assembly):
        return [(n.rsplit("/", 1)[-1], s) for n, s in item.parts()]
    return [(item.name or base, item)]


def to_parts(path: str, color: str | None = None, name: str | None = None):
    import numpy as np

    from .render import Part

    out = []
    for nm, item in load_items(path):
        if isinstance(item, np.ndarray):  # STL
            out.append(Part(name or nm, item.reshape(-1, 3), smooth_normals(item),
                            np.arange(3 * len(item)).reshape(-1, 3), np.zeros(len(item), np.int64),
                            _rgb(color), {}, 0))
            continue
        from tule import tessellate

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


def _overlay_color(spec: dict) -> tuple:
    if spec.get("color"):
        return _rgb(spec["color"]) or PALETTE[0]
    if spec.get("severity") in SEVERITY:
        return SEVERITY[spec["severity"]]
    role = spec.get("role")
    if role in ROLES:
        return ROLES[role]
    return PALETTE[sum(map(ord, str(role or ""))) % len(PALETTE)]


def scene(target: str):
    """(parts, overlay or None, caption) for a file or an output folder (its output.json, or inferred)."""
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
            parts += to_parts(file_in(e["file"]), e.get("color"), (e.get("name") or "").rsplit("/", 1)[-1] or None)
        caption = f"{folder} (assembly, {len(parts)} parts)"
    elif kind == "sequence":
        frames = meta.get("frames") or []
        if not frames:
            raise CliError(f"{jpath}: the sequence has no frames yet")
        fr = frames[-1]
        parts = to_parts(file_in(fr["file"]))
        vals = ", ".join(f"{k} {v:.4g}" for k, v in (fr.get("values") or {}).items())
        caption = f"{folder}: frame {len(frames) - 1} of {len(frames)} ({meta.get('status', 'done')})" + (f", {vals}" if vals else "")
    elif kind in ("part", "overlay"):
        name = meta.get("file") or (files[0] if files else None)
        if not name:
            raise CliError(f"{jpath}: no \"file\" and no geometry in {target}")
        parts = to_parts(file_in(name), meta.get("color"))
        caption = f"{folder}/{name}"
        if kind == "overlay":
            overlay = Overlay()
            for fid, spec in (meta.get("faces") or {}).items():
                spec = spec if isinstance(spec, dict) else {"label": str(spec)}
                try:
                    key = (0, int(str(fid).lstrip("f")))
                except ValueError:
                    raise CliError(f"{jpath}: face key {fid!r} is not a face id (use \"12\" for f12)") from None
                overlay.faces[key] = {"color": _overlay_color(spec), "label": spec.get("label") or spec.get("role") or spec.get("severity") or ""}
            for a in meta.get("arrows") or []:
                if "at" not in a or "dir" not in a:
                    raise CliError(f"{jpath}: every arrow needs \"at\" and \"dir\" (got {a})")
                overlay.arrows.append(a)
            caption += f" (overlay, {len(overlay.faces)} faces, {len(overlay.arrows)} arrows)"
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


def cmd_screenshot(args):
    try:
        from .render import VIEWS, render
    except ImportError as e:
        raise CliError(f"tule screenshot needs numpy and Pillow ({e.name} is missing): pip install numpy pillow") from None
    if args.view not in VIEWS:
        raise CliError(f"unknown view {args.view!r}; use one of {', '.join(VIEWS)}")
    target = find_output(args.output, args.file)
    parts, overlay, caption = scene(target)
    import numpy as np

    P = np.concatenate([p.positions for p in parts])
    size = P.max(0) - P.min(0)
    caption += " · size " + " x ".join(_g(v) for v in size)
    img, rep = render(parts, args.view, args.size, args.labels, overlay, caption)
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
    if rep["hidden"]:
        lines.append(f"  not visible from {args.view}: {_ids(rep['hidden'])} (try another --view)")
    print("\n".join(lines))


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
    path = args.file
    if os.path.isdir(path):
        files = [os.path.join(path, f) for f in sorted(os.listdir(path)) if f.lower().endswith(GEOMETRY)]
        if not files:
            raise CliError(f"{path}: no STEP or STL files")
    elif os.path.exists(path):
        files = [path]
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
    for d in (os.environ.get("TULE_SKILLS"), "/opt/tule/skills", os.path.join(here, "..", "..", "skills")):
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


def cmd_docs(args):
    d = skills_dir()
    names = topics(d)
    if not args.topic:
        print("tule docs <topic>:")
        for n in names:
            print(f"  {n:<13} {summary(os.path.join(d, n + '.md'))}")
        return
    if args.topic not in names:
        raise CliError(f"no topic {args.topic!r}; topics: {', '.join(names)}")
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
    sp.add_argument("file", nargs="?", help="a STEP/STL file or an output folder (default: the newest output)")
    sp.add_argument("-o", default="view.png", help="the PNG to write (default: view.png)")
    sp.add_argument("--output", help="an output's name in the outputs folder")
    sp.add_argument("--view", default="iso", help="iso (default), front, back, left, right, top, bottom")
    sp.add_argument("--labels", action="store_true", help="label visible faces with their ids and tags (f12 · load)")
    sp.add_argument("--size", type=int, default=1200, help="image width in pixels (default 1200)")
    sp = add("inspect", cmd_inspect, "parts, bounds, volume, faces with ids, types, areas and tags", project=False)
    sp.add_argument("file", help="a STEP/STL file or an output folder")
    sp.add_argument("--json", action="store_true", help="print JSON instead of text")
    sp = add("docs", cmd_docs, "the agent instructions: tule docs lists topics, tule docs <topic> prints one", project=False)
    sp.add_argument("topic", nargs="?")
