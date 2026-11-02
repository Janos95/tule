import pytest

from tule import *
def test_iterating_gives_one_item_selections():
    b = box(10, 10, 10)
    ids = [f.id for f in b.faces]
    assert ids == b.faces.ids and all(len(f) == 1 for f in b.faces)
    with pytest.raises(TuleError):
        b.faces.id
