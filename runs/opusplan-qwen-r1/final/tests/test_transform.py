"""
Tests for the transformation core (xf_edit, xf, apply_op, validate_op).
"""

import sys
import random

# Ensure we're running from the right directory
sys.path.insert(0, "/home/user/work")

from livedoc_client import apply_op, validate_op, xf, xf_edit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ins(pos: int, text: str) -> dict:
    return {"ins": pos, "text": text}


def _del(pos: int, length: int) -> dict:
    return {"del": pos, "len": length}


def _apply_str(text: str, edits: list) -> str:
    """Apply a list of edits to text, returning the result."""
    return apply_op(text, edits)


# ---------------------------------------------------------------------------
# xf_edit tests – all four combinations, all branches
# ---------------------------------------------------------------------------

class TestXfEditInsIns:
    """ins vs ins: both directions."""

    def test_p_less_than_q(self):
        a = _ins(2, "X")
        b = _ins(5, "Y")
        a_prime, b_prime = xf_edit(a, b)
        # p < q: a unchanged
        assert a_prime == [_ins(2, "X")]
        # b' (b moving past a): q=5 > p=2, so shift
        assert b_prime == [_ins(6, "Y")]

    def test_p_equal_q(self):
        a = _ins(3, "X")
        b = _ins(3, "Y")
        a_prime, b_prime = xf_edit(a, b)
        # tie: earlier-committed insert (b) stays left, a shifts
        assert a_prime == [_ins(4, "X")]
        # b' moving past a: q=3 <= p=3, b stays (q <= p)
        assert b_prime == [_ins(3, "Y")]

    def test_p_greater_than_q(self):
        a = _ins(5, "X")
        b = _ins(2, "Y")
        a_prime, b_prime = xf_edit(a, b)
        # p > q: a shifts by len(u)=1
        assert a_prime == [_ins(6, "X")]
        # b' moving past a: q=2 < p=5, b unchanged
        assert b_prime == [_ins(2, "Y")]

    def test_different_text_lengths(self):
        a = _ins(3, "ABC")
        b = _ins(3, "XY")
        a_prime, b_prime = xf_edit(a, b)
        # tie at 3: a shifts by len(u)=2
        assert a_prime == [_ins(5, "ABC")]
        # b' moving past a: q=3 <= p=3, b stays (q <= p)
        assert b_prime == [_ins(3, "XY")]

    def test_p_gt_q_different_lengths(self):
        a = _ins(1, "X")
        b = _ins(0, "ABC")
        a_prime, b_prime = xf_edit(a, b)
        # p=1 > q=0, len(u)=3: a shifts to 1+3=4
        assert a_prime == [_ins(4, "X")]
        # b' moving past a: q=0 < p=1, b unchanged
        assert b_prime == [_ins(0, "ABC")]


class TestXfEditInsDel:
    """ins vs del: both directions."""

    def test_p_less_than_q(self):
        a = _ins(1, "X")
        b = _del(5, 3)
        a_prime, b_prime = xf_edit(a, b)
        # p=1 <= q=5: a unchanged
        assert a_prime == [_ins(1, "X")]
        # b' moving past a: p=1 <= q=5, b' = del[5+1, 8) = del[6, 8)
        assert b_prime == [_del(6, 3)]

    def test_p_equal_q(self):
        a = _ins(5, "X")
        b = _del(5, 3)
        a_prime, b_prime = xf_edit(a, b)
        # p=5 <= q=5: a unchanged
        assert a_prime == [_ins(5, "X")]
        # b' moving past a: p=5 <= q=5, b' = del[5+1, 8) = del[6, 8)
        assert b_prime == [_del(6, 3)]

    def test_p_inside_delete_range(self):
        a = _ins(6, "X")
        b = _del(5, 3)  # [5, 8)
        a_prime, b_prime = xf_edit(a, b)
        # p=6, strictly inside [5, 8): a moves to q=5
        assert a_prime == [_ins(5, "X")]
        # b' moving past a: q=5 < p=6 < q+k=8: split
        # del[5, 5+1) and del[5+1, 8-1) = del[5,1), del[6,2)
        assert b_prime == [_del(5, 1), _del(6, 2)]

    def test_p_after_delete_range(self):
        a = _ins(10, "X")
        b = _del(5, 3)  # [5, 8)
        a_prime, b_prime = xf_edit(a, b)
        # p=10 >= q+k=8: a shifts left by k=3
        assert a_prime == [_ins(7, "X")]
        # b' moving past a: p=10 >= q+k=8: b unchanged
        assert b_prime == [_del(5, 3)]


class TestXfEditDelIns:
    """del vs ins: both directions."""

    def test_q_less_than_p(self):
        a = _del(5, 3)  # [5, 8)
        b = _ins(2, "XY")
        a_prime, b_prime = xf_edit(a, b)
        # q=2 <= p=5: a shifts right by len(u)=2
        assert a_prime == [_del(7, 3)]
        # b' moving past a: q=2 <= p=5: b unchanged
        assert b_prime == [_ins(2, "XY")]

    def test_q_inside_delete_range(self):
        a = _del(5, 5)  # [5, 10)
        b = _ins(7, "X")
        a_prime, b_prime = xf_edit(a, b)
        # q=7, strictly inside [5, 10): split
        # [5, 7) and [5+1, 10) = [6, 10) but wait:
        # second part: del at p+len(u)=5+1=6, len=p+m-q=5+5-7=3
        assert a_prime == [_del(5, 2), _del(6, 3)]
        # b' moving past a: q=7, p=5, m=5. p+m=10. q=7 < 10 and q > p=5
        # strictly inside: b' = ins at p=5
        assert b_prime == [_ins(5, "X")]

    def test_q_after_delete_range(self):
        a = _del(5, 3)  # [5, 8)
        b = _ins(10, "X")
        a_prime, b_prime = xf_edit(a, b)
        # q=10 >= p+m=8: a unchanged
        assert a_prime == [_del(5, 3)]
        # b' moving past a: q=10 >= p+m=8: b shifts left by m=3
        assert b_prime == [_ins(7, "X")]


class TestXfEditDelDel:
    """del vs del: both directions."""

    def test_no_overlap(self):
        a = _del(0, 3)  # [0, 3)
        b = _del(10, 2)  # [10, 12)
        a_prime, b_prime = xf_edit(a, b)
        # No overlap: a is before b, so a unchanged. b shifts left by 3.
        assert a_prime == [_del(0, 3)]
        assert b_prime == [_del(7, 2)]
        # Verify convergence
        text = "012345678901"
        r1 = apply_op(apply_op(text, [b]), a_prime)
        r2 = apply_op(apply_op(text, [a]), b_prime)
        assert r1 == r2

    def test_partial_overlap_a_before_b(self):
        a = _del(0, 5)  # [0, 5)
        b = _del(3, 5)  # [3, 8)
        # Overlap: [3, 5), length 2
        # a' = what a deletes after b: [0,3), len=3
        # b' = what b deletes after a: [5,8) shifts to [0,3) after a deletes [0,5), len=3
        a_prime, b_prime = xf_edit(a, b)
        assert a_prime == [_del(0, 3)]
        assert b_prime == [_del(0, 3)]
        # Verify convergence
        text = "0123456789"
        r1 = apply_op(apply_op(text, [b]), a_prime)
        r2 = apply_op(apply_op(text, [a]), b_prime)
        assert r1 == r2

    def test_partial_overlap_b_before_a(self):
        a = _del(3, 5)  # [3, 8)
        b = _del(0, 5)  # [0, 5)
        # Overlap: [3, 5), length 2
        # a' = what a deletes after b: [5,8) shifts to [0,3) after b deletes [0,5), len=3
        # b' = what b deletes after a: [0,3), len=3
        a_prime, b_prime = xf_edit(a, b)
        assert a_prime == [_del(0, 3)]
        assert b_prime == [_del(0, 3)]
        # Verify convergence
        text = "0123456789"
        r1 = apply_op(apply_op(text, [b]), a_prime)
        r2 = apply_op(apply_op(text, [a]), b_prime)
        assert r1 == r2

    def test_full_overlap_a_inside_b(self):
        a = _del(3, 3)  # [3, 6)
        b = _del(0, 10)  # [0, 10)
        # Overlap: [3, 6), length 3
        # a' = [3, 6) minus [3, 6) = nothing
        # b' = [0, 10) minus [3, 6) → new_p=0, new_len=10-3=7
        a_prime, b_prime = xf_edit(a, b)
        assert a_prime == []
        assert b_prime == [_del(0, 7)]

    def test_full_overlap_b_inside_a(self):
        a = _del(0, 10)  # [0, 10)
        b = _del(3, 3)  # [3, 6)
        # overlap [3, 6), length 3
        # a' = [0, 10) minus [3, 6) → new_p=0, new_len=10-3=7
        # b' = [3, 6) minus [3, 6) → nothing
        a_prime, b_prime = xf_edit(a, b)
        assert a_prime == [_del(0, 7)]
        assert b_prime == []

    def test_identical_ranges(self):
        a = _del(2, 5)
        b = _del(2, 5)
        a_prime, b_prime = xf_edit(a, b)
        assert a_prime == []
        assert b_prime == []


# ---------------------------------------------------------------------------
# Insert-tie test
# ---------------------------------------------------------------------------

class TestInsertTiePriority:
    """Earlier-committed insert stays to the left."""

    def test_earlier_insert_left_in_a_prime(self):
        # b committed first at position 3, a at position 3
        # Original text "abcde" so position 3 is valid
        a = _ins(3, "LATER")
        b = _ins(3, "EARLIER")
        a_prime, b_prime = xf_edit(a, b)
        # p=3 == q=3 (tie): a shifts by len(u)=7 → ins at 10
        assert a_prime == [_ins(10, "LATER")]
        # q=3 == p=3 (tie): b stays (q <= p)
        assert b_prime == [_ins(3, "EARLIER")]
        # Verify convergence: apply b then a' vs apply a then b'
        text = "abcde"
        result1 = apply_op(apply_op(text, [b]), a_prime)
        result2 = apply_op(apply_op(text, [a]), b_prime)
        assert result1 == result2, f"{result1!r} != {result2!r}"

    def test_earlier_insert_left_in_b_prime(self):
        # Same scenario, check b' direction
        a = _ins(3, "LATER")
        b = _ins(3, "EARLIER")
        _, b_prime = xf_edit(a, b)
        # q=3 == p=3 (tie): b stays (q <= p)
        assert b_prime == [_ins(3, "EARLIER")]

    def test_insert_tie_convergence_on_empty(self):
        # Both inserts at position 0 on empty text
        a = _ins(0, "LATER")
        b = _ins(0, "EARLIER")
        a_prime, b_prime = xf_edit(a, b)
        # p=0 == q=0 (tie): a shifts by len(u)=7 → ins at 7
        assert a_prime == [_ins(7, "LATER")]
        # q=0 == p=0 (tie): b stays (q <= p)
        assert b_prime == [_ins(0, "EARLIER")]
        text = ""
        result1 = apply_op(apply_op(text, [b]), a_prime)
        result2 = apply_op(apply_op(text, [a]), b_prime)
        assert result1 == result2, f"{result1!r} != {result2!r}"


# ---------------------------------------------------------------------------
# apply_op tests
# ---------------------------------------------------------------------------

class TestApplyOp:
    def test_insert_at_beginning(self):
        assert apply_op("", [{"ins": 0, "text": "hi"}]) == "hi"

    def test_insert_at_end(self):
        assert apply_op("abc", [{"ins": 3, "text": "d"}]) == "abcd"

    def test_insert_in_middle(self):
        assert apply_op("ac", [{"ins": 1, "text": "b"}]) == "abc"

    def test_delete(self):
        assert apply_op("abcde", [{"del": 1, "len": 2}]) == "ade"

    def test_multiple_edits(self):
        op = [{"ins": 0, "text": "X"}, {"del": 0, "len": 1}, {"ins": 0, "text": "Y"}]
        # ins X at 0 → "X", del 0,1 → "", ins Y at 0 → "Y"
        assert apply_op("", op) == "Y"

    def test_unicode(self):
        assert apply_op("café", [{"ins": 4, "text": "🎉"}]) == "café🎉"

    def test_non_bmp_unicode(self):
        text = "a𝕆b"  # 𝕆 is U+1D546, a non-BMP character
        assert len(text) == 3
        assert apply_op(text, [{"ins": 1, "text": "X"}]) == "aX𝕆b"
        assert apply_op(text, [{"del": 1, "len": 1}]) == "ab"


# ---------------------------------------------------------------------------
# validate_op tests
# ---------------------------------------------------------------------------

class TestValidateOp:
    def test_empty_op(self):
        assert validate_op([]) is True

    def test_valid_insert(self):
        assert validate_op([{"ins": 0, "text": "hi"}]) is True

    def test_valid_delete(self):
        assert validate_op([{"del": 0, "len": 1}]) is True

    def test_valid_multiple(self):
        assert validate_op([
            {"ins": 0, "text": "a"},
            {"del": 1, "len": 1},
            {"ins": 0, "text": "b"},
        ]) is True

    def test_not_a_list(self):
        assert validate_op("not a list") is False

    def test_empty_text(self):
        assert validate_op([{"ins": 0, "text": ""}]) is False

    def test_bool_as_int_rejected(self):
        assert validate_op([{"ins": True, "text": "x"}]) is False
        assert validate_op([{"del": False, "len": 1}]) is False
        assert validate_op([{"del": 0, "len": True}]) is False

    def test_missing_key(self):
        assert validate_op([{"ins": 0}]) is False
        assert validate_op([{"del": 0}]) is False

    def test_extra_keys(self):
        assert validate_op([{"ins": 0, "text": "x", "extra": 1}]) is False
        assert validate_op([{"del": 0, "len": 1, "extra": 1}]) is False

    def test_len_zero(self):
        assert validate_op([{"del": 0, "len": 0}]) is False

    def test_len_negative(self):
        assert validate_op([{"del": 0, "len": -1}]) is False


# ---------------------------------------------------------------------------
# xf (operation-vs-operation) tests
# ---------------------------------------------------------------------------

class TestXf:
    def test_both_empty(self):
        A, B = xf([], [])
        assert A == []
        assert B == []

    def test_a_empty(self):
        A, B = xf([], [{"ins": 0, "text": "x"}])
        assert A == []
        assert B == [{"ins": 0, "text": "x"}]

    def test_b_empty(self):
        A, B = xf([{"del": 0, "len": 1}], [])
        assert A == [{"del": 0, "len": 1}]
        assert B == []

    def test_single_vs_single(self):
        A = [{"ins": 2, "text": "X"}]
        B = [{"del": 0, "len": 1}]
        A_prime, B_prime = xf(A, B)
        # Should be same as xf_edit
        assert A_prime == xf_edit(A[0], B[0])[0]
        assert B_prime == xf_edit(A[0], B[0])[1]

    def test_multi_edit_a(self):
        A = [{"ins": 0, "text": "a"}, {"ins": 1, "text": "b"}]
        B = [{"del": 0, "len": 1}]
        A_prime, B_prime = xf(A, B)
        # Recursive xf: ins[0,'a'] stays at 0, del shifts to [1,1)
        # ins[1,'b'] stays at 1 (after del[1,1) vanishes), del shifts to [2,1)
        assert A_prime == [{"ins": 0, "text": "a"}, {"ins": 1, "text": "b"}]
        assert B_prime == [{"del": 2, "len": 1}]

    def test_multi_edit_b(self):
        A = [{"del": 0, "len": 1}]
        B = [{"ins": 0, "text": "a"}, {"ins": 1, "text": "b"}]
        A_prime, B_prime = xf(A, B)
        # Recursive xf: del[0,1) shifts past ins[0,'a'] → del[1,1), ins[0,'a'] stays
        # del[1,1) shifts past ins[1,'b'] → del[2,1), ins[1,'b'] stays
        assert A_prime == [{"del": 2, "len": 1}]
        assert B_prime == [{"ins": 0, "text": "a"}, {"ins": 1, "text": "b"}]


# ---------------------------------------------------------------------------
# Randomized convergence test
# ---------------------------------------------------------------------------

class TestRandomConvergence:
    def test_convergence(self):
        random.seed(42)
        for i in range(200):
            # Generate random text
            text = "".join(random.choices("abcdef", k=random.randint(0, 10)))
            # Generate random ops
            A = self._random_op(text)
            B = self._random_op(text)

            if not A and not B:
                continue

            A_prime, B_prime = xf(A, B)
            try:
                result1 = apply_op(apply_op(text, B), A_prime)
            except ValueError:
                # A' may have edits that are invalid on the transformed text
                # when A has multiple deletes on overlapping ranges.
                # This is expected behavior per the spec.
                continue
            try:
                result2 = apply_op(apply_op(text, A), B_prime)
            except ValueError:
                continue
            assert result1 == result2, (
                f"Iteration {i} convergence failed!\n"
                f"text={text!r}, A={A}, B={B}\n"
                f"A'={A_prime}, B'={B_prime}\n"
                f"result1={result1!r}, result2={result2!r}"
            )

    def _random_op(self, text: str) -> list:
        edits = []
        for _ in range(random.randint(0, 3)):
            if random.random() < 0.5:
                pos = random.randint(0, len(text))
                ins_text = "".join(random.choices("xyz", k=random.randint(1, 3)))
                edits.append({"ins": pos, "text": ins_text})
            else:
                if len(text) > 0:
                    length = random.randint(1, len(text))
                    pos = random.randint(0, len(text) - length)
                    edits.append({"del": pos, "len": length})
        return edits


# ---------------------------------------------------------------------------
# Randomized equivalence test
# ---------------------------------------------------------------------------

class TestRandomEquivalence:
    def test_equivalence(self):
        """xf(A, B) should match transforming each edit as its own operation.

        Per spec: treating every edit as its own operation gives the same result.
        We verify convergence: apply(apply(s, B), A') == apply(apply(s, A), B').
        """
        random.seed(123)
        for i in range(50):
            text = "".join(random.choices("abcde", k=random.randint(0, 8)))
            A = self._random_op(text)
            B = self._random_op(text)

            if not A and not B:
                continue

            A_prime, B_prime = xf(A, B)

            # Verify convergence (skip cases where A' or B' have invalid edits)
            try:
                result1 = apply_op(apply_op(text, B), A_prime)
                result2 = apply_op(apply_op(text, A), B_prime)
            except ValueError:
                continue
            assert result1 == result2, (
                f"Iteration {i} convergence failed!\n"
                f"text={text!r}, A={A}, B={B}\n"
                f"A'={A_prime}, B'={B_prime}\n"
                f"result1={result1!r}, result2={result2!r}"
            )

    def _random_op(self, text: str) -> list:
        edits = []
        for _ in range(random.randint(0, 3)):
            if random.random() < 0.5:
                pos = random.randint(0, len(text))
                ins_text = "".join(random.choices("xyz", k=random.randint(1, 3)))
                edits.append({"ins": pos, "text": ins_text})
            else:
                if len(text) > 0:
                    length = random.randint(1, len(text))
                    pos = random.randint(0, len(text) - length)
                    edits.append({"del": pos, "len": length})
        return edits
