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
LINE_ON_DARK = (0.62, 0.66, 0.72)  # an edge across a dark part: light, so it shows


def _ink(img: np.ndarray, line: np.ndarray):
    """Draws the line pixels: dark, but light where the surface under them is dark."""
    under = img[line]
    dark = (under @ np.array([0.3, 0.59, 0.11])) < 0.22
    img[line] = np.where(dark[:, None], LINE_ON_DARK, LINE)
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
    colors: np.ndarray | None = None  # (n, 3) rgb per vertex (a field): replaces the part's and the overlay's colors
    path: str = ""  # its full name in the assembly ("ship/heatshield/tiles"); "" is its name
    copies: int = 1  # placed copies merged into it, each len(positions) / copies vertices, in order
    material: tuple | None = None  # (metal, roughness), 0..1 each; None: the default look (a matte paint)


@dataclass
class Overlay:
    faces: dict = field(default_factory=dict)  # (part index, face id) -> {"color": rgb, "label": str}
    arrows: list = field(default_factory=list)  # {"at": xyz, "dir": xyz, "label": str}
    scale: dict | None = None  # the color bar: {"label", "unit", "min", "max", "note"}, ramp as RAMP


def _unit(v) -> np.ndarray:
    v = np.asarray(v, dtype=np.float64)
    return v / np.linalg.norm(v)


def best_iso(parts: list[Part]) -> tuple:
    """The 3/4 view (from above, one of four corners) that sees the most surface besides the
    biggest face: a part's features (arms, ribs, pockets) rather than its flat back. The usual
    corner unless another clearly sees more."""
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
    area = np.where(acc[:, 3] < acc[:, 3].max(initial=0), acc[:, 3], 0)  # all but the biggest face
    seen = [float((area * np.clip(mean @ _unit(c), 0, None)).sum()) for c in corners]
    best = max(range(4), key=lambda k: seen[k])
    return corners[best] if seen[best] > 1.2 * seen[0] else corners[0]


def camera(view, direction=None) -> np.ndarray:
    """Rows: screen right, screen up, toward the viewer."""
    d = _unit(direction if direction is not None else VIEWS[view])
    fwd = -d
    right = _unit(np.cross(fwd, (0, 0, 1)))
    up = np.cross(right, fwd)
    return np.stack([right, up, d])


# ---------------------------------------------------------------- light

# in camera space (x right, y up, z toward the viewer): a key light from the upper left, a fill from the
# right, white highlights and a rim where the surface turns away, so a black part still shows its shape
KEY = _unit((-0.45, 0.65, 0.62))
FILL = _unit((0.7, -0.25, 0.45))


def light(n: np.ndarray, v: np.ndarray):
    """(diffuse, specular, rim) per unit normal n toward the viewer v (one direction, or one per normal)."""
    v = np.broadcast_to(np.asarray(v, dtype=n.dtype), n.shape)
    key, fill = KEY.astype(n.dtype), FILL.astype(n.dtype)
    dot = lambda a, b: np.einsum("ij,ij->i", a, b)
    diffuse = 0.27 + 0.58 * np.clip(n @ key, 0, 1) + 0.22 * np.clip(n @ fill, 0, 1)
    hk = key + v
    hk /= np.sqrt(dot(hk, hk))[:, None] + 1e-12
    hf = fill + v
    hf /= np.sqrt(dot(hf, hf))[:, None] + 1e-12
    spec = 0.20 * np.clip(dot(n, hk), 0, 1) ** 36 + 0.07 * np.clip(dot(n, hf), 0, 1) ** 12
    rim = 0.16 * (1 - np.clip(np.abs(dot(n, v)), 0, 1)) ** 3
    return diffuse, spec, rim


def compose(color: np.ndarray, diffuse, spec, rim) -> np.ndarray:
    """Lit colors: a dark color is lifted a little so its shading shows; highlights and the rim are white-ish."""
    c = 0.07 + 0.93 * color
    return np.clip(c * diffuse[:, None] + spec[:, None] + rim[:, None] * (0.6 + 0.4 * color), 0, 1)


def _lit_colors(parts, part_of, color, n, v, up, diffuse, spec, rim, overlay=None) -> np.ndarray:
    """compose, or compose_material for the pixels of parts with a material (not over an overlay's colors)."""
    out = compose(color, diffuse, spec, rim)
    if overlay is not None or not any(p.material for p in parts):
        return out
    mats = np.array([p.material or (np.nan, np.nan) for p in parts], dtype=np.float64)
    m = mats[part_of]
    own = ~np.isnan(m[:, 0])
    if own.any():
        vv = v if np.ndim(v) == 1 else v[own]
        out[own] = compose_material(color[own], n[own], vv, up, m[own].astype(n.dtype), diffuse[own], spec[own], rim[own])
    return out


def _view_dirs(pix, Ws, Hs, f) -> np.ndarray:
    """Toward the viewer through each pixel (camera space), as render_camera lights it."""
    v = np.empty((len(pix), 3), dtype=np.float32)
    v[:, 0] = -((pix % Ws) + 0.5 - Ws / 2) / f
    v[:, 1] = ((pix // Ws) + 0.5 - Hs / 2) / f
    v[:, 2] = 1
    return v / np.sqrt(np.einsum("ij,ij->i", v, v))[:, None]


def _smooth(a, b, x):
    t = np.clip((x - a) / (b - a), 0, 1)
    return t * t * (3 - 2 * t)


def studio(r: np.ndarray, up: np.ndarray, rough: np.ndarray) -> np.ndarray:
    """What a reflection sees in direction r (camera space): a bright ceiling, a grey horizon and a dark
    floor (world up is ``up``), plus the key light's softbox; blurrier and flatter the rougher the surface."""
    h = r @ up
    w = 0.35 * rough  # blurred over this much of the sky
    sky = lambda x: np.interp(x, (-1, -0.5, -0.15, 0.03, 0.5, 1), (0.3, 0.4, 0.62, 0.86, 0.93, 0.98))
    e = (sky(h - w) + sky(h) + sky(h + w)) / 3
    e = e * (1 - 0.25 * r[:, 0] ** 2)  # darker to the sides: a round part looks round
    e = e + (0.62 - e) * 0.45 * rough  # a rough surface averages its surroundings
    p = 2.0 / (rough * rough + 0.004)
    box = np.clip(r @ KEY.astype(r.dtype), 0, 1) ** np.minimum(p, 2000)
    fill = np.clip(r @ FILL.astype(r.dtype), 0, 1) ** np.minimum(0.5 * p, 1000)
    return e + (1.3 * box + 0.35 * fill) * (1.15 - 0.7 * rough)


def compose_material(color, n, v, up, mat, diffuse, spec, rim) -> np.ndarray:
    """Lit colors of parts with a material (``mat``: (k, 2) metal, roughness per pixel): a metal is its
    color times what it reflects (a studio: ceiling, horizon, floor, the key light), whiter at grazing
    angles; a paint or plastic is the usual light with its highlight as sharp as its roughness says."""
    metal, rough = mat[:, 0], mat[:, 1]
    v = np.broadcast_to(np.asarray(v, dtype=n.dtype), n.shape)
    ndv = np.clip(np.einsum("ij,ij->i", n, v), 0, 1)
    r = 2 * ndv[:, None] * n - v
    env = studio(r, np.asarray(up, dtype=n.dtype), rough)
    fres = (1 - ndv) ** 5
    k = np.clip((1 - rough) / 0.38, 0.1, 2.5)  # 0.62 (no material) keeps the usual highlight
    plain = compose(color, diffuse, spec * k, rim) + ((0.04 + 0.96 * fres) * (1 - rough) ** 2 * 0.6 * env)[:, None]
    F = color + (1 - color) * fres[:, None] * (1 - 0.6 * rough[:, None])
    shiny = F * (1.2 * env)[:, None] + 0.06 * color * diffuse[:, None]
    return np.clip((1 - metal)[:, None] * plain + metal[:, None] * shiny, 0, 1)


# ---------------------------------------------------------------- rasterizer


def rasterize(X, Y, Z, tris, W, H, limit=2_000_000):
    """Z-buffer: per pixel the nearest triangle (-1: none) and barycentrics of its first two corners.
    Z grows toward the viewer. A triangle covers the pixels whose centres are inside it, found row by
    row (a long thin triangle across the picture costs its own pixels, not its bounding box's)."""
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
    xs, ys = x[ids], y[ids]
    rows = y1[ids] - y0[ids] + 1
    rends = np.cumsum(rows)
    start = 0
    while start < len(ids):  # in chunks of triangles with about `limit` rows
        base = rends[start - 1] if start else 0
        stop = max(start + 1, int(np.searchsorted(rends, base + limit // 4, side="right")))
        n = rows[start:stop]
        local = np.repeat(np.arange(start, stop), n)  # a (triangle, row) per item
        ry = y0[ids[local]] + np.arange(int(n.sum())) - np.repeat(np.cumsum(n) - n, n)
        fy = ry + 0.5
        # the row's span: where the row's centre line crosses the triangle's edges
        lo = np.full(len(local), np.inf)
        hi = np.full(len(local), -np.inf)
        for i, j in ((0, 1), (1, 2), (2, 0)):
            xi, yi, xj, yj = xs[local, i], ys[local, i], xs[local, j], ys[local, j]
            cross = (np.minimum(yi, yj) <= fy) & (fy <= np.maximum(yi, yj)) & (yi != yj)
            with np.errstate(divide="ignore", invalid="ignore"):
                xc = xi + (fy - yi) * (xj - xi) / (yj - yi)
            lo = np.where(cross, np.minimum(lo, xc), lo)
            hi = np.where(cross, np.maximum(hi, xc), hi)
        sx0 = np.clip(np.ceil(lo - 0.5 - 1e-6), 0, W - 1)
        sx1 = np.clip(np.floor(hi - 0.5 + 1e-6), 0, W - 1)
        has = np.isfinite(lo) & (hi >= lo) & (sx1 >= sx0) & (hi >= 0.5) & (lo <= W - 0.5)
        local, ry, sx0, sx1 = local[has], ry[has], sx0[has].astype(np.int64), sx1[has].astype(np.int64)
        w = sx1 - sx0 + 1
        ends = np.cumsum(w)
        s2 = 0
        while s2 < len(local):  # fragments in chunks of about `limit`
            base2 = ends[s2 - 1] if s2 else 0
            e2 = max(s2 + 1, int(np.searchsorted(ends, base2 + limit, side="right")))
            ww = w[s2:e2]
            item = np.repeat(np.arange(s2, e2), ww)
            px = sx0[item] + np.arange(int(ww.sum())) - np.repeat(np.cumsum(ww) - ww, ww)
            py = ry[item]
            t = local[item]
            fx, fyy = px + 0.5, py + 0.5
            w0 = a0[t] * fx + c0[t] * fyy + e0[t]
            w1 = a1[t] * fx + c1[t] * fyy + e1[t]
            w2 = 1.0 - w0 - w1
            inside = (w0 >= -1e-6) & (w1 >= -1e-6) & (w2 >= -1e-6)
            t, w0, w1, w2 = t[inside], w0[inside], w1[inside], w2[inside]
            pix = (py * W + px)[inside]
            depth = w0 * zz[t, 0] + w1 * zz[t, 1] + w2 * zz[t, 2]
            # the nearest fragment per pixel: the z-buffer takes the largest depth (old and new), and a
            # fragment at that depth wins (of several at the same depth, one of them)
            np.maximum.at(zbuf, pix, depth)
            win = depth >= zbuf[pix]
            p = pix[win]
            tbuf[p] = ids[t[win]]
            b0buf[p] = w0[win]
            b1buf[p] = w1[win]
            s2 = e2
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
           overlay: Overlay | None = None, caption: str = "", plain: bool = False, edges: bool = True) -> tuple[Image.Image, dict]:
    """Returns the image and a report: {"labeled": [...], "small": [...], "hidden": [...]} (face names).
    plain: for documents (white, no axes, no footer but the caption). edges: the CAD edges and outlines.
    An orthographic view framed on everything; render_camera draws through a Camera instead."""
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
    # see-through parts (a color with opacity, (r, g, b, a)): drawn over what is behind them, the nearest layer only
    alpha = np.array([p.color[3] if p.color is not None and len(p.color) > 3 else 1.0 for p in parts])
    see = alpha[tri_part] < 1
    solid = np.nonzero(~see)[0]
    zbuf, tbuf, b0, b1 = rasterize(X, Y, S[:, 2], T[solid], Ws, Hs)
    tbuf = np.where(tbuf >= 0, solid[np.maximum(tbuf, 0)], -1)
    if see.any():
        clear = np.nonzero(see)[0]
        zg, tg, g0, g1 = rasterize(X, Y, S[:, 2], T[clear], Ws, Hs)
        tg = np.where((tg >= 0) & (zg > zbuf), clear[np.maximum(tg, 0)], -1)
    fg = tbuf >= 0
    t = tbuf[fg]

    # colors per triangle
    base = np.empty((len(parts), 3))
    for i, p in enumerate(parts):
        base[i] = p.color[:3] if p.color else DEFAULT_COLOR
    tri_color = base[tri_part]
    if overlay is not None:
        tri_color[:] = DIM_COLOR
        for (pi, fid), spec in overlay.faces.items():
            tri_color[(tri_part == pi) & (tri_face == fid)] = spec["color"]

    # smooth shading from interpolated surface normals, lit in camera space (an orthographic view: toward the viewer is +z)
    def lit(t, w0, w1):
        i0, i1, i2 = T[t, 0], T[t, 1], T[t, 2]
        n = w0[:, None] * Nw[i0] + w1[:, None] * Nw[i1] + (1 - w0 - w1)[:, None] * Nw[i2]
        n = n @ R.T
        n /= np.linalg.norm(n, axis=1, keepdims=True) + 1e-12
        n[n[:, 2] < 0] *= -1  # two-sided
        return (n, *light(n, np.array([0.0, 0.0, 1.0])))

    w0, w1 = b0[fg], b1[fg]
    i0, i1, i2 = T[t, 0], T[t, 1], T[t, 2]
    n, shade, spec, rim = lit(t, w0, w1)
    color = tri_color[t]
    if any(p.colors is not None for p in parts):  # a field: colors per vertex, interpolated in each triangle
        C = np.concatenate([p.colors if p.colors is not None else np.full((len(p.positions), 3), np.nan) for p in parts])
        own = np.array([p.colors is not None for p in parts])[tri_part[t]]
        w2 = 1 - w0 - w1
        color[own] = (w0[:, None] * C[i0] + w1[:, None] * C[i1] + w2[:, None] * C[i2])[own]
    img = np.empty((Ws * Hs, 3))
    img[:] = (1.0, 1.0, 1.0) if plain else BACKGROUND
    img[fg] = _lit_colors(parts, tri_part[t], color, n, np.array([0.0, 0.0, 1.0]), R[:, 2], shade, spec, rim, overlay)

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
    if edges:
        _ink(img, line)
    if see.any():  # the glass over it: its color by its opacity, brighter where it turns away (a rim), its outline faint
        gf = tg >= 0
        if gf.any():
            gt = tg[gf]
            gn, gshade, gspec, _ = lit(gt, g0[gf], g1[gf])
            a = alpha[tri_part[gt]]
            a = np.clip(a * (0.8 + 1.6 * (1 - np.abs(gn[:, 2])) ** 2), 0, 0.92)[:, None]
            flat = img.reshape(-1, 3)
            flat[gf] = (1 - a) * flat[gf] + a * np.clip(tri_color[gt] * gshade[:, None] + 3 * gspec[:, None], 0, 1)
            gk = np.full(Ws * Hs, -1, dtype=np.int64)
            gk[gf] = tri_part[gt] * (1 << 32) + tri_face[gt]
            gk = gk.reshape(Hs, Ws)
            edge = np.zeros((Hs, Ws), dtype=bool)
            for a2, b2 in (((slice(None), slice(1, None)), (slice(None), slice(None, -1))),
                           ((slice(1, None), slice(None)), (slice(None, -1), slice(None)))):
                diff = gk[a2] != gk[b2]
                edge[a2] |= diff
                edge[b2] |= diff
            if SS > 1:
                edge = _dilate(edge)
            img[edge] = 0.55 * img[edge] + 0.45 * np.asarray(LINE)

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
        boxes = _labels(draw, font, parts, Kf, visible, names, overlay, labels, report)
    for at, tail, text in arrows:
        if text:
            p = ((tail @ R[0] - c[0]) * scale + W / 2, H / 2 - (tail @ R[1] - c[1]) * scale)
            _tag_box(draw, font, text, p, boxes, W, H, fill=(255, 236, 230), outline=(200, 40, 20))
    if plain:
        if not caption:
            return im, report
        lines = _wrap(font, caption, W - 16)
        out = Image.new("RGB", (W, H + len(lines) * (fs + 4) + 10), (255, 255, 255))
        out.paste(im, (0, 0))
        for k, line in enumerate(lines):
            ImageDraw.Draw(out).text((8, H + 6 + k * (fs + 4)), line, fill=(40, 44, 50), font=font)
        return out, report
    _triad(draw, R, font, H)

    bar = overlay.scale if overlay is not None and overlay.scale else None
    lines = _wrap(font, f"{caption} · view {view}" if caption else f"view {view}", W - 16)
    top = len(lines) * (fs + 4) + 10
    out = Image.new("RGB", (W, H + top + ((fs + 14) if bar else 0)), (232, 234, 237))
    out.paste(im, (0, 0))
    od = ImageDraw.Draw(out)
    for k, line in enumerate(lines):
        od.text((8, H + 6 + k * (fs + 4)), line, fill=(40, 44, 50), font=font)
    if bar:
        _color_bar(od, font, bar, 8, H + top + 4, fs)
    return out, report


def _wrap(font, text: str, width: int) -> list[str]:
    """The caption in lines that fit (broken between words)."""
    lines, cur = [], ""
    for word in text.split(" "):
        t = f"{cur} {word}" if cur else word
        if cur and _text_size(font, t)[0] > width:
            lines.append(cur)
            cur = word
        else:
            cur = t
    return lines + [cur] if cur else lines or [""]


def _color_bar(draw, font, s, x, y, fs):
    """The legend of a value scale, as in the viewer: label, min, the ramp, max and unit, a note."""
    def fmt(v):
        return f"{v:.4g}"

    unit = f" {s['unit']}" if s.get("unit") else ""
    ink = (40, 44, 50)
    draw.text((x, y), s.get("label") or "value", fill=ink, font=font)
    x += _text_size(font, s.get("label") or "value")[0] + 14
    draw.text((x, y), fmt(s["min"]) + unit, fill=(90, 96, 104), font=font)
    x += _text_size(font, fmt(s["min"]) + unit)[0] + 8
    stops = np.asarray(s["colors"], dtype=np.float64)
    w, h = 16 * fs, max(8, fs - 2)
    tt = np.linspace(0, 1, w) * (len(stops) - 1)
    i = np.minimum(len(stops) - 2, tt.astype(int))
    rgb = stops[i] + (stops[i + 1] - stops[i]) * (tt - i)[:, None]
    for k in range(w):
        draw.line([(x + k, y + 2), (x + k, y + 2 + h)], fill=tuple(int(255 * c + 0.5) for c in rgb[k]))
    draw.rectangle([x, y + 2, x + w, y + 2 + h], outline=(150, 154, 160))
    x += w + 8
    draw.text((x, y), fmt(s["max"]) + unit, fill=(90, 96, 104), font=font)
    x += _text_size(font, fmt(s["max"]) + unit)[0] + 14
    note = "; ".join(filter(None, [s.get("note"), f"shape exaggerated {s['deform']:g} times" if s.get("deform") else ""]))
    if note:
        draw.text((x, y), note, fill=(90, 96, 104), font=font)


def _labels(draw, font, parts, Kf, visible, names, overlay, labels, report) -> list:
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


# ---------------------------------------------------------------- a camera (perspective, as the viewer)


@dataclass
class Prepared:
    """Parts gathered once into flat arrays, to draw through many cameras (tule screenshot --cameras)."""

    parts: list
    P: np.ndarray  # (n, 3) world positions
    N: np.ndarray  # (n, 3) world normals
    T: np.ndarray  # (m, 3) vertex indices
    tri_part: np.ndarray  # (m,) part index
    tri_face: np.ndarray  # (m,) B-rep face id
    tri_copy: np.ndarray  # (m,) placed copy within its part
    base: np.ndarray  # (parts, 3) colors
    alpha: np.ndarray  # (parts,) opacity
    C: np.ndarray | None  # (n, 3) per-vertex colors (a field), NaN where none
    voffs: np.ndarray  # first vertex of each part
    toffs: np.ndarray  # first triangle of each part


def prepare(parts: list[Part]) -> Prepared:
    parts = [p for p in parts if len(p.indices)]
    if not parts:
        raise ValueError("nothing to draw: the file has no triangles")
    voffs = np.cumsum([0] + [len(p.positions) for p in parts])
    toffs = np.cumsum([0] + [len(p.indices) for p in parts])
    P = np.concatenate([p.positions for p in parts]).astype(np.float64)
    N = np.concatenate([p.normals for p in parts]).astype(np.float64)
    T = np.concatenate([np.asarray(p.indices, dtype=np.int64).reshape(-1, 3) + o for p, o in zip(parts, voffs)])
    tri_part = np.concatenate([np.full(len(p.indices), i, dtype=np.int32) for i, p in enumerate(parts)])
    tri_face = np.concatenate([np.asarray(p.face_ids, dtype=np.int64) for p in parts])
    tri_copy = np.concatenate([np.arange(len(p.indices), dtype=np.int64) // max(1, len(p.indices) // max(1, p.copies))
                               for p in parts])
    base = np.array([p.color[:3] if p.color else DEFAULT_COLOR for p in parts], dtype=np.float64)
    alpha = np.array([p.color[3] if p.color is not None and len(p.color) > 3 else 1.0 for p in parts])
    C = None
    if any(p.colors is not None for p in parts):
        C = np.concatenate([p.colors if p.colors is not None else np.full((len(p.positions), 3), np.nan) for p in parts])
    return Prepared(parts, P, N, T, tri_part, tri_face, tri_copy, base, alpha, C, voffs, toffs)


def part_path(p: Part) -> str:
    return p.path or p.name


def select(prep: Prepared, name: str) -> list[tuple[int, int | None]]:
    """[(part index, copy or None)] that ``name`` names: a part, a group ("ship/heatshield"), or one placed
    copy ("tiles[17]", from 0). Raises ValueError naming what there is."""
    from .camera import matches, split_copy

    stem, copy = split_copy(name)
    found = [i for i, p in enumerate(prep.parts) if matches(part_path(p), stem) or matches(part_path(p), name)]
    if not found:
        have = sorted({part_path(p) for p in prep.parts})
        more = f" (and {len(have) - 30} more)" if len(have) > 30 else ""
        raise ValueError(f"no part or group {name!r}; parts: {', '.join(have[:30])}{more}")
    if copy is not None and not matches(part_path(prep.parts[found[0]]), name):
        out = []
        for i in found:
            if copy >= prep.parts[i].copies:
                raise ValueError(f"{stem} has {prep.parts[i].copies} placed copies (0 to {prep.parts[i].copies - 1}), not [{copy}]")
            out.append((i, copy))
        return out
    return [(i, None) for i in found]


def bounds(prep: Prepared, sel=None, hidden=None) -> tuple[np.ndarray, np.ndarray]:
    """The box around the selected parts or copies (default: every part not hidden)."""
    if sel is None:
        hid = hidden_parts(prep, hidden)
        sel = [(i, None) for i in range(len(prep.parts)) if i not in hid]
    lo, hi = np.full(3, np.inf), np.full(3, -np.inf)
    for i, c in sel:
        a, b = prep.voffs[i], prep.voffs[i + 1]
        if c is not None:
            nv = (b - a) // prep.parts[i].copies
            a, b = a + c * nv, a + (c + 1) * nv
        if b > a:
            lo = np.minimum(lo, prep.P[a:b].min(0))
            hi = np.maximum(hi, prep.P[a:b].max(0))
    if not np.isfinite(lo).all():
        raise ValueError("nothing to frame: every part is hidden")
    return lo, hi


def hidden_parts(prep: Prepared, hidden) -> set:
    return {i for i, c in (hidden or []) if c is None}


def _hidden_tris(prep: Prepared, hidden) -> np.ndarray:
    mask = np.zeros(len(prep.T), dtype=bool)
    for i, c in hidden or []:
        a, b = prep.toffs[i], prep.toffs[i + 1]
        if c is None:
            mask[a:b] = True
        else:
            mask[a:b] |= prep.tri_copy[a:b] == c
    return mask


def _clip_near(Q, N, C, T, near):
    """Triangles cut at the near plane (depth -Q[:, 2] >= near): those partly behind the eye become one or
    two triangles in front, their new corners interpolated. Returns the extended Q, N, C, the kept
    triangles and, for each, the index of the triangle it came from."""
    z = -Q[T, 2]
    behind = z < near
    nb = behind.sum(1)
    keep = np.nonzero(nb == 0)[0]
    one, two = np.nonzero(nb == 1)[0], np.nonzero(nb == 2)[0]
    if not len(one) and not len(two):
        return Q, N, C, T[keep], keep
    newQ, newN, newC = [], [], []
    n0 = len(Q)

    def cut(i, j):  # a point on each edge i -> j (vertex index arrays) at the near plane
        zi, zj = -Q[i, 2], -Q[j, 2]
        t = ((near - zi) / (zj - zi))[:, None]
        newQ.append(Q[i] + t * (Q[j] - Q[i]))
        nn = N[i] + t * (N[j] - N[i])
        newN.append(nn / (np.linalg.norm(nn, axis=1, keepdims=True) + 1e-12))
        if C is not None:
            newC.append(C[i] + t * (C[j] - C[i]))
        start = n0 + sum(len(q) for q in newQ[:-1])
        return np.arange(start, start + len(i))

    tris, src = [T[keep]], [keep]
    if len(one):
        r = np.argmax(behind[one], 1)
        tt = T[one]
        a, b, c = tt[np.arange(len(one)), r], tt[np.arange(len(one)), (r + 1) % 3], tt[np.arange(len(one)), (r + 2) % 3]
        ab, ac = cut(a, b), cut(a, c)
        tris += [np.stack([ab, b, c], 1), np.stack([ab, c, ac], 1)]
        src += [one, one]
    if len(two):
        r = np.argmin(behind[two], 1)
        tt = T[two]
        c, a, b = tt[np.arange(len(two)), r], tt[np.arange(len(two)), (r + 1) % 3], tt[np.arange(len(two)), (r + 2) % 3]
        ca, cb = cut(c, a), cut(c, b)
        tris.append(np.stack([c, ca, cb], 1))
        src.append(two)
    Q = np.concatenate([Q, *newQ])
    N = np.concatenate([N, *newN])
    if C is not None:
        C = np.concatenate([C, *newC])
    return Q, N, C, np.concatenate(tris), np.concatenate(src)


def render_camera(prep: Prepared, cam, W: int, H: int, hidden=None, edges: bool = True, labels: bool = False,
                  overlay: Overlay | None = None, background=BACKGROUND) -> tuple[Image.Image, dict]:
    """The parts as the camera (camera.Camera) sees them, W×H pixels, nothing drawn around them (no
    footer, no axes): what the viewer shows through the same camera. hidden: [(part, copy or None)]
    not drawn. The report: as render's, and "parts": the share of the picture each visible part covers."""
    from .camera import project

    R = cam.basis()
    eye = cam.eye
    Ws, Hs = W * SS, H * SS
    tv = math.tan(math.radians(cam.fov) / 2)
    th = tv * W / H
    f = (Hs / 2) / tv
    # what can be seen: in front of the eye, inside the view, not hidden
    Q = (prep.P - eye) @ R.T  # right, up, toward the viewer (the scene is at -z)
    z = -Q[:, 2]
    lo_w, hi_w = prep.P.min(0), prep.P.max(0)
    extent = float(np.linalg.norm(hi_w - lo_w)) or 1.0
    near = max(1e-3 * cam.distance, 1e-6 * extent)
    slack = 1.02
    code = ((Q[:, 0] < -slack * th * z).astype(np.uint8) | ((Q[:, 0] > slack * th * z) << 1)
            | ((Q[:, 1] < -slack * tv * z) << 2) | ((Q[:, 1] > slack * tv * z) << 3) | ((z < near) << 4)).astype(np.uint8)
    T = prep.T
    outside = (code[T[:, 0]] & code[T[:, 1]] & code[T[:, 2]]) != 0
    live = np.nonzero(~outside & ~_hidden_tris(prep, hidden))[0]
    Qx, Nx, Cx, Tx, src = _clip_near(Q, prep.N, prep.C, T[live], near)
    tid = live[src]  # the original triangle of each drawn one
    zx = np.maximum(-Qx[:, 2], near * 0.5)
    X = Ws / 2 + f * Qx[:, 0] / zx
    Y = Hs / 2 - f * Qx[:, 1] / zx
    Zr = 1.0 / zx  # nearer is larger; linear in screen space
    tri_part = prep.tri_part[tid]
    tri_face = prep.tri_face[tid]
    see = prep.alpha[tri_part] < 1
    solid = np.nonzero(~see)[0]
    zbuf, tbuf, b0, b1 = rasterize(X, Y, Zr, Tx[solid], Ws, Hs)
    tbuf = np.where(tbuf >= 0, solid[np.maximum(tbuf, 0)], -1)
    if see.any():
        clear = np.nonzero(see)[0]
        zg, tg, g0, g1 = rasterize(X, Y, Zr, Tx[clear], Ws, Hs)
        tg = np.where((tg >= 0) & (zg > zbuf), clear[np.maximum(tg, 0)], -1)
    fg = tbuf >= 0
    t = tbuf[fg]

    tri_color = prep.base[tri_part]
    if overlay is not None:
        tri_color[:] = DIM_COLOR
        for (pi, fid), spec in overlay.faces.items():
            tri_color[(tri_part == pi) & (tri_face == fid)] = spec["color"]

    Nc = (Nx @ R.T).astype(np.float32)  # normals in camera space
    iz = (1.0 / zx).astype(np.float32)

    def lit(t, w0, w1, pix):
        """Normals (camera space, toward the viewer), the perspective-correct weights, and the light."""
        i = Tx[t]
        w = np.stack([w0, w1, 1 - w0 - w1], 1).astype(np.float32) * iz[i]  # perspective-correct
        w /= w.sum(1, keepdims=True)
        n = w[:, :1] * Nc[i[:, 0]] + w[:, 1:2] * Nc[i[:, 1]] + w[:, 2:] * Nc[i[:, 2]]
        n /= np.sqrt(np.einsum("ij,ij->i", n, n))[:, None] + 1e-12
        # toward the viewer, through the pixel
        v = np.empty((len(pix), 3), dtype=np.float32)
        v[:, 0] = -((pix % Ws) + 0.5 - Ws / 2) / f
        v[:, 1] = ((pix // Ws) + 0.5 - Hs / 2) / f
        v[:, 2] = 1
        v /= np.sqrt(np.einsum("ij,ij->i", v, v))[:, None]
        n[np.einsum("ij,ij->i", n, v) < 0] *= -1  # two-sided
        return (n, w, *light(n, v))

    n, w, shade, spec, rim = lit(t, b0[fg], b1[fg], np.nonzero(fg)[0])
    color = tri_color[t]
    if Cx is not None:
        own = ~np.isnan(Cx[Tx[t], 0]).any(1)
        color[own] = np.einsum("kj,kjc->kc", w, Cx[Tx[t]])[own]
    img = np.empty((Ws * Hs, 3))
    img[:] = background
    img[fg] = _lit_colors(prep.parts, tri_part[t], color, n, _view_dirs(np.nonzero(fg)[0], Ws, Hs, f), R[:, 2], shade, spec, rim, overlay)

    fkey = np.full(Ws * Hs, -1, dtype=np.int64)
    fkey[fg] = tri_part[t].astype(np.int64) * (1 << 32) + tri_face[t]
    K = fkey.reshape(Hs, Ws)
    img = img.reshape(Hs, Ws, 3)
    if edges:
        D = np.where(fg, 1.0 / np.where(fg, zbuf, 1.0), np.nan).reshape(Hs, Ws)  # depth, mm
        meshy = np.array([p.faces == 0 for p in prep.parts])
        nimg = np.zeros((Ws * Hs, 3))
        nimg[fg] = n
        nimg = nimg.reshape(Hs, Ws, 3)
        mk = np.zeros(Ws * Hs, dtype=bool)
        mk[fg] = meshy[tri_part[t]]
        mk = mk.reshape(Hs, Ws)
        line = np.zeros((Hs, Ws), dtype=bool)
        for a, b in (((slice(None), slice(1, None)), (slice(None), slice(None, -1))),
                     ((slice(1, None), slice(None)), (slice(None, -1), slice(None)))):
            diff = K[a] != K[b]
            with np.errstate(invalid="ignore"):
                diff |= np.abs(D[a] - D[b]) > 0.03 * np.fmin(D[a], D[b])
            diff |= mk[a] & mk[b] & ((nimg[a] * nimg[b]).sum(-1) < math.cos(math.radians(35)))
            line[a] |= diff
            line[b] |= diff
        if SS > 1:
            line = _dilate(line)
        _ink(img, line)
    if see.any():
        gf = tg >= 0
        if gf.any():
            gt = tg[gf]
            gn, _, gshade, gspec, _ = lit(gt, g0[gf], g1[gf], np.nonzero(gf)[0])
            a = prep.alpha[tri_part[gt]]
            a = np.clip(a * (0.8 + 1.6 * (1 - np.abs(gn[:, 2])) ** 2), 0, 0.92)[:, None]
            flat = img.reshape(-1, 3)
            flat[gf] = (1 - a) * flat[gf] + a * np.clip(tri_color[gt] * gshade[:, None] + 3 * gspec[:, None], 0, 1)
            if edges:
                gk = np.full(Ws * Hs, -1, dtype=np.int64)
                gk[gf] = tri_part[gt].astype(np.int64) * (1 << 32) + tri_face[gt]
                gk = gk.reshape(Hs, Ws)
                edge = np.zeros((Hs, Ws), dtype=bool)
                for a2, b2 in (((slice(None), slice(1, None)), (slice(None), slice(None, -1))),
                               ((slice(1, None), slice(None)), (slice(None, -1), slice(None)))):
                    diff = gk[a2] != gk[b2]
                    edge[a2] |= diff
                    edge[b2] |= diff
                if SS > 1:
                    edge = _dilate(edge)
                img[edge] = 0.55 * img[edge] + 0.45 * np.asarray(LINE)

    big = Image.fromarray((img * 255 + 0.5).astype(np.uint8))
    arrows = []
    for a in (overlay.arrows if overlay else []):
        at = np.asarray(a["at"], dtype=np.float64)
        tail = at - _unit(a["dir"]) * 0.25 * extent
        arrows.append((at, tail, a.get("label") or ""))
    if arrows:
        dr = ImageDraw.Draw(big)
        for at, tail, _ in arrows:
            x, y, d = project([at, tail], cam, Ws, Hs)
            if (d <= near).any():
                continue
            a2, t2 = np.array([x[0], y[0]]), np.array([x[1], y[1]])
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

    parts = prep.parts
    report = {"labeled": [], "small": [], "hidden": [], "hidden_tagged": []}
    Kf = K[SS // 2::SS, SS // 2::SS][:H, :W]
    keys, counts = np.unique(Kf[Kf >= 0], return_counts=True)
    visible = {(int(k >> 32), int(k & 0xFFFFFFFF)): int(cnt) for k, cnt in zip(keys, counts)}
    share = np.bincount((keys >> 32).astype(np.int64), weights=counts, minlength=len(parts)) / float(W * H)
    report["parts"] = {part_path(parts[i]): round(float(share[i]), 4) for i in np.argsort(-share) if share[i] > 0}
    report["covered"] = round(float(share.sum()), 4)
    if labels or overlay is not None:
        names = {}
        multi = len(parts) > 1
        for i, p in enumerate(parts):
            for fid in range(max(p.faces, 0)):
                names[(i, fid)] = (f"{p.name}:" if multi else "") + f"f{fid}"
        hid = hidden_parts(prep, hidden)
        marked = set(overlay.faces) if overlay is not None else set()
        for k in names:
            if k not in visible and k[0] not in hid:
                report["hidden"].append(names[k])
                tags = (parts[k[0]].tags or {}).get(k[1])
                if tags or k in marked:
                    report["hidden_tagged"].append(names[k] + (" " + ",".join(tags) if tags else ""))
        draw = ImageDraw.Draw(im)
        font = _font(max(11, round(W / 85)))
        boxes = _labels(draw, font, parts, Kf, visible, names, overlay, labels, report)
        for at, tail, text in arrows:
            x, y, d = project([tail], cam, W, H)
            if text and d[0] > near:
                _tag_box(draw, font, text, (x[0], y[0]), boxes, W, H, fill=(255, 236, 230), outline=(200, 40, 20))
    return im, report
