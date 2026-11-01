import math

import pytest

from tule import *


def approx(a, b, rel=1e-6, abs_=1e-6):
    return a == pytest.approx(b, rel=rel, abs=abs_)


# ---------------------------------------------------------------- primitives


def test_box_roles_and_volume():
    b = box(2, 3, 4)
    assert approx(b.volume, 24)
    assert set(b.roles) == {"top", "bottom", "left", "right", "front", "back"}
    assert b.faces["top"].info()[0]["normal"] == (0.0, 0.0, 1.0)
    lo, hi = box(2, center=True).bounds
    assert approx(lo.x, -1) and approx(hi.z, 1)


def test_cylinder_cone_sphere_torus():
    assert approx(cylinder(2, 5).volume, math.pi * 4 * 5)
    assert approx(cone(3, 6).volume, math.pi * 9 * 6 / 3)
    assert approx(cylinder(3, 6, r2=1).volume, math.pi * 6 / 3 * (9 + 3 + 1))
    assert approx(sphere(2).volume, 4 / 3 * math.pi * 8)
    assert approx(torus(3, 1).volume, 2 * math.pi**2 * 3 * 1)
    assert set(cylinder(1, 1).roles) == {"top", "bottom", "side"}


def test_polyhedron_tetrahedron():
    t = polyhedron([(0, 0, 0), (1, 0, 0), (0, 1, 0), (0, 0, 1)], [(0, 2, 1), (0, 1, 3), (0, 3, 2), (1, 2, 3)])
    assert approx(t.volume, 1 / 6)


# ---------------------------------------------------------------- Booleans and transforms


def test_booleans():
    a, b = box(2), box(2).at(1, 0, 0)
    assert approx((a | b).volume, 12)
    assert approx((a - b).volume, 4)
    assert approx((a & b).volume, 4)
    assert approx((a ^ b).volume, 8)
    assert approx(union(box(1), box(1).at(2, 0, 0), box(1).at(4, 0, 0)).volume, 3)
    assert approx(sum([box(1), box(1).at(2, 0, 0)]).volume, 2)
    assert approx(difference(box(4), box(1), box(1).at(2, 0, 0)).volume, 62)


def test_union_merges_coplanar_faces():
    u = box(1) | box(1).at(1, 0, 0)
    assert len(u.faces) == 6


def test_transforms():
    b = box(2).at(1, 2, 3)
    assert approx(b.bounds[0].z, 3)
    r = box(2, 1, 1).rotate(z=90)
    lo, hi = r.bounds
    assert approx(hi.y - lo.y, 2)
    assert approx(box(1).scale(2).volume, 8)
    assert approx(box(1).scale(1, 2, 3).volume, 6)
    m = box(1).at(1, 0, 0).mirror(1, 0, 0)
    assert approx(m.bounds[1].x, -1)


# ---------------------------------------------------------------- provenance and tags


def bracket():
    plate = box(80, 40, 6, center=True).at(0, 0, 3)
    boss = cylinder(9, 12).at(0, 0, 6)
    return plate, boss


def test_feature_selection_survives_union():
    plate, boss = bracket()
    part = (plate | boss).fillet(boss.edges["bottom"], 2)
    assert part.is_valid
    ring = 2 * math.pi * (9 + 2 - 2 * 2 / (3 * (4 - math.pi)) * 0)  # rough: volume grows
    assert part.volume > (plate | boss).volume
    assert len(part.faces["fillet"]) == 1


def two_bosses():
    plate = rect(80, 40, center=True).round(5).extrude(6)
    b0, b1 = [circle(7).extrude(14).at(x, 0, 6) for x in (-22, 22)]
    return plate | b0 | b1, b0, b1


def test_fillet_keeps_roles_of_untouched_edges():
    # OCCT's fillet reports untouched edges as deleted; they must still resolve.
    part, b0, b1 = two_bosses()
    one = part.fillet(b0.edges["bottom"], 2)
    assert len(one.edges[b1.edges["bottom"]]) == 1
    assert len(one.edges[part.edges.parallel(Z)]) == len(part.edges.parallel(Z))


def test_fillets_of_two_features_of_the_same_kind():
    part, b0, b1 = two_bosses()
    ab = part.fillet(b0.edges["bottom"], 2).fillet(b1.edges["bottom"], 2)
    ba = part.fillet(b1.edges["bottom"], 2).fillet(b0.edges["bottom"], 2)
    both = part.fillet(part.edges[b0.edges["bottom"]] | part.edges[b1.edges["bottom"]], 2)
    for s in (ab, ba, both):
        assert s.is_valid
        assert approx(s.volume, both.volume, rel=1e-6)
    assert both.volume > part.volume
    assert len(both.faces["fillet"]) == 2
    with pytest.raises(TuleError, match="no longer exist"):
        ab.fillet(b0.edges["bottom"], 1)  # the fillet consumed it

    # a later Boolean and features still resolve both bosses' roles
    out = ab - [cylinder(3.2, 50, center=True).at(x, 0, 0) for x in (-22, 22)]
    out = out.chamfer(b0.edges["top"], 1).chamfer(b1.edges["top"], 1)
    assert out.is_valid
    assert len(out.faces[b0.faces["side"]]) == 1 and len(out.faces[b1.faces["side"]]) == 1
    assert len(out.faces[ab.faces["fillet"]]) == 1


def test_consumed_selection_raises():
    plate, boss = bracket()
    u = plate | boss
    with pytest.raises(TuleError, match="no longer exist"):
        u.faces[boss.faces["bottom"]]


def test_roles_do_not_cross_booleans():
    plate, boss = bracket()
    with pytest.raises(TuleError, match="no face group named 'top'"):
        (plate | boss).faces["top"]


def test_tags_survive_operations():
    plate, boss = bracket()
    p = plate.tag("load", plate.faces["top"])
    part = (p | boss).at(10, 0, 0).rotate(z=30) - cylinder(2, 50, center=True).at(30, 0, 0)
    faces = part.faces["load"]
    assert len(faces) == 1
    assert approx(faces.area, 80 * 40 - math.pi * 81 - math.pi * 4)
    assert part.tags == ["load"]


def test_tag_edges():
    b = box(10)
    b = b.tag("rim", b.edges["top"])
    assert len(b.at(1, 0, 0).edges["rim"]) == 4


# ---------------------------------------------------------------- selections


def test_queries():
    part = box(10) - cylinder(2, 30, center=True).at(5, 5, 0)
    assert len(part.faces.cylindrical()) == 1
    assert len(part.faces.cylindrical(r=2)) == 1
    assert len(part.faces.normal(+Z)) == 1
    assert len(part.faces.top()) == 1
    assert len(part.edges.parallel(Z)) == 4
    assert len(part.edges.circles(r=2)) == 2
    assert len(part.faces.planar().largest()) == 1
    assert len(part.edges.on(part.faces.top())) == 5
    top_edges = part.edges.top()
    assert all(c["center"][2] == pytest.approx(10) for c in top_edges.info())


def test_selection_set_ops_and_ids():
    b = box(1)
    s = b.faces["top"] | b.faces["bottom"]
    assert len(s) == 2
    assert len(s - b.faces["top"]) == 1
    assert len(b.faces & b.faces["top"]) == 1
    assert b.face(s.ids[0]).ids == [s.ids[0]]


def test_info_lists_tags():
    b = box(1)
    b = b.tag("load", b.faces["top"])
    info = b.faces.top().info()[0]
    assert info["tags"] == ["load"]


# ---------------------------------------------------------------- features


def test_fillet_chamfer_volumes():
    r = 1.0
    f = box(10).fillet(box(10).edges, r) if False else None  # selection must be on the shape
    b = box(10)
    one = b.fillet(b.edges.parallel(Z).nearest((0, 0, 5)), r)
    assert approx(one.volume, 1000 - (1 - math.pi / 4) * r * r * 10)
    c = b.chamfer(b.edges.parallel(Z).nearest((0, 0, 5)), 1)
    assert approx(c.volume, 1000 - 0.5 * 10)
    assert len(c.faces["chamfer"]) == 1


def test_fillet_error_reports_max_radius():
    b = box(10)
    with pytest.raises(TuleError) as e:
        b.fillet(b.edges, 7)
    assert e.value.op == "fillet"
    assert e.value.data["max_radius"] == pytest.approx(5, abs=0.05)
    assert len(e.value.edges) == 12


def test_shell():
    b = box(10, 10, 10)
    s = b.shell(b.faces["top"], 1)
    assert s.is_valid
    assert approx(s.volume, 1000 - 8 * 8 * 9)
    assert len(s.faces["inner"]) == 5


def test_selection_from_other_shape_is_rejected():
    a, b = box(1), box(1).at(5, 0, 0)
    with pytest.raises(TuleError):
        a.fillet(b.edges, 0.1)


# ---------------------------------------------------------------- profiles and sweeps


def test_profiles():
    assert approx(rect(60, 40).area, 2400)
    assert approx(rect(60, 40).round(6).area, 2400 - 4 * (36 - math.pi * 9))
    assert approx(slot(24, 6).area, 18 * 6 + math.pi * 9)
    assert approx(regular_polygon(6, 10).area, 6 * math.sqrt(3) / 4 * 100)
    assert approx(ellipse(10, 4).area, math.pi * 40)
    assert approx((rect(10, 10) | circle(5).at(10, 5)).area, 100 + math.pi * 25 / 2)
    assert approx((rect(10, 10) - rect(4, 4).at(3, 3)).area, 84)
    assert approx(polygon([(0, 0), (10, 0), (10, 10), (0, 10)], holes=[[(2, 2), (4, 2), (4, 4), (2, 4)]]).area, 96)


def test_polygon_rounded_corner():
    p = polygon([(0, 0), (10, 0, 2), (10, 10), (0, 10)])
    assert approx(p.area, 100 - (4 - math.pi))


def test_concave_rounding():
    l_shape = polygon([(0, 0), (10, 0), (10, 4), (4, 4), (4, 10), (0, 10)])
    # five convex corners lose material, the one concave corner gains it
    assert approx(l_shape.round(1).area, l_shape.area - 5 * (1 - math.pi / 4) + (1 - math.pi / 4))


def test_extrude_roles_and_volume():
    p = rect(60, 40, center=True).round(6) - circle(6)
    s = p.extrude(4)
    assert approx(s.volume, p.area * 4)
    assert set(s.roles) == {"bottom", "top", "side"}
    assert approx(extrude(rect(2, 2), 3, center=True).bounds[0].z, -1.5)


def test_extrude_twist_and_scale():
    assert approx(rect(10, 10, center=True).extrude(20, twist=90).volume, 2000, rel=1e-3)
    assert approx(rect(10, 10, center=True).extrude(20, scale=0.5).volume, 20 / 3 * (100 + 25 + 50))


def test_revolve():
    ring = revolve(rect(5, 10).at(3, 0))
    assert approx(ring.volume, math.pi * (64 - 9) * 10)
    assert set(ring.roles) == {"top", "bottom", "side"}
    half = revolve(rect(5, 10).at(3, 0), 180)
    assert approx(half.volume, math.pi * (64 - 9) * 10 / 2)
    assert {"start", "end"} <= set(half.roles)
    with pytest.raises(TuleError):
        revolve(rect(5, 5, center=True))


def test_loft():
    t = loft([rect(10, 10, center=True), rect(4, 4, center=True)], [0, 9])
    assert approx(t.volume, 9 / 3 * (100 + 16 + 40))
    assert set(t.roles) == {"bottom", "top", "side"}


# ---------------------------------------------------------------- assemblies


def test_assembly():
    a = Assembly(box(1).named("a"), Assembly(box(1).named("b").at(2, 0, 0), name="sub"), name="top")
    assert [p for p, _ in a.parts()] == ["top/a", "top/sub/b"]
    assert a["b"].name == "b"
    moved = a.at(10, 0, 0)
    assert approx(moved["a"].bounds[0].x, 10)
    with pytest.raises(TuleError):
        a["missing"]
