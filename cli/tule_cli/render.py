"""The software renderer behind `tule screenshot`: a numpy z-buffer, no GPU or display needed.

It draws triangle meshes with smooth shading from the surface normals,
B-rep face boundaries (the CAD edges) and silhouettes as dark lines, and
optionally a label per visible face (``f12 · load``) placed inside the face,
or next to it with a leader line when it is small. Overlays color chosen
faces and draw arrows. Clarity first: an agent reads these images to check
its work.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
from PIL import Image, ImageDraw, ImageFont

# camera directions (from the target), as in the viewer (web/src/viewer.js): z is up, "front" looks along +y
VIEWS = {
    "iso": (0.62, -1.0, 0.68),
    "front": (0, -1, 0),
    "back": (0, 1, 0),
    "left": (-1, 0, 0),
    "right": (1, 0, 0),
    "top": (0, -1e-3, 1),
    "bottom": (0, -1e-3, -1),
}

DEFAULT_COLOR = (0.62, 0.68, 0.75)
DIM_COLOR = (0.80, 0.81, 0.83)  # faces an overlay does not mention
BACKGROUND = (0.965, 0.97, 0.975)
LINE = (0.10, 0.12, 0.14)
SS = 2  # supersampling


@dataclass
class Part:
    """A mesh to draw: positions/normals (n, 3), indices (m, 3), a B-rep face id per triangle."""

    name: str
    positions: np.ndarray
    normals: np.ndarray
    indices: np.ndarray
    face_ids: np.ndarray
    color: tuple | None = None
    tags: dict = field(default_factory=dict)  # face id -> [tag, ...]
    faces: int = 0  # B-rep faces in the part (0: a mesh without faces, e.g. STL)


@dataclass
class Overlay:
    faces: dict = field(default_factory=dict)  # (part index, face id) -> {"color": rgb, "label": str}
    arrows: list = field(default_factory=list)  # {"at": xyz, "dir": xyz, "label": str}


def _unit(v) -> np.ndarray:
    v = np.asarray(v, dtype=np.float64)
    return v / np.linalg.norm(v)


def best_iso(parts: list[Part]) -> tuple:
    """The 3/4 view (from above, one of four corners) that sees the most faces: a part's
    features rather than its flat back. The usual corner unless another clearly sees more."""
    x, y, z = VIEWS["iso"]
    corners = [(x, y, z), (-x, y, z), (x, -y, z), (-x, -y, z)]
    sums: dict = {}
    for i, p in enumerate(parts):
        v = p.positions[p.indices.astype(np.int64)].astype(np.float64)
        n = np.cross(v[:, 1] - v[:, 0], v[:, 2] - v[:, 0])  # area-weighted normals
        ids = p.face_ids.astype(np.int64) if len(p.face_ids) == len(n) else np.arange(len(n))
        u, inv = np.unique(ids, return_inverse=True)
        acc = np.zeros((len(u), 4))
        np.add.at(acc, inv, np.c_[n, np.linalg.norm(n, axis=1)])
        sums[i] = acc
    acc = np.concatenate(list(sums.values())) if sums else np.zeros((0, 4))
    size = np.linalg.norm(acc[:, :3], axis=1)
    acc = acc[size > 0.5 * acc[:, 3]]  # faces that point one way (not a hole's wall)
    mean = acc[:, :3] / np.linalg.norm(acc[:, :3], axis=1, keepdims=True)
    seen = [int((mean @ _unit(c) > 0.2).sum()) for c in corners]
    best = max(range(4), key=lambda k: seen[k])
    return corners[best] if seen[best] >= max(seen[0] + 2, 1.2 * seen[0]) else corners[0]


def camera(view, direction=None) -> np.ndarray:
    """Rows: screen right, screen up, toward the viewer."""
    d = _unit(direction if direction is not None else VIEWS[view])
    fwd = -d
    right = _unit(np.cross(fwd, (0, 0, 1)))
    up = np.cross(right, fwd)
    return np.stack([right, up, d])


# ---------------------------------------------------------------- rasterizer


def rasterize(X, Y, Z, tris, W, H, limit=3_000_000):
    """Z-buffer: per pixel the nearest triangle (-1: none) and barycentrics of its first two corners."""
    zbuf = np.full(W * H, -np.inf)
    tbuf = np.full(W * H, -1, dtype=np.int64)
    b0buf = np.zeros(W * H)
    b1buf = np.zeros(W * H)
    x, y, z = X[tris], Y[tris], Z[tris]  # (m, 3)
    d = (y[:, 1] - y[:, 2]) * (x[:, 0] - x[:, 2]) + (x[:, 2] - x[:, 1]) * (y[:, 0] - y[:, 2])
    x0 = np.clip(np.ceil(x.min(1) - 0.5), 0, W - 1).astype(np.int64)
    x1 = np.clip(np.floor(x.max(1) - 0.5), 0, W - 1).astype(np.int64)
    y0 = np.clip(np.ceil(y.min(1) - 0.5), 0, H - 1).astype(np.int64)
    y1 = np.clip(np.floor(y.max(1) - 0.5), 0, H - 1).astype(np.int64)
    onscreen = (x.max(1) >= 0) & (x.min(1) <= W) & (y.max(1) >= 0) & (y.min(1) <= H)
    ok = (np.abs(d) > 1e-12) & (x1 >= x0) & (y1 >= y0) & onscreen
    ids = np.nonzero(ok)[0]
    if not len(ids):
        return zbuf, tbuf, b0buf, b1buf
    d = d[ids]
    a0 = (y[ids, 1] - y[ids, 2]) / d
    c0 = (x[ids, 2] - x[ids, 1]) / d
    a1 = (y[ids, 2] - y[ids, 0]) / d
    c1 = (x[ids, 0] - x[ids, 2]) / d
    e0 = -(a0 * x[ids, 2] + c0 * y[ids, 2])
    e1 = -(a1 * x[ids, 2] + c1 * y[ids, 2])
    zz = z[ids]
    w = x1[ids] - x0[ids] + 1
    counts = w * (y1[ids] - y0[ids] + 1)
    ends = np.cumsum(counts)
    start = 0
    while start < len(ids):
        base = ends[start - 1] if start else 0
        stop = max(start + 1, int(np.searchsorted(ends, base + limit, side="right")))
        sl = slice(start, stop)
        n = counts[sl]
        local = np.repeat(np.arange(stop - start), n)
        offs = np.arange(int(n.sum())) - np.repeat(np.cumsum(n) - n, n)
        t = local + start
        wl = w[t]
        px = x0[ids[t]] + offs % wl
        py = y0[ids[t]] + offs // wl
        fx, fy = px + 0.5, py + 0.5
        w0 = a0[t] * fx + c0[t] * fy + e0[t]
        w1 = a1[t] * fx + c1[t] * fy + e1[t]
        w2 = 1.0 - w0 - w1
        inside = (w0 >= -1e-7) & (w1 >= -1e-7) & (w2 >= -1e-7)
        t, w0, w1, w2 = t[inside], w0[inside], w1[inside], w2[inside]
        pix = (py * W + px)[inside]
        depth = w0 * zz[t, 0] + w1 * zz[t, 1] + w2 * zz[t, 2]
        order = np.lexsort((-depth, pix))
        pix, depth, t, w0, w1 = pix[order], depth[order], t[order], w0[order], w1[order]
        first = np.ones(len(pix), dtype=bool)
        first[1:] = pix[1:] != pix[:-1]
        pix, depth, t, w0, w1 = pix[first], depth[first], t[first], w0[first], w1[first]
        nearer = depth > zbuf[pix]
        p = pix[nearer]
        zbuf[p] = depth[nearer]
        tbuf[p] = ids[t[nearer]]
        b0buf[p] = w0[nearer]
        b1buf[p] = w1[nearer]
        start = stop
    return zbuf, tbuf, b0buf, b1buf


def _dilate(m: np.ndarray) -> np.ndarray:
    out = m.copy()
    out[1:] |= m[:-1]
    out[:-1] |= m[1:]
    out[:, 1:] |= m[:, :-1]
    out[:, :-1] |= m[:, 1:]
    return out


def _depth_inside(key: np.ndarray, max_d: int = 48) -> np.ndarray:
    """Per pixel: how many pixels inside its face region (4-neighbourhood erosion steps, capped)."""
    alive = key >= 0
    d = np.zeros(key.shape, dtype=np.int32)
    for k in range(1, max_d + 1):
        nxt = alive.copy()
        nxt[1:] &= alive[:-1] & (key[1:] == key[:-1])
        nxt[:-1] &= alive[1:] & (key[:-1] == key[1:])
        nxt[:, 1:] &= alive[:, :-1] & (key[:, 1:] == key[:, :-1])
        nxt[:, :-1] &= alive[:, 1:] & (key[:, :-1] == key[:, 1:])
        nxt[0, :] = nxt[-1, :] = False
        nxt[:, 0] = nxt[:, -1] = False
        if not nxt.any():
            break
        d[nxt] = k
        alive = nxt
    return d


def _font(size: int):
    try:  # DejaVu (on Tule machines) has the punctuation captions use (•, ×, °, ⌀); Pillow's default lacks some
        return ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", size)
    except OSError:
        pass
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1
        return ImageFont.load_default()


# ---------------------------------------------------------------- the picture


def render(parts: list[Part], view: str = "iso", size: int = 1200, labels: bool = False,
           overlay: Overlay | None = None, caption: str = "", plain: bool = False) -> tuple[Image.Image, dict]:
    """Returns the image and a report: {"labeled": [...], "small": [...], "hidden": [...]} (face names).
    plain: for documents (white, no axes, no footer but the caption)."""
    if view not in VIEWS:
        raise ValueError(f"unknown view {view!r}; use one of {', '.join(VIEWS)}")
    parts = [p for p in parts if len(p.indices)]
    if not parts:
        raise ValueError("nothing to draw: the file has no triangles")
    R = camera(view, best_iso(parts) if view == "iso" else None)
    offs = np.cumsum([0] + [len(p.positions) for p in parts])
    P = np.concatenate([p.positions for p in parts]).astype(np.float64)
    Nw = np.concatenate([p.normals for p in parts]).astype(np.float64)
    T = np.concatenate([p.indices.astype(np.int64) + o for p, o in zip(parts, offs)])
    tri_part = np.concatenate([np.full(len(p.indices), i) for i, p in enumerate(parts)])
    tri_face = np.concatenate([p.face_ids.astype(np.int64) for p in parts])
    lo_w, hi_w = P.min(0), P.max(0)
    extent = float(np.linalg.norm(hi_w - lo_w)) or 1.0

    arrows = []
    for a in (overlay.arrows if overlay else []):
        at = np.asarray(a["at"], dtype=np.float64)
        tail = at - _unit(a["dir"]) * 0.25 * extent
        arrows.append((at, tail, a.get("label") or ""))

    S = P @ R.T  # screen right, up, depth
    pts = np.concatenate([S[:, :2]] + [np.stack([at @ R[:2].T, tail @ R[:2].T]) for at, tail, _ in arrows])
    lo, hi = pts.min(0), pts.max(0)
    span = np.maximum(hi - lo, 1e-9 * extent)
    W = int(size)
    margin = 0.07 * W
    scale = (W - 2 * margin) / span[0]
    H = span[1] * scale + 2 * margin
    if H > W:
        H = W
        scale = (W - 2 * margin) / span[1]
    H = int(round(max(H, 0.6 * W)))
    c = (lo + hi) / 2

    Ws, Hs = W * SS, H * SS
    X = (S[:, 0] - c[0]) * scale * SS + Ws / 2
    Y = Hs / 2 - (S[:, 1] - c[1]) * scale * SS
    zbuf, tbuf, b0, b1 = rasterize(X, Y, S[:, 2], T, Ws, Hs)
    fg = tbuf >= 0
    t = tbuf[fg]

    # colors per triangle
    base = np.empty((len(parts), 3))
    for i, p in enumerate(parts):
        base[i] = p.color or DEFAULT_COLOR
    tri_color = base[tri_part]
    if overlay is not None:
        tri_color[:] = DIM_COLOR
        for (pi, fid), spec in overlay.faces.items():
            tri_color[(tri_part == pi) & (tri_face == fid)] = spec["color"]

    # smooth shading from interpolated surface normals, lit in camera space
    w0, w1 = b0[fg], b1[fg]
    i0, i1, i2 = T[t, 0], T[t, 1], T[t, 2]
    n = w0[:, None] * Nw[i0] + w1[:, None] * Nw[i1] + (1 - w0 - w1)[:, None] * Nw[i2]
    n = n @ R.T
    n /= np.linalg.norm(n, axis=1, keepdims=True) + 1e-12
    n[n[:, 2] < 0] *= -1  # two-sided
    key = _unit((-0.45, 0.65, 0.62))
    fill = _unit((0.7, -0.25, 0.45))
    half = _unit(key + np.array([0, 0, 1.0]))
    shade = 0.30 + 0.62 * np.clip(n @ key, 0, 1) + 0.20 * np.clip(n @ fill, 0, 1)
    spec = 0.10 * np.clip(n @ half, 0, 1) ** 40
    img = np.empty((Ws * Hs, 3))
    img[:] = (1.0, 1.0, 1.0) if plain else BACKGROUND
    img[fg] = np.clip(tri_color[t] * shade[:, None] + spec[:, None], 0, 1)

    # lines: face boundaries (CAD edges), silhouettes, depth jumps; creases on meshes without faces
    fkey = np.full(Ws * Hs, -1, dtype=np.int64)
    fkey[fg] = tri_part[t] * (1 << 32) + tri_face[t]
    K = fkey.reshape(Hs, Ws)
    Zm = np.where(fg, zbuf, np.nan).reshape(Hs, Ws)
    jump = 0.02 * extent
    line = np.zeros((Hs, Ws), dtype=bool)
    for a, b in (((slice(None), slice(1, None)), (slice(None), slice(None, -1))),
                 ((slice(1, None), slice(None)), (slice(None, -1), slice(None)))):
        diff = K[a] != K[b]
        with np.errstate(invalid="ignore"):
            diff |= np.abs(Zm[a] - Zm[b]) > jump
        line[a] |= diff
        line[b] |= diff
    meshy = np.array([p.faces == 0 for p in parts])
    if meshy.any():
        nimg = np.zeros((Ws * Hs, 3))
        nimg[fg] = n
        nimg = nimg.reshape(Hs, Ws, 3)
        mk = np.zeros(Ws * Hs, dtype=bool)
        mk[fg] = meshy[tri_part[t]]
        mk = mk.reshape(Hs, Ws)
        for a, b in (((slice(None), slice(1, None)), (slice(None), slice(None, -1))),
                     ((slice(1, None), slice(None)), (slice(None, -1), slice(None)))):
            crease = mk[a] & mk[b] & ((nimg[a] * nimg[b]).sum(-1) < math.cos(math.radians(35)))
            line[a] |= crease
            line[b] |= crease
    if SS > 1:
        line = _dilate(line)
    img = img.reshape(Hs, Ws, 3)
    img[line] = LINE

    big = Image.fromarray((img * 255 + 0.5).astype(np.uint8))
    # arrows, drawn on top so they are never hidden
    if arrows:
        dr = ImageDraw.Draw(big)
        for at, tail, _ in arrows:
            a2 = np.array([(at @ R[0] - c[0]) * scale * SS + Ws / 2, Hs / 2 - (at @ R[1] - c[1]) * scale * SS])
            t2 = np.array([(tail @ R[0] - c[0]) * scale * SS + Ws / 2, Hs / 2 - (tail @ R[1] - c[1]) * scale * SS])
            v = a2 - t2
            L = np.linalg.norm(v)
            if L < 1:
                dr.ellipse([*(a2 - 8 * SS), *(a2 + 8 * SS)], outline=(200, 40, 20), width=3 * SS)
                continue
            u = v / L
            perp = np.array([-u[1], u[0]])
            head = min(18 * SS, 0.4 * L)
            neck = a2 - u * head
            dr.line([tuple(t2), tuple(neck)], fill=(200, 40, 20), width=4 * SS)
            dr.polygon([tuple(a2), tuple(neck + perp * head * 0.45), tuple(neck - perp * head * 0.45)], fill=(200, 40, 20))
    im = big.resize((W, H), Image.LANCZOS) if SS > 1 else big

    report = {"labeled": [], "small": [], "hidden": []}
    names = {}
    multi = len(parts) > 1
    for i, p in enumerate(parts):
        for f in range(max(p.faces, 0)):
            names[(i, f)] = (f"{p.name}:" if multi else "") + f"f{f}"
    Kf = K[SS // 2::SS, SS // 2::SS][:H, :W]
    keys, counts = np.unique(Kf[Kf >= 0], return_counts=True)
    visible = {(int(k >> 32), int(k & 0xFFFFFFFF)): int(cnt) for k, cnt in zip(keys, counts)}
    report["hidden_tagged"] = []
    marked = set(overlay.faces) if overlay is not None else set()
    for k in names:
        if k not in visible:
            report["hidden"].append(names[k])
            tags = (parts[k[0]].tags or {}).get(k[1])
            if tags or k in marked:
                report["hidden_tagged"].append(names[k] + (" " + ",".join(tags) if tags else ""))

    draw = ImageDraw.Draw(im)
    fs = max(11, round(W / 85))
    font = _font(fs)
    boxes: list[tuple] = []
    if labels or overlay is not None:
        boxes = _labels(im, draw, font, parts, Kf, visible, names, overlay, labels, report)
    for at, tail, text in arrows:
        if text:
            p = ((tail @ R[0] - c[0]) * scale + W / 2, H / 2 - (tail @ R[1] - c[1]) * scale)
            _tag_box(draw, font, text, p, boxes, W, H, fill=(255, 236, 230), outline=(200, 40, 20))
    if plain:
        if not caption:
            return im, report
        out = Image.new("RGB", (W, H + fs + 14), (255, 255, 255))
        out.paste(im, (0, 0))
        ImageDraw.Draw(out).text((8, H + 6), caption, fill=(40, 44, 50), font=font)
        return out, report
    _triad(draw, R, font, H)

    out = Image.new("RGB", (W, H + fs + 14), (232, 234, 237))
    out.paste(im, (0, 0))
    ImageDraw.Draw(out).text((8, H + 6), f"{caption} · view {view}" if caption else f"view {view}", fill=(40, 44, 50), font=font)
    return out, report


def _labels(im, draw, font, parts, Kf, visible, names, overlay, labels, report) -> list:
    H, W = Kf.shape
    inside = _depth_inside(Kf)
    flat_k = Kf.reshape(-1)
    flat_d = inside.reshape(-1).astype(np.float64)
    fgi = np.nonzero(flat_k >= 0)[0]
    uk, inv = np.unique(flat_k[fgi], return_inverse=True)
    px, py = fgi % W + 0.5, fgi // W + 0.5
    cnt = np.bincount(inv)
    cx, cy = np.bincount(inv, px) / cnt, np.bincount(inv, py) / cnt
    dmax = np.zeros(len(uk))
    np.maximum.at(dmax, inv, flat_d[fgi])
    # deep enough (most of the face's depth), then nearest the face's centroid
    score = np.minimum(flat_d[fgi], np.floor(0.6 * dmax[inv]) + 1)
    dist = np.hypot(px - cx[inv], py - cy[inv])
    order = np.lexsort((dist, -score, inv))
    first = np.ones(len(order), dtype=bool)
    first[1:] = inv[order][1:] != inv[order][:-1]
    anchors = {}
    for j in order[first]:
        k = int(uk[inv[j]])
        anchors[(k >> 32, k & 0xFFFFFFFF)] = (px[j], py[j], int(flat_d[fgi[j]]))

    items = []
    for k, cnt in visible.items():
        if k not in names:
            continue
        pi, fid = k
        spec = overlay.faces.get(k) if overlay else None
        tags = parts[pi].tags.get(fid, [])
        if overlay is not None and spec is None and not labels:
            continue
        text = names[k]
        if spec and spec.get("label"):
            text += " · " + spec["label"]
        elif tags:
            text += " · " + ", ".join(tags)
        prio = 0 if (spec or tags) else 1
        items.append((prio, -cnt, k, text, spec, bool(tags)))
    items.sort()
    boxes: list[tuple] = []
    for prio, negcnt, k, text, spec, tagged in items:
        if -negcnt < 12:
            report["small"].append(names[k])
            continue
        ax, ay, depth = anchors[k]
        if spec:
            col = tuple(int(255 * (0.55 + 0.45 * v)) for v in spec["color"])
            fill, outline = col, tuple(int(255 * 0.5 * v) for v in spec["color"])
        elif tagged:
            fill, outline = (255, 226, 140), (150, 110, 20)
        else:
            fill, outline = (255, 255, 255), (90, 96, 104)
        if _place(draw, font, text, (ax, ay), depth, boxes, W, H, fill, outline):
            report["labeled"].append(names[k])
        else:
            report["small"].append(names[k])
    return boxes


def _text_size(font, text):
    l, t, r, b = font.getbbox(text)
    return r - l, b - t, l, t


def _overlaps(b, boxes) -> bool:
    return any(b[0] < o[2] and o[0] < b[2] and b[1] < o[3] and o[1] < b[3] for o in boxes)


def _draw_box(draw, font, text, box, fill, outline):
    tw, th, l, t = _text_size(font, text)
    draw.rounded_rectangle(box, radius=3, fill=fill, outline=outline)
    draw.text((box[0] + 4 - l, box[1] + 2 - t), text, fill=(20, 22, 26), font=font)


def _place(draw, font, text, anchor, depth, boxes, W, H, fill, outline) -> bool:
    """Inside the face at its deepest point if that is free, else nearby with a leader line."""
    tw, th, _, _ = _text_size(font, text)
    bw, bh = tw + 8, th + 5
    ax, ay = anchor

    def box_at(cx, cy):
        return (round(cx - bw / 2), round(cy - bh / 2), round(cx - bw / 2) + bw, round(cy - bh / 2) + bh)

    def fits(b):
        return b[0] >= 1 and b[1] >= 1 and b[2] <= W - 1 and b[3] <= H - 1 and not _overlaps(b, boxes)

    b = box_at(ax, ay)
    if depth * 2 >= bh and fits(b):
        _draw_box(draw, font, text, b, fill, outline)
        boxes.append(b)
        return True
    for r in (1.6, 2.6, 3.8, 5.5, 8.0):
        rad = r * bh + bw / 2
        for j in range(16):
            ang = math.pi * 2 * j / 16 - math.pi / 4
            cx, cy = ax + math.cos(ang) * rad, ay - math.sin(ang) * rad * 0.6
            b = box_at(cx, cy)
            dot = (ax - 4, ay - 4, ax + 4, ay + 4)
            if fits(b) and not _overlaps(dot, boxes):
                ex = min(max(ax, b[0]), b[2])
                ey = min(max(ay, b[1]), b[3])
                draw.line([(ax, ay), (ex, ey)], fill=outline, width=1)
                draw.ellipse(dot, fill=(20, 22, 26), outline=(255, 255, 255))
                _draw_box(draw, font, text, b, fill, outline)
                boxes.append(b)
                boxes.append(dot)
                return True
    return False


def _tag_box(draw, font, text, p, boxes, W, H, fill, outline):
    tw, th, _, _ = _text_size(font, text)
    bw, bh = tw + 8, th + 5
    x0 = min(max(1, p[0] - bw / 2), W - bw - 1)
    y0 = min(max(1, p[1] - bh - 4), H - bh - 1)
    b = (round(x0), round(y0), round(x0) + bw, round(y0) + bh)
    _draw_box(draw, font, text, b, fill, outline)
    boxes.append(b)


def _triad(draw, R, font, H):
    o = np.array([46.0, H - 46.0])
    for axis, name, col in ((0, "x", (210, 50, 40)), (1, "y", (40, 150, 60)), (2, "z", (40, 90, 210))):
        v = np.array([R[0][axis], -R[1][axis]]) * 24
        if np.linalg.norm(v) < 3:
            draw.ellipse([*(o - 3), *(o + 3)], outline=col, width=2)
            draw.text(tuple(o + (6, -18)), name, fill=col, font=font)
            continue
        draw.line([tuple(o), tuple(o + v)], fill=col, width=2)
        u = v / np.linalg.norm(v)
        draw.text(tuple(o + v + u * 8 - (4, 8)), name, fill=col, font=font)
