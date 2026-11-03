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


def test_copies_are_instances_of_one_object(tmp_path):
    pin = cylinder(4, 30).named("pin")
    path = export_3mf([box(20).named("cube"), ("pin", pin, {"copies": 4, "supports": True})], str(tmp_path / "p.3mf"), gap=3)
    z = zipfile.ZipFile(path)
    model = ET.fromstring(z.read("3D/3dmodel.model"))
    assert [o.get("name") for o in model.iter(CORE + "object")] == ["cube", "pin"]  # one mesh per part
    objs = objects(path)
    assert [o[0] for o in objs] == ["cube", "pin", "pin", "pin", "pin"]  # a build item per copy
    boxes = [(V.min(0), V.max(0)) for _, V, _, _ in objs]
    assert footprints_apart(boxes, 3)
    prusa = {o.find("metadata").get("value"): o for o in ET.fromstring(z.read("Metadata/Slic3r_PE_model.config")).iter("object")}
    assert prusa["pin"].get("instances_count") == "4" and prusa["cube"].get("instances_count") == "1"
    assert {m.get("key"): m.get("value") for m in prusa["pin"].findall("metadata")}["support_material"] == "1"
    bambu = [{m.get("key"): m.get("value") for m in o.findall("metadata")} for o in ET.fromstring(z.read("Metadata/model_settings.config")).iter("object")]
    assert "copies" not in str(bambu)
    with pytest.raises(TuleError, match="copies are per part"):
        export_3mf([pin], str(tmp_path / "a.3mf"), settings={"copies": 2})
    with pytest.raises(TuleError, match="material is the whole plate's"):
        export_3mf([("pin", pin, {"material": "PETG"})], str(tmp_path / "b.3mf"))
    with pytest.raises(TuleError, match="copies need arrange=True"):
        export_3mf([("pin", pin, {"copies": 2})], str(tmp_path / "c.3mf"), arrange=False)
    with pytest.raises(TuleError, match="copies must be a whole number"):
        export_3mf([("pin", pin, {"copies": 0})], str(tmp_path / "d.3mf"))
    with pytest.raises(TuleError, match="unknown material") as e:
        export_3mf([pin], str(tmp_path / "e.3mf"), settings={"material": "wood"})
    assert "PETG" in e.value.hint


def test_full_infill_gets_a_pattern_that_allows_it(tmp_path):
    z = zipfile.ZipFile(export_3mf([("solid", box(10), {"infill": 100}), box(10).named("light")], str(tmp_path / "p.3mf")))
    bambu = {o.find("metadata").get("value"): {m.get("key"): m.get("value") for m in o.findall("metadata")}
             for o in ET.fromstring(z.read("Metadata/model_settings.config")).iter("object")}
    prusa = {o.find("metadata").get("value"): {m.get("key"): m.get("value") for m in o.findall("metadata")}
             for o in ET.fromstring(z.read("Metadata/Slic3r_PE_model.config")).iter("object")}
    assert bambu["solid"]["sparse_infill_pattern"] == "zig-zag" and prusa["solid"]["fill_pattern"] == "rectilinear"
    assert "sparse_infill_pattern" not in bambu["light"] and "fill_pattern" not in prusa["light"]


def test_outputs_plate_copies(outdir):
    folder = outputs.plate("print", [box(20).named("cube"), ("pin", cylinder(4, 30).named("pin", "#888"), {"copies": 3})],
                           settings={"material": "PETG"})
    meta = json.load(open(f"{folder}/output.json"))
    assert [p["name"] for p in meta["parts"]] == ["print/cube", "print/pin ×3/1", "print/pin ×3/2", "print/pin ×3/3"]
    assert {p["file"] for p in meta["parts"]} == {"cube.step", "pin.step"}  # a copy is its file placed again
    assert sorted(f.name for f in (outdir / "print").iterdir()) == ["cube.step", "output.json", "pin.step", "print.3mf"]
    assert meta["plate"] == {"file": "print.3mf", "parts": {"cube": 1, "pin": 3}}
    assert meta["info"]["material"] == "PETG"
    # each copy where the 3MF puts it
    lo = import_step(f"{folder}/pin.step").bounds[0]
    got = sorted((round(lo.x + (p.get("transform") or [0] * 8)[3], 3), round(lo.y + (p.get("transform") or [0] * 8)[7], 3))
                 for p in meta["parts"] if p["file"] == "pin.step")
    want = sorted((round(V[:, 0].min(), 3), round(V[:, 1].min(), 3)) for n, V, _, _ in objects(f"{folder}/print.3mf") if n == "pin")
    assert np.allclose(got, want, atol=0.05)


GCODE_TAIL = """G1 X10 Y10 E0.5
M84
; filament used [mm] = 1493.99, 12.01
; filament used [cm3] = 3.59, 0.03
; filament used [g] = 4.45, 0.04
; filament cost = 0.00
; total filament used [g] = 4.49
; total filament cost = 0.00
; estimated printing time (normal mode) = 1d 3h 13m 55s
; estimated first layer printing time (normal mode) = 29s

; prusaslicer_config = begin
; filament_density = 1.24
; layer_height = 0.2
; prusaslicer_config = end
"""


def test_gcode_estimate_parsing():
    from tule.io import _duration, _parse_gcode_estimate

    est = _parse_gcode_estimate(GCODE_TAIL)
    assert est == {"print_time_s": 86400 + 3 * 3600 + 13 * 60 + 55, "filament_g": 4.49, "filament_m": 1.506, "filament_cm3": 3.62}
    # no grams (density 0): from the volume
    est = _parse_gcode_estimate("; filament used [mm] = 1000\n; filament used [cm3] = 2.0\n; total filament used [g] = 0.00\n"
                                "; estimated printing time (normal mode) = 13m 55s\n", density=1.27)
    assert est["filament_g"] == pytest.approx(2.54) and est["print_time_s"] == 835
    with pytest.raises(TuleError, match="no print time"):
        _parse_gcode_estimate("G1 X0\n")
    assert [_duration(s) for s in (20, 835, 3600, 11520, 97235)] == ["under 1 min", "14 min", "1 h 0 min", "3 h 12 min", "27 h 1 min"]


def fake_slicer(tmp_path, monkeypatch, body):
    """A stand-in ``prusa-slicer`` on PATH: ``body`` is Python with ``out`` (the --output path) and ``args``."""
    import sys

    bin_ = tmp_path / "bin"
    bin_.mkdir()
    exe = bin_ / "prusa-slicer"
    exe.write_text(f"#!{sys.executable}\nimport sys, time\nargs = sys.argv[1:]\nout = args[args.index('--output') + 1]\n{body}\n")
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_))


def test_outputs_plate_estimate(outdir, tmp_path, monkeypatch):
    seen = tmp_path / "seen.txt"
    fake_slicer(tmp_path, monkeypatch, f"open({str(seen)!r}, 'w').write(' '.join(args) + open(args[args.index('--load') + 1]).read())\n"
                                       f"open(out, 'w').write({GCODE_TAIL!r})")
    folder = outputs.plate("print", [box(20)], settings={"infill": 20, "material": "PETG"}, estimate=True)
    meta = json.load(open(f"{folder}/output.json"))
    est = meta["plate"]["estimate"]
    assert est["print_time_s"] == 98035 and est["filament_g"] == 4.49 and "PETG" in est["profile"]
    assert meta["info"]["print time"] == "27 h 14 min" and meta["info"]["filament"] == "4.5 g (1.51 m)"
    used = seen.read_text()  # the plate's 3MF on a generic profile with the plate's settings
    assert "--export-gcode" in used and "print.3mf" in used
    for line in ("nozzle_diameter = 0.4", "filament_density = 1.27", "fill_density = 20%", "fill_pattern = gyroid", "perimeters = 2"):
        assert line in used
    assert meta["plate"]["printer"] == "generic" and meta["info"]["estimate"] == "generic 0.4 mm printer, PrusaSlicer"


def test_outputs_plate_printer(outdir, tmp_path, monkeypatch):
    # a stand-in slicer whose print time follows the loaded profile's infill speed and acceleration
    fake_slicer(tmp_path, monkeypatch, "cfg = dict(l.split(' = ', 1) for l in open(args[args.index('--load') + 1]).read().splitlines())\n"
                                       "s = 3e5 / float(cfg['infill_speed']) + 3e6 / float(cfg['infill_acceleration'])\n"
                                       "open(out, 'w').write(f'; estimated printing time (normal mode) = {s:.0f}s\\n')")
    times = {}
    for printer in (True, "bambu-p1s", "Ender-3", "voron-2.4"):
        meta = json.load(open(f"{outputs.plate('print', [box(20)], estimate=printer)}/output.json"))
        times[meta["plate"]["printer"]] = meta["plate"]["estimate"]["print_time_s"]
    assert times["bambu-p1s"] < times["voron-2.4"] < times["generic"] < times["ender-3"]
    assert meta["plate"]["estimate"]["profile"].startswith("Voron 2.4 350, 0.4 mm nozzle, PLA")
    assert meta["info"]["estimate"] == "Voron 2.4 350, PrusaSlicer" and meta["info"]["bed"] == "350 × 350 mm"
    with pytest.raises(TuleError, match="unknown printer 'bambu-p2x'") as e:
        outputs.plate("other", [box(20)], estimate="bambu-p2x")
    assert "bambu-p1s" in e.value.hint and "ender-3" in e.value.hint and not (outdir / "other").exists()


def test_outputs_plate_printer_bed(outdir, tmp_path, monkeypatch):
    fake_slicer(tmp_path, monkeypatch, f"open(out, 'w').write({GCODE_TAIL!r})")
    long = box(240, 20, 5).named("rail")
    folder = outputs.plate("print", [long], estimate="bambu-p1s")  # 256 mm bed: fits
    assert json.load(open(f"{folder}/output.json"))["info"]["bed"] == "256 × 256 mm"
    with pytest.raises(TuleError, match=r"part 'rail' \(240.0 × 20.0 × 5.0 mm\) does not fit the Creality Ender-3's bed \(220 × 220 × 250 mm\)") as e:
        outputs.plate("print", [long], estimate="ender-3")
    assert "larger bed" in e.value.hint
    with pytest.raises(TuleError, match=r"5 parts do not fit on the Prusa MK4 / MK4S's bed \(250 × 210 mm\).*need about"):
        outputs.plate("print", [("tile", box(100, 100, 5), {"copies": 5})], estimate="prusa-mk4")
    outputs.plate("print", [long], bed=(250, 250), estimate="ender-3")  # the user's own bed= wins


def test_outputs_plate_estimate_errors(outdir, tmp_path, monkeypatch):
    import shutil

    real_which = shutil.which
    monkeypatch.setattr(shutil, "which", lambda exe, *a, **k: None if "rusa" in exe else real_which(exe, *a, **k))
    with pytest.raises(TuleError, match="needs PrusaSlicer") as e:
        outputs.plate("print", [box(20)], estimate=True)
    assert "apt-get install -y --no-install-recommends prusa-slicer" in e.value.hint
    assert not (outdir / "print").exists()  # nothing written
    monkeypatch.undo()
    monkeypatch.setenv("TULE_OUTPUTS", str(outdir))
    fake_slicer(tmp_path, monkeypatch, "print('10 => Processing triangulated mesh')\nprint('Objects could not fit on the bed', file=sys.stderr)\nsys.exit(1)")
    with pytest.raises(TuleError, match="could not slice the plate: Objects could not fit on the bed;"):
        outputs.plate("print", [box(20)], estimate=True)
    (tmp_path / "bin" / "prusa-slicer").write_text(f"#!{__import__('sys').executable}\nimport time\ntime.sleep(30)\n")
    monkeypatch.setattr("tule.io.SLICE_TIMEOUT", 1)
    with pytest.raises(TuleError, match="took more than 1 s"):
        outputs.plate("print", [box(20)], estimate=True)


@pytest.mark.skipif(not __import__("shutil").which("prusa-slicer"), reason="PrusaSlicer's CLI is not installed")
def test_outputs_plate_estimate_with_prusa_slicer(outdir):
    folder = outputs.plate("print", [box(20).named("cube"), ("pin", cylinder(4, 30), {"copies": 2})], estimate=True)
    est = json.load(open(f"{folder}/output.json"))["plate"]["estimate"]
    assert 300 < est["print_time_s"] < 7200 and 3 < est["filament_g"] < 30 and est["filament_m"] > 1
    # a 50 mm box, 2 walls, 15 % infill: Bambu Studio says 19 min on a P1S, 84 min on an Ender-3 V2
    s = {"walls": 2, "infill": 15}
    t = {p: json.load(open(f"{outputs.plate(p, [box(50, 50, 20)], settings=s, estimate=p)}/output.json"))["plate"]["estimate"]["print_time_s"]
         for p in ("bambu-p1s", "ender-3")}
    assert 15 * 60 < t["bambu-p1s"] < 25 * 60 and 70 * 60 < t["ender-3"] < 100 * 60


def test_material_slows_the_flow():
    from tule.io import PRINTERS, _slicer_config

    pla = _slicer_config({"material": "PLA"}, (250, 210), PRINTERS["prusa-mk4"])
    petg = _slicer_config({"material": "PETG"}, (250, 210), PRINTERS["prusa-mk4"])
    assert pla["max_volumetric_speed"] == 15 and 9 <= petg["max_volumetric_speed"] <= 10.5


def test_self_touching_part_says_where(tmp_path):
    part = box(10, 10, 10) - cylinder(2, 30, center=True).translate(8, 5, 0)  # the hole just touches x=10
    with pytest.raises(TuleError, match=r"touches itself .*near \(10, 5, 10\)"):
        export_3mf([("lug", part)], str(tmp_path / "p.3mf"))


def test_loose_edges_weld_at_the_solids_tolerance(monkeypatch):
    import tule.io as io

    real = io.tessellate

    def drifted(shape, tolerance, angular):  # each face's copy of an edge point off by up to 8 µm, as on a loose sweep
        m = real(shape, tolerance, angular)
        rng = np.random.default_rng(0)
        m.positions = (m.positions + rng.uniform(-8e-3, 8e-3, m.positions.shape)).astype(np.float32)
        return m

    monkeypatch.setattr(io, "tessellate", drifted)
    part = box(10, 10, 10)
    monkeypatch.setattr(io, "_max_tolerance", lambda s: 1e-7)
    with pytest.raises(TuleError, match="open edges, near"):
        io._closed_mesh(part, "p", 0.05, 0.3)
    monkeypatch.setattr(io, "_max_tolerance", lambda s: 0.02)
    V, F = io._closed_mesh(part, "p", 0.05, 0.3)
    assert len(V) == 8 and len(F) == 12


def test_brim_and_supports_widen_the_gap():
    from tule.io import _plate

    parts = [box(20, 20, 5).named("a"), box(20, 20, 5).named("b")]
    def spread(**base):
        placed = _plate(parts, gap=5, base=base)
        (la, ha), (lb, hb) = (p[1].bounds for p in placed)
        return max(lb.x - ha.x, la.x - hb.x, lb.y - ha.y, la.y - hb.y)
    assert abs(spread() - 5) < 1e-6
    assert spread(brim=4) >= 2 * 4 + 3 - 1e-6
    assert spread(brim=4, supports=True) >= 2 * 4 + 6 - 1e-6
