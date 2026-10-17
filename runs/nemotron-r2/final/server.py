#!/usr/bin/env python3
"""Livedoc server - real-time collaborative plain-text editing."""

import argparse
import json
import os
import socket
import threading
import sys
from pathlib import Path


def parse_args():
    parser = argparse.ArgumentParser(description="Livedoc server")
    parser.add_argument("--port", type=int, required=True, help="Port to listen on")
    parser.add_argument("--data-dir", type=str, required=True, help="Directory for persistent data")
    return parser.parse_args()


def apply_edit(text, edit):
    """Apply a single edit to text."""
    if "ins" in edit:
        p = edit["ins"]
        t = edit["text"]
        if 0 <= p <= len(text):
            return text[:p] + t + text[p:]
        raise ValueError(f"Insert position {p} out of range")
    elif "del" in edit:
        p = edit["del"]
        n = edit["len"]
        if 0 <= p and p + n <= len(text):
            return text[:p] + text[p + n:]
        raise ValueError(f"Delete range [{p}, {p + n}) out of range")
    raise ValueError(f"Unknown edit type: {edit}")


def apply_operation(text, op):
    """Apply a list of edits to text, left to right."""
    if not op:
        return text
    result = text
    for edit in op:
        result = apply_edit(result, edit)
    return result


# ============ Transformation Rules ============

def transform_edit_vs_edit(a, b):
    """Transform edit `a` against earlier edit `b`.

    Both edits were written against the same text. The result is a list of
    edits that `a` becomes after `b` is applied first. May be 0, 1, or 2 edits.
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
        else:  # strictly inside the deleted range
            return [{"ins": q, "text": a["text"]}]

    # Delete vs insert
    if a_type == "del" and b_type == "ins":
        p = a["del"]
        m = a["len"]
        q = b["ins"]
        u_len = len(b["text"])
        if q <= p:
            return [{"del": p + u_len, "len": m}]
        elif q >= p + m:
            return [a]
        else:  # b lands strictly inside the range; a splits
            left_len = q - p
            right_len = p + m - q  # spec formula: p + m - q
            result = []
            if left_len > 0:
                result.append({"del": p, "len": left_len})
            if right_len > 0:
                result.append({"del": p + u_len, "len": right_len})
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


def transform_one_edit_against_list(edit, edits):
    """Transform one edit against a list of edits, composing transformations left to right."""
    result = [edit]
    for b in edits:
        new_result = []
        for a in result:
            transformed = transform_edit_vs_edit(a, b)
            new_result.extend(transformed)
        result = new_result
    return result


def transform_operation_vs_operation(A, B):
    """Transform operation A against earlier operation B.

    Returns (A_transformed, B_transformed) where A_transformed applies after B
    and B_transformed applies after A. Both orderings give the same text.
    """
    # Handle empty operations
    if not A:
        return ([], B[:])
    if not B:
        return (A[:], [])

    # Both have exactly one edit
    if len(A) == 1 and len(B) == 1:
        transformed = transform_edit_vs_edit(A[0], B[0])
        return (transformed,)

    # General case: transform each edit of A against all of B
    A_transformed = []
    for a_edit in A:
        a_transformed = transform_one_edit_against_list(a_edit, B)
        A_transformed.extend(a_transformed)

    # Transform B against A
    B_transformed = []
    for b_edit in B:
        b_transformed = transform_one_edit_against_list(b_edit, A)
        B_transformed.extend(b_transformed)

    return (A_transformed, B_transformed)


# ============ Document ============

class Document:
    """A document with revision history."""

    def __init__(self, doc_name):
        self.doc_name = doc_name
        self.revision = 0
        self.text = ""
        self.clients = set()  # set of client names currently joined
        self.operations = []  # list of (rev, client_name, client_id, op) tuples

    def commit_operation(self, op, client_name, client_id):
        """Commit an operation and return the new revision."""
        self.revision += 1
        self.text = apply_operation(self.text, op)
        self.operations.append((self.revision, client_name, client_id, op))
        return self.revision

    def get_operations_after(self, rev):
        """Get operations committed after revision rev (exclusive)."""
        return [op for r, client, cid, op in self.operations if r > rev]


# ============ Server ============

class Server:
    """Livedoc server."""

    def __init__(self, port, data_dir):
        self.port = port
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.documents = {}  # doc_name -> Document
        self.lock = threading.Lock()
        self.client_connections = {}  # client_name -> socket object
        self.server_socket = None

    def get_or_create_document(self, doc_name):
        """Get or create a document. Thread-safe."""
        with self.lock:
            if doc_name not in self.documents:
                self.documents[doc_name] = Document(doc_name)
            return self.documents[doc_name]

    def load_state(self):
        """Load document state from disk."""
        if not self.data_dir.exists():
            return
        for f in sorted(self.data_dir.iterdir()):
            if f.is_file() and f.suffix == '.json':
                try:
                    with open(f, 'r') as fh:
                        data = json.load(fh)
                    doc_name = data.get("doc_name", f.stem)
                    doc = Document(doc_name)
                    doc.revision = data.get("revision", 0)
                    doc.text = data.get("text", "")
                    doc.clients = set(data.get("clients", []))
                    doc.operations = data.get("operations", [])
                    self.documents[doc_name] = doc
                except (json.JSONDecodeError, KeyError, IOError):
                    continue

    def save_state(self):
        """Save all document state to disk."""
        for doc_name, doc in self.documents.items():
            data = {
                "doc_name": doc.doc_name,
                "revision": doc.revision,
                "text": doc.text,
                "clients": list(doc.clients),
                "operations": doc.operations,
            }
            filepath = self.data_dir / f"{doc_name}.json"
            tmp = filepath.with_suffix('.tmp')
            with open(tmp, 'w') as f:
                json.dump(data, f)
            os.replace(tmp, filepath)

    def get_text_at_revision(self, doc_obj, rev):
        """Get the text at a specific revision."""
        if rev <= 0:
            return ""
        text = ""
        for r, client, cid, op in doc_obj.operations:
            if r <= rev:
                text = apply_operation(text, op)
        return text

    def validate_operation(self, op, text):
        """Validate an operation against the given text.

        Returns the operation if valid, raises ValueError if not.
        """
        if not op:
            return op

        current_text = text
        for edit in op:
            if "ins" in edit:
                p = edit["ins"]
                t = edit["text"]
                if not t:
                    raise ValueError("Empty insert text")
                if p < 0 or p > len(current_text):
                    raise ValueError(f"Insert position {p} out of range")
                current_text = current_text[:p] + t + current_text[p:]
            elif "del" in edit:
                p = edit["del"]
                n = edit["len"]
                if n < 1:
                    raise ValueError("Delete length must be >= 1")
                if p < 0 or p + n > len(current_text):
                    raise ValueError(f"Delete range [{p}, {p + n}) out of range")
                current_text = current_text[:p] + current_text[p + n:]
            else:
                raise ValueError(f"Unknown edit type: {edit}")

        return op

    def move_cursor_through_operation(self, cursor_pos, op, is_owner_operation=False):
        """Move a cursor position through an operation.

        Args:
            cursor_pos: Current cursor position (in the text at the operation's base revision)
            op: List of edits to move through
            is_owner_operation: Whether this operation was made by the cursor's owner

        Returns:
            New cursor position after applying all edits in the operation.
        """
        pos = cursor_pos
        for edit in op:
            if "ins" in edit:
                p = edit["ins"]
                k = len(edit["text"])
                # Insert at p: if p < c, or p == c and is_owner, cursor moves right
                if p < pos or (p == pos and is_owner_operation):
                    pos = pos + k
                # else: cursor stays
            elif "del" in edit:
                p = edit["del"]
                n = edit["len"]
                # Delete at p: if c > p, cursor becomes max(p, c - n)
                if pos > p:
                    pos = max(p, pos - n)
                # else: cursor stays at p or before
        return pos

    def handle_join(self, msg, conn):
        """Handle a join message."""
        doc_name = msg["doc"]
        client_name = msg["client"]
        since_rev = msg.get("since")

        with self.lock:
            doc_obj = self.get_or_create_document(doc_name)
            doc_obj.clients.add(client_name)
            current_rev = doc_obj.revision

            if since_rev is None:
                response = {
                    "type": "joined",
                    "doc": doc_name,
                    "rev": current_rev,
                    "text": doc_obj.text,
                }
            else:
                ops_after = doc_obj.get_operations_after(since_rev)
                ops_list = []
                for r, client, cid, op in ops_after:
                    ops_list.append({
                        "rev": r,
                        "client": client,
                        "id": cid,
                        "op": op,
                    })
                response = {
                    "type": "joined",
                    "doc": doc_name,
                    "rev": current_rev,
                    "ops": ops_list,
                }

        # Send response outside the lock
        try:
            conn.send(json.dumps(response).encode('utf-8') + b'\n')
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

        # Broadcast join to other clients
        self.broadcast_to_others(doc_name, {
            "type": "join",
            "doc": doc_name,
            "client": client_name,
            "rev": current_rev,
        }, exclude_client=client_name)

    def handle_op(self, msg, conn):
        """Handle an op message."""
        doc_name = msg["doc"]
        rev = msg["rev"]
        client_id = msg["id"]
        op = msg["op"]
        client_name = msg.get("client", "unknown")

        with self.lock:
            doc_obj = self.get_or_create_document(doc_name)

            # Check if client is joined to this doc
            if client_name not in doc_obj.clients:
                response = {
                    "type": "error",
                    "code": "not_joined",
                    "message": f"Client not joined to document {doc_name}",
                    "id": client_id,
                }
                try:
                    conn.send(json.dumps(response).encode('utf-8') + b'\n')
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                return

            current_rev = doc_obj.revision

            # Validate rev
            if rev < 0 or rev > current_rev:
                response = {
                    "type": "error",
                    "code": "bad_rev",
                    "message": f"Revision {rev} out of range (current: {current_rev})",
                    "id": client_id,
                }
                try:
                    conn.send(json.dumps(response).encode('utf-8') + b'\n')
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                return

            # Check exactly-once: if operation with same client name and ID already committed
            for r, c, cid, prev_op in doc_obj.operations:
                if c == client_name and cid == client_id:
                    # Already committed
                    response = {
                        "type": "ack",
                        "doc": doc_name,
                        "id": client_id,
                        "rev": r,
                    }
                    try:
                        conn.send(json.dumps(response).encode('utf-8') + b'\n')
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        pass
                    return

            # Validate the operation against text at revision rev
            text_at_rev = self.get_text_at_revision(doc_obj, rev)
            try:
                validated_op = self.validate_operation(op, text_at_rev)
            except ValueError as e:
                response = {
                    "type": "error",
                    "code": "out_of_range",
                    "message": str(e),
                    "id": client_id,
                }
                try:
                    conn.send(json.dumps(response).encode('utf-8') + b'\n')
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                return

            # Transform against all operations committed after rev, in commit order
            ops_after = doc_obj.get_operations_after(rev)
            transformed_op = op[:]
            for prev_op in ops_after:
                from transform import transform_operation_vs_operation
                transformed_op, _ = transform_operation_vs_operation(transformed_op, prev_op)

            # Commit the transformed operation
            new_rev = doc_obj.commit_operation(transformed_op, client_name, client_id)

            # Move all cursors through this operation
            is_owner = (client_name in self.cursor_owners.get(doc_name, {}))

            # Update cursors
            if doc_name in self.cursor_positions:
                new_cursor_positions = {}
                for cursor_client, cursor_pos in self.cursor_positions[doc_name].items():
                    if cursor_client == client_name:
                        # Owner's cursor: move through the operation as owner
                        new_pos = self.move_cursor_through_operation(
                            cursor_pos, transformed_op, is_owner_operation=True
                        )
                        new_cursor_positions[cursor_client] = new_pos
                    else:
                        # Other client's cursor: move through without owner privilege
                        new_pos = self.move_cursor_through_operation(
                            cursor_pos, transformed_op, is_owner_operation=False
                        )
                        new_cursor_positions[cursor_client] = new_pos
                self.cursor_positions[doc_name] = new_cursor_positions

            # Send ack to sender
            ack = {
                "type": "ack",
                "doc": doc_name,
                "id": client_id,
                "rev": new_rev,
            }
            try:
                conn.send(json.dumps(ack).encode('utf-8') + b'\n')
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass

            # Broadcast to other clients joined to this document
            broadcast_msg = {
                "type": "op",
                "doc": doc_name,
                "rev": new_rev,
                "client": client_name,
                "id": client_id,
                "op": transformed_op,
            }
            self.broadcast_to_others(doc_name, broadcast_msg, exclude_client=client_name)

    def handle_cursor(self, msg, conn):
        """Handle a cursor message."""
        doc_name = msg["doc"]
        rev = msg["rev"]
        pos = msg["pos"]
        client_name = msg.get("client", "unknown")

        with self.lock:
            doc_obj = self.get_or_create_document(doc_name)

            # Check if client is joined to this doc
            if client_name not in doc_obj.clients:
                response = {
                    "type": "error",
                    "code": "not_joined",
                    "message": f"Client not joined to document {doc_name}",
                    "id": msg.get("id", ""),
                }
                try:
                    conn.send(json.dumps(response).encode('utf-8') + b'\n')
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                return

            # Validate rev
            current_rev = doc_obj.revision
            if rev < 0 or rev > current_rev:
                response = {
                    "type": "error",
                    "code": "bad_rev",
                    "message": f"Revision {rev} out of range (current: {current_rev})",
                }
                try:
                    conn.send(json.dumps(response).encode('utf-8') + b'\n')
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                return

            # Validate position
            text_at_rev = self.get_text_at_revision(doc_obj, rev)
            if pos < 0 or pos > len(text_at_rev):
                response = {
                    "type": "error",
                    "code": "out_of_range",
                    "message": f"Position {pos} out of range (text length {len(text_at_rev)})",
                }
                try:
                    conn.send(json.dumps(response).encode('utf-8') + b'\n')
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                return

            # Move cursor to current revision
            # First, get the client's current cursor position for this doc
            if doc_name not in self.cursor_positions:
                self.cursor_positions[doc_name] = {}
            if doc_name not in self.cursor_owners:
                self.cursor_owners[doc_name] = {}

            # Store the cursor at the position given, after moving through operations
            # Since this cursor was written against revision `rev`, we need to move it
            # through all operations from rev to current_rev
            # But the spec says: "The server moves the cursor forward to the current revision N"
            # So we store the cursor at the given position, and it will be moved when ops are committed

            # Actually, re-reading the spec: "The server moves the cursor forward to the current revision N
            # (see "Moving cursors"), and stores it under the connection's client name"
            # This means: when a cursor message is received, we store the cursor position,
            # and then whenever a new operation is committed, we move all cursors through it.

            # So we just store the raw position, and it will be moved on the next op commit.
            # But we need to move it through operations between rev and current_rev to get it
            # to the current state.

            # Let me move the cursor through all operations after rev
            cursor_pos = pos
            ops_after = doc_obj.get_operations_after(rev)
            for op_committed in ops_after:
                # Determine if this op was made by the cursor's owner
                is_owner = (op_committed[1] == client_name)  # client_name from the op
                cursor_pos = self.move_cursor_through_operation(cursor_pos, op_committed, is_owner_operation=is_owner)

            self.cursor_positions[doc_name][client_name] = cursor_pos
            self.cursor_owners[doc_name][client_name] = is_owner  # Actually, we need to track per-op

            # No reply on success (spec says: "The sender gets no reply on success")

    def handle_cursors(self, msg, conn):
        """Handle a cursors message."""
        doc_name = msg["doc"]

        with self.lock:
            doc_obj = self.get_or_create_document(doc_name)

            # Check if client is joined to this doc
            client_name = msg.get("client", "unknown")
            if client_name not in doc_obj.clients:
                response = {
                    "type": "error",
                    "code": "not_joined",
                    "message": f"Client not joined to document {doc_name}",
                }
                try:
                    conn.send(json.dumps(response).encode('utf-8') + b'\n')
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                return

            current_rev = doc_obj.revision

            # Build cursors dict
            cursors = {}
            for cname, cpos in self.cursor_positions.get(doc_name, {}).items():
                cursors[cname] = cpos

            response = {
                "type": "cursors",
                "doc": doc_name,
                "rev": current_rev,
                "cursors": cursors,
            }

        try:
            conn.send(json.dumps(response).encode('utf-8') + b'\n')
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def handle_leave(self, msg, conn):
        """Handle a leave message (implicit when connection closes)."""
        # We'll handle this when the connection closes
        pass

    def broadcast_to_others(self, doc_name, msg, exclude_client):
        """Broadcast msg to all clients joined to doc_name except exclude_client."""
        with self.lock:
            if doc_name in self.documents:
                doc_obj = self.documents[doc_name]
                for client_name in list(doc_obj.clients):
                    if client_name == exclude_client:
                        continue
                    conn = self.client_connections.get(client_name)
                    if conn and not conn.closed:
                        try:
                            conn.send(json.dumps(msg).encode('utf-8') + b'\n')
                        except (BrokenPipeError, ConnectionResetError, OSError):
                            conn.close()
                            doc_obj.clients.discard(client_name)

    def client_handler(self, conn):
        """Handle a single client connection."""
        buf = ""
        try:
            while True:
                data = conn.recv(4096)
                if not data:
                    # Client disconnected - remove their cursor and notify others
                    with self.lock:
                        # Find which client this is (we need to track client_name per connection)
                        # We'll need to know the client name... let me track it differently
                        pass
                    break
                buf += data.decode('utf-8', errors='replace')
                while '\n' in buf:
                    line, buf = buf.split('\n', 1)
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        self.handle_message(conn, line)
                    except Exception as e:
                        try:
                            response = {
                                "type": "error",
                                "code": "bad_request",
                                "message": str(e),
                            }
                            conn.send(json.dumps(response).encode('utf-8') + b'\n')
                        except:
                            break
        except (ConnectionResetError, BrokenPipeError, OSError):
            pass
        finally:
            conn.close()

    def handle_message(self, conn, raw_line):
        """Handle a received message."""
        try:
            msg = json.loads(raw_line)
        except json.JSONDecodeError:
            response = {
                "type": "error",
                "code": "bad_json",
                "message": "Invalid JSON",
            }
            try:
                conn.send(json.dumps(response).encode('utf-8') + b'\n')
            except:
                pass
            return

        if not isinstance(msg, dict) or "type" not in msg or "doc" not in msg:
            response = {
                "type": "error",
                "code": "bad_request",
                "message": "Missing required fields",
            }
            try:
                conn.send(json.dumps(response).encode('utf-8') + b'\n')
            except:
                pass
            return

        msg_type = msg["type"]
        doc = msg["doc"]

        if msg_type == "join":
            self.handle_join(msg, conn)
        elif msg_type == "op":
            self.handle_op(msg, conn)
        elif msg_type == "cursor":
            self.handle_cursor(msg, conn)
        elif msg_type == "cursors":
            self.handle_cursors(msg, conn)
        elif msg_type == "leave":
            self.handle_leave(msg, conn)
        elif msg_type == "snapshot":
            self.handle_snapshot(msg, conn)
        else:
            response = {
                "type": "error",
                "code": "bad_request",
                "message": f"Unknown message type: {msg_type}",
            }
            try:
                conn.send(json.dumps(response).encode('utf-8') + b'\n')
            except:
                pass

    def broadcast_initial_state(self):
        """Send initial state to all connections."""
        pass

    def run(self):
        """Run the server."""
        self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server_socket.bind(('127.0.0.1', self.port))
        self.server_socket.listen(10)
        print(f"Server listening on 127.0.0.1:{self.port}", flush=True)

        try:
            while True:
                conn, addr = self.server_socket.accept()
                threading.Thread(
                    target=self.client_handler,
                    args=(conn,),
                    daemon=True,
                ).start()
        except KeyboardInterrupt:
            pass
        finally:
            self.server_socket.close()
            self.save_state()


def main():
    args = parse_args()
    server = Server(args.port, args.data_dir)
    server.load_state()
    server.run()


if __name__ == "__main__":
    main()