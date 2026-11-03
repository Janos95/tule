"""Finite-element analysis of a part: stress, deflection and safety factor under loads
(``static``; linear, or ``nonlinear=True`` for large displacements on tetrahedra), natural frequencies and mode shapes (``modes``), and buckling load factors (``buckling``).

```python
from tule import *

r = fea.static(part, material="PETG",
               fixed="mount",                               # a tag, a face selection, or a list
               loads=[fea.force("load", (0, 0, -30)),        # total force on the faces (N)
                      fea.moment("horn", (0, 0, 250))],      # a couple (N·mm): a servo's stall torque
               accel=(0, 0, -3 * fea.g))                     # self-weight under 3 g (mm/s²)
print(r)                  # peak and 99.5th-percentile stress, safety factor, deflection, reactions, mass
r.show("bracket-fea")     # an overlay: von Mises stress and displacement on the part, loads, supports

md = fea.modes(stage, "aluminium", fixed="base", n=6)          # md.frequencies (Hz), md.shapes
bk = fea.buckling(col, "steel", fixed="base", loads=[fea.force("top", (0, 0, -1e3))])  # bk.factor
```

Units: mm, N, MPa, g/cm³ (density), g (mass), mm/s² (acceleration; ``fea.g`` is 9810).

**Loads and supports.** ``force`` (on whole faces or a patch, ``at``/``r``), ``pressure``,
``moment``, ``bearing`` (a pin in a bore: p ∝ cos θ over the half it pushes, the total
exact). ``balanced=True`` analyses a free part: the loads must balance to 1 % (inertia relief
takes up the rest), three nodes hold it against rigid motion only (3-2-1, no reactions), and
the rigid motion that fits best is taken out of the displacements. ``result.warnings``: an
unconverged solve, a peak over 2× the 99.5th percentile (and whether it sits at a clamp, a
load or a corner), a load on under 2 elements across or pressing past yield, a load or clamp
on slivers of cut voxels, a large deflection. ``converge`` reruns at doubling budgets and
reports the spread. ``move`` drives faces by a displacement (``result.move_forces``).

**Geometric nonlinearity** (``static(nonlinear=True)``, tetrahedra): total Lagrangian
St. Venant–Kirchhoff (large rotations, small strains), dead loads applied in adaptive steps,
Newton–Raphson with the same CG solver and a reused preconditioner; a step that fails is cut
back, and a negative tangent stiffness (LOBPCG at the end) means buckling or snap-through: an error.

**Two meshes.** ``mesh="auto"`` measures the thinnest wall and narrowest gap
(``thinnest``: rays along each face's normals) and uses voxels unless a wall
is under 3 voxels or a gap under 2 at the DOF budget; then (and for
``mesh="tet"``, ``size=``, ``modes`` and ``buckling``) quadratic tetrahedra
from gmsh (``tule.tetfe``: 10-node elements, local sizes of half a thin
face's thickness, conjugate gradients with a P2→P1 two-level preconditioner,
fields on the mesh's own surface). Both return the same result API.

**Voxel mesh.** A voxel grid fitted to the part's bounding box: 8-node hexahedra,
all the same cube. A voxel the surface cuts is integrated over its inside
only (the finite-cell method): rays along z through each column give exact
lengths inside, a few columns per voxel in x and y (quadrature exact for
quadratics) give 27 moments, and every element integral is a quadratic in
each coordinate, so the cut voxel's stiffness is exact for the region the
rays see. Curved and slanted faces are not a staircase. The voxel size comes
from a DOF budget (``dofs``, default 300,000) or ``voxel=`` (mm).

**Element.** Trilinear hexahedra with Wilson's incompatible (bubble) modes,
condensed per element: plain trilinear bricks lock in bending (a cube in pure
bending comes out about 50 % too stiff); these are exact for it. Full voxels share one 24×24 matrix; cut ones have their own.
Conjugate gradients, preconditioned by ``tule.topopt``'s geometric multigrid
V-cycle (Galerkin coarse levels, Chebyshev smoothing), with matrix-free
products over the voxels that hold material; NumPy or CuPy (``device="auto"``).

**Stress.** Stresses are evaluated at each element's corners (where the
bubble modes make bending stress exact), averaged at the grid nodes weighted
by the voxels' solid fractions squared, and sampled on the part's surface
(trilinear in the voxel). The surface samples give the peak (``max_stress``,
where it is) and an area-weighted 99.5th percentile (``stress_p995``: 0.5 % of
the surface is above it). Sharp inside corners and the edges of clamped
faces are singular in linear elasticity, so the peak there grows as the
voxels shrink; the percentile is the robust number, the peak the
conservative one. ``safety_factor`` is yield over the peak.
"""

from __future__ import annotations

import functools
import math
import time
from dataclasses import dataclass
from typing import Any, Sequence

import numpy as np

from .errors import TuleError
from .geom import Vec, vec
from .selection import Faces, Selection
from .shape import Shape
from .topopt import CORNERS, _face_nodes, _host, _Level, _levels_for, _Multigrid, _prolong, _restrict, backend

g = 9810.0  # standard gravity, mm/s²

# ---------------------------------------------------------------- materials


@dataclass(frozen=True)
class Material:
    """An isotropic, linear-elastic material: ``E`` and ``yield_strength`` in MPa, ``density`` in g/cm³."""

    name: str
    E: float
    nu: float
    density: float
    yield_strength: float

    def __post_init__(self):
        if not (self.E > 0 and -1 < self.nu < 0.5 and self.density >= 0 and self.yield_strength > 0):
            raise TuleError(f"material {self.name!r}: E > 0, -1 < nu < 0.5, density >= 0, yield_strength > 0",
                            op="fea", hint='fea.Material("PC", E=2400, nu=0.37, density=1.2, yield_strength=60)')

    def __str__(self) -> str:
        return f"{self.name} (E {self.E:g} MPa, ν {self.nu:g}, {self.density:g} g/cm³, yield {self.yield_strength:g} MPa)"


# Typical values. Printed plastics are weaker across layers: keep a safety factor of 2 or more.
MATERIALS = {m.name: m for m in (
    Material("PLA", 3500, 0.36, 1.24, 50),
    Material("PETG", 2100, 0.38, 1.27, 50),
    Material("ABS", 2200, 0.35, 1.04, 40),
    Material("nylon PA12", 1700, 0.39, 1.01, 48),
    Material("aluminium 6061-T6", 68900, 0.33, 2.70, 276),
    Material("steel S235", 210000, 0.30, 7.85, 235),
    Material("stainless 304", 193000, 0.29, 8.00, 215),
)}
_ALIASES = {
    "nylon": "nylon PA12", "pa12": "nylon PA12", "pa": "nylon PA12",
    "aluminium": "aluminium 6061-T6", "aluminum": "aluminium 6061-T6", "al": "aluminium 6061-T6",
    "6061": "aluminium 6061-T6", "6061t6": "aluminium 6061-T6", "al6061": "aluminium 6061-T6",
    "aluminum6061t6": "aluminium 6061-T6",
    "steel": "steel S235", "s235": "steel S235", "mildsteel": "steel S235",
    "stainless": "stainless 304", "304": "stainless 304", "ss304": "stainless 304", "stainlesssteel": "stainless 304",
}


def _norm(s: str) -> str:
    return "".join(ch for ch in s.lower() if ch.isalnum())


def material(m: str | Material) -> Material:
    """A ``Material`` by name (``"PETG"``, ``"aluminium"``, ``"steel S235"``, …) or as given."""
    if isinstance(m, Material):
        return m
    if isinstance(m, str):
        key = _norm(m)
        for name, mat in MATERIALS.items():
            if _norm(name) == key:
                return mat
        if key in _ALIASES:
            return MATERIALS[_ALIASES[key]]
    raise TuleError(f"unknown material {m!r}", op="fea",
                    hint=f"one of {', '.join(MATERIALS)}; or fea.Material(\"name\", E=..., nu=..., density=..., yield_strength=...)")


_material = material  # static() takes a parameter of the same name


# ---------------------------------------------------------------- loads and supports


@dataclass(frozen=True)
class Load:
    """A load on faces: ``"force"`` (total, N; on a patch with ``at`` and ``r``), ``"pressure"``
    (MPa, pushing on the faces), ``"moment"`` (a couple, N·mm, spread over the faces as a rigid
    twist would) or ``"bearing"`` (total, N, a pin's cosine pressure on a bore)."""

    kind: str
    faces: Any  # a tag name, a face selection, or a list of them
    value: Any  # Vec for force, moment and bearing, float for pressure
    at: Vec | None = None  # a force on a patch: the faces' area within r of this point
    r: float = 0.0

    def label(self) -> str:
        if self.kind == "pressure":
            return f"{self.value:g} MPa"
        unit = "N·mm" if self.kind == "moment" else "N"
        v = self.value
        s = f"{v.length:.4g} {unit} ({v.x:.3g}, {v.y:.3g}, {v.z:.3g})"
        for name, d in (("X", (1, 0, 0)), ("Y", (0, 1, 0)), ("Z", (0, 0, 1))):
            if abs(abs(v.dot(d)) - v.length) < 1e-9 * max(v.length, 1):
                s = f"{v.length:.4g} {unit} {'+' if v.dot(d) > 0 else '-'}{name}"
        if self.kind == "bearing":
            s = f"bearing {s}"
        if self.at is not None:
            s += f" within {self.r:g} mm of {_pt(self.at)}"
        return s


def force(faces, f, at=None, r: float | None = None) -> Load:
    """A total force ``f`` (N, a vector) spread over ``faces`` by area. With ``at`` (a point) and
    ``r`` (mm), only on the faces' area within ``r`` of ``at``: a finger pad, a contact patch, a
    bolt head's footprint. The total is exactly ``f`` either way."""
    v = vec(f)
    if v.length == 0:
        raise TuleError("the force is zero", op="fea.force", hint="give the total force in N, e.g. (0, 0, -30)")
    if (at is None) != (r is None):
        raise TuleError("a patch needs both at= and r=", op="fea.force",
                        hint='fea.force("top", (0, 0, -20), at=(30, 10, 5), r=4): 20 N on the top within 4 mm of the point')
    if r is not None and not float(r) > 0:
        raise TuleError(f"r={r}: the patch's radius must be positive", op="fea.force")
    return Load("force", faces, v, vec(at) if at is not None else None, float(r or 0.0))


def pressure(faces, p: float) -> Load:
    """A pressure ``p`` (MPa = N/mm²) pushing on ``faces`` (negative pulls)."""
    return Load("pressure", faces, float(p))


def moment(faces, m) -> Load:
    """A couple ``m`` (N·mm, a vector along the axis, right-hand rule) on ``faces``: a torque with no net force."""
    v = vec(m)
    if v.length == 0:
        raise TuleError("the moment is zero", op="fea.moment", hint="e.g. (0, 0, 250) for 250 N·mm about +Z")
    return Load("moment", faces, v)


def bearing(faces, f) -> Load:
    """A pin or shaft pressing on a bore (or a bore on a pin: a boss's outside) with the total
    force ``f`` (N, across the axis): a cosine pressure over the half of the cylindrical ``faces``
    that ``f`` pushes on, peaking where it points, nothing on the other half. Bearing stress is
    ``|f| / (d · length)``. A force along the axis goes through a shoulder: ``fea.force`` there."""
    v = vec(f)
    if v.length == 0:
        raise TuleError("the force is zero", op="fea.bearing", hint="the pin's total force in N, e.g. (500, 0, 0)")
    return Load("bearing", faces, v)


@dataclass(frozen=True)
class Support:
    """Faces held in place, in the directions ``dirs`` (any of ``"xyz"``)."""

    faces: Any
    dirs: str = "xyz"


def fix(faces, dirs: str = "xyz") -> Support:
    """Hold ``faces`` in ``dirs``: ``fea.fix("base", "z")`` lets them slide in x and y."""
    d = "".join(sorted(set(dirs.lower())))
    if not d or set(d) - set("xyz"):
        raise TuleError(f"directions {dirs!r}", op="fea.fix", hint='any of "x", "y", "z": "xyz" (clamped), "z" (rests on a plane)')
    return Support(faces, d)


@dataclass(frozen=True)
class Move:
    """Faces moved by ``d`` (mm) in the directions ``dirs``: a prescribed displacement."""

    faces: Any
    d: Vec
    dirs: str = "xyz"

    def label(self) -> str:
        return f"moved {_vec(self.d)} mm" + ("" if self.dirs == "xyz" else f" in {self.dirs}")


def move(faces, d, dirs: str = "xyz") -> Move:
    """Move ``faces`` by ``d`` (mm, a vector) and hold them there: a prescribed displacement, such
    as an actuator driving a flexure's stage. ``dirs`` are the directions held (default all:
    the faces ride on a rigid carriage); ``fea.move("top", (2, 0, 0), "x")`` pushes them 2 mm
    along x and leaves them free in y and z. Give it in ``fixed=`` (or ``loads=``); the force it
    takes is ``result.move_forces``. On tetrahedra (picked for it)."""
    v = vec(d)
    dd = "".join(sorted(set(dirs.lower())))
    if not dd or set(dd) - set("xyz"):
        raise TuleError(f"directions {dirs!r}", op="fea.move", hint='any of "x", "y", "z": "xyz" (all), "x" (free in y and z)')
    if all(abs(v[i]) == 0 for i in range(3) if "xyz"[i] in dd):
        raise TuleError("the move is zero in the directions it holds", op="fea.move",
                        hint='a zero move is fea.fix(faces, dirs); e.g. fea.move("top", (2, 0, 0), "x")')
    return Move(faces, v, dd)


def _faces(part: Shape, spec, what: str) -> Faces:
    if isinstance(spec, str):
        try:
            sel = part.faces[spec]
        except Exception:
            raise TuleError(f"no faces tagged {spec!r} for {what}", op="fea",
                            hint=f"tags on this part: {', '.join(part.tags) or 'none'}; part = part.tag({spec!r}, part.faces...)") from None
    elif isinstance(spec, Selection):
        if spec.kind != "face":
            raise TuleError(f"{what} must be faces, got {spec.kind}s", op="fea")
        sel = part._resolve(spec, "face")
    elif isinstance(spec, int):
        sel = part.face(spec)
    elif isinstance(spec, (list, tuple)) and spec:
        sel = _faces(part, spec[0], what)
        for s in spec[1:]:
            sel = sel | _faces(part, s, what)
    else:
        raise TuleError(f"cannot find the faces for {what} in {spec!r}", op="fea",
                        hint='a tag name ("mount"), a face selection (part.faces["mount"]) or a list of them')
    if not sel:
        raise TuleError(f"the faces for {what} are empty", op="fea", hint="check the tag or the selection")
    return sel


# ---------------------------------------------------------------- loads on surface samples, free parts, checks


def _load_vectors(ld: Load, pts: np.ndarray, w: np.ndarray, nrm: np.ndarray, faces=None):
    """A load as forces at surface samples (points, area weights, unit outward normals): ``(k, 3)``
    vectors, the loaded area (mm²) and the area that bears it (for a pin: projected, d × length)."""
    if ld.at is not None:
        d = np.linalg.norm(pts - np.array(ld.at), axis=1)
        if not (d <= ld.r).any():
            raise TuleError(f"the patch within {ld.r:g} mm of {_pt(ld.at)} catches none of the loaded faces "
                            f"(the nearest point on them is {d.min():.3g} mm away)", op="fea.force", faces=faces,
                            hint="put at= on the faces' surface, or a larger r")
        w = w * (d <= ld.r)
    if not w.sum() > 0:
        raise TuleError(f"the {ld.kind}'s faces have no area", op="fea", faces=faces)
    if ld.kind == "force":
        vecs = np.outer(w / w.sum(), np.array(ld.value))
    elif ld.kind == "pressure":
        vecs = -ld.value * w[:, None] * nrm
    elif ld.kind == "moment":
        c = (pts * w[:, None]).sum(0) / w.sum()
        d = pts - c
        J = (w * (d * d).sum(1)).sum() * np.eye(3) - (d * w[:, None]).T @ d
        omega = np.linalg.lstsq(J, np.array(ld.value), rcond=None)[0]
        vecs = w[:, None] * np.cross(omega, d)
    else:
        vecs, projected = _bearing_vectors(ld, w, nrm, faces)
        return vecs, float(w[np.linalg.norm(vecs, axis=1) > 0].sum()), projected
    area = float(w.sum())
    return vecs, area, area


def _bearing_vectors(ld: Load, w, nrm, faces):
    """A pin's pressure on a bore: p ∝ cos θ from the force's line over the half it pushes on, then
    scaled (and the rounding of the facets corrected, on the same half) so the total is exactly f."""
    f = np.array(ld.value)
    lam, V = np.linalg.eigh(np.einsum("k,ki,kj->ij", w, nrm, nrm))  # normals of a cylinder span a plane
    if not (lam[0] <= 0.05 * lam[2] and lam[1] >= 0.1 * lam[2]):
        raise TuleError("fea.bearing needs a bore or a pin: cylindrical faces", op="fea.bearing", faces=faces,
                        hint="part.faces.cylindrical(r=...) picks a bore; a flat face takes fea.force or fea.pressure")
    axis = V[:, 0]
    fa = float(f @ axis)
    if abs(fa) > 1e-3 * np.linalg.norm(f):
        raise TuleError(f"the bearing force has {abs(fa):.4g} N along the bore's axis {_vec(axis)}", op="fea.bearing",
                        faces=faces, hint="a pin bears across its axis; its thrust goes through a shoulder or a face: "
                                          "fea.bearing(bore, the radial part) and fea.force(shoulder, the axial part)")
    fr = f - fa * axis
    fh = fr / np.linalg.norm(fr)
    q = w * np.maximum(0.0, -(nrm @ fh))  # pressure shape × area: the pin bears where the surface faces it
    if not q.sum() > 0.02 * w.sum():  # (facets at ±90° bear a hair)
        raise TuleError("no part of these faces is pushed by the force", op="fea.bearing", faces=faces,
                        hint="a pin moving along f bears on the side of the bore that f points to: give all the bore's faces")
    t = -q[:, None] * nrm
    R = t.sum(0)
    alpha = float(f @ R) / float(R @ R)
    return alpha * t + np.outer(q / q.sum(), f - alpha * R), float(q.sum())


def _relieve(X: np.ndarray, f: np.ndarray, wts: np.ndarray, size: float, mass: float):
    """``balanced=True``: checks that the nodal loads ``f`` (n, 3) at ``X`` balance (to 1 %), then
    cancels the rest (rounding) by a rigid-body acceleration of the mass ``wts`` (inertia relief).
    Returns the loads and the net force and moment that were removed."""
    c = (wts[:, None] * X).sum(0) / wts.sum()
    d = X - c
    R, M = f.sum(0), np.cross(d, f).sum(0)
    total = float(np.linalg.norm(f, axis=1).sum())
    if np.linalg.norm(R) > 0.01 * total or np.linalg.norm(M) > 0.01 * total * size:
        hint = "add the missing load, or hold the part with fixed= instead of balanced=True"
        if np.linalg.norm(M) <= 0.01 * total * size and mass > 0:
            hint += f"; a free body that accelerates under them: accel={_vec(-R * 1e6 / mass)} (mm/s²) balances the force"
        raise TuleError(f"balanced=True, but the loads do not balance: net force {_vec(R)} N, net moment {_vec(M)} N·mm "
                        f"about the centre of mass {_pt(c)}", op="fea", hint=hint)
    E = np.eye(3)
    basis = [wts[:, None] * E[k] for k in range(3)] + [wts[:, None] * np.cross(E[k], d) for k in range(3)]
    A = np.stack([np.concatenate([g.sum(0), np.cross(d, g).sum(0)]) for g in basis], 1)
    coef = np.linalg.solve(A, -np.concatenate([R, M]))
    return f + sum(k * g for k, g in zip(coef, basis)), R, M


def _three_two_one(X: np.ndarray) -> list[tuple[int, str]]:
    """Minimal supports of a free part among points ``X``: one held in x, y, z, a far one in two
    directions, a third off their line in one (the best conditioned choice): no rigid motion, no
    stress (the loads balance)."""
    c = X.mean(0)
    A = int(np.argmax(np.linalg.norm(X - c, axis=1)))
    B = int(np.argmax(np.linalg.norm(X - X[A], axis=1)))
    d = X[B] - X[A]
    C = int(np.argmax(np.linalg.norm(np.cross(X - X[A], d / max(np.linalg.norm(d), 1e-300)), axis=1)))
    e = X[C] - X[A]
    I3 = np.eye(3)
    best = None
    for drop in range(3):
        jk = [j for j in range(3) if j != drop]
        for k in range(3):
            rows = np.stack([np.cross(d, I3[jk[0]]), np.cross(d, I3[jk[1]]), np.cross(e, I3[k])])
            s = np.linalg.svd(rows, compute_uv=False)[-1]
            if best is None or s > best[0]:
                best = (s, jk, k)
    _, jk, k = best
    return [(A, "xyz"), (B, "".join("xyz"[j] for j in jk)), (C, "xyz"[k])]


def _rigid(X: np.ndarray, u: np.ndarray, wts: np.ndarray) -> np.ndarray:
    """The rigid motion (n, 3) that fits displacements ``u`` at ``X`` best (weights ``wts``)."""
    c = (wts[:, None] * X).sum(0) / wts.sum()
    d = X - c
    t = (wts[:, None] * u).sum(0) / wts.sum()
    J = (wts * (d * d).sum(1)).sum() * np.eye(3) - (d * wts[:, None]).T @ d
    omega = np.linalg.solve(J, (wts[:, None] * np.cross(d, u - t)).sum(0))
    return t + np.cross(omega, d)


SINGULAR = 2.0  # a peak over this × the 99.5th percentile is a singularity, not the design's stress


def _peak_warning(peak: float, at, p995: float, where: str | None, yield_strength: float) -> list[str]:
    if not (p995 > 0 and peak > SINGULAR * p995):
        return []
    what = {"fixed": "at the edge of a fixed face (a clamp's singularity)",
            "load": "at a load (a point load's singularity: spread it)"}.get(where, "likely at a sharp inside corner or edge (a fillet there)")
    return [f"the peak {peak:.3g} MPa at {_pt(at)} is {peak / p995:.1f}× the 99.5th percentile, {what}: it grows as the "
            f"mesh shrinks; judge by the percentile (safety factor {yield_strength / p995:.3g} on it)"]


def _load_warnings(ld: Load, area: float, bearing_area: float, h: float, unit: str, yield_strength: float) -> list[str]:
    out = []
    if area < 4 * h * h:
        out.append(f"the {ld.label()} acts on {area:.2g} mm², under 2 {unit} across: a point load, its local stress "
                   f"is not the part's (spread it, or refine)")
    p = ld.value if ld.kind == "pressure" else ld.value.length / bearing_area if ld.kind in ("force", "bearing") else 0.0
    if abs(p) > yield_strength:
        out.append(f"the {ld.label()} presses {abs(p):.3g} MPa on {bearing_area:.3g} mm², past yield "
                   f"({yield_strength:g} MPa): it would dent or crush there")
    return out


def _common_warnings(residual: float, tol: float, imbalance: float, on_fixed: float) -> list[str]:
    out = []
    if residual > max(100 * tol, 1e-5):
        out.append(f"the solver stopped at a residual of {residual:.1e}: the numbers may be off (run again with tol={tol / 100:g})")
    if imbalance > 1e-3:
        out.append(f"the reactions balance the loads only to {100 * imbalance:.2g} %: a support or load is not where it should be")
    if on_fixed > 0.02:
        out.append(f"{100 * on_fixed:.0f} % of the load acts on fixed nodes and goes straight into the supports")
    return out


def _near(p, fixed_pts: list, load_pts: list, dist: float) -> str | None:
    """Whether a point is within ``dist`` of a fixed face's or a load's samples: "fixed", "load" or None."""
    p = np.asarray(p, dtype=np.float64)
    for kind, groups in (("load", load_pts), ("fixed", fixed_pts)):
        for q in groups:
            if len(q) and float(np.min(np.linalg.norm(np.asarray(q) - p, axis=1))) <= dist:
                return kind
    return None


def _say_warnings(warnings) -> None:
    for w in warnings:
        print(f"fea: warning: {w}", flush=True)


# ---------------------------------------------------------------- the element


def _D(nu: float) -> np.ndarray:
    lam = nu / ((1 + nu) * (1 - 2 * nu))
    mu = 1 / (2 * (1 + nu))
    D = np.zeros((6, 6))
    D[:3, :3] = lam
    D[:3, :3] += 2 * mu * np.eye(3)
    D[3:, 3:] = mu * np.eye(3)
    return D


def _B(t: Sequence[float]) -> tuple[np.ndarray, np.ndarray]:
    """Strain-displacement matrices at ``t`` in the unit cube: ``(6, 24)`` for the corners (column
    3a + c: corner a, component c) and ``(6, 9)`` for the incompatible modes ``4 t_d (1 - t_d)``
    (column 3c + d: component c, direction d). Strains: xx, yy, zz, xy, yz, zx (engineering shear)."""

    def put(B, col, c, grad):
        x, y, z = grad
        B[c, col] = grad[c]
        if c == 0:
            B[3, col], B[5, col] = y, z
        elif c == 1:
            B[3, col], B[4, col] = x, z
        else:
            B[4, col], B[5, col] = y, x

    Bu = np.zeros((6, 24))
    for a, corner in enumerate(CORNERS):
        f = [t[d] if corner[d] else 1 - t[d] for d in range(3)]
        s = [1.0 if corner[d] else -1.0 for d in range(3)]
        grad = (s[0] * f[1] * f[2], f[0] * s[1] * f[2], f[0] * f[1] * s[2])
        for c in range(3):
            put(Bu, 3 * a + c, c, grad)
    Ba = np.zeros((6, 9))
    for c in range(3):
        for d in range(3):
            grad = [0.0, 0.0, 0.0]
            grad[d] = 4 - 8 * t[d]
            put(Ba, 3 * c + d, c, grad)
    return Bu, Ba


# Every integrand of the element (Bᵀ D B, B) is a polynomial of degree ≤ 2 in each coordinate, so it is
# exactly its interpolant on the 3×3×3 points t ∈ {0, ½, 1}³, and its integral over any part of the
# cube is a weighted sum of the values there, the weights being the "moments" ∫ L_a(x) L_b(y) L_c(z)
# of the quadratic Lagrange polynomials over that part (27 numbers per voxel, index 9a + 3b + c).
_T3 = (0.0, 0.5, 1.0)


def _lagrange(t) -> np.ndarray:
    t = np.asarray(t, dtype=np.float64)
    return np.stack([2 * (t - 0.5) * (t - 1), -4 * t * (t - 1), 2 * t * (t - 0.5)], -1)


def _lagrange_integral(a, b) -> np.ndarray:
    """∫_a^b L_k(t) dt, k = 0, 1, 2."""
    def F(t):
        t = np.asarray(t, dtype=np.float64)
        return np.stack([2 * t ** 3 / 3 - 1.5 * t ** 2 + t, -4 * t ** 3 / 3 + 2 * t ** 2, 2 * t ** 3 / 3 - 0.5 * t ** 2], -1)
    return F(b) - F(a)


FULL = np.einsum("a,b,c->abc", *[_lagrange_integral(0.0, 1.0)] * 3).reshape(27)  # the whole cube's moments


@functools.lru_cache(maxsize=8)
def _tables(nu: float):
    D = _D(nu)
    tabs = {k: [] for k in ("uu", "ua", "aa", "bu", "ba")}
    for x in _T3:
        for y in _T3:
            for z in _T3:
                Bu, Ba = _B((x, y, z))
                tabs["uu"].append(Bu.T @ D @ Bu)
                tabs["ua"].append(Bu.T @ D @ Ba)
                tabs["aa"].append(Ba.T @ D @ Ba)
                tabs["bu"].append(Bu)
                tabs["ba"].append(Ba)
    return D, {k: np.array(v).reshape(27, -1) for k, v in tabs.items()}


def cells(m: np.ndarray, nu: float, corners: bool = False):
    """Stiffness of voxels (E = 1, unit edge) from their moments ``m`` ``(n, 27)``: ``(n, 24, 24)``,
    and with ``corners`` the stress operators at the 8 corners, ``(n, 8, 6, 24)``.

    Trilinear hexahedra with Wilson's 9 incompatible modes ``4 t_d (1 - t_d)``, condensed; in a
    partly filled voxel the modes' strains are shifted to mean zero over the material (Taylor's
    correction), so constant strain is still exact."""
    D, Q = _tables(nu)
    m = np.atleast_2d(np.asarray(m, dtype=np.float64))
    n = len(m)
    V = np.maximum(m.sum(1), 1e-12)[:, None, None]
    Kuu = (m @ Q["uu"]).reshape(n, 24, 24)
    Kua = (m @ Q["ua"]).reshape(n, 24, 9)
    Kaa = (m @ Q["aa"]).reshape(n, 9, 9)
    Iu = (m @ Q["bu"]).reshape(n, 6, 24)
    Abar = (m @ Q["ba"]).reshape(n, 6, 9) / V
    DA = D @ Abar
    Kua = Kua - np.swapaxes(Iu, 1, 2) @ DA
    Kaa = Kaa - V * (np.swapaxes(Abar, 1, 2) @ DA) + 1e-9 * np.eye(9)
    G = np.linalg.solve(Kaa, np.swapaxes(Kua, 1, 2))  # bubble amplitudes: α = -G u
    K = Kuu - Kua @ G
    K = 0.5 * (K + np.swapaxes(K, 1, 2))
    if not corners:
        return K
    S = np.empty((n, 8, 6, 24))
    for a, c in enumerate(CORNERS):
        Bu, Ba = _B(c)
        S[:, a] = D @ (Bu - (Ba - Abar) @ G)
    return K, S


@functools.lru_cache(maxsize=8)
def element(nu: float) -> tuple[np.ndarray, np.ndarray]:
    """The full unit cube (E = 1): the 24×24 stiffness and the corner stress operators ``(8, 6, 24)``
    (for a cube of edge h: K·E·h, stress = E/h · S · u_e)."""
    K, S = cells(FULL[None], nu, corners=True)
    return K[0], S[0]


def _columns(sub: int) -> tuple[np.ndarray, np.ndarray]:
    """``sub`` points across a voxel and their weights, exact for quadratics over the whole voxel:
    Gauss points for 2, else the sub-interval midpoints with weights nearest to equal."""
    if sub < 2:
        raise TuleError("sub must be 2 or more", op="fea")
    if sub == 2:
        return np.array([0.5 - 0.5 / math.sqrt(3), 0.5 + 0.5 / math.sqrt(3)]), np.array([0.5, 0.5])
    t = (np.arange(sub) + 0.5) / sub
    A = np.stack([t ** 0, t, t ** 2])
    w0 = np.full(sub, 1.0 / sub)
    w = w0 + A.T @ np.linalg.solve(A @ A.T, np.array([1.0, 0.5, 1 / 3]) - A @ w0)
    return t, w


def moments(shape: Shape, origin, h: float, dims: Sequence[int], sub: int = 3) -> np.ndarray:
    """Each voxel's moments ``(nx, ny, nz, 27)`` of the material inside it.

    Rays along z through ``sub × sub`` points per voxel column cross the B-rep
    (``IntCurvesFace_ShapeIntersector``); the crossings' in/out transitions give
    the inside stretches (a ray that grazes a face or an edge has its stretches
    classified by ``BRepClass3d_SolidClassifier`` instead), which are integrated
    exactly along z: faces across z are exact, the others resolved to about
    ``h / (2 sub)``. The solid fraction is the sum of the moments.
    """
    from OCP.BRepClass3d import BRepClass3d_SolidClassifier
    from OCP.gp import gp_Dir, gp_Lin, gp_Pnt
    from OCP.IntCurvesFace import IntCurvesFace_ShapeIntersector
    from OCP.IntCurveSurface import IntCurveSurface_TransitionOnCurve as Tr
    from OCP.TopAbs import TopAbs_IN

    IN, OUT = Tr.IntCurveSurface_In, Tr.IntCurveSurface_Out
    o = np.asarray(origin, dtype=np.float64)
    nx, ny, nz = dims
    m = np.zeros((nx, ny, nz, 3, 3, 3))
    lo, hi = shape.bounds
    si = IntCurvesFace_ShapeIntersector()
    si.Load(shape.wrapped, 1e-7)
    cl = BRepClass3d_SolidClassifier(shape.wrapped)
    z0, z1 = min(lo.z, o[2]) - 1.0, max(hi.z, o[2] + nz * h) + 1.0
    tol = 1e-9 * max(1.0, z1 - z0)
    t, wt = _columns(sub)
    Lt = _lagrange(t) * wt[:, None]  # per axis: the column's weight times the polynomials there
    t = t + 7.3e-4  # rays a hair off the sample points: never along a face at a round coordinate

    def inside(x, y):
        """The ray's inside stretches [(a, b)]."""
        si.Perform(gp_Lin(gp_Pnt(x, y, z0), gp_Dir(0, 0, 1)), 0.0, z1 - z0)
        hits = sorted(((z0 + si.WParameter(n), si.Transition(n)) for n in range(1, si.NbPnt() + 1)), key=lambda e: e[0])
        events = []  # crossings merged where several faces meet (an edge): one transition each, or None
        for w, tr in hits:
            if events and w - events[-1][0] <= tol:
                if events[-1][1] != tr:
                    events[-1] = (events[-1][0], None)
                continue
            events.append((w, tr))
        kinds = [tr for _, tr in events]
        if all(k == (IN if n % 2 == 0 else OUT) for n, k in enumerate(kinds)) and len(kinds) % 2 == 0:
            return [(events[n][0], events[n + 1][0]) for n in range(0, len(events), 2)]
        bounds = [z0] + [w for w, _ in events] + [z1]  # ambiguous: classify each stretch
        out = []
        for a, b in zip(bounds[:-1], bounds[1:]):
            if b - a > tol:
                cl.Perform(gp_Pnt(x, y, 0.5 * (a + b)), 1e-7)
                if cl.State() == TopAbs_IN:
                    out.append((a, b))
        return out

    segs = []  # (column i, j, ray p, q, z from, z to)
    for i in range(nx):
        for p in range(sub):
            x = o[0] + (i + t[p]) * h
            if x < lo.x or x > hi.x:
                continue
            for j in range(ny):
                for q in range(sub):
                    y = o[1] + (j + t[q]) * h
                    if y < lo.y or y > hi.y:
                        continue
                    for a, b in inside(x, y):
                        segs.append((i, j, p, q, a, b))
    m = m.reshape(-1, 27)
    if not segs:
        return m.reshape(nx, ny, nz, 27)
    s = np.array(segs)
    ta, tb = (s[:, 4] - o[2]) / h, (s[:, 5] - o[2]) / h
    k0 = np.maximum(0, np.floor(ta)).astype(np.int64)
    k1 = np.minimum(nz - 1, np.ceil(tb) - 1).astype(np.int64)
    n = np.maximum(k1 - k0 + 1, 0)
    seg = np.repeat(np.arange(len(s)), n)  # one row per (stretch, voxel it passes)
    k = k0[seg] + np.arange(len(seg)) - np.repeat(np.cumsum(n) - n, n)
    Lz = _lagrange_integral(np.clip(ta[seg] - k, 0, 1), np.clip(tb[seg] - k, 0, 1))
    ii, jj = s[seg, 0].astype(np.int64), s[seg, 1].astype(np.int64)
    Lx, Ly = Lt[s[seg, 2].astype(np.int64)], Lt[s[seg, 3].astype(np.int64)]
    vals = (Lx[:, :, None, None] * Ly[:, None, :, None] * Lz[:, None, None, :]).reshape(-1, 27)
    cell = (ii * ny + jj) * nz + k
    for c in range(27):
        m[:, c] = np.bincount(cell, weights=vals[:, c], minlength=len(m))
    return m.reshape(nx, ny, nz, 27)


def von_mises(s: np.ndarray) -> np.ndarray:
    """Von Mises stress from components ``(6, ...)``: xx, yy, zz, xy, yz, zx."""
    xx, yy, zz, xy, yz, zx = s
    return np.sqrt(np.maximum(0.5 * ((xx - yy) ** 2 + (yy - zz) ** 2 + (zz - xx) ** 2) + 3 * (xy * xy + yz * yz + zx * zx), 0))


# ---------------------------------------------------------------- grid helpers


def _sample(field: np.ndarray, origin, h: float, points: np.ndarray) -> np.ndarray:
    """Trilinear interpolation of a node field ``(Nx, Ny, Nz[, k])`` at ``points`` (clamped to the grid)."""
    N = np.array(field.shape[:3])
    q = (np.asarray(points, dtype=np.float64) - np.asarray(origin, dtype=np.float64)) / h
    q = np.clip(q, 0, N - 1)
    b = np.minimum(np.floor(q).astype(np.int64), np.maximum(N - 2, 0))
    t = q - b
    out = 0
    for c in CORNERS:
        w = np.prod([t[:, d] if c[d] else 1 - t[:, d] for d in range(3)], axis=0)
        idx = np.minimum(b + np.array(c), N - 1)
        v = field[idx[:, 0], idx[:, 1], idx[:, 2]]
        out = out + (w[:, None] * v if v.ndim == 2 else w * v)
    return out


def _fill(field: np.ndarray, known: np.ndarray) -> np.ndarray:
    """Every node outside ``known`` takes the value of the nearest known node."""
    from scipy.ndimage import distance_transform_edt

    if known.all() or not known.any():
        return field
    idx = distance_transform_edt(~known, return_distances=False, return_indices=True)
    return field[idx[0], idx[1], idx[2]]


def _surface(mesh, spacing: float, face_ids=None):
    """Area samples on a tessellation: points, areas, outward normals, face ids, from triangles split to ``spacing``."""
    P = mesh.positions.astype(np.float64)
    T = mesh.indices.astype(np.int64)
    fid = mesh.face_ids.astype(np.int64)
    if face_ids is not None:
        keep = np.isin(fid, list(face_ids))
        T, fid = T[keep], fid[keep]
    a, b, c = P[T[:, 0]], P[T[:, 1]], P[T[:, 2]]
    cr = np.cross(b - a, c - a)
    area = 0.5 * np.linalg.norm(cr, axis=1)
    nrm = cr / np.maximum(2 * area, 1e-300)[:, None]
    vn = mesh.normals.astype(np.float64)
    flip = np.einsum("ij,ij->i", nrm, vn[T[:, 0]] + vn[T[:, 1]] + vn[T[:, 2]]) < 0
    nrm[flip] *= -1
    # each triangle in rows from the corner opposite its shortest edge (a), k rows to the far edge
    # (b-c), each row split into pieces about `spacing` long: a long sliver (a cylinder's side) gets
    # samples along its length without (length / spacing)² of them
    E = np.stack([np.linalg.norm(c - b, axis=1), np.linalg.norm(a - c, axis=1), np.linalg.norm(b - a, axis=1)])
    apex = E.argmin(0)  # the corner opposite the shortest edge
    a, b, c = (np.where((apex == 1)[:, None], y, np.where((apex == 2)[:, None], z, x))
               for x, y, z in ((a, b, c), (b, c, a), (c, a, b)))
    k = np.maximum(1, np.ceil(np.maximum(np.linalg.norm(b - a, axis=1), np.linalg.norm(c - a, axis=1)) / spacing)).astype(np.int64)
    j = np.maximum(1, np.ceil(np.linalg.norm(c - b, axis=1) / spacing)).astype(np.int64)
    pts, ws, ns, fs = [], [], [], []
    for kk, jj in np.unique(np.stack([k, j], 1), axis=0):
        sel = np.nonzero((k == kk) & (j == jj))[0]
        r = np.arange(kk)
        t = (2 / 3) * ((r + 1) ** 3 - r ** 3) / ((r + 1) ** 2 - r ** 2) / kk  # each row's centroid
        n = np.maximum(1, np.ceil((r + 0.5) / kk * jj)).astype(np.int64)  # pieces in the row
        row = np.repeat(r, n)
        s = (np.arange(n.sum()) - np.repeat(np.cumsum(n) - n, n) + 0.5) / np.repeat(n, n)
        u, v = t[row] * (1 - s), t[row] * s
        frac = (((row + 1) ** 2 - row ** 2) / kk ** 2) / n[row]  # the row's share of the area, per piece
        A, B, C = a[sel], b[sel], c[sel]
        p = A[:, None] + u[None, :, None] * (B - A)[:, None] + v[None, :, None] * (C - A)[:, None]
        m = len(u)
        pts.append(p.reshape(-1, 3))
        ws.append((area[sel][:, None] * frac[None]).reshape(-1))
        ns.append(np.repeat(nrm[sel], m, axis=0))
        fs.append(np.repeat(fid[sel], m))
    if not pts:
        z = np.zeros((0, 3))
        return z, np.zeros(0), z, np.zeros(0, np.int64)
    return np.concatenate(pts), np.concatenate(ws), np.concatenate(ns), np.concatenate(fs)


class _Spread:
    """Puts point forces on the grid's active nodes: trilinear weights over the active corners of
    the point's voxel (renormalized), else the nearest active node."""

    def __init__(self, origin, h, node_active):
        from scipy.ndimage import distance_transform_edt

        self.origin, self.h, self.active = np.asarray(origin, dtype=np.float64), h, node_active
        self.N = np.array(node_active.shape)
        self.near = distance_transform_edt(~node_active, return_distances=False, return_indices=True)

    def __call__(self, points, vectors, out):
        if not len(points):
            return out
        q = np.clip((points - self.origin) / self.h, 0, self.N - 1)
        b = np.minimum(np.floor(q).astype(np.int64), self.N - 2)
        t = q - b
        idxs, ws = [], []
        for c in CORNERS:
            idx = b + np.array(c)
            w = np.prod([t[:, d] if c[d] else 1 - t[:, d] for d in range(3)], axis=0)
            w = w * self.active[idx[:, 0], idx[:, 1], idx[:, 2]]
            idxs.append(idx)
            ws.append(w)
        W = np.sum(ws, axis=0)
        ok = W > 1e-12
        flat = out.reshape(-1, 3)
        lin = lambda idx: np.ravel_multi_index((idx[:, 0], idx[:, 1], idx[:, 2]), tuple(self.N))  # noqa: E731
        for idx, w in zip(idxs, ws):
            r = np.where(ok, w / np.where(ok, W, 1), 0)
            np.add.at(flat, lin(idx), vectors * r[:, None])
        if (~ok).any():
            r = np.rint(q[~ok]).astype(np.int64)
            nn = self.near[:, r[:, 0], r[:, 1], r[:, 2]].T
            np.add.at(flat, lin(nn), vectors[~ok])
        return out


# ---------------------------------------------------------------- the result


@dataclass(frozen=True, repr=False)
class Result:
    """A static analysis: fields on the voxel grid's nodes and the numbers that matter.

    ``stress`` (von Mises, MPa) and ``displacement`` (mm, ``(..., 3)``) are node fields of the grid
    at ``origin`` with spacing ``voxel``; ``stress_at(points)`` and ``displacement_at(points)``
    sample them. ``max_stress`` is the peak on the surface, at ``max_stress_at``;
    ``stress_p995`` the area-weighted 99.5th percentile there.
    """

    part: Shape
    material: Material
    supports: tuple  # (Faces, dirs)
    loads: tuple  # (Load, Faces)
    accel: Vec | None
    origin: Vec
    voxel: float
    fraction: np.ndarray  # solid fraction per voxel
    stress: np.ndarray  # (Nx, Ny, Nz) von Mises at nodes, MPa
    displacement: np.ndarray  # (Nx, Ny, Nz, 3) mm
    max_stress: float
    max_stress_at: Vec
    stress_p995: float
    max_displacement: float
    max_displacement_at: Vec
    applied: Vec  # total applied force, N (loads and self-weight)
    reaction: Vec  # total support reaction, N
    imbalance: float  # |applied + reaction| / sum of |nodal loads|
    mass: float  # g
    elements: int
    dofs: int
    cg_iterations: int
    seconds: float
    device: str
    notes: tuple = ()
    warnings: tuple = ()  # red flags: an unconverged solve, a singular peak, a load on too little area
    balanced: bool = False  # a free part (balanced=True): displacements relative to its mean position

    @property
    def safety_factor(self) -> float:
        """Yield strength over the peak von Mises stress."""
        return self.material.yield_strength / self.max_stress if self.max_stress > 0 else math.inf

    def stress_at(self, points) -> np.ndarray:
        """Von Mises stress (MPa) at points (``(n, 3)`` or one point)."""
        p = np.atleast_2d(np.asarray(points, dtype=np.float64))
        return _sample(self.stress, self.origin, self.voxel, p)

    def displacement_at(self, points) -> np.ndarray:
        """Displacement vectors (mm) at points."""
        p = np.atleast_2d(np.asarray(points, dtype=np.float64))
        return _sample(self.displacement, self.origin, self.voxel, p)

    def show(self, name: str, title: str | None = None, deform: float | None = None) -> str:
        """Write an overlay output: two fields on the part (von Mises stress, colored up to the 99.5th
        percentile; displacement on the shape deformed ``deform`` times, by default to about 6 % of
        its size), the supports and loads, and the numbers. Returns the folder."""
        from . import outputs

        faces, arrows = _conditions(self.supports, self.loads)
        size = np.array(self.part.size)
        if deform is None:
            deform = _nice(0.06 * float(size.max()) / self.max_displacement) if self.max_displacement > 0 else 0.0
        n = np.minimum(np.ceil(size / self.voxel).astype(int) + 1, self.stress.shape)  # the part's nodes, not the padding
        cut = (slice(0, n[0]), slice(0, n[1]), slice(0, n[2]))
        disp, dstep = self.displacement[cut], self.voxel
        if disp.shape[0] * disp.shape[1] * disp.shape[2] > 100_000:  # smooth: every other node is plenty, 8× smaller
            disp, dstep = self.displacement[: n[0] + 1: 2, : n[1] + 1: 2, : n[2] + 1: 2], 2 * self.voxel
        fields = [
            {"label": "von Mises stress", "unit": "MPa", "values": self.stress[cut], "origin": self.origin,
             "step": self.voxel, "min": 0.0, "max": _round_up(self.stress_p995),
             "note": f"red: the top 0.5 % of the surface; peak {self.max_stress:.3g} MPa"},
            {"label": "displacement", "unit": "mm", "values": disp, "origin": self.origin,
             "step": dstep, "min": 0.0, "max": float(self.max_displacement), "deform": float(deform)},
        ]
        info = _static_info(self, f"{self.elements:,} voxels of {self.voxel:.3g} mm, {self.dofs:,} DOFs")
        return outputs.overlay(name, self.part, faces, arrows=arrows, info=info, fields=fields,
                               title=title or f"Static analysis: {self.part.name or name}, {self.material.name}")

    def __str__(self) -> str:
        m = self.material
        lines = [
            f"fea.static: {self.part.name or 'part'} in {m.name} (E {m.E:g} MPa, yield {m.yield_strength:g} MPa), "
            f"{self.elements:,} voxels of {self.voxel:.3g} mm, {self.dofs:,} DOFs, CG {self.cg_iterations} iterations, "
            f"{self.seconds:.1f} s on {self.device}",
            f"  von Mises stress: peak {self.max_stress:.4g} MPa at {_pt(self.max_stress_at)}; "
            f"99.5th percentile {self.stress_p995:.4g} MPa (surface)",
            f"  safety factor against yield: {self.safety_factor:.3g} on the peak, "
            f"{m.yield_strength / self.stress_p995 if self.stress_p995 > 0 else math.inf:.3g} on the 99.5th percentile",
            f"  max displacement {self.max_displacement:.4g} mm at {_pt(self.max_displacement_at)}",
            _balance_line(self),
            f"  mass {self.mass:.4g} g",
        ]
        lines += [f"  WARNING: {w}" for w in self.warnings]
        lines += [f"  note: {n}" for n in self.notes]
        return "\n".join(lines)

    __repr__ = __str__


def _conditions(supports, loads, moves=()) -> tuple[dict, list]:
    """The overlay's faces (fixed, loaded) and arrows (forces, moments, moves) for supports and loads."""
    from .selection import face_area, face_center

    faces: dict[int, dict] = {}
    for sel, dirs in supports:
        for i in sel.ids:
            faces[i] = {"role": "fixed", "label": "fixed" if dirs == "xyz" else f"fixed in {dirs}"}
    arrows = []
    for sel, dirs, d in moves:
        label = f"moved {_vec(d)} mm" + ("" if dirs == "xyz" else f" (held in {dirs})")
        for i in sel.ids:
            faces[i] = {"role": "load", "label": label}
        w = [face_area(x) for x in sel.items]
        c = sum((np.array(face_center(x)) * a for x, a in zip(sel.items, w)), np.zeros(3)) / max(sum(w), 1e-30)
        if d.length > 0:
            arrows.append({"at": (np.round(c, 6) + 0.0).tolist(), "dir": list(d.normalized()), "label": label})
    for load, sel in loads:
        label = load.label()
        for i in sel.ids:
            both = faces.get(i, {}).get("role") == "load"  # a second load on the same faces
            faces[i] = {"role": "load", "label": f"{faces[i]['label']}; {label}" if both else label}
        w = [face_area(x) for x in sel.items]
        c = sum((np.array(face_center(x)) * a for x, a in zip(sel.items, w)), np.zeros(3)) / max(sum(w), 1e-30)
        c = np.round(np.array(load.at) if load.at is not None else c, 6) + 0.0  # 0, not -8.9e-16
        if load.kind in ("force", "bearing"):
            arrows.append({"at": c.tolist(), "dir": list(load.value.normalized()), "label": label})
        elif load.kind == "moment":
            arrows.append({"at": c.tolist(), "dir": list(load.value.normalized()), "label": f"moment {label}"})
    return faces, arrows


def _balance_line(r) -> str:
    if r.balanced:
        return (f"  free part (balanced=True): the loads balance (net {_vec(r.applied)} N), held only against rigid "
                "motion; displacements relative to its mean position")
    mf = getattr(r, "move_forces", ())
    if mf:
        moved = np.sum([tuple(v) for v in mf], axis=0)
        return (f"  loads {_vec(r.applied)} N, fea.move {_vec(Vec(*moved))} N, reactions {_vec(r.reaction)} N: "
                f"balanced to {100 * r.imbalance:.2g} %")
    return f"  loads {_vec(r.applied)} N, reactions {_vec(r.reaction)} N: balanced to {100 * r.imbalance:.2g} %"


def _static_info(r, mesh: str) -> dict:
    """The facts under a static analysis in the viewer."""
    warn = {f"warning {i + 1}" if len(r.warnings) > 1 else "warning": w for i, w in enumerate(r.warnings)}
    return {
        **warn,
        "material": str(r.material.name),
        "mass": f"{r.mass:.3g} g",
        # the percentile first: it is the number to judge by (a peak at a sharp corner grows with refinement)
        "stress, 99.5th pct": f"{r.stress_p995:.3g} MPa",
        "safety factor": (f"{r.material.yield_strength / r.stress_p995:.3g} on the 99.5th pct"
                          if r.stress_p995 > 0 else "∞") + f" (yield {r.material.yield_strength:g} MPa)",
        "peak stress": f"{r.max_stress:.3g} MPa at {_pt(r.max_stress_at)}: safety factor {r.safety_factor:.3g}; "
                       "at a sharp corner, add a fillet there or refine before trusting it",
        "max displacement": f"{r.max_displacement:.3g} mm at {_pt(r.max_displacement_at)}",
        "loads, total": f"{_vec(r.applied)} N" + (f" with self-weight at {r.accel.length / g:.3g} g"
                                                  if r.accel is not None and r.accel.length else ""),
        "reactions": "none: a free part under balanced loads (balanced=True)" if r.balanced else
                     f"{_vec(r.reaction)} N (balance {100 * r.imbalance:.2g} %)",
        "mesh": mesh,
    }


def _pt(p) -> str:
    return "(" + ", ".join(f"{v:.3g}" for v in p) + ") mm"


def _vec(v) -> str:
    return "(" + ", ".join(f"{(0.0 if abs(x) < 5e-10 else x):.4g}" for x in v) + ")"


def _nice(x: float) -> float:
    """The largest of 1, 2, 5 × 10^n not above x."""
    if not x > 0 or not math.isfinite(x):
        return 1.0
    p = 10 ** math.floor(math.log10(x))
    return max(m * p for m in (1, 2, 5) if m * p <= x * (1 + 1e-9))


def _round_up(x: float) -> float:
    """x rounded up to two significant digits (a legend's end)."""
    if not x > 0:
        return 1.0
    p = 10 ** (math.floor(math.log10(x)) - 1)
    return math.ceil(x / p - 1e-9) * p


# ---------------------------------------------------------------- the analysis


def _pcg(A, M, b, tol: float, maxiter: int, xp, say):
    x = xp.zeros_like(b)
    bn = float(xp.sqrt(xp.sum(b * b)))
    if bn == 0:
        return x, 0, 0.0
    r = b.copy()
    z = M(r)
    p = z
    rz = float(xp.sum(r * z))
    t0 = last = time.time()
    res = 1.0
    for it in range(1, maxiter + 1):
        Ap = A(p)
        alpha = rz / float(xp.sum(p * Ap))
        x = x + alpha * p
        r = r - alpha * Ap
        res = float(xp.sqrt(xp.sum(r * r))) / bn
        if res <= tol:
            return x, it, res
        if time.time() - last > 10:
            last = time.time()
            say(f"fea:   CG iteration {it}, residual {res:.1e}")
        z = M(r)
        rz_new = float(xp.sum(r * z))
        p = z + (rz_new / rz) * p
        rz = rz_new
    return x, maxiter, res


def _lambda_max(level, xp, iterations: int = 12) -> float:
    """The largest eigenvalue of D⁻¹A on a level (power iterations, 10 % added)."""
    x = xp.asarray(np.random.default_rng(0).standard_normal(level.free.shape)) * level.free
    lam = 1.0
    for _ in range(iterations):
        n = float(xp.sqrt(xp.sum(x * x)))
        if n == 0:
            break
        x = level.free * level.A(x / n) / level.diag
        lam = float(xp.sqrt(xp.sum(x * x)))
    return 1.1 * lam


class _Chebyshev:
    """A multigrid V-cycle (``tule.topopt``'s levels, transfers and coarse solve) smoothed by
    Chebyshev polynomials in D⁻¹A on [λ_max/20, λ_max] instead of damped Jacobi: cut voxels and
    the bubble modes push λ_max(D⁻¹A) to 4–5, where a fixed Jacobi damping either diverges or,
    damped enough, barely smooths."""

    def __init__(self, mg, xp, degree: int = 2):
        self.mg, self.xp, self.degree = mg, xp, degree
        self.prolong, self.restrict = _prolong, _restrict
        self.lam = [_lambda_max(lv, xp) for lv in mg.levels[:-1]]

    def smooth(self, l, r, x, res):
        """``degree`` Chebyshev steps from x (residual res = r - A x); returns x and its residual."""
        lv = self.mg.levels[l]
        lmax = self.lam[l]
        lmin = lmax / 20
        theta, delta = (lmax + lmin) / 2, (lmax - lmin) / 2
        sigma = theta / delta
        rho = 1 / sigma
        d = res / lv.diag / theta
        for k in range(self.degree):
            x = x + d
            res = res - lv.A(d)
            if k < self.degree - 1:
                rho_new = 1 / (2 * sigma - rho)
                d = rho_new * rho * d + (2 * rho_new / delta) * (res / lv.diag)
                rho = rho_new
        return x, res

    def __call__(self, r, l: int = 0):
        xp, mg = self.xp, self.mg
        lv = mg.levels[l]
        if l == len(mg.levels) - 1:
            return xp.asarray(mg.coarse.solve(_host(r).reshape(-1))).reshape(r.shape)
        z, res = self.smooth(l, r, xp.zeros_like(r), r)
        nxt = mg.levels[l + 1]
        rc = nxt.free * self.restrict(lv.free * res, xp)
        z = z + lv.free * self.prolong(self(rc, l + 1), xp)
        z, _ = self.smooth(l, r, z, r - lv.A(z))
        return z


def _auto_voxel(part: Shape, dofs: int, max_cells: int) -> float:
    """A voxel edge for about ``dofs`` unknowns: a guess from the volume, corrected by one count."""
    lo, hi = part.bounds
    size = np.array(hi - lo)
    h = (part.volume / (dofs / 3 / 1.5)) ** (1 / 3)
    for _ in range(2):
        dims = np.maximum(1, np.ceil(size / h - 1e-9)).astype(int)
        if np.prod(dims) > 4 * max_cells:
            h *= (np.prod(dims) / max_cells) ** (1 / 3)
            continue
        inside = moments(part, lo, h, tuple(dims), sub=2).sum(-1) >= 0.005
        nodes = np.zeros(tuple(dims + 1), dtype=bool)
        for (i, j, k) in CORNERS:
            nodes[i:i + dims[0], j:j + dims[1], k:k + dims[2]] |= inside
        n = 3 * int(nodes.sum())
        if n == 0:
            h /= 2
            continue
        h *= (n / dofs) ** (1 / 3)
    dims = np.maximum(1, np.ceil(size / h - 1e-9))
    if np.prod(dims) > max_cells:
        h *= (np.prod(dims) / max_cells) ** (1 / 3)
    return float(h)


def _cell_dofs(cells: np.ndarray, shape, xp):
    """The 24 dofs (3a + c: corner a, component c) of voxels (flat indices into ``shape``), as flat
    indices into a node field ``(3, nx+1, ny+1, nz+1)``: ``(24, n)``."""
    N = tuple(n + 1 for n in shape)
    i, j, k = np.unravel_index(np.asarray(cells, dtype=np.int64), shape)
    nodes = [np.ravel_multi_index((i + a, j + b, k + c), N) for (a, b, c) in CORNERS]
    nn = int(np.prod(N))
    return xp.asarray(np.stack([comp * nn + nd for nd in nodes for comp in range(3)]))


class _SparseLevel(_Level):
    """A coarse grid that stores matrices only for its voxels with material (``cells``, flat
    indices; ``Ke`` ``(n, 24, 24)``): a part that fills a small share of its box stays small."""

    def __init__(self, xp, free, shape, cells, Ke):
        self.xp, self.free, self.shape, self.diag = xp, free, tuple(shape), None
        self.KE = self.coef = None
        self.cells, self.Ke = cells, Ke
        self.dofs = _cell_dofs(cells, self.shape, xp)
        self.size = 3 * int(np.prod([n + 1 for n in self.shape]))

    @staticmethod
    def galerkin(xp, free, shape, groups, KE, P):
        """``Pᵀ K P`` on the grid twice as coarse, from fine voxels in groups (flat indices, and their
        matrices, or None for ``KE``)."""
        nx, ny, nz = shape
        coarse = (nx // 2, ny // 2, nz // 2)
        parents = []
        for cells, _ in groups:
            i, j, k = np.unravel_index(cells, shape)
            parents.append((4 * (i % 2) + 2 * (j % 2) + k % 2,  # the child, in CORNERS order
                            np.ravel_multi_index((i // 2, j // 2, k // 2), coarse)))
        on = np.unique(np.concatenate([p for _, p in parents]))
        Kc = xp.zeros((len(on), 24, 24))
        for (cells, Ke), (child, parent) in zip(groups, parents):
            slot = xp.asarray(np.searchsorted(on, parent))
            for c in range(8):
                sel = np.flatnonzero(child == c)
                if not len(sel):
                    continue
                sel = xp.asarray(sel)
                # one child per parent: no repeated indices
                Kc[slot[sel]] += P[c].T @ KE @ P[c] if Ke is None else P[c].T @ Ke[sel] @ P[c]
        cfree = (_restrict(1.0 - free, xp) <= 1e-12).astype(free.dtype)
        return _SparseLevel(xp, cfree, coarse, on, Kc)

    def K(self, u):
        fe = (self.Ke @ u.reshape(-1)[self.dofs].T[:, :, None])[:, :, 0].T
        return self.xp.bincount(self.dofs.reshape(-1), weights=fe.reshape(-1), minlength=self.size).reshape(u.shape)

    def make_diag(self):
        xp = self.xp
        idx = xp.arange(24)
        d = xp.bincount(self.dofs.reshape(-1), weights=self.Ke[:, idx, idx].T.reshape(-1), minlength=self.size)
        d = d.reshape(self.free.shape)
        self.free = self.free * (d > 1e-30 * float(d.max() if d.size else 1))
        self.diag = xp.where(self.free > 0, d, 1.0)

    def coarsen(self, P):
        return _SparseLevel.galerkin(self.xp, self.free, self.shape, [(self.cells, self.Ke)], None, P)

    def assemble(self):
        """The operator as a sparse matrix on the host, fixed dofs as identity rows (the coarsest solve)."""
        from scipy.sparse import coo_matrix, diags

        free = _host(self.free).reshape(-1)
        dofs = _host(self.dofs).T  # (n, 24)
        rows = np.broadcast_to(dofs[:, :, None], (len(dofs), 24, 24)).reshape(-1)
        cols = np.broadcast_to(dofs[:, None, :], (len(dofs), 24, 24)).reshape(-1)
        vals = _host(self.Ke).reshape(-1) * free[rows] * free[cols]
        return (coo_matrix((vals, (rows, cols)), shape=(self.size, self.size)).tocsc() + diags(1.0 - free)).tocsc()


class _CutLevel(_Level):
    """The finest grid: full voxels share one matrix (``KE`` × ``coef``); voxels cut by the
    surface (flat indices ``cut``) have their own, ``Kcut`` ``(n, 24, 24)``, scaled already."""

    def __init__(self, xp, free, KE, coef, cut, Kcut):
        super().__init__(xp, free, KE=KE, coef=coef)
        self.cut, self.Kcut = cut, Kcut
        # products touch only the voxels with material (a bounding box is mostly empty)
        full = np.flatnonzero(_host(coef).reshape(-1) > 0)
        self.scale = float(_host(coef).reshape(-1)[full[0]]) if len(full) else 0.0
        self.nfull = len(full)
        self.dofs = _cell_dofs(np.concatenate([full, _host(cut)]), self.shape, xp)  # (24, full then cut voxels)
        self.size = 3 * int(np.prod([n + 1 for n in self.shape]))

    def K(self, u):
        xp = self.xp
        ue = u.reshape(-1)[self.dofs]
        fe = xp.empty_like(ue)
        nf = self.nfull
        fe[:, :nf] = (self.KE @ ue[:, :nf]) * self.scale
        if len(self.cut):
            fe[:, nf:] = (self.Kcut @ ue[:, nf:].T[:, :, None])[:, :, 0].T
        return xp.bincount(self.dofs.reshape(-1), weights=fe.reshape(-1), minlength=self.size).reshape(u.shape)

    def element_diagonals(self):
        d = super().element_diagonals()
        if len(self.cut):
            idx = self.xp.arange(24)
            d[:, self.cut] += self.Kcut[:, idx, idx].T
        return d

    def coarsen(self, P):
        full = np.flatnonzero(_host(self.coef).reshape(-1) > 0)
        cut = _host(self.cut)
        KE = self.KE * self.scale
        return _SparseLevel.galerkin(self.xp, self.free, self.shape, [(full, None), (cut, self.Kcut)], KE, P)

    def plain(self) -> _Level:
        """The same operator with a matrix per voxel (for a grid too small for multigrid)."""
        xp = self.xp
        Ke = self.coef.reshape(-1, 1, 1) * self.KE[None]
        if len(self.cut):
            Ke[self.cut] += self.Kcut
        return _Level(xp, self.free, Ke=Ke.reshape(self.shape + (24, 24)))


def _inputs(part, material, fixed, loads, accel, op: str, need_fixed: bool = True, moves: list | None = None):
    """Checks an analysis' inputs: (part, Material, [(Faces, dirs)], [(Load, Faces)], accel Vec or None).
    ``fea.move`` entries (in ``fixed`` or ``loads``) go to ``moves`` as (Faces, dirs, d) when it is a
    list, else they are refused."""
    if not isinstance(part, Shape):
        raise TuleError(f"{op} needs a Shape, got {type(part).__name__}", op="fea")
    mat = _material(material)
    sups = [] if fixed is None else fixed if isinstance(fixed, (list, tuple)) else [fixed]
    if isinstance(loads, (Load, Move)):
        loads = [loads]
    loads = list(loads or ())
    supports = []
    for s in sups + [ld for ld in loads if isinstance(ld, Move)]:
        if isinstance(s, Move):
            if moves is None:
                raise TuleError(f"fea.move is for fea.static, not {op}", op="fea", hint="fea.fix(faces) holds faces in place")
            moves.append((_faces(part, s.faces, "fea.move"), s.dirs, s.d))
            continue
        s = s if isinstance(s, Support) else Support(s)
        supports.append((_faces(part, s.faces, "fixed"), s.dirs))
    loads = [ld for ld in loads if not isinstance(ld, Move)]
    if not supports and not moves and need_fixed:
        raise TuleError("nothing holds the part", op="fea", hint='fixed="mount" (a tag) or fixed=part.faces["mount"]'
                        + ("; a free part whose loads balance (a link pulled at both ends): balanced=True"
                           if op == "fea.static" else ""))
    resolved = []
    for ld in loads:
        if not isinstance(ld, Load):
            raise TuleError(f"loads are fea.force, fea.pressure, fea.moment or fea.bearing (or fea.move), got {type(ld).__name__}", op="fea",
                            hint='loads=[fea.force("load", (0, 0, -30))]')
        resolved.append((ld, _faces(part, ld.faces, f"the {ld.kind}")))
    a = vec(accel) if accel is not None else None
    if a is not None and a.length > 0 and mat.density == 0:
        raise TuleError("accel needs a density", op="fea", hint="give the material a density (g/cm³)")
    return part, mat, supports, resolved, a


def thinnest(part: Shape) -> "tetfe.Thickness":
    """The part's thinnest wall and narrowest gap (mm, and where), and per face (``.wall``,
    ``.gap``, by face id): rays along each face's normals at a few points, the median per face."""
    from . import tetfe

    return tetfe.thickness(part)


def _choose_mesh(part, mesh: str, voxel, size, dofs: int, say, need_tet: str | None = None) -> str:
    """``need_tet``: what needs tetrahedra (nonlinear=True, fea.move), if anything."""
    from . import tetfe

    if mesh not in ("auto", "voxel", "tet"):
        raise TuleError(f"mesh={mesh!r}", op="fea", hint='"auto", "voxel" or "tet"')
    if need_tet:
        if mesh == "voxel" or voxel:
            raise TuleError(f"{need_tet} runs on tetrahedra, not voxels", op="fea",
                            hint='leave out mesh="voxel" and voxel= (mesh="auto" picks tetrahedra; size= sets them)')
        return "tet"
    if mesh != "auto":
        if mesh == "voxel" and size:
            raise TuleError("size= is for tetrahedra", op="fea", hint="voxel= sets the voxel edge")
        return mesh
    if voxel:
        return "voxel"
    if size:
        return "tet"
    if not tetfe.find_gmsh():
        return "voxel"
    th = tetfe.thickness(part)
    h = (part.volume / (dofs / 4.5)) ** (1 / 3)  # about the voxel the budget gives
    if th.min_wall < 3 * h or th.min_gap < 2 * h:
        what = (f"a {th.min_wall:.3g} mm wall" if th.min_wall < 3 * h else "") + \
               (" and " if th.min_wall < 3 * h and th.min_gap < 2 * h else "") + \
               (f"a {th.min_gap:.3g} mm gap" if th.min_gap < 2 * h else "")
        say(f"fea: {what} against voxels of about {h:.2g} mm: quadratic tetrahedra (mesh=\"voxel\" keeps voxels)")
        return "tet"
    return "voxel"


def static(
    part: Shape,
    material: str | Material = "aluminium 6061-T6",
    *,
    fixed=None,
    loads: Sequence[Load] = (),
    accel=None,
    balanced: bool = False,
    voxel: float | None = None,
    dofs: int = 300_000,
    mesh: str = "auto",
    size: float | None = None,
    device: str = "auto",
    tol: float = 1e-6,
    nonlinear: bool = False,
    steps: int | None = None,
    verbose: bool = True,
):
    """Static analysis of ``part``: returns a result (print it; ``.show(name)`` for the viewer).

    ``fixed``: faces held in place (a tag, a face selection, ``fea.fix(faces, "z")``, or a list),
    and faces driven by a displacement (``fea.move(faces, (dx, dy, dz))``; the force each takes
    is ``result.move_forces``). ``loads``: ``fea.force``, ``fea.pressure``, ``fea.moment``,
    ``fea.bearing``. ``accel``: an acceleration (mm/s²) on the part's own mass, e.g. ``(0, 0, -3 * fea.g)``.

    ``nonlinear=True``: large displacements and rotations (small strains, St. Venant–Kirchhoff),
    on tetrahedra: loads and moves applied in steps (``steps=``: equal ones; default adaptive),
    Newton–Raphson in each. Loads are dead (a pressure keeps its direction and area). The result's
    ``steps`` hold the path (``fraction``, ``move_forces``, ``reaction``; ``displacement_at(p,
    step=i)``). A step that fails is retried smaller; if it keeps failing, or the part buckles or snaps
    through under the loads, a ``TuleError`` says where (its ``.result``: the last converged step).
    ``balanced=True`` (no ``fixed``): a free part whose loads balance, such as a link pulled at
    both ends; checked to 1 % (else an error with the net force and moment), the rest taken up
    by inertia relief; three points hold it against rigid motion only, and displacements are
    relative to its mean position. ``result.warnings`` lists red flags (also printed).

    ``mesh``: ``"voxel"`` (a grid of cut voxels, ``Result``), ``"tet"`` (quadratic tetrahedra
    refined in thin walls, ``TetResult``; needs gmsh), or ``"auto"``: tetrahedra when a wall
    is under 3 voxels thick or a gap under 2 voxels wide (``fea.thinnest(part)``), else voxels.
    ``dofs``: the budget of unknowns for either. ``voxel`` (mm) sets the grid (and picks voxels);
    ``size`` (mm) the largest tetrahedron (and picks them). ``device``: ``"auto"`` (CuPy on a
    GPU), ``"cpu"``, ``"gpu"`` (voxels only).
    """
    r = _static(part, material, fixed=fixed, loads=loads, accel=accel, balanced=balanced, voxel=voxel, dofs=dofs,
                mesh=mesh, size=size, device=device, tol=tol, nonlinear=nonlinear, steps=steps, verbose=verbose)
    _say_warnings(r.warnings)
    return r


def _static(part, material, *, fixed, loads, accel, balanced, voxel, dofs, mesh, size, device, tol, verbose,
            nonlinear=False, steps=None):
    t_start = time.time()
    say = (lambda msg: print(f"{msg} [{time.time() - t_start:.1f} s]", flush=True)) if verbose else (lambda *a, **k: None)
    moves: list = []
    part, mat, supports, resolved, a = _inputs(part, material, fixed, loads, accel, "fea.static", need_fixed=not balanced,
                                               moves=moves)
    if balanced and (supports or moves):
        raise TuleError("balanced=True is for a free part: leave out fixed", op="fea",
                        hint="either fixed= (held at faces) or balanced=True (held by the balance of its loads)")
    if balanced and nonlinear:
        raise TuleError("nonlinear=True needs the part held (fixed= or fea.move), not balanced=True", op="fea",
                        hint="fix one end and load the other; a free part's large rotations are rigid motion")
    if not resolved and (a is None or a.length == 0) and not moves:
        raise TuleError("no loads", op="fea", hint='loads=[fea.force("load", (0, 0, -30))], accel=(0, 0, -fea.g), '
                                                   'or fixed=[..., fea.move("top", (1, 0, 0))]')
    if steps is not None and not (isinstance(steps, int) and 1 <= steps <= 1000):
        raise TuleError(f"steps={steps!r}", op="fea", hint="a number of equal load steps, 1 to 1000 (or None: adaptive)")
    if steps is not None and not nonlinear:
        raise TuleError("steps= is for nonlinear=True", op="fea", hint="a linear analysis is one solve")
    need = "nonlinear=True" if nonlinear else "fea.move" if moves else None
    kind = _choose_mesh(part, mesh, voxel, size, dofs, say, need_tet=need)
    if kind == "tet":
        return _static_tet(part, mat, supports, resolved, a, dofs, size, say, t_start, tol=min(tol, 1e-8), balanced=balanced,
                           moves=tuple(moves), nonlinear=nonlinear, steps=steps)

    xp = backend(device)
    dev = "gpu" if xp is not np else "cpu"
    lo, hi = part.bounds
    size = np.array(hi - lo)
    origin = np.array(lo, dtype=np.float64)
    max_cells = max(2_000_000, 4 * dofs)
    h = float(voxel) if voxel else _auto_voxel(part, dofs, max_cells)
    if not h > 0:
        raise TuleError("voxel must be positive", op="fea")
    dims0 = np.maximum(1, np.ceil(size / h - 1e-9)).astype(int)
    levels = _levels_for(tuple(dims0), min_coarse_elements=2)
    mult = 2 ** (levels - 1)
    dims = tuple(int(math.ceil(d / mult) * mult) for d in dims0)
    say(f"fea: {part.name or 'part'} in {mat.name}: voxels of {h:.3g} mm, {dims0[0]}×{dims0[1]}×{dims0[2]} grid, "
        f"{levels} multigrid levels, {dev}")

    # the material in each voxel: moments of the inside (their sum is the solid fraction)
    sub = 3 if dims0[0] * dims0[1] * 9 <= 150_000 else 2  # rays per voxel column: 9, or 4 on big grids
    m = moments(part, origin, h, dims, sub)
    frac = np.clip(m.sum(-1), 0.0, 1.0)
    frac[frac < 0.005] = 0.0  # specks: a voxel grazing a face
    if not frac.any():
        raise TuleError("the part has no voxels", op="fea", hint=f"use a smaller voxel than {h:.3g} mm")
    notes = []

    def nodes_of(cells):
        out = np.zeros(tuple(d + 1 for d in dims), dtype=bool)
        for (i, j, k) in CORNERS:
            out[i:i + dims[0], j:j + dims[1], k:k + dims[2]] |= cells
        return out

    node_active = nodes_of(frac > 0)
    fixed_dir = np.zeros((3,) + node_active.shape, dtype=bool)
    for sel, dirs in supports:
        nodes = _face_nodes(part, sel, Vec(*origin), h, node_active)
        if not nodes.any():
            raise TuleError("no grid nodes found on the fixed faces", op="fea", faces=sel.ids,
                            hint="use a smaller voxel so the faces are resolved")
        for d, c in enumerate("xyz"):
            if c in dirs:
                fixed_dir[d] |= nodes

    # pieces (face-connected voxels) that touch no support would float: drop specks, refuse the rest
    from scipy.ndimage import label

    lab, npieces = label(frac > 0)
    if npieces > 1:
        vols = np.bincount(lab.reshape(-1), weights=frac.reshape(-1))
        if balanced:  # held as one piece: the biggest
            held = {int(np.argmax(vols[1:])) + 1}
        else:
            held_node = fixed_dir.any(axis=0)
            touch = np.zeros(dims, dtype=bool)
            for (i, j, k) in CORNERS:
                touch |= held_node[i:i + dims[0], j:j + dims[1], k:k + dims[2]]
            held = set(np.unique(lab[touch & (lab > 0)]).tolist())
        loose = [p for p in range(1, npieces + 1) if p not in held]
        if loose:
            lv = float(sum(vols[p] for p in loose))
            if lv > 0.005 * float(vols[1:].sum()):
                cells_at = np.argwhere(np.isin(lab, loose))
                at = origin + (cells_at[0] + 0.5) * h
                if balanced:
                    raise TuleError(f"balanced=True needs the part in one piece: {len(loose)} more piece(s) "
                                    f"({100 * lv / vols[1:].sum():.1f} % of it), e.g. at {_pt(at)}", op="fea",
                                    hint="join them (union) where they touch; thin connections need a smaller voxel")
                raise TuleError(f"{len(loose)} piece(s) of the part ({100 * lv / vols[1:].sum():.1f} % of it) touch no "
                                f"fixed face, e.g. at {_pt(at)}", op="fea",
                                hint="hold every piece, or join them; thin connections need a smaller voxel")
            frac = np.where(np.isin(lab, loose), 0.0, frac)
            node_active = nodes_of(frac > 0)
            notes.append(f"dropped {len(loose)} loose speck(s) of voxels")
    fixed_dir &= node_active[None]
    active = frac > 0
    full = frac >= 1 - 1e-9
    cut = np.flatnonzero(active & ~full)
    n_el = int(active.sum())

    # loads on the nodes
    say(f"fea: {n_el:,} voxels ({len(cut):,} cut by the surface), {3 * int(node_active.sum()):,} DOFs; loads …")
    mesh = part.tessellate(min(0.05, h / 8), 0.2)
    spread = _Spread(origin, h, node_active)
    # the material each node holds, in voxels (8 inside, 4 on a face, under ½ a sliver)
    nodevol = np.zeros(node_active.shape)
    for (i, j, k) in CORNERS:
        nodevol[i:i + dims[0], j:j + dims[1], k:k + dims[2]] += frac
    warnings = []
    F = np.zeros(node_active.shape + (3,))
    load_pts = []
    for ld, sel in resolved:
        pts, w, nrm, _ = _surface(mesh, min(h / 3, ld.r / 4) if ld.at is not None else h / 3, sel.ids)
        if not len(pts):
            raise TuleError("the load's faces have no area", op="fea", faces=sel.ids)
        vecs, area, bearing_area = _load_vectors(ld, pts, w, nrm, sel.ids)
        Fl = spread(pts, vecs, np.zeros_like(F))
        F += Fl
        load_pts.append(pts[np.linalg.norm(vecs, axis=1) > 0])
        warnings += _load_warnings(ld, area, bearing_area, h, "voxels", mat.yield_strength)
        mag = np.linalg.norm(Fl, axis=-1)
        weak = float(mag[nodevol < 0.5].sum()) / max(float(mag.sum()), 1e-300)
        if weak > 0.25:
            warnings.append(f"{100 * weak:.0f} % of the {ld.label()} lands on slivers of material in cut voxels: "
                            f"its faces are poorly resolved at {h:.3g} mm (refine: voxel={h / 1.5:.2g}, or mesh=\"tet\")")
    for sel, dirs in supports:  # the material the clamp holds, against a face's worth of full voxels
        from .selection import face_area

        held = _face_nodes(part, sel, Vec(*origin), h, node_active) & node_active
        grip = float(np.minimum(nodevol[held], 4).sum()) / (4 * sum(face_area(x) for x in sel.items) / h ** 2)
        if held.sum() < 4 or grip < 0.35:
            warnings.append(f"the fixed faces {list(sel.ids)} are poorly resolved by the voxels ({int(held.sum())} nodes, "
                            f"holding {100 * grip:.0f} % of a face's worth of material): results can jump between voxel "
                            f"sizes (refine: voxel={h / 1.5:.2g}, or mesh=\"tet\")")
    # each corner's share of the voxel's material (∫ N_a, from the moments): its mass, and the weights of a free part
    Nq = np.array([[np.prod([t[d] if c[d] else 1 - t[d] for d in range(3)]) for c in CORNERS]
                   for t in [(x, y, z) for x in _T3 for y in _T3 for z in _T3]])
    share = (m.reshape(-1, 27) @ Nq).reshape(dims + (8,)) * (frac > 0)[..., None]
    if a is not None and a.length > 0:
        kg = h ** 3 * mat.density * 1e-6  # per unit of moment: mm³ · g/cm³ → kg
        for c, (i, j, k) in enumerate(CORNERS):
            F[i:i + dims[0], j:j + dims[1], k:k + dims[2]] += (share[..., c] * kg)[..., None] * (np.array(a) / 1000)
    if balanced:
        nodal_share = np.zeros(node_active.shape)
        for c, (i, j, k) in enumerate(CORNERS):
            nodal_share[i:i + dims[0], j:j + dims[1], k:k + dims[2]] += share[..., c]
        on = np.argwhere(node_active)
        X = origin + on * h
        wts = np.maximum(nodal_share[node_active], 1e-12)
        F[node_active], R, M = _relieve(X, F[node_active], wts, float(size.max()), float(part.volume * mat.density / 1000))
        if np.linalg.norm(R) + np.linalg.norm(M) / float(size.max()) > 1e-6 * float(np.linalg.norm(F, axis=-1).sum()):
            notes.append(f"the loads were out of balance by {_vec(R)} N, {_vec(M)} N·mm: inertia relief took it up")
        inner = nodevol[node_active] >= 8 - 1e-6  # hold it at nodes inside the material
        cand = np.flatnonzero(inner) if inner.sum() >= 3 else np.argsort(-nodevol[node_active])[:max(3, len(on) // 10)]
        for n, dirs in _three_two_one(X[cand]):
            i, j, k = on[cand[n]]
            for d, c in enumerate("xyz"):
                if c in dirs:
                    fixed_dir[d, i, j, k] = True
    f = np.moveaxis(F, -1, 0)  # (3, Nx, Ny, Nz)
    applied = f.reshape(3, -1).sum(1)
    total_abs = float(np.linalg.norm(f, axis=0).sum())
    on_fixed = float(np.linalg.norm(f * fixed_dir, axis=0).sum()) if not balanced else 0.0

    # solve
    KE, S_full = element(mat.nu)
    free = (~fixed_dir).astype(np.float64)
    coef = np.where(full, mat.E * h, 0.0)
    t_solve = time.time()
    Kcut = np.concatenate([cells(m.reshape(-1, 27)[cut[s:s + 20000]], mat.nu) for s in range(0, len(cut), 20000)]
                          or [np.zeros((0, 24, 24))]) * (mat.E * h)
    fine = _CutLevel(xp, xp.asarray(free), xp.asarray(KE), xp.asarray(coef), xp.asarray(cut), xp.asarray(Kcut))
    del Kcut
    if levels == 1:
        fine = fine.plain()
    mg = _Multigrid(xp, None, None, None, levels, fine=fine)
    cycle = _Chebyshev(mg, xp)
    lv = mg.levels[0]
    ndof = int(_host(lv.free).sum())
    b = xp.asarray(f) * lv.free
    say(f"fea: solving for {ndof:,} unknowns …")
    u, iters, res = _pcg(lv.A, cycle, b, tol, 2000, xp, say)
    if res > tol * 10:
        raise TuleError(f"the solve did not converge (residual {res:.1e} after {iters} iterations)", op="fea",
                        hint="is every piece held against sliding and turning? fea.fix(faces, \"z\") alone lets a part "
                        "slide sideways; fix more faces or more directions")
    say(f"fea: solved in {iters} CG iterations, {time.time() - t_solve:.1f} s")

    # reactions: what the supports push with
    Ku = lv.K(u)
    react = _host((Ku - xp.asarray(f)) * (1 - lv.free) * xp.asarray(node_active[None].astype(np.float64)))
    reaction = react.reshape(3, -1).sum(1)
    imbalance = float(np.linalg.norm(reaction + applied)) / max(total_abs, 1e-300)
    residual = float(xp.sqrt(xp.sum(((Ku - b) * lv.free) ** 2))) / max(float(xp.sqrt(xp.sum(b * b))), 1e-300)
    if balanced:  # displacements relative to the part's mean position
        uh = _host(u)
        U = np.moveaxis(uh, 0, -1)
        rig = _rigid(X, U[node_active], wts)
        U[node_active] -= rig
        u = xp.asarray(np.moveaxis(U, -1, 0))

    # stresses at the voxels' corners, averaged at nodes (weights: solid fraction squared); only the
    # voxels with material, full ones first, then the cut ones (lv.dofs' order)
    order = np.concatenate([np.flatnonzero(full.reshape(-1)), cut])
    nfull = len(order) - len(cut)
    dofs = _cell_dofs(order, dims, xp)
    ue = u.reshape(-1)[dofs]  # (24, voxels)
    uc = _host(ue[:, nfull:])
    sig_cut = np.concatenate([np.einsum("nasj,jn->nas", cells(m.reshape(-1, 27)[cut[s:s + 5000]], mat.nu, True)[1],
                                        uc[:, s:s + 5000]) for s in range(0, len(cut), 5000)] or [np.zeros((0, 8, 6))])
    sig_cut = xp.asarray(sig_cut * (mat.E / h))
    w = xp.asarray((frac ** 2).reshape(-1)[order])
    nn = int(node_active.size)
    nodal = xp.zeros((6, nn))
    wsum = xp.zeros(nn)
    Sd = xp.asarray(S_full) * (mat.E / h)
    for c in range(8):
        sig = xp.concatenate([Sd[c] @ ue[:, :nfull], sig_cut[:, c].T], axis=1)  # (6, voxels)
        node = dofs[3 * c]  # the corner's x dof is its node index
        for s in range(6):
            nodal[s] += xp.bincount(node, weights=sig[s] * w, minlength=nn)
        wsum += xp.bincount(node, weights=w, minlength=nn)
    known = _host(wsum).reshape(node_active.shape) > 0
    nodal = _host(nodal / xp.where(wsum > 0, wsum, 1.0)).reshape((6,) + node_active.shape)
    vm = _fill(von_mises(nodal), known)
    disp = _fill(np.moveaxis(_host(u), 0, -1), node_active)

    # numbers from the surface
    pts, area, _, _ = _surface(mesh, max(h / 2, math.sqrt(part.area / 400_000)))
    s_vm = _sample(vm, origin, h, pts)
    i_max = int(np.argmax(s_vm))
    order = np.argsort(s_vm)
    cum = np.cumsum(area[order])
    p995 = float(s_vm[order[min(int(np.searchsorted(cum, 0.995 * cum[-1])), len(order) - 1)]])
    s_u = np.linalg.norm(_sample(disp, origin, h, pts), axis=1)
    j_max = int(np.argmax(s_u))

    # walls a few voxels thick: the voxels' distance to the outside, its maximum nearby
    from scipy.ndimage import distance_transform_edt, maximum_filter

    depth = maximum_filter(distance_transform_edt(np.pad(active, 1)), size=5)[1:-1, 1:-1, 1:-1]
    thin = float(frac[active & (depth <= 1.5)].sum()) / max(float(frac.sum()), 1e-30)
    if thin > 0.25:
        notes.append(f"{100 * thin:.0f} % of the part is in walls under 3 voxels thick: run again with "
                     f"voxel={h / 1.5:.2g}; if the peak moves by more than ~10 %, refine further")

    # red flags
    peak, peak_at = float(s_vm[i_max]), pts[i_max]
    where = _near(peak_at, [_surface(mesh, h / 2, sel.ids)[0] for sel, _ in supports], load_pts, 1.5 * h)
    warnings = (_common_warnings(residual, tol, 0.0 if balanced else imbalance, on_fixed / max(total_abs, 1e-300))
                + _peak_warning(peak, peak_at, p995, where, mat.yield_strength) + warnings
                + ([] if balanced else _large_warning(float(s_u[j_max]), float(size.max()), None)))
    return Result(
        part=part, material=mat, supports=tuple(supports), loads=tuple(resolved), accel=a,
        origin=Vec(*origin), voxel=h, fraction=frac, stress=vm.astype(np.float32), displacement=disp.astype(np.float32),
        max_stress=peak, max_stress_at=Vec(*peak_at), stress_p995=p995,
        max_displacement=float(s_u[j_max]), max_displacement_at=Vec(*pts[j_max]),
        applied=Vec(*applied), reaction=Vec(*reaction), imbalance=imbalance,
        mass=float(part.volume * mat.density / 1000), elements=n_el, dofs=ndof, cg_iterations=int(iters),
        seconds=time.time() - t_start, device=dev, notes=tuple(notes), warnings=tuple(warnings), balanced=balanced,
    )


@dataclass(frozen=True, repr=False)
class Convergence:
    """``fea.static`` at several budgets: ``results`` (coarse to fine), ``result`` (the finest),
    ``spread(name)`` and ``converged``."""

    results: tuple

    @property
    def result(self):
        """The finest result."""
        return self.results[-1]

    def spread(self, name: str) -> float:
        """How far a number (``"max_displacement"``, ``"stress_p995"``, ``"max_stress"``) moved
        across the runs: (largest - smallest) / the finest."""
        v = np.array([getattr(r, name) for r in self.results], dtype=np.float64)
        return float((v.max() - v.min()) / abs(v[-1])) if v[-1] else 0.0

    @property
    def converged(self) -> bool:
        """Max displacement and 99.5th-percentile stress within 5 % across the runs."""
        return self.spread("max_displacement") <= 0.05 and self.spread("stress_p995") <= 0.05

    def __str__(self) -> str:
        lines = ["fea.converge: " + (self.result.part.name or "part")]
        for r in self.results:
            mesh = f"voxels of {r.voxel:.3g} mm" if isinstance(r, Result) else "quadratic tetrahedra"
            lines.append(f"  {r.dofs:>9,} DOFs ({mesh}): max displacement {r.max_displacement:.4g} mm, "
                         f"99.5th pct {r.stress_p995:.4g} MPa, peak {r.max_stress:.4g} MPa")
        sp = {k: self.spread(k) for k in ("max_displacement", "stress_p995", "max_stress")}
        lines.append(f"  spread: displacement {100 * sp['max_displacement']:.1f} %, 99.5th pct {100 * sp['stress_p995']:.1f} %, "
                     f"peak {100 * sp['max_stress']:.0f} %")
        lines.append("  converged (within 5 %): judge by the finest" if self.converged else
                     "  NOT converged: the numbers move with the mesh; a larger dofs=, mesh=\"tet\", or check the "
                     "supports (a part on a small or partly gripped face tilts differently each time)")
        if sp["max_stress"] > 0.25 and self.converged:
            lines.append("  the peak keeps moving: a singularity (sharp corner, clamp edge, point load); judge by the percentile")
        lines += [f"  WARNING: {w}" for w in self.result.warnings]
        return "\n".join(lines)

    __repr__ = __str__


def converge(part: Shape, material: str | Material = "aluminium 6061-T6", *, dofs: int = 300_000, runs: int = 3,
             **kw) -> Convergence:
    """``fea.static`` (same arguments) ``runs`` times, the budget doubling up to ``dofs``
    (default 75k, 150k, 300k: 1.75× the cost of the last): print it for each run's numbers and
    their spread; ``.converged`` when max displacement and the 99.5th percentile hold within 5 %;
    ``.result`` is the finest. Each voxel size cuts the faces differently, so a part whose
    numbers jump between sizes shows it here."""
    if "voxel" in kw or "size" in kw:
        raise TuleError("fea.converge sets the mesh from dofs", op="fea.converge", hint="leave out voxel= and size=")
    runs = int(runs)
    if not 2 <= runs <= 5:
        raise TuleError(f"runs={runs}", op="fea.converge", hint="2 to 5")
    verbose = kw.pop("verbose", True)
    out = []
    for i in range(runs):
        d = int(dofs / 2 ** (runs - 1 - i))
        if verbose:
            print(f"fea.converge: run {i + 1} of {runs}, {d:,} DOFs …", flush=True)
        out.append(_static(part, material, dofs=d, verbose=False, **{**_STATIC_DEFAULTS, **kw}))
    c = Convergence(tuple(out))
    _say_warnings(c.result.warnings)
    return c


_STATIC_DEFAULTS = dict(fixed=None, loads=(), accel=None, balanced=False, voxel=None, mesh="auto", size=None,
                        device="auto", tol=1e-6, nonlinear=False, steps=None)


# ---------------------------------------------------------------- tetrahedra (thin walls, modes, buckling)

TONNE_PER_MM3 = 1e-9  # a density in g/cm³ in t/mm³: masses in tonnes keep N, mm and s consistent


def _tet_mesh(part: Shape, dofs: int, size: float | None, say, notes: list, patches=()):
    """Thickness, sizes, and gmsh's quadratic tetrahedra (elements of r/2 in load patches (at, r))."""
    from . import tetfe

    th = tetfe.thickness(part)
    H, faces, plan_notes = tetfe.plan(part, th, dofs, size)
    notes += plan_notes
    thin = min(faces.values()) if faces else None
    say(f"fea: meshing {part.name or 'the part'} with gmsh: elements up to {H:.3g} mm"
        + (f", {thin:.3g} mm on {len(faces)} thin face(s) (walls down to {th.min_wall:.3g} mm)" if faces else "") + " …")
    balls = [(tuple(at), r, min(H, r / 2)) for at, r in patches]
    m = tetfe.mesh(part, H, faces, smallest=min([thin / 2 if thin else H, H / 25] + [b[2] for b in balls]), balls=balls)
    notes += m.notes
    if 3 * len(m.nodes) > 2.5 * dofs:
        notes.append(f"the mesh has {3 * len(m.nodes):,} DOFs, over the budget of {dofs:,}: thin walls and small "
                     "curved faces need small elements; size= coarsens the rest")
    return m


def _tet_constraints(m, supports, moves=()):
    """Constrained DOFs (n, 3), their values under the full moves (n, 3, mm), and which move holds
    each (-1: a fixed face or free)."""
    n = len(m.nodes)
    fixed = np.zeros((n, 3), dtype=bool)
    presc = np.zeros((n, 3))
    which = np.full((n, 3), -1)
    for sel, dirs in supports:
        tri = np.isin(m.tri_face, sel.ids)
        if not tri.any():
            raise TuleError("the mesh has no triangles on the fixed faces", op="fea", faces=sel.ids)
        nodes = np.unique(m.tris[tri])
        for d, c in enumerate("xyz"):
            if c in dirs:
                fixed[nodes, d] = True
    held = fixed.copy()
    for j, (sel, dirs, dv) in enumerate(moves):
        tri = np.isin(m.tri_face, sel.ids)
        if not tri.any():
            raise TuleError("the mesh has no triangles on the moved faces", op="fea.move", faces=sel.ids)
        nodes = np.unique(m.tris[tri])
        for d, c in enumerate("xyz"):
            if c not in dirs:
                continue
            clash = held[nodes, d] | ((which[nodes, d] >= 0) & (presc[nodes, d] != dv[d]))
            if clash.any():
                raise TuleError(f"the moved faces {list(sel.ids)} share {int(clash.sum())} node(s) with fixed or other "
                                f"moved faces, e.g. at {_pt(m.nodes[nodes[clash][0]])}: they cannot both hold there",
                                op="fea.move", faces=sel.ids,
                                hint="move or fix faces that do not touch (an edge between them is shared), or fewer directions")
            fixed[nodes, d] = True
            presc[nodes, d] = dv[d]
            which[nodes, d] = j
    return fixed, presc, which


def _tet_fixed(m, supports, moves=()) -> np.ndarray:
    return _tet_constraints(m, supports, moves)[0]


def _tet_pieces(m, fixed, need: bool = True, balanced: bool = False):
    """Refuses pieces of the mesh that no support holds (they would float); with ``balanced``, any second piece."""
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components

    n = len(m.nodes)
    a = np.repeat(m.tets[:, 0], 9)
    b = m.tets[:, 1:].reshape(-1)
    npieces, lab = connected_components(coo_matrix((np.ones(len(a)), (a, b)), shape=(n, n)), directed=False)
    if npieces <= 1 or not (need or balanced):
        return
    used = np.zeros(n, dtype=bool)
    used[m.tets.reshape(-1)] = True
    if balanced:
        pieces = np.unique(lab[used])
        if len(pieces) > 1:
            raise TuleError(f"balanced=True needs the part in one piece: it has {len(pieces)}", op="fea",
                            hint="join them (union) where they touch")
        return
    held = set(np.unique(lab[fixed.any(1)]).tolist())
    loose = [p for p in np.unique(lab[used]) if p not in held]
    if loose:
        at = m.nodes[np.flatnonzero(lab == loose[0])[0]]
        raise TuleError(f"{len(loose)} piece(s) of the part touch no fixed face, e.g. at {_pt(at)}", op="fea",
                        hint="hold every piece, or join them (union) where they touch")


def _tet_loads(m, model, resolved, mat: Material, a, info: list | None = None) -> np.ndarray:
    """Nodal loads (n, 3); ``info`` gets (load, loaded area, bearing area, element size there, loaded points)."""
    from . import tetfe

    f = np.zeros((len(m.nodes), 3))
    for ld, sel in resolved:
        tris = m.tris[np.isin(m.tri_face, sel.ids)]
        if not len(tris):
            raise TuleError("the mesh has no triangles on the load's faces", op="fea", faces=sel.ids)
        pts, N, wn = tetfe.surface_quadrature(m.nodes, tris)
        k, g = pts.shape[:2]
        w = np.linalg.norm(wn, axis=2).reshape(-1)  # area weights
        vecs, area, bearing_area = _load_vectors(ld, pts.reshape(-1, 3), w, wn.reshape(-1, 3) / np.maximum(w, 1e-300)[:, None], sel.ids)
        nodal = np.einsum("kgi,ga->kai", vecs.reshape(k, g, 3), N)
        idx = tris.reshape(-1)
        for c in range(3):
            f[:, c] += np.bincount(idx, weights=nodal[..., c].reshape(-1), minlength=len(f))
        if info is not None:
            on = np.linalg.norm(vecs.reshape(k, g, 3), axis=2).max(1) > 0  # the loaded triangles
            P = m.nodes[tris[on][:, :3]]
            edge = float(np.mean(np.linalg.norm(P - np.roll(P, 1, axis=1), axis=2)))
            info.append((ld, area, bearing_area, edge, pts[on].reshape(-1, 3)))
    if a is not None and a.length > 0:
        f += model.body_force(mat.density * TONNE_PER_MM3, np.array(a) * 1.0)
    return f


def _tet_setup(part, mat, supports, dofs, size, say, notes, need_fixed: bool = True, balanced: bool = False, patches=(),
               moves=()):
    """Mesh, model, stiffness and the CG solver on the free DOFs (a free part: held at three corner nodes)."""
    from . import tetfe

    m = _tet_mesh(part, dofs, size, say, notes, patches)
    model = tetfe.Model(m)
    fixed = _tet_fixed(m, supports, moves)
    _tet_pieces(m, fixed, need_fixed, balanced)
    if balanced:  # at corner nodes (the coarse level needs them), inside the material if it has any
        vtx = np.unique(m.tets[:, :4])
        inner = np.setdiff1d(vtx, np.unique(m.tris))
        cand = inner if len(inner) >= 3 else vtx
        for n, dirs in _three_two_one(m.nodes[cand]):
            for d, c in enumerate("xyz"):
                if c in dirs:
                    fixed[cand[n], d] = True
    say(f"fea: {len(m.tets):,} quadratic tetrahedra ({m.smallest:.2g} to {m.size:.2g} mm), {3 * len(m.nodes):,} DOFs; assembling …")
    K = model.stiffness(mat.E, mat.nu)
    free = ~fixed.reshape(-1)
    solver = tetfe.Solver(m, K, free)
    return m, model, fixed, K, solver


def _surface_stats(m, values: np.ndarray):
    """On the boundary: (vertex node ids, flat triangles, face ids, area per vertex)."""
    used, tri, face = m.surface
    P = m.nodes[used]
    ar = 0.5 * np.linalg.norm(np.cross(P[tri[:, 1]] - P[tri[:, 0]], P[tri[:, 2]] - P[tri[:, 0]]), axis=1)
    area = np.bincount(tri.reshape(-1), weights=np.repeat(ar / 3, 3), minlength=len(used))
    return used, tri, face, area


def _p995(values, area) -> float:
    order = np.argsort(values)
    cum = np.cumsum(area[order])
    return float(values[order[min(int(np.searchsorted(cum, 0.995 * cum[-1])), len(order) - 1)]])


LARGE_ROTATION = 0.15  # rad: past this (or displacements past 10 % of the part's size) linear results drift


def _large_warning(max_disp: float, size: float, rot: float | None) -> list[str]:
    """A linear result whose displacements or rotations are too large for linear theory."""
    big_u, big_r = max_disp > 0.1 * size, rot is not None and rot > LARGE_ROTATION
    if not (big_u or big_r):
        return []
    what = " and ".join(([f"rotations up to {rot:.2g} rad ({math.degrees(rot):.0f}°)"] if big_r else [])
                        + ([f"displacements of {max_disp:.3g} mm ({100 * max_disp / size:.0f} % of the part's size)"] if big_u else []))
    return [f"{what}: past where linear theory holds (it stiffens or softens with the shape; a cantilever at 0.5 rad "
            f"reads about 10 % too far): run again with nonlinear=True"]


def _static_tet(part, mat, supports, resolved, a, dofs, size, say, t_start, tol=1e-9, keep: dict | None = None,
                balanced: bool = False, moves=(), nonlinear: bool = False, steps: int | None = None):
    notes: list[str] = []
    patches = [(ld.at, ld.r) for ld, _ in resolved if ld.at is not None]
    m, model, fixed, K, solver = _tet_setup(part, mat, supports, dofs, size, say, notes, balanced=balanced, patches=patches,
                                            moves=moves)
    if keep is not None:  # buckling goes on with them
        keep.update(model=model, solver=solver)
    _, presc, which = _tet_constraints(m, supports, moves)
    if balanced:
        presc, which = np.zeros_like(presc), np.full_like(which, -1)
    info: list = []
    f = _tet_loads(m, model, resolved, mat, a, info)
    if balanced:  # each node's share of the volume: inertia relief's mass and the mean position's weights
        vol = model.wdet.sum(1)
        wts = np.maximum(np.bincount(m.tets.reshape(-1), weights=np.repeat(vol / 10, 10), minlength=len(m.nodes)), 1e-30)
        L = float(np.array(part.size).max())
        f, R, M = _relieve(m.nodes, f, wts, L, float(part.volume * mat.density / 1000))
        if np.linalg.norm(R) + np.linalg.norm(M) / L > 1e-6 * float(np.linalg.norm(f, axis=1).sum()):
            notes.append(f"the loads were out of balance by {_vec(R)} N, {_vec(M)} N·mm: inertia relief took it up")
    applied = f.sum(0)
    total_abs = float(np.linalg.norm(f, axis=1).sum())
    on_fixed = 0.0 if balanced else float(np.linalg.norm(f * fixed, axis=1).sum())
    free = solver.free
    fixed_flat = fixed.reshape(-1)
    uc = presc.reshape(-1)
    move_dofs = [np.flatnonzero(which.reshape(-1) == j) for j in range(len(moves))]
    fl = f.reshape(-1)
    t_solve = time.time()
    path, history, failure, lin_max = (), None, None, None
    if nonlinear:
        nl = _newton(model, solver, K, fl, uc, fixed_flat, move_dofs, mat, steps, say)
        u, fint, iters, residual = nl["u"], nl["fint"], nl["cg"], nl["residual"]
        path, history, failure, lin_max = nl["steps"], nl["history"], nl["failure"], nl["linear"]
        frac = path[-1].fraction if path else 0.0
        react = (fint - frac * fl).reshape(-1, 3) * fixed
        applied = applied * frac
        f = f * frac
        total_abs *= frac
        notes += nl["notes"]
    else:
        say(f"fea: solving for {int(free.sum()):,} unknowns (conjugate gradients; {solver.pre.coarse:,} on the linear tetrahedra) …")
        b = fl[free] - (K @ uc)[free] if moves else fl[free]
        x, iters, res = solver.solve(b, tol)
        if not res <= 10 * tol:
            raise TuleError(f"the solve did not converge (residual {res:.1e} after {iters} iterations)", op="fea",
                            hint="is every piece held against sliding and turning? fea.fix(faces, \"z\") alone lets a part "
                                 "slide sideways; fix more faces or more directions")
        say(f"fea: solved in {iters} CG iterations, {time.time() - t_solve:.1f} s")
        residual = float(np.linalg.norm(solver.K @ x - b)) / max(float(np.linalg.norm(b)), 1e-300)
        u = uc.copy()
        u[free] = x
        react = (K @ u - fl).reshape(-1, 3) * fixed
    move_forces = tuple(Vec(*np.bincount(d % 3, weights=react.reshape(-1)[d], minlength=3)) for d in move_dofs)
    reaction = react.sum(0) - np.sum([tuple(v) for v in move_forces] or [(0, 0, 0)], axis=0)  # at the supports
    scale = max(total_abs, float(np.linalg.norm(react, axis=1).sum()), 1e-300)
    imbalance = float(np.linalg.norm(react.sum(0) + applied)) / scale
    u = u.reshape(-1, 3)
    if balanced:  # displacements relative to the part's mean position
        u = u - _rigid(m.nodes, u, wts)
    gp = model.cauchy(u, mat.E, mat.nu) if nonlinear else model.gauss_stress(u, mat.E, mat.nu)
    sigma = model.nodal(gp)
    vm = von_mises(sigma.T)
    used, tri, face, area = _surface_stats(m, vm)
    s_vm = vm[used]
    i_max = int(np.argmax(s_vm))
    dmag = np.linalg.norm(u, axis=1)
    j_max = int(np.argmax(dmag))
    # red flags
    peak, node = float(s_vm[i_max]), int(used[i_max])
    p995 = _p995(s_vm, area)
    els = m.tets[(m.tets == node).any(1)][:, :4]
    h = float(np.mean([np.linalg.norm(m.nodes[els[:, i]] - m.nodes[els[:, j]], axis=1).mean() for i, j in ((0, 1), (0, 2), (0, 3), (1, 2), (1, 3), (2, 3))]))
    fixed_pts = [m.nodes[np.unique(m.tris[np.isin(m.tri_face, sel.ids)])] for sel, *_ in list(supports) + list(moves)]
    where = _near(m.nodes[node], fixed_pts, [p for *_, p in info], 2 * h)
    warnings = _common_warnings(residual, tol if not nonlinear else NL_RTOL, 0.0 if balanced else imbalance,
                                on_fixed / max(total_abs, 1e-300))
    warnings += _peak_warning(peak, m.nodes[node], p995, where, mat.yield_strength)
    for ld, ar, bear, edge, _ in info:
        warnings += _load_warnings(ld, ar, bear, edge, "elements", mat.yield_strength)
    if not nonlinear and not balanced:
        rot = model.rotation(u)
        warnings += _large_warning(float(dmag[j_max]), float(np.array(part.size).max()), float(np.percentile(rot, 99.9)))
    if nonlinear and failure is None:
        strain = nl["strain"]
        if strain > 0.05:
            warnings.append(f"strains reach {100 * strain:.0f} %: past small strain (the model is St. Venant–Kirchhoff, "
                            f"fine to a few %) and past yield for most materials")
    result = TetResult(
        part=part, material=mat, supports=tuple(supports), loads=tuple(resolved), accel=a, mesh=m,
        stress=vm.astype(np.float32), stress_tensor=sigma.astype(np.float32), displacement=u,
        max_stress=peak, max_stress_at=Vec(*m.nodes[node]), stress_p995=p995,
        max_displacement=float(dmag[j_max]), max_displacement_at=Vec(*m.nodes[j_max]),
        applied=Vec(*applied), reaction=Vec(*reaction), imbalance=imbalance,
        mass=float(part.volume * mat.density / 1000), elements=len(m.tets), dofs=int(free.sum()),
        cg_iterations=int(iters), seconds=time.time() - t_start, device="cpu", notes=tuple(notes),
        warnings=tuple(warnings), balanced=balanced, _gauss=gp, moves=tuple(moves), move_forces=move_forces,
        nonlinear=nonlinear, steps=tuple(path), linear_max_displacement=lin_max, _history=history,
    )
    if failure is not None:
        err = TuleError(failure["message"], op="fea.static", hint=failure["hint"], **failure["data"])
        err.result = result if path else None  # the last converged step
        raise err
    return result


# ---------------------------------------------------------------- large displacements

NL_RTOL = 1e-5  # Newton converges when the out-of-balance forces are under this share of the forces in play
NL_MAXIT = 12  # Newton iterations per step before it is cut
NL_MIN_STEP = 1e-3  # the smallest load step (a fraction of the loads) before giving up


@dataclass(frozen=True, repr=False)
class Step:
    """A converged load step of ``fea.static(nonlinear=True)``: ``fraction`` of the loads and
    moves, ``max_displacement`` (mm), ``reaction`` (at the supports, total N), ``move_forces`` (the force each
    ``fea.move`` applies to the part, N) and its Newton ``iterations``."""

    fraction: float
    max_displacement: float
    reaction: Vec
    move_forces: tuple
    iterations: int

    def __str__(self) -> str:
        mf = "".join(f", move {_vec(v)} N" for v in self.move_forces)
        return f"Step({self.fraction:.4g} of the loads: max displacement {self.max_displacement:.4g} mm{mf}, " \
               f"reaction {_vec(self.reaction)} N, {self.iterations} iterations)"

    __repr__ = __str__


def _softest(A, B, M, n: int, iterations: int = 15) -> float:
    """The smallest μ of ``A x = μ B x`` found by LOBPCG in a few iterations (B and M positive
    definite): each Rayleigh quotient bounds it from above, so a negative one proves that the
    tangent ``A`` has a direction of negative stiffness."""
    import warnings as _w

    from scipy.sparse.linalg import LinearOperator, lobpcg

    def op(mv):
        return LinearOperator((n, n), matvec=lambda x: mv(np.ravel(x)), dtype=np.float64)

    X = np.random.default_rng(0).standard_normal((n, 1))
    with _w.catch_warnings():
        _w.simplefilter("ignore")
        _, _, hist = lobpcg(op(A.__matmul__), X, B=op(B.__matmul__), M=op(M), largest=False, tol=1e-30,
                            maxiter=iterations, retLambdaHistory=True)
    return float(min(np.min(h) for h in hist))  # (LOBPCG returns its best-converged iterate, not the lowest)


def _newton(model, solver, K, f, uc, fixed, move_dofs, mat, steps, say) -> dict:
    """Load stepping and Newton–Raphson on the free DOFs (total Lagrangian St. Venant–Kirchhoff).

    The loads ``f`` (3n) and moves ``uc`` (3n, on the constrained DOFs) grow with a load factor
    from 0 to 1. The first step's predictor is the linear solution (scaled); the next ones
    extrapolate the last increment. Each Newton correction is a PCG solve with the tangent
    (inexact: to a tenth of the current residual); the coarse factor of the preconditioner is
    kept while it still works and refactored when CG slows. A step that fails (diverges, too many
    iterations, negative stiffness, an inverted element) is halved, and grows again after easy
    ones. At the end the tangent's softest direction is checked: negative stiffness means the
    solved path passed a buckling or snap-through point."""
    import copy

    from . import tetfe

    free = solver.free
    E, nu = mat.E, mat.nu
    n3 = len(f)
    pre0 = copy.copy(solver.pre)  # the linear stiffness and its preconditioner (the stability check's metric)
    K0f = solver.K
    notes: list[str] = []
    driven = bool(fixed[uc != 0].any())
    what = ("loads and moves" if np.any(f) else "moves") if driven else "loads"
    reduce = model.reducer(free)
    b = f[free] - (K @ uc)[free]
    x, cg, res = solver.solve(b, 1e-8)
    if solver.negative or not res <= 1e-6:
        raise TuleError(f"the linear solve did not converge (residual {res:.1e})", op="fea",
                        hint="is every piece held against sliding and turning? fix more faces or more directions")
    u_lin = uc.copy()
    u_lin[free] = x
    lin_max = float(np.linalg.norm(u_lin.reshape(-1, 3), axis=1).max())
    theta = float(np.percentile(model.rotation(u_lin.reshape(-1, 3)), 99.9))
    dmax = 1.0 / steps if steps else 0.25
    dlam = dmax if steps else float(np.clip(0.25 / max(theta, 1e-12), 0.02, dmax))
    say(f"fea: nonlinear: linear theory gives {lin_max:.3g} mm and rotations to {theta:.2g} rad; "
        f"load steps from {100 * dlam:.3g} % …")
    u = np.zeros(n3)
    lam = 0.0
    rec: list = []  # (fraction, u (n, 3) float32, reactions (3n), iterations)
    newton = 0
    fails = 0
    refactor = False
    base_rate = None
    failure = None
    state_last, Kt_last, fint_last, strain = None, None, np.zeros(n3), 0.0
    tangent_next = False  # the next attempt starts from the tangent, not an extrapolation
    misses = 0
    while lam < 1 - 1e-12:
        dlam = 1 - lam if lam + 1.25 * dlam >= 1 else dlam  # no sliver of a last step
        target = lam + dlam
        if tangent_next and rec:  # the tangent at the last converged state, from there (Euler)
            dc = np.zeros(n3)
            dc[fixed] = dlam * uc[fixed]
            Kt_last = model.stiffness_at(state_last) if Kt_last is None else Kt_last
            rhs = (target * f - fint_last - model._bsr(Kt_last) @ dc)[free]
            solver.update(reduce(Kt_last), reduced=True)
            du, its, _ = solver.solve(rhs, 1e-3, maxiter=1000)
            cg += its
            u_try = u + dc
            u_try[free] += du
        else:
            u_try = _predict(rec, u_lin, target)
            u_try[fixed] = target * uc[fixed]
        ok, reason, r0 = False, "", None
        for it in range(NL_MAXIT + 1):
            fint, state, (detmin, emax) = model.internal(u_try.reshape(-1, 3), E, nu)
            fint = fint.reshape(-1)
            R = target * f - fint
            ref = max(float(np.linalg.norm(target * f)), float(np.linalg.norm(fint)), 1e-300)
            rn = float(np.linalg.norm(R[free])) / ref
            if not math.isfinite(rn):
                reason = "it diverged"
                break
            if rn <= NL_RTOL:
                ok = detmin > 0
                reason = "" if ok else "elements turned inside out"
                break
            if it == NL_MAXIT:
                reason = f"not converged in {NL_MAXIT} iterations"
                break
            r0 = rn if r0 is None else r0
            if rn > 1e3 * r0:
                reason = "it diverged"
                break
            try:
                solver.update(reduce(model.stiffness_at(state)), refactor=refactor, reduced=True)
            except RuntimeError:  # the coarse factor of an indefinite tangent
                reason = "the stiffness turned negative"
                break
            if refactor:
                refactor, base_rate = False, None
            eta = min(1e-2, max(1e-10, 0.1 * rn))
            du, its, res = solver.solve(R[free], eta, maxiter=1000)
            if not solver.negative and not res <= 10 * eta:  # the coarse factor is too old: refactor, once
                try:
                    solver.pre.factor()
                except RuntimeError:
                    reason = "the stiffness turned negative"
                    break
                base_rate = None
                cg += its
                du, its, res = solver.solve(R[free], eta, maxiter=1000)
            cg += its
            if solver.negative:
                reason = "the stiffness turned negative"
                break
            if not res <= 10 * eta:
                reason = "the linear solver stalled"
                break
            rate = its / max(math.log10(1 / max(res, 1e-300)), 0.5)
            if base_rate is None:
                base_rate = rate
            elif rate > 2 * base_rate + 2:
                refactor = True
            u_try[free] += du
        newton += it
        if ok:
            u, lam = u_try, target
            state_last, Kt_last, fint_last, strain = state, None, fint, emax  # (the tangent there: when needed)
            react = (fint - lam * f) * fixed
            rec.append((lam, u.reshape(-1, 3).astype(np.float32), react, it))
            say(f"fea:   step {len(rec)}: {100 * lam:.4g} % of the {what}, {it} Newton iteration(s), "
                f"max displacement {float(np.linalg.norm(u.reshape(-1, 3), axis=1).max()):.4g} mm")
            fails = 0
            tangent_next = misses >= 2
            if it <= 3:
                dlam = min(dmax, 1.5 * dlam)
            elif it >= 7:
                dlam *= 0.7
            continue
        fails += 1
        if rec and not tangent_next:  # the extrapolated guess may have been off (it overshoots a turning path):
            tangent_next = True       # the same step again from the tangent (and always, after two such misses)
            misses += 1
            say(f"fea:   a step to {100 * target:.4g} % failed ({reason}): again from the tangent")
            continue
        tangent_next = misses >= 2
        dlam *= 0.25 if reason in ("it diverged", "the stiffness turned negative") else 0.5
        say(f"fea:   a step to {100 * target:.4g} % failed ({reason}): trying {100 * dlam:.3g} %")
        if dlam < NL_MIN_STEP or fails > 16:
            failure = _nl_failure(lam, rec, reason, target - lam, what, driven)
            break
    if failure is None and rec:  # did the path pass a buckling or snap-through point?
        Kt_last = model.stiffness_at(state_last) if Kt_last is None else Kt_last
        n = len(solver.idx)

        def mu_at(Kt):
            return _softest(tetfe.ParCSR(reduce(Kt)), pre0.Am, pre0, n)

        if mu_at(Kt_last) < -1e-6:
            lo, hi = -1, len(rec) - 1  # the last stable step, the first unstable one
            while hi - lo > 1:
                mid = (lo + hi) // 2
                _, Kt, _ = model.tangent(rec[mid][1].astype(np.float64), E, nu, blocks=True)
                lo, hi = (mid, hi) if mu_at(Kt) >= -1e-6 else (lo, mid)
            failure = _nl_unstable(rec, lo, hi, what, driven)
            rec = rec[:lo + 1]  # the result goes back to the last stable step
            u = rec[-1][1].reshape(-1).astype(np.float64) if rec else np.zeros(n3)
            fint_last = model.tangent(u.reshape(-1, 3), E, nu, stiffness=False)[0].reshape(-1) if rec else np.zeros(n3)
    steps_out = []
    for lam_i, ui, react, it in rec:
        mf = tuple(Vec(*np.bincount(d % 3, weights=react[d], minlength=3)) for d in move_dofs)
        support = react.reshape(-1, 3).sum(0) - np.sum([tuple(v) for v in mf] or [(0, 0, 0)], axis=0)
        steps_out.append(Step(float(lam_i), float(np.linalg.norm(ui, axis=1).max()), Vec(*support), mf, int(it)))
    if rec:
        notes.append(f"geometrically nonlinear: {len(rec)} load step(s), {newton} Newton iterations, {cg} CG iterations")
    residual = 0.0
    if rec:
        R = rec[-1][0] * f - fint_last
        residual = float(np.linalg.norm(R[free])) / max(float(np.linalg.norm(rec[-1][0] * f)), float(np.linalg.norm(fint_last)), 1e-300)
    return dict(u=u, fint=fint_last, cg=cg, residual=residual, steps=tuple(steps_out),
                history=np.stack([r[1] for r in rec]) if rec else None, failure=failure, linear=lin_max,
                notes=notes, strain=strain)


def _predict(rec: list, u_lin: np.ndarray, lam: float) -> np.ndarray:
    """The displacements at load fraction ``lam`` extrapolated from the path so far: a parabola
    through the last three points (the origin among them), or through the origin with the
    linear solution as its slope while only one step has converged (a flexure's shortening
    grows with the square of its stroke: a straight-line guess stretches its blades)."""
    if not rec:
        return u_lin * lam
    if len(rec) == 1:
        l1, u1 = rec[0][0], rec[0][1].reshape(-1)
        c = (u1 - u_lin * l1) / l1 ** 2
        return u_lin * lam + c * lam ** 2
    pts = ([(0.0, None)] + [(r[0], r[1]) for r in rec])[-3:]
    out = np.zeros_like(u_lin)
    for i, (li, ui) in enumerate(pts):
        if ui is None:
            continue
        w = 1.0
        for j, (lj, _) in enumerate(pts):
            if j != i:
                w *= (lam - lj) / (li - lj)
        out += w * ui.reshape(-1)
    return out


def _nl_failure(lam: float, rec: list, reason: str, tried: float, what: str, driven: bool) -> dict:
    last = (f"{len(rec)} step(s) converged, the last at {lam:.4g} of the {what} (max displacement "
            f"{float(np.linalg.norm(rec[-1][1], axis=1).max()):.3g} mm)") if rec else "no step converged"
    if reason == "the stiffness turned negative":
        hint = ("the part gives way here: buckling, or snap-through under a force (a limit point). " +
                ("Driven by displacement it can still buckle sideways: a small side load (an imperfection) lets it "
                 "take the buckled path; fea.buckling estimates the load" if driven else
                 "Drive it by displacement (fea.move) to follow it through, or check fea.buckling"))
    elif reason == "elements turned inside out":
        hint = "strains too large for the model here: spread the load over more area, or refine (dofs=, size=)"
    else:
        hint = "more steps= (smaller increments) may get through" + (
            "" if driven else "; if the force–displacement path flattens before here, it is a snap-through: drive it by "
                              "displacement (fea.move) instead")
    return dict(message=f"the nonlinear solve did not converge past {100 * lam:.4g} % of the {what} ({last}); a step of "
                        f"{100 * tried:.2g} % beyond it failed: {reason}",
                hint=hint + ("; error.result is the last converged step" if rec else ""),
                data=dict(last_fraction=lam, steps=len(rec), reason=reason))


def _nl_unstable(rec: list, lo: int, hi: int, what: str, driven: bool) -> dict:
    a = rec[lo][0] if lo >= 0 else 0.0
    b = rec[hi][0]
    return dict(message=f"the part buckles or snaps through between {100 * a:.4g} % and {100 * b:.4g} % of the {what}: its "
                        f"stiffness turns negative there, so the path solved beyond (to {100 * rec[-1][0]:.4g} %) is "
                        f"unstable, not what happens",
                hint=("" if driven else "drive it by displacement (fea.move) to follow a snap-through; ") +
                     "a perfect, symmetric part (a column, an arch) needs a small side load as an imperfection to take the "
                     "buckled path; fea.buckling estimates the load" + ("; error.result is the last stable step" if lo >= 0 else ""),
                data=dict(last_fraction=a, steps=lo + 1, reason="negative stiffness"))


def _surface_field(m, values, **kw) -> dict:
    """An overlay field on the mesh's own boundary (flat triangles, B-rep face ids)."""
    used, tri, face = m.surface
    return {"values": np.asarray(values)[used], "mesh": (m.nodes[used], tri, face), **kw}


@dataclass(frozen=True, repr=False)
class TetResult:
    """A static analysis on quadratic tetrahedra: values at the mesh's nodes (``mesh.nodes``).

    ``stress`` (von Mises, MPa, per node), ``stress_tensor`` (xx yy zz xy yz zx per node),
    ``displacement`` (mm, (n, 3)); ``stress_at(points)`` and ``displacement_at(points)``
    interpolate them. ``max_stress``, ``stress_p995``, ``safety_factor``, ``max_displacement``,
    reactions and mass as for the voxel ``Result``.
    """

    part: Shape
    material: Material
    supports: tuple
    loads: tuple
    accel: Vec | None
    mesh: Any  # tetfe.TetMesh
    stress: np.ndarray
    stress_tensor: np.ndarray
    displacement: np.ndarray
    max_stress: float
    max_stress_at: Vec
    stress_p995: float
    max_displacement: float
    max_displacement_at: Vec
    applied: Vec
    reaction: Vec
    imbalance: float
    mass: float
    elements: int
    dofs: int
    cg_iterations: int
    seconds: float
    device: str
    notes: tuple = ()
    warnings: tuple = ()
    balanced: bool = False
    _gauss: Any = None  # Gauss-point stresses (elements, 4, 6), for buckling
    moves: tuple = ()  # (Faces, dirs, d) per fea.move
    move_forces: tuple = ()  # the force (Vec, N) each fea.move applies to the part: what drives it there
    nonlinear: bool = False
    steps: tuple = ()  # nonlinear: the converged load steps (Step), the path
    linear_max_displacement: float | None = None  # nonlinear: what linear theory gives
    _history: Any = None  # nonlinear: displacements per step (steps, n, 3), float32

    safety_factor = Result.safety_factor

    def _interp(self, values, points) -> np.ndarray:
        from . import tetfe

        p = np.atleast_2d(np.asarray(points, dtype=np.float64))
        if not hasattr(self, "_tree"):
            object.__setattr__(self, "_tree", None)
        e, L, tree = tetfe.locate(self.mesh.nodes, self.mesh.tets, p, self._tree)
        object.__setattr__(self, "_tree", tree)
        return np.einsum("pa,pa...->p...", tetfe.shape(L), np.asarray(values)[self.mesh.tets[e]])

    def stress_at(self, points) -> np.ndarray:
        """Von Mises stress (MPa) at points (``(n, 3)`` or one point)."""
        return self._interp(self.stress, points)

    def displacement_at(self, points, step: int | None = None) -> np.ndarray:
        """Displacement vectors (mm) at points (of the undeformed part); ``step``: at that load step
        of a nonlinear analysis (``steps[step]``) instead of the last."""
        if step is None:
            return self._interp(self.displacement, points)
        if self._history is None:
            raise TuleError("step= is for a nonlinear result", op="fea", hint="fea.static(..., nonlinear=True)")
        return self._interp(self._history[step], points)

    def show(self, name: str, title: str | None = None, deform: float | None = None) -> str:
        """An overlay output: von Mises stress and displacement on the mesh's own surface (thin
        walls and slots drawn as they are), supports, loads and the numbers. Returns the folder.
        A nonlinear result is drawn at its true shape (``deform`` 1) by default."""
        from . import outputs

        faces, arrows = _conditions(self.supports, self.loads, self.moves)
        size = float(np.array(self.part.size).max())
        if deform is None and self.nonlinear:
            deform = 1.0
        if deform is None:
            deform = _nice(0.06 * size / self.max_displacement) if self.max_displacement > 0 else 0.0
        fields = [
            _surface_field(self.mesh, self.stress, label="von Mises stress", unit="MPa", min=0.0,
                           max=_round_up(self.stress_p995),
                           note=f"red: the top 0.5 % of the surface; peak {self.max_stress:.3g} MPa"),
            _surface_field(self.mesh, self.displacement, label="displacement", unit="mm", min=0.0,
                           max=float(self.max_displacement), deform=float(deform)),
        ]
        m = self.mesh
        info = _static_info(self, f"{self.elements:,} quadratic tetrahedra, {m.smallest:.2g} to {m.size:.2g} mm, "
                                  f"{self.dofs:,} DOFs")
        for (sel, _, d), fm in zip(self.moves, self.move_forces):
            info[f"move faces {', '.join(map(str, sel.ids))}"] = f"{_vec(d)} mm takes {_vec(fm)} N"
        if self.nonlinear:
            info["nonlinear"] = (f"{len(self.steps)} load steps; linear theory: max displacement "
                                 f"{self.linear_max_displacement:.3g} mm")
        return outputs.overlay(name, self.part, faces, arrows=arrows, info=info, fields=fields,
                               title=title or f"Static analysis: {self.part.name or name}, {self.material.name}")

    def __str__(self) -> str:
        m = self.material
        lines = [
            f"fea.static: {self.part.name or 'part'} in {m.name} (E {m.E:g} MPa, yield {m.yield_strength:g} MPa), "
            f"{self.elements:,} quadratic tetrahedra ({self.mesh.smallest:.2g} to {self.mesh.size:.2g} mm), "
            f"{self.dofs:,} DOFs, CG {self.cg_iterations} iterations, {self.seconds:.1f} s on {self.device}",
            f"  von Mises stress: peak {self.max_stress:.4g} MPa at {_pt(self.max_stress_at)}; "
            f"99.5th percentile {self.stress_p995:.4g} MPa (surface)",
            f"  safety factor against yield: {self.safety_factor:.3g} on the peak, "
            f"{m.yield_strength / self.stress_p995 if self.stress_p995 > 0 else math.inf:.3g} on the 99.5th percentile",
            f"  max displacement {self.max_displacement:.4g} mm at {_pt(self.max_displacement_at)}"
            + (f" (linear theory: {self.linear_max_displacement:.4g} mm)" if self.nonlinear and self.linear_max_displacement else ""),
            _balance_line(self),
        ]
        for (sel, dirs, d), fm in zip(self.moves, self.move_forces):
            lines.append(f"  fea.move faces {list(sel.ids)} by {_vec(d)} mm{'' if dirs == 'xyz' else f' (held in {dirs})'}: "
                         f"it takes {_vec(fm)} N")
        if self.nonlinear:
            lines.append(f"  geometrically nonlinear (large displacements, St. Venant–Kirchhoff, dead loads): "
                         f"{len(self.steps)} load steps; points are the undeformed part's")
            lines += _path_table(self.steps)
        lines.append(f"  mass {self.mass:.4g} g")
        lines += [f"  WARNING: {w}" for w in self.warnings]
        lines += [f"  note: {n}" for n in self.notes]
        return "\n".join(lines)

    __repr__ = __str__


def _path_table(steps, rows: int = 10) -> list[str]:
    """The load path, at most ``rows`` steps (evenly picked, the last always)."""
    if not steps:
        return []
    k = len(steps)
    pick = sorted(set(np.round(np.linspace(0, k - 1, min(rows, k))).astype(int).tolist()))
    head = "    fraction  max displ (mm)  " + ("force on each fea.move (N)" if steps[0].move_forces else "reaction (N)")
    out = [head]
    for i in pick:
        st = steps[i]
        val = "  ".join(_vec(v) for v in st.move_forces) if st.move_forces else _vec(st.reaction)
        out.append(f"    {st.fraction:8.4g}  {st.max_displacement:14.4g}  {val}")
    return out


def _shape_fields(m, shapes, labels, size: float, deform: float | None) -> list[dict]:
    """A field per mode: its shape (largest motion 1) on the surface, drawn moving."""
    d = float(deform) if deform is not None else _nice(0.08 * size)
    return [_surface_field(m, s, label=lab, unit="", min=0.0, max=1.0, deform=d, animate=True,
                           note="relative motion (the largest is 1); the amplitude is arbitrary")
            for s, lab in zip(shapes, labels)]


def _normalized(m, v: np.ndarray, free: np.ndarray) -> np.ndarray:
    """An eigenvector on the free DOFs as (n, 3), scaled to a largest motion of 1 (its sign: the largest component positive)."""
    u = np.zeros(3 * len(m.nodes))
    u[free] = v
    u = u.reshape(-1, 3)
    mag = np.linalg.norm(u, axis=1)
    k = int(np.argmax(mag))
    s = mag[k] if mag[k] > 0 else 1.0
    c = int(np.argmax(np.abs(u[k])))
    return (u / s * (1.0 if u[k, c] >= 0 else -1.0)).astype(np.float32)


@dataclass(frozen=True, repr=False)
class Modes:
    """Natural frequencies (Hz) and mode shapes ((k, n, 3) at the mesh's nodes, largest motion 1).
    ``effective_mass`` (k, 3): each mode's share of the part's mass moving with it along x, y, z."""

    part: Shape
    material: Material
    supports: tuple
    mesh: Any
    frequencies: np.ndarray
    shapes: np.ndarray
    effective_mass: np.ndarray
    mass: float
    elements: int
    dofs: int
    seconds: float
    notes: tuple = ()

    def show(self, name: str, title: str | None = None, deform: float | None = None) -> str:
        """An overlay output: a field per mode (the buttons under the view), each drawn swinging
        through its shape; ``deform`` sets the drawn amplitude (mm for the largest motion)."""
        from . import outputs

        faces, _ = _conditions(self.supports, ())
        labels = [f"mode {i + 1}: {f:.4g} Hz" for i, f in enumerate(self.frequencies)]
        fields = _shape_fields(self.mesh, self.shapes, labels, float(np.array(self.part.size).max()), deform)
        info = {"material": str(self.material.name), "mass": f"{self.mass:.3g} g"}
        for i, f in enumerate(self.frequencies):
            info[f"mode {i + 1}"] = f"{f:.4g} Hz, {_moves(self.effective_mass[i])}"
        info["mesh"] = f"{self.elements:,} quadratic tetrahedra, {self.dofs:,} DOFs"
        return outputs.overlay(name, self.part, faces, info=info, fields=fields,
                               title=title or f"Vibration modes: {self.part.name or name}, {self.material.name}")

    def __str__(self) -> str:
        m = self.material
        lines = [f"fea.modes: {self.part.name or 'part'} in {m.name} (E {m.E:g} MPa, {m.density:g} g/cm³), "
                 f"{self.elements:,} quadratic tetrahedra, {self.dofs:,} DOFs, {self.seconds:.1f} s"]
        for i, f in enumerate(self.frequencies):
            lines.append(f"  mode {i + 1}: {f:10.4g} Hz   {_moves(self.effective_mass[i])}")
        lines += [f"  note: {n}" for n in self.notes]
        return "\n".join(lines)

    __repr__ = __str__


def _moves(frac) -> str:
    """'moves along x (45 % of the mass)' from effective mass fractions."""
    parts = [f"{c} {100 * v:.0f} %" for c, v in zip("xyz", frac)]
    k = int(np.argmax(frac))
    lead = f"moves along {'xyz'[k]}" if frac[k] >= 0.05 else "bends or twists in place"
    return f"{lead} (effective mass {', '.join(parts)})"


def modes(
    part: Shape,
    material: str | Material = "aluminium 6061-T6",
    *,
    fixed,
    n: int = 6,
    dofs: int = 100_000,
    size: float | None = None,
    verbose: bool = True,
) -> Modes:
    """The ``n`` lowest natural frequencies (Hz) and mode shapes of ``part`` held at ``fixed``
    (as for ``static``), on quadratic tetrahedra (needs gmsh). Print it; ``.show(name)`` draws the
    shapes. ``dofs``/``size`` as for ``static(mesh="tet")``: frequencies converge sooner than
    stresses, so the default budget is 100,000."""
    from scipy.sparse.linalg import eigsh

    from . import tetfe

    t_start = time.time()
    say = (lambda msg: print(f"{msg} [{time.time() - t_start:.1f} s]", flush=True)) if verbose else (lambda *a, **k: None)
    part, mat, supports, _, _ = _inputs(part, material, fixed, (), None, "fea.modes")
    if not mat.density > 0:
        raise TuleError("modes need a density", op="fea", hint="fea.Material(..., density=1.2)")
    n = int(n)
    if not 1 <= n <= 50:
        raise TuleError(f"n={n}", op="fea.modes", hint="1 to 50 modes")
    notes: list[str] = []
    m, model, fixed_mask, K, solver = _tet_setup(part, mat, supports, dofs, size, say, notes)
    rho = mat.density * TONNE_PER_MM3
    Mfull = model.mass(rho)
    idx = solver.idx
    M = Mfull[idx][:, idx].tocsr()
    say(f"fea: {n} modes of {len(idx):,} unknowns (shift-invert Lanczos) …")
    w, v = _eigsh(eigsh, solver.K, n, M=M, sigma=0, which="LM", OPinv=solver.operator(1e-8), tol=1e-9)
    order = np.argsort(w)
    w, v = w[order], v[:, order]
    freqs = np.sqrt(np.maximum(w, 0)) / (2 * np.pi)
    total = np.array([float(np.ones(len(m.nodes)) @ Mfull[c::3, c::3] @ np.ones(len(m.nodes))) for c in range(3)])
    eff = np.zeros((len(w), 3))
    for k in range(len(w)):
        phi = v[:, k] / math.sqrt(max(float(v[:, k] @ (M @ v[:, k])), 1e-300))
        Mphi = M @ phi
        for c in range(3):
            r = np.zeros(3 * len(m.nodes))
            r[c::3] = 1.0
            eff[k, c] = float(r[idx] @ Mphi) ** 2 / total[c]
    shapes = np.stack([_normalized(m, v[:, k], solver.free) for k in range(len(w))])
    say(f"fea: modes in {solver.solves} solves, {solver.iterations} CG iterations")
    return Modes(part=part, material=mat, supports=tuple(supports), mesh=m, frequencies=freqs, shapes=shapes,
                 effective_mass=eff, mass=float(part.volume * mat.density / 1000), elements=len(m.tets),
                 dofs=len(idx), seconds=time.time() - t_start, notes=tuple(notes))


def _eigsh(eigsh, A, k, **kw):
    from scipy.sparse.linalg import ArpackNoConvergence

    if k >= A.shape[0] - 1:
        raise TuleError(f"{k} modes of a model with {A.shape[0]} unknowns", op="fea", hint="fewer modes, or more dofs=")
    try:
        return eigsh(A, k=k, **kw)
    except ArpackNoConvergence as e:
        raise TuleError("the eigenvalue solver did not converge", op="fea",
                        hint="is the part held against every rigid motion? fewer modes (n=) converge sooner") from e


@dataclass(frozen=True, repr=False)
class Buckling:
    """Linear (eigenvalue) buckling: ``factors``, the multiples of the loads at which the part
    buckles (lowest first), their ``shapes`` ((k, n, 3), largest motion 1), and the ``static``
    result (a ``TetResult``) of the loads as given."""

    part: Shape
    material: Material
    supports: tuple
    loads: tuple
    static: TetResult
    factors: np.ndarray
    shapes: np.ndarray
    seconds: float
    notes: tuple = ()

    @property
    def factor(self) -> float:
        """The lowest load factor: the loads times this buckle the part (inf: none found)."""
        return float(self.factors[0]) if len(self.factors) else math.inf

    def show(self, name: str, title: str | None = None, deform: float | None = None) -> str:
        """An overlay output: a field per buckling mode, drawn moving through its shape; supports and loads."""
        from . import outputs

        faces, arrows = _conditions(self.supports, self.loads)
        labels = [f"mode {i + 1}: load × {f:.3g}" for i, f in enumerate(self.factors)]
        fields = _shape_fields(self.static.mesh, self.shapes, labels, float(np.array(self.part.size).max()), deform)
        info = {"material": str(self.material.name)}
        for i, f in enumerate(self.factors):
            info[f"mode {i + 1}"] = f"buckles at {f:.4g} × the loads"
        info["static, 99.5th pct"] = f"{self.static.stress_p995:.3g} MPa (yield {self.material.yield_strength:g} MPa)"
        info["mesh"] = f"{self.static.elements:,} quadratic tetrahedra, {self.static.dofs:,} DOFs"
        return outputs.overlay(name, self.part, faces, arrows=arrows, info=info, fields=fields,
                               title=title or f"Buckling: {self.part.name or name}, {self.material.name}")

    def __str__(self) -> str:
        lines = [f"fea.buckling: {self.part.name or 'part'} in {self.material.name}, {self.static.elements:,} quadratic "
                 f"tetrahedra, {self.static.dofs:,} DOFs, {self.seconds:.1f} s"]
        if not len(self.factors):
            lines.append("  no buckling mode found under these loads (too little in compression)")
        for i, f in enumerate(self.factors):
            lines.append(f"  mode {i + 1}: load factor {f:.4g} (the loads × {f:.4g} buckle it)")
        if len(self.factors) and self.static.stress_p995 * self.factors[0] > self.material.yield_strength:
            lines.append(f"  note: at {self.factors[0]:.3g} × the loads the stress ({self.static.stress_p995 * self.factors[0]:.3g} MPa, "
                         f"99.5th pct) is past yield ({self.material.yield_strength:g} MPa): it yields before it buckles")
        lines += [f"  note: {n}" for n in self.notes]
        return "\n".join(lines)

    __repr__ = __str__


def buckling(
    part: Shape,
    material: str | Material = "aluminium 6061-T6",
    *,
    fixed,
    loads: Sequence[Load] = (),
    accel=None,
    n: int = 4,
    dofs: int = 100_000,
    size: float | None = None,
    verbose: bool = True,
) -> Buckling:
    """Linear buckling of ``part`` under ``loads`` (and ``accel``), held at ``fixed`` (as for
    ``static``), on quadratic tetrahedra (needs gmsh): the ``n`` lowest load factors and shapes.
    A factor of 3 means three times the loads buckle the perfect part; real parts (imperfect,
    plastic) buckle sooner, so keep a margin."""
    from scipy.sparse.linalg import eigsh

    t_start = time.time()
    say = (lambda msg: print(f"{msg} [{time.time() - t_start:.1f} s]", flush=True)) if verbose else (lambda *a, **k: None)
    part, mat, supports, resolved, a = _inputs(part, material, fixed, loads, accel, "fea.buckling")
    if not resolved and (a is None or a.length == 0):
        raise TuleError("no loads", op="fea", hint='loads=[fea.force("top", (0, 0, -1000))]')
    keep: dict = {}
    st = _static_tet(part, mat, supports, resolved, a, dofs, size, say, t_start, keep=keep)
    _say_warnings(st.warnings)
    m, model, solver = st.mesh, keep["model"], keep["solver"]
    Kg = model.geometric(st._gauss)
    idx = solver.idx
    G = -Kg[idx][:, idx].tocsr()
    say(f"fea: {n} buckling modes (Lanczos on K⁻¹K_σ) …")
    # the n eigenvalues μ = 1/factor of largest magnitude: they converge fast at either sign (the
    # wanted positive end alone does not when little is in compression: it is a cluster at 0). The
    # positive ones among them are the lowest factors under the loads; negative ones buckle under
    # the reversed loads.
    mu, v = _eigsh(eigsh, G, int(n), M=solver.K, Minv=solver.operator(1e-8), which="LM", tol=1e-9)
    big = float(np.abs(mu).max()) if len(mu) else 0.0
    pos = mu > 1e-9 * big
    order = np.flatnonzero(pos)[np.argsort(-mu[pos])]
    factors = 1 / mu[order]
    shapes = np.stack([_normalized(m, v[:, k], solver.free) for k in order]) if len(order) else np.zeros((0, len(m.nodes), 3))
    notes = list(st.notes)
    if len(order) < len(mu) and big > 0:
        note = ("no buckling mode" if not len(order) else f"only {len(order)} buckling mode(s)") + \
            f" below {1 / np.abs(mu).min():.3g} × the loads"
        notes.append(note + (f"; reversed, the loads buckle it at {-1 / mu.min():.3g} ×" if mu.min() < 0 else ""))
    return Buckling(part=part, material=mat, supports=tuple(supports), loads=tuple(resolved), static=st,
                    factors=factors, shapes=shapes, seconds=time.time() - t_start, notes=tuple(notes))
