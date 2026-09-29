"""docs/HEXDUMP.md must describe the bytes the programs really produce."""

import os
import re
import unittest

from tools.capture_example import capture

DOC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs", "HEXDUMP.md")
LINE = re.compile(r"^[0-9a-f]{8}  ")


def dumps_in(text):
    """Yield the bytes of each run of consecutive hexdump lines in the doc."""
    current = None
    for line in text.splitlines() + [""]:
        if LINE.match(line):
            current = (current or bytearray()) + bytes.fromhex(line[10:58].replace(" ", ""))
        elif current is not None:
            yield bytes(current)
            current = None


class HexdumpDocTests(unittest.TestCase):
    def test_doc_bytes_match_a_real_exchange(self):
        with open(DOC, encoding="utf-8") as f:
            documented = list(dumps_in(f.read()))
        real = [raw for _, raw in capture()]
        self.assertEqual(len(real), 3, "expected REQUEST, RESPONSE, DATA")
        self.assertEqual(documented, real)


if __name__ == "__main__":
    unittest.main()
