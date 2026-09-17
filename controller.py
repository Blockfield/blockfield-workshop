"""Durable single-backend transition core. The Docker adapter lives in docker_backend.py.

A trusted adapter must fence all other starters and expose actual process readiness,
not an optimistic HTTP response. Proxy/web consumers must fail closed on absent state.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace, asdict, dataclass
import fcntl
import json
import os
import re
import stat
import sys
from pathlib import Path
import tempfile
import time
from typing import Protocol
import unicodedata
from uuid import UUID, uuid4


@dataclass(frozen=True)
class Goal:
    profile: str
    release: str
    session: str | None = None
    candidate: str | None = None

    def __post_init__(self):
        if self.profile not in {"game", "workshop", "test"}:
            raise ValueError("unknown profile")
        if not isinstance(self.release, str) or not self.release or len(self.release) > 128:
            raise ValueError("missing pinned release")
        if self.profile == "game":
            if self.session is not None or self.candidate is not None:
                raise ValueError("game must not run a draft/candidate")
        else:
            if self.session is None:
                raise ValueError("session required")
            UUID(self.session)
            if self.profile == "test":
                if not isinstance(self.candidate, str) or len(self.candidate) != 64 or any(c not in "0123456789abcdef" for c in self.candidate):
                    raise ValueError("immutable candidate SHA256 required")
            elif self.candidate is not None:
                raise ValueError("workshop is not a candidate playtest")


@dataclass(frozen=True)
class Process:
    goal: Goal
    ready: bool


class Backend(Protocol):
    def preflight(self, goal: Goal) -> None:
        """Check space, separate data, pinned profile and fencing of restart/deploy paths."""
    def observe(self) -> list[Process]:
        """Return EVERY full backend, including unready/stopping processes."""
    def request_drain(self) -> None:
        """Reject new campaigns; keep all rounds and valid reconnects of active campaigns."""
    def drained(self) -> bool:
        """True only after all campaigns/battles ended and their results are durable locally."""
    def save_and_freeze(self, process: Process) -> None:
        """Finish tools, save world+config and stop new writes; raise on uncertainty."""
    def stop(self, process: Process) -> None:
        """Graceful stop; success is rechecked by observe before any new start."""
    def fence_pending_starts(self) -> None:
        """Cancel/settle ALL previously accepted starts before explicit recovery.

        Returning must guarantee no delayed old request can later start a backend.
        Raise if that cannot be established. An empty observe() alone is not fencing.
        """
    def start(self, goal: Goal) -> None:
        """Start the single allowed backend with isolated data; no parallel staging."""


DEFAULT_MESSAGE = "На сервере проводятся технические работы. Обычный вход временно недоступен."
OPEN_GATES = {"game": "public", "workshop": "builders", "test": "testers"}
ROLES = {"builder", "lead", "admin"}
# server/lobby/blockfield/maps/*.json ids (blockfield-mod); the controller only validates, it does not read the mod's data.
MAP_IDS = frozenset({"baxmut", "berdichev", "dm_arena", "fields", "firing_range", "konstaxa", "kozyatyn", "zaliznichne"})
NAME = re.compile(r"[A-Za-z0-9_]{3,16}")
INBOX_NAME = re.compile(r"[0-9a-f-]{36}\.json")
INBOX_MAX_BYTES = 65536
INBOX_BATCH = 20
INBOX_TTL_MS = 10 * 60 * 1000
RESULTS_KEPT = 200


def member(value) -> dict:
    """Validated ACL entry; the proxy matches both the Drasl profile id and the current name.

    Optional "maps": which map ids a builder may edit; missing means all maps (backwards
    compatible). lead/admin always have all maps, so an explicit list on those roles is dropped
    rather than rejected -- callers that pass one through unchanged (e.g. a full site payload) stay valid.
    """
    if not isinstance(value, dict) or set(value) - {"draslId", "name", "role", "maps"}:
        raise ValueError("invalid member")
    drasl = value.get("draslId")
    if not isinstance(drasl, str):
        raise ValueError("invalid draslId")
    drasl = drasl.replace("-", "").lower()
    if len(drasl) != 32 or any(c not in "0123456789abcdef" for c in drasl):
        raise ValueError("invalid draslId")
    name, role = value.get("name"), value.get("role", "builder")
    if not isinstance(name, str) or not NAME.fullmatch(name) or role not in ROLES:
        raise ValueError("invalid member name/role")
    result = {"draslId": drasl, "name": name, "role": role}
    if "maps" in value and value["maps"] is not None:
        maps = value["maps"]
        if not isinstance(maps, list) or any(not isinstance(m, str) for m in maps):
            raise ValueError("invalid maps list")
        if len(set(maps)) != len(maps):
            raise ValueError("duplicate map id")
        unknown = set(maps) - MAP_IDS
        if unknown:
            raise ValueError(f"unknown map id: {sorted(unknown)}")
        if role == "builder":
            result["maps"] = maps
    return result


def valid_message(value) -> str:
    if not isinstance(value, str) or len(value) > 200:
        raise ValueError("message must be text of at most 200 characters")
    # Same rule as the site: control characters and Minecraft formatting codes only.
    if "§" in value or any(unicodedata.category(c) == "Cc" for c in value):
        raise ValueError("message must not contain control characters or §")
    return value.strip()


def atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + "-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fchmod(handle.fileno(), mode)
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        Path(temporary).unlink(missing_ok=True)


class Controller:
    def __init__(self, directory: Path, backend: Backend, *, gate_file: Path | None = None,
                 public_file: Path | None = None, message: str = DEFAULT_MESSAGE,
                 admin_dir: Path | None = None, inbox_dir: Path | None = None):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.directory.is_symlink():
            raise ValueError("state directory must not be a symlink")
        self.file = self.directory / "state.json"
        self.members_file = self.directory / "members.json"
        self.gate_file = Path(gate_file) if gate_file else self.directory / "gate.json"
        self.public_file = Path(public_file) if public_file else self.directory / "public.json"
        self.message_file = self.directory / "message.json"
        self.default_message = message.strip()[:500] or DEFAULT_MESSAGE
        self.admin_dir = Path(admin_dir) if admin_dir else None
        self.inbox_dir = Path(inbox_dir) if inbox_dir else None
        if self.admin_dir:
            (self.admin_dir / "results").mkdir(parents=True, exist_ok=True)
            for path in (self.admin_dir, self.admin_dir / "results"):
                os.chmod(path, 0o755)
        self.backend = backend

    @contextmanager
    def _locked(self, name: str = "controller.lock"):
        flags = os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW
        fd = os.open(self.directory / name, flags, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield
        finally:
            os.close(fd)

    def _read(self) -> dict:
        # Check links before existence: a dangling link is corruption, not fresh state.
        if self.file.is_symlink():
            raise ValueError("invalid state file")
        try:
            fd = os.open(self.file, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return {"schema": 1, "phase": "UNINITIALIZED", "gate": "closed", "operation": None, "desired": None, "revision": 0}
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 16384:
                raise ValueError("invalid state file")
            data = handle.read(16385)
        if len(data) > 16384:
            raise ValueError("invalid state file")

        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate state key")
                result[key] = value
            return result

        state = json.loads(data, object_pairs_hook=unique)
        if not isinstance(state, dict) or type(state.get("schema")) is not int or state["schema"] != 1:
            raise ValueError("unsupported state schema")
        if type(state.get("revision")) is not int or state["revision"] < 0:
            raise ValueError("invalid state revision")
        gates = {"UNINITIALIZED": "closed", "REQUESTED": "closed", "DRAINING": "draining",
                 "SAVING": "closed", "STARTING": "closed", "ERROR": "closed",
                 "game": "public", "workshop": "builders", "test": "testers"}
        phase = state.get("phase")
        if not isinstance(phase, str) or phase not in gates or state.get("gate") not in ("closed", gates[phase]):
            raise ValueError("invalid state phase/gate")
        if phase == "UNINITIALIZED":
            if state["revision"] != 0 or state.get("desired") is not None or state.get("operation") is not None:
                raise ValueError("invalid uninitialized state")
            return state
        if state["revision"] < 1 or not isinstance(state.get("desired"), dict) or not isinstance(state.get("operation"), str):
            raise ValueError("invalid operation state")
        try:
            UUID(state["operation"])
            goal = Goal(**state["desired"])
        except (ValueError, TypeError, AttributeError) as error:
            raise ValueError("invalid operation intent") from error
        if phase in {"game", "workshop", "test"} and phase != goal.profile:
            raise ValueError("stable phase does not match desired profile")
        return state

    def _write(self, state: dict) -> None:
        atomic_write(self.file, json.dumps(state, ensure_ascii=False, sort_keys=True, allow_nan=False).encode())
        self._export(state)

    def members(self) -> list[dict]:
        """Missing or invalid ACL admits nobody."""
        try:
            return [member(entry) for entry in json.loads(self.members_file.read_bytes()[:65536])]
        except (OSError, ValueError, TypeError):
            return []

    def set_members(self, entries: list) -> list[dict]:
        members = [member(entry) for entry in entries]
        if len({m["draslId"] for m in members}) != len(members):
            raise ValueError("duplicate member")
        with self._locked():
            atomic_write(self.members_file, json.dumps(members, sort_keys=True).encode())
            self._export(self._read())
        return members

    @property
    def message(self) -> str:
        """Admin override from message.json, else the configured default."""
        try:
            value = json.loads(self.message_file.read_bytes()[:4096])["message"]
            return valid_message(value) or self.default_message
        except (OSError, ValueError, TypeError, KeyError):
            return self.default_message

    def set_message(self, value) -> str:
        """Empty text removes the override."""
        value = valid_message(value)
        with self._locked():
            if value:
                atomic_write(self.message_file, json.dumps({"message": value}, ensure_ascii=False).encode())
            else:
                self.message_file.unlink(missing_ok=True)
            self._export(self._read())
        return self.message

    def status(self) -> dict:
        return self._read()

    def _export(self, state: dict) -> None:
        """Proxy gate (private ACL) and public site status. Rewritten only when content changes."""
        gate = state["gate"] if state["gate"] in {"public", "draining", "builders"} else "closed"
        profile = (state.get("desired") or {}).get("profile", "game")
        backend = "workshop" if gate == "builders" or (gate == "closed" and profile == "workshop") else "game"
        gate_doc = {"schema": 1, "revision": state["revision"], "gate": gate, "backend": backend,
                    "message": "" if gate == "public" else self.message,
                    "allowed": self.members() if gate == "builders" else []}
        self._replace_if_changed(self.gate_file, gate_doc, keep=())
        mode = {"public": "game", "draining": "draining", "builders": "workshop"}.get(gate, "maintenance")
        public = {"mode": mode, "message": "" if mode == "game" else self.message, "since": int(time.time() * 1000)}
        self._replace_if_changed(self.public_file, public, keep=("since",))
        if self.admin_dir:
            self._replace_if_changed(self.admin_dir / "members.json", self.members(), keep=())
            status = {"schema": 1, "revision": state["revision"], "phase": state["phase"], "gate": state["gate"],
                      "error": state.get("error"), "desired": state.get("desired"), "message": self.message,
                      "updatedAt": int(time.time() * 1000)}
            self._replace_if_changed(self.admin_dir / "status.json", status, keep=("updatedAt",))

    @staticmethod
    def _replace_if_changed(path: Path, document, keep: tuple) -> None:
        def strip(value):
            return {k: v for k, v in value.items() if k not in keep} if isinstance(value, dict) else value
        try:
            if strip(json.loads(path.read_bytes()[:65536])) == strip(document):
                return
        except (OSError, ValueError):
            pass
        atomic_write(path, json.dumps(document, ensure_ascii=False, sort_keys=True).encode(), 0o644)

    def request(self, operation: str, goal: Goal, expected_revision: int, *, recover: bool = False) -> dict:
        """Persist intent only. No side effect on a running backend until step is called.

        recover=True accepts an ERROR state, but only toward the game profile; the phase stays
        ERROR so only an explicit step(recover=True) can act on it.
        """
        UUID(operation)
        with self._locked():
            state = self._read()
            if state.get("operation") == operation:
                if state.get("desired") != asdict(goal):
                    raise ValueError("operation ID reused with different intent")
                return state
            if type(expected_revision) is not int or expected_revision != state["revision"]:
                raise ValueError("stale state revision")
            recovering = recover and state.get("phase") == "ERROR" and goal.profile == "game"
            if state.get("phase") not in {"UNINITIALIZED", "game", "workshop", "test"} and not recovering:
                raise RuntimeError("operation in progress or requires recovery")
            self.backend.preflight(goal)
            revision = state["revision"] + 1
            state = {"schema": 1, "revision": revision, "phase": "ERROR" if recovering else "REQUESTED", "gate": "closed",
                     "operation": operation, "desired": asdict(goal), "error": state.get("error") if recovering else None}
            self._write(state)
            return state

    def step(self, *, recover: bool = False) -> dict:
        """One reconciliation under a process lock. Retry STARTING/DRAINING, don't start a worker.

        ERROR requires explicit recovery. STARTING with no visible process waits closed:
        absence cannot distinguish a failed request from an accepted, delayed start.
        Explicit recovery fences pending starts before considering another start.
        A repeated step never counts as forced interruption. State is written only when
        phase/gate/error change, so a stable or draining gate never flaps through closed.
        """
        with self._locked():
            state = self._read()
            if state.get("desired") is None:
                raise RuntimeError("no requested operation")
            if state.get("phase") == "ERROR" and not recover:
                self._export(state)
                return state
            previous_phase = state["phase"]
            goal = Goal(**state["desired"])

            def commit(phase: str, gate: str, error: str | None = None) -> None:
                if (state["phase"], state["gate"], state.get("error")) != (phase, gate, error):
                    state.update(phase=phase, gate=gate, error=error)
                    self._write(state)

            try:
                self.backend.preflight(goal)
                if recover and previous_phase in {"STARTING", "ERROR"}:
                    commit(previous_phase, "closed", state.get("error"))
                    self.backend.fence_pending_starts()
                processes = self.backend.observe()
                if len(processes) > 1:
                    raise RuntimeError("multiple full backends observed")
                current = processes[0] if processes else None
                if (current is not None and goal.profile == current.goal.profile == "game"
                        and current.goal.release != goal.release and replace(current.goal, release=goal.release) == goal):
                    # A release tag redeployed the game image; follow it instead of draining and restarting.
                    goal = current.goal
                    state["desired"] = asdict(goal)
                    self._write(state)
                if current is not None and current.goal == goal:
                    if current.ready:
                        commit(goal.profile, OPEN_GATES[goal.profile])
                    else:
                        commit("STARTING", "closed")
                    self._export(state)
                    return state
                if previous_phase == "STARTING" and not recover:
                    if current is not None:
                        raise RuntimeError("unexpected backend while start is unresolved")
                    # Intent was durable before start(). Never reissue it automatically,
                    # including after a controller crash or delayed process visibility.
                    self._export(state)
                    return state
                if current is not None:
                    if current.goal.profile == "game":
                        commit("DRAINING", "draining")
                        self.backend.request_drain()
                        if not self.backend.drained():
                            return state
                    commit("SAVING", "closed")
                    self.backend.save_and_freeze(current)
                    self.backend.stop(current)
                    if self.backend.observe():
                        raise RuntimeError("old backend not confirmed stopped")
                commit("STARTING", "closed")
                self.backend.start(goal)
                return state
            except Exception as error:
                state.update(phase="ERROR", gate="closed", error=type(error).__name__)
                self._write(state)
                return state

    def process_inbox(self, now_ms: int | None = None) -> int:
        """Apply admin requests from the site, oldest first; each one gets exactly one result file.

        Returns the number of results written. Busy controller lock leaves the request for the next pass.
        """
        if not self.inbox_dir or not self.admin_dir:
            return 0
        now_ms = int(time.time() * 1000) if now_ms is None else now_ms
        results = self.admin_dir / "results"
        try:
            # Separate lock: a concurrent manual step must not apply the same file twice.
            with self._locked("inbox.lock"):
                candidates = []
                for name in os.listdir(self.inbox_dir):
                    if not INBOX_NAME.fullmatch(name):
                        continue
                    if (results / name).exists():
                        _remove(self.inbox_dir / name)
                        continue
                    try:
                        candidates.append((os.lstat(self.inbox_dir / name).st_mtime_ns, name))
                    except FileNotFoundError:
                        pass
                # The site renames complete files, so mtime approximates createdAt without reading every file.
                pending = []
                for _, name in sorted(candidates)[:INBOX_BATCH]:
                    document, error = self._load_request(name)
                    created = document.get("createdAt") if document else None
                    pending.append((created if type(created) is int else 0, name, document, error))
                pending.sort(key=lambda item: item[:2])
                done = 0
                for _, name, document, error in pending:
                    request_id = name[:-len(".json")]
                    actor, revision = "?", None
                    try:
                        if error:
                            raise ValueError(error)
                        actor = member({**document.get("actor"), "role": "builder"})["name"] \
                            if isinstance(document.get("actor"), dict) else None
                        if actor is None:
                            raise ValueError("invalid actor")
                        if document.get("id") != request_id:
                            raise ValueError("id does not match file name")
                        # An operation already accepted (e.g. recovery step was busy) is finished, not expired.
                        if (type(document.get("createdAt")) is not int or now_ms - document["createdAt"] > INBOX_TTL_MS) \
                                and self.status().get("operation") != request_id:
                            raise ValueError("request expired")
                        revision = self._apply(request_id, document)
                    except BlockingIOError:
                        break
                    except Exception as failure:
                        error, revision = str(failure) or type(failure).__name__, None
                    else:
                        error = None
                    result = {"id": request_id, "ok": error is None, "error": error, "revision": revision, "doneAt": now_ms}
                    atomic_write(results / name, json.dumps(result, ensure_ascii=False, sort_keys=True).encode(), 0o644)
                    _remove(self.inbox_dir / name)
                    done += 1
                    kind = document.get("kind") if document else None
                    print(f"inbox {request_id} kind={kind!r} actor={actor} " + ("ok" if error is None else f"error: {error}"),
                          file=sys.stderr, flush=True)
                self._prune_results(results)
        except (BlockingIOError, FileNotFoundError):
            return 0
        return done

    @staticmethod
    def _prune_results(results: Path) -> None:
        kept = []
        for path in results.glob("*.json"):
            try:
                kept.append((path.stat().st_mtime_ns, path.name, path))
            except FileNotFoundError:
                pass
        for _, _, old in sorted(kept, reverse=True)[RESULTS_KEPT:]:
            old.unlink(missing_ok=True)

    def _load_request(self, name: str) -> tuple[dict | None, str | None]:
        try:
            fd = os.open(self.inbox_dir / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except OSError:
            return None, "not a regular file"
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            return None, "not a regular file"
        with os.fdopen(fd, "rb") as handle:
            data = handle.read(INBOX_MAX_BYTES + 1)
        if len(data) > INBOX_MAX_BYTES:
            return None, "request too large"
        try:
            document = json.loads(data)
        except Exception:  # includes RecursionError from deeply nested input
            return None, "invalid json"
        if not isinstance(document, dict) or type(document.get("schema")) is not int or document["schema"] != 1:
            return None, "unsupported schema"
        return document, None

    def _apply(self, request_id: str, document: dict) -> int | None:
        kind = document.get("kind")
        if kind == "members":
            entries = document.get("members")
            if not isinstance(entries, list):
                raise ValueError("members must be a list")
            if not any(member(entry)["role"] == "admin" for entry in entries):
                raise ValueError("members must keep at least one admin")
            self.set_members(entries)
            return None
        if kind == "message":
            self.set_message(document.get("message"))
            return None
        if kind != "mode":
            raise ValueError("unknown kind")
        profile, expected, recover = document.get("profile"), document.get("expectedRevision"), document.get("recover", False)
        if profile not in {"game", "workshop"} or type(expected) is not int or type(recover) is not bool:
            raise ValueError("invalid mode request")
        if recover and profile != "game":
            raise ValueError("recover is only allowed toward game")
        state = self.status()
        if state.get("operation") == request_id:
            goal = Goal(**state["desired"])
        else:
            release = (state.get("desired") or {}).get("release")
            if not release:
                raise ValueError("controller is not initialised")
            goal = Goal("workshop", release, str(uuid4())) if profile == "workshop" else Goal("game", release)
        state = self.request(request_id, goal, expected, recover=recover)
        if recover:
            state = self.step(recover=True)
            if state["phase"] == "ERROR":
                raise RuntimeError(f"recovery failed: {state.get('error')}")
        return state["revision"]


def _remove(path: Path) -> None:
    try:
        path.unlink()
    except IsADirectoryError:
        try:
            path.rmdir()
        except OSError:
            pass
    except FileNotFoundError:
        pass
