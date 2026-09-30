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
        self, map_id: str, author: dict, note: str = "", *, working_copy_id=None
    ) -> dict:
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
        self._write_meta(meta)
        return meta

    def publish(
        self,
        candidate_id: str,
        actor: dict,
        *,
        force: bool = False,
        expected_head=ACCEPT_ONLY,
    ) -> dict:
        meta = self._pending(
            candidate_id, allow_accepted=expected_head is not ACCEPT_ONLY
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
        return self.activate(map_id, version_digest, actor, expected_head)

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
        if manifest["source"] != "template" and not any(
            candidate.get("versionDigest") == version
            and candidate["status"] in {"accepted", "published"}
            for candidate in self.list()
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
        self, working_copy_id: str, maps: list[str], *, resume_only=False
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
        copies, missing = [], []
        for map_id in maps:
            target = self.versions.working_copy(map_id, working_copy_id)
            if target.exists() or target.is_symlink():
                copies.append(
                    self.versions.validate_working_copy(map_id, working_copy_id)
                )
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
        for map_id, version in missing:
            self.versions.check_head(map_id, version)
            self.checkout(map_id, version, working_copy_id)
            copies.append(self.versions.validate_working_copy(map_id, working_copy_id))
        return copies

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
                {**entry, "kind": config.get("kind", "arena"), "mapJson": config}
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
            checksum, size = self.versions.export_package(meta, target)
            meta.update(
                sha256=checksum,
                size=size,
                exportCompressor="zlib-1.3.2",
                exportedAt=int(self._now() * 1000),
            )
            self._write_meta(meta)
            self.prune_exports()
            return target
        raise ValueError("the candidate's archive is gone")

    def exported_archive(self, meta: dict) -> Path | None:
        target = self.versions.archive / "exports" / f"{meta['id']}.zip"
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
    ):
        self._map_id(map_id)
        self._validate_files(files)
        config = map_package._json(self._verify_blob(files["map.json"]).read_bytes())
        if not isinstance(config, dict) or config.get("id") != map_id:
            raise ValueError("map config/world identity mismatch")
        kind = config.get("kind", "arena")
        if kind not in {"arena", "lobby", "limbo"}:
            raise ValueError("unknown map kind")
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


def _atomic_json(path: Path, value, *, owner: bool = True) -> None:
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(json.dumps(value, ensure_ascii=False, indent=2).encode())
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o644)
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
