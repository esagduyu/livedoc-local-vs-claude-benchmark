import random
import string

from livedoc_ot import apply_op, fits, is_wellformed, xf, xf_edit


def ins(p, t):
    return {"ins": p, "text": t}


def dele(p, n):
    return {"del": p, "len": n}


def test_ins_vs_ins_shifts_later_position():
    assert xf_edit(ins(1, "x"), ins(3, "yy"), False) == [ins(1, "x")]
    assert xf_edit(ins(4, "x"), ins(3, "yy"), False) == [ins(6, "x")]


def test_ins_vs_ins_tie_earlier_insert_stays_left():
    # The later-committed edit (a) shifts past the earlier one.
    assert xf_edit(ins(2, "x"), ins(2, "yy"), False) == [ins(4, "x")]
    # Moving the earlier edit past the later one leaves it where it is.
    assert xf_edit(ins(2, "yy"), ins(2, "x"), True) == [ins(2, "yy")]


def test_ins_vs_del():
    # del [3, 6): position before, at the start, and at the end of the range
    assert xf_edit(ins(2, "x"), dele(3, 3), False) == [ins(2, "x")]
    assert xf_edit(ins(3, "x"), dele(3, 3), False) == [ins(3, "x")]
    assert xf_edit(ins(6, "x"), dele(3, 3), False) == [ins(3, "x")]
    assert xf_edit(ins(8, "x"), dele(3, 3), False) == [ins(5, "x")]
    # strictly inside: collapses to the deletion point
    assert xf_edit(ins(4, "x"), dele(3, 3), False) == [ins(3, "x")]


def test_del_vs_ins_before_shifts_by_insert_length():
    assert xf_edit(dele(5, 2), ins(1, "abc"), False) == [dele(8, 2)]
    assert xf_edit(dele(5, 2), ins(5, "abc"), False) == [dele(8, 2)]


def test_del_vs_ins_after_is_unchanged():
    assert xf_edit(dele(5, 2), ins(7, "abc"), False) == [dele(5, 2)]
    assert xf_edit(dele(5, 2), ins(9, "abc"), False) == [dele(5, 2)]


def test_del_vs_ins_inside_splits_and_keeps_inserted_text():
    # text "0123456789"; b inserts "XYZ" at 7 inside the deletion [5, 9)
    text = "0123456789"
    b = ins(7, "XYZ")
    a_prime = xf_edit(dele(5, 4), b, False)
    assert a_prime == [dele(5, 2), dele(8, 2)]
    result = apply_op(apply_op(text, [b]), a_prime)
    assert result == "01234XYZ9"


def test_del_vs_del_disjoint_and_contained():
    assert xf_edit(dele(0, 2), dele(5, 2), False) == [dele(0, 2)]
    assert xf_edit(dele(5, 2), dele(0, 2), False) == [dele(3, 2)]
    # b deletes all of a
    assert xf_edit(dele(3, 2), dele(1, 6), False) == []
    # b deletes the middle of a: remaining pieces become one range
    assert xf_edit(dele(1, 6), dele(3, 2), False) == [dele(1, 4)]


def test_del_vs_del_partial_overlap():
    # a = [2, 6), b = [4, 9): a keeps [2, 4) -> one range at 2
    assert xf_edit(dele(2, 4), dele(4, 5), False) == [dele(2, 2)]
    # a = [6, 9), b = [4, 8): a keeps [8, 9) -> after b it sits at 4
    assert xf_edit(dele(6, 3), dele(4, 4), False) == [dele(4, 1)]


def test_xf_empty_operations_pass_through():
    assert xf([], [ins(0, "a")]) == ([], [ins(0, "a")])
    assert xf([ins(0, "a")], []) == ([ins(0, "a")], [])


def test_xf_multi_edit_example():
    text = "abcdef"
    a = [ins(0, "X"), dele(3, 2)]    # "abcdef" -> "Xabcdef" -> "Xabef" (drops c, d)
    b = [dele(1, 1), ins(4, "Y")]    # "abcdef" -> "acdef" -> "acdeYf" (drops b, adds Y)
    a2, b2 = xf(a, b)
    left = apply_op(apply_op(text, b), a2)
    right = apply_op(apply_op(text, a), b2)
    assert left == right == "XaeYf"


def _random_text(rng):
    return "".join(rng.choice("abé\U0001F600xyz") for _ in range(rng.randint(0, 12)))


def _random_op(rng, text):
    op, length = [], len(text)
    for _ in range(rng.randint(0, 3)):
        if length > 0 and rng.random() < 0.5:
            pos = rng.randint(0, length - 1)
            n = rng.randint(1, length - pos)
            op.append(dele(pos, n))
            length -= n
        else:
            pos = rng.randint(0, length)
            t = "".join(rng.choice(string.ascii_letters) for _ in range(rng.randint(1, 4)))
            op.append(ins(pos, t))
            length += len(t)
    return op


def test_tp1_random_operations_converge():
    """Both orders of two concurrent operations give the same text (TP1)."""
    rng = random.Random(1234)
    for _ in range(3000):
        text = _random_text(rng)
        a = _random_op(rng, text)
        b = _random_op(rng, text)
        assert is_wellformed(a) and fits(a, len(text))
        assert fits(b, len(text))
        a2, b2 = xf(a, b)
        assert fits(b2, len(apply_op(text, a)))
        assert fits(a2, len(apply_op(text, b)))
        assert apply_op(apply_op(text, b), a2) == apply_op(apply_op(text, a), b2), (text, a, b)


def test_is_wellformed_and_fits():
    assert is_wellformed([])
    assert is_wellformed([ins(0, "x"), dele(0, 1)])
    assert not is_wellformed({"ins": 0, "text": "x"})
    assert not is_wellformed([{"ins": 0}])
    assert not is_wellformed([{"ins": True, "text": "x"}])
    assert not is_wellformed([{"del": 0, "len": 1, "x": 1}])
    assert fits([ins(3, "x")], 3)
    assert not fits([ins(4, "x")], 3)
    assert not fits([ins(0, "")], 3)
    assert not fits([dele(0, 0)], 3)
    assert not fits([dele(2, 2)], 3)
    assert fits([dele(0, 3)], 3)
