import json
import socket


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Wire:
    """A raw protocol connection for tests: send dicts, receive dicts."""

    def __init__(self, port, timeout=5.0):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=timeout)
        self.rfile = self.sock.makefile("rb")

    def send(self, message):
        self.sock.sendall(json.dumps(message).encode("utf-8") + b"\n")

    def send_raw(self, data):
        self.sock.sendall(data)

    def recv(self):
        line = self.rfile.readline()
        assert line, "connection closed"
        return json.loads(line)

    def join(self, doc, client, since=None):
        message = {"type": "join", "doc": doc, "client": client}
        if since is not None:
            message["since"] = since
        self.send(message)
        return self.recv()

    def close(self):
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.rfile.close()
        self.sock.close()
