"""Durable Workshop/test lifecycle beside a passively observed Game backend.

A trusted adapter must fence Workshop starters and expose actual process readiness,
not an optimistic HTTP response. Proxy/web consumers must fail closed on absent state.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace, asdict, dataclass
import fcntl
import json
import hashlib
import shutil
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

import desired as desired_state
import map_package
import releases as release_catalog

STATE_MAX_BYTES = 262144


@dataclass(frozen=True)
class Goal:
    profile: str
    release: str
    session: str | None = None
    candidate: str | None = None
    modeId: str | None = None

    def __post_init__(self):
        if self.profile not in {"game", "workshop", "test"}:
            raise ValueError("unknown profile")
        if self.modeId is not None and (
            self.profile != "test" or self.modeId not in map_package.SUPPORTED_MODES
        ):
            raise ValueError("test mode must be a supported game mode")
        if (
            not isinstance(self.release, str)
            or not self.release
            or len(self.release) > 128
        ):
            raise ValueError("missing pinned release")
        if self.profile == "game":
            if self.session is not None or self.candidate is not None:
                raise ValueError("game must not run a draft/candidate")
        else:
            if self.session is None:
                raise ValueError("session required")
            if (
                not isinstance(self.session, str)
                or str(UUID(self.session)) != self.session
            ):
                raise ValueError("canonical session UUID required")
            if self.profile == "test":
                if (
                    not isinstance(self.candidate, str)
                    or len(self.candidate) != 64
                    or any(c not in "0123456789abcdef" for c in self.candidate)
                ):
                    raise ValueError("immutable candidate SHA256 required")
            elif self.candidate is not None:
                raise ValueError("workshop is not a candidate playtest")


@dataclass(frozen=True)
class Process:
    goal: Goal
    ready: bool


def _release_matches(release: str, record: dict) -> bool:
    """Whether a goal release string names a verified record: its tag or its image digest."""
    return (
        release == record["release"]
        or release == record["imageDigest"].split("@", 1)[1]
    )


class Backend(Protocol):
    def preflight(self, goal: Goal) -> None:
        """Check space, separate data, pinned profile and fencing of restart/deploy paths."""

    def observe(self) -> list[Process]:
        """Return EVERY full backend, including unready/stopping processes."""

    def save_and_freeze(self, process: Process) -> None:
        """Finish tools, save world+config and stop new writes; raise on uncertainty."""

    def stop(self, process: Process) -> None:
        """Graceful stop; success is rechecked by observe before any new start."""

    def fence_pending_starts(self) -> None:
        """Cancel/settle previously accepted Workshop starts before explicit recovery.

        Returning must guarantee no delayed old request can later start a backend.
        Raise if that cannot be established. An empty observe() alone is not fencing.
        """

    def start(self, goal: Goal) -> None:
        """Start the selected Workshop/test backend; never mutate Game."""


DEFAULT_MESSAGE = (
    "На сервере проводятся технические работы. Обычный вход временно недоступен."
)
OPEN_GATES = {"game": "public", "workshop": "builders", "test": "testers"}
ROLES = {"builder", "lead", "admin"}
# server/lobby/blockfield/maps/*.json ids (blockfield-mod); the controller only validates, it does not read the mod's data.
MAP_IDS = frozenset(json.loads(Path(__file__).with_name("map_ids.json").read_text()))
# Access scopes a builder's "maps" may name: every map plus the workshop hub itself (blockfield-mod WorkshopMembers.HUB).
SCOPE_IDS = MAP_IDS | {"hub"}
NAME = re.compile(r"[A-Za-z0-9_]{3,16}")
INBOX_NAME = re.compile(r"[0-9a-f-]{36}\.json")
INBOX_MAX_BYTES = 65536
INBOX_MERGE_MAX_BYTES = 8 << 20
INBOX_BATCH = 20
INBOX_TTL_MS = 10 * 60 * 1000
CP_RELEASE_LIMIT = 256
CP_COVERED_INBOX_KINDS = frozenset({"members", "message", "wake", "cancel"})
CP_INBOX_RETIRED_ERROR = "inbox retired: control-plane is the mutation ingress"
RESULTS_KEPT = 200
# Cold Workshop (unreleased section 2): the always-on editor sleeps after 15 min
# with no builders and no active work, and wakes on demand through the proxy/site.
ACTIVITY_FILE = ".blockfield-activity.json"
ACTIVITY_MAX_BYTES = 16384
ACTIVITY_FRESH_MS = 120 * 1000
SLEEP_MESSAGE = "Мастерская спит — запустите её"
# Inbox kinds that need a running Workshop today; refused while sleeping.
# candidate/commit work asleep too: sleep already saved and stopped the editor.
LIVE_ONLY_KINDS = {
    "limbo-export",
    "map-create",
    "map-properties",
}
# Workshop AI key from the site inbox: <workshop data>/blockfield-workshop/openrouter.key,
# 0600 1000:1000. The value is write-only: it never appears in logs, results or exports.
AI_KEY_PATTERN = re.compile(r"[\x21-\x7e]{20,2048}")
AI_KEY_UID = 1000
AI_KEY_GID = 1000
# Provider registry ids (contract with the site): presets keep their historic key
# file and status names; a custom id uses <id>.key and <id>-key.json. The shape
# excludes dots and slashes, so a custom file name can never escape its directory.
AI_PROVIDER_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
AI_PROVIDER_KEY_PRESETS = {
    "openrouter": ("openrouter.key", "ai-key.json", "ai-key"),
    "vercel-gateway": ("gateway.key", "ai-gateway-key.json", "ai-gateway-key"),
}
# Custom ids that would reuse a preset key file or status name stay reserved,
# so a custom key can never overwrite (or satisfy) another provider's key.
AI_PROVIDER_RESERVED_IDS = frozenset(
    {
        *AI_PROVIDER_KEY_PRESETS,
        *(
            filename.removesuffix(".key")
            for filename, _, _ in AI_PROVIDER_KEY_PRESETS.values()
        ),
        *(
            status.removesuffix("-key.json")
            for _, status, _ in AI_PROVIDER_KEY_PRESETS.values()
            if status.endswith("-key.json")
        ),
    }
)


def ai_provider_key_names(provider_id: str) -> tuple[str, str, str]:
    """Key file name, admin status name and inbox kind for any registry provider id."""
    if provider_id in AI_PROVIDER_KEY_PRESETS:
        return AI_PROVIDER_KEY_PRESETS[provider_id]
    return (f"{provider_id}.key", f"{provider_id}-key.json", "ai-provider-key")


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
    except BaseException:
        temporary.unlink(missing_ok=True)  # never leave a partial secret behind
        raise


def member(value, *, scopes=None) -> dict:
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
        unknown = set(maps) - (SCOPE_IDS if scopes is None else scopes)
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


def atomic_write(
    path: Path, data: bytes, mode: int = 0o600, owner: tuple[int, int] | None = None
) -> None:
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + "-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            if owner is not None:
                os.fchown(handle.fileno(), *owner)
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


#: Marker for a gate record file that exists but cannot be read or validated.
#: Pending reads return it (fail closed: hold ACKs, let the next stage heal);
#: only a missing file reads as absent.
_UNREADABLE_GATE_RECORD = object()


class CommitIncompleteError(OSError):
    """A durable commit (state.json, members.json, message.json) is live but
    a later derived write failed. An OSError subclass: callers that do not
    know this class treat it as the local I/O failure it is. Callers that do
    (sync, commit sites) must not report the op rejected — the commit stands
    and the retry must be safe: commit sites ERROR-drive or are naturally
    idempotent, so a retry lands on a vacuous path instead of re-applying a
    non-idempotent op."""


class Controller:
    def __init__(
        self,
        directory: Path,
        backend: Backend,
        *,
        gate_file: Path | None = None,
        public_file: Path | None = None,
        message: str = DEFAULT_MESSAGE,
        admin_dir: Path | None = None,
        inbox_dir: Path | None = None,
        start_timeout: float = 600.0,
        idle_sleep_s: float = 900.0,
        now: Callable[[], float] | None = None,
        maps=None,
        workshop_data_dir: Path | None = None,
        releases: dict | None = None,
        releases_path: Path | None = None,
        target_file: Path | None = None,
        desired_file: Path | None = None,
        inbox_retired: bool = False,
    ):
        """A missed STARTING deadline restores a previous Workshop/test goal when available."""
        # candidates.MapStore: workshop map snapshots, site review and publishing (inbox kinds below); optional.
        self.maps = maps
        self.start_timeout = start_timeout
        # Cold Workshop idle sleep: seconds of continuous idleness before the stable
        # editor sleeps; 0 disables sleeping entirely.
        self.idle_sleep_s = idle_sleep_s
        self._now = now or time.time
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.directory.is_symlink():
            raise ValueError("state directory must not be a symlink")
        self.file = self.directory / "state.json"
        self.members_file = self.directory / "members.json"
        self.gate_file = Path(gate_file) if gate_file else self.directory / "gate.json"
        self.public_file = (
            Path(public_file) if public_file else self.directory / "public.json"
        )
        self.message_file = self.directory / "message.json"
        self.session_file = self.directory / "session.json"
        # Last approved Workshop target: a verified published immutable server
        # release record. It survives with Game off; staging never reads live Game.
        self.target_file = (
            Path(target_file) if target_file else self.directory / "target.json"
        )
        # Controller-owned desired state (deploy tail schema 2): the approved
        # Workshop release plus its revision/operation binding. The host
        # game-deploy file keeps its schema-1 shape for its own readers.
        self.desired_file = (
            Path(desired_file) if desired_file else self.directory / "desired.json"
        )
        self.releases = dict(releases) if releases else {}
        # Hot catalog reload for the long-running loop: entrypoint builds the
        # controller once, but releases.json gains records on every release.
        self.releases_path = Path(releases_path) if releases_path is not None else None
        self._releases_fingerprint = self._fingerprint_releases()
        self.default_message = message.strip()[:500] or DEFAULT_MESSAGE
        self.admin_dir = Path(admin_dir) if admin_dir else None
        self.inbox_dir = Path(inbox_dir) if inbox_dir else None
        # S6b switch: CP-covered inbox kinds are denied closed (map ops stay).
        self.inbox_retired = bool(inbox_retired)
        self.workshop_data_dir = (
            Path(workshop_data_dir) if workshop_data_dir is not None else None
        )
        if self.admin_dir:
            (self.admin_dir / "results").mkdir(parents=True, exist_ok=True)
            for path in (self.admin_dir, self.admin_dir / "results"):
                os.chmod(path, 0o755)
            ensure_workshop_key(self.admin_dir / "workshop.key")
        self.backend = backend
        # Gate-report delivery state (unit CPP3-R4): files are the only
        # truth. gate_pending.json = {seq, leg} desired-but-undelivered;
        # gate_delivered.json = {seq, leg} last confirmed on CP. No
        # process-local caches: every read-modify-write of both files runs
        # under gate_state.lock (local file ops only, never HTTPS).
        # gate_report.lock serializes delivery across processes.
        self._gate_pending_file = self.directory / "gate_pending.json"
        self._gate_delivered_file = self.directory / "gate_delivered.json"
        self._gate_state_lock_file = self.directory / "gate_state.lock"
        self._gate_report_lock_file = self.directory / "gate_report.lock"

    def _now_ms(self) -> int:
        return int(self._now() * 1000)

    def _fingerprint_releases(self):
        """(mtime_ns, size) of the catalog file, or None when it is absent."""
        if self.releases_path is None:
            return None
        try:
            info = os.stat(self.releases_path)
        except OSError:
            return None
        if not stat.S_ISREG(info.st_mode):
            return None
        return (info.st_mtime_ns, info.st_size)

    def maybe_reload_releases(self) -> None:
        """Hot-reload releases.json when it changed; validated exactly as at startup.

        An invalid new catalog keeps the previous one and logs to stderr, so a
        half-written or untrusted export can never revoke a committed target.
        """
        if self.releases_path is None:
            return
        fingerprint = self._fingerprint_releases()
        if fingerprint == self._releases_fingerprint:
            return
        if fingerprint is None:
            if self._releases_fingerprint is not None:
                print(
                    "controller: releases catalog missing; keeping previous releases",
                    file=sys.stderr,
                )
            self._releases_fingerprint = None
            return
        try:
            fresh = release_catalog.load_catalog(self.releases_path)
        except ValueError as error:
            print(
                f"controller: keeping previous releases catalog: {error}",
                file=sys.stderr,
            )
            self._releases_fingerprint = fingerprint
            return
        self.releases = fresh
        if hasattr(self.backend, "releases"):
            self.backend.releases = dict(fresh)
        self._releases_fingerprint = fingerprint

    @contextmanager
    def _locked(self, name: str = "controller.lock"):
        flags = os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW
        fd = os.open(self.directory / name, flags, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield
        finally:
            os.close(fd)
        # After the file lock is released: deliver any staged gate leg. The
        # report is blocking HTTPS and must never run under the lock, or a
        # stalled CP stalls request/cancel/step with BUSY.
        try:
            self._flush_gate_report()
        except Exception as error:
            print(f"controller: gate flush failed: {error!r}", file=sys.stderr)

    def _read(self) -> dict:
        # Every state observation sees catalog updates: the long-running loop
        # builds the controller once, but releases.json gains records on release.
        self.maybe_reload_releases()
        # Check links before existence: a dangling link is corruption, not fresh state.
        if self.file.is_symlink():
            raise ValueError("invalid state file")
        try:
            fd = os.open(self.file, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return {
                "schema": 1,
                "phase": "UNINITIALIZED",
                "gate": "closed",
                "operation": None,
                "desired": None,
                "revision": 0,
                "cancelOperation": None,
            }
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > STATE_MAX_BYTES:
                raise ValueError("invalid state file")
            data = handle.read(STATE_MAX_BYTES + 1)
        if len(data) > STATE_MAX_BYTES:
            raise ValueError("invalid state file")

        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate state key")
                result[key] = value
            return result

        state = json.loads(data, object_pairs_hook=unique)
        if (
            not isinstance(state, dict)
            or type(state.get("schema")) is not int
            or state["schema"] != 1
        ):
            raise ValueError("unsupported state schema")
        if type(state.get("revision")) is not int or state["revision"] < 0:
            raise ValueError("invalid state revision")
        gates = {
            "UNINITIALIZED": "closed",
            "REQUESTED": "closed",
            "DRAINING": "draining",
            "SAVING": "closed",
            "STARTING": "closed",
            "ERROR": "closed",
            "sleeping": "closed",
            "game": "public",
            "workshop": "builders",
            "test": "testers",
        }
        phase = state.get("phase")
        if (
            not isinstance(phase, str)
            or phase not in gates
            or state.get("gate") not in ("closed", gates[phase])
        ):
            raise ValueError("invalid state phase/gate")
        if phase == "UNINITIALIZED":
            if (
                state["revision"] != 0
                or state.get("desired") is not None
                or state.get("operation") is not None
            ):
                raise ValueError("invalid uninitialized state")
            if any(
                state.get(key) is not None
                for key in ("approvedTarget", "activeTarget", "selection")
            ):
                raise ValueError("uninitialized state has a selection")
            return state
        if (
            state["revision"] < 1
            or not isinstance(state.get("desired"), dict)
            or not isinstance(state.get("operation"), str)
        ):
            raise ValueError("invalid operation state")
        try:
            UUID(state["operation"])
            goal = Goal(**state["desired"])
        except (ValueError, TypeError, AttributeError) as error:
            raise ValueError("invalid operation intent") from error
        state["desired"] = asdict(goal)
        if phase in {"game", "workshop", "test"} and phase != goal.profile:
            raise ValueError("stable phase does not match desired profile")
        if phase == "sleeping" and goal.profile != "workshop":
            raise ValueError("sleeping keeps a workshop goal")
        # Additive fields: absent on a state.json written before this fix, so default rather than reject.
        state.setdefault("since", 0)
        state.setdefault("errorMessage", None)
        state.setdefault("rollback", None)
        # Op id of the cancel that journaled this state (None when the journal
        # is not cancel-owned): a same-op retry is a no-op, never a re-apply.
        # A new switch (_request_locked) builds a fresh dict without this key.
        state.setdefault("cancelOperation", None)
        # Cold Workshop idle timer: epoch ms of continuous idleness, None while active.
        state.setdefault("idleSince", None)
        if type(state["since"]) is not int or not (
            state["errorMessage"] is None or isinstance(state["errorMessage"], str)
        ):
            raise ValueError("invalid state timing/error fields")
        if state["idleSince"] is not None and type(state["idleSince"]) is not int:
            raise ValueError("invalid state idle timer")
        if state["rollback"] is not None:
            try:
                state["rollback"] = asdict(Goal(**state["rollback"]))
            except (ValueError, TypeError, AttributeError) as error:
                raise ValueError("invalid rollback goal") from error
        follow = state.get("releaseFollow")
        if follow is not None:
            try:
                previous = Goal(**follow["previous"])
                digest = follow["sessionDigest"]
                if (
                    set(follow) != {"previous", "sessionDigest"}
                    or phase not in {"REQUESTED", "SAVING", "STARTING", "ERROR"}
                    or previous.profile != "workshop"
                    or replace(previous, release=goal.release) != goal
                    or previous.release == goal.release
                    or not isinstance(digest, str)
                    or re.fullmatch(r"[0-9a-f]{64}", digest) is None
                ):
                    raise ValueError("inconsistent release follow")
            except (ValueError, TypeError, KeyError) as error:
                raise ValueError("invalid release follow") from error
        self._validate_selection(state)
        return state

    def _validate_selection(self, state: dict) -> None:
        if state.get("approvedTarget") is None:
            if (
                state.get("activeTarget") is not None
                or state.get("selection") is not None
            ):
                raise ValueError("missing approved target in selection journal")
            return
        for key in (
            "approvedTarget",
            "activeTarget",
            "rollbackTarget",
            "previousTarget",
        ):
            if state.get(key) is not None:
                state[key] = release_catalog.validate_release_record(state[key])
        selection = state.get("selection")
        try:
            UUID(selection["operation"])
            intent = Goal(**selection["intent"])
            Goal(**selection["request"])
            if (
                type(selection["revision"]) is not int
                or not 1 <= selection["revision"] <= state["revision"]
                or type(selection["deferred"]) is not bool
                or type(selection["recover"]) is not bool
                or type(selection["resumeOnly"]) is not bool
                or not (
                    selection["requestSession"] is None
                    or isinstance(selection["requestSession"], dict)
                )
                or not _release_matches(intent.release, state["approvedTarget"])
                or not _release_matches(
                    state["desired"]["release"], state["activeTarget"]
                )
                or (
                    not selection["deferred"]
                    and (state["activeTarget"] != state["approvedTarget"])
                )
                or (
                    state.get("rollback") is not None
                    and (
                        state.get("rollbackTarget") is None
                        or not _release_matches(
                            state["rollback"]["release"], state["rollbackTarget"]
                        )
                    )
                )
            ):
                raise ValueError("inconsistent selection journal")
        except (KeyError, TypeError, AttributeError, ValueError) as error:
            raise ValueError("invalid selection journal") from error

    def _project_target(self, state: dict) -> dict | None:
        record = state.get("approvedTarget")
        if record is None:
            # A standalone file is not a committed approval or an initial import.
            if release_catalog.load_approved_target(self.target_file) is not None:
                raise ValueError(
                    "target has no committed selection journal; import via request"
                )
            return None
        target = {
            **record,
            "stateRevision": state["revision"],
            "operation": state["selection"]["operation"],
            "runtimeOperation": state["operation"],
            "selectionRevision": state["selection"]["revision"],
            "previousTarget": state.get("previousTarget"),
        }
        # state.json is the authority. Repair an interrupted/stale/corrupt projection
        # under the controller lock before any external effect or target read.
        try:
            projected = release_catalog.load_approved_target(self.target_file)
        except ValueError:
            projected = None
        if projected != target:
            atomic_write(
                self.target_file,
                json.dumps(target, sort_keys=True, allow_nan=False).encode(),
            )
        return target

    def _project_desired(self, state: dict) -> dict | None:
        document = desired_state.build_from_state(state)
        if document is None:
            # A standalone file is not a committed selection or an initial
            # import; like target.json, it is adopted only via request.
            if desired_state.read_desired_state(self.desired_file) is not None:
                raise ValueError(
                    "desired state has no committed selection; import via request"
                )
            return None
        return desired_state.project_desired_state(self.desired_file, document)

    def _write(self, state: dict, *, processes: list[Process] | None = None) -> None:
        self._validate_selection(state)
        data = json.dumps(
            state, ensure_ascii=False, sort_keys=True, allow_nan=False
        ).encode()
        if len(data) > STATE_MAX_BYTES:
            raise ValueError("controller state exceeds its bounded reader")
        atomic_write(self.file, data)
        # Commit point: state.json above is durable. Every failure below is
        # commit-then-fail: never a rejection (the commit stands), never a
        # silent ghost. Local-write failures already forced the local gate
        # closed best effort inside _export; ERROR-driving is the op sites'
        # job (_rollback, set_members, set_message), which own their state.
        try:
            self._project_target(state)
            self._project_desired(state)
            # processes reuses the step's observation: every export of a step shares
            # one observation, so a nested export can never be newer than the step's
            # trailing export (stale data must not overwrite fresh gate decisions).
            # Omit it whenever the backend may have changed since the observation.
            self._export(state, processes=processes)
        except CommitIncompleteError:
            raise
        except OSError as error:
            raise CommitIncompleteError(
                f"committed state r{state.get('revision')} but a derived write"
                f" failed: {error}"
            ) from error

    def members(self) -> list[dict]:
        """Missing or invalid ACL admits nobody."""
        try:
            return [
                member(entry, scopes=self._map_ids() | {"hub"})
                for entry in json.loads(self.members_file.read_bytes()[:65536])
            ]
        except (OSError, ValueError, TypeError):
            return []

    def set_members(self, entries: list) -> list[dict]:
        members = [member(entry, scopes=self._map_ids() | {"hub"}) for entry in entries]
        if len({m["draslId"] for m in members}) != len(members):
            raise ValueError("duplicate member")
        with self._locked():
            atomic_write(
                self.members_file, json.dumps(members, sort_keys=True).encode()
            )
            try:
                state = self._read()
            except Exception as read_error:
                # state.json unreadable or invalid: the members commit above
                # is live but the desired leg is unknown. Fail closed: narrow
                # the local gate first (a removed member must stop being
                # admitted even before the closed leg is delivered), then
                # stage a closed leg so the pending marker holds any
                # effective ACK until it is delivered. A staging failure is
                # commit-then-fail (hold and converge on retry, never
                # rejected or effective).
                self._narrow_unreadable_gate_best_effort()
                try:
                    self._stage_closed_gate_report()
                except OSError as stage_error:
                    raise CommitIncompleteError(
                        "members commit is live but no leg could be staged: "
                        f"{stage_error}"
                    ) from stage_error
                print(
                    f"controller: members export failed after commit ({read_error!r});"
                    " staged closed gate leg",
                    file=sys.stderr,
                )
                return members
            try:
                self._export(state)
            except CommitIncompleteError:
                raise
            except OSError as error:
                # The members commit above is live even if a derived export
                # fails. A staging failure leaves this op unstaged (tracked
                # per op, not inferred from any older pending record): hold,
                # never effective, and converge on retry (members re-apply is
                # idempotent). A local-write failure never narrowed the gate:
                # the closed best effort already ran, so park ERROR visibly.
                if (
                    getattr(self, "gate_reporter", None) is not None
                    and getattr(self, "_export_staged_gate_seq", None) is None
                ):
                    if getattr(self, "_export_local_failed", False):
                        self._drive_error_after_commit(state, error)
                    raise CommitIncompleteError(
                        f"members commit is live but its gate leg was not"
                        f" staged: {error}"
                    ) from error
                print(
                    f"controller: members export failed after commit: {error!r}",
                    file=sys.stderr,
                )
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
                atomic_write(
                    self.message_file,
                    json.dumps({"message": value}, ensure_ascii=False).encode(),
                )
            else:
                self.message_file.unlink(missing_ok=True)
            # Commit point: the message file above is live. An export failure
            # is commit-then-fail (message re-apply is idempotent, so the op
            # holds and converges on retry, never rejected or effective); a
            # local-write failure parks ERROR visibly.
            try:
                state = self._read()
            except Exception as read_error:
                # The message commit above is live but the desired leg is
                # unknown: hold and converge on retry, never rejected.
                raise CommitIncompleteError(
                    f"message commit is live but state could not be read: {read_error}"
                ) from read_error
            try:
                self._export(state)
            except CommitIncompleteError:
                raise
            except OSError as error:
                if (
                    getattr(self, "gate_reporter", None) is not None
                    and getattr(self, "_export_staged_gate_seq", None) is None
                ):
                    if getattr(self, "_export_local_failed", False):
                        self._drive_error_after_commit(state, error)
                    raise CommitIncompleteError(
                        "message commit is live but its gate leg was not"
                        f" staged: {error}"
                    ) from error
                raise
        return self.message

    def status(self) -> dict:
        return self._read()

    def session(self) -> dict | None:
        """The last workshop session (id, maps offered in it, who started it); None before the first one."""
        try:
            value = json.loads(self.session_file.read_bytes()[:65536])
            return (
                value if isinstance(value, dict) and value.get("schema") == 1 else None
            )
        except (OSError, ValueError):
            return None

    def approved_target(self) -> dict | None:
        """Validated revision-bound projection of the durable selection journal."""
        with self._locked():
            return self._project_target(self._read())

    def desired_state(self) -> dict | None:
        """Validated owned desired-state document; repairs the projection."""
        with self._locked():
            return self._project_desired(self._read())

    def _select_release(self, release: str, state: dict) -> dict:
        if self.releases and release in self.releases:
            record = release_catalog.resolve(self.releases, release)
            for key in ("approvedTarget", "activeTarget", "previousTarget"):
                known = state.get(key)
                if (
                    known is not None
                    and _release_matches(release, known)
                    and known != record
                ):
                    raise ValueError("published release identity changed")
            return record
        # Catalog loss permits only the exact already committed identity.
        target = state.get("approvedTarget")
        if target is not None and _release_matches(release, target):
            return release_catalog.validate_release_record(target)
        raise ValueError(f"release {release!r} is not a verified published release")

    def _map_ids(self) -> set[str]:
        return self.maps.map_ids() if self.maps is not None else MAP_IDS

    def _session_for(self, document: dict, actor: dict) -> dict:
        """New or resumed session for a mode->workshop request; maps: map ids offered in the workshop, None = all."""
        choice, previous = document.get("session", "new"), self.session()
        if choice not in {"new", "resume"}:
            raise ValueError("session must be new or resume")
        if "sessionId" in document:
            if (
                choice != "resume"
                or "maps" in document
                or "branches" in document
                or getattr(self.maps, "canonical_sessions", False) is not True
            ):
                raise ValueError(
                    "archived resume requires sessionId without map/branch overrides"
                )
            archived = self.maps.session(document["sessionId"])
            if archived is None:
                raise ValueError("archived session not found")
            return archived
        maps = (
            document.get("maps")
            if "maps" in document
            else (previous or {}).get("maps")
            if choice == "resume"
            else None
        )
        if maps is not None:
            if (
                not isinstance(maps, list)
                or not maps
                or any(not isinstance(m, str) for m in maps)
                or len(set(maps)) != len(maps)
            ):
                raise ValueError(
                    "maps must be a non-empty list of distinct map ids or null"
                )
            if set(maps) - self._map_ids():
                raise ValueError(
                    f"unknown map id: {sorted(set(maps) - self._map_ids())}"
                )
        result = (
            {**previous, "maps": maps}
            if choice == "resume" and previous
            else {
                "schema": 1,
                "id": str(uuid4()),
                "maps": maps,
                "startedAt": self._now_ms(),
                "startedBy": actor,
            }
        )
        if "branches" in document:
            if getattr(self.maps, "canonical_sessions", False) is not True:
                raise ValueError("named branches require canonical sessions")
            result["branches"] = document["branches"]
        elif isinstance(maps, list) and "branches" in result:
            result["branches"] = {
                map_id: result["branches"].get(map_id, "work-" + result["id"])
                for map_id in maps
            }
        return result

    def _save_session(self, session: dict) -> None:
        atomic_write(
            self.session_file,
            json.dumps(session, ensure_ascii=False, sort_keys=True).encode(),
        )
        if self.admin_dir is not None:
            self._replace_if_changed(self.admin_dir / "session.json", session, keep=())

    def _session_selection(self, goal: Goal, session: dict | None = None) -> dict:
        session = session if session is not None else self.session()
        if (
            not isinstance(session, dict)
            or type(session.get("schema")) is not int
            or session["schema"] != 1
            or session.get("id") != goal.session
        ):
            raise ValueError("workshop session does not match selected profile")
        previous = self.session()
        resuming = previous and previous.get("id") == goal.session
        maps = session.get("maps")
        if maps is None:
            maps = previous.get("maps") if resuming else None
            if maps is None:
                maps = sorted(self._map_ids())
        if (
            not isinstance(maps, list)
            or not maps
            or any(not isinstance(map_id, str) for map_id in maps)
            or len(set(maps)) != len(maps)
            or set(maps) - self._map_ids()
        ):
            raise ValueError("invalid selected session maps")
        from candidates import MapVersions

        branches = session.get(
            "branches",
            previous.get("branches")
            if resuming and "branches" in previous
            else {map_id: "work-" + goal.session for map_id in maps},
        )
        if not isinstance(branches, dict) or set(branches) != set(maps):
            raise ValueError("session branches must match selected maps")
        for name in branches.values():
            if MapVersions._branch_name(name) == "main":
                raise ValueError("main is protected; select a work branch")
        if resuming:
            prior = previous.get(
                "branches",
                {
                    map_id: "work-" + goal.session
                    for map_id in previous.get("maps") or maps
                },
            )
            if not isinstance(prior, dict) or any(
                map_id in prior and prior[map_id] != name
                for map_id, name in branches.items()
            ):
                raise ValueError("switching branches requires a new isolated session")
        return {**session, "maps": maps, "branches": branches}

    def _selected_session(
        self, goal: Goal, session: dict | None = None, *, resume_only=False
    ) -> dict:
        selected = self._session_selection(goal, session)
        self.maps.prepare_session(
            goal.session,
            selected["maps"],
            resume_only=resume_only,
            branches=selected["branches"],
            actor=selected.get("startedBy") or {"draslId": "host", "name": "host"},
        )
        return selected

    def _editor_goal(self, release: str, state: dict) -> tuple[Goal, dict]:
        session = self.session()
        if session is None:
            if (state.get("desired") or {}).get("profile") in {"workshop", "test"}:
                raise ValueError("selected workshop session is missing or invalid")
            session = {
                "schema": 1,
                "id": (state.get("desired") or {}).get("session") or str(uuid4()),
                "maps": None,
                "startedAt": self._now_ms(),
                "startedBy": None,
            }
        return Goal("workshop", release, session["id"]), session

    def _session_digest(self) -> str | None:
        session = self.session()
        if session is None:
            return None
        data = json.dumps(session, sort_keys=True, ensure_ascii=False, allow_nan=False)
        return hashlib.sha256(data.encode()).hexdigest()

    def _workshop_data_base(self) -> Path | None:
        """Host Workshop app data dir (holds the mod activity file); None when unknown."""
        if self.workshop_data_dir is not None:
            return Path(self.workshop_data_dir)
        apps = getattr(self.backend, "apps", None)
        if apps is not None and "workshop" in apps:
            return Path(apps["workshop"].data)
        return None

    def _read_activity(self) -> dict | None:
        """Latest mod activity report; None means not provably idle (fail-safe = awake).

        The file lives in the Workshop app data dir on the host. Any problem --
        missing, symlink, non-regular, oversized, unreadable, invalid JSON or schema
        -- reads as activity, so a broken reporter never lets the editor sleep.
        """
        base = self._workshop_data_base()
        if base is None:
            return None
        path = base / ACTIVITY_FILE
        if path.is_symlink():
            return None
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except OSError:
            return None
        with os.fdopen(fd, "rb") as handle:
            try:
                info = os.fstat(handle.fileno())
            except OSError:
                return None
            if not stat.S_ISREG(info.st_mode) or info.st_size > ACTIVITY_MAX_BYTES:
                return None
            data = handle.read(ACTIVITY_MAX_BYTES + 1)
        if len(data) > ACTIVITY_MAX_BYTES:
            return None
        try:
            document = json.loads(data)
        except ValueError:
            return None
        if (
            not isinstance(document, dict)
            or document.get("schema") != 1
            or not isinstance(document.get("generation"), str)
            or not document["generation"]
            or type(document.get("players")) is not int
            or type(document.get("busy")) is not bool
            or type(document.get("updatedAt")) is not int
        ):
            return None
        return document

    def _readiness_generation(self) -> str | None:
        """Current Workshop readiness generation; None when unknown (fail-safe = awake)."""
        probe = getattr(self.backend, "readiness_generation", None)
        if probe is None:
            return None
        try:
            generation = probe("workshop")
        except Exception:
            return None
        return generation if isinstance(generation, str) and generation else None

    def _inbox_has_pending(self) -> bool:
        """True unless the inbox provably holds no unanswered request."""
        if not self.inbox_dir or not self.admin_dir:
            return False
        results = self.admin_dir / "results"
        try:
            names = os.listdir(self.inbox_dir)
        except OSError:
            return True
        return any(
            INBOX_NAME.fullmatch(name) and not (results / name).exists()
            for name in names
        )

    def _activity_idle(
        self, state: dict, *, processes: list[Process] | None = None
    ) -> bool:
        """Fresh idle activity report; any activity clears the idle timer."""
        document = self._read_activity()
        generation = self._readiness_generation()
        now = self._now_ms()
        idle = (
            document is not None
            and generation is not None
            and document["generation"] == generation
            and 0 <= now - document["updatedAt"] <= ACTIVITY_FRESH_MS
            and document["players"] == 0
            and document["busy"] is False
        )
        if not idle and state.get("idleSince") is not None:
            state["idleSince"] = None
            self._write(state, processes=processes)
        return idle

    def _observe(self) -> tuple[list[Process], Process | None, Process | None]:
        return self._split(self.backend.observe())

    def _split(
        self, processes: list[Process]
    ) -> tuple[list[Process], Process | None, Process | None]:
        game = [p for p in processes if p.goal.profile == "game"]
        workshop = [p for p in processes if p.goal.profile in {"workshop", "test"}]
        if len(game) > 1 or len(workshop) > 1:
            raise RuntimeError("multiple full backends of the same app observed")
        return processes, game[0] if game else None, workshop[0] if workshop else None

    def _last_game_entry(self) -> tuple | None:
        """Game entry of the current gate/public exports; None when missing/invalid."""
        try:
            game_gate = json.loads(self.gate_file.read_bytes()[:65536])["backends"][
                "game"
            ]
            public = json.loads(self.public_file.read_bytes()[:65536])
            if (
                not isinstance(game_gate, dict)
                or game_gate.get("gate") not in {"public", "closed"}
                or public.get("mode") not in {"game", "maintenance"}
            ):
                return None
            return (
                {
                    "gate": game_gate["gate"],
                    "message": game_gate.get("message", ""),
                    "allowed": game_gate.get("allowed", []),
                },
                public["mode"],
            )
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return None

    def _export(
        self,
        state: dict,
        *,
        deadline: int | None = None,
        processes: list[Process] | None = None,
    ) -> None:
        """Proxy gate (private ACL) and public site status. Rewritten only when content changes.

        deadline is passed in by the caller for the ticks where it is live (STARTING wait
        loops); it is derived from "since" rather than persisted, so it is never stale on disk.
        processes reuses one step's observation so a step probes the backend only once;
        omit it whenever the backend may have changed since the observation.
        """
        # This export's staging outcome for set_members (None = not staged):
        # reset here so a stale seq can never mask a failed write. The
        # local-failure flag tells post-commit handlers whether the local
        # gate narrowed (staging-only failure: hold and converge) or not
        # (local write failure: ERROR-drive, never silently effective).
        self._export_staged_gate_seq = None
        self._export_local_failed = False
        gate = (
            state["gate"]
            if state["gate"] in {"public", "draining", "builders", "testers"}
            else "closed"
        )
        game_status = {"phase": "UNAVAILABLE", "gate": "closed", "release": None}
        game_observed = False
        try:
            if processes is None:
                _, game, workshop = self._observe()
            else:
                _, game, workshop = self._split(processes)
            if game is not None:
                game_observed = True
                game_status = {
                    "phase": "game" if game.ready else "STARTING",
                    "gate": "public" if game.ready else "closed",
                    "release": game.goal.release,
                }
            if (
                workshop is None
                or not workshop.ready
                or asdict(workshop.goal) != state.get("desired")
            ):
                gate = "closed"
        except Exception:
            gate = "closed"
        members_gate = gate in {"builders", "testers"}
        sleeping = state.get("phase") == "sleeping"
        starting = (
            state.get("phase") in {"REQUESTED", "SAVING", "STARTING"}
            and (state.get("desired") or {}).get("profile") == "workshop"
        )
        if sleeping or starting:
            # The proxy and the site render this: the editor is parked at 0 replicas
            # with its goal intact, not closed for maintenance. starting keeps members
            # in the lobby with the waking message instead of the closed one.
            workshop_gate = {
                "gate": "sleeping" if sleeping else "starting",
                "message": self.message,
                "allowed": self.members(),
            }
        else:
            workshop_gate = {
                "gate": gate if members_gate else "closed",
                "message": self.message,
                "allowed": self.members() if members_gate else [],
            }
        # Local gate first: gate.json/public.json narrow (or close) before any
        # pending staging or CP report. A staging failure below must never
        # leave the wider local gate behind it.
        game_gate = {
            "gate": game_status["gate"],
            "message": "" if game_status["gate"] == "public" else self.default_message,
            "allowed": [],
        }
        mode = "game" if game_status["gate"] == "public" else "maintenance"
        if not game_observed:
            # Game independence: never close Game entry on the basis of not
            # seeing Game (container backends do not observe the host Game).
            # Absence preserves the last export; only a missing or invalid
            # export fails closed.
            preserved = self._last_game_entry()
            if preserved is not None:
                game_gate, mode = preserved
                if game_gate["gate"] == "public":
                    # The site admin reads the status game entry, not the
                    # gate: repeat the preserved public verdict there too.
                    # Release is unknown to an unobserving controller (null,
                    # accepted by the site readiness check); direct liveness
                    # stays cross-checked by the site rooms query.
                    game_status = {
                        "phase": "game",
                        "gate": "public",
                        "release": None,
                    }
        gate_doc = {
            "schema": 1,
            "revision": state["revision"],
            **game_gate,
            "backend": "game",
            "backends": {"game": game_gate, "workshop": workshop_gate},
        }
        public = {
            "mode": mode,
            "message": "" if mode == "game" else self.default_message,
            "since": int(time.time() * 1000),
            "workshop": {
                "mode": "sleeping"
                if sleeping
                else "workshop"
                if members_gate
                else "maintenance",
                "message": self.message,
            },
        }
        try:
            self._replace_if_changed(self.gate_file, gate_doc, keep=())
            self._replace_if_changed(self.public_file, public, keep=("since",))
            if self.admin_dir:
                self._replace_if_changed(
                    self.admin_dir / "members.json", self.members(), keep=()
                )
                session = self.session()
                if session is not None:
                    self._replace_if_changed(
                        self.admin_dir / "session.json", session, keep=()
                    )
                status = {
                    "schema": 1,
                    "revision": state["revision"],
                    "operation": state.get("operation"),
                    "phase": state["phase"],
                    "gate": workshop_gate["gate"],
                    "error": state.get("error"),
                    "errorMessage": state.get("errorMessage"),
                    "since": state.get("since"),
                    "deadline": deadline,
                    "desired": state.get("desired"),
                    "selection": {
                        key: state["selection"][key]
                        for key in ("operation", "revision", "deferred")
                    }
                    if state.get("selection")
                    else None,
                    "game": game_status,
                    "message": self.message,
                    "updatedAt": int(time.time() * 1000),
                }
                self._replace_if_changed(
                    self.admin_dir / "status.json", status, keep=("updatedAt",)
                )
        except OSError:
            # The local gate never narrows: force it closed best effort (the
            # Game entry is preserved) so a narrowing change never waits on
            # a dead disk, then let post-commit handlers ERROR-drive + raise.
            self._export_local_failed = True
            self._write_closed_gate_best_effort(gate_doc, public)
            raise
        # Stage last, after every local derivative narrowed: a staging
        # failure raises with the local gate already narrowed, so the op is
        # held (never effective) but never leaves the wider gate behind.
        # Delivery itself happens only via the flush, after the lock.
        self._stage_gate_report(workshop_gate)

    def _write_closed_gate_best_effort(self, gate_doc: dict, public: dict) -> None:
        """Narrow the local gate after a failed export write. Best effort:
        never raises. Preserves the Game entry (only the workshop backend is
        forced closed) so a transient export failure cannot close the Game
        site; the proxy denies until the next export heals."""
        try:
            backends = gate_doc.get("backends", {})
            closed_gate_doc = {
                **gate_doc,
                "backends": {
                    **backends,
                    "workshop": {
                        **backends.get("workshop", {}),
                        "gate": "closed",
                        "allowed": [],
                    },
                },
            }
            self._replace_if_changed(self.gate_file, closed_gate_doc, keep=())
        except Exception:
            pass
        try:
            closed_public = {
                **public,
                "workshop": {"mode": "maintenance", "message": self.message},
            }
            self._replace_if_changed(self.public_file, closed_public, keep=("since",))
        except Exception:
            pass

    def _narrow_unreadable_gate_best_effort(self) -> None:
        """Narrow the on-disk gate for the unreadable-state path. Best
        effort: never raises. Forces the workshop entry closed (Game entry
        preserved) so a just-removed member stops being admitted locally."""
        try:
            gate_doc = json.loads(self.gate_file.read_bytes()[:65536])
        except Exception:
            gate_doc = None
        try:
            public = json.loads(self.public_file.read_bytes()[:65536])
        except Exception:
            public = None
        if not isinstance(gate_doc, dict) or not isinstance(public, dict):
            return
        self._write_closed_gate_best_effort(gate_doc, public)

    def _drive_error_after_commit(self, state: dict, error: Exception) -> None:
        """Persist ERROR after a post-commit derivative failure. Best effort:
        never raises. Writes state.json directly: routing through _write
        would re-export (recursion) and the derivatives just failed."""
        try:
            state.update(
                phase="ERROR",
                gate="closed",
                error=type(error).__name__,
                errorMessage=str(error)[:500],
                since=self._now_ms(),
            )
            data = json.dumps(
                state, ensure_ascii=False, sort_keys=True, allow_nan=False
            ).encode()
            if len(data) > STATE_MAX_BYTES:
                return
            atomic_write(self.file, data)
        except Exception:
            pass

    @staticmethod
    def _project_gate_leg(workshop_gate: dict) -> dict:
        """CP-projected leg (state/message/plain members, no roles or maps)."""
        return {
            "state": workshop_gate["gate"],
            "message": workshop_gate["message"],
            "allowed": [
                {"draslId": entry["draslId"], "name": entry["name"]}
                for entry in workshop_gate["allowed"]
            ],
        }

    def _stage_gate_report(self, workshop_gate: dict) -> None:
        """Stage the desired leg under the lock; delivery happens after unlock.

        Runs inside _export (under controller.lock) before any other file
        write. Raises OSError when the marker cannot be written: _export
        fails and the op must not be ACKed effective. No in-memory fallback.
        """
        if getattr(self, "gate_reporter", None) is None:
            return
        self._stage_gate_leg(self._project_gate_leg(workshop_gate))

    def _stage_closed_gate_report(self) -> None:
        """Fail-closed staging when state.json cannot be read: the desired
        leg is unknown, so hold the most restrictive (closed, nobody
        admitted) leg until delivery. Raises like _stage_gate_report."""
        if getattr(self, "gate_reporter", None) is None:
            return
        self._stage_gate_leg(
            {"state": "closed", "message": self.message, "allowed": []}
        )

    def _stage_gate_leg(self, leg: dict) -> None:
        """Write pending {seq, nonce, leg}; skip only when CP holds the leg.

        Caller holds controller.lock (staging); gate_state.lock guards the
        read-modify-write of both files. The seq is one past the highest
        readable seq; when the previous pending record is unreadable its seq
        is unknown, so the bump may reuse a seq still in flight — the fresh
        nonce keeps the record identity unique anyway, and the drain unlinks
        only on an exact (seq, nonce) match, so the reused number can never
        delete the newer record. A stage of the already-staged leg is a
        no-op (lets duplicate requests re-export without churning seqs).
        Records the covering seq in self._export_staged_gate_seq so set_members
        can tell this op's staging from an older pending record.
        """
        with self._gate_state_locked():
            delivered = self._read_gate_record_locked(self._gate_delivered_file)
            pending = self._read_gate_record_locked(
                self._gate_pending_file, fail_closed=True
            )
            if pending is None and delivered is not None and delivered[2] == leg:
                self._export_staged_gate_seq = delivered[0]
                return
            if (
                pending is not None
                and pending is not _UNREADABLE_GATE_RECORD
                and pending[2] == leg
            ):
                self._export_staged_gate_seq = pending[0]
                return
            if pending is _UNREADABLE_GATE_RECORD:
                # Unreadable seq: bump past delivered only. Delivery advances
                # delivered solely on exact-identity sends, so the reused
                # number resolves via the fresh nonce below, never by unlink.
                seq = (delivered[0] if delivered is not None else 0) + 1
            else:
                seq = (
                    max(
                        [0]
                        + [
                            record[0]
                            for record in (delivered, pending)
                            if record is not None
                        ]
                    )
                    + 1
                )
            nonce = uuid4().hex
            atomic_write(
                self._gate_pending_file,
                json.dumps(
                    {"seq": seq, "nonce": nonce, "leg": leg}, ensure_ascii=False
                ).encode(),
            )
            self._export_staged_gate_seq = seq

    @contextmanager
    def _gate_state_locked(self):
        """Serialize local gate-file ops across processes (never HTTPS)."""
        fd = os.open(
            self._gate_state_lock_file,
            os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
            0o600,
        )
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    @staticmethod
    def _valid_gate_leg(leg) -> bool:
        return (
            isinstance(leg, dict)
            and type(leg.get("state")) is str
            and type(leg.get("message")) is str
            and isinstance(leg.get("allowed"), list)
            and all(
                isinstance(entry, dict)
                and type(entry.get("draslId")) is str
                and type(entry.get("name")) is str
                for entry in leg["allowed"]
            )
        )

    def _read_gate_record_locked(
        self, path: Path, *, fail_closed: bool = False
    ) -> tuple[int, str, dict] | object | None:
        """(seq, nonce, leg) from a gate record file; None only when absent.

        The (seq, nonce) pair is the record identity: every stage mints a
        fresh nonce, and the drain unlinks only on an exact identity match,
        so a seq reused after an unreadable read can never delete the newer
        record. A nonce-less (pre-upgrade) record is invalid: pending reads
        return _UNREADABLE_GATE_RECORD (fail closed, healed by next stage).

        The caller holds gate_state.lock. With fail_closed=False (the
        delivered file) an unreadable or invalid record is unlinked so the
        next stage heals it, and None is returned. With fail_closed=True
        invalid record returns _UNREADABLE_GATE_RECORD instead of None and
        is never unlinked: a leg that cannot be read must keep holding ACKs,
        and the next stage overwrites it. Delivered-identity reads also use
        fail_closed=True: a pure read must never mutate the file.
        """
        try:
            document = json.loads(path.read_bytes()[:65536])
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            if not fail_closed:
                return None
            return _UNREADABLE_GATE_RECORD
        if (
            not isinstance(document, dict)
            or type(document.get("seq")) is not int
            or document["seq"] < 1
            or type(document.get("nonce")) is not str
            or not document["nonce"]
            or not self._valid_gate_leg(document.get("leg"))
        ):
            if not fail_closed:
                try:
                    path.unlink()
                except OSError:
                    pass
                return None
            return _UNREADABLE_GATE_RECORD
        leg = document["leg"]
        return (
            document["seq"],
            document["nonce"],
            {
                "state": leg["state"],
                "message": leg["message"],
                "allowed": [
                    {"draslId": entry["draslId"], "name": entry["name"]}
                    for entry in leg["allowed"]
                ],
            },
        )

    def gate_report_pending(self) -> bool:
        """True while a desired leg is staged but not yet delivered to CP.

        Always reads the file, even without a reporter: a process that cannot
        deliver must still hold while another process's leg is undelivered.
        An unreadable record counts as pending (fail closed); only a missing
        file reads as clear.
        """
        with self._gate_state_locked():
            return (
                self._read_gate_record_locked(self._gate_pending_file, fail_closed=True)
                is not None
            )

    def gate_pending_identity(self) -> dict | None:
        """{seq, nonce} of the staged-but-undelivered leg; None when absent
        or unreadable. A pure read: never mutates the files."""
        with self._gate_state_locked():
            record = self._read_gate_record_locked(
                self._gate_pending_file, fail_closed=True
            )
            if record is None or record is _UNREADABLE_GATE_RECORD:
                return None
            return {"seq": record[0], "nonce": record[1]}

    def gate_delivered_identity(self) -> dict | None:
        """{seq, nonce} of the last leg confirmed on CP; None when absent or
        unreadable. A pure read: never mutates the files."""
        with self._gate_state_locked():
            record = self._read_gate_record_locked(
                self._gate_delivered_file, fail_closed=True
            )
            if record is None or record is _UNREADABLE_GATE_RECORD:
                return None
            return {"seq": record[0], "nonce": record[1]}

    def _flush_gate_report(self) -> None:
        """Deliver the staged leg. Callers must not hold the controller lock.

        Serialized across processes by gate_report.lock (never the
        controller lock): one report in flight at a time. A busy lock skips
        instead of waiting, so a stalled report never stalls request/step;
        the pending marker is retried by the next flush. Files are the only
        truth: the pending record is re-read under gate_state.lock, and a
        stage landing mid-report is delivered by the drain loop last, so the
        server converges on the newest seq.
        """
        try:
            fd = os.open(
                self._gate_report_lock_file,
                os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
                0o600,
            )
        except OSError as error:
            print(f"controller: gate flush failed: {error!r}", file=sys.stderr)
            return
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                # Another process is delivering; the marker stays for retry.
                return
            try:
                self._flush_gate_report_locked()
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def _flush_gate_report_locked(self) -> None:
        """One drain pass; the caller holds gate_report.lock."""
        while True:
            with self._gate_state_locked():
                record = self._read_gate_record_locked(
                    self._gate_pending_file, fail_closed=True
                )
                if record is None:
                    return
                if record is _UNREADABLE_GATE_RECORD:
                    # Staged but unparseable: keep it (the next stage
                    # overwrites it) and stop; never deliver what was not read.
                    return
                seq, nonce, leg = record
            reporter = getattr(self, "gate_reporter", None)
            if reporter is None:
                # Nothing can be delivered from here: keep the marker for a
                # process holding the gate credential. Never unlink another
                # owner's leg, never treat it as delivered.
                return
            try:
                reporter.report(leg)
            except Exception as error:
                print(f"controller: gate report failed: {error!r}", file=sys.stderr)
                return
            with self._gate_state_locked():
                atomic_write(
                    self._gate_delivered_file,
                    json.dumps(
                        {"seq": seq, "nonce": nonce, "leg": leg}, ensure_ascii=False
                    ).encode(),
                )
                current = self._read_gate_record_locked(
                    self._gate_pending_file, fail_closed=True
                )
                if current is None or current is _UNREADABLE_GATE_RECORD:
                    # Gone (impossible under the report lock) or unreadable
                    # mid-flight: stay fail-closed, the next stage heals it.
                    return
                if current[0] == seq and current[1] == nonce:
                    # Still the exact record just sent: delivered, drop it. A
                    # newer stage landing mid-report has a different identity:
                    # loop and deliver it last so CP converges on it.
                    try:
                        self._gate_pending_file.unlink()
                    except OSError:
                        pass
                    return

    @staticmethod
    def _replace_if_changed(path: Path, document, keep: tuple) -> None:
        def strip(value):
            return (
                {k: v for k, v in value.items() if k not in keep}
                if isinstance(value, dict)
                else value
            )

        try:
            if strip(json.loads(path.read_bytes()[:65536])) == strip(document):
                return
        except (OSError, ValueError):
            pass
        atomic_write(
            path,
            json.dumps(document, ensure_ascii=False, sort_keys=True).encode(),
            0o644,
        )

    def request(
        self,
        operation: str,
        goal: Goal,
        expected_revision: int,
        *,
        recover: bool = False,
        session: dict | None = None,
        resume_only: bool = False,
    ) -> dict:
        """Persist intent only. No side effect on a running backend until step is called.

        The legacy game action resumes the persistent editor. recover=True keeps ERROR
        until an explicit step(recover=True) fences and retries Workshop.
        """
        UUID(operation)
        with self._locked():
            return self._request_locked(
                operation,
                goal,
                expected_revision,
                recover=recover,
                session=session,
                resume_only=resume_only,
            )

    def _request_locked(
        self,
        operation: str,
        goal: Goal,
        expected_revision: int,
        *,
        recover: bool = False,
        session: dict | None = None,
        resume_only: bool = False,
        activate: bool = False,
    ) -> dict:
        """request() body under the controller lock, shared with wake()."""
        UUID(operation)
        state = self._read()
        self._project_target(state)
        self._project_desired(state)
        request_intent = asdict(goal)
        request_session = json.loads(json.dumps(session, allow_nan=False))
        returning = goal.profile == "game"
        if recover and not returning:
            raise RuntimeError("explicit recovery must resume the editor")
        if returning:
            release = (
                state["approvedTarget"]["release"]
                if state.get("selection", {}).get("deferred")
                else goal.release
            )
            goal, session = self._editor_goal(release, state)
        record = self._select_release(goal.release, state)
        selection = state.get("selection") or {}
        if (
            selection.get("operation") == operation
            or state.get("operation") == operation
        ):
            if (
                selection.get("request") != request_intent
                or selection.get("requestSession") != request_session
                or selection.get("recover") != recover
                or selection.get("resumeOnly") != resume_only
                or selection.get("intent") != asdict(goal)
                or state.get("approvedTarget") != record
            ):
                raise ValueError(
                    "operation ID reused with different intent or release evidence"
                )
            if getattr(self, "gate_reporter", None) is not None:
                # Same op retried (at-least-once delivery, or a retry after a
                # commit-then-fail): the first attempt may have committed
                # without staging. Re-export so the retry stages instead of
                # ACKing an undelivered leg; the stage dedups an identical
                # already-staged leg, so this never churns seqs. A staging
                # failure is commit-then-fail (the committed selection
                # stands): hold, never a bare OSError that sync would store
                # as terminal rejected.
                try:
                    self._export(state)
                except CommitIncompleteError:
                    raise
                except OSError as error:
                    raise CommitIncompleteError(
                        f"committed {operation!r} selection already stored"
                        f" but its gate leg was not staged: {error}"
                    ) from error
            return state
        if type(expected_revision) is not int or expected_revision != state["revision"]:
            raise ValueError("stale state revision")
        recovering = recover and state.get("phase") == "ERROR" and returning
        unknown_runtime = False
        if state.get("phase") in {"workshop", "test"} and not (activate or returning):
            try:
                current = self._observe()[2]
                unknown_runtime = (
                    current is None
                    or not current.ready
                    or asdict(current.goal) != state["desired"]
                )
            except Exception:
                unknown_runtime = True
        deferred = not (activate or recovering or returning) and state.get(
            "phase"
        ) not in {"UNINITIALIZED", "game", "workshop"}
        deferred = deferred or unknown_runtime
        if deferred:
            if goal.profile != "workshop" or goal.session != (
                state.get("desired") or {}
            ).get("session"):
                raise RuntimeError(
                    "operation in progress or requires recovery; only release staging allowed"
                )
            if unknown_runtime:
                state.update(
                    phase="ERROR",
                    gate="closed",
                    error="UncertainRuntime",
                    errorMessage="runtime does not match the committed goal; explicit recovery required",
                    since=self._now_ms(),
                )
            state.update(
                revision=state["revision"] + 1,
                previousTarget=state.get("approvedTarget"),
                approvedTarget=record,
                # Legacy import: the durable desired goal is the running
                # selection, so commit its verified identity as active too.
                activeTarget=(
                    record
                    if state.get("activeTarget") is None
                    and _release_matches(
                        (state.get("desired") or {}).get("release"), record
                    )
                    else state.get("activeTarget")
                ),
                selection={
                    "operation": operation,
                    "revision": state["revision"] + 1,
                    "request": request_intent,
                    "requestSession": request_session,
                    "recover": recover,
                    "resumeOnly": resume_only,
                    "intent": asdict(goal),
                    "deferred": True,
                },
            )
            self._write(state)
            return state
        if (
            state.get("phase")
            not in {"UNINITIALIZED", "game", "workshop", "test", "sleeping"}
            and not recovering
        ):
            raise RuntimeError("operation in progress or requires recovery")
        self.backend.preflight(goal)
        previous_session = self.session()
        if goal.profile == "workshop":
            if session is None:
                session = (
                    previous_session
                    if previous_session and previous_session.get("id") == goal.session
                    else {
                        "schema": 1,
                        "id": goal.session,
                        "maps": None,
                        "startedAt": self._now_ms(),
                        "startedBy": None,
                    }
                )
            if getattr(self.maps, "canonical_sessions", False) is True:
                session = self._selected_session(
                    goal,
                    session,
                    resume_only=resume_only
                    or bool(
                        previous_session and previous_session.get("id") == goal.session
                    ),
                )
        revision = state["revision"] + 1
        # The stable phase we're leaving IS the running goal (phase==goal.profile is an invariant of
        # _read()); remember it now so a later STARTING timeout can roll back to it without
        # relying on backend.observe() still agreeing once things go wrong.
        previous_stable = (
            state.get("desired")
            if state.get("phase") in {"workshop", "test"}
            and state.get("activeTarget") is not None
            else None
        )
        state = {
            "approvedTarget": record,
            "activeTarget": record,
            "previousTarget": state.get("previousTarget")
            if state.get("selection", {}).get("deferred")
            and state.get("approvedTarget") == record
            else state.get("approvedTarget"),
            "rollbackTarget": state.get("activeTarget"),
            "selection": {
                "operation": operation,
                "revision": revision,
                "request": request_intent,
                "requestSession": request_session,
                "recover": recover,
                "resumeOnly": resume_only,
                "intent": asdict(goal),
                "deferred": False,
            },
            "schema": 1,
            "revision": revision,
            "phase": "ERROR" if recovering else "REQUESTED",
            "gate": "closed",
            "operation": operation,
            "desired": asdict(goal),
            "error": state.get("error") if recovering else None,
            "errorMessage": state.get("errorMessage") if recovering else None,
            "since": self._now_ms(),
            "rollback": None if recovering else previous_stable,
            "rollbackSession": previous_session
            if previous_stable and previous_stable["profile"] in {"workshop", "test"}
            else None,
            "cancelOperation": None,
            "idleSince": None,
        }
        if (
            len(
                json.dumps(
                    state, ensure_ascii=False, sort_keys=True, allow_nan=False
                ).encode()
            )
            > STATE_MAX_BYTES
        ):
            raise ValueError("controller state exceeds its bounded reader")
        previous_state = self.file.read_bytes() if self.file.exists() else None
        session_paths = [self.session_file] + (
            [self.admin_dir / "session.json"] if self.admin_dir is not None else []
        )
        previous_exports = {
            path: path.read_bytes() if path.exists() else None for path in session_paths
        }
        try:
            if session is not None:
                self._save_session(session)
            self._write(state)
        except Exception:
            durable = self.file.read_bytes() if self.file.exists() else None
            if session is not None and durable == previous_state:
                for path, data in previous_exports.items():
                    if data is None:
                        path.unlink(missing_ok=True)
                    else:
                        atomic_write(
                            path,
                            data,
                            0o644 if path.parent == self.admin_dir else 0o600,
                        )
            raise
        return state

    def step(self, *, recover: bool = False) -> dict:
        """Reconcile Workshop only; Game is observed for status display, never selection.

        ERROR requires explicit recovery. STARTING waits closed:
        absence cannot distinguish a failed request from an accepted, delayed start.
        Explicit recovery fences pending starts before considering another start.
        A repeated step never counts as forced interruption. State is written only when
        phase/gate/error change, so a stable gate never flaps through closed.

        STARTING has a deadline and may restore a previous Workshop/test goal. A missing
        process after a durable start never proves that the accepted start was cancelled.
        """
        # Retry an undelivered gate leg before anything else, outside the lock.
        try:
            self._flush_gate_report()
        except Exception as error:
            print(f"controller: gate flush failed: {error!r}", file=sys.stderr)
        with self._locked():
            try:
                state = self._read()
                self._project_target(state)
                if (
                    state.get("desired")
                    and state["desired"]["profile"] != "game"
                    and state.get("activeTarget") is None
                ):
                    raise ValueError(
                        "missing committed active release identity; explicit import required"
                    )
            except (ValueError, OSError) as error:
                self._export(
                    {
                        "revision": 0,
                        "phase": "ERROR",
                        "gate": "closed",
                        "error": type(error).__name__,
                        "errorMessage": str(error),
                        "desired": None,
                    }
                )
                raise
            previous_phase = state["phase"]
            goal = Goal(**state["desired"]) if state.get("desired") else None

            def commit(
                phase: str,
                gate: str,
                error: str | None = None,
                error_message: str | None = None,
            ) -> None:
                # processes resolves at call time, so post-fence/re-observe
                # callers export the rebound (latest) observation, never older.
                if (
                    state["phase"],
                    state["gate"],
                    state.get("error"),
                    state.get("errorMessage"),
                ) != (phase, gate, error, error_message):
                    if state["phase"] != phase:
                        state["since"] = self._now_ms()
                        if phase in {"game", "workshop", "test"}:
                            state["rollback"] = None
                            state["releaseFollow"] = None
                    state.update(
                        phase=phase, gate=gate, error=error, errorMessage=error_message
                    )
                    self._write(state, processes=processes)

            try:
                processes, game, current = self._observe()
                if (
                    not recover
                    and current is not None
                    and not current.ready
                    and current.goal.release == "unknown"
                ):
                    state["releaseFollow"] = None
                    raise RuntimeError(
                        "Workshop selection is missing or invalid; explicit recovery required"
                    )
                # releaseFollow is legacy data from live-Game following, retired:
                # selection never tracks the running Game anymore. ERROR still
                # needs explicit recovery; nothing here consumes the record.
                if previous_phase == "ERROR" and not recover:
                    self._export(state, processes=processes)
                    return state
                elif previous_phase == "sleeping":
                    return self._sleeping_step(
                        state, goal, current, processes=processes
                    )
                if goal is None or goal.profile == "game":
                    # Only an explicit request imports an initial validated target.
                    # Legacy Game goals and running Game are never approval evidence.
                    self._export({**state, "gate": "closed"}, processes=processes)
                    return state
                if (state.get("rollback") or {}).get("profile") == "game":
                    # A legacy global switch must never restart or stop the independently running Game.
                    state["rollback"] = None
                    self._write(state, processes=processes)
                if recover and previous_phase in {"STARTING", "ERROR"}:
                    commit(
                        previous_phase,
                        "closed",
                        state.get("error"),
                        state.get("errorMessage"),
                    )
                    self.backend.fence_pending_starts()
                    processes, game, current = self._observe()
                if (
                    getattr(self.maps, "canonical_sessions", False) is True
                    and state["phase"] == "workshop"
                    and current is not None
                    and current.ready
                    and current.goal == goal
                ):
                    selected = self._selected_session(goal, resume_only=True)
                    if selected != self.session():
                        self._save_session(selected)
                if (
                    current is not None
                    and not current.ready
                    and current.goal.release == "unknown"
                ):
                    state["releaseFollow"] = None
                    raise RuntimeError(
                        "Workshop selection is missing or invalid; explicit recovery required"
                    )
                if (
                    state.get("selection", {}).get("deferred")
                    and previous_phase == "workshop"
                    and current is not None
                    and current.ready
                    and current.goal == goal
                ):
                    self._request_locked(
                        str(uuid4()),
                        replace(goal, release=state["approvedTarget"]["release"]),
                        state["revision"],
                        activate=True,
                    )
                    return self._read()
                # Workshop never follows the live Game release: a Game
                # rollout, stop or absence leaves the approved target alone.
                # Selecting another release is an explicit new operation.
                if current is not None and current.goal == goal:
                    if current.ready:
                        if self._maybe_sleep(state, goal, current, processes=processes):
                            return state
                        commit(goal.profile, OPEN_GATES[goal.profile])
                        self._export(state, processes=processes)
                        return state
                    commit("STARTING", "closed")
                    return self._starting_wait(
                        state, goal, unexpected=None, processes=processes
                    )
                if previous_phase == "STARTING" and not recover:
                    # Either nothing is visible yet (intent was durable before start(); never reissue it
                    # automatically, including after a controller crash or delayed process visibility) or
                    # current is an unexpected backend -- e.g. the 2026-09-18 incident, where an external
                    # restart resurrected another backend mid-start. Either way: wait for the deadline,
                    # fencing off anything unexpected instead of erroring out and requiring root recovery.
                    return self._starting_wait(
                        state, goal, unexpected=current, processes=processes
                    )
                # Only work that stops or starts a backend is gated by space/lock checks: a stable, ready backend
                # must not be closed to everybody because free space dipped (it keeps running either way).
                self.backend.preflight(goal)
                if (
                    goal.profile == "workshop"
                    and getattr(self.maps, "canonical_sessions", False) is True
                ):
                    self._selected_session(goal, resume_only=True)
                if current is not None:
                    commit("SAVING", "closed")
                    self.backend.save_and_freeze(current)
                    self.backend.stop(current)
                    # Rebind: the STARTING commit below must export the post-stop
                    # observation (the game side may have changed during save/stop).
                    processes, game, current = self._observe()
                    if current is not None:
                        raise RuntimeError("old backend not confirmed stopped")
                    if self.maps is not None and not processes:
                        self.maps.versions.reclaim_unheaded_bundles()
                commit("STARTING", "closed")
                self.backend.start(goal)
                return state
            except Exception as error:
                if state.get("desired") is None:
                    self._export(state)
                    raise
                state.update(
                    phase="ERROR",
                    gate="closed",
                    error=type(error).__name__,
                    errorMessage=str(error) or repr(error),
                )
                self._write(state)
                return state

    def _maybe_sleep(
        self,
        state: dict,
        goal: Goal,
        current: Process,
        processes: list[Process] | None = None,
    ) -> bool:
        """Sleep the stable idle editor through the existing graceful save/stop path.

        True when the backend was stopped and the phase is now sleeping. Never sleeps
        a transition, ERROR, test profile, pending release follow or inbox backlog;
        any missing/stale/foreign activity report keeps the editor awake.
        """
        if (
            state.get("phase") != "workshop"
            or goal is None
            or goal.profile != "workshop"
            or state.get("releaseFollow") is not None
            or self._inbox_has_pending()
        ):
            if state.get("idleSince") is not None:
                state["idleSince"] = None
                self._write(state, processes=processes)
            return False
        if not self.idle_sleep_s or self.idle_sleep_s < 0:
            if state.get("idleSince") is not None:
                state["idleSince"] = None
                self._write(state, processes=processes)
            return False
        if not self._activity_idle(state, processes=processes):
            return False
        now = self._now_ms()
        idle_since = state.get("idleSince")
        if type(idle_since) is not int:
            state["idleSince"] = now
            self._write(state, processes=processes)
            return False
        if now - idle_since < int(self.idle_sleep_s * 1000):
            return False
        # Stop admitting before the save: a join after the idle report would lose edits made
        # after freeze. A failed save/stop lands in ERROR, which stays closed.
        state["gate"] = "closed"
        self._write(state, processes=processes)
        self.backend.save_and_freeze(current)
        self.backend.stop(current)
        post_stop, _, managed = self._observe()
        if managed is not None:
            raise RuntimeError("old backend not confirmed stopped")
        if self.maps is not None and not post_stop:
            self.maps.versions.reclaim_unheaded_bundles()
        state.update(
            revision=state["revision"] + 1,
            operation=str(uuid4()),
            phase="sleeping",
            gate="closed",
            error=None,
            errorMessage=None,
            since=now,
            rollback=None,
            idleSince=None,
        )
        state.pop("releaseFollow", None)
        # Post-stop observation: the game side may have changed while the editor
        # was being saved and stopped, so export it fresh, not pre-stop.
        self._write(state, processes=post_stop)
        return True

    def _sleeping_step(
        self,
        state: dict,
        goal: Goal | None,
        current: Process | None,
        processes: list[Process] | None = None,
    ) -> dict:
        """Asleep: no backend is expected; an externally started matching backend is adopted."""
        if (
            goal is not None
            and current is not None
            and current.ready
            and current.goal == goal
        ):
            if state["phase"] != "workshop":
                state["since"] = self._now_ms()
                state["phase"] = "workshop"
                state["gate"] = OPEN_GATES[goal.profile]
                self._write(state, processes=processes)
            else:
                self._export(state, processes=processes)
            return state
        self._export(state, processes=processes)
        return state

    def _starting_wait(
        self,
        state: dict,
        goal: Goal,
        unexpected: Process | None,
        processes: list[Process] | None = None,
    ) -> dict:
        if unexpected is not None:
            self.backend.fence_pending_starts()
            processes = None  # fencing changes backends; re-observe below
        elapsed_ms = self._now_ms() - state["since"]
        if elapsed_ms > self.start_timeout * 1000:
            detail = (
                "unexpected backend present"
                if unexpected is not None
                else "no ready backend appeared"
            )
            return self._rollback(
                state,
                goal,
                f"start of {goal.profile} timed out after {elapsed_ms // 1000}s ({detail})",
            )
        self._export(
            state,
            deadline=state["since"] + int(self.start_timeout * 1000),
            processes=processes,
        )
        return state

    def _rollback(
        self, state: dict, goal: Goal, reason: str, operation_id: str | None = None
    ) -> dict:
        """Abandon the in-flight switch and reopen the gate of the previously stable profile.

        STARTING already stopped it, so bring it back up before declaring the profile stable again;
        if that itself fails there is no safe automatic move left, so it becomes an ERROR (root recovery).
        operation_id keys the journal: a same-op retry of an already-committed
        cancel is a no-op, never a second compensation run.
        """
        if state.get("rollback") is None:
            # An independently restarted backend has no switch to undo; keep waiting for readiness.
            self._export(state)
            return state
        stable = Goal(**state["rollback"])
        if stable.profile == "game":
            raise RuntimeError("legacy Game rollback must be migrated before recovery")
        target = state.get("rollbackTarget")
        if target is None or not _release_matches(stable.release, target):
            raise ValueError(
                "rollback requires its committed previous release identity"
            )
        restarting = state["phase"] == "STARTING"
        pending = state["selection"]["deferred"]
        state.update(
            revision=state["revision"] + 1,
            operation=str(uuid4()),
            phase="STARTING" if restarting else stable.profile,
            gate="closed" if restarting else OPEN_GATES[stable.profile],
            desired=asdict(stable),
            previousTarget=state.get("previousTarget")
            if pending
            else state["approvedTarget"],
            approvedTarget=state["approvedTarget"] if pending else target,
            activeTarget=target,
            rollbackTarget=None,
            error=None,
            errorMessage=reason,
            since=self._now_ms(),
            rollback=None,
            # Key the journal on the owning cancel op (None for timeout
            # rollbacks): a same-op retry must not compensate twice.
            cancelOperation=operation_id,
        )
        if not pending:
            state["selection"] = {
                "operation": state["operation"],
                "revision": state["revision"],
                "request": asdict(stable),
                "requestSession": None,
                "recover": False,
                "resumeOnly": False,
                "intent": asdict(stable),
                "deferred": False,
            }
        # Journal the single compensation attempt before fencing/stopping/starting.
        # The journal is the commit point: a later derivative failure drives
        # ERROR visibly (so a retry lands on the vacuous "nothing to cancel"
        # path) and raises commit-then-fail (hold, never rejected) instead of
        # letting a retry fence the restored backend a second time.
        try:
            self._write(state)
        except CommitIncompleteError as error:
            self._drive_error_after_commit(state, error)
            raise
        if restarting:
            try:
                self.backend.preflight(stable)
                self.backend.fence_pending_starts()
                if state.get("rollbackSession") is not None:
                    self._save_session(state["rollbackSession"])
                self.backend.stop(Process(goal, False))
                if self._observe()[2] is not None:
                    raise RuntimeError("rollback backend not confirmed stopped")
                self.backend.start(stable)
            except Exception as error:
                state.update(
                    phase="ERROR",
                    gate="closed",
                    error=type(error).__name__,
                    errorMessage=f"{reason}; rollback to {stable.profile} also failed: {error}",
                )
                self._write(state)
        return state

    def cancel(self, operation_id: str | None = None) -> dict:
        """Unprivileged abort of a stuck switch (site -> inbox "cancel", no root): roll back now
        instead of waiting for the deadline. Only valid while a switch is actually in flight.
        With no previous stable profile (first-ever start, or a recover retry that never became
        ready) there is nothing to restore: fence anything half-started and park in ERROR so an
        explicit request --recover can retry, instead of spinning with the gate closed.
        operation_id keys the compensation: a same-op retry of an already
        committed cancel returns the journaled state instead of fencing again."""
        with self._locked():
            state = self._read()
            if state.get("phase") not in {"STARTING"}:
                raise ValueError("nothing to cancel: no switch is in progress")
            if (
                operation_id is not None
                and state.get("cancelOperation") == operation_id
            ):
                # The compensation for this op already committed (the receipt
                # was lost after the commit): the retry must be a no-op.
                return state
            if not state.get("rollback"):
                self.backend.fence_pending_starts()
                state.update(
                    revision=state["revision"] + 1,
                    operation=str(uuid4()),
                    phase="ERROR",
                    gate="closed",
                    error="Cancelled",
                    errorMessage="cancelled by admin request; explicit recovery required",
                    since=self._now_ms(),
                    rollback=None,
                    rollbackTarget=None,
                    cancelOperation=operation_id,
                )
                try:
                    self._write(state)
                except CommitIncompleteError as error:
                    self._drive_error_after_commit(state, error)
                    raise
                return state
            return self._rollback(
                state,
                Goal(**state["desired"]),
                "cancelled by admin request",
                operation_id=operation_id,
            )

    def wake(self, operation: str, actor: dict | None = None) -> dict:
        """Idempotent wake of a sleeping editor (site/proxy inbox "wake", workshopctl wake).

        Sleeping restarts the same goal through the ordinary request path, so the next
        step starts it like any other switch (STARTING -> workshop) at the exact
        approved digest; the session, maps and drafts are unchanged. Any other phase is
        already awake or starting and reports ok without side effects, with
        "started": False so the site can tell it apart from a real wake ("started": True).
        A member actor
        (draslId/name) must belong to the workshop ACL; None is the root operator
        (workshopctl) and skips that check.
        """
        UUID(operation)
        with self._locked():
            if actor is not None:
                self._member_actor(actor)
            state = self._read()
            if state.get("phase") != "sleeping":
                return {
                    "kind": "wake",
                    "phase": state.get("phase"),
                    "revision": state.get("revision"),
                    # No-op: the editor is already awake or starting. Explicit so the
                    # site never reads ok:true as "just started" while builders stay out.
                    "started": False,
                }
            target = self._project_target(state)
            if target is None:
                raise ValueError("wake requires a committed approved target")
            goal = replace(Goal(**state["desired"]), release=target["release"])
            state = self._request_locked(
                operation, goal, state["revision"], activate=True
            )
            return {
                "kind": "wake",
                "phase": state["phase"],
                "revision": state["revision"],
                "started": True,
            }

    def apply_cp_releases(self, records) -> list:
        """Merge CP-attested approved-release records into the catalog (S6b).

        Records arrive in the agent snapshot or a release.select operation,
        over the mutually authenticated CP channel -- unlike releases.json,
        which is an operator file, and unlike the inbox, whose fields are
        self-asserted. Each record passes the same provenance validation as
        the file catalog; a tag whose identity changed is refused.
        """
        if not isinstance(records, list) or len(records) > CP_RELEASE_LIMIT:
            raise ValueError("invalid cp release records")
        validated = {}
        for entry in records:
            record = release_catalog.validate_release_record(entry)
            release = record["release"]
            known = validated.get(release, self.releases.get(release))
            if known is not None and known != record:
                raise ValueError("published release identity changed")
            validated[release] = record
        # One commit point: a bad entry refuses the whole batch before
        # anything is mutated, persisted, or handed to the backend.
        merged = [release for release in validated if release not in self.releases]
        self.releases.update(validated)
        if merged:
            if self.releases_path is not None:
                atomic_write(
                    self.releases_path,
                    json.dumps(self.releases, sort_keys=True, allow_nan=False).encode(),
                )
                self._releases_fingerprint = self._fingerprint_releases()
            if hasattr(self.backend, "releases"):
                self.backend.releases = dict(self.releases)
        return merged

    def _apply_cp_release_select(self, operation_id: str, payload: dict) -> dict:
        """Commit a central approved-release record; activation stays journal-bound."""
        release = payload.get("release")
        if not isinstance(release, str) or not release:
            raise ValueError("invalid cp release selection")
        record = payload.get("record")
        if record is not None:
            if not isinstance(record, dict) or record.get("release") != release:
                raise ValueError("cp release record does not match selection")
            self.apply_cp_releases([record])
        with self._locked():
            resolved = self._select_release(release, self._read())
        return {"kind": "release.select", "release": resolved["release"]}

    def apply_cp_operation(
        self, operation_id: str, payload: dict, acl: list | None = None
    ) -> dict:
        """Apply a CP-attested typed operation (unit CP-S6a).

        The payload actor/grantor is attested by the control plane: CP
        authorized the human before committing the outbox row, and the
        frame arrived over our mutually authenticated agent channel. This
        is the opposite of the file inbox, whose ``actor`` field is
        self-asserted and therefore never a grant authority (E4).
        Unknown kinds are rejected, never blind-applied; map operations
        stay inbox-only; release selection is release.select. The payload
        target must be global or a resource this workshop serves, and scoped
        members operations must not attest map ids.
        """
        UUID(operation_id)
        if not isinstance(payload, dict):
            raise ValueError("invalid cp operation")
        kind = payload.get("kind")
        if kind not in ("members", "message", "wake", "cancel", "release.select"):
            raise ValueError(f"unsupported cp operation kind: {kind!r}"[:64])
        served = self._map_ids() | {"hub"}
        target = payload.get("target")
        if not isinstance(target, str) or (target != "*" and target not in served):
            raise ValueError("cp operation targets another resource")
        actor = payload.get("grantor") if kind == "members" else payload.get("actor")
        if not isinstance(actor, dict):
            raise ValueError("invalid cp actor")
        principal = member({**actor, "role": "builder"})
        if acl is None and kind in ("members", "release.select"):
            raise ValueError(f"cp {kind} requires a CP ACL snapshot")
        if acl is None:
            # No snapshot list (direct calls; sync always passes one): check
            # against the local members instead of skipping the check, so
            # message/wake/cancel fail closed without an operator bootstrap.
            acl = self.members()
        matches = [
            entry
            for entry in acl
            if isinstance(entry, dict)
            and entry.get("draslId") == principal["draslId"]
            and entry.get("name") == principal["name"]
        ]
        if not matches:
            raise ValueError("cp actor is not a workshop member")
        if kind in ("members", "release.select") and not all(
            entry.get("role") == "admin" for entry in matches
        ):
            # Duplicate ACL identities with mixed roles fail closed.
            if kind == "members":
                raise ValueError("cp grantor is not a workshop admin")
            raise ValueError("cp actor is not a workshop admin")
        if kind == "release.select":
            return self._apply_cp_release_select(operation_id, payload)
        if kind == "members":
            entries = payload.get("members")
            if not isinstance(entries, list):
                raise ValueError("members must be a list")
            validated = [member(entry, scopes=served) for entry in entries]
            if not any(item["role"] == "admin" for item in validated):
                raise ValueError("members must keep at least one admin")
            if target != "*" and any(
                isinstance(entry, dict) and entry.get("maps") for entry in entries
            ):
                # Map ids are outside any scoped grant: only a global op can attest them.
                raise ValueError("members maps need a global target")
            self.set_members(entries)
            return {"kind": "members", "count": len(entries)}
        if kind == "message":
            self.set_message(payload.get("message"))
            return {"kind": "message"}
        if kind == "wake":
            return self.wake(
                operation_id,
                {"draslId": principal["draslId"], "name": principal["name"]},
            )
        try:
            return {
                "kind": "cancel",
                "revision": self.cancel(operation_id=operation_id)["revision"],
            }
        except ValueError as error:
            if "nothing to cancel" not in str(error):
                raise
            # Idempotent retry: a previous pass already cancelled (its gate
            # report may still be pending); a first-attempt no-op is a vacuous
            # success, like a no-op wake.
            return {"kind": "cancel", "revision": self._read()["revision"]}

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
                        candidates.append(
                            (os.lstat(self.inbox_dir / name).st_mtime_ns, name)
                        )
                    except FileNotFoundError:
                        pass
                # The site renames complete files, so mtime approximates createdAt without reading every file.
                pending = []
                for _, name in sorted(candidates)[:INBOX_BATCH]:
                    document, error = self._load_request(name)
                    created = document.get("createdAt") if document else None
                    pending.append(
                        (created if type(created) is int else 0, name, document, error)
                    )
                pending.sort(key=lambda item: item[:2])
                done = 0
                for _, name, document, error in pending:
                    request_id = name[: -len(".json")]
                    actor, revision, payload = "?", None, None
                    try:
                        if error:
                            raise ValueError(error)
                        actor = (
                            member({**document.get("actor"), "role": "builder"})["name"]
                            if isinstance(document.get("actor"), dict)
                            else None
                        )
                        if actor is None:
                            raise ValueError("invalid actor")
                        if document.get("id") != request_id:
                            raise ValueError("id does not match file name")
                        if (
                            self.inbox_retired
                            and document.get("kind") in CP_COVERED_INBOX_KINDS
                        ):
                            raise ValueError(CP_INBOX_RETIRED_ERROR)
                        # An operation already accepted (e.g. recovery step was busy) is finished, not expired.
                        if (
                            type(document.get("createdAt")) is not int
                            or now_ms - document["createdAt"] > INBOX_TTL_MS
                        ) and self.status().get("operation") != request_id:
                            raise ValueError("request expired")
                        applied = self._apply(request_id, document)
                        if isinstance(applied, dict):
                            payload = applied
                        else:
                            revision = applied
                    except BlockingIOError:
                        break
                    except Exception as failure:
                        error, revision = str(failure) or type(failure).__name__, None
                    else:
                        error = None
                    result = {
                        "id": request_id,
                        "ok": error is None,
                        "error": error,
                        "revision": revision,
                        "doneAt": now_ms,
                    }
                    if payload is not None:
                        result["payload"] = payload
                        if isinstance(payload, dict) and payload.get("kind") in (
                            "ai-key",
                            "ai-gateway-key",
                            "ai-provider-key",
                        ):
                            # The site contract reads the ai-key status flat
                            # (kind/set/last4/sha256Prefix/appliedAt); the envelope
                            # above stays untouched for uniform result polling.
                            result.update(payload)
                    atomic_write(
                        results / name,
                        json.dumps(result, ensure_ascii=False, sort_keys=True).encode(),
                        0o644,
                    )
                    _remove(self.inbox_dir / name)
                    done += 1
                    kind = document.get("kind") if document else None
                    print(
                        f"inbox {request_id} kind={kind!r} actor={actor} "
                        + ("ok" if error is None else f"error: {error}"),
                        file=sys.stderr,
                        flush=True,
                    )
                self._prune_results(results)
        except (BlockingIOError, FileNotFoundError):
            return 0
        return done

    def export_maps(self) -> None:
        """admin/candidates.json for the site: candidate list with status, backups per map."""
        if self.maps is None or not self.admin_dir:
            return
        for map_id in self.maps.retire_merged_overlays():
            print(
                f"maps: published {map_id}.json now equals the repo copy, overlay retired",
                file=sys.stderr,
                flush=True,
            )
        packages = self.maps.packages
        for meta in self.maps.list():
            if meta["status"] != "accepted":
                continue
            source = (
                self.maps.exported_archive(meta)
                if meta.get("native")
                else self.maps.archive_path(meta["id"])
            )
            if source is None:
                continue
            destination = packages / (meta["sha256"] + ".zip")
            if not destination.exists():
                packages.mkdir(parents=True, exist_ok=True)
                # Unique temp per export: concurrent steps (timer + manual) share
                # this loop and must not trample each other's copy mid-digest.
                fd, name = tempfile.mkstemp(
                    dir=packages, prefix=destination.name + ".", suffix=".tmp"
                )
                temporary = Path(name)
                os.close(fd)
                try:
                    shutil.copyfile(source, temporary)
                    with temporary.open("rb") as handle:
                        digest = hashlib.file_digest(handle, "sha256").hexdigest()
                        os.fsync(handle.fileno())
                    if digest != meta["sha256"]:
                        raise ValueError("accepted package checksum mismatch")
                    temporary.chmod(0o444)
                    temporary.replace(destination)
                    directory_fd = os.open(packages, os.O_RDONLY | os.O_DIRECTORY)
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
                    if meta.get("native"):
                        meta["derivedPackage"] = True
                        self.maps._write_meta(meta)
                finally:
                    temporary.unlink(missing_ok=True)
        self._replace_if_changed(
            self.admin_dir / "candidates.json", self.maps.export(), keep=()
        )
        sessions = self.maps.sessions()
        catalog = self.maps.catalog()
        selected = self.session()
        if selected and getattr(self.maps, "canonical_sessions", False) is True:
            for entry in catalog:
                if entry["mapId"] in (selected.get("maps") or []):
                    entry["draft"] = self.maps.copy_properties(
                        entry["mapId"], selected["id"]
                    )
        self._replace_if_changed(
            self.admin_dir / "maps.json",
            {
                "schema": 1,
                "maps": catalog,
                "sessions": sessions["sessions"],
                "sessionsTotal": sessions["total"],
                "sessionsNext": sessions["next"],
            },
            keep=(),
        )

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
            fd = os.open(
                self.inbox_dir / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            )
        except OSError:
            return None, "not a regular file"
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            os.close(fd)
            return None, "not a regular file"
        if info.st_size > INBOX_MERGE_MAX_BYTES:
            os.close(fd)
            return None, "request too large"
        with os.fdopen(fd, "rb") as handle:
            data = handle.read(INBOX_MAX_BYTES + 1)
            if len(data) > INBOX_MAX_BYTES:
                data += handle.read(INBOX_MERGE_MAX_BYTES + 1 - len(data))
        if len(data) > INBOX_MERGE_MAX_BYTES:
            return None, "request too large"
        try:
            from map_package import _json

            document = _json(data)
        except Exception:  # includes RecursionError from deeply nested input
            return None, "request too large" if len(
                data
            ) > INBOX_MAX_BYTES else "invalid json"
        if len(data) > INBOX_MAX_BYTES and (
            not isinstance(document, dict)
            or not isinstance(document.get("kind"), str)
            or document.get("kind") not in {"merge-preview", "publish"}
        ):
            return None, "request too large"
        if (
            not isinstance(document, dict)
            or type(document.get("schema")) is not int
            or document["schema"] != 1
        ):
            return None, "unsupported schema"
        return document, None

    def export_limbo(self, version: str) -> dict:
        if self.maps is None:
            raise ValueError("map publishing is not configured")
        with self._locked():
            state = self.status()
            _, _, current = self._observe()
            if (
                state["phase"] != "workshop"
                or current is None
                or current.goal.profile != "workshop"
                or not current.ready
                or asdict(current.goal) != state["desired"]
            ):
                raise ValueError(
                    "native Limbo export requires the selected ready workshop backend"
                )
            return self.maps.export_limbo(version, self.backend)

    def _snapshot(
        self,
        map_id: str,
        actor: dict,
        note: str,
        *,
        working_copy_id=None,
        branch=None,
        expected_branch_head=None,
        commit=False,
        review=False,
        authorize=False,
    ) -> dict:
        # Refuse a missing draft before issuing any save commands.
        options = (
            {"working_copy_id": working_copy_id} if working_copy_id is not None else {}
        )
        if branch is None or commit:
            self.maps.check(map_id, **options)
        with self._locked():
            if authorize:
                actor = self._map_actor(actor, map_id)
            state = self._read()
            sleeping = state["phase"] == "sleeping"
            if state["phase"] != "workshop" and not sleeping:
                raise ValueError("snapshot requires a stable workshop")
            if getattr(self.maps, "canonical_sessions", False) is True:
                selected = self.session()
                if (
                    not selected
                    or working_copy_id != selected.get("id")
                    or map_id not in (selected.get("maps") or [])
                    or state["desired"]["profile"] == "workshop"
                    and state["desired"]["session"] != working_copy_id
                ):
                    raise ValueError(
                        "snapshot requires the exact selected session and map"
                    )
                self.maps.versions.validate_working_copy(map_id, working_copy_id)
            if branch is not None or commit:
                if getattr(self.maps, "canonical_sessions", False) is not True:
                    raise ValueError("branch snapshots require canonical sessions")
                if (
                    not isinstance(note, str)
                    or len(note) > 200
                    or commit
                    and not note.strip()
                ):
                    raise ValueError("commit message required (1–200 characters)")
                if branch != selected.get("branches", {}).get(map_id):
                    raise ValueError("snapshot requires the exact selected branch")
                if (
                    self.maps.versions.branch(map_id, branch)["versionDigest"]
                    != expected_branch_head
                ):
                    raise ValueError("stale branch head: refresh before committing")
                options.update(branch=branch, expected_branch_head=expected_branch_head)
            _, _, current = self._observe()
            if sleeping:
                if current is not None:
                    raise RuntimeError("sleeping workshop has a running backend")
            elif (
                current is None
                or not current.ready
                or asdict(current.goal) != state["desired"]
            ):
                raise RuntimeError("snapshot backend does not match controller state")

            def take():
                if commit:
                    return self.maps.commit(
                        map_id,
                        actor,
                        note,
                        working_copy_id=working_copy_id,
                        branch=branch,
                        expected_head=expected_branch_head,
                    )
                return self.maps.create(map_id, actor, note, **options)

            if branch is not None and not commit:
                meta = self.maps.review(
                    map_id,
                    actor,
                    note,
                    working_copy_id=working_copy_id,
                    branch=branch,
                    expected_head=expected_branch_head,
                )
                self.export_maps()
                return meta
            # Asleep the drafts on disk are final; awake maps.hold_saves flushes the running editor
            # and pauses autosave for the copy. Neither path restarts Workshop.
            meta = take()
            if review:
                # The tablet's one button: commit what is built now, then review that commit.
                meta = self.maps.review(
                    map_id,
                    actor,
                    note,
                    working_copy_id=working_copy_id,
                    branch=branch,
                    expected_head=meta["versionDigest"],
                )
            self.export_maps()
            return meta

    def _member_actor(self, actor: dict) -> dict:
        principal = member(
            {**actor, "role": "builder"}, scopes=self._map_ids() | {"hub"}
        )
        admitted = next(
            (
                entry
                for entry in self.members()
                if (
                    entry["draslId"] == principal["draslId"]
                    and entry["name"] == principal["name"]
                )
            ),
            None,
        )
        if admitted is None:
            raise ValueError("map actor is not an authenticated workshop member")
        return admitted

    def _map_actor(self, actor: dict, map_id: str, *, admin=False) -> dict:
        admitted = self._member_actor(actor)
        if admin and admitted["role"] != "admin":
            raise ValueError("main map operations require an admin")
        if map_id not in self._map_ids() or (
            admitted["role"] == "builder"
            and map_id not in admitted.get("maps", self._map_ids())
        ):
            raise ValueError("map is outside the actor's scope")
        return {key: admitted[key] for key in ("draslId", "name")}

    def _merge_preview(self, map_id, source, expected_head, resolutions=None) -> dict:
        self.maps.versions.check_head(map_id, expected_head)
        if expected_head is None:
            if resolutions is not None and not isinstance(resolutions, dict):
                raise ValueError("merge conflict choices must be an object")
            if resolutions:
                raise ValueError("initial publication has no merge conflict choices")
            manifest = self.maps.versions.read(map_id, source)
            config = self.maps.versions._config(manifest["files"])
            changes = self.maps.versions._compare_files(
                map_id, None, source, {}, manifest["files"], config
            )
            return {
                "mapId": map_id,
                "baseVersion": None,
                "sourceVersion": source,
                "targetVersion": None,
                "conflicts": [],
                "unresolved": [],
                "changes": changes,
            }
        preview = self.maps.versions.merge_preview(
            map_id, source, expected_head, resolutions
        )
        return {
            key: preview[key]
            for key in (
                "mapId",
                "baseVersion",
                "sourceVersion",
                "targetVersion",
                "conflicts",
                "unresolved",
                "changes",
            )
        }

    def _map_change(self, request_id, document):
        if (
            self.maps is None
            or getattr(self.maps, "canonical_sessions", False) is not True
        ):
            raise ValueError("map authoring requires canonical storage")
        with self._locked():
            admitted = self._member_actor(document["actor"])
            if admitted["role"] not in {"lead", "admin"}:
                raise ValueError("map creation and properties require a lead or admin")
            actor = {key: admitted[key] for key in ("draslId", "name")}
            state, previous = self._read(), self.session()
            creating = document["kind"] == "map-create"
            map_id = document.get("mapId")
            if (
                creating
                and state.get("operation") == request_id
                and (state.get("desired") or {}).get("session")
                == document.get("sessionId")
            ):
                return self.maps.create_map(
                    map_id,
                    document.get("name"),
                    document.get("supportedModes"),
                    document.get("sessionId"),
                    actor,
                )
            revision = document.get("expectedRevision")
            if type(revision) is not int or revision != state["revision"]:
                raise ValueError("stale state revision")
            if state["phase"] != "workshop" or previous is None:
                raise ValueError("map authoring requires a stable workshop")
            if creating:
                self.maps.check_create(
                    map_id,
                    document.get("name"),
                    document.get("supportedModes"),
                    document.get("sessionId"),
                    actor,
                )
            else:
                map_package.map_name(document.get("name"))
                if (
                    document.get("workingCopyId") != previous["id"]
                    or map_id not in previous["maps"]
                ):
                    raise ValueError(
                        "properties require the exact selected working copy"
                    )
                self.maps.versions.validate_working_copy(map_id, previous["id"])
            _, _, current = self._observe()
            if (
                current is None
                or not current.ready
                or asdict(current.goal) != state["desired"]
            ):
                raise RuntimeError(
                    "map authoring backend does not match controller state"
                )
            self.backend.preflight(current.goal)
            state.update(
                revision=state["revision"] + 1,
                phase="ERROR",
                gate="closed",
                operation=request_id,
                error="MapAuthoringInterrupted",
                errorMessage="map change did not finish; recover before reopening",
                since=self._now_ms(),
                rollback=asdict(current.goal),
                rollbackTarget=state.get("activeTarget"),
                rollbackSession=previous,
                releaseFollow=None,
            )
            self._write(state)

            def fail(error):
                state.update(
                    phase="ERROR",
                    gate="closed",
                    error=type(error).__name__,
                    errorMessage=str(error),
                )
                self._write(state)

            try:
                self.backend.save_and_freeze(current)
                self.backend.stop(current)
                _, _, managed = self._observe()
                if managed is not None:
                    raise RuntimeError("workshop not confirmed stopped")
            except Exception as error:
                fail(error)
                raise
            goal, payload, copy_error = current.goal, None, None
            try:
                if creating:
                    payload = self.maps.create_map(
                        map_id,
                        document.get("name"),
                        document.get("supportedModes"),
                        document.get("sessionId"),
                        actor,
                    )
                    self._save_session(payload["session"])
                    goal = Goal(
                        "workshop", current.goal.release, payload["session"]["id"]
                    )
                else:
                    payload = self.maps.rename(
                        map_id, document.get("name"), previous["id"]
                    )
            except Exception as error:
                copy_error = error
                self._save_session(previous)
            try:
                state.update(
                    desired=asdict(goal),
                    phase="STARTING",
                    gate="closed",
                    error=None,
                    errorMessage=None,
                    since=self._now_ms(),
                )
                self._write(state)
                self.backend.start(goal)
                self.export_maps()
            except Exception as error:
                fail(error)
                raise
            if copy_error is not None:
                raise copy_error
            return payload

    def _apply_ai_key(
        self,
        request_id: str,
        document: dict,
        *,
        filename: str = "openrouter.key",
        status_name: str = "ai-key.json",
        kind: str = "ai-key",
    ) -> dict:
        """Write-only workshop AI key from the site; null removes it.

        Runs under the controller lock. The key value never reaches logs, results,
        state or exceptions: only its last4/sha256 prefix leave this function.
        """
        if "key" not in document:
            raise ValueError(f"invalid {kind} request")
        key = document["key"]
        if key is not None and (
            type(key) is not str or AI_KEY_PATTERN.fullmatch(key) is None
        ):
            raise ValueError(f"invalid {kind} request")
        with self._locked():
            base = self.workshop_data_dir
            if base is None:
                apps = getattr(self.backend, "apps", None)
                if apps is not None and "workshop" in apps:
                    base = apps["workshop"].data
            if base is None:
                raise ValueError("workshop data directory is not configured")
            directory, path = (
                Path(base) / "blockfield-workshop",
                Path(base) / f"blockfield-workshop/{filename}",
            )
            for node in (path, *path.parents):
                if node.is_symlink():
                    raise ValueError(f"unsafe {kind} path")
                if node == Path(base):
                    break
            try:
                info = os.stat(directory)
            except FileNotFoundError:
                directory.mkdir(mode=0o755)
                os.chmod(directory, 0o755)
                os.chown(directory, AI_KEY_UID, AI_KEY_GID)
            else:
                if (
                    not stat.S_ISDIR(info.st_mode)
                    or info.st_uid != AI_KEY_UID
                    or info.st_gid != AI_KEY_GID
                ):
                    raise ValueError(f"unsafe {kind} directory")
            if key is None:
                path.unlink(missing_ok=True)
                stored = {"set": False, "last4": "", "sha256Prefix": ""}
            else:
                data = key.encode()
                atomic_write(path, data, owner=(AI_KEY_UID, AI_KEY_GID))
                stored = {
                    "set": True,
                    "last4": key[-4:],
                    "sha256Prefix": hashlib.sha256(data).hexdigest()[:8],
                }
            applied_at = self._now_ms()
            atomic_write(
                self.admin_dir / status_name,
                json.dumps(
                    {"schema": 1, **stored, "appliedAt": applied_at},
                    ensure_ascii=False,
                    sort_keys=True,
                ).encode(),
                0o644,
            )
            return {"kind": kind, **stored, "appliedAt": applied_at}

    def _apply(self, request_id: str, document: dict) -> int | dict | None:
        kind = document.get("kind")
        if kind == "wake":
            actor = document.get("actor")
            if not isinstance(actor, dict):
                raise ValueError("invalid actor")
            return self.wake(
                request_id,
                {"draslId": actor.get("draslId"), "name": actor.get("name")},
            )
        if kind in LIVE_ONLY_KINDS and self.status().get("phase") == "sleeping":
            raise ValueError(SLEEP_MESSAGE)
        if kind in {"map-create", "map-properties"}:
            return self._map_change(request_id, document)
        if kind == "sessions":
            if self.maps is None:
                raise ValueError("map publishing is not configured")
            with self._locked():
                self._member_actor(document["actor"])
                return self.maps.sessions(
                    document.get("offset", 0), document.get("limit", 100)
                )
        if kind == "members":
            entries = document.get("members")
            if not isinstance(entries, list):
                raise ValueError("members must be a list")
            if not any(
                member(entry, scopes=self._map_ids() | {"hub"})["role"] == "admin"
                for entry in entries
            ):
                raise ValueError("members must keep at least one admin")
            self.set_members(entries)
            return None
        if kind == "message":
            self.set_message(document.get("message"))
            return None
        if kind == "cancel":
            # An admin cancels a stuck Workshop transition without root.
            # The inbox request id keys the compensation: a redelivered file
            # after a committed cancel is a no-op, never a second fence.
            return self.cancel(operation_id=request_id)["revision"]
        if kind in {
            "candidate",
            "publish",
            "reject",
            "rollback",
            "checkout",
            "export",
            "limbo-export",
            "branch-create",
            "commit",
            "compare",
            "merge-preview",
        }:
            force = document.get("force", False)
            if type(force) is not bool:
                raise ValueError("force must be a boolean")
            if self.maps is None:
                raise ValueError("map publishing is not configured")
            actor = {
                "draslId": document["actor"]["draslId"],
                "name": document["actor"]["name"],
            }
            map_id = document.get("mapId")
            if kind in {"publish", "reject", "export"}:
                meta = next(
                    (
                        entry
                        for entry in self.maps.list()
                        if entry["id"] == document.get("candidateId")
                    ),
                    None,
                )
                if meta is None:
                    raise ValueError("unknown candidate")
                map_id = meta["mapId"]
            elif kind == "limbo-export":
                map_id = "limbo"
            actor = self._map_actor(
                actor,
                map_id,
                admin=kind in {"publish", "reject", "rollback", "limbo-export"},
            )
            result = None
            if kind in {"candidate", "commit"}:
                if "commit" in document and type(document["commit"]) is not bool:
                    raise ValueError("commit must be a boolean")
                result = self._snapshot(
                    document.get("mapId"),
                    actor,
                    document.get("note", ""),
                    working_copy_id=document.get("workingCopyId"),
                    branch=document.get("branch"),
                    expected_branch_head=document.get("expectedBranchHead"),
                    # candidate {commit: true} is the tablet's commit-and-review in one request.
                    commit=kind == "commit" or document.get("commit") is True,
                    review=kind == "candidate" and document.get("commit") is True,
                    authorize=True,
                )
            elif kind == "limbo-export":
                self.export_limbo(document.get("versionDigest"))
            else:
                with self._locked():
                    actor = self._map_actor(
                        actor, map_id, admin=kind in {"publish", "reject", "rollback"}
                    )
                    if kind == "publish":
                        options = (
                            {"expected_head": document["expectedHead"]}
                            if "expectedHead" in document
                            else {}
                        )
                        if meta.get("sourceBranch"):
                            if force:
                                raise ValueError(
                                    "force cannot bypass map merge conflicts"
                                )
                            if (
                                "expectedHead" in document
                                and meta.get("targetHead") != document["expectedHead"]
                            ):
                                raise ValueError(
                                    "review target changed; submit a new review"
                                )
                        result = self.maps.publish(
                            document.get("candidateId"),
                            actor,
                            force=force,
                            resolutions=document.get("resolutions"),
                            **options,
                        )
                    elif kind == "reject":
                        result = self.maps.reject(document.get("candidateId"), actor)
                    elif kind == "checkout":
                        result = self.maps.checkout(
                            document.get("mapId"),
                            document.get("versionDigest"),
                            document.get("workingCopyId"),
                        )
                    elif kind == "export":
                        self.maps.archive_path(document.get("candidateId"))
                    elif kind == "branch-create":
                        result = self.maps.versions.create_branch(
                            map_id,
                            document.get("branch"),
                            document.get("versionDigest"),
                            actor,
                        )
                    elif kind == "compare":
                        result = self.maps.versions.compare(
                            map_id, document.get("before"), document.get("after")
                        )
                    elif kind == "merge-preview":
                        result = self._merge_preview(
                            map_id,
                            document.get("versionDigest"),
                            document.get("expectedHead"),
                            document.get("resolutions"),
                        )
                    else:
                        if "expectedHead" not in document:
                            raise ValueError(
                                "rollback requires versionDigest and expectedHead"
                            )
                        result = self.maps.rollback(
                            document.get("mapId"),
                            actor,
                            version_digest=document.get("versionDigest"),
                            expected_head=document["expectedHead"],
                        )
            self.export_maps()
            return result
        if kind == "ai-key":
            return self._apply_ai_key(request_id, document)
        if kind == "ai-gateway-key":
            return self._apply_ai_key(
                request_id,
                document,
                filename="gateway.key",
                status_name="ai-gateway-key.json",
                kind="ai-gateway-key",
            )
        if kind == "ai-provider-key":
            provider = document.get("provider")
            if (
                type(provider) is not str
                or AI_PROVIDER_ID.fullmatch(provider) is None
                or (
                    provider not in AI_PROVIDER_KEY_PRESETS
                    and provider in AI_PROVIDER_RESERVED_IDS
                )
            ):
                raise ValueError("invalid ai-provider-key request")
            filename, status_name, _ = ai_provider_key_names(provider)
            return self._apply_ai_key(
                request_id,
                document,
                filename=filename,
                status_name=status_name,
                kind="ai-provider-key",
            )
        if kind != "mode":
            raise ValueError("unknown kind")
        profile, expected, recover = (
            document.get("profile"),
            document.get("expectedRevision"),
            document.get("recover", False),
        )
        if (
            profile not in {"game", "workshop", "test"}
            or type(expected) is not int
            or type(recover) is not bool
        ):
            raise ValueError("invalid mode request")
        if recover and profile != "game":
            raise ValueError("recover is only allowed toward game")
        if "sessionId" in document and profile != "workshop":
            raise ValueError("archived sessionId is only allowed toward workshop")
        if "modeId" in document and profile != "test":
            raise ValueError("modeId is only allowed for a candidate test")
        state, session = self.status(), None
        if (state.get("selection") or {}).get("operation") == request_id:
            goal = Goal(**state["selection"]["request"])
            session = state["selection"]["requestSession"]
        elif state.get("operation") == request_id:
            goal = Goal(**state["desired"])
        else:
            release = (state.get("desired") or {}).get("release")
            if not release:
                raise ValueError("controller is not initialised")
            if profile == "workshop":
                actor = {
                    "draslId": document["actor"]["draslId"],
                    "name": document["actor"]["name"],
                }
                session = self._session_for(document, actor)
                goal = Goal("workshop", release, session["id"])
            elif profile == "test":
                # A closed playtest of one candidate, started from the running workshop and returning to it.
                if state.get("phase") == "sleeping":
                    raise ValueError(SLEEP_MESSAGE)
                if state.get("phase") != "workshop":
                    raise ValueError("a test starts only from the running workshop")
                if self.maps is None:
                    raise ValueError("map publishing is not configured")
                self.maps.validate_test(
                    document.get("candidateId"), document.get("modeId")
                )
                goal = Goal(
                    "test",
                    release,
                    state["desired"]["session"],
                    self.maps.sha256_of(document.get("candidateId")),
                    document.get("modeId"),
                )
            else:
                goal = Goal("game", release)
        state = self.request(
            request_id,
            goal,
            expected,
            recover=recover,
            session=session,
            resume_only="sessionId" in document,
        )
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
