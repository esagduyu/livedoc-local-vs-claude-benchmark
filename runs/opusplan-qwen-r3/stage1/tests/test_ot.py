"""Tests for livedoc_ot.py — transformation rules, validation, apply."""

import random
import pytest

from livedoc_ot import (
    apply_op,
    xf,
    _edit_vs_edit,
    _validate_op,
    transform_against_history,
    transform_op_against_history,
)


# ===================================================================
# Helpers
# ===================================================================

def _eq(a: list[dict], b: list[dict]) -> bool:
    """Compare two edit lists for equality."""
    if len(a) != len(b):
        return False
    return all(a[i] == b[i] for i in range(len(a)))


# ===================================================================
# Validation tests
# ===================================================================


class TestValidateOp:
    def test_valid_ins(self):
        assert _validate_op([{"ins": 0, "text": "a"}], 0) == 1

    def test_valid_del(self):
        assert _validate_op([{"del": 0, "len": 1}], 1) == 0

    def test_valid_empty_op(self):
        assert _validate_op([], 5) == 5

    def test_valid_multi_edit(self):
        # Start with length 5. Insert "hello" at 0 → length 10. Delete 2 from 0 → length 8.
        assert _validate_op(
            [{"ins": 0, "text": "hello"}, {"del": 0, "len": 2}], 5
        ) == 8

    def test_ins_out_of_range(self):
        with pytest.raises(ValueError, match="out of range"):
            _validate_op([{"ins": 5, "text": "x"}], 3)

    def test_ins_negative(self):
        with pytest.raises(ValueError, match="out of range"):
            _validate_op([{"ins": -1, "text": "x"}], 3)

    def test_del_out_of_range(self):
        with pytest.raises(ValueError, match="out of range"):
            _validate_op([{"del": 1, "len": 5}], 3)

    def test_del_len_zero(self):
        with pytest.raises(ValueError, match=">= 1"):
            _validate_op([{"del": 0, "len": 0}], 3)

    def test_bool_position_rejected(self):
        with pytest.raises(ValueError, match="must be int"):
            _validate_op([{"ins": True, "text": "x"}], 0)

    def test_bool_len_rejected(self):
        with pytest.raises(ValueError, match="must be int"):
            _validate_op([{"del": 0, "len": True}], 3)

    def test_empty_text_rejected(self):
        with pytest.raises(ValueError, match="non-empty"):
            _validate_op([{"ins": 0, "text": ""}], 0)

    def test_non_dict_edit_rejected(self):
        with pytest.raises(ValueError, match="must be a dict"):
            _validate_op(["not a dict"], 0)

    def test_non_list_op_rejected(self):
        with pytest.raises(ValueError, match="must be a list"):
            _validate_op("not a list", 0)

    def test_wrong_keys_rejected(self):
        with pytest.raises(ValueError, match="must have"):
            _validate_op([{"ins": 0}], 0)


# ===================================================================
# Apply tests
# ===================================================================


class TestApplyOp:
    def test_insert_at_beginning(self):
        assert apply_op("bc", [{"ins": 0, "text": "a"}]) == "abc"

    def test_insert_at_end(self):
        assert apply_op("ab", [{"ins": 2, "text": "c"}]) == "abc"

    def test_insert_in_middle(self):
        assert apply_op("ac", [{"ins": 1, "text": "b"}]) == "abc"

    def test_delete_from_beginning(self):
        assert apply_op("abc", [{"del": 0, "len": 1}]) == "bc"

    def test_delete_from_end(self):
        assert apply_op("abc", [{"del": 2, "len": 1}]) == "ab"

    def test_delete_middle(self):
        assert apply_op("abc", [{"del": 1, "len": 1}]) == "ac"

    def test_delete_full(self):
        assert apply_op("abc", [{"del": 0, "len": 3}]) == ""

    def test_multi_edit(self):
        # "abcde" → ins "XY" at 1 → "aXYbcde" → del 0, len 2 → "Ybcde"
        assert apply_op("abcde", [{"ins": 1, "text": "XY"}, {"del": 0, "len": 2}]) == "Ybcde"

    def test_empty_op(self):
        assert apply_op("abc", []) == "abc"


# ===================================================================
# Edit-vs-edit transform tests
# ===================================================================

class TestEditVsEdit:
    # --- ins vs ins ---

    def test_ins_ins_p_less_than_q(self):
        a = {"ins": 0, "text": "a"}
        b = {"ins": 1, "text": "b"}
        a_prime, b_prime = _edit_vs_edit(a, b)
        # Forward: a at 0 < b at 1 → a stays
        assert _eq(a_prime, [{"ins": 0, "text": "a"}])
        # Reverse: b at 1 > a at 0 → b shifts to 1+1=2
        assert _eq(b_prime, [{"ins": 2, "text": "b"}])

    def test_ins_ins_p_greater_than_q(self):
        a = {"ins": 2, "text": "a"}
        b = {"ins": 1, "text": "b"}
        a_prime, b_prime = _edit_vs_edit(a, b)
        # Forward: a at 2 >= b at 1 → a shifts to 2+1=3
        assert _eq(a_prime, [{"ins": 3, "text": "a"}])
        # Reverse: b at 1 < a at 2 → b stays at 1
        assert _eq(b_prime, [{"ins": 1, "text": "b"}])

    def test_ins_ins_p_equal_q_tie(self):
        """On a tie, the earlier-committed insert (b) stays to the left."""
        a = {"ins": 1, "text": "a"}
        b = {"ins": 1, "text": "b"}
        a_prime, b_prime = _edit_vs_edit(a, b)
        # Forward: a at 1 >= b at 1 → a shifts to 1+1=2
        assert _eq(a_prime, [{"ins": 2, "text": "a"}])
        # Reverse: b at 1, a at 1. pb=1, pa=1. pb > pa? No → b stays at 1.
        assert _eq(b_prime, [{"ins": 1, "text": "b"}])

    def test_ins_ins_p_greater_than_q_plus_len(self):
        a = {"ins": 3, "text": "a"}
        b = {"ins": 1, "text": "bc"}
        a_prime, b_prime = _edit_vs_edit(a, b)
        assert _eq(a_prime, [{"ins": 5, "text": "a"}])  # 3 + 2
        assert _eq(b_prime, [{"ins": 1, "text": "bc"}])

    # --- ins vs del ---

    def test_ins_del_p_less_than_del_start(self):
        a = {"ins": 0, "text": "x"}
        b = {"del": 2, "len": 1}
        a_prime, b_prime = _edit_vs_edit(a, b)
        assert _eq(a_prime, [{"ins": 0, "text": "x"}])
        assert _eq(b_prime, [{"del": 3, "len": 1}])

    def test_ins_del_p_equal_del_start(self):
        a = {"ins": 2, "text": "x"}
        b = {"del": 2, "len": 1}
        a_prime, b_prime = _edit_vs_edit(a, b)
        # p <= q → unchanged
        assert _eq(a_prime, [{"ins": 2, "text": "x"}])
        # del past ins: 2 <= 2 → del at 2+1=3
        assert _eq(b_prime, [{"del": 3, "len": 1}])

    def test_ins_del_p_equal_del_end(self):
        a = {"ins": 3, "text": "x"}
        b = {"del": 2, "len": 1}
        a_prime, b_prime = _edit_vs_edit(a, b)
        # p >= q+k → ins at p-k = 3-1 = 2
        assert _eq(a_prime, [{"ins": 2, "text": "x"}])
        # del past ins: 3 >= 3 → unchanged
        assert _eq(b_prime, [{"del": 2, "len": 1}])

    def test_ins_del_p_strictly_inside(self):
        a = {"ins": 2, "text": "x"}
        b = {"del": 1, "len": 3}  # del [1,4)
        a_prime, b_prime = _edit_vs_edit(a, b)
        # p=2, q=1, k=3 → 1 < 2 < 4 → strictly inside → ins at q=1
        assert _eq(a_prime, [{"ins": 1, "text": "x"}])
        # del splits: [1,2) and [1+1, 4-2=2) → [del 1 len 1], [del 2 len 2]
        assert _eq(b_prime, [{"del": 1, "len": 1}, {"del": 2, "len": 2}])

    def test_ins_del_p_strictly_inside_edge(self):
        a = {"ins": 1, "text": "x"}
        b = {"del": 1, "len": 3}
        a_prime, b_prime = _edit_vs_edit(a, b)
        # p=1, q=1, k=3 → p <= q → unchanged
        assert _eq(a_prime, [{"ins": 1, "text": "x"}])
        assert _eq(b_prime, [{"del": 2, "len": 3}])

    # --- del vs ins ---

    def test_del_ins_q_less_than_del_start(self):
        a = {"del": 2, "len": 1}
        b = {"ins": 0, "text": "x"}
        a_prime, b_prime = _edit_vs_edit(a, b)
        # q=0 <= p=2 → del at p+len(u) = 3
        assert _eq(a_prime, [{"del": 3, "len": 1}])
        # ins past del: q=0 <= p=2 → ins stays at 0
        assert _eq(b_prime, [{"ins": 0, "text": "x"}])

    def test_del_ins_q_equal_del_start(self):
        a = {"del": 2, "len": 1}
        b = {"ins": 2, "text": "x"}
        a_prime, b_prime = _edit_vs_edit(a, b)
        # q=2 <= p=2 → del at 2+1=3
        assert _eq(a_prime, [{"del": 3, "len": 1}])
        # ins past del: q=2 <= p=2 → ins stays at 2
        assert _eq(b_prime, [{"ins": 2, "text": "x"}])

    def test_del_ins_q_equal_del_end(self):
        a = {"del": 1, "len": 2}  # del [1,3)
        b = {"ins": 3, "text": "x"}
        a_prime, b_prime = _edit_vs_edit(a, b)
        # q=3 >= p+m=3 → a unchanged
        assert _eq(a_prime, [{"del": 1, "len": 2}])
        # ins past del: q=3 >= p+m=3 → b' at q-m=1
        assert _eq(b_prime, [{"ins": 1, "text": "x"}])
        # Verify convergence
        s = "abcde"
        assert apply_op(apply_op(s, [b]), a_prime) == apply_op(apply_op(s, [a]), b_prime)

    def test_del_ins_q_strictly_inside(self):
        a = {"del": 1, "len": 4}  # del [1,5)
        b = {"ins": 2, "text": "x"}
        a_prime, b_prime = _edit_vs_edit(a, b)
        # q=2, p=1, m=4 → 1 <= 2 < 5 → inside → split
        assert _eq(a_prime, [{"del": 1, "len": 1}, {"del": 2, "len": 3}])
        # ins past del: q=2, p=1, m=4 → 1 < 2 < 5 → ins at p=1
        assert _eq(b_prime, [{"ins": 1, "text": "x"}])

    # --- del vs del ---

    def test_del_del_disjoint_left(self):
        a = {"del": 0, "len": 1}  # del [0,1)
        b = {"del": 2, "len": 1}  # del [2,3)
        a_prime, b_prime = _edit_vs_edit(a, b)
        # a's [0,1) doesn't overlap b's [2,3)
        # a stays at 0, len 1
        assert _eq(a_prime, [{"del": 0, "len": 1}])
        # b's [2,3): overlap_start=max(0,2)=2, overlap_end=min(1,3)=1 → no overlap
        # b at 2, but a deleted [0,1), so b shifts to 2-1=1
        assert _eq(b_prime, [{"del": 1, "len": 1}])

    def test_del_del_disjoint_right(self):
        a = {"del": 2, "len": 1}  # [2,3)
        b = {"del": 0, "len": 1}  # [0,1)
        a_prime, b_prime = _edit_vs_edit(a, b)
        # a's [2,3): b at [0,1), no overlap. a at 2, b deleted [0,1), so a shifts to 2-1=1
        assert _eq(a_prime, [{"del": 1, "len": 1}])
        # b stays at 0
        assert _eq(b_prime, [{"del": 0, "len": 1}])

    def test_del_del_contained(self):
        a = {"del": 1, "len": 2}  # [1,3)
        b = {"del": 2, "len": 1}  # [2,3)
        a_prime, b_prime = _edit_vs_edit(a, b)
        # a's [1,3) overlap with b's [2,3): overlap_start=2, overlap_end=3
        # Part before: [1,2) → del at 1, len 1
        # Part after: [3,3) → nothing
        assert _eq(a_prime, [{"del": 1, "len": 1}])
        # b's [2,3) overlap with a's [1,3): overlap_start=2, overlap_end=3
        # Part before: [2,2) → nothing
        # Part after: [3,3) → nothing
        assert _eq(b_prime, [])

    def test_del_del_containing(self):
        a = {"del": 2, "len": 1}  # [2,3)
        b = {"del": 1, "len": 3}  # [1,4)
        a_prime, b_prime = _edit_vs_edit(a, b)
        # a's [2,3) fully inside b's [1,4) → nothing remains
        assert _eq(a_prime, [])
        # b's [1,4): overlap with a's [2,3): overlap_start=2, overlap_end=3
        # Part before: [1,2) → del at 1, len 1
        # Part after: [3,4) → del at 3-1=2, len 1
        # Parts are adjacent (2 == 1+1), so they merge into one: del at 1, len 2
        assert _eq(b_prime, [{"del": 1, "len": 2}])

    def test_del_del_partial_overlap(self):
        a = {"del": 1, "len": 3}  # [1,4)
        b = {"del": 2, "len": 2}  # [2,4)
        a_prime, b_prime = _edit_vs_edit(a, b)
        # a's [1,4) overlap with b's [2,4): overlap_start=2, overlap_end=4
        # Part before: [1,2) → del at 1, len 1
        # Part after: [4,4) → nothing
        assert _eq(a_prime, [{"del": 1, "len": 1}])
        # b's [2,4) overlap with a's [1,4): overlap_start=2, overlap_end=4
        # Part before: [2,2) → nothing
        # Part after: [4,4) → nothing
        assert _eq(b_prime, [])

    def test_del_del_full_overlap_same(self):
        a = {"del": 1, "len": 2}
        b = {"del": 1, "len": 2}
        a_prime, b_prime = _edit_vs_edit(a, b)
        assert _eq(a_prime, [])
        assert _eq(b_prime, [])

    def test_del_del_adjacent(self):
        a = {"del": 0, "len": 1}
        b = {"del": 1, "len": 1}
        a_prime, b_prime = _edit_vs_edit(a, b)
        # a: [0,1), b: [1,2). No overlap.
        # a stays at 0, len 1
        assert _eq(a_prime, [{"del": 0, "len": 1}])
        # b: [1,2), no overlap with a. b shifts to 1-1=0
        assert _eq(b_prime, [{"del": 0, "len": 1}])


# ===================================================================
# Operation transform (xf) tests
# ===================================================================


class TestXf:
    def test_empty_A(self):
        A, B = xf([], [{"ins": 0, "text": "a"}])
        assert A == []
        assert B == [{"ins": 0, "text": "a"}]

    def test_empty_B(self):
        A, B = xf([{"ins": 0, "text": "a"}], [])
        assert A == [{"ins": 0, "text": "a"}]
        assert B == []

    def test_single_vs_single(self):
        A, B = xf([{"ins": 0, "text": "a"}], [{"ins": 1, "text": "b"}])
        assert _eq(A, [{"ins": 0, "text": "a"}])
        # b at 1 > a at 0 → b shifts to 1+1=2
        assert _eq(B, [{"ins": 2, "text": "b"}])

    def test_multi_A_single_B(self):
        """A=[ins 0 a, ins 0 b] B=[ins 0 c]."""
        A = [{"ins": 0, "text": "a"}, {"ins": 0, "text": "b"}]
        B = [{"ins": 0, "text": "c"}]
        A_prime, B_prime = xf(A, B)
        # Step 1: xf([a], [c])
        # a at 0, c at 0. Tie: a >= c → a' = ins 1 a.
        # Reverse: c at 0, a at 0. c > a? No → c stays at 0.
        # So a'=[ins 1 a], B1=[ins 0 c]
        # Step 2: xf([b], [ins 0 c])
        # b at 0, c at 0. Tie: b >= c → b' = ins 1 b.
        # Reverse: c at 0, b at 0. c > b? No → c stays at 0.
        # So b'=[ins 1 b], B2=[ins 0 c]
        # A' = [ins 1 a, ins 1 b], B' = [ins 0 c]
        assert _eq(A_prime, [{"ins": 1, "text": "a"}, {"ins": 1, "text": "b"}])
        assert _eq(B_prime, [{"ins": 0, "text": "c"}])
        # Verify convergence: apply(apply(s, B), A') == apply(apply(s, A), B')
        for s in ["", "x", "abc"]:
            assert apply_op(apply_op(s, B), A_prime) == apply_op(apply_op(s, A), B_prime)

    def test_single_A_multi_B(self):
        """A=[ins 0 a] B=[ins 0 c, ins 0 b]."""
        A = [{"ins": 0, "text": "a"}]
        B = [{"ins": 0, "text": "c"}, {"ins": 0, "text": "b"}]
        A_prime, B_prime = xf(A, B)
        # Step 1: xf([a], [c])
        # a at 0, c at 0. Tie: a >= c → a' = ins 1 a.
        # Reverse: c at 0, a at 0. c > a? No → c stays at 0.
        # A1 = [ins 1 a], b1' = [ins 0 c]
        # Step 2: xf([ins 1 a], [b])
        # a' at 1, b at 0. a' >= b → a'' = ins 2 a.
        # Reverse: b at 0, a'' at 2. b < a'' → b stays at 0.
        # A2 = [ins 2 a], rest' = [ins 0 b]
        # B' = [c] + [b] = [ins 0 c, ins 0 b]
        assert _eq(A_prime, [{"ins": 2, "text": "a"}])
        assert _eq(B_prime, [{"ins": 0, "text": "c"}, {"ins": 0, "text": "b"}])
        # Verify convergence: apply(apply(s, B), A') == apply(apply(s, A), B')
        for s in ["", "x", "abc"]:
            assert apply_op(apply_op(s, B), A_prime) == apply_op(apply_op(s, A), B_prime)

    def test_two_vs_two(self):
        """A=[ins 0 a, del 0 1] B=[ins 0 b, del 0 1]"""
        A = [{"ins": 0, "text": "a"}, {"del": 0, "len": 1}]
        B = [{"ins": 0, "text": "b"}, {"del": 0, "len": 1}]
        A_prime, B_prime = xf(A, B)
        # Check convergence: apply(apply("", A), B') == apply(apply("", B), A')
        text_from_A_then_Bp = apply_op(apply_op("", A), B_prime)
        text_from_B_then_Ap = apply_op(apply_op("", B), A_prime)
        assert text_from_A_then_Bp == text_from_B_then_Ap, (
            f"{text_from_A_then_Bp!r} != {text_from_B_then_Ap!r}"
        )


# ===================================================================
# Transform against history
# ===================================================================


class TestTransformAgainstHistory:
    def test_no_history(self):
        result = transform_against_history([{"ins": 0, "text": "x"}], [])
        assert result == [{"ins": 0, "text": "x"}]

    def test_one_history_entry(self):
        history = [(1, "c1", "id1", [{"ins": 0, "text": "a"}])]
        result = transform_against_history(
            [{"ins": 0, "text": "x"}], history
        )
        # x at 0 past a at 0 → tie, a stays left → x at 0+1=1
        assert result == [{"ins": 1, "text": "x"}]

    def test_multiple_history_entries(self):
        history = [
            (1, "c1", "id1", [{"ins": 0, "text": "a"}]),
            (2, "c1", "id2", [{"ins": 1, "text": "b"}]),
        ]
        result = transform_against_history(
            [{"ins": 0, "text": "x"}], history
        )
        # x at 0 past a at 0 → x at 1
        # x at 1 past b at 1 → tie, b stays left → x at 1+1=2
        assert result == [{"ins": 2, "text": "x"}]


# ===================================================================
# Randomized convergence test
# ===================================================================


class TestRandomConvergence:
    def test_convergence(self):
        """apply(apply(s, B), A') == apply(apply(s, A), B') for many random cases."""
        rng = random.Random(42)
        for _ in range(200):
            length = rng.randint(0, 10)
            text = "".join(rng.choices("abcde", k=length))

            # Generate random operations A and B
            A = self._random_op(rng, length)
            B = self._random_op(rng, length)

            A_prime, B_prime = xf(A, B)
            text_ABp = apply_op(apply_op(text, B), A_prime)
            text_BAp = apply_op(apply_op(text, A), B_prime)
            assert text_ABp == text_BAp, (
                f"Failed for text={text!r}, A={A}, B={B}, A'={A_prime}, B'={B_prime}\n"
                f"  ABp={text_ABp!r}, BAp={text_BAp!r}"
            )

    @staticmethod
    def _random_op(rng: random.Random, length: int) -> list[dict]:
        """Generate a random valid operation."""
        num_edits = rng.randint(0, 3)
        edits = []
        cur = length
        for _ in range(num_edits):
            if rng.random() < 0.5 or cur == 0:
                pos = rng.randint(0, cur)
                tlen = rng.randint(1, 3)
                edits.append({"ins": pos, "text": "".join(rng.choices("xyz", k=tlen))})
                cur += tlen
            else:
                if cur == 0:
                    continue
                pos = rng.randint(0, cur - 1)
                m = rng.randint(1, cur - pos)
                edits.append({"del": pos, "len": m})
                cur -= m
        return edits


# ===================================================================
# transform_op_against_history
# ===================================================================


class TestTransformOpAgainstHistory:
    def test_basic(self):
        history = [
            (1, "c1", "id1", [{"ins": 0, "text": "a"}]),
        ]
        A_prime, B_primes = transform_op_against_history(
            [{"ins": 0, "text": "x"}], history
        )
        assert _eq(A_prime, [{"ins": 1, "text": "x"}])
        # B' = xf([ins 0 a], [ins 0 x]) → a at 0, x at 0. Tie: a >= x → a' = ins 1 a.
        # Reverse: x at 0, a' at 0. x > a'? No → x stays at 0.
        # So B' = [ins 0 a]
        assert _eq(B_primes[0], [{"ins": 0, "text": "a"}])
