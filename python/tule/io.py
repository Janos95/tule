"""Import and export: STEP (with names, colors and tags), STL, glTF, tessellation.

Tags travel inside STEP files as named sub-shapes (``tag:load``), so a part
written by tule and read back, or rendered by the viewer, keeps them.
"""

from __future__ import annotations

import json
import os
import struct
import sys
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Iterable, Union

import numpy as np
from OCP.APIHeaderSection import APIHeaderSection_MakeHeader
from OCP.BRep import BRep_Tool
from OCP.BRepLib import BRepLib_ToolTriangulatedShape
from OCP.BRepMesh import BRepMesh_IncrementalMesh
from OCP.IFSelect import IFSelect_RetDone
from OCP.Quantity import Quantity_Color, Quantity_TOC_RGB
from OCP.STEPCAFControl import STEPCAFControl_Reader, STEPCAFControl_Writer
from OCP.STEPControl import STEPControl_AsIs
from OCP.TCollection import TCollection_ExtendedString, TCollection_HAsciiString
from OCP.TDataStd import TDataStd_Name
from OCP.TDF import TDF_Label
from OCP.TDocStd import TDocStd_Document
from OCP.TopAbs import TopAbs_REVERSED
from OCP.TopLoc import TopLoc_Location
from OCP.BRepBuilderAPI import BRepBuilderAPI_Copy, BRepBuilderAPI_Transform
from OCP.XCAFApp import XCAFApp_Application
from OCP.XCAFDoc import XCAFDoc_ColorGen, XCAFDoc_ColorSurf, XCAFDoc_DocumentTool

try:
    from OCP.TDF import TDF_LabelSequence  # OCP 7.x
except ImportError:  # OCP 8
    from OCP.collections import Sequence_TDF_Label as TDF_LabelSequence  # type: ignore

from ._occ import ShapeSet, kind_of, static, subshapes
from .errors import TuleError


def _quiet_occt():
    """Stop OCCT's data-exchange banners from flooding stdout."""
    try:
        from OCP.Message import Message, Message_PrinterOStream

        m = static(Message, "DefaultMessenger")()
        m.RemovePrinters(static(Message_PrinterOStream, "get_type_descriptor")())
    except Exception:  # pragma: no cover - best effort
        pass


_quiet_occt()


def _enable_subshape_names():
    """Write and read names of sub-shapes (how tags travel in STEP files)."""
    from OCP.Interface import Interface_Static

    for key in ("write.stepcaf.subshapes.name", "read.stepcaf.subshapes.name"):
        try:
            static(Interface_Static, "SetIVal")(key, 1)
        except Exception:  # pragma: no cover
            pass

if TYPE_CHECKING:  # pragma: no cover
    from .assembly import Assembly
    from .shape import Shape


# ---------------------------------------------------------------- colors

_NAMED = {
    "red": "#d33", "green": "#3a3", "blue": "#36c", "orange": "#e83", "yellow": "#eb3",
    "gray": "#888", "grey": "#888", "black": "#222", "white": "#eee", "steel": "#8a9aa8",
    "aluminium": "#c8ccd0", "aluminum": "#c8ccd0", "brass": "#c9a43a", "copper": "#c87533",
}


def parse_color(c: str | Iterable[float] | None) -> tuple[float, float, float] | None:
    """``"#4a8"``, ``"#44aa88"``, a few names, or an ``(r, g, b)`` tuple in 0..1."""
    if c is None:
        return None
    if not isinstance(c, str):
        r, g, b = list(c)[:3]
        return float(r), float(g), float(b)
    c = _NAMED.get(c.lower(), c)
    h = c.lstrip("#")
    if len(h) == 3:
        h = "".join(ch * 2 for ch in h)
    if len(h) != 6:
        raise TuleError(f"cannot read color {c!r}", hint='use "#rgb", "#rrggbb" or (r, g, b) in 0..1')
    return tuple(int(h[i : i + 2], 16) / 255.0 for i in (0, 2, 4))  # type: ignore[return-value]


def color_hex(rgb: tuple[float, float, float] | None) -> str | None:
    if rgb is None:
        return None
    return "#" + "".join(f"{max(0, min(255, round(v * 255))):02x}" for v in rgb)


# ---------------------------------------------------------------- atomic files


def _atomic_path(path: str) -> str:
    d, b = os.path.split(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f".{b}.tmp-{os.getpid()}")


def write_atomic(path: str, data: Union[bytes, str]) -> str:
    tmp = _atomic_path(path)
    mode = "wb" if isinstance(data, bytes) else "w"
    with open(tmp, mode) as f:
        f.write(data)
    os.replace(tmp, path)
    return path


# ---------------------------------------------------------------- STEP


def _new_doc():
    app = static(XCAFApp_Application, "GetApplication")()
    doc = TDocStd_Document(TCollection_ExtendedString("MDTV-XCAF"))
    app.InitDocument(doc)
    st = static(XCAFDoc_DocumentTool, "ShapeTool")(doc.Main())
    ct = static(XCAFDoc_DocumentTool, "ColorTool")(doc.Main())
    return doc, st, ct


def _set_name(label, name: str):
    static(TDataStd_Name, "Set")(label, TCollection_ExtendedString(name))


def _get_name(label) -> str | None:
    gid = static(TDataStd_Name, "GetID")()
    if label.IsNull() or not label.IsAttribute(gid):
        return None  # (FindAttribute on a label without one crashes OCP)
    attr = TDataStd_Name()
    if label.FindAttribute(gid, attr):
        n = attr.Get().ToExtString()
        try:  # STEP names are written as UTF-8 and read back byte by byte
            return n.encode("latin-1").decode("utf-8")
        except UnicodeError:
            return n
    return None


def _add_part(st, ct, shape: "Shape", default_name: str, prototype: bool = False, instances: list | None = None):
    """Add a part to an XDE document with its name, color and tags.

    With ``prototype``, the part's placement is stripped (returned separately)
    so it can be added as an assembly component at that location.
    """
    src = shape.wrapped
    loc = src.Location()
    proto = src.Located(TopLoc_Location())
    if prototype or loc.IsIdentity():
        target_shape = proto if prototype else src
    else:
        # a moved part: bake the placement into the geometry, or the writer makes it an
        # assembly instance and drops the part's name and tags
        target_shape = BRepBuilderAPI_Transform(proto, loc.Transformation(), True).Shape()
    name = shape.name or default_name
    if instances is not None:
        same = [(n, lab) for other, n, lab in instances if other.IsPartner(proto)]
        for other_name, other_label in same:
            if other_name == name:
                return other_label, loc  # another instance of the same part
        if same:  # the same solid under another name (e.g. a moved copy) is a part of its own
            target_shape = BRepBuilderAPI_Copy(target_shape).Shape()
    label = st.AddShape(target_shape, False)
    if instances is not None:
        instances.append((proto, name, label))
    _set_name(label, name)
    rgb = parse_color(shape.color)
    if rgb:
        ct.SetColor(label, Quantity_Color(*rgb, Quantity_TOC_RGB), XCAFDoc_ColorGen)
    # tags: one named sub-shape label per tagged face/edge
    seen = ShapeSet()
    names_of: list[list[str]] = []
    for name, key in shape._alias.items():
        if not key.startswith("tag:"):
            continue
        for x in shape._groups.get(key, ()):
            i = seen.index(x)
            if i < 0:
                seen.add(x)
                names_of.append([name])
            else:
                names_of[i].append(name)
    remap = target_shape is not src
    proto_subs = {k: subshapes(target_shape, k) for k in ("face", "edge", "vertex")} if remap else None
    for x, names in zip(seen, names_of):
        target = x
        if remap:
            kind = kind_of(x)
            i = shape._sub(kind).index(x)
            if i < 0:
                continue
            target = proto_subs[kind][i]
        sub = st.AddSubShape(label, target)
        if not sub.IsNull():
            _set_name(sub, "tag:" + ",".join(sorted(set(names))))
    return label, loc


def export_step(item: Union["Shape", "Assembly"], path: str) -> str:
    """Write a shape or assembly to STEP with part names, colors and tags."""
    from .assembly import Assembly

    doc, st, ct = _new_doc()
    if isinstance(item, Assembly):
        root = st.NewShape()
        _set_name(root, item.name or os.path.splitext(os.path.basename(path))[0])
        instances: list = []
        for i, (pname, part) in enumerate(item.parts()):
            lab, loc = _add_part(st, ct, part, pname.rsplit("/", 1)[-1], prototype=True, instances=instances)
            st.AddComponent(root, lab, loc)
        st.UpdateAssemblies()
    else:
        _add_part(st, ct, item, os.path.splitext(os.path.basename(path))[0])
    w = STEPCAFControl_Writer()
    _enable_subshape_names()
    w.SetNameMode(True)
    w.SetColorMode(True)
    w.Transfer(doc, STEPControl_AsIs)
    # no time of writing in the header: the same shape gives the same file (tule build, git)
    APIHeaderSection_MakeHeader(w.ChangeWriter().Model()).SetTimeStamp(TCollection_HAsciiString("1970-01-01T00:00:00"))
    tmp = _atomic_path(path)
    if w.Write(tmp) != IFSelect_RetDone:
        raise TuleError("could not write the STEP file", op="export_step", hint=path)
    os.replace(tmp, path)
    return path


def import_step(path: str):
    """Read a STEP file: a Shape (one part) or an Assembly, with names, colors and tags."""
    from .assembly import Assembly
    from .shape import Shape

    sys.audit("tule.read", os.path.abspath(path))  # read by native code: tule build records it this way
    r = STEPCAFControl_Reader()
    _enable_subshape_names()
    r.SetNameMode(True)
    r.SetColorMode(True)
    if not os.path.isfile(path):
        d = os.path.dirname(path) or "."
        there = sorted(f for f in os.listdir(d) if f.lower().endswith((".step", ".stp"))) if os.path.isdir(d) else []
        raise TuleError(f"no such file: {path}", op="import_step",
                        hint=f"STEP files in {d}/: {', '.join(there)}" if there else f"{d}/ does not exist" if not os.path.isdir(d) else f"no STEP files in {d}/")
    if r.ReadFile(path) != IFSelect_RetDone:
        raise TuleError("could not read the STEP file", op="import_step", hint=path)
    doc, st, ct = _new_doc()
    r.Transfer(doc)

    def color_of(label):
        c = Quantity_Color()
        for t in (XCAFDoc_ColorGen, XCAFDoc_ColorSurf):
            if static(type(ct), "GetColor")(label, t, c):
                return color_hex((c.Red(), c.Green(), c.Blue()))
        return None

    def part_from(label, loc=None, instance=None) -> Shape:
        s = static(st.__class__, "GetShape")(label)
        name = _get_name(label)
        color = color_of(label) or (color_of(instance) if instance is not None else None)
        if color is None:
            c = Quantity_Color()
            for t in (XCAFDoc_ColorGen, XCAFDoc_ColorSurf):
                if static(type(ct), "GetColor")(s, t, c) if False else ct.GetColor(s, t, c):
                    color = color_hex((c.Red(), c.Green(), c.Blue()))
                    break
        sh = Shape(s, name=name, color=color)
        subs = TDF_LabelSequence()
        static(st.__class__, "GetSubShapes")(label, subs)
        for i in range(1, subs.Length() + 1):
            lab = subs.Value(i)
            n = _get_name(lab) or ""
            if not n.startswith("tag:"):
                continue
            sub = static(st.__class__, "GetShape")(lab)
            from .selection import SELECTION_TYPES

            kind = kind_of(sub)
            if kind not in SELECTION_TYPES:
                continue
            for tag in n[4:].split(","):
                sel = SELECTION_TYPES[kind](sh, [x for x in sh._sub(kind) if x.IsSame(sub)])
                if sel.items:
                    sh = sh.tag(tag, sel)
        if loc is not None and not loc.IsIdentity():
            from .shape import carry
            from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform

            b = BRepBuilderAPI_Transform(sh.wrapped, loc.Transformation(), True)
            from ._occ import from_list

            sh = Shape(b.Shape(), carry(sh._groups, b.Shape(), lambda x: from_list(b.Modified(x))), sh._alias, sh.name, sh.color)
        return sh

    free = TDF_LabelSequence()
    st.GetFreeShapes(free)
    parts = []
    names = []
    for i in range(1, free.Length() + 1):
        lab = free.Value(i)
        if static(st.__class__, "IsAssembly")(lab):
            comps = TDF_LabelSequence()
            static(st.__class__, "GetComponents")(lab, comps, True)
            names.append(_get_name(lab))
            for j in range(1, comps.Length() + 1):
                c = comps.Value(j)
                ref = TDF_Label()
                static(st.__class__, "GetReferredShape")(c, ref)
                loc = static(st.__class__, "GetLocation")(c)
                parts.append(part_from(ref, loc, c))
        else:
            parts.append(part_from(lab))
    if len(parts) == 1 and not names:
        return parts[0]
    return Assembly(*parts, name=(names[0] if names else None))


# ---------------------------------------------------------------- meshes


@dataclass
class Mesh:
    """A triangle mesh with B-rep face ids per triangle and B-rep edges as polylines."""

    positions: np.ndarray  # (n, 3) float32
    normals: np.ndarray  # (n, 3) float32
    indices: np.ndarray  # (m, 3) uint32
    face_ids: np.ndarray  # (m,) uint32, the B-rep face of each triangle
    edges: list = field(default_factory=list)  # [(edge_id, (k, 3) float32 polyline)]

    @property
    def triangles(self) -> int:
        return int(len(self.indices))


def _mesh(shape, tolerance: float, angular: float):
    BRepMesh_IncrementalMesh(shape, float(tolerance), False, float(angular), True)


def tessellate(item: Union["Shape", "Assembly"], tolerance: float = 0.05, angular: float = 0.2) -> Mesh:
    """Triangulate a shape; normals come from the surfaces, ids match ``shape.faces``/``shape.edges``."""
    from .assembly import Assembly

    shape = item.to_shape() if isinstance(item, Assembly) else item
    s = shape.wrapped
    _mesh(s, tolerance, angular)
    pos, nor, idx, fid = [], [], [], []
    base = 0
    face_tri = {}
    faces = shape._sub("face")
    for i, f in enumerate(faces):
        loc = TopLoc_Location()
        tri = static(BRep_Tool, "Triangulation")(f, loc)
        if tri is None:
            continue
        face_tri[i] = (tri, loc)
        trsf = loc.Transformation()
        n = tri.NbNodes()
        if not tri.HasNormals():
            static(BRepLib_ToolTriangulatedShape, "ComputeNormals")(f, tri)
        rev = f.Orientation() == TopAbs_REVERSED
        for j in range(1, n + 1):
            p = tri.Node(j).Transformed(trsf)
            pos.append((p.X(), p.Y(), p.Z()))
            d = tri.Normal(j).Transformed(trsf)
            nv = (d.X(), d.Y(), d.Z())
            nor.append((-nv[0], -nv[1], -nv[2]) if rev else nv)
        for k in range(1, tri.NbTriangles() + 1):
            a, b, c = tri.Triangle(k).Get()
            if rev:
                b, c = c, b
            idx.append((base + a - 1, base + b - 1, base + c - 1))
            fid.append(i)
        base += n
    edges = []
    edge_set = shape._sub("edge")
    seams = shape._seams()
    for ei, e in enumerate(edge_set):
        if e in seams or static(BRep_Tool, "Degenerated")(e):
            continue
        for fi, f in enumerate(faces):
            if fi not in face_tri or e not in subshapes(f, "edge"):
                continue
            tri, loc = face_tri[fi]
            poly = static(BRep_Tool, "PolygonOnTriangulation")(e, tri, loc)
            if poly is None:
                continue
            trsf = loc.Transformation()
            pts = []
            for k in range(1, poly.NbNodes() + 1):
                p = tri.Node(poly.Node(k)).Transformed(trsf)
                pts.append((p.X(), p.Y(), p.Z()))
            edges.append((ei, np.asarray(pts, dtype=np.float32)))
            break
    return Mesh(
        np.asarray(pos, dtype=np.float32).reshape(-1, 3),
        np.asarray(nor, dtype=np.float32).reshape(-1, 3),
        np.asarray(idx, dtype=np.uint32).reshape(-1, 3),
        np.asarray(fid, dtype=np.uint32),
        edges,
    )


def write_stl_mesh(path: str, positions, indices) -> str:
    """Write a binary STL from vertex positions and triangle indices (e.g. a topology optimization result)."""
    P = np.asarray(positions, dtype=np.float32).reshape(-1, 3)
    I = np.asarray(indices, dtype=np.int64).reshape(-1, 3)
    tri = P[I]
    n = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    ln = np.linalg.norm(n, axis=1, keepdims=True)
    n = np.where(ln > 0, n / np.maximum(ln, 1e-30), 0).astype(np.float32)
    rec = np.zeros(len(I), dtype=[("n", "<f4", 3), ("v", "<f4", (3, 3)), ("a", "<u2")])
    rec["n"] = n
    rec["v"] = tri
    data = b"tule".ljust(80, b" ") + struct.pack("<I", len(I)) + rec.tobytes()
    return write_atomic(path, data)


def export_stl(item: Union["Shape", "Assembly"], path: str, tolerance: float = 0.05, angular: float = 0.2) -> str:
    """Write a binary STL (all parts of an assembly in one file)."""
    m = tessellate(item, tolerance, angular)
    return write_stl_mesh(path, m.positions, m.indices)


def export_gltf(item: Union["Shape", "Assembly"], path: str, tolerance: float = 0.05, angular: float = 0.2) -> str:
    """Write a binary glTF (.glb) with one mesh per part, colored."""
    from .assembly import Assembly

    parts = item.parts() if isinstance(item, Assembly) else [(item.name or "part", item)]
    bin_chunks, buffer_views, accessors, meshes, nodes, materials = [], [], [], [], [], []
    offset = 0

    def add_view(arr: np.ndarray, target: int) -> int:
        nonlocal offset
        b = arr.tobytes()
        pad = (-len(b)) % 4
        bin_chunks.append(b + b"\0" * pad)
        buffer_views.append({"buffer": 0, "byteOffset": offset, "byteLength": len(b), "target": target})
        offset += len(b) + pad
        return len(buffer_views) - 1

    for name, part in parts:
        m = tessellate(part, tolerance, angular)
        if not m.triangles:
            continue
        pv = add_view(m.positions, 34962)
        nv = add_view(m.normals, 34962)
        iv = add_view(m.indices.reshape(-1), 34963)
        lo, hi = m.positions.min(0).tolist(), m.positions.max(0).tolist()
        accessors += [
            {"bufferView": pv, "componentType": 5126, "count": len(m.positions), "type": "VEC3", "min": lo, "max": hi},
            {"bufferView": nv, "componentType": 5126, "count": len(m.normals), "type": "VEC3"},
            {"bufferView": iv, "componentType": 5125, "count": int(m.indices.size), "type": "SCALAR"},
        ]
        a0 = len(accessors) - 3
        rgb = parse_color(part.color) or (0.62, 0.66, 0.70)
        materials.append({"name": name, "pbrMetallicRoughness": {"baseColorFactor": [*rgb, 1.0], "metallicFactor": 0.1, "roughnessFactor": 0.6}})
        meshes.append({"name": name, "primitives": [{"attributes": {"POSITION": a0, "NORMAL": a0 + 1}, "indices": a0 + 2, "material": len(materials) - 1}]})
        nodes.append({"name": name, "mesh": len(meshes) - 1})
    gltf = {
        "asset": {"version": "2.0", "generator": "tule"},
        "scene": 0,
        "scenes": [{"nodes": list(range(len(nodes)))}],
        "nodes": nodes,
        "meshes": meshes,
        "materials": materials,
        "accessors": accessors,
        "bufferViews": buffer_views,
        "buffers": [{"byteLength": offset}],
    }
    js = json.dumps(gltf, separators=(",", ":")).encode()
    js += b" " * ((-len(js)) % 4)
    bin_data = b"".join(bin_chunks)
    out = (
        struct.pack("<III", 0x46546C67, 2, 12 + 8 + len(js) + 8 + len(bin_data))
        + struct.pack("<II", len(js), 0x4E4F534A) + js
        + struct.pack("<II", len(bin_data), 0x004E4942) + bin_data
    )
    return write_atomic(path, out)
