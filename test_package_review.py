"""A ZIP member's original name must not be silently truncated into an allowed path."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import zipfile
from map_package import validate


class OriginalPathTests(unittest.TestCase):
    def test_nul_truncated_path_is_rejected_even_with_valid_pins(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "candidate.zip"
            files = {"map.json": b'{"id":"arena"}', "world/region/r.0.0.mca": b"synthetic-region"}
            manifest = {"schemaVersion": 1, "mapId": "arena", "version": "v2", "baseVersion": "v1",
                        "files": {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}}
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("manifest.json", json.dumps(manifest))
                archive.writestr("map.jsonQhidden", files["map.json"])
                archive.writestr("world/region/r.0.0.mca", files["world/region/r.0.0.mca"])
            raw = path.read_bytes().replace(b"map.jsonQhidden", b"map.json\x00hidden")
            path.write_bytes(raw)
            with zipfile.ZipFile(path) as archive:
                entry = archive.infolist()[1]
                self.assertEqual(entry.filename, "map.json")
                self.assertNotEqual(entry.orig_filename, entry.filename)
            with self.assertRaisesRegex(ValueError, "original archive path"):
                validate(path, hashlib.sha256(raw).hexdigest())
