"""
Livedoc server – asyncio TCP server with OT and persistence.

Usage:
    python3 server.py --port 9000 --data-dir ./livedoc-data
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from livedoc_client import apply_op, validate_op, xf, xf_edit

# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------


def _encode_name(name: str) -> str:
    """Hex-encode a document name for use as a filename."""
    return name.encode("utf-8").hex()


class DocumentStore:
    """In-memory store for a single document, backed by a JSONL log."""

    def __init__(self, data_dir: Path, doc_name: str) -> None:
        self.doc_name = doc_name
        self.log_path = data_dir / f"{_encode_name(doc_name)}.log"
        self.text: str = ""
        self.rev: int = 0
        self.history: List[Dict[str, Any]] = []  # [{rev, client, id, op}, ...]
        self.dedupe: Dict[str, int] = {}  # (client, id) -> rev
        self.subscribers: Set[asyncio.Queue] = set()
        # Live cursors: client_name -> position (at current rev)
        self.cursors: Dict[str, int] = {}
        # Track which client_names have active connections subscribed to this doc
        self._client_connections: Dict[str, int] = {}  # client_name -> count of connections

        self._load()

    def _load(self) -> None:
        if not self.log_path.exists():
            return
        lines = self.log_path.read_text(encoding="utf-8").splitlines()
        good_lines: List[str] = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                # Validate structure
                if not all(k in entry for k in ("rev", "client", "id", "op")):
                    break
                if not isinstance(entry["op"], list):
                    break
                good_lines.append(line)
                self.history.append(entry)
                self.text = apply_op(self.text, entry["op"])
                self.rev = entry["rev"]
                key = json.dumps((entry["client"], entry["id"]), ensure_ascii=False)
                self.dedupe[key] = entry["rev"]
            except (json.JSONDecodeError, ValueError):
                # Truncated/invalid trailing line – stop here
                break
        # Truncate file to last good line
        if good_lines and good_lines != lines:
            self.log_path.write_text("\n".join(good_lines) + "\n", encoding="utf-8")

    def append(self, client: str, op_id: str, op: Any) -> int:
        """Append an operation to the log and commit. Returns the new rev."""
        self.rev += 1
        entry = {
            "rev": self.rev,
            "client": client,
            "id": op_id,
            "op": op,
        }
        self.history.append(entry)
        self.text = apply_op(self.text, op)
        key = json.dumps((client, op_id), ensure_ascii=False)
        self.dedupe[key] = self.rev
        # Append to log
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            f.flush()
        return self.rev

    def get_since(self, since: int) -> List[Dict[str, Any]]:
        """Return ops committed after revision `since`."""
        return [e for e in self.history if e["rev"] > since]

    def move_cursors_through_op(self, op: Any, owner: str) -> None:
        """Move all stored cursors through each edit of the operation."""
        new_cursors = {}
        for client_name, pos in self.cursors.items():
            new_pos = pos
            for edit in op:
                if "ins" in edit:
                    p = edit["ins"]
                    k = len(edit["text"])
                    # If insert is before cursor, or at cursor and owned by cursor's owner
                    if p < new_pos or (p == new_pos and client_name == owner):
                        new_pos += k
                elif "del" in edit:
                    p = edit["del"]
                    n = edit["len"]
                    if new_pos > p:
                        new_pos = max(p, new_pos - n)
            new_cursors[client_name] = new_pos
        self.cursors = new_cursors

    def get_snapshot(self, rev: int) -> Optional[str]:
        """Return the text at revision `rev`, or None if rev is invalid."""
        if rev < 0 or rev > self.rev:
            return None
        text = ""
        for h in self.history:
            if h["rev"] > rev:
                break
            text = apply_op(text, h["op"])
        return text

    def register_client(self, client_name: str) -> None:
        """Register a connection for a client name on this document."""
        count = self._client_connections.get(client_name, 0)
        self._client_connections[client_name] = count + 1

    def unregister_client(self, client_name: str) -> None:
        """Unregister a connection for a client name on this document.

        Returns the client name if this was the last connection and the client
        had a cursor (so a leave message should be sent), otherwise None.
        """
        count = self._client_connections.get(client_name, 0)
        if count <= 1:
            del self._client_connections[client_name]
            # Check if this client had a cursor
            if client_name in self.cursors:
                return client_name
            return None
        else:
            self._client_connections[client_name] = count - 1
            return None


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------


class Server:
    def __init__(self, port: int, data_dir: str) -> None:
        self.port = port
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.documents: Dict[str, DocumentStore] = {}
        # Per-connection state
        self._connections: Dict[asyncio.StreamWriter, Set[str]] = {}
        # Per-connection client names per doc (for leave messages)
        self._connection_clients: Dict[asyncio.StreamWriter, Dict[str, str]] = {}
        # Per-connection outbound queues
        self._outbound: Dict[asyncio.StreamWriter, asyncio.Queue] = {}

    def _get_doc(self, doc_name: str) -> DocumentStore:
        if doc_name not in self.documents:
            self.documents[doc_name] = DocumentStore(self.data_dir, doc_name)
        return self.documents[doc_name]

    def _send(self, writer: asyncio.StreamWriter, msg: Dict[str, Any]) -> None:
        data = json.dumps(msg, ensure_ascii=False).encode("utf-8") + b"\n"
        writer.write(data)

    def _error(self, writer: asyncio.StreamWriter, code: str, message: str,
                msg_id: Optional[str] = None) -> None:
        err: Dict[str, Any] = {"type": "error", "code": code, "message": message}
        if msg_id is not None:
            err["id"] = msg_id
        self._send(writer, err)

    def _is_joined(self, writer: asyncio.StreamWriter, doc_name: str) -> bool:
        return doc_name in self._connections.get(writer, set())

    def _broadcast(self, doc_name: str, writer_except: Optional[asyncio.StreamWriter],
                   msg: Dict[str, Any]) -> None:
        doc = self.documents.get(doc_name)
        if not doc:
            return
        for q in doc.subscribers:
            if q is not None and q is not self._outbound.get(writer_except):
                try:
                    q.put_nowait(msg)
                except Exception:
                    pass

    async def _handle_message(self, writer: asyncio.StreamWriter,
                              raw_line: str) -> None:
        msg_id = None
        try:
            msg = json.loads(raw_line)
        except (json.JSONDecodeError, ValueError):
            self._error(writer, "bad_json", "Invalid JSON")
            return

        if not isinstance(msg, dict):
            self._error(writer, "bad_json", "JSON is not an object")
            return

        msg_type = msg.get("type")
        msg_id = msg.get("id")

        if msg_type == "join":
            await self._handle_join(writer, msg)
        elif msg_type == "op":
            await self._handle_op(writer, msg)
        elif msg_type == "cursor":
            await self._handle_cursor(writer, msg)
        elif msg_type == "cursors":
            await self._handle_cursors_query(writer, msg)
        elif msg_type == "snapshot":
            await self._handle_snapshot(writer, msg)
        else:
            self._error(writer, "bad_request", f"Unknown type: {msg_type}", msg_id)

    async def _handle_join(self, writer: asyncio.StreamWriter,
                           msg: Dict[str, Any]) -> None:
        doc_name = msg.get("doc")
        client_name = msg.get("client")
        since = msg.get("since")

        # Validate
        if not isinstance(doc_name, str) or not doc_name or len(doc_name) > 100:
            self._error(writer, "bad_request",
                        "doc must be a non-empty string of at most 100 chars")
            return
        if not isinstance(client_name, str) or not client_name:
            self._error(writer, "bad_request",
                        "client must be a non-empty string")
            return
        if since is not None:
            if not isinstance(since, int) or isinstance(since, bool):
                self._error(writer, "bad_request",
                            "since must be an integer")
                return
            doc = self._get_doc(doc_name)
            if since < 0 or since > doc.rev:
                self._error(writer, "bad_rev",
                            f"since must be 0..{doc.rev}")
                return

        doc = self._get_doc(doc_name)

        # Subscribe
        if writer not in self._connections:
            self._connections[writer] = set()
            self._outbound[writer] = asyncio.Queue()
            self._connection_clients[writer] = {}
            asyncio.ensure_future(self._writer_loop(writer))
        self._connections[writer].add(doc_name)
        doc.subscribers.add(self._outbound[writer])
        # Track client name for this doc on this connection
        self._connection_clients[writer][doc_name] = client_name
        # Register this client connection
        doc.register_client(client_name)

        # Reply
        if since is None:
            reply = {
                "type": "joined",
                "doc": doc_name,
                "rev": doc.rev,
                "text": doc.text,
            }
        else:
            ops = doc.get_since(since)
            reply = {
                "type": "joined",
                "doc": doc_name,
                "rev": doc.rev,
                "ops": ops,
            }
        self._send(writer, reply)

    async def _handle_op(self, writer: asyncio.StreamWriter,
                         msg: Dict[str, Any]) -> None:
        doc_name = msg.get("doc")
        client_name = msg.get("client") or self._connection_clients.get(writer, {}).get(doc_name)  # PATCH: spec op/cursor messages carry no client
        base_rev = msg.get("rev")
        op_id = msg.get("id")
        op = msg.get("op")

        # Validate types
        if not isinstance(doc_name, str) or not doc_name:
            self._error(writer, "bad_request", "doc must be a non-empty string")
            return
        if not isinstance(client_name, str) or not client_name:
            self._error(writer, "bad_request", "client must be a non-empty string")
            return
        if not isinstance(base_rev, int) or isinstance(base_rev, bool):
            self._error(writer, "bad_rev", "rev must be an integer")
            return
        if not isinstance(op_id, str) or not op_id:
            self._error(writer, "bad_request", "id must be a non-empty string")
            return
        if not isinstance(op, list):
            self._error(writer, "bad_request", "op must be a list", op_id)
            return

        # not_joined
        if not self._is_joined(writer, doc_name):
            self._error(writer, "not_joined",
                        f"Not joined to document {doc_name}", op_id)
            return

        doc = self._get_doc(doc_name)

        # Duplicate check (exactly-once)
        key = json.dumps((client_name, op_id), ensure_ascii=False)
        if key in doc.dedupe:
            self._send(writer, {
                "type": "ack",
                "doc": doc_name,
                "id": op_id,
                "rev": doc.dedupe[key],
            })
            return

        # bad_rev
        if base_rev < 0 or base_rev > doc.rev:
            self._error(writer, "bad_rev",
                        f"rev must be 0..{doc.rev}", op_id)
            return

        # Validate op structure
        if not validate_op(op):
            self._error(writer, "bad_request", "Invalid operation structure", op_id)
            return

        # Validate op against text at base_rev
        # Reconstruct text at base_rev
        text_at_rev = ""
        for h in doc.history:
            if h["rev"] > base_rev:
                break
            text_at_rev = apply_op(text_at_rev, h["op"])

        # Check out_of_range
        try:
            apply_op(text_at_rev, op)
        except ValueError:
            self._error(writer, "out_of_range",
                        "Operation is not valid against text at revision "
                        f"{base_rev}", op_id)
            return

        # Transform against operations committed after base_rev
        current_op = list(op)
        after_ops = [h for h in doc.history if h["rev"] > base_rev]
        for h in after_ops:
            current_op, _ = xf(current_op, h["op"])

        # Commit
        new_rev = doc.append(client_name, op_id, current_op)

        # Ack to sender (send through queue to maintain ordering)
        outbound = self._outbound.get(writer)
        if outbound is not None:
            await outbound.put({
                "type": "ack",
                "doc": doc_name,
                "id": op_id,
                "rev": new_rev,
            })
        else:
            self._send(writer, {
                "type": "ack",
                "doc": doc_name,
                "id": op_id,
                "rev": new_rev,
            })

        # Broadcast to other subscribers
        broadcast_msg = {
            "type": "op",
            "doc": doc_name,
            "rev": new_rev,
            "client": client_name,
            "id": op_id,
            "op": current_op,
        }
        self._broadcast(doc_name, writer, broadcast_msg)

        # Move all cursors through the committed operation
        doc.move_cursors_through_op(current_op, client_name)
        # Broadcast cursor updates to other subscribers
        for cname, cpos in doc.cursors.items():
            if cname != client_name:
                cursor_update = {
                    "type": "cursor",
                    "doc": doc_name,
                    "client": cname,
                    "rev": new_rev,
                    "pos": cpos,
                }
                self._broadcast(doc_name, writer, cursor_update)

    async def _handle_cursor(self, writer: asyncio.StreamWriter,
                             msg: Dict[str, Any]) -> None:
        """Handle cursor set request."""
        doc_name = msg.get("doc")
        client_name = msg.get("client") or self._connection_clients.get(writer, {}).get(doc_name)  # PATCH: spec op/cursor messages carry no client
        rev = msg.get("rev")
        pos = msg.get("pos")

        # Validate
        if not isinstance(doc_name, str) or not doc_name:
            self._error(writer, "bad_request", "doc must be a non-empty string")
            return
        if not isinstance(client_name, str) or not client_name:
            self._error(writer, "bad_request", "client must be a non-empty string")
            return
        if not isinstance(rev, int) or isinstance(rev, bool):
            self._error(writer, "bad_request", "rev must be an integer")
            return
        if not isinstance(pos, int) or isinstance(pos, bool):
            self._error(writer, "bad_request", "pos must be an integer")
            return

        # not_joined
        if not self._is_joined(writer, doc_name):
            self._error(writer, "not_joined",
                        f"Not joined to document {doc_name}", msg.get("id"))
            return

        doc = self._get_doc(doc_name)

        # bad_rev
        if rev < 0 or rev > doc.rev:
            self._error(writer, "bad_rev",
                        f"rev must be 0..{doc.rev}", msg.get("id"))
            return

        # out_of_range: validate pos against text at rev
        text_at_rev = ""
        for h in doc.history:
            if h["rev"] > rev:
                break
            text_at_rev = apply_op(text_at_rev, h["op"])
        if pos < 0 or pos > len(text_at_rev):
            self._error(writer, "out_of_range",
                        f"position {pos} out of range [0, {len(text_at_rev)}]",
                        msg.get("id"))
            return

        # Move cursor to current rev
        # First, move pos through all ops from rev to doc.rev
        current_pos = pos
        for h in doc.history:
            if h["rev"] <= rev:
                continue
            for edit in h["op"]:
                if "ins" in edit:
                    p = edit["ins"]
                    k = len(edit["text"])
                    if p < current_pos or (p == current_pos and h["client"] == client_name):
                        current_pos += k
                elif "del" in edit:
                    p = edit["del"]
                    n = edit["len"]
                    if current_pos > p:
                        current_pos = max(p, current_pos - n)

        # Store cursor
        doc.cursors[client_name] = current_pos

        # Broadcast cursor position to other subscribers
        for q in doc.subscribers:
            if q is not None and q is not self._outbound.get(writer):
                try:
                    q.put_nowait({
                        "type": "cursor",
                        "doc": doc_name,
                        "client": client_name,
                        "rev": doc.rev,
                        "pos": current_pos,
                    })
                except Exception:
                    pass

    async def _handle_cursors_query(self, writer: asyncio.StreamWriter,
                                     msg: Dict[str, Any]) -> None:
        """Handle cursors query request."""
        doc_name = msg.get("doc")

        if not isinstance(doc_name, str) or not doc_name:
            self._error(writer, "bad_request", "doc must be a non-empty string")
            return

        if not self._is_joined(writer, doc_name):
            self._error(writer, "not_joined",
                        f"Not joined to document {doc_name}", msg.get("id"))
            return

        doc = self._get_doc(doc_name)
        # Return cursors excluding the querying client
        cursors = {k: v for k, v in doc.cursors.items() if k != msg.get("client")}
        self._send(writer, {
            "type": "cursors",
            "doc": doc_name,
            "rev": doc.rev,
            "cursors": cursors,
        })

    async def _handle_snapshot(self, writer: asyncio.StreamWriter,
                                msg: Dict[str, Any]) -> None:
        """Handle snapshot request (no join required)."""
        doc_name = msg.get("doc")
        rev = msg.get("rev")

        # Validate
        if not isinstance(doc_name, str) or not doc_name:
            self._error(writer, "bad_request", "doc must be a non-empty string")
            return
        if not isinstance(rev, int) or isinstance(rev, bool):
            self._error(writer, "bad_request", "rev must be an integer")
            return

        doc = self._get_doc(doc_name)

        # not_found
        if doc.rev == 0 and not doc.history:
            self._error(writer, "not_found",
                        f"Document {doc_name} does not exist", msg.get("id"))
            return

        # bad_rev
        if rev < 0 or rev > doc.rev:
            self._error(writer, "bad_rev",
                        f"rev must be 0..{doc.rev}", msg.get("id"))
            return

        text = doc.get_snapshot(rev)
        self._send(writer, {
            "type": "snapshot",
            "doc": doc_name,
            "rev": rev,
            "text": text,
        })

    async def _writer_loop(self, writer: asyncio.StreamWriter) -> None:
        q = self._outbound.get(writer)
        if q is None:
            return
        try:
            while True:
                msg = await q.get()
                self._send(writer, msg)
                await writer.drain()
        except Exception:
            pass

    async def _handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        # Start writer loop for every connection so error messages get sent
        if writer not in self._connections:
            self._connections[writer] = set()
            self._outbound[writer] = asyncio.Queue()
            self._connection_clients[writer] = {}
            asyncio.ensure_future(self._writer_loop(writer))
        buf = b""
        try:
            while True:
                data = await reader.read(65536)
                if not data:
                    break
                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if len(line) > 1_000_000:
                        # Too long – treat as bad
                        self._error(writer, "bad_json", "Line too long")
                        continue
                    try:
                        raw = line.decode("utf-8")
                    except UnicodeDecodeError:
                        self._error(writer, "bad_json", "Invalid UTF-8")
                        continue
                    await self._handle_message(writer, raw)
        except asyncio.CancelledError:
            pass
        except Exception:
            pass
        finally:
            # Clean up subscriptions and send leave messages
            if writer in self._connections:
                client_map = self._connection_clients.get(writer, {})
                for doc_name in self._connections[writer]:
                    doc = self.documents.get(doc_name)
                    if doc:
                        doc.subscribers.discard(self._outbound.get(writer))
                        # Check if this was the last connection for this client
                        cname = client_map.get(doc_name)
                        if cname:
                            leave_client = doc.unregister_client(cname)
                            if leave_client:
                                # Send leave message to all remaining subscribers
                                leave_msg = {
                                    "type": "leave",
                                    "doc": doc_name,
                                    "client": leave_client,
                                }
                                for q in doc.subscribers:
                                    if q is not None:
                                        try:
                                            q.put_nowait(leave_msg)
                                        except Exception:
                                            pass
                del self._connections[writer]
                if writer in self._connection_clients:
                    del self._connection_clients[writer]
                if writer in self._outbound:
                    del self._outbound[writer]
            # Flush any remaining data
            try:
                writer.write(b"")
                await writer.drain()
            except Exception:
                pass
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def run(self) -> None:
        self.server = await asyncio.start_server(
            self._handle_connection, "127.0.0.1", self.port
        )
        async with self.server:
            await self.server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description="Livedoc server")
    parser.add_argument("--port", type=int, default=9000)
    parser.add_argument("--data-dir", type=str, default="./livedoc-data")
    args = parser.parse_args()
    server = Server(args.port, args.data_dir)
    asyncio.run(server.run())


if __name__ == "__main__":
    main()
