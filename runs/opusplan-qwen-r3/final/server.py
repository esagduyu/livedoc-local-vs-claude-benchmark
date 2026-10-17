"""
Livedoc collaborative editing server.

Started as ``python3 server.py --port PORT --data-dir DIR``.
Listens on 127.0.0.1:PORT. Keeps everything under DIR.
Standard library only.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from livedoc_ot import (
    apply_op,
    transform_op_against_history,
    _validate_op,
)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

class Document:
    """In-memory state for one document."""

    def __init__(self) -> None:
        self.rev: int = 0  # number of committed operations
        self.text: str = ""
        self.rev_lengths: list[int] = [0]  # rev_lengths[r] = text length at revision r
        # history: list of (rev, client, id, op) in commit order
        self.history: list[tuple[int, str, str, list[dict]]] = []
        # exactly-once: (client, id) -> rev
        self.seen: dict[tuple[str, str], int] = {}
        # Cursors: client_name -> position (in current text)
        self.cursors: dict[str, int] = {}
        # Connections joined to this document: connection_id -> client_name
        self.connections: dict[int, str] = {}


# ---------------------------------------------------------------------------
# Persistence (JSONL log per document)
# ---------------------------------------------------------------------------

def _doc_filename(doc_name: str) -> str:
    """Map a document name to a safe filename (hex of UTF-8 bytes)."""
    return doc_name.encode("utf-8").hex()


def _load_document(data_dir: Path, doc_name: str) -> Document:
    """Rebuild a document from its JSONL log, or return a fresh one."""
    doc = Document()
    log_path = data_dir / (_doc_filename(doc_name) + ".jsonl")
    if not log_path.exists():
        return doc
    with open(log_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                # Truncated / crash mid-write: truncate file back
                break
            rev = entry["rev"]
            client = entry["client"]
            eid = entry["id"]
            op = entry["op"]
            # Validate and apply
            new_len = _validate_op(op, doc.rev_lengths[-1])
            doc.text = apply_op(doc.text, op)
            doc.rev += 1
            doc.rev_lengths.append(new_len)
            doc.history.append((rev, client, eid, op))
            doc.seen[(client, eid)] = rev
        else:
            # All lines parsed successfully — nothing to truncate
            return doc
    # Truncate the file back to the last complete line
    # Re-read and write only valid lines
    valid_lines: list[str] = []
    with open(log_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line:
                continue
            try:
                json.loads(line)
                valid_lines.append(line)
            except json.JSONDecodeError:
                break
    with open(log_path, "w", encoding="utf-8") as f:
        for vl in valid_lines:
            f.write(vl + "\n")
    return doc


def _persist_op(data_dir: Path, doc_name: str, entry: dict) -> None:
    """Append one operation entry to the document's JSONL log."""
    log_path = data_dir / (_doc_filename(doc_name) + ".jsonl")
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# Cursor movement
# ---------------------------------------------------------------------------

def move_cursor_through_op(cursor_pos: int, op: list[dict], owner: str | None = None) -> int:
    """Move a cursor position through an operation.

    Args:
        cursor_pos: Current cursor position.
        op: Operation to move through.
        owner: Client name of the cursor's owner (None = not the owner).

    Returns the new cursor position after applying the op.
    """
    pos = cursor_pos
    for edit in op:
        if "ins" in edit:
            p = edit["ins"]
            k = len(edit["text"])
            # Insert: if p < c, or p == c and this op was made by the cursor's owner
            if p < pos or (p == pos and owner is not None):
                pos += k
        else:  # del
            p = edit["del"]
            n = edit["len"]
            if pos > p:
                pos = max(p, pos - n)
    return pos


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

class Server:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        # Per-document state
        self.documents: dict[str, Document] = {}
        # Per-connection state: connection_id -> set of doc names
        self.connection_docs: dict[int, set[str]] = {}
        # connection_id -> client name
        self.connection_clients: dict[int, str] = {}
        # connection_id -> writer (asyncio.StreamWriter)
        self.connections: dict[int, asyncio.StreamWriter] = {}
        self._next_conn_id: int = 0

    # -- helpers --

    def _get_doc(self, doc_name: str) -> Document:
        if doc_name not in self.documents:
            self.documents[doc_name] = _load_document(self.data_dir, doc_name)
        return self.documents[doc_name]

    async def _send(self, writer: asyncio.StreamWriter, msg: dict) -> None:
        writer.write((json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8"))
        await writer.drain()

    async def _broadcast(self, doc_name: str, msg: dict, exclude_conn: int | None = None) -> None:
        """Broadcast a message to all connections joined to doc_name."""
        target_conns = []
        for cid, docs in self.connection_docs.items():
            if cid != exclude_conn and doc_name in docs:
                target_conns.append(cid)
        for cid in target_conns:
            writer = self.connections.get(cid)
            if writer is not None and not writer.is_closing():
                await self._send(writer, msg)

    # -- protocol handlers --

    async def _handle_join(self, writer: asyncio.StreamWriter, conn_id: int, msg: dict) -> None:
        doc_name = msg.get("doc")
        client = msg.get("client")
        if not isinstance(doc_name, str) or not doc_name:
            await self._send(writer, {"type": "error", "code": "bad_request", "message": "doc must be a non-empty string"})
            return
        if len(doc_name) > 100:
            await self._send(writer, {"type": "error", "code": "bad_request", "message": "doc name too long"})
            return
        if not isinstance(client, str) or not client:
            await self._send(writer, {"type": "error", "code": "bad_request", "message": "client must be a non-empty string"})
            return

        doc = self._get_doc(doc_name)
        # Register connection for this document
        if conn_id not in self.connection_docs:
            self.connection_docs[conn_id] = set()
        self.connection_docs[conn_id].add(doc_name)
        self.connection_clients[conn_id] = client
        doc.connections[conn_id] = client

        since = msg.get("since")
        if since is not None:
            if not isinstance(since, int) or isinstance(since, bool) or since < 0 or since > doc.rev:
                await self._send(writer, {"type": "error", "code": "bad_rev", "message": "since out of range"})
                return
            # Send ops since R
            ops = []
            for rev, client_name, eid, op in doc.history:
                if rev > since:
                    ops.append({"rev": rev, "client": client_name, "id": eid, "op": op})
            await self._send(writer, {
                "type": "joined",
                "doc": doc_name,
                "rev": doc.rev,
                "ops": ops,
            })
        else:
            await self._send(writer, {
                "type": "joined",
                "doc": doc_name,
                "rev": doc.rev,
                "text": doc.text,
            })

    async def _handle_op(self, writer: asyncio.StreamWriter, conn_id: int, msg: dict) -> None:
        doc_name = msg.get("doc")
        client = self.connection_clients.get(conn_id, "")
        rev = msg.get("rev")
        eid = msg.get("id")
        op = msg.get("op")

        # Validate fields
        if not isinstance(doc_name, str) or not doc_name:
            await self._send(writer, {"type": "error", "code": "bad_request", "message": "doc"})
            return
        if not isinstance(client, str) or not client:
            await self._send(writer, {"type": "error", "code": "bad_request", "message": "client"})
            return
        if not isinstance(rev, int) or isinstance(rev, bool):
            await self._send(writer, {"type": "error", "code": "bad_request", "message": "rev"})
            return
        if not isinstance(eid, str):
            await self._send(writer, {"type": "error", "code": "bad_request", "message": "id"})
            return
        if not isinstance(op, list):
            await self._send(writer, {"type": "error", "code": "bad_request", "message": "op"})
            return

        doc = self._get_doc(doc_name)

        # Check joined
        if conn_id not in self.connection_docs or doc_name not in self.connection_docs[conn_id]:
            await self._send(writer, {"type": "error", "code": "not_joined", "message": f"not joined to {doc_name}"})
            return

        # Validate rev
        if rev < 0 or rev > doc.rev:
            await self._send(writer, {"type": "error", "code": "bad_rev", "message": f"rev out of range"})
            return

        # Validate op against text length at rev R
        try:
            _validate_op(op, doc.rev_lengths[rev])
        except ValueError:
            await self._send(writer, {"type": "error", "code": "out_of_range", "message": str(op)})
            return

        # Exactly-once check
        key = (client, eid)
        if key in doc.seen:
            orig_rev = doc.seen[key]
            await self._send(writer, {"type": "ack", "doc": doc_name, "id": eid, "rev": orig_rev})
            return

        # Transform against history
        ops_since = doc.history[rev:]  # ops committed after rev R
        transformed_op, broadcast_ops = transform_op_against_history(op, ops_since)

        # Apply the transformed op
        new_rev = doc.rev + 1
        doc.text = apply_op(doc.text, transformed_op)
        doc.rev = new_rev
        doc.rev_lengths.append(len(doc.text))
        doc.history.append((new_rev, client, eid, transformed_op))
        doc.seen[key] = new_rev

        # Persist
        entry = {"rev": new_rev, "client": client, "id": eid, "op": transformed_op}
        _persist_op(self.data_dir, doc_name, entry)

        # Move all cursors through the committed op
        for c_name in list(doc.cursors.keys()):
            doc.cursors[c_name] = move_cursor_through_op(
                doc.cursors[c_name], transformed_op,
                owner=client if c_name == client else None,
            )

        # Ack to sender
        await self._send(writer, {"type": "ack", "doc": doc_name, "id": eid, "rev": new_rev})

        # Broadcast the op to other connections
        await self._broadcast(doc_name, {
            "type": "op",
            "doc": doc_name,
            "rev": new_rev,
            "client": client,
            "id": eid,
            "op": transformed_op,
        }, exclude_conn=conn_id)

    async def _handle_cursor(self, writer: asyncio.StreamWriter, conn_id: int, msg: dict) -> None:
        """Handle a cursor set request."""
        doc_name = msg.get("doc")
        rev = msg.get("rev")
        pos = msg.get("pos")

        # Validate fields
        if not isinstance(doc_name, str) or not doc_name:
            await self._send(writer, {"type": "error", "code": "bad_request", "message": "doc"})
            return
        if not isinstance(rev, int) or isinstance(rev, bool):
            await self._send(writer, {"type": "error", "code": "bad_request", "message": "rev"})
            return
        if not isinstance(pos, int) or isinstance(pos, bool):
            await self._send(writer, {"type": "error", "code": "bad_request", "message": "pos"})
            return

        client = self.connection_clients.get(conn_id, "")

        # Check joined
        if conn_id not in self.connection_docs or doc_name not in self.connection_docs[conn_id]:
            await self._send(writer, {"type": "error", "code": "not_joined", "message": f"not joined to {doc_name}"})
            return

        doc = self._get_doc(doc_name)

        # Validate rev
        if rev < 0 or rev > doc.rev:
            await self._send(writer, {"type": "error", "code": "bad_rev", "message": "rev out of range"})
            return

        # Validate pos against text length at rev R
        if pos < 0 or pos > doc.rev_lengths[rev]:
            await self._send(writer, {"type": "error", "code": "out_of_range", "message": f"pos {pos} out of range"})
            return

        # Move cursor from rev R to current revision N
        new_rev = doc.rev
        cursor_pos = pos
        # Move through all ops committed after rev R
        for _r, _c, _e, o in doc.history[rev:]:
            cursor_pos = move_cursor_through_op(cursor_pos, o, owner=None)

        # Store the cursor
        doc.cursors[client] = cursor_pos

        # Broadcast to other connections (no ack on success)
        await self._broadcast(doc_name, {
            "type": "cursor",
            "doc": doc_name,
            "client": client,
            "rev": new_rev,
            "pos": cursor_pos,
        }, exclude_conn=conn_id)

    async def _handle_cursors(self, writer: asyncio.StreamWriter, conn_id: int, msg: dict) -> None:
        """Handle a cursors query request."""
        doc_name = msg.get("doc")

        if not isinstance(doc_name, str) or not doc_name:
            await self._send(writer, {"type": "error", "code": "bad_request", "message": "doc"})
            return

        # Check joined
        if conn_id not in self.connection_docs or doc_name not in self.connection_docs[conn_id]:
            await self._send(writer, {"type": "error", "code": "not_joined", "message": f"not joined to {doc_name}"})
            return

        doc = self._get_doc(doc_name)

        await self._send(writer, {
            "type": "cursors",
            "doc": doc_name,
            "rev": doc.rev,
            "cursors": dict(doc.cursors),
        })

    async def _handle_snapshot(self, writer: asyncio.StreamWriter, msg: dict) -> None:
        """Handle a snapshot request (does not require joining)."""
        doc_name = msg.get("doc")
        rev = msg.get("rev")

        if not isinstance(doc_name, str) or not doc_name:
            await self._send(writer, {"type": "error", "code": "bad_request", "message": "doc"})
            return
        if not isinstance(rev, int) or isinstance(rev, bool):
            await self._send(writer, {"type": "error", "code": "bad_request", "message": "rev"})
            return

        # Check if document exists
        doc = self._get_doc(doc_name)
        if doc.rev == 0 and not doc.history:
            # Document was never used (no ops committed)
            # But it exists because _get_doc created it
            # Actually, we should only consider it to exist if it has been loaded or has ops
            # A document that was never accessed doesn't exist
            # But _get_doc creates it on first access. We need to distinguish.
            # If the log file doesn't exist and no ops have been committed, it doesn't exist.
            log_path = self.data_dir / (_doc_filename(doc_name) + ".jsonl")
            if not log_path.exists():
                await self._send(writer, {"type": "error", "code": "not_found", "message": f"document {doc_name} not found"})
                return

        if rev < 0 or rev > doc.rev:
            await self._send(writer, {"type": "error", "code": "bad_rev", "message": "rev out of range"})
            return

        # Rebuild text at revision R by applying ops 1..R
        text = ""
        for r, _c, _e, o in doc.history:
            if r > rev:
                break
            text = apply_op(text, o)

        await self._send(writer, {
            "type": "snapshot",
            "doc": doc_name,
            "rev": rev,
            "text": text,
        })

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        conn_id = self._next_conn_id
        self._next_conn_id += 1
        self.connections[conn_id] = writer
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                text = line.decode("utf-8").strip()
                if not text:
                    continue
                # Parse JSON
                try:
                    msg = json.loads(text)
                except (json.JSONDecodeError, ValueError):
                    await self._send(writer, {"type": "error", "code": "bad_json", "message": "invalid JSON"})
                    continue
                if not isinstance(msg, dict):
                    await self._send(writer, {"type": "error", "code": "bad_json", "message": "not an object"})
                    continue

                msg_type = msg.get("type")
                if msg_type == "join":
                    await self._handle_join(writer, conn_id, msg)
                elif msg_type == "op":
                    await self._handle_op(writer, conn_id, msg)
                elif msg_type == "cursor":
                    await self._handle_cursor(writer, conn_id, msg)
                elif msg_type == "cursors":
                    await self._handle_cursors(writer, conn_id, msg)
                elif msg_type == "snapshot":
                    await self._handle_snapshot(writer, msg)
                else:
                    await self._send(writer, {"type": "error", "code": "bad_request", "message": f"unknown type: {msg_type}"})
        except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
            pass
        finally:
            # Clean up: check for leave messages
            await self._cleanup_connection(conn_id)

    async def _cleanup_connection(self, conn_id: int) -> None:
        """Clean up connection state and send leave messages."""
        client = self.connection_clients.pop(conn_id, None)
        docs = self.connection_docs.pop(conn_id, set())

        for doc_name in docs:
            doc = self.documents.get(doc_name)
            if doc is None:
                continue
            # Remove this connection from the document
            doc.connections.pop(conn_id, None)

            # Check if any other connection uses the same client name on this doc
            if client and not any(v == client for v in doc.connections.values()):
                # Last connection of this client name is gone
                if client in doc.cursors:
                    del doc.cursors[client]
                    # Send leave to remaining connections
                    for cid in doc.connections:
                        writer = self.connections.get(cid)
                        if writer is not None and not writer.is_closing():
                            await self._send(writer, {
                                "type": "leave",
                                "doc": doc_name,
                                "client": client,
                            })

    # -- Main --

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        conn_id = self._next_conn_id
        self._next_conn_id += 1
        self.connections[conn_id] = writer
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                text = line.decode("utf-8").strip()
                if not text:
                    continue
                try:
                    msg = json.loads(text)
                except (json.JSONDecodeError, ValueError):
                    await self._send(writer, {"type": "error", "code": "bad_json", "message": "invalid JSON"})
                    continue
                if not isinstance(msg, dict):
                    await self._send(writer, {"type": "error", "code": "bad_json", "message": "not an object"})
                    continue

                msg_type = msg.get("type")
                if msg_type == "join":
                    await self._handle_join(writer, conn_id, msg)
                elif msg_type == "op":
                    await self._handle_op(writer, conn_id, msg)
                elif msg_type == "cursor":
                    await self._handle_cursor(writer, conn_id, msg)
                elif msg_type == "cursors":
                    await self._handle_cursors(writer, conn_id, msg)
                elif msg_type == "snapshot":
                    await self._handle_snapshot(writer, msg)
                else:
                    await self._send(writer, {"type": "error", "code": "bad_request", "message": f"unknown type: {msg_type}"})
        except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
            pass
        finally:
            self._cleanup_connection(conn_id)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main(port: int, data_dir: str) -> None:
    server = Server(Path(data_dir))
    await asyncio.start_server(
        server._handle_client, "127.0.0.1", port
    )
    print(f"Livedoc server listening on 127.0.0.1:{port}", flush=True)
    # Run forever
    await asyncio.Event().wait()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--data-dir", type=str, required=True)
    args = parser.parse_args()
    asyncio.run(main(args.port, args.data_dir))
