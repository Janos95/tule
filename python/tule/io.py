"""Import and export: STEP (with names, colors and tags), STL, glTF, 3MF print plates, tessellation.

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
from OCP.BRepTools import BRepTools_Modifier
from OCP.ShapeCustom import ShapeCustom_DirectModification
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
    # The writer turns left-handed surfaces (an extruded circle, a mirror) right-handed and loses the
    # names of the faces it rebuilds: do it here and carry the tags across.
    direct = BRepTools_Modifier(target_shape, ShapeCustom_DirectModification())
    final = direct.ModifiedShape(target_shape) if direct.IsDone() else target_shape
    label = st.AddShape(final, False)
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
        if final is not target_shape:
            try:
                target = direct.ModifiedShape(target)
            except Exception:  # pragma: no cover - not a sub-shape of the part
                continue
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
    if len(parts) == 1:  # one part, even inside an assembly node: a Shape
        p = parts[0]
        return p if p.name or not names or not names[0] else p.named(names[0])
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
    faces_of: dict[int, list[int]] = {}  # edge id -> the triangulated faces around it, in face order (one pass)
    for fi, f in enumerate(faces):
        if fi in face_tri:
            for e in subshapes(f, "edge"):
                faces_of.setdefault(edge_set.index(e), []).append(fi)
    for ei, e in enumerate(edge_set):
        if e in seams or static(BRep_Tool, "Degenerated")(e):
            continue
        for fi in faces_of.get(ei, ()):
            f = faces[fi]
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


# ---------------------------------------------------------------- 3MF print plates
#
# What the slicers read (checked against their source: BambuStudio and OrcaSlicer
# src/libslic3r/Format/bbs_3mf.cpp, PrusaSlicer 2.9 src/libslic3r/Format/3mf.cpp):
# - 3D/3dmodel.model (3MF core): one object per part with its mesh in mm, its name, and a
#   build item that places it. Colors as core <basematerials> (shown by 3MF viewers; the
#   slicers print every part with the filament the user picks and ask nothing).
# - Metadata/model_settings.config (Bambu Studio, OrcaSlicer): per-object settings, applied
#   over the process preset the user picks. No project_settings.config: a partial one
#   would be taken for a whole printer and process profile.
# - Metadata/Slic3r_PE_model.config (PrusaSlicer): the same per-object settings, its names.

PRINT_SETTINGS = {
    "layer_height": "layer height in mm (0.2)",
    "walls": "wall loops / perimeters (3)",
    "infill": "sparse infill in percent (15)",
    "supports": "True or False",
    "brim": "brim width in mm, True (5 mm) or False",
    "material": "the filament, PLA, PETG, ABS, ASA, TPU, PA or PC (for the estimate; the user picks it in the slicer)",
    "copies": "a part's number of copies on the plate (its own settings only)",
}

# g/cm³ and nozzle/bed °C of generic filaments: the print estimate's profile
MATERIALS = {"PLA": (1.24, 215, 60), "PETG": (1.27, 240, 80), "ABS": (1.04, 255, 100), "ASA": (1.07, 260, 100),
             "TPU": (1.21, 230, 50), "PA": (1.14, 275, 90), "PC": (1.20, 275, 110)}


def _print_settings(settings, who: str = "settings") -> dict:
    """Checks print settings: the keys of ``PRINT_SETTINGS``, values in sensible ranges.
    ``material`` belongs to the plate's settings, ``copies`` to a part's own."""
    if settings is None:
        return {}
    if not isinstance(settings, dict):
        raise TuleError(f"{who} must be a dict", op="export_3mf", hint='{"layer_height": 0.2, "walls": 3, "infill": 15, "supports": False}')
    own = who != "settings"
    out = {}
    for k, v in settings.items():
        if k not in PRINT_SETTINGS:
            raise TuleError(f"unknown print setting {k!r}", op="export_3mf",
                            hint="known: " + "; ".join(f"{n}: {d}" for n, d in PRINT_SETTINGS.items()))
        if v is None:
            continue
        if hasattr(v, "item"):
            v = v.item()
        if k == "copies" and not own:
            raise TuleError("copies are per part, not for the whole plate", op="export_3mf",
                            hint='("pin", pin, {"copies": 4}); the whole plate twice: print it twice')
        if k == "material" and own:
            raise TuleError(f"material is the whole plate's (one filament), not in {who}", op="export_3mf",
                            hint='settings={"material": "PETG"}')
        if k == "copies":
            if isinstance(v, bool) or not isinstance(v, (int, float)) or v != int(v) or not 1 <= v <= 500:
                raise TuleError(f"copies must be a whole number from 1 to 500, got {v!r}", op="export_3mf", hint='{"copies": 4}')
            v = int(v)
        elif k == "material":
            m = str(v).strip().upper()
            if m not in MATERIALS:
                raise TuleError(f"unknown material {v!r}", op="export_3mf", hint="one of " + ", ".join(MATERIALS))
            v = m
        elif k == "supports":
            if not isinstance(v, bool):
                raise TuleError(f"supports must be True or False, got {v!r}", op="export_3mf")
        elif k == "brim":
            v = 5.0 if v is True else 0.0 if v is False else v
            if not isinstance(v, (int, float)) or not 0 <= v <= 50:
                raise TuleError(f"brim must be a width in mm (0 to 50), True or False, got {v!r}", op="export_3mf")
            v = float(v)
        elif k == "walls":
            if isinstance(v, bool) or not isinstance(v, (int, float)) or v != int(v) or not 1 <= v <= 30:
                raise TuleError(f"walls must be a whole number from 1 to 30, got {v!r}", op="export_3mf")
            v = int(v)
        elif k == "infill":
            if isinstance(v, str) and v.strip().endswith("%"):
                v = v.strip()[:-1]
            try:
                v = float(v)
            except (TypeError, ValueError):
                v = -1.0
            if not 0 <= v <= 100:
                raise TuleError(f"infill is a percentage from 0 to 100, got {settings[k]!r}", op="export_3mf", hint='"infill": 15')
        elif k == "layer_height":
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0.04 <= v <= 1.0:
                raise TuleError(f"layer_height must be in mm, 0.04 to 1.0, got {v!r}", op="export_3mf", hint='"layer_height": 0.2')
            v = float(v)
        out[k] = v
    return out


def _num(v: float) -> str:
    return f"{v:.6g}"


def _slicer_keys(s: dict) -> tuple[list, list]:
    """Per-object config keys for Bambu Studio / OrcaSlicer and for PrusaSlicer."""
    bambu, prusa = [], []
    if "layer_height" in s:
        bambu.append(("layer_height", _num(s["layer_height"])))
        prusa.append(("layer_height", _num(s["layer_height"])))
    if "walls" in s:
        bambu.append(("wall_loops", str(s["walls"])))
        prusa.append(("perimeters", str(s["walls"])))
    if "infill" in s:
        bambu.append(("sparse_infill_density", _num(s["infill"]) + "%"))
        prusa.append(("fill_density", _num(s["infill"]) + "%"))
        if s["infill"] >= 100:  # gyroid and the other sparse patterns refuse 100 %
            bambu.append(("sparse_infill_pattern", "zig-zag"))
            prusa.append(("fill_pattern", "rectilinear"))
    if "supports" in s:
        on = "1" if s["supports"] else "0"
        bambu.append(("enable_support", on))
        prusa.append(("support_material", on))
        if s["supports"]:
            prusa.append(("support_material_auto", "1"))
    if "brim" in s:
        w = s["brim"]
        kind = "outer_only" if w > 0 else "no_brim"
        bambu += [("brim_type", kind)] + ([("brim_width", _num(w))] if w > 0 else [])
        prusa += [("brim_type", kind), ("brim_width", _num(w))]
    return bambu, prusa


def _closed_mesh(shape: "Shape", name: str, tolerance: float, angular: float) -> tuple[np.ndarray, np.ndarray]:
    """A watertight triangle mesh of a solid: the faces' triangulations welded along their edges."""
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components
    from scipy.spatial import cKDTree

    m = tessellate(shape, tolerance, angular)
    if not m.triangles:
        raise TuleError(f"part {name!r} has no surface to print", op="export_3mf", hint="pass solids")
    P = m.positions.astype(np.float64)
    pairs = cKDTree(P).query_pairs(1e-3, output_type="ndarray")  # the same B-rep edge point, once per face
    n = len(P)
    g = coo_matrix((np.ones(len(pairs)), (pairs[:, 0], pairs[:, 1])), shape=(n, n))
    _, label = connected_components(g, directed=False)
    F = label[m.indices.astype(np.int64)]
    F = F[(F[:, 0] != F[:, 1]) & (F[:, 1] != F[:, 2]) & (F[:, 0] != F[:, 2])]
    used, F = np.unique(F, return_inverse=True)
    F = F.reshape(-1, 3)
    V = np.zeros((label.max() + 1, 3))
    V[label] = P  # the points of one weld lie within a micron of each other: any one will do
    V = V[used]
    # closed: every edge in exactly two triangles, once in each direction
    e = np.concatenate([F[:, [0, 1]], F[:, [1, 2]], F[:, [2, 0]]])
    key = e[:, 0] * len(V) + e[:, 1]
    rev = e[:, 1] * len(V) + e[:, 0]
    if len(np.unique(key)) != len(key) or not np.isin(key, rev).all():
        open_ = int((~np.isin(key, rev)).sum())
        raise TuleError(f"part {name!r} does not make a closed mesh ({open_} open edges)", op="export_3mf",
                        hint="pass a closed solid (not a face or an open shell); if it is one, try tolerance=0.01")
    tri = V[F]
    vol = np.einsum("ij,ij->i", tri[:, 0], np.cross(tri[:, 1], tri[:, 2])).sum() / 6
    if vol < 0:
        F = F[:, ::-1]
    return V, F


def _plate_items(items) -> list[tuple[str, "Shape", dict, int]]:
    """``items`` as (name, shape, settings, copies): Shapes, (name, shape[, settings]) pairs, Assemblies, or a list of them."""
    from .assembly import Assembly
    from .shape import Shape

    if isinstance(items, (Shape, Assembly)) or (isinstance(items, tuple) and items and isinstance(items[0], str)):
        items = [items]
    out = []

    def add(name, shape, settings=None):
        if not isinstance(shape, Shape):
            raise TuleError(f"cannot print a {type(shape).__name__}", op="export_3mf",
                            hint="pass Shapes, (name, shape) pairs, (name, shape, settings) or an Assembly")
        own = _print_settings(settings, f"settings of {name or shape.name or 'a part'!r}")
        out.append((name or shape.name or f"part{len(out) + 1}", shape, own, own.pop("copies", 1)))

    for it in items:
        if isinstance(it, Assembly):
            for path, p in it.parts():
                add(path.rsplit("/", 1)[-1], p)
        elif isinstance(it, Shape):
            add(None, it)
        elif isinstance(it, (tuple, list)) and 2 <= len(it) <= 3 and isinstance(it[0], str):
            add(*it)
        else:
            raise TuleError(f"cannot print {it!r}", op="export_3mf",
                            hint="pass Shapes, (name, shape) pairs, (name, shape, settings) or an Assembly")
    if not out:
        raise TuleError("nothing to print", op="export_3mf")
    seen: dict[str, int] = {}
    named = []
    for name, shape, s, n in out:  # one name per object: the slicer's list and the viewer's files
        k = seen[name] = seen.get(name, 0) + 1
        named.append((name if k == 1 else f"{name}-{k}", shape, s, n))
    return named


def _shelves(sizes, width: float, gap: float):
    """Rows left to right, deepest parts first: [(x, y)] lower-left corners, and the layout's size."""
    order = sorted(range(len(sizes)), key=lambda i: (-sizes[i][1], -sizes[i][0], i))
    rows: list[list] = []  # [y, depth, x cursor, [items]]
    for i in order:
        w, d = sizes[i]
        for r in rows:
            if r[2] + w <= width + 1e-9:
                break
        else:
            r = [0.0, d, 0.0, []]
            rows.append(r)
        r[3].append((i, r[2]))
        r[2] += w + gap
    pos = [None] * len(sizes)
    y = 0.0
    total_w = max(r[2] - gap for r in rows)
    for r in rows:
        shift = (total_w - (r[2] - gap)) / 2  # each row centered
        for i, x in r[3]:
            pos[i] = (x + shift, y + (r[1] - sizes[i][1]) / 2)
        y += r[1] + gap
    return pos, (total_w, y - gap)


def _plate(items, bed=(256, 256, 256), gap: float = 5.0, arrange: bool = True) -> list[tuple[str, "Shape", dict, int, int]]:
    """Parts laid out on the bed (origin at its front left corner), each dropped to z = 0:
    (name, shape, settings, copy, copies), one entry per copy (copy 1 to copies, next to each other)."""
    listed = _plate_items(items)
    if not arrange and any(n > 1 for *_, n in listed):
        raise TuleError("copies need arrange=True (with arrange=False they would sit on each other)", op="export_3mf",
                        hint="arrange=True, or place each copy yourself as a part of its own")
    parts = [(name, shape, st) for name, shape, st, n in listed for _ in range(n)]
    which = [(k + 1, n) for *_, n in listed for k in range(n)]
    bed = tuple(float(b) for b in bed)
    if len(bed) not in (2, 3) or min(bed) <= 0:
        raise TuleError(f"bed is (width, depth) or (width, depth, height) in mm, got {bed}", op="export_3mf", hint="bed=(256, 256)")
    W, D = bed[:2]
    boxes = [s.bounds for _, s, _ in parts]
    sizes = [(hi.x - lo.x, hi.y - lo.y) for lo, hi in boxes]
    for (name, _, _), (lo, hi), (w, d) in zip(parts, boxes, sizes):
        h = hi.z - lo.z
        if w > W + 1e-6 or d > D + 1e-6 or (len(bed) == 3 and h > bed[2] + 1e-6):
            turn = d <= W and w <= D and (len(bed) == 2 or h <= bed[2])
            raise TuleError(f"part {name!r} ({w:.1f} × {d:.1f} × {h:.1f} mm) does not fit the bed ({' × '.join(f'{b:g}' for b in bed)} mm)",
                            op="export_3mf", hint="turn it: part.rotate(z=90)" if turn else
                            "lay it on another face, split it, or give the printer's bed=(width, depth, height)")
    if arrange:
        best = None
        widths = sorted({W} | {min(W, sum(w for w, _ in sizes[:k]) + gap * (k - 1)) for k in range(1, len(sizes) + 1)})
        for cw in widths:  # the most compact layout that fits: near-square, parts close to the bed's middle
            if cw < max(w for w, _ in sizes) - 1e-9:
                continue
            pos, (lw, ld) = _shelves(sizes, cw, gap)
            if lw <= W + 1e-6 and ld <= D + 1e-6:
                score = (max(lw, ld), lw * ld)
                if best is None or score < best[0]:
                    best = (score, pos, (lw, ld))
        if best is None:
            pos, (lw, ld) = _shelves(sizes, W, gap)
            area = sum(w * d for w, d in sizes)
            raise TuleError(f"{len(parts)} parts do not fit on one {W:g} × {D:g} mm bed with {gap:g} mm between them "
                            f"(they need about {lw:.0f} × {ld:.0f} mm)", op="export_3mf",
                            hint=f"print them on two plates (two calls), a smaller gap=, or the printer's real bed= "
                                 f"(footprints alone cover {100 * area / (W * D):.0f} % of the bed)")
        _, pos, (lw, ld) = best
        x0, y0 = (W - lw) / 2, (D - ld) / 2
        targets = [(x0 + x, y0 + y) for x, y in pos]
    else:  # the caller's layout, moved as a whole to the middle of the bed
        lo_x = min(lo.x for lo, _ in boxes)
        lo_y = min(lo.y for lo, _ in boxes)
        lw = max(hi.x for _, hi in boxes) - lo_x
        ld = max(hi.y for _, hi in boxes) - lo_y
        if lw > W + 1e-6 or ld > D + 1e-6:
            raise TuleError(f"the parts as placed span {lw:.0f} × {ld:.0f} mm, more than the {W:g} × {D:g} mm bed",
                            op="export_3mf", hint="arrange=True lays them out")
        x0, y0 = (W - lw) / 2, (D - ld) / 2
        targets = [(x0 + lo.x - lo_x, y0 + lo.y - lo_y) for lo, _ in boxes]
    return [(name, s.at(tx - lo.x, ty - lo.y, -lo.z), st, k, n)
            for (name, s, st), (lo, _), (tx, ty), (k, n) in zip(parts, boxes, targets, which)]


def export_3mf(items, path: str, *, bed=(256, 256, 256), gap: float = 5.0, arrange: bool = True,
               settings: dict | None = None, tolerance: float = 0.02, angular: float = 0.1) -> str:
    """Write a print plate: a 3MF that Bambu Studio, OrcaSlicer and PrusaSlicer open ready to slice.

    ``items``: Shapes, ``(name, shape)`` pairs, ``(name, shape, settings)`` for one part's own
    settings, or an Assembly. Each part becomes an object of its own (named, colored), printed as
    oriented: lay it on its flat face first (``part.rotate(x=180)``); it is dropped to z = 0.
    ``arrange`` packs the footprints on the ``bed`` (width, depth[, height] in mm) with ``gap``
    between them; ``arrange=False`` keeps your x/y layout, centered. Parts that do not fit raise.

    ``settings`` go on every part, so the user picks only printer and filament:
    ``{"layer_height": 0.2, "walls": 3, "infill": 15, "supports": False, "brim": 5}`` (all
    optional; what is left out comes from the user's process preset). They show in the slicer
    as each object's own settings. Meshes are watertight, in mm, within ``tolerance`` of the solid.
    ``("pin", pin, {"copies": 4})``: four of a part, one object with four instances, arranged.
    """
    base = _print_settings(settings)
    return _write_3mf(_plate(items, bed, gap, arrange), path, base, tolerance, angular)


def _write_3mf(placed, path: str, base: dict, tolerance: float = 0.02, angular: float = 0.1) -> str:
    """The 3MF of a laid-out plate (``_plate``'s entries) with the plate's settings ``base``."""
    import zipfile
    from xml.sax.saxutils import quoteattr

    model = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<model unit="millimeter" xml:lang="en-US" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">',
             ' <metadata name="Application">Tule</metadata>',
             f' <metadata name="Title">{_xml_text(os.path.splitext(os.path.basename(path))[0])}</metadata>',
             " <resources>"]
    objects: dict[str, list] = {}  # name: its copies, (shape, settings) each
    for name, shape, own, _, _ in placed:
        objects.setdefault(name, []).append((shape, own))
    colors = [parse_color(c[0][0].color) for c in objects.values()]
    palette = sorted({color_hex(c) for c in colors if c})
    if palette:
        model.append('  <basematerials id="1">')
        model += [f'   <base name={quoteattr(h)} displaycolor="{h.upper()}FF"/>' for h in palette]
        model.append("  </basematerials>")
    build, bambu_cfg, prusa_cfg = [], ['<?xml version="1.0" encoding="UTF-8"?>', "<config>"], ['<?xml version="1.0" encoding="UTF-8"?>', "<config>"]
    for k, ((name, copies), rgb) in enumerate(zip(objects.items(), colors)):
        oid = k + 2
        shape, own = copies[0]
        lo, hi = shape.bounds
        cx, cy = (lo.x + hi.x) / 2, (lo.y + hi.y) / 2
        V, F = _closed_mesh(shape.at(-cx, -cy, 0), name, tolerance, angular)  # the object's mesh about its own middle
        mat = f' pid="1" pindex="{palette.index(color_hex(rgb))}"' if rgb else ""
        model.append(f'  <object id="{oid}" type="model" name={quoteattr(name)}{mat}>')
        model.append("   <mesh>\n    <vertices>")
        model += [f'     <vertex x="{x:.6g}" y="{y:.6g}" z="{z:.6g}"/>' for x, y, z in V.tolist()]
        model.append("    </vertices>\n    <triangles>")
        model += [f'     <triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in F.tolist()]
        model.append("    </triangles>\n   </mesh>\n  </object>")
        for c, _ in copies:  # a copy: the same object placed again (an instance in the slicers)
            lo, hi = c.bounds
            build.append(f'  <item objectid="{oid}" transform="1 0 0 0 1 0 0 0 1 {(lo.x + hi.x) / 2:.6g} {(lo.y + hi.y) / 2:.6g} 0"/>')
        bk, pk = _slicer_keys({**base, **own})
        q = quoteattr(name)
        bambu_cfg.append(f'  <object id="{oid}">')
        bambu_cfg.append(f'    <metadata key="name" value={q}/>')
        bambu_cfg += [f'    <metadata key="{key}" value="{v}"/>' for key, v in bk]
        bambu_cfg += [f'    <part id="{oid}" subtype="normal_part">', f'      <metadata key="name" value={q}/>', "    </part>", "  </object>"]
        prusa_cfg.append(f' <object id="{oid}" instances_count="{len(copies)}">')
        prusa_cfg.append(f'  <metadata type="object" key="name" value={q}/>')
        prusa_cfg += [f'  <metadata type="object" key="{key}" value="{v}"/>' for key, v in pk]
        prusa_cfg += [f'  <volume firstid="0" lastid="{len(F) - 1}">', f'   <metadata type="volume" key="name" value={q}/>',
                      '   <metadata type="volume" key="volume_type" value="ModelPart"/>', "  </volume>", " </object>"]
    model += [" </resources>", " <build>", *build, " </build>", "</model>", ""]
    files = {
        "[Content_Types].xml": '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">\n'
        ' <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>\n'
        ' <Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>\n'
        ' <Default Extension="config" ContentType="application/xml"/>\n'
        "</Types>\n",
        "_rels/.rels": '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n'
        ' <Relationship Target="/3D/3dmodel.model" Id="rel0" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>\n'
        "</Relationships>\n",
        "3D/3dmodel.model": "\n".join(model),
        "Metadata/model_settings.config": "\n".join(bambu_cfg + ["</config>", ""]),
        "Metadata/Slic3r_PE_model.config": "\n".join(prusa_cfg + ["</config>", ""]),
    }
    tmp = _atomic_path(path)
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for n, text in files.items():
            info = zipfile.ZipInfo(n, date_time=(1980, 1, 1, 0, 0, 0))  # no time of writing: same plate, same bytes
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, text.encode("utf-8"))
    os.replace(tmp, path)
    return path


# ---------------------------------------------------------------- print estimates (PrusaSlicer's CLI)

SLICE_TIMEOUT = 300  # seconds; a plate that takes longer raises rather than hang the build
SLICER_INSTALL = "apt-get update && apt-get install -y --no-install-recommends prusa-slicer"


def _prusa_slicer() -> str:
    """The PrusaSlicer executable, or a TuleError that says how to install it."""
    import shutil

    for exe in ("prusa-slicer", "PrusaSlicer", "/Applications/PrusaSlicer.app/Contents/MacOS/PrusaSlicer"):
        found = shutil.which(exe)
        if found:
            return found
    raise TuleError("the print estimate needs PrusaSlicer's command line (prusa-slicer), which is not on this machine",
                    op="estimate", hint=f"install it once (about 1 GB, a few minutes): {SLICER_INSTALL}; "
                                        "or leave estimate=False: the 3MF does not need it")


def _slicer_config(settings: dict, bed) -> dict:
    """A generic printer for the estimate: 0.4 mm nozzle, 1.75 mm filament, moderate speeds and
    accelerations (an i3-class printer at its quality preset; a fast CoreXY one prints sooner)."""
    material = settings.get("material", "PLA")
    density, nozzle_t, bed_t = MATERIALS[material]
    W, D = (float(b) for b in tuple(bed)[:2])
    infill = settings.get("infill", 15)
    walls = settings.get("walls", 2)
    return {
        "nozzle_diameter": 0.4, "filament_diameter": 1.75, "filament_type": material, "filament_density": density,
        "temperature": nozzle_t, "first_layer_temperature": nozzle_t, "bed_temperature": bed_t, "first_layer_bed_temperature": bed_t,
        "bed_shape": f"0x0,{W:g}x0,{W:g}x{D:g},0x{D:g}", "max_print_height": float(tuple(bed)[2]) if len(tuple(bed)) > 2 else 256,
        "gcode_flavor": "marlin2", "machine_limits_usage": "time_estimate_only",
        "layer_height": settings.get("layer_height", 0.2), "first_layer_height": 0.2,
        "perimeters": walls, "top_solid_layers": 5, "bottom_solid_layers": 4,
        "top_solid_min_thickness": 0.7, "bottom_solid_min_thickness": 0.5,
        "fill_density": f"{infill:g}%", "fill_pattern": "gyroid" if infill < 100 else "rectilinear",
        "support_material": int(settings.get("supports", False)), "support_material_auto": 1,
        "brim_width": settings.get("brim", 0), "skirts": 1, "skirt_distance": 3,
        "perimeter_speed": 60, "small_perimeter_speed": 40, "external_perimeter_speed": 40, "infill_speed": 120,
        "solid_infill_speed": 100, "top_solid_infill_speed": 60, "gap_fill_speed": 40, "bridge_speed": 30,
        "support_material_speed": 60, "travel_speed": 200, "first_layer_speed": 25, "max_volumetric_speed": 12,
        "default_acceleration": 1500, "perimeter_acceleration": 1000, "external_perimeter_acceleration": 800,
        "infill_acceleration": 2000, "solid_infill_acceleration": 1500, "top_solid_infill_acceleration": 800,
        "bridge_acceleration": 1000, "first_layer_acceleration": 600, "travel_acceleration": 2500,
        "machine_max_acceleration_x": 3000, "machine_max_acceleration_y": 3000, "machine_max_acceleration_extruding": 2500,
        "machine_max_acceleration_travel": 3000, "machine_max_acceleration_retracting": 1500,
    }


def _parse_gcode_estimate(text: str, density: float = 1.24) -> dict:
    """PrusaSlicer's estimate from its G-code comments: print time in s, filament in g, m and cm³
    (several extruders summed)."""
    import re

    vals: dict[str, str] = {}
    for line in text.splitlines():
        if line.startswith(";") and "=" in line:
            k, _, v = line[1:].partition("=")
            vals.setdefault(k.strip().lower(), v.strip())

    def total(key):
        if key not in vals:
            return None
        try:
            return sum(float(x) for x in vals[key].split(",") if x.strip())
        except ValueError:
            return None

    when = next((vals[k] for k in ("estimated printing time (normal mode)", "estimated printing time (silent mode)") if k in vals), None)
    if when is None:
        raise TuleError("the slicer's G-code has no print time estimate", op="estimate",
                        hint="expected a '; estimated printing time (normal mode) = 1h 2m 3s' comment")
    secs = sum(float(n) * {"d": 86400, "h": 3600, "m": 60, "s": 1}[u] for n, u in re.findall(r"(\d+(?:\.\d+)?)\s*([dhms])", when))
    mm, cm3 = total("filament used [mm]"), total("filament used [cm3]")
    g = total("total filament used [g]") or total("filament used [g]") or (cm3 * density if cm3 else None)
    out = {"print_time_s": round(secs)}
    if g is not None:
        out["filament_g"] = round(g, 2)
    if mm is not None:
        out["filament_m"] = round(mm / 1000, 3)
    if cm3 is not None:
        out["filament_cm3"] = round(cm3, 2)
    return out


def _duration(secs: float) -> str:
    """3 h 12 min, 14 min, under 1 min."""
    m = round(secs / 60)
    if m < 1:
        return "under 1 min"
    return f"{m // 60} h {m % 60} min" if m >= 60 else f"{m} min"


def _slice_estimate(path: str, settings: dict, bed, timeout: float | None = None) -> dict:
    """Slices the 3MF at ``path`` with PrusaSlicer on a generic profile (``_slicer_config``) and
    reads its estimate: ``{"print_time_s", "filament_g", "filament_m", "filament_cm3", "profile"}``."""
    import re
    import subprocess
    import tempfile

    exe = _prusa_slicer()
    timeout = timeout or SLICE_TIMEOUT
    cfg = _slicer_config(settings, bed)
    with tempfile.TemporaryDirectory(prefix="tule-slice-") as tmp:
        ini = os.path.join(tmp, "generic.ini")
        with open(ini, "w") as f:
            f.writelines(f"{k} = {v}\n" for k, v in cfg.items())
        gcode = os.path.join(tmp, "plate.gcode")
        cmd = [exe, "--export-gcode", "--dont-arrange", "--datadir", os.path.join(tmp, "data"), "--load", ini,
               "--loglevel", "1", "--output", gcode, os.path.abspath(path)]
        try:
            run = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=tmp, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            raise TuleError(f"slicing the plate took more than {timeout:g} s; stopped", op="estimate",
                            hint="many or finely detailed parts slice slowly: estimate=False, or fewer parts per plate") from None
        if run.returncode != 0 or not os.path.exists(gcode):
            lines = [ln.strip() for ln in (run.stderr + "\n" + run.stdout).splitlines()]
            said = [ln for ln in lines if ln and not re.match(r"^\d+ => ", ln)][-6:]
            raise TuleError("PrusaSlicer could not slice the plate: " + (" | ".join(said) or f"exit status {run.returncode}"),
                            op="estimate", hint="the 3MF is written; the slicer's message says which part or setting it refused")
        with open(gcode, "rb") as f:  # the estimate is in the last comments, before the config block
            f.seek(max(0, os.path.getsize(gcode) - (1 << 20)))
            text = f.read().decode("utf-8", "replace")
    out = _parse_gcode_estimate(text, cfg["filament_density"])
    out["profile"] = f"generic 0.4 mm nozzle, {cfg['filament_type']} ({cfg['filament_density']:g} g/cm³), PrusaSlicer"
    return out


def _xml_text(s: str) -> str:
    from xml.sax.saxutils import escape

    return escape(s)
