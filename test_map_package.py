import hashlib
import json
from pathlib import Path
import stat
import tempfile
import unittest
import warnings
import zipfile
from map_package import validate


def digest(data):
    return hashlib.sha256(data).hexdigest()


class PackageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name, "candidate.zip")

    def pack(
        self,
        *,
        extra=None,
        bad_hash=False,
        config_id="arena",
        duplicate=False,
        symlink=False,
    ):
        files = {
            "map.json": json.dumps({"id": config_id}).encode(),
            "world/region/r.0.0.mca": b"synthetic-test-fixture",
        }
        manifest = {
            "schemaVersion": 1,
            "mapId": "arena",
            "version": "v2",
            "baseVersion": "v1",
            "files": {name: digest(data) for name, data in files.items()},
        }
        if bad_hash:
            manifest["files"]["map.json"] = "0" * 64
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(self.path, "w", zipfile.ZIP_DEFLATED) as z:
                z.writestr("manifest.json", json.dumps(manifest))
                for name, data in files.items():
                    z.writestr(name, data)
                if extra:
                    z.writestr(extra, b"bad")
                if duplicate:
                    z.writestr("map.json", files["map.json"])
                if symlink:
                    info = zipfile.ZipInfo("world/region/r.1.0.mca")
                    info.create_system = 3
                    info.external_attr = (stat.S_IFLNK | 0o777) << 16
                    z.writestr(info, "/etc/passwd")
        return digest(self.path.read_bytes())

    def test_validates_without_extracting(self):
        expected = self.pack()
        self.assertEqual(validate(self.path, expected)["mapId"], "arena")
        self.assertEqual(list(Path(self.temp.name).iterdir()), [self.path])

    def test_outer_pin(self):
        self.pack()
        with self.assertRaises(ValueError):
            validate(self.path, "0" * 64)

    def test_member_pin(self):
        expected = self.pack(bad_hash=True)
        with self.assertRaises(ValueError):
            validate(self.path, expected)

    def test_world_config_pair(self):
        expected = self.pack(config_id="other")
        with self.assertRaises(ValueError):
            validate(self.path, expected)

    def test_paths(self):
        for name in [
            "../escape",
            "/abs",
            "world/../escape",
            "world\\escape",
            "C:evil",
            "./map.json",
            "world//region/r.0.0.mca",
        ]:
            with self.subTest(name=name):
                expected = self.pack(extra=name)
                with self.assertRaises(ValueError):
                    validate(self.path, expected)

    def test_region_coordinate_aliases_rejected(self):
        for name in [
            "world/region/r.٠.0.mca",
            "world/region/r.-0.1.mca",
            "world/region/r.01.1.mca",
        ]:
            with self.subTest(name=name):
                files = {"map.json": b'{"id":"arena"}', name: b"alias"}
                manifest = {
                    "schemaVersion": 1,
                    "mapId": "arena",
                    "version": "v2",
                    "baseVersion": "v1",
                    "files": {n: digest(d) for n, d in files.items()},
                }
                with zipfile.ZipFile(self.path, "w") as z:
                    z.writestr("manifest.json", json.dumps(manifest))
                    for n, d in files.items():
                        z.writestr(n, d)
                with self.assertRaisesRegex(ValueError, "unapproved world file"):
                    validate(self.path, digest(self.path.read_bytes()))

    def test_unknown_sidecars_rejected_not_lost(self):
        expected = self.pack(extra="world/data/mod-state.dat")
        with self.assertRaises(ValueError):
            validate(self.path, expected)

    def test_duplicate_and_symlink(self):
        for kwargs in [{"duplicate": True}, {"symlink": True}]:
            expected = self.pack(**kwargs)
            with self.assertRaises(ValueError):
                validate(self.path, expected)

    def test_size_and_count_limits(self):
        expected = self.pack()
        for kwargs in [{"max_archive": 1}, {"max_expanded": 1}, {"max_files": 1}]:
            with self.assertRaises(ValueError):
                validate(self.path, expected, **kwargs)

    def test_duplicate_json_and_nonfinite_rejected(self):
        from map_package import _json

        for data in [
            b'{"schemaVersion":1,"schemaVersion":2}',
            b'{"x":NaN}',
            b'{"x":1e999}',
        ]:
            with self.assertRaises(ValueError):
                _json(data)


if __name__ == "__main__":
    unittest.main()
