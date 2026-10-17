import ot
def test_apply():
    assert ot.apply("ab", [{"ins": 1, "text": "x"}]) == "axb"
