"""Livedoc client library with operational transformation.

This module contains the shared OT (operational transformation) functions
used by both the server and the client.
"""


def validate_op(op):
    """Validate the structure of an operation.

    An operation is a JSON list of edits. Each edit must be either:
      - {"ins": P, "text": T} where P is an int and T is a non-empty str
      - {"del": P, "len": N} where P and N are ints, N >= 1

    Booleans are rejected even though they are technically ints in Python.

    Returns True if valid, raises ValueError if invalid.
    """
    if not isinstance(op, list):
        raise ValueError("Operation must be a list")
    for edit in op:
        if not isinstance(edit, dict):
            raise ValueError("Each edit must be a dict")
        # Check for ins edit
        if "ins" in edit:
            p = edit["ins"]
            text = edit["text"]
            # Reject booleans as ints
            if isinstance(p, bool) or isinstance(text, bool):
                raise ValueError("Boolean values not allowed for ins position/text")
            if not isinstance(p, int):
                raise ValueError(f"ins position must be int, got {type(p).__name__}")
            if not isinstance(text, str):
                raise ValueError(f"ins text must be str, got {type(text).__name__}")
            if text == "":
                raise ValueError("ins text must be non-empty")
        # Check for del edit
        elif "del" in edit:
            p = edit["del"]
            length = edit["len"]
            # Reject booleans as ints
            if isinstance(p, bool) or isinstance(length, bool):
                raise ValueError("Boolean values not allowed for del position/len")
            if not isinstance(p, int):
                raise ValueError(f"del position must be int, got {type(p).__name__}")
            if not isinstance(length, int):
                raise ValueError(f"del len must be int, got {type(length).__name__}")
            if length < 1:
                raise ValueError("del len must be >= 1")
        else:
            raise ValueError("Each edit must have either 'ins' or 'del' key")
    return True


def apply_op(text, op):
    """Apply a list of edits to a text, left to right.

    Each edit is either {"ins": P, "text": T} or {"del": P, "len": N}.
    Raises ValueError if any edit is out of range.

    The position constraints:
    - ins: 0 <= P <= len(text) at the time of application
    - del: 0 <= P and P + N <= len(text) at the time of application
    """
    validate_op(op)
    result = text
    for edit in op:
        if "ins" in edit:
            p = edit["ins"]
            t = edit["text"]
            if p < 0 or p > len(result):
                raise ValueError(f"Insert position {p} out of range (text length {len(result)})")
            result = result[:p] + t + result[p:]
        elif "del" in edit:
            p = edit["del"]
            n = edit["len"]
            if p < 0 or p + n > len(result):
                raise ValueError(f"Delete range [{p}, {p+n}) out of range (text length {len(result)})")
            result = result[:p] + result[p+n:]
    return result


def xf_edit(a, b):
    """Transform a single edit 'a' against a single edit 'b'.

    Both edits were written against the same text. 'a' is being transformed
    and 'b' is earlier committed. Returns (a_list, b_list) where
    a_list applies after b_list, with insert-tie priority to the earlier-committed side.

    See SPEC.md for the full transformation rules.
    """
    a_ins = "ins" in a
    a_del = "del" in a
    b_ins = "ins" in b
    b_del = "del" in b

    # Case 1: ins vs ins
    if a_ins and b_ins:
        p = a["ins"]
        t = a["text"]
        q = b["ins"]
        u = b["text"]
        if p < q:
            return ([a], [b])
        else:  # p >= q
            new_a = {"ins": p + len(u), "text": t}
            return ([new_a], [b])

    # Case 2: ins vs del
    if a_ins and b_del:
        p = a["ins"]
        t = a["text"]
        q = b["del"]
        k = b["len"]
        if p < q:
            # a unchanged, b unchanged
            return ([a], [b])
        elif p >= q + k:
            # a shifts left by k
            new_a = {"ins": p - k, "text": t}
            return ([new_a], [b])
        elif p == q:
            # p == q: earlier-committed delete at same position;
            # shift delete start by insert text length to maintain convergence
            new_b = {"del": p + len(t), "len": k}
            return ([a], [new_b])
        else:
            # strictly inside (q < p < q+k): ins at q
            new_a = {"ins": q, "text": t}
            return ([new_a], [b])

    # Case 3: del vs ins
    if a_del and b_ins:
        p = a["del"]
        m = a["len"]
        q = b["ins"]
        u = b["text"]
        if q <= p:
            # del at p + len(u), same length
            new_a = {"del": p + len(u), "len": m}
            return ([new_a], [b])
        elif q >= p + m:
            # unchanged
            return ([a], [b])
        else:
            # strictly inside: a splits into two deletes
            left_len = q - p
            right_len = p + m - q
            result = []
            if left_len > 0:
                result.append({"del": p, "len": left_len})
            if right_len > 0:
                result.append({"del": p + len(u), "len": right_len})
            return (result, [b])

    # Case 4: del vs del
    if a_del and b_del:
        p = a["del"]
        m = a["len"]
        q = b["del"]
        k = b["len"]

        # Compute the result of transforming del a against del b.
        # The result deletes only the characters a deletes that b hasn't already deleted,
        # forming one contiguous range.

        overlap_start = max(p, q)
        overlap_end = min(p + m, q + k)

        if overlap_start >= overlap_end:
            # No overlap between the two delete ranges
            if p >= q + k:
                # a entirely after b: shift a left by k
                new_a = {"del": p - k, "len": m}
                return ([new_a], [b])
            elif q >= p + m:
                # b entirely after a: unchanged
                return ([a], [b])
            else:
                # Should not reach here, but fallback
                return ([a], [b])
        else:
            # There is overlap between the delete ranges
            # The characters a deletes that b hasn't already deleted form one contiguous range.
            if q > p:
                # b starts after a starts
                if q + k <= p + m:
                    # b is entirely within a's range (or ends before a ends)
                    # a deletes [p, q) and [q+k, p+m) in original -> combined: del at p, len m-k in transformed
                    remaining_len = m - k
                    new_a = {"del": p, "len": remaining_len}
                    return ([new_a], [b])
                else:
                    # b extends past a's end
                    # a only deletes [p, q) before b's range
                    left_len = q - p
                    if left_len > 0:
                        return ([{"del": p, "len": left_len}], [b])
                    else:
                        return ([], [b])
            else:
                # q <= p, b starts before or at a's start
                if p >= q + k:
                    # a entirely after b: shift a left by k
                    new_a = {"del": p - k, "len": m}
                    return ([new_a], [b])
                else:
                    # p is inside b's range (q <= p < q+k)
                    # After b deletes [q, q+k), original position p is in deleted area.
                    # The remaining part of a starts at original position q+k
                    # and has length p + m - q - k
                    remaining_len = p + m - q - k
                    if remaining_len <= 0:
                        return ([], [b])
                    # The contiguous range starts at q in the transformed text
                    new_a = {"del": q, "len": remaining_len}
                    return ([new_a], [b])

    # Fallback: return unchanged
    return ([a], [b])


def xf(A, B):
    """Transform operation A against earlier-committed operation B.

    Returns (A', B') where A' applies after B and B' applies after A.
    Both orderings give the same text.

    Implements the recursive definition from SPEC.md:
    - If A or B is empty: return (A, B)
    - If both have one edit: return edit-vs-edit rule
    - If A has more than one edit: (a1', B1) = xf([a1], B);
      (rest', B2) = xf(A[1:], B1); return (a1' + rest', B2)
    - If B has more than one edit: (A1, b1') = xf(A, [b1]);
      (A2, rest') = xf(A1, B[1:]); return (A2, b1' + rest')
    """
    # Handle empty operations
    if len(A) == 0:
        return (A, B)
    if len(B) == 0:
        return (A, B)

    # Both have exactly one edit
    if len(A) == 1 and len(B) == 1:
        a = A[0]
        b = B[0]
        result = xf_edit(a, b)
        # result is (a_list, b_list), return as operation lists
        return (result[0], result[1])

    # A has more than one edit
    if len(A) > 1:
        # (a1', B1) = xf([a1], B)
        # Transform single edit a1 against multi-edit B
        a1 = A[0]
        rest_A = A[1:]
        # Transform single edit a1 against each edit in B in order,
        # keeping a1_prime as a single edit dict
        a1_prime = a1.copy()
        B_transformed = []
        for b_edit in B:
            result_a, result_b = xf_edit(a1_prime, b_edit)
            a1_prime = result_a[0]  # Take the single edit, not the list
            B_transformed.extend(result_b)
        # Now transform the rest against the transformed B
        # (rest', B2) = xf(rest_A, B_transformed)
        rest_prime, B2 = xf(rest_A, B_transformed)
        # Combine: a1' + rest' as new A, B2 as new B
        new_A = [a1_prime] + rest_prime
        return (new_A, B2)

    # B has more than one edit
    if len(B) > 1:
        # (A1, b1') = xf(A, [b1])
        # (A2, rest') = xf(A1, B[1:])
        b1 = B[0]
        rest_B = B[1:]
        # Transform A against single edit b1
        A1 = []
        b1_prime_list = []
        for a_edit in A:
            result_a, result_b = xf_edit(a_edit, b1)
            A1.append(result_a[0])  # Take single edit
            b1_prime_list.extend(result_b)
        # Transform A1 against rest_B
        A2, rest_prime = xf(A1, rest_B)
        # Combine b1' + rest'
        new_b = b1_prime_list + rest_prime
        return (A2, new_b)

    return (A, B)
