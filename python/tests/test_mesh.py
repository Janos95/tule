"""tule.Mesh: meshes as parts — regions, tags, booleans, conversions, files."""
import json

import numpy as np
import pytest

from tule import *
from tule import sdf, fea

manifold3d = pytest.importorskip("manifold3d")


def test_brep_to_mesh_keeps_faces_and_tags():
    b = box(20, 10, 5)
    b = b.tag("top", b.faces.top())
    m = b.to_mesh(0.01)
    assert m.kind == "mesh" and m.is_closed and m.regions == 6
    assert m.volume == pytest.approx(1000) and m.faces["top"].area == pytest.approx(200)
    assert m.faces.top().area == pytest.approx(200)
    assert m.faces.normal((1, 0, 0)).area == pytest.approx(50)
    assert m.bounds[1] == pytest.approx((20, 10, 5))


def test_crease_regions_on_plain_triangles():
    m = box(10).to_mesh()
    plain = Mesh(m.points, m.indices.reshape(-1, 3))
    assert plain.regions == 6
    assert sdf.sphere(5).to_mesh(0.3).regions == 1
    assert len(plain.edges) > 0  # feature edges: the cube's 12, drawn as segments


def test_booleans_keep_tags_and_regions():
    a = box(20).to_mesh().tag("base", lambda f: f.bottom())
    hole = cylinder(4, 40).at(10, 10, -10).to_mesh(0.01).named("hole")
    m = a - hole
    assert m.is_closed and m.volume == pytest.approx(8000 - np.pi * 16 * 20, rel=0.01)
    assert "base" in m.tags and m.faces["base"].area == pytest.approx(400 - np.pi * 16, rel=0.01)
    u = a | sdf.sphere(6).at(20, 20, 20).to_mesh(0.3)
    assert u.is_closed and u.volume > 8000
    with pytest.raises(TuleError, match="to_mesh"):
        a | box(3)


def test_transforms_and_selections():
    m = box(10).to_mesh().at(5, 0, 0).rotate(z=90)
    assert m.bounds[0] == pytest.approx((-10, 5, 0), abs=1e-5)
    near = m.faces.nearest((0, 20, 5))
    assert near.normal_mean == pytest.approx((0, 1, 0), abs=1e-6)
    assert m.faces.largest(2).area == pytest.approx(200)
    assert (m.faces.top() | m.faces.bottom()).area == pytest.approx(200)


def test_files_roundtrip(tmp_path):
    m = box(10).to_mesh().named("cube", "#c33").tag("top", lambda f: f.top())
    p = tmp_path / "cube.stl"
    m.export_stl(p)
    back = Mesh.load(p)
    assert back.name == "cube" and back.color == "#cc3333" and back.regions == 6
    assert back.faces["top"].area == pytest.approx(100)
    m.export_3mf(tmp_path / "cube.3mf")
    assert (tmp_path / "cube.3mf").stat().st_size > 0
    with pytest.raises(TuleError):
        m.export_step(tmp_path / "x.step")


def test_simplify_and_back_to_brep():
    s = sdf.sphere(10).to_mesh(0.3)
    t = s.simplify(0.05)
    assert t.triangles < s.triangles and t.volume == pytest.approx(s.volume, rel=0.01)
    b = box(10).to_mesh().to_brep()
    assert b.kind == "brep" and b.volume == pytest.approx(1000)


def test_assembly_and_outputs(tmp_path, monkeypatch):
    from tule import outputs

    monkeypatch.setenv("TULE_OUTPUTS", str(tmp_path))
    m = sdf.sphere(5).to_mesh(0.3).named("ball", "#48c")
    asm = Assembly(box(10).named("block"), m.at(20, 0, 0))
    assert asm.size[0] == pytest.approx(25, abs=0.01)
    folder = outputs.part("ball", m)
    meta = json.load(open(f"{folder}/output.json"))
    assert meta["file"].endswith(".stl") and meta["name"] == "ball"
    outputs.assembly("pair", asm)
    with pytest.raises(TuleError, match="to_mesh"):
        Assembly(sdf.sphere(1))


def test_fea_on_a_mesh_matches_the_brep():
    STEEL = fea.Material("test steel", E=200_000, nu=0.3, density=7.85, yield_strength=250)
    m = box(100, 10, 10).to_mesh()
    m = m.tag("root", m.faces.normal((-1, 0, 0))).tag("tip", m.faces.normal((1, 0, 0)))
    r = fea.static(m, STEEL, fixed="root", loads=[fea.force("tip", (0, 0, -100))], mesh="voxel", verbose=False)
    tip = -r.displacement_at([(100, 5, 5)])[0][2]
    assert tip == pytest.approx(100 * 100 ** 3 / (3 * 200_000 * 10 ** 4 / 12), rel=0.04)
    with pytest.raises(TuleError, match="to_mesh"):
        fea.static(sdf.box(10), STEEL, fixed="root", verbose=False)


def test_picture_and_pinched_roundtrip(tmp_path):
    pytest.importorskip("tule_cli")
    from tule import outputs

    two = box(10).to_mesh() | box(10).at(10, 10, 0).to_mesh()  # touching along an edge: four triangles on it
    assert two.is_closed
    two.export_stl(tmp_path / "two.stl")
    assert Mesh.load(tmp_path / "two.stl").is_closed
    p = outputs.picture(sdf.sphere(5).to_mesh(0.5).named("ball", "#48c"), tmp_path / "ball.png", size=300)
    assert (tmp_path / "ball.png").stat().st_size > 1000
