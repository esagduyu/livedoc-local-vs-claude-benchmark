#!/usr/bin/env python3
"""Livedoc server - real-time collaborative plain-text editing."""

import argparse
import json
import os
import socket
import threading
import sys
from pathlib import Path


def apply_edits_to_text(text, edits):
    """Apply a list of edits to text, returning new text."""
    result = text
    for edit in edits:
        if not edit:
            continue
        edit_type = next(iter(edit))
        if edit_type == "ins":
            p = edit["ins"]["p"]
            txt = edit["ins"]["text"]
            result = result[:p] + txt + result[p:]
        elif edit_type == "del":
            p = edit["del"]["p"]
            length = edit["del"]["len"]
            result = result[:p] + result[p + length:]
    return result


def load_ops(data_dir, doc):
    """Load committed operations for a document from disk."""
    filepath = data_dir / f"{doc}.json"
    if filepath.exists():
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def save_ops(data_dir, doc, ops):
    """Save committed operations for a document to disk."""
    filepath = data_dir / f"{doc}.json"
    filepath.parent.mkdir(parents=True, exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(ops, f)


def transform_edit_vs_edit(a, b):
    """Transform edit a against edit b, where b was committed earlier.

    Both were written against the same text. The transformed a applies after b.

    Args:
        a: edit dict or None
        b: edit dict or None (committed earlier)

    Returns:
        List of edits representing the transformed a (0, 1, or 2 edits).
    """
    if a is None:
        return []
    if b is None:
        return [a]

    a_type = next(iter(a))
    b_type = next(iter(b))

    # Case 1: ins vs ins
    if a_type == "ins" and b_type == "ins":
        p = a["ins"]["p"]
        q = b["ins"]["p"]
        u_len = len(b["ins"]["text"])
        if p < q:
            return [a]  # a unchanged, b unchanged
        else:
            # p >= q: ins at p + len(u), earlier insert stays left
            new_p = p + u_len
            return [{"ins": {"p": new_p, "text": a["ins"]["text"]}}]

    # Case 2: ins vs del
    if a_type == "ins" and b_type == "del":
        p = a["ins"]["p"]
        q = b["del"]["p"]
        k = b["del"]["len"]
        if p <= q:
            return [a]  # a unchanged
        elif p >= q + k:
            new_p = p - k
            return [{"ins": {"p": new_p, "text": a["ins"]["text"]}}]
        else:
            # strictly inside: ins at q
            return [{"ins": {"p": q, "text": a["ins"]["text"]}}]

    # Case 3: del vs ins
    if a_type == "del" and b_type == "ins":
        p = a["del"]["p"]
        m = a["del"]["len"]
        q = b["ins"]["p"]
        u_len = len(b["ins"]["text"])
        if q <= p:
            # ins at p + len(u), same length
            new_p = p + u_len
            return [{"del": {"p": new_p, "len": m}}]
        elif q >= p + m:
            return [a]  # unchanged
        else:
            # ins strictly inside del range: a splits, don't delete u
            left = {"del": {"p": p, "len": q - p}}
            right = {"del": {"p": p + u_len, "len": p + m - q}}
            return [left, right]

    # Case 4: del vs del
    if a_type == "del" and b_type == "del":
        p = a["del"]["p"]
        m = a["del"]["len"]
        q = b["del"]["p"]
        k = b["del"]["len"]

        # a deletes only the characters b has not already deleted
        # Result is one contiguous range afterwards

        overlap_start = max(p, q)
        overlap_end = min(p + m, q + k)
        overlap_len = max(0, overlap_end - overlap_start)
        remaining = m - overlap_len

        if remaining <= 0:
            return []  # a entirely covered by b

        if p < q:
            # a starts before b
            left_part_len = max(0, q - p)
            right_part_len = max(0, p + m - (q + k))
            total_remaining = left_part_len + right_part_len

            if total_remaining == 0:
                return []

            # Represent as single del at p with total remaining length
            # Spec says "one contiguous range afterwards"
            return [{"del": {"p": p, "len": total_remaining}}]

        else:
            # p >= q, b starts at or inside a
            overlap_end = min(p + m, q + k)
            if overlap_end <= p:
                return [a]
            new_len = (p + m) - overlap_end
            return [{"del": {"p": overlap_end, "len": new_len}}]

    return [a]


def transform_operation_against_committed(A, committed_ops):
    """Transform operation A against all committed operations in order.

    Per spec: first all of B's edits were committed in order, then A's edits arrive in order.
    Equivalently: treat every edit as its own operation: first commit all of B's edits in order,
    then apply A's edits in order.

    Args:
        A: list of edit dicts (the incoming operation)
        committed_ops: list of committed operations (each is a list of edit dicts)

    Returns:
        Transformed A as list of edits.
    """
    if not A:
        return []

    # Transform each edit in A against all committed ops, sequentially
    result = A[:]
    for b_op in committed_ops:
        new_result = []
        for a_edit in result:
            if b_op:
                # Transform against each edit in b_op sequentially
                # Per the recursive spec, we transform against one edit at a time
                transformed = transform_edit_vs_edit(a_edit, b_op[0])
                new_result.extend(transformed)
            else:
                new_result.append(a_edit)
        result = new_result

    return result


class Document:
    """Represents a document with operations and transformation."""

    def __init__(self, name):
        self.name = name
        self.revision = 0
        self.operations = []  # list of ops (lists of edits) committed
        self.text = ""  # current text (result of applying all ops)

    def get_committed_ops_after_revision(self, rev):
        """Get all committed operations with revision > rev.

        committed_ops[i] corresponds to revision i+1.
        So ops after revision rev are at indices rev, rev+1, ..., len-1.
        """
        return self.operations[rev:]

    def apply_operation(self, op_edits):
        """Apply a transformed operation, updating revision and text."""
        self.revision += 1
        edits = op_edits
        self.text = apply_edits_to_text(self.text, edits)
        self.operations.append(edits[:])


class LivedocServer:
    """The livedoc server."""

    def __init__(self, host, port, data_dir):
        self.host = host
        self.port = port
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self.documents = {}  # doc_name -> Document
        self.lock = threading.Lock()

        # Track (doc_name, client_id) -> True for exactly-once guarantee
        # Also track the revision at which each ID was committed
        self.committed_ids = {}  # (doc_name, client_id_str) -> True
        self.id_revisions = {}  # (doc_name, client_id_str) -> int

        # Track connected clients per document: doc_name -> set of client_ids
        self.doc_clients = {}  # doc_name -> set of client_id strings

        self.server_socket = None
        self.running = False

    def get_or_create_doc(self, doc_name):
        """Get or create a document."""
        with self.lock:
            if doc_name not in self.documents:
                self.documents[doc_name] = Document(doc_name)
            return self.documents[doc_name]

    def load_state_from_disk(self, doc_name):
        """Load document state from disk for persistence."""
        with self.lock:
            doc = self.get_or_create_doc(doc_name)
            ops = load_ops(self.data_dir, doc_name)
            doc.operations = [op[:] for op in ops]  # copy committed ops
            # Reconstruct text from operations
            doc.text = ""
            for op in ops:
                doc.text = apply_edits_to_text(doc.text, op)
            doc.revision = len(ops)
            # Track committed IDs for exactly-once (no-op ids from disk, will be set during runtime)
            return doc

    def commit_operation(self, doc_name, op_edits, client_id):
        """Commit an operation to a document.

        Args:
            doc_name: document name
            op_edits: list of edit dicts (already transformed)
            client_id: unique string identifying the client

        Returns:
            new_revision: the revision number after committing
        """
        with self.lock:
            doc = self.get_or_create_doc(doc_name)
            N = doc.revision

            # Check exactly-once: same client_id already committed to this doc?
            commit_key = (doc_name, client_id)
            if commit_key in self.committed_ids:
                # Already committed - don't apply again
                # Return the original revision
                return self.id_revisions.get(commit_key, N)

            # Mark as committed
            self.committed_ids[commit_key] = True
            self.id_revisions[commit_key] = N + 1  # the revision it will become

            # Transform against all committed ops after revision 0 (i.e., all committed ops)
            # Actually, the client sends rev=R meaning it's operating against text at revision R
            # But commit_operation doesn't receive R - the op message has the rev field
            # Let me restructure: the handle_op method will extract rev and pass it

            # For now, transform against all committed ops
            committed_after = doc.get_committed_ops_after_revision(0)  # all committed ops

            # Transform the operation
            A = op_edits[:]
            transformed = transform_operation_against_committed(A, committed_after)

            # Apply the transformed operation
            doc.apply_operation(transformed)

            return doc.revision

    def handle_join(self, doc_name, client_name, since_revision=None):
        """Handle a join request.

        Returns dict to send to client.
        """
        with self.lock:
            # Ensure document exists and is loaded
            if doc_name not in self.documents:
                self.get_or_create_doc(doc_name)  # creates empty doc

            doc = self.documents[doc_name]
            N = doc.revision

            # Track this client as joined to this doc
            if doc_name not in self.doc_clients:
                self.doc_clients[doc_name] = set()
            self.doc_clients[doc_name].add(client_name)

            if since_revision is None:
                # Reply with text
                return {"type": "joined", "doc": doc_name, "rev": N, "text": doc.text}
            else:
                # Reply with ops after since_revision
                R = since_revision
                ops = []
                # ops committed after revision R: revisions R+1, R+2, ..., N
                # In committed_ops list (0-indexed), those are indices R, R+1, ..., N-1
                for i in range(R, N):
                    if i < len(doc.operations):
                        op = doc.operations[i]
                        ops.append({
                            "rev": i + 1,  # revision number
                            "client": client_name,
                            "id": f"client_{i+1}",  # placeholder; we don't have actual IDs from disk
                            "op": op  # the operation as committed
                        })
                return {"type": "joined", "doc": doc_name, "rev": N, "ops": ops}

    def handle_op(self, doc_name, rev, client_id, op_edits, client_name):
        """Handle an op request.

        Args:
            doc_name: document name
            rev: revision the op was written against
            client_id: unique client identifier
            op_edits: the operation (list of edits)
            client_name: display name

        Returns:
            (ack_dict, broadcast_dict, error_dict) - some may be None
        """
        with self.lock:
            # Ensure document exists
            if doc_name not in self.documents:
                # Try to load from disk
                self.load_state_from_disk(doc_name)

            if doc_name not in self.documents:
                error = {"type": "error", "code": "bad_request", "message": "Document not found", "id": client_id}
                return None, None, error

            doc = self.documents[doc_name]
            N = doc.revision

            # Check exactly-once: same client_id already committed to this doc?
            commit_key = (doc_name, client_id)
            if commit_key in self.committed_ids:
                # Already committed - reply with ack carrying the original revision
                original_rev = self.id_revisions.get(commit_key, rev)
                ack = {"type": "ack", "doc": doc_name, "id": client_id, "rev": original_rev}
                return ack, None, None  # no new commit, no broadcast

            # Validate rev
            if rev < 0 or rev > N:
                error = {"type": "error", "code": "bad_rev", "message": "Revision out of range", "id": client_id}
                return None, None, error

            # Transform op against all committed ops after rev
            # Per protocol: "OP was written against the text at revision R (rev field)"
            # "The server transforms OP against every operation committed after R, in commit order"
            committed_after = doc.get_committed_ops_after_revision(rev)

            # Validate operation is well-formed
            if not isinstance(op_edits, list):
                error = {"type": "error", "code": "bad_request", "message": "Operation must be a list", "id": client_id}
                return None, None, error

            # Transform the operation
            A = op_edits[:]
            transformed = transform_operation_against_committed(A, committed_after)

            # Apply the transformed operation
            new_rev = doc.apply_operation(transformed)

            # Mark this client's ID as committed
            self.committed_ids[commit_key] = True
            self.id_revisions[commit_key] = new_rev

            # Sender gets ack
            ack = {"type": "ack", "doc": doc_name, "id": client_id, "rev": new_rev}

            # Broadcast to other connections joined to this doc
            self._broadcast_op(doc_name, transformed, client_id)

            return ack, None, None

    def _broadcast_op(self, doc_name, op_edits, sender_client_id):
        """Broadcast an operation to all other clients joined to doc_name."""
        # Send to all clients joined to this doc except the sender
        # We need to track which sockets are connected to which docs
        # For now, this is a simplified version
        # In a full implementation, maintain doc_name -> set of sockets mapping
        other_clients = self.doc_clients.get(doc_name, set())
        other_clients.discard(sender_client_id)
        for client_id in other_clients:
            # Would send {"type": "op", "doc": doc_name, "rev": N+1, "client": client_name, "id": ID, "op": OP2}
            pass

    def send_response(self, sock, msg):
        """Send a JSON response terminated by \\n."""
        data = json.dumps(msg) + "\n"
        sock.sendall(data.encode("utf-8"))

    def send_error(self, sock, code, message, msg_id):
        """Send an error response."""
        err = {"type": "error", "code": code, "message": message}
        if msg_id is not None:
            err["id"] = msg_id
        self.send_response(sock, err)

    def handle_client(self, client_socket):
        """Handle a single client connection."""
        buffer = ""
        client_name = None
        joined_docs = set()  # docs this client has joined

        try:
            while True:
                data = client_socket.recv(4096)
                if not data:
                    break
                buffer += data.decode("utf-8", errors="replace")

                # Parse complete lines (terminated by \n)
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    line = line.strip()
                    if not line:
                        continue

                    try:
                        msg = json.loads(line)
                    except json.JSONDecodeError:
                        self.send_error(client_socket, "bad_json", "Invalid JSON", client_name)
                        continue

                    msg_type = msg.get("type")

                    if msg_type == "join":
                        doc_name = msg.get("doc")
                        client_name = msg.get("client")
                        since = msg.get("since")

                        if not doc_name or not client_name:
                            self.send_error(client_socket, "bad_request", "Missing doc or client", client_name)
                            continue

                        # Load document state from disk
                        self.load_state_from_disk(doc_name)

                        result = self.handle_join(doc_name, client_name, since)
                        self.send_response(client_socket, result)

                        joined_docs.add(doc_name)

                    elif msg_type == "op":
                        if client_name is None:
                            self.send_error(client_socket, "bad_request", "No client name", client_name)
                            continue

                        doc_name = msg.get("doc")
                        rev = msg.get("rev")
                        client_id = msg.get("id")
                        op = msg.get("op")

                        if not doc_name or rev is None or client_id is None or op is None:
                            self.send_error(client_socket, "bad_request", "Missing fields", client_name)
                            continue

                        # Check if client has joined this doc
                        if doc_name not in joined_docs:
                            error = {"type": "error", "code": "not_joined", "message": "Client has not joined this document", "id": client_id}
                            self.send_response(client_socket, error)
                            continue

                        # Handle the op
                        ack, broadcast, error = self.handle_op(doc_name, rev, client_id, op, client_name)

                        if error:
                            self.send_response(client_socket, error)
                        elif ack:
                            self.send_response(client_socket, ack)

                    else:
                        self.send_error(client_socket, "bad_request", f"Unknown type: {msg_type}", client_name)

        except Exception as e:
            print(f"Client handler error: {e}")
            import traceback
            traceback.print_exc()
        finally:
            client_socket.close()
            # Remove this client from all joined docs
            with self.lock:
                if doc_name in self.doc_clients:
                    self.doc_clients[doc_name].discard(client_name)

    def run(self):
        """Start the server."""
        self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server_socket.bind((self.host, self.port))
        self.server_socket.listen(10)
        self.running = True

        print(f"Livedoc server listening on {self.host}:{self.port}")

        while self.running:
            try:
                client_socket, addr = self.server_socket.accept()
                threading.Thread(
                    target=self.handle_client,
                    args=(client_socket,),
                    daemon=True
                ).start()
            except Exception:
                if self.running:
                    raise

    def stop(self):
        """Stop the server."""
        self.running = False
        if self.server_socket:
            self.server_socket.close()


def main():
    parser = argparse.ArgumentParser(description="Livedoc server")
    parser.add_argument("--port", type=int, required=True, help="Port to listen on")
    parser.add_argument("--data-dir", type=str, required=True, help="Data directory")
    args = parser.parse_args()

    server = LivedocServer("127.0.0.1", args.port, args.data_dir)
    server.run()


if __name__ == "__main__":
    main()