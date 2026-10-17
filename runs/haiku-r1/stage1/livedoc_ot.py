"""Operational transformation rules shared by the Livedoc server and client.

An edit is a dict in wire form: {"ins": P, "text": T} or {"del": P, "len": N}.
An operation is a list of edits. Server and clients must agree on every
result character for character, so these rules live only in this module.
"""


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def ins(pos, text):
    return {"ins": pos, "text": text}


def dele(pos, length):
    return {"del": pos, "len": length}


def is_wellformed(op):
    """True if `op` has the wire shape of an operation (says nothing about the text)."""
    if not isinstance(op, list):
        return False
    for edit in op:
        if not isinstance(edit, dict):
            return False
        keys = set(edit)
        if keys == {"ins", "text"}:
            if not (_is_int(edit["ins"]) and isinstance(edit["text"], str)):
                return False
        elif keys == {"del", "len"}:
            if not (_is_int(edit["del"]) and _is_int(edit["len"])):
                return False
        else:
            return False
    return True


def fits(op, length):
    """True if `op` applies cleanly to a text of `length` characters."""
    for edit in op:
        if "ins" in edit:
            if not edit["text"] or not 0 <= edit["ins"] <= length:
                return False
            length += len(edit["text"])
        else:
            if edit["len"] < 1 or edit["del"] < 0 or edit["del"] + edit["len"] > length:
                return False
            length -= edit["len"]
    return True


def apply_edit(text, edit):
    if "ins" in edit:
        pos = edit["ins"]
        return text[:pos] + edit["text"] + text[pos:]
    pos = edit["del"]
    return text[:pos] + text[pos + edit["len"]:]


def apply_op(text, op):
    for edit in op:
        text = apply_edit(text, edit)
    return text


def xf_edit(a, b, a_wins_tie):
    """Transform single edit `a` to apply after single edit `b`.

    Both were written against the same text. Returns a list of 0, 1 or 2 edits.
    `a_wins_tie` decides insert/insert ties at the same position: the side that
    was committed earlier stays to the left.
    """
    if "ins" in a:
        p, text = a["ins"], a["text"]
        if "ins" in b:
            q, other = b["ins"], b["text"]
            if p < q or (p == q and a_wins_tie):
                return [a]
            return [ins(p + len(other), text)]
        q, k = b["del"], b["len"]
        if p <= q:
            return [a]
        if p >= q + k:
            return [ins(p - k, text)]
        return [ins(q, text)]

    p, m = a["del"], a["len"]
    if "ins" in b:
        q, other = b["ins"], b["text"]
        if q <= p:
            return [dele(p + len(other), m)]
        if q >= p + m:
            return [a]
        # b landed strictly inside the range: keep both halves, skip b's text.
        return [dele(p, q - p), dele(p + len(other), p + m - q)]
    q, k = b["del"], b["len"]
    overlap = max(0, min(p + m, q + k) - max(p, q))
    remaining = m - overlap
    if remaining == 0:
        return []
    # Position after b: the first surviving character, shifted past b's deletion.
    start = p - min(max(p - q, 0), k)
    return [dele(start, remaining)]


def xf(a_op, b_op):
    """Transform operation `a_op` against the earlier-committed `b_op`.

    Returns (a', b'): a' applies after b_op, b' applies after a_op, and both
    orders give the same text.
    """
    if not a_op or not b_op:
        return a_op, b_op
    if len(a_op) == 1 and len(b_op) == 1:
        return xf_edit(a_op[0], b_op[0], False), xf_edit(b_op[0], a_op[0], True)
    if len(a_op) > 1:
        first, b1 = xf(a_op[:1], b_op)
        rest, b2 = xf(a_op[1:], b1)
        return first + rest, b2
    a1, first = xf(a_op, b_op[:1])
    a2, rest = xf(a1, b_op[1:])
    return a2, first + rest
