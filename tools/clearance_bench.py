"""Benchmark: tule.clearance on mechanisms of the sizes agents check.

Usage: python tools/clearance_bench.py [geneva|arm|static ...]   (all three by default)

- geneva: 30 parts (three Geneva drives with a gear pair each, all different), 361 poses
- arm:    280 parts (a six-link arm: plates, bearings, shafts, 20 screws and nuts per link), 100 poses
- static: the arm in one pose

Each prints the time to build the frames, the time and peak memory of the check, and its findings.
"""
import math
import resource
import sys
import time

from tule import Assembly, box, circle, clearance, cylinder, gear_pair, rect, regular_polygon, spur_gear


def geneva_module(k: int):
    """A Geneva drive (driver, pin, 4-slot wheel, shafts in a base) and a gear pair turning with the driver."""
    c, pin_r, clr = 36.0 + 4 * k, 3.0, 0.1
    a, b = c * math.sin(math.pi / 4), c * math.cos(math.pi / 4)
    y = 140.0 * k
    base = box(c + 2 * b + 30, 90, 4).at(-b - 12, -45, -4)
    base -= cylinder(3, 10, center=True) | cylinder(3, 10, center=True).at(c, 0, 0)
    crank = (circle(8) | rect(a, 8).at(0, -4)).extrude(3).at(0, 0, 1) - cylinder(3, 20, center=True)
    pin = cylinder(pin_r, 9).at(a, 0, 4)
    wheel = circle(b).extrude(4)
    for s in range(4):
        slot = rect(b + pin_r + 6 - (c - a), 2 * (pin_r + clr)).at(c - a - pin_r - 1, -(pin_r + clr)).extrude(10).at(0, 0, -3)
        wheel -= slot.rotate(z=180 + 90 * s)
    wheel = (wheel - cylinder(3, 20, center=True)).at(0, 0, 7)
    shaft = cylinder(3, 16).at(0, 0, -4)
    z1, z2 = 18 + 2 * k, 27 + 3 * k
    pair = gear_pair(1.5, z1, z2, backlash=(0.05, 0.05))
    g1 = spur_gear(1.5, z1, 6, bore=6, backlash=0.05).at(0, 0, -12)
    g2 = spur_gear(1.5, z2, 6, bore=6, backlash=0.05).at(0, 0, -12)
    gshaft = cylinder(3, 10).at(0, 0, -14)

    def wheel_angle(t):
        t = (t + 180) % 360 - 180
        if abs(t) > 45:
            return 45.0
        r = math.radians(t)
        return math.degrees(math.atan2(a * math.sin(r), a * math.cos(r) - c)) - 180

    def pose(t):
        return Assembly(base.named("base"), shaft.named("shaft1"), shaft.at(c, 0, 0).named("shaft2"),
                        crank.rotate(z=t).named("crank"), pin.rotate(z=t).named("pin"),
                        wheel.rotate(z=wheel_angle(t)).at(c, 0, 0).named("wheel"),
                        g1.rotate(z=t).at(0, -pair.distance, 0).named("g1"),
                        pair.place(g2, angle=90, turn=t).at(0, -pair.distance, 0).named("g2"),
                        gshaft.at(0, -pair.distance, 0).named("gshaft1"), gshaft.at(0, 0, 0).named("gshaft2"),
                        name=f"drive{k}").at(0, y, 0)

    return pose


GENEVA_CONTACTS = [("shaft*", "*"), ("gshaft*", "*"), ("g1", "g2"), ("crank", "pin")]


def arm_links():
    """Six links of an arm, each a list of parts in the link's own frame, the joint at the origin."""
    screw = (cylinder(2.75, 1.5).at(0, 0, 10) | cylinder(1.5, 14).at(0, 0, -4))
    nut = regular_polygon(6, 3.2).extrude(3).at(0, 0, -3) - cylinder(1.5, 10, center=True)
    bearing = circle(11).extrude(7) - cylinder(6, 20, center=True)
    links = []
    for k in range(6):
        plate = rect(130, 36).at(-15, -18).round(14).extrude(10)
        plate -= cylinder(11, 30, center=True) | cylinder(11, 30, center=True).at(100, 0, 0)
        holes = [(x, y) for x in range(5, 96, 10) for y in (-12, 12)]
        for x, y in holes:
            plate -= cylinder(1.6, 30, center=True).at(x, y, 0)
        parts = [plate.named("plate"), bearing.at(0, 0, 1.5).named("bearing_a"),
                 bearing.at(100, 0, 1.5).named("bearing_b"), cylinder(6, 26).at(100, 0, -8).named("shaft"),
                 box(20, 12, 8).at(40, -6, -8).named("motor")]
        for x, y in holes:
            parts += [screw.at(x, y, 0).named("screw"), nut.at(x, y, 0).named("nut")]
        links.append(parts)
    return links


def arm_base():
    bolt = cylinder(4, 3).at(0, 0, 12) | cylinder(2.5, 16).at(0, 0, -4)
    block = rect(80, 80, center=True).round(8).extrude(12).at(0, 0, -26) - cylinder(11, 80, center=True)
    for a in range(0, 360, 45):
        block -= cylinder(2.6, 80, center=True).at(30 * math.cos(math.radians(a)), 30 * math.sin(math.radians(a)), 0)
    parts = [block.named("block"), cylinder(10.5, 26).at(0, 0, -26).named("column")]
    parts += [bolt.at(30 * math.cos(math.radians(a)), 30 * math.sin(math.radians(a)), -26).named("bolt") for a in range(0, 360, 45)]
    return Assembly(*parts, name="base")


def arm_pose(base, links, angles):
    out, x, y, heading = [base], 0.0, 0.0, 0.0
    for k, (parts, a) in enumerate(zip(links, angles)):
        heading += a
        out.append(Assembly(*[p.rotate(z=heading).at(x, y, 14 * k) for p in parts], name=f"link{k}"))
        x += 100 * math.cos(math.radians(heading))
        y += 100 * math.sin(math.radians(heading))
    return Assembly(*out, name="arm")


ARM_CONTACTS = [("screw", "plate"), ("nut", "plate"), ("bearing*", "plate"), ("screw", "nut"), ("bolt", "block"),
                ("column", "*")]


def run(name, make, contacts):
    t = time.time()
    frames = make()
    frames = frames if isinstance(frames, Assembly) else list(frames)
    built = time.time() - t
    t = time.time()
    r = clearance(frames, contacts=contacts, below=0.5)
    took = time.time() - t
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (2**20 if sys.platform == "darwin" else 2**10)
    print(f"{name}: frames built in {built:.1f} s; check {took:.1f} s; peak memory {peak:.0f} MB")
    print(str(r)[:2000], "\n", flush=True)


if __name__ == "__main__":
    which = sys.argv[1:] or ["geneva", "arm", "static"]
    if "geneva" in which:
        modules = [geneva_module(k) for k in range(3)]
        run("geneva, 30 parts x 361 poses",
            lambda: (Assembly(*[m(t) for m in modules], name="drives") for t in range(-180, 181)), GENEVA_CONTACTS)
    if "arm" in which or "static" in which:
        base, links = arm_base(), arm_links()
    if "arm" in which:
        run("arm, 280 parts x 100 poses",
            lambda: (arm_pose(base, links, [0, 50 * math.sin(i / 16), -60 * math.sin(i / 11), 70 * math.cos(i / 13), 40, -30])
                     for i in range(100)), ARM_CONTACTS)
    if "static" in which:
        run("arm, 280 parts, static", lambda: arm_pose(base, links, [0, 30, -45, 60, 40, -30]), ARM_CONTACTS)
