import json
import tempfile
import unittest
from pathlib import Path

from cws.cli import _read_json_file


class CliInputTests(unittest.TestCase):
    def test_json_file_accepts_utf8_bom(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "payload.json"
            path.write_bytes(b"\xef\xbb\xbf" + json.dumps({"ok": True}).encode("utf-8"))
            self.assertEqual(_read_json_file(path), {"ok": True})

    def test_json_file_accepts_plain_utf8(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "payload.json"
            path.write_text('{"name":"cws"}', encoding="utf-8")
            self.assertEqual(_read_json_file(path), {"name": "cws"})


if __name__ == "__main__":
    unittest.main()
