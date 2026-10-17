import random

import pytest

from livedoc_client import OpError, apply_op, check_op, transform_edit, validate_op, xf


def ins(p, t):
    return {"ins": p, "text": t}


def dl(p, n):
    return {"del": p, "len": n}


def later(a, b):
    """Transform later edit a against earlier edit b."""
    return transform_edit(a, b, False)


# -- edit against edit: one test per table row ----------------------------------

@pytest.mark.parametrize("a, b, expected", [
    (ins(1, "x"), ins(3, "uu"), [ins(1, "x")]),
    (ins(5, "x"), ins(3, "uu"), [ins(7, "x")]),
    (ins(3, "x"), ins(3, "uu"), [ins(5, "x")]),          # tie: earlier insert stays left
    (ins(2, "x"), dl(2, 3), [ins(2, "x")]),
    (ins(1, "x"), dl(2, 3), [ins(1, "x")]),
    (ins(5, "x"), dl(2, 3), [ins(2, "x")]),              # at end of deleted range
    (ins(7, "x"), dl(2, 3), [ins(4, "x")]),
    (ins(3, "x"), dl(2, 3), [ins(2, "x")]),              # strictly inside
    (dl(4, 2), ins(4, "uu"), [dl(6, 2)]),                # q == p
    (dl(4, 2), ins(1, "uu"), [dl(6, 2)]),
    (dl(4, 2), ins(6, "uu"), [dl(4, 2)]),                # q == p + m
    (dl(4, 2), ins(9, "uu"), [dl(4, 2)]),
    (dl(2, 4), ins(3, "uu"), [dl(2, 1), dl(4, 3)]),      # split around the insert
    (dl(2, 2), dl(6, 2), [dl(2, 2)]),
    (dl(6, 2), dl(2, 2), [dl(4, 2)]),
    (dl(2, 4), dl(4, 4), [dl(2, 2)]),                    # overlap at the end
    (dl(4, 4), dl(2, 4), [dl(2, 2)]),                    # overlap at the start
    (dl(2, 6), dl(4, 2), [dl(2, 4)]),                    # b inside a
    (dl(4, 2), dl(2, 6), []),                            # a inside b
    (dl(2, 3), dl(2, 3), []),
])
def test_edit_rules(a, b, expected):
    assert later(a, b) == expected


def test_reverse_direction_keeps_earlier_insert_left():
    # Moving earlier insert q past later insert p: unchanged when q <= p, shifted when q > p.
    assert transform_edit(ins(3, "u"), ins(3, "xx"), True) == [ins(3, "u")]
    assert transform_edit(ins(2, "u"), ins(3, "xx"), True) == [ins(2, "u")]
    assert transform_edit(ins(4, "u"), ins(3, "xx"), True) == [ins(6, "u")]


def test_insert_tie_converges():
    text = "abc"
    a, b = [ins(1, "A")], [ins(1, "B")]
    a2, b2 = xf(a, b)
    assert apply_op(apply_op(text, b), a2) == apply_op(apply_op(text, a), b2) == "aBAbc"


def test_empty_ops():
    assert xf([], [ins(0, "x")]) == ([], [ins(0, "x")])
    assert xf([ins(0, "x")], []) == ([ins(0, "x")], [])


def test_multi_edit_example():
    # B: two edits committed earlier; A: insert followed by delete written on "hello world".
    text = "hello world"
    B = [ins(0, ">> "), dl(8, 6)]          # ">> hello world" -> ">> hello"
    A = [ins(5, ","), dl(7, 5)]            # "hello, world" -> "hello, "
    A2, B2 = xf(A, B)
    assert apply_op(apply_op(text, B), A2) == apply_op(apply_op(text, A), B2) == ">> hello,"


# -- reference implementation, written exactly as the spec's pseudocode -----------

def xf_reference(A, B):
    if not A or not B:
        return A, B
    if len(A) == 1 and len(B) == 1:
        return transform_edit(A[0], B[0], False), transform_edit(B[0], A[0], True)
    if len(A) > 1:
        a1, B1 = xf_reference(A[:1], B)
        rest, B2 = xf_reference(A[1:], B1)
        return a1 + rest, B2
    A1, b1 = xf_reference(A, B[:1])
    A2, rest = xf_reference(A1, B[1:])
    return A2, b1 + rest


def random_op(rng, length, max_edits=4, alphabet="abcXYZ"):
    op = []
    for _ in range(rng.randint(0, max_edits)):
        if length == 0 or rng.random() < 0.5:
            t = "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 3)))
            op.append(ins(rng.randint(0, length), t))
            length += len(t)
        else:
            p = rng.randint(0, length - 1)
            n = rng.randint(1, min(4, length - p))
            op.append(dl(p, n))
            length -= n
    return op


def random_text(rng):
    return "".join(rng.choice("0123456789") for _ in range(rng.randint(0, 12)))


def test_random_convergence_and_reference_agreement():
    rng = random.Random(1234)
    for _ in range(5000):
        text = random_text(rng)
        A, B = random_op(rng, len(text)), random_op(rng, len(text))
        A2, B2 = xf(A, B)
        assert (A2, B2) == xf_reference(A, B)
        after_b = apply_op(text, B)
        after_a = apply_op(text, A)
        check_op(A2, len(after_b))
        check_op(B2, len(after_a))
        assert apply_op(after_b, A2) == apply_op(after_a, B2)


def test_sequential_history_convergence():
    # A server-style chain: transform an op against several committed ops in turn.
    rng = random.Random(99)
    for _ in range(1000):
        base = random_text(rng)
        ops = []
        text = base
        for _ in range(rng.randint(1, 4)):
            op = random_op(rng, len(text))
            ops.append(op)
            text = apply_op(text, op)
        mine = random_op(rng, len(base))
        t = mine
        for op in ops:
            t, _ = xf(t, op)
        check_op(t, len(text))
        # Same as transforming against the concatenation of the history.
        assert t == xf(mine, [e for op in ops for e in op])[0]


def test_long_operations_do_not_overflow_the_stack():
    A = [ins(0, "a") for _ in range(3000)]
    B = [ins(0, "b") for _ in range(3000)]
    A2, B2 = xf(A, B)
    assert apply_op(apply_op("", B), A2) == apply_op(apply_op("", A), B2)


# -- validation --------------------------------------------------------------------

@pytest.mark.parametrize("op", [
    "x", {"ins": 0}, [1], [{"ins": 0}], [{"ins": "0", "text": "a"}], [{"ins": 0, "text": 5}],
    [{"del": 0}], [{"del": 0, "len": 1.0}], [{"ins": True, "text": "a"}],
    [{"ins": 0, "text": "a", "del": 0, "len": 1}],
])
def test_validate_rejects_malformed(op):
    with pytest.raises(OpError) as e:
        validate_op(op)
    assert e.value.code == "bad_request"


@pytest.mark.parametrize("op, length", [
    ([ins(4, "a")], 3), ([ins(-1, "a")], 3), ([ins(0, "")], 3), ([dl(0, 0)], 3),
    ([dl(2, 2)], 3), ([dl(-1, 1)], 3), ([ins(3, "ab"), dl(0, 6)], 3),
])
def test_check_rejects_out_of_range(op, length):
    with pytest.raises(OpError) as e:
        check_op(op, length)
    assert e.value.code == "out_of_range"


def test_check_sequential_lengths():
    assert check_op([ins(3, "ab"), dl(0, 5)], 3) == 0
    assert apply_op("héllo", [ins(5, "🙂"), dl(0, 1)]) == "éllo🙂"
