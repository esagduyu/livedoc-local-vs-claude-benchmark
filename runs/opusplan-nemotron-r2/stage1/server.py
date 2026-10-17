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


# ---------- OT transformation (re-used from livedoc_client) ----------

from livedoc_client import (
    validate_op,
    apply_op,
    xf_edit,
    xf,
    transform_against_history,
)


# ---------- Data model ----------


def doc_name_to_file(doc_name: str) -> str:
    """Convert a doc name to a safe file name (hex of UTF-8 bytes)."""
    return doc_name.encode("utf-8").hex()


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
        Uses _rev_lengths for validation without full rebuild.
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
    print(f"[DEBUG _handle_client] starting, writer={writer}", flush=True)
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
            writer.write(json.dumps(error) + "\n")
            asyncio.create_task(self._safe_drain(writer))
        except (BrokenPipeError, ConnectionResetError):
            pass

    async def _handle_join(self, msg, writer, conn_state):
        """Handle a 'join' message."""
        doc_name = msg.get("doc", "")
        since = msg.get("since", None)

        print(f"[DEBUG _handle_join] doc_name={doc_name}, since={since}", flush=True)

        # Validate doc name
        if not doc_name or not isinstance(doc_name, str) or len(doc_name) > 100:
            self._send_error(writer, "bad_request", "Invalid doc name", msg_id=msg.get("id"))
            return

        print(f"[DEBUG _handle_join] doc_name valid", flush=True)

        # Validate since
        doc = self._get_doc(doc_name)
        current_rev = doc.current_rev()

        print(f"[DEBUG _handle_join] current_rev={current_rev}", flush=True)

        if since is not None:
            if not isinstance(since, int) or since < 0 or since > current_rev:
                self._send_error(writer, "bad_rev", f"Invalid since: {since}", msg_id=msg.get("id"))
                return

        print(f"[DEBUG _handle_join] since validation passed", flush=True)

        # Register connection for the doc
        conn_state["doc"] = doc_name
        conn_state["joined"] = True
        conn_state["client_name"] = msg.get("client", "")

        print(f"[DEBUG _handle_join] conn_state={conn_state}", flush=True)

        if since is None:
            # Reply with joined including current text
            text = doc.get_text()
            print(f"[DEBUG _handle_join] text={repr(text)}", flush=True)
            reply = {
                "type": "joined",
                "doc": doc_name,
                "rev": current_rev,
                "text": text,
            }
            print(f"[DEBUG _handle_join] reply={reply}", flush=True)
            writer.write(json.dumps(reply) + "\n")
            print(f"[DEBUG _handle_join] write done", flush=True)
            asyncio.create_task(self._safe_drain(writer))
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
            writer.write(json.dumps(reply) + "\n")
            asyncio.create_task(self._safe_drain(writer))

        # Store conn_state for this writer
        self._connections[writer] = conn_state
        print(f"[DEBUG _handle_join] stored conn_state, returning", flush=True)
        return

    async def _handle_op(self, msg, writer, conn_state):
        """Handle an 'op' message."""
        doc_name = msg.get("doc", "")
        rev = msg.get("rev", 0)
        client_id = msg.get("id", "")
        op = msg.get("op", [])

        print(f"[DEBUG _handle_op] doc_name={doc_name}, rev={rev}, client_id={client_id}", flush=True)

        # Check if this connection has joined the doc
        conn_info = self._connections.get(writer, {})
        print(f"[DEBUG _handle_op] conn_info={conn_info}", flush=True)
        
        if doc_name not in conn_info.get("doc", ""):
            print(f"[DEBUG _handle_op] not_joined", flush=True)
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

        # Validate operation against revision
        error, _ = doc.validate_op_against_rev(op, rev)
        print(f"[DEBUG _handle_op] validate_op_against_rev error={error}", flush=True)
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
                    w.write(json.dumps(broadcast_msg) + "\n")
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
        writer.write(json.dumps(ack_msg) + "\n")
        asyncio.create_task(self._safe_drain(writer))

    def _send_ack(self, writer, client_id, rev):
        """Send an ack message."""
        msg = {"type": "ack", "id": client_id, "rev": rev}
        writer.write(json.dumps(msg) + "\n")
        asyncio.create_task(self._safe_drain(writer))

    def _cleanup_connection(self, writer):
        """Clean up a connection on disconnect."""
        if writer in self._connections:
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


# ---------- Client library (already defined in livedoc_client.py) ----------


# For convenience, also expose Client class from this module
# (the main client implementation is in livedoc_client.py)


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