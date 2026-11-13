"""tule screenshot draws a part's material: metal reflects a studio (bright and banded), a matte part stays
matte, and a part without a material (an old output) is drawn exactly as before."""

import numpy as np
import pytest

from tule_cli.camera import Camera
from tule_cli.machine import scene
from tule_cli.render import Part, prepare, render, render_camera


def _part(shape, name, color, material=None):
    m = shape.tessellate()
    return Part(name, m.positions, m.normals, m.indices, m.face_ids, color, {}, len(shape.faces), material=material)


@pytest.fixture
def studio(tmp_path, monkeypatch):
    from tule import Assembly, box, cylinder, outputs

    monkeypatch.setenv("TULE_OUTPUTS", str(tmp_path / "outputs"))
    outputs.assembly("studio", Assembly(
        cylinder(20, 80).named("steel", material="steel"),
        cylinder(20, 80).at(60, 0, 0).named("paint", "#cfccc6"),
        box(10, 10, 3).placed(at=[(120, 0, 0), (132, 0, 0)]).named("tiles", material="ceramic-tile")))
    return tmp_path / "outputs" / "studio"


def test_the_material_comes_from_output_json(studio):
    parts, _, _ = scene(str(studio))
    by = {p.name: p for p in parts}
    assert by["steel"].material == (1.0, pytest.approx(0.36)) and by["paint"].material is None
    assert by["tiles"].material[0] == 0.0 and by["tiles"].copies == 2


def test_steel_reads_as_metal_and_paint_as_before():
    from tule import cylinder

    can = cylinder(20, 80)
    grey = (0.81, 0.8, 0.78)
    cam = Camera(-90, 10, 400, (0, 0, 40), 30)
    shots = {}
    for name, mat in (("paint", None), ("steel", (1.0, 0.36)), ("matte", (0.0, 0.9))):
        img, _ = render_camera(prepare([_part(can, name, grey, mat)]), cam, 240, 240, edges=False)
        a = np.asarray(img, dtype=float)[60:200, :, :].mean(2)
        row = a[70]
        across = row[row < 240]  # the cylinder, not the background
        shots[name] = across[len(across) // 8:-len(across) // 8]  # inside its outline
    # a metal shows its surroundings: more contrast across the cylinder than the same color painted
    assert np.ptp(shots["steel"]) > 1.3 * np.ptp(shots["paint"])
    assert shots["steel"].mean() > 120  # still bright steel, not a dark grey
    # matte: a duller highlight than the default paint
    assert shots["matte"].max() < shots["paint"].max()


def test_a_part_without_a_material_is_lit_as_before():
    from tule import box, cylinder

    plain = _part(cylinder(20, 80), "plain", (0.5, 0.6, 0.7))
    cube = box(20, 20, 20).at(100, 0, 0)
    before, _ = render([plain, _part(cube, "cube", (0.8, 0.8, 0.8))], "front", 300)
    after, _ = render([plain, _part(cube, "cube", (0.8, 0.8, 0.8), (1.0, 0.1))], "front", 300)
    a, b = np.asarray(before).astype(int), np.asarray(after).astype(int)
    left = slice(0, a.shape[1] // 3)  # the plain cylinder: the same pixels; the polished cube: not
    assert np.array_equal(a[:, left], b[:, left]) and not np.array_equal(a, b)
