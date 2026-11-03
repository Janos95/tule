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


def test_step_moved_part_keeps_name_and_tags(tmp_path):
    part = box(10).named("plate — ü", "#4a8")
    part = part.tag("top", part.faces["top"]).rotate(x=90).at(5, 0, 0)  # a placement, not new geometry
    path = str(tmp_path / "m.step")
    part.export_step(path)
    back = import_step(path)
    assert back.name == "plate — ü" and back.tags == ["top"]
    assert back.faces["top"].info()[0]["center"][1] == pytest.approx(-10)


def test_step_copies_under_other_names_and_compound_parts(tmp_path):
    b = box(10)
    cord = cylinder(1, 5)
    for i in range(1, 4):
        cord = cord | cylinder(1, 5).at(6 * i, 0, 0)  # several solids in one part
    asm = Assembly(b.named("left"), b.named("right").at(20, 0, 0), b.named("left").at(40, 0, 0),
                   cord.named("cord"), name="row")
    path = str(tmp_path / "row.step")
    asm.export_step(path)
    names = [n for n, _ in import_step(path).parts()]
    assert names == ["row/left", "row/right", "row/left", "row/cord"]
    assert open(path).read().count("MANIFOLD_SOLID_BREP") == 2 + 4


def test_one_part_assembly_reads_as_a_shape(tmp_path):
    p = str(tmp_path / "one.step")
    export_step(Assembly(box(10, 10, 10).named("washer", "#888888"), name="PT-007"), p)
    r = import_step(p)
    assert isinstance(r, Shape) and r.name == "washer" and abs(r.volume - 1000) < 1e-6


def test_import_step_missing_file(tmp_path):
    (tmp_path / "a.step").write_text("")
    with pytest.raises(TuleError, match="no such file.*STEP files in .*: a.step"):
        import_step(str(tmp_path / "b.step"))


def _nested_board_step(path, rows=40, pins=50):
    """A vendor-style STEP: board > rows (sub-assemblies, placed) > pins (one solid, placed)."""
    from OCP.gp import gp_Trsf, gp_Vec
    from OCP.STEPCAFControl import STEPCAFControl_Writer
    from OCP.STEPControl import STEPControl_AsIs
    from OCP.TopLoc import TopLoc_Location
    from tule.io import _new_doc, _set_name, parse_color
    from OCP.Quantity import Quantity_Color, Quantity_TOC_RGB
    from OCP.XCAFDoc import XCAFDoc_ColorGen

    def at(x, y, z=0.0):
        t = gp_Trsf()
        t.SetTranslation(gp_Vec(x, y, z))
        return TopLoc_Location(t)

    doc, st, ct = _new_doc()
    pin = st.AddShape(cylinder(0.3, 2).wrapped, False)
    _set_name(pin, "pin")
    ct.SetColor(pin, Quantity_Color(*parse_color("#c9a43a"), Quantity_TOC_RGB), XCAFDoc_ColorGen)
    row = st.NewShape()
    _set_name(row, "row")
    for i in range(pins):
        st.AddComponent(row, pin, at(i, 0))
    board = st.NewShape()
    _set_name(board, "board")
    st.AddComponent(board, st.AddShape(box(60, 50, 1.6).wrapped, False), at(0, 0, -1.6))
    for j in range(rows):
        st.AddComponent(board, row, at(5, 5 + j, 0))
    st.UpdateAssemblies()
    w = STEPCAFControl_Writer()
    w.SetNameMode(True)
    w.SetColorMode(True)
    w.Transfer(doc, STEPControl_AsIs)
    w.Write(path)


def test_import_step_large_nested_assembly(tmp_path):
    import time

    path = str(tmp_path / "board.step")
    _nested_board_step(path)
    t = time.monotonic()
    a = import_step(path)
    took = time.monotonic() - t
    parts = a.parts()
    assert len(parts) == 1 + 40 * 50  # leaves only: no sub-assembly read again as a part
    assert parts[1][0] == "board/row/pin" and parts[1][1].color == "#c9a43a"
    # placements compose down the tree: row j at y = 5 + j, pin i at x = 5 + i
    lo, hi = parts[1 + 3 * 50 + 7][1].bounds
    assert (lo.x, lo.y, lo.z) == pytest.approx((5 + 7 - 0.3, 5 + 3 - 0.3, 0), abs=1e-6)
    assert a.bounds[1] == pytest.approx((60, 50, 2), abs=1e-6)
    # the pins are the file's one solid, placed: no copy per instance (that took minutes on a Pi 5 board)
    assert parts[1][1].wrapped.IsPartner(parts[-1][1].wrapped)
    assert took < 10, f"import_step took {took:.1f} s for 2001 small parts"


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

    seq = outputs.sequence("move")  # an assembly's parts are written once; a frame places them
    base, arm = box(10).named("base", "#4a8"), box(1, 1, 8).named("arm", "#d33")
    for a in (0, 30, 60):
        seq.add([base, arm.rotate(y=a).at(10, 0, 10)], angle=a)
    seq.add([base, box(2).named("arm", "#d33")])  # a new solid: a new file
    seq.done()  # output.json follows the frames at most once a second; done writes them all
    meta = json.load(open(outdir / "move" / "output.json"))
    assert sorted(f for f in os.listdir(outdir / "move") if f.endswith(".step")) == ["arm.step", "arm_2.step", "base.step"]
    f0, f2, f3 = meta["frames"][0], meta["frames"][2], meta["frames"][3]
    assert [p["file"] for p in f0["parts"]] == ["base.step", "arm.step"] and "transform" not in f0["parts"][0]
    assert f0["parts"][1]["color"] == "#d33" and f0["parts"][1]["transform"][3] == pytest.approx(10)
    t = f2["parts"][1]["transform"]  # 60° about y: x' = cos·x + sin·z
    assert t[0] == pytest.approx(0.5) and t[2] == pytest.approx(3 ** 0.5 / 2) and t[12:] == [0, 0, 0, 1]
    assert f3["parts"][1]["file"] == "arm_2.step" and f2["values"] == {"angle": 60.0}
    assert (outputs.folder("bom") / "x.csv").parent.name == "bom"
    f = outputs.folder("bom")
    (f / "x.csv").write_text("a\n")
    assert f.exists() and [p.name for p in f.iterdir()] == ["x.csv"] and len(list(f.glob("*.csv"))) == 1


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
    assert meta["kind"] == "overlay" and meta["file"] == "loads.step" and meta["title"] == "Load case 1"
    assert meta["faces"][str(load)] == {"role": "load", "label": "500 N"}
    assert meta["faces"]["1"] == {"label": "fillet"}
    assert meta["faces"][str(part.faces["mount"].ids[0])] == {"role": "fixed"}
    assert meta["arrows"] == [{"at": [0.0, 5.0, 18.0], "dir": [0.0, 0.0, -1.0], "label": "500 N"}]
    assert import_step(str(outdir / "loads" / "loads.step")).tags == ["load", "mount"]
    with pytest.raises(TuleError, match="99 is not a face"):
        outputs.overlay("bad", part, faces={99: "x"})


def test_overlay_values_scale_and_info(outdir):
    part = tagged_bracket()
    outputs.overlay("walls", part, faces={0: 1.8, 1: 0.9}, title="Wall thickness",
                    scale={"label": "wall thickness", "unit": "mm", "min": 0, "max": 3}, info={"material": "Al 6061", "mass_g": 20.8})
    meta = json.load(open(outdir / "walls" / "output.json"))
    assert meta["faces"]["0"]["value"] == 1.8 and meta["faces"]["1"]["value"] == 0.9
    assert meta["scale"] == {"label": "wall thickness", "unit": "mm", "min": 0.0, "max": 3.0}
    assert meta["info"] == {"material": "Al 6061", "mass_g": 20.8}
    outputs.part("plain", part, info={"mass": "20.8 g"})
    assert json.load(open(outdir / "plain" / "output.json"))["info"] == {"mass": "20.8 g"}
    with pytest.raises(TuleError, match="info\\['bad'\\] is a list"):
        outputs.part("x", part, info={"bad": [1, 2]})
    with pytest.raises(TuleError, match="scale is a dict"):
        outputs.overlay("y", part, faces={0: 1.0}, scale={"colour": "red"})


def test_picture(tmp_path):
    pytest.importorskip("tule_cli")
    from PIL import Image

    p = outputs.picture(Assembly(box(10).named("a", "#4a8"), box(5).at(12, 0, 0)), tmp_path / "s.png", view="top", size=400)
    assert Image.open(p).width == 400


def test_step_keeps_tags_on_left_handed_faces(tmp_path):
    # an extruded circle has a left-handed surface; the writer rebuilds it and used to drop nearby tags
    plate = rect(80, 40, center=True).round(5).extrude(6)
    part = (plate | circle(9).extrude(12).at(0, 0, 6)).mirror(1, 0, 0)
    part = part.tag("load", part.faces.top())
    path = str(tmp_path / "u.step")
    part.export_step(path)
    back = import_step(path)
    assert back.tags == ["load"] and back.faces["load"].info()[0]["center"][2] == pytest.approx(18)
    flat = lambda s: [v for f in s.faces.info() for v in f["center"]]  # the same faces, in the same order
    assert flat(back) == pytest.approx(flat(part), abs=1e-6)


def test_sequence_warns_when_frames_rebuild_their_parts(outdir, monkeypatch, capsys):
    monkeypatch.setattr(outputs.Sequence, "WARN_TOTAL", 20_000)
    with outputs.sequence("moved") as seq:  # made once, moved: no warning
        base, arm = box(10).named("base"), box(2, 2, 20).named("arm")
        for a in range(6):
            seq.add([base, arm.rotate(y=a)])
    assert "MB after" not in capsys.readouterr().err
    with outputs.sequence("rebuilt") as seq:
        for a in range(6):
            seq.add([(cylinder(5, 20) - box(2).at(a, 0, 0)).named("arm")])
    err = capsys.readouterr().err
    assert err.count("sequence 'rebuilt'") == 1 and "make each part once and move it" in err
    meta = json.load(open(outdir / "rebuilt" / "output.json"))
    assert "new" not in meta["frames"][0]["parts"][0] and "per frame" in meta["info"]["warning"]


def test_sequence_writes_a_part_made_again_each_frame_once(outdir):
    from tule import cylinder
    with outputs.sequence("winding") as seq:
        for a in range(5):
            seq.add([cylinder(12, 1).named("horn").rotate(z=10 * a).at(a, 0, 0)])
    files = [p.name for p in (outdir / "winding").iterdir() if p.suffix == ".step"]
    assert files == ["horn.step"]
    frames = json.load(open(outdir / "winding" / "output.json"))["frames"]
    assert len({json.dumps(f["parts"][0]["transform"]) for f in frames[1:]}) == 4


def test_an_empty_part_is_refused_by_name(tmp_path, monkeypatch):
    monkeypatch.setenv("TULE_OUTPUTS", str(tmp_path / "outputs"))
    empty = (box(1, 1, 1) & box(1, 1, 1).at(5, 0, 0)).named("sheath")
    with pytest.raises(TuleError, match="sheath is empty"):
        outputs.assembly("hand", Assembly(box(2, 2, 2).named("palm"), empty))
    assert not (tmp_path / "outputs" / "hand").exists()  # refused before anything is written


def test_bounds_are_cached_and_follow_quarter_turns():
    s = box(10, 20, 30).at(1, 2, 3)
    assert s.bounds is s.bounds and s.named("x").bounds is s.bounds
    for m in (s.rotate(z=90), s.rotate(x=-90).at(5, 0, 0), s.mirror(1, 0, 0), s.rotate(z=30)):
        fresh = Shape(m.wrapped).bounds
        assert [*m.bounds[0], *m.bounds[1]] == pytest.approx([*fresh[0], *fresh[1]], abs=1e-9)
    a = Assembly(s.named("a"), box(1).at(-5, 0, 0).named("b"))
    assert [*a.bounds[0], *a.bounds[1]] == pytest.approx([-5, 0, 0, 11, 22, 33])


def test_sequence_fps_is_the_play_rate(outdir):
    with outputs.sequence("run", fps=20) as seq:
        part = box(10).named("slider")
        for x in range(3):
            seq.add([part.at(x, 0, 0)])
    assert json.load(open(outdir / "run" / "output.json"))["fps"] == 20
    with pytest.raises(TuleError, match="between 0 and 60"):
        outputs.sequence("bad", fps=0)
