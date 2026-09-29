"""Capture the exact bytes of one GET /index.html exchange, for docs/HEXDUMP.md.

Everything that could vary is pinned: file contents, its mtime, and the host
header. Run `python3 -m tools.capture_example` to print the dump; the test
suite calls capture() and checks docs/HEXDUMP.md still matches.
"""

import io
import os
import tempfile
import threading

from hbin.client import Client
from hbin.hexdump import hexdump
from hbin.server import Server

BODY = b"<h1>hello</h1>\n"
MTIME = 1_700_000_000
AUTHORITY = "localhost:9000"


def capture():
    """Return [(direction, raw_frame_bytes)] with direction '>' or '<'."""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "index.html")
        with open(path, "wb") as f:
            f.write(BODY)
        os.utime(path, (MTIME, MTIME))
        server = Server(tmp, "127.0.0.1", 0, log=lambda msg: None)
        thread = threading.Thread(target=server.serve_forever, args=(0.01,), daemon=True)
        thread.start()
        try:
            client = Client("127.0.0.1", server.server_address[1])
            client.authority = AUTHORITY
            frames = []
            client.trace = lambda arrow, frame: frames.append((arrow, frame.encode()))
            client.fetch("/index.html", io.BytesIO())
            client.close()
        finally:
            server.shutdown()
            server.server_close()
    return frames


def main():
    for arrow, raw in capture():
        print(f"{arrow} {len(raw)} bytes")
        print("\n".join(hexdump(raw)))
        print()


if __name__ == "__main__":
    main()
