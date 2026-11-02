import math
import os
import time

import pytest

from tule import *


def ring_band(r, dr, h):
    """A thin tube r - dr .. r + dr (to measure tooth thickness on a circle)."""
    return cylinder(r + dr, h) - cylinder(r - dr, h)


def test_spur_gear_is_light_and_named():
    g = spur_gear(1.5, 20, 8, bore=10)
    assert g.is_valid and g.name == "gear m1.5 z20"
    assert len(g.faces) == 4 * 20 + 3                       # flank, flank, tip, root per tooth; top, bottom, bore
    assert len(g.faces["flanks"]) == 40 and len(g.faces["tips"]) == 20 and len(g.faces["roots"]) == 20
    assert set(g.roles) >= {"flanks", "tips", "roots", "top", "bottom", "bore"}
    assert g.faces["bore"].info()[0]["radius"] == pytest.approx(5)


@pytest.mark.parametrize("m, z, x", [(1, 20, 0), (2, 13, 0), (1.25, 12, 0.4), (1, 40, -0.3)])
def test_spur_gear_diameters(m, z, x):
    w = 2
    g = spur_gear(m, z, w, profile_shift=x)
    da, df, d = m * (z + 2 + 2 * x), m * (z - 2.5 + 2 * x), m * z
    assert g.bounds[1].x == pytest.approx(da / 2, abs=1e-6)  # a tooth's tip on +x
    full = lambda r: math.pi * r * r * w
    assert (g & cylinder(df / 2 - 1e-3, w)).volume == pytest.approx(full(df / 2 - 1e-3), rel=1e-6)
    assert full(df / 2 + 0.02 * m) - (g & cylinder(df / 2 + 0.02 * m, w)).volume > 1e-4 * m * m * w
    assert (g - cylinder(da / 2 - 1e-3, w)).volume > 0 and (g - cylinder(da / 2 + 1e-3, w)).volume < 1e-9
    # tooth thickness on the pitch circle: m (pi/2 + 2 x tan 20°)
    s = m * (math.pi / 2 + 2 * x * math.tan(math.radians(20)))
    dr = 1e-3
    assert (g & ring_band(d / 2, dr, w)).volume == pytest.approx(z * s * 2 * dr * w, rel=2e-3)


def test_backlash_thins_teeth():
    m, z, w, b = 2, 24, 1, 0.1
    band = ring_band(m * z / 2, 1e-3, w)
    v0, v1 = (spur_gear(m, z, w) & band).volume, (spur_gear(m, z, w, backlash=b) & band).volume
    assert (v0 - v1) / (2e-3 * w) == pytest.approx(z * b, rel=2e-3)


def test_step_is_small_and_meshing_fast(tmp_path):
    g = spur_gear(1, 40, 10, bore=8)
    p = str(tmp_path / "gear.step")
    export_step(g, p)
    assert os.path.getsize(p) < 1_000_000
    t = time.time()
    mesh = tessellate(g)
    assert time.time() - t < 2 and mesh.triangles < 100_000


@pytest.mark.parametrize("z2", [40, 41])
def test_pair_meshes_without_overlap(z2):
    m, z1, w, b = 1.5, 18, 4, 0.05
    pair = gear_pair(m, z1, z2, backlash=(b, b))
    assert pair.distance == pytest.approx(m * (z1 + z2) / 2)
    assert 1.5 < pair.contact_ratio < 1.8
    g1 = spur_gear(m, z1, w, backlash=b)
    g2 = spur_gear(m, z2, w, backlash=b)
    assert (g1 & pair.place(g2)).volume < 1e-9
    assert (g1 & pair.place(g2).at(-0.2, 0, 0)).volume > 1e-3      # 0.2 closer: the teeth collide
    assert (g1 & g2.rotate(z=pair.rotation() + 360 / z2 / 2).at(pair.distance, 0, 0)).volume > 1e-3  # out of phase
    # any direction, with gear 1 turned
    assert (g1.rotate(z=7) & pair.place(g2, angle=130, turn=7)).volume < 1e-9


def test_profile_shifted_pair_avoids_undercut():
    with pytest.raises(TuleError) as e:
        gear_pair(1, 12, 30)
    assert "profile_shift=(0.20, -0.20)" in e.value.hint
    with pytest.raises(TuleError) as e:
        gear_pair(1, 30, 12)
    assert "profile_shift=(-0.20, 0.20)" in e.value.hint
    pair = gear_pair(1, 12, 30, profile_shift=(0.4, -0.4), backlash=(0.04, 0.04))
    assert pair.distance == pytest.approx(21)
    g1 = spur_gear(1, 12, 3, profile_shift=0.4, backlash=0.04)
    g2 = spur_gear(1, 30, 3, profile_shift=-0.4, backlash=0.04)
    assert (g1 & pair.place(g2)).volume < 1e-9
    # unequal shifts move the axes apart
    wide = gear_pair(1, 12, 30, profile_shift=(0.5, 0.0))
    assert wide.distance > 21 and wide.pressure_angle > 20


def test_planetary_meshes():
    m, zs, zp, zr, w, b = 1, 18, 27, 72, 3, 0.05
    sp = gear_pair(m, zs, zp, backlash=(b, b))
    rp = gear_pair(m, zp, zr, internal=True, backlash=(b, b))
    assert sp.distance == pytest.approx(rp.distance)
    sun = spur_gear(m, zs, w, backlash=b)
    planet = spur_gear(m, zp, w, backlash=b)
    ring = internal_gear(m, zr, w, backlash=b)
    assert ring.is_valid and ring.name == "ring gear m1 z72" and len(ring.faces) == 4 * zr + 3
    ring = rp.place(ring, angle=180, turn=sp.rotation()).at(sp.distance, 0, 0)
    for k in range(3):
        p = sp.place(planet, angle=120 * k)
        assert (sun & p).volume < 1e-9
        assert (ring & p).volume < 1e-9
    assert (ring & sp.place(planet).at(0.2, 0, 0)).volume > 1e-3   # pushed into the ring


@pytest.mark.parametrize("z1", [24, 25])
def test_ring_placed_around_pinion(z1):
    b = 0.05
    pair = gear_pair(1, z1, 72, internal=True, backlash=(b, b))
    pinion = spur_gear(1, z1, 2, backlash=b)
    ring = internal_gear(1, 72, 2, backlash=b)
    assert (pinion & pair.place(ring)).volume < 1e-9
    assert (pinion.rotate(z=11) & pair.place(ring, angle=-70, turn=11)).volume < 1e-9
    assert (pinion & pair.place(ring).at(0.2, 0, 0)).volume > 1e-3   # the mesh is on the pinion's far side


def test_internal_gear_diameters():
    m, z, w, x = 1, 60, 2, 0.2
    g = internal_gear(m, z, w, rim=4, profile_shift=x)
    da, df = m * (z - 2 + 2 * x), m * (z + 2.5 + 2 * x)
    assert (g & cylinder(da / 2 - 1e-3, w)).volume < 1e-9
    assert (g & cylinder(da / 2 + 1e-3, w)).volume > 0
    assert (cylinder(df / 2 + 4, w) - cylinder(df / 2 + 1e-3, w) - g).volume < 1e-9
    assert g.bounds[1].x == pytest.approx(df / 2 + 4)


def test_undercut_gear_is_valid():
    g = spur_gear(1, 8, 2)
    assert g.is_valid and len(g.faces) == 4 * 8 + 2


def test_pair_errors():
    with pytest.raises(TuleError, match="involute interference"):
        gear_pair(1, 18, 48, internal=True)
    with pytest.raises(TuleError, match="outside the line of contact"):
        gear_pair(1, 30, 36, internal=True)
    with pytest.raises(TuleError, match="contact ratio") as e:
        gear_pair(1, 10, 10, pressure_angle=25, profile_shift=(0.3, 0.3))
    assert e.value.data["contact_ratio"] < 1.2
    with pytest.raises(TuleError, match="whole number"):
        spur_gear(1, 12.5, 2)
    with pytest.raises(TuleError, match="bore"):
        spur_gear(1, 12, 2, bore=12)


# ---------------------------------------------------------------- bevel gears


def test_bevel_cone_angles():
    pair = bevel_pair(2, 20, 40, 12)
    assert pair.cone_angles == pytest.approx((26.565051, 63.434949), abs=1e-5)
    assert pair.ratio == 2 and 1.6 < pair.contact_ratio < 1.8
    assert pair.cone_distance == pytest.approx(math.hypot(20, 40))      # m/2 · √(z1² + z2²)
    other = bevel_pair(2, 20, 30, 10, shaft_angle=110)
    assert sum(other.cone_angles) == pytest.approx(110)
    assert math.sin(math.radians(other.cone_angles[0])) * 30 == pytest.approx(math.sin(math.radians(other.cone_angles[1])) * 20)


def test_bevel_gear_is_light_and_named(tmp_path):
    m, z, w = 1.5, 30, 10
    g = bevel_gear(m, z, w, mating_teeth=45, bore=10)
    assert g.is_valid and g.name == "bevel gear m1.5 z30"
    assert len(g.faces) == 4 * z + 5                        # flank, flank, tip, root per tooth; heel, toe, front, back, bore
    assert len(g.faces["flanks"]) == 60 and len(g.faces["tips"]) == 30 and len(g.faces["roots"]) == 30
    assert all(len(g.faces[r]) == 1 for r in ("heel", "toe", "front", "back", "bore"))
    d = math.atan2(z, 45)
    assert g.bounds[0].z == pytest.approx(0, abs=1e-6) and g.faces["back"].info()[0]["center"][2] == pytest.approx(0, abs=1e-6)
    assert g.bounds[1].x == pytest.approx(m * z / 2 + m * math.cos(d), abs=1e-6)  # tip on +x: d + 2 ha cos δ
    assert bevel_gear(m, z, w, cone_angle=math.degrees(d), bore=10).volume == pytest.approx(g.volume, rel=1e-9)
    p = str(tmp_path / "bevel.step")
    export_step(g, p)
    assert os.path.getsize(p) < 1_500_000
    t = time.time()
    mesh = tessellate(g)
    assert time.time() - t < 2 and mesh.triangles < 100_000


@pytest.mark.parametrize("z1, z2, shaft", [(20, 40, 90), (21, 33, 90), (20, 30, 110)])
def test_bevel_pair_meshes_without_overlap(z1, z2, shaft):
    m, w, b = 2, 9, 0.05
    pair = bevel_pair(m, z1, z2, w, shaft_angle=shaft, backlash=(b, b), bore=(8, 10))
    g1, g2 = pair.gear1, pair.gear2
    assert g1.is_valid and g2.is_valid
    apex = g1.bounds[0].z + pair.apex[0]
    R = pair.cone_distance
    d1 = math.radians(pair.cone_angles[0])
    assert apex == pytest.approx(R * math.cos(d1) + 2.25 * m * math.sin(d1))
    assert Assembly(g1.named("g1"), g2.named("g2")).clashes() == []
    wheel = bevel_gear(m, z2, w, mating_teeth=z1, shaft_angle=shaft, backlash=b, bore=10)
    # through several tooth pitches, gear 2 following by the ratio
    for turn in (0.37, 1.71, 3.5):
        turn *= 360 / z1
        assert (g1.rotate(z=turn) & pair.place(wheel, turn=turn)).volume < 1e-9
    # not far apart: the play is the backlash (b each side along the heel pitch circle)
    step = math.degrees(b / (m * z2 / 2))
    for s in (1, -1):
        assert (g1 & pair.place(wheel.rotate(z=0.5 * s * step))).volume < 1e-9
        assert (g1 & pair.place(wheel.rotate(z=1.5 * s * step))).volume > 1e-3
    if shaft == 90 and z1 == 20:
        gap = g1.distance(g2)                              # half the normal backlash, at the toe
        k = (R - w) / R
        assert gap == pytest.approx(b * math.cos(math.radians(20)) * k, rel=0.05)
        # anywhere around gear 1, with gear 1 turned
        assert (g1.rotate(z=7) & pair.place(wheel, angle=130, turn=7)).volume < 1e-9


def test_bevel_pair_undercut_hint_and_long_addendum():
    with pytest.raises(TuleError) as e:
        bevel_pair(2, 12, 36, 10)
    assert "profile_shift=(0.25, -0.25)" in e.value.hint
    b = 0.05
    pair = bevel_pair(2, 12, 36, 10, profile_shift=(0.4, -0.4), backlash=(b, b))
    wheel = bevel_gear(2, 36, 10, mating_teeth=12, profile_shift=-0.4, backlash=b)
    for turn in (0, 11.0):
        assert (pair.gear1.rotate(z=turn) & pair.place(wheel, turn=turn)).volume < 1e-9


def test_bevel_errors():
    with pytest.raises(TuleError, match="pitch cone angle") as e:
        bevel_gear(2, 20, 10)
    assert "mating_teeth" in e.value.hint
    with pytest.raises(TuleError, match="crown"):
        bevel_gear(2, 60, 10, cone_angle=89.5)
    with pytest.raises(TuleError, match="opposite"):
        bevel_pair(2, 20, 40, 10, profile_shift=(0.3, 0))
    with pytest.raises(TuleError, match="bore"):
        bevel_gear(2, 20, 10, mating_teeth=40, bore=30)
    with pytest.raises(TuleError, match="cone distance"):
        bevel_gear(2, 20, 30, mating_teeth=40)
    with pytest.raises(TuleError, match="outside 0°–90°"):
        bevel_pair(2, 20, 40, 10, shaft_angle=150)
