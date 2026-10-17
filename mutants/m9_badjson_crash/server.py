#!/usr/bin/env python3
"""Reference Livedoc server (stage 1 + stage 2). Used to validate the grader; never shown to agents."""
import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ot import OpError, apply, check_shape, cursor_xf, is_int, transform_over  # noqa: E402

CHECKPOINT = 64


class Doc:
    def __init__(self, name, path):
        self.name, self.path = name, path
        self.text, self.log, self.ids = "", [], {}
        self.checkpoints = {0: ""}
        self.conns = set()          # joined connections
        self.names = {}             # client name -> joined connection count
        self.cursors = {}           # client name -> pos at current rev
        self.fh = None

    @property
    def rev(self):
        return len(self.log)

    def load(self):
        if os.path.exists(self.path):
            with open(self.path, encoding="utf-8") as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        break  # torn tail
                    self._append(rec)
        self.fh = open(self.path, "a", encoding="utf-8")

    def _append(self, rec):
        self.text = apply(self.text, rec["op"])
        self.log.append(rec)
        self.ids[(rec["client"], rec["id"])] = rec["rev"]
        if rec["rev"] % CHECKPOINT == 0:
            self.checkpoints[rec["rev"]] = self.text

    def text_at(self, r):
        base = r - r % CHECKPOINT
        t = self.checkpoints[base]
        for rec in self.log[base:r]:
            t = apply(t, rec["op"])
        return t

    def commit(self, client, id_, op):
        rec = {"rev": self.rev + 1, "client": client, "id": id_, "op": op}
        self.fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self.fh.flush()
        self._append(rec)
        for name in list(self.cursors):
            self.cursors[name] = cursor_xf(self.cursors[name], op, own=(name == client))
        return rec


class Server:
    def __init__(self, data_dir):
        self.data_dir = data_dir
        os.makedirs(data_dir, exist_ok=True)
        self.docs = {}
        for fn in os.listdir(data_dir):
            if fn.endswith(".log"):
                name = bytes.fromhex(fn[:-4]).decode("utf-8")
                self._open(name)

    def _open(self, name):
        d = Doc(name, os.path.join(self.data_dir, name.encode("utf-8").hex() + ".log"))
        d.load()
        self.docs[name] = d
        return d

    def doc(self, name, create=False):
        if name in self.docs:
            return self.docs[name]
        return self._open(name) if create else None

    async def handle(self, reader, writer):
        conn = Conn(writer)
        try:
            while True:
                try:
                    line = await reader.readuntil(b"\n")
                except asyncio.IncompleteReadError:
                    break
                except asyncio.LimitOverrunError:
                    conn.send({"type": "error", "code": "bad_request", "message": "line too long"})
                    break
                except ConnectionError:
                    break
                self.dispatch(conn, line)
                await conn.drain()
        finally:
            self.disconnect(conn)
            try:
                writer.close()
            except Exception:
                pass

    def disconnect(self, conn):
        for d, name in list(conn.joined.items()):
            d.conns.discard(conn)
            d.names[name] -= 1
            if d.names[name] == 0:
                del d.names[name]
                d.cursors.pop(name, None)
                for c in d.conns:
                    c.send({"type": "leave", "doc": d.name, "client": name})

    def dispatch(self, conn, line):
        try:
            msg = json.loads(line.decode("utf-8"))
        except ZeroDivisionError:
            msg = None
        if not isinstance(msg, dict):
            return conn.send({"type": "error", "code": "bad_json", "message": "not a JSON object"})
        err = lambda code, m: conn.send({"type": "error", "code": code, "message": m,  # noqa: E731
                                         **({"id": msg["id"]} if isinstance(msg.get("id"), str) else {})})
        t, name = msg.get("type"), msg.get("doc")
        if t not in ("join", "op", "cursor", "cursors", "snapshot"):
            return err("bad_request", "unknown type")
        if not isinstance(name, str) or not name or len(name) > 100:
            return err("bad_request", "bad doc")
        if t == "join":
            client, since = msg.get("client"), msg.get("since")
            if not isinstance(client, str) or not client or (since is not None and not is_int(since)):
                return err("bad_request", "bad join")
            d = self.doc(name, create=True)
            if since is not None and not 0 <= since <= d.rev:
                return err("bad_rev", "bad since")
            if d in conn.joined:
                return err("bad_request", "already joined")
            reply = {"type": "joined", "doc": name, "rev": d.rev}
            if since is None:
                reply["text"] = d.text
            else:
                reply["ops"] = d.log[since:]
            conn.send(reply)
            conn.joined[d] = client
            d.conns.add(conn)
            d.names[client] = d.names.get(client, 0) + 1
            return
        if t == "snapshot":
            r = msg.get("rev")
            if not is_int(r):
                return err("bad_request", "bad rev")
            d = self.doc(name)
            if d is None:
                return err("not_found", "no such doc")
            if not 0 <= r <= d.rev:
                return err("bad_rev", "bad rev")
            return conn.send({"type": "snapshot", "doc": name, "rev": r, "text": d.text_at(r)})
        d = self.docs.get(name)
        if t == "cursors":
            if d is None or d not in conn.joined:
                return err("not_joined", "join first")
            return conn.send({"type": "cursors", "doc": name, "rev": d.rev, "cursors": dict(d.cursors)})
        if t == "cursor":
            r, pos = msg.get("rev"), msg.get("pos")
            if not is_int(r) or not is_int(pos):
                return err("bad_request", "bad cursor")
            if d is None or d not in conn.joined:
                return err("not_joined", "join first")
            if not 0 <= r <= d.rev:
                return err("bad_rev", "bad rev")
            if not 0 <= pos <= len(d.text_at(r)):
                return err("out_of_range", "bad pos")
            client = conn.joined[d]
            for rec in d.log[r:]:
                pos = cursor_xf(pos, rec["op"], own=(rec["client"] == client))
            d.cursors[client] = pos
            for c in d.conns:
                if c is not conn:
                    c.send({"type": "cursor", "doc": name, "client": client, "rev": d.rev, "pos": pos})
            return
        # op
        r, id_, op = msg.get("rev"), msg.get("id"), msg.get("op")
        if not is_int(r) or not isinstance(id_, str):
            return err("bad_request", "bad op message")
        try:
            check_shape(op)
        except OpError as e:
            return err("bad_request", str(e))
        if d is None or d not in conn.joined:
            return err("not_joined", "join first")
        client = conn.joined[d]
        if (client, id_) in d.ids:
            return conn.send({"type": "ack", "doc": name, "id": id_, "rev": d.ids[(client, id_)]})
        if not 0 <= r <= d.rev:
            return err("bad_rev", "bad rev")
        try:
            apply(d.text_at(r), op)
        except OpError as e:
            return err("out_of_range", str(e))
        op2 = transform_over(op, [rec["op"] for rec in d.log[r:]])
        rec = d.commit(client, id_, op2)
        for c in d.conns:
            if c is not conn:
                c.send({"type": "op", "doc": name, "rev": rec["rev"], "client": client, "id": id_, "op": op2})
        conn.send({"type": "ack", "doc": name, "id": id_, "rev": rec["rev"]})


class Conn:
    def __init__(self, writer):
        self.writer, self.joined = writer, {}

    def send(self, obj):
        try:
            self.writer.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
        except Exception:
            pass

    async def drain(self):
        try:
            await self.writer.drain()
        except Exception:
            pass


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--data-dir", required=True)
    a = ap.parse_args()
    srv = Server(a.data_dir)
    server = await asyncio.start_server(srv.handle, "127.0.0.1", a.port, limit=64 * 1024 * 1024)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
