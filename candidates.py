"""Immutable workshop candidates. Acceptance never modifies game templates; delivery requires a pack release."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import time
import zipfile
from contextlib import contextmanager, nullcontext
from pathlib import Path
from uuid import uuid4

import map_package

WORLD_KINDS = ("region", "entities", "poi")
MAP_ID = re.compile(r"[a-z0-9_]{1,32}")
CANDIDATE_ID = re.compile(r"[0-9a-f]{32}")
CANDIDATES_KEPT = 30
OWNER = (1000, 1000)  # the game and the workshop run as uid/gid 1000 and read these files
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
    return hashlib.sha256("".join(f"{name}\0{digest}\n" for name, digest in sorted(lines)).encode()).hexdigest()


class MapStore:
    def __init__(self, workshop_data: Path, templates: Path, published: Path, candidates: Path, backups: Path,
                 *, repo_maps: Path | None = None, hold_saves=None, now=time.time):
        self.workshop_data = Path(workshop_data)
        self.templates = Path(templates)
        self.published = Path(published)
        self.candidates = Path(candidates)
        self.backups = Path(backups)
        # The game's copy of the image JSON (git server/lobby/blockfield/maps): a published overlay equal to it is retired.
        self.repo_maps = Path(repo_maps) if repo_maps else None
        self._digests = {}
        # () -> context manager that keeps the running workshop from writing region files meanwhile.
        self.hold_saves = hold_saves or nullcontext
        self._now = now

    # ------------------------------------------------------------------ requests

    def create(self, map_id: str, author: dict, note: str = "") -> dict:
        if not isinstance(map_id, str) or not MAP_ID.fullmatch(map_id):
            raise ValueError("invalid map id")
        world = self.workshop_data / "world" / "dimensions" / "blockfield_workshop" / map_id
        if not (world / "region").is_dir():
            raise ValueError("the map was never opened in the workshop")
        self.candidates.mkdir(parents=True, exist_ok=True)
        # Exports run under the controller lock, so any temporary archive here was left by one that was killed.
        for stale in self.candidates.glob(".*.zip.tmp"):
            stale.unlink(missing_ok=True)
        candidate_id = uuid4().hex
        now_ms = int(self._now() * 1000)
        target = self.candidates / f"{candidate_id}.zip"
        temporary = self.candidates / f".{candidate_id}.zip.tmp"
        files = {}
        try:
            with self.hold_saves(), zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
                config = self._map_json(map_id)
                config_bytes = json.dumps(config, ensure_ascii=False, indent=2).encode()
                archive.writestr("map.json", config_bytes)
                files["map.json"] = hashlib.sha256(config_bytes).hexdigest()
                for kind in WORLD_KINDS:
                    for region in sorted((world / kind).glob("r.*.*.mca")) if (world / kind).is_dir() else []:
                        data = region.read_bytes()
                        if not data:
                            continue  # an empty region file is a never-written placeholder, not map data
                        name = f"world/{kind}/{region.name}"
                        archive.writestr(name, data)
                        files[name] = hashlib.sha256(data).hexdigest()
                manifest = {"schemaVersion": 1, "mapId": map_id, "version": f"ws-{now_ms}",
                            "baseVersion": self._published_version(map_id), "files": files}
                archive.writestr("manifest.json", json.dumps(manifest, sort_keys=True).encode())
            digest = _sha256(temporary)
            map_package.validate(temporary, digest)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        meta = {"id": candidate_id, "mapId": map_id, "status": "pending", "author": author, "note": str(note)[:200],
                "createdAt": now_ms, "sha256": digest, "size": target.stat().st_size,
                "regions": sum(1 for name in files if name.startswith("world/region/")),
                "base": self._draft_base(world)}
        self._write_meta(meta)
        self._prune()
        return meta

    def publish(self, candidate_id: str, actor: dict, *, force: bool = False) -> dict:
        meta = self._pending(candidate_id)
        newer = [m for m in self.list() if m["mapId"] == meta["mapId"] and m["status"] in {"accepted", "published"}
                 and m["createdAt"] > meta["createdAt"]]
        if newer:
            raise ValueError("a newer version of this map is already published")
        archive_path = self.candidates / f"{candidate_id}.zip"
        manifest = map_package.validate(archive_path, meta["sha256"])
        map_id = manifest["mapId"]
        current = self.templates / map_id
        live = template_digest(current)
        # The draft was copied from a template; if that template changed since (another publish, rollback, a manual
        # copy), publishing would silently throw those changes away. Only an explicit second confirmation may.
        if not force:
            if meta.get("base") is None:
                raise ValueError("base unknown: the draft predates base tracking; confirm to publish anyway")
            if meta["base"] != live:
                raise ValueError("base changed: the map's template changed after this draft was copied; confirm to overwrite")
        meta.update(status="accepted", decidedBy=actor, decidedAt=int(self._now() * 1000), forced=force)
        self._write_meta(meta)
        return meta

    def reject(self, candidate_id: str, actor: dict) -> dict:
        meta = self._pending(candidate_id)
        meta.update(status="rejected", decidedBy=actor, decidedAt=int(self._now() * 1000))
        self._write_meta(meta)
        return meta

    def rollback(self, map_id: str, actor: dict) -> dict:
        raise ValueError("map rollback requires a new pack release pinned to the previous package")

    # ------------------------------------------------------------------ export for the site

    def list(self) -> list[dict]:
        metas = []
        for path in self.candidates.glob("*.json") if self.candidates.is_dir() else []:
            if not CANDIDATE_ID.fullmatch(path.stem):
                continue
            try:
                metas.append(json.loads(path.read_text()))
            except (OSError, ValueError):
                continue
        return sorted(metas, key=lambda m: m.get("createdAt", 0), reverse=True)

    def export(self) -> dict:
        backups = {}
        for path in self.backups.iterdir() if self.backups.is_dir() else []:
            map_id = path.name.rsplit("-", 1)[0]
            backups[map_id] = backups.get(map_id, 0) + 1
        candidates = self.list()
        for meta in candidates:
            if meta.get("status") == "pending":
                base = meta.get("base")
                meta["baseMatches"] = None if base is None else base == self._cached_digest(meta["mapId"])
        overlays = {}
        for path in sorted(self.published.glob("*.json")) if self.published.is_dir() else []:
            overlays[path.stem] = {"differsFromRepo": not self._matches_repo(path)}
        return {"schema": 1, "candidates": candidates, "backups": backups, "overlays": overlays}

    def retire_merged_overlays(self) -> list[str]:
        """A published JSON that git (the image's copy in the game) now has verbatim is dropped: later git edits apply again."""
        retired = []
        for path in sorted(self.published.glob("*.json")) if self.published.is_dir() and self.repo_maps else []:
            if MAP_ID.fullmatch(path.stem) and self._matches_repo(path):
                path.unlink()
                retired.append(path.stem)
        return retired

    # ------------------------------------------------------------------ playtest

    def sha256_of(self, candidate_id) -> str:
        if not isinstance(candidate_id, str) or not CANDIDATE_ID.fullmatch(candidate_id):
            raise ValueError("invalid candidate id")
        try:
            meta = json.loads((self.candidates / f"{candidate_id}.json").read_text())
        except (OSError, ValueError):
            raise ValueError("unknown candidate") from None
        if not (self.candidates / f"{candidate_id}.zip").exists():
            raise ValueError("the candidate's archive is gone")
        return meta["sha256"]

    def extract(self, sha256: str, target: Path) -> str:
        """The candidate with this digest as <target>/{map-id, map.json, world/...} for the test profile; returns the map id."""
        found = [m for m in self.list() if m.get("sha256") == sha256]
        if not found:
            raise ValueError("no candidate with this digest")
        archive_path = self.candidates / f"{found[0]['id']}.zip"
        manifest = map_package.validate(archive_path, sha256)
        target = Path(target)
        shutil.rmtree(target, ignore_errors=True)
        with zipfile.ZipFile(archive_path) as archive:
            for entry in archive.infolist():
                if entry.is_dir() or entry.filename == "manifest.json":
                    continue
                destination = target / entry.filename  # validated layout: map.json and world/<kind>/r.X.Z.mca
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(archive.read(entry))
        (target / "map-id").write_text(manifest["mapId"] + "\n")
        _chown_tree(target)
        return manifest["mapId"]

    # ------------------------------------------------------------------ helpers

    def _pending(self, candidate_id) -> dict:
        if not isinstance(candidate_id, str) or not CANDIDATE_ID.fullmatch(candidate_id):
            raise ValueError("invalid candidate id")
        path = self.candidates / f"{candidate_id}.json"
        if not path.exists():
            raise ValueError("unknown candidate")
        meta = json.loads(path.read_text())
        if meta.get("status") != "pending":
            raise ValueError(f"candidate is {meta.get('status')}, not pending")
        return meta

    def _map_json(self, map_id: str) -> dict:
        """The workshop's edited copy (markup) wins over the JSON the workshop got from the image."""
        for directory in (self.workshop_data / "world" / "blockfield_workshop" / "maps",
                          self.workshop_data / "blockfield" / "maps"):
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
        return value if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) else None

    def _cached_digest(self, map_id: str) -> str | None:
        root = self.templates / map_id
        try:
            key = tuple(sorted((str(p), p.stat().st_size, p.stat().st_mtime_ns) for p in root.rglob("*") if p.is_file()))
        except OSError:
            return None
        if self._digests.get(map_id, (None,))[0] != key:
            self._digests[map_id] = (key, template_digest(root))
        return self._digests[map_id][1]

    def _matches_repo(self, published: Path) -> bool:
        if not self.repo_maps:
            return False
        try:
            return json.loads(published.read_text()) == json.loads((self.repo_maps / published.name).read_text())
        except (OSError, ValueError):
            return False

    def _published_version(self, map_id: str) -> str:
        published = [m for m in self.list() if m["mapId"] == map_id and m["status"] in {"accepted", "published"}]
        return f"ws-{published[0]['createdAt']}" if published else "template"

    def _write_meta(self, meta: dict) -> None:
        _atomic_json(self.candidates / f"{meta['id']}.json", meta, owner=False)

    def _prune(self) -> None:
        decided = [m for m in self.list() if m["status"] not in {"pending", "accepted", "published"}]
        for meta in decided[CANDIDATES_KEPT:]:
            (self.candidates / f"{meta['id']}.zip").unlink(missing_ok=True)
            (self.candidates / f"{meta['id']}.json").unlink(missing_ok=True)


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
        os.chmod(temporary, 0o644)
        if owner:
            _chown(Path(temporary))
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


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
