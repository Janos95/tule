"""Stage 3: ducts and lines.

* Transfer duct (Ø ≈ 0.11 m, ROUND-1 PH-4): from the fuel turbopump's discharge flange it drops in one slant
  across the front of the chamber (012, 004) into the front of the lower chamber ring at 1.435 m, in a plane
  near y ≈ -270 (007, 026: a straight drop with sharp bends).
  Its function (methane to the regen inlet manifold) is inferred (UNKNOWN §7).
* Two small lines from the disc down to the bell-top ring and the throat ring (026: thin pipes on the
  fuel side; 012: a thin pipe on the right down to the bell-top ring).
* Hot-gas manifold: inside the disc (lib/internals.py), not a duct: nothing is visible (UNKNOWN §7).
"""
from tule import sdf

from lib.common import polar

DUCT_R = 54.0   # Ø ≈ 0.11 m across the slanted leg in 012, ratio to the preburner can in 026 (ROUND-1 PH-4)


WALL = 4.0


def _path():
    """012 and 004: from the flange a nearly straight slant down to the front of the lower chamber ring;
    007 and 026 see it as a straight drop with sharp bends, so it lies in a plane near y ≈ -340 (PH-1, PH2-1)."""
    return [
        (590, -228, 2167),             # the discharge flange's face (pumps.fuel_turbopump)
        (585, -340, 2167),
        (575, -342, 2050),
        (420, -335, 1900),             # outside the disc's edge (007, 026; ROUND-2 PH2-1)
        (300, -315, 1640),
        (200, -285, 1500),
        polar(226, -65, 1440),         # into the lower chamber ring (centre 1.435 m), front right of the axis
    ]


def transfer_duct() -> sdf.Implicit:
    """Hollow (5 mm wall), open where it enters the ring."""
    duct = sdf.tube(_path(), r=DUCT_R, wall=WALL, bend=110)
    flange = sdf.cylinder(95, 24).rotate(x=90).at(590, -228, 2167)  # Ø 0.19 m (012; ROUND-3 PH3-1)
    collar = sdf.sphere(62).at(*polar(234, -65, 1440))
    return (duct | flange | collar) - transfer_duct_bore()


def transfer_duct_bore() -> sdf.Implicit:
    # the bore stops at the ring tube's centre line, so its round end stays inside the ring cavity (INT-9)
    p = _path()
    a, b = p[-2], p[-1]
    path = p[:-1] + [tuple(a[i] + 0.8 * (b[i] - a[i]) for i in range(3))]  # on the duct's own centre line
    return sdf.tube(path, r=DUCT_R - WALL, bend=110, ends="round")


LINE_A = [polar(300, 40, 1935), polar(318, 40, 1700), polar(345, 34, 1420), polar(372, 30, 1175)]


def small_lines() -> sdf.Implicit:
    """Line a is the bell-top ring's return (hollow, ROUND-2 INT-16); line b a closed tap on the throat ring."""
    a = sdf.tube(LINE_A, r=14, bend=60) - line_a_bore()
    b = sdf.tube([polar(290, -30, 1930), polar(314, -29, 1880), polar(314, -28, 1775), polar(232, -26, 1665)], r=11, bend=40)  # clear of the upper ring (PH3-2)
    tee = sdf.sphere(22).at(*polar(372, 30, 1175)) - line_a_bore()
    return a | b | tee


def line_a_bore() -> sdf.Implicit:
    """From the bell-top ring cavity up line a and through the disc into the coolant return annulus."""
    # through every point of line a with the tube's own bend, so it stays inside the tube (ROUND-3 INT-21)
    return sdf.tube([polar(382, 30, 1152)] + LINE_A[::-1] + [polar(262, 40, 1962)], r=9, bend=60, ends="round")
