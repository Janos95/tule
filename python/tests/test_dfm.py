import json
import math
import os

import pytest

from tule import *


def by_check(report, check):
    return [f for f in report.findings if f.check == check]


# ---------------------------------------------------------------- CNC


def pocket_part(corner: float = 0.0):
    p = rect(20, 10)
    if corner:
        p = p.round(corner)
    part = box(40, 30, 10) - p.extrude(6).at(10, 10, 4)
    return part.tag("floor", part.faces.planar().normal(+Z).within((10, 10, 3.9), (30, 20, 4.1)))


def test_sharp_pocket_corners_are_flagged_with_their_faces_and_edges():
    part = pocket_part()
    report = dfm.cnc(part, axis=-Z, tool_radius=3)
    corners = by_check(report, "corner_radius")
    assert len(corners) == 4
    # the four vertical edges inside the pocket, and the pocket walls around them
    expected_edges = set(part.edges.parallel(Z).within((10, 10, 4), (30, 20, 10)).ids)
    walls = {d["id"] for d in part.faces.planar().within((10, 10, 4), (30, 20, 10)).info() if d["normal"][2] == 0}
    assert {f.edges[0] for f in corners} == expected_edges
    assert set().union(*(f.faces for f in corners)) == walls
    for f in corners:
        assert f.severity == "error"
        assert f.values == {"radius": 0.0, "tool_radius": 3}
        assert "fillet" in f.message and "3.3" in f.message
    assert not report.ok
    assert len(report.errors) == 4 and not report.warnings


def test_corner_radius_against_tool_radius():
    small = pocket_part(corner=2)
    report = dfm.cnc(small, axis=-Z, tool_radius=3)
    corners = by_check(report, "corner_radius")
    assert {f.faces[0] for f in corners} == set(small.faces.cylindrical(r=2).ids)
    assert all(f.values["radius"] == pytest.approx(2) for f in corners)
    assert dfm.cnc(pocket_part(corner=4), axis=-Z, tool_radius=3).ok


def test_small_hole_is_a_warning_not_an_error():
    part = box(30, 30, 10) - cylinder(1.5, 30, center=True).at(15, 15, 0)
    report = dfm.cnc(part, axis=-Z, tool_radius=3)
    assert report.ok
    (hole,) = by_check(report, "hole")
    assert hole.severity == "warning"
    assert hole.faces == tuple(part.faces.cylindrical(r=1.5).ids)


def test_cross_hole_is_an_undercut_unless_a_setup_reaches_it():
    part = box(40, 30, 20) - cylinder(3, 60, center=True).rotate(y=90).at(20, 15, 10)
    hole = set(part.faces.cylindrical(r=3).ids)
    report = dfm.cnc(part, axis=-Z, tool_radius=2)
    under = by_check(report, "undercut")
    assert {i for f in under for i in f.faces} == hole
    assert all(f.severity == "error" for f in under)
    sideways = dfm.cnc(part, axis=[-Z, +X], tool_radius=2)
    assert not by_check(sideways, "undercut")


def test_deep_narrow_slot():
    part = box(40, 30, 30) - box(4, 20, 25).at(5, 5, 5)
    floor = part.faces.planar().normal(+Z).within((5, 5, 4.9), (9, 25, 5.1)).ids
    report = dfm.cnc(part, axis=-Z, tool_radius=1.5, max_depth_ratio=4)
    (deep,) = by_check(report, "pocket_depth")
    assert list(deep.faces) == floor
    assert deep.values["depth"] == pytest.approx(25)
    assert deep.values["width"] == pytest.approx(4, abs=0.1)
    narrow = dfm.cnc(part, axis=-Z, tool_radius=3)
    (w,) = by_check(narrow, "pocket_width")
    assert w.severity == "error" and list(w.faces) == floor


def test_thin_floor():
    part = box(40, 30, 10) - box(30, 20, 9.5).at(5, 5, 0.5)
    report = dfm.cnc(part, axis=-Z, tool_radius=3, min_wall=1.0)
    (thin,) = by_check(report, "thin_wall")
    assert thin.values["thickness"] == pytest.approx(0.5, abs=0.01)
    assert thin.severity == "warning"  # error only below half the minimum
    bottom = part.faces.planar().normal(-Z).ids
    floor = part.faces.planar().normal(+Z).within((5, 5, 0.4), (35, 25, 0.6)).ids
    assert set(thin.faces) == set(bottom + floor)


def test_plain_box_is_fine():
    report = dfm.cnc(box(20, 20, 10), axis=-Z)
    assert report.ok and not report.findings


# ---------------------------------------------------------------- FDM


def ramp(angle: float):
    """A block whose -X side leans ``angle`` degrees from vertical, overhanging."""
    run = 10 * math.tan(math.radians(angle))
    return extrude(polygon([(0, 0), (10, 0), (10 + run, 10), (0, 10)]), 20).rotate(x=90).at(0, 20, 0)


@pytest.mark.parametrize("angle,flagged", [(30, False), (60, True)])
def test_overhang_angle(angle, flagged):
    part = ramp(angle)
    report = dfm.fdm(part, up=+Z, max_overhang=45)
    over = by_check(report, "overhang")
    assert bool(over) == flagged
    if flagged:
        (f,) = over
        assert f.values["angle"] == pytest.approx(angle, abs=0.1)
        (face,) = f.faces
        assert part.face(face).info()[0]["normal"][2] < 0


def test_bridges():
    tunnel = box(30, 10, 10) - box(20, 10, 5).at(5, 0, 0)
    (b,) = by_check(dfm.fdm(tunnel, max_bridge=10), "bridge")
    assert b.values["span"] == pytest.approx(20, abs=0.2)
    assert list(b.faces) == tunnel.faces.planar().normal(-Z).within((5, 0, 4.9), (25, 10, 5.1)).ids
    narrow = box(30, 10, 10) - box(8, 10, 5).at(11, 0, 0)
    assert not by_check(dfm.fdm(narrow, max_bridge=10), "bridge")


def test_bed_contact_and_thin_walls():
    report = dfm.fdm(sphere(5))
    (bed,) = by_check(report, "bed_contact")
    assert bed.severity == "error" and not report.ok
    b = box(20, 20, 5)
    (info,) = by_check(dfm.fdm(b), "bed_contact")
    assert info.severity == "info" and info.values["area"] == pytest.approx(400)
    assert info.faces == tuple(b.faces["bottom"].ids)
    shell = box(20, 20, 20) - box(19.6, 30, 19.6, center=True).at(10, 10, 10)
    thin = by_check(dfm.fdm(shell, min_wall=0.8), "thin_wall")
    assert thin and all(f.severity == "error" and f.values["thickness"] == pytest.approx(0.2, abs=0.01) for f in thin)


# ---------------------------------------------------------------- reports and overlays


def test_report_text_json_and_tags():
    part = pocket_part(corner=2)
    part = part.tag("pocket-wall", part.faces.cylindrical(r=2))
    report = dfm.cnc(part, axis=-Z, tool_radius=3)
    text = str(report)
    assert text.startswith("DFM cnc-3axis (axis=-Z, tool_radius=3")
    assert "NOT OK, 4 errors" in text and "[pocket-wall]" in text
    data = json.loads(report.to_json())
    assert data["ok"] is False and data["params"]["axis"] == "-Z"
    assert all(f["tags"] == ["pocket-wall"] for f in data["findings"])
    assert all(set(f) >= {"check", "severity", "label", "message", "faces", "values"} for f in data["findings"])


def test_overlay_output(tmp_path, monkeypatch):
    monkeypatch.setenv("TULE_OUTPUTS", str(tmp_path))
    part = pocket_part().named("plate")
    report = dfm.cnc(part, axis=-Z, tool_radius=3)
    folder = outputs.overlay("plate-dfm", part, report, title="CNC")
    meta = json.load(open(os.path.join(folder, "output.json")))
    assert meta["kind"] == "overlay" and meta["version"] == 1 and meta["title"] == "CNC"
    assert meta["file"] == "plate.step" and os.path.exists(os.path.join(folder, "plate.step"))
    flagged = {str(i) for f in report.findings for i in f.faces}
    assert set(meta["faces"]) == flagged
    assert all(v["severity"] == "error" and "sharp internal corner" in v["label"] for v in meta["faces"].values())
    assert len(meta["findings"]) == len(report.findings)
    assert meta["summary"] == str(report)
    # plain per-face values and arrows
    folder = outputs.overlay("plate-values", part, {0: 0.25, 1: "label", 2: {"role": "fixed"}},
                             arrows=[{"at": (1, 2, 3), "dir": (0, 0, -1), "label": "10 N"}])
    meta = json.load(open(os.path.join(folder, "output.json")))
    assert meta["faces"] == {"0": {"value": 0.25}, "1": {"label": "label"}, "2": {"role": "fixed"}}
    assert meta["arrows"] == [{"at": [1.0, 2.0, 3.0], "dir": [0.0, 0.0, -1.0], "label": "10 N"}]
    with pytest.raises(TuleError, match="not a face"):
        outputs.overlay("bad", part, {999: 1.0})
