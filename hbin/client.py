"""bcurl: fetch files over HBIN/1 (SPEC.md), one connection for everything."""

import socket
import sys

from . import wire
from .hexdump import hexdump

USER_AGENT = "bcurl/1"
DUMP_LIMIT = 256  # bytes of each frame shown by -v before truncating

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_NETWORK = 2
EXIT_PROTOCOL = 3
EXIT_HTTP_ERROR = 22  # same as `curl -f`


class ProtocolError(Exception):
    pass


def parse_target(arg):
    """'host[:port]/path' -> (host, port, path). Port defaults to 9000."""
    rest = arg.split("://", 1)[1] if "://" in arg else arg
    authority, slash, path = rest.partition("/")
    path = "/" + path if slash else "/"
    host, port = authority, 9000
    if authority.startswith("["):  # [::1]:9000
        host, _, tail = authority[1:].partition("]")
        if tail.startswith(":"):
            port = tail[1:]
    elif ":" in authority:
        host, _, port = authority.rpartition(":")
    try:
        port = int(port)
        if not host or not 0 < port < 65536:
            raise ValueError
    except ValueError:
        raise ValueError(f"bad host or port in {arg!r}") from None
    return host, port, path


class Client:
    def __init__(self, host, port, verbose=False, err=sys.stderr, timeout=30.0):
        self.host, self.port = host, port
        self.verbose = verbose
        self.err = err
        self.next_stream = 1
        self.sock = socket.create_connection((host, port), timeout=10.0)
        self.sock.settimeout(timeout)
        self.rfile = self.sock.makefile("rb")
        self.wfile = self.sock.makefile("wb")
        self.authority = f"{host}:{port}"

    def close(self):
        for f in (self.wfile, self.rfile, self.sock):
            try:
                f.close()
            except OSError:
                pass

    # --- -v --------------------------------------------------------------

    def trace(self, arrow, frame):
        if not self.verbose:
            return
        raw = frame.encode()
        print(f"{arrow} {describe(frame)}", file=self.err)
        for line in hexdump(raw, DUMP_LIMIT):
            print(f"{arrow}   {line}", file=self.err)

    # --- one exchange ----------------------------------------------------

    def fetch(self, path, out, method=wire.M_GET, extra_headers=()):
        """Send one request, stream the body into `out`. Returns the status."""
        sid = self.next_stream
        self.next_stream += 1
        headers = [("host", self.authority), ("user-agent", USER_AGENT), ("accept", "*/*"),
                   *extra_headers]
        request = wire.Frame(wire.T_REQUEST, wire.F_END_STREAM, sid,
                             wire.encode_request(method, path, headers))
        self.trace(">", request)
        self.wfile.write(request.encode())
        self.wfile.flush()
        return self.read_response(sid, method, out)

    def read_response(self, sid, method, out):
        status = None
        resp_headers = {}
        received = 0
        while True:
            try:
                frame = wire.read_frame(self.rfile)
            except wire.FrameTooLarge as exc:
                raise ProtocolError(str(exc)) from None
            except wire.Truncated:
                raise ProtocolError("connection closed in the middle of a frame") from None
            if frame is None:
                raise ProtocolError("connection closed before the response ended")
            self.trace("<", frame)
            if frame.type not in (wire.T_RESPONSE, wire.T_DATA):
                continue  # section 3: skip frame types we do not know
            if frame.stream_id != sid:
                raise ProtocolError(f"frame for stream {frame.stream_id}, expected {sid}")
            if frame.type == wire.T_RESPONSE:
                if status is not None:
                    raise ProtocolError("second RESPONSE frame on one stream")
                try:
                    status, headers = wire.decode_response(frame.payload)
                except wire.Malformed as exc:
                    raise ProtocolError(f"bad RESPONSE: {exc}") from None
                resp_headers = dict(headers)
            else:
                if status is None:
                    raise ProtocolError("DATA before RESPONSE")
                out.write(frame.payload)
                received += len(frame.payload)
            if frame.end_stream:
                break
        if status is None:
            raise ProtocolError("stream ended without a RESPONSE")
        expected = resp_headers.get("content-length")
        if expected is not None and method != wire.M_HEAD and status != 304:
            if not expected.isdigit() or int(expected) != received:
                raise ProtocolError(f"content-length {expected} but received {received} bytes")
        return status


def describe(frame):
    """One line saying what a frame is, in the spec's vocabulary."""
    names = {wire.T_REQUEST: "REQUEST", wire.T_RESPONSE: "RESPONSE", wire.T_DATA: "DATA"}
    name = names.get(frame.type, f"UNKNOWN(0x{frame.type:02x})")
    flags = "END_STREAM" if frame.end_stream else "-"
    line = f"{name} stream={frame.stream_id} flags={flags} len={len(frame.payload)}"
    try:
        if frame.type == wire.T_REQUEST:
            method, path, headers = wire.decode_request(frame.payload)
            line += f"  {wire.METHOD_NAMES.get(method, method)} {path}"
            line += "".join(f" {k}={v!r}" for k, v in headers)
        elif frame.type == wire.T_RESPONSE:
            status, headers = wire.decode_response(frame.payload)
            line += f"  {status}" + "".join(f" {k}={v!r}" for k, v in headers)
    except wire.Malformed as exc:
        line += f"  (malformed: {exc})"
    return line


USAGE = """usage: bcurl [-v] [-I] [-H 'name: value']... [-o FILE] host[:port]/path...

  -v   hexdump every frame sent and received (to stderr)
  -I   send HEAD instead of GET
  -H   add a request header (repeatable)
  -o   write bodies to FILE instead of stdout

Several paths are fetched one after another over a single connection, so all
of them must name the same host:port. Default port is 9000. Exit status:
0 ok, 1 usage, 2 network, 3 protocol error, 22 for a 4xx/5xx response."""


def parse_args(argv):
    opts = {"verbose": False, "method": wire.M_GET, "headers": [], "output": None, "targets": []}
    args = list(argv)
    while args:
        arg = args.pop(0)
        if arg == "-v":
            opts["verbose"] = True
        elif arg == "-I":
            opts["method"] = wire.M_HEAD
        elif arg in ("-H", "-o"):
            if not args:
                raise ValueError(f"{arg} needs an argument")
            value = args.pop(0)
            if arg == "-o":
                opts["output"] = value
            else:
                name, colon, val = value.partition(":")
                if not colon or not name.strip():
                    raise ValueError(f"bad header {value!r}; want 'name: value'")
                opts["headers"].append((name.strip().lower(), val.strip()))
        elif arg in ("-h", "--help"):
            raise ValueError("")
        elif arg.startswith("-") and arg != "-":
            raise ValueError(f"unknown option {arg}")
        else:
            opts["targets"].append(parse_target(arg))
    if not opts["targets"]:
        raise ValueError("no URL given")
    if len({(h, p) for h, p, _ in opts["targets"]}) > 1:
        raise ValueError("all URLs must share one host:port (bcurl never opens a second connection)")
    return opts


def main(argv, stdout=None, err=None):
    err = err or sys.stderr
    try:
        opts = parse_args(argv)
    except ValueError as exc:
        if str(exc):
            print(f"bcurl: {exc}", file=err)
        print(USAGE, file=err)
        return EXIT_USAGE

    host, port, _ = opts["targets"][0]
    try:
        client = Client(host, port, opts["verbose"], err)
    except OSError as exc:
        print(f"bcurl: cannot connect to {host}:{port}: {exc}", file=err)
        return EXIT_NETWORK

    out = open(opts["output"], "wb") if opts["output"] else (stdout or sys.stdout.buffer)
    worst = EXIT_OK
    try:
        for _, _, path in opts["targets"]:
            status = client.fetch(path, out, opts["method"], opts["headers"])
            if opts["verbose"]:
                print(f"* {path}: status {status}", file=err)
            if status >= 400:
                print(f"bcurl: {path}: server answered {status}", file=err)
                worst = EXIT_HTTP_ERROR
            out.flush()
    except ProtocolError as exc:
        print(f"bcurl: protocol error: {exc}", file=err)
        return EXIT_PROTOCOL
    except OSError as exc:
        print(f"bcurl: network error: {exc}", file=err)
        return EXIT_NETWORK
    finally:
        client.close()
        if opts["output"]:
            out.close()
    return worst
