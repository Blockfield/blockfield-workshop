"""Criterion 14 (blockfield-releases#8): hostile map candidate archives vs map_package.validate().

Ported from the ad-hoc /var/mnt/projects/.bf-accept8/package_attacks.py acceptance run, which fed
map_package.validate() 19 hostile archives built from one real captured candidate plus one real
accepted case; here the "real" candidate is built in a temp dir instead, so this runs standalone
under `python3 -m unittest discover -s tools/workshop` (no stand/candidates dir needed).

Only cases that are NOT already identical to an existing test_map_package.py case are kept here
(see the skip list below) -- ponytail: no duplicate coverage.

Skipped as identical to test_map_package.py:
  checksum mismatch            -> test_outer_pin
  zip-slip ../, zip-slip in world/, absolute path, backslash path -> test_paths
  symlink member, duplicate member name -> test_duplicate_and_symlink
  unknown sidecar               -> test_unknown_sidecars_rejected_not_lost
  member checksum mismatch      -> test_member_pin
  map.json id != manifest mapId -> test_world_config_pair
  oversize archive, too many entries -> test_size_and_count_limits
Kept here as genuinely new code paths: truncated archive (BadZipFile), missing region file,
manifest referencing a file absent from the archive, level.dat smuggled in as a top-level file,
NaN reaching validate() through map.json (not just through _json() directly), an encrypted-flag
member that is not a symlink, and a real (not artificially-capped) zip bomb.
"""

import hashlib
import io
import json
import stat
import tempfile
import unittest
import warnings
import zipfile
from pathlib import Path

from map_package import validate


def digest(data):
    return hashlib.sha256(data).hexdigest()


def build(entries):
    """entries: {name: (bytes, extra)} -> zip bytes; extra may set 'attr' or 'flags'."""
    buf = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for name, (data, extra) in entries.items():
                info = zipfile.ZipInfo(name)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = extra.get("attr", (stat.S_IFREG | 0o644) << 16)
                info.flag_bits |= extra.get("flags", 0)
                z.writestr(info, data)
    return buf.getvalue()


class PackageAttackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name, "candidate.zip")
        # A realistic candidate: region + entities + poi, like a real export, not a single fixture file.
        self.world = {
            "world/region/r.0.0.mca": b"region-bytes",
            "world/entities/r.0.0.mca": b"entities-bytes",
            "world/poi/r.0.0.mca": b"poi-bytes",
        }
        self.map_json = json.dumps({"id": "fields"}).encode()

    def manifest(self, files, **overrides):
        base = {
            "schemaVersion": 1,
            "mapId": "fields",
            "version": "v2",
            "baseVersion": "v1",
            "files": {name: digest(data) for name, data in files.items()},
        }
        base.update(overrides)
        return base

    def package(
        self, *, files=None, manifest=None, extra_manifest_files=None, mutate_zip=None
    ):
        files = dict(
            files if files is not None else {**self.world, "map.json": self.map_json}
        )
        manifest = manifest if manifest is not None else self.manifest(files)
        if extra_manifest_files:
            manifest = {
                **manifest,
                "files": {**manifest["files"], **extra_manifest_files},
            }
        entries = {"manifest.json": (json.dumps(manifest).encode(), {})}
        entries.update({name: (data, {}) for name, data in files.items()})
        data = build(entries)
        if mutate_zip:
            data = mutate_zip(data)
        return data

    def write(self, data):
        self.path.write_bytes(data)
        return digest(data)

    def accept(self, data):
        sha = self.write(data)
        return validate(self.path, sha)

    def reject(self, data):
        sha = self.write(
            data
        )  # hash of the exact bytes on disk: rejection must come from validate's own checks
        with self.assertRaises((ValueError, zipfile.BadZipFile)):
            validate(self.path, sha)

    # ---------------------------------------------------------------- cases

    def test_real_candidate_accepted(self):
        manifest = self.accept(self.package())
        self.assertEqual(manifest["mapId"], "fields")

    def test_truncated_archive_interrupted_export(self):
        data = self.package()
        self.reject(data[: len(data) // 2])

    def test_missing_region_entities_poi_only(self):
        files = {
            "world/entities/r.0.0.mca": self.world["world/entities/r.0.0.mca"],
            "world/poi/r.0.0.mca": self.world["world/poi/r.0.0.mca"],
            "map.json": self.map_json,
        }
        self.reject(self.package(files=files))

    def test_manifest_lists_a_missing_file(self):
        self.reject(
            self.package(extra_manifest_files={"world/region/r.9.9.mca": "0" * 64})
        )

    def test_level_dat_smuggled(self):
        files = {**self.world, "map.json": self.map_json, "level.dat": b"nbt-ish"}
        self.reject(self.package(files=files, manifest=self.manifest(files)))

    def test_nan_reaches_validate_through_map_json(self):
        bad_map_json = b'{"id": "fields", "x": NaN}'
        files = {**self.world, "map.json": bad_map_json}
        self.reject(self.package(files=files, manifest=self.manifest(files)))

    def test_encrypted_flag_on_a_plain_member(self):
        # Not a symlink: a normal regular-file region entry with the encryption bit set on its flags.
        data = self.package()
        self.reject(set_encrypted_flag(data, "world/region/r.0.0.mca"))

    def test_zip_bomb_realistic_size(self):
        files = {**self.world, "map.json": self.map_json}
        manifest = self.manifest(files)
        buf = io.BytesIO()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as z:
                for name, data in files.items():
                    z.writestr(name, data)
                bomb_name = "world/region/r.50.50.mca"
                with z.open(bomb_name, "w", force_zip64=True) as w:
                    chunk = bytes(1 << 24)
                    for _ in range(
                        70
                    ):  # ~1.1 GiB expanded, well past the real 1 GiB default limit
                        w.write(chunk)
                manifest = {
                    **manifest,
                    "files": {**manifest["files"], bomb_name: "0" * 64},
                }
                z.writestr("manifest.json", json.dumps(manifest))
        data = buf.getvalue()
        self.assertLess(
            len(data), 16 * 1024 * 1024, "bomb archive itself should stay small on disk"
        )
        self.reject(data)


def set_encrypted_flag(raw: bytes, member_name: str) -> bytes:
    raw = bytearray(raw)
    name = member_name.encode()
    cd = raw.find(b"PK\x01\x02")
    while cd != -1:
        name_len = int.from_bytes(raw[cd + 28 : cd + 30], "little")
        if bytes(raw[cd + 46 : cd + 46 + name_len]) == name:
            raw[cd + 8] |= 1
            local = int.from_bytes(raw[cd + 42 : cd + 46], "little")
            raw[local + 6] |= 1
        cd = raw.find(b"PK\x01\x02", cd + 4)
    return bytes(raw)


if __name__ == "__main__":
    unittest.main()
