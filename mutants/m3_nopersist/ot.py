"""Reference operational transformation for the Livedoc spec.

Edits are dicts exactly as on the wire: {"ins": p, "text": t} or {"del": p, "len": n}.
An operation is a list of edits applied left to right.
"""


class OpError(ValueError):
    pass


def is_int(x):
    return isinstance(x, int) and not isinstance(x, bool)


def check_shape(op):
    """Raise OpError if `op` isn't a well-formed operation (ignores positions vs text)."""
    if not isinstance(op, list):
        raise OpError("op must be a list")
    for e in op:
        if not isinstance(e, dict):
            raise OpError("edit must be an object")
        if "ins" in e:
            if set(e) != {"ins", "text"} or not is_int(e["ins"]) or not isinstance(e["text"], str):
                raise OpError("bad insert edit")
        elif "del" in e:
            if set(e) != {"del", "len"} or not is_int(e["del"]) or not is_int(e["len"]):
                raise OpError("bad delete edit")
        else:
            raise OpError("unknown edit")


def apply(text, op):
    """Apply op to text; raise OpError if any edit is out of range."""
    for e in op:
        if "ins" in e:
            p, t = e["ins"], e["text"]
            if not t or p < 0 or p > len(text):
                raise OpError(f"insert out of range: {e}")
            text = text[:p] + t + text[p:]
        else:
            p, n = e["del"], e["len"]
            if n < 1 or p < 0 or p + n > len(text):
                raise OpError(f"delete out of range: {e}")
            text = text[:p] + text[p + n:]
    return text


def _ins(p, t):
    return {"ins": p, "text": t}


def _del(p, n):
    return {"del": p, "len": n}


def _del_minus(p, m, q, k):
    """Delete [p,p+m) after delete [q,q+k) has been applied."""
    ov = max(0, min(p + m, q + k) - max(p, q))
    n = m - ov
    if n == 0:
        return []
    start = p if p <= q else p - min(k, p - q)
    return [_del(start, n)]


def xf_edit(a, b):
    """a is later, b is earlier-committed; both against the same text.
    Returns (a', b'): a' applies after b, b' applies after a."""
    if "ins" in a and "ins" in b:
        p, t, q, u = a["ins"], a["text"], b["ins"], b["text"]
        if p < q:
            return [a], [_ins(q + len(t), u)]
        return [_ins(p + len(u), t)], [b]
    if "ins" in a:  # b is del
        p, t, q, k = a["ins"], a["text"], b["del"], b["len"]
        if p <= q:
            return [a], [_del(q + len(t), k)]
        if p >= q + k:
            return [_ins(p - k, t)], [b]
        return [_ins(q, t)], [_del(q, p - q), _del(q + len(t), q + k - p)]
    if "ins" in b:  # a is del
        p, m, q, u = a["del"], a["len"], b["ins"], b["text"]
        k = len(u)
        if q <= p:
            return [_del(p + k, m)], [b]
        if q >= p + m:
            return [a], [_ins(q - m, u)]
        return [_del(p, q - p), _del(p + k, p + m - q)], [_ins(p, u)]
    p, m, q, k = a["del"], a["len"], b["del"], b["len"]
    return _del_minus(p, m, q, k), _del_minus(q, k, p, m)


def xf(A, B):
    """A is later, B is earlier-committed. Returns (A', B')."""
    if not A or not B:
        return list(A), list(B)
    if len(A) == 1 and len(B) == 1:
        return xf_edit(A[0], B[0])
    if len(A) > 1:
        a1, B1 = xf(A[:1], B)
        rest, B2 = xf(A[1:], B1)
        return a1 + rest, B2
    A1, b1 = xf(A, B[:1])
    A2, rest = xf(A1, B[1:])
    return A2, b1 + rest


def transform_over(op, committed_ops):
    """Transform op (based on rev R) over the ops committed after R, in order."""
    for c in committed_ops:
        op, _ = xf(op, c)
    return op


def cursor_xf(pos, op, own):
    """Move a cursor through op. `own`: op was made by the cursor's owner."""
    for e in op:
        if "ins" in e:
            p, k = e["ins"], len(e["text"])
            if p < pos or (p == pos and own):
                pos += k
        else:
            p, n = e["del"], e["len"]
            if pos > p:
                pos = max(p, pos - n)
    return pos
