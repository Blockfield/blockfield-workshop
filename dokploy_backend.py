"""Workshop lifecycle over CP events/state without docker.sock or RCON.

Mechanism (coordination#64): the server release CI owns the approved image tag
and triggers the Workshop app redeploy when released. start() writes the
verified desired selection to CP state (``game-desired``), then POSTs the
Workshop app's Dokploy deploy webhook when WORKSHOP_DEPLOY_WEBHOOK_URL is set.
Readiness/activity come from CP state (``game-ready`` / ``game-activity``) over
SSE, and save/stop/export-limbo dispatch to the Workshop game CP consumer
via ``POST /v2/workshop/game`` + SSE settlement. Never touches Game.
"""

from __future__ import annotations

import logging
import os
import shutil
import socket
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID, uuid4

from controller import Goal, Process
from cp_client import (
    DEFAULT_READY_TTL_MS,
    GateReporter,
    StateReporter,
    WorkshopGameClient,
    _NoRedirect,
    _check_cp_url,
    cp_saves_held,
    parse_game_activity,
    parse_game_desired,
    parse_game_ready,
)

logger = logging.getLogger(__name__)

UNKNOWN_SESSION = "00000000-0000-0000-0000-000000000000"

DEPLOY_WEBHOOK_ENV = "WORKSHOP_DEPLOY_WEBHOOK_URL"
DEPLOY_WEBHOOK_FILE_ENV = "WORKSHOP_DEPLOY_WEBHOOK_URL_FILE"
DEPLOY_WEBHOOK_TIMEOUT_S = 10.0
DEPLOY_WEBHOOK_ATTEMPTS = 3
DEPLOY_WEBHOOK_RETRY_DELAY_S = 2.0
DEPLOY_WEBHOOK_RESPONSE_CAP = 65536


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


class DokployBackend:
    """Workshop-only lifecycle using CP state & agent events plus deploy webhook."""

    def __init__(
        self,
        *,
        data: Path,
        lock_dir: Path,
        releases: dict | None = None,
        maps=None,
        stop_timeout: float = 300.0,
        poll: float = 2.0,
        min_free_bytes: int = 2 << 30,
        ready_ttl_ms: int | None = DEFAULT_READY_TTL_MS,
        sleep=time.sleep,
        clock=time.monotonic,
        now_ms=None,
        state_reporter: StateReporter | None = None,
        game_client: WorkshopGameClient | None = None,
    ):
        self.data = Path(data)
        self.lock_dir = Path(lock_dir)
        self.releases = dict(releases) if releases else {}
        self.maps = maps
        self.stop_timeout = stop_timeout
        self.poll = poll
        self.min_free_bytes = min_free_bytes
        self.ready_ttl_ms = ready_ttl_ms
        self._sleep = sleep
        self._clock = clock
        self._now_ms = now_ms or (lambda: int(time.time() * 1000))
        self.state_reporter = state_reporter
        self.game_client = game_client

    @classmethod
    def from_env(cls, env=os.environ, **overrides) -> "DokployBackend":
        """Build from BF_ / BLOCKFIELD_CP_ env; secrets stay in env vars or files."""
        import releases as release_catalog

        releases_path = env.get("BF_RELEASES_FILE")
        extra = dict(overrides)
        if "state_reporter" not in extra or "game_client" not in extra:
            gate = GateReporter.from_env(env)
            if gate is not None:
                extra.setdefault(
                    "state_reporter", StateReporter(gate.base_url, gate.transport)
                )
                extra.setdefault(
                    "game_client",
                    WorkshopGameClient.from_env(
                        env, base_url=gate.base_url, transport=gate.transport
                    ),
                )
        ttl_raw = (env.get("BF_WORKSHOP_READY_TTL_S") or "").strip()
        if ttl_raw and "ready_ttl_ms" not in extra:
            extra["ready_ttl_ms"] = int(float(ttl_raw) * 1000)
        return cls(
            data=Path(env.get("BF_WORKSHOP_DATA", "/srv/minecraft/workshop/data")),
            lock_dir=Path(env.get("BF_LOCK_DIR", "/srv/minecraft/backend-lock")),
            releases=release_catalog.load_catalog(
                Path(releases_path) if releases_path else None
            ),
            **extra,
        )

    def _require_state_reporter(self) -> StateReporter:
        if self.state_reporter is None:
            raise RuntimeError("CP state reporter is not configured")
        return self.state_reporter

    def _require_game_client(self) -> WorkshopGameClient:
        if self.game_client is None:
            raise RuntimeError("CP Workshop game client is not configured")
        return self.game_client

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
        doc = self._require_state_reporter().doc("game-desired")
        return parse_game_desired(doc, allowed_profiles={"workshop", "test"})

    def _read_ready(self, *, refresh: bool = False) -> tuple[dict | None, bool]:
        doc = self._require_state_reporter().doc("game-ready", refresh=refresh)
        return parse_game_ready(
            doc,
            allowed_profiles={"workshop", "test"},
            now_ms=self._now_ms(),
            ready_ttl_ms=self.ready_ttl_ms,
        )

    def readiness(self) -> dict | None:
        ready, alive = self._read_ready()
        return ready if alive else None

    def readiness_generation(self, profile: str) -> str | None:
        if profile not in {"workshop", "test"}:
            return None
        ready = self.readiness()
        generation = ready.get("generation") if ready else None
        return generation if isinstance(generation, str) and generation else None

    def activity(self) -> dict | None:
        return parse_game_activity(self._require_state_reporter().doc("game-activity"))

    def _ready_matches(self, ready: dict, expected: dict) -> bool:
        """Whether the CP game-ready doc matches the selected backend."""
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
        ready, alive = self._read_ready()
        if not alive:
            return []
        if corrupt:
            return [Process(Goal("workshop", "unknown", UNKNOWN_SESSION), False)]
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
        self._require_game_client().command("save-flush")

    @contextmanager
    def workshop_saves_held(self):
        """Hot snapshot: flush chunks and hold autosave off via CP agent ops."""
        _, alive = self._read_ready(refresh=True)
        if not alive:
            yield
            return
        with cp_saves_held(self._require_game_client()):
            yield

    def _stop_and_wait(self, timeout_message: str) -> None:
        client = self._require_game_client()
        deadline = self._clock() + self.stop_timeout
        client.command(
            "stop",
            expires_in_s=max(1, min(300, int(self.stop_timeout))),
        )
        while self._read_ready(refresh=True)[1]:
            if self._clock() >= deadline:
                raise TimeoutError(timeout_message)
            self._sleep(self.poll)

    def stop(self, process: Process) -> None:
        if process.goal.profile == "game":
            raise ValueError("Workshop controller cannot save or stop Game")
        self.save_and_freeze(process)
        self._stop_and_wait("workshop still running after CP stop")

    def fence_pending_starts(self) -> None:
        if self._read_ready(refresh=True)[1]:
            self._stop_and_wait("cannot fence the workshop backend")

    def export_limbo(self, job: str) -> None:
        if str(UUID(job)) != job or self.maps is None or self.maps.limbo is None:
            raise ValueError("configured native Limbo export job required")
        self._require_game_client().command(
            "export-limbo",
            job=job,
            expires_in_s=max(60, min(300, int(self.stop_timeout))),
        )

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
        self._require_state_reporter().put("game-desired", document)
        self._trigger_deploy_webhook()
