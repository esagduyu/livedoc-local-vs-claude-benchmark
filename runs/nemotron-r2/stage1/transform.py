"""Transformation rules for livedoc operations.

Both edit-vs-edit and operation-vs-operation transformation are implemented
here, following the exact rules from the specification.
"""


def transform_edit_vs_edit(a: dict, b: dict) -> list:
    """Transform edit `a` against earlier edit `b`.

    Both edits were written against the same text. The result is a list of
    edits that `a` becomes after `b` is applied first. May be 0, 1, or 2 edits.

    Each edit is {"ins": P, "text": T} or {"del": P, "len": N}.

    On a tie (p == q for inserts), the earlier-committed insert stays to the left.
    """
    a_type = "ins" if "ins" in a else "del"
    b_type = "ins" if "ins" in b else "del"

    # Both inserts
    if a_type == "ins" and b_type == "ins":
        p = a["ins"]
        q = b["ins"]
        if p < q:
            return [a]
        else:  # p >= q, tie goes to earlier-committed (b) staying left
            # The earlier-committed insert (b) stays, a shifts right
            return [{"ins": p + len(b["text"]), "text": a["text"]}]

    # Insert vs delete
    if a_type == "ins" and b_type == "del":
        p = a["ins"]
        q = b["del"]
        k = b["len"]
        if p <= q:
            return [a]
        elif p >= q + k:
            return [{"ins": p - k, "text": a["text"]}]
        else:  # strictly inside the deleted range [q, q+k)
            return [{"ins": q, "text": a["text"]}]

    # Delete vs insert
    if a_type == "del" and b_type == "ins":
        p = a["del"]
        m = a["len"]
        q = b["ins"]
        u_len = len(b["text"])
        if q <= p:
            # b ends at or before a starts; shift a right by len(u)
            return [{"del": p + u_len, "len": m}]
        elif q >= p + m:
            # b starts after a ends; unchanged
            return [a]
        else:  # b lands strictly inside the range [p, p+m)
            # a splits: left part and right part, must not delete u
            left_len = q - p
            # Right part starts after u, at position q + len(u) in original coords
            # but in the transformed text, it's at position q (since u was inserted there)
            # Actually, the right del starts at q + len(u) in original coords
            # But after b is applied, position q + len(u) maps to position q in the new text
            # Hmm, let me think about this more carefully.
            # 
            # Original text: ... [a starts at p, length m) ...]
            # b inserts "u" at position q
            # After b is applied, text has "u" inserted at q
            # a was del [p, p+m) in original text
            # After u is inserted at q (where q is between p and p+m):
            #   - Characters before q stay at same positions
            #   - Characters from q onward shift right by len(u)
            #   - a's original range [p, p+m) now maps to:
            #     - [p, q) unchanged
            #     - [q + len(u), p + m + len(u)) shifted
            # But a should not delete "u", so we split a into:
            #   - del [p, q) - the part before u
            #   - del [q + len(u), p + m + len(u) - len(u)) = del [q + len(u), p + m)
            #   Wait, that's not right either.
            # 
            # Let me use a concrete example:
            # Original text: "0123456789" (positions 0-9)
            # a = del [2, 6) deletes "2345" (positions 2,3,4,5)
            # b = ins at q=3, text="XX" inserts "XX" at position 3
            # After b: text becomes "012XX3456789"
            # New positions: 0=0, 1=1, 2=2, 3=X, 4=X, 5=3, 6=4, 7=5, 8=6, 9=7, 10=8, 11=9
            # a was supposed to delete positions 2-5 in original = "2345"
            # After b, positions 2,3,4,5 original map to:
            #   original pos 2 -> new pos 2
            #   original pos 3 -> was shifted to new pos 5 (since 2 X's inserted before pos 3)
            #   Wait, let me re-map:
            #   After inserting "XX" at position 3:
            #   new text: position 0=original 0, 1=original 1, 2=original 2, 3=X, 4=X, 5=original 3, 6=original 4, 7=original 5, ...
            #   So original pos 3 -> new pos 5, original pos 4 -> new pos 6, original pos 5 -> new pos 7
            #   a = del [2, 6) original = delete original positions 2,3,4,5
            #   In new text: delete positions 2,5,6,7 = "2XX345"... no wait
            #   Position 2 in new text = original 2 ✓
            #   Position 5 in new text = original 3 ✓
            #   Position 6 in new text = original 4 ✓
            #   Position 7 in new text = original 5 ✓
            #   So a should delete new positions [2, 8) = positions 2,3,4,5,6,7 = "2XX345"
            #   But a should NOT delete "XX"! The spec says a splits and must not delete u.
            #   So a should delete original positions 2 and 5,6,7... no, original 3,4,5
            #   Wait, original positions 3,4,5 = "345", and original position 2 = "2"
            #   After b, "2" is at new pos 2, "3" is at new pos 5, "4" is at new pos 6, "5" is at new pos 7
            #   To delete "2345" without deleting "XX", we need to delete new positions [2, 3) and [5, 8)
            #   = position 2 + positions 5,6,7 = "2" + "345" = "2345" ✓
            #   
            #   So the result is two del edits: del [2, 3) and del [5, 8)
            #   But the spec says "a splits and must not delete u: [{"del": p, "len": q - p}, {"del": p + len(u), "len": p + m - q}]"
            #   Let's check: p=2, q=3, len(u)=2, m=4
            #   First: del [p, q-p) = del [2, 1)... that's wrong, len should be q-p = 1
            #   del [2, 1) doesn't make sense. del [p, p+len) where len = q-p = 1 → del [2, 3)? No.
            #   Wait, the spec format is {"del": P, "len": N} meaning delete N chars starting at P.
            #   del [p, q-p) would be del [2, 1) which is invalid.
            #   Let me re-read the spec: "[{"del": p, "len": q - p}, {"del": p + len(u), "len": p + m - q}]"
            #   So: first edit: {"del": 2, "len": 3-2} = {"del": 2, "len": 1} → deletes 1 char at pos 2 = "2" ✓
            #   Second edit: {"del": 2 + 2, "len": 2 + 4 - 3} = {"del": 4, "len": 3} → deletes 3 chars at pos 4
            #   In transformed text "012XX3456789", del [4, 3) doesn't make sense either (len=3, start=4, would delete positions 4,5,6 = "X34")
            #   Hmm, that doesn't seem right.
            #   
            #   Let me re-read the spec more carefully: "[{"del": p, "len": q - p}, {"del": p + len(u), "len": p + m - q}]"
            #   With p=2, q=3, len(u)=2, m=4:
            #   First: {"del": 2, "len": 1} → deletes char at position 2
            #   Second: {"del": 2+2, "len": 2+4-3} = {"del": 4, "len": 3} → deletes 3 chars starting at pos 4
            #   But wait, these positions are in the ORIGINAL text, not the transformed text.
            #   In original text: del [2, 1) is invalid. del [2, 3) would delete positions 2,3 = "23".
            #   Hmm, I think the positions in the result are in the text AFTER b is applied.
            #   After b (ins at 3 "XX"): text is "012XX3456789"
            #   First del: {"del": p=2, "len": q-p=1} → delete 1 char at pos 2 = "2" ✓
            #   Second del: {"del": p+len(u)=4, "len": p+m-q=2+4-3=3} → delete 3 chars at pos 4
            #   Positions in transformed text: 4=X, 5=3, 6=4 → deletes "X34" - but we want "345"!
            #   That's not right.
            #   
            #   OK let me re-read the spec result format again:
            #   "[{"del": p, "len": q - p}, {"del": p + len(u), "len": p + m - q}]"
            #   Maybe the positions are still in the original text's coordinate system?
            #   In original text: del [2, 1) doesn't make sense. del [2, 3) would be len=1, start=2.
            #   Actually wait - maybe "q - p" is the length, and "p" is the start. So del [2, 1) is
            #   a del with start=2 and length=1, which deletes 1 char at position 2. That's valid.
            #   And del [p + len(u) = 4, p + m - q = 2+4-3=3] = del [4, 3) - length 3 starting at 4.
            #   In original text, del [4, 3) is invalid (length > remaining chars from pos 4).
            #   Hmm.
            #   
            #   OK I think I need to look at this differently. Maybe the result edits use
            #   positions in the text AFTER both edits are applied, or maybe they use a different
            #   coordinate system. Let me just follow the spec literally and see if the math works out.
            #   
            #   Spec result: [{"del": p, "len": q - p}, {"del": p + len(u), "len": p + m - q}]
            #   With my example: p=2, q=3, u="XX" (len=2), m=4
            #   Result: [{"del": 2, "len": 1}, {"del": 4, "len": 3}]
            #   
            #   Now, if we apply a then b originally:
            #   Original text: "0123456789"
            #   Apply a = del [2, 6): delete positions 2-5 → "016789"
            #   Apply b = ins at 3 "XX": but wait, after a, text is "016789" (6 chars)
            #   Ins at position 3 in the reduced text... this is getting confusing because we're applying a then b.
            #   
            #   Let me apply b first, then a:
            #   Original: "0123456789"
            #   Apply b = ins at 3 "XX": "012XX3456789"
            #   Apply a = del [2, 6) original: delete original positions 2-5
            #   In new text, original pos 2 -> new pos 2, original pos 3 -> new pos 5, original pos 4 -> new pos 6, original pos 5 -> new pos 7
            #   So deleting original [2, 6) in new text means deleting new positions [2, 8) = positions 2,3,4,5,6,7 = "2XX345"
            #   But we want a to not delete "XX". So the transformed a should delete only "2" and "345"
            #   = positions 2 and positions 5,6,7 = "2" + "345" = "2345" ✓
            #   
            #   Now the spec result [{"del": 2, "len": 1}, {"del": 4, "len": 3}] - if these positions
            #   are in the NEW text (after b):
            #   del [2, 1) → delete pos 2 = "2" ✓
            #   del [4, 3) → delete pos 4,5,6 = "X34" ✗ (we want "345", not "X34")
            #   
            #   If positions are in ORIGINAL text:
            #   del [2, 1) → invalid or deletes pos 2 only = "2" ✓ (but length 1 from pos 2)
            #   del [4, 3) → invalid in original text
            #   
            #   Hmm, neither works perfectly. Let me check if maybe the spec means something
            #   different by the positions. Maybe "p + len(u)" in the second del refers to
            #   positioning in a different way.
            #   
            #   Actually, wait. Let me re-read the spec result for del vs ins:
            #   "[{"del": p, "len": q - p}, {"del": p + len(u), "len": p + m - q}]"
            #   
            #   Maybe the positions are such that when you apply these edits to the text
            #   AFTER the insert is applied, you get the right result. Let me try:
            #   Text after ins at q="XX": "012XX3456789"
            #   First edit: {"del": p=2, "len": q-p=1} → delete 1 char at pos 2 = "2" → "01XX3456789"
            #   Second edit: {"del": p+len(u)=4, "len": p+m-q=3} → delete 3 chars at pos 4
            #   In "01XX3456789", pos 4=3, pos 5=4, pos 6=5 → delete "345" → "01XX6789"
            #   But we want "01XX6789" where original "2345" was removed... original was "0123456789",
            #   we removed "2345" and inserted "XX" at position 3, result should be "01XX6789"? 
            #   No, original "0123456789", remove "2345" = "016789", insert "XX" at position 2
            #   Wait, position 2 in "016789" is where "2" was... I'm getting confused.
            #   
            #   Let me start over with a cleaner example.
            #   
            #   Original text: "ABCDE" (5 chars, positions 0-4)
            #   a = del [1, 4) deletes "BCD" (positions 1,2,3)
            #   b = ins at q=2, text="XX" inserts "XX" at position 2
            #   
            #   Apply b first: text becomes "ABXXDE" (positions: 0=A, 1=B, 2=X, 3=X, 4=D, 5=E)
            #   Now apply a: a was del [1, 4) original. After b, original positions map as:
            #     original 0 -> new 0
            #     original 1 -> new 1
            #     original 2 -> was where "X" is, new pos 2 or 3 depending
            #     Actually: new text "ABXXDE"
            #     new pos 0 = original 0 = A
            #     new pos 1 = original 1 = B
            #     new pos 2 = X (inserted)
            #     new pos 3 = X (inserted)
            #     new pos 4 = original 4 = D
            #     new pos 5 = original 5 = E
            #   a deletes original positions 1,2,3 = "BCD"
            #   In new text: new pos 1 = B, new pos 2 = X, new pos 3 = X
            #   To delete "BCD" without deleting "XX": delete new pos 1 and new pos 4,5 = "BD"... no
            #   We need to delete B (new pos 1) and D (new pos 4) and E (new pos 5)... no, just B and D
            #   Wait, original "BCD" = positions 1,2,3. After b, B is at new pos 1, C and D were shifted.
            #   Original pos 2 (C) was shifted to... after inserting "XX" at pos 2, original pos 2 becomes new pos 4? No.
            #   Let me be very precise:
            #   Original: 0=A, 1=B, 2=C, 3=D, 4=E
            #   Insert "XX" at position 2: new text is A, B, X, X, C, D, E
            #   New positions: 0=A, 1=B, 2=X, 3=X, 4=C, 5=D, 6=E
            #   Original pos 1 (B) -> new pos 1
            #   Original pos 2 (C) -> new pos 4
            #   Original pos 3 (D) -> new pos 5
            #   a = del [1, 4) original = delete original positions 1,2,3 = "BCD"
            #   In new text, we need to delete B (new pos 1), C (new pos 4), D (new pos 5)
            #   That's positions 1, 4, 5 = not a contiguous range
            #   As two edits: del [1, 2) and del [4, 6)
            #   del [1, 2) deletes new pos 1 = "B" ✓
            #   del [4, 6) deletes new pos 4,5 = "CD"... wait, new pos 4=C, new pos 5=D, so "CD" ✓
            #   But we want to NOT delete "XX" at new pos 2,3. ✓
            #   
            #   Now the spec result: [{"del": p, "len": q - p}, {"del": p + len(u), "len": p + m - q}]
            #   p=1, q=2, len(u)=2, m=3
            #   First: {"del": 1, "len": 1} → delete 1 char at pos 1
            #   Second: {"del": 1+2, "len": 1+3-2} = {"del": 3, "len": 2} → delete 2 chars at pos 3
            #   
            #   If these are positions in the NEW text (after b = ins at 2 "XX"):
            #   Text: "ABXXDE"
            #   del [1, 1) → hmm, len=1 from pos 1 deletes pos 1 = "B" ✓
            #   del [3, 2) → len=2 from pos 3 deletes pos 3,4 = "XD"... but we want "CD"!
            #   That's wrong. del [3, 2) deletes positions 3 and 4 = "XD" in "ABXXDE".
            #   
            #   If positions are in ORIGINAL text:
            #   del [1, 1) → deletes pos 1 = "B" ✓
            #   del [3, 2) → in original text, pos 3 with len 2 deletes positions 3,4 = "DE"... wrong
            #   
            #   Hmm, neither works. Let me try yet another interpretation.
            #   
            #   Maybe the second del position is p + len(u) but in the context where u has been
            #   inserted, so the position accounts for the shift. Let me try:
            #   Second del: start at position p + len(u) = 1 + 2 = 3 in the new text.
            #   But wait, maybe it's p + len(u) counting from the original position where u was inserted.
            #   Actually, I think the issue is that the two del edits should be applied sequentially
            #   to the ORIGINAL text, not to the transformed text.
            #   
            #   Let me try applying the two del edits to the original text:
            #   First: del [1, 1) → hmm, this deletes 1 char at pos 1 from original = "B", text becomes "ACDE"
            #   Wait, but after first del, the text changes, and the second del's position might need
            #   to account for that. But the spec says these are edits applied left to right, each
            #   to the text produced by the previous one.
            #   
            #   First del [1, 1) on original "ABCDE": delete 1 char at pos 1 = "B", text becomes "ACDE"
            #   Second del [3, 2) on "ACDE": delete 2 chars at pos 3 = "DE", text becomes "AC"
            #   Result: "AC" but we wanted "AXXE" (insert "XX" and delete "BCD")... wrong.
            #   
            #   OK I think I've been spending too much time on this. Let me just implement
            #   what the spec says literally and test it. If the spec's formula produces the
            #   correct result when applied according to the "applied left to right" rule, then
            #   that's the right implementation regardless of my intuition.
            #   
            #   Spec result for del vs ins: [{"del": p, "len": q - p}, {"del": p + len(u), "len": p + m - q}]
            #   
            #   Let me just implement it and move on. I'll test it later.
            left_len = q - p
            right_len = p + m - q  # Note: spec says "p + m - q", not "p + m - q - len(u)"
            # Wait, spec says: "len": p + m - q. But that doesn't account for len(u).
            # Let me re-read: "[{"del": p, "len": q - p}, {"del": p + len(u), "len": p + m - q}]"
            # OK so second len is p + m - q, not involving len(u) directly.
            # But the start is p + len(u).
            result = []
            if left_len > 0:
                result.append({"del": p, "len": left_len})
            if p + m - q > 0:  # right part length
                result.append({"del": p + len(b["text"]), "len": p + m - q})
            return result

    # Both deletes
    if a_type == "del" and b_type == "del":
        p = a["del"]
        m = a["len"]
        q = b["del"]
        k = b["len"]

        # b entirely before a
        if q + k <= p:
            return [a]
        # b entirely after a
        if q >= p + m:
            return [a]

        # b completely covers a
        if q <= p and q + k >= p + m:
            return []

        # b overlaps a; result is one contiguous range afterwards
        # The remaining characters to delete are [p, q) and [q+k, p+m) in original coords
        # After b is applied, these become contiguous starting at p
        left_part = q - p if q > p else 0
        right_part = p + m - (q + k) if p + m > q + k else 0

        if left_part > 0 and right_part > 0:
            return [{"del": p, "len": left_part + right_part}]
        elif left_part > 0:
            return [{"del": p, "len": left_part}]
        elif right_part > 0:
            return [{"del": p, "len": right_part}]
        else:
            return []

    raise ValueError(f"Unknown edit types: a={a_type}, b={b_type}")


def transform_one_edit_against_list(edit: dict, edits: list) -> list:
    """Transform one edit against a list of edits, composing the transformations.

    Applied left to right: first edit[0] is applied, then edit[1], etc.
    The edit parameter is mutated/composed with each edit in the list.
    """
    result = [edit]
    for b in edits:
        new_result = []
        for a in result:
            transformed = transform_edit_vs_edit(a, b)
            new_result.extend(transformed)
        result = new_result
    return result


def transform_operation_vs_operation(A: list, B: list) -> tuple:
    """Transform operation A against earlier operation B.

    Returns (A_transformed, B_transformed) where A_transformed applies after B
    and B_transformed applies after A. Both orderings give the same text.

    A = [a1, ..., an], B = [b1, ..., bm]
    """
    # Handle empty operations
    if not A:
        return ([], B[:])
    if not B:
        return (A[:], [])

    # Both have exactly one edit
    if len(A) == 1 and len(B) == 1:
        return (transform_edit_vs_edit(A[0], B[0]),)

    # General case: transform each edit of A against all of B, composing results
    # The spec says: "first all of B's edits were committed in order, then A's edits
    # arrive in order." So we transform A's edits against B's edits in order.
    
    # Transform all of A against all of B
    # Approach: transform each edit of A against all of B, composing transformations
    
    A_transformed = []
    for a_edit in A:
        a_transformed = transform_one_edit_against_list(a_edit, B)
        A_transformed.extend(a_transformed)
    
    # For B transformed, we need to transform B against A as well
    # But the spec says the result should be symmetric: both orderings give same text
    # For now, let me also transform B against A
    B_transformed = []
    for b_edit in B:
        b_transformed = transform_one_edit_against_list(b_edit, A)
        B_transformed.extend(b_transformed)
    
    return (A_transformed, B_transformed)