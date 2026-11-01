"""Design for manufacturing: check a part for a process, report in terms of its faces.

```python
from tule import *

report = dfm.cnc(part, axis=-Z, tool_radius=3, min_wall=1.0)
print(report)                       # one line per finding, worst first
report.ok                           # no errors (warnings and infos allowed)
report.errors, report.warnings      # findings by severity
outputs.overlay("bracket-dfm", part, report)   # the findings on the part, in the viewer
```

Every finding names B-rep faces by the ids of ``part.faces`` (the viewer's
``f12``), the tags on those faces, a severity (``"error"``: cannot be made
this way, ``"warning"``: makeable but costly or risky, ``"info"``: a fact
worth knowing), a short ``label``, a ``message`` that says what is wrong and
what would fix it, and ``values`` (in mm and degrees).

**CNC, 3-axis** (``dfm.cnc``): the tool moves along ``axis`` into the part
(``-Z``: from above). A list of axes means several setups; a face passes if
any setup reaches it.

- ``undercut``: faces (or parts of them) facing away from the tool or hidden
  behind other material along the tool axis. The face the part sits on
  (facing straight away, at the lowest level) is the stock face, not an
  undercut.
- ``corner_radius``: internal corners along the tool axis must be rounded
  at least to the tool radius (sharp concave edges and concave cylinders
  smaller than ``tool_radius``). A full hole smaller than the tool is a
  ``hole`` warning (drill it or use a smaller tool).
- ``pocket_width`` / ``pocket_depth``: floors (faces facing the tool,
  below the top) narrower than the tool, or deeper than ``max_depth_ratio``
  times their width.
- ``thin_wall``: material thinner than ``min_wall`` (error below half of it).

**FDM** (``dfm.fdm``): the part is built along ``up``, on a bed at its
lowest level.

- ``overhang``: faces leaning more than ``max_overhang`` degrees from
  vertical (downward-facing) need support.
- ``bridge``: horizontal ceilings spanning more than ``max_bridge`` between
  supporting walls (or not supported at all) need support.
- ``thin_wall``: below ``min_wall`` (about two nozzle widths).
- ``bed_contact``: the flat area on the bed (an error if there is none).

The checks work on the B-rep and a tessellation of it (ray casts for access
and thickness, a raster for floor widths), so values are accurate to about
the tessellation tolerance (0.05 mm by default).
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

import numpy as np
from OCP.BRepAdaptor import BRepAdaptor_Surface
from OCP.BRepOffset import BRepOffset_Analyse
from OCP.ChFiDS import ChFiDS_Concave, ChFiDS_Convex
from OCP.GeomAbs import GeomAbs_Cylinder

from ._occ import subshapes
from .errors import TuleError
from .geom import Vec, VecLike, vec
from .selection import edge_direction, face_center, face_normal, face_type, is_degenerate
from .shape import Shape

SEVERITIES = ("error", "warning", "info")
_RANK = {s: i for i, s in enumerate(SEVERITIES)}


# ---------------------------------------------------------------- values


@dataclass(frozen=True)
class Finding:
    """One problem (or fact) about a part, in terms of its B-rep faces."""

    check: str
    severity: str
    label: str
    message: str
    faces: tuple[int, ...]
    edges: tuple[int, ...] = ()
    tags: tuple[str, ...] = ()
    values: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d: dict[str, Any] = {"check": self.check, "severity": self.severity, "label": self.label,
                             "message": self.message, "faces": list(self.faces)}
        if self.edges:
            d["edges"] = list(self.edges)
        if self.tags:
            d["tags"] = list(self.tags)
        if self.values:
            d["values"] = dict(self.values)
        return d

    def __str__(self) -> str:
        where = " ".join([f"f{i}" for i in self.faces[:6]] + (["…"] if len(self.faces) > 6 else []))
        tags = f" [{', '.join(self.tags)}]" if self.tags else ""
        return f"{self.severity:<7} {self.check:<14} {where}{tags}: {self.message}"


@dataclass(frozen=True)
class Report:
    """The findings of one DFM check. ``ok`` means no errors."""

    process: str
    params: dict
    findings: tuple[Finding, ...]

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def errors(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.severity == "error")

    @property
    def warnings(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.severity == "warning")

    def __len__(self) -> int:
        return len(self.findings)

    def __iter__(self):
        return iter(self.findings)

    def to_dict(self) -> dict:
        return {"process": self.process, "params": dict(self.params), "ok": self.ok,
                "findings": [f.to_dict() for f in self.findings]}

    def to_json(self, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    def __str__(self) -> str:
        n = {s: sum(1 for f in self.findings if f.severity == s) for s in SEVERITIES}
        params = ", ".join(f"{k}={_fmt_param(v)}" for k, v in self.params.items())
        head = (f"DFM {self.process} ({params}): "
                f"{'ok' if self.ok else 'NOT OK'}, {n['error']} errors, {n['warning']} warnings, {n['info']} info")
        return "\n".join([head] + [f"  {f}" for f in self.findings])

    __repr__ = __str__


def _fmt_param(v) -> str:
    if isinstance(v, (list, tuple)) and v and isinstance(v[0], str):
        return "[" + ", ".join(v) + "]"
    return str(v)


def _r(x: float, nd: int = 3) -> float:
    return float(round(float(x), nd))


def _axis_name(v: Vec) -> str:
    for name, d in (("X", (1, 0, 0)), ("Y", (0, 1, 0)), ("Z", (0, 0, 1))):
        if abs(v.dot(d) - 1) < 1e-9:
            return "+" + name
        if abs(v.dot(d) + 1) < 1e-9:
            return "-" + name
    return f"({v.x:.3g}, {v.y:.3g}, {v.z:.3g})"


def _unit(v: VecLike, what: str) -> Vec:
    v = vec(v)
    if v.length < 1e-12:
        raise TuleError(f"{what} must be a nonzero direction", op="dfm", hint="e.g. -Z or (0, 0, -1)")
    return v.normalized()


# ---------------------------------------------------------------- analysis of a part


class _Part:
    """Facts about a shape shared by the checks: faces, edges, adjacency, convexity, a mesh."""

    def __init__(self, shape: Shape, tolerance: float):
        if not isinstance(shape, Shape):
            raise TuleError(f"expected a Shape, got {type(shape).__name__}", op="dfm")
        if not shape._sub("face"):
            raise TuleError("the shape has no faces", op="dfm")
        self.shape = shape
        self.faces = shape._sub("face")
        self.edges = shape._sub("edge")
        lo, hi = shape.bounds
        self.lo, self.hi = np.array(lo), np.array(hi)
        self.size = float(np.linalg.norm(self.hi - self.lo))
        self.eps = 1e-5 * self.size
        m = shape.tessellate(tolerance, 0.2)
        self.mesh = m
        P = m.positions.astype(np.float64)
        T = m.indices.astype(np.int64)
        self.V = P[T]  # (m, 3, 3)
        cr = np.cross(self.V[:, 1] - self.V[:, 0], self.V[:, 2] - self.V[:, 0])
        ln = np.linalg.norm(cr, axis=1)
        self.tri_area = ln / 2
        self.tri_n = cr / np.maximum(ln, 1e-300)[:, None]
        self.tri_c = self.V.mean(axis=1)
        self.tri_face = m.face_ids.astype(np.int64)
        self.face_area = np.bincount(self.tri_face, self.tri_area, minlength=len(self.faces))
        self.edge_lines = {}
        for eid, pl in m.edges:
            self.edge_lines.setdefault(eid, pl.astype(np.float64))
        # adjacency
        self.edge_faces: dict[int, list[int]] = {}
        self.face_edges: list[list[int]] = []
        for fi, f in enumerate(self.faces):
            es = []
            for e in subshapes(f, "edge"):
                if is_degenerate(e):
                    continue
                ei = self.edges.index(e)
                if ei < 0:
                    continue
                es.append(ei)
                lst = self.edge_faces.setdefault(ei, [])
                if fi not in lst:
                    lst.append(fi)
            self.face_edges.append(es)
        self._concavity = None
        self.reach_by: list[np.ndarray] = []  # per CNC setup: triangles the tool reaches

    # -- facts
    def tags(self, faces: Iterable[int]) -> tuple[str, ...]:
        out = set()
        for i in faces:
            out.update(self.shape._tags_of(self.faces[i]))
        return tuple(sorted(out))

    def concavity(self, ei: int) -> str:
        """``"concave"``, ``"convex"`` or ``"tangent"`` for an edge between two faces."""
        if self._concavity is None:
            an = BRepOffset_Analyse(self.shape.wrapped, 0.01)
            self._concavity = an
        try:
            ivs = list(self._concavity.Type(self.edges[ei]))
        except Exception:  # pragma: no cover - OCCT refuses some edges
            return "other"
        if not ivs:
            return "other"
        t = ivs[0].Type()
        return "concave" if t == ChFiDS_Concave else "convex" if t == ChFiDS_Convex else "tangent"

    def heights(self, fi: int, up: np.ndarray) -> tuple[float, float]:
        sel = self.tri_face == fi
        if not sel.any():
            c = np.array(face_center(self.faces[fi]))
            h = float(c @ up)
            return h, h
        h = self.V[sel].reshape(-1, 3) @ up
        return float(h.min()), float(h.max())

    def walls(self, fi: int) -> list[int]:
        """Edges of planar face ``fi`` where a neighbour rises on its outward side (a wall, not an open side)."""
        n = np.array(face_normal(self.faces[fi]))
        lvl = float(np.array(face_center(self.faces[fi])) @ n)
        out = []
        for ei in self.face_edges[fi]:
            kind = self.concavity(ei)
            if kind == "concave":
                out.append(ei)
            elif kind == "tangent":
                others = [g for g in self.edge_faces.get(ei, ()) if g != fi]
                if any(float(np.array(face_center(self.faces[g])) @ n) > lvl + 1e-9 for g in others):
                    out.append(ei)
        return out

    def reached(self, fi: int, setup: int) -> bool:
        """Whether CNC setup ``setup`` reaches (almost) all of face ``fi``."""
        sel = self.tri_face == fi
        return bool(self.tri_area[sel & self.reach_by[setup]].sum() >= 0.99 * self.face_area[fi])

    def tri_samples(self, fi: int, cap: int = 200) -> np.ndarray:
        idx = np.nonzero(self.tri_face == fi)[0]
        if len(idx) > cap:
            idx = idx[np.linspace(0, len(idx) - 1, cap).astype(np.int64)]
        return idx

    # -- ray casting against the tessellation
    def cast(self, origins: np.ndarray, dirs: np.ndarray, tmax: np.ndarray | float) -> tuple[np.ndarray, np.ndarray]:
        """Nearest hit of each ray within ``tmax``: (distance or inf, triangle index or -1)."""
        n = len(origins)
        tmax = np.broadcast_to(np.asarray(tmax, dtype=np.float64), (n,)).copy()
        tmax = np.minimum(tmax, 2 * self.size + 1.0)
        t_hit = np.full(n, np.inf)
        tri_hit = np.full(n, -1, dtype=np.int64)
        if n == 0:
            return t_hit, tri_hit
        V0, E1, E2 = self.V[:, 0], self.V[:, 1] - self.V[:, 0], self.V[:, 2] - self.V[:, 0]
        tlo, thi = self.V.min(axis=1), self.V.max(axis=1)
        cell = max(self.size / 32, 1e-9)
        key = np.floor((origins - self.lo) / cell).astype(np.int64)
        order = np.lexsort((key[:, 2], key[:, 1], key[:, 0]))
        tiny = 1e-9 * self.size
        for start in range(0, n, 48):
            c = order[start:start + 48]
            o, d, tm = origins[c], dirs[c], tmax[c]
            end = o + d * tm[:, None]
            lo = np.minimum(o, end).min(axis=0) - tiny
            hi = np.maximum(o, end).max(axis=0) + tiny
            cand = np.nonzero(np.all(thi >= lo, axis=1) & np.all(tlo <= hi, axis=1))[0]
            if len(cand) == 0:
                continue
            for s2 in range(0, len(cand), 4096):
                cc = cand[s2:s2 + 4096]
                e1, e2, v0 = E1[cc][None], E2[cc][None], V0[cc][None]
                dd = d[:, None, :]
                p = np.cross(dd, e2)
                det = np.einsum("ijk,ijk->ij", e1, p)
                ok = np.abs(det) > 1e-14
                inv = np.where(ok, 1.0 / np.where(ok, det, 1.0), 0.0)
                s = o[:, None, :] - v0
                u = np.einsum("ijk,ijk->ij", s, p) * inv
                q = np.cross(s, e1)
                v = np.einsum("ijk,ijk->ij", dd, q) * inv
                t = np.einsum("ijk,ijk->ij", e2, q) * inv
                hit = ok & (u >= -1e-9) & (v >= -1e-9) & (u + v <= 1 + 1e-9) & (t > tiny) & (t <= tm[:, None])
                t = np.where(hit, t, np.inf)
                j = np.argmin(t, axis=1)
                tj = t[np.arange(len(c)), j]
                better = tj < t_hit[c]
                t_hit[c[better]] = tj[better]
                tri_hit[c[better]] = cc[j[better]]
        return t_hit, tri_hit

    # -- floors and ceilings: width between the walls that bound them
    def width(self, fi: int, plane_n: np.ndarray, obstacle_edges: list[int]) -> float:
        """Twice the largest distance from a point of planar face ``fi`` to the nearest obstacle edge.

        With the face bounded by obstacle edges all round this is the face's
        narrowest width (twice its largest inscribed circle); edges that are
        not obstacles are open sides a tool (or extrusion) can pass.
        """
        from scipy.ndimage import distance_transform_edt

        if not obstacle_edges:
            return math.inf
        u = np.cross(plane_n, [1.0, 0, 0] if abs(plane_n[0]) < 0.9 else [0, 1.0, 0])
        u /= np.linalg.norm(u)
        v = np.cross(plane_n, u)
        sel = self.tri_face == fi
        tris = self.V[sel]
        if len(tris) == 0:
            return math.inf
        uv = np.stack([tris @ u, tris @ v], axis=-1)  # (k, 3, 2)
        lo, hi = uv.reshape(-1, 2).min(0), uv.reshape(-1, 2).max(0)
        ext = float((hi - lo).max())
        px = max(ext / 256, 1e-6)
        lo = lo - 2 * px
        nx, ny = int(math.ceil((hi[0] - lo[0]) / px)) + 3, int(math.ceil((hi[1] - lo[1]) / px)) + 3
        cx = lo[0] + (np.arange(nx) + 0.5) * px
        cy = lo[1] + (np.arange(ny) + 0.5) * px
        inside = np.zeros((nx, ny), dtype=bool)
        for t in uv:
            (x0, y0), (x1, y1), (x2, y2) = t
            i0, i1 = np.searchsorted(cx, min(x0, x1, x2)), np.searchsorted(cx, max(x0, x1, x2))
            j0, j1 = np.searchsorted(cy, min(y0, y1, y2)), np.searchsorted(cy, max(y0, y1, y2))
            if i1 <= i0 or j1 <= j0:
                continue
            gx, gy = np.meshgrid(cx[i0:i1], cy[j0:j1], indexing="ij")
            d = (y1 - y2) * (x0 - x2) + (x2 - x1) * (y0 - y2)
            if abs(d) < 1e-300:
                continue
            a = ((y1 - y2) * (gx - x2) + (x2 - x1) * (gy - y2)) / d
            b = ((y2 - y0) * (gx - x2) + (x0 - x2) * (gy - y2)) / d
            inside[i0:i1, j0:j1] |= (a >= -1e-9) & (b >= -1e-9) & (a + b <= 1 + 1e-9)
        if not inside.any():
            return math.inf
        obstacle = np.zeros((nx, ny), dtype=bool)
        for ei in obstacle_edges:
            pl = self.edge_lines.get(ei)
            if pl is None or len(pl) < 2:
                continue
            q = np.stack([pl @ u, pl @ v], axis=-1)
            for a, b in zip(q[:-1], q[1:]):
                k = int(np.ceil(np.linalg.norm(b - a) / (0.5 * px))) + 1
                pts = a + (b - a) * np.linspace(0, 1, k)[:, None]
                ii = np.clip(((pts[:, 0] - lo[0]) / px).astype(np.int64), 0, nx - 1)
                jj = np.clip(((pts[:, 1] - lo[1]) / px).astype(np.int64), 0, ny - 1)
                obstacle[ii, jj] = True
        if not obstacle.any():
            return math.inf
        dist = distance_transform_edt(~obstacle) * px
        return float(2 * dist[inside].max() + px)  # + px: obstacle and sample pixels sit half a pixel off the lines


# ---------------------------------------------------------------- shared checks


def _thin_walls(p: _Part, min_wall: float) -> list[Finding]:
    """Ray-cast from each face into the material; pairs of faces closer than ``min_wall``."""
    samples = np.concatenate([p.tri_samples(fi) for fi in range(len(p.faces))])
    if len(samples) == 0:
        return []
    o = p.tri_c[samples] - p.tri_n[samples] * p.eps
    d = -p.tri_n[samples]
    t, tri = p.cast(o, d, min_wall * 1.0001)
    thin = np.isfinite(t)
    pairs: dict[tuple[int, int], float] = {}
    for s, tt, h in zip(samples[thin], t[thin], tri[thin]):
        a, b = int(p.tri_face[s]), int(p.tri_face[h])
        if a == b:
            continue
        # the opposite face must face back at us (a wall, not a grazing neighbour)
        if p.tri_n[s] @ p.tri_n[h] > -0.5:
            continue
        key = (min(a, b), max(a, b))
        pairs[key] = min(pairs.get(key, math.inf), float(tt) + p.eps)
    out = []
    for (a, b), th in sorted(pairs.items(), key=lambda kv: kv[1]):
        sev = "error" if th < min_wall / 2 else "warning"
        out.append(Finding(
            "thin_wall", sev, f"wall {th:.2f} < {min_wall:g}",
            f"material between f{a} and f{b} is {th:.2f} mm thick, below the minimum wall of {min_wall:g} mm; "
            f"thicken it to at least {min_wall:g} mm (move one face) or remove the wall",
            (a, b), (), p.tags((a, b)), {"thickness": _r(th), "min_wall": min_wall}))
    return out


def _sort(findings: list[Finding]) -> tuple[Finding, ...]:
    return tuple(sorted(findings, key=lambda f: (_RANK[f.severity], f.check, f.faces)))


# ---------------------------------------------------------------- CNC


def cnc(
    part: Shape,
    *,
    axis: VecLike | Sequence[VecLike] = (0, 0, -1),
    tool_radius: float = 3.0,
    min_wall: float = 1.0,
    max_depth_ratio: float = 4.0,
    tolerance: float = 0.05,
) -> Report:
    """Check a part for 3-axis milling with the tool moving along ``axis`` (``-Z``: from above).

    ``axis`` may be a list of directions, one per setup. ``tool_radius`` is
    the radius of the end mill that cuts internal corners and pockets (mm);
    ``min_wall`` the thinnest wall that can be cut without chatter (mm);
    ``max_depth_ratio`` the deepest pocket relative to its width.
    """
    axes = _axes(axis)
    if tool_radius <= 0:
        raise TuleError("tool_radius must be positive", op="dfm.cnc", hint="e.g. tool_radius=3 for a 6 mm end mill")
    p = _Part(part, tolerance)
    ups = [-a for a in axes]
    names = [_axis_name(a) for a in axes]
    findings: list[Finding] = []
    findings += _cnc_access(p, ups, names)
    findings += _cnc_corners(p, ups, tool_radius)
    findings += _cnc_pockets(p, ups, tool_radius, max_depth_ratio)
    findings += _thin_walls(p, min_wall)
    params = {"axis": names if len(names) > 1 else names[0], "tool_radius": tool_radius,
              "min_wall": min_wall, "max_depth_ratio": max_depth_ratio}
    return Report("cnc-3axis", params, _sort(findings))


def _axes(axis) -> list[Vec]:
    if isinstance(axis, Vec) or (isinstance(axis, (tuple, list)) and axis and isinstance(axis[0], (int, float))):
        return [_unit(axis, "axis")]
    axes = [_unit(a, "axis") for a in axis]
    if not axes:
        raise TuleError("give at least one setup axis", op="dfm.cnc")
    return axes


def _cnc_access(p: _Part, ups: list[Vec], names: list[str]) -> list[Finding]:
    n_tri = len(p.tri_c)
    p.reach_by = []
    reach = np.zeros(n_tri, dtype=bool)
    blocker = np.full(n_tri, -1, dtype=np.int64)
    away = np.ones(n_tri, dtype=bool)
    for up in ups:
        u = np.array(up)
        nd = p.tri_n @ u
        h = p.tri_c @ u
        base = float((p.V.reshape(-1, 3) @ u).min())
        stock = (nd < -0.9999) & (h - base < 1e-6 * p.size + 1e-9)  # the face the part sits on
        facing = nd >= -0.01
        cand = np.nonzero(facing & ~reach)[0]
        o = p.tri_c[cand] + p.tri_n[cand] * p.eps * 10 + u * p.eps
        t, tri = p.cast(o, np.broadcast_to(u, o.shape).copy(), np.inf)
        free = ~np.isfinite(t)
        reach[cand[free]] = True
        blocker[cand[~free]] = p.tri_face[tri[~free]]
        reach |= stock
        away &= ~facing & ~stock
        mine = stock.copy()
        mine[cand[free]] = True
        p.reach_by.append(mine)
    out = []
    for fi in range(len(p.faces)):
        sel = p.tri_face == fi
        area = p.face_area[fi]
        if area <= 0:
            continue
        bad = sel & ~reach
        a_bad = float(p.tri_area[bad].sum())
        if a_bad <= max(0.01 * area, 1e-6 * p.size**2):
            continue
        frac = a_bad / area
        where = " or ".join(names)
        a_away = float(p.tri_area[bad & away].sum())
        blocked = bad & ~away
        hidden = sorted({int(b) for b in blocker[blocked] if b >= 0})
        why = []
        if a_away > 0:
            why.append(f"it faces away from the tool along {where}")
        if hidden:
            others = ", ".join("itself" if b == fi else f"f{b}" for b in hidden[:4])
            why.append(f"it is hidden behind {others} along {where}")
        part_s = "" if frac > 0.99 else f"{frac:.0%} of "
        label = "undercut" if not hidden or a_away > 0 else "hidden from the tool"
        out.append(Finding(
            "undercut", "error", label,
            f"{part_s}f{fi} cannot be reached by a 3-axis tool: {' and '.join(why)}; add a setup from another side "
            f"(axis=[...]), open the region toward the tool, or redesign it as a feature visible along the tool axis",
            (fi,), (), p.tags((fi,)), {"area": _r(a_bad), "fraction": _r(frac)}))
    return out


def _cnc_corners(p: _Part, ups: list[Vec], tool_radius: float) -> list[Finding]:
    out = []

    def along_axis(d: Vec) -> bool:
        return any(abs(abs(d.dot(u)) - 1) < 1e-4 for u in ups)

    # sharp concave edges along the tool axis
    for ei, fs in p.edge_faces.items():
        if len(fs) != 2:
            continue
        d = edge_direction(p.edges[ei])
        if d is None or not along_axis(d):
            continue
        if p.concavity(ei) != "concave":
            continue
        # a floor corner in some setup is cut sharp by a flat end mill
        if any(abs(d.dot(u)) < 1e-4 and any(face_type(p.faces[g]) == "plane" and face_normal(p.faces[g]).dot(u) > 0.9999
                                           and p.reached(g, k) for g in fs) for k, u in enumerate(ups)):
            continue
        fs = tuple(sorted(fs))
        out.append(Finding(
            "corner_radius", "error", f"sharp internal corner < tool r{tool_radius:g}",
            f"sharp internal corner e{ei} between f{fs[0]} and f{fs[1]} runs along the tool axis; an end mill of "
            f"radius {tool_radius:g} leaves r{tool_radius:g} there. Round the edge with r ≥ {tool_radius:g} "
            f"(part.fillet(part.edge({ei}), {tool_radius * 1.1:.3g})) or plan a smaller tool",
            fs, (ei,), p.tags(fs), {"radius": 0.0, "tool_radius": tool_radius}))
    # concave cylinders along the tool axis
    groups: dict[tuple, list[tuple[int, float, float]]] = {}
    for fi, f in enumerate(p.faces):
        s = BRepAdaptor_Surface(f)
        if s.GetType() != GeomAbs_Cylinder:
            continue
        cyl = s.Cylinder()
        ax = cyl.Axis()
        dv = Vec(ax.Direction().X(), ax.Direction().Y(), ax.Direction().Z())
        if not along_axis(dv):
            continue
        loc = np.array([ax.Location().X(), ax.Location().Y(), ax.Location().Z()])
        r = cyl.Radius()
        c = np.array(face_center(f))
        n = np.array(face_normal(f))
        dd = np.array(dv)
        radial = (c - loc) - ((c - loc) @ dd) * dd
        sel = p.tri_face == fi
        if sel.any():  # the center of mass of a half cylinder is off the surface; use the mesh
            radial_t = (p.tri_c[sel] - loc) - np.outer((p.tri_c[sel] - loc) @ dd, dd)
            concave = float(np.einsum("ij,ij->i", radial_t, p.tri_n[sel]).sum()) < 0
        else:
            concave = float(n @ radial) < 0
        if not concave:
            continue
        span = s.LastUParameter() - s.FirstUParameter()
        # one key per axis line and radius, so a hole split in halves is still a hole
        foot = loc - (loc @ dd) * dd
        key = (tuple(np.round(foot, 4)), tuple(np.round(np.abs(dd), 4)), round(r, 6))
        groups.setdefault(key, []).append((fi, r, span))
    for key, items in groups.items():
        r = items[0][1]
        if r >= tool_radius - 1e-9:
            continue
        fs = tuple(sorted(i[0] for i in items))
        total = sum(i[2] for i in items)
        if total >= 2 * math.pi - 1e-3:
            out.append(Finding(
                "hole", "warning", f"hole r{r:g} < tool r{tool_radius:g}",
                f"hole {', '.join(f'f{i}' for i in fs)} (Ø{2 * r:g}) is smaller than the end mill (Ø{2 * tool_radius:g}); "
                f"drill it with a Ø{2 * r:g} drill, use a smaller end mill, or make the hole at least Ø{2 * tool_radius:g}",
                fs, (), p.tags(fs), {"radius": _r(r, 6), "tool_radius": tool_radius}))
        else:
            out.append(Finding(
                "corner_radius", "error", f"internal corner r{r:g} < tool r{tool_radius:g}",
                f"internal corner {', '.join(f'f{i}' for i in fs)} has radius {r:g}, smaller than the tool radius "
                f"{tool_radius:g}; make the corner radius at least {tool_radius:g} (about {tool_radius * 1.1:.3g} "
                f"cuts cleaner) or plan a smaller tool",
                fs, (), p.tags(fs), {"radius": _r(r, 6), "tool_radius": tool_radius}))
    return out


def _cnc_pockets(p: _Part, ups: list[Vec], tool_radius: float, max_ratio: float) -> list[Finding]:
    out = []
    for fi, f in enumerate(p.faces):
        if face_type(f) != "plane":
            continue
        n = np.array(face_normal(f))
        for k, up in enumerate(ups):
            u = np.array(up)
            if n @ u < 0.9999:
                continue
            if not p.reached(fi, k):
                continue  # not a floor this setup can reach (an undercut reports it)
            lvl = p.heights(fi, u)[1]
            top = float((p.V.reshape(-1, 3) @ u).max())
            if top - lvl < 1e-6 * p.size + 1e-9:
                break  # the top of the stock
            rising = p.walls(fi)
            if not rising:
                break
            walls = {g for ei in rising for g in p.edge_faces[ei] if g != fi}
            ring = set(walls)
            for g in walls:  # one more ring, so a fillet at the floor does not hide the wall above it
                for ei in p.face_edges[g]:
                    ring.update(h for h in p.edge_faces.get(ei, ()) if h != fi and p.heights(h, u)[0] >= lvl - 1e-6 * p.size)
            depth = max(p.heights(g, u)[1] for g in ring) - lvl
            width = p.width(fi, u, rising)
            if not math.isfinite(width) or depth <= 1e-9:
                break
            vals = {"depth": _r(depth), "width": _r(width), "tool_radius": tool_radius}
            if width < 2 * tool_radius - 1e-6:
                out.append(Finding(
                    "pocket_width", "error", f"pocket {width:.2f} wide < tool Ø{2 * tool_radius:g}",
                    f"the floor f{fi} is {width:.2f} mm wide between its walls, narrower than the end mill "
                    f"(Ø{2 * tool_radius:g}); widen it to at least {2 * tool_radius:g} mm or plan a smaller tool",
                    (fi,), tuple(rising), p.tags((fi,)), vals))
            ratio = depth / width
            if ratio > max_ratio:
                out.append(Finding(
                    "pocket_depth", "warning", f"pocket depth {ratio:.1f}× width",
                    f"the floor f{fi} is {depth:.2f} mm deep and {width:.2f} mm wide ({ratio:.1f}:1, limit "
                    f"{max_ratio:g}:1); long tools deflect and chatter. Make it shallower or wider, or reach it "
                    f"from another setup",
                    (fi,), tuple(rising), p.tags((fi,)), {**vals, "ratio": _r(ratio), "max_ratio": max_ratio}))
            break
    return out


# ---------------------------------------------------------------- FDM


def fdm(
    part: Shape,
    *,
    up: VecLike = (0, 0, 1),
    max_overhang: float = 45.0,
    min_wall: float = 0.8,
    max_bridge: float = 10.0,
    tolerance: float = 0.05,
) -> Report:
    """Check a part for FDM printing, built along ``up`` on a bed at its lowest level.

    ``max_overhang`` is the largest angle from vertical a downward-facing
    surface may lean without support (degrees); ``min_wall`` the thinnest
    printable wall (mm, about two nozzle widths); ``max_bridge`` the longest
    horizontal span between supports (mm).
    """
    if not 0 < max_overhang < 90:
        raise TuleError("max_overhang must be between 0 and 90 degrees", op="dfm.fdm", hint="45 is typical")
    u = np.array(_unit(up, "up"))
    p = _Part(part, tolerance)
    findings: list[Finding] = []
    findings += _fdm_overhangs(p, u, max_overhang)
    findings += _fdm_bridges(p, u, max_bridge)
    findings += _thin_walls(p, min_wall)
    findings += _fdm_bed(p, u)
    params = {"up": _axis_name(Vec(*u)), "max_overhang": max_overhang, "min_wall": min_wall, "max_bridge": max_bridge}
    return Report("fdm", params, _sort(findings))


def _bed(p: _Part, u: np.ndarray) -> tuple[float, np.ndarray]:
    h = p.tri_c @ u
    base = float((p.V.reshape(-1, 3) @ u).min())
    on_bed = (p.tri_n @ u < -0.9999) & (h - base < 1e-6 * p.size + 1e-9)
    return base, on_bed


def _fdm_overhangs(p: _Part, u: np.ndarray, limit: float) -> list[Finding]:
    nd = p.tri_n @ u
    angle = np.degrees(np.arcsin(np.clip(-nd, -1, 1)))  # from vertical, for downward faces
    base, on_bed = _bed(p, u)
    ceiling = nd < -math.cos(math.radians(0.5))  # horizontal ceilings: see bridges
    bad = (angle > limit + 1e-6) & ~on_bed & ~ceiling
    out = []
    for fi in range(len(p.faces)):
        sel = bad & (p.tri_face == fi)
        a = float(p.tri_area[sel].sum())
        if a <= max(0.01 * p.face_area[fi], 1e-6 * p.size**2):
            continue
        worst = float(angle[sel].max())
        out.append(Finding(
            "overhang", "warning", f"overhang {worst:.0f}° > {limit:g}°",
            f"f{fi} leans {worst:.0f}° from vertical (limit {limit:g}°) over {a:.1f} mm² and needs support; "
            f"make it steeper than {limit:g}° from vertical (chamfer instead of a flat ledge), or reorient the part",
            (fi,), (), p.tags((fi,)), {"angle": _r(worst, 1), "max_overhang": limit, "area": _r(a)}))
    return out


def _fdm_bridges(p: _Part, u: np.ndarray, max_bridge: float) -> list[Finding]:
    base, _ = _bed(p, u)
    out = []
    for fi, f in enumerate(p.faces):
        if face_type(f) != "plane" or np.array(face_normal(f)) @ u > -0.9999:
            continue
        lo, hi = p.heights(fi, u)
        if hi - base < 1e-6 * p.size + 1e-9:
            continue  # on the bed
        supports = p.walls(fi)
        span = p.width(fi, -u, supports)
        if not math.isfinite(span):
            out.append(Finding(
                "bridge", "warning", "unsupported ceiling",
                f"the horizontal ceiling f{fi} has no wall below it to bridge from and prints in mid-air; "
                f"add support, a chamfer or arch under it, or reorient the part",
                (fi,), (), p.tags((fi,)), {"height": _r(lo - base)}))
        elif span > max_bridge:
            out.append(Finding(
                "bridge", "warning", f"bridge {span:.1f} > {max_bridge:g}",
                f"the horizontal ceiling f{fi} spans {span:.1f} mm between supporting walls (limit {max_bridge:g} mm) "
                f"and will sag; add support, narrow the span, use an arched or chamfered (≤ 45°) ceiling, "
                f"or reorient the part",
                (fi,), tuple(supports), p.tags((fi,)), {"span": _r(span), "max_bridge": max_bridge}))
    return out


def _fdm_bed(p: _Part, u: np.ndarray) -> list[Finding]:
    base, on_bed = _bed(p, u)
    faces = tuple(sorted({int(x) for x in p.tri_face[on_bed]}))
    area = float(p.tri_area[on_bed].sum())
    # footprint: the part's shadow on the bed, from its bounding box in the bed plane
    a = np.cross(u, [1.0, 0, 0] if abs(u[0]) < 0.9 else [0, 1.0, 0])
    a /= np.linalg.norm(a)
    b = np.cross(u, a)
    P = p.V.reshape(-1, 3)
    footprint = float(np.ptp(P @ a) * np.ptp(P @ b))
    if area <= 1e-9:
        low = np.argsort(p.tri_c @ u)[:1]
        lowest = tuple(sorted({int(p.tri_face[i]) for i in low}))
        return [Finding(
            "bed_contact", "error", "no flat face on the bed",
            "the part touches the bed only at a point or line, so it will not stick; add a flat face at the bottom "
            "(cut it flat) or reorient the part onto a flat face",
            lowest, (), p.tags(lowest), {"area": 0.0, "footprint": _r(footprint)})]
    if area < 0.1 * footprint:
        return [Finding(
            "bed_contact", "warning", f"small bed contact {area:.0f} mm²",
            f"only {area:.1f} mm² ({area / footprint:.0%} of the footprint) touches the bed, so the part may detach "
            f"or tip; add a brim, a larger flat base, or reorient the part",
            faces, (), p.tags(faces), {"area": _r(area), "footprint": _r(footprint)})]
    return [Finding(
        "bed_contact", "info", f"bed contact {area:.0f} mm²",
        f"{area:.1f} mm² sits on the bed ({', '.join(f'f{i}' for i in faces)})",
        faces, (), p.tags(faces), {"area": _r(area), "footprint": _r(footprint)})]
