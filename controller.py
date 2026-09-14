"""Durable single-backend transition core. The Docker adapter lives in docker_backend.py.

A trusted adapter must fence all other starters and expose actual process readiness,
not an optimistic HTTP response. Proxy/web consumers must fail closed on absent state.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
import fcntl
import json
import os
import re
import stat
from pathlib import Path
import tempfile
import time
from typing import Protocol
from uuid import UUID


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
NAME = re.compile(r"[A-Za-z0-9_]{3,16}")


def member(value) -> dict:
    """Validated ACL entry; the proxy matches both the Drasl profile id and the current name."""
    if not isinstance(value, dict) or set(value) - {"draslId", "name", "role"}:
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
    return {"draslId": drasl, "name": name, "role": role}


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
                 public_file: Path | None = None, message: str = DEFAULT_MESSAGE):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.directory.is_symlink():
            raise ValueError("state directory must not be a symlink")
        self.file = self.directory / "state.json"
        self.members_file = self.directory / "members.json"
        self.gate_file = Path(gate_file) if gate_file else self.directory / "gate.json"
        self.public_file = Path(public_file) if public_file else self.directory / "public.json"
        self.message = message.strip()[:500] or DEFAULT_MESSAGE
        self.backend = backend

    @contextmanager
    def _locked(self):
        flags = os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW
        fd = os.open(self.directory / "controller.lock", flags, 0o600)
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

    @staticmethod
    def _replace_if_changed(path: Path, document: dict, keep: tuple) -> None:
        try:
            previous = json.loads(path.read_bytes()[:65536])
            if isinstance(previous, dict) and {k: v for k, v in previous.items() if k not in keep} == \
                    {k: v for k, v in document.items() if k not in keep}:
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
