"""bserve: serve files under a root directory over HBIN/1 (SPEC.md)."""

import mimetypes
import os
import socketserver
import sys
from email.utils import formatdate

from . import wire

SERVER_NAME = "bserve/1"
IDLE_TIMEOUT = 300.0


def _content_type(path):
    guessed, _ = mimetypes.guess_type(path)
    if guessed is None:
        return "application/octet-stream"
    if guessed.startswith("text/") or guessed in ("application/json", "application/javascript"):
        return guessed + "; charset=utf-8"
    return guessed


def _etag(st):
    return f'"{st.st_size:x}-{st.st_mtime_ns:x}"'


def resolve(root, path):
    """Map a request path to an open-able regular file under root, or None (5)."""
    rel = path[1:]
    if rel == "" or rel.endswith("/"):
        rel += "index.html"
    root = os.path.realpath(root)
    full = os.path.realpath(os.path.join(root, rel))
    if os.path.commonpath([root, full]) != root:
        return None
    return full if os.path.isfile(full) else None


class Handler(socketserver.BaseRequestHandler):
    def setup(self):
        self.request.settimeout(IDLE_TIMEOUT)
        self.rfile = self.request.makefile("rb")
        self.wfile = self.request.makefile("wb")
        self.peer = "%s:%s" % self.client_address[:2]

    def finish(self):
        for f in (self.wfile, self.rfile):
            try:
                f.close()
            except OSError:
                pass

    def log(self, msg):
        self.server.log(f"{self.peer} {msg}")

    def handle(self):
        try:
            self.serve_connection()
        except (OSError, wire.Truncated) as exc:
            self.log(f"connection dropped: {exc}")

    def serve_connection(self):
        while True:
            try:
                frame = wire.read_frame(self.rfile)
            except wire.FrameTooLarge as exc:
                # We cannot find the next frame boundary, so this one is fatal.
                self.error(exc.stream_id, 400, str(exc))
                return
            if frame is None:
                return
            if frame.type not in wire.KNOWN_TYPES:
                continue  # section 3: unknown types are skipped, silently
            self.dispatch(frame)

    # --- one exchange ------------------------------------------------------

    def dispatch(self, frame):
        sid = frame.stream_id
        if frame.type != wire.T_REQUEST:
            return self.error(sid, 400, "only REQUEST frames are valid from a client")
        if sid == 0:
            return self.error(sid, 400, "stream id 0 is reserved")
        if not frame.end_stream:
            return self.error(sid, 400, "REQUEST must carry END_STREAM")
        try:
            method, path, headers = wire.decode_request(frame.payload)
        except wire.Malformed as exc:
            return self.error(sid, 400, str(exc))
        if method not in wire.METHOD_NAMES:
            return self.error(sid, 405, f"method {method} not supported",
                              extra=[("allow", "GET, HEAD")])
        try:
            self.serve_file(sid, method, path, dict(headers))
        except OSError:
            raise
        except Exception as exc:  # noqa: BLE001 — a bug must not kill the connection loop
            self.log(f"internal error: {exc!r}")
            self.error(sid, 500, "internal error")

    def serve_file(self, sid, method, path, req_headers):
        name = wire.METHOD_NAMES[method]
        full = resolve(self.server.root, path)
        try:
            f = open(full, "rb") if full else None
        except OSError:
            f = None  # unreadable is indistinguishable from absent
        if f is None:
            return self.error(sid, 404, f"no such file: {path}", label=f"{name} {path}")
        with f:
            st = os.fstat(f.fileno())
            etag = _etag(st)
            headers = [
                ("server", SERVER_NAME),
                ("content-type", _content_type(full)),
                ("content-length", str(st.st_size)),
                ("last-modified", formatdate(st.st_mtime, usegmt=True)),
                ("etag", etag),
            ]
            if req_headers.get("if-none-match") == etag:
                self.send_response(sid, 304, [h for h in headers if h[0] in ("server", "etag")], end=True)
                self.wfile.flush()
                return self.log(f"stream={sid} {name} {path} -> 304")
            no_body = method == wire.M_HEAD or st.st_size == 0
            self.send_response(sid, 200, headers, end=no_body)
            if not no_body:
                self.send_body(sid, f, st.st_size)
            self.wfile.flush()
        self.log(f"stream={sid} {name} {path} -> 200 {st.st_size}B")

    def send_response(self, sid, status, headers, end):
        payload = wire.encode_response(status, headers)
        flags = wire.F_END_STREAM if end else 0
        self.wfile.write(wire.encode_frame(wire.T_RESPONSE, flags, sid, payload))

    def send_body(self, sid, f, size):
        remaining = size
        while remaining:
            chunk = f.read(min(wire.MAX_PAYLOAD, remaining))
            if not chunk:
                # File shrank after we promised content-length. The only honest
                # thing left is to hang up; the client sees no END_STREAM.
                raise ConnectionAbortedError("file shrank while being served")
            remaining -= len(chunk)
            flags = wire.F_END_STREAM if remaining == 0 else 0
            self.wfile.write(wire.encode_frame(wire.T_DATA, flags, sid, chunk))

    def error(self, sid, status, message, extra=(), label=None):
        body = (message + "\n").encode("utf-8")
        headers = [("server", SERVER_NAME), ("content-type", "text/plain; charset=utf-8"),
                   ("content-length", str(len(body))), *extra]
        self.send_response(sid, status, headers, end=False)
        self.wfile.write(wire.encode_frame(wire.T_DATA, wire.F_END_STREAM, sid, body))
        self.wfile.flush()
        self.log(f"stream={sid} {label or '-'} -> {status} ({message})")


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, root, host="127.0.0.1", port=0, log=None):
        self.root = os.path.abspath(root)
        self.log = log or (lambda msg: print(f"bserve: {msg}", file=sys.stderr, flush=True))
        super().__init__((host, port), Handler)


def main(argv):
    args = list(argv)
    host = "127.0.0.1"
    if "--bind" in args:
        i = args.index("--bind")
        try:
            host = args[i + 1]
        except IndexError:
            return usage()
        del args[i:i + 2]
    if len(args) != 2:
        return usage()
    root, port_arg = args
    if not os.path.isdir(root):
        print(f"bserve: {root}: not a directory", file=sys.stderr)
        return 1
    try:
        port = int(port_arg)
        if not 0 <= port <= 65535:
            raise ValueError
    except ValueError:
        print(f"bserve: bad port {port_arg!r}", file=sys.stderr)
        return 1
    try:
        server = Server(root, host, port)
    except OSError as exc:
        print(f"bserve: cannot listen on {host}:{port}: {exc}", file=sys.stderr)
        return 1
    server.log(f"serving {server.root} on {host}:{server.server_address[1]}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def usage():
    print("usage: bserve <root-dir> <port> [--bind ADDR]", file=sys.stderr)
    return 1
