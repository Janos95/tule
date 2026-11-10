"""Annotations from the viewer: what the human drew and picked, for the AI agent to read.

The page sends {t: "req", op: "annotate", args} with a PNG of the view (the
strokes composited), the strokes in screen pixels with what each point hit,
labeled face picks, the camera, the output shown and a note. This writes

    /workspace/annotations/<YYYYMMDD-HHMMSS>/annotation.png
    /workspace/annotations/<YYYYMMDD-HHMMSS>/annotation.json

and broadcasts {event: "annotation", id, path, summary} to every viewer.
annotation.json:

    {"version": 1, "id": "20261101-101233", "created": "2026-11-01T10:12:33Z",
     "summary": "on bracket: stroke 1 (red) over bracket f14 (tags: load); picks: 'fixed' = bracket f16, f25",
     "image": "annotation.png",
     "output": {"name", "kind", "title", "files", "frame", "snapshot"},
     "camera": {"position": [x,y,z], "target": [x,y,z], "up": [x,y,z], "fov": deg},
     "viewport": {"width": css px, "height": css px},
     "strokes": [{"color": "red", "width": px, "points": [[x, y], ...],
                  "hits": [{"part", "face", "point": [x,y,z]} | null per point],
                  "faces": [{"part", "face", "tags"}]}],
     "picks": [{"label": "fixed", "faces": [{"part", "face", "tags"}]}],
     "note": "..."}

Screen points are CSS pixels from the viewport's top left; face ids are the
B-rep face ids the viewer shows (f14), the same as shape.faces ids.

    annotations {}  -> {annotations: [{id, path, created, summary, note, output}]}, newest first
"""

from __future__ import annotations

import base64
import json
import os
import time

DIR = "annotations"
MAX_PNG = 20 << 20


def _faces(faces) -> str:
    """'bracket f13, f25 (tags: load)' for a list of {part, face, tags}."""
    out, last, tags = [], None, []
    for f in faces or []:
        part = f.get("part") or "part"
        out.append(f"{part} f{f.get('face')}" if part != last else f"f{f.get('face')}")
        last = part
        tags += [t for t in f.get("tags") or [] if t not in tags]
    return ", ".join(out) + (f" (tags: {', '.join(tags)})" if tags else "")


def summary(a: dict) -> str:
    bits = []
    for i, s in enumerate(a.get("strokes") or [], 1):
        where = f"over {_faces(s['faces'])}" if s.get("faces") else "off the model"
        bits.append(f"stroke {i} ({s.get('color', 'ink')}) {where}")
    picks = [f"'{p.get('label') or 'unlabeled'}' = {_faces(p.get('faces'))}" for p in a.get("picks") or []]
    if picks:
        bits.append("picks: " + ", ".join(picks))
    if (a.get("note") or "").strip():
        bits.append(f"note: {a['note'].strip()!r}")
    o = a.get("output") or {}
    head = f"on {o['name']}" + (f" (snapshot {o['snapshot']})" if o.get("snapshot") else "") + ": " if o.get("name") else ""
    return head + ("; ".join(bits) if bits else "a view without marks")


def annotate(agent, args: dict) -> dict:
    root = os.path.join(agent.workspace.root, DIR)
    os.makedirs(root, exist_ok=True)
    now = time.gmtime()
    sid = time.strftime("%Y%m%d-%H%M%S", now)
    for n in range(2, 100):
        if not os.path.exists(os.path.join(root, sid)):
            break
        sid = time.strftime("%Y%m%d-%H%M%S", now) + f"-{n}"
    png = args.get("png") or ""
    if png.startswith("data:"):
        png = png.split(",", 1)[-1]
    data = base64.b64decode(png) if png else b""
    if len(data) > MAX_PNG:
        raise ValueError(f"the picture is {len(data) >> 20} MiB; at most {MAX_PNG >> 20} MiB")
    folder = os.path.join(root, sid)  # made once the picture is known to be good: no empty folder left
    os.makedirs(folder)
    if data:
        _write(os.path.join(folder, "annotation.png"), data)

    keep = ("output", "camera", "viewport", "strokes", "picks", "note")
    record = {"version": 1, "id": sid, "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", now)}
    record["summary"] = summary(args)
    record["image"] = "annotation.png" if data else None
    record.update({k: args.get(k) for k in keep if k in args})
    # one line per key: the summary reads at a glance, the strokes stay compact
    text = "{\n" + ",\n".join(f" {json.dumps(k)}: {json.dumps(v)}" for k, v in record.items()) + "\n}\n"
    _write(os.path.join(folder, "annotation.json"), text.encode())
    path = f"{DIR}/{sid}"
    event = {"event": "annotation", "id": sid, "path": path, "summary": record["summary"]}
    return {"id": sid, "path": path, "summary": record["summary"], "broadcast": event}


def annotations(agent, args: dict) -> dict:
    root = os.path.join(agent.workspace.root, DIR)
    out = []
    try:
        names = sorted(os.listdir(root), reverse=True)
    except FileNotFoundError:
        names = []
    for name in names[: int(args.get("limit") or 50)]:
        try:
            with open(os.path.join(root, name, "annotation.json")) as f:
                a = json.load(f)
        except (OSError, ValueError):
            continue
        out.append({"id": name, "path": f"{DIR}/{name}", "created": a.get("created"), "summary": a.get("summary", ""),
                    "note": a.get("note", ""), "output": a.get("output"), "image": a.get("image")})
    return {"annotations": out}


def _write(path: str, data: bytes):
    tmp = path + ".tule-tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


REQUESTS = {"annotate": annotate, "annotations": annotations}
