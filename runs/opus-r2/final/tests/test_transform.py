import random

import pytest

from livedoc_client import apply_op, transform, transform_edit


def ins(p, t):
    return {"ins": p, "text": t}


def dele(p, n):
    return {"del": p, "len": n}


def xf1(a, b):
    """Transform later edit a against earlier edit b."""
    return transform([a], [b])[0]


# -- edit against edit (the table in the spec) --------------------------------

@pytest.mark.parametrize("a, b, expected", [
    # ins vs ins
    (ins(2, "x"), ins(5, "yy"), [ins(2, "x")]),
    (ins(7, "x"), ins(5, "yy"), [ins(9, "x")]),
    (ins(5, "x"), ins(5, "yy"), [ins(7, "x")]),        # tie: earlier stays left
    # ins vs del
    (ins(2, "x"), dele(2, 3), [ins(2, "x")]),
    (ins(1, "x"), dele(2, 3), [ins(1, "x")]),
    (ins(5, "x"), dele(2, 3), [ins(2, "x")]),           # p == q + k
    (ins(9, "x"), dele(2, 3), [ins(6, "x")]),
    (ins(3, "x"), dele(2, 3), [ins(2, "x")]),           # strictly inside
    (ins(4, "x"), dele(2, 3), [ins(2, "x")]),
    # del vs ins
    (dele(2, 3), ins(2, "uu"), [dele(4, 3)]),           # q == p
    (dele(2, 3), ins(0, "uu"), [dele(4, 3)]),
    (dele(2, 3), ins(5, "uu"), [dele(2, 3)]),           # q == p + m
    (dele(2, 3), ins(9, "uu"), [dele(2, 3)]),
    (dele(2, 3), ins(3, "uu"), [dele(2, 1), dele(4, 2)]),  # split
    (dele(2, 3), ins(4, "u"), [dele(2, 2), dele(3, 1)]),
    # del vs del
    (dele(0, 2), dele(5, 2), [dele(0, 2)]),
    (dele(5, 2), dele(0, 2), [dele(3, 2)]),
    (dele(2, 4), dele(4, 4), [dele(2, 2)]),             # overlap at the end
    (dele(4, 4), dele(2, 4), [dele(2, 2)]),             # overlap at the start
    (dele(2, 6), dele(3, 2), [dele(2, 4)]),             # b inside a
    (dele(3, 2), dele(2, 6), []),                       # a inside b
    (dele(2, 3), dele(2, 3), []),                       # identical
    (dele(2, 2), dele(4, 2), [dele(2, 2)]),             # adjacent
    (dele(4, 2), dele(2, 2), [dele(2, 2)]),
])
def test_edit_rules(a, b, expected):
    assert xf1(a, b) == expected


def test_reverse_direction_keeps_earlier_priority_on_ties():
    # Moving the earlier insert b past the later insert a.
    assert transform_edit(ins(5, "b"), ins(5, "a"), True) == [ins(5, "b")]
    assert transform_edit(ins(4, "b"), ins(5, "a"), True) == [ins(4, "b")]
    assert transform_edit(ins(6, "b"), ins(5, "a"), True) == [ins(7, "b")]
    a2, b2 = transform([ins(3, "A")], [ins(3, "B")])
    assert a2 == [ins(4, "A")] and b2 == [ins(3, "B")]
    assert apply_op(apply_op("abcdef", [ins(3, "B")]), a2) == "abcBAdef"


def test_empty_operations():
    assert transform([], [ins(0, "x")]) == ([], [ins(0, "x")])
    assert transform([dele(0, 1)], []) == ([dele(0, 1)], [])


def test_split_delete_keeps_inserted_text():
    base = "0123456789"
    a, b = [dele(2, 5)], [ins(4, "XY")]
    a2, b2 = transform(a, b)
    assert apply_op(apply_op(base, b), a2) == "01XY789"
    assert apply_op(apply_op(base, a), b2) == "01XY789"


def test_multi_edit_operation():
    base = "abcdef"
    a = [ins(0, "X"), dele(3, 2)]           # "Xabcdef" -> "Xabef"
    b = [dele(0, 1), ins(5, "Z")]           # "bcdef" -> "bcdefZ"
    a2, b2 = transform(a, b)
    assert apply_op(apply_op(base, b), a2) == apply_op(apply_op(base, a), b2) == "XbefZ"


# -- reference implementation and randomized properties ------------------------

def xf_reference(A, B):
    """Literal transcription of the spec's recursive definition."""
    if not A or not B:
        return list(A), list(B)
    if len(A) == 1 and len(B) == 1:
        return transform_edit(A[0], B[0], False), transform_edit(B[0], A[0], True)
    if len(A) > 1:
        a1, B1 = xf_reference(A[:1], B)
        rest, B2 = xf_reference(A[1:], B1)
        return a1 + rest, B2
    A1, b1 = xf_reference(A, B[:1])
    A2, rest = xf_reference(A1, B[1:])
    return A2, b1 + rest


def random_op(rng, text, max_edits=4):
    op = []
    for _ in range(rng.randint(0, max_edits)):
        if text and rng.random() < 0.5:
            p = rng.randrange(len(text))
            n = rng.randint(1, min(4, len(text) - p))
            edit = dele(p, n)
        else:
            p = rng.randint(0, len(text))
            edit = ins(p, "".join(rng.choice("xyzé😀") for _ in range(rng.randint(1, 3))))
        op.append(edit)
        text = apply_op(text, [edit])
    return op


def test_randomized_convergence_and_reference():
    rng = random.Random(1234)
    for _ in range(5000):
        base = "".join(rng.choice("abcdefgh") for _ in range(rng.randint(0, 12)))
        A, B = random_op(rng, base), random_op(rng, base)
        A2, B2 = transform(A, B)
        assert (A2, B2) == xf_reference(A, B)
        assert apply_op(apply_op(base, B), A2) == apply_op(apply_op(base, A), B2)


def test_matches_edit_by_edit_definition():
    # B's edits committed one at a time, then A's edits arriving one at a time;
    # each of A's edits moves past all of B's (already moved past earlier A edits).
    rng = random.Random(99)
    for _ in range(2000):
        base = "".join(rng.choice("abcdefgh") for _ in range(rng.randint(0, 12)))
        A, B = random_op(rng, base), random_op(rng, base)
        committed = [[b] for b in B]
        result = []
        for a in A:
            cur = [a]
            for i, c in enumerate(committed):
                cur, committed[i] = transform(cur, c)
            result += cur
        assert transform(A, B)[0] == result


def test_server_style_sequential_transform():
    # Transforming against several committed ops one after another, as the server does.
    rng = random.Random(7)
    for _ in range(1000):
        base = "".join(rng.choice("abc") for _ in range(rng.randint(0, 10)))
        history, text = [], base
        for _ in range(rng.randint(1, 4)):
            op = random_op(rng, text)
            history.append(op)
            text = apply_op(text, op)
        mine = random_op(rng, base)
        for op in history:
            mine, _ = transform(mine, op)
        apply_op(text, mine)  # must be valid against the latest text


def test_long_operations_do_not_overflow_the_stack():
    base = "a" * 3000
    A = [dele(i, 1) for i in range(0, 1500)]
    B = [ins(i * 2, "b") for i in range(1500)]
    A2, B2 = transform(A, B)
    assert apply_op(apply_op(base, B), A2) == apply_op(apply_op(base, A), B2)
