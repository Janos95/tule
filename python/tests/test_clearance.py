"""tule.clearance: mechanisms through their motion, checked against exact B-rep measures."""

import json
import math
import os
import subprocess
import sys

import pytest

from tule import *


@pytest.fixture
def outdir(tmp_path, monkeypatch):
    d = tmp_path / "outputs"
    monkeypatch.setenv("TULE_OUTPUTS", str(d))
    return d


def part_at(frames, i, path):
    """The part at ``path`` in frame i (``shaft#2``: the second part of that name)."""
    name, _, n = path.partition("#")
    return [s for p, s in frames[i].parts() if p == name][int(n or 1) - 1]


def check_exact(frames, p):
    """The pair's numbers are the B-rep's at its frame."""
    a, b = part_at(frames, p.frame, p.a), part_at(frames, p.frame, p.b)
    assert p.gap == pytest.approx(a.distance(b), abs=1e-6)
    if p.overlapping:
        assert p.overlap == pytest.approx((a & b).volume, rel=1e-6)
    elif p.gap > 0:
        assert math.dist(p.points[0], p.points[1]) == pytest.approx(p.gap, abs=1e-6)


def test_gear_pair_turning_is_a_contact_with_the_backlash():
    pair = gear_pair(1.5, 20, 30, backlash=(0.05, 0.05))
    g1 = spur_gear(1.5, 20, 8, bore=6, backlash=0.05).named("g1")
    g2 = spur_gear(1.5, 30, 8, bore=6, backlash=0.05).named("g2")
    pitch = 360 / 20
    frames = [Assembly(g1.rotate(z=t), pair.place(g2, turn=t), name="gears") for t in (pitch * k / 8 for k in range(9))]
    r = clearance(frames, contacts=[("g1", "g2")])
    assert r.ok and [(p.a, p.b, p.status) for p in r] == [("gears/g1", "gears/g2", "contact")]
    p = r.worst
    assert 0.03 < p.gap < 0.1  # the backlash, along the line of action
    assert p.frames == tuple(range(9))
    check_exact(frames, p)
    assert clearance(frames).worst.status == "gap"  # not listed as a contact: a gap, no overlap


C, PIN_R, CLR = 40.0, 3.0, 0.1  # a Geneva drive: crank at the origin, a 4-slot wheel at (C, 0)
A = B = C * math.sqrt(0.5)


def geneva(press_fit=0.0):
    base = box(110, 70, 4).at(-25, -35, -4) - cylinder(3, 10, center=True) - cylinder(3, 10, center=True).at(C, 0, 0)
    crank = (circle(8) | rect(A, 8).at(0, -4)).extrude(3).at(0, 0, 1) - cylinder(3, 20, center=True)
    wheel = circle(B).extrude(4)
    for k in range(4):
        slot = rect(B - (C - A) + PIN_R + 6, 2 * (PIN_R + CLR)).at(C - A - PIN_R - 1, -(PIN_R + CLR)).extrude(10).at(0, 0, -3)
        wheel -= slot.rotate(z=180 + 90 * k)
    wheel -= cylinder(3, 20, center=True)
    parts = dict(base=base, crank=crank, pin=cylinder(PIN_R, 9).at(A, 0, 4), wheel=wheel.at(0, 0, 7),
                 shaft=cylinder(3 + press_fit, 16).at(0, 0, -4))

    def wheel_angle(t):
        t = (t + 180) % 360 - 180
        r = math.radians(t)
        return math.degrees(math.atan2(A * math.sin(r), A * math.cos(r) - C)) - 180 if abs(t) <= 45 else 45.0

    def pose(t):
        return Assembly(parts["base"].named("base"), parts["shaft"].named("shaft"), parts["shaft"].at(C, 0, 0).named("shaft"),
                        parts["crank"].rotate(z=t).named("crank"), parts["pin"].rotate(z=t).named("pin"),
                        parts["wheel"].rotate(z=wheel_angle(t)).at(C, 0, 0).named("wheel"), name="geneva")

    return pose


def test_geneva_pin_runs_in_the_slot_with_its_clearance():
    pose = geneva()
    angles = list(range(-180, 180, 10))
    frames = {f"{t} deg": pose(t) for t in angles}
    r = clearance(frames, contacts=[("shaft", "*"), ("crank", "pin")], below=0.5)
    print(r)
    assert r.ok
    slot = next(p for p in r if (p.a, p.b) == ("geneva/pin", "geneva/wheel"))
    assert slot.status == "gap" and slot.gap == pytest.approx(CLR, abs=1e-6)
    assert slot.label == f"{angles[slot.frame]} deg"
    assert all(abs(angles[i]) <= 45 for i in slot.frames) and len(slot.frames) >= 5  # only while engaged
    check_exact(list(frames.values()), slot)
    shafts = [p for p in r if "shaft" in p.a + p.b]
    assert shafts and all(p.contact and p.gap == 0 and not p.overlapping for p in shafts)  # a pin in a bore: touching
    assert {p.b for p in shafts if p.a == "geneva/shaft#2"} == {"geneva/wheel"}  # two parts of one name


def test_press_fit_pin_is_a_contact_or_a_clash():
    pose = geneva(press_fit=0.02)
    frames = [pose(t) for t in (-40, 0, 90)]
    r = clearance(frames, contacts=[("shaft", "*"), ("crank", "pin")])
    fits = [p for p in r if p.contact and p.overlapping]
    assert r.ok and {(p.a, p.b) for p in fits} >= {("geneva/base", "geneva/shaft"), ("geneva/shaft#2", "geneva/wheel")}
    for p in fits:
        check_exact(frames, p)
    hard = clearance(frames, contacts=[("crank", "pin")])
    assert not hard.ok and {(p.a, p.b) for p in hard.clashes} == {(p.a, p.b) for p in fits} | {("geneva/shaft", "geneva/crank")}
    assert clearance(frames, ignore=[("shaft*", "*")], contacts=[("crank", "pin")]).ok


def test_a_clash_is_found_at_its_frame():
    lever = box(40, 6, 5).at(0, -3, 0).named("lever")
    post = cylinder(4, 10).at(0, 35, -2).named("post")
    angles = list(range(0, 181, 5))
    frames = [Assembly(lever.rotate(z=a), post, name="m") for a in angles]
    r = clearance(frames)
    assert not r.ok and len(r) == 1
    p = r.clashes[0]
    assert (p.a, p.b, p.status, p.gap) == ("m/lever", "m/post", "clash", 0)
    assert angles[p.frame] == 90 and p.overlap > 100  # the post is at 90 degrees
    assert {angles[i] for i in p.frames} == set(range(80, 101, 5))  # |35 cos a| < 3 + 4
    check_exact(frames, p)
    assert p.points[0] == pytest.approx((0, 35, 2.5), abs=0.5)  # the overlap's center
    assert "clash" in str(r) and "m/lever × m/post" in str(r)


def test_a_part_inside_another_overlaps():
    r = clearance(Assembly(box(20).named("block"), sphere(2).at(10, 10, 10).named("ball")))
    assert r.clashes[0].overlap == pytest.approx(sphere(2).volume, rel=1e-6)


def test_frames_from_a_sequence_and_its_folder(outdir):
    lever = box(40, 6, 5).at(0, -3, 0).named("lever")
    post = cylinder(4, 10).at(0, 35, -2).named("post")
    with outputs.sequence("sweep") as seq:
        for a in range(0, 181, 15):
            seq.add(Assembly(lever.rotate(z=a), post, name="m"), label=f"{a} deg", angle=a)
    for r in (clearance(seq), clearance(str(outdir / "sweep")), clearance("sweep")):
        p = r.clashes[0]
        assert (p.a, p.b, p.label) == ("lever", "post", "90 deg")
        assert p.overlap == pytest.approx((lever.rotate(z=90) & post).volume, rel=1e-5)
    with pytest.raises(TuleError, match="not placed parts"):
        with outputs.sequence("single") as single:
            single.add(box(3))
        clearance(single)


def test_show_marks_the_worst_frame(outdir):
    lever = box(40, 6, 5).at(0, -3, 0).named("lever")
    post = cylinder(4, 10).at(0, 35, -2).named("post")
    wall = box(2, 40, 5).at(40.3, -20, 0).named("wall")  # 0.3 mm from the lever's end at 0 degrees
    frames = {f"{a} deg": Assembly(lever.rotate(z=a), post, wall) for a in range(0, 91, 10)}
    r = clearance(frames)
    assert [(p.status, p.frame) for p in r] == [("clash", 9), ("gap", 0)]
    assert r.pairs[1].gap == pytest.approx(0.3)
    folder = r.show("sweep-clearance")
    meta = json.load(open(os.path.join(folder, "output.json")))
    assert meta["kind"] == "overlay" and meta["title"] == "Clearance: 1 clash"
    assert {f["severity"] for f in meta["faces"].values()} == {"error"} and len(meta["arrows"]) == 1
    assert meta["info"]["shown"] == "frame 9 (90 deg)"
    rows = json.load(open(os.path.join(folder, "clearance.json")))
    assert [(x["status"], x["a"], x["b"]) for x in rows] == [("clash", "lever", "post"), ("gap", "lever", "wall")]
    meta = json.load(open(os.path.join(r.show("sweep-clearance", frame=0), "output.json")))
    assert {f["severity"] for f in meta["faces"].values()} == {"warning"} and len(meta["faces"]) >= 2
    assert meta["arrows"][0]["label"] == "lever × wall: 0.3 mm"


def test_closest_points():
    gap, pa, pb = box(10).closest(box(10).at(15, 3, 0))
    assert gap == pytest.approx(5) and pa.x == pytest.approx(10) and pb.x == pytest.approx(15)
    assert box(10).closest(sphere(1).at(5, 5, 5))[0] == 0


def test_clashes_and_gaps_keep_their_results_on_gears():
    pair = gear_pair(1.5, 20, 30, backlash=(0.05, 0.05))
    g1 = spur_gear(1.5, 20, 8, bore=6, backlash=0.05).rotate(z=3).named("g1")
    g2 = pair.place(spur_gear(1.5, 30, 8, bore=6, backlash=0.05), turn=3).named("g2")
    assert Assembly(g1.rotate(z=-3), g2).clashes()[0][2] == pytest.approx((g1.rotate(z=-3) & g2).volume)  # out of mesh
    asm = Assembly(g1, g2, cylinder(3, 20).at(0, 0, -6).named("shaft"))
    assert asm.clashes() == []
    gaps = asm.gaps(below=0.5)
    assert [(a, b) for a, b, _ in gaps] == [("g1", "shaft"), ("g1", "g2")]
    assert gaps[1][2] == pytest.approx(g1.distance(g2), abs=1e-9)


def test_needs_fcl_only_when_checking():
    code = ("import sys; sys.modules['fcl'] = None\n"
            "from tule import *\n"
            "try:\n    Assembly(box(1), box(1).at(2, 0, 0)).gaps()\n"
            "except TuleError as e:\n    print(e)\n")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
    assert "python-fcl" in out and "pip install python-fcl" in out
