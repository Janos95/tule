import functools
import math
import re

import pytest

from tule import *
from tule import drawing


@pytest.fixture
def outdir(tmp_path, monkeypatch):
    d = tmp_path / "outputs"
    monkeypatch.setenv("TULE_OUTPUTS", str(d))
    return d


@functools.cache
def plate():  # shapes are values: build it once
    p = box(100, 60, 6)
    for x in (10, 90):
        for y in (10, 50):
            p -= cylinder(2.75, 20, center=True).at(x, y, 0)
    p -= cylinder(10, 20, center=True).at(50, 30, 0)
    return p.named("plate")


def texts(**kw):
    item = kw.pop("item", None) or plate()
    _, _, facts = drawing.render(item, **kw)
    return facts["texts"]


def svg_texts(path):
    return re.findall(r"<text[^>]*>([^<]*)</text>", path.read_text())


def test_plate_drawing_files_and_dims(outdir):
    folder = outputs.drawing("plate-drawing", plate(), info={"material": "aluminium 6061"})
    names = sorted(p.name for p in (outdir / "plate-drawing").iterdir())
    assert folder == str(outdir / "plate-drawing")
    assert names == ["plate-drawing.pdf", "plate-drawing.svg"]
    t = svg_texts(outdir / "plate-drawing" / "plate-drawing.svg")
    # overall sizes, each once, and the holes as callouts with their count
    assert t.count("100") == 1 and t.count("60") == 1 and t.count("6") == 1
    assert "4× ⌀5.5" in t and "⌀20" in t
    assert "aluminium 6061" in t and "1:1" in t and "mm" in t and "A4" in t
    assert "Material" in t and "plate" in t


def test_hidden_lines_present_or_dropped():
    page, size, _ = drawing.render(plate())
    with_hidden = drawing.to_svg(page, size)
    page, size, _ = drawing.render(plate(), hidden=False)
    without = drawing.to_svg(page, size)
    assert 'stroke-dasharray="2 1"' in with_hidden
    assert 'stroke-dasharray="2 1"' not in without
    v = drawing._View("front")
    drawing._hlr(plate().wrapped, v, True, 1e-4)
    # the holes are hidden in the front view: vertical dashed lines at their walls
    xs = sorted({round(p[1], 3) for p in v.hid if p[0] == "line" and abs(p[1] - p[3]) < 1e-9})
    assert xs == [7.25, 12.75, 40.0, 60.0, 87.25, 92.75]


def test_deterministic_bytes(outdir):
    a = outputs.drawing("d", plate(), dxf="top")
    first = {p.name: p.read_bytes() for p in (outdir / "d").iterdir()}
    outputs.drawing("d", plate(), dxf="top")
    second = {p.name: p.read_bytes() for p in (outdir / "d").iterdir()}
    assert sorted(first) == ["d.dxf", "d.pdf", "d.svg"]
    assert first == second
    assert a == str(outdir / "d")


def test_pdf_is_well_formed(outdir):
    outputs.drawing("p", plate())
    pdf = (outdir / "p" / "p.pdf").read_bytes()
    assert pdf.startswith(b"%PDF-1.4\n") and pdf.endswith(b"%%EOF\n")
    startxref = int(re.search(rb"startxref\n(\d+)\n%%EOF", pdf).group(1))
    assert pdf[startxref:startxref + 4] == b"xref"
    lines = pdf[startxref:].split(b"\n")
    n = int(lines[1].split()[1])
    entries = lines[2:2 + n]
    assert all(len(e) == 19 for e in entries)  # 20 bytes with the newline
    for i, e in enumerate(entries[1:], 1):
        off = int(e[:10])
        assert pdf[off:].startswith(f"{i} 0 obj".encode())
    assert b"/Size %d" % n in pdf and b"/Helvetica" in pdf
    assert b"CreationDate" not in pdf


def test_agent_dims_project_into_the_view():
    p = plate()
    t = texts(item=p, dims=[
        ("top", (0, 0, 6), (10, 10, 6), {"along": "h"}),          # 10 along x
        ("top", (0, 0, 6), (37.5, 12, 6), {"along": "v"}),        # 12 along y
        ("front", (0, 0, 0), (37.5, 40, 6)),                      # z is up in front: 37.5 and 6 -> aligned
        ("right", (0, 0, 0), (0, 25.25, 0)),                      # y is across in the right view
        ("top", (0, 0, 0), (100, 60, 0), "L1"),                   # text override
        ("top", p.edges.circles(r=10)),                           # a diameter on a given circle
    ])
    assert "10" in t and "12" in t and "25.25" in t and "L1" in t
    assert _fmt_check(math.hypot(37.5, 6)) in t
    assert t.count("⌀20") == 1  # given by the agent, not repeated by the automatic callout
    with pytest.raises(TuleError):
        texts(item=p, dims=[("left", (0, 0, 0), (1, 0, 0))])  # a view that is not drawn
    with pytest.raises(TuleError):
        texts(item=p, dims=[("front", p.edges.circles(r=10))])  # not a circle in the front view


def _fmt_check(v):
    return f"{round(v, 2):.2f}".rstrip("0").rstrip(".")


def test_projection_matches_view_coordinates():
    part = box(40, 20, 6).at(10, 5, 2)
    for name, (x, y) in {"front": ((10, 50), (2, 8)), "top": ((10, 50), (5, 25)), "right": ((5, 25), (2, 8))}.items():
        v = drawing._View(name)
        drawing._hlr(part.wrapped, v, False, 1e-4)
        bb = v.bbox()
        assert bb == pytest.approx((x[0], y[0], x[1], y[1]))
        assert v.p((10, 5, 2)) == pytest.approx((bb[0], bb[1]))


def test_counterbore_depth_and_round_part():
    f = cylinder(30, 12) - cylinder(3.3, 50, center=True)
    f -= cylinder(5.5, 6).at(0, 0, 6)
    t = texts(item=f.named("flange"))
    assert "⌀60" in t and "12" in t
    assert "⌀11 depth 6" in t and "⌀6.6" in t


def test_dxf_has_circles_where_the_holes_are(tmp_path):
    path = drawing.dxf(plate(), str(tmp_path / "plate.dxf"), view="top")
    text = open(path).read()
    assert "AC1009" in text and text.endswith("0\nEOF\n")
    codes = text.split("\n")
    pairs = list(zip(codes[0::2], codes[1::2]))
    circles = []
    for i, (c, v) in enumerate(pairs):
        if (c, v) == ("0", "CIRCLE"):
            d = dict(pairs[i + 1:i + 6])
            circles.append((float(d["10"]), float(d["20"]), float(d["40"])))
    assert sorted(circles) == [(10, 10, 2.75), (10, 50, 2.75), (50, 30, 10), (90, 10, 2.75), (90, 50, 2.75)]
    assert sum(1 for p in pairs if p == ("0", "LINE")) == 4


def test_assembly_and_list(outdir):
    base = box(80, 40, 5).named("base")
    post = (box(10, 10, 30).at(35, 15, 5) - cylinder(2, 50, center=True).at(40, 20, 0)).named("post")
    t = texts(item=Assembly(base, post, name="stand"))
    assert "80" in t and "40" in t and "35" in t and "⌀4 depth 20" in t and "stand" in t  # blind from below
    outputs.drawing("stand", [base, post], views=("front", "top"), projection="third", scale="1:2")
    t = svg_texts(outdir / "stand" / "stand.svg")
    assert "1:2" in t and "35" in t


def test_polygonal_hidden_lines(monkeypatch):
    monkeypatch.setattr(drawing, "EXACT_FACES", 0)
    t = texts(item=plate())
    assert "100" in t and "60" in t


def test_bad_arguments():
    with pytest.raises(TuleError):
        texts(views=("front", "side"))
    with pytest.raises(TuleError):
        texts(sheet="Letter")
    with pytest.raises(TuleError):
        texts(projection="second")
    with pytest.raises(TuleError):
        texts(dims=[("top", (0, 0, 0), (1, 1, 1), {"angle": 3})])


def test_scale_is_chosen_or_checked():
    _, _, facts = drawing.render(plate(), views=("top",))
    assert facts["scale"] == "2:1"  # 200 x 120 fits A4, 500 x 300 does not
    with pytest.raises(TuleError, match="do not fit A4 at 5:1"):
        drawing.render(plate(), scale="5:1")
    _, _, facts = drawing.render(plate(), scale="1:2", sheet="A3")
    assert facts["scale"] == "1:2" and facts["sheet"] == "A3"
