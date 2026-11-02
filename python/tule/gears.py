"""Involute spur gears: external, internal (ring) and the pair that meshes them; cycloidal drives.

```python
from tule import *

pair = gear_pair(1.5, 18, 54)                         # center distance, contact ratio; raises if they interfere
pinion = spur_gear(1.5, 18, 10, bore=6, backlash=0.05)
wheel = pair.place(spur_gear(1.5, 54, 10, backlash=0.05))   # at pair.distance on +x, turned to mesh
```

Teeth are cut as a standard rack (DIN 867: addendum 1 m, dedendum 1.25 m,
tip radius 0.38 m) would cut them: involute flanks (one B-spline each,
within 0.0001 module of the involute), root fillets that are the trochoid
the rack's rounded tip leaves (so small tooth counts come out undercut, as
they would be made) and a circular tip. Each tooth is four faces: two
``flanks``, a ``tips`` face and a ``roots`` face (fillets and root circle in
one), so a 40-tooth gear is under 1 MB of STEP.

An external gear has a tooth centered on +x; a ring gear has a tooth
(pointing inward) centered on +x. ``profile_shift`` x moves the cutter out
by x·module: a positive shift thickens the teeth and avoids undercut. For a
ring gear a positive x moves the teeth outward (its tip circle grows), so a
planet and a ring with the same x mesh at the standard center distance.
``backlash`` thins each tooth (an external gear's tooth, a ring gear's tooth)
by that much along the pitch circle; a pair's backlash is the sum.

A cycloidal drive (``cycloidal_drive(26, 80, 5, 1.2, 8)``: 26 pins on an 80
circle, a 25-lobe disc on a 1.2 crank) has ``disc``, ``pins``, ``ratio`` and
``place(disc, angle)``; ``cycloidal_disc`` is the disc alone.

Straight bevel gears are in ``tule.bevel`` (``bevel_gear``, ``bevel_pair``).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeEdge, BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakeVertex, BRepBuilderAPI_MakeWire
from OCP.BRep import BRep_Builder
from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder, BRepPrimAPI_MakePrism
from OCP.GC import GC_MakeArcOfCircle
from OCP.Approx import Approx_ParametrizationType
from OCP.GeomAbs import GeomAbs_Shape
from OCP.GeomAPI import GeomAPI_PointsToBSpline
from OCP.Geom import Geom_BSplineCurve
from OCP.gp import gp_Ax1, gp_Ax2, gp_Circ, gp_Dir, gp_Pnt, gp_Vec
from OCP.TopoDS import TopoDS_Compound

try:  # OCP 8
    from OCP.collections import Array1_gp_Pnt as TColgp_Array1OfPnt  # type: ignore
except ImportError:  # OCP 7.x
    from OCP.TColgp import TColgp_Array1OfPnt  # type: ignore

from ._occ import cast, from_list, kind_of, subshapes
from .errors import TuleError
from .profile import _quarter_turns
from .shape import Shape

__all__ = ["CycloidalDrive", "GearPair", "cycloidal_disc", "cycloidal_drive", "gear_pair", "internal_gear", "spur_gear"]

_FLANK_POINTS = 40   # exact samples per involute flank, fitted by a B-spline within _TOL
_ROOT_POINTS = 30    # per fillet (the root curve runs fillet, root circle, fillet)
_TOL = 1e-4          # fit tolerance, in modules (0.1 µm on a module-1 gear)
_SLACK = 0.02        # interference shallower than this (in modules) is cleared by backlash


def _inv(a: float) -> float:
    return math.tan(a) - a


def _inv_inverse(v: float) -> float:
    """The angle whose involute function is ``v``."""
    a = (3 * v) ** (1 / 3) if v > 0 else 1e-6
    for _ in range(50):
        f = _inv(a) - v
        a -= f / math.tan(a) ** 2
        if abs(f) < 1e-15:
            break
    return a


def _check(module, teeth, pressure_angle, backlash, op):
    if module <= 0:
        raise TuleError("module must be positive", op=op)
    if int(teeth) != teeth or teeth < 5:
        raise TuleError(f"a gear needs a whole number of teeth ≥ 5, not {teeth}", op=op)
    if not 10 <= pressure_angle <= 35:
        raise TuleError(f"pressure angle {pressure_angle}° is outside 10°–35°", op=op, hint="20° is the standard")
    if backlash < 0:
        raise TuleError("backlash must be ≥ 0", op=op)


# ---------------------------------------------------------------- tooth geometry
# Polar coordinates (radius, angle) of the upper half of the tooth centered on +x:
# ``root`` runs from the middle of the space (angle pi/z) to the start of the
# involute; ``flank`` from there to the tip. The angle at a radius is the
# tooth's half-angle there.


class _External:
    def __init__(self, m, z, pa, x, backlash, op="spur_gear"):
        _check(m, z, pa, backlash, op)
        self.m, self.z, self.x, self.backlash = m, int(z), x, backlash
        a = self.alpha = math.radians(pa)
        r = self.r = m * z / 2
        self.rb = r * math.cos(a)
        self.ra = r + m * (1 + x)
        self.rf = r - m * (1.25 - x)
        if self.rf <= 0.1 * m:
            raise TuleError("the root circle is too small", op=op, hint="use more teeth or a larger profile_shift")
        self.psi = (m * (math.pi / 2 + 2 * x * math.tan(a)) - backlash) / (2 * r)  # half-angle at the pitch circle
        if self.ra <= self.rb:
            raise TuleError("the tip circle lies inside the base circle", op=op, hint="use a larger profile_shift")
        tip = 2 * self.ra * self.half_angle(self.ra)
        if tip < 0.1 * m:
            raise TuleError(f"the teeth come to a point (tip thickness {tip:.3g})", op=op,
                            hint="use a smaller profile_shift or more teeth", tip_thickness=tip)
        self._cut()

    def half_angle(self, rho):
        """Half-angle of the involute tooth at radius ``rho`` (≥ the base circle)."""
        return self.psi + _inv(self.alpha) - _inv(math.acos(min(1.0, self.rb / rho)))

    def _cut(self):
        """Generate the root with the rack: fillet trochoid, form radius, undercut."""
        m, a, r, z = self.m, self.alpha, self.r, self.z
        ta, ca = math.tan(a), math.cos(a)
        half = math.pi * m / 4 + self.backlash / 2  # the rack tooth's half-width on its reference line
        rho = min(0.38 * m, max(0.0, (half - 1.25 * m * ta) / (1 / ca - ta)))
        vc = self.x * m - 1.25 * m + rho                     # tip-fillet center, radial (rack frame)
        uc = max(0.0, half + (vc - self.x * m) * ta - rho / ca)  # and tangential
        self.fillet_radius = rho

        def troch(phi):
            phi = np.asarray(phi, float)
            cx, cy = r + vc, uc + r * phi                     # fillet center, fixed frame
            dx, dy = cx - r, cy                               # from the pitch point
            d = np.hypot(dx, dy)
            qx, qy = cx + rho * dx / d, cy + rho * dy / d     # contact point (Willis)
            c, s = np.cos(-phi), np.sin(-phi)                 # into the gear frame
            gx, gy = c * qx - s * qy, s * qx + c * qy
            return np.hypot(gx, gy), math.pi / z - np.arctan2(gy, gx)  # tooth half-angle

        phi0 = -uc / r
        phi1 = (-vc / ta - uc) / r                            # the fillet meets the straight flank
        if rho == 0:
            phi1 = phi0 + 1e-9
        grid = np.linspace(phi0, phi1, 801)
        rr, th = troch(grid)
        inv_hw = np.array([self.half_angle(q) if q > self.rb else np.nan for q in rr])
        cut = (inv_hw - th) * rr > 1e-7 * m                   # the fillet bites into the involute
        self.undercut = bool(np.any(cut))
        if self.undercut:
            # the profile follows the trochoid up to where it crosses the involute again
            i = int(np.nonzero(cut)[0][-1])
            lo, hi = grid[i], grid[min(i + 1, len(grid) - 1)]
            for _ in range(60):
                mid = (lo + hi) / 2
                q, t = troch(mid)
                if q > self.rb and (self.half_angle(float(q)) - float(t)) * q > 0:
                    lo = mid
                else:
                    hi = mid
            phi_end = lo
        else:
            phi_end = phi1
        self.form = float(troch(phi_end)[0])                 # where the involute starts
        if self.form >= self.ra:
            raise TuleError("no involute is left between root and tip", op="spur_gear", hint="use a larger profile_shift")
        self._troch, self._phi = troch, (phi0, phi_end)

    def root(self, n):
        """Space middle → along the root circle → fillet trochoid → involute start, as (radius, angle)."""
        rr, th = self._troch(np.linspace(*self._phi, 50 * n))
        xy = _xy(np.column_stack([rr, th]))
        s = np.concatenate([[0], np.cumsum(np.hypot(*np.diff(xy, axis=0).T))])
        pick = np.searchsorted(s, np.linspace(0, s[-1], n)).clip(0, len(s) - 1)  # evenly along the curve
        pick[-1] = len(s) - 1
        rr, th = rr[pick], th[pick]
        return np.vstack([_root_arc(self.rf, math.pi / self.z, th[0], rr, th), np.column_stack([rr, th])])

    def flank(self, n):
        rho = _roll_samples(self.rb, self.form, self.ra, n)
        return np.column_stack([rho, [self.half_angle(q) for q in rho]])

    def boundary(self):
        """Dense (radius, half-angle) of the tooth from root to tip, for interference checks."""
        return np.vstack([self.root(200), self.flank(200)])


class _Internal:
    def __init__(self, m, z, pa, x, backlash, op="internal_gear"):
        _check(m, z, pa, backlash, op)
        self.m, self.z, self.x, self.backlash = m, int(z), x, backlash
        a = self.alpha = math.radians(pa)
        r = self.r = m * z / 2
        self.rb = r * math.cos(a)
        self.ra = max(r - m * (1 - x), self.rb * (1 + 1e-6))  # inner tip circle: no involute inside the base circle
        self.rf = r + m * (1.25 + x)
        self.psi_space = (m * (math.pi / 2 + 2 * x * math.tan(a)) + backlash) / (2 * r)
        if self.half_angle(self.ra) * 2 * self.ra < 0.1 * m:
            raise TuleError("the ring's teeth come to a point", op=op, hint="use a smaller profile_shift or more teeth")
        self._fillet(0.25 * m)

    def half_angle(self, rho):
        return math.pi / self.z - (self.psi_space + _inv(self.alpha) - _inv(math.acos(min(1.0, self.rb / rho))))

    def _point(self, rho):
        t = self.half_angle(rho)
        return np.array([rho * math.cos(t), rho * math.sin(t)])

    def _fillet(self, rho_f):
        """A circular fillet tangent to the involute and the root circle, on the space side."""
        for _ in range(20):
            def f(q):
                p = self._point(q)
                d = self._point(q + 1e-7 * self.m) - p
                n = np.array([-d[1], d[0]]) / np.hypot(*d)   # left of increasing radius: into the space
                return np.hypot(*(p + rho_f * n)) - (self.rf - rho_f), p + rho_f * n
            lo, hi = self.ra, self.rf
            if f(lo)[0] < 0 < f(hi)[0]:
                for _ in range(80):
                    mid = (lo + hi) / 2
                    lo, hi = (mid, hi) if f(mid)[0] < 0 else (lo, mid)
                c = f(lo)[1]
                end = math.atan2(c[1], c[0])
                if end < math.pi / self.z and lo > self.ra + 0.05 * self.m:
                    self.form, self.fillet_center, self.fillet_radius = lo, c, rho_f
                    self.root_end = end                       # where the fillet meets the root circle
                    return
            rho_f *= 0.7
        raise TuleError("could not fit the root fillet", op="internal_gear", hint="use more teeth or a smaller profile_shift")

    def root(self, n):
        """Space middle → fillet → involute start, as (radius, angle)."""
        c, rf = self.fillet_center, self.fillet_radius
        p0 = c * (self.rf / np.hypot(*c))
        p1 = self._point(self.form)
        a0, a1 = math.atan2(*(p0 - c)[::-1]), math.atan2(*(p1 - c)[::-1])
        if a1 - a0 > math.pi:
            a1 -= 2 * math.pi
        elif a0 - a1 > math.pi:
            a1 += 2 * math.pi
        k = np.linspace(a0, a1, n)
        pts = c + rf * np.column_stack([np.cos(k), np.sin(k)])
        rr, th = np.hypot(pts[:, 0], pts[:, 1]), np.arctan2(pts[:, 1], pts[:, 0])
        return np.vstack([_root_arc(self.rf, math.pi / self.z, th[0], rr, th), np.column_stack([rr, th])])

    def flank(self, n):
        rho = _roll_samples(self.rb, self.ra, self.form, n)[::-1]  # from the fillet in to the tip
        return np.column_stack([rho, [self.half_angle(q) for q in rho]])

    def boundary(self):
        return np.vstack([self.root(200), self.flank(200)])


def _root_arc(rf, middle, end, rr, th):
    """Points on the root circle from the space middle to ``end`` (excluded), spaced like the fillet's."""
    if middle - end < 1e-9:
        return np.empty((0, 2))
    step = math.hypot(rr[1] - rr[0], rr[1] * th[1] - rr[0] * th[0]) / rf
    n = max(1, int(round((middle - end) / step)) if step > 0 else 1)
    a = np.linspace(middle, end, n + 1)[:-1]
    return np.column_stack([np.full(len(a), rf), a])


def _roll_samples(rb, r0, r1, n):
    """Radii between r0 and r1, evenly spaced in the involute's roll angle."""
    t0, t1 = (math.sqrt(max(0.0, (q / rb) ** 2 - 1)) for q in (r0, r1))
    return rb * np.sqrt(1 + np.linspace(t0, t1, n) ** 2)


# ---------------------------------------------------------------- the solid


def _xy(polar, turn=0.0, mirror=False):
    rho, th = polar[:, 0], polar[:, 1]
    th = -th if mirror else th
    return np.column_stack([rho * np.cos(th + turn), rho * np.sin(th + turn)])


def _fit(points, tol, degree=5):
    """A B-spline through the ends and within ``tol`` of the points (x, y or x, y, z) between."""
    arr = TColgp_Array1OfPnt(1, len(points))
    for i, p in enumerate(points):
        arr.SetValue(i + 1, gp_Pnt(float(p[0]), float(p[1]), float(p[2]) if len(p) > 2 else 0.0))
    fit = GeomAPI_PointsToBSpline(arr, Approx_ParametrizationType.Approx_ChordLength, 3, degree,
                                  GeomAbs_Shape.GeomAbs_C2, tol)
    if not fit.IsDone():
        raise TuleError("could not fit the tooth profile", op="gear")
    return fit.Curve()


def _turned(curve, angle, v0, v1, u0=None, u1=None):
    """An edge on a copy of ``curve`` (or of its piece from ``u0`` to ``u1``) turned by ``angle`` about z,
    between two vertices."""
    c = Geom_BSplineCurve(curve.Poles(), curve.Knots(), curve.Multiplicities(), curve.Degree())
    if u0 is not None:
        c.Segment(u0, u1)
    c.Rotate(gp_Ax1(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1)), angle)
    return BRepBuilderAPI_MakeEdge(c, v0, v1).Edge()


def _arc(p0, pm, p1, v0, v1):
    c = GC_MakeArcOfCircle(*(gp_Pnt(float(p[0]), float(p[1]), float(p[2]) if len(p) > 2 else 0.0)
                             for p in (p0, pm, p1))).Value()
    return BRepBuilderAPI_MakeEdge(c, v0, v1).Edge()


def _toothed_wire(g, lift=None):
    """The closed toothed outline (counter-clockwise) and its edges by role.

    ``lift`` maps (n, 2) points of the outline to the (n, 3) points of the wire (default: z = 0); it
    must commute with turns about z (a bevel gear's outline lifted onto its back cone).
    """
    lift = lift or (lambda p: np.column_stack([p, np.zeros(len(p))]))
    z = g.z
    pitch = 2 * math.pi / z
    root = g.root(_ROOT_POINTS)        # space middle → involute start (upper side of the tooth on +x)
    flank = g.flank(_FLANK_POINTS)     # involute start → tip
    # a tooth counter-clockwise: lower flank (start → tip), tip arc, upper flank (tip → start),
    # then the root curve on to the next tooth's lower flank
    lower = _xy(flank, mirror=True)
    upper = _xy(flank[::-1])
    tip_mid = np.array([flank[-1, 0], 0.0])
    root_curve = np.vstack([_xy(root[::-1]), _xy(root[1:], turn=pitch, mirror=True)])

    def turned(pts, k):
        c, s = math.cos(k * pitch), math.sin(k * pitch)
        return pts @ np.array([[c, s], [-s, c]])

    def vertex(p):
        p = lift(np.asarray(p, float).reshape(1, 2))[0]
        return BRepBuilderAPI_MakeVertex(gp_Pnt(*(float(c) for c in p))).Vertex()

    tol = _TOL * g.m
    c_lo, c_hi = _fit(lift(lower), tol), _fit(lift(upper), tol)
    c_root = _fit(lift(root_curve), 5 * tol, 8)  # the root carries no load
    corners = [[vertex(turned(p, k)) for p in (lower[0], lower[-1], upper[0], upper[-1])] for k in range(z)]
    wire = BRepBuilderAPI_MakeWire()
    roles = {"flanks": [], "tips": [], "roots": []}
    for k in range(z):
        v_lo, v_tip_lo, v_tip_hi, v_hi = corners[k]
        e_lo = _turned(c_lo, k * pitch, v_lo, v_tip_lo)
        e_tip = _arc(*lift(np.array([turned(p, k) for p in (lower[-1], tip_mid, upper[0])])), v_tip_lo, v_tip_hi)
        e_hi = _turned(c_hi, k * pitch, v_tip_hi, v_hi)
        e_root = _turned(c_root, k * pitch, v_hi, corners[(k + 1) % z][0])
        for e in (e_lo, e_tip, e_hi, e_root):
            wire.Add(e)
        roles["flanks"] += [e_lo, e_hi]
        roles["tips"].append(e_tip)
        roles["roots"].append(e_root)
    if not wire.IsDone():
        raise TuleError("could not close the gear outline", op="gear")
    return wire.Wire(), roles


def _prism(face, width, edge_roles):
    mk = BRepPrimAPI_MakePrism(face, gp_Vec(0, 0, float(width)))
    mk.Build()
    if not mk.IsDone():
        raise TuleError("could not extrude the gear", op="gear")
    roles = {"bottom": [cast(mk.FirstShape())], "top": [cast(mk.LastShape())]}
    for role, edges in edge_roles.items():
        roles[role] = [f for e in edges for f in from_list(mk.Generated(e)) if kind_of(f) == "face"]
    return Shape(mk.Shape())._with_roles(roles)


def _circle_wire(radius, clockwise=False):
    d = gp_Dir(0, 0, -1 if clockwise else 1)
    e = BRepBuilderAPI_MakeEdge(gp_Circ(gp_Ax2(gp_Pnt(0, 0, 0), d), float(radius))).Edge()
    return BRepBuilderAPI_MakeWire(e).Wire(), e


def _name(prefix, m, z):
    return f"{prefix} m{m:g} z{z}"


def spur_gear(module: float, teeth: int, width: float, *, pressure_angle: float = 20,
              profile_shift: float = 0.0, backlash: float = 0.0, bore: float | None = None) -> Shape:
    """An involute spur gear from z = 0 up to ``width``, a tooth centered on +x.

    Pitch diameter ``module * teeth``, tip diameter ``module * (teeth + 2 + 2x)``,
    root diameter ``module * (teeth - 2.5 + 2x)``. ``bore`` is a center hole's
    diameter. Faces are named ``flanks``, ``tips``, ``roots``, ``top``,
    ``bottom`` (and ``bore``): ``gear.faces["flanks"]``. Below about 17 teeth
    the rack undercuts the roots as it would in the shop; ``gear_pair`` says
    when that costs contact, and a positive ``profile_shift`` avoids it.
    """
    g = _External(module, teeth, pressure_angle, profile_shift, backlash)
    if width <= 0:
        raise TuleError("width must be positive", op="spur_gear")
    wire, roles = _toothed_wire(g)
    mk = BRepBuilderAPI_MakeFace(wire, True)
    if bore:
        if not 0 < bore / 2 < g.rf - 0.5 * module:
            raise TuleError(f"bore {bore:g} does not fit inside the root circle (diameter {2 * g.rf:.4g})",
                            op="spur_gear", hint="leave at least half a module of material under the teeth")
        hole, e = _circle_wire(bore / 2, clockwise=True)
        mk.Add(hole)
        roles["bore"] = [e]
    return _prism(mk.Face(), width, roles).named(_name("gear", module, teeth))


def internal_gear(module: float, teeth: int, width: float, *, rim: float | None = None, pressure_angle: float = 20,
                  profile_shift: float = 0.0, backlash: float = 0.0) -> Shape:
    """A ring gear (teeth pointing inward) from z = 0 up to ``width``, a tooth centered on +x.

    Pitch diameter ``module * teeth``; the teeth's tips are on diameter
    ``module * (teeth - 2 + 2x)``, their roots on ``module * (teeth + 2.5 + 2x)``;
    ``rim`` (default 3 modules) is the material outside the roots. Faces:
    ``flanks``, ``tips``, ``roots``, ``outside``, ``top``, ``bottom``.
    """
    g = _Internal(module, teeth, pressure_angle, profile_shift, backlash)
    if width <= 0:
        raise TuleError("width must be positive", op="internal_gear")
    rim = 3 * module if rim is None else rim
    if rim <= 0:
        raise TuleError("rim must be positive", op="internal_gear")
    inner, roles = _toothed_wire(g)
    outer, e = _circle_wire(g.rf + rim)
    mk = BRepBuilderAPI_MakeFace(outer, True)
    mk.Add(cast(inner.Reversed()))
    roles["outside"] = [e]
    return _prism(mk.Face(), width, roles).named(_name("ring gear", module, teeth))


# ---------------------------------------------------------------- pairs


@dataclass(frozen=True)
class GearPair:
    """Two gears that mesh: where to put the second, and how well they run.

    ``distance`` between the axes, ``ratio`` (z2 / z1), ``contact_ratio``,
    ``pressure_angle`` (the working one, in degrees), ``backlash`` (along the
    pitch circle, from the gears' own backlash). ``place`` and ``rotation``
    put gear 2 so the teeth mesh.
    """

    module: float
    teeth: tuple
    internal: bool
    distance: float
    ratio: float
    contact_ratio: float
    pressure_angle: float
    backlash: float

    def rotation(self, angle: float = 0, turn: float = 0) -> float:
        """Degrees to turn gear 2 about its own axis to mesh.

        Gear 1 sits at the origin turned by ``turn`` degrees (0: a tooth on
        +x); gear 2 sits at ``distance`` in direction ``angle`` (degrees). For
        an internal pair gear 1 is the pinion and gear 2 the ring.
        """
        z1, z2 = self.teeth
        pitch2 = 360 / z2
        if self.internal:
            base = 180 - 180 / z2 if z1 % 2 == 0 else 180
            rot = base + angle + (turn - angle) * z1 / z2
        else:
            rot = 180 - 180 / z2 + angle + (angle - turn) * z1 / z2
        return round(rot % pitch2, 12)

    def place(self, gear: Shape, angle: float = 0, turn: float = 0) -> Shape:
        """``gear`` (built at the origin) turned to mesh and moved to ``distance`` in direction ``angle``."""
        a = math.radians(angle)
        return gear.rotate(z=self.rotation(angle, turn)).at(self.distance * math.cos(a), self.distance * math.sin(a), 0)


def gear_pair(module: float, z1: int, z2: int, *, internal: bool = False, pressure_angle: float = 20,
              profile_shift: tuple = (0.0, 0.0), backlash: tuple = (0.0, 0.0)) -> GearPair:
    """Check two gears and compute how they mesh; ``internal=True``: gear 2 is a ring around gear 1.

    Returns a :class:`GearPair` (``distance``, ``contact_ratio``, ``place``).
    Raises :class:`TuleError` when the teeth would interfere (undercut roots,
    tips hitting the other gear's roots or, in a ring, its tips), when the
    tip clearance is too small, or when the contact ratio is under 1.2.
    ``profile_shift`` and ``backlash`` are per gear, as given to the gears.
    """
    x1, x2 = (float(v) for v in profile_shift)
    b1, b2 = (float(v) for v in backlash)
    pair, problem = _mesh(module, z1, z2, internal, pressure_angle, x1, x2, b1, b2)
    if problem is None:
        return pair
    message, hint, data = problem
    if hint is None:  # interference: look for the profile shift that cures it
        hint = "use more teeth on the smaller gear" + (" or on the ring" if internal else "")
        s1 = 1 if internal or z1 <= z2 else -1  # the pinion shifts out; the other keeps the center distance
        for k in range(1, 21):
            y1 = x1 + 0.05 * k * s1
            y2 = x2 + 0.05 * k if internal else x2 - 0.05 * k * s1
            try:
                ok = _mesh(module, z1, z2, internal, pressure_angle, y1, y2, b1, b2)[1] is None
            except TuleError:
                break
            if ok:
                hint = f"profile_shift=({y1:.2f}, {y2:.2f}) works (build the gears with the same shifts); or " + hint
                break
    raise TuleError(message, op="gear_pair", hint=hint, **data)


def _mesh(m, z1, z2, internal, pressure_angle, x1, x2, b1, b2):
    """``(pair, None)``, or ``(pair, (message, hint, data))`` with hint None for interference."""
    op = "gear_pair"
    a = math.radians(pressure_angle)
    g1 = _External(m, z1, pressure_angle, x1, b1, op)
    g2 = (_Internal if internal else _External)(m, z2, pressure_angle, x2, b2, op)
    if internal and z2 <= z1:
        raise TuleError("a ring gear needs more teeth than the pinion inside it", op=op)
    sign = -1 if internal else 1
    zs = z2 + sign * z1
    aw = _inv_inverse(_inv(a) + 2 * math.tan(a) * (x2 + sign * x1) / zs)
    dist = m * zs / 2 * math.cos(a) / math.cos(aw)
    l1 = math.sqrt(g1.ra ** 2 - g1.rb ** 2)
    l2 = math.sqrt(g2.ra ** 2 - g2.rb ** 2)
    line = dist * math.sin(aw)
    eps = ((l1 - l2 + line) if internal else (l1 + l2 - line)) / (math.pi * m * math.cos(a))
    pair = GearPair(module=m, teeth=(int(z1), int(z2)), internal=internal, distance=dist, ratio=z2 / z1,
                    contact_ratio=eps, pressure_angle=math.degrees(aw), backlash=b1 + b2)
    names = (f"the {z1}-tooth gear", f"the {z2}-tooth ring" if internal else f"the {z2}-tooth gear")
    data = {"contact_ratio": round(eps, 4), "distance": dist}

    # clearance between each tip circle and the other's root circle
    gap = min([g2.rf - dist - g1.ra, g2.ra - dist - g1.rf] if internal else [dist - g1.ra - g2.rf, dist - g2.ra - g1.rf])
    if gap < 0.1 * m:
        return pair, (f"a tip runs within {gap:.3g} of the other gear's root (want ≥ {0.1 * m:.3g})",
                      "the profile shifts are too large; make them smaller", {**data, "clearance": gap})
    # involute interference: contact must stay between the points where the line of action touches the base circles
    if (l2 - line if internal else line - l2) < 0:
        return pair, (f"{names[1]}'s tips reach past {names[0]}'s base circle (involute interference)", None, data)
    if not internal and line - l1 < 0:
        return pair, (f"{names[0]}'s tips reach past {names[1]}'s base circle (involute interference)", None, data)
    hit = _interference(g1, g2, dist, internal)
    if hit:
        dst, r_hit = hit
        who = names[0] if dst is g1 else names[1]
        if isinstance(dst, _External) and r_hit < dst.form + 0.02 * m:
            what = "undercut roots" if dst.undercut else "roots, below where its involute starts"
            return pair, (f"the other gear's tips cut into {who}'s {what} (radius {r_hit:.4g})", None, data)
        return pair, (f"the other gear's tips hit {who}'s teeth outside the line of contact (radius {r_hit:.4g})",
                      None, data)
    if eps < 1.2:
        return pair, (f"contact ratio {eps:.3f} is under 1.2: the next pair of teeth may not engage in time",
                      "use more teeth, or less profile shift", data)
    return pair, None


def _interference(g1, g2, dist, internal):
    """Turn gear 1 through a pitch, gear 2 with it, and look for tip points inside the other's teeth.

    Returns (the gear cut into, radius) or None. Cuts shallower than ``_SLACK`` modules along the
    circumference are not interference: at zero backlash a ring's tip corners graze the pinion's
    root fillet by a few thousandths of a module, and any backlash clears that.
    """
    pair = GearPair(g1.m, (g1.z, g2.z), internal, dist, 0, 0, 0, 0)
    turns = np.linspace(0, 360 / g1.z, 241)
    rot1 = np.radians(turns)
    rot2 = np.radians([pair.rotation(0, t) for t in turns])
    for src, s_rot, s_at, dst, d_rot, d_at in ((g1, rot1, 0.0, g2, rot2, dist), (g2, rot2, dist, g1, rot1, 0.0)):
        hw = src.half_angle(src.ra)
        tips = np.linspace(-hw, hw, 7)[None, None, :] + (2 * math.pi / src.z) * np.arange(src.z)[None, :, None]
        ang = tips + s_rot[:, None, None]
        x = src.ra * np.cos(ang) + s_at - d_at
        y = src.ra * np.sin(ang)
        rho = np.hypot(x, y)
        pitch = 2 * math.pi / dst.z
        th = (np.arctan2(y, x) - d_rot[:, None, None] + pitch / 2) % pitch - pitch / 2
        b = dst.boundary()
        b = b[np.argsort(b[:, 0])]
        inside = (rho > b[0, 0]) & (rho < b[-1, 0])
        depth = np.where(inside, (np.interp(rho, b[:, 0], b[:, 1]) - np.abs(th)) * rho, -1.0)
        if depth.max() > _SLACK * g1.m:
            return dst, float(rho.flat[int(np.argmax(depth))])
    return None


# ---------------------------------------------------------------- cycloidal drives
# The disc of a cycloidal drive with N pins (radius rr, on a circle of radius R) and an
# eccentric crank e, in the disc's frame: a pin's center rolls along the shortened
# epicycloid p(t) = R e^(it) - e e^(iNt) (N - 1 lobes), and the disc's outline is that
# curve offset inward by rr. With the crank at angle θ the disc's center is at e e^(iθ)
# and the disc is turned by -θ / (N - 1); every pin then touches the outline.


class _Cycloid:
    def __init__(self, pins, pin_circle_d, pin_d, e, op):
        if int(pins) != pins or pins < 4:
            raise TuleError(f"a cycloidal drive needs a whole number of pins ≥ 4, not {pins}", op=op)
        if pin_circle_d <= 0 or pin_d <= 0 or e <= 0:
            raise TuleError("pin_circle_d, pin_d and eccentricity must be positive", op=op)
        n, R, rr = self.n, self.R, self.rr = int(pins), pin_circle_d / 2, pin_d / 2
        self.e = e
        if rr >= R * math.sin(math.pi / n):
            raise TuleError(f"{n} pins of diameter {pin_d:g} overlap on a {pin_circle_d:g} circle", op=op,
                            hint=f"use pins under {2 * R * math.sin(math.pi / n):.4g} in diameter")
        self.max_e = self._max_eccentricity()
        if e >= self.max_e:
            raise TuleError(f"eccentricity {e:g} undercuts the lobes (their tips would loop): the limit for "
                            f"{pin_d:g} pins on a {pin_circle_d:g} circle is {self.max_e:.4g}", op=op,
                            hint="use a smaller eccentricity, smaller pins or a larger pin circle",
                            max_eccentricity=self.max_e)
        self.r_root = R - e - rr                                  # the valleys' circle

    def _max_curvature(self, e):
        """The largest curvature of the pins' path (convex side): the offset by rr loops where rr exceeds 1/κ."""
        n, R = self.n, self.R
        t = np.linspace(0, math.pi / (n - 1), 2001)               # half a lobe: the rest is symmetric
        a = R * np.exp(1j * t) - n * e * np.exp(1j * n * t)       # p' / i
        b = R * np.exp(1j * t) - n * n * e * np.exp(1j * n * t)   # -p''
        return float(np.max(np.real(np.conj(a) * b) / np.abs(a) ** 3))

    def _max_eccentricity(self):
        lo, hi = 0.0, self.R / self.n                            # past R / N the path itself loops
        for _ in range(50):
            mid = (lo + hi) / 2
            lo, hi = (mid, hi) if self.rr * self._max_curvature(mid) < 1 else (lo, mid)
        return lo

    def points(self, t, inset=0.0):
        """Outline points (complex) at curve parameters ``t``, moved ``inset`` inward; t = 0 is the valley on +x."""
        n, R, rr, e = self.n, self.R, self.rr, self.e
        t = np.asarray(t, float)
        a = R * np.exp(1j * t) - n * e * np.exp(1j * n * t)       # outward normal × |p'|
        return R * np.exp(1j * t) - e * np.exp(1j * n * t) - (rr + inset) * a / np.abs(a)

    def lobe(self, tol):
        """One lobe, valley (on +x) to valley: a B-spline 0 to 2 ``tol`` inside the exact outline.

        Fitted within ``tol`` of the outline moved ``tol`` inward, so the disc touches the pins but
        never cuts into them.
        """
        from OCP.GeomAPI import GeomAPI_Interpolate, GeomAPI_ProjectPointOnCurve

        try:
            from OCP.collections import HArray1_gp_Pnt  # OCP 8
        except ImportError:  # pragma: no cover
            from OCP.TColgp import TColgp_HArray1OfPnt as HArray1_gp_Pnt

        span = 2 * math.pi / (self.n - 1)
        dense = np.linspace(0, span, 20001)
        s = np.concatenate([[0], np.cumsum(np.abs(np.diff(self.points(dense))))])
        tangents = [gp_Vec(0, 1, 0), gp_Vec(-math.sin(span), math.cos(span), 0)]  # valleys: across the radius
        k = 48
        while True:
            t = np.interp(np.linspace(0, s[-1], 2 * k + 1), s, dense)  # even along the curve
            pts, mids = self.points(t[::2], tol), self.points(t[1::2], tol)
            arr = HArray1_gp_Pnt(1, len(pts))
            for i, p in enumerate(pts):
                arr.SetValue(i + 1, gp_Pnt(p.real, p.imag, 0))
            it = GeomAPI_Interpolate(arr, False, 1e-9)
            it.Load(*tangents, False)
            it.Perform()
            curve = it.Curve()
            err = max(GeomAPI_ProjectPointOnCurve(gp_Pnt(p.real, p.imag, 0), curve).LowerDistance() for p in mids)
            if err <= tol:
                return curve
            if k >= 1536:
                raise TuleError("could not fit the disc's lobes", op="cycloidal_disc",
                                hint="the eccentricity is very close to its limit; use a smaller one")
            k *= 2


def _hole_wire(radius, x=0.0, y=0.0):
    e = BRepBuilderAPI_MakeEdge(gp_Circ(gp_Ax2(gp_Pnt(x, y, 0), gp_Dir(0, 0, -1)), float(radius))).Edge()
    return BRepBuilderAPI_MakeWire(e).Wire(), e


def cycloidal_disc(pins: int, pin_circle_d: float, pin_d: float, eccentricity: float, width: float | None = None, *,
                   bore: float | None = None, holes: tuple | None = None):
    """The disc of a cycloidal drive: ``pins - 1`` lobes rolling inside ``pins`` pins, from z = 0 up to ``width``.

    ``pins`` pins of diameter ``pin_d`` stand on a circle of diameter
    ``pin_circle_d``; the input crank's ``eccentricity`` moves the disc's
    center. The outline is the exact profile (the shortened epicycloid offset
    by the pin radius) in B-splines lying 0 to 0.0002 · pin_circle_d / pins
    inside it, so the disc touches every pin without cutting in; a valley is
    centered on +x. ``bore`` is the center hole's
    diameter (the crank's bearing); ``holes=(count, circle_d, d)`` are the
    output pins' holes, the first on +x: d = output pin + 2 · eccentricity.
    Without ``width`` it is a Profile. Faces: ``lobes``, ``top``, ``bottom``,
    ``bore``, ``holes``. Raises when the eccentricity undercuts the lobes
    (``e.data["max_eccentricity"]``); ``cycloidal_drive`` builds the pins and
    places the disc.
    """
    op = "cycloidal_disc"
    c = _Cycloid(pins, pin_circle_d, pin_d, eccentricity, op)
    curve = c.lobe(_TOL * pin_circle_d / pins)
    lobes = c.n - 1
    pitch = 2 * math.pi / lobes
    cuts = _quarter_turns(curve)  # a few edges per lobe, so area and volume measure right
    corners = [[BRepBuilderAPI_MakeVertex(curve.Value(u).Rotated(gp_Ax1(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1)), k * pitch))
                .Vertex() for u in cuts[:-1]] for k in range(lobes)]
    wire = BRepBuilderAPI_MakeWire()
    roles = {"lobes": []}
    for k in range(lobes):
        vs = corners[k] + [corners[(k + 1) % lobes][0]]
        for j in range(len(cuts) - 1):
            e = _turned(curve, k * pitch, vs[j], vs[j + 1], cuts[j], cuts[j + 1])
            wire.Add(e)
            roles["lobes"].append(e)
    if not wire.IsDone():
        raise TuleError("could not close the disc's outline", op=op)
    mk = BRepBuilderAPI_MakeFace(wire.Wire(), True)
    inner = 0.0
    if bore:
        if not 0 < bore / 2 < c.r_root:
            raise TuleError(f"bore {bore:g} does not fit inside the valleys (diameter {2 * c.r_root:.4g})", op=op)
        hole, e = _hole_wire(bore / 2)
        mk.Add(hole)
        roles["bore"] = [e]
        inner = bore / 2
    if holes:
        try:
            count, circle_d, d = holes
        except (TypeError, ValueError):
            raise TuleError("holes is (count, circle_d, d)", op=op) from None
        rc, rh = circle_d / 2, d / 2
        if int(count) != count or count < 1 or rh <= 0:
            raise TuleError("holes needs a whole count ≥ 1 and a positive diameter", op=op)
        if rc + rh >= c.r_root or rc - rh <= inner or (count > 1 and rh >= rc * math.sin(math.pi / count)):
            raise TuleError(f"{count} holes of diameter {d:g} on a {circle_d:g} circle do not fit between the bore "
                            f"and the valleys (diameter {2 * c.r_root:.4g}), or overlap", op=op)
        roles["holes"] = []
        for k in range(int(count)):
            a = 2 * math.pi * k / count
            hole, e = _hole_wire(rh, rc * math.cos(a), rc * math.sin(a))
            mk.Add(hole)
            roles["holes"].append(e)
    face = mk.Face()
    if width is None:
        from .profile import Profile

        return Profile(face)
    if width <= 0:
        raise TuleError("width must be positive", op=op)
    return _prism(face, width, roles).named(f"cycloidal disc {lobes} lobes")


@dataclass(frozen=True)
class CycloidalDrive:
    """A cycloidal disc and its pins: ``disc`` as made (centered on the origin), ``pins`` in place.

    ``ratio`` is pins - 1: the disc turns once, backwards, per ``ratio``
    turns of the input crank. ``eccentricity`` and ``max_eccentricity`` (the
    undercut limit). ``place`` and ``rotation`` put the disc where the crank
    at an angle holds it, touching every pin.
    """

    ratio: int
    eccentricity: float
    max_eccentricity: float
    disc: Shape = field(repr=False, compare=False)
    pins: Shape = field(repr=False, compare=False)

    def rotation(self, angle: float = 0) -> float:
        """Degrees the disc has turned about its own center with the crank at ``angle`` degrees."""
        return -angle / self.ratio

    def place(self, shape: Shape, angle: float = 0) -> Shape:
        """``shape`` (the disc as made, or anything fixed to it) with the crank at ``angle`` degrees (0: toward +x)."""
        a = math.radians(angle)
        return shape.rotate(z=self.rotation(angle)).at(self.eccentricity * math.cos(a), self.eccentricity * math.sin(a), 0)


def cycloidal_drive(pins: int, pin_circle_d: float, pin_d: float, eccentricity: float, width: float, *,
                    bore: float | None = None, holes: tuple | None = None) -> CycloidalDrive:
    """A cycloidal disc and its ``pins`` pins (from z = 0 up to ``width``, the first on +x).

    Arguments as for ``cycloidal_disc``. ``drive.place(drive.disc, angle)``
    is the disc with the crank at ``angle`` degrees; ``drive.ratio`` is
    pins - 1.
    """
    disc = cycloidal_disc(pins, pin_circle_d, pin_d, eccentricity, width, bore=bore, holes=holes)
    c = _Cycloid(pins, pin_circle_d, pin_d, eccentricity, "cycloidal_drive")
    comp = TopoDS_Compound()
    b = BRep_Builder()
    b.MakeCompound(comp)
    for k in range(c.n):
        a = 2 * math.pi * k / c.n
        ax = gp_Ax2(gp_Pnt(c.R * math.cos(a), c.R * math.sin(a), 0), gp_Dir(0, 0, 1))
        b.Add(comp, BRepPrimAPI_MakeCylinder(ax, c.rr, float(width)).Shape())
    return CycloidalDrive(ratio=c.n - 1, eccentricity=eccentricity, max_eccentricity=c.max_e, disc=disc,
                          pins=Shape(comp).named(f"pins {c.n}"))
