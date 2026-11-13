"""Cameras for tule screenshot (azimuth, elevation, distance, target, fov) and the viewer's ?cam=."""

import json
import math

import numpy as np
import pytest
from PIL import Image

from tule_cli import main
from tule_cli.camera import Camera, basis, direction_angles, fit_distance, matches, parse_cam, parse_size, project, split_copy
from tule_cli.render import VIEWS, Part, prepare, render_camera, select


def test_the_orbit_puts_the_eye_where_azimuth_and_elevation_say():
    cam = Camera(30, 20, 1000, (10, 20, 30))
    e, a = math.radians(20), math.radians(30)
    assert cam.eye == pytest.approx([10 + 1000 * math.cos(e) * math.cos(a), 20 + 1000 * math.cos(e) * math.sin(a), 30 + 1000 * math.sin(e)])
    R = basis(30, 20)
    assert R @ R.T == pytest.approx(np.eye(3), abs=1e-12)
    assert np.cross(R[0], R[1]) == pytest.approx(R[2])  # right-handed: right × up = toward the viewer
    assert R[0][2] == 0 and R[1][2] > 0  # z stays up on screen
    for view, d in VIEWS.items():  # the fixed views are cameras too
        az, el = direction_angles(d)
        assert basis(az, el)[2] == pytest.approx(np.asarray(d) / np.linalg.norm(d), abs=2e-4), view
    assert direction_angles((0, -1, 0)) == pytest.approx((-90, 0))  # front: from -Y
    assert basis(0, 90)[2][2] < 1 and basis(0, 90)[0] == pytest.approx([0, 1, 0])  # straight down keeps its azimuth


def test_points_project_as_the_viewer_projects_them():
    cam = Camera(-90, 0, 500, (0, 0, 0), 30)  # from the front: x to the right, z up
    x, y, d = project([(0, 0, 0), (50, 0, 0), (0, 0, 50)], cam, 800, 600)
    f = 300 / math.tan(math.radians(15))
    assert (x[0], y[0], d[0]) == pytest.approx((400, 300, 500))
    assert x[1] == pytest.approx(400 + f * 50 / 500) and y[2] == pytest.approx(300 - f * 50 / 500)


def test_a_camera_reads_back_from_its_link():
    cam = Camera(37.5, -12.25, 15000, (1, -2.5, 60000), 22)
    assert parse_cam(cam.param()) == cam
    assert parse_cam("0,10,100,0,0,0").fov == 30
    assert parse_cam("0,90,100,0,0,0").elevation == pytest.approx(89.99)
    for bad in ("1,2,3", "0,0,-5,0,0,0", "a,b,c,d,e,f"):
        with pytest.raises(ValueError):
            parse_cam(bad)
    assert parse_size("1600x1200") == (1600, 1200) and parse_size("900") == (900, None) and parse_size([64, 48]) == (64, 48)
    with pytest.raises(ValueError):
        parse_size("10x5")


def test_fitting_keeps_every_corner_in_the_picture():
    lo, hi = np.array([-4500, -4500, 0]), np.array([4500, 4500, 124000])
    for az, el, fov, (W, H) in ((0, 10, 30, (800, 600)), (135, -40, 8, (600, 900)), (-90, 85, 60, (1000, 1000))):
        cam = Camera(az, el, 1, tuple((lo + hi) / 2), fov)
        cam = Camera(az, el, fit_distance(lo, hi, cam, W / H), cam.target, fov)
        corners = [[(hi if i & 1 else lo)[0], (hi if i & 2 else lo)[1], (hi if i & 4 else lo)[2]] for i in range(8)]
        x, y, d = project(corners, cam, W, H)
        assert (d > 0).all() and x.min() >= 0 and x.max() <= W and y.min() >= 0 and y.max() <= H
        assert max(np.abs(x - W / 2).max() / W, np.abs(y - H / 2).max() / H) > 0.4  # and a corner is near the edge


def test_parts_are_named_by_path_group_or_copy():
    assert matches("ship/heatshield/tiles", "ship/heatshield") and matches("ship/heatshield/tiles", "heatshield")
    assert matches("ship/heatshield/tiles", "heatshield/tiles") and matches("ship/heatshield/tiles", "ship/heatshield/tiles")
    assert not matches("ship/heatshield/tiles", "tile") and not matches("ship/heatshield/tiles", "ship/tiles")
    assert split_copy("tiles[17]") == ("tiles", 17) and split_copy("tiles") == ("tiles", None)


def _box(name, lo, hi, color=(0.5, 0.5, 0.5), copies=1, path=""):
    from tule import box

    s = box(*(np.subtract(hi, lo))).at(*lo)
    m = s.tessellate()
    return Part(name, m.positions, m.normals, m.indices, m.face_ids, color, {}, 6, path=path, copies=copies)


def test_the_picture_puts_a_point_where_the_camera_math_says():
    """A small cube at a known point: its pixels' middle is where camera.project puts it."""
    prep = prepare([_box("dot", (95, 45, 15), (105, 55, 25), (0.9, 0.1, 0.1))])
    cam = Camera(-70, 25, 400, (60, 20, 0), 30)
    img, rep = render_camera(prep, cam, 640, 480, edges=False)
    a = np.asarray(img).astype(int)
    red = (a[..., 0] > 120) & (a[..., 1] < 90)
    ys, xs = np.nonzero(red)
    x, y, _ = project([(100, 50, 20)], cam, 640, 480)
    assert img.size == (640, 480)
    assert abs(xs.mean() + 0.5 - x[0]) < 1.5 and abs(ys.mean() + 0.5 - y[0]) < 1.5


def test_a_camera_close_to_a_big_part_sees_it_whole():
    """Triangles behind the eye are cut at the near plane, not dropped: a wall beside the camera fills the view."""
    prep = prepare([_box("wall", (-100000, -10, -100000), (100000, 0, 100000))])
    img, rep = render_camera(prep, Camera(-90, 0, 50, (0, 0, 0)), 200, 150)
    assert rep["covered"] == pytest.approx(1.0)


def test_hidden_parts_and_copies_are_not_drawn():
    tiles = [_box("t", (i * 20, 0, 0), (i * 20 + 10, 10, 10)) for i in range(4)]
    P = np.concatenate([t.positions for t in tiles])
    I = np.concatenate([t.indices.astype(np.int64) + k * len(tiles[0].positions) for k, t in enumerate(tiles)])
    merged = Part("tiles", P, np.concatenate([t.normals for t in tiles]), I, np.tile(tiles[0].face_ids, 4), (0.2, 0.2, 0.2),
                  {}, 6, path="ship/tiles", copies=4)
    prep = prepare([merged, _box("hull", (-50, 20, -50), (150, 30, 50), path="ship/hull")])
    assert select(prep, "ship") == [(0, None), (1, None)] and select(prep, "tiles[2]") == [(0, 2)]
    with pytest.raises(ValueError, match="parts: ship/hull, ship/tiles"):
        select(prep, "fins")
    cam = Camera(-90, 0, 300, (35, 0, 5))
    _, all_ = render_camera(prep, cam, 300, 200)
    _, nohull = render_camera(prep, cam, 300, 200, hidden=select(prep, "hull"))
    _, two = render_camera(prep, cam, 300, 200, hidden=select(prep, "hull") + select(prep, "tiles[2]") + select(prep, "tiles[3]"))
    assert "ship/hull" in all_["parts"] and "ship/hull" not in nohull["parts"]
    assert two["parts"]["ship/tiles"] == pytest.approx(nohull["parts"]["ship/tiles"] / 2, rel=0.05)


@pytest.fixture
def rocket(tmp_path, monkeypatch):
    from tule import Assembly, box, cylinder, outputs

    monkeypatch.setenv("TULE_OUTPUTS", str(tmp_path / "outputs"))
    tile = box(300, 300, 40, center=True).named("tile", "#151515")
    outputs.assembly("rocket", Assembly(cylinder(4500, 50000).named("hull", "#c0c4c8"),
                                        tile.placed(at=[(0, -4520, 5000 + 400 * k) for k in range(30)]).named("tiles"),
                                        box(100, 2000, 3000).at(4500, -1000, 45000).named("fin", "#444")).named("booster"))
    return tmp_path


def test_screenshot_through_a_camera(rocket, capsys):
    png = rocket / "c.png"
    main(["screenshot", "--output", "rocket", "-o", str(png), "--azimuth", "-90", "--elevation", "0", "--target-part", "tiles[3]",
          "--size", "320x240", "--hide", "fin"])
    out = capsys.readouterr().out
    assert Image.open(png).size == (320, 240)
    cam = parse_cam(out.split("?cam=")[1].split()[0].split("&")[0])
    assert cam.azimuth == -90 and cam.target == pytest.approx((0, -4520, 6200)) and cam.distance < 3000
    assert "&hide=fin" in out and "booster/tiles" in out.split("in view:")[1] and "booster/fin" not in out
    main(["screenshot", "--output", "rocket", "-o", str(png), f"--cam={cam.param()}", "--size", "320x240", "--no-edges"])
    assert f"?cam={cam.param()} " in capsys.readouterr().out  # the same camera again
    with pytest.raises(SystemExit, match="no part or group 'flaps'.*booster/fin"):
        main(["screenshot", "--output", "rocket", "-o", str(png), "--target-part", "flaps"])
    main(["screenshot", "--output", "rocket", "-o", str(png), "--view", "front", "--hide", "fin"])  # the fixed views take --hide
    assert "view front" in capsys.readouterr().out


def test_a_batch_of_cameras_loads_the_model_once(rocket, capsys):
    cams = rocket / "cams.json"
    cams.write_text(json.dumps({"defaults": {"size": "160x120", "elevation": 10},
                                "cameras": [{"name": f"az{a}", "azimuth": a} for a in (0, 90, 180, 270)]
                                + [{"name": "fin", "target_part": "fin", "azimuth": 0, "edges": False},
                                   {"azimuth": 45, "distance": 2000, "target": [4500, 0, 45000], "hide": "tiles"}]}))
    main(["screenshot", "--output", "rocket", "--cameras", str(cams), "-o", str(rocket / "shots"), "--jobs", "2"])
    assert "6 pictures" in capsys.readouterr().out
    meta = json.loads((rocket / "shots" / "cameras.json").read_text())["cameras"]
    assert [c["name"] for c in meta] == ["az0", "az90", "az180", "az270", "fin", "0006"]
    for c in meta:
        assert Image.open(rocket / "shots" / c["file"]).size == (160, 120)
        assert parse_cam(c["cam"]).distance == pytest.approx(c["distance"])
    assert meta[4]["in_view"].get("booster/fin", 0) > 0.05 and meta[5]["hide"] == ["tiles"] and "&hide=tiles" in meta[5]["link"]
    cams.write_text(json.dumps([{"azimuth": 0, "zoom": 2}]))
    with pytest.raises(SystemExit, match="unknown key 'zoom'"):
        main(["screenshot", "--output", "rocket", "--cameras", str(cams), "-o", str(rocket / "shots")])
    cams.write_text(json.dumps([{"azimuth": 0}, {"target_part": "nope"}]))
    with pytest.raises(SystemExit, match="camera '0002'.*no part or group 'nope'"):
        main(["screenshot", "--output", "rocket", "--cameras", str(cams), "-o", str(rocket / "shots")])
