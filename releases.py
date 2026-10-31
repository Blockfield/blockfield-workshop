"""Validate an operator-trusted export of a published immutable server release.

The catalog is an authority input, writable only by the operator. It must export
publication metadata, the original build.json/SHA256SUMS bytes, and the verified
image provenance binding that build to its registry digest. This parser checks
those bindings; it does not authenticate a remote registry or execute images.
"""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from uuid import UUID

SERVER_REPOSITORY = "ghcr.io/blockfield/blockfield-server"
IMAGE_DIGEST = re.compile(re.escape(SERVER_REPOSITORY) + r"@sha256:[0-9a-f]{64}")
RELEASE = re.compile(r"v[0-9]+\.[0-9]+\.[0-9]+")
CLIENT_VERSION = re.compile(r"v?[0-9]+\.[0-9]+\.[0-9]+")
SHA256 = re.compile(r"[0-9a-f]{64}")
SCHEMA = 1


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate release metadata key")
        result[key] = value
    return result


def read_document(path: Path, limit=1 << 20):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        if path.is_symlink():
            raise ValueError("invalid release metadata file") from None
        return None
    except OSError as error:
        raise ValueError(f"unreadable release metadata: {error}") from error
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError("invalid release metadata file")
        payload = handle.read(limit + 1)
    if len(payload) > limit:
        raise ValueError("release metadata exceeds reader limit")
    try:
        value = json.loads(payload, object_pairs_hook=_unique)
    except (ValueError, UnicodeError) as error:
        raise ValueError("invalid release metadata JSON") from error
    if not isinstance(value, dict):
        raise ValueError("release metadata must be an object")
    return value


def _matches(pattern, value):
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _image_evidence(source, digest, release):
    manifest_bytes = source["imageManifest"].encode()
    if (
        "sha256:" + hashlib.sha256(manifest_bytes).hexdigest()
        != digest.split("@", 1)[1]
    ):
        raise ValueError("published image manifest digest mismatch")
    manifest = json.loads(manifest_bytes, object_pairs_hook=_unique)
    if "manifests" in manifest:
        platform_bytes = source["platformManifest"].encode()
        platform_digest = "sha256:" + hashlib.sha256(platform_bytes).hexdigest()
        platforms = [
            entry
            for entry in manifest["manifests"]
            if entry.get("platform") == {"architecture": "amd64", "os": "linux"}
        ]
        if len(platforms) != 1 or platforms[0]["digest"] != platform_digest:
            raise ValueError("published linux/amd64 manifest binding mismatch")
        manifest = json.loads(platform_bytes, object_pairs_hook=_unique)
    if manifest.get("schemaVersion") != 2 or not manifest.get("layers"):
        raise ValueError("published image manifest required")
    config_bytes = source["imageConfig"].encode()
    if (
        manifest["config"]["digest"]
        != "sha256:" + hashlib.sha256(config_bytes).hexdigest()
    ):
        raise ValueError("published image config checksum mismatch")
    config = json.loads(config_bytes, object_pairs_hook=_unique)
    labels = config["config"]["Labels"]
    if (
        labels.get("org.opencontainers.image.source")
        != "https://github.com/Blockfield/blockfield-server"
        or labels.get("io.blockfield.server-digest") != release
    ):
        raise ValueError("published image source/release mismatch")


def validate_release_record(document: dict) -> dict:
    """Check publication, checksum, build and image provenance consistency."""
    if not isinstance(document, dict):
        raise ValueError("release record must be an object")
    if (
        type(document.get("schema")) is not int
        or document["schema"] != SCHEMA
        or type(document.get("revision")) is not int
        or document["revision"] < 1
    ):
        raise ValueError("unsupported release record schema/revision")
    release, digest = document.get("release"), document.get("imageDigest")
    if not _matches(RELEASE, release):
        raise ValueError("release record needs a published vX.Y.Z tag")
    if not _matches(IMAGE_DIGEST, digest):
        raise ValueError("release record needs an immutable server imageDigest")
    for key in ("serverRuntime", "clientContract", "source"):
        if not isinstance(document.get(key), dict) or not document[key]:
            raise ValueError(f"release record needs {key} metadata")
    source = document["source"]
    if (
        source.get("repository") != "Blockfield/blockfield-server"
        or source.get("tag") != release
        or source.get("draft") is not False
        or source.get("prerelease") is not False
        or not _matches(re.compile(r"[0-9a-f]{40}"), source.get("commit"))
        or source["commit"] == "0" * 40
    ):
        raise ValueError("invalid published release provenance")
    try:
        published = datetime.fromisoformat(source["publishedAt"].replace("Z", "+00:00"))
        if published.tzinfo is None:
            raise ValueError("publication timestamp must include timezone")
        payload, sums = source["buildJson"], source["checksums"]
        if not isinstance(payload, str) or not isinstance(sums, str):
            raise ValueError("original build.json and SHA256SUMS required")
        build = json.loads(payload, object_pairs_hook=_unique)
        checksums = {}
        for line in sums.splitlines():
            checksum, name = line.split("  ", 1)
            if not _matches(SHA256, checksum) or name in checksums:
                raise ValueError("invalid or duplicate publication checksum")
            checksums[name] = checksum
        build_sha = hashlib.sha256(payload.encode()).hexdigest()
        if checksums.get("build.json") != build_sha:
            raise ValueError("published build.json checksum mismatch")
        _image_evidence(source, digest, release)
        provenance = source["provenance"]
        if provenance != {
            "imageDigest": digest,
            "sourceCommit": source["commit"],
            "buildSha256": build_sha,
        }:
            raise ValueError("image/build provenance mismatch")
        if build["build"] != release or build["sources"]["server"] != source["commit"]:
            raise ValueError("published build release/source mismatch")
        for key in ("server_runtime", "server_digest", "client", "compatibility"):
            if not _matches(SHA256, build[key]) or build[key] == "0" * 64:
                raise ValueError("invalid published build fingerprint/contract")
        # Published build.json carries the client version without the tag's "v".
        if not _matches(CLIENT_VERSION, build["client_version"]):
            raise ValueError("invalid published client version")
        for key in ("minecraft", "fabric"):
            if not _matches(re.compile(r"[0-9]+(?:\.[0-9]+){1,3}"), build[key]):
                raise ValueError("invalid published runtime version")
        if document["serverRuntime"] != {
            "fingerprint": build["server_runtime"],
            "serverDigest": build["server_digest"],
            "minecraft": build["minecraft"],
            "fabric": build["fabric"],
        } or document["clientContract"] != {
            "digest": build["client"],
            "version": build["client_version"],
            "compatibility": build["compatibility"],
        }:
            raise ValueError(
                "release runtime/client metadata differs from published build"
            )
    except (KeyError, TypeError, AttributeError, ValueError) as error:
        raise ValueError(f"invalid publication evidence: {error}") from error
    # Copy mutable input: a caller cannot change a committed identity in memory.
    return json.loads(
        json.dumps(
            {
                key: document[key]
                for key in (
                    "schema",
                    "revision",
                    "release",
                    "imageDigest",
                    "serverRuntime",
                    "clientContract",
                    "source",
                )
            },
            allow_nan=False,
        )
    )


def load_catalog(path: Path | None) -> dict[str, dict]:
    """Missing input grants no releases; every selection still requires resolve()."""
    raw = read_document(Path(path)) if path is not None else None
    if raw is None:
        return {}
    return {name: resolve(raw, name) for name in raw}


def resolve(catalog: dict[str, dict], release: str) -> dict:
    try:
        record = validate_release_record(catalog[release])
    except (KeyError, TypeError):
        raise ValueError(
            f"release {release!r} is not a verified published release"
        ) from None
    if (
        record["release"] != release
        and record["imageDigest"].split("@", 1)[1] != release
    ):
        raise ValueError("catalog key differs from record release and digest")
    return record


def load_approved_target(path: Path) -> dict | None:
    raw = read_document(path)
    if raw is not None:
        validate_release_record(raw)
        try:
            for key in ("operation", "runtimeOperation"):
                if str(UUID(raw[key])) != raw[key]:
                    raise ValueError("noncanonical target operation")
            if (
                type(raw["stateRevision"]) is not int
                or type(raw["selectionRevision"]) is not int
                or not 1 <= raw["selectionRevision"] <= raw["stateRevision"]
            ):
                raise ValueError("invalid target revision")
            if raw["previousTarget"] is not None:
                validate_release_record(raw["previousTarget"])
        except (KeyError, TypeError, AttributeError, ValueError) as error:
            raise ValueError("invalid revision-bound target projection") from error
    return raw
