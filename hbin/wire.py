"""HBIN/1 wire format: frames, header blocks, REQUEST and RESPONSE payloads.

Everything here is pure bytes-in, bytes-out (plus read_frame on a file-like
object). No sockets, no filesystem. Section numbers refer to SPEC.md.
"""

from dataclasses import dataclass

FRAME_HEADER_LEN = 8
MAX_PAYLOAD = 16384
MAX_PATH = 8192
MAX_VALUE = 0xFFFF
MAX_STREAM_ID = 0xFFFFFF

T_REQUEST = 0x01
T_RESPONSE = 0x02
T_DATA = 0x03
KNOWN_TYPES = (T_REQUEST, T_RESPONSE, T_DATA)

F_END_STREAM = 0x01

M_GET = 1
M_HEAD = 2
METHOD_NAMES = {M_GET: "GET", M_HEAD: "HEAD"}

# Section 4: the ten names we actually send, numbered from 1.
HEADER_NAMES = (
    "host", "user-agent", "accept", "if-none-match", "server",
    "content-type", "content-length", "last-modified", "etag", "allow",
)
_NAME_TO_ID = {name: i for i, name in enumerate(HEADER_NAMES, start=1)}
_ID_TO_NAME = {i: name for name, i in _NAME_TO_ID.items()}


class WireError(Exception):
    """Base class for everything that can go wrong decoding bytes."""


class Truncated(WireError):
    """The connection ended in the middle of a frame."""


class FrameTooLarge(WireError):
    """A frame header announced more than MAX_PAYLOAD bytes."""

    def __init__(self, length, stream_id):
        super().__init__(f"frame length {length} exceeds limit {MAX_PAYLOAD}")
        self.length = length
        self.stream_id = stream_id


class Malformed(WireError):
    """A complete frame whose payload is not valid (server answers 400)."""


@dataclass(frozen=True)
class Frame:
    type: int
    flags: int
    stream_id: int
    payload: bytes = b""

    @property
    def end_stream(self):
        return bool(self.flags & F_END_STREAM)

    def encode(self):
        return encode_frame(self.type, self.flags, self.stream_id, self.payload)


# --- frames (section 2) -----------------------------------------------------

def encode_frame(type_, flags, stream_id, payload=b""):
    if len(payload) > MAX_PAYLOAD:
        raise ValueError(f"payload of {len(payload)} bytes exceeds {MAX_PAYLOAD}")
    if not 0 <= stream_id <= MAX_STREAM_ID:
        raise ValueError(f"stream id {stream_id} does not fit in 24 bits")
    return (
        len(payload).to_bytes(3, "big")
        + bytes([type_, flags])
        + stream_id.to_bytes(3, "big")
        + payload
    )


def _read_exactly(rfile, n):
    data = rfile.read(n)
    if data is None or len(data) != n:
        raise Truncated(f"wanted {n} bytes, got {0 if data is None else len(data)}")
    return data


def read_frame(rfile):
    """Read one frame. Returns None on a clean EOF at a frame boundary.

    Raises Truncated if the stream ends mid-frame, FrameTooLarge if the header
    announces more than MAX_PAYLOAD (nothing past the header is consumed).
    """
    head = rfile.read(FRAME_HEADER_LEN)
    if not head:
        return None
    if len(head) != FRAME_HEADER_LEN:
        raise Truncated(f"frame header cut short after {len(head)} bytes")
    length = int.from_bytes(head[0:3], "big")
    stream_id = int.from_bytes(head[5:8], "big")
    if length > MAX_PAYLOAD:
        raise FrameTooLarge(length, stream_id)
    payload = _read_exactly(rfile, length) if length else b""
    return Frame(head[3], head[4], stream_id, payload)


# --- header block (section 4) ----------------------------------------------

def encode_headers(headers):
    out = bytearray()
    for name, value in headers:
        name = name.lower()
        raw = value.encode("utf-8")
        if len(raw) > MAX_VALUE:
            raise ValueError(f"value of {name!r} is {len(raw)} bytes")
        index = _NAME_TO_ID.get(name)
        if index is not None:
            out.append(index)
        else:
            raw_name = name.encode("ascii")
            if not 1 <= len(raw_name) <= 255:
                raise ValueError(f"bad header name {name!r}")
            out.append(0)
            out.append(len(raw_name))
            out += raw_name
        out += len(raw).to_bytes(2, "big")
        out += raw
    return bytes(out)


def decode_headers(buf):
    """Return [(name, value)]. Unknown indexed entries are skipped (4)."""
    headers = []
    pos = 0
    end = len(buf)

    def take(n):
        nonlocal pos
        if pos + n > end:
            raise Malformed("header entry runs past the end of the payload")
        chunk = buf[pos:pos + n]
        pos += n
        return chunk

    while pos < end:
        index = take(1)[0]
        if index == 0:
            name_len = take(1)[0]
            if name_len == 0:
                raise Malformed("literal header with empty name")
            try:
                name = take(name_len).decode("ascii").lower()
            except UnicodeDecodeError:
                raise Malformed("header name is not ASCII") from None
        else:
            name = _ID_TO_NAME.get(index)  # None: reserved id, skip below
        value = take(int.from_bytes(take(2), "big"))
        if name is None:
            continue
        try:
            headers.append((name, value.decode("utf-8")))
        except UnicodeDecodeError:
            raise Malformed(f"value of {name!r} is not UTF-8") from None
    return headers


# --- REQUEST / RESPONSE payloads (section 3) --------------------------------

def encode_request(method, path, headers=()):
    raw = path.encode("utf-8")
    if len(raw) > MAX_PATH:
        raise ValueError("path too long")
    return (bytes([method]) + len(raw).to_bytes(2, "big") + raw
            + encode_headers(headers))


def decode_request(payload):
    """Return (method, path, headers). Raises Malformed."""
    if len(payload) < 3:
        raise Malformed("REQUEST payload shorter than its fixed fields")
    method = payload[0]
    path_len = int.from_bytes(payload[1:3], "big")
    if path_len > MAX_PATH:
        raise Malformed("path too long")
    if 3 + path_len > len(payload):
        raise Malformed("path runs past the end of the payload")
    try:
        path = payload[3:3 + path_len].decode("utf-8")
    except UnicodeDecodeError:
        raise Malformed("path is not UTF-8") from None
    if not path.startswith("/"):
        raise Malformed("path does not start with '/'")
    if "\0" in path:
        raise Malformed("path contains NUL")
    return method, path, decode_headers(payload[3 + path_len:])


def encode_response(status, headers=()):
    return status.to_bytes(2, "big") + encode_headers(headers)


def decode_response(payload):
    """Return (status, headers). Raises Malformed."""
    if len(payload) < 2:
        raise Malformed("RESPONSE payload shorter than its fixed fields")
    return int.from_bytes(payload[:2], "big"), decode_headers(payload[2:])
