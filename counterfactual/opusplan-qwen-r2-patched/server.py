"""
Livedoc server: real-time collaborative plain-text editing service.

Usage:
    python3 server.py --port PORT --data-dir DIR

Listens on 127.0.0.1:PORT. Persists documents under DIR.
"""

import argparse
import asyncio
import json
import os
import sys
import traceback

# Import OT functions from client library
from livedoc_client import apply_op, transform_against_history, validate_op


class Document:
    """In-memory document state with persistence via append-only JSONL log."""

    def __init__(self, name: str, data_dir: str):
        self.name = name
        self.text = ""
        self.entries: list[dict] = []  # committed entries {rev, client, id, op}
        self.rev = 0
        self.rev_lengths: list[int] = [0]  # rev_lengths[r] = text length at revision r
        self.dedup: dict[tuple[str, str], int] = {}  # (client, id) -> rev
        self._log_path = os.path.join(data_dir, self._safe_name(name))

    @staticmethod
    def _safe_name(name: str) -> str:
        """Convert document name to a safe filename (hex of UTF-8)."""
        return name.encode("utf-8").hex()

    def _ensure_log(self):
        """Create log file if it doesn't exist."""
        if not os.path.exists(self._log_path):
            with open(self._log_path, "w", encoding="utf-8") as f:
                pass  # Empty file, rev 0

    def commit(self, client: str, op_id: str, op: list[dict]) -> int:
        """Commit an operation. Returns the new revision number."""
        self.rev += 1
        entry = {
            "rev": self.rev,
            "client": client,
            "id": op_id,
            "op": op
        }
        self.entries.append(entry)
        self.rev_lengths.append(len(self.text))
        self.dedup[(client, op_id)] = self.rev

        # Persist before sending ack/broadcasts
        self._ensure_log()
        with open(self._log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            f.flush()

        return self.rev

    @classmethod
    def load(cls, name: str, data_dir: str) -> "Document":
        """Load a document from its log file."""
        doc = cls(name, data_dir)
        log_path = doc._log_path
        if not os.path.exists(log_path):
            return doc

        with open(log_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    # Truncated/invalid trailing line from SIGKILL - truncate it
                    break
                if not isinstance(entry, dict) or "rev" not in entry:
                    break
                doc.entries.append(entry)
                doc.rev = entry["rev"]
                doc.dedup[(entry["client"], entry["id"])] = entry["rev"]

        # Rebuild text by replaying all operations
        text = ""
        for entry in doc.entries:
            text = apply_op(text, entry["op"])
        doc.text = text
        doc.rev_lengths = list(range(doc.rev + 1))

        return doc


class LivedocServer:
    """Livedoc server using asyncio for single-threaded event loop ordering."""

    def __init__(self, port: int, data_dir: str):
        self.port = port
        self.data_dir = data_dir
        os.makedirs(data_dir, exist_ok=True)
        self.documents: dict[str, Document] = {}
        # doc_name -> set of (writer_transport, writer_protocol) or just set of writers
        self.doc_connections: dict[str, set] = {}
        # connection -> doc_name (for cleanup)
        self.conn_doc: dict = {}

    def _get_doc(self, name: str) -> Document:
        """Get or create a document."""
        if name not in self.documents:
            self.documents[name] = Document(name, self.data_dir)
        return self.documents[name]

    def _broadcast(self, doc_name: str, msg: dict, exclude_writer=None):
        """Broadcast a message to all connections joined to a document."""
        if doc_name not in self.doc_connections:
            return
        data = (json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8")
        for writer in list(self.doc_connections[doc_name]):
            if writer is exclude_writer:
                continue
            try:
                writer.write(data)
            except Exception:
                pass

    def _send(self, writer, msg: dict):
        """Send a message to a single connection."""
        data = (json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8")
        writer.write(data)

    def _handle_message(self, message: dict, writer, protocol):
        """Handle a single parsed message."""
        msg_type = message.get("type")

        if msg_type == "join":
            self._handle_join(message, writer, protocol)
        elif msg_type == "op":
            self._handle_op(message, writer, protocol)
        else:
            self._send_error(writer, message, "bad_request",
                             f"Unknown message type: {msg_type}")

    def _handle_join(self, message: dict, writer, protocol):
        """Handle a join message."""
        doc = message.get("doc")
        client = message.get("client")

        # Validate fields
        if not isinstance(doc, str) or not doc or len(doc) > 100:
            self._send_error(writer, message, "bad_request",
                             "doc must be a non-empty string of at most 100 characters")
            return
        if not isinstance(client, str) or not client:
            self._send_error(writer, message, "bad_request",
                             "client must be a non-empty string")
            return

        since = message.get("since")
        if since is not None:
            if not isinstance(since, int) or isinstance(since, bool) or since < 0:
                self._send_error(writer, message, "bad_rev",
                                 "since must be a non-negative integer")
                return
            current_rev = self._get_doc(doc).rev
            if since > current_rev:
                self._send_error(writer, message, "bad_rev",
                                 f"since {since} exceeds current revision {current_rev}")
                return

        doc_obj = self._get_doc(doc)

        # Register connection for this document
        if doc not in self.doc_connections:
            self.doc_connections[doc] = set()
        self.doc_connections[doc].add(writer)
        self.conn_doc[writer] = doc

        # Reply with joined
        reply = {"type": "joined", "doc": doc, "rev": doc_obj.rev}

        if since is not None and since < doc_obj.rev:
            # Send ops since revision
            ops = []
            for entry in doc_obj.entries:
                if entry["rev"] > since:
                    ops.append({
                        "rev": entry["rev"],
                        "client": entry["client"],
                        "id": entry["id"],
                        "op": entry["op"]
                    })
            reply["ops"] = ops
        else:
            reply["text"] = doc_obj.text

        self._send(writer, reply)

    def _handle_op(self, message: dict, writer, protocol):
        """Handle an operation message. Strict ordering of checks."""
        doc = message.get("doc")
        client = message.get("client")
        rev = message.get("rev")
        op_id = message.get("id")
        op = message.get("op")

        # 1. bad_request: validate structure
        if not isinstance(doc, str) or not doc or len(doc) > 100:
            self._send_error(writer, message, "bad_request",
                             "doc must be a non-empty string of at most 100 characters")
            return
        if not isinstance(rev, int) or isinstance(rev, bool):
            self._send_error(writer, message, "bad_request",
                             "rev must be an integer")
            return
        if not isinstance(op_id, str):
            self._send_error(writer, message, "bad_request",
                             "id must be a string")
            return
        if op is None or not isinstance(op, list):
            self._send_error(writer, message, "bad_request",
                             "op must be a list")
            return

        # Validate op structure
        doc_obj = self._get_doc(doc)
        err_code, err_detail, _ = validate_op(op, doc_obj.rev_lengths[rev] if rev < len(doc_obj.rev_lengths) else len(doc_obj.text))
        if err_code:
            self._send_error(writer, message, err_code, err_detail)
            return

        # 2. not_joined
        if self.conn_doc.get(writer) != doc:
            self._send_error(writer, message, "not_joined",
                             f"not joined to document '{doc}'")
            return

        # 3. Duplicate check (client, id)
        if (client, op_id) in doc_obj.dedup:
            original_rev = doc_obj.dedup[(client, op_id)]
            self._send(writer, {
                "type": "ack",
                "doc": doc,
                "id": op_id,
                "rev": original_rev
            })
            return

        # 4. bad_rev
        if rev < 0 or rev > doc_obj.rev:
            self._send_error(writer, message, "bad_rev",
                             f"rev {rev} out of range [0, {doc_obj.rev}]")
            return

        # 5. out_of_range: validate against text at revision R
        # We need the text length at revision R
        if rev < len(doc_obj.rev_lengths):
            rev_length = doc_obj.rev_lengths[rev]
        else:
            # Rebuild text at revision R
            text_at_rev = ""
            for entry in doc_obj.entries[:rev]:
                text_at_rev = apply_op(text_at_rev, entry["op"])
            rev_length = len(text_at_rev)
            doc_obj.rev_lengths.append(rev_length)

        err_code, err_detail, _ = validate_op(op, rev_length)
        if err_code:
            self._send_error(writer, message, err_code, err_detail)
            return

        # 6. Transform against history
        # Get ops committed after revision R
        future_ops = [entry["op"] for entry in doc_obj.entries if entry["rev"] > rev]
        transformed_op = transform_against_history(op, future_ops)

        # 7. Apply
        doc_obj.text = apply_op(doc_obj.text, transformed_op)

        # 8. Commit and persist
        new_rev = doc_obj.commit(client, op_id, transformed_op)

        # 9. Broadcast to other connections
        self._broadcast(doc, {
            "type": "op",
            "doc": doc,
            "rev": new_rev,
            "client": client,
            "id": op_id,
            "op": transformed_op
        }, exclude_writer=writer)

        # 10. Ack sender
        self._send(writer, {
            "type": "ack",
            "doc": doc,
            "id": op_id,
            "rev": new_rev
        })

    def _send_error(self, writer, message: dict, code: str, message_text: str):
        """Send an error response."""
        err = {"type": "error", "code": code, "message": message_text}
        if "id" in message:
            err["id"] = message["id"]
        self._send(writer, err)

    async def handle_connection(self, reader: asyncio.StreamReader,
                                 writer: asyncio.StreamWriter):
        """Handle a single client connection."""
        buf = b""
        try:
            while True:
                try:
                    data = await reader.read(4096)
                except (ConnectionResetError, BrokenPipeError):
                    break
                if not data:
                    break

                buf += data

                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if not line:
                        continue

                    # Parse JSON
                    try:
                        message = json.loads(line.decode("utf-8"))
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        self._send_error(writer, {}, "bad_json",
                                         "Invalid JSON")
                        continue

                    if not isinstance(message, dict):
                        self._send_error(writer, message, "bad_json",
                                         "JSON must be an object")
                        continue

                    self._handle_message(message, writer, None)
        except Exception:
            traceback.print_exc()
        finally:
            # Cleanup
            doc_name = self.conn_doc.pop(writer, None)
            if doc_name and doc_name in self.doc_connections:
                self.doc_connections[doc_name].discard(writer)
                if not self.doc_connections[doc_name]:
                    del self.doc_connections[doc_name]
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def run(self):
        """Start the server."""
        server = await asyncio.start_server(
            self.handle_connection, "127.0.0.1", self.port
        )
        print(f"Livedoc server listening on 127.0.0.1:{self.port}", flush=True)
        async with server:
            await server.serve_forever()


def main():
    parser = argparse.ArgumentParser(description="Livedoc server")
    parser.add_argument("--port", type=int, default=8765, help="Port to listen on")
    parser.add_argument("--data-dir", type=str, default="./livedoc_data",
                        help="Directory for persistent data")
    args = parser.parse_args()

    server = LivedocServer(args.port, args.data_dir)
    asyncio.run(server.run())


if __name__ == "__main__":
    main()
