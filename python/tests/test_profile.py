

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
