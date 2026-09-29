import io
import unittest

from hbin import wire
from hbin.hexdump import hexdump


class FrameTests(unittest.TestCase):
    def test_header_layout(self):
        raw = wire.encode_frame(wire.T_DATA, wire.F_END_STREAM, 0x010203, b"abcd")
        self.assertEqual(raw, bytes.fromhex("000004 03 01 010203") + b"abcd")

    def test_roundtrip(self):
        frame = wire.Frame(wire.T_RESPONSE, 0, 7, b"payload")
        got = wire.read_frame(io.BytesIO(frame.encode()))
        self.assertEqual(got, frame)

    def test_clean_eof_is_none(self):
        self.assertIsNone(wire.read_frame(io.BytesIO(b"")))

    def test_truncated_header_and_payload(self):
        with self.assertRaises(wire.Truncated):
            wire.read_frame(io.BytesIO(b"\x00\x00\x01"))
        with self.assertRaises(wire.Truncated):
            wire.read_frame(io.BytesIO(bytes.fromhex("000005 03 00 000001") + b"ab"))

    def test_oversize_reports_stream_id_without_reading_payload(self):
        stream = io.BytesIO(bytes.fromhex("004001 03 00 000009") + b"x" * 10)
        with self.assertRaises(wire.FrameTooLarge) as ctx:
            wire.read_frame(stream)
        self.assertEqual(ctx.exception.stream_id, 9)
        self.assertEqual(stream.tell(), wire.FRAME_HEADER_LEN)

    def test_max_payload_is_allowed(self):
        raw = wire.encode_frame(wire.T_DATA, 0, 1, b"x" * wire.MAX_PAYLOAD)
        self.assertEqual(len(wire.read_frame(io.BytesIO(raw)).payload), wire.MAX_PAYLOAD)

    def test_encode_rejects_bad_values(self):
        with self.assertRaises(ValueError):
            wire.encode_frame(wire.T_DATA, 0, 1, b"x" * (wire.MAX_PAYLOAD + 1))
        with self.assertRaises(ValueError):
            wire.encode_frame(wire.T_DATA, 0, 1 << 24)

    def test_zero_length_frame(self):
        raw = wire.encode_frame(0x99, 0, 0)
        self.assertEqual(wire.read_frame(io.BytesIO(raw)), wire.Frame(0x99, 0, 0))


class HeaderTests(unittest.TestCase):
    def test_indexed_entry(self):
        self.assertEqual(wire.encode_headers([("host", "a")]), b"\x01\x00\x01a")

    def test_case_folding_and_literal(self):
        buf = wire.encode_headers([("X-Thing", "v")])
        self.assertEqual(buf, b"\x00\x07x-thing\x00\x01v")
        self.assertEqual(wire.decode_headers(buf), [("x-thing", "v")])

    def test_all_ten_names_are_indexed_in_order(self):
        self.assertEqual(len(wire.HEADER_NAMES), 10)
        for i, name in enumerate(wire.HEADER_NAMES, start=1):
            self.assertEqual(wire.encode_headers([(name, "")])[0], i)

    def test_roundtrip_unicode_and_empty(self):
        headers = [("etag", '"é"'), ("allow", ""), ("x-y", "z")]
        self.assertEqual(wire.decode_headers(wire.encode_headers(headers)), headers)

    def test_unknown_indexed_id_is_skipped(self):
        buf = b"\x2a\x00\x03xyz" + wire.encode_headers([("host", "h")])
        self.assertEqual(wire.decode_headers(buf), [("host", "h")])

    def test_malformed(self):
        for bad in (
            b"\x01",                      # no value length
            b"\x01\x00\x05ab",            # value runs off the end
            b"\x00",                      # literal without name length
            b"\x00\x00\x00\x00",          # empty literal name
            b"\x00\x03ab",                # name runs off the end
            b"\x01\x00\x01\xff",          # value not UTF-8
            b"\x00\x02\xc3\xa9\x00\x00",  # name not ASCII
        ):
            with self.subTest(bad=bad), self.assertRaises(wire.Malformed):
                wire.decode_headers(bad)

    def test_value_too_long(self):
        with self.assertRaises(ValueError):
            wire.encode_headers([("host", "x" * 0x10000)])


class PayloadTests(unittest.TestCase):
    def test_request_roundtrip(self):
        payload = wire.encode_request(wire.M_GET, "/a/b", [("host", "h")])
        self.assertEqual(wire.decode_request(payload),
                         (wire.M_GET, "/a/b", [("host", "h")]))

    def test_request_layout(self):
        self.assertEqual(wire.encode_request(1, "/x"), bytes.fromhex("01 0002") + b"/x")

    def test_request_malformed(self):
        for bad in (
            b"",
            b"\x01\x00",
            b"\x01\x00\x05/ab",              # path runs off the end
            b"\x01\x00\x02ab",               # no leading slash
            b"\x01\x00\x02/\x00",            # NUL
            b"\x01\x00\x02/\xff",            # not UTF-8
            b"\x01\x20\x01" + b"/" * 8193,   # over MAX_PATH
            b"\x01\x00\x01/\x01",            # bad header block
        ):
            with self.subTest(bad=bad[:12]), self.assertRaises(wire.Malformed):
                wire.decode_request(bad)

    def test_response_roundtrip(self):
        payload = wire.encode_response(200, [("content-length", "5")])
        self.assertEqual(payload[:2], b"\x00\xc8")
        self.assertEqual(wire.decode_response(payload), (200, [("content-length", "5")]))

    def test_response_too_short(self):
        with self.assertRaises(wire.Malformed):
            wire.decode_response(b"\x00")


class HexdumpTests(unittest.TestCase):
    def test_format(self):
        lines = hexdump(bytes(range(0x41, 0x41 + 17)))
        self.assertEqual(lines[0], "00000000  41 42 43 44 45 46 47 48  49 4a 4b 4c 4d 4e 4f 50  |ABCDEFGHIJKLMNOP|")
        self.assertTrue(lines[1].startswith("00000010  51 "))
        self.assertTrue(lines[1].endswith("  |Q|"))
        self.assertEqual(len(lines[1]), len(lines[0]) - 15)

    def test_truncation_note(self):
        lines = hexdump(b"x" * 40, limit=16)
        self.assertEqual(len(lines), 2)
        self.assertIn("24 more bytes", lines[1])


if __name__ == "__main__":
    unittest.main()
