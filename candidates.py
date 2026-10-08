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
        branch=None,
        expected_branch_head=None,
        review=True,
    ) -> dict:
        if branch is not None and review:
            return self.review(
                map_id,
                author,
                note,
                working_copy_id=working_copy_id,
                branch=branch,
                expected_head=expected_branch_head,
            )
        world = self.check(map_id, working_copy_id)
        base_version = self._base_version(map_id)
        if working_copy_id is not None:
            base_version = map_package._json((world / BASE_MARKER).read_bytes())[
                "versionDigest"
            ]
            self.versions.read(map_id, base_version)
        self.candidates.mkdir(parents=True, exist_ok=True)
        # Exports run under the controller lock, so any temporary archive here was left by one that was killed.
        for stale in self.candidates.glob(".*.zip.tmp"):
            stale.unlink(missing_ok=True)
        candidate_id = uuid4().hex
        now_ms = int(self._now() * 1000)
        note = str(note)[:200]
        with self.hold_saves():
            config = self._map_json(map_id, working_copy_id)
            files = self.versions.world_files(
                world, json.dumps(config, ensure_ascii=False, indent=2).encode()
            )
            if branch is not None:
                if working_copy_id is None or expected_branch_head is None:
                    raise ValueError(
                        "branch snapshot requires working copy and expectedBranchHead"
                    )
                version = self.versions.commit_branch(
                    map_id,
                    branch,
                    files,
                    author,
                    note or f"Update map {map_id}",
                    expected_branch_head,
                    working_copy_id,
                    base_version,
                )
                base_version = expected_branch_head
            else:
                version = self.versions._record(
                    map_id,
                    files,
                    parent=base_version,
                    author=author,
                    created_at=now_ms,
                    source="candidate",
                    source_id=candidate_id,
                    note=note,
                )
        if not review:
            return {
                "mapId": map_id,
                "versionDigest": version,
                "branch": self.versions.branch(map_id, branch),
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
            "workingCopyId": working_copy_id,
        }
        if branch is not None:
            head = self.versions.head(map_id)
            meta.update(
                sourceBranch=branch, targetHead=head["versionDigest"] if head else None
            )
        self._write_meta(meta)
        return meta

    def commit(self, map_id, author, note, *, working_copy_id, branch, expected_head):
        if not isinstance(note, str) or not note.strip() or len(note) > 200:
            raise ValueError("commit message required (1–200 characters)")
        return self.create(
            map_id,
            author,
            note,
            working_copy_id=working_copy_id,
            branch=branch,
            expected_branch_head=expected_head,
            review=False,
        )

    def review(self, map_id, author, note, *, working_copy_id, branch, expected_head):
        self.versions._actor(author)
        self.versions.working_copy(map_id, working_copy_id)
        ref = self.versions.branch(map_id, branch)
        if branch == "main" or ref["versionDigest"] != expected_head:
            raise ValueError("stale review branch head")
        head = self.versions.head(map_id)
        if head is None or head["versionDigest"] == expected_head:
            raise ValueError("commit changes on the branch before requesting review")
        if not isinstance(note, str) or len(note) > 200:
            raise ValueError("review message too long")
        manifest = self.versions.read(map_id, expected_head)
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
            "sourceBranch": branch,
            "targetHead": head["versionDigest"],
            "base": None,
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
        if (
            expected_head is not ACCEPT_ONLY
            and expected_head is not None
            and meta.get("native")
        ):
            if force:
                raise ValueError("force cannot bypass map merge conflicts")
            if meta.get("sourceBranch") and meta.get("targetHead") != expected_head:
                raise ValueError("review target changed; request a fresh review")
            choices = {} if resolutions is None else resolutions
            prepared = (
                meta.get("mergeVersion")
                if meta.get("mergeTarget") == expected_head
                and meta.get("mergeResolutions") == choices
                else None
            )
            if prepared is None:
                prepared = self.versions.prepare_merge(
                    meta["mapId"],
                    meta["versionDigest"],
                    actor,
                    expected_head,
                    note=meta.get("note", ""),
                    resolutions=choices,
                )
            else:
                self.versions.check_head(meta["mapId"], expected_head)
                self.versions.read(meta["mapId"], prepared)
            meta.update(
                status="accepted",
                decidedBy=actor,
                decidedAt=int(self._now() * 1000),
                forced=False,
                mergeVersion=prepared,
                mergeTarget=expected_head,
                mergeResolutions=choices,
            )
            if meta.get("exportVersion") != prepared:
                meta.update(sha256=None, size=None)
            self._write_meta(meta)
            self.activate(meta["mapId"], prepared, actor, expected_head)
            return meta
        if (
            expected_head is not ACCEPT_ONLY
            and expected_head is not None
            and not meta.get("versionDigest")
        ):
            raise ValueError(
                "legacy candidate has no verified commit parent; import it into a branch"
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
            self.versions.check_head(map_id, expected_head)
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
        )
        self._write_meta(meta)
        if expected_head is not ACCEPT_ONLY:
            self.activate(map_id, version, actor, expected_head)
        return meta

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
        if branches is not None and (
            not isinstance(branches, dict)
            or set(branches) != set(maps)
            or any(
                not isinstance(name, str) or name == "main"
                for name in branches.values()
            )
        ):
            raise ValueError("session branches must match its selected maps")
        if branches is not None:
            self.versions._actor(actor)
            existing = self.session(working_copy_id)
            if existing is not None and (
                existing["maps"] != maps or existing["branches"] != branches
            ):
                raise ValueError(
                    "working copy is already bound to another map/branch selection"
                )
        copies, missing, refs = [], [], []
        for map_id in maps:
            target = self.versions.working_copy(map_id, working_copy_id)
            copy = (
                self.versions.validate_working_copy(map_id, working_copy_id)
                if target.exists() or target.is_symlink()
                else None
            )
            selected = None
            if branches is not None:
                name = branches[map_id]
                self.versions._branch_name(name)
                branch_file = self.versions._branch_path(map_id, name)
                if not branch_file.exists() and not branch_file.is_symlink():
                    if name != f"work-{working_copy_id}":
                        raise ValueError("selected named branch is missing")
                    self.versions._actor(actor)
                    if copy is not None:
                        origin = copy["versionDigest"]
                    else:
                        head = self.versions.head(map_id)
                        if head is None:
                            raise ValueError("map has no active head; import it first")
                        origin = head["versionDigest"]
                    refs.append((map_id, name, origin))
                    selected = {"versionDigest": origin}
                else:
                    selected = self.versions.branch(map_id, name)
            if copy is not None:
                copies.append(copy)
            else:
                if resume_only:
                    raise ValueError("selected working copy is missing")
                head = selected if branches is not None else self.versions.head(map_id)
                if head is None:
                    raise ValueError(
                        f"map {map_id} has no active head; import and activate it first"
                    )
                self.versions.prepare(map_id, head["versionDigest"])
                missing.append((map_id, head["versionDigest"]))
        created_copies, created_refs = [], []
        try:
            for map_id, version in missing:
                if branches is None or any(item[0] == map_id for item in refs):
                    self.versions.check_head(map_id, version)
                elif (
                    self.versions.branch(map_id, branches[map_id])["versionDigest"]
                    != version
                ):
                    raise ValueError("stale session branch head")
                self.checkout(map_id, version, working_copy_id)
                created_copies.append(
                    self.versions.working_copy(map_id, working_copy_id)
                )
                copies.append(
                    self.versions.validate_working_copy(map_id, working_copy_id)
                )
            for map_id, name, origin in refs:
                self.versions.create_branch(map_id, name, origin, actor)
                created_refs.append(self.versions._branch_path(map_id, name))
            if branches is not None:
                self.bind_session(working_copy_id, maps, branches, actor)
        except Exception:
            # A renamed archive is the commit point; a failed final fsync must not destroy its complete copies.
            if branches is not None and self._session_path(working_copy_id).exists():
                raise
            # Only this attempt's new copies/refs are removed; pre-existing dirty work stays intact.
            for path in created_refs:
                path.unlink()
                _sync_directory(path.parent)
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
        fields = {key: value[key] for key in ("id", "maps", "branches")}
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
            or not isinstance(value.get("branches"), dict)
            or set(value["branches"]) != set(value["maps"])
            or type(value.get("startedAt")) is not int
            or value["startedAt"] < 0
        ):
            raise ValueError("invalid archived session")
        self.versions._actor(value.get("startedBy"))
        for map_id, branch in value["branches"].items():
            self.versions._map_id(map_id)
            if self.versions._branch_name(branch) == "main":
                raise ValueError("session cannot edit protected main")
        if value.get("selectionDigest") != self._selection_digest(value):
            raise ValueError("archived session selection checksum mismatch")
        return value

    def bind_session(self, session_id, maps, branches, actor):
        self.versions._actor(actor)
        existing = self.session(session_id)
        if existing is not None:
            if existing["maps"] != maps or existing["branches"] != branches:
                raise ValueError(
                    "working copy is already bound to another map/branch selection"
                )
            return existing
        value = {
            "schema": 1,
            "id": session_id,
            "maps": maps,
            "branches": branches,
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
                    "branches": self.versions.branches(map_id),
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
        shutil.rmtree(target, ignore_errors=True)
        with zipfile.ZipFile(archive_path) as archive:
            for entry in archive.infolist():
                if entry.is_dir() or entry.filename == "manifest.json":
                    continue
                destination = target / entry.filename
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(archive.read(entry))
        (target / "map-id").write_text(manifest["mapId"] + "\n")
        _chown_tree(target)
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
        if not isinstance(name, str) or not BRANCH_NAME.fullmatch(name):
            raise ValueError("canonical map branch name required")
        return name

    def _branch_path(self, map_id, name):
        self._map_id(map_id)
        self._branch_name(name)
        directory = self.archive / "branches" / map_id
        for path in (directory.parent, directory):
            if path.is_symlink() or path.exists() and not path.is_dir():
                raise ValueError("unsafe map branch directory")
        return directory / f"{name}.json"

    def branch(self, map_id, name) -> dict:
        self._branch_name(name)
        if name == "main":
            head = self.head(map_id)
            if head is None:
                raise ValueError("map main has no active head")
            return {
                "schema": 1,
                "mapId": map_id,
                "name": name,
                "versionDigest": head["versionDigest"],
                "generation": head["generation"],
                "workingCopyId": None,
                "createdBy": head["activatedBy"],
                "createdAt": head["activatedAt"],
            }
        path = self._branch_path(map_id, name)
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 65536:
            raise ValueError("map branch missing or unsafe")
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
            raise ValueError("invalid map branch")
        self._actor(value.get("createdBy"))
        self.read(map_id, value.get("versionDigest"))
        if value.get("workingCopyId") is not None:
            self.working_copy(map_id, value["workingCopyId"])
        return value

    def branches(self, map_id) -> list[dict]:
        values = [self.branch(map_id, "main")] if self.head(map_id) else []
        directory = self._branch_path(map_id, "work").parent
        for path in sorted(directory.glob("*.json")):
            if path.stem == "main":
                raise ValueError("main must use the active map head")
            values.append(self.branch(map_id, path.stem))
        return values

    def create_branch(self, map_id, name, version, actor) -> dict:
        self._actor(actor)
        if self._branch_name(name) == "main":
            raise ValueError("main is protected; submit a review")
        self.read(map_id, version)
        path = self._branch_path(map_id, name)
        if path.exists() or path.is_symlink():
            raise ValueError("map branch already exists")
        value = {
            "schema": 1,
            "mapId": map_id,
            "name": name,
            "versionDigest": version,
            "generation": 1,
            "workingCopyId": None,
            "createdAt": int(self._now() * 1000),
            "createdBy": actor,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_json(path, value, owner=False)
        return value

    def _ancestors(self, map_id, version) -> dict[str, dict]:
        pending, found = [self._digest(version)], {}
        while pending:
            digest = pending.pop()
            if digest in found:
                continue
            if len(found) >= 10000:
                raise ValueError("map history traversal limit exceeded")
            manifest = self.read(map_id, digest)
            found[digest] = manifest
            pending.extend(
                manifest.get(
                    "parents", [manifest["parent"]] if manifest["parent"] else []
                )
            )
        return found

    def commit_branch(
        self,
        map_id,
        name,
        files,
        actor,
        note,
        expected_head,
        working_copy_id,
        base_version,
    ):
        self._actor(actor)
        if name == "main":
            raise ValueError("main is protected; submit a review")
        self.working_copy(map_id, working_copy_id)
        branch = self.branch(map_id, name)
        if branch["versionDigest"] != expected_head:
            raise ValueError("stale branch head: refresh before committing")
        if base_version not in self._ancestors(map_id, expected_head) or (
            base_version != expected_head
            and branch.get("workingCopyId") != working_copy_id
        ):
            raise ValueError(
                "working copy is behind the branch; checkout its current head"
            )
        if not isinstance(note, str) or not note.strip() or len(note) > 200:
            raise ValueError("commit message required (1–200 characters)")
        if files == self.read(map_id, expected_head)["files"]:
            return expected_head
        version = self._record(
            map_id,
            files,
            parent=expected_head,
            author=actor,
            created_at=int(self._now() * 1000),
            source="commit",
            source_id=working_copy_id,
            note=note.strip(),
            parents=[expected_head],
        )
        # One ref write commits the snapshot; the editable copy and its original base stay intact.
        if self.branch(map_id, name)["versionDigest"] != expected_head:
            raise ValueError("stale branch head: refresh before committing")
        _atomic_json(
            self._branch_path(map_id, name),
            branch
            | {
                "versionDigest": version,
                "generation": branch["generation"] + 1,
                "workingCopyId": working_copy_id,
            },
            owner=False,
        )
        return version

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
                self._config(old), self._config(new) if config is None else config
            ),
        }

    def merge_preview(self, map_id, source, target, resolutions=None) -> dict:
        left, right = self._ancestors(map_id, source), self._ancestors(map_id, target)
        common = left.keys() & right.keys()
        parents = {
            parent
            for digest in common
            for parent in left[digest].get(
                "parents", [left[digest]["parent"]] if left[digest]["parent"] else []
            )
        }
        bases = common - parents
        if len(bases) != 1:
            raise ValueError("merge needs one unambiguous common ancestor")
        base = bases.pop()
        base_files, ours, theirs = (
            left[base]["files"],
            right[target]["files"],
            left[source]["files"],
        )
        if left[source]["kind"] != right[target]["kind"]:
            raise ValueError("map kind cannot change during merge")
        resolutions = {} if resolutions is None else resolutions
        if (
            not isinstance(resolutions, dict)
            or len(resolutions) > 10000
            or any(
                not isinstance(path, str)
                or not isinstance(choice, str)
                or choice not in {"ours", "theirs"}
                for path, choice in resolutions.items()
            )
        ):
            raise ValueError("invalid merge conflict choices")
        conflicts, used = [], set()

        def merge_value(old, own, incoming, path):
            if self._same(own, incoming) or self._same(incoming, old):
                return own
            if self._same(own, old):
                return incoming
            if old is MISSING and isinstance(own, dict) and isinstance(incoming, dict):
                old = {}
            if all(isinstance(value, dict) for value in (old, own, incoming)):
                merged = {}
                for key in sorted(old.keys() | own.keys() | incoming.keys()):
                    value = merge_value(
                        old.get(key, MISSING),
                        own.get(key, MISSING),
                        incoming.get(key, MISSING),
                        path + "/" + key.replace("~", "~0").replace("/", "~1"),
                    )
                    if value is not MISSING:
                        merged[key] = value
                return merged
            conflict = {
                "path": path,
                "kind": "json" if path.startswith("map.json#") else "binary",
                "basePresent": old is not MISSING,
                "oursPresent": own is not MISSING,
                "theirsPresent": incoming is not MISSING,
                "base": None if old is MISSING else old,
                "ours": None if own is MISSING else own,
                "theirs": None if incoming is MISSING else incoming,
            }
            conflicts.append(conflict)
            if path in resolutions:
                used.add(path)
                return own if resolutions[path] == "ours" else incoming
            return own

        files, merged_config = {}, None
        for name in sorted(base_files.keys() | ours.keys() | theirs.keys()):
            old, own, incoming = (
                base_files.get(name, MISSING),
                ours.get(name, MISSING),
                theirs.get(name, MISSING),
            )
            if (
                name == "map.json"
                and own != incoming
                and own != old
                and incoming != old
            ):
                merged_config = merge_value(
                    self._config(base_files),
                    self._config(ours),
                    self._config(theirs),
                    "map.json#",
                )
                raw = json.dumps(merged_config, ensure_ascii=False, indent=2).encode()
                files[name] = hashlib.sha256(raw).hexdigest()
            else:
                value = merge_value(old, own, incoming, name)
                if value is not MISSING:
                    files[name] = value
        if set(resolutions) != used:
            raise ValueError("merge choices must name actual conflicts only")
        changes = self._compare_files(map_id, target, None, ours, files, merged_config)
        return {
            "mapId": map_id,
            "baseVersion": base,
            "sourceVersion": source,
            "targetVersion": target,
            "conflicts": conflicts,
            "unresolved": [c["path"] for c in conflicts if c["path"] not in used],
            "files": files,
            "config": merged_config,
            "changes": {"files": changes["files"], "config": changes["config"]},
        }

    def prepare_merge(
        self, map_id, source, actor, expected_head, *, note="", resolutions=None
    ) -> str:
        self._actor(actor)
        if not isinstance(note, str) or len(note) > 200:
            raise ValueError("merge message too long")
        self.check_head(map_id, expected_head)
        if expected_head is None:
            raise ValueError("import main before merging")
        merged = self.merge_preview(map_id, source, expected_head, resolutions)
        if merged["unresolved"]:
            raise ValueError("merge conflicts: " + ", ".join(merged["unresolved"]))
        if merged["config"] is not None:
            self.validate_config(map_id, merged["config"])
            merged["files"]["map.json"] = self._put_blob(
                json.dumps(merged["config"], ensure_ascii=False, indent=2).encode()
            )
        for digest in merged["files"].values():
            self._verify_blob(digest)
        if source in self._ancestors(map_id, expected_head):
            return expected_head
        if expected_head in self._ancestors(map_id, source):
            version = source
        else:
            version = self._record(
                map_id,
                merged["files"],
                parent=expected_head,
                parents=[expected_head, source],
                author=actor,
                created_at=int(self._now() * 1000),
                source="merge",
                source_id=source,
                note=note or "Merge map changes",
            )
        return version

    def merge(
        self, map_id, source, actor, expected_head, *, note="", resolutions=None
    ) -> dict:
        version = self.prepare_merge(
            map_id, source, actor, expected_head, note=note, resolutions=resolutions
        )
        return self.activate(map_id, version, actor, expected_head)

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
            or len(ancestors) > 2
            or any(not isinstance(item, str) for item in ancestors)
            or len(set(ancestors)) != len(ancestors)
            or (ancestors[0] if ancestors else None) != parent
        ):
            raise ValueError("invalid map commit parents")
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
        files = {"map.json": self._put_blob(config)}
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
                data = path.read_bytes()
                if data or path.suffix == ".mcc":
                    files[name] = self._put_blob(data)
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
        if (
            shutil.disk_usage(target.parent).free
            < sum(path.stat().st_size for path in files.values()) + SNAPSHOT_MARGIN
        ):
            raise OSError("not enough free space for ZIP export")
        manifest = {
            "schemaVersion": 1,
            "mapId": meta["mapId"],
            "version": meta["versionDigest"],
            "baseVersion": meta.get("baseVersion") or "template",
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
                Path(directory).chmod(0o755)
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
        version = _sha256(manifest_file)
        manifest = self.read(map_id, version)
        marker = map_package._json((target / "world" / BASE_MARKER).read_bytes())
        if (
            not isinstance(marker, dict)
            or marker.get("versionDigest") != version
            or marker.get("templateDigest") != manifest["worldDigest"]
        ):
            raise ValueError("working copy base mismatch")
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


def _chown_tree(root: Path) -> None:
    for directory, _, files in os.walk(root):
        _chown(Path(directory))
        os.chmod(directory, 0o755)
        for name in files:
            _chown(Path(directory) / name)
            os.chmod(Path(directory) / name, 0o644)
