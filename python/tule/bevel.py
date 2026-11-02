"""Straight bevel gears: spherical involute teeth, and the pair that meshes them.

```python
from tule import *

pair = bevel_pair(2, 20, 40, 12, bore=(8, 12), backlash=(0.05, 0.05))   # 90° shafts; checks interference
outputs.part("pinion", pair.gear1)                     # axis +z, back face on z = 0, apex at (0, 0, pair.apex[0])
outputs.part("wheel", pair.gear2)                      # placed: the apexes meet, the teeth mesh
wheel = bevel_gear(2, 40, 12, mating_teeth=20)         # the same gear on its own, to place or turn yourself
frame = pair.place(wheel, turn=30)                     # meshes with pair.gear1.rotate(z=30)
```

Every tooth surface is a cone through the pitch apex (tip, root and flanks
all converge there, the "standard taper" of a straight bevel gear), so a
tooth is its profile on a sphere about the apex. The flanks are spherical
involutes: what a plane of action, rolling on the base cone
(sin δb = sin δ cos α), traces. Two of them mesh exactly, at any shaft
angle; a Tredgold (back-cone) profile only approximates that. The roots are
what the generating crown gear (the bevel gear's rack, with DIN 867
proportions and a 0.38 m tip radius, measured at the heel) leaves as it
rolls, worked out on the sphere, so small pinions come out undercut as they
would be made. Each tooth is four faces: two ``flanks``, a ``tips`` face
and a ``roots`` face, so a 30-tooth gear is about 1 MB of STEP.

Module, addendum (1 m), dedendum (1.25 m), backlash and tooth thickness are
at the heel, the outer end; the teeth shrink toward the apex. Profile shift
on bevel gears is the long-addendum pinion (x1 = -x2): the cones stay put.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace

import numpy as np
from OCP.BRepBuilderAPI import (BRepBuilderAPI_MakeEdge, BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakeSolid,
                                BRepBuilderAPI_MakeWire, BRepBuilderAPI_Sewing, BRepBuilderAPI_Transform)
from OCP.BRepOffsetAPI import BRepOffsetAPI_ThruSections
from OCP.gp import gp_Ax2, gp_Ax3, gp_Circ, gp_Cone, gp_Cylinder, gp_Dir, gp_Pln, gp_Pnt, gp_Trsf
from OCP.ShapeFix import ShapeFix_Face, ShapeFix_Solid
from OCP.TopAbs import TopAbs_FACE, TopAbs_SHELL
from OCP.TopExp import TopExp_Explorer

from ._occ import cast
from .errors import TuleError
from .gears import _SLACK, _check, _root_arc, _toothed_wire, _xy
from .shape import Shape

__all__ = ["BevelPair", "bevel_gear", "bevel_pair"]


def _unit(v):
    return v / np.linalg.norm(v, axis=-1, keepdims=True)


def _sph_inv(theta, delta_b):
    """The spherical involute function: azimuth of the involute of base cone ``delta_b`` at polar angle ``theta``."""
    s = math.sin(delta_b)
    lam = np.arccos(np.clip(np.cos(theta) / math.cos(delta_b), -1.0, 1.0))  # arc the plane of action has rolled
    return lam / s - np.arctan2(np.sin(lam), np.cos(lam) * s)


def _cone_angle(teeth, cone_angle, mating_teeth, shaft_angle, op):
    """The pitch cone angle in degrees, given or from the mating gear's teeth and the shaft angle."""
    if cone_angle is not None and mating_teeth is not None:
        raise TuleError("give cone_angle or mating_teeth, not both", op=op)
    if cone_angle is None:
        if mating_teeth is None:
            raise TuleError("a bevel gear needs its pitch cone angle", op=op,
                            hint="pass mating_teeth= (the other gear's teeth, shaft_angle=90 by default) or cone_angle=")
        if not 0 < shaft_angle < 180:
            raise TuleError(f"shaft angle {shaft_angle}° is outside 0°–180°", op=op)
        if int(mating_teeth) != mating_teeth or mating_teeth < 5:
            raise TuleError(f"the mating gear needs a whole number of teeth ≥ 5, not {mating_teeth}", op=op)
        s = math.radians(shaft_angle)
        cone_angle = math.degrees(math.atan2(math.sin(s), mating_teeth / teeth + math.cos(s)))
    if not 0 < cone_angle < 90:
        raise TuleError(f"pitch cone angle {cone_angle:.4g}° is outside 0°–90°", op=op,
                        hint="crown and internal bevel gears are not supported; use fewer teeth on this gear "
                             "or a smaller shaft angle")
    return cone_angle


# ---------------------------------------------------------------- tooth geometry
# A gear's own frame here: apex at the origin, axis +z, the gear at z > 0.
# Its tooth is a curve on the unit sphere, as (polar angle from the axis,
# half-angle about it), upper half of the tooth centered on azimuth 0, the
# space middle at pi/z; like the spur gear's (radius, half-angle).


class _Bevel:
    def __init__(self, m, z, pa, cone_angle, x, backlash, op="bevel_gear"):
        _check(m, z, pa, backlash, op)
        self.m, self.z, self.x, self.backlash = m, int(z), x, backlash
        a = self.alpha = math.radians(pa)
        d = self.delta = math.radians(cone_angle)
        self.r = m * z / 2                                   # heel pitch radius
        R = self.R = self.r / math.sin(d)                    # cone distance to the heel
        self.ha, self.hf = m * (1 + x), m * (1.25 - x)
        self.da = d + math.atan(self.ha / R)                 # tip cone
        self.df = d - math.atan(self.hf / R)                 # root cone
        if self.df <= 0.02:
            raise TuleError("the root cone closes up on the axis", op=op, hint="use more teeth or a larger profile_shift")
        self.db = math.asin(math.sin(d) * math.cos(a))       # base cone
        self.psi = (m * (math.pi / 2 + 2 * x * math.tan(a)) - backlash) / (2 * self.r)  # half-angle on the pitch cone
        if self.da <= self.db:
            raise TuleError("the tip cone lies inside the base cone", op=op, hint="use a larger profile_shift")
        tip = 2 * R * math.sin(self.da) * self.half_angle(self.da)
        if tip < 0.1 * m:
            raise TuleError(f"the teeth come to a point (tip thickness {tip:.3g} at the heel)", op=op,
                            hint="use a smaller profile_shift or more teeth", tip_thickness=tip)
        self._cut()

    def half_angle(self, theta):
        """Half-angle of the involute tooth at polar angle ``theta`` (≥ the base cone)."""
        return self.psi + _sph_inv(self.delta, self.db) - _sph_inv(theta, self.db)

    def _cut(self):
        """Roll the generating crown gear: root circle, fillet, undercut, where the involute starts.

        The crown gear (pitch cone 90°) is the bevel gear's rack. Its flanks are spherical involutes
        of the base cone 90° - α, its tip a circle of latitude, and its tip fillet a circle on the
        sphere; Willis's theorem holds on the sphere (the contact normal is a great circle through
        the pitch line), which gives the root the fillet leaves, as for the spur gear's trochoid.
        """
        m, a, d, R, z = self.m, self.alpha, self.delta, self.R, self.z
        e1 = np.array([math.sin(d), 0.0, math.cos(d)])       # the pitch line
        e2 = np.array([0.0, 1.0, 0.0])                        # where the pitch line moves as the gear turns
        c = np.array([math.cos(d), 0.0, -math.sin(d)])       # crown axis: away from the gear axis

        def crown(u, v):  # crown point at azimuth u along its pitch circle, latitude v toward c
            u, v = np.asarray(u, float)[..., None], np.asarray(v, float)[..., None]
            return np.cos(v) * (np.cos(u) * e1 + np.sin(u) * e2) + np.sin(v) * c

        ta, ca = math.tan(a), math.cos(a)
        half = math.pi * m / 4 + self.backlash / 2           # the rack tooth's half-width on its reference line
        rho = min(0.38 * m, max(0.0, (half - 1.25 * m * ta) / (1 / ca - ta))) / R  # tip fillet, angular
        w_p = (half - self.x * m * ta) / R                    # crown tooth half-width on its pitch circle
        v_tip = self.df - d                                   # crown tip latitude (it cuts the root cone)
        v_c = v_tip + rho                                     # fillet center latitude
        crown_inv = lambda v: _sph_inv(math.pi / 2 - v, math.pi / 2 - a)
        flank = lambda v: crown(w_p + crown_inv(0.0) - crown_inv(v), v)  # crown tooth's +u flank

        if rho > 0:
            def offset(v):  # the flank point at latitude v, moved rho into the crown tooth
                f = flank(v)
                t = _unit(flank(v + 1e-7) - flank(v - 1e-7))
                n = _unit(np.cross(f, t))
                n = np.where((n @ e2)[..., None] < 0, n, -n)  # toward -u, into the tooth
                return math.cos(rho) * f + math.sin(rho) * n

            lo, hi = v_tip, 0.0
            for _ in range(60):  # the flank point whose offset is at the fillet center's latitude
                mid = (lo + hi) / 2
                lo, hi = (mid, hi) if math.asin(float(offset(mid) @ c)) < v_c else (lo, mid)
            center, touch = offset(lo), flank(lo)
            u_c = max(0.0, math.atan2(center @ e2, center @ e1))
            center = crown(u_c, v_c)
            # the crown turns until the normal at the touch point runs through the pitch line
            nrm = np.cross(np.cross(touch, center), c)
            nrm = nrm if nrm @ e1 > 0 else -nrm
            u_x = math.atan2(nrm @ e2, nrm @ e1)
        else:
            u_c = max(0.0, w_p + crown_inv(0.0) - crown_inv(v_tip))
            u_x = None
        sd = math.sin(d)

        def troch(phi):
            phi = np.asarray(phi, float)
            cen = crown(u_c + phi * sd, np.full_like(phi, v_c))       # fillet center, fixed frame
            dist = np.arccos(np.clip(cen @ e1, -1, 1))[..., None]
            q = (np.sin(-rho) * e1 + np.sin(dist + rho) * cen) / np.sin(dist)  # contact point (Willis)
            cp, sp = np.cos(-phi), np.sin(-phi)                       # into the gear frame
            gx, gy = cp * q[..., 0] - sp * q[..., 1], sp * q[..., 0] + cp * q[..., 1]
            return np.arctan2(np.hypot(gx, gy), q[..., 2]), math.pi / z - np.arctan2(gy, gx)

        phi0 = -u_c / sd
        phi1 = -u_x / sd if u_x is not None else phi0 + 1e-9
        grid = np.linspace(phi0, phi1, 801)
        th_r, hw = troch(grid)
        inv_hw = np.where(th_r > self.db, self.half_angle(np.maximum(th_r, self.db)), np.nan)
        cut = (inv_hw - hw) * np.sin(th_r) * R > 1e-7 * m    # the fillet bites into the involute
        self.undercut = bool(np.any(cut))
        if self.undercut:
            i = int(np.nonzero(cut)[0][-1])
            lo, hi = grid[i], grid[min(i + 1, len(grid) - 1)]
            for _ in range(60):
                mid = (lo + hi) / 2
                t, h = troch(mid)
                if t > self.db and (self.half_angle(float(t)) - float(h)) > 0:
                    lo = mid
                else:
                    hi = mid
            phi_end = lo
        else:
            phi_end = phi1
        self.form = float(troch(phi_end)[0])                 # where the involute starts
        if self.form >= self.da:
            raise TuleError("no involute is left between root and tip", op="bevel_gear", hint="use a larger profile_shift")
        self._troch, self._phi = troch, (phi0, phi_end)

    # profiles projected from the apex onto the plane z = h: (radius, half-angle), like a spur gear's
    def root(self, n, h=1.0):
        th, hw = self._troch(np.linspace(*self._phi, 50 * n))
        rr = h * np.tan(th)
        xy = _xy(np.column_stack([rr, hw]))
        s = np.concatenate([[0], np.cumsum(np.hypot(*np.diff(xy, axis=0).T))])
        pick = np.searchsorted(s, np.linspace(0, s[-1], n)).clip(0, len(s) - 1)
        pick[-1] = len(s) - 1
        rr, hw = rr[pick], hw[pick]
        rr[-1], hw[-1] = h * math.tan(self.form), self.half_angle(self.form)  # meet the flank exactly (µm near the base cone)
        return np.vstack([_root_arc(h * math.tan(self.df), math.pi / self.z, hw[0], rr, hw), np.column_stack([rr, hw])])

    def flank(self, n, h=1.0):
        lam = [math.acos(min(1.0, math.cos(t) / math.cos(self.db))) for t in (self.form, self.da)]
        th = np.arccos(np.cos(np.linspace(*lam, n)) * math.cos(self.db))  # evenly in roll angle
        th[0], th[-1] = max(th[0], self.form), self.da
        return np.column_stack([h * np.tan(th), self.half_angle(th)])

    def boundary(self):
        """Dense (polar angle, half-angle) of the tooth from root to tip, for interference checks."""
        b = np.vstack([self.root(200), self.flank(200)])
        return np.column_stack([np.arctan(b[:, 0]), b[:, 1]])


class _Projected:
    """A bevel tooth seen on the plane z = h (a central projection from the apex), for ``_toothed_wire``."""

    def __init__(self, g, h, m):
        self.g, self.h, self.z, self.m = g, h, g.z, m          # m sets the fit tolerance

    def root(self, n):
        return self.g.root(n, self.h)

    def flank(self, n):
        return self.g.flank(n, self.h)


# ---------------------------------------------------------------- the solid


def _circle(r, z):
    return BRepBuilderAPI_MakeEdge(gp_Circ(gp_Ax2(gp_Pnt(0, 0, float(z)), gp_Dir(0, 0, 1)), float(r))).Edge()


def _face(surface, outer, inner=None):
    """A face on ``surface`` inside wire ``outer`` (and outside ``inner``), with its p-curves."""
    mk = BRepBuilderAPI_MakeFace(surface, outer, True)
    if inner is not None:
        mk.Add(inner)
    fix = ShapeFix_Face(mk.Face())
    fix.Perform()
    return cast(fix.Face())


def _wire(*edges):
    mk = BRepBuilderAPI_MakeWire()
    for e in edges:
        mk.Add(e)
    return mk.Wire()


def _bevel_solid(g, width, bore, op):
    """The gear solid standing on its back face, and the apex height.

    Built in the gear's own frame (apex at the origin, axis +z) face by face: the tooth outline at
    the heel (on the back cone), the same outline scaled toward the apex to the toe, ruled faces
    between them (cones through the apex: the flanks, tips and roots), the two back cones inside
    the outlines, and the hub (front and back faces, bore). No Boolean: the ends of the teeth are
    the exact outlines, which keeps the STEP small.
    """
    m, d, R = g.m, g.delta, g.R
    if width <= 0:
        raise TuleError("width must be positive", op=op)
    if width > 0.5 * R:
        raise TuleError(f"face width {width:g} is over half the cone distance {R:.4g}", op=op,
                        hint=f"bevel gears are usually at most R/3 = {R / 3:.3g} wide")
    k = (R - width) / R
    pitch = np.array([R * math.sin(d), R * math.cos(d)])  # (radius, z) of the heel pitch circle
    out = np.array([math.cos(d), -math.sin(d)])           # along the back cone, toward the tips
    heel_foot = pitch - (g.hf + m) * out                   # the back face: a module below the heel's roots
    toe_foot = k * (pitch - (g.hf + 0.5 * m) * out)       # the front face: half a module below the toe's
    toe_tip = k * (pitch + g.ha * out)
    if toe_tip[1] < 0.05 * R:
        raise TuleError("the teeth reach past the apex", op=op,
                        hint="the pitch cone is too close to 90° (a crown gear); use a narrower width or fewer teeth")
    r0 = bore / 2 if bore else 0.0
    inner = min(toe_foot[0], heel_foot[0])
    if bore and not 0 < r0 < inner - 0.5 * m:
        raise TuleError(f"bore {bore:g} does not fit inside the gear (largest about {2 * (inner - 0.5 * m):.3g})",
                        op=op, hint="leave at least half a module of material under the teeth")

    # the outline on the plane z = h, each point slid along its ray from the apex onto the back cone
    h = R * math.cos(d)
    zbc, cot = R / math.cos(d), 1 / math.tan(d)            # back cone: r = (zbc - z) cot δ

    def lift(p):
        s = zbc * cot / (np.hypot(p[:, 0], p[:, 1]) + h * cot)
        return np.column_stack([p[:, 0] * s, p[:, 1] * s, h * s])

    heel, edge_roles = _toothed_wire(_Projected(g, h, g.m), lift)
    scale = gp_Trsf()
    scale.SetScale(gp_Pnt(0, 0, 0), k)
    toe = cast(BRepBuilderAPI_Transform(heel, scale, True).Shape())
    mk = BRepOffsetAPI_ThruSections(False, True, 1e-6)
    mk.AddWire(heel)
    mk.AddWire(toe)
    mk.CheckCompatibility(False)
    mk.Build()
    if not mk.IsDone():
        raise TuleError("could not make the teeth", op=op)
    roles = {role: [mk.GeneratedFace(e) for e in edges] for role, edges in edge_roles.items()}

    def back_cone(f):  # the heel's back cone scaled by f
        return gp_Cone(gp_Ax3(gp_Pnt(0, 0, f * h), gp_Dir(0, 0, -1)), math.pi / 2 - d, f * R * math.sin(d))

    c_heel, c_toe = _circle(heel_foot[0], heel_foot[1]), _circle(toe_foot[0], toe_foot[1])
    roles["heel"] = [_face(back_cone(1.0), heel, _wire(c_heel))]
    roles["toe"] = [_face(back_cone(k), toe, _wire(c_toe))]
    plane = lambda z: gp_Pln(gp_Pnt(0, 0, float(z)), gp_Dir(0, 0, 1))
    if bore:
        b_front, b_back = _circle(r0, toe_foot[1]), _circle(r0, heel_foot[1])
        roles["front"] = [_face(plane(toe_foot[1]), _wire(c_toe), _wire(b_front))]
        roles["back"] = [_face(plane(heel_foot[1]), _wire(c_heel), _wire(b_back))]
        cyl = gp_Cylinder(gp_Ax3(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1)), r0)
        roles["bore"] = [cast(BRepBuilderAPI_MakeFace(cyl, 0, 2 * math.pi, toe_foot[1], heel_foot[1]).Face())]
    else:
        roles["front"] = [_face(plane(toe_foot[1]), _wire(c_toe))]
        roles["back"] = [_face(plane(heel_foot[1]), _wire(c_heel))]

    sew = BRepBuilderAPI_Sewing(1e-5 * m)
    for f in _faces(mk.Shape()) + [f for r in ("heel", "toe", "front", "back", "bore") for f in roles.get(r, [])]:
        sew.Add(f)
    sew.Perform()
    shells = [s for s in _explore(sew.SewedShape(), TopAbs_SHELL)]
    if len(shells) != 1:
        raise TuleError("could not close the gear's faces into a solid", op=op)
    solid = BRepBuilderAPI_MakeSolid(cast(shells[0])).Solid()
    fix = ShapeFix_Solid(solid)
    fix.Perform()
    solid = cast(fix.Solid())
    roles = {r: [cast(sew.Modified(f)) if sew.IsModified(f) else f for f in fs] for r, fs in roles.items()}
    flat = Shape(solid).faces.planar()  # sewing does not always report the flat faces it rebuilt
    at = lambda z: flat.within((-1e9, -1e9, z - 1e-6), (1e9, 1e9, z + 1e-6)).items
    roles["front"], roles["back"] = at(toe_foot[1]), at(heel_foot[1])
    gear = Shape(solid)._with_roles(roles)
    if not gear.is_valid or gear.volume <= 0:
        raise TuleError("the gear's faces do not make a valid solid", op=op)
    # onto its back face: axis +z, back face on z = 0, apex above at heel_foot's depth
    return gear.rotate(x=180).at(0, 0, heel_foot[1]), float(heel_foot[1])


def _faces(shape):
    return [f for f in _explore(shape, TopAbs_FACE)]


def _explore(shape, kind):
    ex = TopExp_Explorer(shape, kind)
    while ex.More():
        yield ex.Current()
        ex.Next()


def bevel_gear(module: float, teeth: int, width: float, *, cone_angle: float | None = None,
               mating_teeth: int | None = None, shaft_angle: float = 90, pressure_angle: float = 20,
               profile_shift: float = 0.0, backlash: float = 0.0, bore: float | None = None) -> Shape:
    """A straight bevel gear with spherical involute teeth, standing on its back face at z = 0.

    ``module`` is at the heel (outer end): pitch diameter ``module * teeth``
    there. ``width`` is the face width along the cone (at most a third of the
    cone distance R = module·teeth / (2 sin δ) is usual). The pitch cone
    angle δ is ``cone_angle`` (degrees) or comes from ``mating_teeth`` and
    ``shaft_angle`` (default 90°: tan δ = teeth / mating_teeth).

    The axis is +z and a tooth is centered on +x. The pitch apex, where the
    cones meet, is at (0, 0, apex) with apex = R cos δ + (2.25 - x)·module·sin δ;
    ``bevel_pair`` gives it as ``pair.apex``. Faces: ``flanks``, ``tips``,
    ``roots``, ``heel`` and ``toe`` (the back cones at the ends of the teeth),
    ``back`` (z = 0), ``front`` (toward the apex) and ``bore``.
    """
    op = "bevel_gear"
    delta = _cone_angle(teeth, cone_angle, mating_teeth, shaft_angle, op)
    g = _Bevel(module, teeth, pressure_angle, delta, profile_shift, backlash, op)
    gear, _ = _bevel_solid(g, width, bore, op)
    return gear.named(f"bevel gear m{module:g} z{int(teeth)}")


# ---------------------------------------------------------------- pairs


@dataclass(frozen=True)
class BevelPair:
    """Two bevel gears that mesh, built and placed: ``gear1`` as made, ``gear2`` turned and moved to mesh.

    ``cone_angles`` (degrees, they add up to ``shaft_angle``), ``ratio``
    (z2 / z1), ``contact_ratio``, ``cone_distance`` (apex to heel along the
    pitch cone), ``apex`` (each gear's apex height above its back face; the
    apexes coincide once placed), ``backlash`` (at the heel, the sum of the
    gears'). ``place`` and ``rotation`` mesh gear 2 with gear 1 turned.
    """

    module: float
    teeth: tuple
    shaft_angle: float
    cone_angles: tuple
    ratio: float
    contact_ratio: float
    cone_distance: float
    apex: tuple
    backlash: float
    gear1: Shape = field(repr=False, compare=False)
    gear2: Shape = field(repr=False, compare=False)

    def rotation(self, angle: float = 0, turn: float = 0) -> float:
        """Degrees to turn gear 2 about its own axis (before ``place`` moves it) to mesh with gear 1
        turned by ``turn`` degrees, gear 2 standing at ``angle`` degrees around gear 1's axis."""
        z1, z2 = self.teeth
        return round((180 - 180 / z2 + (angle - turn) * z1 / z2) % (360 / z2), 12)

    def place(self, gear: Shape, angle: float = 0, turn: float = 0) -> Shape:
        """``gear`` (gear 2 as ``bevel_gear`` makes it) turned to mesh, its apex on gear 1's, its axis
        at ``shaft_angle`` to gear 1's, in direction ``angle`` (degrees about z; 0: toward +x)."""
        a1, a2 = self.apex
        return (gear.rotate(z=self.rotation(angle, turn)).at(0, 0, -a2)
                .rotate(y=-self.shaft_angle).rotate(z=angle).at(0, 0, a1))


def bevel_pair(module: float, z1: int, z2: int, width: float, *, shaft_angle: float = 90,
               pressure_angle: float = 20, profile_shift: tuple = (0.0, 0.0), backlash: tuple = (0.0, 0.0),
               bore: tuple = (None, None)) -> BevelPair:
    """Check two bevel gears, make them and mesh them: shafts at ``shaft_angle`` degrees, apexes together.

    Returns a :class:`BevelPair` (``gear1``, ``gear2`` placed, ``cone_angles``,
    ``ratio``, ``contact_ratio``, ``place``). Raises :class:`TuleError` when
    one gear's tips would cut into the other's (undercut) roots or reach past
    its base cone, or when the contact ratio is under 1.2. ``profile_shift``
    is a long-addendum pinion: (x, -x). ``profile_shift``, ``backlash`` and
    ``bore`` are per gear, as given to ``bevel_gear``.
    """
    op = "bevel_pair"
    x1, x2 = (float(v) for v in profile_shift)
    b1, b2 = (float(v) for v in backlash)
    if abs(x1 + x2) > 1e-9:
        raise TuleError(f"bevel profile shifts must be opposite, not ({x1:g}, {x2:g})", op=op,
                        hint=f"profile_shift=({x1:g}, {-x1:g}): other sums would change the pitch cones")
    d1 = _cone_angle(z1, None, z2, shaft_angle, op)
    d2 = shaft_angle - d1
    if not 0 < d2 < 90:
        raise TuleError(f"gear 2's pitch cone angle {d2:.4g}° is outside 0°–90°", op=op,
                        hint="internal and crown bevel gears are not supported; use a smaller shaft angle")
    pair, problem = _mesh(module, z1, z2, shaft_angle, d1, d2, pressure_angle, x1, x2, b1, b2)
    if problem is not None:
        message, hint, data = problem
        if hint is None:  # interference: look for the long-addendum shift that cures it
            hint = "use more teeth on the smaller gear"
            s = 1 if z1 <= z2 else -1
            for k in range(1, 21):
                y = x1 + 0.05 * k * s
                try:
                    ok = _mesh(module, z1, z2, shaft_angle, d1, d2, pressure_angle, y, -y, b1, b2)[1] is None
                except TuleError:
                    break
                if ok:
                    hint = f"profile_shift=({y:.2f}, {-y:.2f}) works; or " + hint
                    break
        raise TuleError(message, op=op, hint=hint, **data)
    g1, g2, eps = pair
    gear1, a1 = _bevel_solid(g1, width, bore[0], op)
    gear2, a2 = _bevel_solid(g2, width, bore[1], op)
    p = BevelPair(module=module, teeth=(int(z1), int(z2)), shaft_angle=shaft_angle, cone_angles=(d1, d2),
                  ratio=z2 / z1, contact_ratio=eps, cone_distance=g1.R, apex=(a1, a2), backlash=b1 + b2,
                  gear1=gear1.named(f"bevel gear m{module:g} z{int(z1)}"),
                  gear2=gear2.named(f"bevel gear m{module:g} z{int(z2)}"))
    return replace(p, gear2=p.place(p.gear2))


def _frames(d1, d2):
    """Gear 2's frame (columns x, y, z) in gear 1's (apex at the origin, axes into the gears), meshing at +x."""
    e1 = np.array([math.sin(d1), 0.0, math.cos(d1)])
    c = np.array([math.cos(d1), 0.0, -math.sin(d1)])
    z2 = e1 * math.cos(d2) + c * math.sin(d2)
    x2 = (z2 * math.cos(d2) - e1) / math.sin(d2)            # gear 2's +x points away from the mesh
    return e1, c, np.column_stack([x2, np.cross(z2, x2), z2])


def _mesh(m, z1, z2, shaft_angle, d1, d2, pressure_angle, x1, x2, b1, b2):
    """``((g1, g2, contact ratio), None)``, or ``(..., (message, hint, data))`` with hint None for interference."""
    op = "bevel_pair"
    g1 = _Bevel(m, z1, pressure_angle, d1, x1, b1, op)
    g2 = _Bevel(m, z2, pressure_angle, d2, x2, b2, op)
    a = g1.alpha
    # the path of contact on the great circle of action through the pitch line, between the tip cones
    sa, rd1, rd2 = math.sin(a), math.radians(d1), math.radians(d2)
    t1 = math.atan2(sa * math.sin(rd1), math.cos(rd1))      # where it touches gear 1's base cone
    t2 = -math.atan2(sa * math.sin(rd2), math.cos(rd2))     # and gear 2's
    s1 = t1 - math.acos(math.cos(g1.da) / math.hypot(math.cos(rd1), sa * math.sin(rd1)))  # gear 1's tips
    s2 = t2 + math.acos(math.cos(g2.da) / math.hypot(math.cos(rd2), sa * math.sin(rd2)))  # gear 2's tips
    eps = (s2 - s1) / (2 * math.pi * math.sin(g1.db) / z1)
    names = (f"the {z1}-tooth gear", f"the {z2}-tooth gear")
    data = {"contact_ratio": round(eps, 4), "cone_angles": (round(d1, 4), round(d2, 4))}
    out = (g1, g2, eps)
    if s2 > t1:
        return out, (f"{names[1]}'s tips reach past {names[0]}'s base cone (involute interference)", None, data)
    if s1 < t2:
        return out, (f"{names[0]}'s tips reach past {names[1]}'s base cone (involute interference)", None, data)
    hit = _interference(g1, g2, d1, d2)
    if hit:
        dst, r_hit = hit
        who = names[0] if dst is g1 else names[1]
        if r_hit < math.sin(dst.form) * dst.R + 0.02 * m:
            what = "undercut roots" if dst.undercut else "roots, below where its involute starts"
            return out, (f"the other gear's tips cut into {who}'s {what} (radius {r_hit:.4g} at the heel)", None, data)
        return out, (f"the other gear's tips hit {who}'s teeth outside the line of contact (radius {r_hit:.4g} at the heel)",
                     None, data)
    if eps < 1.2:
        return out, (f"contact ratio {eps:.3f} is under 1.2: the next pair of teeth may not engage in time",
                     "use more teeth, or less profile shift", data)
    return out, None


def _interference(g1, g2, d1, d2):
    """Turn gear 1 through a pitch, gear 2 with it, and look for tip points inside the other's teeth.

    Returns (the gear cut into, radius at the heel) or None. The spur gear's check, on the sphere.
    """
    _, _, frame2 = _frames(math.radians(d1), math.radians(d2))
    turns = np.linspace(0, 2 * math.pi / g1.z, 121)
    rot1 = turns
    rot2 = math.radians(180 - 180 / g2.z) - turns * g1.z / g2.z
    eye = np.eye(3)
    for src, s_rot, s_f, dst, d_rot, d_f in ((g1, rot1, eye, g2, rot2, frame2), (g2, rot2, frame2, g1, rot1, eye)):
        hw = float(src.half_angle(src.da))
        ang = (np.linspace(-hw, hw, 7)[None, None, :] + (2 * math.pi / src.z) * np.arange(src.z)[None, :, None]
               + s_rot[:, None, None])
        p = np.stack([math.sin(src.da) * np.cos(ang), math.sin(src.da) * np.sin(ang),
                      np.full_like(ang, math.cos(src.da))], axis=-1)
        q = p @ s_f.T @ d_f                                   # into the other gear's frame
        polar = np.arctan2(np.hypot(q[..., 0], q[..., 1]), q[..., 2])
        pitch = 2 * math.pi / dst.z
        th = (np.arctan2(q[..., 1], q[..., 0]) - d_rot[:, None, None] + pitch / 2) % pitch - pitch / 2
        b = dst.boundary()
        b = b[np.argsort(b[:, 0])]
        inside = (polar > b[0, 0]) & (polar < b[-1, 0])
        depth = np.where(inside, (np.interp(polar, b[:, 0], b[:, 1]) - np.abs(th)) * np.sin(polar) * dst.R, -1.0)
        if depth.max() > _SLACK * g1.m:
            return dst, float(np.sin(polar.flat[int(np.argmax(depth))]) * dst.R)
    return None
