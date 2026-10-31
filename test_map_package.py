import hashlib
import json
import struct
import zlib
from pathlib import Path
import stat
import tempfile
import unittest
import warnings
import zipfile
from map_package import (
    FORMAT_VERSION,
    GAME_DATA_VERSION,
    scan_package,
    seed_region,
    validate,
)


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


class ManifestV2Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name, "candidate.zip")
        self.region = seed_region()
        self.palette = scan_package({"world/region/r.0.0.mca": self.region})

    def manifest(self, **overrides):
        base = {
            "schemaVersion": 2,
            "mapId": "arena",
            "version": "v2",
            "baseVersion": "v1",
            "formatVersion": FORMAT_VERSION,
            "dataVersion": self.palette["data_version"],
            "requiredBlocks": {
                "registry": self.palette["registry"],
                "blocks": self.palette["blocks"],
            },
            "requiredMods": [],
            "files": {},
        }
        base.update(overrides)
        return base

    def pack(self, manifest, files):
        manifest = dict(manifest)
        manifest["files"] = {name: digest(data) for name, data in files.items()}
        with zipfile.ZipFile(self.path, "w") as archive:
            archive.writestr("manifest.json", json.dumps(manifest))
            for name, data in files.items():
                archive.writestr(name, data)
        return digest(self.path.read_bytes())

    def files(self, region=None):
        return {
            "map.json": json.dumps({"id": "arena"}).encode(),
            "world/region/r.0.0.mca": region if region is not None else self.region,
        }

    def test_v2_validates_and_reports_fields(self):
        manifest = validate(self.path, self.pack(self.manifest(), self.files()))
        self.assertEqual(manifest["schemaVersion"], 2)
        self.assertEqual(manifest["formatVersion"], FORMAT_VERSION)
        self.assertEqual(manifest["dataVersion"], GAME_DATA_VERSION)
        self.assertEqual(
            manifest["requiredBlocks"]["blocks"], ["minecraft:air", "minecraft:stone"]
        )

    def test_v1_still_accepted_as_legacy(self):
        files = self.files()
        manifest = {
            "schemaVersion": 1,
            "mapId": "arena",
            "version": "v2",
            "baseVersion": "v1",
            "files": {name: digest(data) for name, data in files.items()},
        }
        with zipfile.ZipFile(self.path, "w") as archive:
            archive.writestr("manifest.json", json.dumps(manifest))
            for name, data in files.items():
                archive.writestr(name, data)
        self.assertEqual(
            validate(self.path, digest(self.path.read_bytes()))["schemaVersion"], 1
        )

    def test_v2_rejects_newer_format(self):
        with self.assertRaisesRegex(ValueError, "unsupported map format"):
            validate(
                self.path,
                self.pack(
                    self.manifest(formatVersion=FORMAT_VERSION + 1), self.files()
                ),
            )

    def test_v2_rejects_block_drift(self):
        manifest = self.manifest()
        manifest["requiredBlocks"] = {
            "registry": hashlib.sha256(b"minecraft:air").hexdigest(),
            "blocks": ["minecraft:air"],
        }
        with self.assertRaisesRegex(ValueError, "differ from the manifest"):
            validate(self.path, self.pack(manifest, self.files()))

    def test_v2_rejects_data_version_drift(self):
        with self.assertRaisesRegex(ValueError, "differs from the manifest"):
            validate(
                self.path,
                self.pack(self.manifest(dataVersion=1), self.files()),
            )

    def test_v2_rejects_registry_mismatch(self):
        manifest = self.manifest()
        manifest["requiredBlocks"] = {
            "registry": "0" * 64,
            "blocks": manifest["requiredBlocks"]["blocks"],
        }
        with self.assertRaisesRegex(ValueError, "registry mismatch"):
            validate(self.path, self.pack(manifest, self.files()))

    def test_v2_rejects_wrong_content_hash(self):
        manifest = self.manifest(sha256="0" * 64)
        with self.assertRaisesRegex(ValueError, "content hash mismatch"):
            validate(self.path, self.pack(manifest, self.files()))

    def test_v2_rejects_bad_mods(self):
        for mods in (
            [{"id": "no spaces", "version": None}],
            [{"id": "ok", "version": 3}],
            "mods",
        ):
            with self.subTest(mods=mods):
                with self.assertRaisesRegex(ValueError, "invalid required mods"):
                    validate(
                        self.path,
                        self.pack(self.manifest(requiredMods=mods), self.files()),
                    )

    def test_v2_rejects_garbage_region(self):
        with self.assertRaisesRegex(ValueError, "truncated region file"):
            validate(
                self.path, self.pack(self.manifest(), self.files(region=b"garbage"))
            )

    def test_registry_construction_matches_game_admission(self):
        # Cross-repo contract with blockfield-server scripts/maps/map_manifest.py.
        self.assertEqual(
            self.palette["registry"],
            hashlib.sha256(b"minecraft:air\nminecraft:stone").hexdigest(),
        )

    def test_scan_tolerates_empty_files(self):
        palette = scan_package(
            {
                "world/region/r.0.0.mca": self.region,
                "world/poi/r.0.0.mca": b"",
            }
        )
        self.assertEqual(palette["blocks"], self.palette["blocks"])

    def test_missing_sidecar_skips_its_chunk(self):
        header = bytearray(8192)
        header[0:4] = (2).to_bytes(3, "big") + bytes([1])
        mca = bytes(header) + struct.pack(">i", 1) + bytes([2 | 128])
        palette = scan_package({"world/region/r.0.0.mca": mca})
        self.assertEqual(palette["blocks"], [])
        self.assertIsNone(palette["data_version"])

    def test_external_sidecar_resolves(self):
        seed = seed_region()
        (length,) = struct.unpack_from(">i", seed, 8192)
        nbt_body = zlib.decompress(seed[8197 : 8197 + length - 1])
        header = bytearray(8192)
        header[0:4] = (2).to_bytes(3, "big") + bytes([1])
        mca = bytes(header) + struct.pack(">i", 1) + bytes([2 | 128])
        palette = scan_package(
            {
                "world/region/r.0.0.mca": mca,
                "world/region/c.0.0.mcc": zlib.compress(nbt_body),
            }
        )
        self.assertEqual(palette["blocks"], ["minecraft:air", "minecraft:stone"])
        self.assertEqual(palette["data_version"], GAME_DATA_VERSION)


if __name__ == "__main__":
    unittest.main()
