"""Controller-owned desired state: the approved Workshop release (schema 2).

The host game-deploy file (desired.json schema-1, written by the deploy
runner) keeps its exact shape so schema-1 readers keep working. This module
owns the controller-side desired-state document: the approved Workshop
release plus the revision/operation binding that committed it. Schema-1
release records stay valid inside schema-2 documents, and a standalone
schema-1 record is accepted as the initial import. Like the other controller
files it stays plain JSON: readable with the controller dead.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from uuid import UUID

import releases as release_catalog

DESIRED_SCHEMA = 2
DESIRED_KEYS = frozenset(
    {
        "schema",
        "revision",
        "operation",
        "selectionRevision",
        "workshop",
        "previousWorkshop",
    }
)


def _canonical_operation(value) -> str:
    if not isinstance(value, str) or str(UUID(value)) != value:
        raise ValueError("desired state needs a canonical operation id")
    return value


def build_desired_document(
    *,
    revision: int,
    operation: str,
    selection_revision: int,
    workshop: dict,
    previous=None,
) -> dict:
    """Validate and return the canonical schema-2 desired-state document."""
    if type(revision) is not int or revision < 1:
        raise ValueError("desired state needs a positive state revision")
    if type(selection_revision) is not int or not 1 <= selection_revision <= revision:
        raise ValueError("desired state needs a selection revision within state")
    document = {
        "schema": DESIRED_SCHEMA,
        "revision": revision,
        "operation": _canonical_operation(operation),
        "selectionRevision": selection_revision,
        "workshop": release_catalog.validate_release_record(workshop),
        "previousWorkshop": (
            None
            if previous is None
            else release_catalog.validate_release_record(previous)
        ),
    }
    return document


def build_from_state(state: dict) -> dict | None:
    """Project a controller state journal to its desired-state document.

    None when no selection is committed yet (UNINITIALIZED or a journal that
    never selected a release). An inconsistent journal fails closed.
    """
    record = state.get("approvedTarget")
    selection = state.get("selection")
    if record is None or selection is None:
        return None
    try:
        operation = selection["operation"]
        selection_revision = selection["revision"]
        revision = state["revision"]
    except (KeyError, TypeError) as error:
        raise ValueError(f"invalid selection journal: {error}") from error
    return build_desired_document(
        revision=revision,
        operation=operation,
        selection_revision=selection_revision,
        workshop=record,
        previous=state.get("previousTarget"),
    )


def read_desired_state(path: Path) -> dict | None:
    """Read an owned desired-state file. None when absent.

    Accepts schema-2 documents and standalone schema-1 release records
    (the operator-verified initial import); the host game-deploy shape is
    not a desired-state document and is refused.
    """
    raw = release_catalog.read_document(Path(path))
    if raw is None:
        return None
    schema = raw.get("schema")
    if schema == DESIRED_SCHEMA:
        if set(raw) != DESIRED_KEYS:
            raise ValueError("invalid desired-state keys")
        return build_desired_document(
            revision=raw["revision"],
            operation=raw["operation"],
            selection_revision=raw["selectionRevision"],
            workshop=raw["workshop"],
            previous=raw["previousWorkshop"],
        )
    if schema == 1 and set(raw) == {
        "schema",
        "revision",
        "release",
        "imageDigest",
        "serverRuntime",
        "clientContract",
        "source",
    }:
        # Backward compatible: a bare release record adopts as the import.
        record = release_catalog.validate_release_record(raw)
        return {
            "schema": DESIRED_SCHEMA,
            "revision": record["revision"],
            "operation": None,
            "selectionRevision": record["revision"],
            "workshop": record,
            "previousWorkshop": None,
        }
    raise ValueError("unsupported desired-state schema")


def _atomic_write(path: Path, document: dict) -> None:
    payload = json.dumps(document, sort_keys=True, allow_nan=False).encode() + b"\n"
    path = Path(path)
    tmp_name = None
    fd, tmp_name = tempfile.mkstemp(
        prefix="." + path.name + "-", dir=path.parent, suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fchmod(handle.fileno(), 0o600)
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if tmp_name is not None:
            Path(tmp_name).unlink(missing_ok=True)


def project_desired_state(path: Path, document: dict) -> dict:
    """Publish the desired-state document under the single-writer lock.

    An identical document is a no-op (idempotent replay writes nothing).
    The same operation id with changed content is rejected; anything else
    follows the authority the caller already committed under its lock.
    """
    if document.get("schema") != DESIRED_SCHEMA or set(document) != DESIRED_KEYS:
        raise ValueError("invalid desired-state keys")
    target = build_desired_document(
        revision=document["revision"],
        operation=document["operation"],
        selection_revision=document["selectionRevision"],
        workshop=document["workshop"],
        previous=document["previousWorkshop"],
    )
    try:
        existing = read_desired_state(path)
    except ValueError:
        existing = None  # corrupt projection is repaired, never trusted
    if existing == target:
        return target
    if (
        existing is not None
        and existing.get("operation") is not None
        and existing["operation"] == target["operation"]
        and existing["revision"] == target["revision"]
    ):
        # Same committed (operation, revision) must always project the same
        # content: anything else is a changed duplicate, never a follow-up
        # (follow-ups advance the revision under a new operation).
        raise ValueError("desired-state operation reused with different content")
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, target)
    return target
