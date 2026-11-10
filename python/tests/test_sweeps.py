"""Sweeps: why one fails."""

import numpy as np
from tule import *


def test_a_failed_sweep_says_where_the_path_is_at_fault():
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeEdge, BRepBuilderAPI_MakeWire
    from OCP.BRepTools import BRepTools
    from OCP.gp import gp_Pnt

    from tule._occ import static
    from tule.sweeps import _path, _why

    ring = static(BRepTools, "OuterWire")(circle(2).faces[0])
    kink = BRepBuilderAPI_MakeWire(BRepBuilderAPI_MakeEdge(gp_Pnt(0, 0, 0), gp_Pnt(0, 0, 20)).Edge(),
                                   BRepBuilderAPI_MakeEdge(gp_Pnt(0, 0, 20), gp_Pnt(20, 0, 20)).Edge()).Wire()
    assert "turns 90° at (0, 0, 20)" in _why(kink, ring)
    assert "radius 0.5 mm" in _why(_path([(0, 0, 0), (0, 0, 10, 0.5), (10, 0, 10)], False), ring)
    assert _why(_path([(0, 0, 0), (0, 0, 10, 5), (10, 0, 10)], False), ring) == ""


def test_sweep_places_an_offset_profile_as_extrude_does():
    pr = rect(2, 2).translate(5, 0)
    lo, hi = sweep(pr, [(0, 0, 0), (0, 0, 10)]).bounds
    elo, ehi = pr.extrude(10).bounds
    assert np.allclose(list(lo), list(elo), atol=1e-6) and np.allclose(list(hi), list(ehi), atol=1e-6)
