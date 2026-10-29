"""Verified published immutable server releases for Workshop selection.

The desired Workshop release is never taken from a live Game image, a
floating tag or a caller-supplied registry. It is selected from this
catalog of published immutable server releases, each bound to a digest
and build metadata. The last approved target is persisted beside the
controller state so Workshop can stage and wake while Game is off.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

SERVER_REPOSITORY = "ghcr.io/blockfield/blockfield-server"
IMAGE_DIGEST = re.compile(re.escape(SERVER_REPOSITORY) + r"@sha256:[0-9a-f]{64}")

SCHEMA = 1


def validate_release_record(document: dict) -> dict:
    """Check one published immutable server release record; return it normalized."""
    if not isinstance(document, dict):
        raise ValueError("release record must be an object")
    if document.get("schema") != SCHEMA or type(document.get("revision")) is not int:
        raise ValueError("unsupported release record schema")
    release = document.get("release")
    if not isinstance(release, str) or not release or len(release) > 128:
        raise ValueError("release record needs a pinned release")
    digest = document.get("imageDigest")
    if not isinstance(digest, str) or IMAGE_DIGEST.fullmatch(digest) is None:
        # Never accept :latest, a foreign registry or a bare image ID here.
        raise ValueError("release record needs an immutable server imageDigest")
    for key in ("serverRuntime", "clientContract", "source"):
        if not isinstance(document.get(key), dict):
            raise ValueError(f"release record needs {key} metadata")
    if not document["source"]:
        raise ValueError("release record source must bind the approved metadata")
    return {
        "schema": SCHEMA,
        "revision": document["revision"],
        "release": release,
        "imageDigest": digest,
        "serverRuntime": document["serverRuntime"],
        "clientContract": document["clientContract"],
        "source": document["source"],
    }


def load_catalog(path: Path | None) -> dict[str, dict]:
    """Read {release: record}; a missing file means no catalog is configured."""
    if path is None:
        return {}
    try:
        raw = json.loads(Path(path).read_bytes()[:65536])
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as error:
        raise ValueError(f"invalid releases catalog: {error}") from error
    if not isinstance(raw, dict):
        raise ValueError("invalid releases catalog")
    return {name: validate_release_record(record) for name, record in raw.items()}


def resolve(catalog: dict[str, dict], release: str) -> dict:
    """Return the verified record for a pinned release, or raise."""
    try:
        return catalog[release]
    except (KeyError, TypeError):
        raise ValueError(f"release {release!r} is not a verified published release")


def save_approved_target(path: Path, record: dict) -> None:
    from controller import atomic_write

    atomic_write(
        path,
        json.dumps(
            validate_release_record(record),
            ensure_ascii=False,
            sort_keys=True,
            allow_nan=False,
        ).encode(),
    )


def load_approved_target(path: Path) -> dict | None:
    try:
        raw = json.loads(path.read_bytes()[:65536])
    except (OSError, ValueError):
        return None
    try:
        return validate_release_record(raw)
    except (ValueError, TypeError, AttributeError):
        return None
