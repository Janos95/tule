"""Shared helpers: frame, looks and meshing for the Raptor model (all bodies are tule.sdf implicits).

FRAME: millimetres, z = 0 at the nozzle exit plane, z up the engine axis (the engine stands on its
nozzle as in the photos). The fuel turbopump sits on +x; the front of the photos (evidence/img/012,
camera on -y) sees it on the right.
"""
import math

import numpy as np
from tule.errors import TuleError
from tule import Mesh, sdf

# looks: matte black printed housings and bell, satin metal flanges, gimbal and fasteners (no markings)
BELL = ("#2f3236", dict(metal=0.25, roughness=0.75))
HOUSING = ("#3a3e43", dict(metal=0.35, roughness=0.6))
DUCT = ("#34383c", dict(metal=0.3, roughness=0.65))
SATIN = ("#9a9ea3", dict(metal=0.8, roughness=0.35))
STEEL = ("#c9c6c0", dict(metal=1.0, roughness=0.3))
BRONZE = ("#6b5a48", dict(metal=0.7, roughness=0.5))  # the rust-brown gimbal ring (012, 026)
COPPER = ("#b0643c", dict(metal=0.8, roughness=0.4))  # inferred copper-alloy liner (internals)
INTERNAL = ("#7d858c", dict(metal=0.5, roughness=0.5))  # inferred internal parts


def polar(r: float, deg: float, z: float) -> tuple:
    a = math.radians(deg)
    return (r * math.cos(a), r * math.sin(a), z)


def ring(R: float, a: float, z: float) -> sdf.Implicit:
    """A torus about the engine axis: ring radius R, tube radius a, at height z."""
    return sdf.torus(R, a).at(0, 0, z)


def disc(r: float, z0: float, z1: float) -> sdf.Implicit:
    return sdf.cylinder(r, z1 - z0).at(0, 0, z0)


def lathe(points) -> sdf.Implicit:
    """A revolved solid from (r, z) outline points (closed polygon, r >= 0)."""
    return sdf.polygon(points).revolve()


def bolt_circle(n: int, r: float, z0: float, d: float, h: float, phase: float = 0.0) -> sdf.Implicit:
    """n bolt heads (hex-ish: cylinders) on a circle, one expression."""
    b = sdf.cylinder(d / 2, h).at(r, 0, z0)
    if phase:
        b = b.rotate(z=phase)
    return b.repeat_polar(n)


def mesh(body: sdf.Implicit, name: str, look, cell: float, simplify: float | None = None, **kw) -> Mesh:
    """The body as a named mesh with its look, no crease lines (dual contouring's are noise)."""
    m = body.to_mesh(cell, **kw)
    if simplify:
        # dual contouring now and then leaves a non-manifold vertex, which simplify refuses: shift the grid
        # slightly and try again, else keep the full mesh
        for attempt in (cell * 0.97, cell * 1.03, None):
            try:
                m = m.simplify(simplify)
                break
            except TuleError:
                if attempt is None:
                    print(f"  ({name}: not simplified, mesh has a non-manifold vertex)", flush=True)
                    break
                m = body.to_mesh(attempt, **kw)
    color, mat = look
    out = Mesh(m.points, m.indices, name=name, edges=[], history=m.history)
    return out.named(name, color).with_material(**mat)
