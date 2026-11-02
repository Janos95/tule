"""Contact: drive the input, and the follower moves until it touches.

```python
for a in range(0, 360, 5):
    c = touch(cam.rotate(z=a), lambda y: follower.at(0, y, 0), 40, 0)  # lower the follower onto the cam
    print(a, c.t)                                                      # the lift, measured from the shapes
```

Each solid is meshed once (a part moved with ``.at`` or ``.rotate`` keeps its solid). The follower
advances on the meshes (FCL) no further than their gap allows, so it never passes through a thin
part, then to within ``tol`` of contact on the B-rep, checked on every pair of faces that could be closer.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from OCP.TopLoc import TopLoc_Location

from .clearance import _closest, _extrema, _fcl, _inverse, _narrow, _Solid, _split, _trsf
from .errors import TuleError

_SOLIDS: dict[int, list] = {}  # meshed solids, by their TShape: a motion meshes each part once
_KEEP = 32
MESH = 0.005  # mesh deflection, mm: finer than clearance's, to tell nearly simultaneous contacts apart


def _solid(shape, fcl):
    """A shape's meshed solid (at the origin) and its placement."""
    solid, m = _split(shape)
    bucket = _SOLIDS.setdefault(hash(solid), [])
    for s, found in bucket:
        if s.IsSame(solid):
            return found, m
    if sum(map(len, _SOLIDS.values())) >= _KEEP:
        _SOLIDS.clear()
        bucket = _SOLIDS.setdefault(hash(solid), [])
    found = _Solid(solid, fcl, MESH)
    bucket.append((solid, found))
    return found, m


@dataclass(frozen=True)
class Touch:
    """Where ``touch`` found contact: the parameter ``t``, the moving part there (``shape``), the
    B-rep ``gap`` left (0 < gap ≤ tol), and the closest ``points`` on the fixed and the moving part."""

    t: float
    gap: float
    points: tuple
    shape: object = field(repr=False, compare=False)


def touch(a, pose, start: float, end: float, tol: float = 1e-4) -> Touch:
    """The first ``t`` from ``start`` toward ``end`` where ``pose(t)``, a Shape, touches ``a``:
    gap at most ``tol`` mm, no overlap. Drive the input, let the follower come from contact, and the
    lift or ratio is measured::

        lift = touch(cam.rotate(z=a), lambda y: follower.at(0, y, 0), 40, 0).t   # lowered onto the cam
        lag = touch(pinion.rotate(z=a), lambda r: pair.place(wheel.rotate(z=r), turn=a), 0, 3).t

    Returns a :class:`Touch`: ``t``, ``shape`` (the follower there), ``gap``, ``points`` (on a, on
    it). Raises :class:`TuleError` if they touch at ``start`` (start clear of ``a``) or not by ``end``.
    Fast on parts moved per call (``.at``, ``.rotate``): each is meshed once.
    """
    from .shape import Shape

    if not isinstance(a, Shape):
        raise TuleError(f"touch takes a Shape to touch, got {type(a).__name__}", op="touch",
                        hint="several fixed parts: make them one Shape once, before the loop: Assembly(...).to_shape()")
    if not tol > 0:
        raise TuleError("tol must be positive", op="touch")
    start, end = float(start), float(end)
    if start == end:
        raise TuleError("start and end are the same", op="touch")
    fcl = _fcl()
    A, ma = _solid(a.wrapped, fcl)
    if A.empty:
        raise TuleError("the part to touch has no faces", op="touch")
    to_a = _inverse(ma)
    way = 1.0 if end > start else -1.0
    span = abs(end - start)

    def place(t):
        s = pose(t)
        if not isinstance(s, Shape):
            raise TuleError(f"pose({t:g}) gave {type(s).__name__}, not a Shape", op="touch",
                            hint="touch(cam, lambda y: follower.at(0, y, 0), 40, 0)")
        B, mb = _solid(s.wrapped, fcl)
        if B.empty:
            raise TuleError("the moving part has no faces", op="touch")
        return s, B, to_a @ mb

    def speed(t, B, rel):
        """How fast b's fastest mesh point moves against a, mm per unit of t."""
        h = 1e-6 * span * (way if (end - t) * way > 1e-6 * span else -way)
        rel2 = place(t + h)[2]
        d = rel2 - rel
        moved = B.points @ d[:3, :3].T + d[:3, 3]
        return 1.05 * float(np.sqrt((moved * moved).sum(1).max())) / abs(h)

    tracked = []  # pairs of faces found to meet first: measured at every step

    def full(t):
        """The gap on all faces, and the pair of faces it is between (None: one part holds the other)."""
        s, B, rel = place(t)
        mesh_gap, crossing, seed = _narrow(fcl, A, B, rel)
        if crossing == math.inf:
            return 0.0, *_extrema(A.shape, B.shape.Moved(TopLoc_Location(_trsf(rel))), True)[1:], s, None
        g, p, q, ij = _closest(fcl, A, B, rel, seed, faces=True)
        return g, p, q, s, ij

    def quick(t):
        """The gap between the faces the meshes put closest, and the tracked ones: no less than the gap."""
        s, B, rel = place(t)
        mesh_gap, _, seed = _narrow(fcl, A, B, rel)
        loc = TopLoc_Location(_trsf(rel))
        if mesh_gap == 0:  # the meshes cross: past contact if the faces they cross at do
            hits = fcl.CollisionResult()
            fcl.collide(A.obj, fcl.CollisionObject(B.model, fcl.Transform(rel[:3, :3], rel[:3, 3])),
                        fcl.CollisionRequest(num_max_contacts=200), hits)
            pairs = list(dict.fromkeys((A.face_of[c.b1], B.face_of[c.b2]) for c in hits.contacts))[:4]
        else:
            pairs = [tuple(seed)]
        best = None
        for fa, fb in dict.fromkeys(pairs + tracked):
            found = _extrema(A.faces()[0][fa], B.faces()[0][fb].Moved(loc))
            if best is None or found[0] < best[0]:
                best = found
            if best[0] < 1e-7:
                return 0.0, best[1], best[2], s
        if mesh_gap == 0:
            return full(t)[:4]
        return best[0], best[1], best[2], s

    def never(g):
        return TuleError(f"they never touch between {start:g} and {end:g}: {g:.4g} mm apart at {end:g}",
                         op="touch", hint="extend end, or check pose moves the part toward the other", gap=g)

    def overlap():
        return TuleError(f"they already touch or overlap at start = {start:g}", op="touch",
                         hint="start where the moving part is clear of the other (lifted off, turned back)")

    def beyond(t):
        return (t - end) * way > 0

    # on the meshes: advance by no more than the gap allows, until within the meshes' error
    t = start
    s, B, rel = place(t)
    g = _narrow(fcl, A, B, rel)[0]
    slack = A.slack + B.slack
    last = None  # the previous point, for the rate the gap closes at
    for _ in range(500):
        if g <= 2 * slack:
            break
        v = speed(t, B, rel)
        if v == 0:
            raise TuleError(f"pose(t) does not move the part at t = {t:g}", op="touch")
        nt = t + way * 0.98 * (g - slack) / v
        if beyond(nt):
            if t == end:
                raise never(g)
            nt = end
        last = (t, g)
        t = nt
        s, B, rel = place(t)
        g = _narrow(fcl, A, B, rel)[0]
    if t == start and g == 0 and full(t)[0] == 0:
        raise overlap()
    mesh_t, mesh_last = t, last

    # on the B-rep: secant steps to just short of contact (bisecting when one goes past), on the
    # closest faces first, then checked on all of them
    def solve(measure, t, last, hit):
        g, p, q, s = measure(t)
        if g == 0:
            if last is None:
                raise overlap()
            hit, t, last = t, last[0], None  # the meshes stopped past contact: back to the last clear point
            g, p, q, s = measure(t)
            if g == 0:
                raise overlap()
        v = speed(t, *place(t)[1:])
        for _ in range(100):
            if g <= tol or (hit is not None and abs(hit - t) * v <= 0.5 * tol):
                break  # within tol, or within tol of a point past contact (the measured faces may not be the closest)
            if last is not None and last[1] > g and last[0] != t:
                rate = (last[1] - g) / abs(t - last[0])
            else:
                rate = v
            nt = t + way * (g - 0.5 * tol) / rate
            if hit is not None and (nt - hit) * way >= 0:
                nt = 0.5 * (t + hit)
            if beyond(nt):
                nt = end
            if nt == t:
                if t == end:
                    raise never(g)
                break
            ng, np_, nq, ns = measure(nt)
            if ng == 0:
                hit = nt
            else:
                last = (t, g) if ng < g else None
                t, g, p, q, s = nt, ng, np_, nq, ns
                if t == end and g > tol:
                    raise never(g)
        return t, g, p, q, s, hit

    hit = None
    for _ in range(8):
        t, g, p, q, s, hit = solve(quick, t, last, hit)
        fg, fp, fq, _, ij = full(t)
        if fg > 0:
            g, p, q = fg, fp, fq
            break
        if ij is None or ij in tracked:
            t, g, p, q, s, _ = solve(lambda t: full(t)[:4], mesh_t, mesh_last, t)
            break
        tracked.append(ij)  # other faces met first: again from the meshes' last point, with them
        hit, t, last = t, mesh_t, mesh_last
    else:
        t, g, p, q, s, _ = solve(lambda t: full(t)[:4], mesh_t, mesh_last, hit)
    world = lambda x: tuple(float(c) + 0.0 for c in ma[:3, :3] @ x + ma[:3, 3])
    return Touch(float(t), float(g), (world(p), world(q)), s)
