"""Placed copies (part.placed / tule.Instances): one body, N placements, written and drawn once."""

import json

import numpy as np
import pytest

from tule import Assembly, Instances, TuleError, box, cylinder, outputs, regular_polygon, sdf


@pytest.fixture
def outdir(tmp_path, monkeypatch):
    d = tmp_path / "outputs"
    monkeypatch.setenv("TULE_OUTPUTS", str(d))
    return d


def close(a, b, tol=1e-6):
    return np.allclose(np.asarray(a, float), np.asarray(b, float), atol=tol)


def test_placements_match_rotate_then_at():
    part = box(10, 4, 2).named("tile")
    tiles = part.placed(at=[(0, 0, 0), (20, 5, 1)], rotate=[(0, 0, 0), (30, 0, 90)])
    assert isinstance(tiles, Instances) and len(tiles) == 2 and tiles.kind == "instances" and tiles.name == "tile"
    same = part.rotate(30, 0, 90).at(20, 5, 1)
    assert close(tiles.copy(1).bounds, same.bounds)
    assert close(tiles.parts()[1][1].bounds, same.bounds) and tiles.parts()[1][0] == "tile[1]"
    assert tiles.volume == pytest.approx(2 * part.volume)
    # every copy moved: the same as moving each copy
    moved = tiles.rotate(z=45).at(0, 0, 7)
    assert close(moved.copy(1).bounds, same.rotate(z=45).at(0, 0, 7).bounds)
    # matrices, one row of rotate for all
    T = np.tile(np.eye(4), (3, 1, 1))
    T[:, 0, 3] = [0, 10, 20]
    row = part.placed(T, rotate=(0, 0, 90))
    assert close(row.copy(2).bounds, part.at(20, 0, 0).rotate(z=90).bounds)
    assert row.transforms.shape == (3, 4, 4) and not row.transforms.flags.writeable


def test_bounds_cover_every_copy_and_bad_input_says_so():
    part = box(2, 2, 2)
    grid = part.placed(at=np.array([(x, y, 0) for x in range(0, 100, 10) for y in range(0, 50, 10)]))
    lo, hi = grid.bounds
    assert close(lo, (0, 0, 0)) and close(hi, (92, 42, 2))
    with pytest.raises(TuleError, match="where are the copies"):
        part.placed()
    with pytest.raises(TuleError, match=r"\(N, 4, 4\)"):
        part.placed(np.zeros((3, 2)))
    with pytest.raises(TuleError, match="flattens"):
        part.placed(np.zeros((1, 4, 4)))


def test_assembly_lists_copies_and_finds_the_group():
    tile = box(5, 5, 1).named("tile", "#333")
    fin = Assembly(box(2, 2, 8).named("fin"), box(1, 1, 1).at(0, 0, 8).named("actuator"), name="fin_set")
    ship = Assembly(box(100, 10, 10).named("hull"), tile.placed(at=[(i * 6, 0, 10) for i in range(5)]).named("tiles"),
                    fin.placed(rotate=[(0, 0, a) for a in (0, 90, 180, 270)]).named("fins"), name="ship")
    paths = [p for p, _ in ship.parts()]
    assert paths[:3] == ["ship/hull", "ship/tiles[0]", "ship/tiles[1]"]
    assert "ship/fins[3]/actuator" in paths and len(paths) == 1 + 5 + 8
    assert isinstance(ship["tiles"], Instances)
    assert ship["tiles[2]"].color == "#333"
    assert ship.volume == pytest.approx(1000 * 10 + 5 * 25 + 4 * (32 + 1))
    assert close(ship.bounds[1], (100, 10, 11))
    assert repr(ship) == "<Assembly 'ship': 1 parts and 9 placed copies>"
    assert ship.to_shape().volume == pytest.approx(ship.volume)  # B-rep operations see every copy


def test_implicit_copies_are_meshed_once():
    body = sdf.sphere(3)
    with pytest.raises(TuleError, match="mesh the body once"):
        Assembly(body.placed(at=[(0, 0, 0), (10, 0, 0)]))
    m = body.placed(at=[(0, 0, 0), (10, 0, 0)]).to_mesh(cell=0.5)
    assert m.body.kind == "mesh" and len(m) == 2
    Assembly(m)  # now it can be shown


def test_outputs_write_the_body_once(outdir):
    tile = regular_polygon(6, 5).extrude(2).named("tile", "#222")
    many = tile.placed(at=np.array([(x * 12, y * 10, 0) for x in range(10) for y in range(10)]))
    engine = cylinder(3, 8).to_mesh(0.05).named("engine")
    fins = Assembly(box(2, 2, 8).named("fin"), name="set").placed(rotate=[(0, 0, a) for a in (0, 90)]).named("fins")
    folder = outputs.assembly("ship", Assembly(box(130, 100, 1).named("hull"), many.named("tiles"),
                                               engine.placed(at=[(-10, 0, 0), (-20, 0, 0)]), fins, name="ship"))
    meta = json.loads((outdir / "ship" / "output.json").read_text())
    by = {p["name"]: p for p in meta["parts"]}
    assert set(by) == {"ship/hull", "ship/tiles", "ship/engine", "ship/fins/fin"}
    assert "transforms" not in by["ship/hull"]
    assert by["ship/tiles"]["transforms"] == "tiles.transforms.npy" and by["ship/tiles"]["color"] == "#222222"
    T = np.load(outdir / "ship" / "tiles.transforms.npy")
    assert T.dtype == np.float32 and T.shape == (100, 4, 4) and close(T, many.transforms, 1e-4)
    assert by["ship/engine"]["file"] == "engine.ply" and len(by["ship/engine"]["transforms"]) == 2
    assert by["ship/engine"]["transforms"][1][3] == -20 and len(by["ship/engine"]["transforms"][1]) == 16
    assert len(by["ship/fins/fin"]["transforms"]) == 2
    assert sorted(f.name for f in (outdir / "ship").iterdir()) == [
        "engine.ply", "fins_fin.step", "hull.step", "output.json", "tiles.step", "tiles.transforms.npy"]
    # a placement is one line of output.json
    assert '"transforms": [\n        [1.0, 0.0, 0.0, -10.0,' in (outdir / "ship" / "output.json").read_text()
    assert folder.endswith("ship")
    # outputs.part takes copies too
    outputs.part("row", tile.placed(at=[(0, 0, 0), (12, 0, 0)]))
    meta = json.loads((outdir / "row" / "output.json").read_text())
    assert meta["kind"] == "assembly" and len(meta["parts"]) == 1 and len(meta["parts"][0]["transforms"]) == 2


def test_picture_draws_copies(outdir, tmp_path):
    pytest.importorskip("tule_cli")
    from PIL import Image

    tile = box(5, 5, 1).named("tile", "#a33")
    rail = box(240, 2, 2).at(0, -4, 0)

    def red(n):
        p = outputs.picture(Assembly(tile.placed(at=[(i * 8, 0, 0) for i in range(n)]), rail), str(tmp_path / f"{n}.png"), size=300)
        im = np.asarray(Image.open(p).convert("RGB")).astype(int)
        return (im[..., 0] > im[..., 2] + 40).sum()

    assert red(30) > 10 * red(1) > 0  # every copy is drawn
