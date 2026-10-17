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
            if q is not None:
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
            asyncio.ensure_future(self._writer_loop(writer))
        self._connections[writer].add(doc_name)
        doc.subscribers.add(self._outbound[writer])

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
        client_name = msg.get("client")
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
            self._error(writer, "bad_request", "op must be a list")
            return

        # not_joined
        if not self._is_joined(writer, doc_name):
            self._error(writer, "not_joined",
                        f"Not joined to document {doc_name}", msg_id)
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
                        f"rev must be 0..{doc.rev}", msg_id)
            return

        # Validate op structure
        if not validate_op(op):
            self._error(writer, "bad_request", "Invalid operation structure", msg_id)
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
                        f"{base_rev}", msg_id)
            return

        # Transform against operations committed after base_rev
        current_op = list(op)
        after_ops = [h for h in doc.history if h["rev"] > base_rev]
        for h in after_ops:
            current_op, _ = xf(current_op, h["op"])

        # Commit
        new_rev = doc.append(client_name, op_id, current_op)

        # Ack to sender
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
            # Clean up subscriptions
            if writer in self._connections:
                for doc_name in self._connections[writer]:
                    doc = self.documents.get(doc_name)
                    if doc:
                        doc.subscribers.discard(self._outbound.get(writer))
                del self._connections[writer]
                if writer in self._outbound:
                    del self._outbound[writer]
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
