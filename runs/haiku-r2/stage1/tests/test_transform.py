import random

import pytest

from livedoc_client import apply_op, transform


def ins(p, t):
    return {"ins": p, "text": t}


def dele(p, n):
    return {"del": p, "len": n}


def xf1(a, b):
    return transform([a], [b])


# Edit against edit, one row of the table each.

def test_ins_ins_before_is_unchanged():
    assert xf1(ins(1, "x"), ins(3, "yy")) == ([ins(1, "x")], [ins(3 + 1, "yy")])


def test_ins_ins_after_shifts_by_committed_length():
    assert xf1(ins(5, "x"), ins(3, "yy")) == ([ins(7, "x")], [ins(3, "yy")])


def test_ins_ins_tie_keeps_earlier_insert_left():
    a2, b2 = xf1(ins(2, "x"), ins(2, "yy"))
    assert a2 == [ins(4, "x")]
    assert b2 == [ins(2, "yy")]
    assert apply_op(apply_op("ab", [ins(2, "yy")]), a2) == apply_op(apply_op("ab", [ins(2, "x")]), b2)


def test_ins_del_before_is_unchanged():
    assert xf1(ins(1, "x"), dele(3, 2)) == ([ins(1, "x")], [dele(3 + 1, 2)])


def test_ins_del_after_shifts_back():
    assert xf1(ins(8, "x"), dele(3, 2)) == ([ins(6, "x")], [dele(3, 2)])


def test_ins_del_inside_moves_to_deletion_start():
    a2, b2 = xf1(ins(4, "x"), dele(3, 3))
    assert a2 == [ins(3, "x")]
    assert b2 == [dele(3, 1), dele(3 + 1, 2)]


def test_del_ins_before_shifts_deletion():
    assert xf1(dele(5, 2), ins(2, "ab")) == ([dele(7, 2)], [ins(2, "ab")])


def test_del_ins_tie_at_start_shifts_deletion():
    assert xf1(dele(5, 2), ins(5, "ab")) == ([dele(7, 2)], [ins(5, "ab")])


def test_del_ins_after_is_unchanged():
    assert xf1(dele(2, 2), ins(4, "ab")) == ([dele(2, 2)], [ins(2, "ab")])


def test_del_ins_inside_splits_and_keeps_inserted_text():
    a2, b2 = xf1(dele(2, 4), ins(4, "XY"))
    assert a2 == [dele(2, 2), dele(2 + 2, 2)]
    assert b2 == [ins(2, "XY")]


def test_del_del_disjoint():
    assert xf1(dele(5, 2), dele(0, 2)) == ([dele(3, 2)], [dele(0, 2)])


def test_del_del_overlap_leaves_one_range():
    assert xf1(dele(0, 5), dele(3, 5)) == ([dele(0, 3)], [dele(0, 3)])


def test_del_del_fully_covered_is_nothing():
    a2, b2 = xf1(dele(3, 2), dele(0, 10))
    assert a2 == []
    assert b2 == [dele(0, 8)]


def test_del_del_identical_is_nothing_for_both():
    assert xf1(dele(2, 3), dele(2, 3)) == ([], [])


# Operation against operation.

def test_empty_operations_pass_through():
    assert transform([], [ins(0, "a")]) == ([], [ins(0, "a")])
    assert transform([ins(0, "a")], []) == ([ins(0, "a")], [])


def test_multi_edit_is_applied_sequentially():
    # a inserts at 1, then deletes the character it just inserted and one more.
    text = "abcdef"
    a = [ins(1, "X"), dele(1, 2)]
    b = [ins(0, "__"), dele(4, 1)]
    a2, b2 = transform(a, b)
    assert apply_op(apply_op(text, b), a2) == apply_op(apply_op(text, a), b2)


def test_split_edit_in_multi_edit_operation():
    text = "0123456789"
    a = [dele(2, 6)]
    b = [ins(4, "AB"), ins(0, "x")]
    a2, b2 = transform(a, b)
    assert apply_op(apply_op(text, b), a2) == apply_op(apply_op(text, a), b2)


def random_edit(rng, text):
    if not text or rng.random() < 0.5:
        return ins(rng.randint(0, len(text)), "".join(rng.choice("abc") for _ in range(rng.randint(1, 3))))
    p = rng.randint(0, len(text) - 1)
    return dele(p, rng.randint(1, len(text) - p))


def random_op(rng, text):
    op = []
    for _ in range(rng.randint(0, 3)):
        edit = random_edit(rng, text)
        text = apply_op(text, [edit])
        op.append(edit)
    return op


@pytest.mark.parametrize("seed", range(3000))
def test_both_orders_converge(seed):
    rng = random.Random(seed)
    base = "".join(rng.choice("xyz") for _ in range(rng.randint(0, 8)))
    a = random_op(rng, base)
    b = random_op(rng, base)
    a2, b2 = transform(a, b)
    assert apply_op(apply_op(base, a), b2) == apply_op(apply_op(base, b), a2)


def test_apply_op_rejects_out_of_range():
    with pytest.raises(ValueError):
        apply_op("abc", [ins(4, "x")])
    with pytest.raises(ValueError):
        apply_op("abc", [ins(0, "")])
    with pytest.raises(ValueError):
        apply_op("abc", [dele(2, 2)])
    with pytest.raises(ValueError):
        apply_op("abc", [dele(0, 0)])
    with pytest.raises(ValueError):
        apply_op("abc", [dele(-1, 1)])


def test_apply_op_empty_is_identity():
    assert apply_op("abc", []) == "abc"
