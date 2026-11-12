

def test_text():
    import pytest

    from tule import TuleError, box, text

    try:
        t = text("MK4 Tools", 10, bold=True)
    except TuleError as e:  # no DejaVu fonts on this computer (they are on Tule machines)
        pytest.skip(str(e))
    raised = (box(80, 20, 3) + t.at(4, 6).extrude(1).at(0, 0, 3))
    assert raised.is_valid and raised.volume > box(80, 20, 3).volume
    assert 6 < t.extrude(1).size[1] < 9 and text("MK4", 10, align="center").extrude(1).bounds[0][0] < 0
    with pytest.raises(TuleError):
        text("x", font="no-such-font")


def test_spline_circle():
    import math

    import pytest

    from tule import TuleError, spline

    pts = [(10 * math.cos(2 * math.pi * k / 64), 10 * math.sin(2 * math.pi * k / 64)) for k in range(64)]
    p = spline(pts)
    assert p.area == pytest.approx(math.pi * 100, rel=1e-4)
    solid = p.extrude(3)
    assert solid.is_valid and solid.volume == pytest.approx(3 * math.pi * 100, rel=1e-4)
    assert spline(pts[::-1]).area == pytest.approx(p.area, rel=1e-9)        # either direction
    assert spline(pts + pts[:1], closed=False).area == pytest.approx(p.area, rel=1e-4)  # a corner at the start
    with pytest.raises(TuleError, match="at least 3"):
        spline([(0, 0), (1, 0), (0, 0)])
    with pytest.raises(TuleError, match="open curve") as e:
        spline(pts, closed=False)
    assert "closed=True" in e.value.hint
    with pytest.raises(TuleError, match="crosses itself"):
        spline([(10 * math.sin(t), 5 * math.sin(2 * t)) for t in (0.3 + 2 * math.pi * k / 40 for k in range(40))])
    with pytest.raises(TuleError, match="on a line"):
        spline([(0, 0), (1, 1), (2, 2)])


def test_spline_dense_and_tol():
    import math

    import pytest

    from tule import spline

    # 1,250 samples of a wavy outline: one smooth curve that measures right, also when fitted within tol
    r = lambda a: 20 + 1.5 * math.cos(25 * a)
    pts = [(r(a) * math.cos(a), r(a) * math.sin(a)) for a in (2 * math.pi * k / 1250 for k in range(1250))]
    exact = math.pi * (400 + 1.5 ** 2 / 2)
    p = spline(pts)
    assert p.area == pytest.approx(exact, rel=1e-5)
    assert p.extrude(1).volume == pytest.approx(exact, rel=1e-5)
    light = spline(pts, tol=1e-3)
    assert light.area == pytest.approx(exact, rel=1e-5) and light.extrude(1).is_valid


def test_round_too_big_says_the_largest_that_fits():
    import pytest

    from tule import TuleError, rect

    with pytest.raises(TuleError, match="largest radius that works ≈ 5") as e:
        rect(20, 10).round(6)
    assert 4.9 < e.value.data["max_radius"] <= 5


def test_a_corner_that_cannot_be_rounded_or_chamfered_is_not_skipped():
    import pytest

    from tule import TuleError, polygon, rect

    step = polygon([(0, 0), (10, 0), (10, 0.5), (4, 0.5), (4, 10), (0, 10)])  # a 0.5 mm edge between two corners
    with pytest.raises(TuleError, match="could not round corners with 1"):
        step.round(1)  # it used to round four of the six and say nothing
    with pytest.raises(TuleError, match="could not chamfer corners with 6"):
        rect(20, 10).chamfer(6)  # it used to return an invalid outline (area 208 of 200)
    assert rect(20, 10).chamfer(2).area == pytest.approx(192) and step.round(0.2).area < step.area


def test_offset_grows_shrinks_and_closes_holes():
    import math

    import pytest

    from tule import TuleError, circle, rect

    assert rect(40, 20).offset(2, round=False).area == pytest.approx(44 * 24, rel=1e-6)
    assert rect(40, 20).offset(2).area == pytest.approx(44 * 24 - (4 - math.pi) * 4, rel=1e-6)
    assert rect(40, 20).offset(-2).area == pytest.approx(36 * 16, rel=1e-6)
    assert circle(10).offset(-1).area == pytest.approx(math.pi * 81, rel=1e-6)
    h = rect(40, 20) - circle(5).at(20, 10)
    assert h.offset(-1).area == pytest.approx(38 * 18 - math.pi * 36, rel=1e-6)   # the hole grows
    assert h.offset(1).area == pytest.approx(42 * 22 - (4 - math.pi) - math.pi * 16, rel=1e-6)
    assert h.offset(6).area == pytest.approx(52 * 32 - (4 - math.pi) * 36, rel=1e-6)  # the hole closes
    rim = rect(30, 30).round(5).offset(1.5) - rect(30, 30).round(5)
    assert rim.extrude(3).is_valid
    with pytest.raises(TuleError, match="leaves nothing"):
        rect(10, 4).offset(-3)


def test_union_with_a_clockwise_profile_is_one_clean_face():
    import pytest

    from tule import circle, polygon, rect

    # clockwise points make a face facing -z; it used to stay a separate face from a circle's (+z) after a union
    prof = polygon([(0, 0), (45.5, 0), (18.9, -65.8)]).offset(5) - circle(1.65)
    a, b = prof | circle(3), circle(3) | prof  # the circle covers the hole
    assert len(a.faces) == len(b.faces) == 1 and a.area == pytest.approx(b.area)
    assert len(a.extrude(3).solids) == 1
    toe = circle(6.5).at(18.9, -65.8)  # concentric with a rounded corner
    assert len((prof | toe).faces) == len((toe | prof).faces) == 1
    assert len((rect(10, 10) | rect(10, 10).mirror().at(5, 0)).faces) == 1


def test_spline_profiles_offset_and_round():
    import math

    import pytest

    from tule import TuleError, rect, spline

    t = [2 * math.pi * k / 60 for k in range(60)]
    blob = spline([(30 * math.cos(a) + 3 * math.cos(3 * a), 30 * math.sin(a)) for a in t], tol=0.1)
    assert blob.round(1.5).area == pytest.approx(blob.area)  # a spline's smooth joins are not corners
    with pytest.raises(TuleError, match="only joins lines and arcs"):
        (blob | rect(10, 40).at(25, -20)).round(1.5)  # not "no size works"
    with pytest.raises(TuleError, match="leaves nothing"):
        blob.offset(-40)


def test_offset_retries_when_opencascade_fails_on_a_spline():
    import math

    import numpy as np

    from tule import rect, spline

    rng = np.random.default_rng(1)
    for _ in range(18):  # the 18th blob of this sequence made OpenCascade's offset fail on the way back
        t = np.linspace(0, 2 * math.pi, 60, endpoint=False)
        r = 30 + sum(rng.uniform(-4, 4) * np.cos(m * t + rng.uniform(0, 6)) for m in range(2, 7))
    pts = np.c_[r * np.cos(t), r * np.sin(t)]
    p = spline(pts, tol=0.1) - spline(pts * 0.4 + 2, tol=0.1) | rect(10, 40).at(25, -20)
    q = p.offset(2).offset(-2)  # it used to say "leaves nothing"
    assert len(q.faces) == 1 and p.area <= q.area < p.area * 1.02 and q.extrude(1).is_valid
