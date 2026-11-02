"""2D engineering drawings made from the model: projected views, dimensions, a title block.

```python
from tule import *
outputs.drawing("bracket-drawing", part, info={"number": "BR-1", "rev": "A", "material": "S235, 2 mm"},
                dims=[("top", (0, 0, 0), (10, 10, 0), {"along": "h", "tol": "±0.05"})],
                tol={"⌀5.5": "+0.1/0"}, notes=["Break all edges 0.3"], dxf="top")
```

writes ``outputs/bracket-drawing/bracket-drawing.pdf`` and ``.svg`` (and a
``.dxf`` of the top view at 1:1 in mm). Views come from OCCT's hidden-line
removal (visible edges solid, hidden ones dashed), placed by ISO first-angle
(default) or third-angle projection, with an isometric view in a free spot.
Overall sizes and callouts for holes (with counterbores, countersinks) and
slots come from the faces; ``dims`` adds what only the author knows. Lines are
drawn first, then each text goes on the first spot touching no line and no
other text (:func:`collisions` checks a page). The scale is the largest
standard one that fits the sheet. Output is deterministic (no dates), so a
rebuild that changes nothing writes the same bytes.
"""

from __future__ import annotations

import math
import os
import re
import zlib
from typing import Union

import numpy as np
from OCP.BRep import BRep_Tool
from OCP.BRepAdaptor import BRepAdaptor_Curve, BRepAdaptor_Surface
from OCP.BRepLib import BRepLib
from OCP.BRepLProp import BRepLProp_SLProps
from OCP.BRepMesh import BRepMesh_IncrementalMesh
from OCP.BRepTools import BRepTools, BRepTools_WireExplorer
from OCP.GCPnts import GCPnts_TangentialDeflection
from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
from OCP.GeomAbs import GeomAbs_Circle, GeomAbs_Cone, GeomAbs_Cylinder, GeomAbs_Line, GeomAbs_Plane
from OCP.gp import gp_Ax2, gp_Dir, gp_Lin, gp_Pnt
from OCP.HLRAlgo import HLRAlgo_Projector
from OCP.IntCurvesFace import IntCurvesFace_ShapeIntersector
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
# between them, for the hint when a fixed scale does not fit
NICE = sorted({m * 10.0 ** e for e in range(-3, 2) for m in (1, 1.5, 2, 2.5, 3, 4, 5, 7.5)} - set(SCALES), reverse=True)

# line styles: width (mm), dash pattern (mm)
STYLES = {
    "vis": (0.35, ()),
    "hid": (0.18, (2.0, 1.0)),
    "thin": (0.18, ()),
    "center": (0.18, (6.0, 1.0, 0.5, 1.0)),
    "frame": (0.5, ()),
    "sym": (0.25, ()),  # drawn symbols in text (⌴ ⌵ ↧)
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
_WIDTH.update({"⌀": 778, "Ø": 778, "×": 584, "°": 400, "±": 584, "…": 1000, "µ": 556,
               "⌴": 820, "⌵": 820, "↧": 700, "→": 1000})
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


# symbols Helvetica lacks, drawn as strokes: polylines in em units from the baseline
_SYMS = {
    "⌴": [((0.1, 0.72), (0.1, 0.05), (0.72, 0.05), (0.72, 0.72))],             # counterbore
    "⌵": [((0.08, 0.72), (0.41, 0.05), (0.74, 0.72))],                        # countersink
    "↧": [((0.35, 0.74), (0.35, 0.06)), ((0.15, 0.3), (0.35, 0.06), (0.55, 0.3)),
          ((0.07, 0.04), (0.63, 0.04))],                                       # depth
    "→": [((0.1, 0.3), (0.88, 0.3)), ((0.66, 0.48), (0.9, 0.3), (0.66, 0.12))],
}


def _split_syms(s: str) -> list[str]:
    return [p for p in re.split("([" + "".join(_SYMS) + "])", s) if p]


# ---------------------------------------------------------------- tolerances


_NUM = r"[+-]?(?:\d+\.?\d*|\.\d+)"


def _tol(spec):
    """A tolerance: ``("pm", "0.1")``, ``("dev", "+0.05", "0")`` or ``("fit", "H7")``; None for none."""
    if spec is None or spec == "":
        return None
    hint = '"±0.1", "+0.05/0", "0/-0.1", "H7", "g6", or a number for ±'
    if isinstance(spec, bool):
        raise TuleError(f"cannot read tolerance {spec!r}", op="drawing", hint=hint)
    if isinstance(spec, (int, float)):
        return ("pm", _fmt(abs(spec)))
    if isinstance(spec, (tuple, list)) and len(spec) == 2:
        try:
            return _dev(float(spec[0]), float(spec[1]))
        except (TypeError, ValueError):
            pass
    if isinstance(spec, str):
        s = spec.strip().replace("+/-", "±").replace("+-", "±").replace(" ", "")
        if m := re.fullmatch(r"±(" + _NUM + ")", s):
            return ("pm", _fmt(abs(float(m.group(1)))))
        if m := re.fullmatch("(" + _NUM + ")/(" + _NUM + ")", s):
            return _dev(float(m.group(1)), float(m.group(2)))
        if re.fullmatch(r"[A-Za-z]{1,2}\d{1,2}(/[A-Za-z]{1,2}\d{1,2})?", s):
            return ("fit", s)
    raise TuleError(f"cannot read tolerance {spec!r}", op="drawing", hint=hint)


def _dev(up: float, lo: float):
    up, lo = max(up, lo), min(up, lo)
    if abs(up + lo) < 1e-9 and up > 0:
        return ("pm", _fmt(up))

    def f(v):
        return "0" if abs(v) < 1e-9 else ("+" if v > 0 else "-") + _fmt(abs(v))

    return ("dev", f(up), f(lo))


def _tol_text(t) -> str:
    if t is None:
        return ""
    return {"pm": lambda: " ±" + t[1], "fit": lambda: " " + t[1], "dev": lambda: f" {t[1]}/{t[2]}"}[t[0]]()


def _key(s) -> str:
    """A tolerance key as printed: ``"⌀6.6"``, ``"88"``, ``"R3"``, ``"slot 5.5"``, ``"↧4.3"``."""
    if isinstance(s, (int, float)) and not isinstance(s, bool):
        return _fmt(s)
    s = str(s).strip().replace("Ø", "⌀").replace("ø", "⌀")
    s = re.sub(r"^(?:dia\.?|diameter)\s*", "⌀", s, flags=re.I)
    s = re.sub(r"^(?:depth|deep)\s*", "↧", s, flags=re.I)
    m = re.fullmatch(r"(⌀|↧|R|slot\s*)?\s*(" + _NUM + ")", s, flags=re.I)
    if not m:
        return s
    pre = (m.group(1) or "").strip().lower()
    pre = {"r": "R", "slot": "slot "}.get(pre, pre)
    return pre + _fmt(float(m.group(2)))


class _Tols:
    """Tolerances by printed size (``{"⌀6.6": "+0.2/0", "88": "±0.1"}``); every key must match something."""

    def __init__(self, spec):
        if spec is not None and not isinstance(spec, dict):
            raise TuleError(f"tol is a dict of size: tolerance, got {type(spec).__name__}", op="drawing",
                            hint='tol={"⌀6.6": "+0.2/0", "88": "±0.1", "slot 5.5": "+0.1/0"}')
        self.map = {_key(k): _tol(v) for k, v in (spec or {}).items()}
        self.used: set = set()
        self.seen: set = set()
        self.owners: dict = {}  # key: the callouts it tolerances, (view, label, identity)

    def get(self, *keys, owner=None):
        for k in keys:
            self.seen.add(k)
            if k in self.map:
                self.used.add(k)
                if owner is not None:
                    self.owners.setdefault(k, set()).add(owner)
                return self.map[k]
        return None

    def check(self):
        left = sorted(set(self.map) - self.used)
        if left:
            raise TuleError(f"tol: nothing on the drawing is {', '.join(map(repr, left))}", op="drawing",
                            hint="sizes drawn: " + ", ".join(sorted(self.seen, key=lambda k: (k[:1].isdigit(), k))))
        for k, owners in sorted(self.owners.items()):
            if len(owners) > 1:
                where = "; ".join(sorted(f"{lbl} in the {v} view" for v, lbl, _ in owners))
                raise TuleError(f"tol {k!r} would go on {len(owners)} different callouts: {where}", op="drawing",
                                hint='give the one feature its tolerance in dims: '
                                     '("right", part.faces.cylindrical(r=3.25), {"tol": "+0.1/0"})')


def _runs(pieces, size: float = TEXT):
    """Text runs ``(text, dx, dy, size)`` for pieces: strings and tolerances (deviations stacked)."""
    runs, u, plain = [], 0.0, False
    for p in pieces:
        if p is None:
            continue
        if isinstance(p, str) or p[0] in ("pm", "fit"):
            t = p if isinstance(p, str) else _tol_text(p)
            if plain:  # one run of plain text: SVG would drop a run's leading space
                runs[-1] = (runs[-1][0] + t, *runs[-1][1:])
            else:
                runs.append((t, u, 0.0, size))
            u += text_width(t, size)
            plain = True
        else:
            plain = False
            ss = size * 0.7
            u += size * 0.2
            runs.append((p[1], u, size * 0.5, ss))
            runs.append((p[2], u, -size * 0.13, ss))
            u += max(text_width(p[1], ss), text_width(p[2], ss))
    return runs


def _runs_name(pieces) -> str:
    return "".join(p if isinstance(p, str) else _tol_text(p) for p in pieces if p is not None)


def _runs_extent(runs):
    """Width, lowest and highest y of runs (sheet mm)."""
    if not runs:
        return 0.0, 0.0, 0.0
    w = max(dx + text_width(t, sz) for t, dx, _, sz in runs)
    return w, min(dy - 0.22 * sz for _, _, dy, sz in runs), max(dy + 0.75 * sz for _, _, dy, sz in runs)


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
        self.section: str | None = None  # this view cut at a plane: its letter
        self.plane: float | None = None  # where (along d); material nearer the viewer is gone
        self.cut: list = []              # loops of the cut faces (view coordinates), hatched
        self.cutlines: list = []         # sections cut through this view: (letter, point, along, sight)

    def keeps(self, lo: float, hi: float) -> bool:
        """Something between ``lo`` and ``hi`` (along d) is left in this view (always, unless it is a section)."""
        return self.plane is None or min(lo, hi) < self.plane - 1e-6

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


def _wire_points(w, f, tol):
    """A wire's points in order, curves sampled."""
    pts = []
    ex = BRepTools_WireExplorer(w, f)
    while ex.More():
        e = ex.Current()
        a = BRepAdaptor_Curve(e)
        t0, t1 = a.FirstParameter(), a.LastParameter()
        n = 2 if a.GetType() == GeomAbs_Line else 33
        ts = [t0 + (t1 - t0) * i / (n - 1) for i in range(n)]
        if ex.Orientation() == TopAbs_REVERSED:
            ts.reverse()
        for t in ts:
            q = a.Value(t)
            q = (q.X(), q.Y(), q.Z())
            if not pts or math.dist(pts[-1], q) > tol:
                pts.append(q)
        ex.Next()
    return pts


def _section(shape, view: "_View", point, diag: float, tol: float):
    """``shape`` without what lies between ``point``'s plane (normal: the view direction) and the viewer;
    sets the view's plane and the loops of the cut faces."""
    n, p = view.d, np.asarray(point, dtype=float)
    L = 2 * diag + 10
    corner = p - L * view.x - L * view.y
    box = BRepPrimAPI_MakeBox(gp_Ax2(gp_Pnt(*corner), gp_Dir(*n), gp_Dir(*view.x)), 2 * L, 2 * L, L).Shape()
    op = BRepAlgoAPI_Cut(shape, box)
    op.Build()
    if not op.IsDone():  # pragma: no cover
        raise TuleError("could not cut the section", op="drawing", hint="move the section point a little")
    kept = op.Shape()
    view.plane = float(p @ n)
    for f in explore(kept, "face"):
        a = BRepAdaptor_Surface(f)
        if a.GetType() != GeomAbs_Plane:
            continue
        pl = a.Plane()
        ax, loc = pl.Axis().Direction(), pl.Location()
        if abs(np.array([ax.X(), ax.Y(), ax.Z()]) @ n) < 0.999:
            continue
        if abs(np.array([loc.X(), loc.Y(), loc.Z()]) @ n - view.plane) > tol * 10:
            continue
        for w in explore(f, "wire"):
            pts = _wire_points(w, f, tol)
            if len(pts) >= 3:
                view.cut.append([view.p(q) for q in pts])
    if not view.cut:
        raise TuleError(f"the section plane of the {view.name} view through {tuple(round(float(v), 3) for v in p)} "
                        "cuts no material", op="drawing", hint="put the point inside the part, on the features to show")
    return kept


# ---------------------------------------------------------------- features: holes, slots, bosses


class _Cyl:
    """A cylindrical feature: all faces on one axis with one radius."""

    def __init__(self, o, d, r, t0, t1, sweep, concave):
        self.o, self.d, self.r = o, d, r
        self.t0, self.t1, self.sweep, self.concave = t0, t1, sweep, concave
        self.capped = False
        self.faces: list = []
        self.rad = np.zeros(3)  # where the faces bulge to (radial, weighted by sweep)

    @property
    def full(self) -> bool:
        return self.sweep >= 359.0

    @property
    def depth(self) -> float:
        return self.t1 - self.t0


class _Slot:
    """Two concave half-cylinders facing apart, joined by flats: a slot from center ``a`` to center ``b``."""

    def __init__(self, a, b, d, r, t0, t1, capped):
        self.a, self.b, self.d, self.r = a, b, d, r
        self.t0, self.t1, self.capped = t0, t1, capped
        self.cb: "_Slot | None" = None
        self.open = (True, True)

    @property
    def thru(self) -> bool:
        return not self.capped and all(self.open)

    @property
    def width(self) -> float:
        return 2 * self.r

    @property
    def length(self) -> float:
        return float(np.linalg.norm(self.b - self.a)) + 2 * self.r

    @property
    def depth(self) -> float:
        return self.t1 - self.t0


class _Hole:
    """A round hole, maybe counterbored (``cb``: a wider capped hole on its end) or countersunk (``cs``)."""

    def __init__(self, cyl: _Cyl):
        self.cyl = cyl
        self.o, self.d, self.r = cyl.o, cyl.d, cyl.r
        self.cb: _Cyl | None = None
        self.cs: tuple | None = None  # (outer radius, included angle in degrees)
        self.open = (True, True)      # its ends at t0, t1 open to the outside (not into a slit or pocket)

    @property
    def thru(self) -> bool:
        return not self.cyl.capped and all(self.open)


def _features(shape, tol: float):
    """Cylinders (holes, bosses), slots and holes with their counterbores and countersinks."""
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
    cones = []
    for f in faces:
        s = BRepAdaptor_Surface(f)
        typ = s.GetType()
        if typ not in (GeomAbs_Cylinder, GeomAbs_Cone):
            continue
        g = s.Cylinder() if typ == GeomAbs_Cylinder else s.Cone()
        ax = g.Axis()
        o = np.array([ax.Location().X(), ax.Location().Y(), ax.Location().Z()])
        dz = np.array([ax.Direction().X(), ax.Direction().Y(), ax.Direction().Z()])
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
        op = o - (o @ d) * d
        if typ == GeomAbs_Cone:
            if concave:
                rs, ts = [], []
                for v in explore(f, "vertex"):
                    p = static(BRep_Tool, "Pnt")(v)
                    w = np.array([p.X(), p.Y(), p.Z()]) - op
                    rs.append(float(np.linalg.norm(w - (w @ d) * d)))
                    ts.append(float(w @ d))
                if rs:
                    cones.append((op, d, min(rs), max(rs), 2 * math.degrees(abs(g.SemiAngle())), min(ts), max(ts)))
            continue
        r = g.Radius()
        ts = sorted(o @ d + v * sign for v in (v0, v1))
        key = (tuple(np.round(d, 5)), tuple(np.round(op / tol)), round(r / tol), concave)
        c = groups.get(key)
        if c is None:
            c = groups[key] = _Cyl(op, d, r, ts[0], ts[1], 0.0, concave)
        c.t0, c.t1 = min(c.t0, ts[0]), max(c.t1, ts[1])
        sw = math.degrees(u1 - u0)
        c.sweep += sw
        c.faces.append(f)
        if np.linalg.norm(radial) > 1e-12:
            c.rad += _unit(radial) * sw
    cyls = list(groups.values())

    def capped(cy, a, b):
        return any(_has_bottom(f, edges, efaces, a, b, cy.d, cy.r, tol) for f in cy.faces)

    for cy in cyls:
        if cy.concave and cy.full:
            cy.capped = capped(cy, cy.o, cy.o)
    # slots: concave half-cylinders in pairs, same radius and depth, bulging away from each other
    halves = [cy for cy in cyls if cy.concave and 170 <= cy.sweep <= 190 and np.linalg.norm(cy.rad) > 1e-9]
    slots, used = [], set()
    for i, h in enumerate(halves):
        if i in used:
            continue
        best = None
        for j in range(i + 1, len(halves)):
            g = halves[j]
            if j in used or abs(g.r - h.r) > tol * 10 or np.abs(g.d - h.d).max() > 1e-6:
                continue
            if abs(g.t0 - h.t0) > tol * 10 or abs(g.t1 - h.t1) > tol * 10:
                continue
            v = g.o - h.o
            L = float(np.linalg.norm(v))
            if L < tol * 10:
                continue
            if _unit(h.rad) @ (-v / L) > 0.98 and _unit(g.rad) @ (v / L) > 0.98 and (best is None or L < best[0]):
                best = (L, j)
        if best is None:
            continue
        g = halves[best[1]]
        used |= {i, best[1]}
        a, b = sorted((h.o, g.o), key=lambda p: tuple(np.round(p, 6)))
        slots.append(_Slot(a, b, h.d, h.r, h.t0, h.t1, capped(h, a, b) or capped(g, a, b)))
    # counterbored slots: a wider capped slot on the same centers, its floor at the slot's end
    for sl in slots:
        for w in slots:
            if (w is not sl and w.r > sl.r and w.capped and np.abs(w.a - sl.a).max() < tol * 10
                    and np.abs(w.b - sl.b).max() < tol * 10
                    and min(abs(w.t0 - sl.t1), abs(w.t1 - sl.t0)) < tol * 10):
                sl.cb = w
    slots = [sl for sl in slots if not any(o.cb is sl for o in slots)]
    # holes: the narrowest concave cylinder on an axis; a wider capped one on its end is a counterbore
    axes: dict = {}
    for cy in cyls:
        if cy.concave and cy.full:
            axes.setdefault((tuple(np.round(cy.d, 5)), tuple(np.round(cy.o / tol))), []).append(cy)
    holes = []
    for stack in axes.values():
        stack.sort(key=lambda c: c.r)
        rest = list(stack)
        while rest:
            h = _Hole(rest.pop(0))
            for w in rest:
                if w.capped and min(abs(w.t0 - h.cyl.t1), abs(w.t1 - h.cyl.t0)) < tol * 10:
                    h.cb = w
                    rest.remove(w)
                    break
            for op, d, r0, r1, ang, t0, t1 in cones:
                if (np.abs(d - h.d).max() < 1e-6 and np.linalg.norm(op - h.o) < tol * 10 and abs(r0 - h.r) < tol * 10
                        and r1 > h.r + tol * 10 and min(abs(t0 - h.cyl.t1), abs(t1 - h.cyl.t0)) < tol * 10):
                    h.cs = (r1, ang)
            holes.append(h)
    # through only when nothing is beyond either end: rays just inside the wall, along the axis
    inter = IntCurvesFace_ShapeIntersector()
    inter.Load(shape, tol)
    for h in holes:
        h.open = _open_ends(inter, [h.o], h.d, h.r, h.cyl.t0, h.cyl.t1)
    for sl in slots:
        sl.open = _open_ends(inter, [sl.a, sl.b], sl.d, sl.r, sl.t0, sl.t1)
    return cyls, slots, holes


def _open_ends(inter, axes, d, r, t0, t1):
    """Whether rays leaving each end of a bore (at 0.9 r from its axes) meet no more material."""
    e1 = _unit(np.cross(d, [1.0, 0, 0] if abs(d[0]) < 0.9 else [0, 1.0, 0]))
    e2 = np.cross(d, e1)
    out = []
    for t, sg in ((t0, -1.0), (t1, 1.0)):
        free = True
        for o in axes:
            for e in (e1, -e1, e2, -e2):
                q = o + (t + sg * 1e-3) * d + 0.9 * r * e
                inter.Perform(gp_Lin(gp_Pnt(*q), gp_Dir(*(sg * d))), 0.0, 1e9)
                if inter.NbPnt() > 0:
                    free = False
        out.append(free)
    return tuple(out)


def _cylinders(shape, tol: float) -> list[_Cyl]:
    """Full and partial cylinders; a concave one is a hole, ``capped`` when it has a bottom (blind, counterbore)."""
    return _features(shape, tol)[0]


def _has_bottom(f, edges, efaces, a, b, d, r, tol) -> bool:
    """A face next to ``f`` across a circular edge lies within ``r`` of the axis segment a–b: a bottom."""
    e_ab = b - a
    L2 = float(e_ab @ e_ab)
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
                w = np.array([p.X(), p.Y(), p.Z()]) - a
                w = w - (w @ d) * d
                if L2 > 1e-12:
                    w = w - np.clip(w @ e_ab / L2, 0, 1) * e_ab
                far = max(far, float(np.linalg.norm(w)))
            if far <= r + tol * 10:
                return True
    return False


# ---------------------------------------------------------------- canvas


class _Canvas:
    """Drawing operations in sheet millimetres (y up)."""

    def __init__(self):
        self.ops: list = []
        self.labels: list = []  # (text, box) per line of text, for facts and collision checks
        self.leaders: list = []  # callout leaders (x0, y0, x1, y1): none may cross another

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
        return self.label(x, y, [(s, 0.0, 0.0, size)], anchor, angle, bold)

    def label(self, x, y, runs, anchor="start", angle=0.0, bold=False, name=None):
        """One line of text runs, baseline at (x, y); symbols are drawn as strokes. Returns its box."""
        w, y0, y1 = _runs_extent(runs)
        if bold:
            w *= 1.06
        u0 = {"start": 0.0, "middle": -w / 2, "end": -w}[anchor]
        cs, sn = math.cos(math.radians(angle)), math.sin(math.radians(angle))

        def P(u, v):
            return (x + u * cs - v * sn, y + u * sn + v * cs)

        for text, dx, dy, size in runs:
            u = u0 + dx * (1.06 if bold else 1.0)
            for seg in _split_syms(text):
                if seg in _SYMS:
                    for pl in _SYMS[seg]:
                        pts = [P(u + px * size, dy + py * size) for px, py in pl]
                        self.path("sym", [("M", *pts[0])] + [("L", *q) for q in pts[1:]])
                    u += text_width(seg, size)
                    continue
                lead = len(seg) - len(seg.lstrip(" "))  # placed by position, not by leading spaces
                u += text_width(seg[:lead], size, bold)
                seg = seg[lead:]
                if seg.strip():
                    px, py = P(u, dy)
                    self.ops.append(("text", px, py, seg.rstrip(" "), size, "start", float(angle), bold))
                u += text_width(seg, size, bold)
        box = (*P(u0 + w / 2, (y0 + y1) / 2), cs, sn, w / 2, (y1 - y0) / 2)
        self.labels.append((name if name is not None else "".join(r[0] for r in runs), box))
        return box

    def rect(self, style, x0, y0, x1, y1):
        self.path(style, [("M", x0, y0), ("L", x1, y0), ("L", x1, y1), ("L", x0, y1), ("Z",)])

    def add(self, other: "_Canvas", dx=0.0, dy=0.0):
        self.ops += [_moved(op, dx, dy) for op in other.ops]
        self.labels += [(t, (b[0] + dx, b[1] + dy, *b[2:])) for t, b in other.labels]
        self.leaders += [(x0 + dx, y0 + dy, x1 + dx, y1 + dy) for x0, y0, x1, y1 in other.leaders]

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


# -- collisions: text boxes against line segments and each other


def _segments(ops) -> np.ndarray:
    """Every stroke of ``ops`` as segments (x0, y0, x1, y1); curves are sampled. Drawn symbols are text, not lines."""
    out = []
    for op in ops:
        if op[0] == "fill":
            pts = op[1]
            out += [(*pts[i], *pts[(i + 1) % len(pts)]) for i in range(len(pts))]
            continue
        if op[0] != "path" or op[1] == "sym":
            continue
        cur = start = None
        for cm in op[2]:
            if cm[0] == "M":
                cur = start = (cm[1], cm[2])
            elif cm[0] == "L":
                out.append((*cur, cm[1], cm[2]))
                cur = (cm[1], cm[2])
            elif cm[0] == "C":
                p0, p1, p2, p3 = (np.array(cur), np.array(cm[1:3]), np.array(cm[3:5]), np.array(cm[5:7]))
                prev = cur
                for t in np.linspace(0, 1, 9)[1:]:
                    q = (1 - t) ** 3 * p0 + 3 * (1 - t) ** 2 * t * p1 + 3 * (1 - t) * t * t * p2 + t ** 3 * p3
                    out.append((*prev, float(q[0]), float(q[1])))
                    prev = (float(q[0]), float(q[1]))
                cur = prev
            elif cur is not None and start is not None:
                out.append((*cur, *start))
                cur = start
    return np.asarray(out, dtype=float).reshape(-1, 4)


def _seg_hits(box, S: np.ndarray, pad: float = 0.0) -> np.ndarray:
    """Which segments cross the (padded) box ``(cx, cy, cos, sin, half w, half h)``."""
    if not len(S):
        return np.zeros(0, dtype=bool)
    cx, cy, c, s, hw, hh = box
    hw, hh = hw + pad, hh + pad
    x0 = (S[:, 0] - cx) * c + (S[:, 1] - cy) * s
    y0 = -(S[:, 0] - cx) * s + (S[:, 1] - cy) * c
    x1 = (S[:, 2] - cx) * c + (S[:, 3] - cy) * s
    y1 = -(S[:, 2] - cx) * s + (S[:, 3] - cy) * c
    dx, dy = x1 - x0, y1 - y0
    t0, t1 = np.zeros(len(S)), np.ones(len(S))
    out = np.zeros(len(S), dtype=bool)
    with np.errstate(divide="ignore", invalid="ignore"):
        for p, q in ((-dx, x0 + hw), (dx, hw - x0), (-dy, y0 + hh), (dy, hh - y0)):
            par = np.abs(p) < 1e-12
            out |= par & (q < 0)
            r = np.where(par, 0.0, q / np.where(par, 1.0, p))
            t0 = np.where(~par & (p < 0), np.maximum(t0, r), t0)
            t1 = np.where(~par & (p > 0), np.minimum(t1, r), t1)
    return ~out & (t0 <= t1)


def _box_hit(a, b, pad: float = 0.0) -> bool:
    """Two boxes overlap (separating axes)."""
    ca, cb = np.array(a[:2]), np.array(b[:2])
    axes = [np.array([a[2], a[3]]), np.array([-a[3], a[2]]), np.array([b[2], b[3]]), np.array([-b[3], b[2]])]
    ea = (axes[0] * a[4], axes[1] * a[5])
    eb = (axes[2] * b[4], axes[3] * b[5])
    for ax in axes:
        ra = abs(ea[0] @ ax) + abs(ea[1] @ ax) + pad
        rb = abs(eb[0] @ ax) + abs(eb[1] @ ax)
        if abs((cb - ca) @ ax) >= ra + rb:
            return False
    return True


def _box_rect(b):
    """Axis-aligned bounds of a box."""
    cx, cy, c, s, hw, hh = b
    ex, ey = abs(c) * hw + abs(s) * hh, abs(s) * hw + abs(c) * hh
    return (cx - ex, cy - ey, cx + ex, cy + ey)


class _Occ:
    """What is already on a view: segments and text boxes, for placing more text without collisions."""

    PAD = 0.4  # clearance around text (mm)

    def __init__(self, segs):
        self.S = np.asarray(segs, dtype=float).reshape(-1, 4)
        self.boxes: list = []

    def free(self, boxes, segs=()) -> bool:
        """``boxes`` touch no segment or box, and ``segs`` (new lines) touch no box."""
        for b in boxes:
            if _seg_hits(b, self.S, self.PAD).any() or any(_box_hit(b, o, self.PAD) for o in self.boxes):
                return False
        if segs and self.boxes:
            S = np.asarray(segs, dtype=float).reshape(-1, 4)
            if any(_seg_hits(o, S, self.PAD).any() for o in self.boxes):
                return False
        return True

    def add(self, boxes=(), segs=()):
        self.boxes += list(boxes)
        if len(segs):
            self.S = np.concatenate([self.S, np.asarray(segs, dtype=float).reshape(-1, 4)])


def collisions(page: "_Canvas") -> list[str]:
    """Texts on ``page`` that touch a line or another text, and leaders that cross (a finished drawing has none)."""
    S = _segments(page.ops)
    out = []
    for i, (t, b) in enumerate(page.labels):
        hit = _seg_hits(b, S)
        if hit.any():
            out.append(f"{t!r} touches a line at {tuple(round(v, 1) for v in S[np.argmax(hit)])}")
        for t2, b2 in page.labels[:i]:
            if _box_hit(b, b2):
                out.append(f"{t!r} overlaps {t2!r}")
    for i, L in enumerate(page.leaders):
        if _cross(L[:2], L[2:], page.leaders[:i]):
            out.append(f"the leader at {tuple(round(v, 1) for v in L[2:])} crosses another")
    return out


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

    def __init__(self, a, b, along, text=None, offset=None, side=None, tol=None):
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
        self.tol = tol

    @property
    def n(self):
        return np.array([-self.u[1], self.u[0]])

    @property
    def pieces(self):
        return [self.text, self.tol]


def _box_of(x, y, runs, anchor="start", angle=0.0):
    """The box :meth:`_Canvas.label` would give these runs."""
    w, y0, y1 = _runs_extent(runs)
    u0 = {"start": 0.0, "middle": -w / 2, "end": -w}[anchor]
    cs, sn = math.cos(math.radians(angle)), math.sin(math.radians(angle))
    u, v = u0 + w / 2, (y0 + y1) / 2
    return (x + u * cs - v * sn, y + u * sn + v * cs, cs, sn, w / 2, (y1 - y0) / 2)


def _dim_lines(c: _Canvas, dim: _Dim, s: float, L: float):
    """Draw ``dim``'s lines (scale ``s``, dimension line where ``point · n == L``, paper mm).

    Returns its text runs, the text angle and the spots for the text, best first:
    ``(baseline point, anchor, extra line or None)``.
    """
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
    runs = _runs(dim.pieces)
    w, y0, _ = _runs_extent(runs)
    lift = (0.5 - y0) * up  # the text's box clear of the line
    inside = span >= w + 2 * ARROW[0] + 3
    if inside:
        c.line("thin", A, B)
        c.arrow(A, A - B)
        c.arrow(B, B - A)
    else:  # too short: arrows from outside
        d = (B - A) / span if span > 1e-9 else u
        c.line("thin", A - 5 * d, B + 5 * d)
        c.arrow(A, d)
        c.arrow(B, -d)
    spots = []
    if inside:
        m = (A + B) / 2
        spots.append((m + lift, "middle", None, True))
        room = (span - w) / 2 - ARROW[0] - 1.0
        k = 2.0
        while k <= room:
            spots += [(m + k * t + lift, "middle", None, True), (m - k * t + lift, "middle", None, True)]
            k += 2.0
    hi, lo = (B, A) if (B - A) @ t >= 0 else (A, B)
    for g in np.arange(6.0, 170.0, 4.0):  # beyond either end, on the dimension line drawn longer
        bare = g == 6.0 and not inside
        p = hi + g * t
        spots.append((p + lift, "start", None if bare else (hi, p + (w + 0.5) * t), g <= 10))
        p = lo - g * t
        spots.append((p + lift, "end", None if bare else (lo, p - (w + 0.5) * t), g <= 10))
    return runs, angle, spots, dim.text if dim.tol is None else _runs_name(dim.pieces)


def _place_text(c: _Canvas, occ: "_Occ", runs, angle, spots, name, bold=False) -> bool:
    """Put a text on the first free spot (the first spot when none is); whether that spot is a near one.

    ``spots``: ``(baseline point, anchor, extra line or None[, near])``.
    """
    pick = spots[0]
    for spot in spots:
        p, anchor, extra = spot[:3]
        segs = [(*extra[0], *extra[1])] if extra is not None else []
        box = _box_of(p[0], p[1], runs, anchor, angle)
        if bold:
            box = (*box[:4], box[4] * 1.06, box[5])
        if occ.free([box], segs):
            pick = spot
            break
    p, anchor, extra = pick[:3]
    segs = []
    if extra is not None:
        c.line("thin", extra[0], extra[1])
        segs = [(*extra[0], *extra[1])]
    occ.add([c.label(p[0], p[1], runs, anchor, angle, bold=bold, name=name)], segs)
    return pick[3] if len(pick) > 3 else True


def _dim_interval(dim: _Dim, s: float):
    """The stretch of the side a dimension occupies (for stacking)."""
    lo, hi = sorted((dim.a @ dim.u * s, dim.b @ dim.u * s))
    w = _runs_extent(_runs(dim.pieces))[0]
    if hi - lo < w + 2 * ARROW[0] + 3:
        hi += 6 + w + 5
        lo -= 5
    return lo - 2, hi + 2


# ---------------------------------------------------------------- one view, annotated


SIDES = {"top": (np.array([0.0, 1.0]), "h"), "bottom": (np.array([0.0, -1.0]), "h"),
         "right": (np.array([1.0, 0.0]), "v"), "left": (np.array([-1.0, 0.0]), "v")}


def _side_edge(G, side):
    return {"top": G[3], "bottom": -G[1], "right": G[2], "left": -G[0]}[side]


def _hatch(loops, spacing=2.5):
    """45° hatch lines inside ``loops`` (sheet mm; nested loops are holes, even-odd)."""
    segs = []
    for L in loops:
        P = np.asarray(L, dtype=float)
        segs += [(*P[i], *P[(i + 1) % len(P)]) for i in range(len(P))]
    if not segs:
        return []
    S = np.asarray(segs)
    u = np.array([1.0, 1.0]) / math.sqrt(2)   # along the hatch
    n = np.array([-1.0, 1.0]) / math.sqrt(2)  # across it
    a, b = S[:, :2] @ n, S[:, 2:] @ n
    out = []
    for c in np.arange(math.floor(min(a.min(), b.min()) / spacing) * spacing + spacing / 2, max(a.max(), b.max()), spacing):
        m = (np.minimum(a, b) <= c) & (np.maximum(a, b) > c)
        if not m.any():
            continue
        t = (c - a[m]) / (b[m] - a[m])
        p = S[m, :2] + t[:, None] * (S[m, 2:] - S[m, :2])
        xs = np.sort(p @ u)
        for x0, x1 in zip(xs[0::2], xs[1::2]):
            if x1 - x0 > 0.3:
                out.append((tuple(x0 * u + c * n), tuple(x1 * u + c * n)))
    return out


def _annotate(view: _View, s: float, dims: list, callouts: list, centers: list, nb: dict) -> _Canvas:
    """Geometry, center lines, dimensions and callouts of one view at scale ``s`` (view-local sheet mm).

    Lines go first; then every text takes the first spot that touches no line and no other text. A
    dimension whose text found no spot near its line moves out a tier, and the view is drawn again.
    """
    bump: dict = {}
    for _ in range(4):
        c, far = _annotate_once(view, s, dims, callouts, centers, nb, bump)
        if not far:
            break
        for d in far:
            bump[id(d)] = bump.get(id(d), 0) + 1
    return c


def _annotate_once(view, s, dims, callouts, centers, nb, bump):
    c = _Canvas()
    for p in view.hid:
        c.path("hid", _prim_cmds(p, s))
    for p in view.vis:
        c.path("vis", _prim_cmds(p, s))
    for p, q in _hatch([[(x * s, y * s) for x, y in L] for L in view.cut]):
        c.line("thin", p, q)
    geo = _segments(c.ops)  # the part's own lines: leaders should cross as few as possible
    for p, q in centers:
        c.center_line(np.asarray(p) * s, np.asarray(q) * s)
    bb = view.bbox()
    G = (bb[0] * s, bb[1] * s, bb[2] * s, bb[3] * s)
    # dimensions: stacked outward per side, shorter ones closer
    reach = {k: 0.0 for k in SIDES}
    texts = []
    for side, (out, _) in SIDES.items():
        mine = sorted([d for d in dims if d.side == side], key=lambda d: (d.value, d.text))
        tiers: list[list] = []
        for d in mine:
            iv = _dim_interval(d, s)
            t = next((i for i, row in enumerate(tiers)
                      if i >= bump.get(id(d), 0) and all(iv[1] <= a or iv[0] >= b for a, b in row)),
                     max(len(tiers), bump.get(id(d), 0)))
            while len(tiers) <= t:
                tiers.append([])
            tiers[t].append(iv)
            dist = _side_edge(G, side) + FIRST + STEP * t  # along `out`
            L = dist * float(out @ d.n)  # the line's coordinate along the dimension's own normal
            texts.append((t, d, _dim_lines(c, d, s, L)))
        if tiers:
            reach[side] = FIRST + STEP * (len(tiers) - 1) + TEXT + 1.5
    for d in dims:
        if d.side is None:
            texts.append((0, None, _dim_lines(c, d, s, float(d.a * s @ d.n) + d.offset)))
    # section cutting planes seen edge-on here: a chain line, thick ends, arrows looking along the cut
    marks = []
    for letter, p, w, sight in view.cutlines:
        p, w, sight = np.asarray(p) * s, np.asarray(w), np.asarray(sight)
        ts = [(np.array(q) - p) @ w for q in ((G[0], G[1]), (G[2], G[1]), (G[0], G[3]), (G[2], G[3]))]
        E0, E1 = p + (min(ts) - 5) * w, p + (max(ts) + 5) * w
        c.center_line(E0, E1)
        for E, inward in ((E0, w), (E1, -w)):
            c.line("frame", E, E + 5 * inward)
            c.line("thin", E - 8 * sight, E - 0.5 * sight)
            c.arrow(E, sight)
            T = E - 8 * sight
            spots = [(T - 1.5 * sight + k * inward * -1 + np.array([0, -TEXT * 0.36]), "middle", None)
                     for k in (0, 3, 6, 9, 12)]
            marks.append((_runs([letter], 5.0), spots, letter))
    occ = _Occ(_segments(c.ops))
    far = []
    for t, d, tx in sorted(texts, key=lambda it: it[0]):  # inner tiers first
        if not _place_text(c, occ, *tx) and d is not None:
            far.append(d)
    for runs, spots, letter in marks:
        _place_text(c, occ, runs, 0.0, spots, letter, bold=True)
    if callouts:
        _draw_callouts(c, occ, G, callouts, s, reach, nb, geo, {d.side for d in dims})
    if view.section:
        title = f"{view.section}-{view.section}"
        runs = _runs([title], 5.0)
        x = (G[0] + G[2]) / 2
        top = max(G[3] + reach["top"], c.bbox()[3] if c.ops else G[3])
        _place_text(c, occ, runs, 0.0, [(np.array([x, top + 4 + 2 * k]), "middle", None) for k in range(40)],
                    title, bold=True)
    return c, far


def _ncross(p, q, S) -> int:
    """How many of the segments S the segment p–q properly crosses."""
    if not len(S):
        return 0
    S = np.asarray(S, dtype=float).reshape(-1, 4)
    a, b = np.asarray(p, float), np.asarray(q, float)
    c0, c1 = S[:, :2], S[:, 2:]

    def orient(x, y, z):
        return (y[..., 0] - x[..., 0]) * (z[..., 1] - x[..., 1]) - (y[..., 1] - x[..., 1]) * (z[..., 0] - x[..., 0])

    d1, d2 = orient(c0, c1, a), orient(c0, c1, b)
    d3, d4 = orient(a, b, c0), orient(a, b, c1)
    return int(np.count_nonzero((d1 * d2 < -1e-9) & (d3 * d4 < -1e-9)))


def _cross(p, q, S) -> bool:
    """Segment p–q crosses any of the segments S (proper crossings only)."""
    return _ncross(p, q, S) > 0


def _draw_callouts(c: _Canvas, occ: _Occ, G, callouts, s, reach, nb, geo, dim_sides):
    """Callouts beside the view: lines of text, a shoulder and a slanted leader to the feature.

    ``callouts``: ``(lines, targets, side or None)``; lines are lists of text pieces, targets
    ``(x0, y0, x1, y1, r)`` (a circle when the two points coincide, else a slot) in view coordinates.
    Each goes on the side where its leader crosses the fewest lines of the part, then the shortest; no
    leader crosses another, no text touches anything.
    """
    mid = TEXT * 0.36  # half the height of digits
    LH = TEXT * 1.45   # line pitch
    blocks = []
    for lines, targets, forced in callouts:
        T = [(np.array(t[:2]) * s, np.array(t[2:4]) * s, t[4] * s) for t in targets]
        blocks.append(([_runs(p) for p in lines], [_runs_name(p) for p in lines], T, forced))

    def depth(b):  # how far inside the view its nearest feature is: the hardest go first
        return min(min(G[2] - max(t[0][0], t[1][0]), min(t[0][0], t[1][0]) - G[0],
                       G[3] - max(t[0][1], t[1][1]), min(t[0][1], t[1][1]) - G[1]) for t in b[2])

    blocks.sort(key=lambda b: (b[3] is None, -depth(b), b[1]))
    leaders: list = []

    def layout(runs, tgt, side, push, k):
        p0, p1, r = tgt
        n = len(runs)
        d0 = reach[side] + 6.0
        if side in ("left", "right"):
            sg = 1.0 if side == "right" else -1.0
            x = G[2] + d0 + 8 * push if side == "right" else G[0] - d0 - 8 * push
            ym = max(p0[1], p1[1]) + 0.7 * r + 4.0 + k
            base = [ym - mid - i * LH for i in range(n)]
            anchor = "start" if side == "right" else "end"
        else:
            sg = 1.0
            x = max(p0[0], p1[0]) + 0.7 * r + 7.0 + k
            if side == "top":
                b0 = G[3] + d0 + 6 * push + (n - 1) * LH
            else:
                b0 = G[1] - d0 - 6 * push - mid
            base = [b0 - i * LH for i in range(n)]
            ym = base[0] + mid
            anchor = "start"
        S = np.array([x - sg * 4.0, ym])
        e = p1 - p0
        L2 = float(e @ e)
        q = p0 + (np.clip((S - p0) @ e / L2, 0, 1) * e if L2 > 1e-12 else 0.0)
        if np.linalg.norm(S - q) < r + ARROW[0] + 1:
            return None
        Q = q + r * _unit(S - q)
        boxes = [_box_of(x, yb, rr, anchor) for yb, rr in zip(base, runs)]
        segs = [(*S, x - sg * 1.0, ym), (*S, *Q)]
        return x, base, anchor, S, Q, boxes, segs

    ks_side = [0.0] + [v for i in range(1, 31) for v in (-1.5 * i, 1.5 * i)]
    ks_row = [0.0] + [v for i in range(1, 31) for v in (2.0 * i, -2.0 * i)]

    def search(runs, T, side, strict):
        ks = ks_side if side in ("left", "right") else ks_row
        out = SIDES[side][0]
        T = sorted(T, key=lambda t: -(max(t[0] @ out, t[1] @ out) + t[2]))  # nearest that side first
        for push in range(8):
            for tgt in T[:4]:
                for k in ks:
                    lay = layout(runs, tgt, side, push, k)
                    if lay is None or not occ.free(lay[5], lay[6]):
                        continue
                    if strict and _cross(lay[3], lay[4], leaders):
                        continue
                    return lay
        return None

    for runs, names, T, forced in blocks:
        pick = None
        for strict in (True, False):
            for side in ([forced] if forced else ["right", "top", "bottom", "left"]):
                lay = search(runs, T, side, strict)
                if lay is None:
                    continue
                S, Q = lay[3], lay[4]
                Qin = Q + 0.4 * _unit(S - Q)  # not the feature's own edge, where the arrow ends
                cost = (10.0 * _ncross(S, Qin, geo) + 1.0 * (_ncross(S, Qin, occ.S) - _ncross(S, Qin, geo))
                        + 0.05 * float(np.linalg.norm(S - Q)) + 3.0 * nb.get(side, 0) + 0.5 * (side in dim_sides))
                if pick is None or cost < pick[0] - 1e-9:
                    pick = (cost, lay)
            if pick:
                break
        if pick is None:  # pragma: no cover - nothing free: the nearest spot
            side = forced or "right"
            pick = (0, next(lay for tgt in T for k in ks_side if (lay := layout(runs, tgt, side, 0, k))))
        x, base, anchor, S, Q, boxes, segs = pick[1]
        c.line("thin", S, segs[0][2:])
        c.line("thin", S, Q)
        c.leaders.append((*S, *Q))
        c.arrow(Q, Q - S)
        placed = [c.label(x, yb, rr, anchor, name=nm) for yb, rr, nm in zip(base, runs, names)]
        arrow = _segments(c.ops[-1:])
        occ.add(placed, np.concatenate([np.asarray(segs, float), arrow]))
        leaders.append((*S, *Q))


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
    """Agent dimensions ``(view, p1, p2[, text or {text, along, offset, tol}])`` and features picked by
    selection ``(view, faces or circular edges[, text or {text, tol, side}])``."""
    linear: dict = {k: [] for k in views}
    picks: dict = {k: [] for k in views}
    for spec in dims or ():
        if not isinstance(spec, (list, tuple)) or len(spec) < 2:
            raise TuleError(f"a dimension is (view, p1, p2) or (view, circle edges), got {spec!r}", op="drawing")
        vname = spec[0]
        if vname not in views or vname == "iso":
            raise TuleError(f"dimension in view {vname!r}, which is not drawn", op="drawing",
                            hint=f"views drawn: {', '.join(k for k in views if k != 'iso')}")
        v = views[vname]
        if (isinstance(spec[1], Selection) and spec[1].kind in ("edge", "face")
                and (len(spec) == 2 or not _is_point(spec[2]))):
            opts = spec[2] if len(spec) > 2 else {}
            if isinstance(opts, str):
                opts = {"text": opts}
            if not isinstance(opts, dict) or set(opts) - {"text", "tol", "side"}:
                raise TuleError(f"feature options are text, tol and side, got {opts!r}", op="drawing",
                                hint='("right", part.faces.cylindrical(r=3.25), {"tol": "+0.1/0", "side": "top"})')
            if opts.get("side") not in (None, *SIDES):
                raise TuleError(f"side is one of {', '.join(SIDES)}, got {opts['side']!r}", op="drawing")
            geo = []
            for e in spec[1].items:
                if spec[1].kind == "edge":
                    a = BRepAdaptor_Curve(e)
                    if a.GetType() != GeomAbs_Circle:
                        raise TuleError("a diameter dimension needs circular edges", op="drawing",
                                        hint="part.edges.circles(r=3), or a hole's faces: part.faces.cylindrical(r=3)")
                    g = a.Circle()
                    q = g.Location()
                    full = a.IsClosed() or (a.LastParameter() - a.FirstParameter()) > 2 * math.pi - 1e-6
                else:
                    a = BRepAdaptor_Surface(e)
                    if a.GetType() != GeomAbs_Cylinder:
                        raise TuleError("a feature is picked by its cylindrical faces or circular edges", op="drawing",
                                        hint="part.faces.cylindrical(r=3.25)")
                    g = a.Cylinder()
                    q = g.Axis().Location()
                    full = True
                ax = g.Axis().Direction()
                axis = np.array([ax.X(), ax.Y(), ax.Z()])
                if abs(axis @ v.d) < 0.999:  # the picked ones seen as circles here
                    continue
                geo.append((np.array([q.X(), q.Y(), q.Z()]), axis, g.Radius(), full, spec[1].kind))
            if not geo:
                what = "circle" if spec[1].kind == "edge" else "hole"
                raise TuleError(f"no {what} picked is seen as a circle in the {vname} view", op="drawing",
                                hint="use the view that looks along its axis")
            picks[vname].append({"geo": geo, "opts": opts, "used": [False] * len(geo)})
            continue
        if len(spec) < 3:
            raise TuleError("a linear dimension needs two points", op="drawing", hint='("front", p1, p2)')
        opts = spec[3] if len(spec) > 3 else {}
        if isinstance(opts, str):
            opts = {"text": opts}
        if not isinstance(opts, dict) or set(opts) - {"text", "along", "offset", "tol"}:
            raise TuleError(f"dimension options are text, along, offset and tol, got {opts!r}", op="drawing",
                            hint='("top", p1, p2, {"along": "h", "tol": "±0.05", "text": "40", "offset": 12})')
        a, b = v.p(_point(spec[1])), v.p(_point(spec[2]))
        along = opts.get("along")
        if along is None:
            dx, dy = abs(a[0] - b[0]), abs(a[1] - b[1])
            along = "v" if dx < tol else "h" if dy < tol else "aligned"
        if along not in ("h", "v", "aligned"):
            raise TuleError(f"along is 'h', 'v' or 'aligned', got {along!r}", op="drawing")
        t = _tol(opts["tol"]) if "tol" in opts else False
        linear[vname].append((a, b, along, opts.get("text"), opts.get("offset"), t))
    return linear, picks


def _neighbors(name, grid) -> dict:
    c, r = grid[name]
    cells = {grid[k] for k in grid}
    return {"top": (c, r - 1) in cells, "bottom": (c, r + 1) in cells,
            "left": (c - 1, r) in cells, "right": (c + 1, r) in cells}


def _best_view(views, names, d, center, r, tol, open_=(True, True), ends=()):
    """The view looking along ``d`` from an open end of the feature, showing it as a visible circle of
    radius ``r`` at ``center``; else any view along ``d``. A section only shows what its plane cuts
    (``ends``: the feature's end points)."""
    best = None
    for k in names:
        v = views[k]
        if abs(d @ v.d) < 0.999:
            continue
        if v.section and not (ends and min(e @ v.d for e in ends) < v.plane < max(e @ v.d for e in ends)):
            continue
        cx, cy = v.p(center)
        seen = any(p[0] in ("circle", "arc") and abs(p[1] - cx) < tol * 10 and abs(p[2] - cy) < tol * 10
                   and abs(p[3] - r) < tol * 10 for p in v.vis)
        entry = open_[1] if d @ v.d > 0 else open_[0]  # the viewer looks into this end
        score = (entry, seen, -names.index(k))
        if best is None or score > best[0]:
            best = (score, k)
    return best[1] if best else None


def _depth_pieces(thru: bool, depth: float, tols: "_Tols", owner=None):
    if thru:
        return [" THRU"]
    return [f" ↧{_fmt(depth)}", tols.get(f"↧{_fmt(depth)}", owner=owner)]


def _on_axis(c, ax, pts, d, tol) -> bool:
    """A circle or cylinder (center ``c``, axis ``ax``) lies on the axis through one of ``pts`` along ``d``."""
    if abs(abs(ax @ d) - 1) > 1e-6:
        return False
    for p in pts:
        w = c - p
        if np.linalg.norm(w - (w @ d) * d) < tol * 10:
            return True
    return False


def _plan(views: dict, feats, linear: dict, picks: dict, grid: dict, hidden: bool, tol: float, tols: "_Tols",
          sides: dict):
    """Decide every view's dimensions (in view coordinates), callouts and center lines."""
    shape_cyls, slots, holes = feats
    names = [k for k in views if k != "iso"]
    order = sorted(names, key=ORTHO.index)
    nb = {k: _neighbors(k, grid) for k in names}
    plans = {k: {"dims": [], "callouts": [], "centers": [], "nb": nb[k]} for k in names}
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
            d = _Dim(ends[0], ends[1], along, text, side=side)
            d.tol = tols.get(text or d.text)
            plans[k]["dims"].append(d)
    # agent dimensions
    for k in names:
        v = views[k]
        bb = v.bbox()
        mid = np.array([(bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2])
        for a, b, along, text, offset, t in linear[k]:
            m = (np.asarray(a) + np.asarray(b)) / 2
            if along == "h":
                side = "top" if m[1] > mid[1] else "bottom"
            elif along == "v":
                side = "right" if m[0] > mid[0] else "left"
            else:
                side = None
            d = _Dim(a, b, along, text, side=side if offset is None else None)
            d.tol = t if t is not False else tols.get(_fmt(d.value))
            if offset is not None:
                d.offset = float(offset)
            elif side is None:
                d.offset = 8.0 if (m - mid) @ d.n >= 0 else -8.0
            plans[k]["dims"].append(d)
    # holes and slots: "4× ⌀6.6 THRU / ⌴ ⌀11 ↧6.5", in the view that looks into them; a feature picked in
    # dims gets that view, its own callout and the options given
    def pick_of(pts, d, radii):
        for k in names:
            for i, pk in enumerate(picks[k]):
                for j, (c, ax, r, _, _) in enumerate(pk["geo"]):
                    if any(abs(r - rr) < tol * 10 for rr in radii) and _on_axis(c, ax, pts, d, tol):
                        pk["used"][j] = True
                        return (k, i)
        return None

    groups: dict = {}
    for h in holes:
        pk = pick_of([h.o], h.d, [h.r] + ([h.cb.r] if h.cb else []) + ([h.cs[0]] if h.cs else []))
        ends = [c.o + t * c.d for c in (h.cyl, h.cb) if c is not None for t in (c.t0, c.t1)]
        k = pk[0] if pk else _best_view(views, names, h.d, h.o, h.cb.r if h.cb else h.r, tol, h.open, ends)
        if k is None:
            continue
        cx, cy = views[k].p(h.o)
        cb = (round(h.cb.r, 4), round(h.cb.depth, 3)) if h.cb else None
        cs = (round(h.cs[0], 4), round(h.cs[1], 1)) if h.cs else None
        depth = None if h.thru else round(h.cyl.depth, 3)
        groups.setdefault((k, "hole", round(h.r, 4), depth, cb, cs, pk), []).append((cx, cy, cx, cy, h.r))
    for sl in slots:
        pk = pick_of([sl.a, sl.b], sl.d, [sl.r] + ([sl.cb.r] if sl.cb else []))
        ends = [s.a + t * s.d for s in (sl, sl.cb) if s is not None for t in (s.t0, s.t1)]
        k = pk[0] if pk else _best_view(views, names, sl.d, sl.a, sl.cb.r if sl.cb else sl.r, tol, sl.open, ends)
        if k is None:
            continue
        a, b = views[k].p(sl.a), views[k].p(sl.b)
        cb = (round(sl.cb.width, 4), round(sl.cb.length, 3), round(sl.cb.depth, 3)) if sl.cb else None
        depth = None if sl.thru else round(sl.depth, 3)
        groups.setdefault((k, "slot", round(sl.width, 4), round(sl.length, 3), depth, cb, pk), []).append((*a, *b, sl.r))
    for key, cs in sorted(groups.items(), key=lambda kv: (names.index(kv[0][0]), kv[0][1], str(kv[0][2:]))):
        k, kind, pk = key[0], key[1], key[-1]
        opts = picks[pk[0]][pk[1]]["opts"] if pk else {}
        n = len(cs)
        head = f"{n}× " if n > 1 else ""
        owner = (k, head + (f"⌀{_fmt(2 * key[2])}" if kind == "hole" else f"slot {_fmt(key[2])}"), key)

        def tol_of(*keys, main=False):
            if main and "tol" in opts:
                return _tol(opts["tol"])
            return tols.get(*keys, owner=owner)

        if kind == "hole":
            _, _, r, depth, cb, cs_, _ = key
            size = f"⌀{_fmt(2 * r)}"
            lines = [[head + size, tol_of(size, main=True)] + _depth_pieces(depth is None, depth or 0, tols, owner)]
            if cb:
                lines.append([f"⌴ ⌀{_fmt(2 * cb[0])}", tol_of(f"⌀{_fmt(2 * cb[0])}")]
                             + _depth_pieces(False, cb[1], tols, owner))
            if cs_:
                lines.append([f"⌵ ⌀{_fmt(2 * cs_[0])}", tol_of(f"⌀{_fmt(2 * cs_[0])}"), f" × {_fmt(cs_[1])}°"])
        else:
            _, _, w, L, depth, cb, _ = key
            lines = [[head + f"slot {_fmt(w)}", tol_of(f"slot {_fmt(w)}", _fmt(w), main=True), f" × {_fmt(L)}"]
                     + _depth_pieces(depth is None, depth or 0, tols, owner)]
            if cb:
                lines.append([f"⌴ slot {_fmt(cb[0])} × {_fmt(cb[1])}"] + _depth_pieces(False, cb[2], tols, owner))
        if opts.get("text"):
            t = _tol(opts["tol"]) if "tol" in opts else None
            lines = [[ln] for ln in str(opts["text"]).split("\n")]
            lines[0].append(t)
        plans[k]["callouts"].append([lines, sorted(cs), opts.get("side") or sides.get(k)])
    # picked circles that are no feature (a boss, a fillet's arc): a diameter or radius callout
    for k in names:
        v = views[k]
        for pk in picks[k]:
            found: dict = {}
            for (c, ax, r, whole, kind), used in zip(pk["geo"], pk["used"]):
                if used:
                    continue
                if kind == "face":
                    raise TuleError(f"a face picked in the {k} view is not a hole or slot", op="drawing",
                                    hint="pick a hole by its cylindrical faces, a boss or arc by its circular edges")
                cx, cy = v.p(c)
                found.setdefault((round(r, 4), whole), {})[(round(cx / tol), round(cy / tol))] = (cx, cy)
            opts = pk["opts"]
            for (r, whole), cs in found.items():
                n = len(cs)
                size = f"⌀{_fmt(2 * r)}" if whole else f"R{_fmt(r)}"
                t = _tol(opts["tol"]) if "tol" in opts else tols.get(size, owner=(k, size, (size, n, tuple(cs))))
                pieces = [opts["text"]] if opts.get("text") else [(f"{n}× " if n > 1 else "") + size]
                plans[k]["callouts"].append([[pieces + [t]], [(x, y, x, y, r) for x, y in sorted(cs.values())],
                                             opts.get("side") or sides.get(k)])
    # center lines: a cross on circles, the axis on holes and bosses seen from the side, slots end to end
    for k in names:
        v = views[k]
        marks: dict = {}
        axes: dict = {}
        for cy in full:
            along = cy.o @ v.d
            if abs(cy.d @ v.d) > 0.999:
                if not v.keeps(along + cy.t0 * (cy.d @ v.d), along + cy.t1 * (cy.d @ v.d)):
                    continue
                cx, cyy = v.p(cy.o)
                key = (round(cx / tol), round(cyy / tol))
                marks[key] = (cx, cyy, max(cy.r, marks.get(key, (0, 0, 0))[2]))
            elif abs(cy.d @ v.d) < 1e-6 and (hidden and not v.section or not cy.concave or v.section
                                             and abs(along - v.plane) < cy.r) and v.keeps(along - cy.r, along + cy.r):
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
        plans[k]["slots"] = sorted((v.p(sl.a), v.p(sl.b), (sl.cb or sl).r) for sl in slots if abs(sl.d @ v.d) > 0.999
                                   and v.keeps(sl.a @ v.d + sl.t0 * (sl.d @ v.d), sl.a @ v.d + sl.t1 * (sl.d @ v.d)))
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
    for a, b, r in plan["slots"]:
        a, b = np.asarray(a), np.asarray(b)
        u = _unit(b - a)
        n = np.array([-u[1], u[0]])
        out.append((tuple(a - (r + e) * u), tuple(b + (r + e) * u)))
        for p in (a, b):
            out.append((tuple(p - (r + e) * n), tuple(p + (r + e) * n)))
    return out


# ---------------------------------------------------------------- sheet layout


def _scale_text(s: float) -> str:
    return f"{_fmt(s)}:1" if s > 1 else f"1:{_fmt(1 / s)}"


def _place(rects, frame, keep_outs):
    """An offset putting all ``rects`` inside ``frame`` and off every ``keep_outs`` rect, nearest centered; or None."""
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
    for k in keep_outs:
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


# ---------------------------------------------------------------- title block, revisions, notes


# title block fields: key, label, the names ``info`` may use for it
FIELDS = [
    ("number", "Part number", ("number", "part number", "part no", "part no.", "pn", "p/n", "drawing number", "dwg no")),
    ("material", "Material", ("material",)),
    ("finish", "Finish", ("finish", "surface", "treatment", "coating")),
    ("tolerance", "General tolerances", ("tolerance", "tolerances", "general tolerance", "general tolerances")),
    ("drawn", "Drawn", ("drawn", "drawn by", "author")),
    ("date", "Date", ("date",)),
    ("checked", "Checked", ("checked", "checked by")),
    ("approved", "Approved", ("approved", "approved by")),
]
GENERAL_TOLERANCE = "ISO 2768-m"
ROW = 9.0        # title block row height
NOTE = 3.0       # notes text size
NOTE_LH = 4.6    # notes line pitch
REV_LH = 6.0     # revision table row height


def _value(v) -> str:
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, float):
        return _fmt(v)
    return str(v)


def _fields(info, part_name, title):
    """Title block rows ``(label, value)`` and the revision, from ``info`` (known fields first, in order)."""
    known, rest, rev = {}, [], None
    for k, v in (info or {}).items():
        norm = re.sub(r"[_\s]+", " ", str(k).strip().lower())
        if norm in ("rev", "revision"):
            rev = _value(v) if v not in ("", None, False) else None
            continue
        for key, label, names in FIELDS:
            if norm in names:
                known[key] = (label, v)
                break
        else:
            rest.append((str(k).capitalize() if str(k).islower() else str(k), v))
    known.setdefault("tolerance", ("General tolerances", GENERAL_TOLERANCE))
    rows = [("Part", part_name)] if part_name and part_name != title else []
    rows += [known[key] for key, _, _ in FIELDS if key in known]
    rows += rest
    return [(label, _value(v)) for label, v in rows if v not in ("", None, False)], rev


def _wrap(s: str, w: float, size: float) -> list[str]:
    """Words of ``s`` in lines no wider than ``w``."""
    lines: list[str] = []
    for para in str(s).split("\n"):
        cur = ""
        for word in para.split():
            nxt = f"{cur} {word}" if cur else word
            if cur and text_width(nxt, size) > w:
                lines.append(cur)
                cur = word
            else:
                cur = nxt
        lines.append(_fit(cur, w, size))
    return lines


def _tb_rows(rows):
    """Title block rows of one or two cells: a value too wide for half the block gets a whole row."""
    half = TB_W / 2 - 3
    out, pending = [], None
    for label, value in rows:
        if text_width(value, TEXT) > half:
            out.append([(label, value)])
            continue
        if pending is None:
            pending = [(label, value)]
            out.append(pending)
        else:
            pending.append((label, value))
            pending = None
    return out


def _tb_height(rows) -> float:
    return 12.0 + ROW * len(_tb_rows(rows)) + ROW


def _title_block(c: _Canvas, W, title, rows, rev, scale_txt, sheet, projection):
    x1, y0 = W - MARGIN, MARGIN
    x0 = x1 - TB_W
    lines = _tb_rows(rows)
    h = _tb_height(rows)
    c.rect("frame", x0, y0, x1, y0 + h)
    # bottom row: scale, units, projection, sheet
    cells = [("Scale", scale_txt), ("Units", "mm"), ("Projection", None), ("Sheet", sheet)]
    cw = TB_W / 4
    for i, (label, value) in enumerate(cells):
        cx0 = x0 + i * cw
        if i:
            c.line("thin", (cx0, y0), (cx0, y0 + ROW))
        c.text(cx0 + 1.5, y0 + ROW - 2.6, label, size=2.0)
        if value is not None:
            c.text(cx0 + 1.5, y0 + 1.8, value, size=TEXT)
        else:
            _projection_symbol(c, cx0 + cw / 2 + 2.5, y0 + ROW / 2 - 1.2, projection)
    y = y0 + ROW
    for pair in reversed(lines):
        c.line("thin", (x0, y), (x1, y))
        for i, (label, value) in enumerate(pair):
            w = TB_W if len(pair) == 1 else TB_W / 2
            cx0 = x0 + i * TB_W / 2
            if i:
                c.line("thin", (cx0, y), (cx0, y + ROW))
            c.text(cx0 + 1.5, y + ROW - 2.6, label, size=2.0)
            size = TEXT if text_width(value, TEXT) <= w - 3 else max(2.5, TEXT * (w - 3) / text_width(value, TEXT))
            c.text(cx0 + 1.5, y + 1.8, _fit(value, w - 3, size), size=size)
        y += ROW
    c.line("frame", (x0, y), (x1, y))
    tw = TB_W - 4 - (20 if rev else 0)
    c.text(x0 + 2, y + 3.6, _fit(title, tw, 5.0, True), size=5.0, bold=True)
    if rev:
        c.line("thin", (x1 - 20, y), (x1 - 20, y0 + h))
        c.text(x1 - 18.5, y0 + h - 2.6, "Rev", size=2.0)
        c.text(x1 - 10, y + 2.2, _fit(rev, 17, 5.0, True), size=5.0, anchor="middle", bold=True)
    return (x0, y0, x1, y0 + h)


def _revisions(revisions):
    """``[(rev, description[, date]), ...]`` checked; None for none."""
    if not revisions:
        return []
    out = []
    for r in revisions:
        if not isinstance(r, (list, tuple)) or len(r) not in (2, 3):
            raise TuleError(f"a revision is (rev, description) or (rev, description, date), got {r!r}",
                            op="drawing", hint='revisions=[("A", "First issue"), ("B", "Slots 2 mm longer")]')
        out.append(tuple(_value(v) for v in r))
    return out


def _rev_layout(revs):
    """Column widths and each row's wrapped description lines."""
    dated = any(len(r) == 3 for r in revs)
    cols = (12.0, TB_W - 12.0 - (24.0 if dated else 0.0), 24.0 if dated else 0.0)
    return cols, [_wrap(r[1], cols[1] - 3, NOTE) for r in revs]


def _rev_height(revs) -> float:
    if not revs:
        return 0.0
    _, desc = _rev_layout(revs)
    return 5.0 + sum(REV_LH + NOTE_LH * (len(d) - 1) for d in desc)


def _revision_table(c: _Canvas, x0, y0, revs):
    """The revision table, its bottom at ``y0``: a header, then one row per revision, newest at the bottom."""
    cols, desc = _rev_layout(revs)
    h = _rev_height(revs)
    x1 = x0 + TB_W
    y1 = y0 + h
    c.rect("thin", x0, y0, x1, y1)
    xs = [x0, x0 + cols[0], x0 + cols[0] + cols[1]]
    y = y1 - 5.0
    c.line("thin", (x0, y), (x1, y))
    for x, label, w in zip(xs, ("Rev", "Description", "Date"), cols):
        if w:
            c.text(x + 1.5, y + 1.6, label, size=2.0)
    for r, d in zip(revs, desc):
        rh = REV_LH + NOTE_LH * (len(d) - 1)
        for j, line in enumerate(d):
            c.text(xs[1] + 1.5, y - 4.3 - j * NOTE_LH, line, size=NOTE)
        c.text(xs[0] + 1.5, y - 4.3, _fit(r[0], cols[0] - 3, NOTE), size=NOTE)
        if len(r) == 3:
            c.text(xs[2] + 1.5, y - 4.3, _fit(r[2], cols[2] - 3, NOTE), size=NOTE)
        y -= rh
        if y > y0 + 1e-6:
            c.line("thin", (x0, y), (x1, y))
    for x, w in zip(xs[1:], cols[1:]):
        if w:
            c.line("thin", (x, y0), (x, y1))


def _notes(notes) -> list[str]:
    if not notes:
        return []
    if isinstance(notes, str):
        notes = [n for n in notes.split("\n") if n.strip()]
    if not isinstance(notes, (list, tuple)) or not all(isinstance(n, str) for n in notes):
        raise TuleError("notes is a list of text", op="drawing", hint='notes=["Break all edges 0.3", "Anodize clear"]')
    return [n.strip() for n in notes]


def _notes_lines(notes, w):
    """``(number or "", text)`` lines of the NOTES block, wrapped to width ``w``."""
    out = []
    for i, n in enumerate(notes, 1):
        for j, line in enumerate(_wrap(n, w - 6, NOTE)):
            out.append((f"{i}." if j == 0 else "", line))
    return out


def _notes_height(notes, w) -> float:
    return 7.0 + NOTE_LH * len(_notes_lines(notes, w)) if notes else 0.0


def _notes_block(c: _Canvas, x0, y0, w, notes):
    """NOTES, numbered, its bottom at ``y0``."""
    lines = _notes_lines(notes, w)
    y = y0 + _notes_height(notes, w) - 4.0
    c.text(x0, y, "NOTES", size=TEXT, bold=True)
    y -= 7.0 - 0.6
    for num, line in lines:
        if num:
            c.text(x0, y, num, size=NOTE)
        c.text(x0 + 6, y, line, size=NOTE)
        y -= NOTE_LH


def _fit(s: str, w: float, size: float, bold=False) -> str:
    if text_width(s, size, bold) <= w:
        return s
    while s and text_width(s + "…", size, bold) > w:
        s = s[:-1]
    return s + "…"


def _projection_symbol(c: _Canvas, cx, cy, projection):
    """ISO 5456-2: a truncated cone, narrow end left, and its end view (circles) right (first) or left (third)."""
    R, r, L = 2.0, 1.0, 5.0
    sg = 1.0 if projection == "first" else -1.0
    tx = cx - sg * 4.2  # cone center
    ex = cx + sg * 4.2  # circles center
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
           scale=None, hidden=True, projection="first", name=None, notes=None, tol=None, revisions=None,
           callouts=None):
    """Lay out a drawing; returns ``(canvas, (width, height), facts)``. See ``outputs.drawing``."""
    shape, part_name = _as_shape(item)
    if sheet not in SHEETS:
        raise TuleError(f"unknown sheet {sheet!r}", op="drawing", hint=", ".join(SHEETS))
    if projection not in GRID:
        raise TuleError(f"projection is 'first' or 'third', got {projection!r}", op="drawing")
    cuts = {}  # sectioned views: name: point
    names = []
    for v in views:
        if isinstance(v, (list, tuple)) and len(v) == 2 and isinstance(v[0], str):
            if v[0] not in ORTHO:
                raise TuleError(f"a section is a view and a point, like ('front', (0, 0, 0)), got {v!r}", op="drawing",
                                hint="the view is one of " + ", ".join(ORTHO))
            cuts[v[0]] = _point(v[1])
            v = v[0]
        if not isinstance(v, str):
            raise TuleError(f"a view is a name or (name, point) for a section, got {v!r}", op="drawing",
                            hint='views=(("front", (0, 0, 0)), "top", "right", "iso")')
        names.append(v)
    views = list(dict.fromkeys(names))
    if not views:
        raise TuleError("no views to draw", op="drawing", hint='views=("front", "top", "right", "iso")')
    if isinstance(callouts, str) or callouts is None:
        callouts = {k: callouts for k in views} if callouts else {}
    if not isinstance(callouts, dict) or set(callouts.values()) - set(SIDES) or set(callouts) - set(views):
        raise TuleError(f"callouts is a side ({', '.join(SIDES)}) or a dict of view: side, got {callouts!r}",
                        op="drawing", hint='callouts={"right": "top"}')
    W, H = SHEETS[sheet]
    wrapped = shape.wrapped
    lo, hi = shape.bounds
    diag = float(np.linalg.norm(np.subtract(hi, lo)))
    if diag <= 0:
        raise TuleError("the shape is empty", op="drawing")
    tol_ = max(1e-4, diag * 1e-6)
    tols = _Tols(tol)
    notes = _notes(notes)
    revs = _revisions(revisions)
    V = {}
    for i, k in enumerate(views):
        V[k] = _View(k)
        if k in cuts:  # a section: no hidden lines, cut faces hatched
            V[k].section = "ABCDEFGHJKLMNPRSTUVWXYZ"[len([c for c in views[:i] if c in cuts])]
            _hlr(_section(wrapped, V[k], cuts[k], diag, tol_), V[k], False, tol_)
        else:
            _hlr(wrapped, V[k], hidden and k != "iso", tol_)
    for k, v in V.items():  # each section's plane marked on the first plain view that sees it edge-on
        if v.section:
            parent = next((V[j] for j in views if j != "iso" and not V[j].section and abs(V[j].d @ v.d) < 1e-6), None)
            if parent is not None:
                w = np.cross(parent.d, v.d)
                sight = -v.d
                parent.cutlines.append((v.section, parent.p(cuts[k]), _unit([w @ parent.x, w @ parent.y]),
                                        _unit([sight @ parent.x, sight @ parent.y])))
    grid = {k: GRID[projection][k] for k in views if k != "iso"}
    feats = _features(wrapped, tol_)
    linear, picks = _parse_dims(dims, V, tol_)
    plans = _plan(V, feats, linear, picks, grid, hidden, tol_, tols, callouts)
    tols.check()

    title = title or part_name or name or "Drawing"
    rows, rev = _fields(info, part_name or name, title)
    if revs:
        if rev and rev != revs[-1][0]:
            raise TuleError(f"info rev {rev!r} is not the last revision {revs[-1][0]!r}", op="drawing",
                            hint="the last entry of revisions is the current one; drop rev from info")
        rev = revs[-1][0]
    frame = (MARGIN + 3, MARGIN + 3, W - MARGIN - 3, H - MARGIN - 3)
    tb_h = _tb_height(rows)
    rv_h = _rev_height(revs)
    # where the notes go: above the title block, or in the bottom left corner
    layouts = []
    nh = _notes_height(notes, TB_W)
    col = tb_h + rv_h + (4.0 + nh if notes else 0.0)
    layouts.append(("right", [(W - MARGIN - TB_W - 3, MARGIN - 1, W - MARGIN + 1, MARGIN + col + 3)]))
    if notes:
        nw = min(TB_W + 20, W - 2 * MARGIN - TB_W - 16)
        nh2 = _notes_height(notes, nw)
        layouts.append(("left", [(W - MARGIN - TB_W - 3, MARGIN - 1, W - MARGIN + 1, MARGIN + tb_h + rv_h + 3),
                                 (MARGIN - 1, MARGIN - 1, MARGIN + 4 + nw + 3, MARGIN + 4 + nh2 + 3)]))

    def fit(s):
        """The views at scale ``s`` and where they go, or None when they do not fit the sheet."""
        canv = {}
        for k in grid:
            p = plans[k]
            c = _annotate(V[k], s, p["dims"], p["callouts"], _centers(p, s), p["nb"])
            bb = V[k].bbox()
            canv[k] = (c, (bb[0] * s, bb[1] * s, bb[2] * s, bb[3] * s))
        off, rects, cells = _grid_block(canv, grid, s) if canv else ({}, [], [])
        for where, keep in layouts:
            if "iso" not in V:
                at = _place(rects, frame, keep)
                if at:
                    return canv, off, at, None, None, where
                continue
            ib = V["iso"].bbox()
            # the isometric view may be one standard scale smaller than the others (labelled)
            for si in [s] + ([x for x in SCALES if x < s][:1] if canv else []):
                iw, ih = (ib[2] - ib[0]) * si, (ib[3] - ib[1]) * si
                below = 7.0 if si != s else 0.0  # its scale, under it
                for sx, sy in _iso_spots(rects, cells, iw, ih + below):
                    ir = (sx, sy, sx + iw, sy + ih + below)
                    if any(_overlaps(ir, r, GAP) for r in rects):
                        continue
                    at = _place(rects + [ir], frame, keep)
                    if at:
                        return canv, off, at, (sx - ib[0] * si, sy + below - ib[1] * si), si, where
        return None

    fixed = _parse_scale(scale)
    if fixed:
        s, placed = fixed, fit(fixed)
        if placed is None:
            std = next((x for x in SCALES if x < fixed and fit(x)), None)
            nice = next((x for x in NICE if x < fixed and (std is None or x > std) and fit(x)), None)
            hints = []
            if nice:
                hints.append(f'scale="{_scale_text(nice)}" fits')
            if std:
                hints.append(f'"{_scale_text(std)}" is the largest ISO 5455 scale that fits (leave scale out to get it)')
            bigger = next((k for k, v in SHEETS.items() if v[0] > W), None)
            hints.append((f'or a larger sheet (sheet="{bigger}", …) or' if bigger else "or") + " fewer views")
            raise TuleError(f"the views do not fit {sheet} at {_scale_text(fixed)}", op="drawing", hint="; ".join(hints))
    else:
        s, placed = next(((x, f) for x in SCALES if (f := fit(x))), (SCALES[-1], None))
        if placed is None:
            raise TuleError(f"the views do not fit {sheet} even at {_scale_text(SCALES[-1])}", op="drawing",
                            hint="use a larger sheet or fewer views")
    canv, off, (ax, ay), iso_at, iso_s, where = placed

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
    _title_block(page, W, title, rows, rev, _scale_text(s), sheet, projection)
    x0 = W - MARGIN - TB_W
    if revs:
        _revision_table(page, x0, MARGIN + tb_h, revs)
    if notes:
        if where == "right":
            _notes_block(page, x0 + 1, MARGIN + tb_h + rv_h + 4.0, TB_W - 1, notes)
        else:
            _notes_block(page, MARGIN + 4, MARGIN + 4, min(TB_W + 20, W - 2 * MARGIN - TB_W - 16), notes)
    facts = {"scale": _scale_text(s), "sheet": sheet, "title": title, "rev": rev,
             "texts": [t for t, _ in page.labels]}
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
           ".f{fill:#000;stroke:none}text{font-family:Helvetica,Arial,sans-serif;fill:#000;white-space:pre}</style>",
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
          title=None, info=None, sheet="A4", scale=None, hidden=True, projection="first", dxf_view=None,
          notes=None, tol=None, revisions=None, callouts=None) -> dict:
    """Write ``<base>.pdf`` and ``<base>.svg`` (and ``<base>.dxf``) into ``folder``; returns facts (scale, texts)."""
    page, size, facts = render(item, views, dims, title, info, sheet, scale, hidden, projection, base,
                               notes=notes, tol=tol, revisions=revisions, callouts=callouts)
    write_atomic(os.path.join(folder, base + ".pdf"), to_pdf(page, size, facts["title"]))
    write_atomic(os.path.join(folder, base + ".svg"), to_svg(page, size, facts["title"]))
    if dxf_view:
        dxf(item, os.path.join(folder, base + ".dxf"), dxf_view)
    return facts
