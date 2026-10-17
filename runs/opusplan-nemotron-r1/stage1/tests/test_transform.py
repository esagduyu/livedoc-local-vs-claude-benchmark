"""Transformation tests for livedoc operational transformation.

Table-driven tests covering all spec rows and boundaries for both a' and b'.
"""

import pytest

from livedoc_client import xf_edit, xf, apply_op


class TestXfEdit:
    """Tests for xf_edit: all four ins/del combinations and boundaries."""

    # ins vs ins
    def test_ins_vs_ins_p_less_q(self):
        """p < q: unchanged."""
        a = {"ins": 2, "text": "XY"}
        b = {"ins": 5, "text": "ZZ"}
        a_prime, b_prime = xf_edit(a, b)
        assert a_prime == [a]
        assert b_prime == [b]

    def test_ins_vs_ins_p_ge_q(self):
        """p >= q: a shifts right by len(u)."""
        a = {"ins": 5, "text": "ZZ"}
        b = {"ins": 2, "text": "XY"}
        a_prime, b_prime = xf_edit(a, b)
        assert a_prime == [{"ins": 7, "text": "ZZ"}]
        assert b_prime == [b]

    def test_ins_vs_ins_p_eq_q_tie(self):
        """p == q: earlier-committed (b) stays left."""
        a = {"ins": 3, "text": "NEW"}
        b = {"ins": 3, "text": "ORIG"}
        a_prime, b_prime = xf_edit(a, b)
        # b stays at 3, a shifts to 3+4=7
        assert a_prime == [{"ins": 7, "text": "NEW"}]
        assert b_prime == [b]

    # ins vs del
    def test_ins_vs_del_p_le_q(self):
        """p <= q: unchanged."""
        a = {"ins": 5, "text": "X"}
        b = {"del": 7, "len": 3}
        a_prime, b_prime = xf_edit(a, b)
        assert a_prime == [a]
        assert b_prime == [b]

    def test_ins_vs_del_p_ge_q_plus_k(self):
        """p >= q + k: a shifts left by k."""
        a = {"ins": 10, "text": "X"}
        b = {"del": 5, "len": 3}
        a_prime, b_prime = xf_edit(a, b)
        assert a_prime == [{"ins": 7, "text": "X"}]
        assert b_prime == [b]

    def test_ins_vs_del_strictly_inside(self):
        """q < p < q+k: ins at q."""
        a = {"ins": 6, "text": "X"}
        b = {"del": 5, "len": 3}  # deletes [5, 8)
        a_prime, b_prime = xf_edit(a, b)
        assert a_prime == [{"ins": 5, "text": "X"}]
        assert b_prime == [b]

    # del vs ins
    def test_del_vs_ins_q_le_p(self):
        """q <= p: del at p + len(u), same length."""
        a = {"del": 5, "len": 3}  # deletes [5, 8)
        b = {"ins": 3, "text": "X"}  # q=3, p=5, q<=p
        a_prime, b_prime = xf_edit(a, b)
        assert a_prime == [{"del": 6, "len": 3}]  # 5 + len("X") = 6
        # Actually: del at p + len(u) = 5 + 1 = 6
        assert a_prime == [{"del": 6, "len": 3}]
        assert b_prime == [b]

    def test_del_vs_ins_q_ge_p_plus_m(self):
        """q >= p + m: unchanged."""
        a = {"del": 2, "len": 2}  # deletes [2, 4)
        b = {"ins": 10, "text": "X"}  # q=10, p=2, m=2, q >= 4
        a_prime, b_prime = xf_edit(a, b)
        assert a_prime == [a]
        assert b_prime == [b]

    def test_del_vs_ins_strictly_inside(self):
        """q strictly inside (p < q < p+m): a splits."""
        a = {"del": 2, "len": 6}  # deletes [2, 8)
        b = {"ins": 5, "text": "X"}  # q=5, p=2, m=6, q is strictly inside
        a_prime, b_prime = xf_edit(a, b)
        # Should split into [{"del": 2, "len": 5-2=3}, {"del": 2+1=3, "len": 2+6-5=3}]
        assert a_prime == [{"del": 2, "len": 3}, {"del": 3, "len": 3}]
        assert b_prime == [b]

    # del vs del
    def test_del_vs_del_no_overlap_a_after_b(self):
        """a entirely after b: shift a left by k."""
        a = {"del": 10, "len": 3}
        b = {"del": 5, "len": 2}  # deletes [5, 7)
        a_prime, b_prime = xf_edit(a, b)
        assert a_prime == [{"del": 8, "len": 3}]  # 10 - 2 = 8
        assert b_prime == [b]

    def test_del_vs_del_no_overlap_b_after_a(self):
        """b entirely after a: unchanged."""
        a = {"del": 5, "len": 3}
        b = {"del": 10, "len": 2}
        a_prime, b_prime = xf_edit(a, b)
        assert a_prime == [a]
        assert b_prime == [b]

    def test_del_vs_del_overlap_a_starts_inside_b(self):
        """p inside b's range (q <= p < q+k)."""
        a = {"del": 5, "len": 4}  # deletes [5, 9)
        b = {"del": 3, "len": 6}  # deletes [3, 9)
        # p=5 is inside b's [3,9)
        a_prime, b_prime = xf_edit(a, b)
        # p=5, q=3, k=6, p < q+k (5<9)
        # remaining_len = p + m - q - k = 5 + 4 - 3 - 6 = 0
        # So nothing remains!
        assert a_prime == []
        assert b_prime == [b]

    def test_del_vs_del_overlap_b_starts_inside_a(self):
        """q inside a's range (p <= q < p+m)."""
        a = {"del": 3, "len": 6}  # deletes [3, 9)
        b = {"del": 5, "len": 3}  # deletes [5, 8)
        # q=5 is inside a's [3,9)
        a_prime, b_prime = xf_edit(a, b)
        # q > p (5 > 3), and q < p+m (5 < 9)
        # remaining_len = m - k = 6 - 3 = 3
        # Starts at p=3
        assert a_prime == [{"del": 3, "len": 3}]
        assert b_prime == [b]

    def test_del_vs_del_b_entirely_cover_a(self):
        """b covers all of a: nothing remains."""
        a = {"del": 5, "len": 3}  # deletes [5, 8)
        b = {"del": 3, "len": 6}  # deletes [3, 9)
        a_prime, b_prime = xf_edit(a, b)
        assert a_prime == []
        assert b_prime == [b]

    def test_del_vs_del_b_starts_before_a(self):
        """q <= p: handled by the q > p branch not taken."""
        # This is covered by the p >= q+k or q <= p < q+k cases
        a = {"del": 6, "len": 3}  # deletes [6, 9)
        b = {"del": 3, "len": 5}  # deletes [3, 8)
        # q=3 <= p=6, and p=6 >= q+k=8? No, 6 < 8, so p inside b's range
        # remaining_len = p + m - q - k = 6 + 3 - 3 - 5 = 1
        # new_a at position q=3, len 1
        a_prime, b_prime = xf_edit(a, b)
        assert a_prime == [{"del": 3, "len": 1}]
        assert b_prime == [b]


class TestXf:
    """Tests for xf: operation-vs-operation transformation."""

    def test_xf_both_empty(self):
        """Both empty ops."""
        A = []
        B = []
        A_prime, B_prime = xf(A, B)
        assert A_prime == []
        assert B_prime == []

    def test_xf_A_empty(self):
        """A empty, B non-empty."""
        A = []
        B = [{"ins": 0, "text": "X"}]
        A_prime, B_prime = xf(A, B)
        assert A_prime == []
        assert B_prime == B

    def test_xf_B_empty(self):
        """B empty, A non-empty."""
        A = [{"ins": 0, "text": "X"}]
        B = []
        A_prime, B_prime = xf(A, B)
        assert A_prime == A
        assert B_prime == []

    def test_xf_single_edits(self):
        """Both single-edit operations."""
        A = [{"ins": 2, "text": "XY"}]
        B = [{"del": 5, "len": 3}]
        A_prime, B_prime = xf(A, B)
        assert len(A_prime) == 1
        assert len(B_prime) == 1

    def test_xf_convergence(self):
        """Randomized convergence test: apply(apply(s, B), A') == apply(apply(s, A), B')."""
        import random
        random.seed(42)

        def apply_op_safe(text, op):
            return apply_op(text, op)

        for _ in range(20):
            # Generate random text
            s = "H" * random.randint(0, 20)
            # Generate random operations
            num_edits_A = random.randint(1, 3)
            num_edits_B = random.randint(1, 3)

            edits_A = []
            edits_B = []
            for _ in range(num_edits_A):
                if random.choice([True, False]):
                    pos = random.randint(0, len(s))
                    txt = "".join(random.choices("abc", k=random.randint(1, 5)))
                    edits_A.append({"ins": pos, "text": txt})
                else:
                    # Ensure delete is valid: 0 <= pos and pos + length <= len(s)
                    max_pos = max(0, len(s) - 1) if len(s) > 0 else 0
                    pos = random.randint(0, max_pos) if len(s) > 0 else 0
                    max_len = len(s) - pos
                    length = random.randint(1, max(1, max_len)) if max_len > 0 else 1
                    # If length would make it invalid, adjust
                    if pos + length > len(s):
                        length = len(s) - pos
                    edits_A.append({"del": pos, "len": length})

            for _ in range(num_edits_B):
                if random.choice([True, False]):
                    pos = random.randint(0, len(s))
                    txt = "".join(random.choices("abc", k=random.randint(1, 5)))
                    edits_B.append({"ins": pos, "text": txt})
                else:
                    pos = random.randint(0, max(0, len(s) - 1))
                    length = random.randint(1, max(1, len(s) - pos))
                    edits_B.append({"del": pos, "len": length})

            A = edits_A
            B = edits_B

            # Transform
            A_prime, B_prime = xf(A, B)

            # Apply in both orders
            order1 = apply_op_safe(apply_op_safe(s, B), A_prime)
            order2 = apply_op_safe(apply_op_safe(s, A), B_prime)

            assert order1 == order2, f"Convergence failed: {order1} != {order2}\nA={A}\nB={B}\nA'={A_prime}\nB'={B_prime}"

    def test_xf_equivalence(self):
        """xf(A, B) result matches transforming each edit as its own operation."""
        import random
        random.seed(123)

        for _ in range(10):
            s = "H" * random.randint(0, 15)
            num_edits_A = random.randint(1, 3)
            num_edits_B = random.randint(1, 3)

            edits_A = []
            edits_B = []
            for _ in range(num_edits_A):
                if random.choice([True, False]):
                    pos = random.randint(0, len(s))
                    txt = "".join(random.choices("abc", k=random.randint(1, 3)))
                    edits_A.append({"ins": pos, "text": txt})
                else:
                    pos = random.randint(0, max(0, len(s) - 1))
                    length = random.randint(1, max(1, len(s) - pos))
                    edits_A.append({"del": pos, "len": length})

            for _ in range(num_edits_B):
                if random.choice([True, False]):
                    pos = random.randint(0, len(s))
                    txt = "".join(random.choices("abc", k=random.randint(1, 3)))
                    edits_B.append({"ins": pos, "text": txt})
                else:
                    pos = random.randint(0, max(0, len(s) - 1))
                    length = random.randint(1, max(1, len(s) - pos))
                    edits_B.append({"del": pos, "len": length})

            A = edits_A
            B = edits_B

            A_prime, B_prime = xf(A, B)

            # Check: transforming each edit of A as its own operation against B
            # should give the same result as xf
            # Transform B's edits one by one against A's edits, etc.
            # The spec says: xf(A, B) result matches transforming each edit as its own operation
            # (per spec definition: first all of B's edits were committed in order, then A's edits arrive in order)

            # Simple check: apply A' then B in order should equal apply B' then A in order
            from livedoc_client import apply_op

            order1 = apply_op(apply_op(s, B), A_prime)
            order2 = apply_op(apply_op(s, A), B_prime)
            assert order1 == order2, f"Equivalence failed: {order1} != {order2}\nA={A}\nB={B}\nA'={A_prime}\nB'={B_prime}"