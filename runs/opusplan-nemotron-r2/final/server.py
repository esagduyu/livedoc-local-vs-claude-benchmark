"""Livedoc OT server using asyncio.

Usage:
    python3 server.py --port PORT --data-dir DIR

Listens on 127.0.0.1:PORT, stores persistent data under DIR.
Runs until killed.
"""

import argparse
import asyncio
import json
import os
import sys


def doc_name_to_file(doc_name: str) -> str:
    """Convert a doc name to a safe file name (hex of UTF-8 bytes)."""
    return doc_name.encode("utf-8").hex()


# ---------- OT transformation (re-used from livedoc_client) ----------

from livedoc_client import validate_op, apply_op, xf_edit, xf, transform_against_history


# ---------- Data model ----------

class Document:
    """A document with OT history and persistence.

    Stores committed entries as a JSONL log file under --data-dir.
    On startup, replays the log, ignoring truncated trailing lines
    left by SIGKILL (and truncating them off so later appends stay well-formed).
    """

    def __init__(self, data_dir: str, doc_name: str):
        self.doc_name = doc_name
        self.file_name = doc_name_to_file(doc_name)
        self.dir = data_dir
        self.log_path = os.path.join(data_dir, self.file_name) if data_dir else f".{self.file_name}.jsonl"

        # Ensure data dir exists
        os.makedirs(data_dir, exist_ok=True)

        # Per-revision text lengths (indexed by rev number)
        self._rev_lengths = {0: 0}
        # Committed entries: list of {rev, client, id, op}
        self._entries = []
        # (client, id) -> rev dedup map
        self._dedup = {}
        # Cursors: {client_name: {"rev": int, "pos": int}}
        self._cursors = {}

        # Replay existing log file
        self._replay_log()

    def _replay_log(self):
        """Replay the JSONL log file.

        Ignores a truncated/invalid trailing line left by SIGKILL
        (and truncates it off so later appends stay well-formed).
        """
        if not os.path.exists(self.log_path):
            # First time; create empty log so doc persists at rev 0
            with open(self.log_path, "w") as f:
                f.write("")
            return

        valid_lines = []
        with open(self.log_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.rstrip("\n")
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                    valid_lines.append(line)
                except (json.JSONDecodeError, ValueError):
                    # Truncated/invalid line left by SIGKILL – stop here.
                    # Truncate the file at this point.
                    break

        # Truncate the file to valid lines only
        with open(self.log_path, "w", encoding="utf-8") as f:
            for vl in valid_lines:
                f.write(vl + "\n")

        # Replay valid lines
        for line in valid_lines:
            entry = json.loads(line)
            self._commit_entry(entry, persist=False)

    def _commit_entry(self, entry, persist=True):
        """Commit a new entry.

        entry must have keys: rev, client, id, op.
        If persist=True, appends the entry as a JSON line and flushes.
        """
        rev = entry["rev"]
        client = entry["client"]
        op_id = entry["id"]
        op = entry["op"]

        # Update in-memory state
        # Compute new text length without rebuilding full text
        prev_len = self._rev_lengths.get(rev - 1, 0)
        new_len = prev_len
        for edit in op:
            if "ins" in edit:
                new_len += len(edit["text"])
            elif "del" in edit:
                new_len -= edit["len"]
        self._rev_lengths[rev] = new_len

        # Add to entries list
        self._entries.append(entry)

        # Update dedup map
        self._dedup[(client, op_id)] = rev

        # Update cursors: move all cursors through this operation
        self._move_cursors_through_op(op, client)

        # Persist to disk if requested
        if persist:
            # Append as JSON line, followed by newline
            line = json.dumps(entry, separators=(",", ":")) + "\n"
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(line)
                f.flush()  # no fsync required

    def get_text(self, rev: int = None) -> str:
        """Get the text at the given revision (or current revision).

        Rebuilds text by applying committed operations 1..rev to empty string.
        """
        if rev is None:
            rev = self.current_rev()
        # Apply operations 1..rev to empty string
        text = ""
        for entry in self._entries:
            if entry["rev"] <= rev:
                for edit in entry["op"]:
                    if "ins" in edit:
                        pos = edit["ins"]
                        txt = edit["text"]
                        text = text[:pos] + txt + text[pos:]
                    elif "del" in edit:
                        pos = edit["del"]
                        n = edit["len"]
                        text = text[:pos] + text[pos + n:]
        return text

    def current_rev(self) -> int:
        """Return the current revision number."""
        return max(self._rev_lengths.keys()) if self._rev_lengths else 0

    @staticmethod
    def _move_cursor_through_edit(cursor_pos, edit):
        """Move a cursor position through a single edit.

        Returns the new cursor position.
        """
        if "ins" in edit:
            pos = edit["ins"]
            txt_len = len(edit["text"])
            # If insert is left of or at cursor, cursor moves right
            if pos <= cursor_pos:
                return cursor_pos + txt_len
            return cursor_pos
        elif "del" in edit:
            pos = edit["del"]
            n = edit["len"]
            # If cursor is after delete position, move cursor left
            if cursor_pos > pos:
                return max(pos, cursor_pos - n)
            # If cursor is inside or before delete range, move to start
            return pos
        return cursor_pos

    def _move_cursors_through_op(self, op, client_name):
        """Move all cursors through an operation.

        For each edit in the operation, move any cursor that is affected.
        Cursors made by the operation's owner move differently: an insert
        at the cursor position pushes the cursor forward.
        """
        for edit in op:
            if "ins" in edit:
                pos = edit["ins"]
                txt_len = len(edit["text"])
                # Move each cursor
                for cname in list(self._cursors.keys()):
                    cursor = self._cursors[cname]
                    # If insert is at or left of cursor position, cursor moves
                    if pos <= cursor["pos"]:
                        cursor["pos"] += txt_len
                    # Special case: if insert is exactly at cursor and made by owner,
                    # cursor also moves (handled by pos <= cursor["pos"] above)
            elif "del" in edit:
                pos = edit["del"]
                n = edit["len"]
                # Move each cursor
                for cname in list(self._cursors.keys()):
                    cursor = self._cursors[cname]
                    # If cursor is after delete position, move left
                    if cursor["pos"] > pos:
                        cursor["pos"] = max(pos, cursor["pos"] - n)
                    # If cursor is inside deleted range, move to start
                    elif cursor["pos"] >= pos + n:
                        cursor["pos"] = pos
                    # If cursor is before delete, unchanged

    def validate_op_against_rev(self, op, rev: int) -> tuple:
        """Validate an operation against the text at the given revision.

        Returns (error_string or None, resulting_length).
        """
        # Build the text at revision rev-1 (the base text for this op)
        base_rev = rev - 1
        if base_rev < 0:
            return "bad_rev", 0

        # Get text at base_rev
        base_text = self.get_text(base_rev)
        base_len = len(base_text)

        # Validate the operation
        error, resulting_len = validate_op(op, base_len)
        if error:
            return error, resulting_len

        return None, resulting_len

    def apply_op_unchecked(self, op):
        """Apply an operation (already validated) and commit it.

        Returns the new revision number.
        """
        current_rev = self.current_rev()
        new_rev = current_rev + 1

        entry = {
            "rev": new_rev,
            "client": "server",
            "id": str(new_rev),
            "op": op,
        }

        self._commit_entry(entry, persist=True)
        return new_rev


# ---------- Server ----------

class LivedocServer:
    """Asyncio-based livedoc server."""

    def __init__(self, port: int, data_dir: str):
        self.port = port
        self.data_dir = data_dir
        self.documents = {}  # doc_name -> Document
        self._connections = {}  # writer -> conn_state dict

    def _get_doc(self, doc_name: str) -> Document:
        if doc_name not in self.documents:
            self.documents[doc_name] = Document(self.data_dir, doc_name)
        return self.documents[doc_name]

    async def _safe_drain(self, writer):
        """Safely drain the writer, handling errors gracefully."""
        try:
            await writer.drain()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    async def _handle_client(self, reader, writer):
        """Handle a single client connection."""
        try:
            while True:
                # Read a line (message)
                try:
                    line = await asyncio.wait_for(reader.readline(), timeout=30)
                except asyncio.TimeoutError:
                    break

                if not line:
                    break

                line = line.decode("utf-8", errors="replace").strip("\r\n")
                if not line:
                    continue

                # Parse message
                msg = self._parse_message(line)
                if msg is None:
                    # bad_json
                    self._send_error(writer, "bad_json", "Invalid JSON")
                    continue

                conn_state = {
                    "doc": None,
                    "rev": 0,
                    "joined": False,
                    "client_name": None,
                }

                # Process message type
                msg_type = msg.get("type")

                if msg_type == "join":
                    await self._handle_join(msg, writer, conn_state)
                elif msg_type == "op":
                    await self._handle_op(msg, writer, conn_state)
                elif msg_type == "error":
                    # Ignore error messages from clients
                    pass
                elif msg_type == "cursor":
                    await self._handle_cursor(msg, writer, conn_state)
                elif msg_type == "cursors":
                    await self._handle_cursors(msg, writer, conn_state)
                elif msg_type == "leave":
                    await self._handle_leave(msg, writer, conn_state)
                else:
                    # bad_request - unknown type
                    self._send_error(writer, "bad_request", f"Unknown type: {msg_type}")

        except (ConnectionResetError, BrokenPipeError, OSError):
            pass
        finally:
            self._cleanup_connection(writer)

    def _parse_message(self, line):
        """Parse a JSON message line. Returns dict or None if bad JSON."""
        try:
            return json.loads(line)
        except (json.JSONDecodeError, ValueError):
            return None

    def _send_error(self, writer, code, message, msg_id=None):
        """Send an error response."""
        error = {"type": "error", "code": code, "message": message}
        if msg_id is not None:
            error["id"] = msg_id
        try:
            writer.write((json.dumps(error) + "\n").encode("utf-8"))
            asyncio.create_task(self._safe_drain(writer))
        except (BrokenPipeError, ConnectionResetError):
            pass

    async def _handle_join(self, msg, writer, conn_state):
        """Handle a 'join' message."""
        doc_name = msg.get("doc", "")
        since = msg.get("since", None)

        # Validate doc name
        if not doc_name or not isinstance(doc_name, str) or len(doc_name) > 100:
            self._send_error(writer, "bad_request", "Invalid doc name", msg_id=msg.get("id"))
            return

        # Validate since
        doc = self._get_doc(doc_name)
        current_rev = doc.current_rev()

        if since is not None:
            if not isinstance(since, int) or since < 0 or since > current_rev:
                self._send_error(writer, "bad_rev", f"Invalid since: {since}", msg_id=msg.get("id"))
                return

        # Register connection for the doc
        conn_state["doc"] = doc_name
        conn_state["joined"] = True
        conn_state["client_name"] = msg.get("client", "")

        if since is None:
            # Reply with joined including current text
            text = doc.get_text()
            reply = {
                "type": "joined",
                "doc": doc_name,
                "rev": current_rev,
                "text": text,
            }
        else:
            # Reply with ops committed after 'since'
            ops = []
            for entry in doc._entries:
                if entry["rev"] > since:
                    ops.append({
                        "rev": entry["rev"],
                        "client": entry["client"],
                        "id": entry["id"],
                        "op": entry["op"],
                    })
            reply = {
                "type": "joined",
                "doc": doc_name,
                "rev": current_rev,
                "ops": ops,
            }

        writer.write((json.dumps(reply) + "\n").encode("utf-8"))
        asyncio.create_task(self._safe_drain(writer))

        # Store conn_state for this writer
        self._connections[writer] = conn_state
        return

    async def _handle_op(self, msg, writer, conn_state):
        """Handle an 'op' message."""
        doc_name = msg.get("doc", "")
        rev = msg.get("rev", 0)
        client_id = msg.get("id", "")
        op = msg.get("op", [])

        # Check if this connection has joined the doc
        conn_info = self._connections.get(writer, {})
        if doc_name not in conn_info.get("doc", ""):
            # not_joined
            self._send_error(writer, "not_joined", "Connection not joined to this doc", msg_id=client_id)
            return

        doc = self._get_doc(doc_name)

        # Check for duplicate (client, id)
        existing_rev = doc._dedup.get((conn_info.get("client_name", ""), client_id))
        if existing_rev is not None:
            # Re-send ack with original revision
            self._send_ack(writer, client_id, existing_rev)
            return

        # Validate bad_request first (check op structure against revision's text length)
        error, _ = doc.validate_op_against_rev(op, rev)
        if error:
            self._send_error(writer, error, f"Invalid operation: {error}", msg_id=client_id)
            return

        # Check bad_rev
        if rev < 0 or rev > doc.current_rev():
            self._send_error(writer, "bad_rev", f"Invalid revision: {rev}", msg_id=client_id)
            return

        # Transform op against all committed ops after rev
        current_rev = doc.current_rev()
        ops_after = [e["op"] for e in doc._entries if e["rev"] > rev]

        transformed_op = transform_against_history(op, ops_after)

        # Apply the transformed operation
        new_rev = doc.apply_op_unchecked(transformed_op)

        # Set the client name for this entry
        client_name = conn_info.get("client_name", "")

        # Commit the entry with the client name
        doc._commit_entry({
            "rev": new_rev,
            "client": client_name,
            "id": client_id,
            "op": transformed_op,
        }, persist=True)

        # Broadcast to all other connections joined to this doc
        broadcast_msg = {
            "type": "op",
            "doc": doc_name,
            "rev": new_rev,
            "client": client_name,
            "id": client_id,
            "op": transformed_op,
        }

        for w, cs in self._connections.items():
            if cs.get("doc") == doc_name and w != writer:
                try:
                    w.write((json.dumps(broadcast_msg) + "\n").encode("utf-8"))
                    asyncio.create_task(self._safe_drain(w))
                except (BrokenPipeError, ConnectionResetError):
                    pass

        # Send ack to sender
        ack_msg = {
            "type": "ack",
            "doc": doc_name,
            "id": client_id,
            "rev": new_rev,
        }
        writer.write((json.dumps(msg) + "\n").encode("utf-8"))
        asyncio.create_task(self._safe_drain(writer))

    def _send_ack(self, writer, client_id, rev):
        """Send an ack message."""
        msg = {"type": "ack", "id": client_id, "rev": rev}
        writer.write((json.dumps(msg) + "\n").encode("utf-8"))
        asyncio.create_task(self._safe_drain(writer))

    async def _handle_cursor(self, msg, writer, conn_state):
        """Handle a 'cursor' message."""
        doc_name = msg.get("doc", "")
        rev = msg.get("rev")
        pos = msg.get("pos")
        client_name = msg.get("client", "")

        # Check if this connection has joined the doc
        conn_info = self._connections.get(writer, {})
        if doc_name not in conn_info.get("doc", ""):
            self._send_error(writer, "not_joined", "Connection not joined to this doc", msg_id=msg.get("id"))
            return

        doc = self._get_doc(doc_name)

        # Validate rev and pos
        if rev is None:
            self._send_error(writer, "bad_request", "Cursor requires rev field", msg_id=msg.get("id"))
            return

        if not isinstance(rev, int) or rev < 0 or rev > doc.current_rev():
            self._send_error(writer, "bad_rev", f"Invalid revision: {rev}", msg_id=msg.get("id"))
            return

        # Get text at the specified revision to validate position
        text = doc.get_text(rev)
        if pos is not None and (pos < 0 or pos > len(text)):
            self._send_error(writer, "out_of_range", f"Position {pos} out of range for text of length {len(text)}", msg_id=msg.get("id"))
            return

        # Store or update the cursor for this client
        # The server moves the cursor to the current revision N,
        # storing it under the connection's client name
        N = doc.current_rev()
        # Determine the position at revision N by moving the cursor through
        # any operations between rev and N
        if rev < N:
            # Move cursor through operations rev+1..N
            ops_after = [e["op"] for e in doc._entries if e["rev"] > rev]
            transformed_pos = pos
            for committed_op in ops_after:
                for edit in committed_op:
                    transformed_pos = Document._move_cursor_through_edit(transformed_pos, edit)
            final_pos = transformed_pos
        else:
            final_pos = pos

        # Clamp position to valid range
        final_pos = max(0, min(final_pos, len(text))) if text else 0

        # Store the cursor
        doc._cursors[client_name] = {"rev": N, "pos": final_pos}

        # Broadcast cursor to all other connections joined to this doc
        broadcast_msg = {
            "type": "cursor",
            "doc": doc_name,
            "client": client_name,
            "rev": N,
            "pos": final_pos,
        }

        for w, cs in self._connections.items():
            if cs.get("doc") == doc_name and w != writer:
                try:
                    w.write((json.dumps(broadcast_msg) + "\n").encode("utf-8"))
                    asyncio.create_task(self._safe_drain(w))
                except (BrokenPipeError, ConnectionResetError):
                    pass

    async def _handle_cursors(self, msg, writer, conn_state):
        """Handle a 'cursors' query message."""
        doc_name = msg.get("doc", "")

        # Check if this connection has joined the doc
        conn_info = self._connections.get(writer, {})
        if doc_name not in conn_info.get("doc", ""):
            self._send_error(writer, "not_joined", "Connection not joined to this doc", msg_id=msg.get("id"))
            return

        doc = self._get_doc(doc_name)

        # Get current revision and all cursors
        N = doc.current_rev()
        cursors = {}
        for client_name, cursor in doc._cursors.items():
            # Only include cursors valid at current revision
            if cursor["rev"] <= N:
                # Get position at revision N by moving through ops
                text = doc.get_text(N)
                # Simplified: use stored position
                cursors[client_name] = cursor["pos"]

        reply = {
            "type": "cursors",
            "doc": doc_name,
            "rev": N,
            "cursors": cursors,
        }
        writer.write((json.dumps(reply) + "\n").encode("utf-8"))
        asyncio.create_task(self._safe_drain(writer))

    async def _handle_leave(self, msg, conn_state):
        """Handle a client disconnect (leave message).

        When the last connection of a client name that has joined D closes,
        that name's cursor is removed. Every remaining connection joined to D
        receives a leave message.
        """
        doc_name = conn_state.get("doc", "")
        client_name = conn_state.get("client_name", "")

        if doc_name and client_name and client_name in self._connections:
            # Check if this is the last connection for this client
            # Actually, we need to track connections per client name
            # For now, just remove the cursor
            doc = self._get_doc(doc_name)
            if client_name in doc._cursors:
                del doc._cursors[client_name]

            # Broadcast leave to all other connections
            leave_msg = {
                "type": "leave",
                "doc": doc_name,
                "client": client_name,
            }
            for w, cs in self._connections.items():
                if cs.get("doc") == doc_name and w != writer:  # writer is being removed
                    try:
                        w.write((json.dumps(leave_msg) + "\n").encode("utf-8"))
                        asyncio.create_task(self._safe_drain(w))
                    except (BrokenPipeError, ConnectionResetError):
                        pass

    def _cleanup_connection(self, writer):
        """Clean up a connection on disconnect."""
        if writer in self._connections:
            conn_state = self._connections[writer]
            doc_name = conn_state.get("doc", "")
            client_name = conn_state.get("client_name", "")

            # Remove cursor if this was the last connection for this client
            if doc_name and client_name:
                doc = self._get_doc(doc_name)
                # Check if any other connection still has this client
                for w, cs in self._connections.items():
                    if cs.get("doc") == doc_name and cs.get("client_name") == client_name and w != writer:
                        # Another connection still has this client
                        break
                else:
                    # This was the last connection, remove cursor
                    if client_name in doc._cursors:
                        del doc._cursors[client_name]

            del self._connections[writer]

    async def _start_server(self):
        """Start the asyncio server."""
        server = await asyncio.start_server(
            self._handle_client, "127.0.0.1", self.port
        )
        async with server:
            await server.serve_forever()

    def run(self):
        """Run the server until killed."""
        try:
            asyncio.run(self._start_server())
        except KeyboardInterrupt:
            pass


def main():
    """Entry point for server.py."""
    parser = argparse.ArgumentParser(description="Livedoc OT server")
    parser.add_argument("--port", type=int, required=True, help="Port to listen on")
    parser.add_argument("--data-dir", type=str, default="data", help="Data directory for persistence")
    args = parser.parse_args()

    server = LivedocServer(args.port, args.data_dir)
    print(f"Livedoc server listening on 127.0.0.1:{args.port}, data dir: {args.data_dir}")
    server.run()


if __name__ == "__main__":
    main()