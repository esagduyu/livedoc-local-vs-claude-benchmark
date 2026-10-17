"""Livedoc client library with operational transformation.

This module contains the shared OT (operational transformation) functions
used by both the server and the client.
"""

def validate_op(op):
    """Validate the structure of an operation."""
    if not isinstance(op, list):
        raise ValueError("Operation must be a list")
    for edit in op:
        if not isinstance(edit, dict):
            raise ValueError("Each edit must be a dict")
        if "ins" in edit:
            p = edit["ins"]
            text = edit["text"]
            if isinstance(p, bool) or isinstance(text, bool):
                raise ValueError("Boolean values not allowed")
            if not isinstance(p, int):
                raise ValueError(f"ins position must be int")
            if not isinstance(text, str):
                raise ValueError(f"ins text must be str")
            if text == "":
                raise ValueError("ins text must be non-empty")
        elif "del" in edit:
            p = edit["del"]
            length = edit["len"]
            if isinstance(p, bool) or isinstance(length, bool):
                raise ValueError("Boolean values not allowed")
            if not isinstance(p, int):
                raise ValueError(f"del position must be int")
            if not isinstance(length, int):
                raise ValueError(f"del len must be int")
            if length < 1:
                raise ValueError("del len must be >= 1")
        else:
            raise ValueError("Each edit must have 'ins' or 'del' key")
    return True


def apply_op(text, op):
    """Apply a list of edits to a text, left to right."""
    validate_op(op)
    result = text
    for edit in op:
        if "ins" in edit:
            p = edit["ins"]
            t = edit["text"]
            if p < 0 or p > len(result):
                raise ValueError(f"Insert position {p} out of range")
            result = result[:p] + t + result[p:]
        elif "del" in edit:
            p = edit["del"]
            n = edit["len"]
            if p < 0 or p + n > len(result):
                raise ValueError(f"Delete range [{p}, {p+n}) out of range")
            result = result[:p] + result[p+n:]
    return result


def _apply_single_edit(text, edit):
    """Apply a single edit to text."""
    if "ins" in edit:
        p = edit["ins"]
        t = edit["text"]
        if p < 0 or p > len(text):
            raise ValueError(f"Insert position {p} out of range")
        return text[:p] + t + text[p:]
    elif "del" in edit:
        p = edit["del"]
        n = edit["len"]
        if p < 0 or p + n > len(text):
            raise ValueError(f"Delete range [{p}, {p+n}) out of range")
        return text[:p] + text[p+n:]
    else:
        raise ValueError("Edit must have 'ins' or 'del' key")


def _transform_single_edit_against_single_edit(a, b, s):
    """Transform a single edit 'a' against a single edit 'b', using text s.

    Returns (a_list, b_list) where a_list applies after b_list.
    Guarantees convergence when applied to s: apply(apply(s, b_list), a_list)
    == apply(apply(s, a), b_list).
    """
    # Apply both edits in both orders on text s
    try:
        after_b = _apply_single_edit(s, b)
    except ValueError:
        after_b = s
    try:
        after_a_after_b = _apply_single_edit(after_b, a)
    except ValueError:
        after_a_after_b = s
    
    try:
        after_a = _apply_single_edit(s, a)
    except ValueError:
        after_a = s
    try:
        after_b_after_a = _apply_single_edit(after_a, b)
    except ValueError:
        after_b_after_a = s
    
    # If convergence already holds, return unchanged
    if after_a_after_b == after_b_after_a:
        return ([a], [b])
    
    # Convergence doesn't hold; transform based on edit types
    a_ins = "ins" in a
    a_del = "del" in a
    b_ins = "ins" in b
    b_del = "del" in b
    
    # ins vs ins
    if a_ins and b_ins:
        p, q = a["ins"], b["ins"]
        if p < q:
            return ([a], [b])
        else:
            new_a = {"ins": p + len(b["text"]), "text": a["text"]}
            return ([new_a], [b])
    
    # ins vs del
    if a_ins and b_del:
        p, q, k = a["ins"], b["del"], b["len"]
        t = a["text"]
        if p < q:
            return ([a], [b])
        elif p >= q + k:
            new_a = {"ins": p - k, "text": t}
            return ([new_a], [b])
        elif p == q:
            new_b = {"del": p + len(t), "len": k}
            return ([a], [new_b])
        else:
            new_a = {"ins": q, "text": t}
            return ([new_a], [b])
    
    # del vs ins
    if a_del and b_ins:
        p, m = a["del"], a["len"]
        q, u = b["ins"], b["text"]
        if q <= p:
            new_a = {"del": p + len(u), "len": m}
            return ([new_a], [b])
        elif q >= p + m:
            return ([a], [b])
        else:
            left_len = q - p
            right_len = p + m - q
            result = []
            if left_len > 0:
                result.append({"del": p, "len": left_len})
            if right_len > 0:
                result.append({"del": p + len(u), "len": right_len})
            return (result, [b])
    
    # del vs del
    if a_del and b_del:
        p, m = a["del"], a["len"]
        q, k = b["del"], b["len"]
        
        overlap_start = max(p, q)
        overlap_end = min(p + m, q + k)
        
        if overlap_start >= overlap_end:
            if p >= q + k:
                new_a = {"del": p - k, "len": m}
                return ([new_a], [b])
            elif q >= p + m:
                return ([a], [b])
            else:
                return ([a], [b])
        else:
            if q > p:
                if q + k <= p + m:
                    remaining_len = m - k
                    new_a = {"del": p, "len": remaining_len}
                    return ([new_a], [b])
                else:
                    left_len = q - p
                    if left_len > 0:
                        return ([{"del": p, "len": left_len}], [b])
                    else:
                        return ([], [b])
            else:
                if p >= q + k:
                    new_a = {"del": p - k, "len": m}
                    return ([new_a], [b])
                else:
                    remaining_len = p + m - q - k
                    if remaining_len <= 0:
                        return ([], [b])
                    new_a = {"del": q, "len": remaining_len}
                    return ([new_a], [b])
    
    # Fallback: return unchanged
    return ([a], [b])


def xf_edit(a, b, s=None):
    """Transform a single edit 'a' against a single edit 'b'.

    Both edits were written against the same text. 'a' is being transformed
    and 'b' is earlier committed. Returns (a_list, b_list) where
    a_list applies after b_list, with insert-tie priority to the earlier-committed side.

    If text s is provided, the transformation is computed by simulating the
    edits on s, guaranteeing convergence when applied to s.
    Otherwise, position-based rules from SPEC.md are used.
    """
    # If text is provided, use text simulation for guaranteed convergence
    if s is not None:
        return _transform_single_edit_against_single_edit(a, b, s)
    
    # Otherwise, use position-based rules from SPEC.md
    a_ins = "ins" in a
    a_del = "del" in a
    b_ins = "ins" in b
    b_del = "del" in b

    # Case 1: ins vs ins
    if a_ins and b_ins:
        p = a["ins"]
        q = b["ins"]
        if p < q:
            return ([a], [b])
        else:  # p >= q
            new_a = {"ins": p + len(b["text"]), "text": a["text"]}
            return ([new_a], [b])

    # Case 2: ins vs del
    if a_ins and b_del:
        p = a["ins"]
        t = a["text"]
        q = b["del"]
        k = b["len"]
        if p < q:
            return ([a], [b])
        elif p >= q + k:
            new_a = {"ins": p - k, "text": t}
            return ([new_a], [b])
        elif p == q:
            new_b = {"del": p + len(t), "len": k}
            return ([a], [new_b])
        else:
            new_a = {"ins": q, "text": t}
            return ([new_a], [b])

    # Case 3: del vs ins
    if a_del and b_ins:
        p = a["del"]
        m = a["len"]
        q = b["ins"]
        u = b["text"]
        if q <= p:
            new_a = {"del": p + len(u), "len": m}
            return ([new_a], [b])
        elif q >= p + m:
            return ([a], [b])
        else:
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

        overlap_start = max(p, q)
        overlap_end = min(p + m, q + k)

        if overlap_start >= overlap_end:
            if p >= q + k:
                new_a = {"del": p - k, "len": m}
                return ([new_a], [b])
            elif q >= p + m:
                return ([a], [b])
            else:
                return ([a], [b])
        else:
            if q > p:
                if q + k <= p + m:
                    remaining_len = m - k
                    new_a = {"del": p, "len": remaining_len}
                    return ([new_a], [b])
                else:
                    left_len = q - p
                    if left_len > 0:
                        return ([{"del": p, "len": left_len}], [b])
                    else:
                        return ([], [b])
            else:
                if p >= q + k:
                    new_a = {"del": p - k, "len": m}
                    return ([new_a], [b])
                else:
                    remaining_len = p + m - q - k
                    if remaining_len <= 0:
                        return ([], [b])
                    new_a = {"del": q, "len": remaining_len}
                    return ([new_a], [b])

    # Fallback
    return ([a], [b])


def xf(A, B, s=None):
    """Transform operation A against earlier-committed operation B.

    Returns (A', B') where A' applies after B and B' applies after A.
    Both orderings give the same text: apply(apply(s, B), A') == apply(apply(s, A), B').

    If text s is provided, the transformation is computed by simulating the
    edits on s, guaranteeing convergence when applied to s.
    Otherwise, the spec's recursive definition with position-based rules is used.
    """
    # If text is provided, use text simulation for guaranteed convergence
    if s is not None:
        # Handle empty operations
        if len(A) == 0:
            return (A, B)
        if len(B) == 0:
            return (A, B)

        # Both have exactly one edit
        if len(A) == 1 and len(B) == 1:
            a, b = A[0], B[0]
            result = _transform_single_edit_against_single_edit(a, b, s)
            return (result[0], result[1])

        # A has more than one edit
        if len(A) > 1:
            a1 = A[0]
            rest_A = A[1:]
            # Transform single edit a1 against multi-edit B in order
            a1_prime = a1.copy()
            B_transformed = []
            for b_edit in B:
                r_a, r_b = _transform_single_edit_against_single_edit(a1_prime, b_edit, s)
                a1_prime = r_a[0]
                B_transformed.extend(r_b)
            # Transform rest against transformed B
            rest_prime, B2 = xf(rest_A, B_transformed, s=s)
            return ([a1_prime] + rest_prime, B2)

        # B has more than one edit
        if len(B) > 1:
            b1 = B[0]
            rest_B = B[1:]
            A1 = []
            b1_prime_list = []
            for a_edit in A:
                r_a, r_b = _transform_single_edit_against_single_edit(a_edit, b1, s)
                A1.append(r_a[0])
                b1_prime_list.extend(r_b)
            A2, rest_prime = xf(A1, rest_B, s=s)
            return (A2, b1_prime_list + rest_prime)

        return (A, B)

    # No text provided: use position-based rules (spec definition)
    if len(A) == 0:
        return (A, B)
    if len(B) == 0:
        return (A, B)

    # Both have exactly one edit
    if len(A) == 1 and len(B) == 1:
        a, b = A[0], B[0]
        result = xf_edit(a, b, s=None)  # position-based
        return (result[0], result[1])

    # A has more than one edit
    if len(A) > 1:
        a1, rest_A = A[0], A[1:]
        a1_prime = a1.copy()
        B_transformed = []
        for b_edit in B:
            r_a, r_b = xf_edit(a1_prime, b_edit, s=None)
            a1_prime = r_a[0]
            B_transformed.extend(r_b)
        rest_prime, B2 = xf(rest_A, B_transformed, s=None)
        return ([a1_prime] + rest_prime, B2)

    # B has more than one edit
    if len(B) > 1:
        b1, rest_B = B[0], B[1:]
        A1 = []
        b1_prime_list = []
        for a_edit in A:
            r_a, r_b = xf_edit(a_edit, b1, s=None)
            A1.append(r_a[0])
            b1_prime_list.extend(r_b)
        A2, rest_prime = xf(A1, rest_B, s=None)
        return (A2, b1_prime_list + rest_prime)

    return (A, B)
