# HBIN/1 — HTTP, in binary

One TCP connection. Length-prefixed frames. A client asks for a file by path,
a server answers with a status, some headers and the bytes.

This document is the whole protocol. The words MUST, SHOULD and MAY are used
as in RFC 2119. All integers are **unsigned, big-endian**. All text is UTF-8.

## 1. Connection

* The client opens one TCP connection and sends frames. There is **no preface
  and no handshake**; the first byte on the wire is the first byte of a frame.
* The connection stays open after a response. The client MAY send another
  request on it, and SHOULD do so instead of opening a second connection.
* v1 is strictly one request at a time: the client MUST NOT send a request until
  the previous response has ended (a frame with END_STREAM). The server
  answers in order.
* Either side may close at a frame boundary; that is a normal end. A server MAY
  close an idle connection. A connection that closes mid-frame, or mid-response
  (no END_STREAM seen), is an error and the response is incomplete.

## 2. Frame header (fixed, 8 bytes)

Every frame is this header followed by `Length` bytes of payload.

```
 byte 0   1   2 | 3    | 4     | 5   6   7
     +---------+------+-------+-----------+
     | Length  | Type | Flags | Stream ID |
     |  (24)   | (8)  |  (8)  |   (24)    |
     +---------+------+-------+-----------+
```

| Field     | Bits | Meaning                                                    |
|-----------|------|------------------------------------------------------------|
| Length    | 24   | Payload size in bytes, header not included.                |
| Type      | 8    | What the payload is (section 3).                           |
| Flags     | 8    | Bit 0 (`0x01`) = END_STREAM. Bits 1–7 are undefined.       |
| Stream ID | 24   | Names one request/response exchange. `0` is reserved.      |

**Why these widths** (HTTP/2 chose 24 / 8 / 8 / 31; here is what changes and why):

* **8 bytes, no reserved bits.** Two 32-bit words; a reader does one `read(8)`.
  HTTP/2 spends one bit as "reserved" inside a 31-bit ID and ends up at 9 bytes.
  We take a 24-bit ID (16 M exchanges per connection, far beyond one-at-a-time
  use) and keep the header a power of two.
* **Length is 24 bits although the limit is 16384.** The limit (section 6) is
  policy, the field is format. 16 bits would freeze the ceiling at 64 KiB
  forever; 24 bits lets a v2 raise the limit to 16 MiB with no new header.
* **Type is 8 bits.** Three types are used; 253 are left for extension.
* **Flags are 8 bits, one is used.** A sender MUST set undefined flags to 0; a
  receiver MUST ignore them. That is how a flag can mean something in v2.
* **Stream ID** is chosen by the client, starts at 1 and increases by 1 per
  request, and is echoed in every frame of the response. It lets the client
  check that a response belongs to its request, and is the hook for
  multiplexing later. `0` is reserved for future connection-level frames.

## 3. Frame types

| Type | Name     | Direction        | Payload                                    |
|------|----------|------------------|--------------------------------------------|
| 0x01 | REQUEST  | client → server  | `method:u8` `path_len:u16` `path` `headers` |
| 0x02 | RESPONSE | server → client  | `status:u16` `headers`                     |
| 0x03 | DATA     | server → client  | body bytes (raw)                           |
| other| —        | either           | reserved for later versions                |

**Unknown types.** A receiver that meets a frame type it does not know MUST
read and discard exactly `Length` bytes, send nothing back, and carry on with
the next frame. Because every frame has the same 8-byte header, this is always
possible. It is what lets a version 2 add frame types that a version 1 peer
survives. (It applies on both ends, at any point, even between the frames of a
response. The 24-bit `Length` of an unknown frame is still bound by section 6.)

**Methods:** `1` = GET, `2` = HEAD. Others (including `0`) are unsupported.
**Path:** starts with `/`, contains no NUL byte, is not percent-encoded.

## 4. Header block

The `headers` field of REQUEST and RESPONSE is the rest of the payload: zero or
more entries, back to back, until the payload ends. There is no count.

```
indexed:  id:u8 (1..255)   value_len:u16  value
literal:  0x00  name_len:u8 (1..255)  name  value_len:u16  value
```

The ten names this protocol actually uses are numbered; anything else is a
literal with its name spelled out (lower-case ASCII).

| ID | Name            | ID | Name             |
|----|-----------------|----|------------------|
| 1  | host            | 6  | content-type     |
| 2  | user-agent      | 7  | content-length   |
| 3  | accept          | 8  | last-modified    |
| 4  | if-none-match   | 9  | etag             |
| 5  | server          | 10 | allow            |

IDs 11–255 are reserved. Because every indexed entry has the same shape, a
receiver that meets an ID it does not know MUST skip that entry (`value_len` +
`value`) and go on. Unknown literal names are ignored the same way. An entry
that runs past the end of the payload makes the frame malformed. Names are
compared case-insensitively.

## 5. Exchange

**Request.** One REQUEST frame with END_STREAM set (v1 has no request body).
A client SHOULD send `host` (`host:port` as typed), `user-agent` and `accept`.

**Response.** One RESPONSE frame, then zero or more DATA frames, all with the
request's Stream ID. END_STREAM is set on the last of them: on RESPONSE itself if
there is no body (HEAD, 304, or an empty file), otherwise on the last DATA. A body
is split into DATA frames of at most 16384 bytes. `content-length` is the total
body size; for a HEAD response it is the size a GET would return, and no DATA
follows.

**Path → file.** The server has a root directory. It drops the leading `/`; a
path that is empty or ends in `/` gets `index.html` appended. The result is
looked up under the root. Only regular files are served. Any path that would
resolve outside the root (`..`, symlinks out) is treated as absent.

| Status | When                                                                          |
|--------|-------------------------------------------------------------------------------|
| 200    | File found. Headers: `server`, `content-type`, `content-length`, `last-modified`, `etag`. |
| 304    | `if-none-match` equals the file's `etag`. No body.                            |
| 400    | Frame is well-formed but not a valid request (below).                         |
| 404    | No such regular file under the root.                                          |
| 405    | Method is not 1 or 2. Adds `allow: GET, HEAD`.                                |
| 500    | The server failed for a reason of its own.                                    |

Status values are the HTTP numbers. Error responses carry a short
`text/plain` body explaining the problem.

**400 — malformed.** The server sends 400 (Stream ID copied from the bad frame)
**and keeps the connection open**, when a frame is complete but: a known type that
is not valid in that direction (RESPONSE or DATA from a client); a REQUEST with
Stream ID 0 or without END_STREAM; a payload too short for its fixed fields;
a header entry that runs off the end; a path that does not start with `/`, holds
a NUL, is longer than 8192 bytes, or is not UTF-8.

The one exception is a frame whose `Length` exceeds the limit (section 6): the
server cannot tell where the next frame starts without reading it, so it sends
400 and closes.

## 6. Limits and extension rules

* **Max payload: 16384 bytes**, for every frame, in both directions. A header
  block therefore fits in one frame; there are no continuation frames.
* A **path** is at most 8192 bytes; a header **value** at most 65535.
* v1 has no version number on the wire. A future version announces itself with
  frames v1 ignores (section 3), new header IDs (section 4) and new flags
  (section 2). A v1 peer needs no change to survive any of these.

A worked example with every byte accounted for is in
[`docs/HEXDUMP.md`](docs/HEXDUMP.md).
