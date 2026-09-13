"""Durable single-backend transition core. No production/Docker adapter is installed.

A trusted adapter must fence all other starters and expose actual process readiness,
not an optimistic HTTP response. Proxy/web consumers must fail closed on absent state.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
import fcntl
import json
import os
import stat
from pathlib import Path
import tempfile
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


class Controller:
    def __init__(self, directory: Path, backend: Backend):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.directory.is_symlink():
            raise ValueError("state directory must not be a symlink")
        self.file = self.directory / "state.json"
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
        data = json.dumps(state, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()
        fd, temporary = tempfile.mkstemp(prefix=".state-", dir=self.directory)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.file)
            directory_fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def request(self, operation: str, goal: Goal, expected_revision: int) -> dict:
        """Persist intent only. No side effect on a running backend until step is called."""
        UUID(operation)
        with self._locked():
            state = self._read()
            if state.get("operation") == operation:
                if state.get("desired") != asdict(goal):
                    raise ValueError("operation ID reused with different intent")
                return state
            if type(expected_revision) is not int or expected_revision != state["revision"]:
                raise ValueError("stale state revision")
            if state.get("phase") not in {"UNINITIALIZED", "game", "workshop", "test"}:
                raise RuntimeError("operation in progress or requires recovery")
            self.backend.preflight(goal)
            revision = state["revision"] + 1
            state = {"schema": 1, "revision": revision, "phase": "REQUESTED", "gate": "closed", "operation": operation,
                     "desired": asdict(goal), "error": None}
            self._write(state)
            return state

    def step(self, *, recover: bool = False) -> dict:
        """One reconciliation under a process lock. Retry STARTING/DRAINING, don't start a worker.

        ERROR requires explicit recovery. STARTING with no visible process waits closed:
        absence cannot distinguish a failed request from an accepted, delayed start.
        Explicit recovery fences pending starts before considering another start.
        A repeated step never counts as forced interruption.
        """
        with self._locked():
            state = self._read()
            if state.get("desired") is None:
                raise RuntimeError("no requested operation")
            if state.get("phase") == "ERROR" and not recover:
                return state
            previous_phase = state["phase"]
            goal = Goal(**state["desired"])
            state.update(gate="closed", error=None)
            self._write(state)
            try:
                self.backend.preflight(goal)
                if recover and previous_phase in {"STARTING", "ERROR"}:
                    self.backend.fence_pending_starts()
                processes = self.backend.observe()
                if len(processes) > 1:
                    raise RuntimeError("multiple full backends observed")
                current = processes[0] if processes else None
                if current is not None and current.goal == goal:
                    state["phase"] = goal.profile if current.ready else "STARTING"
                    state["gate"] = ({"game": "public", "workshop": "builders", "test": "testers"}[goal.profile]
                                     if current.ready else "closed")
                    self._write(state)
                    return state
                if previous_phase == "STARTING" and not recover:
                    if current is not None:
                        raise RuntimeError("unexpected backend while start is unresolved")
                    # Intent was durable before start(). Never reissue it automatically,
                    # including after a controller crash or delayed process visibility.
                    return state
                if current is not None:
                    if current.goal.profile == "game":
                        state.update(phase="DRAINING", gate="draining")
                        self._write(state)
                        self.backend.request_drain()
                        if not self.backend.drained():
                            return state
                    state.update(phase="SAVING", gate="closed")
                    self._write(state)
                    self.backend.save_and_freeze(current)
                    self.backend.stop(current)
                    if self.backend.observe():
                        raise RuntimeError("old backend not confirmed stopped")
                state.update(phase="STARTING", gate="closed")
                self._write(state)
                self.backend.start(goal)
                return state
            except Exception as error:
                state.update(phase="ERROR", gate="closed", error=type(error).__name__)
                self._write(state)
                return state
