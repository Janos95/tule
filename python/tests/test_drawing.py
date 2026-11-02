import functools
import math
import re

import numpy as np
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
    assert "4× ⌀5.5 THRU" in t and "⌀20 THRU" in t
    assert "aluminium 6061" in t and "1:1" in t and "mm" in t and "A4" in t
    assert "Material" in t and "plate" in t and "ISO 2768-m" in t  # general tolerances by default


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
    assert sum(s.startswith("⌀20") for s in t) == 1  # given by the agent, not repeated by the automatic callout
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
    assert "⌀6.6 THRU" in t and "⌴ ⌀11 ↧6" in t  # one callout, standard form


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
    assert "80" in t and "40" in t and "35" in t and "⌀4 ↧20" in t and "stand" in t  # blind from below
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
    assert facts["scale"] == "1:1" and facts["sheet"] == "A4"  # 1:1 when it fits: 2:1 would too
    _, _, facts = drawing.render(box(30, 20, 4), views=("top",))
    assert facts["scale"] == "2:1"  # a small part is enlarged
    _, _, facts = drawing.render(box(250, 150, 6), views=("top", "front"))
    assert facts["scale"] == "1:1" and facts["sheet"] == "A3"  # the sheet grows before the scale shrinks
    _, _, facts = drawing.render(box(250, 150, 6), views=("top", "front"), sheet="A4")
    assert facts["scale"] == "1:2" and facts["sheet"] == "A4"
    with pytest.raises(TuleError, match="do not fit A4 at 5:1"):
        drawing.render(plate(), scale="5:1", sheet="A4")
    _, _, facts = drawing.render(plate(), scale="1:2", sheet="A3")
    assert facts["scale"] == "1:2" and facts["sheet"] == "A3"


@functools.cache
def motor_plate():
    """A NEMA 23 plate: 4 counterbored holes, 4 slots, a slotted center opening."""
    p = box(88, 108, 8)
    for sx in (-1, 1):
        for sy in (-1, 1):
            p -= slot(11.5, 5.5).rotate(90).extrude(10).at(44 + sx * 23.57, 60 + sy * 23.57, -1)
    p -= slot(44.6, 38.6).rotate(90).extrude(10).at(44, 60, -1)
    for x in (8, 80):
        for y in (8, 100):
            p -= cylinder(3.3, 10).at(x, y, -1)
            p -= cylinder(5.5, 8).at(x, y, 1.5)
    return p.named("motor plate")


MOTOR_DIMS = [("top", (0, 0, 8), (44, 60, 8), {"along": "h"}),
              ("top", (0, 0, 8), (44, 60, 8), {"along": "v", "tol": "±0.05"}),
              ("top", (20.43, 36.43, 8), (67.57, 36.43, 8), {"along": "h"}),
              ("top", (0, 0, 8), (8, 8, 8), {"along": "h"}),
              ("top", (0, 0, 8), (8, 8, 8), {"along": "v"}),
              ("top", (0, 0, 8), (8, 100, 8), {"along": "v"})]


def test_slots_and_counterbores_in_standard_form():
    t = texts(item=motor_plate())
    assert "4× ⌀6.6 THRU" in t and "⌴ ⌀11 ↧6.5" in t
    assert "4× slot 5.5 × 11.5 THRU" in t and "slot 38.6 × 44.6 THRU" in t
    blind = box(40, 20, 6) - slot(12, 6).extrude(3).at(20, 10, 3)
    assert "slot 6 × 12 ↧3" in texts(item=blind)
    sunk = box(30, 30, 6) - cylinder(2, 10, center=True).at(15, 15, 0) - cone(2, 2, 4).at(15, 15, 4)
    t = texts(item=sunk)
    assert "⌀4 THRU" in t and "⌵ ⌀8 × 90°" in t


def test_tolerances():
    t = texts(item=motor_plate(), dims=MOTOR_DIMS,
              tol={"⌀6.6": "+0.2/0", "Ø11": "+0.1/0", "88": 0.1, "slot 5.5": "+0.1/0", "depth 6.5": "±0.1",
                   "47.14": "H7"})
    assert "4× ⌀6.6 +0.2/0 THRU" in t and "⌴ ⌀11 +0.1/0 ↧6.5 ±0.1" in t
    assert "88 ±0.1" in t and "60 ±0.05" in t and "47.14 H7" in t
    assert "4× slot 5.5 +0.1/0 × 11.5 THRU" in t
    t = texts(item=plate(), dims=[("top", plate().edges.circles(r=10), {"tol": "H7"})])
    assert "⌀20 H7 THRU" in t
    with pytest.raises(TuleError, match="nothing on the drawing is '⌀6.5'") as e:
        texts(item=motor_plate(), tol={"⌀6.5": 0.1})
    assert "⌀6.6" in e.value.hint  # the sizes it could have meant
    with pytest.raises(TuleError, match="cannot read tolerance"):
        texts(item=plate(), tol={"100": "loose"})
    assert drawing._tol("0/-0.1") == ("dev", "0", "-0.1") and drawing._tol("+0.1/-0.1") == ("pm", "0.1")


def test_title_block_fields_revisions_and_notes():
    _, _, facts = drawing.render(
        plate(), info={"number": "MP-1", "material": "6061-T6", "finish": "Clear anodize", "drawn": "JM",
                       "date": "2026-11-02", "batch": 20},
        notes=["Break all edges 0.3", "Anodize clear, MIL-A-8625 Type II"],
        revisions=[("A", "First issue"), ("B", "Slots 2 mm longer", "2026-11-02")])
    t = facts["texts"]
    for s in ("Part number", "MP-1", "6061-T6", "Finish", "Clear anodize", "JM", "2026-11-02", "Batch", "20",
              "General tolerances", "ISO 2768-m", "NOTES", "1.", "Break all edges 0.3", "Slots 2 mm longer"):
        assert s in t, s
    assert facts["rev"] == "B" and "B" in t
    t = texts(item=plate(), info={"rev": "C", "tolerance": "ISO 2768-f"})
    assert "C" in t and "ISO 2768-f" in t and "ISO 2768-m" not in t
    assert "General tolerances" not in texts(item=plate(), info={"tolerance": ""})
    with pytest.raises(TuleError, match="not the last revision"):
        texts(item=plate(), info={"rev": "A"}, revisions=[("A", "x"), ("B", "y")])


def test_no_text_touches_a_line_or_another_text():
    kw = dict(dims=MOTOR_DIMS, info={"number": "MP-23", "material": "6061-T6", "rev": "B"},
              notes=["Break all edges 0.3", "Anodize clear"], tol={"⌀6.6": "+0.2/0", "88": "±0.1"})
    for sheet in ("A4", "A3"):
        page, _, facts = drawing.render(motor_plate(), sheet=sheet, **kw)
        assert drawing.collisions(page) == [], sheet
        assert facts["scale"] == {"A4": "1:2", "A3": "1:1"}[sheet]
    base = box(80, 40, 5).named("base")
    post = (box(10, 10, 30).at(35, 15, 5) - cylinder(2, 50, center=True).at(40, 20, 0)).named("post")
    for item in (plate(), Assembly(base, post, name="stand")):
        assert drawing.collisions(drawing.render(item)[0]) == []
    # and the check itself sees one
    c = drawing._Canvas()
    c.line("vis", (0, 0), (20, 0))
    c.text(5, -1, "88")
    assert drawing.collisions(c) and not drawing.collisions(drawing._Canvas())


def test_grouped_name_and_drawn_symbols(outdir):
    folder = outputs.drawing("drawings/motor-plate", motor_plate())
    path = outdir / "drawings" / "motor-plate" / "motor-plate.svg"
    assert folder == str(path.parent) and path.exists()
    t = svg_texts(path)
    assert "4× ⌀6.6 THRU" in t and "⌀11" in t  # "⌴" and "↧" are strokes: Helvetica has no such glyphs
    assert not any("⌴" in s or "↧" in s for s in t)


@functools.cache
def split_clamp():
    """A block split by a slit from the top: a clearance hole in the left ear ends in the slit, a blind tapped
    hole in the right ear, and two vertical through holes of the clearance hole's size."""
    p = box(40, 20, 30) - box(1, 22, 21).at(19.5, -1, 10)
    p -= cylinder(2.25, 21).rotate(y=90).at(-1, 10, 24)    # x -1 .. 20: into the slit
    p -= cylinder(1.6, 12).rotate(y=90).at(20.5, 10, 24)   # x 20.5 .. 32.5: blind
    for x in (8, 32):
        p -= cylinder(2.25, 32).at(x, 4, -1)
    return p.named("clamp")


CLAMP_VIEWS = ("front", "top", "left", "right")


def test_thru_only_when_both_ends_are_open():
    t = texts(item=split_clamp(), views=CLAMP_VIEWS)
    assert "2× ⌀4.5 THRU" in t
    assert any(s.startswith("⌀4.5 ↧") for s in t)    # the clearance hole ends in the slit: its depth, not THRU
    assert any(s.startswith("⌀3.2 ↧") for s in t)    # the tapped hole is blind


def test_a_picked_feature_gets_its_own_callout_and_tolerance():
    c = split_clamp()
    # by printed text the tolerance would land on two different features: an error, not a guess
    with pytest.raises(TuleError, match="different callouts") as e:
        texts(item=c, views=CLAMP_VIEWS, tol={"⌀4.5": "+0.1/0"})
    assert "dims" in e.value.hint
    clear = c.faces.cylindrical(r=2.25)  # the mounting holes match too: the ones on the view's axis are taken
    t = texts(item=c, views=CLAMP_VIEWS, dims=[("left", clear, {"tol": "+0.1/0"})])
    assert any(s.startswith("⌀4.5 +0.1/0 ↧") for s in t)
    assert "2× ⌀4.5 THRU" in t                        # the mounting holes keep theirs, untoleranced
    t = texts(item=c, views=CLAMP_VIEWS, dims=[("right", c.faces.cylindrical(r=1.6), {"text": "M4×0.7-6H ↧12"})])
    assert "M4×0.7-6H ↧12" in t and not any(s.startswith("⌀3.2") for s in t)
    with pytest.raises(TuleError, match="feature options"):
        texts(item=c, views=CLAMP_VIEWS, dims=[("left", clear, {"along": "h"})])
    with pytest.raises(TuleError, match="side is one of"):
        texts(item=c, views=CLAMP_VIEWS, dims=[("left", clear, {"side": "up"})])


def test_callout_side_can_be_chosen():
    c = split_clamp()
    ys = {}
    for side in ("top", "bottom"):
        page, _, _ = drawing.render(c, views=CLAMP_VIEWS, dims=[("left", c.faces.cylindrical(r=2.25), {"side": side})])
        ys[side] = next(b for t, b in page.labels if t.startswith("⌀4.5 ↧"))[1]
        assert drawing.collisions(page) == []
    assert abs(ys["top"] - ys["bottom"]) > 30  # the whole view between them
    page, _, _ = drawing.render(c, views=CLAMP_VIEWS, callouts={"top": "bottom"})
    assert drawing.collisions(page) == []
    for bad in ("middle", {"iso": "top"}, {"top": "up"}, 3):
        with pytest.raises(TuleError, match="callouts"):
            texts(item=c, views=CLAMP_VIEWS, callouts=bad)


def test_leaders_never_cross():
    p = box(120, 80, 6)
    for x, y, r in [(10, 10, 2), (30, 15, 3), (50, 40, 4), (70, 20, 2.5), (90, 60, 3.5),
                    (110, 70, 2), (20, 60, 1.5), (60, 70, 2.2), (100, 30, 4.5)]:
        p -= cylinder(r, 20, center=True).at(x, y, 0)
    page, _, _ = drawing.render(p.named("perforated"), views=("top", "front"))
    assert len(page.leaders) == 8  # the two ⌀4 share a callout
    assert drawing.collisions(page) == []
    c = drawing._Canvas()  # and the check sees two that cross
    c.leaders += [(0, 0, 10, 10), (0, 10, 10, 0)]
    assert any("crosses" in s for s in drawing.collisions(c))


def test_section_view():
    c = split_clamp()
    page, size, facts = drawing.render(c, views=(("front", (20, 10, 0)), "top", "right"))
    t = facts["texts"]
    assert "A-A" in t and t.count("A") == 2      # the title, and the letters at the ends of the cut line
    assert drawing.collisions(page) == []
    def thin(page):
        return sum(op[0] == "path" and op[1] == "thin" for op in page.ops)
    assert thin(page) > thin(drawing.render(c, views=("front", "top", "right"))[0]) + 50  # the hatching
    t = texts(item=c, views=(("front", c.faces.cylindrical(r=1.6)), ("right", (30, 10, 0)), "top"))
    assert "A-A" in t and "B-B" in t
    with pytest.raises(TuleError, match="cuts no material"):
        texts(item=c, views=(("front", (20, -50, 0)), "top"))
    with pytest.raises(TuleError, match="a section is a view and a point"):
        texts(item=c, views=(("iso", (0, 0, 0)), "top"))


def test_scale_hint_names_scales_that_fit():
    with pytest.raises(TuleError, match="do not fit A4 at 2:1") as e:
        drawing.render(box(140, 60, 6), views=("top",), scale="2:1", sheet="A4")
    assert 'scale="1.5:1" is the largest that fits' in e.value.hint and "292 × 130 mm" in e.value.hint  # and why
    assert 'sheet="A3"' in e.value.hint
    assert drawing.render(box(140, 60, 6), views=("top",), scale="1.5:1", sheet="A4")[2]["scale"] == "1.5:1"
    assert drawing.render(box(140, 60, 6), views=("top",), scale="2:1")[2]["sheet"] == "A3"  # the sheet to suit


@functools.cache
def gdt_plate():
    """A 90 × 70 × 10 plate: a ⌀30 bore, four ⌀5.5 holes, two 5.5 × 15.5 slots."""
    p = box(90, 70, 10) - cylinder(15, 30, center=True).at(45, 40, 0)
    for x in (12, 78):
        for y in (12, 58):
            p -= cylinder(2.75, 30, center=True).at(x, y, 0)
        p -= slot(15.5, 5.5).extrude(30).at(x, 35, -10)
    return p.named("plate_v3_internal")


def gdt_kw(p, **kw):
    return dict(title="Bore plate", datums={"A": p.faces.bottom(), "B": p.faces.normal((-1, 0, 0)).largest()},
                dims=[(p.faces.top(), {"frame": "flatness 0.05"}),
                      (p.faces.cylindrical(r=15), {"tol": "H7", "frame": ["perpendicular ⌀0.02 A",
                                                                         "position ⌀0.05 A B"]}),
                      (p.faces.cylindrical(r=2.75).within((0, 0, -1), (90, 25, 11)), {"frame": "position ⌀0.1 M A B"}),
                      ("top", (0, 0, 10), (45, 40, 10), {"along": "h", "tol": "basic"})], **kw)


def test_gdt_frames_datums_and_basic_dimensions(outdir):
    p = gdt_plate()
    page, _, facts = drawing.render(p, **gdt_kw(p))
    for f in ("datum A", "datum B", "⏥ | 0.05", "⊥ | ⌀0.02 | A", "⌖ | ⌀0.05 | A | B", "⌖ | ⌀0.1 Ⓜ | A | B"):
        assert f in facts["frames"], f
    t = facts["texts"]
    assert "⌀0.1 Ⓜ" in t and "⌀0.05" in t and t.count("A") >= 4 and "[45]" in t  # the basic size, boxed
    assert drawing.collisions(page) == []
    outputs.drawing("gdt", p, **gdt_kw(p))
    s = svg_texts(outdir / "gdt" / "gdt.svg")
    assert "⌀0.02" in s and "B" in s and not any("⌖" in x or "⊥" in x for x in s)  # symbols are strokes
    pdf = (outdir / "gdt" / "gdt.pdf").read_bytes()
    assert b"/Title (Bore plate)" in pdf
    with pytest.raises(TuleError, match="datum"):
        texts(item=p, dims=[(p.faces.top(), {"frame": "parallel 0.05 A"})])  # no datum A on the drawing
    with pytest.raises(TuleError, match="flatness"):
        drawing._Frame("flatness 0.05 A")  # a form tolerance takes no datum
    assert drawing._Frame("position Ø0.1 (M) A B C").name == "⌖ | ⌀0.1 Ⓜ | A | B | C"


def test_slot_width_and_length():
    p = gdt_plate()
    t = texts(item=p, dims=[(p.faces.cylindrical(r=2.75).within((5, 25, -1), (85, 45, 11)),
                              {"width": "+0.1/0", "length": True})])
    assert "5.5 +0.1/0" in t and "15.5" in t and "2× slot THRU" in t


def test_sheets_call_each_feature_out_once(outdir):
    p = gdt_plate()
    sheets = [{"views": ("front", "top")}, {"views": (("front", (45, 40, 5)), "iso"), "callouts": "none"}]
    page, _, facts = drawing.render(p, sheets=sheets, **gdt_kw(p))
    assert len(facts["pages"]) == 2 and len(facts["sheets"]) == 2
    t = facts["texts"]
    assert t.count("⌀30 H7 THRU") == 1 and sum(s.startswith(("4× ⌀5.5", "2× ⌀5.5")) for s in t) == 2
    p2 = [s for s, _ in facts["pages"][1][0].labels]
    assert "C-C" in p2 and not any(s.startswith("⌀") or "THRU" in s for s in p2)  # sheet 2: no callouts
    assert "1/2" in " ".join(s for s, _ in page.labels)  # the sheet number in the title block
    for pg, _ in facts["pages"]:
        assert drawing.collisions(pg) == []
    outputs.drawing("two", p, sheets=sheets, **gdt_kw(p))
    pdf = (outdir / "two" / "two.pdf").read_bytes()
    assert b"/Count 2" in pdf and b"/Title (Bore plate)" in pdf
    assert (outdir / "two" / "two.svg").exists() and (outdir / "two" / "two-2.svg").exists()
    outputs.drawing("two", p, sheets=sheets, **gdt_kw(p))
    assert (outdir / "two" / "two.pdf").read_bytes() == pdf  # deterministic
    with pytest.raises(TuleError, match="sheet 1 is a dict with views"):
        texts(item=p, sheets=[{"view": ("top",)}])


def test_no_leader_crosses_a_dimension_line():
    p = gdt_plate()
    page, _, _ = drawing.render(p, **gdt_kw(p, sheet="A3"))
    assert page.leaders and page.dimsegs
    for L in page.leaders:
        assert not drawing._cross(L[:2], L[2:], page.dimsegs), L
        assert not drawing._cross(L[:2], L[2:], [M for M in page.leaders if M != L]), L


def test_title_wins_over_the_shape_name():
    p = gdt_plate()
    t = texts(item=p, title="Bore plate")
    assert "Bore plate" in t and "plate_v3_internal" not in " ".join(t)
    t = texts(item=p, info={"part": "BP-7 bore plate"})
    assert "BP-7 bore plate" in t and "plate_v3_internal" not in " ".join(t)


def test_concave_radius_callout():
    blk = box(80, 40, 30) - rect(40, 16, center=True).round(3).extrude(14).rotate(x=-90).at(40, -1, 15)
    r3 = blk.faces.cylindrical(r=3)
    t = texts(item=blk, views=("front", "top", "right"), dims=[("front", r3)])
    assert any(s.startswith("4× R3 ↧") for s in t)
    t = texts(item=blk, views=("front", "top", "right"), dims=[("front", r3, {"text": "SIDE POCKETS: R3, DEPTH 14"})])
    assert "SIDE POCKETS: R3, DEPTH 14" in t


def _gap(x0, y0, x1, y1, box):
    """How far a text's box is from a dimension line (0 when it touches it)."""
    cx, cy, cs, sn, hw, hh = box
    a, b = np.array([x0, y0]), np.array([x1, y1])

    def local(p):  # in the box's frame
        d = p - (cx, cy)
        return np.array([d @ (cs, sn), d @ (-sn, cs)])

    def to_seg(p):
        d = b - a
        t = 0.0 if d @ d == 0 else np.clip((p - a) @ d / (d @ d), 0, 1)
        return float(np.linalg.norm(p - a - t * d))

    if any(abs(q[0]) <= hw and abs(q[1]) <= hh for q in (local(a), local(b))):
        return 0.0
    corners = [np.array([cx, cy]) + u * np.array([cs, sn]) + v * np.array([-sn, cs])
               for u in (-hw, hw) for v in (-hh, hh)]
    return min(to_seg(q) for q in corners)


def test_dimension_text_stays_by_its_line():
    p = box(60, 30, 8)
    for x in (6, 11, 16, 21, 26):
        p -= cylinder(1.25, 20, center=True).at(x, 8, 0)
    p -= slot(10, 3).extrude(20).at(40, 8, -5)
    for x in (36, 50):
        p -= cylinder(1.7, 20, center=True).at(x, 22, 0) + cylinder(3, 3.5).at(x, 22, 4.5)
    xs = (0, 6, 11, 16, 21, 26)
    dims = [("top", (a, 8, 8), (b, 8, 8), {"side": "bottom"}) for a, b in zip(xs, xs[1:])]
    dims += [("top", (0, 0, 8), (6, 8, 8)), ("top", (6, 8, 8), (36, 22, 8)), ("top", (11, 8, 8), (36, 22, 8)),
             ("top", (36, 22, 8), (50, 22, 8)), ("top", (35, 8, 8), (45, 8, 8), {"side": "top"}),
             ("top", (0, 0, 8), (0, 8, 8)), ("top", (0, 8, 8), (0, 22, 8)), ("top", (0, 0, 8), (0, 22, 8)),
             ("front", (0, 0, 0), (0, 0, 4.5)), ("front", (0, 0, 4.5), (0, 0, 8))]
    page, _, _ = drawing.render(p.named("crowded"), views=("front", "top"), dims=dims)
    placed = page.dimtexts
    assert len(placed) >= len(dims)
    for x0, y0, x1, y1, b in placed:  # on its line, or just beyond an arrow
        assert _gap(x0, y0, x1, y1, b) <= 10.5, (x0, y0, x1, y1)
    boxes = [b for *_, b in placed]
    for i, b in enumerate(boxes):
        assert not any(drawing._box_hit(b, o) for o in boxes[:i])
    assert drawing.collisions(page) == []


def test_dimension_label_follows_the_value():
    p = box(40, 20, 5) - cylinder(2, 20, center=True).at(10, 10, 0) - cylinder(2, 20, center=True).at(30, 10, 0)
    t = texts(item=p, views=("top",), dims=[("top", (0, 0, 5), (2.857, 0, 5), {"label": "ROOT"}),
                                           ("top", (0, 20, 5), (40, 20, 5), {"label": "REF", "tol": "±0.1"}),
                                           ("top", (0, 0, 5), (0, 20, 5), {"text": "W", "label": "MAX"}),
                                           (p.faces.cylindrical(r=2), {"label": "REAM"})])
    assert {"2.86 ROOT", "40 ±0.1 REF", "W MAX", "2× ⌀4 THRU REAM"} <= set(t)
    with pytest.raises(TuleError, match="text, label, along"):
        texts(item=p, dims=[("top", (0, 0, 5), (1, 0, 5), {"suffix": "X"})])


def test_several_parts_and_a_table_sheet(outdir):
    def bracket(L):
        p = box(L, 20, 4)
        for x in (6, L - 6):
            p -= cylinder(1.7, 20, center=True).at(x, 10, 0)
        return p

    parts = {f"B-{L}": bracket(L) for L in (40, 60)}
    rows = [{"Part": k, "L": v.bounds[1][0]} for k, v in parts.items()]
    outputs.drawing("family", parts, views=("front", "top"), title="Bracket family", dxf="top",
                    dims={"B-60": [("top", (0, 10, 4), (6, 10, 4), {"label": "TYP"})]},
                    info={"B-40": {"number": "B-40-01"}, "B-60": {"number": "B-60-01"}},
                    notes=["Print flat"], table=rows)
    d = outdir / "family"
    assert sorted(x.name for x in d.iterdir()) == ["family-2.svg", "family-3.svg", "family-B-40.dxf",
                                                   "family-B-60.dxf", "family.pdf", "family.svg"]
    pdf = (d / "family.pdf").read_bytes()
    assert b"/Count 3" in pdf and b"/Title (Bracket family)" in pdf
    s1, s2, s3 = (svg_texts(d / f) for f in ("family.svg", "family-2.svg", "family-3.svg"))
    assert "B-40" in s1 and "B-40-01" in s1 and "A4  1/3" in s1 and "NOTES" not in s1
    assert "B-60" in s2 and "6 TYP" in s2 and "B-60-01" in s2
    assert {"Part", "L", "B-40", "60", "NOTES", "Print flat", "Bracket family", "A4  3/3"} <= set(s3)
    # one part with a table: two sheets, the notes on the second
    _, _, f = drawing.render(plate(), notes=["Deburr"], table=[["Size", "Qty"], ["M5", 4]])
    assert len(f["pages"]) == 2 and "Deburr" in [t for t, _ in f["pages"][1][0].labels]
    assert "Deburr" not in [t for t, _ in f["pages"][0][0].labels]
    with pytest.raises(TuleError, match="'B-99' is no part"):
        drawing.render(parts, dims={"B-99": []})
    with pytest.raises(TuleError, match="table is a list"):
        drawing.render(plate(), table="M5")
