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

        # Ack to sender
        await self._send(writer, {"type": "ack", "doc": doc_name, "id": eid, "rev": new_rev})

        # Broadcast to other connections
        for bid, bop in zip(range(len(broadcast_ops)), broadcast_ops):
            # Get the corresponding history entry for the broadcast op
            hist_entry = doc.history[new_rev - 1 - bid]  # This is wrong indexing
            pass  # We'll handle this differently

        # Actually, broadcast_ops[i] corresponds to doc.history[i] for i >= rev
        # But we need to broadcast each transformed op. Let me reconsider.
        # broadcast_ops[i] is the transformed version of doc.history[i]'s op.
        # But we need to broadcast them in order. The spec says broadcasts should
        # be in revision order. Since we're committing one operation at a time,
        # the broadcast should be the transformed op for this new revision.

        # Wait, I'm overcomplicating this. The broadcast for this new revision
        # is just the transformed_op. The broadcast_ops are for transforming
        # OTHER clients' operations. We don't broadcast them here.

        # Actually re-reading the spec:
        # "Every other connection joined to D receives
        #  {"type": "op", "doc": D, "rev": N+1, "client": NAME, "id": ID, "op": OP2}"
        # where OP2 is the transformed operation.
        # OP2 is the transformed version of the incoming operation, applied after
        # all operations committed after R.

        # So we broadcast the transformed_op (which is what we committed).
        # broadcast_ops are used when a client receives remote ops and needs
        # to transform its buffered/inflight ops against them.

        # Broadcast
        await self._broadcast(doc_name, {
            "type": "op",
            "doc": doc_name,
            "rev": new_rev,
            "client": client,
            "id": eid,
            "op": transformed_op,
        }, exclude_conn=conn_id)

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
                else:
                    await self._send(writer, {"type": "error", "code": "bad_request", "message": f"unknown type: {msg_type}"})
        except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
            pass
        finally:
            # Clean up
            if conn_id in self.connection_docs:
                self.connection_docs[conn_id] = set()
            self.connection_clients.pop(conn_id, None)
            self.connections.pop(conn_id, None)


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
