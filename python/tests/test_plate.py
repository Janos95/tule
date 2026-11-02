"""3MF print plates: export_3mf and outputs.plate."""

import json
import zipfile
import xml.etree.ElementTree as ET

import numpy as np
import pytest

from tule import *

CORE = "{http://schemas.microsoft.com/3dmanufacturing/core/2015/02}"


@pytest.fixture
def outdir(tmp_path, monkeypatch):
    d = tmp_path / "outputs"
    monkeypatch.setenv("TULE_OUTPUTS", str(d))
    return d


def parts():
    plate = rect(80, 40, center=True).round(5).extrude(6)
    boss = circle(9).extrude(12).at(0, 0, 6)
    bracket = ((plate | boss).fillet(boss.edges["bottom"], 2) - cylinder(3.2, 50, center=True)).named("bracket", "#4a8")
    lid = box(60, 60, 4).at(500, 0, -30).named("lid", "#d33")  # far away and below the bed: laid out anyway
    pin = cylinder(4, 30).named("pin")
    return [bracket, lid, ("pin", pin), ("pin", pin, {"supports": True})]


def objects(path):
    """(name, vertices, triangles, translation) of each build item, read with ElementTree."""
    z = zipfile.ZipFile(path)
    root = ET.fromstring(z.read("3D/3dmodel.model"))
    objs = {o.get("id"): o for o in root.iter(CORE + "object")}
    out = []
    for item in root.iter(CORE + "item"):
        o = objs[item.get("objectid")]
        V = np.array([[float(v.get(a)) for a in "xyz"] for v in o.iter(CORE + "vertex")])
        F = np.array([[int(t.get(a)) for a in ("v1", "v2", "v3")] for t in o.iter(CORE + "triangle")])
        T = np.array([float(x) for x in item.get("transform").split()]).reshape(4, 3)
        out.append((o.get("name"), V @ T[:3] + T[3], F, T[3]))
    return out


def footprints_apart(boxes, gap):
    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            (a0, a1), (b0, b1) = boxes[i], boxes[j]
            if not (a1[0] + gap - 1e-3 <= b0[0] or b1[0] + gap - 1e-3 <= a0[0] or a1[1] + gap - 1e-3 <= b0[1] or b1[1] + gap - 1e-3 <= a0[1]):
                return False
    return True


def test_3mf_package_is_well_formed(tmp_path):
    path = export_3mf(parts(), str(tmp_path / "plate.3mf"))
    z = zipfile.ZipFile(path)
    assert set(z.namelist()) == {"[Content_Types].xml", "_rels/.rels", "3D/3dmodel.model",
                                 "Metadata/model_settings.config", "Metadata/Slic3r_PE_model.config"}
    for n in z.namelist():
        ET.fromstring(z.read(n))  # every part is well-formed XML
    types = ET.fromstring(z.read("[Content_Types].xml"))
    defaults = {d.get("Extension"): d.get("ContentType") for d in types}
    assert defaults["rels"] == "application/vnd.openxmlformats-package.relationships+xml"
    assert defaults["model"] == "application/vnd.ms-package.3dmanufacturing-3dmodel+xml"
    rel = ET.fromstring(z.read("_rels/.rels"))[0]
    assert rel.get("Target") == "/3D/3dmodel.model"
    assert rel.get("Type") == "http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"
    model = ET.fromstring(z.read("3D/3dmodel.model"))
    assert model.get("unit") == "millimeter"
    colors = {b.get("displaycolor") for b in model.iter(CORE + "base")}
    assert colors == {"#44AA88FF", "#DD3333FF"}
    objs = objects(path)
    assert [o[0] for o in objs] == ["bracket", "lid", "pin", "pin-2"]
    for name, V, F, _ in objs:
        # closed and consistently oriented: every directed edge has its reverse, once
        e = np.concatenate([F[:, [0, 1]], F[:, [1, 2]], F[:, [2, 0]]])
        fwd = {tuple(x) for x in e.tolist()}
        assert len(fwd) == len(e) and all((b, a) in fwd for a, b in fwd), name
        tri = V[F]
        vol = np.einsum("ij,ij->i", tri[:, 0], np.cross(tri[:, 1], tri[:, 2])).sum() / 6
        assert vol > 0, name
        assert V[:, 2].min() == pytest.approx(0, abs=1e-6)  # on the bed
    vols = [np.einsum("ij,ij->i", V[F][:, 0], np.cross(V[F][:, 1], V[F][:, 2])).sum() / 6 for _, V, F, _ in objs]
    assert vols[1] == pytest.approx(60 * 60 * 4, rel=1e-6)  # mm, not m or inches
    assert vols[2] == pytest.approx(np.pi * 16 * 30, rel=0.01)
    # same plate, same bytes (tule build, git)
    (tmp_path / "again").mkdir()
    again = export_3mf(parts(), str(tmp_path / "again" / "plate.3mf"))
    assert open(path, "rb").read() == open(again, "rb").read()


def test_3mf_reads_back_with_trimesh(tmp_path):
    trimesh = pytest.importorskip("trimesh")
    pytest.importorskip("lxml")
    pytest.importorskip("networkx")
    bed = (180, 180)
    path = export_3mf(parts(), str(tmp_path / "plate.3mf"), bed=bed, gap=4)
    scene = trimesh.load(path)
    names = [scene.graph[n][1] for n in scene.graph.nodes_geometry]
    assert sorted(names) == ["bracket", "lid", "pin", "pin-2"]
    boxes = []
    for node in scene.graph.nodes_geometry:
        T, g = scene.graph[node]
        mesh = scene.geometry[g].copy()
        mesh.apply_transform(T)
        assert mesh.is_watertight and mesh.is_winding_consistent and mesh.volume > 0, g
        lo, hi = mesh.bounds
        assert (lo >= -1e-6).all() and hi[0] <= bed[0] + 1e-6 and hi[1] <= bed[1] + 1e-6, g
        assert lo[2] == pytest.approx(0, abs=1e-6)
        boxes.append((lo, hi))
    assert footprints_apart(boxes, 4)


def test_3mf_settings_land_in_the_slicer_configs(tmp_path):
    path = export_3mf(parts(), str(tmp_path / "plate.3mf"),
                      settings={"layer_height": 0.16, "walls": 4, "infill": "25%", "supports": False, "brim": 3})
    z = zipfile.ZipFile(path)
    tris = {o[0]: len(o[2]) for o in objects(path)}
    # Bambu Studio / OrcaSlicer: Metadata/model_settings.config, one <object> per 3MF object id
    bambu = ET.fromstring(z.read("Metadata/model_settings.config"))
    model = ET.fromstring(z.read("3D/3dmodel.model"))
    ids = {o.get("name"): o.get("id") for o in model.iter(CORE + "object")}
    got = {}
    for o in bambu.iter("object"):
        meta = {m.get("key"): m.get("value") for m in o.findall("metadata")}
        assert ids[meta["name"]] == o.get("id")
        assert o.find("part").get("id") == o.get("id") and o.find("part").get("subtype") == "normal_part"
        got[meta.pop("name")] = meta
    assert got["bracket"] == {"layer_height": "0.16", "wall_loops": "4", "sparse_infill_density": "25%",
                              "enable_support": "0", "brim_type": "outer_only", "brim_width": "3"}
    assert got["pin-2"]["enable_support"] == "1" and got["pin"]["enable_support"] == "0"  # a part's own settings
    # PrusaSlicer: Metadata/Slic3r_PE_model.config, with each object's one volume over all its triangles
    prusa = ET.fromstring(z.read("Metadata/Slic3r_PE_model.config"))
    for o in prusa.iter("object"):
        meta = {m.get("key"): m.get("value") for m in o.findall("metadata")}
        vol = o.find("volume")
        assert vol.get("firstid") == "0" and int(vol.get("lastid")) == tris[meta["name"]] - 1
        assert meta["perimeters"] == "4" and meta["fill_density"] == "25%" and meta["layer_height"] == "0.16"
        assert meta["brim_width"] == "3" and meta["support_material"] == ("1" if meta["name"] == "pin-2" else "0")
    # nothing set: no settings, the user's preset decides
    plain = zipfile.ZipFile(export_3mf([box(10)], str(tmp_path / "plain.3mf")))
    keys = {m.get("key") for m in ET.fromstring(plain.read("Metadata/model_settings.config")).iter("metadata")}
    assert keys == {"name"}


def test_3mf_errors(tmp_path):
    with pytest.raises(TuleError, match="does not fit the bed") as e:
        export_3mf([box(200, 20, 5)], str(tmp_path / "a.3mf"), bed=(180, 220))
    assert "rotate(z=90)" in e.value.hint
    with pytest.raises(TuleError, match="parts do not fit") as e:
        export_3mf([box(100, 100, 5)] * 5, str(tmp_path / "b.3mf"), bed=(220, 220))
    assert "two plates" in e.value.hint
    with pytest.raises(TuleError, match="unknown print setting 'perimeters'") as e:
        export_3mf([box(10)], str(tmp_path / "c.3mf"), settings={"perimeters": 3})
    assert "walls" in e.value.hint
    with pytest.raises(TuleError, match="infill"):
        export_3mf([box(10)], str(tmp_path / "d.3mf"), settings={"infill": 150})
    with pytest.raises(TuleError, match="10.0 × 10.0 × 300.0 mm.*does not fit") as e:
        export_3mf([box(10, 10, 300)], str(tmp_path / "e.3mf"))
    assert "lay it on another face" in e.value.hint


def test_3mf_keeps_a_given_layout(tmp_path):
    a, b = box(20, 20, 5).named("a"), box(10, 10, 8).at(40, 5, 3).named("b")
    objs = objects(export_3mf([a, b], str(tmp_path / "p.3mf"), arrange=False, bed=(100, 100)))
    (_, Va, _, _), (_, Vb, _, _) = objs
    assert Vb.min(0)[:2] - Va.min(0)[:2] == pytest.approx([40, 5])  # as placed, the whole moved to the middle
    both = np.concatenate([Va, Vb])
    assert (both.min(0)[:2] + both.max(0)[:2]) / 2 == pytest.approx([50, 50])
    assert Vb[:, 2].min() == pytest.approx(0, abs=1e-6)


def test_outputs_plate(outdir):
    folder = outputs.plate("print", parts(), settings={"layer_height": 0.2, "walls": 3, "infill": 15, "supports": False})
    meta = json.load(open(f"{folder}/output.json"))
    assert meta["kind"] == "assembly"
    assert [p["file"] for p in meta["parts"]] == ["bracket.step", "lid.step", "pin.step", "pin-2.step"]
    assert meta["info"] == {"bed": "256 × 256 mm", "layer height": "0.2 mm", "walls": 3, "infill": "15 %",
                            "supports": False, "own settings": "pin-2"}
    # the viewer's parts sit where the 3MF puts them
    step = {p["file"][:-5]: import_step(f"{folder}/{p['file']}").bounds for p in meta["parts"]}
    for name, V, _, _ in objects(f"{folder}/print.3mf"):
        lo, hi = step[name]
        assert [lo.x, lo.y, lo.z, hi.x, hi.y, hi.z] == pytest.approx([*V.min(0), *V.max(0)], abs=0.05)
