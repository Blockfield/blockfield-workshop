"""Read-only validator for immutable map candidates. Never extracts or activates a map.

CLI: python tools/workshop/map_package.py candidate.zip --sha256 EXPECTED_SHA256
Layout v1: manifest.json, map.json, world/{region,entities,poi}/r.X.Z.mca.
Unknown world sidecars are rejected, not silently discarded; extend only after compatibility tests.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import re
import stat
import tempfile
import zipfile

_HEX = re.compile(r"[0-9a-f]{64}\Z")
_ID = re.compile(r"[a-z0-9][a-z0-9_.-]{0,95}\Z")
# One spelling per coordinate: \d would admit Unicode digits and 0/-0/00 aliases.
_REGION = re.compile(r"world/(region|entities|poi)/r\.(0|-?[1-9][0-9]*)\.(0|-?[1-9][0-9]*)\.mca\Z")


def _json(raw: bytes):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate JSON key")
            value[key] = item
        return value
    def bad_constant(value):
        raise ValueError("non-finite JSON value")
    def finite_float(value):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("non-finite JSON number")
        return number
    return json.loads(raw, object_pairs_hook=unique, parse_constant=bad_constant, parse_float=finite_float)


def validate(path: Path, expected_sha256: str, *, max_archive: int = 512 * 1024 * 1024,
             max_expanded: int = 1024 * 1024 * 1024, max_files: int = 10000) -> dict:
    """Validate the exact pinned archive, all file hashes and the world/config ID pairing."""
    if not _HEX.fullmatch(expected_sha256):
        raise ValueError("expected lowercase SHA256 required")
    path = Path(path)
    # Copy bounded bytes to private scratch so hash and parsing use identical bytes even
    # if another process replaces or edits the original path during validation.
    with path.open("rb") as original, tempfile.TemporaryFile() as source:
        digest = hashlib.sha256()
        copied = 0
        while chunk := original.read(1024 * 1024):
            copied += len(chunk)
            if copied > max_archive:
                raise ValueError("archive too large")
            digest.update(chunk)
            source.write(chunk)
        if digest.hexdigest() != expected_sha256:
            raise ValueError("archive checksum mismatch")
        source.seek(0)
        with zipfile.ZipFile(source) as archive:
            entries = archive.infolist()
            if len(entries) > max_files:
                raise ValueError("too many entries")
            total = 0
            files = {}
            all_paths = set()
            for entry in entries:
                name = entry.filename
                # ZipInfo truncates filename at NUL; validate the original spelling too.
                if entry.orig_filename != name or "\x00" in entry.orig_filename:
                    raise ValueError("noncanonical original archive path")
                parts = PurePosixPath(name).parts
                if not name or "\\" in name or ":" in name or name.startswith("/") or "\x00" in name:
                    raise ValueError("unsafe path")
                if any(p in {"", ".", ".."} for p in name.rstrip("/").split("/")):
                    raise ValueError("noncanonical path")
                canonical = "/".join(parts)
                if canonical in all_paths:
                    raise ValueError("duplicate archive path")
                all_paths.add(canonical)
                mode = entry.external_attr >> 16
                kind = stat.S_IFMT(mode)
                if entry.flag_bits & 1 or kind not in {0, stat.S_IFREG, stat.S_IFDIR}:
                    raise ValueError("encrypted or special file")
                if entry.is_dir():
                    if canonical not in {"world", "world/region", "world/entities", "world/poi"}:
                        raise ValueError("unexpected directory")
                    continue
                if kind == stat.S_IFDIR:
                    raise ValueError("directory masquerading as file")
                if name not in {"manifest.json", "map.json"} and not _REGION.fullmatch(name):
                    raise ValueError("unapproved world file")
                total += entry.file_size
                if total > max_expanded:
                    raise ValueError("expanded size limit")
                files[name] = entry
            if "manifest.json" not in files or "map.json" not in files or not any(n.startswith("world/region/") for n in files):
                raise ValueError("world and map config required together")
            if files["manifest.json"].file_size > 65536 or files["map.json"].file_size > 1024 * 1024:
                raise ValueError("metadata too large")
            manifest = _json(archive.read(files["manifest.json"]))
            if not isinstance(manifest, dict) or type(manifest.get("schemaVersion")) is not int or manifest["schemaVersion"] != 1:
                raise ValueError("unsupported manifest schema")
            for field in ("mapId", "version", "baseVersion"):
                if not isinstance(manifest.get(field), str) or not _ID.fullmatch(manifest[field]):
                    raise ValueError("invalid map/version identity")
            checksums = manifest.get("files")
            if not isinstance(checksums, dict) or set(checksums) != set(files) - {"manifest.json"}:
                raise ValueError("manifest/file set mismatch")
            for name, expected in checksums.items():
                if not isinstance(expected, str) or not _HEX.fullmatch(expected):
                    raise ValueError("invalid file checksum")
                with archive.open(files[name]) as member:
                    digest = hashlib.sha256(); seen = 0
                    while chunk := member.read(1024 * 1024):
                        seen += len(chunk)
                        if seen > files[name].file_size:
                            raise ValueError("member size mismatch")
                        digest.update(chunk)
                    if seen != files[name].file_size or digest.hexdigest() != expected:
                        raise ValueError("member checksum mismatch")
            config = _json(archive.read(files["map.json"]))
            if not isinstance(config, dict) or config.get("id") != manifest["mapId"]:
                raise ValueError("map config/world identity mismatch")
            return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--sha256", required=True)
    args = parser.parse_args()
    try:
        manifest = validate(args.archive, args.sha256)
    except (OSError, ValueError, zipfile.BadZipFile, RuntimeError) as error:
        parser.exit(1, f"Candidate rejected: {type(error).__name__}: {error}\n")
    print(json.dumps({"valid": True, "mapId": manifest["mapId"], "version": manifest["version"]}))


if __name__ == "__main__":
    main()
