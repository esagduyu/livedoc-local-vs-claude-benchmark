import asyncio
import json
import os
import sys
import socket

def doc_name_to_file(doc_name):
    return doc_name.encode("utf-8").hex()

from livedoc_client import validate_op, apply_op, xf_edit, xf, transform_against_history

class Document:
    def __init__(self, data_dir, doc_name):
        self.doc_name = doc_name
        self.file_name = doc_name_to_file(doc_name)
        self.dir = data_dir
        self.log_path = os.path.join(data_dir, self.file_name) if data_dir else f".{self.file_name}.jsonl"
        os.makedirs(data_dir, exist_ok=True)
        self._rev_lengths = {0: 0}
        self._entries = []
        self._dedup = {}
        self._replay_log()
    
    def _replay_log(self):
        if not os.path.exists(self.log_path):
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
                    break
        with open(self.log_path, "w", encoding="utf-8") as f:
            for vl in valid_lines:
                f.write(vl + "\n")
        for line in valid_lines:
            entry = json.loads(line)
            self._commit_entry(entry, persist=False)
    
    def _commit_entry(self, entry, persist=True):
        rev = entry["rev"]
        client = entry["client"]
        op_id = entry["id"]
        op = entry["op"]
        prev_len = self._rev_lengths.get(rev - 1, 0)
        new_len = prev_len
        for edit in op:
            if "ins" in edit:
                new_len += len(edit["text"])
            elif "del" in edit:
                new_len -= edit["len"]
        self._rev_lengths[rev] = new_len
        self._entries.append(entry)
        self._dedup[(client, op_id)] = rev
        if persist:
            line = json.dumps(entry, separators=(",", ":")) + "\n"
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(line)
                f.flush()
    
    def current_rev(self):
        return max(self._rev_lengths.keys()) if self._rev_lengths else 0
    
    def get_text(self, rev=None):
        if rev is None:
            rev = self.current_rev()
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

class LivedocServer:
    def __init__(self, port, data_dir):
        self.port = port
        self.data_dir = data_dir
        self.documents = {}
        self._connections = {}
    
    def _get_doc(self, doc_name):
        if doc_name not in self.documents:
            self.documents[doc_name] = Document(self.data_dir, doc_name)
        return self.documents[doc_name]
    
    def _parse_message(self, line):
        try:
            return json.loads(line)
        except (json.JSONDecodeError, ValueError):
            return None
    
    def _send_error(self, writer, code, message, msg_id=None):
        error = {"type": "error", "code": code, "message": message}
        if msg_id is not None:
            error["id"] = msg_id
        try:
            writer.write(json.dumps(error) + "\n")
            asyncio.create_task(writer.drain())
        except (BrokenPipeError, ConnectionResetError):
            pass
    
    async def _handle_client(self, reader, writer):
        addr = writer.get_extra_info("peername")
        print(f"[DEBUG] Client connected from {addr}", flush=True)
        try:
            while True:
                try:
                    line = await asyncio.wait_for(reader.readline(), timeout=30)
                except asyncio.TimeoutError:
                    print(f"[DEBUG] Timeout reading from {addr}", flush=True)
                    break
                if not line:
                    print(f"[DEBUG] Empty line from {addr}", flush=True)
                    break
                line = line.decode("utf-8", errors="replace").strip("\r\n")
                print(f"[DEBUG] Decoded line: {line[:100]}", flush=True)
                if not line:
                    continue
                msg = self._parse_message(line)
                if msg is None:
                    self._send_error(writer, "bad_json", "Invalid JSON", msg_id=None)
                    continue
                conn_state = {
                    "doc": None,
                    "rev": 0,
                    "joined": False,
                    "client_name": None,
                    "writer": writer,
                }
                msg_type = msg.get("type")
                print(f"[DEBUG] Message type: {msg_type}", flush=True)
                if msg_type == "join":
                    doc_name = msg.get("doc", "")
                    since = msg.get("since", None)
                    if not doc_name or not isinstance(doc_name, str) or len(doc_name) > 100:
                        self._send_error(writer, "bad_request", "Invalid doc name", msg_id=msg.get("id"))
                        return
                    doc = self._get_doc(doc_name)
                    current_rev = doc.current_rev()
                    if since is not None:
                        if not isinstance(since, int) or since < 0 or since > current_rev:
                            self._send_error(writer, "bad_rev", f"Invalid since: {since}", msg_id=msg.get("id"))
                            return
                    conn_state["doc"] = doc_name
                    conn_state["joined"] = True
                    conn_state["client_name"] = msg.get("client", "")
                    if since is None:
                        text = doc.get_text()
                        reply = {"type": "joined", "doc": doc_name, "rev": current_rev, "text": text}
                        writer.write(json.dumps(reply) + "\n")
                        asyncio.create_task(writer.drain())
                    else:
                        ops = []
                        for entry in doc._entries:
                            if entry["rev"] > since:
                                ops.append({"rev": entry["rev"], "client": entry["client"], "id": entry["id"], "op": entry["op"]})
                        reply = {"type": "joined", "doc": doc_name, "rev": current_rev, "ops": ops}
                        writer.write(json.dumps(reply) + "\n")
                        asyncio.create_task(writer.drain())
                    self._connections[writer] = conn_state
                    return
                elif msg_type == "op":
                    doc_name = msg.get("doc", "")
                    rev = msg.get("rev", 0)
                    client_id = msg.get("id", "")
                    op = msg.get("op", [])
                    conn_info = self._connections.get(writer, {})
                    if doc_name not in conn_info.get("doc", ""):
                        self._send_error(writer, "not_joined", "Connection not joined to this doc", msg_id=client_id)
                        return
                    doc = self._get_doc(doc_name)
                    existing_rev = doc._dedup.get((conn_info.get("client_name", ""), client_id))
                    if existing_rev is not None:
                        self._send_ack(writer, client_id, existing_rev)
                        return
                    error, _ = doc.validate_op_against_rev(op, rev)
                    if error:
                        self._send_error(writer, error, f"Invalid operation: {error}", msg_id=client_id)
                        return
                    if rev < 0 or rev > doc.current_rev():
                        self._send_error(writer, "bad_rev", f"Invalid revision: {rev}", msg_id=client_id)
                        return
                    current_rev = doc.current_rev()
                    ops_after = [e["op"] for e in doc._entries if e["rev"] > rev]
                    transformed_op = transform_against_history(op, ops_after)
                    new_rev = doc.apply_op_unchecked(transformed_op)
                    client_name = conn_info.get("client_name", "")
                    doc._commit_entry({"rev": new_rev, "client": client_name, "id": client_id, "op": transformed_op}, persist=True)
                    broadcast_msg = {"type": "op", "doc": doc_name, "rev": new_rev, "client": client_name, "id": client_id, "op": transformed_op}
                    for w, cs in self._connections.items():
                        if cs.get("doc") == doc_name and w != writer:
                            try:
                                w.write(json.dumps(broadcast_msg) + "\n")
                                asyncio.create_task(w.drain())
                            except (BrokenPipeError, ConnectionResetError):
                                pass
                    ack_msg = {"type": "ack", "doc": doc_name, "id": client_id, "rev": new_rev}
                    writer.write(json.dumps(ack_msg) + "\n")
                    asyncio.create_task(writer.drain())
                else:
                    self._send_error(writer, "bad_request", f"Unknown type: {msg_type}", msg_id=None)
        except (ConnectionResetError, BrokenPipeError, OSError) as e:
            print(f"[DEBUG] Connection error from {addr}: {e}", flush=True)
        finally:
            print(f"[DEBUG] Cleaning up connection from {addr}", flush=True)
            if writer in self._connections:
                del self._connections[writer]
            try:
                writer.close()
            except:
                pass
    
    def _send_ack(self, writer, client_id, rev):
        msg = {"type": "ack", "id": client_id, "rev": rev}
        writer.write(json.dumps(msg) + "\n")
        asyncio.create_task(writer.drain())
    
    async def _start_server(self):
        server = await asyncio.start_server(
            self._handle_client, "127.0.0.1", self.port
        )
        async with server:
            await server.serve_forever()
    
    def run(self):
        try:
            asyncio.run(self._start_server())
        except KeyboardInterrupt:
            pass

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Livedoc OT server")
    parser.add_argument("--port", type=int, required=True, help="Port to listen on")
    parser.add_argument("--data-dir", type=str, default="data", help="Data directory for persistence")
    args = parser.parse_args()
    server = LivedocServer(args.port, args.data_dir)
    print(f"Livedoc server listening on 127.0.0.1:{args.port}, data dir: {args.data_dir}")
    server.run()

if __name__ == "__main__":
    main()
