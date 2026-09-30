import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from release_maps import apply, package


class ReleaseMapsTests(unittest.TestCase):
    def test_pinned_pair_replaces_only_one_map_and_rejects_corruption(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            content = {
                "map.json": b'{"id":"fields","name":"new"}',
                "world/region/r.0.0.mca": b"new",
                "world/entities/r.0.0.mca": b"entity",
            }
            manifest = {
                "schemaVersion": 1,
                "mapId": "fields",
                "version": "v2",
                "baseVersion": "v1",
                "files": {
                    name: hashlib.sha256(data).hexdigest()
                    for name, data in content.items()
                },
            }
            archive = root / "candidate.zip"
            with zipfile.ZipFile(archive, "w") as out:
                for name, data in content.items():
                    out.writestr(name, data)
                out.writestr("manifest.json", json.dumps(manifest))
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            archive.rename(root / (digest + ".zip"))
            pin = {
                "release": "map-fields-test",
                "asset": "fields.zip",
                "sha256": digest,
            }
            before = {
                "maps/fields/region/r.9.9.mca": b"old",
                "maps/other/region/r.0.0.mca": b"untouched",
                "blockfield/maps/fields.json": content["map.json"],
            }
            files = dict(before)
            apply(files, {"fields": pin}, root)
            self.assertNotIn("maps/fields/region/r.9.9.mca", files)
            self.assertEqual(files["maps/fields/region/r.0.0.mca"], b"new")
            self.assertEqual(files["maps/fields/entities/r.0.0.mca"], b"entity")
            self.assertEqual(files["maps/other/region/r.0.0.mca"], b"untouched")
            bad = dict(
                before,
                **{"blockfield/maps/fields.json": b'{"id":"fields","name":"mismatch"}'},
            )
            unchanged = dict(bad)
            with self.assertRaisesRegex(ValueError, "differs"):
                apply(bad, {"fields": pin}, root)
            self.assertEqual(bad, unchanged)
            (root / (digest + ".zip")).write_bytes(b"broken")
            with self.assertRaisesRegex(ValueError, "checksum"):
                apply(files, {"fields": pin}, root)
            for map_id, invalid in [
                ("../escape", pin),
                ("fields", dict(pin, asset="../escape")),
            ]:
                with self.assertRaises(ValueError):
                    package(map_id, invalid, root)
