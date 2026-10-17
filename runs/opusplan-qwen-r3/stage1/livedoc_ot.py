"""
Livedoc Operational Transformation core.

Edit-vs-edit transform, operation transform, apply, and validation.
All functions are pure and deterministic.
"""

from __future__ import annotations

from typing import Any


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _validate_edit(edit: Any) -> None:
    """Raise ValueError if *edit* is malformed."""
    if not isinstance(edit, dict):
        raise ValueError("edit must be a dict")
    keys = set(edit.keys())
    if keys == {"ins", "text"}:
        p = edit["ins"]
        t = edit["text"]
        if isinstance(p, bool) or not isinstance(p, int):
            raise ValueError(f"ins position must be int, got {type(p).__name__}")
        if not isinstance(t, str) or len(t) == 0:
            raise ValueError(f"ins text must be non-empty str, got {type(t).__name__}")
    elif keys == {"del", "len"}:
        p = edit["del"]
        n = edit["len"]
        if isinstance(p, bool) or not isinstance(p, int):
            raise ValueError(f"del position must be int, got {type(p).__name__}")
        if isinstance(n, bool) or not isinstance(n, int):
            raise ValueError(f"del len must be int, got {type(n).__name__}")
        if n < 1:
            raise ValueError(f"del len must be >= 1, got {n}")
    else:
        raise ValueError(f"edit must have {{'ins','text'}} or {{'del','len'}}, got {keys}")


def _validate_op(op: Any, length: int) -> int:
    """Validate *op* against text of *length* and return the new length.

    Returns the resulting text length after applying all edits.
    Raises ``ValueError`` on malformed edits or bounds violations.
    """
    if not isinstance(op, list):
        raise ValueError("op must be a list")
    cur = length
    for edit in op:
        _validate_edit(edit)
        if "ins" in edit:
            p = edit["ins"]
            if p < 0 or p > cur:
                raise ValueError(
                    f"ins position {p} out of range [0, {cur}]"
                )
            cur += len(edit["text"])
        else:  # del
            p = edit["del"]
            n = edit["len"]
            if p < 0 or p + n > cur:
                raise ValueError(
                    f"del [{p},{p+n}) out of range [0, {cur}]"
                )
            cur -= n
    return cur


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------

def apply_op(text: str, op: list[dict]) -> str:
    """Apply *op* to *text* and return the resulting string."""
    result = text
    for edit in op:
        if "ins" in edit:
            result = result[: edit["ins"]] + edit["text"] + result[edit["ins"] :]
        else:
            result = result[: edit["del"]] + result[edit["del"] + edit["len"] :]
    return result


# ---------------------------------------------------------------------------
# Edit-vs-edit transform
# ---------------------------------------------------------------------------

def _edit_vs_edit(a: dict, b: dict) -> tuple[list[dict], list[dict]]:
    """Transform edit *a* against earlier-committed edit *b*.

    Returns ``(a', b')`` where ``a'`` applies after ``b`` was applied,
    and ``b'`` applies after ``a`` was applied (reversed direction).
    """
    a_is_ins = "ins" in a
    b_is_ins = "ins" in b

    # ---- ins vs ins ----
    if a_is_ins and b_is_ins:
        pa, ta = a["ins"], a["text"]
        pb, tb = b["ins"], b["text"]
        # Forward: a moves past b. On tie (pa == pb), b stays left (lower position).
        if pa >= pb:
            a_prime = [{"ins": pa + len(tb), "text": ta}]
        else:
            a_prime = [{"ins": pa, "text": ta}]
        # Reverse: b moves past a. Same rules with roles reversed.
        # "leaves it unchanged when q <= p and shifts it when q > p"
        if pb > pa:
            b_prime = [{"ins": pb + len(ta), "text": tb}]
        else:
            b_prime = [{"ins": pb, "text": tb}]
        return a_prime, b_prime

    # ---- ins vs del ----
    if a_is_ins and not b_is_ins:
        pa, ta = a["ins"], a["text"]
        pb, nb = b["del"], b["len"]
        # Forward
        if pa <= pb:
            a_prime = [{"ins": pa, "text": ta}]
        elif pa >= pb + nb:
            a_prime = [{"ins": pa - nb, "text": ta}]
        else:
            a_prime = [{"ins": pb, "text": ta}]
        # Reverse: b (del) moves past a (ins)
        if pa <= pb:
            b_prime = [{"del": pb + len(ta), "len": nb}]
        elif pa >= pb + nb:
            b_prime = [{"del": pb, "len": nb}]
        else:
            b_prime = [
                {"del": pb, "len": pa - pb},
                {"del": pb + len(ta), "len": pb + nb - pa},
            ]
        return a_prime, b_prime

    # ---- del vs ins ----
    if not a_is_ins and b_is_ins:
        pa, ma = a["del"], a["len"]
        pb, tb = b["ins"], b["text"]
        # Forward: a (del) moves past b (ins)
        if pb <= pa:
            a_prime = [{"del": pa + len(tb), "len": ma}]
        elif pb >= pa + ma:
            a_prime = [{"del": pa, "len": ma}]
        else:
            a_prime = [
                {"del": pa, "len": pb - pa},
                {"del": pa + len(tb), "len": pa + ma - pb},
            ]
        # Reverse: b (ins) moves past a (del)
        # b is an insert; after a deletes [pa, pa+ma), the position pb shifts.
        # If pb <= pa: b is before the delete, so position stays pb - ma? No.
        #   The text before pa is unchanged. pb < pa means b's insert is before a's delete.
        #   When a's delete happens, everything from pa onward shifts left by ma.
        #   But b is at pb < pa, so b stays at pb.
        #   Wait - we need b' to apply after a. After a deletes [pa, pa+ma),
        #   the text has shrunk. b's insert at pb (pb <= pa) is before the deleted region,
        #   so it stays at pb.
        # If pb >= pa + ma: b is after the deleted region. After a, the position
        #   pb shifts to pb - ma.
        # If pa < pb < pa + ma: b is inside a's range. After a deletes [pa, pa+ma),
        #   b's insert point (which was inside) is now at pa.
        if pb <= pa:
            b_prime = [{"ins": pb, "text": tb}]
        elif pb >= pa + ma:
            b_prime = [{"ins": pb - ma, "text": tb}]
        else:
            b_prime = [{"ins": pa, "text": tb}]
        return a_prime, b_prime

    # ---- del vs del ----
    if not a_is_ins and not b_is_ins:
        pa, ma = a["del"], a["len"]
        pb, nb = b["del"], b["len"]

        # a' after b: b deleted [pb, pb+nb). Map a's range [pa, pa+ma) through b.
        # The overlap of [pa,pa+ma) and [pb,pb+nb):
        overlap_start = max(pa, pb)
        overlap_end = min(pa + ma, pb + nb)

        a_primes: list[dict] = []
        if overlap_start < overlap_end:
            # There is overlap
            # Part before overlap: [pa, overlap_start)
            part1_len = overlap_start - pa
            # Part after overlap: [overlap_end, pa+ma), shifted by -nb
            part2_start = overlap_end - nb
            part2_len = pa + ma - overlap_end
            if part1_len >= 1 and part2_len >= 1:
                # Check if parts are adjacent in the post-b text
                if part2_start == pa + part1_len:
                    # Merge into one edit
                    a_primes.append({"del": pa, "len": part1_len + part2_len})
                else:
                    a_primes.append({"del": pa, "len": part1_len})
                    a_primes.append({"del": part2_start, "len": part2_len})
            elif part1_len >= 1:
                a_primes.append({"del": pa, "len": part1_len})
            elif part2_len >= 1:
                a_primes.append({"del": part2_start, "len": part2_len})
        else:
            # No overlap
            if pa >= pb + nb:
                # a is entirely after b's deletion → shift left by nb
                a_primes.append({"del": pa - nb, "len": ma})
            else:
                # a is entirely before b's deletion → no shift
                a_primes.append({"del": pa, "len": ma})

        # b' after a: a deleted [pa, pa+ma). Map b's range [pb, pb+nb) through a.
        overlap_start2 = max(pa, pb)
        overlap_end2 = min(pa + ma, pb + nb)

        b_primes: list[dict] = []
        if overlap_start2 < overlap_end2:
            # There is overlap
            # Part before overlap: [pb, overlap_start2)
            part1_len = overlap_start2 - pb
            # Part after overlap: [overlap_end2, pb+nb), shifted by -ma
            part2_start = overlap_end2 - ma
            part2_len = pb + nb - overlap_end2
            if part1_len >= 1 and part2_len >= 1:
                # Check if parts are adjacent in the post-a text
                if part2_start == pb + part1_len:
                    # Merge into one edit
                    b_primes.append({"del": pb, "len": part1_len + part2_len})
                else:
                    b_primes.append({"del": pb, "len": part1_len})
                    b_primes.append({"del": part2_start, "len": part2_len})
            elif part1_len >= 1:
                b_primes.append({"del": pb, "len": part1_len})
            elif part2_len >= 1:
                b_primes.append({"del": part2_start, "len": part2_len})
        else:
            # No overlap
            if pb >= pa + ma:
                # b is entirely after a's deletion → shift left by ma
                b_primes.append({"del": pb - ma, "len": nb})
            else:
                # b is entirely before a's deletion → no shift
                b_primes.append({"del": pb, "len": nb})

        return a_primes, b_primes

    raise RuntimeError("impossible edit types")


# ---------------------------------------------------------------------------
# Operation transform (recursive, per spec pseudocode)
# ---------------------------------------------------------------------------

def xf(A: list[dict], B: list[dict]) -> tuple[list[dict], list[dict]]:
    """Transform operation A against earlier-committed operation B.

    Returns ``(A', B')`` exactly as specified in the spec.
    """
    if not A or not B:
        return list(A), list(B)

    if len(A) == 1 and len(B) == 1:
        a_prime, b_prime = _edit_vs_edit(A[0], B[0])
        return a_prime, b_prime

    if len(A) > 1:
        # (a1', B1) = xf([a1], B); (rest', B2) = xf(A[1:], B1); return (a1'+rest', B2)
        a1_prime, B1 = xf([A[0]], B)
        rest_prime, B2 = xf(A[1:], B1)
        return a1_prime + rest_prime, B2

    # len(B) > 1 and len(A) == 1
    # (A1, b1') = xf(A, [b1]); (A2, rest') = xf(A1, B[1:]); return (A2, b1'+rest')
    A1, b1_prime = xf(A, [B[0]])
    A2, rest_prime = xf(A1, B[1:])
    return A2, b1_prime + rest_prime


# ---------------------------------------------------------------------------
# Transform against history
# ---------------------------------------------------------------------------

def transform_against_history(
    op: list[dict], ops: list[tuple[int, str, str, list[dict]]]
) -> list[dict]:
    """Fold ``xf`` over committed operations, returning only A'.

    *ops* is a list of ``(rev, client, id, op)`` in commit order.
    """
    A = list(op)
    for _rev, _client, _id, B in ops:
        A, _B = xf(A, B)
    return A


def transform_op_against_history(op: list[dict], ops: list[tuple[int, str, str, list[dict]]]) -> tuple[list[dict], list[list[dict]]]:
    """Transform *op* against history and return (A', list of B')."""
    A = list(op)
    B_primes: list[list[dict]] = []
    for _rev, _client, _id, B in ops:
        A, B_prime = xf(A, B)
        B_primes.append(B_prime)
    return A, B_primes
