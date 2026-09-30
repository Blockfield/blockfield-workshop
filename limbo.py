"""Derived Sponge artifacts for one immutable Limbo map; Anvil CAS remains its history."""

import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
from uuid import UUID, uuid4

from controller import atomic_write
from map_package import _json

DIGEST = re.compile(r"[0-9a-f]{64}")
MAX_SCHEMATIC = 4 << 20


def metadata(config: dict) -> dict:
    """Limbo's pinned reader uses zero-origin blocks/block entities, not native entities/biomes."""
    if config.get("id") != "limbo" or config.get("kind") != "limbo":
        raise ValueError("canonical Limbo config required")
    mode_data = config.get("mode_data", {})
    data = mode_data.get("limbo", {}) if isinstance(mode_data, dict) else None
    if not isinstance(data, dict):
        raise ValueError("Limbo mode metadata required")
    dimensions = {key: data.get(key) for key in ("width", "height", "length")}
    if any(type(value) is not int or value < 1 for value in dimensions.values()):
        raise ValueError("integer Limbo width/height/length required")
    if (
        dimensions["width"] > 256
        or dimensions["length"] > 256
        or dimensions["height"] > 256
    ):
        raise ValueError("Limbo dimensions exceed the native adapter limit")
    if math.prod(dimensions.values()) > 2_000_000:
        raise ValueError("Limbo export volume exceeds the native adapter limit")
    bounds = config.get("bounds", {})
    if (
        not isinstance(bounds, dict)
        or any(type(value) not in {int, float} for value in bounds.values())
        or bounds
        != {
            "minX": 0,
            "maxX": dimensions["width"],
            "minZ": 0,
            "maxZ": dimensions["length"],
        }
    ):
        raise ValueError("Limbo bounds must match its zero-origin volume")
    point = data.get("spawn", {})
    if not isinstance(point, dict):
        raise ValueError("Limbo spawn metadata required")
    spawn = {
        key: point.get(key, 0 if key in {"yaw", "pitch"} else None)
        for key in ("x", "y", "z", "yaw", "pitch")
    }
    if any(
        type(value) not in {int, float} or not math.isfinite(value)
        for value in spawn.values()
    ):
        raise ValueError("finite numeric Limbo spawn required")
    if (
        any(
            not 0 <= spawn[key] < dimensions[dimension]
            for key, dimension in (("x", "width"), ("y", "height"), ("z", "length"))
        )
        or not -90 <= spawn["pitch"] <= 90
    ):
        raise ValueError("Limbo spawn is outside its volume")
    return {"format": "Sponge-v2", **dimensions, "spawn": spawn}


def bounded(path: Path, limit: int) -> bytes:
    if path.is_symlink():
        raise ValueError("missing, unsafe or oversized Limbo artifact")
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
        )
    except OSError as error:
        raise ValueError("missing, unsafe or oversized Limbo artifact") from error
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError("missing, unsafe or oversized Limbo artifact")
        result = stream.read(limit + 1)
    if len(result) > limit:
        raise ValueError("Limbo artifact grew beyond its limit")
    return result


class LimboArtifacts:
    def __init__(self, root: Path, proxy_stopped):
        self.root = Path(root).absolute()
        self.jobs = self.root / "jobs"
        self.artifacts = self.root / "artifacts"
        self.proxy_stopped = proxy_stopped

    def _expected(self, versions, version: str) -> dict:
        versions._digest(version)
        manifest = versions.read("limbo", version)
        if manifest["kind"] != "limbo":
            raise ValueError("Limbo version kind mismatch")
        config_hash = manifest["files"]["map.json"]
        config = _json(versions._verify_blob(config_hash).read_bytes())
        return {
            "schema": 1,
            "mapId": "limbo",
            "versionDigest": version,
            "configSha256": config_hash,
            **metadata(config),
        }

    def prepare_request(self, versions, version: str) -> tuple[str, Path]:
        expected = self._expected(versions, version)
        versions.prepare("limbo", version)
        if (
            self.root.is_symlink()
            or self.jobs.is_symlink()
            or self.artifacts.is_symlink()
        ):
            raise ValueError("unsafe Limbo derived storage")
        self.jobs.mkdir(parents=True, exist_ok=True)
        job = str(uuid4())
        directory = self.jobs / job
        directory.mkdir(mode=0o755)
        if hasattr(os, "chown") and os.geteuid() == 0:
            os.chown(directory, 1000, 1000)
        atomic_write(
            directory / "request.json",
            json.dumps({**expected, "jobId": job}).encode(),
            0o644,
        )
        return job, directory

    def install_job(self, versions, version: str, job: str) -> dict:
        if str(UUID(job)) != job:
            raise ValueError("canonical Limbo export job UUID required")
        if (
            self.root.is_symlink()
            or self.jobs.is_symlink()
            or self.artifacts.is_symlink()
        ):
            raise ValueError("unsafe Limbo derived storage")
        directory = self.jobs / job
        if directory.is_symlink() or not directory.is_dir():
            raise ValueError("unsafe Limbo export job")
        expected = self._expected(versions, version)
        result = _json(bounded(directory / "result.json", 65536))
        if (
            type(result.get("schema")) is not int
            or result.get("jobId") != job
            or any(result.get(key) != value for key, value in expected.items())
        ):
            raise ValueError(
                "native Limbo export disagrees with its exact source version"
            )
        data = bounded(directory / "lobby.schem", MAX_SCHEMATIC)
        checksum = hashlib.sha256(data).hexdigest()
        if (
            not data
            or result.get("schematicSha256") != checksum
            or type(result.get("size")) is not int
            or result.get("size") != len(data)
        ):
            raise ValueError("native Limbo export checksum/size mismatch")
        selection = {
            **expected,
            "schematicSha256": checksum,
            "artifact": f"{version}-{checksum}.schem",
        }
        self.artifacts.mkdir(parents=True, exist_ok=True)
        artifact = self.artifacts / selection["artifact"]
        if artifact.exists() or artifact.is_symlink():
            if bounded(artifact, MAX_SCHEMATIC) != data:
                raise ValueError(
                    "immutable Limbo derivative already exists with different bytes"
                )
        else:
            atomic_write(artifact, data, 0o444)
        stamp = self.artifacts / f"{version}.json"
        if stamp.exists() or stamp.is_symlink():
            if _json(bounded(stamp, 65536)) != selection:
                raise ValueError("immutable Limbo derivative metadata already differs")
        else:
            atomic_write(stamp, json.dumps(selection, sort_keys=True).encode(), 0o444)
        return selection

    def ready(self, versions, version: str) -> dict:
        expected = self._expected(versions, version)
        selection = _json(bounded(self.artifacts / f"{version}.json", 65536))
        if type(selection.get("schema")) is not int or any(
            selection.get(key) != value for key, value in expected.items()
        ):
            raise ValueError(
                "Limbo derivative metadata disagrees with its source config"
            )
        checksum = selection.get("schematicSha256")
        if (
            not isinstance(checksum, str)
            or not DIGEST.fullmatch(checksum)
            or selection.get("artifact") != f"{version}-{checksum}.schem"
        ):
            raise ValueError("invalid Limbo derivative reference")
        if (
            self.root.is_symlink()
            or self.artifacts.is_symlink()
            or hashlib.sha256(
                bounded(self.artifacts / selection["artifact"], MAX_SCHEMATIC)
            ).hexdigest()
            != checksum
        ):
            raise ValueError("corrupt/unsafe Limbo derivative")
        return selection

    def activate(self, versions, version: str, actor: dict, expected_head) -> dict:
        versions.check_head("limbo", expected_head)
        selection = self.ready(versions, version)
        versions.prepare("limbo", version)
        if self.proxy_stopped() is not True:
            raise ValueError(
                "configured proxy must be proven stopped before Limbo activation"
            )
        target = versions.active / "limbo" / "selection.json"
        previous = (
            bounded(target, 65536) if target.exists() or target.is_symlink() else None
        )
        atomic_write(target, json.dumps(selection, sort_keys=True).encode(), 0o644)
        try:
            if self.proxy_stopped() is not True:
                raise ValueError("proxy started before Limbo activation commit")
            return versions.activate("limbo", version, actor, expected_head)
        except Exception:
            current = versions.head("limbo")
            if (current["versionDigest"] if current else None) == expected_head:
                if previous is None:
                    target.unlink(missing_ok=True)
                else:
                    atomic_write(target, previous, 0o644)
            raise
