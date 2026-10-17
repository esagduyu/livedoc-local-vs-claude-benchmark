import random

import pytest

from livedoc_client import OpError, apply_op, check_op, transform, transform_edit


def ins(p, t):
    return {"ins": p, "text": t}


def dele(p, n):
    return {"del": p, "len": n}


def xf_reference(A, B):
    """The recursive definition, verbatim from the spec."""
    if not A or not B:
        return list(A), list(B)
    if len(A) == 1 and len(B) == 1:
        return transform_edit(A[0], B[0])
    if len(A) > 1:
        a1, B1 = xf_reference(A[:1], B)
        rest, B2 = xf_reference(A[1:], B1)
        return a1 + rest, B2
    A1, b1 = xf_reference(A, B[:1])
    A2, rest = xf_reference(A1, B[1:])
    return A2, b1 + rest


# --- edit vs edit, the table from the spec ---------------------------------

@pytest.mark.parametrize("a, b, expected", [
    (ins(1, "x"), ins(3, "uv"), [ins(1, "x")]),
    (ins(3, "x"), ins(3, "uv"), [ins(5, "x")]),  # tie: earlier stays left
    (ins(4, "x"), ins(3, "uv"), [ins(6, "x")]),
    (ins(2, "x"), dele(2, 3), [ins(2, "x")]),
    (ins(5, "x"), dele(2, 3), [ins(2, "x")]),
    (ins(6, "x"), dele(2, 3), [ins(3, "x")]),
    (ins(3, "x"), dele(2, 3), [ins(2, "x")]),  # strictly inside
    (dele(2, 3), ins(2, "uv"), [dele(4, 3)]),
    (dele(2, 3), ins(1, "uv"), [dele(4, 3)]),
    (dele(2, 3), ins(5, "uv"), [dele(2, 3)]),
    (dele(2, 3), ins(3, "uv"), [dele(2, 1), dele(4, 2)]),  # split around u
    (dele(2, 3), dele(2, 3), []),
    (dele(2, 3), dele(0, 10), []),
    (dele(2, 3), dele(3, 1), [dele(2, 2)]),
    (dele(2, 3), dele(0, 3), [dele(0, 2)]),
    (dele(2, 3), dele(4, 5), [dele(2, 2)]),
    (dele(5, 2), dele(0, 2), [dele(3, 2)]),
    (dele(0, 2), dele(5, 2), [dele(0, 2)]),
])
def test_edit_table(a, b, expected):
    assert transform([a], [b])[0] == expected


def test_reverse_direction_insert_tie():
    # moving earlier insert at q past a later insert at p: unchanged if q <= p
    assert transform([ins(3, "x")], [ins(3, "uv")])[1] == [ins(3, "uv")]
    assert transform([ins(2, "x")], [ins(3, "uv")])[1] == [ins(4, "uv")]
    # reversed del/ins: earlier delete splits around later insert
    assert transform([ins(3, "x")], [dele(2, 3)])[1] == [dele(2, 1), dele(3, 2)]


def test_empty_operations():
    assert transform([], [ins(0, "a")]) == ([], [ins(0, "a")])
    assert transform([dele(0, 1)], []) == ([dele(0, 1)], [])


def test_multi_edit_example():
    # B inserts at 0 then deletes; A edits sequentially
    A = [ins(1, "X"), dele(3, 2)]
    B = [ins(0, "ab"), dele(4, 1)]
    assert transform(A, B) == xf_reference(A, B)


def test_apply_and_check():
    assert apply_op("hello", [ins(5, "!"), dele(0, 1), ins(0, "J")]) == "Jello!"
    assert check_op([ins(0, "ab"), dele(1, 1)], 0)[1] == 1
    with pytest.raises(OpError) as e:
        check_op([dele(0, 1)], 0)
    assert e.value.kind == "out_of_range"
    with pytest.raises(OpError) as e:
        check_op([{"ins": "0", "text": "a"}], 0)
    assert e.value.kind == "bad_request"
    for bad in ([ins(0, "")], [dele(0, 0)], [ins(-1, "a")]):
        with pytest.raises(OpError) as e:
            check_op(bad, 5)
        assert e.value.kind == "out_of_range"


# --- randomized properties ---------------------------------------------------

def random_op(rng, text, max_edits=4):
    op = []
    n = len(text)
    for _ in range(rng.randint(0, max_edits)):
        if n == 0 or rng.random() < 0.5:
            t = "".join(rng.choice("abcé😀") for _ in range(rng.randint(1, 3)))
            op.append(ins(rng.randint(0, n), t))
            n += len(t)
        else:
            p = rng.randint(0, n - 1)
            k = rng.randint(1, n - p)
            op.append(dele(p, k))
            n -= k
    return op


def test_random_convergence_and_reference():
    rng = random.Random(1234)
    for _ in range(3000):
        base = "".join(rng.choice("0123456789") for _ in range(rng.randint(0, 12)))
        A = random_op(rng, base)
        B = random_op(rng, base)
        A2, B2 = transform(A, B)
        assert (A2, B2) == xf_reference(A, B)
        after_b = apply_op(base, B)
        after_a = apply_op(base, A)
        check_op(A2, len(after_b))
        check_op(B2, len(after_a))
        assert apply_op(after_b, A2) == apply_op(after_a, B2)


def test_long_operations_do_not_recurse_deeply():
    A = [ins(0, "a")] * 1000
    B = [ins(0, "b")] * 1000
    A2, B2 = transform(A, B)
    assert apply_op("b" * 1000, A2) == apply_op("a" * 1000, B2)
