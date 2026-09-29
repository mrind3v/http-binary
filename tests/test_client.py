import os
import socket
import subprocess
import sys
import threading
import unittest

from hbin import wire
from hbin.client import parse_target
from tests.support import ServerCase

BCURL = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bcurl")


def run_bcurl(*args, timeout=15):
    return subprocess.run([sys.executable, BCURL, *args], capture_output=True, timeout=timeout)


class ParseTargetTests(unittest.TestCase):
    def test_forms(self):
        self.assertEqual(parse_target("localhost:9000/index.html"), ("localhost", 9000, "/index.html"))
        self.assertEqual(parse_target("hbin://h:1/a/b"), ("h", 1, "/a/b"))
        self.assertEqual(parse_target("h:5"), ("h", 5, "/"))
        self.assertEqual(parse_target("h/x"), ("h", 9000, "/x"))
        self.assertEqual(parse_target("[::1]:7/x"), ("::1", 7, "/x"))

    def test_bad(self):
        for bad in ("h:0/x", "h:99999/x", "h:abc/x", ":80/x", ""):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_target(bad)


class BcurlTests(ServerCase):
    def url(self, path):
        return f"127.0.0.1:{self.port}{path}"

    def test_body_to_stdout(self):
        r = run_bcurl(self.url("/index.html"))
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, b"<h1>hello</h1>\n", b""))

    def test_binary_body_intact(self):
        r = run_bcurl(self.url("/big.bin"))
        self.assertEqual((r.returncode, r.stdout), (0, self.files["big.bin"]))

    def test_404_exits_22_and_still_prints_the_body(self):
        r = run_bcurl(self.url("/missing"))
        self.assertEqual(r.returncode, 22)
        self.assertIn(b"no such file", r.stdout)
        self.assertIn(b"404", r.stderr)

    def test_verbose_dumps_every_frame_to_stderr(self):
        r = run_bcurl("-v", self.url("/index.html"))
        self.assertEqual((r.returncode, r.stdout), (0, b"<h1>hello</h1>\n"))
        err = r.stderr.decode()
        self.assertIn("> REQUEST stream=1 flags=END_STREAM", err)
        self.assertIn("GET /index.html", err)
        self.assertIn("< RESPONSE stream=1 flags=-", err)
        self.assertIn("< DATA stream=1 flags=END_STREAM len=15", err)
        # request frame header bytes: len=?, type 01, flags 01, stream 000001
        self.assertRegex(err, r"> {3}00000000  \d\d \d\d \d\d 01 01 00 00 01")
        self.assertIn("|........<h1>hell|", err)
        self.assertIn("|o</h1>.|", err)

    def test_verbose_truncates_big_frames(self):
        r = run_bcurl("-v", self.url("/big.bin"))
        self.assertEqual(r.stdout, self.files["big.bin"])
        self.assertIn("more bytes not shown", r.stderr.decode())

    def test_many_urls_share_one_connection(self):
        r = run_bcurl(self.url("/index.html"), self.url("/sub/note.txt"), self.url("/nope"))
        self.assertEqual(r.stdout, b"<h1>hello</h1>\nnested\nno such file: /nope\n")
        self.assertEqual(r.returncode, 22)  # one 404 among them
        self.assertEqual(self.connections, 1)
        streams = [line for line in self.logs if "stream=" in line]
        self.assertEqual([s.split("stream=")[1].split()[0] for s in streams], ["1", "2", "3"])

    def test_different_hosts_are_refused_before_any_connection(self):
        r = run_bcurl(self.url("/index.html"), f"127.0.0.1:{self.port + 1}/x")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(self.connections, 0)

    def test_head(self):
        r = run_bcurl("-I", "-v", self.url("/index.html"))
        self.assertEqual((r.returncode, r.stdout), (0, b""))
        self.assertIn("HEAD /index.html", r.stderr.decode())
        self.assertIn("content-length='15'", r.stderr.decode())

    def test_extra_header_and_304(self):
        etag = None
        for line in run_bcurl("-I", "-v", self.url("/index.html")).stderr.decode().splitlines():
            if "< RESPONSE" in line:
                etag = line.split("etag='")[1].split("'")[0]
        r = run_bcurl("-v", "-H", f"If-None-Match: {etag}", self.url("/index.html"))
        self.assertEqual((r.returncode, r.stdout), (0, b""))
        self.assertIn("RESPONSE stream=1 flags=END_STREAM", r.stderr.decode())
        self.assertIn("status 304", r.stderr.decode())

    def test_output_file(self):
        out = os.path.join(self.tmp.name, "got.bin")
        r = run_bcurl("-o", out, self.url("/big.bin"))
        self.assertEqual((r.returncode, r.stdout), (0, b""))
        with open(out, "rb") as f:
            self.assertEqual(f.read(), self.files["big.bin"])

    def test_unwritable_output_file_is_a_clean_error(self):
        r = run_bcurl("-o", os.path.join(self.tmp.name, "no", "such", "dir"), self.url("/index.html"))
        self.assertEqual(r.returncode, 1)
        self.assertIn(b"cannot write", r.stderr)
        self.assertNotIn(b"Traceback", r.stderr)

    def test_default_port_and_scheme_prefix_are_accepted(self):
        r = run_bcurl(f"hbin://127.0.0.1:{self.port}/index.html")
        self.assertEqual(r.stdout, b"<h1>hello</h1>\n")


class BcurlUsageTests(unittest.TestCase):
    def test_no_args(self):
        r = run_bcurl()
        self.assertEqual(r.returncode, 1)
        self.assertIn(b"usage:", r.stderr)

    def test_unknown_option(self):
        self.assertEqual(run_bcurl("-z", "h/x").returncode, 1)

    def test_connection_refused_is_exit_2(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]  # closed again on exit: nobody listens
        r = run_bcurl(f"127.0.0.1:{port}/x")
        self.assertEqual(r.returncode, 2)
        self.assertIn(b"cannot connect", r.stderr)


class FakeServer:
    """Accepts one connection and lets a test script the server's side."""

    def __init__(self, script):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.script = script
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        conn, _ = self.sock.accept()
        with conn:
            rfile = conn.makefile("rb")
            request = wire.read_frame(rfile)
            self.script(conn, request)

    def close(self):
        self.sock.close()


class BcurlAgainstOtherServersTests(unittest.TestCase):
    """The client must not depend on quirks of our own server."""

    def check(self, script):
        fake = FakeServer(script)
        self.addCleanup(fake.close)
        return run_bcurl(f"127.0.0.1:{fake.port}/x")

    def test_skips_unknown_frames_anywhere_in_the_response(self):
        def script(conn, req):
            sid = req.stream_id
            conn.sendall(wire.encode_frame(0x55, 0, 0, b"hello from v2"))
            conn.sendall(wire.encode_frame(wire.T_RESPONSE, 0, sid, wire.encode_response(200)))
            conn.sendall(wire.encode_frame(0x56, 0xFF, sid, b"?"))
            conn.sendall(wire.encode_frame(wire.T_DATA, 0, sid, b"ab"))
            conn.sendall(wire.encode_frame(wire.T_DATA, 0x81, sid, b"cd"))  # unknown flag bit
        r = self.check(script)
        self.assertEqual((r.returncode, r.stdout), (0, b"abcd"))

    def test_content_length_mismatch_is_a_protocol_error(self):
        def script(conn, req):
            hdr = wire.encode_response(200, [("content-length", "10")])
            conn.sendall(wire.encode_frame(wire.T_RESPONSE, 0, req.stream_id, hdr))
            conn.sendall(wire.encode_frame(wire.T_DATA, 1, req.stream_id, b"short"))
        self.assertEqual(self.check(script).returncode, 3)

    def test_wrong_stream_id_is_a_protocol_error(self):
        def script(conn, req):
            conn.sendall(wire.encode_frame(wire.T_RESPONSE, 1, req.stream_id + 2, wire.encode_response(200)))
        self.assertEqual(self.check(script).returncode, 3)

    def test_hangup_before_end_stream_is_a_protocol_error(self):
        def script(conn, req):
            conn.sendall(wire.encode_frame(wire.T_RESPONSE, 0, req.stream_id, wire.encode_response(200)))
        r = self.check(script)
        self.assertEqual(r.returncode, 3)
        self.assertIn(b"closed", r.stderr)

    def test_hangup_mid_frame_is_a_protocol_error(self):
        def script(conn, req):
            conn.sendall(bytes.fromhex("000010 02 00") + req.stream_id.to_bytes(3, "big") + b"ab")
        self.assertEqual(self.check(script).returncode, 3)

    def test_500_from_a_foreign_server_exits_22(self):
        def script(conn, req):
            conn.sendall(wire.encode_frame(wire.T_RESPONSE, 1, req.stream_id, wire.encode_response(500)))
        r = self.check(script)
        self.assertEqual((r.returncode, r.stdout), (22, b""))

    def test_literal_and_unknown_indexed_headers_in_response(self):
        def script(conn, req):
            hdr = (b"\x63\x00\x01z" + wire.encode_headers([("x-odd", "1"), ("content-length", "2")]))
            conn.sendall(wire.encode_frame(wire.T_RESPONSE, 0, req.stream_id,
                                           wire.encode_response(200) + hdr))
            conn.sendall(wire.encode_frame(wire.T_DATA, 1, req.stream_id, b"ok"))
        r = self.check(script)
        self.assertEqual((r.returncode, r.stdout), (0, b"ok"))


if __name__ == "__main__":
    unittest.main()
