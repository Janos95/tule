"""Sheet metal: flanges, unfolding, bend data, cut files. Numbers are checked by hand.

t = 1.5, inside radius R = 1.5, K = 0.44, 90° bends:
  outside setback OSSB = (R + t) · tan 45° = 3
  bend allowance  BA   = π/2 · (R + K·t) = π/2 · 2.16 = 3.392920
  bend deduction  BD   = 2 · OSSB − BA = 2.607080
A flat is the sum of the outside lengths less one BD per bend.
"""

import math
import os

import pytest

from tule import *

T, R, K = 1.5, 1.5, 0.44
OSSB = 3.0
BA = math.pi / 2 * 2.16
BD = 2 * OSSB - BA


def approx(a, b, tol=1e-6):
    return a == pytest.approx(b, abs=tol)


def base(w=100, h=60):
    return rect(w, h).sheet(T, bend_radius=R, k=K)


def test_hand_numbers():
    assert approx(BA, 3.392920, 1e-6) and approx(BD, 2.607080, 1e-6)
    b = Bend("B1", "up", 90, R, K, T, ((0, 0), (10, 0)))
    assert approx(b.allowance, BA) and approx(b.setback, OSSB) and approx(b.deduction, BD)
    assert b.label == "UP 90° R1.5" and b.length == 10


def test_sheet_remembers_thickness_radius_k():
    s = base()
    assert s.sheet_metal.thickness == T and s.sheet_metal.radius == R and s.sheet_metal.k == K
    assert (s - cylinder(2, 10).at(5, 5, -1)).sheet_metal == s.sheet_metal  # Booleans keep it
    assert s.at(1, 2, 3).sheet_metal.thickness == T
    assert s.scale(2).sheet_metal is None                                  # a scaled sheet is not 1.5 thick
    assert rect(10, 10).sheet(2).sheet_metal.radius == 2                   # default radius: t
    with pytest.raises(TuleError, match="bend_radius must be positive") as e:
        rect(10, 10).sheet(2, bend_radius=0)
    assert "t/2" in e.value.hint
    with pytest.raises(TuleError, match="not a sheet part"):
        box(10, 10, 2).unfold()


def test_u_channel_formed_volume_and_flat_size():
    s = base(100, 60)
    u = s.flange(s.faces.normal(-Y) | s.faces.normal(Y), 20)
    assert u.is_valid and len(u.solids) == 1
    assert approx(u.size.z, 20) and approx(u.size.y, 60)                  # outside height, outside width
    # base 54 wide (60 - 2 OSSB), two quarter bends on the mean radius R + t/2, two 17 mm flanges
    vol = 100 * T * (60 - 2 * OSSB) + 2 * math.pi / 2 * (R + T / 2) * T * 100 + 2 * 100 * T * (20 - OSSB)
    assert approx(u.volume, vol, 1e-6)
    flat = u.unfold()
    assert flat.is_valid
    assert approx(flat.size.y, 60 + 2 * 20 - 2 * BD) and approx(flat.size.y, 94.785840)
    assert approx(flat.size.x, 100) and approx(flat.size.z, T)
    lo, hi = flat.bounds
    assert approx(lo.y, OSSB - BA - (20 - OSSB)) and approx(lo.z, 0)       # the base stays where it was made
    assert approx(flat.volume, T * 100 * (54 + 2 * BA + 2 * 17))


def test_u_channel_bend_lines():
    s = base(100, 60)
    u = s.flange(s.faces.normal(-Y), 20).flange(s.faces.normal(Y), 20, angle=-90)
    flat = u.unfold()
    b1, b2 = flat.bends                                                    # reading order: top first
    assert (b1.id, b1.direction, b2.id, b2.direction) == ("B1", "down", "B2", "up")
    assert approx(b1.angle, 90) and approx(b1.radius, R) and b1.k == K
    # the center of each bend zone: BA/2 outside the tangent line, which is OSSB inside the edge
    assert approx(b2.line[0][1], OSSB - BA / 2) and approx(b2.line[1][1], OSSB - BA / 2)
    assert approx(b1.line[0][1], 60 - OSSB + BA / 2)
    assert sorted((b2.line[0][0], b2.line[1][0])) == [0, 100] and approx(b2.length, 100)
    assert [z[0][1] for z in b2.zone] == pytest.approx([OSSB, OSSB - BA])
    assert len(flat.edges["bend_up"]) == 1 and len(flat.edges["bend_down"]) == 1
    assert u.bends == flat.bends                                          # the formed part has them too
    row = flat.bend_table()[1]
    assert row == {"bend": "B2", "direction": "up", "angle": 90.0, "radius": 1.5, "k": 0.44, "thickness": 1.5,
                   "allowance": 3.393, "deduction": 2.607, "length": 100.0}


def test_tray_four_flanges_corner_reliefs():
    W, H, L = 120, 80, 20
    s = base(W, H)
    tray = s.flange(s.faces["side"], L)
    assert tray.is_valid and len(tray.solids) == 1
    assert approx(tray.size.x, W) and approx(tray.size.y, H) and approx(tray.size.z, L)
    # every flange stops at its neighbors' bends: walls W - 2 OSSB and H - 2 OSSB long
    lw, lh = W - 2 * OSSB, H - 2 * OSSB
    vol = T * lw * lh + (math.pi / 2 * (R + T / 2) * T + (L - OSSB) * T) * 2 * (lw + lh)
    assert approx(tray.volume, vol, 1e-6)
    flat = tray.unfold()
    assert approx(flat.size.x, W + 2 * L - 2 * BD) and approx(flat.size.y, H + 2 * L - 2 * BD)
    assert approx(flat.size.x, 154.785840) and approx(flat.size.y, 114.785840)
    # a cross: the corners of the blank are square notches (the corner reliefs)
    assert approx(flat.volume / T, lw * lh + (BA + L - OSSB) * 2 * (lw + lh), 1e-6)
    assert sorted(round(b.length, 6) for b in flat.bends) == [lh, lh, lw, lw]
    assert [b.direction for b in flat.bends] == ["up"] * 4
    assert [b.id for b in flat.bends] == ["B1", "B2", "B3", "B4"]
    # the left bend line: x = OSSB - BA/2, from y = OSSB to H - OSSB
    left = next(b for b in flat.bends if approx(b.line[0][0], b.line[1][0]) and b.line[0][0] < W / 2)
    assert approx(left.line[0][0], OSSB - BA / 2)
    assert sorted((left.line[0][1], left.line[1][1])) == pytest.approx([OSSB, H - OSSB])


def test_flat_is_the_same_after_moving_or_mirroring_the_part():
    s = base(100, 60)
    p = s.flange(s.faces["side"], 15)
    a = p.unfold()
    b = p.rotate(x=90, z=30).at(5, 6, 7).unfold()
    assert approx(a.size.x, b.size.x) and approx(a.size.y, b.size.y)
    assert approx(b.bends[0].line[0][1], a.bends[0].line[0][1])
    assert approx(p.mirror(1, 0, 0).unfold().volume, a.volume)


def test_45_degree_flange_deduction():
    s = base(100, 60)
    p = s.flange(s.faces.normal(-Y), 15, angle=45)
    ossb = 3 * math.tan(math.radians(22.5))
    ba = math.pi / 4 * 2.16
    b = p.unfold().bends[0]
    assert approx(b.angle, 45) and approx(b.deduction, 2 * ossb - ba) and approx(b.deduction, 0.788821)
    assert approx(p.unfold().size.y, 60 + 15 - (2 * ossb - ba))


def test_inset_flange_gets_reliefs():
    s = base(100, 60)
    for relief in ("rect", "obround"):
        p = s.flange(s.faces.normal(-Y), 15, inset=20, relief=relief)
        assert p.is_valid
        flat = p.unfold()
        (x0, _), (x1, _) = flat.bends[0].line
        assert sorted((x0, x1)) == pytest.approx([20, 80])
        notch = T * (OSSB + T / 2) if relief == "rect" else T * OSSB + math.pi * (T / 2) ** 2 / 2
        # base minus the bend zone along the flange, minus two reliefs; plus bend and flange
        area = 100 * 60 - 60 * OSSB - 2 * notch + 60 * (BA + 15 - OSSB)
        assert approx(flat.volume / T, area, 1e-5)
    p = s.flange(s.faces.normal(-Y), 15, inset=(10, 30))                   # lower x end first
    assert sorted(x for x, _ in p.unfold().bends[0].line) == pytest.approx([10, 70])


def test_return_flange_on_the_tip():
    s = base(100, 60)
    p = s.flange(s.faces.normal(-Y), 20)
    p = p.flange(p.faces["tip"], 8)                                          # folds toward the top side: inward
    assert p.is_valid and approx(p.size.y, 60) and approx(p.size.z, 20)
    flat = p.unfold()
    assert approx(flat.size.y, 60 + 20 + 8 - 2 * BD)
    assert [b.direction for b in flat.bends] == ["up", "up"]


def test_down_flanges_lid():
    s = rect(103.6, 73.6).at(-1.8, -1.8).sheet(T, R, K)
    lid = s.flange(s.faces["side"], 12, angle=-90)
    assert lid.is_valid and approx(lid.bounds[0].z, T - 12)
    flat = lid.unfold()
    assert approx(flat.size.x, 103.6 + 24 - 2 * BD)
    assert {b.direction for b in flat.bends} == {"down"}


def test_holes_before_and_after_flanging_are_in_the_flat():
    s = base(100, 60) - cylinder(3, 10).at(50, 30, -1)                     # before
    p = s.flange(s.faces.normal(-Y), 20)
    p -= cylinder(2, 10).rotate(x=90).at(30, 5, 12)                         # after, in the flange at z = 12
    flat = p.unfold()
    holes = {round(f["radius"], 6): f["center"] for f in flat.faces.cylindrical().info()}
    assert holes[3.0][:2] == pytest.approx((50, 30))
    # up the flange: from the tangent line at y = OSSB, out by BA, then 12 - OSSB along the flat flange
    assert holes[2.0][:2] == pytest.approx((30, OSSB - BA - (12 - OSSB)))
    # a hole across the bend is in the flat too (its outline mapped point by point)
    q = p - cylinder(2, 50).rotate(x=45).at(70, 0, 0)
    fq = q.unfold()
    assert fq.is_valid and fq.volume < flat.volume - 5


def _dxf(text):
    """A DXF read back by hand: header values, layer table, entities as (type, {code: [values]})."""
    lines = text.split("\n")
    pairs = [(int(c), v.strip()) for c, v in zip(lines[0::2], lines[1::2]) if c.strip()]
    header = {pairs[i][1]: pairs[i + 1][1] for i in range(len(pairs) - 1) if pairs[i][0] == 9}
    layers = {pairs[i + 1][1] for i in range(len(pairs) - 1) if pairs[i] == (0, "LAYER")}
    start = pairs.index((2, "ENTITIES"))
    ents = []
    for c, v in pairs[start + 1:]:
        if c == 0:
            ents.append((v, {}))
        else:
            ents[-1][1].setdefault(c, []).append(v)
    return header, layers, [e for e in ents if e[0] not in ("ENDSEC", "EOF")]


def _outlines(ents):
    """The closed outlines on CUT as (kind, area, layer): POLYLINE + VERTEX bulges added up exactly."""
    out, i = [], 0
    while i < len(ents):
        kind, d = ents[i]
        if kind == "CIRCLE":
            out.append(("circle", math.pi * float(d[40][0]) ** 2, d[8][0]))
        elif kind == "POLYLINE":
            assert int(d[70][0]) & 1, "an open polyline"
            vs = []
            i += 1
            while ents[i][0] == "VERTEX":
                v = ents[i][1]
                vs.append((float(v[10][0]), float(v[20][0]), float(v.get(42, [0])[0])))
                i += 1
            assert ents[i][0] == "SEQEND"
            area = 0.0
            for (x0, y0, b), (x1, y1, _) in zip(vs, vs[1:] + vs[:1]):
                area += (x0 * y1 - x1 * y0) / 2
                if b:
                    th = 4 * math.atan(abs(b))
                    r = math.hypot(x1 - x0, y1 - y0) / (2 * math.sin(th / 2))
                    area += math.copysign(r * r / 2 * (th - math.sin(th)), b)
            out.append(("poly", abs(area), d[8][0]))
        elif kind in ("LINE", "ARC") and d[8][0] == "CUT":
            out.append((kind.lower(), None, "CUT"))                       # loose cut geometry: not closed
        i += 1
    return out


def test_dxf_is_a_clean_cut_file(tmp_path):
    s = base(100, 60) - cylinder(3, 10).at(50, 30, -1)
    p = s.flange(s.faces.normal(-Y), 20).flange(s.faces.normal(Y), 20, angle=-90)
    p -= slot(30, 4).extrude(10).rotate(x=90).at(50, 5, 12)                 # a vent in the up flange
    assert "BEND" not in open(p.export_dxf(str(tmp_path / "cut.dxf"))).read()  # a portal cuts every line
    text = open(p.export_dxf(str(tmp_path / "flat.dxf"), bends=True)).read()
    header, layers, ents = _dxf(text)
    assert header["$INSUNITS"] == "4" and layers == {"CUT", "BEND_UP", "BEND_DOWN"}
    assert "TEXT" not in {k for k, _ in ents} and "%%d" not in text
    assert "LAYER\n2\nBEND_UP\n70\n0\n62\n3\n6\nDASHDOT" in text and "LTYPE\n2\nDASHDOT" in text
    kinds = [(k, d[8][0]) for k, d in ents]
    assert kinds.count(("LINE", "BEND_UP")) == 1 and kinds.count(("LINE", "BEND_DOWN")) == 1
    cut = _outlines(ents)
    assert all(a is not None for _, a, _ in cut)                              # every cut is a closed outline
    areas = sorted((a for _, a, _ in cut), reverse=True)
    assert areas[1:] == pytest.approx([26 * 4 + math.pi * 4, math.pi * 9])     # the slot (arcs as bulges), the hole
    assert areas[0] - sum(areas[1:]) == pytest.approx(p.unfold().volume / T, rel=1e-6)
    noted = open(p.export_dxf(str(tmp_path / "noted.dxf"), notes=True)).read()  # the labels, when asked for
    assert "B2 UP 90%%d R1.5" in noted and "BEND_NOTES" in noted


def test_dxf_reads_in_ezdxf(tmp_path):
    ezdxf = pytest.importorskip("ezdxf")
    s = base(100, 60)
    p = s.flange(s.faces.normal(-Y), 20) - cylinder(2, 50).rotate(x=45).at(70, 0, 0)   # a hole across the bend
    doc = ezdxf.readfile(p.export_dxf(str(tmp_path / "flat.dxf")))
    assert doc.header["$INSUNITS"] == 4
    cut = [e for e in doc.modelspace() if e.dxf.layer == "CUT"]
    assert {e.dxftype() for e in cut} == {"POLYLINE"} and all(e.is_closed for e in cut)


def test_drawing_of_the_flat(tmp_path, monkeypatch):
    monkeypatch.setenv("TULE_OUTPUTS", str(tmp_path / "outputs"))
    s = base(120, 80)
    tray = s.flange(s.faces["side"], 20) - cylinder(3, 10).at(60, 40, -1)
    folder = outputs.drawing("tray-flat", tray.unfold(), dxf="top")
    assert sorted(os.listdir(folder)) == ["output.json", "tray-flat-bends.csv", "tray-flat.dxf", "tray-flat.pdf", "tray-flat.step", "tray-flat.svg"]
    _, _, facts = drawing.render(tray.unfold())
    texts = facts["texts"]
    for i in range(1, 5):
        assert f"B{i} UP 90° R1.5" in texts
    assert "BA" in texts and "BD" in texts and "3.393" in texts and "2.607" in texts
    assert "154.79" in texts and "114.79" in texts                                # the blank
    assert not drawing.collisions(facts["pages"][0][0])
    csv = open(os.path.join(folder, "tray-flat-bends.csv")).read().splitlines()
    assert csv[0] == "bend,direction,angle,radius,k,thickness,allowance,deduction,length"
    assert csv[1].startswith("B1,up,90.0,1.5,0.44,1.5,3.393,2.607,")
    assert "LAYER\n2\nCUT" in open(os.path.join(folder, "tray-flat.dxf")).read()


def test_errors_carry_hints():
    s = base(100, 60)
    with pytest.raises(TuleError, match="too short") as e:
        s.flange(s.faces.normal(-Y), 2.5)
    assert e.value.data["min_length"] == pytest.approx(3) and "longer than 3" in e.value.hint
    narrow = rect(100, 40).sheet(T, R, K)
    p = narrow.flange(narrow.faces.normal(-Y), 40, angle=150)
    with pytest.raises(TuleError, match="overlap the part") as e:                 # two walls folded into each other
        p.flange(narrow.faces.normal(Y), 40, angle=150)
    assert "shorter flange" in e.value.hint
    strip = rect(100, 20).sheet(T, R, K)
    p = strip.flange(strip.faces.normal(-Y), 25, angle=150)
    with pytest.raises(TuleError, match="runs into the bend of an earlier flange"):
        p.flange(strip.faces.normal(Y), 25, angle=150)
    with pytest.raises(TuleError, match="where this flange's bend goes") as e:   # a hole cut before, in the bend
        (s - cylinder(1, 10).at(50, 1.5, -1)).flange(s.faces.normal(-Y), 20)
    assert "after flanging" in e.value.hint
    with pytest.raises(TuleError, match="not a sheet of uniform thickness"):
        (s.flange(s.faces.normal(-Y), 20) | box(5, 5, 5).at(50, 30, T)).unfold()
    with pytest.raises(TuleError, match="not a straight edge of the sheet"):
        s.flange(s.edges.parallel(Z), 10)
    hexagon = regular_polygon(6, 50).sheet(T, R, K)
    with pytest.raises(TuleError, match="inset"):                               # corners other than 90°
        hexagon.flange(hexagon.faces["side"], 15)
    assert len(hexagon.flange(hexagon.faces["side"], 15, inset=6).unfold().bends) == 6


def test_a_sheet_without_bends_unfolds_to_itself(tmp_path, monkeypatch):
    plate = rect(100, 110).sheet(1.6)
    flat = plate.unfold()
    assert flat.size == pytest.approx((100, 110, 1.6)) and flat.volume == pytest.approx(100 * 110 * 1.6)
    assert flat.bends == [] and plate.bend_table() == [] and flat.bend_table() == []
    lid = (rect(120, 80).sheet(1.5) - cylinder(1.7, 10).at(10, 10, -1)).rotate(x=90).at(5, 5, 50)
    assert lid.unfold().bounds[0] == pytest.approx((0, 0, 0))                 # back where the sheet was made
    monkeypatch.setenv("TULE_OUTPUTS", str(tmp_path / "outputs"))
    folder = outputs.drawing("lid", lid, dxf="top")
    assert sorted(os.listdir(folder)) == ["lid.dxf", "lid.pdf", "lid.step", "lid.svg", "output.json"]    # no bends: no bend table
    header, layers, ents = _dxf(open(os.path.join(folder, "lid.dxf")).read())
    assert layers == {"CUT"} and sorted(k for k, _, _ in _outlines(ents)) == ["circle", "poly"]
    assert sorted(a for _, a, _ in _outlines(ents)) == pytest.approx([math.pi * 1.7 ** 2, 120 * 80])


def test_a_drawing_of_a_part_that_does_not_unfold_fails_once_naming_it(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TULE_OUTPUTS", str(tmp_path / "outputs"))
    s = base(100, 60)
    bad = (s.flange(s.faces.normal(-Y), 20) | box(5, 5, 5).at(50, 30, T)).named("bracket")
    with pytest.raises(TuleError, match="sheet part 'bracket' does not unfold: this is not a sheet") as e:
        outputs.drawing("bracket", bad, dxf="top")
    assert e.value.hint and capsys.readouterr().err == ""                    # no warning before the error
    with pytest.raises(TuleError, match="'bracket' does not unfold"):
        bad.export_dxf(str(tmp_path / "b.dxf"))
    folder = outputs.drawing("bracket-3d", bad)                               # no cut file asked: a warning
    err = capsys.readouterr().err
    assert err.count("drawing without a bend table") == 1 and "'bracket'" in err
    assert sorted(os.listdir(folder)) == ["bracket-3d.pdf", "bracket-3d.step", "bracket-3d.svg", "output.json"]


def test_bend_numbers_keep_three_decimals():
    s = rect(100, 60).sheet(1.524, bend_radius=0.762, k=0.4)                 # 16 ga
    p = s.flange(s.faces.normal(-Y), 20)
    b = p.bends[0]
    assert b.label == "UP 90° R0.762"
    row = p.bend_table()[0]
    assert row["thickness"] == 1.524 and row["radius"] == 0.762
    _, _, facts = drawing.render(p.unfold())
    texts = facts["texts"]
    assert "BENDS   t 1.524   K 0.4   (BA allowance, BD deduction)" in texts and "B1 UP 90° R0.762" in texts
    assert "0.762" in texts and f"{row['allowance']:g}" in texts and f"{row['deduction']:g}" in texts


def test_cuts_in_a_formed_sheet_are_called_out_thru():
    s = base(120, 80)
    p = s.flange(s.faces.normal(-Y) | s.faces.normal(Y), 30)                 # a U: a wall behind each vent
    for i in range(3):
        p -= slot(30, 4).extrude(10).rotate(x=90).at(60, 5, 10 + 6 * i)
    _, _, facts = drawing.render(p)
    assert "3× slot 4 × 30 THRU" in facts["texts"]


def test_enclosure_end_to_end(tmp_path, monkeypatch):
    """A 1.5 mm aluminium box for a laser-cut-and-bend shop: a U base with lips, holes and vents, a flat lid."""
    monkeypatch.setenv("TULE_OUTPUTS", str(tmp_path / "outputs"))
    t, W, D, H = 1.5, 120, 80, 40
    s = rect(W, D).sheet(t, bend_radius=1.5, k=0.4)
    b = s.flange(s.faces["side"].normal(-Y) | s.faces["side"].normal(Y), H)
    b = b.flange(b.faces["tip"], 10, angle=-90)                               # mounting lips, outward
    for x in (10, W - 10):
        for y in (10, D - 10):
            b -= cylinder(1.7, 10).at(x, y, -1)
    for i in range(5):                                                        # the last one runs into a bend
        b -= slot(30, 4).extrude(10).rotate(x=90).at(W / 2, 5, 12 + 6 * i)
    lid = rect(W, D + 17).sheet(t)
    for x in (10, W - 10):
        for y in (5, D + 12):
            lid -= cylinder(1.7, 10).at(x, y, -1)
    fb, fl = b.unfold(), lid.unfold()
    assert [x.direction for x in fb.bends] == ["down", "up", "up", "down"] and fl.bends == []
    assert len(b.bend_table()) == 4 and lid.bend_table() == []
    for name, part, n_cuts in (("base", b, 1 + 4 + 5), ("lid", lid, 1 + 4)):
        flat = part.unfold()
        folder = outputs.drawing(f"{name}-flat", flat, dxf="top", info={"material": "5052-H32"})
        header, layers, ents = _dxf(open(os.path.join(folder, f"{name}-flat.dxf")).read())
        cut = _outlines(ents)
        assert header["$INSUNITS"] == "4" and "TEXT" not in {k for k, _ in ents}
        assert len(cut) == n_cuts and all(a is not None and layer == "CUT" for _, a, layer in cut)
        areas = sorted((a for _, a, _ in cut), reverse=True)
        assert areas[0] - sum(areas[1:]) == pytest.approx(flat.volume / t, rel=1e-5)
        assert (f"{name}-flat-bends.csv" in os.listdir(folder)) == bool(flat.bends)
        outputs.drawing(name, part, dxf="top")                                # the formed part: the same flat
        assert open(os.path.join(tmp_path, "outputs", name, f"{name}.dxf")).read() == \
            open(os.path.join(folder, f"{name}-flat.dxf")).read()


def test_part_and_flat_in_one_output(tmp_path, monkeypatch):
    """One script, one output: the formed part and its flat, each a sheet, both in 3D side by side, one cut file."""
    import json

    monkeypatch.setenv("TULE_OUTPUTS", str(tmp_path / "outputs"))
    s = base(120, 80)
    tray = s.flange(s.faces["side"], 20)
    folder = outputs.drawing("tray", {"tray": tray, "tray-flat": tray.unfold()}, dxf="top")
    assert sorted(os.listdir(folder)) == ["output.json", "tray-2.svg", "tray-flat-bends.csv", "tray-flat.dxf",
                                          "tray-flat.step", "tray.pdf", "tray.step", "tray.svg"]
    meta = json.load(open(os.path.join(folder, "output.json")))
    a, b = meta["parts"]
    assert meta["kind"] == "assembly" and "transform" not in a and b["transform"][3] > 120  # beside, not inside
