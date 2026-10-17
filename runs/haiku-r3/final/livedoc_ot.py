"""Operational transformation rules for Livedoc.

Shared by the server and the client so that both compute exactly the same
results. An edit is a tuple ``(INS, pos, text)`` or ``(DEL, pos, length)``;
an operation is a list of edits applied left to right.
"""

INS = "ins"
DEL = "del"


class OpError(ValueError):
    """An operation is not valid for the text it is written against."""


def validate(edits, length):
    """Check ``edits`` against a text of ``length`` characters.

    Returns the length of the text after the operation.
    """
    for kind, pos, arg in edits:
        if kind == INS:
            if not 0 <= pos <= length or not arg:
                raise OpError("insert outside the text or with empty text")
            length += len(arg)
        else:
            if arg < 1 or pos < 0 or pos + arg > length:
                raise OpError("delete outside the text or with length < 1")
            length -= arg
    return length


def apply(text, edits):
    """Apply ``edits`` to ``text``. The edits must already be valid."""
    for kind, pos, arg in edits:
        if kind == INS:
            text = text[:pos] + arg + text[pos:]
        else:
            text = text[:pos] + text[pos + arg:]
    return text


def move_cursor(pos, edits, own):
    """Move a cursor at ``pos`` through ``edits``, applied in order.

    ``own`` says whether the operation was made by the cursor's owner. An
    insert exactly at the cursor pushes it only for its owner's own typing.
    """
    for kind, p, arg in edits:
        if kind == INS:
            if p < pos or (p == pos and own):
                pos += len(arg)
        elif pos > p:
            pos = max(p, pos - arg)
    return pos


def edits_to_wire(edits):
    return [{"ins": pos, "text": arg} if kind == INS else {"del": pos, "len": arg}
            for kind, pos, arg in edits]


def edits_from_wire(op):
    """Convert a decoded JSON operation. The caller checks the structure."""
    return [(INS, e["ins"], e["text"]) if "ins" in e else (DEL, e["del"], e["len"])
            for e in op]


def _xf_edit(a, b, a_later):
    """Transform single edit ``a`` past single edit ``b`` (b is committed earlier).

    Returns the list of edits (0, 1 or 2) that ``a`` becomes after ``b``.
    ``a_later`` says whether ``a`` is the later-committed side; it only matters
    for insert/insert ties, where the earlier-committed insert stays left.
    """
    kind_a, p, arg_a = a
    kind_b, q, arg_b = b
    if kind_a == INS:
        if kind_b == INS:
            if p < q or (p == q and not a_later):
                return [a]
            return [(INS, p + len(arg_b), arg_a)]
        if p <= q:
            return [a]
        if p >= q + arg_b:
            return [(INS, p - arg_b, arg_a)]
        return [(INS, q, arg_a)]

    # a is a delete of [p, p + m)
    m = arg_a
    if kind_b == INS:
        if q <= p:
            return [(DEL, p + len(arg_b), m)]
        if q >= p + m:
            return [a]
        return [(DEL, p, q - p), (DEL, p + len(arg_b), p + m - q)]
    k = arg_b
    overlap = max(0, min(p + m, q + k) - max(p, q))
    remaining = m - overlap
    if remaining == 0:
        return []
    start = p if p <= q else p - min(k, p - q)
    return [(DEL, start, remaining)]


def xf(a_ops, b_ops):
    """Transform operations. ``a_ops`` is the later one, ``b_ops`` the earlier.

    Returns ``(a', b')``: ``a'`` applies after ``b_ops`` and ``b'`` applies after
    ``a_ops``. Both orders give the same text.
    """
    if not a_ops or not b_ops:
        return a_ops, b_ops
    if len(a_ops) == 1 and len(b_ops) == 1:
        return _xf_edit(a_ops[0], b_ops[0], True), _xf_edit(b_ops[0], a_ops[0], False)
    if len(a_ops) > 1:
        a1, b1 = xf(a_ops[:1], b_ops)
        rest, b2 = xf(a_ops[1:], b1)
        return a1 + rest, b2
    a1, b1 = xf(a_ops, b_ops[:1])
    a2, rest = xf(a1, b_ops[1:])
    return a2, b1 + rest
