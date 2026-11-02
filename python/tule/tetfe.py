"""Quadratic tetrahedra for ``tule.fea``: gmsh meshes the part, NumPy and SciPy do the rest.

The voxel solver (``tule.fea``'s default for chunky parts) needs walls a few voxels thick
and merges gaps narrower than a voxel. Thin-walled and compliant parts (flexure blades,
wire-EDM slots, sheet-like ribs) go here instead:

- ``thickness(part)``: each B-rep face's wall thickness (a ray into the material along the
  inward normal, to the far side) and gap (a ray out, to the next face across the void),
  from a few samples per face. Cheap: about a second for hundreds of faces.
- ``mesh(part, ...)``: the ``gmsh`` binary (Debian's ``gmsh`` package; ``$TULE_GMSH`` overrides
  it) meshes the part's B-rep (written as a ``.brep``) with 10-node tetrahedra, curved on
  the surface. Faces whose walls are thin get elements of a fraction of their thickness
  (``Constant`` size fields on those surfaces; the interior follows from the boundary),
  curvature sets sizes on holes and fillets, the rest is graded up to the global size.
  gmsh's HXT algorithm avoids slivers (they slowed the solver 3-6× on a flexure).
  Each surface triangle keeps its B-rep face id (gmsh numbers the OCC faces in the same
  exploration order; the bounding boxes are checked).
- ``Model``: the C3D10 element (4-point Gauss rule for stiffness and stress, which is exact
  on straight-sided elements; a 27-point conical product rule for the consistent mass),
  assembled into a SciPy sparse matrix by 3×3 node blocks. ``Solver``: conjugate gradients
  preconditioned by ``TwoLevel`` (Chebyshev-Jacobi smoothing, and an exact SuperLU solve on
  the linear tetrahedra of the same mesh: a sparse direct solve of the quadratic system
  took minutes and gigabytes at 400,000 DOFs). Stresses at the Gauss points are extrapolated to the
  corners (a straight quadratic tet's stress is linear, so this is exact), midside nodes
  take the mean of their edge's corners, and nodes average over their elements.
- Modes: ``K φ = ω² M φ`` by shift-invert Lanczos (ARPACK through ``scipy.sparse.linalg.eigsh``,
  ``K⁻¹`` applied by ``Solver``). Buckling: the static stress gives the geometric stiffness
  ``K_σ``; ``(K + λ K_σ) φ = 0`` is solved as ``-K_σ φ = μ K φ`` for the ``μ = 1/λ`` of largest
  magnitude (the positive ones are the smallest load factors; both ends converge fast,
  while the positive end alone stalls when little is in compression).

Units: mm, N, MPa; mass in tonnes (N·s²/mm) inside, so frequencies come out in Hz.
"""

from __future__ import annotations

import functools
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field

import numpy as np

from .errors import TuleError

# internal node order of a 10-node tetrahedron: corners 0-3, then midside nodes on the edges
TET_EDGES = ((0, 1), (1, 2), (0, 2), (0, 3), (1, 3), (2, 3))
# a 6-node triangle: corners 0-2, then the edges 01, 12, 20
TRI_EDGES = ((0, 1), (1, 2), (2, 0))
# a 6-node triangle as 4 flat ones (for drawing and surface areas)
TRI_SPLIT = np.array([(0, 3, 5), (3, 1, 4), (5, 4, 2), (3, 4, 5)])


def find_gmsh() -> str | None:
    """The gmsh binary: ``$TULE_GMSH``, else ``gmsh`` on the PATH."""
    p = os.environ.get("TULE_GMSH")
    if p:
        return p if os.path.exists(p) else shutil.which(p)
    return shutil.which("gmsh")


# ---------------------------------------------------------------- wall thickness


@dataclass(frozen=True)
class Thickness:
    """Per B-rep face: the thinnest wall behind it and the narrowest gap in front of it (mm,
    ``inf`` where a ray found nothing within ``reach``), with where the thinnest of each is."""

    wall: np.ndarray  # (faces,)
    gap: np.ndarray  # (faces,)
    min_wall: float
    min_wall_at: tuple
    min_gap: float
    min_gap_at: tuple
    seconds: float

    def __str__(self) -> str:
        def one(v, at):
            return f"{v:.3g} mm at ({', '.join(f'{x:.3g}' for x in at)})" if math.isfinite(v) else "none found"
        return f"thinnest wall {one(self.min_wall, self.min_wall_at)}; narrowest gap {one(self.min_gap, self.min_gap_at)}"

    __repr__ = __str__


def thickness(part, per_face: int = 12, reach: float | None = None) -> Thickness:
    """Each face's wall thickness and gap width from rays along its normals at up to
    ``per_face`` points (centers of its largest triangles): into the material to the far side,
    and out of it to the next face. A wall ends where the ray leaves; a ray that grazes an
    edge is dropped (ambiguous). ``reach`` caps the rays (default: a fifth of the part's size)."""
    from OCP.gp import gp_Dir, gp_Lin, gp_Pnt
    from OCP.IntCurvesFace import IntCurvesFace_ShapeIntersector

    t0 = time.time()
    lo, hi = part.bounds
    size = float((hi - lo).length)
    reach = float(reach or 0.2 * size)
    defl = size / 2000
    mesh = part.tessellate(defl, 0.3)
    P = mesh.positions.astype(np.float64)
    T = mesh.indices.astype(np.int64).reshape(-1, 3)
    fid = mesh.face_ids.astype(np.int64)
    a, b, c = P[T[:, 0]], P[T[:, 1]], P[T[:, 2]]
    cr = np.cross(b - a, c - a)
    area = 0.5 * np.linalg.norm(cr, axis=1)
    nrm = cr / np.maximum(2 * area, 1e-300)[:, None]
    vn = mesh.normals.astype(np.float64)
    flip = np.einsum("ij,ij->i", nrm, vn[T[:, 0]] + vn[T[:, 1]] + vn[T[:, 2]]) < 0
    nrm[flip] *= -1  # outward
    cen = (a + b + c) / 3
    nf = len(part.faces)
    wall = np.full(nf, np.inf)
    gap = np.full(nf, np.inf)
    wall_at = [(0.0, 0.0, 0.0)] * nf
    gap_at = [(0.0, 0.0, 0.0)] * nf
    si = IntCurvesFace_ShapeIntersector()
    si.Load(part.wrapped, 1e-7)
    eps = 1e-6 * size
    order = np.lexsort((-area, fid))  # by face, largest triangles first
    starts = np.searchsorted(fid[order], np.arange(nf + 1))
    items = part.faces.items
    for f in range(nf):
        tris = order[starts[f]:starts[f + 1]][:per_face]
        tris = tris[area[tris] > 1e-12 * size * size]
        if not len(tris):
            continue
        for sign, out, at in ((-1.0, wall, wall_at), (1.0, gap, gap_at)):
            hits = []
            for t in tris:
                p, d = cen[t], sign * nrm[t]
                si.Perform(gp_Lin(gp_Pnt(*(p + d * eps)), gp_Dir(*d)), 0.0, reach)
                best = math.inf
                for k in range(1, si.NbPnt() + 1):
                    w = si.WParameter(k) + eps
                    # a curved face's own surface just off the triangle's chord is not a wall
                    if 10 * eps < w < best and not (w < 4 * defl and si.Face(k).IsSame(items[f])):
                        best = w
                hits.append(best)
            # the face's typical wall (gap): the median of its rays, so one ray past an edge or into a
            # knife edge does not set it
            k = int(np.argsort(hits)[(len(hits) - 1) // 2])
            out[f] = hits[k]
            at[f] = tuple(float(x) for x in cen[tris[k]])
    iw = int(np.argmin(wall)) if nf else 0
    ig = int(np.argmin(gap)) if nf else 0
    return Thickness(wall, gap, float(wall[iw]) if nf else math.inf, wall_at[iw] if nf else (0.0, 0.0, 0.0),
                     float(gap[ig]) if nf else math.inf, gap_at[ig] if nf else (0.0, 0.0, 0.0), time.time() - t0)


# ---------------------------------------------------------------- meshing


@dataclass
class TetMesh:
    """Quadratic tetrahedra: ``nodes`` (n, 3) mm; ``tets`` (m, 10) node indices (corners, then
    midside nodes on TET_EDGES); surface triangles ``tris`` (k, 6) with their B-rep face ids."""

    nodes: np.ndarray
    tets: np.ndarray
    tris: np.ndarray
    tri_face: np.ndarray
    size: float  # the largest element size asked for, mm
    smallest: float  # the smallest
    seconds: float
    notes: list = field(default_factory=list)

    @functools.cached_property
    def surface(self) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """The boundary as flat triangles for drawing: (vertex node ids, triangles into them, face ids)."""
        flat = self.tris[:, TRI_SPLIT].reshape(-1, 3)
        used, inv = np.unique(flat, return_inverse=True)
        return used, inv.reshape(-1, 3), np.repeat(self.tri_face, 4)


def _write_brep(part, path: str):
    from OCP.BRepTools import BRepTools
    from OCP.TopTools import TopTools_FormatVersion

    if not BRepTools.Write_s(part.wrapped, path, False, False, TopTools_FormatVersion.TopTools_FormatVersion_VERSION_1):
        raise TuleError("cannot write the part for gmsh", op="fea")


def _face_boxes(part) -> np.ndarray:
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib

    out = []
    for f in part.faces.items:
        b = Bnd_Box()
        BRepBndLib.Add_s(f, b, False)
        p, q = b.CornerMin(), b.CornerMax()
        out.append((p.X(), p.Y(), p.Z(), q.X(), q.Y(), q.Z()))
    return np.array(out).reshape(-1, 6)


def _parse_msh(path: str):
    """Nodes, tets (type 11) and triangles (type 9) with their surface tags from an ASCII MSH 2.2 file."""
    with open(path) as fh:
        text = fh.read()

    def section(name):
        i = text.index(f"${name}\n") + len(name) + 2
        j = text.index(f"$End{name}", i)
        return text[i:j]

    nodes = section("Nodes").split("\n", 1)[1]
    arr = np.array(nodes.split(), dtype=np.float64).reshape(-1, 4)
    ids = arr[:, 0].astype(np.int64)
    where = np.full(ids.max() + 1, -1, dtype=np.int64)
    where[ids] = np.arange(len(ids))
    xyz = arr[:, 1:]
    tets, tris, tags = [], [], []
    for line in section("Elements").split("\n")[1:]:
        if not line:
            continue
        v = line.split()
        typ, nt = v[1], int(v[2])
        if typ == "11":
            tets.append(v[3 + nt:])
        elif typ == "9":
            tris.append(v[3 + nt:])
            tags.append(v[4] if nt > 1 else v[3])
    tets = where[np.array(tets, dtype=np.int64).reshape(-1, 10)]
    tris = where[np.array(tris, dtype=np.int64).reshape(-1, 6)]
    return xyz, tets, tris, np.array(tags, dtype=np.int64)


def _edge_order(xyz, elems, edges) -> np.ndarray:
    """A permutation of the midside columns so each lies on ``edges`` (gmsh's orders differ by type)."""
    nc = 4 if len(edges) == 6 else 3
    sample = elems[:: max(1, len(elems) // 200)]
    perm = []
    for a, b in edges:
        mid = 0.5 * (xyz[sample[:, a]] + xyz[sample[:, b]])
        d = [np.median(np.linalg.norm(xyz[sample[:, k]] - mid, axis=1)) for k in range(nc, nc + len(edges))]
        perm.append(nc + int(np.argmin(d)))
    if len(set(perm)) != len(edges):
        raise TuleError("cannot read gmsh's node order", op="fea")
    return np.array(list(range(nc)) + perm)


# gmsh's HXT tetrahedralizer and its optimizer: the default Delaunay one leaves slivers (quality
# 1e-4) that slowed conjugate gradients 3-6× on a flexure; Netgen's optimizer fixes them too but is
# not in Debian's gmsh
QUALITY = ["Mesh.Algorithm3D = 10;", "Mesh.Optimize = 1;"]


def mesh(part, size: float, face_sizes: dict | None = None, smallest: float | None = None,
         curvature: int = 16, threads: int = 0, timeout: float = 1800, keep: str | None = None,
         balls=()) -> TetMesh:
    """Quadratic tetrahedra for ``part``: elements up to ``size`` mm, ``face_sizes`` ``{face id:
    size}`` on chosen faces (thin walls), ``balls`` ``[(center, radius, size)]`` (load patches),
    ``curvature`` elements per full turn on curved faces, none smaller than ``smallest``."""
    gmsh = find_gmsh()
    if not gmsh:
        raise TuleError("tetrahedral meshing needs gmsh", op="fea",
                        hint="apt-get install -y --no-install-recommends gmsh (it is in the Tule image); "
                             "or mesh=\"voxel\"")
    t0 = time.time()
    smallest = float(smallest or size / 50)
    face_sizes = {int(k): max(float(v), smallest) for k, v in (face_sizes or {}).items() if v < size}
    nf = len(part.faces)
    work = keep or tempfile.mkdtemp(prefix="tule-fea-")
    try:
        brep = os.path.join(work, "part.brep")
        _write_brep(part, brep)
        lines = [
            "General.Terminal = 1;",
            'SetFactory("OpenCASCADE");',
            "Geometry.OCCFixDegenerated = 0; Geometry.OCCFixSmallEdges = 0; Geometry.OCCFixSmallFaces = 0;",
            "Geometry.OCCSewFaces = 0; Geometry.OCCMakeSolids = 0;",
            'Merge "part.brep";',
            "ss[] = Surface{:};",
            "For i In {0:#ss[]-1}",
            "  bb[] = BoundingBox Surface{ss[i]};",
            '  Printf("TULE-FACE %g %.9g %.9g %.9g %.9g %.9g %.9g", ss[i], bb[0], bb[1], bb[2], bb[3], bb[4], bb[5]);',
            "EndFor",
            f"Mesh.MeshSizeMax = {size:.6g};",
            f"Mesh.MeshSizeMin = {smallest:.6g};",
            f"Mesh.MeshSizeFromCurvature = {int(curvature)};",
            "Mesh.ElementOrder = 2;",
            "Mesh.HighOrderOptimize = 0;",
            *QUALITY,
            f"General.NumThreads = {int(threads)};",
            "Mesh.MshFileVersion = 2.2;",
            "Physical Volume(1) = Volume{:};",
            "For i In {0:#ss[]-1}",
            "  Physical Surface(ss[i]) = {ss[i]};",
            "EndFor",
        ]
        by_size: dict[float, list[int]] = {}
        for f, h in face_sizes.items():
            if 0 <= f < nf:
                by_size.setdefault(round(h, 4), []).append(f + 1)
        fids = []
        for n, (h, surfs) in enumerate(sorted(by_size.items()), start=1):
            lines += [f"Field[{n}] = Constant;", f"Field[{n}].VIn = {h:.6g};", f"Field[{n}].VOut = {1e22:g};",
                      f"Field[{n}].SurfacesList = {{{', '.join(map(str, surfs))}}};", f"Field[{n}].IncludeBoundary = 1;"]
            fids.append(n)
        for (cx, cy, cz), r, h in balls:  # elements of h in the ball, growing to size over another radius
            n = len(fids) + 1
            lines += [f"Field[{n}] = Ball;", f"Field[{n}].Radius = {r:.6g};", f"Field[{n}].Thickness = {r:.6g};",
                      f"Field[{n}].VIn = {max(h, smallest):.6g};", f"Field[{n}].VOut = {size:.6g};",
                      f"Field[{n}].XCenter = {cx:.9g};", f"Field[{n}].YCenter = {cy:.9g};", f"Field[{n}].ZCenter = {cz:.9g};"]
            fids.append(n)
        if fids:
            m = len(fids) + 1
            lines += [f"Field[{m}] = Min;", f"Field[{m}].FieldsList = {{{', '.join(map(str, fids))}}};", f"Background Field = {m};"]
        lines += ["Mesh 3;", 'Save "part.msh";']
        with open(os.path.join(work, "part.geo"), "w") as fh:
            fh.write("\n".join(lines) + "\n")
        msh = os.path.join(work, "part.msh")
        for attempt in range(2):  # gmsh now and then aborts (a signal: "double free"); a second run meshes
            if os.path.exists(msh):
                os.remove(msh)
            try:
                r = subprocess.run([gmsh, "-nopopup", "-0", "part.geo"], cwd=work, capture_output=True, text=True, timeout=timeout)
            except subprocess.TimeoutExpired:
                raise TuleError(f"gmsh took over {timeout:.0f} s", op="fea", hint="a coarser size=, or fewer dofs") from None
            out = r.stdout + r.stderr
            # a mesh it finished writing before crashing on exit is good
            written = "Done writing 'part.msh'" in out and os.path.exists(msh)
            if r.returncode >= 0 or written:
                break
        if (r.returncode != 0 and not written) or not os.path.exists(msh):
            errs = [ln for ln in out.splitlines() if "Error" in ln][-3:]
            raise TuleError("gmsh could not mesh the part: " + ("; ".join(errs) or out[-400:]), op="fea",
                            hint="check the part is one valid solid (part.check()); a different size= sometimes helps; "
                                 "or mesh=\"voxel\"")
        xyz, tets, tris, tags = _parse_msh(os.path.join(work, "part.msh"))
    finally:
        if not keep:
            shutil.rmtree(work, ignore_errors=True)
    if not len(tets):
        raise TuleError("gmsh made no tetrahedra", op="fea", hint="is the part a closed solid? part.check()")
    tets = tets[:, _edge_order(xyz, tets, TET_EDGES)]
    tris = tris[:, _edge_order(xyz, tris, TRI_EDGES)]
    # gmsh's surface tags are the B-rep face ids + 1 (the same exploration order): check by bounding box
    boxes = {int(m.group(1)): np.array(m.groups()[1:], dtype=np.float64)
             for m in re.finditer(r"TULE-FACE (\d+) (\S+) (\S+) (\S+) (\S+) (\S+) (\S+)", out)}
    ours = _face_boxes(part)
    tol = 1e-4 * max(1.0, float(np.abs(ours).max()))
    remap = {}
    for tag, bb in boxes.items():
        guess = tag - 1
        if 0 <= guess < nf and np.abs(ours[guess] - bb).max() <= tol:
            remap[tag] = guess
        else:
            remap[tag] = int(np.argmin(np.abs(ours - bb).max(axis=1)))
    lut = np.full(max(boxes, default=0) + 2, -1, dtype=np.int64)
    for tag, f in remap.items():
        lut[tag] = f
    tri_face = lut[np.minimum(tags, len(lut) - 1)]
    notes = []
    if any(remap[t] != t - 1 for t in remap):
        notes.append("gmsh numbered the faces differently; matched them by bounding box")
    # drop nodes no element uses (gmsh keeps the B-rep's vertices of dropped bits)
    used = np.zeros(len(xyz), dtype=bool)
    used[tets.reshape(-1)] = True
    used[tris.reshape(-1)] = True
    if not used.all():
        new = np.cumsum(used) - 1
        xyz, tets, tris = xyz[used], new[tets], new[tris]
    m = TetMesh(xyz, tets, tris, tri_face, float(size), smallest, time.time() - t0, notes)
    _orient(m)
    _fix_curved(m)
    return m


def _orient(m: TetMesh):
    """Turns each face's surface triangles to face outward (gmsh keeps a surface's own
    orientation, which may point in): one triangle per face is checked against its tetrahedron."""
    order = np.argsort(m.tets[:, :4].reshape(-1), kind="stable")
    owner = order // 4
    starts = np.searchsorted(m.tets[:, :4].reshape(-1)[order], np.arange(len(m.nodes) + 1))
    flip = np.zeros(len(m.tris), dtype=bool)
    for f in np.unique(m.tri_face):
        sel = np.flatnonzero(m.tri_face == f)
        votes = 0
        for t in sel[:: max(1, len(sel) // 3)][:3]:
            a, b, c = m.tris[t, :3]
            cand = owner[starts[a]:starts[a + 1]]
            cand = cand[np.isin(m.tets[cand, :4], [b]).any(1) & np.isin(m.tets[cand, :4], [c]).any(1)]
            if not len(cand):
                continue
            tet = m.tets[cand[0], :4]
            opp = tet[~np.isin(tet, [a, b, c])][0]
            P = m.nodes
            n = np.cross(P[b] - P[a], P[c] - P[a])
            votes += 1 if np.dot(n, P[opp] - P[a]) > 0 else -1
        if votes > 0:
            flip[sel] = True
    m.tris[flip] = m.tris[flip][:, [0, 2, 1, 5, 4, 3]]


def _fix_curved(m: TetMesh):
    """Straightens the midside nodes of elements whose curved shape folds (a negative Jacobian
    at a Gauss point): the element's edges become straight, the surface there flat."""
    for _ in range(3):
        _, det = _jacobians(m.nodes, m.tets, GP4)
        bad = np.flatnonzero((det <= 0).any(axis=1))
        if not len(bad):
            return
        for k, (a, b) in enumerate(TET_EDGES):
            e = m.tets[bad]
            m.nodes[e[:, 4 + k]] = 0.5 * (m.nodes[e[:, a]] + m.nodes[e[:, b]])
        m.notes.append(f"{len(bad)} curved element(s) were folded: straightened")
    _, det = _jacobians(m.nodes, m.tets, GP4)
    if (det <= 0).any():
        raise TuleError("the mesh has inverted elements", op="fea", hint="try another size=, or mesh=\"voxel\"")


# ---------------------------------------------------------------- the element


def _gauss4():
    a, b = 0.5854101966249685, 0.1381966011250105
    L = np.full((4, 4), b)
    np.fill_diagonal(L, a)
    return L, np.full(4, 1 / 24)


def _conical(n: int = 3):
    """A conical product rule on the unit tetrahedron (n³ points, exact to degree 2n - 1): (L (g, 4), w)."""
    from scipy.special import roots_jacobi

    x1, w1 = roots_jacobi(n, 2, 0)
    x2, w2 = roots_jacobi(n, 1, 0)
    x3, w3 = roots_jacobi(n, 0, 0)
    u1, u2, u3 = (x1 + 1) / 2, (x2 + 1) / 2, (x3 + 1) / 2
    pts, ws = [], []
    for i in range(n):
        for j in range(n):
            for k in range(n):
                x = u1[i]
                y = (1 - u1[i]) * u2[j]
                z = (1 - u1[i]) * (1 - u2[j]) * u3[k]
                pts.append((1 - x - y - z, x, y, z))
                ws.append(w1[i] * w2[j] * w3[k])
    w = np.array(ws)
    return np.array(pts), w / w.sum() / 6


GP4, W4 = _gauss4()
GP27, W27 = _conical(3)


def shape(L: np.ndarray) -> np.ndarray:
    """10-node shape functions at volume coordinates ``L`` (g, 4): (g, 10)."""
    L = np.atleast_2d(L)
    N = np.empty((len(L), 10))
    for i in range(4):
        N[:, i] = L[:, i] * (2 * L[:, i] - 1)
    for k, (a, b) in enumerate(TET_EDGES):
        N[:, 4 + k] = 4 * L[:, a] * L[:, b]
    return N


def shape_grad(L: np.ndarray) -> np.ndarray:
    """Derivatives along ξ, η, ζ (L1, L2, L3; L0 = 1 - ξ - η - ζ): (g, 10, 3)."""
    L = np.atleast_2d(L)
    dL = np.array([[-1.0, -1, -1], [1, 0, 0], [0, 1, 0], [0, 0, 1]])
    dN = np.empty((len(L), 10, 3))
    for i in range(4):
        dN[:, i] = (4 * L[:, i] - 1)[:, None] * dL[i]
    for k, (a, b) in enumerate(TET_EDGES):
        dN[:, 4 + k] = 4 * (L[:, b, None] * dL[a] + L[:, a, None] * dL[b])
    return dN


def _jacobians(nodes, tets, L):
    """Physical shape gradients (m, g, 10, 3) and Jacobian determinants (m, g)."""
    dN = shape_grad(L)
    X = nodes[tets]  # (m, 10, 3)
    J = np.einsum("mai,gaj->mgij", X, dN)  # ∂x_i/∂ξ_j
    det = np.linalg.det(J)
    return np.einsum("gaj,mgjk->mgak", dN, np.linalg.inv(J)), det


def tri_shape(st: np.ndarray):
    """6-node triangle shape functions and their derivatives at (s, t): (g, 6), (g, 6, 2)."""
    s, t = st[:, 0], st[:, 1]
    L = np.stack([1 - s - t, s, t], 1)
    dL = np.array([[-1.0, -1], [1, 0], [0, 1]])
    N = np.empty((len(st), 6))
    dN = np.empty((len(st), 6, 2))
    for i in range(3):
        N[:, i] = L[:, i] * (2 * L[:, i] - 1)
        dN[:, i] = (4 * L[:, i] - 1)[:, None] * dL[i]
    for k, (a, b) in enumerate(TRI_EDGES):
        N[:, 3 + k] = 4 * L[:, a] * L[:, b]
        dN[:, 3 + k] = 4 * (L[:, b, None] * dL[a] + L[:, a, None] * dL[b])
    return N, dN


# a 6-point rule on the triangle, exact to degree 4 (weights sum to the reference area 1/2)
_A, _B = 0.445948490915965, 0.091576213509771
TRI6_ST = np.array([(_A, _A), (1 - 2 * _A, _A), (_A, 1 - 2 * _A), (_B, _B), (1 - 2 * _B, _B), (_B, 1 - 2 * _B)])
TRI6_W = np.array([0.223381589678011] * 3 + [0.109951743655322] * 3) / 2


def surface_quadrature(nodes, tris):
    """Points (k, g, 3), shape values (g, 6) and area-weighted normals w·n (k, g, 3) on curved
    6-node triangles (normals by the corner order: outward for gmsh's boundary triangles)."""
    N, dN = tri_shape(TRI6_ST)
    X = nodes[tris]  # (k, 6, 3)
    pts = np.einsum("ga,kai->kgi", N, X)
    ds = np.einsum("ga,kai->kgi", dN[:, :, 0], X)
    dt = np.einsum("ga,kai->kgi", dN[:, :, 1], X)
    return pts, N, np.cross(ds, dt) * TRI6_W[None, :, None]


def D_matrix(E: float, nu: float) -> np.ndarray:
    lam = E * nu / ((1 + nu) * (1 - 2 * nu))
    mu = E / (2 * (1 + nu))
    D = np.zeros((6, 6))
    D[:3, :3] = lam
    D[:3, :3] += 2 * mu * np.eye(3)
    D[3:, 3:] = mu * np.eye(3)
    return D


# ---------------------------------------------------------------- the model


class Model:
    """A mesh with its element integrals: assembles stiffness, mass and geometric stiffness,
    loads, and recovers stresses. DOF 3·node + component."""

    def __init__(self, m: TetMesh):
        self.mesh = m
        self.nodes, self.tets = m.nodes, m.tets
        self.n = len(m.nodes)
        self.G, self.det = _jacobians(self.nodes, self.tets, GP4)  # (e, 4, 10, 3), (e, 4)
        self.wdet = self.det * W4  # (e, 4)
        self.volume = float(self.wdet.sum())
        # node-pair blocks: the sparsity pattern, once
        rows = np.repeat(self.tets, 10, axis=1).reshape(-1)
        cols = np.tile(self.tets, (1, 10)).reshape(-1)
        keys = rows * self.n + cols
        self.keys, self.slot = np.unique(keys, return_inverse=True)
        self.slot = self.slot.reshape(len(self.tets), 100)
        self.brow, self.bcol = self.keys // self.n, self.keys % self.n

    def _bsr(self, blocks: np.ndarray):
        """A (3n × 3n) CSR matrix from (pattern, 3, 3) blocks."""
        from scipy.sparse import bsr_matrix

        indptr = np.searchsorted(self.brow, np.arange(self.n + 1))
        return bsr_matrix((blocks, self.bcol, indptr), shape=(3 * self.n, 3 * self.n)).tocsr()

    def _accumulate(self, Ke_fn, chunk: int = 20000):
        """Sum element blocks (e, 10, 10, 3, 3) from ``Ke_fn(slice)`` into the pattern."""
        data = np.zeros((len(self.keys), 3, 3))
        for s in range(0, len(self.tets), chunk):
            sl = slice(s, min(s + chunk, len(self.tets)))
            Ke = Ke_fn(sl).reshape(-1, 3, 3)
            slot = self.slot[sl].reshape(-1)
            for i in range(3):
                for j in range(3):
                    data[:, i, j] += np.bincount(slot, weights=Ke[:, i, j], minlength=len(self.keys))
        return self._bsr(data)

    def stiffness(self, E: float, nu: float):
        lam = E * nu / ((1 + nu) * (1 - 2 * nu))
        mu = E / (2 * (1 + nu))
        I3 = np.eye(3)

        def Ke(sl):
            G, w = self.G[sl], self.wdet[sl]
            K = lam * np.einsum("mg,mgai,mgbj->mabij", w, G, G)
            K += mu * np.einsum("mg,mgaj,mgbi->mabij", w, G, G)
            K += mu * np.einsum("mg,mgak,mgbk->mab", w, G, G)[..., None, None] * I3
            return K

        return self._accumulate(Ke)

    def mass(self, rho: float):
        """Consistent mass (ρ in t/mm³)."""
        N = shape(GP27)
        I3 = np.eye(3)

        def Me(sl):
            _, det = _jacobians(self.nodes, self.tets[sl], GP27)
            M = np.einsum("mg,ga,gb->mab", det * W27 * rho, N, N)
            return M[..., None, None] * I3

        return self._accumulate(Me, chunk=5000)

    def geometric(self, sigma_gp: np.ndarray):
        """The stress stiffness from Gauss-point stresses (e, 4, 6: xx yy zz xy yz zx)."""
        I3 = np.eye(3)
        S = np.empty(sigma_gp.shape[:2] + (3, 3))
        xx, yy, zz, xy, yz, zx = np.moveaxis(sigma_gp, -1, 0)
        S[..., 0, 0], S[..., 1, 1], S[..., 2, 2] = xx, yy, zz
        S[..., 0, 1] = S[..., 1, 0] = xy
        S[..., 1, 2] = S[..., 2, 1] = yz
        S[..., 0, 2] = S[..., 2, 0] = zx

        def Kg(sl):
            H = np.einsum("mg,mgai,mgij,mgbj->mab", self.wdet[sl], self.G[sl], S[sl], self.G[sl])
            return H[..., None, None] * I3

        return self._accumulate(Kg)

    def body_force(self, rho: float, a) -> np.ndarray:
        """Nodal forces (n, 3) of an acceleration field ``a`` (mm/s²) on the mass (ρ in t/mm³)."""
        N = shape(GP4)
        share = np.einsum("mg,ga->ma", self.wdet, N) * rho  # ∫ρN_a per element
        f = np.zeros((self.n, 3))
        for c in range(3):
            f[:, c] = np.bincount(self.tets.reshape(-1), weights=(share * a[c]).reshape(-1), minlength=self.n)
        return f

    def strain_operator(self):
        """B·u at the Gauss points: (e, 4, 6) strains from element displacements via G."""
        return self.G

    def gauss_stress(self, u: np.ndarray, E: float, nu: float) -> np.ndarray:
        """Stresses at the Gauss points (e, 4, 6) from nodal displacements (n, 3)."""
        ue = u[self.tets]  # (e, 10, 3)
        grad = np.einsum("mgak,mai->mgik", self.G, ue)  # ∂u_i/∂x_k
        eps = np.stack([grad[..., 0, 0], grad[..., 1, 1], grad[..., 2, 2],
                        grad[..., 0, 1] + grad[..., 1, 0], grad[..., 1, 2] + grad[..., 2, 1],
                        grad[..., 2, 0] + grad[..., 0, 2]], -1)
        return eps @ D_matrix(E, nu).T

    def nodal(self, gp: np.ndarray) -> np.ndarray:
        """Gauss-point values (e, 4, k) extrapolated to the corners (linear: exact on straight
        elements), midside nodes the mean of their corners, averaged over the elements at each node."""
        A = np.linalg.inv(GP4)  # corner values = A @ Gauss values (rows: corners)
        corner = np.einsum("cg,mgk->mck", A, gp)
        mid = np.stack([0.5 * (corner[:, a] + corner[:, b]) for a, b in TET_EDGES], 1)
        vals = np.concatenate([corner, mid], 1)  # (e, 10, k)
        k = vals.shape[-1]
        out = np.zeros((self.n, k))
        idx = self.tets.reshape(-1)
        for c in range(k):
            out[:, c] = np.bincount(idx, weights=vals[..., c].reshape(-1), minlength=self.n)
        cnt = np.bincount(idx, minlength=self.n)
        return out / np.maximum(cnt, 1)[:, None]


def factorize(K):
    """A sparse LU of a symmetric positive definite matrix (SuperLU with a symmetric ordering): .solve(b)."""
    from scipy.sparse.linalg import splu

    return splu(K.tocsc(), permc_spec="MMD_AT_PLUS_A", diag_pivot_thresh=0.0, options={"SymmetricMode": True})


# ---------------------------------------------------------------- the solver


def prolongation(m: TetMesh):
    """Linear tetrahedra on the same corners into the quadratic ones: (3n × 3c) sparse, and the corner nodes."""
    import scipy.sparse as sp

    n = len(m.nodes)
    vtx = np.unique(m.tets[:, :4])
    cidx = np.full(n, -1, dtype=np.int64)
    cidx[vtx] = np.arange(len(vtx))
    rows, cols, vals = [vtx], [np.arange(len(vtx))], [np.ones(len(vtx))]
    for k, (a, b) in enumerate(TET_EDGES):
        mid = m.tets[:, 4 + k]
        for v in (m.tets[:, a], m.tets[:, b]):
            rows.append(mid)
            cols.append(cidx[v])
            vals.append(np.full(len(mid), 0.5))
    r, c, v = np.concatenate(rows), np.concatenate(cols), np.concatenate(vals)
    key, first = np.unique(r * len(vtx) + c, return_index=True)  # a midside node appears once per element
    Pn = sp.csr_matrix((v[first], (key // len(vtx), key % len(vtx))), shape=(n, len(vtx)))
    return sp.kron(Pn, sp.eye(3), format="csr"), vtx


class TwoLevel:
    """A two-level preconditioner for the quadratic stiffness: Chebyshev smoothing (degree 2, Jacobi-scaled,
    on [λmax/30, λmax]) and an exact solve on the linear tetrahedra of the same mesh (Galerkin,
    SuperLU). Symmetric, so it preconditions conjugate gradients."""

    def __init__(self, A, P, degree: int = 2):
        from scipy.sparse.linalg import splu

        self.A, self.P, self.Pt, self.degree = A, P, P.T.tocsr(), degree
        Ac = (self.Pt @ A @ P).tocsc()
        self.lu = splu(Ac, permc_spec="MMD_AT_PLUS_A", diag_pivot_thresh=0.0, options={"SymmetricMode": True})
        self.coarse = Ac.shape[0]
        d = A.diagonal()
        self.dinv = 1 / np.where(d > 0, d, 1.0)
        x = np.random.default_rng(0).standard_normal(A.shape[0])
        lam = 1.0
        for _ in range(15):
            x /= np.linalg.norm(x)
            x = self.dinv * (A @ x)
            lam = np.linalg.norm(x)
        self.lmax = 1.1 * lam

    def smooth(self, r, x):
        lmax = self.lmax
        lmin = lmax / 30
        theta, delta = (lmax + lmin) / 2, (lmax - lmin) / 2
        sigma = theta / delta
        rho = 1 / sigma
        res = r - self.A @ x if x is not None else r
        x = np.zeros_like(r) if x is None else x
        d = self.dinv * res / theta
        for k in range(self.degree):
            x = x + d
            if k < self.degree - 1:
                res = res - self.A @ d
                rho_new = 1 / (2 * sigma - rho)
                d = rho_new * rho * d + (2 * rho_new / delta) * (self.dinv * res)
                rho = rho_new
        return x

    def __call__(self, r):
        x = self.smooth(r, None)
        x = x + self.P @ self.lu.solve(self.Pt @ (r - self.A @ x))
        return self.smooth(r, x)


class Solver:
    """``K u = f`` on the free DOFs (``free``: a mask over 3n) by conjugate gradients with ``TwoLevel``."""

    def __init__(self, m: TetMesh, K, free: np.ndarray):
        self.free = free
        self.idx = np.flatnonzero(free)
        self.K = K[self.idx][:, self.idx].tocsr()
        P, vtx = prolongation(m)
        cfree = np.flatnonzero(free.reshape(-1, 3)[vtx].reshape(-1))
        self.pre = TwoLevel(self.K, P[self.idx][:, cfree].tocsr())
        self.iterations = 0
        self.solves = 0

    def solve(self, b: np.ndarray, tol: float = 1e-9, maxiter: int = 5000) -> tuple[np.ndarray, int, float]:
        """x with K x = b (both on the free DOFs), the iterations and the relative residual."""
        A, M = self.K, self.pre
        x = np.zeros_like(b)
        bn = float(np.linalg.norm(b))
        if bn == 0:
            return x, 0, 0.0
        r = b.copy()
        z = M(r)
        p = z.copy()
        rz = float(r @ z)
        res = 1.0
        for it in range(1, maxiter + 1):
            Ap = A @ p
            pAp = float(p @ Ap)
            if not pAp > 0:
                break
            alpha = rz / pAp
            x += alpha * p
            r -= alpha * Ap
            res = float(np.linalg.norm(r)) / bn
            if res <= tol:
                break
            z = M(r)
            rz_new = float(r @ z)
            p = z + (rz_new / rz) * p
            rz = rz_new
        self.iterations += it
        self.solves += 1
        return x, it, res

    def operator(self, tol: float = 1e-8):
        """K⁻¹ as a SciPy LinearOperator (for ARPACK)."""
        from scipy.sparse.linalg import LinearOperator

        def mv(b):
            return self.solve(np.asarray(b, dtype=np.float64).reshape(-1), tol)[0]

        return LinearOperator(self.K.shape, matvec=mv, dtype=np.float64)


# ---------------------------------------------------------------- sizes


# DOFs of gmsh's quadratic tetrahedra of size h: about 17·volume/h³ + 13·area/h² (measured on beams,
# blades and plates: thin parts are mostly surface)
DOF_VOLUME, DOF_AREA = 17.0, 13.0


def _dofs(volume, area, h):
    return DOF_VOLUME * volume / h ** 3 + DOF_AREA * area / h ** 2


def _size_for(volume, area, dofs):
    """The element size giving about ``dofs`` unknowns."""
    lo, hi = 1e-6, 1e6
    for _ in range(80):
        mid = math.sqrt(lo * hi)
        lo, hi = (mid, hi) if _dofs(volume, area, mid) > dofs else (lo, mid)
    return hi


def plan(part, thick: Thickness | None, dofs: int, size: float | None = None, layers: int = 2):
    """Element sizes for about ``dofs`` unknowns: ``(size, {face id: size}, notes)``. Faces whose
    wall would get fewer than ``layers`` elements through it get ``wall / layers``; when those alone
    would take most of the budget, a floor under their sizes rises until they fit (the thinnest get
    fewer layers). The rest of the part takes what the budget leaves (or ``size``)."""
    from .selection import face_area

    V, A = float(part.volume), float(part.area)
    H = float(size) if size else _size_for(V, A, dofs)
    notes = []
    if thick is None:
        return H, {}, notes
    wall = np.asarray(thick.wall)
    areas = np.array([face_area(f) for f in part.faces.items])
    faces: dict[int, float] = {}
    for _ in range(6):
        thin = np.flatnonzero(np.isfinite(wall) & (wall < layers * H * 0.9))
        vol = areas[thin] * wall[thin] / 2  # each face carries half its wall
        h = wall[thin] / layers

        def count(h):
            return float(_dofs(vol, areas[thin], np.maximum(h, 1e-9)).sum())

        if not size and count(h) > 0.7 * dofs:  # over the budget: a floor under the sizes, raised until they fit
            lo, hi = float(h.min()), float(H)
            for _ in range(40):
                mid = math.sqrt(lo * hi)
                lo, hi = (mid, hi) if count(np.maximum(h, mid)) > 0.7 * dofs else (lo, mid)
            h = np.maximum(h, hi)
        keep = h < H
        n_thin = count(h[keep]) if keep.any() else 0.0
        faces = {int(f): float(x) for f, x, k in zip(thin, h, keep) if k}
        rest_v = max(V - float(vol[keep].sum()), 0.05 * V)
        rest_a = max(A - float(areas[thin][keep].sum()), 0.05 * A)
        H_new = float(size) if size else _size_for(rest_v, rest_a, max(dofs - n_thin, 0.3 * dofs))
        if abs(H_new - H) < 0.02 * H:
            H = H_new
            break
        H = H_new
    faces = {f: x for f, x in faces.items() if x < H}
    if faces:
        worst = min(faces, key=lambda f: wall[f] / faces[f])
        through = wall[worst] / faces[worst]
        if through < 0.99:  # one layer of quadratic elements still bends right; less does not
            notes.append(f"walls down to {wall[worst]:.3g} mm have {through:.2g} elements through their thickness "
                         f"(budget); more dofs= resolve them better")
    return H, faces, notes


def locate(nodes, tets, points, tree=None):
    """For each point, a containing (or nearest) element and its volume coordinates: (elem, L (p, 4), tree)."""
    from scipy.spatial import cKDTree

    corners = nodes[tets[:, :4]]
    if tree is None:
        tree = cKDTree(corners.mean(1))
    p = np.atleast_2d(np.asarray(points, dtype=np.float64))
    k = min(24, len(tets))
    _, cand = tree.query(p, k=k)
    cand = cand.reshape(len(p), k)
    X = corners[cand]  # (p, k, 4, 3)
    T = np.stack([X[:, :, 1] - X[:, :, 0], X[:, :, 2] - X[:, :, 0], X[:, :, 3] - X[:, :, 0]], -1)  # (p, k, 3, 3)
    rhs = p[:, None, :] - X[:, :, 0]
    with np.errstate(all="ignore"):
        lam = np.linalg.solve(T, rhs[..., None])[..., 0]
    L = np.concatenate([1 - lam.sum(-1, keepdims=True), lam], -1)  # (p, k, 4)
    worst = np.nan_to_num(-L.min(-1), nan=np.inf)
    best = np.argmin(worst, axis=1)
    rows = np.arange(len(p))
    Lb = np.clip(L[rows, best], 0, None)
    Lb /= np.maximum(Lb.sum(-1, keepdims=True), 1e-300)
    return cand[rows, best], Lb, tree
