"""
Livedoc client library.

Provides a ``Client`` class for connecting to a Livedoc server and
collaboratively editing documents. Uses a background thread with its own
event loop for all I/O.
"""

from __future__ import annotations

import asyncio
import json
import threading
import uuid
from collections import deque
from typing import Any

from livedoc_ot import apply_op, xf, _validate_op


class _ClientIO:
    """Async I/O running in a dedicated thread. All socket operations go through here."""

    def __init__(self, on_msg) -> None:
        """
        Args:
            on_msg: Callback(dict) called when a message arrives from server.
        """
        self._loop: asyncio.AbstractEventLoop | None = None
        self._t: threading.Thread | None = None
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._on_msg = on_msg
        self._doc: str = ""
        self._rev: int = 0
        self._call_queue: asyncio.Queue = asyncio.Queue()
        self._msg_reader_task: asyncio.Task | None = None

    def start(self) -> None:
        self._loop = asyncio.new_event_loop()

        def _run() -> None:
            asyncio.set_event_loop(self._loop)

            async def _process_calls() -> None:
                self._ready.set()
                while not self._stop.is_set():
                    try:
                        coro, fut = await self._call_queue.get()
                        try:
                            result = await coro
                            if not fut.done():
                                fut.set_result(result)
                        except Exception as e:
                            if not fut.done():
                                fut.set_exception(e)
                    except Exception:
                        break

            self._msg_reader_task = self._loop.create_task(self._read_messages())
            self._loop.create_task(_process_calls())
            self._loop.run_forever()

        self._t = threading.Thread(target=_run, daemon=True)
        self._t.start()
        self._ready.wait()

    def stop(self) -> None:
        self._stop.set()
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._t:
            self._t.join(timeout=2.0)

    def connect(self, host: str, port: int, timeout: float) -> None:
        fut = asyncio.run_coroutine_threadsafe(
            self._do_connect(host, port, timeout), self._loop
        )
        fut.result(timeout=timeout + 5)

    async def _do_connect(self, host: str, port: int, timeout: float) -> None:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout
        )
        self._reader = reader
        self._writer = writer

    def join(self, doc: str, client: str) -> dict:
        fut = asyncio.run_coroutine_threadsafe(
            self._do_join(doc, client), self._loop
        )
        return fut.result(timeout=30.0)

    async def _do_join(self, doc: str, client: str) -> dict:
        self._writer.write((json.dumps({
            "type": "join",
            "doc": doc,
            "client": client,
        }) + "\n").encode("utf-8"))
        await self._writer.drain()
        line = await asyncio.wait_for(self._reader.readline(), timeout=30.0)
        return json.loads(line.decode("utf-8"))

    def send_op(self, doc: str, rev: int, eid: str, op: list[dict]) -> None:
        """Send an operation (fire-and-forget)."""
        asyncio.run_coroutine_threadsafe(
            self._do_send_op(doc, rev, eid, op), self._loop
        )

    async def _do_send_op(self, doc: str, rev: int, eid: str, op: list[dict]) -> None:
        self._writer.write((json.dumps({
            "type": "op",
            "doc": doc,
            "rev": rev,
            "id": eid,
            "op": op,
        }) + "\n").encode("utf-8"))
        await self._writer.drain()

    async def _read_messages(self) -> None:
        """Background task: read messages and forward to on_msg callback."""
        try:
            while not self._stop.is_set():
                try:
                    line = await asyncio.wait_for(
                        self._reader.readline(), timeout=0.5
                    )
                except (asyncio.TimeoutError, TimeoutError):
                    continue
                except (ConnectionResetError, BrokenPipeError):
                    break
                if not line:
                    break
                try:
                    msg = json.loads(line.decode("utf-8"))
                    # Callback runs in the I/O thread, which calls back into
                    # the client's thread-safe methods
                    self._on_msg(msg)
                except (json.JSONDecodeError, ValueError):
                    pass
        except Exception:
            pass


class Client:
    """Connect to a Livedoc server and edit a document.

    Args:
        host: Server hostname.
        port: Server port.
        doc: Document name.
        name: Client name (participant identifier).
        timeout: Timeout in seconds for join.
    """

    def __init__(
        self,
        host: str,
        port: int,
        doc: str,
        name: str,
        timeout: float = 5.0,
    ) -> None:
        self._host = host
        self._port = port
        self._doc = doc
        self._name = name
        self._timeout = timeout

        # State protected by lock
        self._lock = threading.Lock()
        self._text: str = ""
        self._rev: int = 0
        self._inflight: dict[str, Any] | None = None
        self._buffer: list[list[dict]] = []
        self._cond = threading.Condition(self._lock)

        # Start async I/O in background thread
        self._io = _ClientIO(on_msg=self._on_msg)
        self._io.start()

        # Connect and join
        self._io.connect(host, port, timeout)
        resp = self._io.join(doc, name)
        if resp.get("type") != "joined":
            self.close()
            raise ConnectionError(f"Expected joined, got {resp.get('type')}")

        self._rev = resp["rev"]
        if "text" in resp:
            self._text = resp["text"]
        else:
            self._text = ""
            for op_entry in resp.get("ops", []):
                self._text = apply_op(self._text, op_entry["op"])

    # -- properties --

    @property
    def text(self) -> str:
        with self._lock:
            return self._text

    @property
    def rev(self) -> int:
        with self._lock:
            return self._rev

    # -- edits --

    def insert(self, pos: int, text: str) -> None:
        """Insert *text* at *pos* in the local text.

        Raises ``ValueError`` if the edit is invalid.
        """
        with self._lock:
            if pos < 0 or pos > len(self._text):
                raise ValueError(f"insert position {pos} out of range")
            if not text:
                raise ValueError("insert text must be non-empty")
            self._text = self._text[:pos] + text + self._text[pos:]
            op = [{"ins": pos, "text": text}]
            self._buffer.append(op)
            self._maybe_send()

    def delete(self, pos: int, length: int) -> None:
        """Delete *length* characters starting at *pos*.

        Raises ``ValueError`` if the edit is invalid.
        """
        with self._lock:
            if pos < 0 or pos + length > len(self._text):
                raise ValueError(f"delete [{pos},{pos+length}) out of range")
            if length < 1:
                raise ValueError("delete length must be >= 1")
            self._text = self._text[:pos] + self._text[pos + length:]
            op = [{"del": pos, "len": length}]
            self._buffer.append(op)
            self._maybe_send()

    def _maybe_send(self) -> None:
        """Send buffered operations if nothing is in flight."""
        if self._inflight is not None:
            return
        if not self._buffer:
            return
        op = self._buffer.pop(0)
        eid = str(uuid.uuid4())
        self._inflight = {"id": eid, "op": op}
        self._io.send_op(self._doc, self._rev, eid, op)

    def wait_synced(self, timeout: float = 5.0) -> bool:
        """Block until all local edits have been acknowledged."""
        import time
        with self._cond:
            end_time = time.monotonic() + timeout
            while self._inflight is not None or self._buffer:
                remaining = end_time - time.monotonic()
                if remaining <= 0:
                    return False
                self._cond.wait(timeout=min(remaining, 0.5))
            return True

    def close(self) -> None:
        """Close the connection and stop the background reader."""
        try:
            self._io.stop()
        except Exception:
            pass

    # -- message handling --

    def _on_msg(self, msg: dict) -> None:
        """Called from the I/O thread when a message arrives."""
        msg_type = msg.get("type")
        if msg_type == "ack":
            self._handle_ack(msg)
        elif msg_type == "op":
            self._handle_remote_op(msg)

    def _handle_ack(self, msg: dict) -> None:
        with self._cond:
            eid = msg.get("id")
            new_rev = msg.get("rev")
            if self._inflight and self._inflight.get("id") == eid:
                self._rev = new_rev
                self._inflight = None
                self._maybe_send()
                self._cond.notify_all()

    def _handle_remote_op(self, msg: dict) -> None:
        if msg.get("doc") != self._doc:
            return
        remote_op = msg.get("op", [])
        with self._cond:
            if self._inflight is not None:
                inflight_op = self._inflight["op"]
                inflight_op, _ = xf(inflight_op, remote_op)
                self._inflight["op"] = inflight_op

            new_buffer: list[list[dict]] = []
            for buf_op in self._buffer:
                buf_op, _ = xf(buf_op, remote_op)
                new_buffer.append(buf_op)
            self._buffer = new_buffer

            self._text = apply_op(self._text, remote_op)
            self._rev = msg.get("rev", self._rev)
            self._cond.notify_all()
