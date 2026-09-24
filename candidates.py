"""Workshop map candidates: snapshot a map from the workshop, review it on the site, publish it to the game.

Flow (blockfield-releases#8, owner decision 2026-09-24 "через проверку"):
  candidate  -- snapshot of one map's workshop world + its markup JSON into an immutable ZIP (map_package layout v1),
                taken after the controller saves and fully stops the workshop;
  publish    -- an admin accepts a pending candidate: the game template (templates/maps/<id>) and the published map
                JSON (lobby/blockfield-published/maps, which the game prefers over the image's JSON) are replaced,
                the previous ones kept as a backup; new matches pick the map up, running ones finish on the old one;
  reject     -- an admin declines a pending candidate;
  rollback   -- restore the map's previous template and JSON from the newest backup.
Everything runs in the root controller on the host; the site only writes inbox requests and reads candidates.json.
"""
from __future__ import annotations

import ctypes
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
BACKUPS_KEPT = 3
OWNER = (1000, 1000)  # the game and the workshop run as uid/gid 1000 and read these files
BASE_MARKER = "blockfield-base.json"  # written by blockfield-mod WorkshopMaps when it seeds a draft from the template
MIN_FREE_BYTES = 256 << 20


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
        newer = [m for m in self.list() if m["mapId"] == meta["mapId"] and m["status"] == "published"
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
        with zipfile.ZipFile(archive_path) as archive:
            needed = sum(entry.file_size for entry in archive.infolist())
            if shutil.disk_usage(self.templates).free < needed + MIN_FREE_BYTES:
                raise OSError("not enough free space to publish the map")
            config = map_package._json(archive.read("map.json"))
            stamp = int(self._now() * 1000)
            staged = self.templates / f".{map_id}.new-{stamp}"
            try:
                for entry in archive.infolist():
                    if entry.filename.startswith("world/") and not entry.is_dir():
                        # Names are validated by map_package (world/<kind>/r.X.Z.mca only): no traversal possible.
                        destination = staged / entry.filename[len("world/"):]
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        destination.write_bytes(archive.read(entry))
                _chown_tree(staged)
            except BaseException:
                shutil.rmtree(staged, ignore_errors=True)
                raise
        backup = self.backups / f"{map_id}-{stamp}"
        backup.mkdir(parents=True)
        json_path = self.published / f"{map_id}.json"
        if json_path.exists():
            shutil.copy2(json_path, backup / "map.json")
        self.published.mkdir(parents=True, exist_ok=True)
        if current.exists():
            # Exchanged in one rename: a match copying the template at this moment sees the old or the new world,
            # never a missing directory. The old one then moves into the backup.
            _exchange(staged, current)
            os.replace(staged, backup / "template")
        else:
            os.replace(staged, current)
        try:
            _atomic_json(json_path, config)
        except BaseException:
            # World and JSON go live together or not at all; the half-made backup must not become a rollback target.
            if (backup / "template").exists():
                _exchange(backup / "template", current)
            else:
                shutil.rmtree(current, ignore_errors=True)
            shutil.rmtree(backup, ignore_errors=True)
            raise
        meta.update(status="published", decidedBy=actor, decidedAt=stamp, backup=backup.name, forced=force)
        for other in self.list():
            if other["mapId"] == map_id and other["status"] == "published" and other["id"] != candidate_id:
                other.update(status="superseded")
                self._write_meta(other)
        self._write_meta(meta)
        self._prune_backups(map_id)
        self._rebase_draft(map_id)
        return meta

    def reject(self, candidate_id: str, actor: dict) -> dict:
        meta = self._pending(candidate_id)
        meta.update(status="rejected", decidedBy=actor, decidedAt=int(self._now() * 1000))
        self._write_meta(meta)
        return meta

    def rollback(self, map_id: str, actor: dict) -> dict:
        if not isinstance(map_id, str) or not MAP_ID.fullmatch(map_id):
            raise ValueError("invalid map id")
        backups = self._backups(map_id)
        if not backups:
            raise ValueError("no backup of this map to roll back to")
        backup = backups[-1]
        stamp = int(self._now() * 1000)
        current, json_path = self.templates / map_id, self.published / f"{map_id}.json"
        # What is live now becomes a backup too, so a mistaken rollback can itself be undone.
        undo = self.backups / f"{map_id}-{stamp}"
        undo.mkdir(parents=True)
        if current.exists():
            os.replace(current, undo / "template")
        if json_path.exists():
            os.replace(json_path, undo / "map.json")
        if (backup / "template").exists():
            os.replace(backup / "template", current)
        if (backup / "map.json").exists():
            os.replace(backup / "map.json", json_path)
        shutil.rmtree(backup, ignore_errors=True)
        for meta in self.list():
            if meta["mapId"] == map_id and meta["status"] == "published":
                meta.update(status="rolled_back", decidedBy=actor, decidedAt=stamp)
                self._write_meta(meta)
        return {"mapId": map_id, "restored": backup.name}

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

    def _rebase_draft(self, map_id: str) -> None:
        """The draft now descends from the template it just became; the next candidate from it needs no force."""
        world = self.workshop_data / "world" / "dimensions" / "blockfield_workshop" / map_id
        digest = template_digest(self.templates / map_id)
        if world.is_dir() and digest:
            _atomic_json(world / BASE_MARKER, {"schema": 1, "templateDigest": digest, "seededAt": int(self._now() * 1000)})

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
        published = [m for m in self.list() if m["mapId"] == map_id and m["status"] == "published"]
        return f"ws-{published[0]['createdAt']}" if published else "template"

    def _write_meta(self, meta: dict) -> None:
        _atomic_json(self.candidates / f"{meta['id']}.json", meta, owner=False)

    def _backups(self, map_id: str) -> list[Path]:
        found = []
        for path in self.backups.glob(f"{map_id}-*") if self.backups.is_dir() else []:
            stamp = path.name[len(map_id) + 1:]
            if stamp.isdigit():
                found.append((int(stamp), path))
        return [path for _, path in sorted(found)]

    def _prune_backups(self, map_id: str) -> None:
        for path in self._backups(map_id)[:-BACKUPS_KEPT]:
            shutil.rmtree(path, ignore_errors=True)

    def _prune(self) -> None:
        decided = [m for m in self.list() if m["status"] not in {"pending", "published"}]
        for meta in decided[CANDIDATES_KEPT:]:
            (self.candidates / f"{meta['id']}.zip").unlink(missing_ok=True)
            (self.candidates / f"{meta['id']}.json").unlink(missing_ok=True)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


_RENAME_EXCHANGE = 2


def _exchange(a: Path, b: Path) -> None:
    """Atomically swap two paths on one filesystem (renameat2 RENAME_EXCHANGE, Linux 3.15+)."""
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.renameat2(-100, os.fsencode(a), -100, os.fsencode(b), _RENAME_EXCHANGE) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(a))


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
