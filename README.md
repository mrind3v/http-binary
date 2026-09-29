# http-binary — HBIN/1

HTTP-style file fetching over a hand-designed **binary** protocol. Two tracks,
one protocol; the only thing that crosses between them is the spec.

| What                    | Where                                  |
|-------------------------|----------------------------------------|
| The spec                | [`SPEC.md`](SPEC.md)                   |
| Annotated hexdump       | [`docs/HEXDUMP.md`](docs/HEXDUMP.md)   |
| Track 1 — the server    | `./bserve` → `hbin/server.py`          |
| Track 2 — the client    | `./bcurl` → `hbin/client.py`           |
| Frame / header codec    | `hbin/wire.py`                         |

Pure Python 3 standard library; no install step.

```
$ ./bserve ./examples/www 9000          # serve a directory
$ ./bcurl localhost:9000/index.html     # body to stdout
$ ./bcurl -v localhost:9000/index.html  # + hexdump of every frame, on stderr
$ ./bcurl localhost:9000/a localhost:9000/b   # two requests, ONE connection
$ ./bcurl -I localhost:9000/index.html  # HEAD
$ ./bcurl -H 'if-none-match: "f-…"' …   # extra header (see the 304 path)
```

`bcurl` exits `0` on success, `22` on a 4xx/5xx answer, `1` on bad usage,
`2` on network errors and `3` on protocol errors. `bserve` binds to
`127.0.0.1` unless given `--bind ADDR`.

## Tests

```
python3 -m unittest discover -s tests -t .
```

The server and client are tested against hand-built frames (not just against
each other), and `tests/test_hexdump_doc.py` fails if `docs/HEXDUMP.md` ever
stops matching the real bytes. `python3 -m tools.capture_example` prints them.
