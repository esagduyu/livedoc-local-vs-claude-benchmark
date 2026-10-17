"""Shared transformation core for Livedoc OT."""

from __future__ import annotations


def validate_op(op, length):
    """Validate an operation and return the resulting text length.

    op: list of dicts, each with exactly {"ins": P, "text": T} or {"del": P, "len": N}
    length: current text length (int)
    Returns: new text length (int)
    Raises: ValueError if the op is malformed
    """
    if not isinstance(op, list):
        raise ValueError("op must be a list")
    result_len = length
    for edit in op:
        if not isinstance(edit, dict):
            raise ValueError("each edit must be a dict")
        keys = set(edit.keys())
        if keys == {"ins", "text"}:
            p = edit["ins"]
            t = edit["text"]
            if not isinstance(p, int) or isinstance(p, bool):
                raise ValueError("ins must be int")
            if not isinstance(t, str) or t == "":
                raise ValueError("text must be non-empty string")
            if p < 0 or p > result_len:
                raise ValueError("ins position out of range")
            result_len += len(t)
        elif keys == {"del", "len"}:
            p = edit["del"]
            n = edit["len"]
            if not isinstance(p, int) or isinstance(p, bool):
                raise ValueError("del position must be int")
            if not isinstance(n, int) or n < 1 or isinstance(n, bool):
                raise ValueError("del len must be int >= 1")
            if p < 0 or p + n > result_len:
                raise ValueError("del out of range")
            result_len -= n
        else:
            raise ValueError("edit must have exactly 'ins'/'text' or 'del'/'len' keys")
    return result_len


def apply_op(text, op):
    """Apply an operation to text and return the resulting text.

    op: list of edit dicts
    text: current text str
    Returns: new text str
    """
    result = text
    for edit in op:
        if "ins" in edit and "text" in edit:
            p = edit["ins"]
            t = edit["text"]
            result = result[:p] + t + result[p:]
        elif "del" in edit and "len" in edit:
            p = edit["del"]
            n = edit["len"]
            result = result[:p] + result[p + n:]
    return result


def transform_edits(a, b):
    """Transform two single edits against each other.

    Both were written against the same text. Returns (a', b') where:
    - a' is a list of edits applying after b
    - b' is a list of edits applying after a (earlier-committed side wins insert ties)
    Note: a and b are single edit dicts.
    """
    a_has_ins = "ins" in a and "del" not in a
    b_has_ins = "ins" in b and "del" not in b
    a_has_del = "del" in a
    b_has_del = "del" in b

    # Case: ins vs ins
    if a_has_ins and b_has_ins:
        p = a["ins"]
        q = b["ins"]
        u_len = len(b["text"])
        if p < q:
            a_prime = [{"ins": p, "text": a["text"]}]
        elif p > q:
            a_prime = [{"ins": p + u_len, "text": a["text"]}]
        else:
            # p == q: earlier-committed (b) stays left, a moves right
            a_prime = [{"ins": q + u_len, "text": a["text"]}]
        b_prime = [{"ins": q, "text": b["text"]}]
        return (a_prime, b_prime)

    # Case: ins (a) vs del (b)
    if "ins" in a and "del" in b:
        p = a["ins"]
        q = b["del"]
        k = b["len"]
        if p <= q:
            a_prime = [{"ins": p, "text": a["text"]}]
        elif p >= q + k:
            a_prime = [{"ins": p - k, "text": a["text"]}]
        else:
            # strictly inside delete range
            a_prime = [{"ins": q, "text": a["text"]}]
        b_prime = [b]
        return (a_prime, b_prime)

    # Case: del (a) vs ins (b)
    if "del" in a and "ins" in b:
        p = a["del"]
        m = a["len"]
        q = b["ins"]
        u = b["text"]
        u_len = len(u)
        if q <= p:
            # insert at or before delete position: delete shifts right
            a_prime = [{"del": p + u_len, "len": m}]
        elif q >= p + m:
            # insert after delete range: unchanged
            a_prime = [a]
        else:
            # insert strictly inside delete range: split a into two deletes
            left_len = q - p
            right_start = p + u_len
            right_len = p + m - q
            a_prime = [{"del": p, "len": left_len},
                       {"del": right_start, "len": right_len}]
        b_prime = [b]
        return (a_prime, b_prime)

    # Case: del (a) vs del (b)
    if a_has_del and b_has_del:
        p = a["del"]
        m = a["len"]
        q = b["del"]
        k = b["len"]

        # Non-overlapping cases
        if p + m <= q:
            # a entirely left of b, unchanged
            return ([a], [b])
        if q + k <= p:
            # b entirely left of a: a shifts left by k
            return ([{"del": p - k, "len": m}], [b])

        # Overlapping cases
        if q <= p:
            # b starts before or at a's start
            # a originally deletes [p, p+m), b deletes [q, q+k)
            # After b, a deletes from p onward, but only what's left after b's deletion
            # The remaining starts at p (since b started at or before p, and deleted k chars)
            # a's remaining range: from p to p+m-k (if m > k), or empty if m <= k
            remaining_len = m - k
            if remaining_len <= 0:
                a_prime = []
            else:
                a_prime = [{"del": p, "len": remaining_len}]
        else:
            # p < q: a starts before b
            # a originally deletes [p, p+m), b deletes [q, q+k)
            # After b, a deletes the part of its range before b starts: positions p to q-1
            # That's q - p characters, starting at position p
            remaining_len = q - p
            if remaining_len <= 0:
                a_prime = []
            else:
                a_prime = [{"del": p, "len": remaining_len}]

        return (a_prime, [b])

    # Default: wrap in lists
    return ([a], [b])


def xf(A, B):
    """Transform operation A against a history of earlier operations B.

    Returns (A', B') where A' applies after B (keeps only A'), 
    and B' applies after A.
    Equivalent to: first all of B's edits were committed in order, then A's edits arrive.
    """
    if not A:
        return (A, B)
    if not B:
        return (A, B)

    # If both have one edit:
    if len(A) == 1 and len(B) == 1:
        a_prime, b_prime = transform_edits(A[0], B[0])
        return (a_prime, b_prime)

    # If A has more than one edit:
    if len(A) > 1:
        # Transform first edit of A against all of B
        a1_prime, B1 = xf([A[0]], B)
        # Recursively transform rest of A against the transformed B1
        rest_prime, B2 = xf(A[1:], B1)
        # Concatenate a1_prime and rest_prime
        A_prime = a1_prime + rest_prime
        return (A_prime, B2)

    # If B has more than one edit:
    if len(B) > 1:
        # Transform A against first edit of B
        A1, b1_prime = xf(A, [B[0]])
        # Transform A1 against rest of B
        A2, rest_prime = xf(A1, B[1:])
        # b1_prime accumulates with rest_prime
        B_prime = b1_prime + rest_prime
        return (A2, B_prime)

    return (A, B)


def transform_against_history(op, ops):
    """Transform an operation against a history of committed operations.

    op: the incoming operation
    ops: list of earlier committed operations (in order)
    Returns: transformed op that applies after all committed ops
    """
    # Fold xf over committed ops in order, keep only A'
    A_prime = op
    for b in ops:
        A_prime, _ = xf(A_prime, [b])
    return A_prime