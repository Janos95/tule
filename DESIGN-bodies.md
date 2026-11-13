# Tule — three kinds of geometry (B-rep, mesh, implicit)

Status: built (see "Open questions" at the end). The rest of the design is in `DESIGN.md`.

Tule stays unopinionated about how a part is modeled. A part is one of three kinds, each a
full modeling system with the same everyday API (`named`, `colored`, `at`, `rotate`,
`tag`, `|`, `-`, `&`, `bounds`, `volume`, outputs, FEA), like nTop's typed bodies:

| kind | Python type | holds | made by | exact? |
|---|---|---|---|---|
| `"brep"` | `tule.Shape` | OpenCascade B-rep: exact faces, edges | `box`, `extrude`, … `.fillet` | exact surfaces |
| `"mesh"` | `tule.Mesh` | triangles + per-triangle regions + tags | `.to_mesh()`, `Mesh.load`, `Mesh(points, triangles)`, `topopt` | facets |
| `"implicit"` | `tule.Implicit` (`tule.sdf`) | a signed-distance expression (or a sampled grid) | `sdf.box`, `sdf.gyroid`, `sdf.field(lambda x, y, z: …)` | exact field, no faces |

Every value says what it is: `part.kind`, and its `repr` names the kind and, for meshes and
implicits, where it came from and what was lost on the way (`.history`):

    <Mesh 'bracket': 18,204 triangles, closed, 9 regions, 2 tags
          (tessellated from B-rep within 0.05 mm; union with a mesh of an implicit, cells 0.4 mm)>

## Conversions are explicit

Operators never mix kinds: `brep | mesh` raises and says which conversion to write. Each
conversion records what it lost in the result's `history`.

| from → to | call | engine | what is lost |
|---|---|---|---|
| B-rep → mesh | `part.to_mesh(tolerance=0.05, angular=0.2)` | OCCT BRepMesh | exact surfaces (chord error ≤ tolerance); faces become regions, tags carried |
| implicit → mesh | `body.to_mesh(cell=0.5, bounds=None)` | **Fidget** (JIT + manifold dual contouring; sharp edges kept) | detail under a cell; tags given by predicate are applied to the triangles |
| mesh → implicit | `mesh.to_implicit(cell=0.5)` | sampled signed distance (grid, trilinear) | detail under a cell; regions and tags |
| B-rep → implicit | `part.to_implicit(cell=0.5)` | via `to_mesh` then the above | both of the above |
| mesh → B-rep | `mesh.to_brep()` | OCCT sewing, coplanar facets merged | nothing geometric, but faces are flat facets; refused above 20,000 triangles |
| implicit → B-rep | `body.to_mesh(...).to_brep()` | | |

`tessellate(part)` stays (it is the same as `part.to_mesh` and returns a `Mesh`).

## Booleans

- B-rep ∘ B-rep: OCCT (unchanged).
- mesh ∘ mesh: **manifold3d** (exact-ish, robust, fast). Each input triangle's region survives
  (manifold keeps `faceID` per input), so tags survive unions and cuts.
- implicit ∘ implicit: `min`/`max` (sharp) or `union(a, b, smooth=r)` (polynomial smooth min), in
  the expression; no meshing until `to_mesh`.
- across kinds: convert first, e.g. `part.to_mesh() | lattice.to_mesh(cell=0.4)`.

## Meshes (`tule.Mesh`)

Vertices (float64, welded), triangles, `face_ids` (a region id per triangle) and tags (name →
set of regions). Regions play the part B-rep faces play: the viewer's `f12`, selections,
loads. A mesh tessellated from a B-rep keeps one region per B-rep face (`mesh.faces[3]` is the
B-rep's face 3) and every tag. A mesh from elsewhere starts with regions split at creases
(feature edges sharper than 35°), so a traced bracket's flat pads come out as regions.

Selections (`mesh.faces`) are sets of triangles with the B-rep `Faces` vocabulary where it
makes sense: `["tag"]`, `[3]`, `.top()`, `.bottom()`, `.normal(dir, tol_deg)`,
`.within(lo, hi)`, `.nearest(point)`, `.where(fn)` (`fn(centers, normals) -> bool mask`),
`.largest()`, `|`, `&`, `-`, `.ids`, `.area`. `mesh.tag("load", sel)` names a selection; when it
cuts through regions they are split, so a tag is always whole regions and everything
downstream (viewer, FEA, overlays) keeps working on region ids.

Also: `volume`, `area`, `bounds`, `is_closed` (watertight: each edge used as often both ways, so
two solids touching along an edge still count), `simplify(tolerance)` (manifold3d),
`refine(edge)`, transforms, `export_ply`/`export_stl`/`export_3mf`, `Mesh.load(path)` (PLY, STL,
OBJ; a PLY Tule wrote keeps its regions, name, color and tags), `Mesh(points, triangles)`.

## Implicits (`tule.sdf`, `tule.Implicit`)

An expression over `x, y, z` (mm), negative inside. Primitives (`box`, `sphere`, `cylinder`,
`capsule`, `torus`, `plane`, `slab`), TPMS (`gyroid`, `schwarz_p`, `diamond`, as
fields with a period, and `.shell(t)` to make walls), booleans (sharp and `smooth=`),
`offset(d)`, `shell(t)`, transforms, `field(fn, bounds)` for anything written as math with
`sdf.sin`, `sdf.sqrt`, … on symbolic `x, y, z`. Bounds travel with the expression (union:
both, intersection: the overlap, TPMS: unbounded) and `to_mesh` needs finite ones.

Tags on an implicit are predicates (`body.tag("top", lambda f: f.top())`), applied to the
mesh's faces at `to_mesh`.

**Engine: Fidget.** There are no Python bindings to Fidget anywhere (PyPI's `fidget` is an
unrelated Qt package), so Tule has its own, minimal: `fidget-py/` (PyO3, abi3 wheel
`tule_fidget`, ~170 lines of Rust). Python serializes the expression DAG as a node list
`(op, a, b, value)`; Rust rebuilds a `fidget::Context`, JIT-compiles it and either evaluates
points (values, or values and gradients) or meshes it (`Octree::build` + `walk_dual`, in a cube
`center ± half` at `depth` = ceil(log2(size / cell))). No state crosses calls; the GIL is
released while Fidget works. Built in a Rust stage of `runtime/Dockerfile` (x86_64 and
arm64) and in CI (`.github/workflows/runtime.yml`).

Sampled fields (`mesh.to_implicit()`) are not closed-form, so Fidget cannot evaluate them. An
expression containing one is evaluated by NumPy (the same ops) and meshed by marching cubes
at the cell size; its history says so. Without the `tule_fidget` wheel (a laptop without
Rust), the same NumPy path runs everything, with a note.

## Tags and FEA per kind

| | B-rep | mesh | implicit |
|---|---|---|---|
| tag what | faces, edges (by selection) | regions (whole, or split by a selection) | predicates, applied on meshing |
| FEA | voxel or tet (unchanged) | tet: gmsh remeshes the region surfaces (`ClassifySurfaces`), tet faces map back to regions; a mesh gmsh cannot reparametrize (dual-contoured) is meshed on its own triangles, slowly (41k triangles: 250k tets, ~40 s). `mesh="auto"` picks voxels above 20,000 triangles. Voxel: z-rays through the triangles | convert: `fea.static(body.to_mesh(cell=…), …)` |
| thickness | rays on B-rep | manifold3d ray casts (~4 µs a ray) | |

## Viewer, pictures, prints

A mesh part is written as binary little-endian PLY: shared float32 vertices, triangles with an
`int region` per face, and name, color, tags and history in a `comment tule {json}` header line,
so one file carries everything (STL only when asked for, for slicers). The agent's `encode_mesh`
reads PLY, STL and OBJ; view-only links parse them in the browser. It splits normals
at creases (smooth shading on organic parts) and sends only feature edges (region boundaries and
creases over 35°): a traced part never shows every triangle edge. Pictures, plates/3MF,
assemblies and `outputs.part` take meshes like shapes. Implicits must be meshed first
(`outputs.part("lattice", body.to_mesh(cell=0.4))`) so the resolution is a visible choice.

## Open questions

1. Smooth booleans between a B-rep and an implicit need the B-rep as a sampled field
   (`part.to_implicit(cell=…)`); memory grows as (size / cell)³. A narrow-band sparse grid
   would scale further.
2. FEA straight on implicits (no surface mesh): voxels from the field's sign would be natural;
   not built (explicit `to_mesh` first).
3. Mesh → B-rep is faceted only; fitting analytic surfaces (planes, cylinders) to regions is
   possible later.
4. Tet FEA on Fidget meshes: gmsh's surface classification fails on dual-contoured triangles,
   and `simplify` first did not help. Remeshing the field (isotropic remesh, or gmsh on the
   field's own levelset) would give tetrahedra fit for thin lattice walls; until then voxels
   (about 2 per 1.6 mm sheet in the example) are the honest default and a coarse one.
