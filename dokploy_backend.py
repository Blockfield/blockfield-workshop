"""Workshop lifecycle without docker.sock or a Dokploy API token.

Mechanism (owner decision 2026-10-30): the server release CI owns the fixed
GHCR tag (``<repository>:approved``) — it moves the tag to the verified
release with its own GITHUB_TOKEN after release verification passes, then
triggers the Workshop app redeploy through the app's Dokploy webhook. This
backend never moves tags. start() persists the verified selection
(profile.json) and clears the READY file, then POSTs the Workshop app's
deploy webhook when WORKSHOP_DEPLOY_WEBHOOK_URL is set, so a stopped
editor restarts without a manual deploy; unset keeps the manual-deploy
behaviour. save/stop/status go through direct private-network RCON plus the mod's READY
file. The approved release is read back from the running Workshop (READY +
selection) for reporting only. No docker CLI, no Dokploy API token, no
GitHub credentials at all: only the Workshop's own RCON password plus,
optionally, the deploy webhook URL (both secrets, never logged).

The old DockerBackend stays authoritative (see docs/OPERATIONS.md); this
module is staged beside it and shares the controller Backend protocol, the
profile.json selection schema and the READY-file contract.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import socket
import struct
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID, uuid4

from controller import Goal, Process, atomic_write

logger = logging.getLogger(__name__)

READY_FILE = ".blockfield-ready.json"
UNKNOWN_SESSION = "00000000-0000-0000-0000-000000000000"

DEPLOY_WEBHOOK_ENV = "WORKSHOP_DEPLOY_WEBHOOK_URL"
DEPLOY_WEBHOOK_FILE_ENV = "WORKSHOP_DEPLOY_WEBHOOK_URL_FILE"
DEPLOY_WEBHOOK_TIMEOUT_S = 10.0
DEPLOY_WEBHOOK_ATTEMPTS = 3
DEPLOY_WEBHOOK_RETRY_DELAY_S = 2.0
DEPLOY_WEBHOOK_RESPONSE_CAP = 65536


class RconError(RuntimeError):
    """Private-network RCON command failed; never carries the password."""


def read_secret(
    *,
    value: str | None,
    env: str,
    file_env: str,
    what: str,
    error: type = RconError,
) -> str:
    """Credential from an explicit value, env var or secret file (first line)."""
    if value:
        return value
    direct = os.environ.get(env, "").strip()
    if direct:
        return direct
    path = os.environ.get(file_env, "").strip()
    if path:
        try:
            with open(path, encoding="utf-8") as handle:
                secret = handle.readline().strip()
        except OSError as exc:
            raise error(f"{what} secret file unreadable") from exc
        if secret:
            return secret
    raise error(f"{what} credential is not configured")


def _webhook_not_sent(error: BaseException) -> bool:
    """True only when the POST never reached Dokploy, so a retry cannot queue
    a second deployment (a timeout or HTTP answer may already be accepted)."""
    reason = getattr(error, "reason", None)
    return (
        isinstance(error, urllib.error.URLError)
        and not isinstance(error, urllib.error.HTTPError)
        and isinstance(reason, (ConnectionRefusedError, socket.gaierror))
    )


def _post_deploy_webhook(url: str, timeout_s: float = DEPLOY_WEBHOOK_TIMEOUT_S) -> None:
    """POST one Dokploy deploy webhook; raise without ever carrying the URL."""
    from cp_client import _NoRedirect

    opener = urllib.request.build_opener(_NoRedirect)
    request = urllib.request.Request(url, data=b"", method="POST")
    try:
        with opener.open(request, timeout=timeout_s) as response:
            response.read(DEPLOY_WEBHOOK_RESPONSE_CAP + 1)
            status = response.status
    except urllib.error.HTTPError as error:
        try:
            error.close()
        except Exception:
            pass
        raise RuntimeError(f"deploy webhook answered http {error.code}") from error
    if not 200 <= status <= 299:
        raise RuntimeError(f"deploy webhook answered http {status}")


class RconClient:
    """Minimal Source RCON over TCP on the private network (stdlib only)."""

    def __init__(
        self,
        host: str,
        port: int,
        *,
        password: str | None = None,
        timeout: float = 10.0,
    ):
        if not host or not (0 < port < 65536):
            raise ValueError("RCON needs a private-network host and port")
        self.host, self.port = host, port
        self._password = read_secret(
            value=password,
            env="BF_WORKSHOP_RCON_PASSWORD",
            file_env="BF_WORKSHOP_RCON_PASSWORD_FILE",
            what="workshop RCON",
            error=RconError,
        )
        self.timeout = timeout
        self._socket: socket.socket | None = None

    def _packet(self, request_id: int, kind: int, body: bytes) -> bytes:
        return (
            struct.pack("<iii", 4 + 4 + len(body) + 2, request_id, kind)
            + body
            + b"\x00\x00"
        )

    def _read_exact(self, count: int) -> bytes:
        assert self._socket is not None
        chunks = []
        while sum(map(len, chunks)) < count:
            chunk = self._socket.recv(count - sum(map(len, chunks)))
            if not chunk:
                raise RconError("RCON connection closed mid-packet")
            chunks.append(chunk)
        return b"".join(chunks)

    def _read_packet(self) -> tuple[int, bytes]:
        (size,) = struct.unpack("<i", self._read_exact(4))
        if not 10 <= size <= 1 << 20:
            raise RconError("RCON answered with a malformed packet")
        payload = self._read_exact(size)
        request_id, _kind = struct.unpack("<ii", payload[:8])
        return request_id, payload[8:-2]

    def connect(self) -> None:
        """TCP + auth; raises RconError without leaking the password."""
        try:
            self._socket = socket.create_connection(
                (self.host, self.port), timeout=self.timeout
            )
            self._socket.settimeout(self.timeout)
            self._socket.sendall(self._packet(1, 3, self._password.encode()))
            request_id, _ = self._read_packet()
        except RconError:
            self.close()
            raise
        except Exception as error:
            self.close()
            raise RconError(f"RCON connect failed: {type(error).__name__}") from error
        if request_id == -1:
            self.close()
            raise RconError("RCON rejected the password")

    def command(self, *words: str) -> str:
        """Run one trusted console command; at most 64 response packets.

        The command goes out under request id 1, followed by a re-auth
        fence under id 2. A real Java answers every command packet under
        its own id and the fence with an empty body, but never echoes an
        empty drain marker (it answers the empty command with error text
        instead), so the fence reply marks the end of the output.
        """
        if self._socket is None:
            self.connect()
        try:
            assert self._socket is not None
            self._socket.sendall(self._packet(1, 2, " ".join(words).encode()))
            self._socket.sendall(self._packet(2, 3, self._password.encode()))
            parts = []
            for _ in range(64):
                request_id, body = self._read_packet()
                if request_id == -1:
                    raise RconError("RCON session was refused mid-command")
                if request_id == 2:
                    # Fence reply: output drained, fence itself has no output.
                    break
                if request_id != 1:
                    raise RconError("RCON answered with an unknown request id")
                parts.append(body)
            else:
                raise RconError("RCON flooded the response budget")
            # shortcut: multi-packet truncation at 64 chunks; raise the budget when commands need it.
            return b"".join(parts).decode("utf-8", "replace")
        except RconError:
            self.close()
            raise
        except Exception as error:
            self.close()
            raise RconError(f"RCON command failed: {type(error).__name__}") from error

    def close(self) -> None:
        if self._socket is not None:
            try:
                self._socket.close()
            except OSError:
                pass
            self._socket = None

    def __enter__(self) -> "RconClient":
        self.connect()
        return self

    def __exit__(self, *ignored) -> None:
        self.close()


class DokployBackend:
    """Workshop-only lifecycle beside the release-owned approved tag.

    Observes the mod's READY file plus an RCON liveness probe (TCP connect,
    no auth), saves through ``save-all flush`` and stops through RCON
    ``stop``. start() persists the verified selection and clears the
    READY file — the :approved rollout itself is the server release CI's
    job — then POSTs the Workshop app's Dokploy deploy webhook when
    WORKSHOP_DEPLOY_WEBHOOK_URL is set, so a stopped editor restarts
    without a manual deploy; unset keeps the manual-deploy path. The
    approved release is read back from the running Workshop for
    reporting only. Never touches Game: observe() reports Workshop/test
    only, so Game reads as unavailable.
    """

    def __init__(
        self,
        *,
        data: Path,
        lock_dir: Path,
        rcon_host: str,
        rcon_port: int,
        rcon_password: str | None = None,
        releases: dict | None = None,
        maps=None,
        stop_timeout: float = 300.0,
        poll: float = 2.0,
        min_free_bytes: int = 2 << 30,
        sleep=time.sleep,
        clock=time.monotonic,
    ):
        self.data = Path(data)
        self.lock_dir = Path(lock_dir)
        self.rcon_host = rcon_host
        self.rcon_port = rcon_port
        self.rcon_password = rcon_password
        self.releases = dict(releases) if releases else {}
        self.maps = maps
        self.stop_timeout = stop_timeout
        self.poll = poll
        self.min_free_bytes = min_free_bytes
        self._sleep = sleep
        self._clock = clock

    @classmethod
    def from_env(cls, env=os.environ, **overrides) -> "DokployBackend":
        """Build from BF_ env; the secret stays in env vars or secret files."""
        import releases as release_catalog

        releases_path = env.get("BF_RELEASES_FILE")
        return cls(
            data=Path(env.get("BF_WORKSHOP_DATA", "/srv/minecraft/workshop/data")),
            lock_dir=Path(env.get("BF_LOCK_DIR", "/srv/minecraft/backend-lock")),
            rcon_host=env.get("BF_WORKSHOP_RCON_HOST", "blockfield-workshop"),
            rcon_port=int(env.get("BF_WORKSHOP_RCON_PORT", "25575")),
            releases=release_catalog.load_catalog(
                Path(releases_path) if releases_path else None
            ),
            **overrides,
        )

    def _rcon(self) -> RconClient:
        return RconClient(self.rcon_host, self.rcon_port, password=self.rcon_password)

    def _alive(self) -> bool:
        """TCP liveness probe: java accepts RCON connections without auth."""
        try:
            probe = socket.create_connection(
                (self.rcon_host, self.rcon_port), timeout=5.0
            )
        except OSError:
            return False
        with probe:
            return True

    def preflight(self, goal: Goal) -> None:
        if goal.profile == "game":
            raise ValueError("Game lifecycle belongs to the server deployment")
        if goal.profile not in {"workshop", "test"}:
            raise ValueError(f"profile {goal.profile} has no app in this backend")
        if goal.profile == "test":
            if self.maps is None:
                raise ValueError("test needs the map candidates store")
            if hasattr(self.maps, "validate_test"):
                self.maps.validate_test(goal.candidate, goal.modeId)
        if self.lock_dir.is_symlink() or not self.lock_dir.is_dir():
            raise FileNotFoundError("backend lock directory missing")
        if self.data.is_symlink() or not self.data.is_dir():
            raise FileNotFoundError(f"data directory for {goal.profile} missing")
        if shutil.disk_usage(self.data).free < self.min_free_bytes:
            raise OSError("not enough free space for the backend data")
        self._digest(goal.release)
        # Fail closed on a misconfigured webhook secret before anything
        # stops: a set-but-invalid URL raises here, never mid-restart.
        self._deploy_webhook_url()

    def _digest(self, release: str) -> str:
        import releases as release_catalog

        if not self.releases:
            raise ValueError("no verified releases catalog configured")
        return release_catalog.resolve(self.releases, release)["imageDigest"]

    def selection(self) -> dict | None:
        path = self.lock_dir / "workshop" / "profile.json"
        if path.is_symlink():
            raise ValueError("invalid profile.json")
        try:
            data = json.loads(path.read_bytes()[:16384])
        except FileNotFoundError:
            return None
        if (
            not isinstance(data, dict)
            or data.get("schema") != 1
            or data.get("profile") not in {"workshop", "test"}
            or not isinstance(data.get("release"), str)
            or not data["release"]
        ):
            raise ValueError("invalid profile.json")
        UUID(data.get("generation"))
        if data.get("session") is not None:
            UUID(data["session"])
        return data

    def readiness(self) -> dict | None:
        try:
            data = json.loads((self.data / READY_FILE).read_bytes()[:16384])
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or set(data) != {
            "profile",
            "generation",
            "session",
            "release",
        }:
            return None
        try:
            if data["profile"] not in {"workshop", "test"}:
                return None
            if not isinstance(data["release"], str) or not data["release"].strip():
                return None
            if (
                data["generation"] is not None
                and str(UUID(data["generation"])) != data["generation"]
            ):
                return None
            if str(UUID(data["session"])) != data["session"]:
                return None
        except (ValueError, TypeError, AttributeError):
            return None
        return data

    def _ready_matches(self, ready: dict, expected: dict) -> bool:
        """Whether the attested READY file is the selected backend.

        profile/generation/session compare strictly; release accepts either
        name of the same verified catalog record (the image's build tag vs
        the approval's image digest). Without a catalog binding both names
        the backend is not ready.
        """
        if (
            ready.get("profile") != expected.get("profile")
            or ready.get("generation") != expected.get("generation")
            or ready.get("session") != expected.get("session")
        ):
            return False
        if ready.get("release") == expected.get("release"):
            return True
        # No catalog configured: nothing binds the two names, so not ready.
        for record in (self.releases or {}).values():
            if not isinstance(record, dict):
                continue
            names = {
                record.get("release"),
                (record.get("imageDigest") or "").split("@", 1)[-1],
            }
            if ready.get("release") in names and expected.get("release") in names:
                return True
        return False

    def observe(self) -> list[Process]:
        try:
            selected = self.selection()
        except (ValueError, TypeError, AttributeError, OSError):
            selected = None
            corrupt = True
        else:
            corrupt = False
        alive = self._alive()
        if not alive:
            # Stopped backends report nothing, mirroring the scaled-to-zero
            # service: a stale selection or READY file is not a process.
            # Corrupt selection with no living java stays invisible too;
            # recovery is explicit, never an invented unknown backend.
            return []
        if corrupt:
            return [Process(Goal("workshop", "unknown", UNKNOWN_SESSION), False)]
        ready = self.readiness()
        if selected is None:
            selected, expected = ready, ready
        else:
            expected = {
                key: selected.get(key)
                for key in ("profile", "generation", "session", "release")
            }
        if not isinstance(selected, dict):
            goal, expected = Goal("workshop", "unknown", UNKNOWN_SESSION), None
        else:
            try:
                goal = Goal(
                    selected["profile"],
                    selected["release"],
                    selected.get("session"),
                    selected.get("candidate"),
                    selected.get("modeId"),
                )
            except (ValueError, TypeError):
                goal, expected = Goal("workshop", "unknown", UNKNOWN_SESSION), None
        return [
            Process(
                goal,
                bool(
                    alive
                    and expected
                    and ready
                    and self._ready_matches(ready, expected)
                ),
            )
        ]

    def save_and_freeze(self, process: Process) -> None:
        if process.goal.profile == "game":
            raise ValueError("Workshop controller cannot save or stop Game")
        with self._rcon() as rcon:
            result = rcon.command("save-all", "flush")
        if "Saved the game" not in result:
            raise RuntimeError(f"save-all did not confirm success: {result[:300]}")

    @staticmethod
    def _save_on_confirmed(result: str) -> bool:
        return any(
            message in result
            for message in (
                "Automatic saving is now enabled",
                "Saving is already turned on",
            )
        )

    @contextmanager
    def workshop_saves_held(self):
        """Hot snapshot: flush chunks and hold autosave off while the copy runs, so builders stay
        connected. A stopped editor has nothing to hold. Entry re-enables autosave first: a
        previous failed save-on (or a kill mid-snapshot) otherwise leaves it off silently and the
        next save-off accepts "already turned off" as success."""
        if not self._alive():
            yield
            return
        with self._rcon() as rcon:
            try:
                if not self._save_on_confirmed(rcon.command("save-on")):
                    raise RuntimeError("save-on did not confirm autosave enabled")
                result = rcon.command("save-off")
                if not any(
                    message in result
                    for message in (
                        "Automatic saving is now disabled",
                        "Saving is already turned off",
                    )
                ):
                    raise RuntimeError("save-off did not confirm autosave disabled")
                result = rcon.command("save-all", "flush")
                if "Saved the game" not in result:
                    raise RuntimeError(
                        "save-all did not confirm success for live snapshot"
                    )
                yield
            finally:
                # A timed-out save-off may still have reached Java; retry an
                # unconfirmed save-on once, since it likely only missed the reply.
                try:
                    result = rcon.command("save-on")
                except RconError:
                    result = rcon.command("save-on")
                if not self._save_on_confirmed(result):
                    result = rcon.command("save-on")
                if not self._save_on_confirmed(result):
                    raise RuntimeError("save-on did not confirm autosave restored")

    def _stop_and_wait(self, timeout_message: str) -> None:
        """RCON stop, then wait dark. Shutdown tears down the reply path, so
        a mid-command RCON error only matters if the backend stays alive; a
        connect/auth failure retries while the TCP probe still sees the
        backend (a booting java listens before RCON answers), bounded by
        stop_timeout."""
        error: RconError | None = None
        deadline = self._clock() + self.stop_timeout
        while self._alive():
            try:
                with self._rcon() as rcon:
                    rcon.command("stop")
                break
            except RconError as exc:
                error = exc
                if not self._alive():
                    return
                if self._clock() >= deadline:
                    raise TimeoutError(timeout_message) from error
                self._sleep(self.poll)
        while self._alive():
            if self._clock() >= deadline:
                raise TimeoutError(timeout_message) from error
            self._sleep(self.poll)

    def stop(self, process: Process) -> None:
        if process.goal.profile == "game":
            raise ValueError("Workshop controller cannot save or stop Game")
        self.save_and_freeze(process)
        self._stop_and_wait("workshop still running after RCON stop")

    def fence_pending_starts(self) -> None:
        """No delayed old request may later start a backend when this returns.

        Stop anything still alive and wait until the liveness probe goes
        quiet. Unknown states raise. (A delayed queued Workshop deploy
        landing after the fence still resurrects java; it then reads as
        alive-but-unselected and fails closed.)
        """
        if self._alive():
            self._stop_and_wait("cannot fence the workshop backend")

    def _operation_id(self, goal: Goal) -> str:
        """Generation for this start; a retry reuses the persisted intent's."""
        try:
            selected = self.selection()
        except (ValueError, TypeError, AttributeError, OSError):
            selected = None
        if isinstance(selected, dict) and all(
            selected.get(key) == value
            for key, value in (
                ("profile", goal.profile),
                ("release", goal.release),
                ("session", goal.session),
                ("candidate", goal.candidate),
                ("modeId", goal.modeId),
            )
        ):
            generation = selected.get("generation")
            try:
                if generation is not None and str(UUID(generation)) == generation:
                    return generation
            except (ValueError, TypeError, AttributeError):
                pass
        return str(uuid4())

    def _deploy_webhook_url(self) -> str:
        """Workshop app's Dokploy deploy webhook URL, or "" when unset.

        Direct env var first, then a secret file, mirroring the RCON
        password lookup. Set-but-invalid raises (fail closed); unset is
        not an error — the caller keeps the manual-deploy path. The URL
        itself never appears in errors: it is a secret.
        """
        from cp_client import _check_cp_url

        url = os.environ.get(DEPLOY_WEBHOOK_ENV, "").strip()
        if not url:
            path = os.environ.get(DEPLOY_WEBHOOK_FILE_ENV, "").strip()
            if not path:
                return ""
            try:
                with open(path, encoding="utf-8") as handle:
                    url = handle.readline().strip()
            except OSError as exc:
                raise ValueError(
                    f"{DEPLOY_WEBHOOK_ENV} secret file unreadable"
                ) from exc
            if not url:
                # A configured but empty secret is a misconfiguration: treating
                # it as unset would let a stop proceed with no way to start.
                raise ValueError(f"{DEPLOY_WEBHOOK_ENV} secret file is empty")
        try:
            _check_cp_url(url)
        except ValueError as exc:
            raise ValueError(
                f"{DEPLOY_WEBHOOK_ENV} must be https "
                "(http only for loopback or an internal service name)"
            ) from exc
        return url

    def _trigger_deploy_webhook(self) -> bool:
        """POST the configured deploy webhook; never raises, never logs the URL.

        Unset keeps today's manual-deploy path with one actionable warning.
        A failed POST warns (exception type only, no URL, no body) and
        returns False: the persisted selection is the durable intent, and
        the STARTING deadline/rollback stays the visible state — never an
        ERROR loop.
        """
        try:
            url = self._deploy_webhook_url()
        except ValueError as error:
            # Preflight validated it; a secret changed since then must not
            # turn start() into an exception loop.
            logger.warning(
                "deploy webhook not posted (%s); selection is persisted, "
                "waiting for a manual deploy",
                error,
            )
            return False
        if not url:
            logger.warning(
                "%s is not set; a stopped editor needs a manual deploy "
                "until the Workshop app's Dokploy deploy webhook URL is configured",
                DEPLOY_WEBHOOK_ENV,
            )
            return False
        for attempt in range(1, DEPLOY_WEBHOOK_ATTEMPTS + 1):
            try:
                _post_deploy_webhook(url, DEPLOY_WEBHOOK_TIMEOUT_S)
                return True
            except Exception as error:
                if attempt >= DEPLOY_WEBHOOK_ATTEMPTS or not _webhook_not_sent(error):
                    logger.warning(
                        "deploy webhook POST failed after %d attempt(s) (%s); "
                        "selection is persisted, waiting for a manual deploy",
                        attempt,
                        type(error).__name__,
                    )
                    return False
                self._sleep(DEPLOY_WEBHOOK_RETRY_DELAY_S)
        return False

    def start(self, goal: Goal) -> None:
        """Persist the verified selection, then restart a stopped editor.

        Only a verified catalog release is accepted (fail-closed selection);
        the approved release itself is read back from the running Workshop
        for reporting only (observe()/readiness()). When
        WORKSHOP_DEPLOY_WEBHOOK_URL is set this also POSTs the Workshop
        app's Dokploy deploy webhook once, so a stopped container comes
        back; a failed POST only warns (see _trigger_deploy_webhook).
        """
        self.preflight(goal)
        self._digest(goal.release)
        if goal.profile == "test" and self.maps is not None:
            self.maps.extract(goal.candidate, self.data / "test-candidate")
        document = {
            "schema": 1,
            "profile": goal.profile,
            "generation": self._operation_id(goal),
            "session": goal.session,
            "release": goal.release,
        }
        if goal.candidate:
            document["candidate"] = goal.candidate
        if goal.modeId is not None:
            document["modeId"] = goal.modeId
        selected_dir = self.lock_dir / "workshop"
        selected_dir.mkdir(exist_ok=True)
        atomic_write(
            selected_dir / "profile.json",
            json.dumps(document, sort_keys=True).encode(),
            0o644,
        )
        (self.data / READY_FILE).unlink(missing_ok=True)
        # The generation persists in profile.json first, so a retried
        # start() for the same goal reuses it instead of minting a new one.
        # The webhook goes out last: the persisted selection is already the
        # durable restart intent if the POST fails.
        self._trigger_deploy_webhook()
