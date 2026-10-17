#!/usr/bin/env python3
"""Livedoc real-time collaborative editing server."""

import argparse
import json
import os
import socket
import threading
import sys


def parse_args():
    parser = argparse.ArgumentParser(description="Livedoc server")
    parser.add_argument("--port", type=int, required=True, help="Port to listen on")
    parser.add_argument("--data-dir", type=str, required=True, help="Directory for persistence")
    return parser.parse_args()


def load_ops(data_dir):
    f = os.path.join(data_dir, "ops.json")
    if os.path.exists(f):
        with open(f) as fh:
            return json.load(f)
    return []


def save_ops(data_dir, ops):
    f = os.path.join(data_dir, "ops.json")
    with open(f, "w") as fh:
        json.dump(ops, f)


def apply_edits(text, edits):
    for e in edits:
        if not e:
            continue
        if "ins" in e:
            p, t = e["ins"], e["text"]
            if t:
                text = text[:p] + t + text[p:]
        elif "del" in e:
            p, n = e["del"], e["len"]
            text = text[:p] + text[p + n:]
    return text


def edit_vs_edit(a, b):
    """Transform edit a against earlier edit b. a is later, b is earlier."""
    a_ins = "ins" in a
    a_del = "del" in a
    b_ins = "ins" in b
    b_del = "del" in b
    if a_ins and b_ins:
        p, q = a["ins"], b["ins"]
        if p < q:
            return [{"ins": p, "text": a["text"]}]
        return [{"ins": p + len(b["text"]), "text": a["text"]}]
    if a_ins and b_del:
        p, q, k = a["ins"], b["del"], b["len"]
        if p <= q:
            return [{"ins": p, "text": a["text"]}]
        if p >= q + k:
            return [{"ins": p - k, "text": a["text"]}]
        return [{"ins": q, "text": a["text"]}]
    if a_del and b_ins:
        p, m, q, u_len = a["del"], a["len"], b["ins"], len(b["text"])
        if q <= p:
            return [{"del": p + u_len, "len": m}]
        if q >= p + m:
            return [{"del": p, "len": m}]
        res = []
        if q - p > 0:
            res.append({"del": p, "len": q - p})
        if p + m - q > 0:
            res.append({"del": p + u_len, "len": p + m - q})
        return res
    if a_del and b_del:
        p, m, q, k = a["del"], a["len"], b["del"], b["len"]
        if q <= p and q + k >= p + m:
            return []
        if q <= p:
            new_m = (p + m) - (q + k)
            return [{"del": p, "len": new_m}] if new_m > 0 else []
        if q >= p + m:
            return [{"del": p, "len": m}]
        new_m = m - k
        return [{"del": p, "len": new_m}] if new_m > 0 else []
    return []


def xf(a_edits, b_edits):
    """Transform edits a against earlier edits b, returning (a_transformed, b_transformed)."""
    if not a_edits:
        return (list(b_edits), [])
    if not b_edits:
        return (list(a_edits), [])
    if len(a_edits) == 1 and len(b_edits) == 1:
        a_prime = edit_vs_edit(a_edits[0], b_edits[0])
        b_prime = edit_vs_edit(b_edits[0], a_edits[0])
        return (a_prime, b_prime)
    if len(a_edits) > 1 and len(b_edits) == 1:
        (a1_prime, b1) = xf([a_edits[0]], b_edits)
        (rest_prime, b2) = xf(a_edits[1:], b1)
        from copy import deepcopy
        result_a = deepcopy(a1_prime)
        for e in rest_prime:
            result_a = result_a + e
        result_b = b2
        return (result_a, result_b)
    if len(a_edits) == 1 and len(b_edits) > 1:
        (A1, b1_prime) = xf(a_edits, [b_edits[0]])
        (A2, rest_prime) = xf(A1, b_edits[1:])
        from copy import deepcopy
        result_b = deepcopy(b1_prime)
        for e in rest_prime:
            result_b = result_b + e
        return (A2, result_b)
    return (list(a_edits), list(b_edits))


def transform_operation(A, committed_ops):
    """Transform operation A against all committed operations after its base revision."""
    if not A:
        return []
    current_A = list(A)
    for B in committed_ops:
        if not B:
            continue
        (transformed_A, _) = xf(current_A, B)
        current_A = transformed_A
    return current_A


def apply_operation(text, op):
    return apply_edits(text, op)


def send_error(sock, code, message, msg_id=None):
    err = {"type": "error", "code": code, "message": message}
    if msg_id is not None:
        err["id"] = msg_id
    try:
        sock.sendall((json.dumps(err) + "\n").encode("utf-8"))
    except Exception:
        pass


class DocState:
    def __init__(self):
        self.text = ""
        self.rev = 0
        self.ops = []


class LivedocServer:
    def __init__(self, port, data_dir):
        self.port = port
        self.data_dir = data_dir
        self.docs = {}
        self.clients = {}
        self.clients_lock = threading.Lock()
        self.docs_lock = threading.Lock()

    def init_dir(self):
        os.makedirs(self.data_dir, exist_ok=True)

    def get_doc(self, doc):
        with self.docs_lock:
            if doc not in self.docs:
                self.docs[doc] = DocState()
            return self.docs[doc]


def handle_client(client_sock, addr, server_obj):
    client_sock.settimeout(300)
    buffer = ""
    try:
        while True:
            data = client_sock.recv(4096)
            if not data:
                break
            buffer += data.decode("utf-8")
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    send_error(client_sock, "bad_json", "Invalid JSON")
                    continue
                response = server_obj.handle_message(msg, client_sock)
                if response is not None:
                    try:
                        client_sock.sendall((json.dumps(response) + "\n").encode("utf-8"))
                    except Exception:
                        break
    except (ConnectionResetError, BrokenPipeError, OSError):
        pass
    finally:
        with server_obj.clients_lock:
            if client_sock in server_obj.clients:
                del server_obj.clients[client_sock]
        client_sock.close()


def handle_message(msg, client_sock, server_obj):
    mt = msg.get("type")
    doc = msg.get("doc")
    with server_obj.clients_lock:
        ci = server_obj.clients.get(client_sock)

    if not doc or not isinstance(doc, str) or len(doc) == 0 or len(doc) > 100:
        send_error(client_sock, "bad_request", "Invalid doc name", ci.get("id") if ci else None)
        return None

    with server_obj.docs_lock:
        if doc not in server_obj.docs:
            server_obj.docs[doc] = DocState()
        ds = server_obj.docs[doc]

    if mt == "join":
        name = msg.get("client")
        since = msg.get("since")
        if not name or not isinstance(name, str) or len(name) == 0:
            send_error(client_sock, "bad_request", "Invalid client name", ci.get("id") if ci else None)
            return None

        rev = ds.rev
        text = ds.text

        if since is not None:
            if not isinstance(since, int) or since < 0 or since > rev:
                send_error(client_sock, "bad_rev", "Invalid since revision", ci.get("id") if ci else None)
                return None
            ops_list = []
            for od in ds.ops:
                if od.rev > since:
                    ops_list.append({"rev": od.rev, "client": od.client, "id": od.id, "op": od.op})
            response = {"type": "joined", "doc": doc, "rev": rev, "ops": ops_list}
        else:
            response = {"type": "joined", "doc": doc, "rev": rev, "text": text}

        with server_obj.clients_lock:
            server_obj.clients[client_sock] = {"name": name, "doc": doc, "rev": rev}

        # Broadcast join to other clients joined to this doc
        with server_obj.clients_lock:
            for s, info in server_obj.clients.items():
                if s != client_sock and info.get("doc") == doc:
                    try:
                        s.sendall((json.dumps(response) + "\n").encode("utf-8"))
                    except Exception:
                        pass

        return None

    elif mt == "op":
        if ci is None or ci.get("doc") != doc:
            send_error(client_sock, "not_joined", "Not joined to document", ci.get("id") if ci else None)
            return None

        rev = msg.get("rev")
        op_id = msg.get("id")
        op = msg.get("op")

        if rev is None or op is None or op_id is None:
            send_error(client_sock, "bad_request", "Missing fields", ci.get("id") if ci else None)
            return None

        if not isinstance(rev, int) or rev < 0 or rev > ds.rev:
            send_error(client_sock, "bad_rev", "Invalid rev", ci.get("id") if ci else None)
            return None

        if not isinstance(op, list):
            send_error(client_sock, "bad_request", "Operation must be a list", ci.get("id") if ci else None)
            return None

        with server_obj.docs_lock:
            committed_after = [od.op for od in ds.ops if od.rev > rev]
            transformed_op = transform_operation(op, committed_after)
            new_rev = ds.rev + 1
            new_text = apply_operation(ds.text, transformed_op)
            committed_op = {"rev": new_rev, "client": ci["name"], "id": op_id, "op": transformed_op}
            ds.ops.append(committed_op)
            ds.rev = new_rev
            ds.text = new_text
            save_ops(server_obj.data_dir, ds.ops)

        ack = {"type": "ack", "doc": doc, "id": op_id, "rev": new_rev}

        # Broadcast to other clients
        broadcast_msg = {
            "type": "op",
            "doc": doc,
            "rev": new_rev,
            "client": ci["name"],
            "id": op_id,
            "op": transformed_op
        }

        with server_obj.clients_lock:
            ci["rev"] = new_rev

        return ack

    elif mt == "error":
        return None
    else:
        send_error(client_sock, "bad_request", "Unknown message type", ci.get("id") if ci else None)
        return None


def main():
    args = parse_args()
    port = args.port
    data_dir = args.data_dir

    os.makedirs(data_dir, exist_ok=True)
    server_obj = LivedocServer(port, data_dir)
    server_obj.init_dir()

    server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_sock.bind(("127.0.0.1", port))
    server_sock.listen(10)
    print(f"Livedoc on 127.0.0.1:{port}", flush=True)

    try:
        while True:
            client_sock, addr = server_sock.accept()
            with server_obj.clients_lock:
                server_obj.clients[client_sock] = {"name": "", "doc": "", "rev": 0}
            t = threading.Thread(
                target=handle_client,
                args=(client_sock, addr, server_obj),
                daemon=True
            )
            t.start()
    except KeyboardInterrupt:
        pass
    finally:
        server_sock.close()


if __name__ == "__main__":
    main()
