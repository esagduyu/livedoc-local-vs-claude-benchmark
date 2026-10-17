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
    """Load committed operations from disk."""
    ops_file = os.path.join(data_dir, "ops.json")
    if os.path.exists(ops_file):
        with open(ops_file, "r") as f:
            return json.load(f)
    return []


def save_ops(data_dir, ops):
    """Save committed operations to disk."""
    ops_file = os.path.join(data_dir, "ops.json")
    with open(ops_file, "w") as f:
        json.dump(ops, f)


def apply_edits(text, edits):
    """Apply a list of edits to text."""
    for edit in edits:
        if not edit:
            continue
        if len(edit) == 1 and "ins" in edit:
            p = edit["ins"]
            t = edit["text"]
            if t:
                text = text[:p] + t + text[p:]
        elif len(edit) == 1 and "del" in edit:
            p = edit["del"]
            n = edit["len"]
            text = text[:p] + text[p + n:]
    return text


def edit_vs_edit(a, b):
    """
    Transform edit a against earlier edit b.
    a is the later edit, b is the earlier edit.
    Returns a list of edits.
    """
    a_has_ins = "ins" in a
    a_has_del = "del" in a
    b_has_ins = "ins" in b
    b_has_del = "del" in b

    result = []

    if a_has_ins and b_has_ins:
        p = a["ins"]
        q = b["ins"]
        if p < q:
            result = [{"ins": p, "text": a["text"]}]
        else:
            result = [{"ins": p + len(b["text"]), "text": a["text"]}]

    elif a_has_ins and b_has_del:
        p = a["ins"]
        q = b["del"]
        k = b["len"]
        if p <= q:
            result = [{"ins": p, "text": a["text"]}]
        elif p >= q + k:
            result = [{"ins": p - k, "text": a["text"]}]
        else:
            result = [{"ins": q, "text": a["text"]}]

    elif a_has_del and b_has_ins:
        p = a["del"]
        m = a["len"]
        q = b["ins"]
        u_len = len(b["text"])
        if q <= p:
            result = [{"del": p + u_len, "len": m}]
        elif q >= p + m:
            result = [{"del": p, "len": m}]
        else:
            left_len = q - p
            right_len = p + m - q
            result = []
            if left_len > 0:
                result.append({"del": p, "len": left_len})
            if right_len > 0:
                result.append({"del": p + u_len, "len": right_len})

    elif a_has_del and b_has_del:
        p = a["del"]
        m = a["len"]
        q = b["del"]
        k = b["len"]
        if q <= p and q + k >= p + m:
            result = []
        elif q <= p:
            if q + k >= p + m:
                result = []
            else:
                new_p = p
                new_m = (p + m) - (q + k)
                if new_m > 0:
                    result = [{"del": new_p, "len": new_m}]
                else:
                    result = []
        elif q >= p + m:
            result = [{"del": p, "len": m}]
        else:
            # p < q < p+m
            new_m = m - k
            if new_m > 0:
                result = [{"del": p, "len": new_m}]
            else:
                result = []

    return result


def transform_edits(a_edits, b_edits):
    """Transform a list of edits against another list of edits."""
    result = list(a_edits)
    for b in b_edits:
        new_result = []
        for a_edit in result:
            transformed = edit_vs_edit(a_edit, b)
            new_result.extend(transformed)
        result = new_result
    return result


def transform_operation(A, committed_ops):
    """Transform operation A against all committed operations after its base revision."""
    result = list(A)
    for committed in committed_ops:
        result = transform_edits(result, committed)
    return result


def apply_operation(text, op):
    """Apply an operation (list of edits) to text."""
    return apply_edits(text, op)


def send_error(sock, code, message, msg_id=None):
    """Send an error response."""
    error = {"type": "error", "code": code, "message": message}
    if msg_id is not None:
        error["id"] = msg_id
    try:
        sock.sendall((json.dumps(error) + "\n").encode("utf-8"))
    except Exception:
        pass


def broadcast_to_doc(doc_name, message, exclude_client, clients_lock, clients):
    """Send a message to all connections joined to doc_name."""
    with clients_lock:
        for sock, info in clients.items():
            if info.get("doc") == doc_name and sock != exclude_client:
                try:
                    sock.sendall((json.dumps(message) + "\n").encode("utf-8"))
                except Exception:
                    pass


def handle_client(client_sock, addr, docs, docs_lock, clients_lock, clients):
    """Handle a single client connection."""
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

                handle_message(client_sock, msg, docs, docs_lock, clients_lock, clients)
    except (ConnectionResetError, BrokenPipeError, OSError):
        pass
    finally:
        with clients_lock:
            if client_sock in clients:
                del clients[client_sock]


def handle_message(client_sock, msg, docs, docs_lock, clients_lock, clients):
    import traceback as tb
    msg_type = msg.get("type")
    doc = msg.get("doc")
    print(f"[DBG] handle_message type={msg_type}, doc={doc}", flush=True)
    tb.print_stack()
    """Handle a single message from a client."""
    msg_type = msg.get("type")
    doc = msg.get("doc")
    client_info = None

    with clients_lock:
        for sock, info in clients.items():
            if sock == client_sock:
                client_info = info
                break

    if not doc or not isinstance(doc, str) or len(doc) == 0 or len(doc) > 100:
        send_error(client_sock, "bad_request", "Invalid doc name", client_info.get("id") if client_info else None)
        return

    with docs_lock:
        if doc not in docs:
            docs[doc] = {"text": "", "rev": 0, "ops": []}

    doc_state = docs[doc]

    if msg_type == "join":
        name = msg.get("client")
        since = msg.get("since")

        if not name or not isinstance(name, str) or len(name) == 0:
            send_error(client_sock, "bad_request", "Invalid client name", client_info.get("id") if client_info else None)
            return

        rev = doc_state["rev"]
        text = doc_state["text"]

        if since is not None:
            if not isinstance(since, int) or since < 0 or since > rev:
                send_error(client_sock, "bad_rev", "Invalid since revision", client_info.get("id") if client_info else None)
                return
            ops_to_send = []
            for op_data in doc_state["ops"]:
                if op_data["rev"] > since:
                    ops_to_send.append({
                        "rev": op_data["rev"],
                        "client": op_data["client"],
                        "id": op_data["id"],
                        "op": op_data["op"]
                    })
            response = {"type": "joined", "doc": doc, "rev": rev, "ops": ops_to_send}
        else:
            response = {"type": "joined", "doc": doc, "rev": rev, "text": text}

        try:
            client_sock.sendall((json.dumps(response) + "\n").encode("utf-8"))
        except Exception:
            return

        with clients_lock:
            clients[client_sock] = {
                "name": name,
                "doc": doc,
                "rev": rev
            }

        broadcast_to_doc(doc, response, client_sock, clients_lock, clients)

    elif msg_type == "op":
        if client_info is None or client_info.get("doc") != doc:
            send_error(client_sock, "not_joined", "Not joined to document", client_info.get("id") if client_info else None)
            return

        rev = msg.get("rev")
        op_id = msg.get("id")
        op = msg.get("op")

        if rev is None or op is None or op_id is None:
            send_error(client_sock, "bad_request", "Missing fields", client_info.get("id") if client_info else None)
            return

        if not isinstance(rev, int) or rev < 0 or rev > doc_state["rev"]:
            send_error(client_sock, "bad_rev", "Invalid rev", client_info.get("id") if client_info else None)
            return

        if not isinstance(op, list):
            send_error(client_sock, "bad_request", "Operation must be a list", client_info.get("id") if client_info else None)
            return

        # Transform against all operations committed after rev
        with docs_lock:
            committed_after = [op_data["op"] for op_data in doc_state["ops"] if op_data["rev"] > rev]
            transformed_op = transform_operation(op, committed_after)
            new_rev = doc_state["rev"] + 1
            new_text = apply_operation(doc_state["text"], transformed_op)

            committed_op = {
                "rev": new_rev,
                "client": client_info["name"],
                "id": op_id,
                "op": transformed_op
            }
            doc_state["ops"].append(committed_op)
            doc_state["rev"] = new_rev
            doc_state["text"] = new_text

            save_ops(data_dir, doc_state["ops"])

        # Send ack to sender
        ack = {"type": "ack", "doc": doc, "id": op_id, "rev": new_rev}
        try:
            client_sock.sendall((json.dumps(ack) + "\n").encode("utf-8"))
        except Exception:
            return

        # Broadcast to other clients
        broadcast_op_msg = {"type": "op", "doc": doc, "rev": new_rev, "client": client_info["name"], "id": op_id, "op": transformed_op}
        broadcast_to_doc(doc, broadcast_op_msg, client_sock, clients_lock, clients)

        # Update client's rev
        with clients_lock:
            client_info["rev"] = new_rev

    elif msg_type == "error":
        pass
    else:
        send_error(client_sock, "bad_request", "Unknown message type", client_info.get("id") if client_info else None)


def main():
    args = parse_args()
    port = args.port
    data_dir = args.data_dir

    os.makedirs(data_dir, exist_ok=True)

    # Load committed operations
    committed_ops_data = load_ops(data_dir)

    # Document state: doc_name -> {"text": str, "rev": int, "ops": list of ops}
    docs = {}

    # Server socket
    server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_sock.bind(("127.0.0.1", port))
    server_sock.listen(10)
    print(f"Livedoc server listening on 127.0.0.1:{port}", flush=True)

    # Client tracking: client_socket -> {"name": str, "doc": str, "rev": int}
    clients = {}

    # Locks
    docs_lock = threading.Lock()
    clients_lock = threading.Lock()

    try:
        while True:
            client_sock, addr = server_sock.accept()
            thread = threading.Thread(
                target=handle_client,
                args=(client_sock, addr, docs, docs_lock, clients_lock, clients),
                daemon=True
            )
            thread.start()
    except KeyboardInterrupt:
        pass
    finally:
        server_sock.close()


if __name__ == "__main__":
    main()