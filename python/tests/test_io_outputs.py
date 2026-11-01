import json
import os
import struct

import numpy as np
import pytest

from tule import *


@pytest.fixture
def outdir(tmp_path, monkeypatch):
    d = tmp_path / "outputs"
    monkeypatch.setenv("TULE_OUTPUTS", str(d))
    return d


def tagged_bracket():
    plate = rect(80, 40, center=True).round(5).extrude(6)
    plate = plate.tag("mount", plate.faces["bottom"])
    boss = circle(9).extrude(12).at(0, 0, 6)
    part = (plate | boss).fillet(boss.edges["bottom"], 2) - cylinder(3.2, 50, center=True)
    return part.tag("load", part.faces.top()).named("bracket", "#4a8")


def test_step_roundtrip_keeps_name_color_tags(tmp_path):
    part = tagged_bracket()
    path = str(tmp_path / "b.step")
    part.export_step(path)
    back = import_step(path)
    assert back.name == "bracket"
    assert back.color == "#44aa88"
    assert back.tags == ["load", "mount"]
    assert back.volume == pytest.approx(part.volume)
    assert back.faces["load"].info()[0]["center"][2] == pytest.approx(18)


def test_assembly_step_roundtrip(tmp_path):
    a = box(20).named("plate", "#4a8")
    a = a.tag("load", a.faces["top"])
    b = box(10).named("block", "#d33").at(50, 0, 0).rotate(z=30)
    asm = Assembly(a, b, name="demo")
    path = str(tmp_path / "a.step")
    asm.export_step(path)
    back = import_step(path)
    parts = dict(back.parts())
    assert set(parts) == {"demo/plate", "demo/block"}
    assert parts["demo/block"].color == "#dd3333"
    assert parts["demo/plate"].tags == ["load"]
    assert parts["demo/block"].center_of_mass.x == pytest.approx(b.center_of_mass.x)


def test_tessellate_ids_and_normals():
    part = tagged_bracket()
    m = part.tessellate()
    assert m.triangles > 0
    assert set(m.face_ids.tolist()) == set(range(len(part.faces)))
    assert np.allclose(np.linalg.norm(m.normals, axis=1), 1, atol=1e-3)
    assert len(m.edges) == len(part.edges)
    # outward winding: the signed volume of the mesh matches the solid
    p = m.positions[m.indices]
    vol = np.einsum("ij,ij->i", p[:, 0], np.cross(p[:, 1], p[:, 2])).sum() / 6
    assert vol == pytest.approx(part.volume, rel=0.02)


def test_stl_and_gltf(tmp_path):
    b = box(10)
    stl = b.export_stl(str(tmp_path / "b.stl"))
    data = open(stl, "rb").read()
    n = struct.unpack("<I", data[80:84])[0]
    assert len(data) == 84 + 50 * n and n >= 12
    glb = export_gltf(Assembly(b.named("a"), box(1).named("b")), str(tmp_path / "x.glb"))
    d = open(glb, "rb").read()
    assert d[:4] == b"glTF"
    js = json.loads(d[20 : 20 + struct.unpack("<I", d[12:16])[0]])
    assert [m["name"] for m in js["meshes"]] == ["a", "b"]


def test_outputs_part_assembly_sequence(outdir):
    part = tagged_bracket()
    folder = outputs.part("bracket", part)
    meta = json.load(open(os.path.join(folder, "output.json")))
    assert meta == {"version": 1, "kind": "part", "file": "bracket.step", "name": "bracket", "color": "#4a8"}

    asm = Assembly(box(1).named("a", "#f00"), box(1).named("b").at(3, 0, 0), name="pair")
    folder = outputs.assembly("pair", asm)
    meta = json.load(open(os.path.join(folder, "output.json")))
    assert meta["kind"] == "assembly"
    assert [p["file"] for p in meta["parts"]] == ["a.step", "b.step"]

    seq = outputs.sequence("grow", "Growing")
    seq.add(box(1), label="one", volume=1)
    meta = json.load(open(outdir / "grow" / "output.json"))
    assert meta["status"] == "running" and len(meta["frames"]) == 1
    seq.add(mesh=([(0, 0, 0), (1, 0, 0), (0, 1, 0), (0, 0, 1)], [(0, 2, 1), (0, 1, 3), (0, 3, 2), (1, 2, 3)]))
    seq.done()
    meta = json.load(open(outdir / "grow" / "output.json"))
    assert meta["status"] == "done"
    assert [f["file"] for f in meta["frames"]] == ["frame_0000.stl", "frame_0001.stl"]
    assert meta["frames"][0]["values"] == {"volume": 1.0}
    assert not [f for f in os.listdir(outdir / "grow") if f.startswith(".")]  # no temp files left


def test_sequence_context_marks_failure(outdir):
    with pytest.raises(RuntimeError):
        with outputs.sequence("broken") as s:
            s.add(box(1))
            raise RuntimeError("boom")
    assert json.load(open(outdir / "broken" / "output.json"))["status"] == "failed"


def test_overlay(outdir):
    part = tagged_bracket()
    load = part.faces["load"].ids[0]
    outputs.overlay("loads", part, title="Load case 1",
                    faces={part.faces["load"]: {"role": "load", "label": "500 N"}, 1: "fillet",
                           part.faces["mount"]: {"role": "fixed"}},
                    arrows=[{"at": (0, 5, 18), "dir": (0, 0, -1), "label": "500 N"}])
    meta = json.load(open(outdir / "loads" / "output.json"))
    assert meta["kind"] == "overlay" and meta["file"] == "bracket.step" and meta["title"] == "Load case 1"
    assert meta["faces"][str(load)] == {"role": "load", "label": "500 N"}
    assert meta["faces"]["1"] == {"label": "fillet"}
    assert meta["faces"][str(part.faces["mount"].ids[0])] == {"role": "fixed"}
    assert meta["arrows"] == [{"at": [0.0, 5.0, 18.0], "dir": [0.0, 0.0, -1.0], "label": "500 N"}]
    assert import_step(str(outdir / "loads" / "bracket.step")).tags == ["load", "mount"]
    with pytest.raises(TuleError, match="99 is not a face"):
        outputs.overlay("bad", part, faces={99: "x"})
