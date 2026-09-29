import os
import unittest

from hbin import wire
from tests.support import ServerCase


class ServeTests(ServerCase):
    def test_get_file(self):
        c = self.connect()
        c.request(1, "/index.html")
        status, headers, body, frames = c.exchange()
        self.assertEqual((status, body), (200, b"<h1>hello</h1>\n"))
        self.assertEqual(headers["content-length"], "15")
        self.assertEqual(headers["content-type"], "text/html; charset=utf-8")
        self.assertEqual(headers["server"], "bserve/1")
        self.assertIn("etag", headers)
        self.assertEqual(headers["last-modified"], "Tue, 14 Nov 2023 22:13:20 GMT")
        self.assertTrue(all(f.stream_id == 1 for f in frames))
        self.assertEqual([f.type for f in frames], [wire.T_RESPONSE, wire.T_DATA])
        self.assertEqual([f.end_stream for f in frames], [False, True])

    def test_root_and_directory_paths_serve_index(self):
        c = self.connect()
        c.request(1, "/")
        self.assertEqual(c.exchange()[2], b"<h1>hello</h1>\n")

    def test_nested_file(self):
        c = self.connect()
        c.request(1, "/sub/note.txt")
        status, headers, body, _ = c.exchange()
        self.assertEqual((status, body), (200, b"nested\n"))
        self.assertEqual(headers["content-type"], "text/plain; charset=utf-8")

    def test_large_file_is_chunked_at_max_payload(self):
        c = self.connect()
        c.request(1, "/big.bin")
        status, headers, body, frames = c.exchange()
        self.assertEqual(body, self.files["big.bin"])
        self.assertEqual(headers["content-length"], "51200")
        sizes = [len(f.payload) for f in frames if f.type == wire.T_DATA]
        self.assertEqual(sizes, [16384, 16384, 16384, 2048])
        self.assertEqual(headers["content-type"], "application/octet-stream")

    def test_empty_file_ends_on_the_response_frame(self):
        c = self.connect()
        c.request(1, "/empty.txt")
        status, headers, body, frames = c.exchange()
        self.assertEqual((status, body, len(frames)), (200, b"", 1))
        self.assertTrue(frames[0].end_stream)

    def test_head_has_headers_but_no_data(self):
        c = self.connect()
        c.request(1, "/index.html", method=wire.M_HEAD)
        status, headers, body, frames = c.exchange()
        self.assertEqual((status, body, len(frames)), (200, b"", 1))
        self.assertEqual(headers["content-length"], "15")

    def test_if_none_match(self):
        c = self.connect()
        c.request(1, "/index.html")
        etag = c.exchange()[1]["etag"]
        c.request(3, "/index.html", headers=[("if-none-match", etag)])
        status, headers, body, frames = c.exchange()
        self.assertEqual((status, body, len(frames)), (304, b"", 1))
        self.assertEqual(headers["etag"], etag)
        c.request(5, "/index.html", headers=[("if-none-match", '"nope"')])
        self.assertEqual(c.exchange()[0], 200)

    def test_unknown_request_headers_are_ignored(self):
        c = self.connect()
        payload = (bytes([1]) + (11).to_bytes(2, "big") + b"/index.html"
                   + b"\x2a\x00\x02zz" + wire.encode_headers([("x-what", "ever")]))
        c.send(wire.encode_frame(wire.T_REQUEST, wire.F_END_STREAM, 1, payload))
        self.assertEqual(c.exchange()[0], 200)


class NotFoundTests(ServerCase):
    def assert_404(self, path):
        c = self.connect()
        c.request(1, path)
        status, headers, body, _ = c.exchange()
        self.assertEqual(status, 404, path)
        self.assertEqual(headers["content-type"], "text/plain; charset=utf-8")
        self.assertEqual(int(headers["content-length"]), len(body))

    def test_missing(self):
        self.assert_404("/nope.html")

    def test_directory_without_index(self):
        self.assert_404("/sub/")
        self.assert_404("/sub")

    def test_traversal(self):
        self.assert_404("/../secret.txt")
        self.assert_404("/sub/../../secret.txt")
        self.assert_404("//../secret.txt")

    def test_symlink_out_of_root(self):
        os.symlink(os.path.join(self.tmp.name, "secret.txt"), os.path.join(self.root, "link.txt"))
        self.assert_404("/link.txt")

    def test_absent_is_not_an_error_for_the_connection(self):
        c = self.connect()
        c.request(1, "/nope")
        self.assertEqual(c.exchange()[0], 404)
        c.request(2, "/index.html")
        self.assertEqual(c.exchange()[0], 200)


class KeepAliveTests(ServerCase):
    def test_many_requests_one_connection(self):
        c = self.connect()
        for sid, path, want in [(1, "/index.html", 200), (2, "/nope", 404),
                                (3, "/sub/note.txt", 200), (4, "/big.bin", 200)]:
            c.request(sid, path)
            status, _, _, frames = c.exchange()
            self.assertEqual(status, want)
            self.assertEqual({f.stream_id for f in frames}, {sid})
        self.assertEqual(self.connections, 1)

    def test_clean_close_by_client_is_quiet(self):
        c = self.connect()
        c.request(1, "/index.html")
        c.exchange()
        c.sock.close()


class MalformedTests(ServerCase):
    def assert_400_then_alive(self, raw, sid, needle=None):
        c = self.connect()
        c.send(raw)
        status, _, body, frames = c.exchange()
        self.assertEqual(status, 400, body)
        if needle:
            self.assertIn(needle.encode(), body)
        self.assertEqual({f.stream_id for f in frames}, {sid})
        # ...and the connection is still usable:
        c.request(99, "/index.html")
        status, _, body, frames = c.exchange()
        self.assertEqual((status, body), (200, b"<h1>hello</h1>\n"))
        return c

    def test_short_payload(self):
        self.assert_400_then_alive(wire.encode_frame(wire.T_REQUEST, 1, 5, b"\x01"), 5)

    def test_path_without_slash(self):
        payload = wire.encode_request(1, "/x")[:3] + b"xx"
        self.assert_400_then_alive(wire.encode_frame(wire.T_REQUEST, 1, 5, payload), 5, "/")

    def test_header_block_runs_off_end(self):
        payload = wire.encode_request(1, "/index.html") + b"\x01\x00\x09ab"
        self.assert_400_then_alive(wire.encode_frame(wire.T_REQUEST, 1, 5, payload), 5)

    def test_stream_id_zero(self):
        self.assert_400_then_alive(
            wire.encode_frame(wire.T_REQUEST, 1, 0, wire.encode_request(1, "/index.html")), 0)

    def test_missing_end_stream(self):
        self.assert_400_then_alive(
            wire.encode_frame(wire.T_REQUEST, 0, 5, wire.encode_request(1, "/index.html")),
            5, "END_STREAM")

    def test_response_frame_from_client(self):
        self.assert_400_then_alive(wire.encode_frame(wire.T_RESPONSE, 1, 5, b"\x00\xc8"), 5)

    def test_data_frame_from_client(self):
        self.assert_400_then_alive(wire.encode_frame(wire.T_DATA, 1, 5, b"hi"), 5)

    def test_response_carries_the_bad_frames_stream_id(self):
        c = self.connect()
        c.send(wire.encode_frame(wire.T_REQUEST, 1, 0x123456, b""))
        _, _, _, frames = c.exchange()
        self.assertEqual({f.stream_id for f in frames}, {0x123456})

    def test_unsupported_method_is_405_with_allow(self):
        c = self.connect()
        c.request(1, "/index.html", method=7)
        status, headers, _, _ = c.exchange()
        self.assertEqual((status, headers["allow"]), (405, "GET, HEAD"))
        c.request(3, "/index.html")
        self.assertEqual(c.exchange()[0], 200)

    def test_oversize_frame_gets_400_then_close(self):
        c = self.connect()
        c.send(bytes.fromhex("004001 01 01 000007"))
        status, _, _, frames = c.exchange()
        self.assertEqual((status, frames[0].stream_id), (400, 7))
        self.assertTrue(c.at_eof())

    def test_garbage_that_looks_like_http1_is_refused(self):
        c = self.connect()
        c.send(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
        self.assertEqual(c.exchange()[0], 400)
        self.assertTrue(c.at_eof())

    def test_truncated_frame_then_close_does_not_wedge_the_server(self):
        c = self.connect()
        c.send(bytes.fromhex("000010 01 01 000001") + b"abc")
        c.sock.close()
        c2 = self.connect()
        c2.request(1, "/index.html")
        self.assertEqual(c2.exchange()[0], 200)


class ExtensionTests(ServerCase):
    def test_unknown_frame_types_are_skipped_silently(self):
        c = self.connect()
        c.send(wire.encode_frame(0x7F, 0xFF, 0, b"from the future"))
        c.send(wire.encode_frame(0x04, 0, 3, b""))
        c.request(1, "/index.html")
        status, _, body, frames = c.exchange()
        self.assertEqual((status, body), (200, b"<h1>hello</h1>\n"))
        self.assertEqual([f.type for f in frames], [wire.T_RESPONSE, wire.T_DATA])

    def test_unknown_flags_are_ignored(self):
        c = self.connect()
        c.request(1, "/index.html", flags=wire.F_END_STREAM | 0xFE)
        self.assertEqual(c.exchange()[0], 200)

    def test_unknown_frame_between_requests(self):
        c = self.connect()
        c.request(1, "/index.html")
        c.exchange()
        c.send(wire.encode_frame(0xEE, 0, 1, b"x" * 1000))
        c.request(2, "/sub/note.txt")
        self.assertEqual(c.exchange()[2], b"nested\n")


if __name__ == "__main__":
    unittest.main()
