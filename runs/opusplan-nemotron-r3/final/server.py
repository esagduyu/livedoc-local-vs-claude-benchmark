"""Livedoc OT server."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import uuid
from pathlib import Path

from livedoc_ot import validate_op, apply_op, transform_against_history


def doc_name_to_filename(doc_name: str) -> str:
    """Convert a doc name to a safe filename (hex of UTF-8 name)."""
    return hashlib.sha256(doc_name.encode("utf-8")).hexdigest()


class Document:
    """Represents a document with its revision history."""

    def __init__(self, doc_name: str):
        self.doc_name = doc_name
        self.rev = 0  # current revision number
        self.text = ""  # current text
        self.history = []  # list of (client, id, op) tuples, one per revision
        self.client_rev_map = {}  # (client, id) -> rev, for exactly-once guarantee
        # Per-revision text lengths for efficient validation
        self.rev_lengths = [0]  # rev_lengths[r] = length after revision r
        # Cursor tracking: {client_name: {"rev": int, "pos": int}}
        self.cursors = {}
        # Track which client names have joined this document
        self.joined_names = set()

    def remove_cursor(self, client_name: str) -> bool:
        """Remove a cursor for a client. Returns True if cursor was removed."""
        if client_name in self.cursors:
            del self.cursors[client_name]
            return True
        return False

    def get_length_at_rev(self, rev: int) -> int:
        """Get the text length at the given revision."""
        if rev < 0:
            return 0
        if rev < len(self.rev_lengths):
            return self.rev_lengths[rev]
        # Should not happen if rev <= current rev
        return self.rev_lengths[-1] if self.rev_lengths else 0

    def commit_op(self, op: list, client_name: str, client_id: str) -> int | None:
        """Commit an operation and update document state.

        Returns the new revision number, or None if the (client, id) was already committed.
        """
        # Check exactly-once: if same (client, id) already committed
        key = (client_name, client_id)
        if key in self.client_rev_map:
            return None  # Already committed

        # Transform op against history
        transformed = transform_against_history(op, [h[2] for h in self.history])

        # Get current text length before applying
        current_len = len(self.text)

        # Validate op against text length at current revision
        try:
            new_len = validate_op(op, current_len)
        except ValueError:
            # Validation failed, don't commit
            return None

        # Apply op to current text
        self.text = apply_op(self.text, transformed)

        # Increment revision
        new_rev = self.rev + 1
        self.rev = new_rev

        # Update per-revision lengths
        self.rev_lengths.append(new_len)

        # Record in history with the client_id
        self.history.append((client_name, client_id, transformed))
        self.client_rev_map[key] = self.rev

        return self.rev


class Server:
    """Livedoc OT server using asyncio."""

    def __init__(self, port: int, data_dir: str | None = None):
        self.port = port
        self.data_dir = data_dir
        self.documents: dict[str, Document] = {}
        self.connections: dict[str, set[asyncio.StreamWriter]] = {}  # doc -> set of writers

        # Ensure data directory exists
        if data_dir:
            os.makedirs(data_dir, exist_ok=True)
            # Load existing documents from persistence log
            self._load_persistence()

    def _load_persistence(self):
        """Load document state from persistence logs on startup."""
        if not self.data_dir:
            return

        # Read all .jsonl files in data dir
        for filename in os.listdir(self.data_dir):
            if not filename.endswith(".jsonl"):
                continue
            filepath = os.path.join(self.data_dir, filename)

            # Parse all valid lines
            valid_entries = []
            with open(filepath, "r") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        valid_entries.append(entry)
                    except json.JSONDecodeError:
                        # Truncated line from crash - ignore and truncate file later
                        break

            # Truncate file to valid entries only
            total_lines = sum(1 for _ in open(filepath))
            if len(valid_entries) < total_lines:
                with open(filepath, "w") as f:
                    for entry in valid_entries:
                        f.write(json.dumps(entry) + "\n")

            # Rebuild document state from entries
            # Group entries by doc name and replay in order
            doc_entries: dict[str, list] = {}
            for entry in valid_entries:
                doc_name = entry.get("doc", "")
                if doc_name not in doc_entries:
                    doc_entries[doc_name] = []
                doc_entries[doc_name].append(entry)

            for doc_name, entries in doc_entries.items():
                if doc_name not in self.documents:
                    self.documents[doc_name] = Document(doc_name)

                document = self.documents[doc_name]
                # Replay all ops in order to reconstruct text and history
                # We need to apply each op starting from empty text
                # But Document.commit_op checks exactly-once, so we need to directly set state
                # For replay, we directly set the internal state

                # Reset document state
                document.text = ""
                document.history = []
                document.client_rev_map = {}
                document.rev_lengths = [0]
                document.rev = 0

                # Replay each entry in order
                for entry in entries:
                    rev = entry["rev"]
                    client = entry["client"]
                    op_id = entry["id"]
                    op = entry["op"]

                    # Apply the op directly, bypassing exactly-once check
                    # Transform against empty history (first op) or existing transformed ops
                    if not document.history:
                        transformed = op  # First op, no transformation needed
                    else:
                        transformed = transform_against_history(op, [h[2] for h in document.history])

                    # Apply op
                    document.text = apply_op(document.text, transformed)

                    # Increment revision
                    document.rev = rev

                    # Update per-revision lengths
                    document.rev_lengths.append(len(document.text))

                    # Record in history
                    document.history.append((client, op_id, transformed))
                    document.client_rev_map[(client, op_id)] = document.rev

    async def handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        """Handle a single client connection."""
        doc_name = None
        client_name = None

        try:
            while True:
                try:
                    line = await asyncio.wait_for(reader.readline(), timeout=30)
                except asyncio.TimeoutError:
                    break

                if not line:
                    break

                # Decode line as UTF-8 JSON
                try:
                    line_str = line.decode("utf-8").strip()
                except UnicodeDecodeError:
                    self._send_error_bytes(writer, "bad_json", "Invalid UTF-8")
                    continue

                if not line_str:
                    continue

                try:
                    message = json.loads(line_str)
                except json.JSONDecodeError:
                    self._send_error_bytes(writer, "bad_json", "Invalid JSON")
                    continue

                if not isinstance(message, dict):
                    self._send_error_bytes(writer, "bad_json", "Not a JSON object")
                    continue

                msg_type = message.get("type")

                if msg_type == "join":
                    await self._handle_join(writer, message)
                    doc_name = message.get("doc")
                    client_name = message.get("client")
                elif msg_type == "op":
                    if doc_name is None or client_name is None:
                        self._send_error_bytes(writer, "not_joined", "Document not joined")
                        continue
                    await self._handle_op(writer, message, doc_name, client_name)
                elif msg_type == "cursor":
                    if doc_name is None or client_name is None:
                        self._send_error_bytes(writer, "not_joined", "Document not joined")
                        continue
                    await self._handle_cursor(writer, message, doc_name, client_name)
                elif msg_type == "cursors":
                    if doc_name is None:
                        self._send_error_bytes(writer, "not_joined", "Document not joined")
                        continue
                    await self._handle_cursors(writer, message, doc_name)
                elif msg_type == "leave":
                    break
                else:
                    self._send_error_bytes(writer, "bad_request", f"Unknown type: {msg_type}")

        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            # On disconnect, clean up
            if doc_name and client_name and self.documents.get(doc_name):
                document = self.documents[doc_name]
                document.remove_cursor(client_name)
                # Broadcast leave for this client
                self._broadcast_cursor_leave(doc_name, client_name)
                # Unregister this writer from connections
                if doc_name in self.connections:
                    self.connections[doc_name].discard(writer)

    async def _handle_join(self, writer: asyncio.StreamWriter, message: dict):
        """Handle a join message."""
        doc = message.get("doc")
        client = message.get("client")

        if not doc or not client:
            self._send_error_bytes(writer, "bad_request", "Missing doc or client field")
            return

        if len(doc) == 0 or len(doc) > 100:
            self._send_error_bytes(writer, "bad_request", "Invalid doc name")
            return

        # Lazily create document
        if doc not in self.documents:
            self.documents[doc] = Document(doc)

        document = self.documents[doc]
        document.joined_names.add(client)  # Track joined names

        since = message.get("since")

        if since is not None:
            if not isinstance(since, int) or since < 0 or since > document.rev:
                self._send_error_bytes(writer, "bad_rev", "Invalid since revision")
                return

        # Send joined response
        if since is None:
            response = {
                "type": "joined",
                "doc": doc,
                "rev": document.rev,
                "text": document.text,
            }
        else:
            # Get ops since revision 'since'
            ops_since = []
            for rev_num, (c, cid, op) in enumerate(document.history, 1):
                if rev_num > since:
                    ops_since.append({"rev": rev_num, "client": c, "id": cid, "op": op})

            response = {
                "type": "joined",
                "doc": doc,
                "rev": document.rev,
                "ops": ops_since,
            }

        self._send_json_bytes(writer, response, message.get("id"))

    async def _handle_op(self, writer: asyncio.StreamWriter, message: dict, doc: str, client_name: str):
        """Handle an op message."""
        rev = message.get("rev")
        op_id = message.get("id")
        op = message.get("op")

        if not op_id or op is None:
            self._send_error_bytes(writer, "bad_request", "Missing fields")
            return

        if doc not in self.documents:
            self._send_error_bytes(writer, "not_joined", "Document not joined")
            return

        document = self.documents[doc]

        # Check exactly-once: if same (client, id) already committed
        key = (client_name, op_id)
        if key in document.client_rev_map:
            original_rev = document.client_rev_map[key]
            self._send_ack_bytes(writer, doc, op_id, original_rev)
            return

        # Validate rev
        if rev is not None:
            if not isinstance(rev, int) or rev < 0 or rev > document.rev:
                self._send_error_bytes(writer, "bad_rev", "Invalid revision")
                return
        else:
            rev = 0

        # Validate op against text length at revision R
        # For new operations (rev = current rev), validate against current text length
        current_len = len(document.text)

        try:
            new_len = validate_op(op, current_len)
        except ValueError:
            self._send_error_bytes(writer, "out_of_range", "Operation out of range")
            return

        # Transform op against history
        transformed = transform_against_history(op, [h[2] for h in document.history])

        # Apply op
        document.text = apply_op(document.text, transformed)
        document.rev += 1

        # Update per-revision lengths
        document.rev_lengths.append(len(document.text))

        # Record in history and client_rev_map
        key = (client_name, op_id)
        document.history.append((client_name, op_id, transformed))
        document.client_rev_map[key] = document.rev

        # Persist operation
        self._persist_op(doc, document.rev, client_name, op_id, op)

        # Send ack to sender
        self._send_ack_bytes(writer, doc, op_id, document.rev)

        # Broadcast to other connections joined to this doc
        self._broadcast_op(doc, writer, {"rev": document.rev, "client": client_name, "id": op_id, "op": transformed})

    async def _handle_cursor(self, writer: asyncio.StreamWriter, message: dict, doc: str, client_name: str):
        """Handle a cursor set message."""
        rev = message.get("rev")
        pos = message.get("pos")

        if rev is None or pos is None:
            self._send_error_bytes(writer, "bad_request", "Missing cursor fields")
            return

        if doc not in self.documents:
            self._send_error_bytes(writer, "not_joined", "Document not joined")
            return

        document = self.documents[doc]

        # Validate pos against text at revision rev
        length_at_rev = document.get_length_at_rev(rev)
        if pos < 0 or pos > length_at_rev:
            self._send_error_bytes(writer, "out_of_range", "Position out of range")
            return

        # Store cursor
        document.set_cursor(client_name, rev, pos)

        # Broadcast cursor to other connections
        self._broadcast_cursor(doc, writer, {"rev": rev, "client": client_name, "pos": pos}, exclude=writer)

    async def _handle_cursors(self, writer: asyncio.StreamWriter, message: dict, doc: str):
        """Handle a query cursors message."""
        if doc not in self.documents:
            self._send_error_bytes(writer, "not_found", "Document not found")
            return

        document = self.documents[doc]
        cursors = document.get_cursors()

        response = {
            "type": "cursors",
            "doc": doc,
            "rev": document.rev,
            "cursors": cursors,
        }

        self._send_json_bytes(writer, response, message.get("id"))

    def _broadcast_op(self, doc: str, exclude_writer: asyncio.StreamWriter, message: dict):
        """Broadcast an op message to all connections joined to this doc, except exclude_writer."""
        if doc not in self.connections:
            return
        msg = (json.dumps(message) + "\n").encode("utf-8")
        for writer in list(self.connections.get(doc, set())):
            if writer != exclude_writer:
                try:
                    writer.write(msg)
                except (ConnectionResetError, BrokenPipeError):
                    self.connections.get(doc, set()).discard(writer)

    def _broadcast_cursor(self, doc: str, exclude_writer: asyncio.StreamWriter, message: dict, exclude: asyncio.StreamWriter | None = None):
        """Broadcast a cursor message to all connections joined to this doc."""
        if doc not in self.connections:
            return
        msg = (json.dumps(message) + "\n").encode("utf-8")
        for writer in list(self.connections.get(doc, set())):
            if writer != exclude_writer:
                try:
                    writer.write(msg)
                except (ConnectionResetError, BrokenPipeError):
                    self.connections.get(doc, set()).discard(writer)

    def _broadcast_cursor_leave(self, doc: str, client_name: str):
        """Broadcast a leave message for a client."""
        if doc not in self.connections:
            return
        msg = json.dumps({"type": "leave", "doc": doc, "client": client_name}) + "\n"
        for writer in list(self.connections.get(doc, set())):
            try:
                writer.write(msg)
            except (ConnectionResetError, BrokenPipeError):
                self.connections.get(doc, set()).discard(writer)

    def _persist_op(self, doc: str, rev: int, client: str, op_id: str, op: list):
        """Persist an operation to the JSONL log file."""
        if not self.data_dir:
            return

        filename = doc_name_to_filename(doc)
        filepath = os.path.join(self.data_dir, f"{filename}.jsonl")

        entry = {
            "rev": rev,
            "client": client,
            "id": op_id,
            "op": op,
            "doc": doc,
        }

        # Append and flush
        with open(filepath, "a") as f:
            f.write(json.dumps(entry) + "\n")
            f.flush()

    def _send_json_bytes(self, writer: asyncio.StreamWriter, data: dict, msg_id: str | None = None):
        """Send a JSON message to the writer as bytes."""
        response = dict(data)
        if msg_id is not None:
            response["id"] = msg_id
        writer.write((json.dumps(response) + "\n").encode("utf-8"))

    def _send_error_bytes(self, writer: asyncio.StreamWriter, code: str, message: str):
        """Send an error response as bytes."""
        response = {"type": "error", "code": code, "message": message}
        writer.write((json.dumps(response) + "\n").encode("utf-8"))

    def _send_ack_bytes(self, writer: asyncio.StreamWriter, doc: str, client_id: str, rev: int):
        """Send an ack message as bytes."""
        response = {"type": "ack", "doc": doc, "id": client_id, "rev": rev}
        writer.write((json.dumps(response) + "\n").encode("utf-8"))


async def main():
    import argparse

    parser = argparse.ArgumentParser(description="Livedoc OT Server")
    parser.add_argument("--port", type=int, required=True, help="Port to listen on")
    parser.add_argument("--data-dir", type=str, default=None, help="Data directory for persistence")
    args = parser.parse_args()

    server = Server(args.port, args.data_dir)

    async def client_handler(reader, writer):
        await server.handle_client(reader, writer)

    sock = await asyncio.start_server(client_handler, "127.0.0.1", server.port)
    print(f"Server listening on 127.0.0.1:{server.port}", flush=True)

    async with sock:
        await sock.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())