"""Read-only validator for immutable map candidates. Never extracts or activates a map.

CLI: python tools/workshop/map_package.py candidate.zip --sha256 EXPECTED_SHA256
Layout v1: manifest.json, map.json, world/{region,entities,poi}/r.X.Z.mca and external c.X.Z.mcc.
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
import struct
import tempfile
import unicodedata
import zipfile
import zlib

SUPPORTED_MODES = (
    "campaign",
    "room",
    "tdm",
    "snipers",
    "awp",
    "deathmatch",
    "training",
)


def map_name(value):
    if (
        not isinstance(value, str)
        or not 1 <= len(value.strip()) <= 100
        or any(unicodedata.category(char) == "Cc" or char == "§" for char in value)
    ):
        raise ValueError("map name requires 1–100 characters without controls")
    return value.strip()


def supported_modes(value, *, required=False):
    if (
        not isinstance(value, list)
        or any(
            not isinstance(mode, str) or mode not in SUPPORTED_MODES for mode in value
        )
        or len(set(value)) != len(value)
        or required
        and not value
    ):
        raise ValueError("select distinct supported game modes")
    return [mode for mode in SUPPORTED_MODES if mode in value]


def seed_config(map_id, name, modes):
    modes = supported_modes(modes, required=True)
    data = {}
    if set(modes) & {"campaign", "room", "tdm", "snipers", "awp"}:
        data["campaign"] = {"bases": {}, "points": [], "lobbies": {}}
    if "deathmatch" in modes:
        data["deathmatch"] = {"spawn_points": []}
    if "training" in modes:
        data["training"] = {
            "spawn": {"x": 8.5, "y": 65, "z": 8.5, "yaw": 0},
            "targets": [],
            "forceload_chunks": {"minX": 0, "maxX": 0, "minZ": 0, "maxZ": 0},
            "zones": {},
        }
    return {
        "id": map_id,
        "name": map_name(name),
        "kind": "arena",
        "bounds": {"minX": 0, "maxX": 15, "minZ": 0, "maxZ": 15},
        "supported_modes": modes,
        "mode_data": data,
    }


def seed_region():
    """One 1.21.1 void chunk with a 16×16 stone platform at Y64; no runtime dependencies."""

    def text(value):
        data = value.encode()
        return struct.pack(">H", len(data)) + data

    def payload(tag, value):
        if tag in {1, 3, 4}:
            return struct.pack({1: ">b", 3: ">i", 4: ">q"}[tag], value)
        if tag == 8:
            return text(value)
        if tag == 9:
            subtype, entries = value
            return (
                bytes([subtype])
                + struct.pack(">i", len(entries))
                + b"".join(payload(subtype, entry) for entry in entries)
            )
        if tag == 10:
            return (
                b"".join(
                    bytes([kind]) + text(name) + payload(kind, item)
                    for name, (kind, item) in value.items()
                )
                + b"\0"
            )
        if tag == 12:
            return struct.pack(">i", len(value)) + b"".join(
                struct.pack(">q", word) for word in value
            )
        raise ValueError("unsupported seed NBT tag")

    def packed(values, bits):
        # Minecraft 1.21.1 palette entries never straddle a long, as in server/scripts/nbt.py.
        per = 64 // bits
        return [
            sum(
                value << (bits * offset)
                for offset, value in enumerate(values[index : index + per])
            )
            for index in range(0, len(values), per)
        ]

    sections = []
    for y in range(-4, 20):
        palette = [{"Name": (8, "minecraft:air")}]
        states = {"palette": (9, (10, palette))}
        if y == 4:
            palette.append({"Name": (8, "minecraft:stone")})
            states["data"] = (12, packed([1] * 256 + [0] * 3840, 4))
        sections.append(
            {
                "Y": (1, y),
                "block_states": (10, states),
                "biomes": (10, {"palette": (9, (8, ["minecraft:plains"]))}),
            }
        )
    chunk = {
        "DataVersion": (3, 3955),
        "xPos": (3, 0),
        "zPos": (3, 0),
        "yPos": (3, -4),
        "Status": (8, "minecraft:full"),
        "LastUpdate": (4, 0),
        "InhabitedTime": (4, 0),
        "isLightOn": (1, 0),
        "sections": (9, (10, sections)),
        "block_entities": (9, (10, [])),
        "block_ticks": (9, (10, [])),
        "fluid_ticks": (9, (10, [])),
        "structures": (10, {"starts": (10, {}), "References": (10, {})}),
        "Heightmaps": (
            10,
            {
                name: (12, packed([129] * 256, 9))
                for name in (
                    "MOTION_BLOCKING",
                    "MOTION_BLOCKING_NO_LEAVES",
                    "WORLD_SURFACE",
                    "OCEAN_FLOOR",
                )
            },
        ),
    }
    compressed = zlib.compress(b"\x0a\0\0" + payload(10, chunk), 6)
    body = struct.pack(">i", len(compressed) + 1) + b"\x02" + compressed
    body += b"\0" * (-len(body) % 4096)
    header = (2).to_bytes(3, "big") + bytes([len(body) // 4096]) + bytes(4092)
    return header + bytes(4096) + body


def validate_playable(config):
    """Match the selected-mode readers; editable drafts may intentionally fail this check."""
    if config.get("kind", "arena") != "arena":
        return
    modes = supported_modes(config.get("supported_modes", []))
    if not modes:
        return
    bounds, data = config.get("bounds"), config.get("mode_data")
    if not isinstance(bounds, dict) or not isinstance(data, dict):
        raise ValueError("selected modes require bounds and mode_data")

    def number(value):
        if type(value) not in {int, float} or not math.isfinite(value):
            raise ValueError("mode coordinates must be finite numbers")
        return value

    def point(value, *, angles=False, centred=False, training=False):
        if not isinstance(value, dict):
            raise ValueError("mode requires a point")
        x, y, z = (number(value.get(key)) for key in ("x", "y", "z"))
        if centred:
            x += 0.5 if x == math.floor(x) else 0
            z += 0.5 if z == math.floor(z) else 0
        if (
            not bounds["minX"] <= x <= bounds["maxX"]
            or not bounds["minZ"] <= z <= bounds["maxZ"]
        ):
            raise ValueError("mode point is outside map bounds")
        yaw, pitch = (
            number(value.get(key, value.get(alias, 0)))
            for key, alias in (("yaw", "yRot"), ("pitch", "xRot"))
        )
        if angles and (
            not -180 <= yaw <= 180 or not training and not -90 <= pitch <= 90
        ):
            raise ValueError("invalid spawn orientation")
        return x, y, z

    team_modes = set(modes) & {"campaign", "room", "tdm", "snipers", "awp"}
    if team_modes:
        campaign = data.get("campaign")
        if not isinstance(campaign, dict) or not isinstance(
            campaign.get("bases"), dict
        ):
            raise ValueError("team modes require campaign bases")
        for team in ("RedTeam", "BlueTeam"):
            base = campaign["bases"].get(team)
            if not isinstance(base, dict):
                raise ValueError("team modes require both team bases")
            spawns = base.get("spawn_points", [])
            if not isinstance(spawns, list):
                raise ValueError("invalid team spawn_points")
            for spawn in spawns or [base]:
                point(spawn, centred=True)
        lobbies = campaign.get("lobbies", {})
        if not isinstance(lobbies, dict):
            raise ValueError("invalid team lobbies")
        for lobby in lobbies.values():
            point(lobby, centred=True)
        if team_modes & {"campaign", "room"}:
            points = campaign.get("points")
            if not isinstance(points, list) or not points:
                raise ValueError("capture modes require at least one point")
            for capture in points:
                point(capture)
                if any(
                    not isinstance(capture.get(key), str) or not capture[key].strip()
                    for key in ("id", "name")
                ):
                    raise ValueError("capture point requires id and name")
                boxes = capture.get("zone", [])
                if not isinstance(boxes, list):
                    raise ValueError("invalid capture zone")
                if not boxes and "aabb" in capture:
                    boxes = [capture["aabb"]]
                for box in boxes:
                    if not isinstance(box, dict) or any(
                        type(box.get(key)) is not int
                        for key in ("minX", "maxX", "minY", "maxY", "minZ", "maxZ")
                    ):
                        raise ValueError(
                            "capture zone requires integer box coordinates"
                        )
                # Inclusive block cells have a far face at max+1, and runtime normalizes inverted endpoints.
                if boxes and not any(
                    all(
                        min(box["min" + axis], box["max" + axis])
                        <= bounds["max" + axis] + 1
                        and max(box["min" + axis], box["max" + axis]) + 1
                        > bounds["min" + axis]
                        for axis in ("X", "Z")
                    )
                    for box in boxes
                ):
                    raise ValueError("capture zone does not overlap the battlefield")
                if not boxes:
                    if "radius" in capture and type(capture["radius"]) is not int:
                        raise ValueError("invalid capture radius")
                    height = capture.get("height", {"min": 3, "max": 5})
                    if not isinstance(height, dict) or any(
                        type(height.get(key)) is not int for key in ("min", "max")
                    ):
                        raise ValueError("invalid capture height")
    if "deathmatch" in modes:
        dm = data.get("deathmatch")
        spawns = dm.get("spawn_points") if isinstance(dm, dict) else None
        if not isinstance(spawns, list) or len(spawns) < 2:
            raise ValueError("deathmatch requires at least two spawn points")
        positions = [point(spawn, angles=True, centred=True) for spawn in spawns]
        if len(set(positions)) != len(positions):
            raise ValueError("duplicate deathmatch spawn point")
    if "training" in modes:
        training = data.get("training")
        if not isinstance(training, dict):
            raise ValueError("training requires its layout")
        point(training.get("spawn"), angles=True, centred=True, training=True)
        targets = training.get("targets")
        if not isinstance(targets, list) or not 1 <= len(targets) <= 32:
            raise ValueError("training requires 1–32 targets")
        ids = set()
        for target in targets:
            point(target, angles=True, centred=True, training=True)
            identifier = target.get("id")
            if (
                not isinstance(identifier, str)
                or not re.fullmatch(r"[A-Za-z0-9_]{1,16}", identifier)
                or identifier in ids
            ):
                raise ValueError("invalid or duplicate training target id")
            ids.add(identifier)
            armor = target.get("armor", "none")
            if (
                not isinstance(armor, str)
                or armor != "none"
                and not re.fullmatch(r"class:[a-z0-9_]{1,64}", armor)
            ):
                raise ValueError("invalid training target armor")
            motion = target.get("motion")
            if motion is not None:
                if not isinstance(motion, dict) or motion.get("type") != "strafe":
                    raise ValueError("training supports only strafe motion")
                to_x, speed = number(motion.get("to_x")), number(motion.get("speed"))
                if not bounds["minX"] <= to_x <= bounds["maxX"] or not 0 < speed <= 20:
                    raise ValueError("invalid training target strafe")
        chunks = training.get("forceload_chunks")
        if not isinstance(chunks, dict) or any(
            type(chunks.get(key)) is not int for key in ("minX", "maxX", "minZ", "maxZ")
        ):
            raise ValueError("training requires integer forceload_chunks")
        width, depth = (
            chunks["maxX"] - chunks["minX"] + 1,
            chunks["maxZ"] - chunks["minZ"] + 1,
        )
        if width <= 0 or depth <= 0 or width * depth > 64:
            raise ValueError("training permits at most 64 forced chunks")
        zones = training.get("zones", {})
        if not isinstance(zones, dict):
            raise ValueError("invalid training zones")
        for zone in zones.values():
            if not isinstance(zone, dict):
                raise ValueError("invalid training zone")
            coordinates = {
                key: number(zone.get(key)) for key in ("minX", "maxX", "minZ", "maxZ")
            }
            if not (
                bounds["minX"]
                <= coordinates["minX"]
                < coordinates["maxX"]
                <= bounds["maxX"]
                and bounds["minZ"]
                <= coordinates["minZ"]
                < coordinates["maxZ"]
                <= bounds["maxZ"]
            ):
                raise ValueError("training zone is outside map bounds")


_HEX = re.compile(r"[0-9a-f]{64}\Z")
_ID = re.compile(r"[a-z0-9][a-z0-9_.-]{0,95}\Z")
# One spelling per coordinate: \d would admit Unicode digits and 0/-0/00 aliases.
_COORD = r"(?:0|-?[1-9][0-9]*)"
_REGION = re.compile(
    rf"world/(region|entities|poi)/(?:r\.{_COORD}\.{_COORD}\.mca|c\.{_COORD}\.{_COORD}\.mcc)\Z"
)


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

    return json.loads(
        raw,
        object_pairs_hook=unique,
        parse_constant=bad_constant,
        parse_float=finite_float,
    )


def validate(
    path: Path,
    expected_sha256: str,
    *,
    max_archive: int = 512 * 1024 * 1024,
    max_expanded: int = 1024 * 1024 * 1024,
    max_files: int = 10000,
) -> dict:
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
                if (
                    not name
                    or "\\" in name
                    or ":" in name
                    or name.startswith("/")
                    or "\x00" in name
                ):
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
                    if canonical not in {
                        "world",
                        "world/region",
                        "world/entities",
                        "world/poi",
                    }:
                        raise ValueError("unexpected directory")
                    continue
                if kind == stat.S_IFDIR:
                    raise ValueError("directory masquerading as file")
                if name not in {"manifest.json", "map.json"} and not _REGION.fullmatch(
                    name
                ):
                    raise ValueError("unapproved world file")
                total += entry.file_size
                if total > max_expanded:
                    raise ValueError("expanded size limit")
                files[name] = entry
            if (
                "manifest.json" not in files
                or "map.json" not in files
                or not any(
                    n.startswith("world/region/") and n.endswith(".mca") for n in files
                )
            ):
                raise ValueError("world and map config required together")
            if (
                files["manifest.json"].file_size > 65536
                or files["map.json"].file_size > 1024 * 1024
            ):
                raise ValueError("metadata too large")
            manifest = _json(archive.read(files["manifest.json"]))
            if (
                not isinstance(manifest, dict)
                or type(manifest.get("schemaVersion")) is not int
                or manifest["schemaVersion"] != 1
            ):
                raise ValueError("unsupported manifest schema")
            for field in ("mapId", "version", "baseVersion"):
                if not isinstance(manifest.get(field), str) or not _ID.fullmatch(
                    manifest[field]
                ):
                    raise ValueError("invalid map/version identity")
            checksums = manifest.get("files")
            if not isinstance(checksums, dict) or set(checksums) != set(files) - {
                "manifest.json"
            }:
                raise ValueError("manifest/file set mismatch")
            for name, expected in checksums.items():
                if not isinstance(expected, str) or not _HEX.fullmatch(expected):
                    raise ValueError("invalid file checksum")
                with archive.open(files[name]) as member:
                    digest = hashlib.sha256()
                    seen = 0
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
            if config.get("kind", "arena") not in {"arena", "lobby", "limbo"}:
                raise ValueError("unknown map kind")
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
    print(
        json.dumps(
            {"valid": True, "mapId": manifest["mapId"], "version": manifest["version"]}
        )
    )


if __name__ == "__main__":
    main()
