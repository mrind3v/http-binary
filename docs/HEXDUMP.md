# One request, one response, every byte

`GET /index.html` against a server whose root holds one 15-byte file,
`<h1>hello</h1>\n`, last modified `2023-11-14 22:13:20 UTC`. The client typed
`localhost:9000/index.html`. Three frames cross the wire, 186 bytes in all.

Reproduce: `python3 -m tools.capture_example`. `tests/test_hexdump_doc.py`
fails if the dumps below stop matching what the programs really send.
Offsets in the tables are decimal byte positions inside the frame.

All integers are big-endian (SPEC.md, top).

## 1. Client → server: REQUEST (55 bytes)

```
00000000  00 00 2f 01 01 00 00 01  01 00 0b 2f 69 6e 64 65  |../......../inde|
00000010  78 2e 68 74 6d 6c 01 00  0e 6c 6f 63 61 6c 68 6f  |x.html...localho|
00000020  73 74 3a 39 30 30 30 02  00 07 62 63 75 72 6c 2f  |st:9000...bcurl/|
00000030  31 03 00 03 2a 2f 2a                              |1...*/*|
```

| Bytes | Hex                        | Meaning                                                    |
|-------|----------------------------|------------------------------------------------------------|
| 0–2   | `00 00 2f`                 | Length = 47: the payload, not counting these 8 bytes       |
| 3     | `01`                       | Type = REQUEST                                             |
| 4     | `01`                       | Flags = END_STREAM (a request has no body)                 |
| 5–7   | `00 00 01`                 | Stream ID = 1, the client's first request                  |
| 8     | `01`                       | Method = GET                                               |
| 9–10  | `00 0b`                    | Path length = 11                                           |
| 11–21 | `2f 69 6e 64 65 78 2e 68 74 6d 6c` | Path = `/index.html`                               |
| 22    | `01`                       | Header id 1 = `host` (indexed, so the name costs 1 byte)   |
| 23–24 | `00 0e`                    | Value length = 14                                          |
| 25–38 | `6c 6f 63 61 6c 68 6f 73 74 3a 39 30 30 30` | Value = `localhost:9000`                  |
| 39    | `02`                       | Header id 2 = `user-agent`                                 |
| 40–41 | `00 07`                    | Value length = 7                                           |
| 42–48 | `62 63 75 72 6c 2f 31`     | Value = `bcurl/1`                                          |
| 49    | `03`                       | Header id 3 = `accept`                                     |
| 50–51 | `00 03`                    | Value length = 3                                           |
| 52–54 | `2a 2f 2a`                 | Value = `*/*`                                              |

Check: 8 header bytes + 47 payload bytes = 55. The header block has no count
and no terminator; it simply runs to the end of the payload (byte 54).

## 2. Server → client: RESPONSE (108 bytes)

```
00000000  00 00 64 02 00 00 00 01  00 c8 05 00 08 62 73 65  |..d..........bse|
00000010  72 76 65 2f 31 06 00 18  74 65 78 74 2f 68 74 6d  |rve/1...text/htm|
00000020  6c 3b 20 63 68 61 72 73  65 74 3d 75 74 66 2d 38  |l; charset=utf-8|
00000030  07 00 02 31 35 08 00 1d  54 75 65 2c 20 31 34 20  |...15...Tue, 14 |
00000040  4e 6f 76 20 32 30 32 33  20 32 32 3a 31 33 3a 32  |Nov 2023 22:13:2|
00000050  30 20 47 4d 54 09 00 14  22 66 2d 31 37 39 37 39  |0 GMT..."f-17979|
00000060  63 66 65 33 36 32 61 30  30 30 30 22              |cfe362a0000"|
```

| Bytes  | Hex                    | Meaning                                                     |
|--------|------------------------|-------------------------------------------------------------|
| 0–2    | `00 00 64`             | Length = 100                                                |
| 3      | `02`                   | Type = RESPONSE                                             |
| 4      | `00`                   | Flags = none. **Not** END_STREAM: DATA follows              |
| 5–7    | `00 00 01`             | Stream ID = 1, copied from the request                      |
| 8–9    | `00 c8`                | Status = 200                                                |
| 10     | `05`                   | Header id 5 = `server`                                      |
| 11–12  | `00 08`                | Value length = 8                                            |
| 13–20  | `62 73 65 72 76 65 2f 31` | Value = `bserve/1`                                       |
| 21     | `06`                   | Header id 6 = `content-type`                                |
| 22–23  | `00 18`                | Value length = 24                                           |
| 24–47  | `74 65 78 74 2f 68 …`  | Value = `text/html; charset=utf-8`                          |
| 48     | `07`                   | Header id 7 = `content-length`                              |
| 49–50  | `00 02`                | Value length = 2                                            |
| 51–52  | `31 35`                | Value = `15` (ASCII text, not a binary integer)             |
| 53     | `08`                   | Header id 8 = `last-modified`                               |
| 54–55  | `00 1d`                | Value length = 29                                           |
| 56–84  | `54 75 65 2c 20 …`     | Value = `Tue, 14 Nov 2023 22:13:20 GMT`                     |
| 85     | `09`                   | Header id 9 = `etag`                                        |
| 86–87  | `00 14`                | Value length = 20                                           |
| 88–107 | `22 66 2d 31 37 39 …`  | Value = `"f-17979cfe362a0000"` (size in hex, mtime in ns, quotes included) |

Check: 8 + 2 (status) + 11 + 27 + 5 + 32 + 23 = 108. All five headers are
indexed, so no header name is spelled out anywhere in this frame.

## 3. Server → client: DATA (23 bytes)

```
00000000  00 00 0f 03 01 00 00 01  3c 68 31 3e 68 65 6c 6c  |........<h1>hell|
00000010  6f 3c 2f 68 31 3e 0a                              |o</h1>.|
```

| Bytes | Hex                                        | Meaning                            |
|-------|--------------------------------------------|------------------------------------|
| 0–2   | `00 00 0f`                                 | Length = 15                        |
| 3     | `03`                                       | Type = DATA                        |
| 4     | `01`                                       | Flags = END_STREAM: response over  |
| 5–7   | `00 00 01`                                 | Stream ID = 1                      |
| 8–22  | `3c 68 31 3e 68 65 6c 6c 6f 3c 2f 68 31 3e 0a` | Body = `<h1>hello</h1>\n`      |

The client counts 15 body bytes, which equals `content-length`, and it saw
END_STREAM, so the exchange is complete. The connection stays open; the next
request would use Stream ID 2.

## 4. The rule you may not skip

Suppose a future server slips this frame in before the RESPONSE (type `0x55`,
5 bytes, stream 0), shown here as raw hex:

```text
00 00 05 55 00 00 00 00 68 65 6c 6c 6f
```

A v1 client reads the header (`Length` = 5, type `0x55` unknown), discards
exactly 5 bytes and moves on to the next 8-byte header. It sends nothing back
and prints nothing. This is why every frame, known or not, begins with the same
8 bytes: a receiver never needs to understand a frame in order to step over it.
