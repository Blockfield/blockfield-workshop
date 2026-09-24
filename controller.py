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
import secrets
import stat
import sys
from pathlib import Path
import tempfile
import time
from typing import Callable, Protocol
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
    def drain_progress(self) -> dict | None:
        """None when there is nothing to drain (matches the old drained()==True fast path).

        Otherwise a dict with "drained" (bool) plus whatever `bf maintenance status` reports:
        "rooms", "ranked", "pending" (int) and "healthy" (bool), used to size the drain deadline
        and to short-circuit the "nothing left but stats unhealthy" case.
        """
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
MAP_IDS = frozenset({
    "ancient", "anubis", "baxmut", "berdichev", "dm_arena", "dust2", "fields", "firing_range", "inferno",
    "konstaxa", "kozyatyn", "mirage", "nuke", "overpass", "snipers_towers", "train", "vertigo", "zaliznichne",
})
# Access scopes a builder's "maps" may name: every map plus the workshop hub itself (blockfield-mod WorkshopMembers.HUB).
SCOPE_IDS = MAP_IDS | {"hub"}
NAME = re.compile(r"[A-Za-z0-9_]{3,16}")
INBOX_NAME = re.compile(r"[0-9a-f-]{36}\.json")
INBOX_MAX_BYTES = 65536
INBOX_BATCH = 20
INBOX_TTL_MS = 10 * 60 * 1000
RESULTS_KEPT = 200


WORKSHOP_KEY_GID = 1000  # the site and the workshop server both run as uid/gid 1000 and mount admin/ read-only


def ensure_workshop_key(path: Path) -> None:
    """Shared secret for the workshop tablet -> site write (blockfield-web /api/workshop/access).

    Created once (never rotated here), 0640 root:1000 so only the two containers can read it.
    """
    if path.exists() or path.is_symlink():
        return
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o640)
    try:
        os.write(fd, (secrets.token_hex(32) + "\n").encode())
        try:
            os.fchown(fd, -1, WORKSHOP_KEY_GID)
        except PermissionError:
            pass  # tests and non-root runs: the file stays with the caller's group
        os.fchmod(fd, 0o640)
    finally:
        os.close(fd)
    os.replace(temporary, path)


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
        unknown = set(maps) - SCOPE_IDS
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
                 admin_dir: Path | None = None, inbox_dir: Path | None = None,
                 drain_timeout: float = 600.0, drain_timeout_ranked: float = 3600.0,
                 drain_health_grace: float = 60.0, start_timeout: float = 600.0,
                 now: Callable[[], float] | None = None, maps=None):
        """drain_timeout/drain_timeout_ranked: DRAINING deadline (seconds) with no/some ranked campaigns
        left; drain_health_grace: extra short-circuit when rooms=ranked=pending=0 but stats are still
        unhealthy (the 2026-09-18 incident) -- don't hold players hostage to that flag. start_timeout:
        STARTING deadline. Any deadline exceeded rolls back to the previous stable profile automatically.
        """
        # candidates.MapStore: workshop map snapshots, site review and publishing (inbox kinds below); optional.
        self.maps = maps
        self.drain_timeout, self.drain_timeout_ranked = drain_timeout, drain_timeout_ranked
        self.drain_health_grace, self.start_timeout = drain_health_grace, start_timeout
        self._now = now or time.time
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.directory.is_symlink():
            raise ValueError("state directory must not be a symlink")
        self.file = self.directory / "state.json"
        self.members_file = self.directory / "members.json"
        self.gate_file = Path(gate_file) if gate_file else self.directory / "gate.json"
        self.public_file = Path(public_file) if public_file else self.directory / "public.json"
        self.message_file = self.directory / "message.json"
        self.session_file = self.directory / "session.json"
        self.default_message = message.strip()[:500] or DEFAULT_MESSAGE
        self.admin_dir = Path(admin_dir) if admin_dir else None
        self.inbox_dir = Path(inbox_dir) if inbox_dir else None
        if self.admin_dir:
            (self.admin_dir / "results").mkdir(parents=True, exist_ok=True)
            for path in (self.admin_dir, self.admin_dir / "results"):
                os.chmod(path, 0o755)
            ensure_workshop_key(self.admin_dir / "workshop.key")
        self.backend = backend

    def _now_ms(self) -> int:
        return int(self._now() * 1000)

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
        # Additive fields: absent on a state.json written before this fix, so default rather than reject.
        state.setdefault("since", 0)
        state.setdefault("errorMessage", None)
        state.setdefault("rollback", None)
        if type(state["since"]) is not int or not (state["errorMessage"] is None or isinstance(state["errorMessage"], str)):
            raise ValueError("invalid state timing/error fields")
        if state["rollback"] is not None:
            try:
                Goal(**state["rollback"])
            except (ValueError, TypeError, AttributeError) as error:
                raise ValueError("invalid rollback goal") from error
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

    def session(self) -> dict | None:
        """The last workshop session (id, maps offered in it, who started it); None before the first one."""
        try:
            value = json.loads(self.session_file.read_bytes()[:65536])
            return value if isinstance(value, dict) and value.get("schema") == 1 else None
        except (OSError, ValueError):
            return None

    def _session_for(self, document: dict, actor: dict) -> dict:
        """New or resumed session for a mode->workshop request; maps: map ids offered in the workshop, None = all."""
        choice, previous = document.get("session", "new"), self.session()
        if choice not in {"new", "resume"}:
            raise ValueError("session must be new or resume")
        maps = document.get("maps") if "maps" in document else (previous or {}).get("maps") if choice == "resume" else None
        if maps is not None:
            if not isinstance(maps, list) or not maps or any(not isinstance(m, str) for m in maps) or len(set(maps)) != len(maps):
                raise ValueError("maps must be a non-empty list of distinct map ids or null")
            if set(maps) - MAP_IDS:
                raise ValueError(f"unknown map id: {sorted(set(maps) - MAP_IDS)}")
        if choice == "resume" and previous:
            return {**previous, "maps": maps}
        return {"schema": 1, "id": str(uuid4()), "maps": maps, "startedAt": self._now_ms(), "startedBy": actor}

    def _export(self, state: dict, *, deadline: int | None = None) -> None:
        """Proxy gate (private ACL) and public site status. Rewritten only when content changes.

        deadline is passed in by the caller for the ticks where it is live (DRAINING/STARTING wait
        loops); it is derived from "since" rather than persisted, so it is never stale on disk.
        """
        gate = state["gate"] if state["gate"] in {"public", "draining", "builders", "testers"} else "closed"
        profile = (state.get("desired") or {}).get("profile", "game")
        # The playtest (profile test) runs in the workshop app for the same members.
        members_gate = gate in {"builders", "testers"}
        backend = "workshop" if members_gate or (gate == "closed" and profile in {"workshop", "test"}) else "game"
        gate_doc = {"schema": 1, "revision": state["revision"], "gate": gate, "backend": backend,
                    "message": "" if gate == "public" else self.message,
                    "allowed": self.members() if members_gate else []}
        self._replace_if_changed(self.gate_file, gate_doc, keep=())
        mode = {"public": "game", "draining": "draining", "builders": "workshop", "testers": "workshop"}.get(gate, "maintenance")
        public = {"mode": mode, "message": "" if mode == "game" else self.message, "since": int(time.time() * 1000)}
        self._replace_if_changed(self.public_file, public, keep=("since",))
        if self.admin_dir:
            self._replace_if_changed(self.admin_dir / "members.json", self.members(), keep=())
            session = self.session()
            if session is not None:
                self._replace_if_changed(self.admin_dir / "session.json", session, keep=())
            status = {"schema": 1, "revision": state["revision"], "phase": state["phase"], "gate": state["gate"],
                      "error": state.get("error"), "errorMessage": state.get("errorMessage"),
                      "since": state.get("since"), "deadline": deadline,
                      "desired": state.get("desired"), "message": self.message,
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
            # The stable phase we're leaving IS the running goal (phase==goal.profile is an invariant of
            # _read()); remember it now so a later DRAINING/STARTING timeout can roll back to it without
            # relying on backend.observe() still agreeing once things go wrong.
            previous_stable = state.get("desired") if state.get("phase") in {"game", "workshop", "test"} else None
            state = {"schema": 1, "revision": revision, "phase": "ERROR" if recovering else "REQUESTED", "gate": "closed",
                     "operation": operation, "desired": asdict(goal), "error": state.get("error") if recovering else None,
                     "errorMessage": state.get("errorMessage") if recovering else None,
                     "since": self._now_ms(), "rollback": None if recovering else previous_stable}
            self._write(state)
            return state

    def step(self, *, recover: bool = False) -> dict:
        """One reconciliation under a process lock. Retry STARTING/DRAINING, don't start a worker.

        ERROR requires explicit recovery. STARTING with no visible process waits closed:
        absence cannot distinguish a failed request from an accepted, delayed start.
        Explicit recovery fences pending starts before considering another start.
        A repeated step never counts as forced interruption. State is written only when
        phase/gate/error change, so a stable or draining gate never flaps through closed.

        DRAINING and STARTING each carry a deadline (see __init__); past it, step() rolls back to
        the profile recorded in state["rollback"] instead of waiting or requiring root recovery.
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

            def commit(phase: str, gate: str, error: str | None = None, error_message: str | None = None) -> None:
                if (state["phase"], state["gate"], state.get("error"), state.get("errorMessage")) != (phase, gate, error, error_message):
                    if state["phase"] != phase:
                        state["since"] = self._now_ms()
                        if phase in {"game", "workshop", "test"}:
                            state["rollback"] = None
                    state.update(phase=phase, gate=gate, error=error, errorMessage=error_message)
                    self._write(state)

            try:
                self.backend.preflight(goal)
                if recover and previous_phase in {"STARTING", "ERROR"}:
                    commit(previous_phase, "closed", state.get("error"), state.get("errorMessage"))
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
                        self._export(state)
                        return state
                    commit("STARTING", "closed")
                    return self._starting_wait(state, goal, unexpected=None)
                if previous_phase == "STARTING" and not recover:
                    # Either nothing is visible yet (intent was durable before start(); never reissue it
                    # automatically, including after a controller crash or delayed process visibility) or
                    # current is an unexpected backend -- e.g. the 2026-09-18 incident, where an external
                    # restart resurrected another backend mid-start. Either way: wait for the deadline,
                    # fencing off anything unexpected instead of erroring out and requiring root recovery.
                    return self._starting_wait(state, goal, unexpected=current)
                if current is not None:
                    if current.goal.profile == "game":
                        commit("DRAINING", "draining")
                        self.backend.request_drain()
                        progress = self.backend.drain_progress()
                        if progress is not None and not progress.get("drained", False):
                            verdict = self._drain_wait(state, progress)
                            if verdict is not None:
                                return self._rollback(state, goal, verdict)
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
                state.update(phase="ERROR", gate="closed", error=type(error).__name__, errorMessage=str(error) or repr(error))
                self._write(state)
                return state

    def _starting_wait(self, state: dict, goal: Goal, unexpected: Process | None) -> dict:
        elapsed_ms = self._now_ms() - state["since"]
        if elapsed_ms > self.start_timeout * 1000:
            detail = "unexpected backend present" if unexpected is not None else "no ready backend appeared"
            return self._rollback(state, goal, f"start of {goal.profile} timed out after {elapsed_ms // 1000}s ({detail})")
        if unexpected is not None:
            self.backend.fence_pending_starts()
        self._export(state, deadline=state["since"] + int(self.start_timeout * 1000))
        return state

    def _drain_wait(self, state: dict, progress: dict) -> str | None:
        """None to keep waiting (also exports the live deadline); else the rollback reason."""
        now_ms, since = self._now_ms(), state["since"]
        nothing_left = not progress.get("rooms") and not progress.get("ranked") and not progress.get("pending")
        if nothing_left and progress.get("healthy") is False:
            if now_ms - since > self.drain_health_grace * 1000:
                return (f"drain: nothing left to drain but stats persistence stayed unhealthy for "
                        f"{(now_ms - since) // 1000}s; rolling back rather than risk losing results ({progress})")
            deadline = since + int(self.drain_health_grace * 1000)
        else:
            timeout = self.drain_timeout_ranked if progress.get("ranked") else self.drain_timeout
            if now_ms - since > timeout * 1000:
                return f"drain timed out after {(now_ms - since) // 1000}s: {progress}"
            deadline = since + int(timeout * 1000)
        self._export(state, deadline=deadline)
        return None

    def _rollback(self, state: dict, goal: Goal, reason: str) -> dict:
        """Abandon the in-flight switch and reopen the gate of the previously stable profile.

        DRAINING never touched the old backend, so reverting is just switching "desired" back.
        STARTING already stopped it, so bring it back up before declaring the profile stable again;
        if that itself fails there is no safe automatic move left, so it becomes an ERROR (root recovery).
        """
        stable = Goal(**state["rollback"])
        if state["phase"] == "STARTING":
            try:
                self.backend.stop(Process(goal, False))
                self.backend.start(stable)
            except Exception as error:
                state.update(phase="ERROR", gate="closed", error=type(error).__name__,
                              errorMessage=f"{reason}; rollback to {stable.profile} also failed: {error}")
                self._write(state)
                return state
            state.update(phase="STARTING", gate="closed", desired=asdict(stable), error=None,
                          errorMessage=reason, since=self._now_ms(), rollback=None)
        else:
            state.update(phase=stable.profile, gate=OPEN_GATES[stable.profile], desired=asdict(stable),
                          error=None, errorMessage=reason, since=self._now_ms(), rollback=None)
        self._write(state)
        return state

    def cancel(self) -> dict:
        """Unprivileged abort of a stuck switch (site -> inbox "cancel", no root): roll back now
        instead of waiting for the deadline. Only valid while a switch is actually in flight."""
        with self._locked():
            state = self._read()
            if state.get("phase") not in {"DRAINING", "STARTING"}:
                raise ValueError("nothing to cancel: no switch is in progress")
            if not state.get("rollback"):
                raise ValueError("no previous stable profile recorded to roll back to")
            return self._rollback(state, Goal(**state["desired"]), "cancelled by admin request")

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

    def export_maps(self) -> None:
        """admin/candidates.json for the site: candidate list with status, backups per map."""
        if self.maps is None or not self.admin_dir:
            return
        for map_id in self.maps.retire_merged_overlays():
            print(f"maps: published {map_id}.json now equals the repo copy, overlay retired", file=sys.stderr, flush=True)
        self._replace_if_changed(self.admin_dir / "candidates.json", self.maps.export(), keep=())

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
        if kind == "cancel":
            # Unprivileged recovery: an admin cancels a stuck DRAINING/STARTING switch without root.
            return self.cancel()["revision"]
        if kind in {"candidate", "publish", "reject", "rollback"}:
            force = document.get("force", False)
            if type(force) is not bool:
                raise ValueError("force must be a boolean")
            if self.maps is None:
                raise ValueError("map publishing is not configured")
            actor = {"draslId": document["actor"]["draslId"], "name": document["actor"]["name"]}
            if kind == "candidate":
                self.maps.create(document.get("mapId"), actor, document.get("note", ""))
            elif kind == "publish":
                self.maps.publish(document.get("candidateId"), actor, force=force)
            elif kind == "reject":
                self.maps.reject(document.get("candidateId"), actor)
            else:
                self.maps.rollback(document.get("mapId"), actor)
            self.export_maps()
            return None
        if kind != "mode":
            raise ValueError("unknown kind")
        profile, expected, recover = document.get("profile"), document.get("expectedRevision"), document.get("recover", False)
        if profile not in {"game", "workshop", "test"} or type(expected) is not int or type(recover) is not bool:
            raise ValueError("invalid mode request")
        if recover and profile != "game":
            raise ValueError("recover is only allowed toward game")
        state, session = self.status(), None
        if state.get("operation") == request_id:
            goal = Goal(**state["desired"])
        else:
            release = (state.get("desired") or {}).get("release")
            if not release:
                raise ValueError("controller is not initialised")
            if profile == "workshop":
                actor = {"draslId": document["actor"]["draslId"], "name": document["actor"]["name"]}
                session = self._session_for(document, actor)
                goal = Goal("workshop", release, session["id"])
            elif profile == "test":
                # A closed playtest of one candidate, started from the running workshop and returning to it.
                if state.get("phase") != "workshop":
                    raise ValueError("a test starts only from the running workshop")
                if self.maps is None:
                    raise ValueError("map publishing is not configured")
                goal = Goal("test", release, state["desired"]["session"], self.maps.sha256_of(document.get("candidateId")))
            else:
                goal = Goal("game", release)
        state = self.request(request_id, goal, expected, recover=recover)
        if session is not None:
            with self._locked():
                atomic_write(self.session_file, json.dumps(session, ensure_ascii=False, sort_keys=True).encode())
                self._export(self._read())
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
