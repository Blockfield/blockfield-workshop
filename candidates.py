"""Workshop candidates and independent immutable map histories.

Legacy acceptance keeps the release workflow. Explicit expectedHead requests select
a coherent SSD bundle; game adapters consume its world and map.json together.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import time
import zipfile
import zlib
from contextlib import nullcontext
from pathlib import Path
from uuid import UUID, uuid4

import map_package

WORLD_KINDS = ("region", "entities", "poi")
MAP_ID = re.compile(r"[a-z0-9_]{1,32}")
CANDIDATE_ID = re.compile(r"[0-9a-f]{32}")
BRANCH_NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}\Z")
MISSING = object()
SNAPSHOT_MARGIN = 16 << 20
EXPORTS_KEPT = 16
ACCEPT_ONLY = object()
# A snapshot/commit call without any head expectation. Distinct from an
# explicit null head (the caller saw a headless map), which is checked.
EXPECTED_HEAD_ABSENT = object()
# A working copy whose base is not the current head is stale. Publishing from
# it is refused with this message; nothing is edited or merged. The way out
# is snapshot_working_copy plus checkout_fresh_copy.
STALE_COPY_MESSAGE = (
    "stale working copy: the map has a newer version; "
    "save your draft as a snapshot and start from the current version"
)
# Pre-upgrade pending reviews are not carried into the linear model: their
# versions and files stay in the archive, but the review itself is closed.
CANDIDATE_SUPERSEDE_REASON = "pre-upgrade review; submit again after the upgrade"
OWNER = (
    1000,
    1000,
)  # the game and the workshop run as uid/gid 1000 and read these files
BASE_MARKER = "blockfield-base.json"  # written by blockfield-mod WorkshopMaps when it seeds a draft from the template


def template_digest(root: Path) -> str | None:
    """Same algorithm as blockfield-mod WorkshopMaps: sorted "<kind>/<path>\0<sha256>\n" lines over region/entities/poi."""
    root = Path(root)
    if not (root / "region").is_dir():
        return None
    lines = []
    for kind in WORLD_KINDS:
        for path in (root / kind).rglob("*") if (root / kind).is_dir() else []:
            if path.is_file() and not path.is_symlink():
                lines.append((path.relative_to(root).as_posix(), _sha256(path)))
    return hashlib.sha256(
        "".join(f"{name}\0{digest}\n" for name, digest in sorted(lines)).encode()
    ).hexdigest()


class MapStore:
    def __init__(
        self,
        workshop_data: Path,
        templates: Path,
        published: Path,
        candidates: Path,
        backups: Path,
        *,
        repo_maps: Path | None = None,
        archive_root: Path | None = None,
        active_root: Path | None = None,
        legacy_candidates: Path | None = None,
        packages: Path | None = None,
        hold_saves=None,
        now=time.time,
    ):
        self.workshop_data = Path(workshop_data)
        self.templates = Path(templates)
        self.published = Path(published)
        self.candidates = Path(candidates)
        self.backups = Path(backups)
        self.legacy_candidates = Path(legacy_candidates) if legacy_candidates else None
        self.versions = MapVersions(
            archive_root or self.candidates.parent / "map-history",
            active_root or self.templates.parent / "map-active",
            now=now,
        )
        self.canonical_sessions = active_root is not None
        self.packages = (
            Path(packages) if packages else self.versions.archive / "packages"
        )
        # The game's copy of the image JSON (git server/lobby/blockfield/maps): a published overlay equal to it is retired.
        self.repo_maps = Path(repo_maps) if repo_maps else None
        self._digests = {}
        # () -> context manager that keeps the running workshop from writing region files meanwhile.
        self.hold_saves = hold_saves or nullcontext
        self._now = now
        self.limbo = None

    # ------------------------------------------------------------------ requests

    def check_create(self, map_id, name, modes, session_id, actor):
        if (
            not isinstance(map_id, str)
            or not re.fullmatch(r"[a-z0-9][a-z0-9_]{0,31}", map_id)
            or map_id in {"hub", "lobby", "limbo"}
        ):
            raise ValueError(
                "new map ID requires 1–32 lowercase letters, digits or underscores"
            )
        if not isinstance(session_id, str) or str(UUID(session_id)) != session_id:
            raise ValueError("new map requires a canonical session UUID")
        self.versions._actor(actor)
        config = map_package.seed_config(map_id, name, modes)
        existing = self.session(session_id)
        if existing is not None and existing["maps"] != [map_id]:
            raise ValueError("creation UUID is bound to another session")
        recorded = self.versions.archive / "versions" / map_id
        if recorded.is_symlink():
            raise ValueError("map ID already exists")
        if recorded.exists():
            seeds = [
                version
                for version in self.versions.history(map_id)
                if version["source"] == "created"
                and version.get("sourceId") == session_id
            ]
            if len(seeds) != 1 or seeds[0]["author"] != actor:
                raise ValueError("map ID already exists")
            seed = self.versions.read(map_id, seeds[0]["versionDigest"])
            if self.versions._config(seed["files"]) != config:
                raise ValueError("creation UUID reused with different map properties")
            return config, seeds[0]["versionDigest"]
        if existing is not None:
            raise ValueError("creation session refers to a missing map")
        locations = [
            self.templates / map_id,
            self.versions.active / map_id,
            self.versions.archive / "snapshots" / map_id,
        ]
        if (
            any(path.exists() or path.is_symlink() for path in locations)
            or map_id in self.map_ids()
            or any(
                (directory / f"{map_id}.json").exists()
                or (directory / f"{map_id}.json").is_symlink()
                for directory in (
                    self.published,
                    self.repo_maps,
                    self.workshop_data / "blockfield/maps",
                )
                if directory is not None
            )
        ):
            raise ValueError("map ID already exists")
        target = self.versions.active / "drafts" / session_id
        if (
            target.is_symlink()
            or target.exists()
            and (not target.is_dir() or any(target.iterdir()))
        ):
            raise ValueError("creation requires a new isolated session UUID")
        for directory in (self.versions.archive, self.versions.active):
            while not directory.exists():
                directory = directory.parent
            if shutil.disk_usage(directory).free < SNAPSHOT_MARGIN:
                raise OSError("not enough free space to create a map")
        return config, None

    def create_map(self, map_id, name, modes, session_id, actor):
        config, version = self.check_create(map_id, name, modes, session_id, actor)
        fresh = version is None
        archive = self._session_path(session_id)
        copy = self.versions.working_copy(map_id, session_id)
        records = self.versions.archive / "versions" / map_id
        try:
            if archive.exists():
                self.versions.validate_working_copy(map_id, session_id)
            if fresh:
                files = {
                    "map.json": self.versions._put_blob(
                        json.dumps(config, ensure_ascii=False, indent=2).encode()
                    ),
                    "world/region/r.0.0.mca": self.versions._put_blob(
                        map_package.seed_region()
                    ),
                }
                version = self.versions._record(
                    map_id,
                    files,
                    parent=None,
                    author=actor,
                    created_at=int(self._now() * 1000),
                    source="created",
                    source_id=session_id,
                    note="Create private map",
                )
            if not copy.exists() and not copy.is_symlink():
                self.versions.checkout(map_id, version, session_id)
            self.versions.validate_working_copy(map_id, session_id)
            session = self.bind_session(session_id, [map_id], actor)
        except Exception:
            # Only an unpublished first attempt is rolled back. A durable archive keeps its complete private work.
            if fresh and not archive.exists():
                for path in (copy, records):
                    if path.is_dir() and not path.is_symlink():
                        shutil.rmtree(path)
                        _sync_directory(path.parent)
            raise
        return {
            "mapId": map_id,
            "versionDigest": version,
            "session": session,
        }

    def copy_properties(self, map_id, working_copy_id):
        config = self._map_json(map_id, working_copy_id)
        return {"workingCopyId": working_copy_id, "name": config["name"]}

    def _candidate_config(self, meta):
        if meta.get("native"):
            manifest = self.versions.read(
                meta["mapId"], meta.get("mergeVersion") or meta["versionDigest"]
            )
            return self.versions._config(manifest["files"])
        with zipfile.ZipFile(self.archive_path(meta["id"])) as archive:
            entry = archive.getinfo("map.json")
            if entry.file_size > 1 << 20:
                raise ValueError("map config too large")
            return map_package._json(archive.read(entry))

    def validate_test(self, candidate_id, mode_id=None):
        meta = next(
            (
                item
                for item in self.list()
                if item["id"] == candidate_id or item.get("sha256") == candidate_id
            ),
            None,
        )
        if meta is None:
            raise ValueError("unknown candidate")
        config = self._candidate_config(meta)
        modes = map_package.supported_modes(config.get("supported_modes", []))
        if mode_id is not None and mode_id not in modes:
            raise ValueError("candidate does not support the selected mode")
        map_package.validate_playable(config)
        return modes

    def rename(self, map_id, name, working_copy_id):
        name = map_package.map_name(name)
        self.versions.validate_working_copy(map_id, working_copy_id)
        config = self._map_json(map_id, working_copy_id)
        config["name"] = name
        _atomic_json(
            self.versions.working_copy(map_id, working_copy_id) / "map.json",
            config,
            mode=0o664,
        )
        return {"mapId": map_id, "workingCopyId": working_copy_id, "name": name}

    def check(self, map_id, working_copy_id=None) -> Path:
        """The draft of a map that can be snapshotted; raises before the controller stops anything."""
        if (zlib.ZLIB_VERSION, zlib.ZLIB_RUNTIME_VERSION) != ("1.3.2", "1.3.2"):
            raise RuntimeError(
                "Candidate ZIPs require zlib 1.3.2; install the pinned workshop Python"
            )
        if not isinstance(map_id, str) or not MAP_ID.fullmatch(map_id):
            raise ValueError("invalid map id")
        world = self._draft_world(map_id, working_copy_id)
        if not (world / "region").is_dir():
            raise ValueError("the map was never opened in the workshop")
        self._map_json(map_id, working_copy_id)
        need = (
            sum(
                p.stat().st_size
                for kind in WORLD_KINDS
                for p in (world / kind).glob("*")
                if p.suffix in (".mca", ".mcc")
            )
            + SNAPSHOT_MARGIN
        )
        target = self.candidates
        while not target.exists():
            target = target.parent
        if shutil.disk_usage(target).free < need:
            raise OSError(
                f"not enough free space for the candidate: need {need >> 20} MiB"
            )
        return world

    def create(
        self,
        map_id: str,
        author: dict,
        note: str = "",
        *,
        working_copy_id=None,
        expected_head=EXPECTED_HEAD_ABSENT,
        review=True,
    ) -> dict:
        # Linear history: a commit parents the exact current head (CAS); the
        # head itself advances only on publish/activate/restore, never on merge.
        world = self.check(map_id, working_copy_id)
        if working_copy_id is not None:
            # Manifest-authoritative: also repairs a torn base write on read.
            _, base_version, _ = self.versions._working_copy_manifest(
                map_id, working_copy_id
            )
        else:
            base_version = self._base_version(map_id)
        head = self.versions.head(map_id)
        head_digest = head["versionDigest"] if head else None
        if expected_head is not EXPECTED_HEAD_ABSENT:
            self.versions.check_head(map_id, expected_head)
            if expected_head is None:
                # An explicit null head pins a headless map: the first commit
                # bootstraps history from the template import.
                parent = base_version
            elif base_version != expected_head:
                # Stale base with a fresh expectation: this PR removed merges,
                # so an automatic file rebase is a merge in disguise. Refuse
                # and leave the draft untouched; the explicit snapshot path
                # preserves it.
                raise ValueError(STALE_COPY_MESSAGE)
            else:
                parent = expected_head
        elif head_digest is None:
            parent = base_version
        else:
            if base_version != head_digest:
                raise ValueError(STALE_COPY_MESSAGE)
            parent = head_digest
        self.candidates.mkdir(parents=True, exist_ok=True)
        # Exports run under the controller lock, so any temporary archive here was left by one that was killed.
        for stale in self.candidates.glob(".*.zip.tmp"):
            stale.unlink(missing_ok=True)
        candidate_id = uuid4().hex
        now_ms = int(self._now() * 1000)
        note = str(note)[:200]
        with self.hold_saves():
            config = self._map_json(map_id, working_copy_id)
            if review:
                map_package.validate_playable(config)
            files = self.versions.world_files(
                world, json.dumps(config, ensure_ascii=False, indent=2).encode()
            )
        # Publish refs only after autosave restoration has succeeded.
        # The version records its origin: a review candidate points back at
        # the candidate, a plain commit at the copy that made it — so publish
        # can verify the copy it is about to advance actually produced it.
        version = self.versions.commit_head(
            map_id,
            files,
            author,
            note or f"Update map {map_id}",
            parent,
            working_copy_id,
            base_version,
            source="candidate" if review else "commit",
            source_id=candidate_id if review else None,
        )
        if not review:
            return {
                "mapId": map_id,
                "versionDigest": version,
            }
        meta = {
            "id": candidate_id,
            "mapId": map_id,
            "status": "pending",
            "author": author,
            "note": note,
            "createdAt": now_ms,
            "sha256": None,
            "size": None,
            "native": True,
            "versionDigest": version,
            "regions": sum(1 for name in files if name.startswith("world/region/")),
            "base": self._draft_base(world),
            "baseVersion": base_version,
            "targetHead": head_digest,
            "workingCopyId": working_copy_id,
        }
        self._write_meta(meta)
        return meta

    def commit(
        self,
        map_id,
        author,
        note,
        *,
        working_copy_id,
        expected_head=EXPECTED_HEAD_ABSENT,
    ):
        if not isinstance(note, str) or not note.strip() or len(note) > 200:
            raise ValueError("commit message required (1–200 characters)")
        return self.create(
            map_id,
            author,
            note,
            working_copy_id=working_copy_id,
            expected_head=expected_head,
            review=False,
        )

    def review(self, map_id, author, note, *, working_copy_id, expected_head):
        # Linear review freezes one immutable version: it must be the fresh
        # child of the current head (anything else is stale, never merged).
        # A headless map reviews its first commit with targetHead None.
        self.versions._actor(author)
        self.versions.working_copy(map_id, working_copy_id)
        manifest = self.versions.read(map_id, expected_head)
        head = self.versions.head(map_id)
        if head is None:
            target = None
        else:
            if head["versionDigest"] == expected_head:
                raise ValueError("commit changes before requesting review")
            if manifest.get("parent") != head["versionDigest"]:
                raise ValueError("review target changed; commit on the current head")
            target = head["versionDigest"]
        if not isinstance(note, str) or len(note) > 200:
            raise ValueError("review message too long")
        map_package.validate_playable(self.versions._config(manifest["files"]))
        for digest in manifest["files"].values():
            self.versions._verify_blob(digest)
        meta = {
            "id": uuid4().hex,
            "mapId": map_id,
            "status": "pending",
            "author": author,
            "note": note or manifest["note"],
            "createdAt": int(self._now() * 1000),
            "sha256": None,
            "size": None,
            "native": True,
            "versionDigest": expected_head,
            "baseVersion": manifest["parent"],
            "workingCopyId": working_copy_id,
            "targetHead": target,
            "base": self._draft_base(self._draft_world(map_id, working_copy_id)),
            "regions": sum(
                name.startswith("world/region/") for name in manifest["files"]
            ),
        }
        self._write_meta(meta)
        return meta

    def publish(
        self,
        candidate_id: str,
        actor: dict,
        *,
        force: bool = False,
        expected_head=ACCEPT_ONLY,
        resolutions=None,
    ) -> dict:
        meta = self._pending(
            candidate_id, allow_accepted=expected_head is not ACCEPT_ONLY
        )
        # Linear history: branch/merge fields are legacy reads only. A stale
        # head is an error here, never a merge; force only waives the
        # template-base guard, never the head CAS.
        if meta.get("native"):
            if expected_head is ACCEPT_ONLY:
                raise ValueError("publication requires an explicit expectedHead")
            if resolutions is not None:
                raise ValueError("linear map history accepts no conflict choices")
            map_id = meta["mapId"]
            head = self.versions.head(map_id)
            if (
                expected_head is not None
                and not force
                and head is not None
                and meta.get("status") == "accepted"
                and meta.get("committedVersion") == meta.get("versionDigest")
                and head["versionDigest"] == meta.get("versionDigest")
                and (
                    meta.get("targetHead") is None
                    or meta["targetHead"] == expected_head
                )
            ):
                # A retry of this candidate's own committed publication: the
                # accepted status plus the marker written before activation
                # prove the publish, so only best-effort work remains. A
                # pending duplicate that merely shares the version never
                # enters here; it falls through to the target/parent checks
                # and fails. A forced retry still refuses a stale head: force
                # waives only the template-base guard, never the head CAS.
                self._settle_publish(meta)
                return meta
            self.versions.check_head(map_id, expected_head)
            # Pre-upgrade records carry baseVersion but no targetHead: their
            # target is derived from the immutable version's parent below.
            if (
                meta.get("targetHead") is not None
                and meta["targetHead"] != expected_head
            ):
                raise ValueError("review target changed; request a fresh review")
            if expected_head is not None:
                manifest = self.versions.read(map_id, meta["versionDigest"])
                if manifest.get("parent") != expected_head:
                    raise ValueError("stale map head: the review is behind the head")
            map_package.validate_playable(self._candidate_config(meta))
            manifest = self.versions.read(map_id, meta["versionDigest"])
            for digest in manifest["files"].values():
                self.versions._verify_blob(digest)
            if not force:
                current = self.versions.live_world(map_id, self.templates / map_id)
                live = template_digest(current)
                # No live template (fresh map, no head and no template dir):
                # nothing exists yet that a snapshot could clobber.
                if live is not None:
                    if meta.get("base") is None:
                        raise ValueError(
                            "base unknown: the draft predates base tracking; confirm to publish anyway"
                        )
                    if meta["base"] != live:
                        raise ValueError(
                            "base changed: the map's template changed after this draft was copied; confirm to overwrite"
                        )
            # Publication is a transaction: validate, stage, move the head (the
            # commit point). A failed stage or activation leaves the copy at
            # its old base, so the unchanged head stays publishable by another
            # review.
            self.versions.prepare(map_id, meta["versionDigest"])
            meta.update(
                status="accepted",
                decidedBy=actor,
                decidedAt=int(self._now() * 1000),
                forced=force,
                committedVersion=meta["versionDigest"],
            )
            meta.pop("sourceBranch", None)
            self._write_meta(meta)
            self.activate(map_id, meta["versionDigest"], actor, expected_head)
            # The copy advance after activation is best effort: the publish is
            # final at activation. A failing advance is logged and leaves the
            # copy stale (a stale copy is refused, never merged), and the
            # result reports the publish plus copyAdvanced False.
            self._settle_publish(meta)
            return meta
        map_package.validate_playable(self._candidate_config(meta))
        if (
            expected_head is not ACCEPT_ONLY
            and expected_head is not None
            and not meta.get("versionDigest")
        ):
            raise ValueError(
                "legacy candidate has no verified commit; re-snapshot it first"
            )
        newer = [
            m
            for m in self.list()
            if m["mapId"] == meta["mapId"]
            and m["status"] in {"accepted", "published"}
            and m["createdAt"] > meta["createdAt"]
        ]
        if newer:
            raise ValueError("a newer version of this map is already published")
        if meta.get("native"):
            manifest = self.versions.read(meta["mapId"], meta["versionDigest"])
            for digest in manifest["files"].values():
                self.versions._verify_blob(digest)
        else:
            archive_path = self.archive_path(candidate_id)
            manifest = map_package.validate(archive_path, meta["sha256"])
        map_id = manifest["mapId"]
        current = self.versions.live_world(map_id, self.templates / map_id)
        live = template_digest(current)
        # The draft was copied from a template; if that template changed since (another publish, rollback, a manual
        # copy), publishing would silently throw those changes away. Only an explicit second confirmation may.
        if not force:
            if meta.get("base") is None:
                raise ValueError(
                    "base unknown: the draft predates base tracking; confirm to publish anyway"
                )
            if meta["base"] != live:
                raise ValueError(
                    "base changed: the map's template changed after this draft was copied; confirm to overwrite"
                )
        if expected_head is not ACCEPT_ONLY:
            # CAS before recording: a stale head must not strand an import.
            self.versions.check_head(map_id, expected_head)
        version = meta.get("versionDigest")
        if version is None:
            version = self.versions.import_package(
                archive_path,
                meta["sha256"],
                parent=self._base_version(map_id),
                author=meta["author"],
                created_at=meta["createdAt"],
                source_id=candidate_id,
                note=meta.get("note", ""),
            )
        if expected_head is not ACCEPT_ONLY:
            if (
                not force
                and expected_head is not None
                and meta.get("baseVersion") != expected_head
            ):
                raise ValueError(
                    "base changed: the map head changed after this draft was copied"
                )
            self.versions.prepare(map_id, version)
        meta.update(
            status="accepted",
            decidedBy=actor,
            decidedAt=int(self._now() * 1000),
            forced=force,
            versionDigest=version,
            committedVersion=version,
        )
        self._write_meta(meta)
        if expected_head is not ACCEPT_ONLY:
            self.activate(map_id, version, actor, expected_head)
        return meta

    def _settle_publish(self, meta) -> None:
        """Best-effort work after the activation commit point.

        The publish is final at activation; only the working-copy advance and
        the final metadata rewrite remain. Either may fail (e.g. disk full)
        without unpublishing the head: the copy stays stale for the explicit
        path, and the result reports the publish plus copyAdvanced False or
        metaSynced False. The committed state itself always derives from the
        head, never from this metadata.
        """
        copy_advanced = True
        copy_id = meta.get("workingCopyId")
        if copy_id is not None and not self._version_from_copy(meta):
            # The published version was not snapshotted from this candidate's
            # copy (e.g. a pre-upgrade review of another session's tip):
            # advancing it would strand the copy on foreign content, so it
            # stays stale for the explicit path instead.
            copy_advanced = False
            print(
                "candidate: published version is not from this working copy; "
                "leaving the copy stale",
                file=sys.stderr,
            )
        elif copy_id is not None:
            try:
                copy_advanced = self.versions.advance_working_copy(
                    meta["mapId"], copy_id, meta["versionDigest"]
                )
            except Exception as error:
                copy_advanced = False
                print(
                    f"candidate: working copy advance failed after publish: {error!r}",
                    file=sys.stderr,
                )
        meta["copyAdvanced"] = copy_advanced
        try:
            self._write_meta(meta)
        except Exception as error:
            meta["metaSynced"] = False
            print(
                f"candidate: publish metadata sync failed after activation: {error!r}",
                file=sys.stderr,
            )

    def _version_from_copy(self, meta) -> bool:
        """The published version must have been snapshotted from this candidate's copy.

        The version manifest points back at its origin: a candidate-created
        version carries this candidate's id, a plain commit carries the
        committing copy's id. Anything else (another candidate's version,
        another copy's commit, a restore) must never advance this copy.
        """
        try:
            manifest = self.versions.read(meta["mapId"], meta["versionDigest"])
        except (OSError, ValueError):
            return False
        return manifest.get("sourceId") in (
            meta.get("id"),
            meta.get("workingCopyId"),
        )

    def reject(self, candidate_id: str, actor: dict) -> dict:
        meta = self._pending(candidate_id)
        meta.update(
            status="rejected", decidedBy=actor, decidedAt=int(self._now() * 1000)
        )
        self._write_meta(meta)
        return meta

    def rollback(
        self,
        map_id: str,
        actor: dict,
        *,
        version_digest=None,
        expected_head=ACCEPT_ONLY,
    ) -> dict:
        if version_digest is None or expected_head is ACCEPT_ONLY:
            raise ValueError("rollback requires versionDigest and expectedHead")
        version = self.versions.prepare_restore(
            map_id, version_digest, actor, expected_head
        )
        if map_id == "limbo" and self.limbo is not None and version != version_digest:
            self.limbo.reuse(self.versions, version_digest, version)
        return self.activate(map_id, version, actor, expected_head)

    def activate(self, map_id: str, version: str, actor: dict, expected_head) -> dict:
        if self.versions.read(map_id, version)["kind"] == "limbo":
            if self.limbo is None:
                raise ValueError(
                    "Limbo native export and stopped-proxy selection are not configured"
                )
            return self.limbo.activate(self.versions, version, actor, expected_head)
        return self.versions.activate(map_id, version, actor, expected_head)

    def export_limbo(self, version: str, backend) -> dict:
        if self.limbo is None:
            raise ValueError("Limbo native export is not configured")
        manifest = self.versions.read("limbo", version)
        if (
            manifest["source"] != "template"
            and not any(
                version
                in {candidate.get("versionDigest"), candidate.get("mergeVersion")}
                and candidate["status"] in {"accepted", "published"}
                for candidate in self.list()
            )
            and manifest["source"] != "restore"
        ):
            raise ValueError(
                "Limbo native export requires an accepted immutable version"
            )
        if (self.limbo.artifacts / f"{version}.json").exists() or (
            self.limbo.artifacts / f"{version}.json"
        ).is_symlink():
            return self.limbo.ready(self.versions, version)
        job, _ = self.limbo.prepare_request(self.versions, version)
        backend.export_limbo(job)
        return self.limbo.install_job(self.versions, version, job)

    def checkout(self, map_id: str, version_digest: str, working_copy_id: str) -> dict:
        return self.versions.checkout(map_id, version_digest, working_copy_id)

    def snapshot_working_copy(
        self, map_id: str, name: str, working_copy_id: str, actor: dict
    ) -> dict:
        """Preserve a (possibly stale) draft as a named snapshot; the copy is untouched.

        Records the draft's current world as an immutable version parented at
        the copy's base — never at the head, never merged — then pins it with
        the existing snapshot create. An unedited draft adds no new content,
        so the snapshot points at its base version. The draft is read only
        after hold_saves() flushed and paused the running editor.
        """
        self.versions._actor(actor)
        self.versions._branch_name(name)
        with self.hold_saves():
            target, base_version, base_manifest = self.versions._working_copy_manifest(
                map_id, working_copy_id
            )
            config = self._map_json(map_id, working_copy_id)
            files = self.versions.world_files(
                target / "world",
                json.dumps(config, ensure_ascii=False, indent=2).encode(),
            )
            files = self.versions._same_config_files(files, base_manifest["files"])
        if files == base_manifest["files"]:
            version = base_version
        else:
            version = self.versions._record(
                map_id,
                files,
                parent=base_version,
                author=actor,
                created_at=int(self._now() * 1000),
                source="snapshot",
                source_id=working_copy_id,
                note=f"Draft snapshot {name}",
            )
        return self.versions.create_snapshot(map_id, name, version, actor)

    def checkout_fresh_copy(
        self, map_id: str, working_copy_id: str, actor: dict
    ) -> dict:
        """Start from the current head: materialize it into a new copy id.

        The copy goes through the normal session path, so an archived session
        record binds the new id exactly like a session start — an inbox
        client can then select it for editing via an archived resume. The
        draft is never read, but the copy lands on disk beside the live
        editor, so this also runs after hold_saves(). An existing id is
        refused rather than overwritten.
        """
        target = self.versions.working_copy(map_id, working_copy_id)
        if target.exists() or target.is_symlink():
            raise ValueError("working copy already exists; resume it instead")
        with self.hold_saves():
            return self.prepare_session(working_copy_id, [map_id], actor=actor)[0]

    @staticmethod
    def _is_legacy_pending(meta: dict) -> bool:
        """A pending review the linear model cannot honor.

        Post-upgrade candidates record targetHead + workingCopyId at
        creation; branch-era reviews recorded those fields under
        shared-branch semantics (two sessions could select the same branch,
        two reviews could share one version), which the linear publish path
        cannot verify. Either shape stays out of the linear model.
        """
        return meta.get("status") == "pending" and (
            "targetHead" not in meta
            or "workingCopyId" not in meta
            or meta.get("sourceBranch") is not None
        )

    def legacy_pending_candidates(self) -> list[dict]:
        """Pending reviews migration would supersede (lock-free read)."""
        return [m for m in self.list() if self._is_legacy_pending(m)]

    def migrate_branches_to_snapshots(self) -> dict:
        """Store-level linearization: pending pre-upgrade reviews, stale legacy
        drafts, then branch refs.

        Pre-upgrade pending reviews become `superseded` first (their versions
        and files stay in the archive, nothing is deleted): the linear model
        cannot verify their copy/version binding. Then drafts are preserved
        (each snapshot written and fsynced before the next), and the branch
        refs are removed only afterwards by the archive migration — a stop
        between the steps keeps the refs, so the rerun still finds and
        preserves every draft. Each step is idempotent on rerun: superseded
        reviews stay superseded, an already preserved draft reuses its
        snapshot, and removed refs stay removed. Every stale edited draft on
        a map that had legacy branch refs is preserved as a migrated-draft-<hex> snapshot (the draft's content can no longer be
        committed, so without this it would be silently lost). Each draft is
        read after hold_saves(); when the editor cannot be paused the draft
        is logged and left for the explicit snapshot_working_copy path.
        Unedited drafts need no snapshot: their content is already immutable
        history. Assumes the controller lock, like every other map mutation.
        """
        superseded = []
        for meta in self.list():
            if not self._is_legacy_pending(meta):
                continue
            meta.update(
                status="superseded",
                supersedeReason=CANDIDATE_SUPERSEDE_REASON,
                supersededAt=int(self._now() * 1000),
            )
            self._write_meta(meta)
            superseded.append(
                {
                    "id": meta["id"],
                    "mapId": meta.get("mapId"),
                    "versionDigest": meta.get("versionDigest"),
                }
            )
        branch_authors: dict[str, dict] = {}
        ref_maps: set[str] = set()
        root = self.versions.archive / "branches"
        if not root.is_symlink() and root.is_dir():
            for map_dir in sorted(root.iterdir()):
                if map_dir.is_symlink() or not map_dir.is_dir():
                    continue
                for path in sorted(map_dir.glob("*.json")):
                    try:
                        value = map_package._json(path.read_bytes())
                    except (OSError, ValueError):
                        continue
                    if (
                        isinstance(value, dict)
                        and value.get("mapId") == map_dir.name
                        and isinstance(value.get("createdBy"), dict)
                    ):
                        branch_authors.setdefault(map_dir.name, value["createdBy"])
                        ref_maps.add(map_dir.name)
        draft_snapshots, draft_skipped = [], []
        drafts = self.versions.active / "drafts"
        if ref_maps and not drafts.is_symlink() and drafts.is_dir():
            for copy_dir in sorted(drafts.iterdir()):
                if copy_dir.is_symlink() or not copy_dir.is_dir():
                    continue
                try:
                    working_copy_id = str(UUID(copy_dir.name))
                except (ValueError, AttributeError):
                    continue
                for map_dir in sorted(copy_dir.iterdir()):
                    if (
                        map_dir.is_symlink()
                        or not map_dir.is_dir()
                        or map_dir.name not in ref_maps
                    ):
                        continue
                    map_id = map_dir.name
                    try:
                        head = self.versions.head(map_id)
                        head_digest = head["versionDigest"] if head else None
                        target, base, base_manifest = (
                            self.versions._working_copy_manifest(
                                map_id, working_copy_id
                            )
                        )
                    except ValueError as error:
                        draft_skipped.append(
                            {
                                "mapId": map_id,
                                "workingCopyId": working_copy_id,
                                "reason": str(error),
                            }
                        )
                        print(
                            f"workshop: draft snapshot skipped for {map_id}: {error!r}",
                            file=sys.stderr,
                        )
                        continue
                    if head_digest is None or base == head_digest:
                        continue
                    actor = branch_authors.get(map_id)
                    try:
                        session = self.session(working_copy_id)
                    except ValueError:
                        session = None
                    if session is not None:
                        actor = session["startedBy"]
                    if actor is None:
                        draft_skipped.append(
                            {
                                "mapId": map_id,
                                "workingCopyId": working_copy_id,
                                "reason": "no draft author",
                            }
                        )
                        print(
                            "workshop: draft snapshot skipped for"
                            f" {map_id}: no draft author",
                            file=sys.stderr,
                        )
                        continue
                    try:
                        with self.hold_saves():
                            config = self._map_json(map_id, working_copy_id)
                            files = self.versions.world_files(
                                target / "world",
                                json.dumps(
                                    config, ensure_ascii=False, indent=2
                                ).encode(),
                            )
                            files = self.versions._same_config_files(
                                files, base_manifest["files"]
                            )
                    except Exception as error:
                        draft_skipped.append(
                            {
                                "mapId": map_id,
                                "workingCopyId": working_copy_id,
                                "reason": str(error),
                            }
                        )
                        print(
                            f"workshop: draft snapshot skipped for {map_id}: {error!r}",
                            file=sys.stderr,
                        )
                        continue
                    if files == base_manifest["files"]:
                        continue
                    version = None
                    for already in self.versions.snapshots(map_id):
                        if not already["name"].startswith("migrated-draft-"):
                            continue
                        if (
                            self.versions.read(map_id, already["versionDigest"])[
                                "files"
                            ]
                            == files
                        ):
                            # A rerun after a stop between the steps: the same
                            # draft content already has a snapshot.
                            version = already["versionDigest"]
                            break
                    if version is None:
                        version = self.versions._record(
                            map_id,
                            files,
                            parent=base,
                            author=actor,
                            created_at=int(self._now() * 1000),
                            source="snapshot",
                            source_id=working_copy_id,
                            note=f"Migrated stale draft of {map_id}",
                        )
                    name = f"migrated-draft-{version[:12]}"
                    try:
                        existing = self.versions.snapshot(map_id, name)
                    except ValueError:
                        existing = None
                    if existing is not None and (existing["versionDigest"] == version):
                        continue
                    snap = self.versions.create_snapshot(map_id, name, version, actor)
                    draft_snapshots.append(
                        {
                            "mapId": map_id,
                            "name": name,
                            "versionDigest": snap["versionDigest"],
                        }
                    )
        # The branch refs go last: their snapshots are written and re-read
        # inside, and only then is each ref removed — so every draft above is
        # already preserved before the first ref disappears.
        summary = self.versions.migrate_branches_to_snapshots()
        summary["candidatesSuperseded"] = superseded
        summary["draftSnapshots"] = draft_snapshots
        summary["draftSkipped"] = draft_skipped
        return summary

    def prepare_session(
        self,
        working_copy_id: str,
        maps: list[str],
        *,
        resume_only=False,
        branches=None,
        actor=None,
    ) -> list[dict]:
        if (
            not isinstance(maps, list)
            or not maps
            or any(not isinstance(map_id, str) for map_id in maps)
            or len(set(maps)) != len(maps)
        ):
            raise ValueError("session requires distinct selected maps")
        if set(maps) - self.map_ids():
            raise ValueError("session selects an unavailable map")
        if branches is not None:
            # Pre-linear compat: named branch selections are validated for
            # shape only, then ignored — one head per map. Live sessions keep
            # their working copies, which are keyed by session UUID.
            if (
                not isinstance(branches, dict)
                or set(branches) != set(maps)
                or any(
                    not isinstance(name, str) or name == "main"
                    for name in branches.values()
                )
            ):
                raise ValueError("session branches must match its selected maps")
            self.versions._actor(actor)
            for name in branches.values():
                self.versions._branch_name(name)
            existing = self.session(working_copy_id)
            if existing is not None and existing["maps"] != maps:
                raise ValueError(
                    "working copy is already bound to another map selection"
                )
        copies, missing = [], []
        for map_id in maps:
            target = self.versions.working_copy(map_id, working_copy_id)
            copy = (
                self.versions.validate_working_copy(map_id, working_copy_id)
                if target.exists() or target.is_symlink()
                else None
            )
            if copy is not None:
                copies.append(copy)
            else:
                if resume_only:
                    raise ValueError("selected working copy is missing")
                head = self.versions.head(map_id)
                if head is None:
                    raise ValueError(
                        f"map {map_id} has no active head; import and activate it first"
                    )
                self.versions.prepare(map_id, head["versionDigest"])
                missing.append((map_id, head["versionDigest"]))
        created_copies = []
        try:
            for map_id, version in missing:
                self.versions.check_head(map_id, version)
                self.checkout(map_id, version, working_copy_id)
                created_copies.append(
                    self.versions.working_copy(map_id, working_copy_id)
                )
                copies.append(
                    self.versions.validate_working_copy(map_id, working_copy_id)
                )
            if actor is not None:
                self.bind_session(working_copy_id, maps, actor)
        except Exception:
            # A renamed session file is the commit point; a failed final
            # fsync must not destroy its complete copies.
            if self._session_path(working_copy_id).exists():
                raise
            # Only this attempt's new copies are removed; pre-existing dirty
            # work stays intact.
            for path in created_copies:
                shutil.rmtree(path)
                _sync_directory(path.parent)
            raise
        return copies

    def _session_path(self, session_id):
        self.versions.working_copy("session", session_id)
        directory = self.versions.archive / "sessions"
        if directory.is_symlink() or directory.exists() and not directory.is_dir():
            raise ValueError("unsafe archived session directory")
        return directory / f"{session_id}.json"

    @staticmethod
    def _selection_digest(value):
        # Linear sessions track maps only; pre-linear archives also carry
        # branches and keep their old digest.
        fields = {key: value[key] for key in ("id", "maps") if key in value}
        if "branches" in value:
            fields["branches"] = value["branches"]
        return hashlib.sha256(
            json.dumps(fields, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def session(self, session_id):
        path = self._session_path(session_id)
        if not path.exists() and not path.is_symlink():
            return None
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 65536:
            raise ValueError("archived session missing or unsafe")
        value = map_package._json(path.read_bytes())
        if (
            not isinstance(value, dict)
            or type(value.get("schema")) is not int
            or value["schema"] != 1
            or value.get("id") != session_id
            or not isinstance(value.get("maps"), list)
            or not value["maps"]
            or any(not isinstance(item, str) for item in value["maps"])
            or len(value["maps"]) != len(set(value["maps"]))
            or ("branches" in value and not isinstance(value["branches"], dict))
            or ("branches" in value and set(value["branches"]) != set(value["maps"]))
            or type(value.get("startedAt")) is not int
            or value["startedAt"] < 0
        ):
            raise ValueError("invalid archived session")
        self.versions._actor(value.get("startedBy"))
        for map_id, branch in (value.get("branches") or {}).items():
            self.versions._map_id(map_id)
            if self.versions._branch_name(branch) == "main":
                raise ValueError("session cannot edit protected main")
        if value.get("selectionDigest") != self._selection_digest(value):
            raise ValueError("archived session selection checksum mismatch")
        return value

    def bind_session(self, session_id, maps, actor, branches=None):
        self.versions._actor(actor)
        existing = self.session(session_id)
        if existing is not None:
            if existing["maps"] != maps:
                raise ValueError(
                    "working copy is already bound to another map selection"
                )
            return existing
        value = {
            "schema": 1,
            "id": session_id,
            "maps": maps,
            "startedAt": int(self._now() * 1000),
            "startedBy": actor,
        }
        value["selectionDigest"] = self._selection_digest(value)
        path = self._session_path(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_json(path, value, owner=False, mode=0o444)
        return value

    def sessions(self, offset=0, limit=100):
        if (
            type(offset) is not int
            or offset < 0
            or type(limit) is not int
            or not 1 <= limit <= 100
        ):
            raise ValueError("invalid session page")
        directory = self._session_path("00000000-0000-0000-0000-000000000000").parent
        paths = sorted(
            directory.glob("*.json"),
            key=lambda path: path.stat().st_mtime_ns,
            reverse=True,
        )
        page = paths[offset : offset + limit]
        return {
            "sessions": [self.session(path.stem) for path in page],
            "total": len(paths),
            "next": offset + limit if offset + limit < len(paths) else None,
        }

    def map_ids(self) -> set[str]:
        maps = set()
        for root in (self.templates, self.versions.archive / "versions"):
            if root.is_dir():
                maps.update(
                    path.name
                    for path in root.iterdir()
                    if path.is_dir()
                    and not path.is_symlink()
                    and MAP_ID.fullmatch(path.name)
                    and (root != self.templates or (path / "region").is_dir())
                )
        return maps

    def catalog(self) -> list[dict]:
        histories = {entry["mapId"]: entry for entry in self.versions.export()}
        catalog = []
        for map_id in sorted(self.map_ids()):
            entry = histories.get(
                map_id, {"mapId": map_id, "head": None, "versions": []}
            )
            head = entry["head"]
            if head:
                config = map_package._json(
                    (Path(head["bundle"]) / "map.json").read_bytes()
                )
            else:
                path = self._base_config_path(map_id)
                if path:
                    config = map_package._json(path.read_bytes())
                elif entry["versions"]:
                    version = self.versions.read(
                        map_id, entry["versions"][0]["versionDigest"]
                    )
                    config = map_package._json(
                        self.versions._verify_blob(
                            version["files"]["map.json"]
                        ).read_bytes()
                    )
                else:
                    config = {"id": map_id}
            if not isinstance(config, dict) or config.get("id") != map_id:
                raise ValueError("catalog map identity mismatch")
            catalog.append(
                {
                    **entry,
                    "kind": config.get("kind", "arena"),
                    "mapJson": config,
                    "snapshots": self.versions.snapshots(map_id),
                }
            )
        return catalog

    # ------------------------------------------------------------------ export for the site

    def list(self) -> list[dict]:
        metas = {}
        for directory in (self.legacy_candidates, self.candidates):
            if directory is None or not directory.is_dir():
                continue
            for path in directory.glob("*.json"):
                if not CANDIDATE_ID.fullmatch(path.stem):
                    continue
                try:
                    metas[path.stem] = json.loads(path.read_text())
                except (OSError, ValueError):
                    continue
        return sorted(metas.values(), key=lambda m: m.get("createdAt", 0), reverse=True)

    def archive_path(self, candidate_id: str) -> Path:
        if not isinstance(candidate_id, str) or not CANDIDATE_ID.fullmatch(
            candidate_id
        ):
            raise ValueError("invalid candidate id")
        for directory in (self.candidates, self.legacy_candidates):
            if directory is not None:
                path = directory / f"{candidate_id}.zip"
                if path.is_file() and not path.is_symlink():
                    return path
        meta = next((item for item in self.list() if item["id"] == candidate_id), None)
        if meta is not None and meta.get("native"):
            target = self.exported_archive(meta)
            if target is not None:
                meta["exportedAt"] = int(self._now() * 1000)
                self._write_meta(meta)
                return target
            target = self.versions.archive / "exports" / f"{candidate_id}.zip"
            export_meta = (
                meta
                | {
                    "versionDigest": meta["mergeVersion"],
                    "baseVersion": meta["mergeTarget"],
                }
                if meta.get("mergeVersion")
                else meta
            )
            checksum, size = self.versions.export_package(export_meta, target)
            meta.update(
                sha256=checksum,
                size=size,
                exportCompressor="zlib-1.3.2",
                exportedAt=int(self._now() * 1000),
                exportVersion=export_meta["versionDigest"],
            )
            self._write_meta(meta)
            self.prune_exports()
            return target
        raise ValueError("the candidate's archive is gone")

    def exported_archive(self, meta: dict) -> Path | None:
        target = self.versions.archive / "exports" / f"{meta['id']}.zip"
        if (
            meta.get("mergeVersion")
            and meta.get("exportVersion") != meta["mergeVersion"]
        ):
            return None
        if not meta.get("sha256") or not target.exists():
            return None
        if (
            target.is_symlink()
            or not target.is_file()
            or _sha256(target) != meta["sha256"]
        ):
            raise ValueError("export cache checksum mismatch")
        return target

    def prune_exports(self) -> None:
        metas = self.list()
        protected = {meta.get("sha256") for meta in metas if not meta.get("native")}
        exports = sorted(
            (
                meta
                for meta in metas
                if meta.get("native") and meta.get("exportedAt") is not None
            ),
            key=lambda meta: (meta["exportedAt"], meta["id"]),
            reverse=True,
        )
        for meta in exports[EXPORTS_KEPT:]:
            (self.versions.archive / "exports" / f"{meta['id']}.zip").unlink(
                missing_ok=True
            )
            if (
                meta.get("derivedPackage")
                and meta.get("sha256")
                and meta["sha256"] not in protected
            ):
                (self.packages / f"{meta['sha256']}.zip").unlink(missing_ok=True)

    def export(self) -> dict:
        backups = {}
        for path in self.backups.iterdir() if self.backups.is_dir() else []:
            map_id = path.name.rsplit("-", 1)[0]
            backups[map_id] = backups.get(map_id, 0) + 1
        candidates = self.list()
        for meta in candidates:
            meta["supportedModes"] = map_package.supported_modes(
                self._candidate_config(meta).get("supported_modes", [])
            )
            meta["exportReady"] = bool(
                meta.get("sha256")
                and (self.packages / f"{meta['sha256']}.zip").is_file()
            )
            if meta.get("status") == "pending":
                base = meta.get("base")
                meta["baseMatches"] = (
                    None if base is None else base == self._cached_digest(meta["mapId"])
                )
        overlays = {}
        for path in (
            sorted(self.published.glob("*.json")) if self.published.is_dir() else []
        ):
            overlays[path.stem] = {"differsFromRepo": not self._matches_repo(path)}
        return {
            "schema": 1,
            "candidates": candidates,
            "backups": backups,
            "overlays": overlays,
            "maps": self.catalog(),
        }

    def retire_merged_overlays(self) -> list[str]:
        """A published JSON that git (the image's copy in the game) now has verbatim is dropped: later git edits apply again."""
        retired = []
        for path in (
            sorted(self.published.glob("*.json"))
            if self.published.is_dir() and self.repo_maps
            else []
        ):
            if MAP_ID.fullmatch(path.stem) and self._matches_repo(path):
                path.unlink()
                retired.append(path.stem)
        return retired

    # ------------------------------------------------------------------ playtest

    def sha256_of(self, candidate_id) -> str:
        if not isinstance(candidate_id, str) or not CANDIDATE_ID.fullmatch(
            candidate_id
        ):
            raise ValueError("invalid candidate id")
        meta = next((m for m in self.list() if m["id"] == candidate_id), None)
        if meta is None:
            raise ValueError("unknown candidate") from None
        path = self.archive_path(candidate_id)
        current = next(item for item in self.list() if item["id"] == candidate_id)
        if _sha256(path) != current["sha256"]:
            raise ValueError("candidate archive checksum mismatch")
        return current["sha256"]

    def extract(self, sha256: str, target: Path) -> str:
        """The candidate with this digest as <target>/{map-id, map.json, world/...} for the test profile; returns the map id."""
        found = [m for m in self.list() if m.get("sha256") == sha256]
        if not found:
            raise ValueError("no candidate with this digest")
        archive_path = self.archive_path(found[0]["id"])
        manifest = map_package.validate(archive_path, sha256)
        target = Path(target)
        if target.is_symlink() or target.is_file():
            raise ValueError("unsafe extract target")
        if target.is_dir():
            # Loud, never ignored: a partial wipe plus unvalidated writes
            # would mix old and new bytes into a Franken-map for the test run.
            shutil.rmtree(target)
        with zipfile.ZipFile(archive_path) as archive:
            for entry in archive.infolist():
                if entry.is_dir() or entry.filename == "manifest.json":
                    continue
                destination = target / entry.filename
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(archive.read(entry))
        for name, digest in manifest["files"].items():
            data = (target / name).read_bytes()
            if hashlib.sha256(data).hexdigest() != digest:
                raise ValueError(f"extracted {name} checksum mismatch")
        actual = {
            path.relative_to(target).as_posix()
            for path in target.rglob("*")
            if path.is_file() and path.name != "map-id"
        }
        if actual != set(manifest["files"]):
            raise ValueError(
                "extracted tree does not match its manifest: "
                f"{sorted(actual ^ set(manifest['files']))[:8]}"
            )
        (target / "map-id").write_text(manifest["mapId"] + "\n")
        _chown_tree(target, writable=False)
        return manifest["mapId"]

    # ------------------------------------------------------------------ helpers

    def _pending(self, candidate_id, *, allow_accepted=False) -> dict:
        if not isinstance(candidate_id, str) or not CANDIDATE_ID.fullmatch(
            candidate_id
        ):
            raise ValueError("invalid candidate id")
        meta = next((m for m in self.list() if m["id"] == candidate_id), None)
        if meta is None:
            raise ValueError("unknown candidate")
        if meta.get("status") != "pending" and not (
            allow_accepted and meta.get("status") == "accepted"
        ):
            raise ValueError(f"candidate is {meta.get('status')}, not pending")
        return meta

    def _draft_world(self, map_id: str, working_copy_id=None) -> Path:
        if working_copy_id is not None:
            return self.versions.working_copy(map_id, working_copy_id) / "world"
        return (
            self.workshop_data / "world" / "dimensions" / "blockfield_workshop" / map_id
        )

    def _map_json(self, map_id: str, working_copy_id=None) -> dict:
        """The workshop's edited copy (markup) wins over the JSON the workshop got from the image."""
        if working_copy_id is not None:
            value = map_package._json(
                (
                    self.versions.working_copy(map_id, working_copy_id) / "map.json"
                ).read_bytes()
            )
            if not isinstance(value, dict) or value.get("id") != map_id:
                raise ValueError("working copy map identity mismatch")
            return value
        for directory in (
            self.workshop_data / "world" / "blockfield_workshop" / "maps",
            self.workshop_data / "blockfield" / "maps",
        ):
            for path in sorted(directory.glob("*.json")) if directory.is_dir() else []:
                try:
                    config = json.loads(path.read_text())
                except (OSError, ValueError):
                    continue
                if isinstance(config, dict) and config.get("id") == map_id:
                    return config
        raise ValueError("no map config for this map")

    @staticmethod
    def _draft_base(world: Path) -> str | None:
        try:
            value = json.loads((world / BASE_MARKER).read_text()).get("templateDigest")
        except (OSError, ValueError, AttributeError):
            return None
        return (
            value
            if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value)
            else None
        )

    def _cached_digest(self, map_id: str) -> str | None:
        root = self.versions.live_world(map_id, self.templates / map_id)
        try:
            key = tuple(
                sorted(
                    (str(p), p.stat().st_size, p.stat().st_mtime_ns)
                    for p in root.rglob("*")
                    if p.is_file()
                )
            )
        except OSError:
            return None
        if self._digests.get(map_id, (None,))[0] != key:
            self._digests[map_id] = (key, template_digest(root))
        return self._digests[map_id][1]

    def _matches_repo(self, published: Path) -> bool:
        if not self.repo_maps:
            return False
        try:
            return json.loads(published.read_text()) == json.loads(
                (self.repo_maps / published.name).read_text()
            )
        except (OSError, ValueError):
            return False

    def _base_version(self, map_id: str) -> str:
        head = self.versions.head(map_id)
        if head is not None:
            return head["versionDigest"]
        path = self._base_config_path(map_id)
        if path is not None:
            return self.versions.import_template(map_id, self.templates / map_id, path)
        raise ValueError("no base map config for this map")

    def _base_config_path(self, map_id: str) -> Path | None:
        for directory in (
            self.published,
            self.repo_maps,
            self.workshop_data / "blockfield" / "maps",
        ):
            if directory is None:
                continue
            path = directory / f"{map_id}.json"
            if path.is_file() and not path.is_symlink():
                return path
        return None

    def _write_meta(self, meta: dict) -> None:
        self.candidates.mkdir(parents=True, exist_ok=True)
        _atomic_json(self.candidates / f"{meta['id']}.json", meta, owner=False)


class MapVersions:
    """All mutations run under the controller lock; runtime reads only SSD heads/bundles."""

    def __init__(self, archive: Path, active: Path, *, now=time.time):
        self.archive, self.active = Path(archive).resolve(), Path(active).resolve()
        self._now = now

    @staticmethod
    def _map_id(map_id):
        if not isinstance(map_id, str) or not MAP_ID.fullmatch(map_id):
            raise ValueError("invalid map id")
        return map_id

    @staticmethod
    def _digest(digest):
        if not isinstance(digest, str) or not map_package._HEX.fullmatch(digest):
            raise ValueError("version/blob SHA256 required")
        return digest

    def _manifest_path(self, map_id, version):
        return (
            self.archive
            / "versions"
            / self._map_id(map_id)
            / f"{self._digest(version)}.json"
        )

    def _blob(self, digest):
        digest = self._digest(digest)
        return self.archive / "blobs" / digest[:2] / digest

    def _bundle(self, map_id, version):
        return self.active / self._map_id(map_id) / "versions" / self._digest(version)

    def read(self, map_id: str, version: str) -> dict:
        path = self._manifest_path(map_id, version)
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 4 << 20:
            raise ValueError("map version missing or unsafe")
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != version:
            raise ValueError("map manifest checksum mismatch")
        value = map_package._json(raw)
        if (
            not isinstance(value, dict)
            or type(value.get("schema")) is not int
            or value["schema"] != 1
            or value.get("mapId") != map_id
        ):
            raise ValueError("map version identity mismatch")
        if value.get("kind") not in {"arena", "lobby", "limbo"}:
            raise ValueError("unknown map kind")
        parent = value.get("parent")
        if parent is not None:
            self._digest(parent)
        parents = value.get("parents", [parent] if parent else [])
        if (
            not isinstance(parents, list)
            or len(parents) > 2
            or any(not isinstance(item, str) for item in parents)
            or len(set(parents)) != len(parents)
            or (parents[0] if parents else None) != parent
        ):
            raise ValueError("invalid map commit parents")
        for previous in parents:
            self._digest(previous)
        self._validate_files(value.get("files"))
        return value

    @staticmethod
    def _validate_files(files):
        if not isinstance(files, dict) or "map.json" not in files or len(files) > 10000:
            raise ValueError("invalid map file manifest")
        if not any(
            name.startswith("world/region/") and name.endswith(".mca") for name in files
        ):
            raise ValueError("map requires region data")
        for name, digest in files.items():
            if name != "map.json" and not map_package._REGION.fullmatch(name):
                raise ValueError("unapproved map file")
            MapVersions._digest(digest)

    def head(self, map_id: str) -> dict | None:
        path = self.active / self._map_id(map_id) / "head.json"
        if not path.exists() and not path.is_symlink():
            return None
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 65536:
            raise ValueError("map head missing or unsafe")
        value = map_package._json(path.read_bytes())
        if (
            not isinstance(value, dict)
            or type(value.get("schema")) is not int
            or value["schema"] != 1
            or value.get("mapId") != map_id
            or type(value.get("generation")) is not int
            or value["generation"] < 1
            or value.get("kind") not in {"arena", "lobby", "limbo"}
            or value.get("bundle")
            != str(self._bundle(map_id, value.get("versionDigest")))
        ):
            raise ValueError("invalid map head")
        return value

    def check_head(self, map_id: str, expected_head) -> None:
        if expected_head is not None:
            self._digest(expected_head)
        current = self.head(map_id)
        if (current["versionDigest"] if current else None) != expected_head:
            raise ValueError("stale map head: refresh the map history")

    @staticmethod
    def _actor(actor) -> None:
        if not isinstance(actor, dict) or any(
            not isinstance(actor.get(key), str) or not actor[key].strip()
            for key in ("draslId", "name")
        ):
            raise ValueError("map operation requires actor draslId and name")

    @staticmethod
    def _branch_name(name):
        # Shared name rule for snapshots and pre-linear compat reads.
        if not isinstance(name, str) or not BRANCH_NAME.fullmatch(name):
            raise ValueError("canonical map snapshot name required")
        return name

    def _snapshot_path(self, map_id, name):
        self._map_id(map_id)
        self._branch_name(name)
        directory = self.archive / "snapshots" / map_id
        for path in (directory.parent, directory):
            if path.is_symlink() or path.exists() and not path.is_dir():
                raise ValueError("unsafe map snapshot directory")
        return directory / f"{name}.json"

    def snapshot(self, map_id, name) -> dict:
        path = self._snapshot_path(map_id, name)
        if not path.exists() and not path.is_symlink():
            raise ValueError("unknown map snapshot")
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 65536:
            raise ValueError("map snapshot missing or unsafe")
        value = map_package._json(path.read_bytes())
        if (
            not isinstance(value, dict)
            or type(value.get("schema")) is not int
            or value["schema"] != 1
            or value.get("mapId") != map_id
            or value.get("name") != name
            or type(value.get("generation")) is not int
            or value["generation"] < 1
            or type(value.get("createdAt")) is not int
            or value["createdAt"] < 0
        ):
            raise ValueError("invalid map snapshot")
        self._actor(value.get("createdBy"))
        self.read(map_id, value.get("versionDigest"))
        return value

    def snapshots(self, map_id) -> list[dict]:
        directory = self._snapshot_path(map_id, "work").parent
        return [
            self.snapshot(map_id, path.stem)
            for path in sorted(directory.glob("*.json"))
        ]

    def create_snapshot(self, map_id, name, version, actor) -> dict:
        self._actor(actor)
        if self._branch_name(name) == "main":
            raise ValueError("main is the active head, not a snapshot")
        self.read(map_id, version)
        path = self._snapshot_path(map_id, name)
        if path.exists() or path.is_symlink():
            existing = self.snapshot(map_id, name)
            if existing["versionDigest"] != version:
                raise ValueError("map snapshot already exists")
            return existing
        head = self.head(map_id)
        value = {
            "schema": 1,
            "mapId": map_id,
            "name": name,
            "versionDigest": version,
            "generation": head["generation"] if head else 1,
            "createdAt": int(self._now() * 1000),
            "createdBy": actor,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_json(path, value, owner=False)
        return self.snapshot(map_id, name)

    def delete_snapshot(self, map_id, name) -> None:
        path = self._snapshot_path(map_id, name)
        if path.is_symlink() or not path.is_file():
            raise ValueError("unknown map snapshot")
        self.snapshot(map_id, name)
        path.unlink()
        _sync_directory(path.parent)

    def migrate_branches_to_snapshots(self) -> dict:
        """One-way linearization of pre-linear branch refs.

        Every named branch tip that differs from the map head becomes a named
        snapshot (generation/author/timestamp preserved); tips already at the
        head need no snapshot. Branch refs are removed only after their
        snapshot is written and re-read. Idempotent: a second run finds no
        branch refs and changes nothing. Never deletes a version or blob;
        working copies and sessions are untouched, so live sessions keep
        working against the same files.
        """
        root = self.archive / "branches"
        migrated, at_head = [], []
        if root.is_symlink():
            raise ValueError("unsafe map branch directory")
        if not root.is_dir():
            return {"migrated": migrated, "atHead": at_head}
        for map_dir in sorted(root.iterdir()):
            if map_dir.is_symlink() or not map_dir.is_dir():
                raise ValueError("unsafe map branch directory")
            map_id = self._map_id(map_dir.name)
            head = self.head(map_id)
            head_digest = head["versionDigest"] if head else None
            for path in sorted(map_dir.glob("*.json")):
                if path.is_symlink() or not path.is_file():
                    raise ValueError("map branch missing or unsafe")
                name = path.stem
                if name == "main":
                    raise ValueError("main must use the active map head")
                self._branch_name(name)
                raw = path.read_bytes()
                value = map_package._json(raw)
                if (
                    not isinstance(value, dict)
                    or value.get("mapId") != map_id
                    or value.get("name") != name
                ):
                    raise ValueError("invalid map branch")
                self._actor(value.get("createdBy"))
                tip = value.get("versionDigest")
                self.read(map_id, tip)
                if tip == head_digest:
                    at_head.append(
                        {"mapId": map_id, "name": name, "versionDigest": tip}
                    )
                else:
                    snap_path = self._snapshot_path(map_id, name)
                    if not snap_path.exists() and not snap_path.is_symlink():
                        snap_path.parent.mkdir(parents=True, exist_ok=True)
                        _atomic_json(
                            snap_path,
                            {
                                "schema": 1,
                                "mapId": map_id,
                                "name": name,
                                "versionDigest": tip,
                                "generation": value.get("generation", 1),
                                "createdAt": value.get("createdAt", 0),
                                "createdBy": value.get("createdBy"),
                            },
                            owner=False,
                        )
                    snap = self.snapshot(map_id, name)
                    if snap["versionDigest"] != tip:
                        raise ValueError("map snapshot already exists")
                    migrated.append(
                        {"mapId": map_id, "name": name, "versionDigest": tip}
                    )
                # Recheck before removing: the ref must still be the one the
                # snapshot was taken from (migration runs under the lock, so a
                # change here means the lock did not serialize writers).
                if path.read_bytes() != raw:
                    raise ValueError("map branch changed during migration")
                path.unlink()
                _sync_directory(map_dir)
            try:
                map_dir.rmdir()
                _sync_directory(root)
            except OSError:
                pass
        try:
            root.rmdir()
            _sync_directory(root.parent)
        except OSError:
            pass
        return {"migrated": migrated, "atHead": at_head}

    def commit_head(
        self,
        map_id,
        files,
        actor,
        note,
        parent_version,
        working_copy_id,
        base_version,
        *,
        source="commit",
        source_id=None,
    ) -> str:
        # Linear commit: one parent (the exact current head), no ref to move.
        self._actor(actor)
        if parent_version is not None:
            self._digest(parent_version)
        if base_version != parent_version:
            raise ValueError(STALE_COPY_MESSAGE)
        if working_copy_id is not None:
            self.working_copy(map_id, working_copy_id)
        previous = self.read(map_id, parent_version)["files"] if parent_version else {}
        files = self._same_config_files(files, previous)
        # The first snapshot bootstraps history; every later commit must move it.
        if (
            parent_version is not None
            and files == previous
            and self.head(map_id) is not None
        ):
            raise ValueError("no changes since the current map head")
        return self._record(
            map_id,
            files,
            parent=parent_version,
            author=actor,
            created_at=int(self._now() * 1000),
            source=source,
            source_id=source_id if source_id is not None else working_copy_id,
            note=note.strip() if isinstance(note, str) else note,
            parents=[parent_version] if parent_version else [],
        )

    def _same_config_files(self, files, previous_files):
        # JSON whitespace/key order is not a builder edit; keep the old blob
        # so an untouched draft compares equal to its base version.
        if previous_files and self._same(
            self._config(files), self._config(previous_files)
        ):
            return files | {"map.json": previous_files["map.json"]}
        return files

    def _config(self, files):
        path = self._verify_blob(files["map.json"])
        if path.stat().st_size > 1 << 20:
            raise ValueError("map config too large")
        return map_package._json(path.read_bytes())

    @staticmethod
    def _same(left, right):
        if left is MISSING or right is MISSING:
            return left is right
        if type(left) is not type(right):
            return False
        if isinstance(left, dict):
            return left.keys() == right.keys() and all(
                MapVersions._same(value, right[key]) for key, value in left.items()
            )
        if isinstance(left, list):
            return len(left) == len(right) and all(
                MapVersions._same(a, b) for a, b in zip(left, right)
            )
        return left == right

    @staticmethod
    def _json_changes(before, after, path=""):
        if MapVersions._same(before, after):
            return []
        if isinstance(before, dict) and isinstance(after, dict):
            return [
                change
                for key in sorted(before.keys() | after.keys())
                for change in MapVersions._json_changes(
                    before.get(key, MISSING),
                    after.get(key, MISSING),
                    path + "/" + key.replace("~", "~0").replace("/", "~1"),
                )
            ]
        return [
            {
                "path": path,
                "beforePresent": before is not MISSING,
                "afterPresent": after is not MISSING,
                "before": None if before is MISSING else before,
                "after": None if after is MISSING else after,
            }
        ]

    def compare(self, map_id, before, after) -> dict:
        old, new = self.read(map_id, before), self.read(map_id, after)
        return self._compare_files(map_id, before, after, old["files"], new["files"])

    def _compare_files(self, map_id, before, after, old, new, config=None):
        changes = []
        for name in sorted(old.keys() | new.keys()):
            left, right = old.get(name), new.get(name)
            if left == right:
                continue
            item = {
                "path": name,
                "before": left,
                "after": right,
                "change": "added"
                if left is None
                else "deleted"
                if right is None
                else "modified",
            }
            if name != "map.json":
                match = re.fullmatch(
                    r"world/(region|entities|poi)/([rc])\.(-?\d+)\.(-?\d+)\.(mca|mcc)",
                    name,
                )
                if match:
                    x, z = int(match[3]), int(match[4])
                    size = 512 if match[2] == "r" else 16
                    item["area"] = {
                        "minX": x * size,
                        "minZ": z * size,
                        "maxX": (x + 1) * size - 1,
                        "maxZ": (z + 1) * size - 1,
                    }
            changes.append(item)
        return {
            "mapId": map_id,
            "before": before,
            "after": after,
            "files": changes,
            "config": self._json_changes(
                self._config(old) if "map.json" in old else {},
                self._config(new) if config is None else config,
            ),
        }

    def prepare_restore(self, map_id, target, actor, expected_head) -> str:
        self._actor(actor)
        self.check_head(map_id, expected_head)
        if expected_head is None:
            raise ValueError("import main before restoring")
        archived = self.read(map_id, target)
        if archived["source"] == "restore" and archived["parent"] == expected_head:
            return target
        existing = next(
            (
                entry
                for entry in self.history(map_id)
                if entry["source"] == "restore"
                and entry["parent"] == expected_head
                and entry.get("sourceId") == target
                and entry.get("author") == actor
            ),
            None,
        )
        if existing:
            return existing["versionDigest"]
        version = self._record(
            map_id,
            archived["files"],
            parent=expected_head,
            parents=[expected_head],
            author=actor,
            created_at=int(self._now() * 1000),
            source="restore",
            source_id=target,
            note=f"Restore map version {target[:12]}",
        )
        return version

    def live_world(self, map_id: str, fallback: Path) -> Path:
        head = self.head(map_id)
        return Path(head["bundle"]) / "world" if head else fallback

    def _put_blob(self, data: bytes) -> str:
        digest = hashlib.sha256(data).hexdigest()
        target = self._blob(digest)
        if target.exists() or target.is_symlink():
            self._verify_blob(digest)
            return digest
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=target.parent, prefix=".blob-")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o444)
            os.replace(temporary, target)
            _sync_directory(target.parent)
        finally:
            Path(temporary).unlink(missing_ok=True)
        return digest

    def _put_blob_file(self, path: Path) -> str:
        """Stream a world file into the CAS without ever loading it fully into RAM."""
        blob_dir = self.archive / "blobs"
        blob_dir.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=blob_dir, prefix=".blob-")
        try:
            digest = hashlib.sha256()
            with os.fdopen(fd, "wb") as out:
                with path.open("rb") as handle:
                    while chunk := handle.read(1 << 20):
                        digest.update(chunk)
                        out.write(chunk)
                out.flush()
                os.fsync(out.fileno())
            name = digest.hexdigest()
            target = self._blob(name)
            if target.exists() or target.is_symlink():
                self._verify_blob(name)
                return name
            target.parent.mkdir(parents=True, exist_ok=True)
            os.chmod(temporary, 0o444)
            os.replace(temporary, target)
            _sync_directory(target.parent)
            return name
        finally:
            Path(temporary).unlink(missing_ok=True)

    def _verify_blob(self, digest: str) -> Path:
        path = self._blob(digest)
        if path.is_symlink() or not path.is_file() or _sha256(path) != digest:
            raise ValueError("map blob missing or checksum mismatch")
        return path

    def _record(
        self,
        map_id,
        files,
        *,
        parent,
        author,
        created_at,
        source,
        source_id=None,
        note="",
        parents=None,
    ):
        self._map_id(map_id)
        self._validate_files(files)
        config = self._config(files)
        if (
            sum(self._verify_blob(digest).stat().st_size for digest in files.values())
            > 1 << 30
        ):
            raise ValueError("map expanded size exceeds package limit")
        if not isinstance(config, dict) or config.get("id") != map_id:
            raise ValueError("map config/world identity mismatch")
        kind = config.get("kind", "arena")
        if kind not in {"arena", "lobby", "limbo"}:
            raise ValueError("unknown map kind")
        ancestors = parents if parents is not None else ([parent] if parent else [])
        if (
            not isinstance(ancestors, list)
            or len(ancestors) > 1
            or any(not isinstance(item, str) for item in ancestors)
            or len(set(ancestors)) != len(ancestors)
            or (ancestors[0] if ancestors else None) != parent
        ):
            raise ValueError("map history is linear: single parent required")
        for ancestor in ancestors:
            if self.read(map_id, ancestor)["kind"] != kind:
                raise ValueError("map commit kind cannot change")
        previous = self.read(map_id, parent)["files"] if parent else {}
        lines = "".join(
            f"{name.removeprefix('world/')}\0{digest}\n"
            for name, digest in sorted(files.items())
            if name.startswith("world/")
        )
        manifest = {
            "schema": 1,
            "mapId": map_id,
            "kind": kind,
            "parent": parent,
            "createdAt": created_at,
            "author": author,
            "source": source,
            "sourceId": source_id,
            "note": note,
            "worldDigest": hashlib.sha256(lines.encode()).hexdigest(),
            "files": dict(sorted(files.items())),
            "changed": sorted(
                name for name, digest in files.items() if previous.get(name) != digest
            ),
            "deleted": sorted(set(previous) - set(files)),
        }
        if parents is not None:
            manifest["parents"] = ancestors
        raw = json.dumps(manifest, ensure_ascii=False, indent=2).encode()
        version = hashlib.sha256(raw).hexdigest()
        target = self._manifest_path(map_id, version)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() or target.is_symlink():
            self.read(map_id, version)
        else:
            _atomic_json(target, manifest, owner=False)
            target.chmod(0o444)
        return version

    def world_files(self, world: Path, config: bytes) -> dict:
        if world.is_symlink() or not world.is_dir():
            raise ValueError("unsafe template world")
        regions = []
        for kind in WORLD_KINDS:
            directory = world / kind
            if directory.is_symlink() or directory.exists() and not directory.is_dir():
                raise ValueError("unsafe template Anvil directory")
            for path in (
                sorted((world / kind).iterdir()) if (world / kind).is_dir() else []
            ):
                if path.is_symlink() or not path.is_file():
                    raise ValueError("unsafe template file")
                name = f"world/{kind}/{path.name}"
                if not map_package._REGION.fullmatch(name):
                    raise ValueError("unapproved Anvil sidecar")
                regions.append((name, path))
        # Fail before loading anything: a degenerate multi-GiB region file must
        # not OOM the controller mid-snapshot (same limit _record enforces).
        if sum(path.stat().st_size for _, path in regions) + len(config) > 1 << 30:
            raise ValueError("map expanded size exceeds package limit")
        files = {"map.json": self._put_blob(config)}
        for name, path in regions:
            if path.stat().st_size or path.suffix == ".mcc":
                files[name] = self._put_blob_file(path)
        return files

    def import_template(self, map_id: str, world: Path, config: Path) -> str:
        files = self.world_files(world, config.read_bytes())
        # No active head: import the actual release template, never the newest accepted draft.
        for version in self.history(map_id):
            if version["source"] == "template" and version["files"] == files:
                return version["versionDigest"]
        return self._record(
            map_id,
            files,
            parent=None,
            author=None,
            created_at=int(self._now() * 1000),
            source="template",
        )

    def import_package(self, archive: Path, checksum: str, **metadata) -> str:
        manifest = map_package.validate(archive, checksum)
        files = {}
        with zipfile.ZipFile(archive) as package:
            for name, digest in manifest["files"].items():
                files[name] = self._put_blob(package.read(name))
                if files[name] != digest:
                    raise ValueError("package changed after validation")
        if _sha256(archive) != checksum:
            raise ValueError("package changed after validation")
        return self._record(manifest["mapId"], files, source="candidate", **metadata)

    def export_package(self, meta: dict, target: Path) -> tuple[str, int]:
        if (zlib.ZLIB_VERSION, zlib.ZLIB_RUNTIME_VERSION) != ("1.3.2", "1.3.2"):
            raise RuntimeError("Candidate ZIPs require zlib 1.3.2")
        version = self.read(meta["mapId"], meta["versionDigest"])
        files = {
            name: self._verify_blob(digest) for name, digest in version["files"].items()
        }
        target.parent.mkdir(parents=True, exist_ok=True)
        # A killed export leaves its temp behind; the next export reaps the same prefix.
        for stale in target.parent.glob(".export-*.tmp"):
            stale.unlink(missing_ok=True)
        palette = map_package.scan_package(
            {name: path.read_bytes() for name, path in files.items()}
        )
        namespaces = sorted(
            {name.split(":")[0] for name in palette["blocks"]} - {"minecraft"}
        )
        if (
            shutil.disk_usage(target.parent).free
            < sum(path.stat().st_size for path in files.values()) + SNAPSHOT_MARGIN
        ):
            raise OSError("not enough free space for ZIP export")
        manifest = {
            "schemaVersion": 2,
            "mapId": meta["mapId"],
            "version": meta["versionDigest"],
            "baseVersion": meta.get("baseVersion") or "template",
            "formatVersion": map_package.FORMAT_VERSION,
            "dataVersion": palette["data_version"],
            "requiredBlocks": {
                "registry": palette["registry"],
                "blocks": palette["blocks"],
            },
            # The Workshop does not own the Game mod list; versions are
            # stamped at publish from the Game pins.
            "requiredMods": [
                {"id": namespace, "version": None} for namespace in namespaces
            ],
            "files": version["files"],
        }
        fd, name = tempfile.mkstemp(dir=target.parent, prefix=".export-", suffix=".tmp")
        temporary = Path(name)
        os.close(fd)
        try:
            with zipfile.ZipFile(temporary, "w") as archive:
                for path in sorted(set(files) | {"manifest.json"}):
                    data = (
                        json.dumps(manifest, sort_keys=True).encode()
                        if path == "manifest.json"
                        else files[path].read_bytes()
                    )
                    entry = zipfile.ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
                    entry.create_system = 3
                    entry.external_attr = 0o100644 << 16
                    archive.writestr(
                        entry, data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9
                    )
            checksum = _sha256(temporary)
            map_package.validate(temporary, checksum)
            with temporary.open("rb") as handle:
                os.fsync(handle.fileno())
            temporary.chmod(0o444)
            os.replace(temporary, target)
            _sync_directory(target.parent)
        finally:
            temporary.unlink(missing_ok=True)
        return checksum, target.stat().st_size

    def history(self, map_id: str) -> list[dict]:
        directory = self.archive / "versions" / self._map_id(map_id)
        versions = []
        for path in directory.glob("*.json"):
            self._digest(path.stem)
            versions.append(
                {**self.read(map_id, path.stem), "versionDigest": path.stem}
            )
        return sorted(
            versions,
            key=lambda item: (item["createdAt"], item["versionDigest"]),
            reverse=True,
        )

    def export(self) -> list[dict]:
        directory = self.archive / "versions"
        if not directory.is_dir():
            return []
        maps = []
        for path in sorted(directory.iterdir()):
            if path.is_dir() and MAP_ID.fullmatch(path.name):
                versions = self.history(path.name)
                if versions:
                    history = [
                        {key: value for key, value in version.items() if key != "files"}
                        | {"author": version.get("author") or {}}
                        | {"fileCount": len(version["files"])}
                        for version in versions
                    ]
                    maps.append(
                        {
                            "mapId": path.name,
                            "kind": versions[0]["kind"],
                            "head": self.head(path.name),
                            "versions": history,
                        }
                    )
        return maps

    def _materialize(
        self, map_id: str, version: str, target: Path, *, writable: bool
    ) -> Path:
        manifest = self.read(map_id, version)
        sources = {
            name: self._verify_blob(digest)
            for name, digest in manifest["files"].items()
        }
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() or target.is_symlink():
            raise ValueError("working copy already exists; resume it instead")
        if (
            shutil.disk_usage(target.parent).free
            < sum(path.stat().st_size for path in sources.values()) + SNAPSHOT_MARGIN
        ):
            raise OSError("not enough free space for map staging")
        temporary = Path(tempfile.mkdtemp(dir=target.parent, prefix=".staging-"))
        try:
            for name, source in sources.items():
                destination = temporary / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                # Copies own inodes. Region writes must never reach archive blobs.
                shutil.copyfile(source, destination)
                if _sha256(destination) != manifest["files"][name]:
                    raise ValueError("map blob changed during staging")
                destination.chmod(0o644 if writable else 0o444)
                with destination.open("rb") as handle:
                    os.fsync(handle.fileno())
            _atomic_json(temporary / "manifest.json", manifest, owner=False)
            if writable:
                _atomic_json(
                    temporary / "world" / BASE_MARKER,
                    {
                        "schema": 1,
                        "templateDigest": manifest["worldDigest"],
                        "versionDigest": version,
                    },
                )
                _chown_tree(temporary)
            else:
                (temporary / "manifest.json").chmod(0o444)
            for directory, _, _ in os.walk(temporary, topdown=False):
                Path(directory).chmod(0o775 if writable else 0o755)
                _sync_directory(Path(directory))
            os.replace(temporary, target)
            _sync_directory(target.parent)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
        return target

    def prepare(self, map_id: str, version: str) -> Path:
        target = self._bundle(map_id, version)
        manifest = self.read(map_id, version)
        if not target.exists() and not target.is_symlink():
            return self._materialize(map_id, version, target, writable=False)
        if target.is_symlink() or not target.is_dir():
            raise ValueError("unsafe active map bundle")
        if _sha256(target / "manifest.json") != version:
            raise ValueError("active map manifest checksum mismatch")
        actual = {
            path.relative_to(target).as_posix()
            for path in target.rglob("*")
            if path.is_file()
        }
        if actual != set(manifest["files"]) | {"manifest.json"}:
            raise ValueError("active map bundle file set mismatch")
        for name, digest in manifest["files"].items():
            self._verify_blob(digest)
            path = target / name
            if path.is_symlink() or not path.is_file() or _sha256(path) != digest:
                raise ValueError("active map bundle checksum mismatch")
        return target

    def activate(self, map_id: str, version: str, actor: dict, expected_head) -> dict:
        if not isinstance(actor, dict) or any(
            not isinstance(actor.get(key), str) or not actor[key].strip()
            for key in ("draslId", "name")
        ):
            raise ValueError("map activation requires actor draslId and name")
        self.check_head(map_id, expected_head)
        bundle = self.prepare(map_id, version)
        map_package.validate_playable(
            map_package._json((bundle / "map.json").read_bytes())
        )
        previous = self.head(map_id)
        head = {
            "schema": 1,
            "mapId": map_id,
            "versionDigest": version,
            "kind": self.read(map_id, version)["kind"],
            "bundle": str(bundle),
            "generation": previous["generation"] + 1 if previous else 1,
            "activatedAt": int(self._now() * 1000),
            "activatedBy": actor,
        }
        # Last commit point: all blob checks and complete staging succeeded first.
        self.check_head(map_id, expected_head)
        _atomic_json(self.active / map_id / "head.json", head, owner=False)
        return head

    def reclaim_unheaded_bundles(self) -> None:
        """Only after the controller saved, stopped and observed zero full backends."""
        if not self.active.is_dir():
            return
        for path in self.active.iterdir():
            if (
                not path.is_dir()
                or path.is_symlink()
                or path.name == "drafts"
                or not MAP_ID.fullmatch(path.name)
            ):
                continue
            head = self.head(path.name)
            keep = head["versionDigest"] if head else None
            for bundle in (path / "versions").glob("*"):
                if (
                    bundle.name != keep
                    and map_package._HEX.fullmatch(bundle.name)
                    and bundle.is_dir()
                    and not bundle.is_symlink()
                ):
                    shutil.rmtree(bundle)
            _sync_directory(path)

    def working_copy(self, map_id: str, working_copy_id: str) -> Path:
        try:
            if (
                not isinstance(working_copy_id, str)
                or str(UUID(working_copy_id)) != working_copy_id
            ):
                raise ValueError("canonical working-copy UUID required")
        except (ValueError, AttributeError) as error:
            raise ValueError("canonical working-copy UUID required") from error
        return self.active / "drafts" / working_copy_id / self._map_id(map_id)

    def checkout(self, map_id: str, version: str, working_copy_id: str) -> dict:
        target = self.working_copy(map_id, working_copy_id)
        self._materialize(map_id, version, target, writable=True)
        return {
            "mapId": map_id,
            "versionDigest": version,
            "workingCopyId": working_copy_id,
            "bundle": str(target),
        }

    def _working_copy_manifest(self, map_id: str, working_copy_id: str) -> tuple:
        """The copy's authoritative base: manifest.json verified against the CAS.

        manifest.json is byte-identical to an immutable archived version, so its
        sha256 both identifies and verifies the base; the world marker only
        mirrors it. A torn advance write (or a legacy/hand-written marker) is
        repaired here, so no crash point leaves the copy permanently invalid.
        """
        target = self.working_copy(map_id, working_copy_id)
        manifest_file = target / "manifest.json"
        marker_path = target / "world" / BASE_MARKER
        for path in (target, target / "world", manifest_file):
            if path.is_symlink() or not path.exists():
                raise ValueError("working copy missing or unsafe")
        if manifest_file.stat().st_size > 4 << 20:
            raise ValueError("working copy metadata missing or unsafe")
        version = _sha256(manifest_file)
        manifest = self.read(map_id, version)
        if marker_path.is_symlink() or (
            marker_path.is_file() and marker_path.stat().st_size > 65536
        ):
            raise ValueError("working copy missing or unsafe")
        try:
            marker = map_package._json(marker_path.read_bytes())
            intact = (
                isinstance(marker, dict)
                and marker.get("versionDigest") == version
                and marker.get("templateDigest") == manifest["worldDigest"]
            )
        except (OSError, ValueError):
            intact = False
        if not intact:
            _atomic_json(
                marker_path,
                {
                    "schema": 1,
                    "templateDigest": manifest["worldDigest"],
                    "versionDigest": version,
                },
                owner=False,
            )
        return target, version, manifest

    def _write_copy_base(self, target: Path, manifest: dict, version: str) -> None:
        """Move a working copy's base pointer; the world files stay untouched."""
        _atomic_json(target / "manifest.json", manifest, owner=False)
        _atomic_json(
            target / "world" / BASE_MARKER,
            {
                "schema": 1,
                "templateDigest": manifest["worldDigest"],
                "versionDigest": version,
            },
            owner=False,
        )
        _chown_tree(target)

    def advance_working_copy(
        self, map_id: str, working_copy_id: str, version: str
    ) -> bool:
        """Move a working copy's base pointer to a newer immutable version.

        Only the copy a published version was snapshotted from may advance:
        its world already contains that version, so later commits parent it
        without dropping anything. The world files (pending edits) are kept.
        A missing copy was removed after the review; that is not an error.
        """
        target = self.working_copy(map_id, working_copy_id)
        if not target.exists() and not target.is_symlink():
            return False
        _, current, _ = self._working_copy_manifest(map_id, working_copy_id)
        manifest = self.read(map_id, version)
        # The copy sits on the published version's parent — or already on the
        # version itself (first publish on a headless map, or a republish
        # after the activation below failed midway).
        if current not in {version, manifest["parent"]}:
            raise ValueError("working copy moved during review; resume it first")
        self._write_copy_base(target, manifest, version)
        return True

    def validate_working_copy(self, map_id: str, working_copy_id: str) -> dict:
        target = self.working_copy(map_id, working_copy_id)
        for path in (
            self.active,
            self.active / "drafts",
            target.parent,
            target,
            target / "world",
        ):
            if path.is_symlink() or not path.is_dir():
                raise ValueError("working copy missing or unsafe")
        manifest_file, config_file = target / "manifest.json", target / "map.json"
        for path in (manifest_file, config_file, target / "world" / BASE_MARKER):
            if (
                path.is_symlink()
                or not path.is_file()
                or path.stat().st_nlink != 1
                or path.stat().st_size > 4 << 20
            ):
                raise ValueError("working copy metadata missing or unsafe")
        if config_file.stat().st_size > 1 << 20:
            raise ValueError("map config too large")
        # The manifest is the authoritative base; resolving it repairs a torn
        # marker write, so a crash between the two base writes heals on read.
        _, version, manifest = self._working_copy_manifest(map_id, working_copy_id)
        config = map_package._json(config_file.read_bytes())
        self.validate_config(map_id, config)
        if config.get("kind", "arena") != manifest["kind"]:
            raise ValueError("working copy kind mismatch")
        regions = 0
        for kind in WORLD_KINDS:
            directory = target / "world" / kind
            if directory.is_symlink() or directory.exists() and not directory.is_dir():
                raise ValueError("unsafe working copy Anvil directory")
            for path in directory.iterdir() if directory.is_dir() else []:
                if (
                    path.is_symlink()
                    or not path.is_file()
                    or path.stat().st_nlink != 1
                    or not map_package._REGION.fullmatch(f"world/{kind}/{path.name}")
                ):
                    raise ValueError("unsafe working copy Anvil file")
                regions += (
                    kind == "region"
                    and path.suffix == ".mca"
                    and path.stat().st_size > 0
                )
        if not regions:
            raise ValueError("working copy has no region data")
        # Drafts checked out before the permission change keep 0755/0644, which masks
        # inherited operator ACLs; every resume path routes through here, so repair it once.
        _chown_tree(target)
        return {
            "mapId": map_id,
            "versionDigest": version,
            "workingCopyId": working_copy_id,
            "bundle": str(target),
        }

    @staticmethod
    def validate_config(map_id: str, config: dict) -> None:
        if (
            not isinstance(config, dict)
            or config.get("id") != map_id
            or config.get("kind", "arena") not in {"arena", "lobby", "limbo"}
        ):
            raise ValueError("map config identity/kind mismatch")
        bounds = config.get("bounds")
        if (
            not isinstance(config.get("name"), str)
            or not config["name"].strip()
            or not isinstance(config.get("mode_data"), dict)
            or not isinstance(bounds, dict)
        ):
            raise ValueError("map requires name, bounds and mode_data")
        for key in ("minX", "maxX", "minZ", "maxZ"):
            value = bounds.get(key)
            if (
                type(value) not in (int, float)
                or not -30_000_000 <= value <= 30_000_000
            ):
                raise ValueError("invalid map bounds")
        if bounds["minX"] >= bounds["maxX"] or bounds["minZ"] >= bounds["maxZ"]:
            raise ValueError("invalid map bounds")
        modes = config.get("supported_modes", [])
        if not isinstance(modes, list) or any(
            not isinstance(mode, str) for mode in modes
        ):
            raise ValueError("invalid supported_modes")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path: Path, value, *, owner: bool = True, mode: int = 0o644) -> None:
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(json.dumps(value, ensure_ascii=False, indent=2).encode())
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        if owner:
            _chown(Path(temporary))
        os.replace(temporary, path)
        _sync_directory(path.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _sync_directory(path: Path) -> None:
    if os.name != "nt":
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _chown(path: Path) -> None:
    try:
        os.chown(path, *OWNER)
    except PermissionError:
        pass  # tests and non-root runs


def _chown_tree(root: Path, *, writable: bool = True) -> None:
    # Drafts are writable by the game server: fwalk and O_NOFOLLOW keep a planted symlink
    # from redirecting root's chown/chmod outside the tree. Correct entries cost one lstat.
    for _, _, files, directory_fd in os.fwalk(root):
        _repair(directory_fd, os.fstat(directory_fd), 0o775 if writable else 0o755)
        for name in files:
            info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            # Editable copies keep inherited operator ACLs effective after copying immutable blobs.
            mode = 0o664 if writable else 0o644
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or (stat.S_IMODE(info.st_mode), info.st_uid, info.st_gid)
                == (mode, *OWNER)
            ):
                continue
            fd = os.open(
                name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd
            )
            try:
                fresh = os.fstat(fd)
                # The entry can be swapped for a hard link to an outside inode
                # between the stat above and open; O_NOFOLLOW misses hard links.
                if not stat.S_ISREG(fresh.st_mode) or fresh.st_nlink != 1:
                    continue
                _repair(fd, fresh, mode)
            finally:
                os.close(fd)


def _repair(fd: int, info: os.stat_result, mode: int) -> None:
    if (info.st_uid, info.st_gid) != OWNER:
        try:
            os.fchown(fd, *OWNER)
        except PermissionError:
            pass  # tests and non-root runs
    if stat.S_IMODE(info.st_mode) != mode:
        os.fchmod(fd, mode)
