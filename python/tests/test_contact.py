"""tule.touch: followers placed by contact with the driver, against analytic motion."""

import math
import time

import numpy as np
import pytest
from OCP.BRepAdaptor import BRepAdaptor_Curve
from OCP.TopoDS import TopoDS

from tule import *
from tule._occ import subshapes


def test_eccentric_cam_lifts_a_flat_follower_by_its_eccentricity():
    R, E = 20, 5
    cam = circle(R).at(E, 0).extrude(10)
    follower = box(60, 5, 10).at(-30, 0, 0)  # flat face at y = 0, moving along y
    start = time.monotonic()
    for a in range(0, 360, 10):
        c = touch(cam.rotate(z=a), lambda y: follower.at(0, y, 0), 40, 0)
        assert c.t == pytest.approx(R + E * math.sin(math.radians(a)), abs=0.01)
        assert 0 < c.gap <= 1e-4
        assert math.dist(*c.points) == pytest.approx(c.gap, abs=1e-7)
        assert c.shape.distance(cam.rotate(z=a)) == pytest.approx(c.gap, abs=1e-7)
    assert time.monotonic() - start < 10


def outline(shape, n=3000):
    """Points on a prism's outline at z = 0, sampled from its B-rep edges."""
    pts = []
    for e in subshapes(shape.wrapped, "edge"):
        c = BRepAdaptor_Curve(TopoDS.Edge(e))
        for u in np.linspace(c.FirstParameter(), c.LastParameter(), n):
            pts.append(c.Value(u).Coord())
    pts = np.array(pts)
    return pts[np.abs(pts[:, 2]) < 1e-6, :2]


def test_spline_cam_lifts_a_roller_as_its_outline_does():
    cam = spline([(25, 0), (14, 16), (-2, 18), (-16, 9), (-17, -8), (-4, -17), (13, -14)]).extrude(8)
    r = 6
    roller = cylinder(r, 8)
    for a in (0, 50, 130, 220, 300):
        driven = cam.rotate(z=a)
        c = touch(driven, lambda y: roller.at(0, y, 0), 60, 0)
        p = outline(driven)
        p = p[np.abs(p[:, 0]) < r]
        expected = np.max(p[:, 1] + np.sqrt(r * r - p[:, 0] ** 2))  # the roller's center on the highest point under it
        assert c.t == pytest.approx(expected, abs=0.01)


def test_gear_pair_ratio_is_measured_from_the_flanks():
    z1, z2 = 20, 30
    pair = gear_pair(1.5, z1, z2, backlash=(0.15, 0.15))
    pinion = spur_gear(1.5, z1, 8, bore=6, backlash=0.15)
    wheel = spur_gear(1.5, z2, 8, bore=6, backlash=0.15)
    r0 = pair.rotation()
    turns = []
    for a in np.linspace(0, 360 / z1, 7):  # one tooth pitch of the pinion
        start = r0 - a * z1 / z2  # centered in the backlash, then turned back until a flank touches
        c = touch(pinion.rotate(z=a), lambda r: wheel.rotate(z=r).at(pair.distance, 0, 0), start, start + 3)
        turns.append(c.t)
    assert (360 / z1) / (turns[0] - turns[-1]) == pytest.approx(z2 / z1, rel=0.005)
    assert np.ptp(np.array(turns) + np.linspace(0, 360 / z1, 7) * z1 / z2) < 0.01  # involutes: a steady ratio


def test_cycloidal_disc_turns_back_once_per_pins_minus_one_crank_turns():
    pins, e = 12, 1.5
    drive = cycloidal_drive(pins, 60, 6, e, 6)
    loose = cycloidal_drive(pins, 60, 5.8, e, 6).pins  # 0.2 mm smaller pins: the disc has play
    turns = []
    for a in (0, 120, 240, 360):
        x, y = e * math.cos(math.radians(a)), e * math.sin(math.radians(a))
        c = touch(loose, lambda r: drive.disc.rotate(z=r).at(x, y, 0), drive.rotation(a), drive.rotation(a) + 3)
        turns.append(c.t)
    assert 360 / (turns[0] - turns[-1]) == pytest.approx(pins - 1, rel=0.005)


def test_a_thin_wall_stops_a_fast_part():
    wall = box(0.3, 40, 40, center=True).at(50, 0, 0)
    ball = sphere(4)
    c = touch(wall, lambda x: ball.at(x, 0, 0), 0, 100)  # one mesh step would jump the wall
    assert c.t == pytest.approx(50 - 0.15 - 4, abs=1e-3)


def test_errors_say_what_to_change():
    plate = box(20, 20, 2)
    peg = cylinder(2, 10)
    with pytest.raises(TuleError, match="already touch or overlap at start"):
        touch(plate, lambda z: peg.at(5, 5, z), 0, 10)
    with pytest.raises(TuleError, match="never touch") as e:
        touch(plate, lambda z: peg.at(5, 5, z), 10, 20)
    assert e.value.data["gap"] == pytest.approx(18, abs=0.05)
    assert touch(plate, lambda z: peg.at(5, 5, z), 10, -5).t == pytest.approx(2, abs=1e-3)
