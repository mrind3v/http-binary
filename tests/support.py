"""Shared test helpers: a throwaway server and a raw-frame client."""

import os
import socket
import tempfile
import threading
import unittest

from hbin import wire
from hbin.server import Server


class ServerCase(unittest.TestCase):
    """Runs a Server on an ephemeral port over a temp web root."""

    files = {
        "index.html": b"<h1>hello</h1>\n",
        "sub/note.txt": b"nested\n",
        "empty.txt": b"",
        "big.bin": bytes(range(256)) * 200,  # 51200 bytes: four DATA frames
    }

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.join(self.tmp.name, "www")
        for rel, data in self.files.items():
            full = os.path.join(self.root, rel)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "wb") as f:
                f.write(data)
            os.utime(full, (1_700_000_000, 1_700_000_000))
        with open(os.path.join(self.tmp.name, "secret.txt"), "w") as f:
            f.write("outside the root\n")
        self.logs = []
        self.connections = 0
        self.server = Server(self.root, "127.0.0.1", 0, log=self.logs.append)
        self.port = self.server.server_address[1]
        real_setup = self.server.RequestHandlerClass.setup

        def counting_setup(handler):
            self.connections += 1
            real_setup(handler)

        self.server.RequestHandlerClass.setup = counting_setup
        self.addCleanup(setattr, self.server.RequestHandlerClass, "setup", real_setup)
        threading.Thread(target=self.server.serve_forever, args=(0.01,), daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def connect(self):
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        self.addCleanup(sock.close)
        return RawClient(sock)


class RawClient:
    """Sends hand-built frames and reads whole exchanges back."""

    def __init__(self, sock):
        self.sock = sock
        self.rfile = sock.makefile("rb")

    def send(self, data):
        self.sock.sendall(data)

    def request(self, sid, path, method=wire.M_GET, headers=(), flags=wire.F_END_STREAM):
        self.send(wire.encode_frame(wire.T_REQUEST, flags, sid,
                                    wire.encode_request(method, path, headers)))

    def frame(self):
        return wire.read_frame(self.rfile)

    def exchange(self):
        """Read one response. Returns (status, headers dict, body, frames)."""
        head = self.frame()
        assert head is not None and head.type == wire.T_RESPONSE, head
        status, headers = wire.decode_response(head.payload)
        frames, body, last = [head], b"", head
        while not last.end_stream:
            last = self.frame()
            assert last is not None and last.type == wire.T_DATA and last.stream_id == head.stream_id
            frames.append(last)
            body += last.payload
        return status, dict(headers), body, frames

    def at_eof(self):
        return self.frame() is None
