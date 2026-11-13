"""Materials (how a part looks: metal, roughness): set on any part kind, kept through operations,
written beside the color (output.json, a PLY's header); a part without one is written as before."""

import json
import pickle

import pytest

from tule import Assembly, Mesh, TuleError, box, cylinder, outputs, sdf, sphere
from tule.look import MATERIALS


@pytest.fixture
def outdir(tmp_path, monkeypatch):
    d = tmp_path / "outputs"
    monkeypatch.setenv("TULE_OUTPUTS", str(d))
    return d


def test_a_preset_sets_metal_roughness_and_its_color():
    can = cylinder(10, 40).named("can", material="steel")
    assert can.material == {"metal": 1.0, "roughness": MATERIALS["steel"][1]}
    assert can.color == MATERIALS["steel"][2]  # no color of its own: steel's warm grey
    assert cylinder(10, 40).material is None and cylinder(10, 40).color is None
    # aliases, a dict, and metal=/roughness= over a preset
    assert box(1, 1, 1).with_material("stainless").material == can.material
    assert box(1, 1, 1).with_material({"metal": 1, "roughness": 0.2}).material == {"metal": 1.0, "roughness": 0.2}
    assert can.with_material(roughness=0.1).material == {"metal": 1.0, "roughness": 0.1}


def test_a_part_keeps_its_own_color_and_a_preset_color_follows_the_material():
    gold = sphere(5).with_material("polished", color="#e0b45c")
    assert gold.color == "#e0b45c" and gold.material["roughness"] < 0.1
    red = box(1, 1, 1).named("r", "#d33").with_material("plastic")
    assert red.color == "#d33"
    # steel's grey was the material's, not chosen: tiles bring their black
    assert cylinder(1, 1).with_material("steel").with_material("ceramic-tile").color == MATERIALS["ceramic-tile"][2]
    # recoloring keeps the material
    assert cylinder(1, 1).with_material("steel").colored("#888").material["metal"] == 1.0


def test_the_material_survives_operations_and_conversions():
    can = cylinder(10, 40).named("can", material="satin")
    for derived in (can - box(5, 5, 5), can.at(5, 0, 0).rotate(z=30), can.named("other"), can.to_mesh(0.2),
                    can.placed(at=[(0, 0, 0), (30, 0, 0)])):
        assert derived.material == can.material, derived
    assert pickle.loads(pickle.dumps(can.to_mesh(0.2))).material == can.material  # e.g. to a worker process
    body = sdf.sphere(5).named("ball", material="polished")
    assert body.material["metal"] == 1.0 and body.to_mesh(cell=1.0).material == body.material
    m = Mesh([(0, 0, 0), (1, 0, 0), (0, 1, 0)], [(0, 1, 2)], material="rubber")
    assert m.material == {"metal": 0.0, "roughness": MATERIALS["rubber"][1]}


def test_wrong_materials_say_what_there_is():
    with pytest.raises(TuleError, match="ceramic-tile"):
        box(1, 1, 1).with_material("unobtanium")
    with pytest.raises(TuleError, match="between 0 and 1"):
        box(1, 1, 1).with_material(roughness=2)
    with pytest.raises(TuleError, match="metal and roughness"):
        box(1, 1, 1).with_material({"shiny": True})


def test_outputs_write_the_material_beside_the_color(outdir):
    hull = cylinder(10, 40).named("hull", material="steel")
    tiles = box(4, 4, 1).placed(at=[(0, 0, 50), (5, 0, 50)]).named("tiles", material="ceramic-tile")
    plain = box(3, 3, 3).named("plain", "#4a8")
    outputs.assembly("ship", Assembly(hull, tiles, plain, sphere(3).named("nose").with_material("polished").to_mesh(0.1)))
    parts = {p["name"]: p for p in json.loads((outdir / "ship" / "output.json").read_text())["parts"]}
    assert parts["hull"]["material"] == hull.material and parts["hull"]["color"] == MATERIALS["steel"][2]
    assert parts["tiles"]["material"]["roughness"] == MATERIALS["ceramic-tile"][1] and "transforms" in parts["tiles"]
    assert "material" not in parts["plain"]  # no material: written as before
    assert parts["nose"]["material"]["metal"] == 1.0
    # a mesh keeps it in its PLY too
    assert Mesh.load(str(outdir / "ship" / parts["nose"]["file"])).material == parts["nose"]["material"]
    outputs.part("can", hull)
    meta = json.loads((outdir / "can" / "output.json").read_text())
    assert meta["material"] == hull.material


def test_an_assembly_takes_one_material_for_all():
    a = Assembly(box(1, 1, 1).named("a"), cylinder(1, 2).named("b")).named("kit", material="aluminium")
    assert all(p.material == {"metal": 1.0, "roughness": MATERIALS["aluminium"][1]} for _, p in a.parts())
    assert all(p.material["metal"] == 0 for _, p in a.with_material("rubber").parts())
