"""hexdump -C style formatting, used by `bcurl -v`."""


def hexdump(data, limit=None):
    """Return lines like `00000000  01 02 ...  |..|`. Truncates after `limit` bytes."""
    shown = data if limit is None else data[:limit]
    lines = []
    for off in range(0, len(shown), 16):
        chunk = shown[off:off + 16]
        left = " ".join(f"{b:02x}" for b in chunk[:8])
        right = " ".join(f"{b:02x}" for b in chunk[8:])
        text = "".join(chr(b) if 0x20 <= b < 0x7F else "." for b in chunk)
        lines.append(f"{off:08x}  {left:<23}  {right:<23}  |{text}|")
    if len(shown) < len(data):
        lines.append(f"... {len(data) - len(shown)} more bytes not shown")
    return lines
