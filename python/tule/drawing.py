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
other text (:func:`collisions` checks a page); leaders cross no leader and no
dimension line. GD&T (datum feature symbols, feature control frames, basic
sizes) hangs on model faces through ``datums`` and ``dims``. ``sheets`` spreads
the views over pages of one PDF, each feature called out once; ``{name: part}``
draws parts one after another, and ``table`` adds a sheet. ``explode`` draws an
assembly moved apart, with numbered balloons and a parts list. The scale is
1:1 when it fits, else the largest ISO 5455 one. Output is deterministic (no
dates), so a rebuild that changes nothing writes the same bytes.
"""

from __future__ import annotations

import math
import os
import re
import sys
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
    "bend": (0.25, (8.0, 1.5, 0.5, 1.5)),  # sheet metal bend lines: dash-dot
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


def _circ(cx, cy, r, a0=0.0, a1=360.0, n=20):
    return tuple((cx + r * math.cos(math.radians(a0 + (a1 - a0) * i / n)),
                  cy + r * math.sin(math.radians(a0 + (a1 - a0) * i / n))) for i in range(n + 1))


# geometric characteristics (ISO 1101) and material modifiers, drawn as strokes in frames
_SYMS.update({
    "⏤": [((0.1, 0.36), (0.9, 0.36))],                                                    # straightness
    "⏥": [((0.04, 0.14), (0.7, 0.14), (0.96, 0.6), (0.3, 0.6), (0.04, 0.14))],            # flatness
    "○": [_circ(0.5, 0.36, 0.34)],                                                         # circularity
    "⌭": [_circ(0.5, 0.36, 0.27), ((0.05, 0.02), (0.55, 0.74)), ((0.45, -0.02), (0.95, 0.7))],  # cylindricity
    "⌒": [_circ(0.5, 0.12, 0.4, 0, 180)],                                                 # profile of a line
    "⌓": [_circ(0.5, 0.12, 0.4, 0, 180), ((0.1, 0.12), (0.9, 0.12))],                     # profile of a surface
    "∥": [((0.22, 0.02), (0.5, 0.72)), ((0.5, 0.02), (0.78, 0.72))],                      # parallelism
    "⊥": [((0.5, 0.04), (0.5, 0.74)), ((0.1, 0.04), (0.9, 0.04))],                        # perpendicularity
    "∠": [((0.9, 0.04), (0.1, 0.04), (0.78, 0.62))],                                      # angularity
    "⌖": [_circ(0.5, 0.36, 0.26), ((0.08, 0.36), (0.92, 0.36)), ((0.5, -0.06), (0.5, 0.78))],  # position
    "◎": [_circ(0.5, 0.36, 0.34), _circ(0.5, 0.36, 0.16)],                                # concentricity
    "⌯": [((0.08, 0.36), (0.92, 0.36)), ((0.26, 0.12), (0.74, 0.12)), ((0.26, 0.6), (0.74, 0.6))],  # symmetry
    "↗": [((0.2, 0.04), (0.8, 0.7)), ((0.5, 0.68), (0.8, 0.7), (0.78, 0.4))],             # circular runout
    "⌰": [((0.1, 0.04), (0.5, 0.7)), ((0.36, 0.66), (0.5, 0.7), (0.52, 0.52)), ((0.45, 0.04), (0.85, 0.7)),
          ((0.71, 0.66), (0.85, 0.7), (0.87, 0.52)), ((0.05, 0.04), (0.6, 0.04))],          # total runout
    "Ⓜ": [_circ(0.5, 0.36, 0.42), ((0.3, 0.16), (0.3, 0.56), (0.5, 0.3), (0.7, 0.56), (0.7, 0.16))],
    "Ⓛ": [_circ(0.5, 0.36, 0.42), ((0.36, 0.56), (0.36, 0.16), (0.66, 0.16))],
})
_WIDTH.update({k: 1000 for k in "⏤⏥○⌭⌒⌓∥⊥∠⌖◎⌯↗⌰"})
_WIDTH.update({"Ⓜ": 900, "Ⓛ": 900})


def _split_syms(s: str) -> list[str]:
    return [p for p in re.split("([" + "".join(_SYMS) + "])", s) if p]


# ---------------------------------------------------------------- GD&T: feature control frames, datums

# characteristic: symbol, names an agent may write, whether it takes datums (None: optional)
_GDT = [
    ("⏤", ("straightness", "straight"), False), ("⏥", ("flatness", "flat"), False),
    ("○", ("circularity", "roundness", "round"), False), ("⌭", ("cylindricity",), False),
    ("⌒", ("profile of a line", "line profile"), None), ("⌓", ("profile of a surface", "profile", "surface profile"), None),
    ("∥", ("parallelism", "parallel"), True), ("⊥", ("perpendicularity", "perpendicular", "squareness"), True),
    ("∠", ("angularity",), True), ("⌖", ("position", "true position"), None),
    ("◎", ("concentricity", "coaxiality"), True), ("⌯", ("symmetry",), True),
    ("↗", ("circular runout", "runout"), True), ("⌰", ("total runout",), True),
]
DATUM_LETTERS = "ABCDEFGHJKLMNPRSTUVWXYZ"
FRAME_H = 7.0  # a frame's height: twice the text


class _Frame:
    """A feature control frame: ``[symbol | ⌀0.1 Ⓜ | A | B | C]``."""

    def __init__(self, spec):
        hint = ('"flatness 0.05", "parallel 0.05 A", "perpendicular ⌀0.02 A", "position ⌀0.1 M A B C" '
                '(M or L after a size: MMC, LMC)')
        if not isinstance(spec, str) or not spec.strip():
            raise TuleError(f"a frame is text like 'position ⌀0.1 A B C', got {spec!r}", op="drawing", hint=hint)
        s = spec.strip().replace("Ø", "⌀").replace("ø", "⌀").replace("|", " ").replace("(M)", "Ⓜ").replace("(L)", "Ⓛ")
        low = s.lower()
        found = None
        for sym, names, needs in _GDT:
            for nm in (sym,) + names:
                if low.startswith(nm) and (found is None or len(nm) > len(found[1])):
                    found = (sym, nm, needs)
        if found is None:
            raise TuleError(f"frame {spec!r}: no geometric characteristic", op="drawing",
                            hint="one of " + ", ".join(n[0] for _, n, _ in _GDT) + "; e.g. " + hint)
        sym, nm, needs = found
        toks = s[len(nm):].split()
        m = re.fullmatch(r"(⌀|dia\.?)?(" + _NUM + r")(Ⓜ|Ⓛ|M|L)?", toks[0], flags=re.I) if toks else None
        if not m:
            raise TuleError(f"frame {spec!r}: the tolerance (a number, '⌀0.1' for a cylindrical zone) "
                            f"comes after the characteristic", op="drawing", hint=hint)
        mod = {"m": "Ⓜ", "l": "Ⓛ"}.get((m.group(3) or "").lower(), m.group(3) or "")
        toks = toks[1:]
        if toks and toks[0].upper() in ("M", "L", "Ⓜ", "Ⓛ", "MMC", "LMC"):
            mod = "Ⓜ" if toks[0].upper() in ("M", "Ⓜ", "MMC") else "Ⓛ"
            toks = toks[1:]
        self.tol = ("⌀" if m.group(1) else "") + _fmt(abs(float(m.group(2)))) + (" " + mod if mod else "")
        self.datums = []
        for t in toks:
            dm = re.fullmatch(r"([A-Z](?:-[A-Z])?)(Ⓜ|Ⓛ|M|L)?", t)
            if not dm:
                raise TuleError(f"frame {spec!r}: {t!r} is no datum reference (a capital letter, like A)",
                                op="drawing", hint=hint)
            mm = dm.group(2) or ""
            self.datums.append(dm.group(1) + {"M": " Ⓜ", "L": " Ⓛ"}.get(mm, " " + mm if mm else ""))
        if needs is False and self.datums:
            raise TuleError(f"frame {spec!r}: {nm} is a form tolerance and takes no datum",
                            op="drawing", hint="drop the datum letters")
        if needs is True and not self.datums:
            raise TuleError(f"frame {spec!r}: {nm} needs a datum", op="drawing", hint=f"'{nm} {self.tol} A'")
        self.sym = sym
        self.cells = [sym, self.tol] + self.datums
        self.widths = [FRAME_H] + [max(FRAME_H, text_width(c) + 3.0) for c in self.cells[1:]]
        self.width = sum(self.widths)
        self.name = " | ".join(self.cells)

    def letters(self):
        return [d.split()[0] for d in self.datums]


def _frames(spec) -> list:
    if spec is None:
        return []
    return [_Frame(x) for x in ([spec] if isinstance(spec, str) else spec)]


class _DatumTag:
    """A datum letter hung under a callout (a datum feature of size: a bore, a slot)."""

    def __init__(self, letter):
        self.letter = letter
        self.width = FRAME_H
        self.name = f"datum {letter}"


# ---------------------------------------------------------------- tolerances


_NUM = r"[+-]?(?:\d+\.?\d*|\.\d+)"


def _tol(spec):
    """A tolerance: ``("pm", "0.1")``, ``("dev", "+0.05", "0")`` or ``("fit", "H7")``; None for none."""
    if spec is None or spec == "":
        return None
    hint = '"±0.1", "+0.05/0", "0/-0.1", "H7", "g6", "basic" (boxed: GD&T), or a number for ±'
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
        if s.lower() in ("basic", "□", "[]"):
            return ("basic",)
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
    if t is None or t[0] == "basic":
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
        if p is None or isinstance(p, tuple) and p[0] == "basic":
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
        self.sheet = 0                   # the sheet it is on (0: the first)
        self.bends: list = []            # bend lines of a flat pattern: ((x0, y0), (x1, y1), label)

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
        self.dimsegs: list = []  # dimension lines (x0, y0, x1, y1): no leader may cross one
        self.extsegs: list = []  # extension lines: a leader crosses one only where nothing else is free
        self.frames: list = []   # feature control frames and datums, as text (facts)
        self.dimtexts: list = []  # (x0, y0, x1, y1, box): a dimension line and its text's box

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
        self.dimsegs += [(x0 + dx, y0 + dy, x1 + dx, y1 + dy) for x0, y0, x1, y1 in other.dimsegs]
        self.extsegs += [(x0 + dx, y0 + dy, x1 + dx, y1 + dy) for x0, y0, x1, y1 in other.extsegs]
        self.frames += other.frames
        self.dimtexts += [(x0 + dx, y0 + dy, x1 + dx, y1 + dy, (b[0] + dx, b[1] + dy, *b[2:]))
                          for x0, y0, x1, y1, b in other.dimtexts]

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
        if _cross(L[:2], L[2:], page.dimsegs):
            out.append(f"the leader at {tuple(round(v, 1) for v in L[2:])} crosses a dimension line")
        if _cross(L[:2], L[2:], page.extsegs):
            out.append(f"the leader at {tuple(round(v, 1) for v in L[2:])} crosses an extension line")
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

    def __init__(self, a, b, along, text=None, offset=None, side=None, tol=None, label=None):
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
        self.label = label  # after the value: "2.86 ROOT"
        self.pad = 0.0    # model mm added to ``offset`` (paper mm), along n

    @property
    def n(self):
        return np.array([-self.u[1], self.u[0]])

    @property
    def basic(self) -> bool:
        return self.tol is not None and self.tol[0] == "basic"

    @property
    def pieces(self):
        return [self.text, self.tol] + ([f" {self.label}"] if self.label else [])


def _box_of(x, y, runs, anchor="start", angle=0.0):
    """The box :meth:`_Canvas.label` would give these runs."""
    w, y0, y1 = _runs_extent(runs)
    u0 = {"start": 0.0, "middle": -w / 2, "end": -w}[anchor]
    cs, sn = math.cos(math.radians(angle)), math.sin(math.radians(angle))
    u, v = u0 + w / 2, (y0 + y1) / 2
    return (x + u * cs - v * sn, y + u * sn + v * cs, cs, sn, w / 2, (y1 - y0) / 2)


def _dim_lines(c: _Canvas, dim: _Dim, s: float, L: float, clines=(), far=False):
    """Draw ``dim``'s lines (scale ``s``, dimension line where ``point · n == L``, paper mm).

    Extension lines start a gap off the feature; from a center (``clines``: the center lines drawn, sheet
    mm), a gap past the end of the center line they would run along.
    Returns its text runs, the text angle, the spots for the text, best first
    (``(baseline point, anchor, extra line or None)``), its name, bold and boxed (a basic size); and the
    line's ends. The spots are on the line or just beyond an arrow; ``far``: further along it too.
    """
    u, n = dim.u, dim.n
    a, b = dim.a * s, dim.b * s
    if (b - a) @ u < 0:
        a, b = b, a
    A, B = a + (L - a @ n) * n, b + (L - b @ n) * n
    for p, P in ((a, A), (b, B)):
        dist = L - p @ n
        sg = 1.0 if dist > 0 else -1.0
        start = 1.0
        for q0, q1 in clines:
            d = np.subtract(q1, q0)
            if abs(d[0] * n[1] - d[1] * n[0]) > 1e-6 * (abs(d[0]) + abs(d[1])):
                continue  # not along the extension line
            w = np.subtract(p, q0)
            if abs(w[0] * n[1] - w[1] * n[0]) > 1e-3:
                continue  # not through the point
            t0, t1 = sorted((float((np.subtract(q0, p)) @ n) * sg, float((np.subtract(q1, p)) @ n) * sg))
            if t0 <= 1e-6 <= t1 + 1e-6:
                start = max(start, t1 + 1.0)
        if abs(dist) > start + 0.5:
            c.line("thin", p + sg * start * n, P + sg * 2.0 * n)
            c.extsegs.append((*(p + sg * start * n), *(P + sg * 2.0 * n)))
    t = u if (u[0] > 1e-9 or (abs(u[0]) <= 1e-9 and u[1] > 0)) else -u
    up = np.array([-t[1], t[0]])
    angle = math.degrees(math.atan2(t[1], t[0]))
    span = float(np.linalg.norm(B - A))
    runs = _runs(dim.pieces)
    w, y0, _ = _runs_extent(runs)
    lift = (0.5 - y0 + (BOX if dim.basic else 0.0)) * up  # the text's box clear of the line
    inside = span >= w + 2 * ARROW[0] + 3
    if inside:
        c.line("thin", A, B)
        c.dimsegs.append((*A, *B))
        c.arrow(A, A - B)
        c.arrow(B, B - A)
    else:  # too short: arrows from outside
        d = (B - A) / span if span > 1e-9 else u
        c.line("thin", A - 5 * d, B + 5 * d)
        c.dimsegs.append((*(A - 5 * d), *(B + 5 * d)))
        c.arrow(A, d)
        c.arrow(B, -d)
    spots = []
    if inside:
        m = (A + B) / 2
        spots.append((m + lift, "middle", None))
        room = (span - w) / 2 - ARROW[0] - 1.0
        k = 2.0
        while k <= room:
            spots += [(m + k * t + lift, "middle", None), (m - k * t + lift, "middle", None)]
            k += 2.0
    hi, lo = (B, A) if (B - A) @ t >= 0 else (A, B)
    # beyond either arrow, on the dimension line drawn longer: just past it, or (far) anywhere along it
    for g in [6.0, 8.0, 10.0] + (list(np.arange(14.0, 170.0, 4.0)) if far else []):
        bare = g == 6.0 and not inside
        p = hi + g * t
        spots.append((p + lift, "start", None if bare else (hi, p + (w + 0.5) * t)))
        p = lo - g * t
        spots.append((p + lift, "end", None if bare else (lo, p - (w + 0.5) * t)))
    if dim.basic:
        name = f"[{dim.text}]"
    else:
        name = dim.text if dim.tol is None and not dim.label else _runs_name(dim.pieces)
    if dim.basic:  # the box's side clear of the dimension line's end
        spots = [(p + BOX * (t if anchor == "start" else -t if anchor == "end" else 0), anchor, x, *rest)
                 for p, anchor, x, *rest in spots]
    return (runs, angle, spots, name, False, dim.basic), (A, B)


BOX = 0.8  # a basic size's box, off its text


def _place_text(c: _Canvas, occ: "_Occ", runs, angle, spots, name, bold=False, boxed=False):
    """Put a text on the first free spot (the first spot when none is); whether a free spot took it, and its box.

    ``spots``: ``(baseline point, anchor, extra line or None)``. ``boxed``: framed (a basic size).
    """
    pick, free = spots[0], False
    grow = BOX if boxed else 0.0
    for spot in spots:
        p, anchor, extra = spot[:3]
        segs = [(*extra[0], *extra[1])] if extra is not None else []
        box = _box_of(p[0], p[1], runs, anchor, angle)
        box = (*box[:4], box[4] * (1.06 if bold else 1.0) + grow, box[5] + grow)
        if occ.free([box], segs):
            pick, free = spot, True
            break
    p, anchor, extra = pick[:3]
    segs = []
    if extra is not None:
        c.line("thin", extra[0], extra[1])
        c.dimsegs.append((*extra[0], *extra[1]))
        segs = [(*extra[0], *extra[1])]
    box = c.label(p[0], p[1], runs, anchor, angle, bold=bold, name=name)
    if boxed:
        cx, cy, cs, sn, hw, hh = box
        hw, hh = hw + BOX, hh + BOX
        pts = [(cx + u * cs - v * sn, cy + u * sn + v * cs) for u, v in ((-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh))]
        c.path("thin", [("M", *pts[0])] + [("L", *q) for q in pts[1:]] + [("Z",)])
        box = (cx, cy, cs, sn, hw, hh)
    occ.add([box], segs)
    return free, box


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


def _annotate(view: _View, s: float, plan: dict) -> _Canvas:
    """Geometry, center lines, dimensions, datums and callouts of one view at scale ``s`` (view-local sheet mm).

    Lines go first; then every text takes the first spot that touches no line and no other text. A
    dimension whose text finds no free spot on or beside its own line moves out a tier (stacks) and the
    dimensions are drawn again; the last try also lets a text slide further along its line.
    """
    dims, callouts, nb = plan["dims"], plan["callouts"], plan["nb"]
    base = _Canvas()
    for p in view.hid:
        base.path("hid", _prim_cmds(p, s))
    for p in view.vis:
        base.path("vis", _prim_cmds(p, s))
    for p, q in _hatch([[(x * s, y * s) for x, y in L] for L in view.cut]):
        base.line("thin", p, q)
    for p, q, _ in view.bends:
        base.line("bend", (p[0] * s, p[1] * s), (q[0] * s, q[1] * s), 4.0)
    geo = _segments(base.ops)  # the part's own lines: leaders should cross as few as possible
    marks = [(np.asarray(p) * s, np.asarray(q) * s) for p, q in _centers(plan, s)]  # extension lines go past them
    for p, q in marks:
        base.center_line(p, q)
    bb = view.bbox()
    G = (bb[0] * s, bb[1] * s, bb[2] * s, bb[3] * s)
    bump: dict = {}
    for i in range(BUMPS + 1):
        c = _Canvas()
        c.add(base)
        reach, occ, stuck = _dims_drawn(c, view, s, dims, G, marks, bump, last=i == BUMPS)
        if not stuck:
            break
        for d in stuck:
            bump[id(d)] = bump.get(id(d), 0) + 1
    for p, q, label in view.bends:
        _bend_label(c, occ, np.asarray(p) * s, np.asarray(q) * s, label)
    leaders: list = []
    if plan.get("datums"):
        _draw_datum_planes(c, occ, plan["datums"], s, geo, leaders)
    if callouts:
        _draw_callouts(c, occ, G, callouts, s, reach, nb, geo, {d.side for d in dims}, leaders)
    if view.section:
        title = f"{view.section}-{view.section}"
        runs = _runs([title], 5.0)
        x = (G[0] + G[2]) / 2
        top = max(G[3] + reach["top"], c.bbox()[3] if c.ops else G[3])
        _place_text(c, occ, runs, 0.0, [(np.array([x, top + 4 + 2 * k]), "middle", None) for k in range(40)],
                    title, bold=True)
    return c


BUMPS = 6  # how often a crowded dimension may move out a tier


def _dims_drawn(c: _Canvas, view, s, dims, G, marks, bump, last=False):
    """Dimensions (stacked outward per side, shorter ones closer; ``bump``: tiers further out) and section
    cut lines on ``c``. Returns each side's reach, what is occupied, and the dimensions whose text found no
    free spot."""
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
            texts.append((t, d, *_dim_lines(c, d, s, L, marks, last)))
        if tiers:
            reach[side] = FIRST + STEP * (len(tiers) - 1) + TEXT + 1.5
    for d in dims:
        if d.side is None:  # placed by its offset: out by a tier per bump
            off = d.offset + math.copysign(STEP * bump.get(id(d), 0), d.offset) if d.offset else 0.0
            texts.append((0, d, *_dim_lines(c, d, s, float(d.a * s @ d.n) + off + d.pad * s, marks, last)))
    # section cutting planes seen edge-on here: a chain line, thick ends, arrows looking along the cut
    letters = []
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
            letters.append((_runs([letter], 5.0), spots, letter))
    occ = _Occ(_segments(c.ops))
    stuck = []
    for t, d, tx, (A, B) in sorted(texts, key=lambda it: it[0]):  # inner tiers first
        free, box = _place_text(c, occ, *tx)
        c.dimtexts.append((*A, *B, box))
        if not free and (d.side or d.offset):
            stuck.append(d)
    for runs, spots, letter in letters:
        _place_text(c, occ, runs, 0.0, spots, letter, bold=True)
    return reach, occ, stuck


BEND_TEXT = 2.5


def _bend_label(c: _Canvas, occ: "_Occ", p, q, label: str):
    """``B1 UP 90° R1.5`` along a bend line, beside it, on the first free spot."""
    d = q - p
    L = float(np.linalg.norm(d))
    if L < 1e-9:
        return
    u = d / L
    if u[0] < -1e-9 or (abs(u[0]) <= 1e-9 and u[1] < 0):  # read from the left or from below
        u = -u
    n = np.array([-u[1], u[0]])
    angle = math.degrees(math.atan2(u[1], u[0]))
    runs = _runs([label], BEND_TEXT)
    mid = (p + q) / 2
    spots = []
    for f in (0.0, -0.2, 0.2, -0.35, 0.35):
        for off in (1.0, -1.0 - 0.75 * BEND_TEXT):
            spots.append((mid + u * f * L + n * off, "middle", None))
    _place_text(c, occ, runs, angle, spots, label)


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


def _draw_frame(c: _Canvas, x0, y0, fr: "_Frame"):
    """A feature control frame, its bottom left corner at (x0, y0); returns its box."""
    w = fr.width
    c.rect("thin", x0, y0, x0 + w, y0 + FRAME_H)
    x = x0
    for i, (cell, cw) in enumerate(zip(fr.cells, fr.widths)):
        if i:
            c.line("thin", (x, y0), (x, y0 + FRAME_H))
        c.label(x + cw / 2, y0 + FRAME_H / 2 - TEXT * 0.36, [(cell, 0.0, 0.0, TEXT)], "middle")
        x += cw
    c.frames.append(fr.name)
    return (x0 + w / 2, y0 + FRAME_H / 2, 1.0, 0.0, w / 2, FRAME_H / 2)


def _draw_datum_box(c: _Canvas, cx, cy, letter):
    """The datum letter in its square, centered at (cx, cy)."""
    h = FRAME_H / 2
    c.rect("thin", cx - h, cy - h, cx + h, cy + h)
    c.label(cx, cy - TEXT * 0.36, [(letter, 0.0, 0.0, TEXT)], "middle", name=letter)
    c.frames.append(f"datum {letter}")
    return (cx, cy, 1.0, 0.0, h, h)


def _datum_triangle(c: _Canvas, base, d, t):
    """A filled datum triangle, its base on a line at ``base`` (along ``t``), pointing along ``d``; returns its apex."""
    base, d, t = np.asarray(base, float), np.asarray(d, float), np.asarray(t, float)
    apex = base + TRI[1] * d
    c.ops.append(("fill", (tuple(base + TRI[0] * t), tuple(base - TRI[0] * t), tuple(apex))))
    return apex


TRI = (1.6, 2.8)  # datum triangle: half base, height
DATUM_ROW = FRAME_H + 3.6  # a datum hung under a callout: its box and the stem up to the row above


def _rows(lines):
    """Callout rows: ``(kind, content, name, height, width)``; text, frames and datum letters."""
    out = []
    for ln in lines:
        if isinstance(ln, _Frame):
            out.append(("frame", ln, ln.name, FRAME_H + 1.2, ln.width))
        elif isinstance(ln, _DatumTag):
            out.append(("datum", ln, ln.name, DATUM_ROW, FRAME_H))
        else:
            runs = _runs(ln)
            out.append(("text", runs, _runs_name(ln), TEXT * 1.45, _runs_extent(runs)[0]))
    return out


def _row_boxes(rows, x, ym, anchor):
    """Each row's bottom and its collision box, the first row's middle at ``ym``."""
    out = []
    top = ym + rows[0][3] / 2
    for kind, content, _, h, w in rows:
        bot = top - h
        x0 = x if anchor == "start" else x - w
        if kind == "text":
            box = _box_of(x, bot + h / 2 - TEXT * 0.36, content, anchor)
        elif kind == "frame":
            box = (x0 + w / 2, bot + 0.6 + FRAME_H / 2, 1.0, 0.0, w / 2, FRAME_H / 2)
        else:
            box = (x0 + w / 2, bot + h / 2, 1.0, 0.0, w / 2, h / 2)
        out.append((bot, box))
        top = bot
    return out


def _draw_rows(c: _Canvas, rows, x, ym, anchor):
    """Draw callout rows; returns the boxes they take."""
    boxes = []
    prev = None
    for (kind, content, name, h, w), (bot, box) in zip(rows, _row_boxes(rows, x, ym, anchor)):
        x0 = x if anchor == "start" else x - w
        if kind == "text":
            boxes.append(c.label(x, bot + h / 2 - TEXT * 0.36, content, anchor, name=name))
        elif kind == "frame":
            boxes.append(_draw_frame(c, x0, bot + 0.6, content))
        else:  # the datum letter, its stem and triangle up to the row above
            cx = x0 + FRAME_H / 2
            top = bot + h + (0.6 if prev == "frame" else 0.0)  # a frame above sits 0.6 up in its row
            apex = _datum_triangle(c, (cx, top), (0.0, -1.0), (1.0, 0.0))
            c.line("thin", apex, (cx, bot + FRAME_H))
            _draw_datum_box(c, cx, bot + FRAME_H / 2, content.letter)
            boxes.append(box)
        prev = kind
    return boxes


def _draw_callouts(c: _Canvas, occ: _Occ, G, callouts, s, reach, nb, geo, dim_sides, leaders):
    """Callouts beside the view: rows of text and frames, a shoulder and a slanted leader to the feature.

    ``callouts``: ``(rows, targets, side or None)``; rows are lists of text pieces, frames or datum tags;
    targets ``(x0, y0, x1, y1, r[, nx, ny])`` in view coordinates: a circle when the two points coincide,
    a slot, or (with the face's outward normal) a plane seen edge-on, met from outside. Each goes on the
    side where its leader crosses the fewest lines of the part, then the shortest; no leader crosses
    another or a dimension or extension line, no text touches anything.
    """
    blocks = []
    for lines, targets, forced in callouts:
        T = [(np.array(t[:2]) * s, np.array(t[2:4]) * s, t[4] * s, np.array(t[5:7]) if len(t) > 5 else None)
             for t in targets]
        rows = _rows(lines)
        blocks.append((rows, [r[2] for r in rows], T, forced))

    def depth(b):  # how far inside the view its nearest feature is: the hardest go first
        return min(min(G[2] - max(t[0][0], t[1][0]), min(t[0][0], t[1][0]) - G[0],
                       G[3] - max(t[0][1], t[1][1]), min(t[0][1], t[1][1]) - G[1]) for t in b[2])

    blocks.sort(key=lambda b: (b[3] is None, -depth(b), b[1]))
    dimS = np.asarray(c.dimsegs, dtype=float).reshape(-1, 4)
    extS = np.asarray(c.extsegs, dtype=float).reshape(-1, 4)

    def layout(rows, tgt, side, push, k, beyond=0):
        p0, p1, r, nrm = tgt
        H = sum(rw[3] for rw in rows)
        h0 = rows[0][3]
        d0 = reach[side] + 6.0
        if side in ("left", "right"):
            sg = 1.0 if side == "right" else -1.0
            x = G[2] + d0 + 8 * push if side == "right" else G[0] - d0 - 8 * push
            ym = max(p0[1], p1[1]) + 0.7 * r + 4.0 + k
            anchor = "start" if side == "right" else "end"
        else:
            sg = 1.0
            x = max(p0[0], p1[0]) + 0.7 * r + 7.0 + k
            if side == "top":
                ym = G[3] + d0 + 6 * push + H - h0 / 2
            else:
                ym = G[1] - d0 - 6 * push
            anchor = "start"
        gap = 1.0 if rows[0][0] == "text" else 0.0  # a frame takes the leader at its side
        S = np.array([x - sg * 4.0, ym])
        e = p1 - p0
        L2 = float(e @ e)
        lo, hi = (0.15, 0.85) if nrm is not None else (0.0, 1.0)
        tp = (S - p0) @ e / L2 if L2 > 1e-12 else 0.0
        q = p0 + np.clip(tp, lo, hi) * e
        ext = None
        if beyond:  # a face: the arrow on its extension line, 3 to 12 mm past an end
            L = math.sqrt(L2)
            far = (beyond - 1) // 4
            near_p1 = (tp > 0.5) != bool(far)
            gapb = (3.0, 6.0, 9.0, 12.0)[(beyond - 1) % 4]
            tq = 1 + gapb / L if near_p1 else -gapb / L
            q = p0 + tq * e
            end, u = (p1, e / L) if tq > 1 else (p0, -e / L)
            ext = (end + 1.0 * u, q + 2.0 * u)
        if np.linalg.norm(S - q) < r + ARROW[0] + 1:
            return None
        if nrm is not None and (S - q) @ nrm < 3.0:  # a face: met from the outside
            return None
        Q = q + r * _unit(S - q)
        boxes = [b for _, b in _row_boxes(rows, x, ym, anchor)]
        segs = [(*S, x - sg * gap, ym), (*S, *Q)] + ([(*ext[0], *ext[1])] if ext else [])
        return x, ym, anchor, S, Q, boxes, segs, ext

    ks_side = [0.0] + [v for i in range(1, 31) for v in (-1.5 * i, 1.5 * i)]
    ks_row = [0.0] + [v for i in range(1, 31) for v in (2.0 * i, -2.0 * i)]

    def clear(lay, level):
        """Level 0: the leader and its shoulder cross no other leader, dimension or extension line; 1: no
        leader or dimension line; 2: anything."""
        if level >= 2:
            return True
        walls = [leaders, dimS] + ([extS] if level == 0 else [])
        return not any(_cross(a, b, W) for W in walls for a, b in ((lay[3], lay[4]), (lay[6][0][:2], lay[6][0][2:])))

    def search(rows, T, side, level):
        ks = ks_side if side in ("left", "right") else ks_row
        out = SIDES[side][0]
        T = sorted(T, key=lambda t: -(max(t[0] @ out, t[1] @ out) + t[2]))  # nearest that side first
        for push in range(8 if level < 2 else 3):
            for tgt in T[:4]:
                for beyond in range(9) if tgt[3] is not None else (0,):
                    for k in ks:
                        lay = layout(rows, tgt, side, push, k, beyond)
                        if lay is None or not occ.free(lay[5], lay[6]):
                            continue
                        if not clear(lay, level):
                            continue
                        ext = lay[7]
                        if ext is not None and (_cross(ext[0], ext[1], leaders) or _cross(ext[0], ext[1], dimS)):
                            continue
                        return lay
        return None

    def inside(rows, T):
        """In free space within the view, near the feature: a short leader that crosses nothing at all."""
        gap = 1.0 if rows[0][0] == "text" else 0.0
        for L in (5.0, 8.0, 12.0, 16.0, 22.0):
            for tgt in T[:4]:
                p0, p1, r, nrm = tgt
                c0 = (p0 + p1) / 2
                for ang in (45, 135, -45, -135, 25, 155, -25, -155, 65, 115, -65, -115):
                    d = np.array([math.cos(math.radians(ang)), math.sin(math.radians(ang))])
                    if nrm is not None and d @ nrm < 0.3:
                        continue
                    S = c0 + (r + L) * d
                    e = p1 - p0
                    L2 = float(e @ e)
                    lo, hi = (0.15, 0.85) if nrm is not None else (0.0, 1.0)
                    q = p0 + (np.clip((S - p0) @ e / L2, lo, hi) * e if L2 > 1e-12 else 0.0)
                    Q = q + r * _unit(S - q)
                    sg = 1.0 if d[0] >= 0 else -1.0
                    x, ym = S[0] + sg * 4.0, S[1]
                    anchor = "start" if sg > 0 else "end"
                    boxes = [b for _, b in _row_boxes(rows, x, ym, anchor)]
                    segs = [(*S, x - sg * gap, ym), (*S, *Q)]
                    lay = (x, ym, anchor, S, Q, boxes, segs, None)
                    Qin = Q + 0.4 * _unit(S - Q)
                    if (occ.free(boxes, segs) and clear(lay, 0) and not _cross(S, Qin, occ.S)
                            and all(_box_rect(b)[0] > G[0] - 30 and _box_rect(b)[2] < G[2] + 30 for b in boxes)):
                        return lay
        return None

    for rows, names, T, forced in blocks:
        pick = None
        for level in (0, 1, 2):
            for side in ([forced] if forced else ["right", "top", "bottom", "left"]):
                lay = search(rows, T, side, level)
                if lay is None:
                    continue
                S, Q = lay[3], lay[4]
                Qin = Q + 0.4 * _unit(S - Q)  # not the feature's own edge, where the arrow ends
                cost = (10.0 * _ncross(S, Qin, geo) + 1.0 * (_ncross(S, Qin, occ.S) - _ncross(S, Qin, geo))
                        + 15.0 * _ncross(S, Q, extS) + 30.0 * _ncross(S, Q, dimS)
                        + 0.05 * float(np.linalg.norm(S - Q)) + 3.0 * nb.get(side, 0) + 0.5 * (side in dim_sides))
                if pick is None or cost < pick[0] - 1e-9:
                    pick = (cost, lay)
            if level == 0 and not forced and (pick is None or pick[0] > 4.0):
                lay = inside(rows, T)  # crossing the part's lines to get out: better inside, if there is room
                if lay is not None:
                    pick = (4.0, lay)
            if pick:
                break
        if pick is None:  # pragma: no cover - nothing free: the nearest spot
            side = forced or "right"
            pick = (0, next(lay for tgt in T for k in ks_side if (lay := layout(rows, tgt, side, 0, k))))
        x, ym, anchor, S, Q, boxes, segs, ext = pick[1]
        if ext is not None:
            c.line("thin", ext[0], ext[1])
            c.extsegs.append((*ext[0], *ext[1]))
        c.line("thin", S, segs[0][2:])
        c.line("thin", S, Q)
        c.leaders.append((*S, *Q))
        c.arrow(Q, Q - S)
        n0 = len(c.ops)
        placed = _draw_rows(c, rows, x, ym, anchor)
        arrow = _segments([op for op in c.ops[n0 - 1:n0] if op[0] == "fill"])
        occ.add(placed, np.concatenate([np.asarray(segs, float), arrow.reshape(-1, 4)]))
        leaders.append((*S, *Q))


def _draw_datum_planes(c: _Canvas, occ: _Occ, datums, s, geo, leaders):
    """Datum feature symbols on faces seen edge-on: a filled triangle on the face's line (or its extension
    past an end), a stem straight out of the material and the letter in a square at its end. The stem
    crosses no dimension or extension line and no leader; the square touches nothing."""
    for letter, p0, p1, nrm in datums:
        P0, P1, nn = np.asarray(p0, float) * s, np.asarray(p1, float) * s, _unit(nrm)
        L = float(np.linalg.norm(P1 - P0))
        t = (P1 - P0) / L if L > 1e-9 else np.array([-nn[1], nn[0]])
        cands = []
        for f in (0.5, 0.35, 0.65, 0.2, 0.8):
            if L * min(f, 1 - f) >= TRI[0] + 1.0:
                cands.append((P0 + f * (P1 - P0), None, 0.0))
        for e in (4.0, 8.0, 12.0, 16.0):  # on the face's extension line, past either end
            cands.append((P1 + e * t, (P1 + 1.0 * t, P1 + (e + TRI[0] + 1.0) * t), e))
            cands.append((P0 - e * t, (P0 - 1.0 * t, P0 - (e + TRI[0] + 1.0) * t), e))
        dimS = np.asarray(c.dimsegs, dtype=float).reshape(-1, 4)
        extS = np.asarray(c.extsegs, dtype=float).reshape(-1, 4)
        reach = FRAME_H / 2 / max(abs(nn[0]), abs(nn[1]))
        best = None
        for T, ext, e in cands:
            apex = T + TRI[1] * nn
            for stem in np.arange(2.0, 46.0, 2.0):
                end = apex + stem * nn
                ctr = end + reach * nn
                box = (float(ctr[0]), float(ctr[1]), 1.0, 0.0, FRAME_H / 2, FRAME_H / 2)
                segs = [(*apex, *end), (*(T + TRI[0] * t), *apex), (*(T - TRI[0] * t), *apex)]
                if ext is not None:
                    segs.append((*ext[0], *ext[1]))
                if not occ.free([box], segs):
                    continue
                if any(_cross(q[:2], q[2:], dimS) or _cross(q[:2], q[2:], leaders) for q in segs):
                    continue
                cost = stem + 1.2 * e + 8.0 * sum(_ncross(q[:2], q[2:], geo) for q in segs[:3]) \
                    + 2.0 * sum(_ncross(q[:2], q[2:], occ.S) for q in segs[:3]) + 15.0 * _ncross(apex, end, extS)
                if best is None or cost < best[0] - 1e-9:
                    best = (cost, T, ext, apex, end, ctr, box, segs)
                break  # longer stems on this spot cost more
        if best is None:
            raise TuleError(f"no room for datum {letter}'s symbol", op="drawing",
                            hint="use a larger sheet, or put the datum on another face")
        _, T, ext, apex, end, ctr, box, segs = best
        if ext is not None:
            c.line("thin", ext[0], ext[1])
        _datum_triangle(c, T, nn, t)
        c.line("thin", apex, end)
        c.leaders.append((*apex, *end))
        leaders.append((*apex, *end))
        _draw_datum_box(c, ctr[0], ctr[1], letter)
        occ.add([box], np.asarray(segs, float))


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


PICK_OPTS = {"text", "label", "tol", "side", "frame", "width", "length"}


def _circles_of(sel: Selection, v: "_View | None", vname):
    """Circles of picked cylindrical faces or circular edges: (center, axis, r, whole, kind); those on the
    axis of view ``v`` when one is given."""
    geo = []
    for e in sel.items:
        if sel.kind == "edge":
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
                raise TuleError("a feature is picked by its cylindrical faces or circular edges, a face for a "
                                "frame by its planar faces", op="drawing", hint="part.faces.cylindrical(r=3.25)")
            g = a.Cylinder()
            q = g.Axis().Location()
            u0, u1, v0, v1 = static(BRepTools, "UVBounds")(e)
            props = BRepLProp_SLProps(a, (u0 + u1) / 2, (v0 + v1) / 2, 1, 1e-7)
            m = props.Value()
            mid = np.array([m.X(), m.Y(), m.Z()])
            nv = props.Normal()
            nv = np.array([nv.X(), nv.Y(), nv.Z()]) * (-1 if e.Orientation() == TopAbs_REVERSED else 1)
            o = np.array([q.X(), q.Y(), q.Z()])
            full = {"sweep": math.degrees(u1 - u0), "depth": abs(v1 - v0), "mid": mid,
                    "concave": bool(nv @ (mid - o) < 0)}  # an arc of a face: for a callout of its radius
        ax = g.Axis().Direction()
        axis = np.array([ax.X(), ax.Y(), ax.Z()])
        if v is not None and abs(axis @ v.d) < 0.999:  # the picked ones seen as circles here
            continue
        geo.append((np.array([q.X(), q.Y(), q.Z()]), axis, g.Radius(), full, sel.kind))
    if not geo:
        what = "circle" if sel.kind == "edge" else "hole"
        raise TuleError(f"no {what} picked is seen as a circle in the {vname} view", op="drawing",
                        hint="use the view that looks along its axis, or leave the view out")
    return geo


def _plane_of(sel: Selection, tol: float):
    """Planar faces (all in one plane): their outward normal and points on their outline."""
    n0, pts, off = None, [], None
    for f in sel.items:
        a = BRepAdaptor_Surface(f)
        if a.GetType() != GeomAbs_Plane:
            return None
        ax = a.Plane().Axis().Direction()
        n = np.array([ax.X(), ax.Y(), ax.Z()]) * (-1 if f.Orientation() == TopAbs_REVERSED else 1)
        loc = a.Plane().Location()
        o = float(np.array([loc.X(), loc.Y(), loc.Z()]) @ n)
        if n0 is None:
            n0, off = n, o
        elif n @ n0 < 0.9999 or abs(o - off) > tol * 10:
            raise TuleError(f"the {len(sel.items)} faces picked for one frame or datum lie in different planes",
                            op="drawing", hint="narrow them to one plane: .largest(), .nearest(point), "
                                               "e.g. part.faces.normal((-1, 0, 0)).largest()")
        for w in explore(f, "wire"):
            pts += _wire_points(w, f, tol)
    return n0, np.asarray(pts, dtype=float)


def _parse_dims(dims, views: dict, alias: dict, tol: float):
    """Agent dimensions ``(view, p1, p2[, text or {text, along, offset, tol}])``, features picked by
    selection ``([view, ]faces or circular edges[, text or {text, label, tol, side, frame, width, length}])``
    and planar faces picked for a frame ``([view, ]faces, {"frame": ..., "side": ...})``."""
    linear: dict = {k: [] for k in views}
    picks: list = []   # {"view", "geo", "opts", "used"}
    planes: list = []  # {"view", "normal", "points", "frames", "datum", "side"}
    drawn = sorted(n for n, k in alias.items() if views[k].name != "iso")
    for spec in dims or ():
        if not isinstance(spec, (list, tuple)) or not spec:
            raise TuleError(f"a dimension is (view, p1, p2) or ([view, ]faces[, options]), got {spec!r}", op="drawing")
        if isinstance(spec[0], Selection):  # a feature or face, its view chosen
            vname, spec = None, (None, *spec)
            v = None
        else:
            vname = spec[0]
            k = alias.get(vname) if isinstance(vname, str) else None
            if k is None or views[k].name == "iso":
                raise TuleError(f"dimension in view {vname!r}, which is not drawn", op="drawing",
                                hint=f"views drawn: {', '.join(drawn)}")
            vname, v = k, views[k]
        if len(spec) < 2:
            raise TuleError(f"a dimension is (view, p1, p2) or ([view, ]faces[, options]), got {spec!r}", op="drawing")
        if (isinstance(spec[1], Selection) and spec[1].kind in ("edge", "face")
                and (len(spec) == 2 or not _is_point(spec[2]))):
            opts = spec[2] if len(spec) > 2 else {}
            if isinstance(opts, str):
                opts = {"text": opts}
            if not isinstance(opts, dict) or set(opts) - PICK_OPTS:
                raise TuleError(f"feature options are {', '.join(sorted(PICK_OPTS))}, got {opts!r}", op="drawing",
                                hint='("right", part.faces.cylindrical(r=3.25), {"tol": "+0.1/0", "side": "top"})')
            if opts.get("side") not in (None, *SIDES):
                raise TuleError(f"side is one of {', '.join(SIDES)}, got {opts['side']!r}", op="drawing")
            opts = dict(opts, frame=_frames(opts.get("frame")))
            plane = _plane_of(spec[1], tol) if spec[1].kind == "face" else None
            if plane is not None:
                if set(opts) - {"frame", "side"} or not opts["frame"]:
                    raise TuleError("a planar face is picked for a frame: {\"frame\": \"flatness 0.05\"}", op="drawing",
                                    hint='(part.faces.top(), {"frame": ["flatness 0.05", "parallel 0.05 A"]})')
                planes.append({"view": vname, "normal": plane[0], "points": plane[1], "frames": opts["frame"],
                               "datum": None, "side": opts.get("side")})
                continue
            if v is None and spec[1].kind == "edge":
                raise TuleError("a circle picked by its edges needs its view", op="drawing",
                                hint='("top", part.edges.circles(r=10)), or pick a hole by its faces')
            geo = _circles_of(spec[1], v, vname)
            picks.append({"view": vname, "geo": geo, "opts": opts, "used": [False] * len(geo)})
            continue
        if v is None:
            raise TuleError("a linear dimension needs its view: (view, p1, p2)", op="drawing")
        if len(spec) < 3:
            raise TuleError("a linear dimension needs two points", op="drawing", hint='("front", p1, p2)')
        opts = spec[3] if len(spec) > 3 else {}
        if isinstance(opts, str):
            opts = {"text": opts}
        if not isinstance(opts, dict) or set(opts) - {"text", "label", "along", "offset", "tol", "side"}:
            raise TuleError(f"dimension options are text, label, along, side, offset and tol, got {opts!r}", op="drawing",
                            hint='("top", p1, p2, {"along": "h", "side": "top", "tol": "±0.05", "text": "40"})')
        a, b = v.p(_point(spec[1])), v.p(_point(spec[2]))
        along = opts.get("along")
        if along is None:
            dx, dy = abs(a[0] - b[0]), abs(a[1] - b[1])
            along = "v" if dx < tol else "h" if dy < tol else "aligned"
        if along not in ("h", "v", "aligned"):
            raise TuleError(f"along is 'h', 'v' or 'aligned', got {along!r}", op="drawing")
        side = opts.get("side")
        if side is not None and side not in {"h": ("top", "bottom"), "v": ("left", "right")}.get(along, ()):
            raise TuleError(f"side {side!r} does not suit a dimension along {along!r}", op="drawing",
                            hint="along h: top or bottom; along v: left or right; aligned: offset")
        t = _tol(opts["tol"]) if "tol" in opts else False
        linear[vname].append((a, b, along, opts.get("text"), opts.get("offset"), t, side, opts.get("label")))
    return linear, picks, planes


def _parse_datums(datums, picks: list, planes: list, tol: float):
    """``datums={"A": part.faces.bottom(), "B": bore faces}``: a plane gets the datum symbol on its edge,
    a bore or slot gets it under its callout."""
    if datums is None:
        return []
    if not isinstance(datums, dict):
        raise TuleError(f"datums is a dict of letter: faces, got {type(datums).__name__}", op="drawing",
                        hint='datums={"A": part.faces.bottom(), "B": part.faces.cylindrical(r=19.05)}')
    for letter, sel in datums.items():
        if not isinstance(letter, str) or letter not in DATUM_LETTERS:
            raise TuleError(f"a datum is a capital letter ({DATUM_LETTERS}; not I, O, Q), got {letter!r}", op="drawing")
        if not isinstance(sel, Selection) or sel.kind != "face" or not sel.items:
            raise TuleError(f"datum {letter} is a face selection: a plane or a bore's cylindrical faces", op="drawing",
                            hint='datums={"A": part.faces.bottom()}')
        plane = _plane_of(sel, tol)
        if plane is not None:
            planes.append({"view": None, "normal": plane[0], "points": plane[1], "frames": [], "datum": letter,
                           "side": None})
        else:
            geo = _circles_of(sel, None, None)
            picks.append({"view": None, "geo": geo, "opts": {"frame": [], "datum": letter}, "used": [False] * len(geo)})
    return sorted(datums)


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


def _merged(optss) -> dict:
    """Options of every pick of one feature: frames add up, otherwise the first one given wins."""
    out: dict = {"frame": []}
    for o in optss:
        for key, val in o.items():
            if key == "frame":
                out["frame"] += val
            elif out.get(key) in (None, ""):
                out[key] = val
    return out


def _plane_view(views, names, n, pts, tol):
    """The first view in ``names`` that sees a plane (outward normal ``n``, outline ``pts``) edge-on, where its
    line is drawn visible (else the first that sees it at all): ``(key, (p0, p1, normal))`` in view coordinates."""
    first = (None, None)
    for k in names:
        v = views[k]
        if abs(n @ v.d) > 1e-6:
            continue
        n2 = _unit([n @ v.x, n @ v.y])
        t = np.array([-n2[1], n2[0]])
        P = np.stack([pts @ v.x, pts @ v.y], axis=1)
        a, b = float((P @ t).min()), float((P @ t).max())
        off = float(np.median(P @ n2))
        seg = (tuple(a * t + off * n2), tuple(b * t + off * n2), tuple(n2))
        cover = 0.0
        for prim in v.vis:
            if prim[0] != "line":
                continue
            q0, q1 = np.array(prim[1:3]), np.array(prim[3:5])
            if abs(q0 @ n2 - off) < tol * 10 and abs(q1 @ n2 - off) < tol * 10:
                lo, hi = sorted((float(q0 @ t), float(q1 @ t)))
                cover += max(0.0, min(hi, b) - max(lo, a))
        if cover >= 0.3 * (b - a):
            return k, seg
        if first[0] is None:
            first = (k, seg)
    return first


def _slot_dims(v: "_View", first, w, L, opts, tol_of) -> list:
    """A slot sized by dimensions: its width across the middle and its overall length beside it."""
    a, b, r = np.array(first[:2]), np.array(first[2:4]), first[4]
    u = _unit(b - a)
    n = np.array([-u[1], u[0]])
    mid = (a + b) / 2

    def along(d):
        return "h" if abs(d[1]) < 1e-9 else "v" if abs(d[0]) < 1e-9 else "aligned"

    def tol_for(key, keys):
        val = opts.get(key)
        if val not in (None, True, False):
            return _tol(val)
        if key == "width" and "tol" in opts:
            return _tol(opts["tol"])
        return tol_of(*keys)

    dw = _Dim(mid + r * n, mid - r * n, along(n), tol=tol_for("width", (f"slot {_fmt(w)}", _fmt(w))))
    dw.offset = 0.0
    dl = _Dim(a - r * u, b + r * u, along(u), tol=tol_for("length", (f"slot × {_fmt(L)}", _fmt(L))))
    # beside the slot, on the side where the line and its extensions cross fewer of the view's lines
    S = _segments([("path", "vis", tuple(_prim_cmds(p, 1.0)), 0.0) for p in v.vis])
    best = None
    for sg in (1.0, -1.0):
        o = sg * (r + 6.0)
        A, B = dl.a + o * dl.n, dl.b + o * dl.n
        hits = _ncross(A, B, S) + _ncross(dl.a + 0.5 * sg * dl.n, A, S) + _ncross(dl.b + 0.5 * sg * dl.n, B, S)
        if best is None or hits < best[0]:
            best = (hits, sg)
    dl.offset, dl.pad = best[1] * 6.0, best[1] * r
    return [dw, dl]


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


def _plan(views: dict, feats, linear: dict, picks: list, planes: list, nb: dict, hidden: bool, tol: float,
          tols: "_Tols", sides: dict, quiet: set):
    """Decide every view's dimensions (in view coordinates), callouts, datums and center lines.

    Every feature is called out once in the whole drawing: in the first view, in sheet order, that looks
    into it best (views in ``quiet`` take none, unless picked there)."""
    shape_cyls, slots, holes = feats
    names = [k for k in views if views[k].name != "iso"]
    order = sorted(names, key=lambda k: (views[k].sheet, ORTHO.index(views[k].name)))
    loud = [k for k in names if k not in quiet]
    plans = {k: {"dims": [], "callouts": [], "centers": [], "nb": nb[k], "datums": []} for k in names}
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
        for a, b, along, text, offset, t, side, label in linear[k]:
            m = (np.asarray(a) + np.asarray(b)) / 2
            if side is not None:
                pass
            elif along == "h":
                side = "top" if m[1] > mid[1] else "bottom"
            elif along == "v":
                side = "right" if m[0] > mid[0] else "left"
            else:
                side = None
            d = _Dim(a, b, along, text, side=side if offset is None else None, label=label)
            d.tol = t if t is not False else tols.get(_fmt(d.value))
            if offset is not None:
                d.offset = float(offset)
            elif side is None:
                d.offset = 8.0 if (m - mid) @ d.n >= 0 else -8.0
            plans[k]["dims"].append(d)
    # holes and slots: "4× ⌀6.6 THRU / ⌴ ⌀11 ↧6.5", in the view that looks into them; a feature picked in
    # dims gets that view, its own callout and the options given
    def pick_of(pts, d, radii):
        hits = []
        for i, pk in enumerate(picks):
            for j, (c, ax, r, _, _) in enumerate(pk["geo"]):
                if any(abs(r - rr) < tol * 10 for rr in radii) and _on_axis(c, ax, pts, d, tol):
                    pk["used"][j] = True
                    if i not in hits:
                        hits.append(i)
        return tuple(hits) or None

    def view_of(pk, d, center, r, open_, ends):
        if pk:
            k = next((picks[i]["view"] for i in pk if picks[i]["view"]), None)
            if k:
                return k
            k = _best_view(views, loud, d, center, r, tol, open_, ends)
            return k or _best_view(views, names, d, center, r, tol, open_, ends)
        return _best_view(views, loud, d, center, r, tol, open_, ends)

    groups: dict = {}
    for h in holes:
        pk = pick_of([h.o], h.d, [h.r] + ([h.cb.r] if h.cb else []) + ([h.cs[0]] if h.cs else []))
        ends = [c.o + t * c.d for c in (h.cyl, h.cb) if c is not None for t in (c.t0, c.t1)]
        k = view_of(pk, h.d, h.o, h.cb.r if h.cb else h.r, h.open, ends)
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
        k = view_of(pk, sl.d, sl.a, sl.cb.r if sl.cb else sl.r, sl.open, ends)
        if k is None:
            continue
        a, b = views[k].p(sl.a), views[k].p(sl.b)
        cb = (round(sl.cb.width, 4), round(sl.cb.length, 3), round(sl.cb.depth, 3)) if sl.cb else None
        depth = None if sl.thru else round(sl.depth, 3)
        groups.setdefault((k, "slot", round(sl.width, 4), round(sl.length, 3), depth, cb, pk), []).append((*a, *b, sl.r))
    for key, cs in sorted(groups.items(), key=lambda kv: (names.index(kv[0][0]), kv[0][1], str(kv[0][2:]))):
        k, kind, pk = key[0], key[1], key[-1]
        opts = _merged([picks[i]["opts"] for i in pk]) if pk else {}
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
            if "width" in opts or "length" in opts:  # sized by dimensions on the first slot, not in the callout
                plans[k]["dims"] += _slot_dims(views[k], sorted(cs)[0], w, L, opts, tol_of)
                lines = [[head + "slot"] + _depth_pieces(depth is None, depth or 0, tols, owner)]
            else:
                lines = [[head + f"slot {_fmt(w)}", tol_of(f"slot {_fmt(w)}", _fmt(w), main=True), f" × {_fmt(L)}"]
                         + _depth_pieces(depth is None, depth or 0, tols, owner)]
            if cb:
                lines.append([f"⌴ slot {_fmt(cb[0])} × {_fmt(cb[1])}"] + _depth_pieces(False, cb[2], tols, owner))
        if opts.get("text"):
            t = _tol(opts["tol"]) if "tol" in opts else None
            lines = [[ln] for ln in str(opts["text"]).split("\n")]
            lines[0].append(t)
        if opts.get("label"):
            lines[0].append(f" {opts['label']}")
        for ln in lines:
            if any(isinstance(p, tuple) and p[0] == "basic" for p in ln):
                raise TuleError(f"'basic' is for dimensions; {_runs_name(ln)!r} is a callout", op="drawing",
                                hint="give the feature a frame: {\"frame\": \"position ⌀0.1 A B C\"}")
        lines += opts.get("frame", [])
        if opts.get("datum"):
            lines.append(_DatumTag(opts["datum"]))
        plans[k]["callouts"].append([lines, sorted(cs), opts.get("side") or sides.get(k)])
    # picked circles that are no feature (a boss, a fillet's arc): a diameter or radius callout
    # and cylindrical faces that are no hole or slot (a pocket's corner, a fillet): R and, inside, its depth
    for pk in picks:
        if all(pk["used"]):
            continue
        if pk["opts"].get("datum"):
            raise TuleError(f"datum {pk['opts']['datum']} is not a hole or slot", op="drawing",
                            hint="a datum is a plane (its planar face) or a bore or slot (its cylindrical faces)")
        opts = pk["opts"]
        found: dict = {}  # (view, r, whole, depth): {where: target}
        arcs: dict = {}   # the faces of one cylinder, merged
        for (c, ax, r, whole, kind), used in zip(pk["geo"], pk["used"]):
            if used:
                continue
            k = pk["view"] or next((n for n in loud + names if abs(ax @ views[n].d) > 0.999), None)
            if k is None:
                raise TuleError("no view drawn looks along the axis of a face picked", op="drawing",
                                hint="add the view that sees it as an arc, or give the view")
            v = views[k]
            cx, cy = v.p(c)
            where = (round(cx / tol), round(cy / tol))
            if kind == "edge":
                found.setdefault((k, round(r, 4), whole, None), {})[where] = (cx, cy, cx, cy, r)
                continue
            a = arcs.setdefault((k, where, round(r, 4)), {"sweep": 0.0, "depth": 0.0, "mid": whole["mid"],
                                                          "concave": whole["concave"], "c": (cx, cy)})
            a["sweep"] += whole["sweep"]
            a["depth"] = max(a["depth"], whole["depth"])
        for (k, where, r), a in arcs.items():
            closed = a["sweep"] >= 359.0
            depth = round(a["depth"], 3) if a["concave"] and not closed else None
            mx, my = views[k].p(a["mid"])
            tgt = (*a["c"], *a["c"], r) if closed else (mx, my, mx, my, 0.0)
            found.setdefault((k, r, closed, depth), {})[where] = tgt
        for (k, r, whole, depth), cs in sorted(found.items(), key=lambda kv: (names.index(kv[0][0]), str(kv[0][1:]))):
            n = len(cs)
            size = f"⌀{_fmt(2 * r)}" if whole else f"R{_fmt(r)}"
            owner = (k, size, (size, n, tuple(sorted(cs))))
            t = _tol(opts["tol"]) if "tol" in opts else tols.get(size, owner=owner)
            if opts.get("text"):
                lines = [[ln] for ln in str(opts["text"]).split("\n")]
                lines[0].append(t)
            else:
                lines = [[(f"{n}× " if n > 1 else "") + size, t]
                         + (_depth_pieces(False, depth, tols, owner) if depth else [])]
            if opts.get("label"):
                lines[0].append(f" {opts['label']}")
            plans[k]["callouts"].append([lines + opts.get("frame", []), sorted(cs.values()),
                                         opts.get("side") or sides.get(k)])
    # planar faces: datum symbols and frames, in the first view (sheet order) that sees the face edge-on
    for pl in planes:
        k, seg = _plane_view(views, [pl["view"]] if pl["view"] else loud + [n for n in names if n in quiet],
                             pl["normal"], pl["points"], tol)
        what = f"datum {pl['datum']}" if pl["datum"] else "the face of " + repr(pl["frames"][0].name)
        if k is None:
            where = f"the {pl['view']} view" if pl["view"] else "any view drawn"
            raise TuleError(f"{what}: {where} does not see that face edge-on", op="drawing",
                            hint="a face is marked where it is seen as a line: add a view along it (front, right, ...)")
        p0, p1, n2 = seg
        if pl["datum"]:
            plans[k]["datums"].append((pl["datum"], p0, p1, n2))
        else:
            plans[k]["callouts"].append([list(pl["frames"]), [(*p0, *p1, 0.0, *n2)], pl["side"] or sides.get(k)])
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
    ("part", "Part", ("part", "part name", "name")),
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
NOTE = 3.0       # revision table text size
NOTE_LH = 4.6    # its line pitch
NOTES_TEXT = 3.5  # notes: ISO 3098's smallest height for them, wherever they sit
NOTES_LH = 5.4    # their line pitch
NOTES_BESIDE = 12  # notes of more lines than this (beside the title block) get sheets of their own
A4_PORTRAIT = (210, 297)  # a notes sheet
REV_LH = 6.0     # revision table row height


def _value(v) -> str:
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, float):
        return _fmt(v)
    return str(v)


def _fields(info, title):
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
    if known.get("part", (None, None))[1] == title:
        del known["part"]
    rows = [known[key] for key, _, _ in FIELDS if key in known]
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
        elif projection:
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


BEND_COLS = (("Bend", 16), ("Dir", 17), ("Angle", 19), ("R", 18), ("BA", 25), ("BD", 25))
BEND_LH = 4.6


def _bend_height(bends) -> float:
    return 5.0 + 5.0 + BEND_LH * len(bends) + 0.4 if bends else 0.0


def _bend_table(c: _Canvas, x0, y0, bends):
    """The bend table, its bottom at ``y0``: a title with t and K, a header, one row per bend."""
    h = _bend_height(bends)
    x1, y1 = x0 + TB_W, y0 + h
    c.rect("thin", x0, y0, x1, y1)
    b0 = bends[0]
    c.text(x0 + 1.5, y1 - 3.6, f"BENDS   t {_n(b0.thickness)}   K {_n(b0.k)}   (BA allowance, BD deduction)",
           size=2.0)
    y = y1 - 5.0
    c.line("thin", (x0, y), (x1, y))
    xs = [x0]
    for _, w in BEND_COLS[:-1]:
        xs.append(xs[-1] + w)
    for x, (label, _) in zip(xs, BEND_COLS):
        c.text(x + 1.5, y - 3.6, label, size=2.0)
    y -= 5.0
    c.line("thin", (x0, y), (x1, y))
    for b in bends:
        # three decimals at most, as in the bend CSV: 16 ga is t 1.524, not 1.52
        cells = (b.id, b.direction.upper(), f"{_n(b.angle)}°", _n(b.radius), _n(b.allowance), _n(b.deduction))
        for x, v in zip(xs, cells):
            c.text(x + 1.5, y - 3.3, v, size=2.5)
        y -= BEND_LH
    for x in xs[1:]:
        c.line("thin", (x, y0), (x, y1 - 5.0))


class _Unnumbered(str):
    """A line of the NOTES block without a number: where the notes are, when they have a sheet of their own."""


_MARKER = re.compile(r"\s*(?:\(?\d{1,3}[.)]|[-–•*])\s+")  # numbering or a bullet the author typed
_OPEN_END = re.compile(r"(?:[,;:/(&+–-]|\b(?:and|or|to|with|of|the|a|an|per|for|in|at|on|by|from))$", re.I)


def _notes(notes) -> list[str]:
    """One note per list item (or line of a text): numbering typed in (``1.``, ``-``) is dropped, and an item
    that only continues the one before (indented, starting lowercase, after an open end like ``and`` or ``,``,
    or unmarked after numbered ones) is joined to it."""
    if not notes:
        return []
    if isinstance(notes, list) and all(isinstance(n, _Unnumbered) for n in notes):
        return notes
    if isinstance(notes, str):
        notes = notes.split("\n")
    if not isinstance(notes, (list, tuple)) or not all(isinstance(n, str) for n in notes):
        raise TuleError("notes is a list of text", op="drawing", hint='notes=["Break all edges 0.3", "Anodize clear"]')
    items = [n.rstrip() for n in notes if n.strip()]
    if items and re.fullmatch(r"\s*notes?\s*:?", items[0], re.I):  # a heading typed in: the block has its own
        items = items[1:]
    marked = [bool(_MARKER.match(n)) for n in items]
    out: list[str] = []
    for k, (n, m) in enumerate(zip(items, marked)):
        text = n[_MARKER.match(n).end():].strip() if m else n.strip()
        prev = out[-1] if out else None
        if prev is not None and not m and (any(marked[:k]) or n[0].isspace() or bool(_OPEN_END.search(prev))
                                           or (text[0].islower() and text[0] not in "øµ")):
            if prev.endswith("-") and prev[-2:-1].islower() and text[0].islower():
                out[-1] = prev[:-1] + text  # a word broken at the line's end: de- / burr
            elif prev.endswith(("-", "/")):
                out[-1] = prev + text
            else:
                out[-1] = f"{prev} {text}"
            continue
        out.append(text)
    return out


def _notes_lines(notes, w, first=1):
    """``(number or "", text)`` lines of the NOTES block wrapped to width ``w``, numbered from ``first``; a
    wrapped line gets no number."""
    out = []
    i = first - 1
    indent = 2.0 * NOTES_TEXT if any(not isinstance(n, _Unnumbered) for n in notes) else 0.0
    for n in notes:
        num = not isinstance(n, _Unnumbered)
        i += num
        for j, line in enumerate(_wrap(n, w - indent, NOTES_TEXT)):
            out.append((f"{i}." if j == 0 and num else "", line))
    return out


def _notes_height(notes, w) -> float:
    return 7.5 + NOTES_LH * len(_notes_lines(notes, w)) if notes else 0.0


def _notes_block(c: _Canvas, x0, y0, w, notes, first=1, title="NOTES"):
    """NOTES, numbered from ``first``, its bottom at ``y0``."""
    lines = _notes_lines(notes, w, first)
    y = y0 + _notes_height(notes, w) - 4.0
    c.text(x0, y, title, size=TEXT, bold=True)
    y -= 7.5 - 0.6
    indent = 2.0 * NOTES_TEXT if any(not isinstance(n, _Unnumbered) for n in notes) else 0.0
    for num, line in lines:
        if num:
            c.text(x0, y, num, size=NOTES_TEXT)
        c.text(x0 + indent, y, line, size=NOTES_TEXT)
        y -= NOTES_LH


def _notes_spill(notes) -> bool:
    """More lines than sit comfortably beside the title block: the notes get sheets of their own."""
    return len(_notes_lines(notes, TB_W - 1)) > NOTES_BESIDE


def _notes_pages(notes) -> list[list[str]]:
    """The notes over A4 portrait sheets, each note whole on one where it fits."""
    W, H = A4_PORTRAIT
    room = H - 2 * MARGIN - 16 - _tb_height([("General tolerances", GENERAL_TOLERANCE)]) - 40
    pages: list[list[str]] = [[]]
    for n in notes:
        if pages[-1] and _notes_height(pages[-1] + [n], W - 2 * MARGIN - 16) > room:
            pages.append([])
        pages[-1].append(n)
    return pages


def _notes_sheet(title, info, rev, notes, first, number):
    """An A4 portrait sheet of notes (the first numbered ``first``) with the title block."""
    W, H = A4_PORTRAIT
    tb_rows, _ = _fields(info, title)
    c = _Canvas()
    c.rect("frame", MARGIN, MARGIN, W - MARGIN, H - MARGIN)
    w = W - 2 * MARGIN - 16
    _notes_block(c, MARGIN + 8, H - MARGIN - 8 - _notes_height(notes, w), w, notes, first,
                 "NOTES" if first == 1 else "NOTES (continued)")
    _title_block(c, W, title, tb_rows, rev, "–", f"A4  {number[0]}/{number[1]}" if number[1] > 1 else "A4", None)
    return c


def _notes_where(first, last) -> list[str]:
    """The NOTES block on the other sheets: where the notes are."""
    return [_Unnumbered(f"See sheet {first}." if first == last else f"See sheets {first} to {last}.")]


TABLE_LH = 6.0  # table row height


def _table(table):
    """``table`` as a header and rows of text: a list of dicts (keys head the columns) or of lists (the first
    is the header); None for none."""
    if table is None:
        return None
    err = TuleError("table is a list of dicts, or of lists with the header first", op="drawing",
                    hint='table=[{"Part": "B-20", "L": 20}, {"Part": "B-30", "L": 30}]')
    if not isinstance(table, (list, tuple)) or not table:
        raise err
    if all(isinstance(r, dict) for r in table):
        head = list(dict.fromkeys(k for r in table for k in r))
        body = [[r.get(k, "") for k in head] for r in table]
    elif all(isinstance(r, (list, tuple)) for r in table):
        head, body = list(table[0]), [list(r) for r in table[1:]]
        if any(len(r) > len(head) for r in body):
            raise TuleError("a table row is longer than its header", op="drawing", hint="the first row names every column")
        body = [r + [""] * (len(head) - len(r)) for r in body]
    else:
        raise err
    return [str(h) for h in head], [[_value(v) if v is not None else "" for v in r] for r in body]


def _table_size(rows, size=NOTE):
    head, body = rows
    ws = [max(text_width(r[i], size, bold=r is head) for r in [head] + body) + 4.0 * size / NOTE
          for i in range(len(head))]
    return ws, TABLE_LH * size / NOTE * (1 + len(body))


def _table_block(c: _Canvas, x0, y1, rows, size=NOTE, ws=None):
    """The table, its top left at (x0, y1): a bold header, ruled rows, numbers right-aligned; ``ws``: column
    widths (else as wide as their text)."""
    head, body = rows
    ws0, h = _table_size(rows, size)
    ws = ws or ws0
    lh = TABLE_LH * size / NOTE
    xs = [x0]
    for w in ws:
        xs.append(xs[-1] + w)
    c.rect("thin", x0, y1 - h, xs[-1], y1)
    for x in xs[1:-1]:
        c.line("thin", (x, y1 - h), (x, y1))
    num = [all(re.fullmatch(r"[+-]?(\d+\.?\d*|\.\d+)", r[i]) or not r[i] for r in body) for i in range(len(head))]
    pad = 2.0 * size / NOTE
    for j, r in enumerate([head] + body):
        y = y1 - lh * (j + 1)
        if j:
            c.line("thin", (x0, y + lh), (xs[-1], y + lh))
        for i, v in enumerate(r):
            right = num[i] and j > 0
            c.text(xs[i + 1] - pad if right else xs[i] + pad, y + 1.8 * size / NOTE, v, size=size,
                   anchor="end" if right else "start", bold=j == 0)
    c.line("frame" if body else "thin", (x0, y1 - lh), (xs[-1], y1 - lh))


def _table_sheet(title, info, rev, notes, rows, sheet, number):
    """A sheet of the table and the notes, with the title block: the smallest of A4, A3, A2 that takes them."""
    tb_rows, _ = _fields(info, title)
    tb_h = _tb_height(tb_rows)
    tw, th = _table_size(rows) if rows else ([0.0], 0.0)
    for size in [sheet] if sheet else AUTO_SHEETS:
        W, H = SHEETS[size]
        room = W - 2 * MARGIN - 16
        nw = min(room, max(sum(tw), TB_W + 20))
        h = th + (8.0 if rows and notes else 0.0) + _notes_height(notes, nw)
        if sum(tw) <= room and h <= H - 2 * MARGIN - tb_h - 22:
            break
    else:
        raise TuleError(f"the table and notes do not fit {sheet or AUTO_SHEETS[-1]}: {sum(tw):.0f} × {h:.0f} mm",
                        op="drawing", hint="fewer columns or rows, shorter notes" + ("" if sheet else ", or sheet=\"A1\""))
    c = _Canvas()
    c.rect("frame", MARGIN, MARGIN, W - MARGIN, H - MARGIN)
    x0, y = MARGIN + 8, H - MARGIN - 8
    if rows:
        _table_block(c, x0, y, rows)
        y -= th + 8.0
    if notes:
        _notes_block(c, x0, y - _notes_height(notes, nw), nw, notes)
    _title_block(c, W, title, tb_rows, rev, "–", f"{size}  {number[0]}/{number[1]}" if number[1] > 1 else size, None)
    return c, size


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


DEFAULT_VIEWS = ("front", "top", "right", "iso")
AUTO_SHEETS = ("A4", "A3", "A2")  # a sheet chosen for the views: the smallest that takes them at the scale
ENLARGE = 100.0  # an enlarging scale (2:1, ...) only while the part's largest size stays within this on paper
SHEET_KEYS = {"views", "sheet", "scale", "callouts"}
QUIET = ("none", False)  # callouts={"right": "none"}: no automatic callouts in that view


def _sheet_specs(views, sheet, scale, callouts, sheets):
    """Each sheet's views, size (None: chosen), scale (None: chosen) and callout sides."""
    if sheets is None:
        sheets = [{"views": DEFAULT_VIEWS if views is None else views}]
    elif views is not None:
        raise TuleError("views go into each sheet when sheets is given", op="drawing",
                        hint='sheets=[{"views": ("front", "top", "right")}, {"views": (("front", point), "iso")}]')
    if not isinstance(sheets, (list, tuple)) or not sheets:
        raise TuleError(f"sheets is a list of dicts, one per sheet, got {sheets!r}", op="drawing",
                        hint='sheets=[{"views": ("front", "top", "right")}, {"views": ("iso",), "sheet": "A4"}]')
    out = []
    for i, sp in enumerate(sheets, 1):
        if not isinstance(sp, dict) or "views" not in sp or set(sp) - SHEET_KEYS:
            raise TuleError(f"sheet {i} is a dict with views (and sheet, scale, callouts), got {sp!r}", op="drawing",
                            hint='{"views": ("front", "top"), "sheet": "A3", "callouts": {"front": "none"}}')
        size = sp.get("sheet", sheet)
        if size is not None and size not in SHEETS:
            raise TuleError(f"unknown sheet {size!r}", op="drawing", hint=", ".join(SHEETS) + ", or leave it out")
        out.append({"views": sp["views"], "sheet": size, "scale": _parse_scale(sp.get("scale", scale)),
                    "callouts": sp.get("callouts", callouts)})
    return out


def _view_list(views):
    """``views`` of one sheet: names in order, and the section points by name."""
    if isinstance(views, str):
        views = (views,)
    cuts, names = {}, []
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
        if v not in VIEWS:
            raise TuleError(f"unknown view {v!r}", op="drawing", hint=", ".join(VIEWS))
        names.append(v)
    names = list(dict.fromkeys(names))
    if not names:
        raise TuleError("no views to draw", op="drawing", hint='views=("front", "top", "right", "iso")')
    return names, cuts


def _callout_sides(specs, callouts, V, alias):
    """Each view's callout side (or None), and the views that take no automatic callouts."""
    err = TuleError(f"callouts is a side ({', '.join(SIDES)}), 'none', or a dict of view: side, got {callouts!r}",
                    op="drawing", hint='callouts={"right": "top", "front": "none"}')
    sides, quiet = {}, set()

    def apply(spec, keys, where):
        if spec is None:
            return
        if isinstance(spec, (str, bool)):
            spec = {V[k].name: spec for k in keys if V[k].name != "iso"}
            byname = True
        else:
            byname = False
        if not isinstance(spec, dict):
            raise err
        for nm, side in spec.items():
            if side not in SIDES and side not in QUIET or isinstance(side, bool) and side:
                raise err
            hit = [k for k in keys if V[k].name != "iso" and (V[k].name == nm or alias.get(nm) == k)]
            if not hit and not byname:
                raise TuleError(f"callouts for view {nm!r}, which is not drawn{where}", op="drawing",
                                hint="views drawn: " + ", ".join(sorted({V[k].name for k in keys if V[k].name != 'iso'})))
            for k in hit:
                if side in QUIET:
                    quiet.add(k)
                    sides.pop(k, None)
                else:
                    sides[k] = side
                    quiet.discard(k)

    for si, sp in enumerate(specs):
        keys = [k for k in V if V[k].sheet == si]
        apply(sp["callouts"], keys, f" on sheet {si + 1}" if len(specs) > 1 else "")
    return sides, quiet


def render(item, views=None, dims=(), title=None, info=None, sheet=None, scale=None, hidden=True,
           projection="first", name=None, notes=None, tol=None, revisions=None, callouts=None, sheets=None,
           datums=None, table=None, explode=None, bom=None):
    """Lay out a drawing; returns the first sheet's ``(canvas, (width, height), facts)``; ``facts["pages"]``
    holds every sheet's ``(canvas, size)``, ``facts["parts"]`` each part's ``(name, shape, bends)``. ``item``: a
    part, or ``{name: part}`` for one part after another; with ``explode``, an assembly drawing (``facts`` has
    ``items``, ``parts_list``, ``balloons``). See ``outputs.drawing``."""
    if explode is False:
        explode = None
    if explode is None and bom is not None:
        raise TuleError("bom is the parts list of an assembly drawing: it goes with explode", op="drawing",
                        hint="explode=1 moves the parts apart, explode=0 keeps them in place")
    if explode is not None:
        if isinstance(item, dict):
            raise TuleError("explode draws one assembly, not {name: part}", op="drawing",
                            hint="outputs.drawing(name, Assembly(...), explode=1)")
        for arg, what in ((dims, "dims"), (datums, "datums"), (tol, "tol"), (sheets, "sheets"), (callouts, "callouts")):
            if arg:
                raise TuleError(f"{what} does not go with explode: an assembly drawing has balloons and a parts list, "
                                "no dimensions", op="drawing", hint="dimension each part in a drawing of its own")
    parts = _parts(item)
    names = [k for k, _ in parts]
    if isinstance(dims, dict) and (set(dims) - set(names) or names == [None]):
        raise TuleError(f"dims keyed by part name: {', '.join(map(repr, sorted(set(dims) - set(names))))} is no "
                        "part", op="drawing", hint="parts: " + ", ".join(map(repr, names)))
    rows = _table(table)
    notes = _notes(notes)
    spill = _notes_pages(notes) if _notes_spill(notes) else []  # many notes: sheets of their own, at the end
    last = (len(parts) > 1 and bool(notes) and not spill) or rows is not None  # the table (and notes) on a sheet
    per = 1 if explode is not None else len(_sheet_specs(views, sheet, scale, callouts, sheets))
    total = per * len(parts) + last + len(spill)
    if spill:
        notes = _notes_where(total - len(spill) + 1, total)

    def each(a):  # given per part: {part name: value}
        return isinstance(a, dict) and bool(a) and names != [None] and set(a) <= set(names)

    pages, facts = [], None
    for i, (k, it) in enumerate(parts):
        def arg(a):
            return a.get(k) if each(a) else a
        try:
            if explode is not None:
                _, _, f = _render_assembly(it, explode, bom, views, title, info, sheet, scale, name,
                                           None if last and not spill else notes, revisions, (1, total))
            else:
                _, _, f = _render_part(it, views, arg(dims) or (), title if k is None else k, arg(info), sheet,
                                       scale, hidden, projection, name if k is None else k,
                                       None if last and not spill else notes, arg(tol),
                                       revisions, callouts, sheets, arg(datums), first=i * per, total=total,
                                       lead=i == 0)
        except TuleError as e:
            if k is not None:
                e.message = f"{k}: {e.message}"
            raise
        pages += f["pages"]
        f["parts"] = [(k, f.pop("shape"), f["bends"])]
        if facts is None:
            facts = f
        else:
            facts["parts"] += f["parts"]
            facts["texts"] += f["texts"]
            facts["frames"] += f["frames"]
            facts["sheets"] += f["sheets"]
    doc = title or (facts["title"] if len(parts) == 1 else name) or "Drawing"
    if last:
        page, size = _table_sheet(doc, None if each(info) else info, facts["rev"], [] if spill else notes, rows,
                                  sheet, (total - len(spill), total))
        pages.append((page, SHEETS[size]))
        facts["sheets"].append({"sheet": size, "scale": None})
        facts["texts"] += [t for t, _ in page.labels]
    _add_notes_sheets(pages, facts, spill, doc, None if each(info) else info, total)
    if len(parts) > 1:
        facts["title"] = title or name or "Drawing"
    facts["pages"] = pages
    return pages[0][0], pages[0][1], facts


def _add_notes_sheets(pages, facts, spill, title, info, total):
    """The notes sheets (A4 portrait) at the end of the drawing."""
    first = 1
    for i, ns in enumerate(spill):
        page = _notes_sheet(title, info, facts["rev"], ns, first, (total - len(spill) + i + 1, total))
        first += len(ns)
        pages.append((page, A4_PORTRAIT))
        facts["sheets"].append({"sheet": "A4", "scale": None})
        facts["texts"] += [t for t, _ in page.labels]


def _parts(item):
    """``[(name, item)]``: one part (name None), or ``{name: part}``."""
    if not isinstance(item, dict):
        return [(None, item)]
    if not item or not all(isinstance(k, str) and k.strip() for k in item):
        raise TuleError("several parts are {name: part}, each name a text", op="drawing",
                        hint='outputs.drawing("family", {"B-20": b20, "B-30": b30})')
    return list(item.items())


def _render_part(item, views=None, dims=(), title=None, info=None, sheet=None, scale=None, hidden=True,
                 projection="first", name=None, notes=None, tol=None, revisions=None, callouts=None, sheets=None,
                 datums=None, first=0, total=None, lead=True):
    """One part's sheets, numbered from ``first`` + 1 of ``total``; the revision table when it ``lead``s."""
    shape, part_name = _as_shape(item)
    if projection not in GRID:
        raise TuleError(f"projection is 'first' or 'third', got {projection!r}", op="drawing")
    if sheet is not None and sheet not in SHEETS:
        raise TuleError(f"unknown sheet {sheet!r}", op="drawing", hint=", ".join(SHEETS) + ", or leave it out")
    flat = shape.sheet_metal is not None and shape.sheet_metal.flat
    if flat and views is None and sheets is None:
        views = ("top",)  # a flat pattern is its top view
    bends = _bends_of(shape, name)
    specs = _sheet_specs(views, sheet, scale, callouts, sheets)
    wrapped = shape.wrapped
    lo, hi = shape.bounds
    diag = float(np.linalg.norm(np.subtract(hi, lo)))
    if diag <= 0:
        raise TuleError("the shape is empty", op="drawing")
    extent = float(np.max(np.subtract(hi, lo)))
    tol_ = max(1e-4, diag * 1e-6)
    tols = _Tols(tol)
    notes = _notes(notes)
    revs = _revisions(revisions)
    # every view of every sheet, keyed by name ("top"; "top (sheet 2)" when a name comes again)
    V, alias, cuts = {}, {}, {}
    for si, sp in enumerate(specs):
        names, here = _view_list(sp["views"])
        for nm in names:
            key = nm if nm not in V else f"{nm} (sheet {si + 1})"
            v = V[key] = _View(nm)
            v.sheet = si
            alias.setdefault(nm, key)
            if nm in here:
                v.section = [x for x in DATUM_LETTERS if x not in (datums or {})][len(cuts)]  # not a datum's letter
                cuts[key] = here[nm]
                alias[f"{v.section}-{v.section}"] = key
    for k, v in V.items():
        if v.section:  # a section: no hidden lines, cut faces hatched
            _hlr(_section(wrapped, v, cuts[k], diag, tol_), v, False, tol_)
        else:
            _hlr(wrapped, v, hidden and v.name != "iso", tol_)
            if flat and v.name in ("top", "bottom"):
                _bend_lines(v, bends, shape.sheet_metal.thickness, tol_)
    for k, v in V.items():  # each section's plane marked on a plain view that sees it edge-on, its own sheet first
        if v.section:
            seen = [j for j in V if V[j].name != "iso" and not V[j].section and abs(V[j].d @ v.d) < 1e-6]
            parent = next((V[j] for j in seen if V[j].sheet == v.sheet), None) or next((V[j] for j in seen), None)
            if parent is not None:
                w = np.cross(parent.d, v.d)
                sight = -v.d
                parent.cutlines.append((v.section, parent.p(cuts[k]), _unit([w @ parent.x, w @ parent.y]),
                                        _unit([sight @ parent.x, sight @ parent.y])))
    sides, quiet = _callout_sides(specs, callouts, V, alias)
    grids = [{k: GRID[projection][V[k].name] for k in V if V[k].sheet == si and V[k].name != "iso"}
             for si in range(len(specs))]
    nb = {k: _neighbors(k, g) for g in grids for k in g}
    feats = _features(wrapped, tol_)
    if shape.sheet_metal is not None:  # a cut in a sheet goes through it, also with a wall or flange beyond
        for x in feats[1] + feats[2]:
            x.open = (True, True)
    try:
        linear, picks, planes = _parse_dims(dims, V, alias, tol_)
    except TuleError as e:  # say which entry: parsed alone, the first one that fails
        for i, d in enumerate(dims or ()):
            try:
                _parse_dims([d], V, alias, tol_)
            except TuleError:
                e.message = f"dims[{i}]: {e.message}"
                break
        raise
    letters = _parse_datums(datums, picks, planes, tol_)
    for fr in [f for pk in picks for f in pk["opts"].get("frame", [])] + [f for pl in planes for f in pl["frames"]]:
        missing = [d for d in fr.letters() if d not in letters]
        if missing:
            raise TuleError(f"frame {fr.name!r} refers to datum {', '.join(missing)}, which is not defined",
                            op="drawing", hint='datums={"A": part.faces.bottom(), ...}')
    plans = _plan(V, feats, linear, picks, planes, nb, hidden, tol_, tols, sides, quiet)
    tols.check()

    given = next((str(v) for k, v in (info or {}).items()  # a part name given in the call wins over the shape's id
                  if re.sub(r"[_\s]+", " ", str(k).strip().lower()) in FIELDS[0][2] and v), None)
    title = title or given or part_name or name or "Drawing"
    rows, rev = _fields(info, title)
    if revs:
        if rev and rev != revs[-1][0]:
            raise TuleError(f"info rev {rev!r} is not the last revision {revs[-1][0]!r}", op="drawing",
                            hint="the last entry of revisions is the current one; drop rev from info")
        rev = revs[-1][0]
    tb_h = _tb_height(rows)
    pages, chosen = [], []
    for si, sp in enumerate(specs):
        top = si == 0
        s, size, page = _sheet(V, plans, grids[si], si, sp, extent, title, rows, rev, projection,
                               notes if top else [], revs if top and lead else [], tb_h,
                               (first + si + 1, total or len(specs)), bends if top else ())
        pages.append((page, SHEETS[size]))
        chosen.append({"sheet": size, "scale": _scale_text(s)})
    facts = {"scale": chosen[0]["scale"], "sheet": chosen[0]["sheet"], "sheets": chosen, "title": title, "rev": rev,
             "texts": [t for p, _ in pages for t, _ in p.labels], "frames": [f for p, _ in pages for f in p.frames],
             "pages": pages, "bends": bends, "shape": shape}
    return pages[0][0], pages[0][1], facts


def _bends_of(shape, name=None) -> list:
    """A sheet part's bends for its drawing (none when it is no sheet part or does not unfold)."""
    sm = shape.sheet_metal
    if sm is None:
        return []
    if sm.flat:
        return list(sm.bends)
    from .sheetmetal import _flat

    try:
        return list(_flat(shape, "drawing", name)._sm.bends)
    except TuleError as e:
        print(f"tule: drawing without a bend table: {e}", file=sys.stderr, flush=True)
        return []


def _bend_lines(v: _View, bends, t: float, tol: float):
    """Bend lines of a flat pattern in its top (or bottom) view: dash-dot with a label, not solid edges."""
    segs = []
    for b in bends:
        (x0, y0), (x1, y1) = b.line
        p, q = np.array(v.p((x0, y0, t))), np.array(v.p((x1, y1, t)))
        segs.append((p, q))
        v.bends.append((tuple(p), tuple(q), f"{b.id} {b.label}"))

    def on_bend(prim):
        if prim[0] != "line":
            return False
        a, c = np.array(prim[1:3]), np.array(prim[3:5])
        for p, q in segs:
            u = (q - p) / max(np.linalg.norm(q - p), 1e-12)
            n = np.array([-u[1], u[0]])
            if abs((a - p) @ n) < tol * 10 and abs((c - p) @ n) < tol * 10:
                return True
        return False

    v.vis = [p for p in v.vis if not on_bend(p)]


def _sheet(V, plans, grid, si, sp, extent, title, rows, rev, projection, notes, revs, tb_h, number, bends=()):
    """One sheet: its scale and size (chosen unless given) and the page."""
    iso = next((k for k in V if V[k].sheet == si and V[k].name == "iso"), None)
    rv_h = _rev_height(revs) + _bend_height(bends)  # revisions and the bend table stand on the title block
    cache: dict = {}

    def views_at(s):
        if s not in cache:
            canv = {}
            for k in grid:
                c = _annotate(V[k], s, plans[k])
                bb = V[k].bbox()
                canv[k] = (c, (bb[0] * s, bb[1] * s, bb[2] * s, bb[3] * s))
            cache[s] = (canv, *(_grid_block(canv, grid, s) if canv else ({}, [], [])))
        return cache[s]

    def fit(s, size):
        """The views at scale ``s`` on sheet ``size`` and where they go, or None when they do not fit (and
        ``fit.why`` says what does not)."""
        W, H = SHEETS[size]
        canv, off, rects, cells = views_at(s)
        frame = (MARGIN + 3, MARGIN + 3, W - MARGIN - 3, H - MARGIN - 3)
        if rects:
            U = (min(r[0] for r in rects), min(r[1] for r in rects), max(r[2] for r in rects), max(r[3] for r in rects))
            big = max(canv, key=lambda k: (lambda b: (b[2] - b[0]) * (b[3] - b[1]))(_union_box(canv[k])))
            bb = _union_box(canv[big])
            fit.why = (f"the views with their dimensions and callouts take {U[2] - U[0]:.0f} × {U[3] - U[1]:.0f} mm "
                       f"(the {big} view {bb[2] - bb[0]:.0f} × {bb[3] - bb[1]:.0f}); {size} has "
                       f"{frame[2] - frame[0]:.0f} × {frame[3] - frame[1]:.0f} inside its frame, less the title block "
                       f"({TB_W:.0f} × {tb_h + rv_h:.0f}{' and notes' if notes else ''})")
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
        for where, keep in layouts:
            if iso is None:
                at = _place(rects, frame, keep)
                if at:
                    return canv, off, at, None, None, where
                continue
            if _place(rects, frame, keep) is None:
                continue
            ib = V[iso].bbox()
            fit.why = f"the views fit {size}, but the isometric view finds no free spot beside them"
            # the isometric view may be up to three standard scales smaller than the others (labelled)
            for si_ in [s] + ([x for x in SCALES if x < s][:3] if canv else []):
                iw, ih = (ib[2] - ib[0]) * si_, (ib[3] - ib[1]) * si_
                below = 7.0 if si_ != s else 0.0  # its scale, under it
                for sx, sy in _iso_spots(rects, cells, iw, ih + below):
                    ir = (sx, sy, sx + iw, sy + ih + below)
                    if any(_overlaps(ir, r, GAP) for r in rects):
                        continue
                    at = _place(rects + [ir], frame, keep)
                    if at:
                        return canv, off, at, (sx - ib[0] * si_, sy + below - ib[1] * si_), si_, where
        return None

    fit.why = ""
    s, size, placed = _choose(fit, sp["sheet"], sp["scale"], extent, number)
    W, H = SHEETS[size]
    canv, off, (ax, ay), iso_at, iso_s, where = placed
    page = _Canvas()
    page.rect("frame", MARGIN, MARGIN, W - MARGIN, H - MARGIN)
    for k in canv:
        page.add(canv[k][0], off[k][0] + ax, off[k][1] + ay)
    if iso_at is not None:
        ic = _Canvas()
        for p in V[iso].vis:
            ic.path("vis", _prim_cmds(p, iso_s))
        page.add(ic, iso_at[0] + ax, iso_at[1] + ay)
        if iso_s != s:
            ib = ic.bbox()
            page.text(iso_at[0] + ax + (ib[0] + ib[2]) / 2, iso_at[1] + ay + ib[1] - 5, f"({_scale_text(iso_s)})",
                      size=2.5, anchor="middle")
    label = size if number[1] == 1 else f"{size}  {number[0]}/{number[1]}"
    _title_block(page, W, title, rows, rev, _scale_text(s), label, projection)
    x0 = W - MARGIN - TB_W
    if revs:
        _revision_table(page, x0, MARGIN + tb_h, revs)
    if bends:
        _bend_table(page, x0, MARGIN + tb_h + _rev_height(revs), bends)
    if notes:
        if where == "right":
            _notes_block(page, x0 + 1, MARGIN + tb_h + rv_h + 4.0, TB_W - 1, notes)
        else:
            _notes_block(page, MARGIN + 4, MARGIN + 4, min(TB_W + 20, W - 2 * MARGIN - TB_W - 16), notes)
    return s, size, page


def _union_box(cv):
    """A placed view's bounds: its geometry and everything drawn around it (sheet mm, view-local)."""
    a, g = cv[0].bbox(), cv[1]
    return (min(a[0], g[0]), min(a[1], g[1]), max(a[2], g[2]), max(a[3], g[3]))


def _choose(fit, size, fixed, extent, number):
    """Scale and sheet: 1:1 when the views fit, enlarged only for a small part, else the largest reduction;
    a sheet left out is the smallest of A4, A3, A2 that takes them at that scale."""
    on = f" (sheet {number[0]})" if number[1] > 1 else ""
    if fixed:
        for sz in [size] if size else list(SHEETS):
            if (f := fit(fixed, sz)):
                return fixed, sz, f
        sz = size or "A0"
        fit(fixed, sz)
        why = fit.why
        # the largest scale below that fits, standard or not, tested the same way
        best = next((x for x in sorted(set(SCALES) | set(NICE), reverse=True) if x < fixed and fit(x, sz)), None)
        hints = [why] if why else []
        if best in SCALES:
            hints.append(f'"{_scale_text(best)}" is the largest scale that fits, an ISO 5455 one (leave scale out to get it)')
        elif best:
            hints.append(f'scale="{_scale_text(best)}" is the largest that fits; leave scale out for the largest '
                         'ISO 5455 scale that fits')
        bigger = next((k for k, v in SHEETS.items() if v[0] > SHEETS[sz][0]), None)
        hints.append((f'or a larger sheet (sheet="{bigger}", …) or' if bigger else "or") + " fewer views")
        raise TuleError(f"the views{on} do not fit {sz} at {_scale_text(fixed)}", op="drawing", hint="; ".join(hints))
    sizes = [size] if size else list(AUTO_SHEETS)
    order = [x for x in SCALES if x > 1 and extent * x <= ENLARGE] + [x for x in SCALES if x <= 1]
    why = None
    for x in order:
        for sz in sizes:
            if (f := fit(x, sz)):
                if x < 1 and why:  # say what kept it from 1:1, so the author can make room
                    print(f"tule: drawing{on} at {_scale_text(x)} on {sz}, not 1:1: at 1:1 {why}. For 1:1: "
                          f"sheet=\"{next((k for k in SHEETS if SHEETS[k][0] > SHEETS[sizes[-1]][0]), 'A0')}\", "
                          "fewer views, or the callouts of a crowded view on another side (callouts=...)",
                          file=sys.stderr, flush=True)
                return x, sz, f
            if x == 1 and sz == sizes[-1]:
                why = fit.why
    raise TuleError(f"the views{on} do not fit {size or AUTO_SHEETS[-1]} even at {_scale_text(SCALES[-1])}",
                    op="drawing", hint="use a larger sheet or fewer views")


# ---------------------------------------------------------------- assembly drawings: exploded view, balloons, parts list


BALLOON_R = 5.0        # balloon radius on the sheet (mm)
BALLOON_TEXT = 5.0     # its item number
BALLOON_SEP = 2 * BALLOON_R + 3.0  # balloon centres apart along their ring, at least
RING = BALLOON_R + 6.0  # the view's outline to the balloons' centres, at least
DOT = 0.7              # the dot ending a balloon's leader, on the part
PARTS_TEXT = 3.5       # parts list text


def _asm_parts(item):
    """The assembly and each of its parts: ``(path, name, shape)``."""
    if isinstance(item, (list, tuple)):
        item = Assembly(*item)
    if not isinstance(item, Assembly):
        raise TuleError(f"explode draws an Assembly (its parts are moved apart, numbered and listed), got a "
                        f"{type(item).__name__}", op="drawing",
                        hint='outputs.drawing("toy", Assembly(base.named("base"), cam.named("cam")), explode=1)')
    out = []
    for path, sh in item.parts():
        if not sh.faces:
            raise TuleError(f"{path} is empty (no faces)", op="drawing", hint="check len(part.faces) after each step")
        out.append((path, sh.name or path.rsplit("/", 1)[-1], sh))
    if not out:
        raise TuleError("the assembly has no parts", op="drawing")
    return item, out


def _base_name(name: str) -> str:
    """A copy's name without its number: ``screw3`` → ``screw``."""
    return re.sub(r"[\s_.#-]*\d+$", "", name).strip() or name


def _signature(sh: Shape) -> tuple:
    """A part's geometry wherever it is placed: counts, volume, area, principal moments."""
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps

    g = GProp_GProps()
    BRepGProp.VolumeProperties_s(sh.wrapped, g)
    return (len(sh.faces), len(sh.edges)), np.array([g.Mass(), sh.area, *sorted(g.PrincipalProperties().Moments())])


def _same(a, b) -> bool:
    return a[0] == b[0] and bool(np.all(np.abs(a[1] - b[1]) <= 1e-6 * np.maximum(np.abs(a[1]), np.abs(b[1])) + 1e-9))


def _items(parts) -> list[dict]:
    """The parts list's items, in the order the parts first come: copies of one part (same geometry, and names
    that differ only by a number) are one item."""
    items: list[dict] = []
    for i, (path, name, sh) in enumerate(parts):
        sig, base = _signature(sh), _base_name(name)
        for it in items:
            if it["base"] == base and _same(it["sig"], sig):
                it["members"].append(i)
                it["names"].append(name)
                break
        else:
            items.append({"sig": sig, "base": base, "members": [i], "names": [name]})
    for k, it in enumerate(items, 1):
        it["no"] = k
        it["name"] = it["names"][0] if len(set(it["names"])) == 1 else it["base"]
        it["paths"] = [parts[i][0] for i in it["members"]]
    return items


def _match(parts, key) -> list[int]:
    """The parts a name in ``explode`` or ``bom`` means: by path, name, or name without its number."""
    return [i for i, (path, name, _) in enumerate(parts)
            if key in (path, name) or path.endswith("/" + key) or _base_name(name) == key]


def _part_mesh(sh: Shape, tol: float):
    """A coarse triangle mesh of a part (meshed apart: its own exports keep theirs): points, triangles."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Copy

    from .io import tessellate

    m = tessellate(Shape(BRepBuilderAPI_Copy(sh.wrapped, False, False).Shape()), tol, 0.5)
    return np.asarray(m.positions, dtype=float), np.asarray(m.indices, dtype=np.int64)


def _span(Q, P, T, a: int, reach: float = 0.0, chunk: int = 256):
    """The lowest and highest point (along axis ``a``) of the triangles ``(P, T)`` over each column ``Q``
    (points in the other two axes), counting triangles that pass within ``reach`` of it; nan where none does."""
    lo, hi = np.full(len(Q), np.nan), np.full(len(Q), np.nan)
    if not len(Q) or not len(T):
        return lo, hi
    b, c = [k for k in range(3) if k != a]
    V0, V1, V2 = P[T[:, 0]], P[T[:, 1]], P[T[:, 2]]
    e1, e2 = V1 - V0, V2 - V0
    den = e1[:, b] * e2[:, c] - e1[:, c] * e2[:, b]
    tmin, tmax = np.minimum(np.minimum(V0, V1), V2), np.maximum(np.maximum(V0, V1), V2)
    qlo, qhi = Q.min(0) - reach, Q.max(0) + reach
    ok = (np.abs(den) > 1e-12 * max(1.0, float(np.abs(den).max()))) & (tmax[:, b] >= qlo[0]) & \
        (tmin[:, b] <= qhi[0]) & (tmax[:, c] >= qlo[1]) & (tmin[:, c] <= qhi[1])
    V0, e1, e2, den = V0[ok], e1[ok], e2[ok], den[ok]
    if not len(den):
        return lo, hi
    # each edge moved out by ``reach``: the barycentric coordinates' slack
    g1 = 1e-9 + reach * np.hypot(e2[:, b], e2[:, c]) / np.abs(den)
    g2 = 1e-9 + reach * np.hypot(e1[:, b], e1[:, c]) / np.abs(den)
    g3 = 1e-9 + reach * np.hypot(e2[:, b] - e1[:, b], e2[:, c] - e1[:, c]) / np.abs(den)
    for s in range(0, len(Q), chunk):
        q = Q[s:s + chunk]
        dx, dy = q[:, None, 0] - V0[None, :, b], q[:, None, 1] - V0[None, :, c]
        u = (dx * e2[None, :, c] - dy * e2[None, :, b]) / den
        v = (dy * e1[None, :, b] - dx * e1[None, :, c]) / den
        inside = (u >= -g1) & (v >= -g2) & (u + v <= 1 + g3)
        h = V0[None, :, a] + u * e1[None, :, a] + v * e2[None, :, a]
        hit = inside.any(1)
        lo[s:s + chunk] = np.where(hit, np.where(inside, h, np.inf).min(1), np.nan)
        hi[s:s + chunk] = np.where(hit, np.where(inside, h, -np.inf).max(1), np.nan)
    return lo, hi


def _columns(P, T, a: int, cap: int = 300):
    """Sample columns across axis ``a`` where the part has material: inside its triangles not seen edge-on."""
    b, c = [k for k in range(3) if k != a]
    V0, V1, V2 = P[T[:, 0]], P[T[:, 1]], P[T[:, 2]]
    n = np.cross(V1 - V0, V2 - V0)
    keep = np.abs(n[:, a]) > 0.05 * np.linalg.norm(n, axis=1)
    m = (V0 + V1 + V2)[keep] / 3
    pts = np.concatenate([m] + [V[keep] + 0.1 * (m - V[keep]) for V in (V0, V1, V2)])[:, [b, c]]
    if len(pts) > cap:
        pts = pts[np.linspace(0, len(pts) - 1, cap).astype(int)]
    return pts


def _explode(parts, explode, meshes):
    """Each part's offset (3D) and the axis it was exploded along (None for offsets given).

    A factor stacks the parts along the axis that separates every touching pair with the least travel: a part
    resting on another stays above it; of two interlocked (a shaft through a bore) the one listed later goes
    above; each stands off the one below by ``factor`` × a gap."""
    n = len(parts)
    offs = [np.zeros(3) for _ in parts]
    if isinstance(explode, dict):
        for key, vec in explode.items():
            hit = _match(parts, str(key))
            if not hit:
                raise TuleError(f"explode names {key!r}, which is no part", op="drawing",
                                hint="parts: " + ", ".join(dict.fromkeys(p[1] for p in parts)))
            try:
                vec = np.asarray(vec, dtype=float).reshape(3)
            except (TypeError, ValueError):
                raise TuleError(f"explode[{key!r}] is an offset (dx, dy, dz), got {vec!r}", op="drawing") from None
            for i in hit:
                offs[i] = vec
        return offs, None, None
    try:
        factor = float(explode)
    except (TypeError, ValueError):
        factor = math.nan
    if not math.isfinite(factor) or factor < 0:
        raise TuleError(f"explode is a factor (1: the usual spacing, 0: in place) or {{part: (dx, dy, dz)}}, got "
                        f"{explode!r}", op="drawing", hint='explode=1, or explode={"cam": (0, 0, 40)}')
    if factor == 0 or n < 2:
        return offs, None, None
    lo = np.array([[float(v) for v in p[2].bounds[0]] for p in parts])
    hi = np.array([[float(v) for v in p[2].bounds[1]] for p in parts])
    size = float(np.max(hi.max(0) - lo.min(0)))
    eps = 1e-3 * size
    reach = max(0.5, 0.004 * size)  # a part in a hole with clearance is still in it
    touch = [(i, j) for i in range(n) for j in range(i + 1, n)
             if np.all(lo[i] <= hi[j] + 0.01 * size) and np.all(lo[j] <= hi[i] + 0.01 * size)]
    best = None
    for a in (2, 0, 1):  # z first on a tie: the usual way up
        b, c = [k for k in range(3) if k != a]
        cols = [_columns(P, T, a) for P, T in meshes]
        own = [_span(cols[i], *meshes[i], a) for i in range(n)]
        lifts = {}
        for i in range(n):
            for j in range(i + 1, n):
                if min(hi[i, b], hi[j, b]) - max(lo[i, b], lo[j, b]) <= -reach or \
                        min(hi[i, c], hi[j, c]) - max(lo[i, c], lo[j, c]) <= -reach:
                    continue
                rows = []
                for p, q in ((i, j), (j, i)):
                    m = ((cols[p][:, 0] >= lo[q, b] - reach) & (cols[p][:, 0] <= hi[q, b] + reach) &
                         (cols[p][:, 1] >= lo[q, c] - reach) & (cols[p][:, 1] <= hi[q, c] + reach))
                    if not m.any():
                        continue
                    ql, qh = _span(cols[p][m], *meshes[q], a, reach)
                    pl, ph = own[p][0][m], own[p][1][m]
                    got = ~np.isnan(ql) & ~np.isnan(pl)
                    if got.any():
                        both = np.stack([pl[got], ph[got], ql[got], qh[got]], 1)
                        rows.append(both if p == i else both[:, [2, 3, 0, 1]])
                if rows:
                    R = np.concatenate(rows)
                    lifts[(i, j)] = (float(np.max(R[:, 1] - R[:, 2])), float(np.max(R[:, 3] - R[:, 0])))
        apart = sum(1 for pr in touch if pr not in lifts)  # touching pairs this axis would not separate
        travel = sum(max(0.0, min(L)) for L in lifts.values())
        key = (apart, round(travel / max(size, 1e-9), 2))
        if best is None or key < best[0]:
            best = (key, a, lifts)
    _, a, lifts = best
    ext = hi[:, a] - lo[:, a]
    gap = factor * max(8.0, 0.75 * float(np.median(ext)))
    vol = [p[2].volume for p in parts]
    below: dict[int, list] = {j: [] for j in range(n)}  # j: [(i, lift)], i under j
    for (i, j), (lij, lji) in lifts.items():
        if min(lij, lji) > 0.02 * size:  # interlocked (a shaft through a bore): the one listed later goes on top
            up = True
        else:  # stacked: the one on top stays on top
            up = lij < lji - eps or (abs(lij - lji) <= eps and vol[j] <= vol[i])
        if up:
            below[j].append((i, lij))
        else:
            below[i].append((j, lji))
    plan = (a, below, lo[:, a], gap)
    return _stack(plan, np.zeros(n))[0], "xyz"[a], plan


def _stack(plan, extra):
    """Offsets of the parts stacked by ``plan``, each part ``extra`` further off those below it, and the
    part each rests on (None: on none)."""
    a, below, lo, gap = plan
    n = len(below)
    t = np.zeros(n)
    on: list = [None] * n
    left = set(range(n))
    while left:  # bottom up; a cycle is broken at its lowest part
        ready = sorted(j for j in left if all(i not in left for i, _ in below[j]))
        if not ready:
            ready = [min(left, key=lambda j: (lo[j], j))]
        for j in ready:
            for i, L in below[j]:
                if i not in left and t[i] + L + gap + extra[j] > t[j]:
                    t[j], on[j] = t[i] + L + gap + extra[j], i
            left.discard(j)
    return [np.eye(3)[a] * t[j] for j in range(n)], on


def _hull(P):
    """Convex hull, counterclockwise (monotone chain)."""
    P = sorted(set(map(tuple, np.round(np.asarray(P, float), 6))))
    if len(P) < 3:
        return np.asarray(P, float)

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower, upper = [], []
    for p in P:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    for p in reversed(P):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return np.asarray(lower[:-1] + upper[:-1], float)


def _ring(points, off: float, step: float = 0.5):
    """The view's convex outline grown by ``off`` (round corners), sampled every ``step`` mm: points and the
    arc length at each."""
    ang = np.linspace(0, 2 * math.pi, 48, endpoint=False)
    H = _hull(points)
    H = _hull((H[:, None, :] + off * np.stack([np.cos(ang), np.sin(ang)], 1)[None]).reshape(-1, 2))
    loop = np.vstack([H, H[:1]])
    seg = np.linalg.norm(np.diff(loop, axis=0), axis=1)
    L = float(seg.sum())
    s = np.arange(0.0, L, step)
    cum = np.concatenate([[0.0], np.cumsum(seg)])
    k = np.clip(np.searchsorted(cum, s, side="right") - 1, 0, len(seg) - 1)
    f = (s - cum[k]) / np.where(seg[k] > 0, seg[k], 1.0)
    return loop[k] + f[:, None] * (loop[k + 1] - loop[k]), s, L


def _pav(y):
    """Least-squares non-decreasing fit (pool adjacent violators)."""
    blocks: list[list[float]] = []  # [mean, weight]
    for v in y:
        blocks.append([float(v), 1.0])
        while len(blocks) > 1 and blocks[-2][0] > blocks[-1][0]:
            m2, w2 = blocks.pop()
            m1, w1 = blocks.pop()
            blocks.append([(m1 * w1 + m2 * w2) / (w1 + w2), w1 + w2])
    return np.concatenate([[m] * int(w) for m, w in blocks])


def _spread(t, L: float, D: float):
    """Positions on a loop of length ``L``, in the cyclic order of ``t``, at least ``D`` apart, each as near its
    ``t`` as that allows."""
    n = len(t)
    if n == 1:
        return np.asarray(t, float) % L
    order = np.argsort(t, kind="stable")
    ts = np.asarray(t, float)[order] % L
    gaps = np.diff(np.concatenate([ts, [ts[0] + L]]))
    k = int(np.argmax(gaps))  # cut the loop in its widest gap
    idx = np.concatenate([np.arange(k + 1, n), np.arange(0, k + 1)])
    seq = np.concatenate([ts[k + 1:], ts[:k + 1] + L])
    x = _pav(seq - np.arange(n) * D) + np.arange(n) * D
    if x[-1] - x[0] > L - D:  # it does not close: evenly spaced, as near as can be
        x = np.arange(n) * (L / n)
        x += float(np.mean(seq - x))
    out = np.empty(n)
    out[order[idx]] = x % L
    return out


def _seg_point_dist(P, a, b):
    """Distance from points ``P`` (n, 2) to the segment a–b."""
    d = b - a
    t = np.clip(((P - a) @ d) / max(float(d @ d), 1e-12), 0.0, 1.0)
    return np.linalg.norm(P - (a + t[:, None] * d), axis=1)


def _clearance(Q, S):
    """Distance from each point of ``Q`` to the nearest of segments ``S`` (x0, y0, x1, y1)."""
    if not len(S) or not len(Q):
        return np.full(len(Q), np.inf)
    a, d = S[None, :, :2], S[None, :, 2:] - S[None, :, :2]
    out = np.empty(len(Q))
    for s in range(0, len(Q), 128):
        q = Q[s:s + 128, None, :]
        t = np.clip(np.sum((q - a) * d, 2) / np.maximum(np.sum(d * d, 2), 1e-12), 0.0, 1.0)
        out[s:s + 128] = np.linalg.norm(q - (a + t[..., None] * d), axis=2).min(1)
    return out


def _candidates(view: _View, meshes, items):
    """Per item, points on faces of its parts that face the viewer, in view coordinates ``(x, y, seen)``, and
    the part most often in front of the unseen ones (None when all are seen)."""
    R = np.stack([view.x, view.y, view.d])
    allP = [P @ R.T for P, _ in meshes]
    big_P = np.concatenate(allP)
    off = np.cumsum([0] + [len(P) for P in allP[:-1]])
    big_T = np.concatenate([T + o for (_, T), o in zip(meshes, off)])
    out = []
    for it in items:
        cands = []
        for i in it["members"]:
            P, T = allP[i], meshes[i][1]
            V0, V1, V2 = P[T[:, 0]], P[T[:, 1]], P[T[:, 2]]
            n = np.cross(V1 - V0, V2 - V0)
            area = np.linalg.norm(n, axis=1)
            front = n[:, 2] > 0.2 * area
            if not front.any():
                front = area > 0
            k = np.flatnonzero(front)[np.argsort(-area[front], kind="stable")][:60]  # the largest first
            m = (V0[k] + V1[k] + V2[k]) / 3
            cands.append(np.concatenate([m] + [m + 0.5 * (V[k] - m) for V in (V0, V1, V2)]))
        C = np.concatenate(cands)
        _, top = _span(C[:, :2], big_P, big_T, 2)
        seen = ~(top > C[:, 2] + 1e-6 * max(1.0, float(np.abs(C).max())))
        cover = None
        if seen.mean() < 0.25:  # mostly hidden: what is in front of it
            hid = C[~seen]
            fronts = [np.sum(_span(hid[:, :2], allP[p], meshes[p][1], 2)[1] > hid[:, 2] + 1e-6)
                      if p not in it["members"] else -1 for p in range(len(meshes))]
            cover = int(np.argmax(fronts))
        out.append((np.column_stack([C[:, :2], seen.astype(float)]), cover))
    return out


def _anchors(cands, geo1):
    """The candidates with their distance to the nearest drawn line: ``(x, y, seen, clearance)`` (model mm)."""
    return [np.column_stack([C, _clearance(C[:, :2], geo1)]) for C, _ in cands]


def _balloons(c: _Canvas, geo, anchors, items, s):
    """Numbered balloons on a ring around the view, a leader from each to a dot on its part.

    Each dot goes where its part is seen, clear of lines and with a short way out; the balloons keep the dots'
    order around the view and are spread to stand apart; then leaders that cross swap balloons (each swap
    shortens them, so it ends) until none does."""
    P = geo.reshape(-1, 2) if len(geo) else np.zeros((1, 2))
    n = len(items)
    off = RING
    for _ in range(12):  # grow the ring until the balloons go round
        ring, arc, L = _ring(P, off)
        if n * BALLOON_SEP <= 0.9 * L:
            break
        off += (n * BALLOON_SEP / 0.9 - L) / (2 * math.pi) + 1.0
    dots, want = [], []
    for A in anchors:
        Q = A[:, :2] * s
        clr = A[:, 3] * s
        d = np.linalg.norm(Q[:, None, :] - ring[None, :, :], axis=2)
        near = d.argmin(1)
        dist = d[np.arange(len(Q)), near]
        cost = 0.04 * dist - np.minimum(clr, 5.0) + 50.0 * (A[:, 2] < 0.5) + 30.0 * (clr < 1.0)
        top = np.argsort(cost, kind="stable")[:25]  # the best few: and the fewest lines crossed on the way out
        cost = cost[top] + 3.0 * np.array([_ncross(Q[k], ring[near[k]], geo) for k in top])
        k = int(top[int(np.argmin(cost))])
        dots.append(Q[k])
        want.append(arc[near[k]])
    at = _spread(np.array(want), L, BALLOON_SEP)
    slots = [ring[int(round(x / (arc[1] - arc[0]))) % len(ring)] for x in at]
    order = list(range(n))  # item k's balloon at slots[order[k]]
    for _ in range(4 * n * n):
        swapped = False
        for a_ in range(n):
            for b_ in range(a_ + 1, n):
                if _cross(dots[a_], slots[order[a_]], np.array([[*dots[b_], *slots[order[b_]]]])):
                    order[a_], order[b_] = order[b_], order[a_]
                    swapped = True
        if not swapped:
            break
    for _ in range(n):  # a leader passing through another balloon: swap the two when that crosses nothing
        bad = False
        for a_ in range(n):
            for b_ in range(n):
                if a_ == b_:
                    continue
                Cb = slots[order[b_]]
                if _seg_point_dist(np.array([Cb]), dots[a_], slots[order[a_]])[0] < BALLOON_R + 0.8:
                    trial = list(order)
                    trial[a_], trial[b_] = trial[b_], trial[a_]
                    segs = np.array([[*dots[k], *slots[trial[k]]] for k in range(n)])
                    if not any(_ncross(segs[k, :2], segs[k, 2:], np.delete(segs, k, 0)) for k in range(n)):
                        order = trial
                        bad = True
        if not bad:
            break
    out = []
    for k, it in enumerate(items):
        ctr, dot = np.asarray(slots[order[k]]), np.asarray(dots[k])
        u = _unit(dot - ctr)
        edge = ctr + BALLOON_R * u
        c.path("sym", _prim_cmds(("circle", float(ctr[0]), float(ctr[1]), BALLOON_R), 1.0))
        c.line("thin", edge, dot)
        c.leaders.append((*edge, *dot))
        ang = np.linspace(0, 2 * math.pi, 10, endpoint=False)
        c.ops.append(("fill", tuple((float(dot[0] + DOT * math.cos(t)), float(dot[1] + DOT * math.sin(t))) for t in ang)))
        c.text(float(ctr[0]), float(ctr[1]) - 0.36 * BALLOON_TEXT, str(it["no"]), size=BALLOON_TEXT, anchor="middle")
        out.append({"item": it["no"], "center": tuple(map(float, ctr)), "dot": tuple(map(float, dot))})
    return out


def _bom_rows(items, parts, bom):
    """The parts list: Item, Name, Qty and the columns ``bom`` gives per part."""
    extra: dict[int, dict] = {}
    if bom is not None:
        if not isinstance(bom, dict) or not all(isinstance(v, dict) for v in bom.values()):
            raise TuleError("bom is {part name: {column: value}}", op="drawing",
                            hint='bom={"cam": {"material": "PLA", "source": "printed"}, "screw": {"source": "ISO 4762 M3×12"}}')
        for key, cols in bom.items():
            hit = {i for i in _match(parts, str(key))} | {i for it in items if it["name"] == key for i in it["members"]}
            its = [it for it in items if set(it["members"]) & hit]
            if not its:
                raise TuleError(f"bom names {key!r}, which is no part", op="drawing",
                                hint="parts: " + ", ".join(it["name"] for it in items))
            for it in its:
                extra.setdefault(it["no"], {}).update(cols)
    heads = list(dict.fromkeys(k for cols in extra.values() for k in cols if str(k).strip().lower() != "name"))
    head = ["Item", "Name", "Qty"] + [str(k).capitalize() if str(k).islower() else str(k) for k in heads]
    body = []
    for it in items:
        e = {(str(k).strip().lower() if str(k).strip().lower() == "name" else k): v for k, v in extra.get(it["no"], {}).items()}
        body.append([str(it["no"]), _value(e.get("name", it["name"])), str(len(it["members"]))]
                    + [_value(e[k]) if e.get(k) is not None else "" for k in heads])
    return head, body


def _trails(view: _View, parts, offs, back, meshes):
    """Where each moved part goes: a chain line (view coordinates) from the middle of its face toward where it
    went, ``back`` (its move less that of the part it rests on); only where no part is in front of it."""
    R = np.stack([view.x, view.y, view.d])
    big_P = np.concatenate([P @ R.T for P, _ in meshes])
    off = np.cumsum([0] + [len(P) for P, _ in meshes[:-1]])
    big_T = np.concatenate([T + o for (_, T), o in zip(meshes, off)])
    out = []
    for (_, _, sh), o, b in zip(parts, offs, back):
        L = float(np.linalg.norm(b))
        if L < 1e-9:
            continue
        u = b / L
        lo, hi = (np.asarray(v, dtype=float) for v in sh.bounds)
        c = (lo + hi) / 2 + o - u * (np.abs(hi - lo) @ np.abs(u)) / 2  # the middle of its face toward the move
        t = np.linspace(0.0, 1.0, max(2, int(L / 0.5) + 1))
        pts = (c[None, :] - t[:, None] * b[None, :]) @ R.T
        _, top = _span(pts[:, :2], big_P, big_T, 2)
        seen = ~(top > pts[:, 2] + 1e-6 * max(1.0, float(np.abs(pts).max())))
        k = 0
        while k < len(t):  # each run of seen samples
            if not seen[k]:
                k += 1
                continue
            j = k
            while j + 1 < len(t) and seen[j + 1]:
                j += 1
            if j > k:
                out.append((tuple(pts[k, :2]), tuple(pts[j, :2])))
            k = j + 1
    return out


def _asm_view(views) -> str:
    if views is None:
        return "iso"
    if isinstance(views, (list, tuple)) and len(views) == 1:
        views = views[0]
    if not isinstance(views, str) or views not in VIEWS:
        raise TuleError(f"an assembly drawing has one view, got {views!r}", op="drawing",
                        hint='views="front" (iso when left out); one of ' + ", ".join(VIEWS))
    return views


def _ring_box(P, n, s):
    """The view and its ring of ``n`` balloons at scale ``s``: their bounds (sheet mm)."""
    off = RING
    for _ in range(12):
        ring, _, L = _ring(P * s, off, 2.0)
        if n * BALLOON_SEP <= 0.9 * L:
            break
        off += (n * BALLOON_SEP / 0.9 - L) / (2 * math.pi) + 1.0
    r = BALLOON_R + 1.0
    return (float(ring[:, 0].min()) - r, float(ring[:, 1].min()) - r, float(ring[:, 0].max()) + r,
            float(ring[:, 1].max()) + r)


def _render_assembly(item, explode, bom, views=None, title=None, info=None, sheet=None, scale=None, name=None,
                     notes=None, revisions=None, number=(1, 1)):
    """An assembly drawing on one sheet: the parts (exploded) in one view, a numbered balloon on each item,
    the parts list on the title block. Facts as ``_render_part``'s, and ``items``, ``parts_list``, ``balloons``."""
    asm, parts = _asm_parts(item)
    view = _View(_asm_view(views))
    if sheet is not None and sheet not in SHEETS:
        raise TuleError(f"unknown sheet {sheet!r}", op="drawing", hint=", ".join(SHEETS) + ", or leave it out")
    fixed = _parse_scale(scale)
    items = _items(parts)
    meshes = []
    for _, _, sh in parts:
        lo, hi = sh.bounds
        meshes.append(_part_mesh(sh, max(0.01, 0.004 * float(np.linalg.norm(np.subtract(hi, lo))))))
    offs, axis, plan = _explode(parts, explode, meshes)
    cands = _candidates(view, [(P + o, T) for (P, T), o in zip(meshes, offs)], items)
    if plan is not None:  # a part hidden behind another (under a lid): that one goes further up, until all are seen
        extra = np.zeros(len(parts))
        for _ in range(6):
            covers = {cv for _, cv in cands if cv is not None}
            if not covers:
                break
            for cv in covers:
                lo_, hi_ = parts[cv][2].bounds
                extra[cv] += 0.3 * max(float(np.subtract(hi_, lo_)[k]) for k in range(3) if k != "xyz".index(axis))
            offs = _stack(plan, extra)[0]
            cands = _candidates(view, [(P + o, T) for (P, T), o in zip(meshes, offs)], items)
    moved = Assembly(*[sh.at(*map(float, o)) if np.any(o) else sh for (_, _, sh), o in zip(parts, offs)])
    shape = moved.to_shape()
    lo, hi = shape.bounds
    diag = float(np.linalg.norm(np.subtract(hi, lo)))
    _hlr(shape.wrapped, view, False, max(1e-4, diag * 1e-6))
    if plan is not None:  # each part's way back onto the one it rests on
        on = _stack(plan, extra)[1]
        back = [offs[j] - offs[on[j]] if on[j] is not None else np.zeros(3) for j in range(len(parts))]
    else:
        back = offs
    trails = _trails(view, parts, offs, back, [(P + o, T) for (P, T), o in zip(meshes, offs)])
    one = _Canvas()
    for p in view.vis:
        one.path("vis", _prim_cmds(p, 1.0))
    for p, q in trails:
        one.line("center", p, q)
    geo1 = _segments(one.ops)
    anchors = _anchors(cands, geo1)
    rows = _bom_rows(items, parts, bom)
    notes = _notes(notes)
    revs = _revisions(revisions)

    given = next((str(v) for k, v in (info or {}).items()
                  if re.sub(r"[_\s]+", " ", str(k).strip().lower()) in FIELDS[0][2] and v), None)
    title = title or given or asm.name or name or "Assembly"
    tb_rows, rev = _fields(info, title)
    if revs:
        if rev and rev != revs[-1][0]:
            raise TuleError(f"info rev {rev!r} is not the last revision {revs[-1][0]!r}", op="drawing",
                            hint="the last entry of revisions is the current one; drop rev from info")
        rev = revs[-1][0]
    tb_h, rv_h = _tb_height(tb_rows), _rev_height(revs)
    ws, ph = _table_size(rows, PARTS_TEXT)
    colw = max(TB_W, sum(ws))
    ws = ws[:1] + [ws[1] + colw - sum(ws)] + ws[2:]  # as wide as the title block: the names take the rest
    P1 = geo1.reshape(-1, 2) if len(geo1) else np.zeros((1, 2))
    extent = float(np.max(np.subtract(hi, lo)))
    boxes: dict = {}

    def fit(s, size):
        """Where the view goes at scale ``s`` on ``size``, and where the notes go; or None."""
        W, H = SHEETS[size]
        if s not in boxes:
            boxes[s] = _ring_box(P1, len(items), s)
        b = boxes[s]
        frame = (MARGIN + 3, MARGIN + 3, W - MARGIN - 3, H - MARGIN - 3)
        col = tb_h + rv_h + ph
        options = [("column", col + (4.0 + _notes_height(notes, colw - 1) if notes else 0.0), [])]
        if notes:
            nw = min(TB_W + 40, W - 2 * MARGIN - colw - 16)
            options.append(("left", col, [(MARGIN - 1, MARGIN - 1, MARGIN + 4 + nw + 3,
                                           MARGIN + 4 + _notes_height(notes, nw) + 3)]))
        for where, h, more in options:
            if MARGIN + h > H - MARGIN - 4:
                continue
            keep = [(W - MARGIN - colw - 3, MARGIN - 1, W - MARGIN + 1, MARGIN + h + 3)] + more
            at = _place([b], frame, keep)
            if at is not None:
                return at, where
        return None

    sizes = [sheet] if sheet else list(AUTO_SHEETS)
    if fixed:
        got = next(((fixed, sz, f) for sz in sizes if (f := fit(fixed, sz))), None)
        if got is None:
            raise TuleError(f"the assembly does not fit {sizes[-1]} at {_scale_text(fixed)}", op="drawing",
                            hint="leave scale out for the largest that fits" + ("" if sheet else ', or sheet="A1"'))
    else:
        order = [x for x in SCALES if x > 1 and extent * x <= ENLARGE] + [x for x in SCALES if x <= 1]
        found = []
        for sz in sizes:  # each sheet's largest scale that takes it
            for x in order:
                if (f := fit(x, sz)):
                    found.append((sz, x, f))
                    break
        if not found:
            raise TuleError(f"the assembly and its parts list do not fit {sizes[-1]}", op="drawing",
                            hint="a larger sheet (sheet=\"A1\"), or fewer parts")
        top = max(x for _, x, _ in found)  # the smallest sheet within one standard scale of the largest's
        sz, x, f = next(g for g in found if g[1] >= top / 2.5 - 1e-9)
        got = (x, sz, f)
    s, size, ((ax, ay), where) = got
    W, H = SHEETS[size]
    page = _Canvas()
    page.rect("frame", MARGIN, MARGIN, W - MARGIN, H - MARGIN)
    vc = _Canvas()
    for p in view.vis:
        vc.path("vis", _prim_cmds(p, s))
    for p, q in trails:
        vc.center_line(np.asarray(p) * s, np.asarray(q) * s)
    balloons = _balloons(vc, _segments(vc.ops), anchors, items, s)
    page.add(vc, ax, ay)
    for bl in balloons:
        bl["center"] = (bl["center"][0] + ax, bl["center"][1] + ay)
        bl["dot"] = (bl["dot"][0] + ax, bl["dot"][1] + ay)
    label = size if number[1] == 1 else f"{size}  {number[0]}/{number[1]}"
    _title_block(page, W, title, tb_rows, rev, _scale_text(s), label, None)
    if revs:
        _revision_table(page, W - MARGIN - TB_W, MARGIN + tb_h, revs)
    y = MARGIN + tb_h + rv_h + ph
    _table_block(page, W - MARGIN - colw, y, rows, PARTS_TEXT, ws)
    if notes:
        if where == "column":
            _notes_block(page, W - MARGIN - colw + 1, y + 4.0, colw - 1, notes)
        else:
            _notes_block(page, MARGIN + 4, MARGIN + 4, min(TB_W + 40, W - 2 * MARGIN - colw - 16), notes)
    sheets = [{"sheet": size, "scale": _scale_text(s)}]
    facts = {"scale": sheets[0]["scale"], "sheet": size, "sheets": sheets, "title": title, "rev": rev,
             "texts": [t for t, _ in page.labels], "frames": [], "pages": [(page, SHEETS[size])], "bends": [],
             "shape": asm.to_shape(), "parts_list": rows, "balloons": balloons,
             "items": [{"item": it["no"], "name": it["name"], "qty": len(it["members"]), "parts": it["paths"]}
                       for it in items],
             "explode": {"axis": axis, "offsets": {parts[i][0]: tuple(float(v) for v in o)
                                                   for i, o in enumerate(offs) if np.any(o)}}}
    return page, SHEETS[size], facts


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


def to_pdf(c, size=None, title: str = "") -> bytes:
    """A vector PDF of one page (``c``, ``size``) or of several (``c``: a list of ``(canvas, size)``);
    Helvetica, its title the drawing's, no dates: the same drawing gives the same bytes."""
    pages = c if isinstance(c, list) else [(c, size)]
    k = 72 / 25.4
    n = len(pages)
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{' '.join(f'{6 + 2 * i} 0 R' for i in range(n))}] /Count {n} >>".encode(),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>",
        f"<< /Title ({_pdf_text(title)}) /Producer (tule) >>".encode("latin-1"),
    ]
    for i, (pc, (W, H)) in enumerate(pages):
        stream = _pdf_stream(pc)
        objs.append((f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {_n(W * k)} {_n(H * k)}] "
                     f"/Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> /Contents {7 + 2 * i} 0 R >>").encode())
        objs.append(f"<< /Length {len(stream)} /Filter /FlateDecode >>\nstream\n".encode() + stream + b"\nendstream")
    buf = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for i, o in enumerate(objs, 1):
        offsets.append(len(buf))
        buf += f"{i} 0 obj\n".encode() + o + b"\nendobj\n"
    xref = len(buf)
    buf += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for o in offsets:
        buf += f"{o:010d} 00000 n \n".encode()
    buf += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R /Info 5 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(buf)


def _pdf_stream(c: _Canvas) -> bytes:
    """One page's content stream, compressed."""
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
    return zlib.compress("\n".join(out).encode("latin-1"), 9)


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
    return to_dxf_entities([("0", p) for p in view.vis])


# R12 linetypes: name -> (description, pattern in mm: dashes > 0, gaps < 0, dots 0)
DXF_LTYPES = {"CONTINUOUS": ("Solid line", ()), "DASHDOT": ("Dash dot __ . __ .", (8.0, -2.0, 0.0, -2.0)),
              "DASHED": ("Dashed __ __ __", (6.0, -3.0))}


def to_dxf_entities(prims, texts=(), layers=None) -> str:
    """An ASCII DXF (R12, mm) of ``(layer, primitive)`` pairs and ``(layer, x, y, height, angle, text)``
    texts. ``layers`` (``{name: (ACI color, linetype)}``) adds a layer table, so CAM programs see the
    layers' colors and dash-dot bend lines; without it everything is plain."""

    def g(code, value):
        return f"{code}\n{value}\n"

    def num(v):
        s = f"{v:.6f}".rstrip("0").rstrip(".")
        return "0" if s in ("-0", "") else s

    out = [g(0, "SECTION"), g(2, "HEADER"), g(9, "$ACADVER"), g(1, "AC1009"), g(9, "$INSUNITS"), g(70, 4),
           g(0, "ENDSEC")]
    if layers:
        out.append(g(0, "SECTION") + g(2, "TABLES") + g(0, "TABLE") + g(2, "LTYPE") + g(70, len(DXF_LTYPES)))
        for name, (desc, pat) in DXF_LTYPES.items():
            out.append(g(0, "LTYPE") + g(2, name) + g(70, 0) + g(3, desc) + g(72, 65) + g(73, len(pat))
                       + g(40, num(sum(abs(x) for x in pat))) + "".join(g(49, num(x)) for x in pat))
        out.append(g(0, "ENDTAB") + g(0, "TABLE") + g(2, "LAYER") + g(70, len(layers)))
        for name, (color, ltype) in layers.items():
            out.append(g(0, "LAYER") + g(2, name) + g(70, 0) + g(62, color) + g(6, ltype))
        out.append(g(0, "ENDTAB") + g(0, "ENDSEC"))
    out.append(g(0, "SECTION") + g(2, "ENTITIES"))
    for layer, p in prims:
        L = g(8, layer)
        if p[0] == "line":
            out.append(g(0, "LINE") + L + g(10, num(p[1])) + g(20, num(p[2])) + g(30, 0)
                       + g(11, num(p[3])) + g(21, num(p[4])) + g(31, 0))
        elif p[0] == "circle":
            out.append(g(0, "CIRCLE") + L + g(10, num(p[1])) + g(20, num(p[2])) + g(30, 0) + g(40, num(p[3])))
        elif p[0] == "arc":
            out.append(g(0, "ARC") + L + g(10, num(p[1])) + g(20, num(p[2])) + g(30, 0) + g(40, num(p[3]))
                       + g(50, num(p[4])) + g(51, num(p[5])))
        else:  # ("poly", points) open, or ("poly", points, closed, bulges): arcs as bulges tan(sweep / 4)
            closed = len(p) > 2 and p[2]
            bulges = p[3] if len(p) > 3 else ()
            out.append(g(0, "POLYLINE") + L + g(66, 1) + g(10, 0) + g(20, 0) + g(30, 0) + g(70, 1 if closed else 0))
            for i, (x, y) in enumerate(p[1]):
                b = bulges[i] if i < len(bulges) else 0.0
                out.append(g(0, "VERTEX") + L + g(10, num(x)) + g(20, num(y)) + g(30, 0)
                           + (g(42, num(b)) if abs(b) > 1e-12 else ""))
            out.append(g(0, "SEQEND") + L)
    for layer, x, y, h, angle, text in texts:
        out.append(g(0, "TEXT") + g(8, layer) + g(10, num(x)) + g(20, num(y)) + g(30, 0) + g(40, num(h))
                   + g(1, text) + (g(50, num(angle)) if angle else ""))
    out.append(g(0, "ENDSEC") + g(0, "EOF"))
    return "".join(out)


def dxf(item, path: str, view: str = "top") -> str:
    """Write one view's visible edges as a DXF at 1:1 in mm (laser, waterjet, CNC). Returns the path.

    A sheet part (``sheet``, ``unfold``) writes its flat pattern instead, whatever the view: the
    outline and holes as closed outlines on layer CUT, nothing else (``export_dxf(bends=True)`` for bend lines)."""
    shape, _ = _as_shape(item)
    if shape.sheet_metal is not None:
        from .sheetmetal import export_dxf

        return export_dxf(shape, path)
    if view not in VIEWS:
        raise TuleError(f"unknown view {view!r}", op="drawing.dxf", hint=", ".join(VIEWS))
    v = _View(view)
    lo, hi = shape.bounds
    tol = max(1e-4, float(np.linalg.norm(np.subtract(hi, lo))) * 1e-6)
    _hlr(shape.wrapped, v, False, tol)
    return write_atomic(path, to_dxf(v))


def write(folder: str, base: str, item, views=None, dims=(),
          title=None, info=None, sheet=None, scale=None, hidden=True, projection="first", dxf_view=None,
          notes=None, tol=None, revisions=None, callouts=None, sheets=None, datums=None, table=None, explode=None,
          bom=None) -> dict:
    """Write ``<base>.pdf`` (every sheet a page) and ``<base>.svg`` (``<base>-2.svg``, ... for further sheets),
    and ``<base>.dxf`` (``<base>-<part>.dxf`` per part of several), into ``folder``; returns facts (scale, texts)."""
    parts = _parts(item)

    def stem(k):
        from .outputs import _slug

        if len(parts) == 1:
            return base
        return _slug(k) if _slug(k).startswith(base) else f"{base}-{_slug(k)}"  # tray-flat, not tray-tray-flat

    if dxf_view and explode is not None:
        raise TuleError("dxf writes a part's view for cutting, not an assembly drawing", op="drawing",
                        hint="outputs.drawing(name, asm[\"plate\"], dxf=\"top\") for the part")
    for k, it in parts:
        shape, _ = _as_shape(it)
        if dxf_view and shape.sheet_metal is not None:  # a cut file needs the flat: fail first, once, naming the part
            from .sheetmetal import _flat

            _flat(shape, "drawing", k or base)
    _, _, facts = render(item, views, dims, title, info, sheet, scale, hidden, projection, base, notes=notes, tol=tol,
                         revisions=revisions, callouts=callouts, sheets=sheets, datums=datums, table=table,
                         explode=explode, bom=bom)
    pages = facts["pages"]
    write_atomic(os.path.join(folder, base + ".pdf"), to_pdf(pages, title=facts["title"]))
    for i, (page, size) in enumerate(pages):
        n = len(pages)
        write_atomic(os.path.join(folder, base + (f"-{i + 1}" if i else "") + ".svg"),
                     to_svg(page, size, facts["title"] + (f" ({i + 1}/{n})" if n > 1 else "")))
    # a formed part drawn with its flat: the flat's DXF and bend table are the cut files, once
    flats = any(sh.sheet_metal is not None and sh.sheet_metal.flat for _, sh, _ in facts["parts"])
    for k, shape, bends in facts["parts"]:
        if flats and shape.sheet_metal is not None and not shape.sheet_metal.flat:
            continue
        if dxf_view:
            dxf(shape, os.path.join(folder, stem(k) + ".dxf"), dxf_view)
        if bends:  # found by render (a part that does not unfold has said so there, once)
            from .sheetmetal import bend_csv

            write_atomic(os.path.join(folder, stem(k) + "-bends.csv"), bend_csv(shape))
    facts["stems"] = [(stem(k), it) for k, it in parts]
    return facts
