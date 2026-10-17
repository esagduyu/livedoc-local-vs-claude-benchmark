import random

import pytest

from livedoc_ot import DEL, INS, OpError, apply, edits_from_wire, edits_to_wire, validate, xf


def ins(p, t):
    return (INS, p, t)


def dele(p, n):
    return (DEL, p, n)


def converges(text, a, b):
    """Both orders of concurrent ops a (later) and b (earlier) give the same text."""
    a2, b2 = xf(a, b)
    left = apply(apply(text, b), a2)
    right = apply(apply(text, a), b2)
    return left == right, a2, b2


# Edit against edit: the table in the spec, checked on the transformed text.

def test_insert_before_insert_is_unchanged():
    # The earlier insert at 4 is pushed right by the later insert at 2.
    assert xf([ins(2, "X")], [ins(4, "YY")]) == ([ins(2, "X")], [ins(5, "YY")])


def test_insert_after_insert_shifts_by_length_of_earlier():
    a2, _ = xf([ins(4, "X")], [ins(2, "YY")])
    assert a2 == [ins(6, "X")]


def test_insert_tie_earlier_insert_stays_left():
    a2, b2 = xf([ins(2, "X")], [ins(2, "YY")])
    assert a2 == [ins(4, "X")]
    assert b2 == [ins(2, "YY")]
    assert apply(apply("abcd", [ins(2, "YY")]), a2) == "abYYXcd"


def test_insert_before_delete_unchanged():
    assert xf([ins(1, "X")], [dele(2, 2)])[0] == [ins(1, "X")]


def test_insert_at_delete_start_unchanged():
    assert xf([ins(2, "X")], [dele(2, 2)])[0] == [ins(2, "X")]


def test_insert_after_delete_shifts_back():
    assert xf([ins(5, "X")], [dele(2, 2)])[0] == [ins(3, "X")]


def test_insert_inside_deleted_range_goes_to_delete_start():
    assert xf([ins(3, "X")], [dele(2, 3)])[0] == [ins(2, "X")]


def test_delete_after_insert_at_same_point_moves_past_insert():
    assert xf([dele(0, 1)], [ins(0, "ZZ")])[0] == [dele(2, 1)]


def test_delete_before_insert_unchanged():
    assert xf([dele(1, 2)], [ins(5, "Z")])[0] == [dele(1, 2)]


def test_delete_split_by_insert_inside_range():
    # "abcd": delete "bc" while "ZZ" is inserted between b and c.
    a2, _ = xf([dele(1, 2)], [ins(2, "ZZ")])
    assert a2 == [dele(1, 1), dele(3, 1)]
    assert apply(apply("abcd", [ins(2, "ZZ")]), a2) == "aZZd"


def test_delete_vs_delete_disjoint_shifts():
    # "abcdefg": a deletes "cde", b deletes "ab".
    assert xf([dele(2, 3)], [dele(0, 2)])[0] == [dele(0, 3)]


def test_delete_vs_delete_partial_overlap_keeps_remainder():
    # "abcdefg": a deletes "bcd", b deletes "cdef". Only "b" is left to delete.
    assert xf([dele(1, 3)], [dele(2, 4)])[0] == [dele(1, 1)]


def test_delete_vs_delete_fully_covered_vanishes():
    assert xf([dele(2, 2)], [dele(1, 4)])[0] == []


def test_delete_vs_delete_remainder_after_containing_delete():
    # "abcdefg": a deletes "bcde", b deletes "cd". a keeps "b" and "e".
    a2, _ = xf([dele(1, 4)], [dele(2, 2)])
    assert a2 == [dele(1, 2)]
    assert apply(apply("abcdefg", [dele(2, 2)]), a2) == "afg"


# Operation against operation.

def test_empty_operations_pass_through():
    assert xf([], [ins(0, "x")]) == ([], [ins(0, "x")])
    assert xf([ins(0, "x")], []) == ([ins(0, "x")], [])
    assert xf([], []) == ([], [])


def test_multi_edit_against_single_edit_converges():
    text = "abcd"
    a = [ins(0, "x"), dele(2, 1)]  # "abcd" -> "xabcd" -> "xacd"
    b = [dele(1, 2)]  # "abcd" -> "ad"
    ok, a2, _ = converges(text, a, b)
    assert ok
    assert apply(apply(text, b), a2) == "xad"


def test_multi_edit_against_multi_edit_converges():
    text = "hello world"
    a = [dele(0, 6), ins(0, "H"), ins(6, "!")]
    b = [ins(5, ","), dele(0, 1), dele(3, 2)]
    ok, _, _ = converges(text, a, b)
    assert ok


def test_vanished_operation_is_empty_list():
    a = [dele(1, 2)]
    b = [dele(0, 5)]
    assert xf(a, b)[0] == []


# Validation and application.

def test_validate_accepts_valid_operation_and_returns_new_length():
    assert validate([ins(0, "ab"), dele(1, 2)], 3) == 3


@pytest.mark.parametrize("edits, length", [
    ([ins(4, "x")], 3),  # insert past end
    ([ins(-1, "x")], 3),  # negative position
    ([ins(0, "")], 3),  # empty text
    ([dele(0, 0)], 3),  # zero length
    ([dele(2, 2)], 3),  # runs past the end
    ([dele(-1, 1)], 3),
    ([dele(0, 3), ins(1, "x")], 3),  # second edit sees the shorter text
])
def test_validate_rejects_invalid_operations(edits, length):
    with pytest.raises(OpError):
        validate(edits, length)


def test_wire_round_trip():
    edits = [ins(1, "héllo"), dele(0, 2)]
    assert edits_from_wire(edits_to_wire(edits)) == edits


# Convergence over random operations: the property the whole system depends on.

def _random_op(rng, text):
    edits, length = [], len(text)
    for _ in range(rng.randint(0, 4)):
        if length == 0 or rng.random() < 0.5:
            pos = rng.randint(0, length)
            t = "".join(rng.choice("xyz") for _ in range(rng.randint(1, 3)))
            edits.append(ins(pos, t))
            length += len(t)
        else:
            pos = rng.randint(0, length - 1)
            n = rng.randint(1, length - pos)
            edits.append(dele(pos, n))
            length -= n
    return edits


@pytest.mark.parametrize("seed", range(2000))
def test_random_concurrent_operations_converge(seed):
    rng = random.Random(seed)
    text = "".join(rng.choice("abc") for _ in range(rng.randint(0, 8)))
    a = _random_op(rng, text)
    b = _random_op(rng, text)
    ok, a2, b2 = converges(text, a, b)
    assert ok, (text, a, b, a2, b2)
    validate(a2, len(apply(text, b)))
    validate(b2, len(apply(text, a)))
