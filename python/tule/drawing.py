"""2D engineering drawings made from the model: projected views, dimensions, a title block.

```python
from tule import *
outputs.drawing("bracket-drawing", part, info={"material": "S235, 2 mm"},
                dims=[("top", (0, 0, 0), (10, 10, 0), {"along": "h"})], dxf="top")
```

writes ``outputs/bracket-drawing/bracket-drawing.pdf`` and ``.svg`` (and a
``.dxf`` of the top view at 1:1 in mm). Views come from OCCT's hidden-line
removal (visible edges solid, hidden ones dashed), placed by ISO first-angle
(default) or third-angle projection, with an isometric view in a free spot.
Overall sizes and hole diameters are dimensioned automatically; ``dims`` adds
the ones only the author knows. The scale is the largest standard one that
fits the sheet. Output is deterministic (no dates), so a rebuild that changes
nothing writes the same bytes.
"""

from __future__ import annotations

import math
import os
import zlib
from typing import Union

import numpy as np
from OCP.BRep import BRep_Tool
from OCP.BRepAdaptor import BRepAdaptor_Curve, BRepAdaptor_Surface
from OCP.BRepLib import BRepLib
from OCP.BRepLProp import BRepLProp_SLProps
from OCP.BRepMesh import BRepMesh_IncrementalMesh
from OCP.BRepTools import BRepTools
from OCP.GCPnts import GCPnts_TangentialDeflection
from OCP.GeomAbs import GeomAbs_Circle, GeomAbs_Cylinder, GeomAbs_Line
from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt
from OCP.HLRAlgo import HLRAlgo_Projector
from OCP.HLRBRep import HLRBRep_Algo, HLRBRep_HLRToShape, HLRBRep_PolyAlgo, HLRBRep_PolyHLRToShape
from OCP.TopAbs import TopAbs_REVERSED

from ._occ import ShapeSet, explore, static
from .assembly import Assembly
from .errors import TuleError
from .io import write_atomic
from .selection import Selection
from .shape import Shape

# view name: (direction toward the viewer, sheet x direction); sheet y = direction × x
VIEWS = {
    "front": ((0, -1, 0), (1, 0, 0)),
    "back": ((0, 1, 0), (-1, 0, 0)),
    "top": ((0, 0, 1), (1, 0, 0)),
    "bottom": ((0, 0, -1), (1, 0, 0)),
    "right": ((1, 0, 0), (0, 1, 0)),
    "left": ((-1, 0, 0), (0, -1, 0)),
    "iso": ((1, -1, 1), (1, 1, 0)),
}
ORTHO = ("front", "top", "right", "left", "bottom", "back")
# (column, row) of each view, row 0 at the top
GRID = {
    "first": {"front": (1, 1), "top": (1, 2), "bottom": (1, 0), "right": (0, 1), "left": (2, 1), "back": (3, 1)},
    "third": {"front": (1, 1), "top": (1, 0), "bottom": (1, 2), "right": (2, 1), "left": (0, 1), "back": (3, 1)},
}
SHEETS = {"A4": (297, 210), "A3": (420, 297), "A2": (594, 420), "A1": (841, 594), "A0": (1189, 841)}
SCALES = [50, 20, 10, 5, 2, 1, 1 / 2, 1 / 5, 1 / 10, 1 / 20, 1 / 50, 1 / 100, 1 / 200, 1 / 500, 1 / 1000]

# line styles: width (mm), dash pattern (mm)
STYLES = {
    "vis": (0.35, ()),
    "hid": (0.18, (2.0, 1.0)),
    "thin": (0.18, ()),
    "center": (0.18, (6.0, 1.0, 0.5, 1.0)),
    "frame": (0.5, ()),
}
TEXT = 3.5      # dimension text size (mm)
MARGIN = 10     # sheet edge to frame
TB_W = 120      # title block width
FIRST = 8.0     # geometry to the first dimension line
STEP = 7.0      # between stacked dimension lines
ARROW = (3.0, 0.5)  # arrow length, half width
GAP = 5.0       # between views (beyond their annotations)
EXACT_FACES = 1500  # above this, hidden lines come from a mesh (HLRBRep_PolyAlgo): faster, polygonal

# Helvetica advance widths (1/1000 em) for ASCII 32..126, and a few more
_HW = [278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278] + [556] * 10 + [
    278, 278, 584, 584, 584, 556, 1015, 667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556, 833, 722,
    778, 667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 278, 278, 278, 469, 556, 333, 556, 556, 500,
    556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556, 556, 556, 333, 500, 278, 556, 500, 722, 500,
    500, 500, 334, 260, 334, 584]
_WIDTH = {chr(32 + i): w for i, w in enumerate(_HW)}
_WIDTH.update({"⌀": 778, "Ø": 778, "×": 584, "°": 400, "±": 584, "…": 1000, "µ": 556})
_WINANSI = {"⌀": 0xD8, "×": 0xD7, "°": 0xB0, "±": 0xB1, "…": 0x85, "µ": 0xB5, "–": 0x96, "—": 0x97}


def text_width(s: str, size: float = TEXT, bold: bool = False) -> float:
    return sum(_WIDTH.get(ch, 556) for ch in s) * size / 1000 * (1.06 if bold else 1.0)


def _fmt(v: float) -> str:
    """A measured value: at most two decimals, no trailing zeros."""
    v = round(float(v), 2)
    s = f"{v:.2f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def _n(v: float) -> str:
    """A coordinate in a file: three decimals at most."""
    s = f"{v:.3f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def _unit(v) -> np.ndarray:
    v = np.asarray(v, dtype=float)
    return v / np.linalg.norm(v)


# ---------------------------------------------------------------- projection


class _View:
    """One projection direction and the 2D primitives seen along it.

    Primitives are tuples in view coordinates (model mm): ``("line", x0, y0, x1, y1)``,
    ``("arc", cx, cy, r, a0, a1)`` (degrees, counterclockwise), ``("circle", cx, cy, r)``,
    ``("poly", ((x, y), ...))``.
    """

    def __init__(self, name: str):
        if name not in VIEWS:
            raise TuleError(f"unknown view {name!r}", op="drawing", hint=", ".join(VIEWS))
        d, x = VIEWS[name]
        self.name = name
        self.d = _unit(d)
        self.x = _unit(x)
        self.y = np.cross(self.d, self.x)
        self.vis: list = []
        self.hid: list = []

    def p(self, pt) -> tuple[float, float]:
        pt = np.asarray(pt, dtype=float)
        return float(pt @ self.x), float(pt @ self.y)

    def bbox(self) -> tuple[float, float, float, float]:
        P = _points(self.vis + self.hid)
        if not len(P):
            return (0.0, 0.0, 0.0, 0.0)
        return (float(P[:, 0].min()), float(P[:, 1].min()), float(P[:, 0].max()), float(P[:, 1].max()))


def _arc_points(cx, cy, r, a0, a1):
    sweep = (a1 - a0) % 360 or 360
    pts = [(cx + r * math.cos(math.radians(a)), cy + r * math.sin(math.radians(a))) for a in (a0, a0 + sweep)]
    k = math.ceil(a0 / 90) * 90
    while k < a0 + sweep:
        pts.append((cx + r * math.cos(math.radians(k)), cy + r * math.sin(math.radians(k))))
        k += 90
    return pts


def _points(prims) -> np.ndarray:
    """Endpoints and extreme points of primitives (for bounds and extension lines)."""
    out = []
    for p in prims:
        if p[0] == "line":
            out += [(p[1], p[2]), (p[3], p[4])]
        elif p[0] == "circle":
            _, cx, cy, r = p
            out += [(cx - r, cy), (cx + r, cy), (cx, cy - r), (cx, cy + r)]
        elif p[0] == "arc":
            out += _arc_points(*p[1:])
        else:
            out += list(p[1])
    return np.asarray(out, dtype=float).reshape(-1, 2)


def _edge_prims(e, tol: float) -> list:
    try:
        a = BRepAdaptor_Curve(e)
    except Exception:  # pragma: no cover - edges without curves
        return []
    t0, t1 = a.FirstParameter(), a.LastParameter()

    def P(t):
        q = a.Value(t)
        return (q.X(), q.Y())

    typ = a.GetType()
    if typ == GeomAbs_Line:
        (x0, y0), (x1, y1) = P(t0), P(t1)
        return [("line", x0, y0, x1, y1)] if math.hypot(x1 - x0, y1 - y0) > tol else []
    if typ == GeomAbs_Circle:
        c = a.Circle()
        cx, cy, r = c.Location().X(), c.Location().Y(), c.Radius()
        p0, pm, p1 = P(t0), P((t0 + t1) / 2), P(t1)
        if math.dist(p0, p1) < tol and t1 - t0 > math.pi:
            return [("circle", cx, cy, r)]
        ang = [math.degrees(math.atan2(q[1] - cy, q[0] - cx)) % 360 for q in (p0, pm, p1)]
        if (ang[1] - ang[0]) % 360 <= (ang[2] - ang[0]) % 360:
            return [("arc", cx, cy, r, ang[0], ang[2])]
        return [("arc", cx, cy, r, ang[2], ang[0])]
    try:
        g = GCPnts_TangentialDeflection(a, 0.1, tol * 5)
        pts = [P(g.Parameter(i)) for i in range(1, g.NbPoints() + 1)]
    except Exception:  # pragma: no cover
        pts = [P(t0 + (t1 - t0) * i / 32) for i in range(33)]
    A = np.asarray(pts)
    far = A[np.argmax(np.linalg.norm(A - A[0], axis=1))]
    d = far - A[0]
    L = float(np.linalg.norm(d))
    if L < tol:
        return []
    u = d / L
    off = (A - A[0]) @ np.array([-u[1], u[0]])
    if np.abs(off).max() < tol:  # a curve seen edge-on (a hole's rim from the side): a line
        s = (A - A[0]) @ u
        q0, q1 = A[0] + s.min() * u, A[0] + s.max() * u
        return [("line", float(q0[0]), float(q0[1]), float(q1[0]), float(q1[1]))]
    return [("poly", tuple((float(x), float(y)) for x, y in pts))]


def _compound_prims(c, tol: float) -> list:
    if c is None or c.IsNull():
        return []
    static(BRepLib, "BuildCurves3d")(c)
    out = []
    for e in explore(c, "edge"):
        out += _edge_prims(e, tol)
    return out


def _hlr(shape, view: _View, hidden: bool, tol: float) -> None:
    """Fill ``view.vis`` and ``view.hid`` with OCCT hidden-line removal (exact; polygonal for big parts)."""
    proj = HLRAlgo_Projector(gp_Ax2(gp_Pnt(0, 0, 0), gp_Dir(*view.d), gp_Dir(*view.x)))
    nfaces = sum(1 for _ in explore(shape, "face"))
    ts = None
    if nfaces <= EXACT_FACES:
        try:
            algo = HLRBRep_Algo()
            algo.Add(shape, 0)
            algo.Projector(proj)
            algo.Update()
            algo.Hide()
            ts = HLRBRep_HLRToShape(algo)
        except Exception:  # pragma: no cover - fall back to the polygonal algorithm
            ts = None
    if ts is None:
        BRepMesh_IncrementalMesh(shape, tol * 20, False, 0.3, True)
        algo = HLRBRep_PolyAlgo()
        algo.Load(shape)
        algo.Projector(proj)
        algo.Update()
        ts = HLRBRep_PolyHLRToShape()
        ts.Update(algo)
    vis = []
    for c in (ts.VCompound(), ts.Rg1LineVCompound(), ts.OutLineVCompound()):
        vis += _compound_prims(c, tol)
    hid = []
    if hidden:
        for c in (ts.HCompound(), ts.OutLineHCompound()):
            hid += _compound_prims(c, tol)
    view.vis, view.hid = _clean(vis, hid, tol)


# -- merging coincident primitives and removing hidden lines that lie under visible ones


def _union(iv, tol):
    out = []
    for a, b in sorted(iv):
        if out and a <= out[-1][1] + tol:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def _subtract(iv, cut, tol):
    out = []
    for a, b in iv:
        pieces = [[a, b]]
        for c0, c1 in cut:
            nxt = []
            for p0, p1 in pieces:
                if c1 <= p0 + tol or c0 >= p1 - tol:
                    nxt.append([p0, p1])
                    continue
                if c0 > p0 + tol:
                    nxt.append([p0, c0])
                if c1 < p1 - tol:
                    nxt.append([c1, p1])
            pieces = nxt
        out += [p for p in pieces if p[1] - p[0] > tol]
    return out


def _line_groups(prims, tol):
    g: dict = {}
    for _, x0, y0, x1, y1 in prims:
        L = math.hypot(x1 - x0, y1 - y0)
        ux, uy = (x1 - x0) / L, (y1 - y0) / L
        if ux < -1e-9 or (abs(ux) <= 1e-9 and uy < 0):
            ux, uy = -ux, -uy
        off = -uy * x0 + ux * y0
        key = (round(math.atan2(uy, ux), 5), round(off / tol))
        s0, s1 = sorted((ux * x0 + uy * y0, ux * x1 + uy * y1))
        e = g.setdefault(key, [(ux, uy, off), []])
        e[1].append((s0, s1))
    for e in g.values():
        e[1] = _union(e[1], tol)
    return g


def _arc_groups(prims, tol):
    g: dict = {}
    for p in prims:
        if p[0] == "circle":
            _, cx, cy, r = p
            iv = [(0.0, 360.0)]
        else:
            _, cx, cy, r, a0, a1 = p
            a0 %= 360
            sw = (a1 - a0) % 360 or 360
            iv = [(a0, a0 + sw)] if a0 + sw <= 360 else [(a0, 360.0), (0.0, a0 + sw - 360)]
        key = (round(cx / tol), round(cy / tol), round(r / tol))
        e = g.setdefault(key, [(cx, cy, r), []])
        e[1] += iv
    atol = math.degrees(tol / max(tol, min(v[0][2] for v in g.values()))) if g else 0.01
    for e in g.values():
        e[1] = _union(e[1], min(atol, 0.5))
    return g


def _clean(vis, hid, tol):
    def split(prims):
        return ([p for p in prims if p[0] == "line"], [p for p in prims if p[0] in ("arc", "circle")],
                [p for p in prims if p[0] == "poly"])

    lv, av, pv = split(vis)
    lh, ah, ph = split(hid)
    LV, LH = _line_groups(lv, tol), _line_groups(lh, tol)
    AV, AH = _arc_groups(av, tol), _arc_groups(ah, tol)
    for k, e in LH.items():
        if k in LV:
            e[1] = _subtract(e[1], LV[k][1], tol)
    for k, e in AH.items():
        if k in AV:
            e[1] = _subtract(e[1], AV[k][1], 0.01)

    def lines(G):
        out = []
        for (ux, uy, off), iv in G.values():
            for a, b in iv:
                out.append(("line", a * ux - off * uy, a * uy + off * ux, b * ux - off * uy, b * uy + off * ux))
        return out

    def arcs(G):
        out = []
        for (cx, cy, r), iv in G.values():
            if len(iv) == 1 and iv[0][1] - iv[0][0] >= 359.99:
                out.append(("circle", cx, cy, r))
                continue
            if len(iv) >= 2 and iv[0][0] <= 1e-6 and iv[-1][1] >= 360 - 1e-6:  # joined across 0°
                iv = [[iv[-1][0], iv[0][1] + 360]] + iv[1:-1]
            out += [("arc", cx, cy, r, a % 360, b % 360 if b % 360 else 360.0) for a, b in iv if b - a > 0.01]
        return out

    def dedupe(ps):
        seen, out = set(), []
        for p in ps:
            k = tuple(round(v, 4) for pt in p[1] for v in pt)
            if k not in seen:
                seen.add(k)
                out.append(p)
        return out

    return lines(LV) + arcs(AV) + dedupe(pv), lines(LH) + arcs(AH) + dedupe(ph)


# ---------------------------------------------------------------- cylinders (holes, bosses)


class _Cyl:
    """A cylindrical feature: all faces on one axis with one radius."""

    def __init__(self, o, d, r, t0, t1, sweep, concave):
        self.o, self.d, self.r = o, d, r
        self.t0, self.t1, self.sweep, self.concave = t0, t1, sweep, concave
        self.capped = False

    @property
    def full(self) -> bool:
        return self.sweep >= 359.0

    @property
    def depth(self) -> float:
        return self.t1 - self.t0


def _cylinders(shape, tol: float) -> list[_Cyl]:
    """Full and partial cylinders; a concave one is a hole, ``capped`` when it has a bottom (blind, counterbore)."""
    faces = list(ShapeSet(explore(shape, "face")))
    edges = ShapeSet()
    efaces: list[list] = []
    for f in faces:
        for e in explore(f, "edge"):
            i = edges.index(e)
            if i < 0:
                edges.add(e)
                efaces.append([f])
            else:
                efaces[i].append(f)
    groups: dict = {}
    for f in faces:
        s = BRepAdaptor_Surface(f)
        if s.GetType() != GeomAbs_Cylinder:
            continue
        cyl = s.Cylinder()
        ax = cyl.Axis()
        o = np.array([ax.Location().X(), ax.Location().Y(), ax.Location().Z()])
        dz = np.array([ax.Direction().X(), ax.Direction().Y(), ax.Direction().Z()])
        r = cyl.Radius()
        u0, u1, v0, v1 = static(BRepTools, "UVBounds")(f)
        props = BRepLProp_SLProps(s, (u0 + u1) / 2, (v0 + v1) / 2, 1, 1e-7)
        if not props.IsNormalDefined():
            continue
        n = props.Normal()
        nv = np.array([n.X(), n.Y(), n.Z()]) * (-1 if f.Orientation() == TopAbs_REVERSED else 1)
        q = props.Value()
        pv = np.array([q.X(), q.Y(), q.Z()])
        radial = (pv - o) - ((pv - o) @ dz) * dz
        concave = bool(nv @ radial < 0)
        k = int(np.argmax(np.abs(dz)))
        sign = 1.0 if dz[k] > 0 else -1.0
        d = dz * sign
        ts = sorted(o @ d + v * sign for v in (v0, v1))
        op = o - (o @ d) * d
        key = (tuple(np.round(d, 5)), tuple(np.round(op / tol)), round(r / tol), concave)
        c = groups.get(key)
        if c is None:
            c = groups[key] = _Cyl(op, d, r, ts[0], ts[1], 0.0, concave)
        c.t0, c.t1 = min(c.t0, ts[0]), max(c.t1, ts[1])
        c.sweep += math.degrees(u1 - u0)
        if concave and not c.capped:
            c.capped = _has_bottom(f, edges, efaces, op, d, r, tol)
    return list(groups.values())


def _has_bottom(f, edges, efaces, o, d, r, tol) -> bool:
    for e in explore(f, "edge"):
        if BRepAdaptor_Curve(e).GetType() != GeomAbs_Circle:
            continue
        i = edges.index(e)
        for g in efaces[i] if i >= 0 else ():
            if g.IsSame(f) or BRepAdaptor_Surface(g).GetType() == GeomAbs_Cylinder:
                continue
            far = 0.0
            for v in explore(g, "vertex"):
                p = static(BRep_Tool, "Pnt")(v)
                w = np.array([p.X(), p.Y(), p.Z()]) - o
                far = max(far, float(np.linalg.norm(w - (w @ d) * d)))
            if far <= r + tol * 10:
                return True
    return False


# ---------------------------------------------------------------- canvas


class _Canvas:
    """Drawing operations in sheet millimetres (y up)."""

    def __init__(self):
        self.ops: list = []

    def path(self, style, cmds, phase=0.0):
        if cmds:
            self.ops.append(("path", style, tuple(cmds), phase))

    def line(self, style, p, q, phase=0.0):
        self.path(style, [("M", p[0], p[1]), ("L", q[0], q[1])], phase)

    def center_line(self, p, q):
        L = math.dist(p, q)
        if L < 12:  # too short for a chain line
            self.line("thin", p, q)
        else:  # a long dash in the middle
            self.line("center", p, q, (3.0 - L / 2) % 8.5)

    def arrow(self, tip, d):
        d = _unit(d)
        n = np.array([-d[1], d[0]])
        b = np.asarray(tip) - ARROW[0] * d
        self.ops.append(("fill", (tuple(tip), tuple(b + ARROW[1] * n), tuple(b - ARROW[1] * n))))

    def text(self, x, y, s, size=TEXT, anchor="start", angle=0.0, bold=False):
        self.ops.append(("text", float(x), float(y), s, size, anchor, float(angle), bold))

    def rect(self, style, x0, y0, x1, y1):
        self.path(style, [("M", x0, y0), ("L", x1, y0), ("L", x1, y1), ("L", x0, y1), ("Z",)])

    def add(self, other: "_Canvas", dx=0.0, dy=0.0):
        self.ops += [_moved(op, dx, dy) for op in other.ops]

    def bbox(self):
        return _bbox(self.ops)


def _moved(op, dx, dy):
    if op[0] == "path":
        cmds = tuple((c[0], *[v + (dy if i % 2 else dx) for i, v in enumerate(c[1:])]) for c in op[2])
        return ("path", op[1], cmds, op[3])
    if op[0] == "fill":
        return ("fill", tuple((x + dx, y + dy) for x, y in op[1]))
    return ("text", op[1] + dx, op[2] + dy, *op[3:])


def _text_corners(op):
    _, x, y, s, size, anchor, angle, bold = op
    w = text_width(s, size, bold)
    x0 = {"start": 0.0, "middle": -w / 2, "end": -w}[anchor]
    c, sn = math.cos(math.radians(angle)), math.sin(math.radians(angle))
    return [(x + u * c - v * sn, y + u * sn + v * c) for u in (x0, x0 + w) for v in (-0.22 * size, 0.75 * size)]


def _bbox(ops):
    xs, ys = [], []
    for op in ops:
        if op[0] == "path":
            for c in op[2]:
                xs += c[1::2]
                ys += c[2::2]
        elif op[0] == "fill":
            xs += [p[0] for p in op[1]]
            ys += [p[1] for p in op[1]]
        else:
            for px, py in _text_corners(op):
                xs.append(px)
                ys.append(py)
    if not xs:
        return (0.0, 0.0, 0.0, 0.0)
    return (min(xs), min(ys), max(xs), max(ys))


def _prim_cmds(p, s, dx=0.0, dy=0.0):
    def P(x, y):
        return (x * s + dx, y * s + dy)

    if p[0] == "line":
        return [("M", *P(p[1], p[2])), ("L", *P(p[3], p[4]))]
    if p[0] == "poly":
        pts = [P(*q) for q in p[1]]
        return [("M", *pts[0])] + [("L", *q) for q in pts[1:]]
    if p[0] == "circle":
        _, cx, cy, r = p
        a0, sweep = 0.0, 360.0
    else:
        _, cx, cy, r, a0, a1 = p
        sweep = (a1 - a0) % 360 or 360
    n = max(1, math.ceil(sweep / 90 - 1e-9))
    h = math.radians(sweep / n)
    k = 4 / 3 * math.tan(h / 4)
    t = math.radians(a0)
    cmds = [("M", *P(cx + r * math.cos(t), cy + r * math.sin(t)))]
    for _ in range(n):
        t1 = t + h
        c1 = P(cx + r * (math.cos(t) - k * math.sin(t)), cy + r * (math.sin(t) + k * math.cos(t)))
        c2 = P(cx + r * (math.cos(t1) + k * math.sin(t1)), cy + r * (math.sin(t1) - k * math.cos(t1)))
        cmds.append(("C", *c1, *c2, *P(cx + r * math.cos(t1), cy + r * math.sin(t1))))
        t = t1
    if p[0] == "circle":
        cmds.append(("Z",))
    return cmds


# ---------------------------------------------------------------- dimensions


class _Dim:
    """A linear dimension between two view points (model mm), measured along ``u`` (view coordinates)."""

    def __init__(self, a, b, along, text=None, offset=None, side=None):
        self.a, self.b = np.asarray(a, float), np.asarray(b, float)
        if along == "h":
            self.u = np.array([1.0, 0.0])
        elif along == "v":
            self.u = np.array([0.0, 1.0])
        else:
            d = self.b - self.a
            if np.linalg.norm(d) < 1e-9:
                raise TuleError("the two points of a dimension coincide in this view", op="drawing",
                                hint="pick points apart in this view, or another view")
            self.u = _unit(d)
        self.along = along
        self.value = abs(float((self.b - self.a) @ self.u))
        self.text = text if text is not None else _fmt(self.value)
        self.offset = offset
        self.side = side  # "top", "bottom", "left", "right"; None: aligned, placed by offset

    @property
    def n(self):
        return np.array([-self.u[1], self.u[0]])


def _draw_dim(c: _Canvas, dim: _Dim, s: float, L: float):
    """Draw ``dim`` (scale ``s``) with its dimension line where ``point · n == L`` (paper mm)."""
    u, n = dim.u, dim.n
    a, b = dim.a * s, dim.b * s
    if (b - a) @ u < 0:
        a, b = b, a
    A, B = a + (L - a @ n) * n, b + (L - b @ n) * n
    for p, P in ((a, A), (b, B)):
        dist = L - p @ n
        if abs(dist) > 1.5:
            sg = 1.0 if dist > 0 else -1.0
            c.line("thin", p + sg * 1.0 * n, P + sg * 2.0 * n)
    t = u if (u[0] > 1e-9 or (abs(u[0]) <= 1e-9 and u[1] > 0)) else -u
    up = np.array([-t[1], t[0]])
    angle = math.degrees(math.atan2(t[1], t[0]))
    span = float(np.linalg.norm(B - A))
    w = text_width(dim.text)
    if span >= w + 2 * ARROW[0] + 3:
        c.line("thin", A, B)
        c.arrow(A, A - B)
        c.arrow(B, B - A)
        m = (A + B) / 2 + 1.0 * up
        c.text(m[0], m[1], dim.text, anchor="middle", angle=angle)
    else:  # too short: arrows from outside, text beyond the far end
        d = (B - A) / span if span > 1e-9 else u
        c.line("thin", A - 5 * d, B + 5 * d)
        c.arrow(A, d)
        c.arrow(B, -d)
        E = B if (B - A) @ t >= 0 else A
        q = E + 6 * t + 1.0 * up
        c.text(q[0], q[1], dim.text, anchor="start", angle=angle)


def _dim_interval(dim: _Dim, s: float):
    """The stretch of the side a dimension occupies (for stacking)."""
    lo, hi = sorted((dim.a @ dim.u * s, dim.b @ dim.u * s))
    w = text_width(dim.text)
    if hi - lo < w + 2 * ARROW[0] + 3:
        hi += 6 + w + 5
        lo -= 5
    return lo - 2, hi + 2


# ---------------------------------------------------------------- one view, annotated


SIDES = {"top": (np.array([0.0, 1.0]), "h"), "bottom": (np.array([0.0, -1.0]), "h"),
         "right": (np.array([1.0, 0.0]), "v"), "left": (np.array([-1.0, 0.0]), "v")}


def _side_edge(G, side):
    return {"top": G[3], "bottom": -G[1], "right": G[2], "left": -G[0]}[side]


def _annotate(view: _View, s: float, dims: list, callouts: list, centers: list, callout_side: str) -> _Canvas:
    """Geometry, center lines, dimensions and callouts of one view at scale ``s`` (view-local sheet mm)."""
    c = _Canvas()
    for p in view.hid:
        c.path("hid", _prim_cmds(p, s))
    for p in view.vis:
        c.path("vis", _prim_cmds(p, s))
    for p, q in centers:
        c.center_line(np.asarray(p) * s, np.asarray(q) * s)
    bb = view.bbox()
    G = (bb[0] * s, bb[1] * s, bb[2] * s, bb[3] * s)
    # dimensions: stacked outward per side, shorter ones closer
    reach = {k: 0.0 for k in SIDES}
    for side, (out, _) in SIDES.items():
        mine = sorted([d for d in dims if d.side == side], key=lambda d: (d.value, d.text))
        tiers: list[list] = []
        for d in mine:
            iv = _dim_interval(d, s)
            t = next((i for i, row in enumerate(tiers) if all(iv[1] <= a or iv[0] >= b for a, b in row)), len(tiers))
            if t == len(tiers):
                tiers.append([])
            tiers[t].append(iv)
            dist = _side_edge(G, side) + FIRST + STEP * t  # along `out`
            n = d.n
            L = dist * float(out @ n)  # the line's coordinate along the dimension's own normal
            _draw_dim(c, d, s, L)
        if tiers:
            reach[side] = FIRST + STEP * (len(tiers) - 1) + TEXT
    for d in dims:
        if d.side is None:
            a = d.a * s
            _draw_dim(c, d, s, float(a @ d.n) + d.offset)
    # callouts: text beyond the dimensions on one side, a leader to the circle
    if callouts:
        _draw_callouts(c, G, callouts, s, callout_side, reach[callout_side])
    return c


def _draw_callouts(c: _Canvas, G, callouts, s, side, reach):
    """Callout texts beyond the dimensions on ``side``, each with a slanted leader and an arrow on its circle."""
    d0 = reach + 6.0
    mid = TEXT * 0.36  # half the height of digits
    items = [(text, np.array([cx, cy]) * s, r * s) for text, cx, cy, r in callouts]

    def leader(text, C, r, x, ym, anchor):
        sg = 1.0 if anchor == "start" else -1.0
        S = np.array([x - sg * 4.0, ym])
        c.line("thin", S, (x - sg * 1.0, ym))
        Q = C + r * _unit(S - C)
        c.line("thin", S, Q)
        c.arrow(Q, Q - S)
        c.text(x, ym - mid, text, anchor=anchor)

    if side in ("left", "right"):
        # up and away from each circle, so the leader does not run along its center line
        items.sort(key=lambda it: (-(it[1][1] + 0.7 * it[2]), it[0]))
        x = G[2] + d0 if side == "right" else G[0] - d0
        last = None
        for text, C, r in items:
            ym = C[1] + 0.7 * r + 4.0
            if last is not None:
                ym = min(ym, last - 5.5)
            last = ym
            leader(text, C, r, x, ym, "start" if side == "right" else "end")
    else:
        items.sort(key=lambda it: (it[1][0] + 0.7 * it[2], it[0]))
        ym = G[3] + d0 + mid if side == "top" else G[1] - d0 - mid
        end = -1e9
        for text, C, r in items:
            x = max(C[0] + 0.7 * r + 7.0, end + 8.0)
            end = x + text_width(text)
            leader(text, C, r, x, ym, "start")


# ---------------------------------------------------------------- what to dimension


def _as_shape(item) -> tuple[Shape, str | None]:
    if isinstance(item, (list, tuple)):
        item = Assembly(*item)
    if isinstance(item, Assembly):
        return item.to_shape(), item.name
    if isinstance(item, Shape):
        return item, item.name
    raise TuleError(f"cannot draw a {type(item).__name__}", op="drawing", hint="pass a Shape or an Assembly")


def _point(p) -> np.ndarray:
    if isinstance(p, Selection):
        if len(p.items) != 1:
            raise TuleError(f"a dimension point is one vertex, edge or face, got {len(p.items)}", op="drawing",
                            hint="narrow the selection (.nearest(point)) or give the point (x, y, z)")
        x = p.items[0]
        if p.kind == "edge":
            a = BRepAdaptor_Curve(x)
            if a.GetType() == GeomAbs_Circle:  # a hole's rim: its center
                q = a.Circle().Location()
                return np.array([q.X(), q.Y(), q.Z()])
        return np.asarray(p._center(x), dtype=float)
    a = np.asarray(p, dtype=float).reshape(-1)
    if a.shape != (3,):
        raise TuleError(f"a dimension point is (x, y, z), got {p!r}", op="drawing")
    return a


def _is_point(p) -> bool:
    if isinstance(p, Selection):
        return p.kind != "edge" or len(p.items) == 1 and BRepAdaptor_Curve(p.items[0]).GetType() != GeomAbs_Circle
    try:
        return np.asarray(p, dtype=float).reshape(-1).shape == (3,)
    except (TypeError, ValueError):
        return False


def _parse_dims(dims, views: dict, tol: float):
    """Agent dimensions: ``(view, p1, p2[, text or {text, along, offset}])`` or ``(view, circle edges[, text])``."""
    linear: dict = {k: [] for k in views}
    circles: dict = {k: [] for k in views}
    for spec in dims or ():
        if not isinstance(spec, (list, tuple)) or len(spec) < 2:
            raise TuleError(f"a dimension is (view, p1, p2) or (view, circle edges), got {spec!r}", op="drawing")
        vname = spec[0]
        if vname not in views or vname == "iso":
            raise TuleError(f"dimension in view {vname!r}, which is not drawn", op="drawing",
                            hint=f"views drawn: {', '.join(k for k in views if k != 'iso')}")
        v = views[vname]
        if isinstance(spec[1], Selection) and spec[1].kind == "edge" and (len(spec) == 2 or not _is_point(spec[2])):
            opts = spec[2] if len(spec) > 2 else None
            text = opts.get("text") if isinstance(opts, dict) else opts
            found: dict = {}
            for e in spec[1].items:
                a = BRepAdaptor_Curve(e)
                if a.GetType() != GeomAbs_Circle:
                    raise TuleError("a diameter dimension needs circular edges", op="drawing",
                                    hint="part.edges.circles(r=3)")
                ci = a.Circle()
                ax = ci.Axis().Direction()
                if abs(np.array([ax.X(), ax.Y(), ax.Z()]) @ v.d) < 0.999:
                    raise TuleError(f"this circle is not seen as a circle in the {vname} view", op="drawing",
                                    hint="dimension it in the view that looks along its axis")
                q = ci.Location()
                cx, cy = v.p((q.X(), q.Y(), q.Z()))
                full = a.IsClosed() or (a.LastParameter() - a.FirstParameter()) > 2 * math.pi - 1e-6
                key = (round(ci.Radius(), 4), full)
                found.setdefault(key, {})[(round(cx / tol), round(cy / tol))] = (cx, cy)
            for (r, full), cs in found.items():
                n = len(cs)
                t = text or ((f"{n}× " if n > 1 else "") + (f"⌀{_fmt(2 * r)}" if full else f"R{_fmt(r)}"))
                cx, cy = sorted(cs.values())[0]
                circles[vname].append([t, cx, cy, r, [(x, y, r) for x, y in cs.values()]])
            continue
        if len(spec) < 3:
            raise TuleError("a linear dimension needs two points", op="drawing", hint='("front", p1, p2)')
        opts = spec[3] if len(spec) > 3 else {}
        if isinstance(opts, str):
            opts = {"text": opts}
        if not isinstance(opts, dict) or set(opts) - {"text", "along", "offset"}:
            raise TuleError(f"dimension options are text, along and offset, got {opts!r}", op="drawing",
                            hint='("top", p1, p2, {"along": "h", "text": "40 h7", "offset": 12})')
        a, b = v.p(_point(spec[1])), v.p(_point(spec[2]))
        along = opts.get("along")
        if along is None:
            dx, dy = abs(a[0] - b[0]), abs(a[1] - b[1])
            along = "v" if dx < tol else "h" if dy < tol else "aligned"
        if along not in ("h", "v", "aligned"):
            raise TuleError(f"along is 'h', 'v' or 'aligned', got {along!r}", op="drawing")
        linear[vname].append((a, b, along, opts.get("text"), opts.get("offset")))
    return linear, circles


def _neighbors(name, grid) -> dict:
    c, r = grid[name]
    cells = {grid[k] for k in grid}
    return {"top": (c, r - 1) in cells, "bottom": (c, r + 1) in cells,
            "left": (c - 1, r) in cells, "right": (c + 1, r) in cells}


def _plan(views: dict, shape_cyls: list, linear: dict, circles: dict, grid: dict, hidden: bool, tol: float):
    """Decide every view's dimensions (in view coordinates), callouts, center lines and callout side."""
    names = [k for k in views if k != "iso"]
    order = sorted(names, key=ORTHO.index)
    plans = {k: {"dims": [], "callouts": [], "centers": []} for k in names}
    nb = {k: _neighbors(k, grid) for k in names}
    full = [cy for cy in shape_cyls if cy.full]
    # overall sizes: each model axis once, in the first view that shows it
    done_axes: set = set()
    done_cyl: set = set()
    for k in order:
        v = views[k]
        bb = v.bbox()
        P = np.concatenate([_points(v.vis), _points(v.hid)]) if v.vis or v.hid else np.zeros((0, 2))
        for axis_vec, along in ((v.x, "h"), (v.y, "v")):
            ax = int(np.argmax(np.abs(axis_vec)))
            if ax in done_axes or not len(P):
                continue
            done_axes.add(ax)
            i = 0 if along == "h" else 1
            lo, hi = (bb[0], bb[2]) if along == "h" else (bb[1], bb[3])
            if hi - lo < tol:
                continue
            text = None
            for j, cy in enumerate(full):  # a round part: ⌀ instead of a plain width
                if cy.concave or abs(cy.d @ axis_vec) > 1e-6:
                    continue
                m = cy.o @ axis_vec
                if abs(m - cy.r - lo) < tol * 10 and abs(m + cy.r - hi) < tol * 10:
                    text = "skip" if j in done_cyl else f"⌀{_fmt(2 * cy.r)}"
                    done_cyl.add(j)
                    break
            if text == "skip":
                continue
            # the side with short extension lines, away from neighboring views
            j = 1 - i
            perp = (bb[3] - bb[1]) if along == "h" else (bb[2] - bb[0])
            best = None
            for side in (("bottom", "top") if along == "h" else ("left", "right")):
                far = side in ("top", "right")
                ends = []
                for val in (lo, hi):
                    cand = P[np.abs(P[:, i] - val) < tol * 10]
                    ends.append(cand[(np.argmax if far else np.argmin)(cand[:, j])])
                edge = bb[3] if side == "top" else bb[1] if side == "bottom" else bb[2] if far else bb[0]
                cost = sum(abs(edge - q[j]) for q in ends) / max(perp, tol) + 0.5 * nb[k][side]
                if best is None or cost < best[0] - 1e-9:
                    best = (cost, side, ends)
            _, side, ends = best
            plans[k]["dims"].append(_Dim(ends[0], ends[1], along, text, side=side))
    # agent dimensions
    for k in names:
        v = views[k]
        bb = v.bbox()
        mid = np.array([(bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2])
        for a, b, along, text, offset in linear[k]:
            m = (np.asarray(a) + np.asarray(b)) / 2
            if along == "h":
                side = "top" if m[1] > mid[1] else "bottom"
            elif along == "v":
                side = "right" if m[0] > mid[0] else "left"
            else:
                side = None
            d = _Dim(a, b, along, text, side=side if offset is None else None)
            if offset is not None:
                d.offset = float(offset)
            elif side is None:
                d.offset = 8.0 if (m - mid) @ d.n >= 0 else -8.0
            plans[k]["dims"].append(d)
    # holes: "4× ⌀5.5" where they are seen as circles, preferring a view that shows them visible
    given = {round(c[3], 4) for k in names for c in circles[k]}
    groups: dict = {}
    for cy in full:
        if not cy.concave or round(cy.r, 4) in given:
            continue
        best = None
        for k in names:
            v = views[k]
            if abs(cy.d @ v.d) < 0.999:
                continue
            cx, cyy = v.p(cy.o)
            seen = any(p[0] in ("circle", "arc") and abs(p[1] - cx) < tol * 10 and abs(p[2] - cyy) < tol * 10
                       and abs(p[3] - cy.r) < tol * 10 for p in v.vis)
            if best is None or (seen and not best[1]):
                best = (k, seen, cx, cyy)
        if best is None:
            continue
        depth = round(cy.depth, 3) if cy.capped else None
        groups.setdefault((best[0], round(cy.r, 4), depth), []).append((best[2], best[3], cy.r))
    for (k, r, depth), cs in sorted(groups.items(), key=lambda kv: (names.index(kv[0][0]), kv[0][1], kv[0][2] or 0)):
        n = len(cs)
        text = (f"{n}× " if n > 1 else "") + f"⌀{_fmt(2 * r)}" + (f" depth {_fmt(depth)}" if depth else "")
        plans[k]["callouts"].append([text, None, None, r, cs])
    for k in names:
        plans[k]["callouts"] += circles[k]
    # callout side: near the circles, away from neighboring views and dimensions
    for k in names:
        used = {d.side for d in plans[k]["dims"]}
        bb = views[k].bbox()
        size = max(bb[2] - bb[0], bb[3] - bb[1], tol)
        edge = {"right": bb[2], "top": bb[3], "left": -bb[0], "bottom": -bb[1]}

        def cost(sd):
            out, _ = SIDES[sd]
            near = [min(edge[sd] - (q[0] * out[0] + q[1] * out[1]) for q in co[4]) for co in plans[k]["callouts"]]
            return (sum(near) / max(len(near), 1)) / size + 1.0 * nb[k][sd] + 0.6 * (sd in used)

        prefs = ["right", "bottom", "top", "left"]
        plans[k]["side"] = min(prefs, key=lambda sd: (round(cost(sd), 6), prefs.index(sd)))
        # point each callout at the circle nearest that side
        out, _ = SIDES[plans[k]["side"]]
        for co in plans[k]["callouts"]:
            cx, cy, r = max(co[4], key=lambda q: (q[0] * out[0] + q[1] * out[1], -q[0], -q[1]))
            co[1], co[2], co[3] = cx, cy, r
    # center lines: a cross on circles, the axis on holes and bosses seen from the side
    for k in names:
        v = views[k]
        marks: dict = {}
        axes: dict = {}
        for cy in full:
            if abs(cy.d @ v.d) > 0.999:
                cx, cyy = v.p(cy.o)
                key = (round(cx / tol), round(cyy / tol))
                marks[key] = (cx, cyy, max(cy.r, marks.get(key, (0, 0, 0))[2]))
            elif abs(cy.d @ v.d) < 1e-6 and (hidden or not cy.concave):
                p0, p1 = v.p(cy.o + cy.t0 * cy.d), v.p(cy.o + cy.t1 * cy.d)
                u = _unit(np.subtract(p1, p0))
                key = (round(u[0], 5), round(u[1], 5), round((-u[1] * p0[0] + u[0] * p0[1]) / tol))
                s0, s1 = float(np.dot(p0, u)), float(np.dot(p1, u))
                if key in axes:
                    a0, a1, _ = axes[key]
                    s0, s1 = min(s0, a0), max(s1, a1)
                axes[key] = (s0, s1, (u, -u[1] * p0[0] + u[0] * p0[1]))
        plans[k]["marks"] = sorted(marks.values())
        plans[k]["axes"] = sorted(((s0, s1, tuple(u), off) for s0, s1, (u, off) in axes.values()))
    return plans


def _centers(plan, s):
    """Center line segments in view coordinates, extended a fixed sheet distance."""
    out = []
    e = 2.5 / s
    for cx, cy, r in plan["marks"]:
        out.append(((cx - r - e, cy), (cx + r + e, cy)))
        out.append(((cx, cy - r - e), (cx, cy + r + e)))
    for s0, s1, u, off in plan["axes"]:
        u = np.asarray(u)
        n = np.array([-u[1], u[0]])
        out.append((tuple((s0 - e) * u + off * n), tuple((s1 + e) * u + off * n)))
    return out


# ---------------------------------------------------------------- sheet layout


def _scale_text(s: float) -> str:
    return f"{_fmt(s)}:1" if s > 1 else f"1:{_fmt(1 / s)}"


def _place(rects, frame, keep_out):
    """An offset putting all ``rects`` inside ``frame`` and off ``keep_out``, nearest centered; or None."""
    if not rects:
        return (0.0, 0.0)
    R = np.asarray(rects, dtype=float)
    U = (R[:, 0].min(), R[:, 1].min(), R[:, 2].max(), R[:, 3].max())
    lo_x, hi_x = frame[0] - U[0], frame[2] - U[2]
    lo_y, hi_y = frame[1] - U[1], frame[3] - U[3]
    if lo_x > hi_x + 1e-9 or lo_y > hi_y + 1e-9:
        return None
    xs = np.unique(np.concatenate([np.arange(lo_x, hi_x, 1.0), [hi_x, (lo_x + hi_x) / 2]]))
    ys = np.unique(np.concatenate([np.arange(lo_y, hi_y, 1.0), [hi_y, (lo_y + hi_y) / 2]]))
    X, Y = np.meshgrid(xs, ys)
    ok = np.ones_like(X, dtype=bool)
    k = keep_out
    for r in R:
        ok &= ~((r[0] + X < k[2]) & (r[2] + X > k[0]) & (r[1] + Y < k[3]) & (r[3] + Y > k[1]))
    if not ok.any():
        return None
    cost = (X - (lo_x + hi_x) / 2) ** 2 + (Y - (lo_y + hi_y) / 2) ** 2
    cost[~ok] = np.inf
    i = np.unravel_index(int(np.argmin(cost)), cost.shape)
    return float(X[i]), float(Y[i])


def _grid_block(canv: dict, grid: dict, s: float):
    """Place annotated views in their projection cells; returns view offsets, their rects and the empty cells."""
    names = list(canv)
    G = {k: canv[k][1] for k in names}  # geometry bbox (sheet mm, view-local)
    A = {k: canv[k][0].bbox() for k in names}
    A = {k: (min(A[k][0], G[k][0]), min(A[k][1], G[k][1]), max(A[k][2], G[k][2]), max(A[k][3], G[k][3])) for k in names}
    cols = sorted({grid[k][0] for k in names})
    rows = sorted({grid[k][1] for k in names})
    colx, roww = {}, {}
    for col in cols:
        ks = [k for k in names if grid[k][0] == col]
        x0, x1 = min(G[k][0] for k in ks), max(G[k][2] for k in ks)
        ml = max(G[k][0] - A[k][0] for k in ks)
        mr = max(A[k][2] - G[k][2] for k in ks)
        colx[col] = [x0, x1, ml, mr]
    for row in rows:
        ks = [k for k in names if grid[k][1] == row]
        y0, y1 = min(G[k][1] for k in ks), max(G[k][3] for k in ks)
        mb = max(G[k][1] - A[k][1] for k in ks)
        mt = max(A[k][3] - G[k][3] for k in ks)
        roww[row] = [y0, y1, mb, mt]
    left, x = {}, 0.0
    for col in cols:
        x0, x1, ml, mr = colx[col]
        left[col] = x
        x += ml + (x1 - x0) + mr + GAP
    top, y = {}, 0.0
    for row in rows:
        y0, y1, mb, mt = roww[row]
        top[row] = y
        y -= mt + (y1 - y0) + mb + GAP
    off, rects = {}, []
    for k in names:
        col, row = grid[k]
        ox = left[col] + colx[col][2] - colx[col][0]
        oy = top[row] - roww[row][3] - roww[row][1]
        off[k] = (ox, oy)
        a = A[k]
        rects.append((a[0] + ox, a[1] + oy, a[2] + ox, a[3] + oy))
    cells = []
    occupied = {grid[k] for k in names}
    for col in cols:
        for row in rows:
            if (col, row) not in occupied:
                x0, x1, ml, mr = colx[col]
                y0, y1, mb, mt = roww[row]
                cells.append((left[col], top[row] - (mt + y1 - y0 + mb), left[col] + ml + x1 - x0 + mr, top[row]))
    return off, rects, cells


def _iso_spots(rects, cells, w, h):
    """Where the isometric view may go, relative to the block, in order of preference."""
    spots = []
    if rects:
        U = (min(r[0] for r in rects), min(r[1] for r in rects), max(r[2] for r in rects), max(r[3] for r in rects))
        cx, cy = (U[0] + U[2]) / 2, (U[1] + U[3]) / 2
        for x0, y0, x1, y1 in cells:  # an empty cell, against its corner nearest the middle
            spots.append((x1 - w if (x0 + x1) / 2 < cx else x0, y1 - h if (y0 + y1) / 2 < cy else y0))
        spots += [(U[2] + GAP, U[3] - h), (U[2] + GAP, cy - h / 2), (U[2] + GAP, U[1]),
                  (U[0] - GAP - w, U[3] - h), (U[2] - w, U[1] - GAP - h), (U[0], U[1] - GAP - h)]
    else:
        spots.append((0.0, 0.0))
    return spots


def _overlaps(a, b, pad=2.0):
    return a[0] < b[2] + pad and a[2] > b[0] - pad and a[1] < b[3] + pad and a[3] > b[1] - pad


# ---------------------------------------------------------------- title block


def _title_block(c: _Canvas, W, title, rows, scale_txt, sheet, projection):
    x1, y0 = W - MARGIN, MARGIN
    x0 = x1 - TB_W
    hr, ht = 9.0, 12.0
    pairs = [rows[i:i + 2] for i in range(0, len(rows), 2)]
    h = ht + hr * len(pairs) + hr
    c.rect("frame", x0, y0, x1, y0 + h)
    # bottom row: scale, units, projection, sheet
    cells = [("Scale", scale_txt), ("Units", "mm"), ("Projection", None), ("Sheet", sheet)]
    cw = TB_W / 4
    for i, (label, value) in enumerate(cells):
        cx0 = x0 + i * cw
        if i:
            c.line("thin", (cx0, y0), (cx0, y0 + hr))
        c.text(cx0 + 1.5, y0 + hr - 2.6, label, size=2.0)
        if value is not None:
            c.text(cx0 + 1.5, y0 + 1.8, value, size=TEXT)
        else:
            _projection_symbol(c, cx0 + cw / 2, y0 + hr / 2 - 0.9, projection)
    y = y0 + hr
    for pair in reversed(pairs):
        c.line("thin", (x0, y), (x1, y))
        for i, (label, value) in enumerate(pair):
            w = TB_W if len(pair) == 1 else TB_W / 2
            cx0 = x0 + i * TB_W / 2
            if i:
                c.line("thin", (cx0, y), (cx0, y + hr))
            c.text(cx0 + 1.5, y + hr - 2.6, label, size=2.0)
            c.text(cx0 + 1.5, y + 1.8, _fit(str(value), w - 3, TEXT), size=TEXT)
        y += hr
    c.line("frame", (x0, y), (x1, y))
    c.text(x0 + 2, y + 3.6, _fit(title, TB_W - 4, 5.0, True), size=5.0, bold=True)
    return (x0, y0, x1, y0 + h)


def _fit(s: str, w: float, size: float, bold=False) -> str:
    if text_width(s, size, bold) <= w:
        return s
    while s and text_width(s + "…", size, bold) > w:
        s = s[:-1]
    return s + "…"


def _projection_symbol(c: _Canvas, cx, cy, projection):
    """ISO 5456-2: a truncated cone, narrow end left, and its end view (circles) right (first) or left (third)."""
    R, r, L = 2.3, 1.15, 5.5
    cy -= 0.5
    sg = 1.0 if projection == "first" else -1.0
    tx = cx - sg * 4.5  # cone center
    ex = cx + sg * 4.5  # circles center
    c.path("thin", [("M", tx - L / 2, cy - r), ("L", tx + L / 2, cy - R), ("L", tx + L / 2, cy + R),
                    ("L", tx - L / 2, cy + r), ("Z",)])
    for rr in (R, r):
        c.path("thin", _prim_cmds(("circle", ex, cy, rr), 1.0))
    c.line("thin", (tx - L / 2 - 0.8, cy), (tx + L / 2 + 0.8, cy))
    c.line("thin", (ex - R - 0.8, cy), (ex + R + 0.8, cy))
    c.line("thin", (ex, cy - R - 0.8), (ex, cy + R + 0.8))


# ---------------------------------------------------------------- the drawing


def _parse_scale(scale) -> float | None:
    if scale is None:
        return None
    if isinstance(scale, str):
        a, _, b = scale.partition(":")
        try:
            return float(a) / float(b or 1)
        except ValueError:
            raise TuleError(f"cannot read scale {scale!r}", op="drawing", hint='"1:2", "2:1" or 0.5') from None
    return float(scale)


def render(item, views=("front", "top", "right", "iso"), dims=(), title=None, info=None, sheet="A4",
           scale=None, hidden=True, projection="first", name=None):
    """Lay out a drawing; returns ``(canvas, (width, height), facts)``. See ``outputs.drawing``."""
    shape, part_name = _as_shape(item)
    if sheet not in SHEETS:
        raise TuleError(f"unknown sheet {sheet!r}", op="drawing", hint=", ".join(SHEETS))
    if projection not in GRID:
        raise TuleError(f"projection is 'first' or 'third', got {projection!r}", op="drawing")
    views = list(dict.fromkeys(views))
    if not views:
        raise TuleError("no views to draw", op="drawing", hint='views=("front", "top", "right", "iso")')
    W, H = SHEETS[sheet]
    wrapped = shape.wrapped
    lo, hi = shape.bounds
    diag = float(np.linalg.norm(np.subtract(hi, lo)))
    if diag <= 0:
        raise TuleError("the shape is empty", op="drawing")
    tol = max(1e-4, diag * 1e-6)
    V = {}
    for k in views:
        V[k] = _View(k)
        _hlr(wrapped, V[k], hidden and k != "iso", tol)
    grid = {k: GRID[projection][k] for k in views if k != "iso"}
    cyls = _cylinders(wrapped, tol)
    linear, circles = _parse_dims(dims, V, tol)
    plans = _plan(V, cyls, linear, circles, grid, hidden, tol)

    tb_rows = [("Part", part_name or name or "part")]
    for k, v in (info or {}).items():
        tb_rows.append((str(k).capitalize() if k.islower() else str(k), v if not isinstance(v, float) else _fmt(v)))
    title = title or part_name or name or "Drawing"
    tb_h = 12.0 + 9.0 * math.ceil(len(tb_rows) / 2) + 9.0
    frame = (MARGIN + 3, MARGIN + 3, W - MARGIN - 3, H - MARGIN - 3)
    keep_out = (W - MARGIN - TB_W - 3, MARGIN - 1, W - MARGIN + 1, MARGIN + tb_h + 3)

    def fit(s):
        """The views at scale ``s`` and where they go, or None when they do not fit the sheet."""
        canv = {}
        for k in grid:
            p = plans[k]
            c = _annotate(V[k], s, p["dims"], [(t, x, y, r) for t, x, y, r, _ in p["callouts"]],
                          _centers(p, s), p["side"])
            bb = V[k].bbox()
            canv[k] = (c, (bb[0] * s, bb[1] * s, bb[2] * s, bb[3] * s))
        off, rects, cells = _grid_block(canv, grid, s) if canv else ({}, [], [])
        if "iso" not in V:
            at = _place(rects, frame, keep_out)
            return (canv, off, at, None, None) if at else None
        ib = V["iso"].bbox()
        # the isometric view may be one standard scale smaller than the others (labelled)
        for si in [s] + ([x for x in SCALES if x < s][:1] if canv else []):
            iw, ih = (ib[2] - ib[0]) * si, (ib[3] - ib[1]) * si
            for sx, sy in _iso_spots(rects, cells, iw, ih):
                ir = (sx, sy, sx + iw, sy + ih)
                if any(_overlaps(ir, r, GAP) for r in rects):
                    continue
                at = _place(rects + [ir], frame, keep_out)
                if at:
                    return canv, off, at, (sx - ib[0] * si, sy - ib[1] * si), si
        return None

    fixed = _parse_scale(scale)
    if fixed:
        s, placed = fixed, fit(fixed)
        if placed is None:
            ok = next((_scale_text(x) for x in SCALES if x < fixed and fit(x)), None)
            raise TuleError(f"the views do not fit {sheet} at {_scale_text(fixed)}", op="drawing",
                            hint=(f'scale="{ok}" fits, or ' if ok else "") + "use a larger sheet (A3, A2, …) or fewer views")
    else:
        s, placed = next(((x, f) for x in SCALES if (f := fit(x))), (SCALES[-1], None))
        if placed is None:
            raise TuleError(f"the views do not fit {sheet} even at {_scale_text(SCALES[-1])}", op="drawing",
                            hint="use a larger sheet or fewer views")
    canv, off, (ax, ay), iso_at, iso_s = placed

    page = _Canvas()
    page.rect("frame", MARGIN, MARGIN, W - MARGIN, H - MARGIN)
    for k in canv:
        page.add(canv[k][0], off[k][0] + ax, off[k][1] + ay)
    if iso_at is not None:
        ic = _Canvas()
        for p in V["iso"].vis:
            ic.path("vis", _prim_cmds(p, iso_s))
        page.add(ic, iso_at[0] + ax, iso_at[1] + ay)
        if iso_s != s:
            ib = ic.bbox()
            page.text(iso_at[0] + ax + (ib[0] + ib[2]) / 2, iso_at[1] + ay + ib[1] - 5, f"({_scale_text(iso_s)})",
                      size=2.5, anchor="middle")
    _title_block(page, W, title, tb_rows, _scale_text(s), sheet, projection)
    facts = {"scale": _scale_text(s), "sheet": sheet, "title": title,
             "texts": [op[3] for op in page.ops if op[0] == "text"]}
    return page, (W, H), facts


# ---------------------------------------------------------------- writers


def _pdf_text(s: str) -> str:
    out = []
    for ch in s:
        o = _WINANSI.get(ch, ord(ch))
        if o > 255 or (127 <= o < 160 and ch not in _WINANSI):
            o = ord("?")
        if ch in "()\\":
            out.append("\\" + ch)
        elif o < 32 or o > 126:
            out.append(f"\\{o:03o}")
        else:
            out.append(chr(o))
    return "".join(out)


def to_pdf(c: _Canvas, size, title: str = "") -> bytes:
    """A one-page vector PDF (Helvetica, no dates: the same drawing gives the same bytes)."""
    W, H = size
    k = 72 / 25.4
    out = [f"{k:.6f} 0 0 {k:.6f} 0 0 cm", "1 J 1 j 0 g 0 G"]
    state = None
    for op in c.ops:
        if op[0] == "path":
            _, style, cmds, phase = op
            w, dash = STYLES[style]
            st = (w, dash, round(phase, 3))
            if st != state:
                out.append(f"{_n(w)} w [{' '.join(_n(x) for x in dash)}] {_n(phase) if dash else 0} d")
                state = st
            parts = []
            for cm in cmds:
                if cm[0] == "M":
                    parts.append(f"{_n(cm[1])} {_n(cm[2])} m")
                elif cm[0] == "L":
                    parts.append(f"{_n(cm[1])} {_n(cm[2])} l")
                elif cm[0] == "C":
                    parts.append(" ".join(_n(v) for v in cm[1:]) + " c")
                else:
                    parts.append("h")
            out.append(" ".join(parts) + " S")
        elif op[0] == "fill":
            pts = op[1]
            out.append(f"{_n(pts[0][0])} {_n(pts[0][1])} m " + " ".join(f"{_n(x)} {_n(y)} l" for x, y in pts[1:]) + " h f")
        else:
            _, x, y, s, size_, anchor, angle, bold = op
            w = text_width(s, size_, bold)
            d = {"start": 0.0, "middle": w / 2, "end": w}[anchor]
            cs, sn = math.cos(math.radians(angle)), math.sin(math.radians(angle))
            x, y = x - d * cs, y - d * sn
            out.append(f"BT /{'F2' if bold else 'F1'} {_n(size_)} Tf {_n(cs)} {_n(sn)} {_n(-sn)} {_n(cs)} {_n(x)} {_n(y)} Tm "
                       f"({_pdf_text(s)}) Tj ET")
    stream = zlib.compress("\n".join(out).encode("latin-1"), 9)
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {_n(W * k)} {_n(H * k)}] "
         "/Resources << /Font << /F1 5 0 R /F2 6 0 R >> >> /Contents 4 0 R >>").encode(),
        f"<< /Length {len(stream)} /Filter /FlateDecode >>\nstream\n".encode() + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>",
        f"<< /Title ({_pdf_text(title)}) /Producer (tule) >>".encode("latin-1"),
    ]
    buf = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for i, o in enumerate(objs, 1):
        offsets.append(len(buf))
        buf += f"{i} 0 obj\n".encode() + o + b"\nendobj\n"
    xref = len(buf)
    buf += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for o in offsets:
        buf += f"{o:010d} 00000 n \n".encode()
    buf += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R /Info 7 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(buf)


def _xml(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def to_svg(c: _Canvas, size, title: str = "") -> str:
    """The same drawing as SVG, in millimetres."""
    W, H = size
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{_n(W)}mm" height="{_n(H)}mm" viewBox="0 0 {_n(W)} {_n(H)}">',
           f"<title>{_xml(title)}</title>",
           "<style>path{fill:none;stroke:#000;stroke-linecap:round;stroke-linejoin:round}"
           ".f{fill:#000;stroke:none}text{font-family:Helvetica,Arial,sans-serif;fill:#000}</style>",
           f'<rect width="{_n(W)}" height="{_n(H)}" fill="#fff"/>']

    def P(x, y):
        return f"{_n(x)} {_n(H - y)}"

    pend: list = []
    pstate = None

    def flush():
        if pend:
            w, dash, phase = pstate
            extra = f' stroke-dasharray="{" ".join(_n(x) for x in dash)}" stroke-dashoffset="{_n(phase)}"' if dash else ""
            out.append(f'<path d="{" ".join(pend)}" stroke-width="{_n(w)}"{extra}/>')
            pend.clear()

    for op in c.ops:
        if op[0] == "path":
            _, style, cmds, phase = op
            w, dash = STYLES[style]
            st = (w, dash, round(phase, 3))
            if st != pstate or dash:
                flush()
                pstate = st
            d = []
            for cm in cmds:
                if cm[0] == "M":
                    d.append("M" + P(cm[1], cm[2]))
                elif cm[0] == "L":
                    d.append("L" + P(cm[1], cm[2]))
                elif cm[0] == "C":
                    d.append("C" + " ".join(P(cm[i], cm[i + 1]) for i in (1, 3, 5)))
                else:
                    d.append("Z")
            pend.append("".join(d))
            continue
        flush()
        pstate = None
        if op[0] == "fill":
            out.append(f'<path class="f" d="M{"L".join(P(x, y) for x, y in op[1])}Z"/>')
        else:
            _, x, y, s, size_, anchor, angle, bold = op
            rot = f' transform="rotate({_n(-angle)} {P(x, y).replace(" ", " ")})"' if angle else ""
            b = ' font-weight="bold"' if bold else ""
            a = f' text-anchor="{anchor}"' if anchor != "start" else ""
            out.append(f'<text x="{_n(x)}" y="{_n(H - y)}" font-size="{_n(size_)}"{a}{b}{rot}>{_xml(s)}</text>')
    flush()
    out.append("</svg>")
    return "\n".join(out) + "\n"


def to_dxf(view: _View) -> str:
    """Visible edges of one view as an ASCII DXF (R12): LINE, ARC, CIRCLE, POLYLINE, in mm at 1:1."""

    def g(code, value):
        return f"{code}\n{value}\n"

    def num(v):
        s = f"{v:.6f}".rstrip("0").rstrip(".")
        return "0" if s in ("-0", "") else s

    out = [g(0, "SECTION"), g(2, "HEADER"), g(9, "$ACADVER"), g(1, "AC1009"), g(9, "$INSUNITS"), g(70, 4),
           g(0, "ENDSEC"), g(0, "SECTION"), g(2, "ENTITIES")]
    for p in view.vis:
        if p[0] == "line":
            out.append(g(0, "LINE") + g(8, "0") + g(10, num(p[1])) + g(20, num(p[2])) + g(30, 0)
                       + g(11, num(p[3])) + g(21, num(p[4])) + g(31, 0))
        elif p[0] == "circle":
            out.append(g(0, "CIRCLE") + g(8, "0") + g(10, num(p[1])) + g(20, num(p[2])) + g(30, 0) + g(40, num(p[3])))
        elif p[0] == "arc":
            out.append(g(0, "ARC") + g(8, "0") + g(10, num(p[1])) + g(20, num(p[2])) + g(30, 0) + g(40, num(p[3]))
                       + g(50, num(p[4])) + g(51, num(p[5])))
        else:
            out.append(g(0, "POLYLINE") + g(8, "0") + g(66, 1) + g(10, 0) + g(20, 0) + g(30, 0) + g(70, 0))
            for x, y in p[1]:
                out.append(g(0, "VERTEX") + g(8, "0") + g(10, num(x)) + g(20, num(y)) + g(30, 0))
            out.append(g(0, "SEQEND") + g(8, "0"))
    out.append(g(0, "ENDSEC") + g(0, "EOF"))
    return "".join(out)


def dxf(item, path: str, view: str = "top") -> str:
    """Write one view's visible edges as a DXF at 1:1 in mm (laser, waterjet, CNC). Returns the path."""
    shape, _ = _as_shape(item)
    if view not in VIEWS:
        raise TuleError(f"unknown view {view!r}", op="drawing.dxf", hint=", ".join(VIEWS))
    v = _View(view)
    lo, hi = shape.bounds
    tol = max(1e-4, float(np.linalg.norm(np.subtract(hi, lo))) * 1e-6)
    _hlr(shape.wrapped, v, False, tol)
    return write_atomic(path, to_dxf(v))


def write(folder: str, base: str, item: Union[Shape, Assembly], views=("front", "top", "right", "iso"), dims=(),
          title=None, info=None, sheet="A4", scale=None, hidden=True, projection="first", dxf_view=None) -> dict:
    """Write ``<base>.pdf`` and ``<base>.svg`` (and ``<base>.dxf``) into ``folder``; returns facts (scale, texts)."""
    page, size, facts = render(item, views, dims, title, info, sheet, scale, hidden, projection, base)
    write_atomic(os.path.join(folder, base + ".pdf"), to_pdf(page, size, facts["title"]))
    write_atomic(os.path.join(folder, base + ".svg"), to_svg(page, size, facts["title"]))
    if dxf_view:
        dxf(item, os.path.join(folder, base + ".dxf"), dxf_view)
    return facts
