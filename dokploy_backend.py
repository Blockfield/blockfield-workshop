"""Workshop lifecycle without docker.sock or a Dokploy API token.

Mechanism (owner decision 2026-10-30): the Workshop Dokploy app points at a
fixed GHCR tag (``<repository>:approved``). After the controller verifies a
published immutable release (see releases.py), this backend moves that tag to
the verified manifest digest with a package-scoped credential, triggers the
Workshop app's deploy webhook with bounded retry, and drives
start/stop/save/status through direct private-network RCON plus the mod's
READY file. No docker CLI, no Dokploy API token.

The old DockerBackend stays authoritative (see docs/OPERATIONS.md); this
module is staged beside it and shares the controller Backend protocol, the
profile.json selection schema and the READY-file contract.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
import re
import shutil
import socket
import struct
import time
from pathlib import Path
from uuid import UUID, uuid4

from controller import Goal, Process, atomic_write

APPROVED_TAG = "approved"
READY_FILE = ".blockfield-ready.json"
UNKNOWN_SESSION = "00000000-0000-0000-0000-000000000000"
DIGEST = re.compile(r"[0-9a-f]{64}")

# OCI manifest media types the registry may serve for a server release.
MANIFEST_TYPES = (
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
)


class RegistryError(RuntimeError):
    """GHCR tag move failed; the message never carries a credential."""


class WebhookError(RuntimeError):
    """Deploy webhook trigger failed; the message never carries the URL."""


class RconError(RuntimeError):
    """Private-network RCON command failed; never carries the password."""


def read_secret(*, value: str | None, env: str, file_env: str, what: str) -> str:
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
        except OSError as error:
            raise RegistryError(f"{what} secret file unreadable") from error
        if secret:
            return secret
    raise RegistryError(f"{what} credential is not configured")


@dataclass
class HttpResponse:
    status: int
    headers: dict
    body: bytes


def http_request(
    method: str, url: str, headers: dict, body: bytes | None, timeout: float
) -> HttpResponse:
    """Default HTTPS transport (stdlib urllib); tests inject a fake."""
    import urllib.error
    import urllib.request

    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as reply:
            return HttpResponse(
                reply.status,
                {k.lower(): v for k, v in reply.headers.items()},
                reply.read(8 << 20),
            )
    except urllib.error.HTTPError as error:
        return HttpResponse(
            error.code,
            {k.lower(): v for k, v in (error.headers or {}).items()},
            error.read(65536),
        )


def _registry_base(repository: str) -> tuple[str, str]:
    """Split ghcr.io/<owner>/<image> into (manifest URL base, repository)."""
    parts = repository.split("/")
    if (
        len(parts) == 3
        and parts[0] == "ghcr.io"
        and all(part and part not in (".", "..") for part in parts[1:])
    ):
        return f"https://ghcr.io/v2/{parts[1]}/{parts[2]}", repository
    raise ValueError("repository must be ghcr.io/<owner>/<image>")


class GhcrTagMover:
    """Move a fixed :approved tag to a verified release digest (registry API).

    The credential (``token`` / ``BF_GHCR_PACKAGE_TOKEN`` /
    ``BF_GHCR_PACKAGE_TOKEN_FILE``) is scoped to write ONLY the approved-tag
    package. It travels in one Authorization header and never appears in
    errors or logs.
    """

    def __init__(
        self,
        repository: str,
        *,
        token: str | None = None,
        http=http_request,
        timeout: float = 15.0,
    ):
        self.base, self.repository = _registry_base(repository)
        self._token = read_secret(
            value=token,
            env="BF_GHCR_PACKAGE_TOKEN",
            file_env="BF_GHCR_PACKAGE_TOKEN_FILE",
            what="GHCR package",
        )
        self._http = http
        self.timeout = timeout

    def _call(
        self,
        method: str,
        reference: str,
        body: bytes | None = None,
        content_type: str | None = None,
    ) -> HttpResponse:
        headers = {
            "Accept": ", ".join(MANIFEST_TYPES),
            "Authorization": f"Bearer {self._token}",
        }
        if body is not None:
            headers["Content-Type"] = content_type or MANIFEST_TYPES[1]
        try:
            return self._http(
                method,
                f"{self.base}/manifests/{reference}",
                headers,
                body,
                self.timeout,
            )
        except Exception as error:
            raise RegistryError(
                f"registry {method} failed: {type(error).__name__}"
            ) from error

    def current(self) -> str | None:
        """Digest the :approved tag points at now; None when the tag is absent."""
        reply = self._call("GET", APPROVED_TAG)
        if reply.status == 404:
            return None
        if reply.status != 200:
            raise RegistryError(f"registry observe failed ({reply.status})")
        digest = reply.headers.get("docker-content-digest", "")
        if not DIGEST.fullmatch(digest.removeprefix("sha256:")):
            raise RegistryError("registry returned an unparseable digest")
        return digest

    def move(self, digest: str) -> str:
        """Point :approved at ``digest`` (``<repo>@sha256:<64hex>``); idempotent.

        Verifies the release manifest exists, copies its exact bytes to the
        tag, then reads the tag back and refuses a mismatch. Returns the
        confirmed ``sha256:<hex>`` digest.
        """
        match = re.fullmatch(
            re.escape(self.repository) + r"@sha256:([0-9a-f]{64})", digest
        )
        if match is None:
            # Never accept :latest, a foreign registry or a bare image ID here.
            raise ValueError("approved target needs this repository's digest")
        want = f"sha256:{match.group(1)}"
        if self.current() == want:
            return want
        fetched = self._call("GET", want)
        if fetched.status != 200:
            raise RegistryError(f"release manifest unreadable ({fetched.status})")
        media = fetched.headers.get("content-type", "").split(";")[0].strip()
        if media not in MANIFEST_TYPES:
            raise RegistryError("release has an unexpected manifest type")
        # shortcut: GHCR serves small manifests; upgrade to ranged fetch when images need it.
        stored = self._call("PUT", APPROVED_TAG, fetched.body, media)
        if stored.status not in (200, 201):
            raise RegistryError(f"tag move rejected ({stored.status})")
        if self.current() != want:
            raise RegistryError("tag moved but verification read back another digest")
        return want


class DeployWebhook:
    """Trigger the Workshop app's deploy webhook (redeploy only, bounded retry).

    The URL (``url`` / ``BF_WORKSHOP_DEPLOY_WEBHOOK_URL`` /
    ``BF_WORKSHOP_DEPLOY_WEBHOOK_URL_FILE``) is a Dokploy-held secret and
    never appears in errors or logs. Every attempt carries
    ``Idempotency-Key: <operation id>`` so a retried trigger cannot queue a
    second deploy.
    """

    def __init__(
        self,
        *,
        url: str | None = None,
        http=http_request,
        attempts: int = 5,
        delay: float = 1.0,
        timeout: float = 15.0,
        sleep=time.sleep,
    ):
        if attempts < 1:
            raise ValueError("webhook needs at least one attempt")
        self._url = read_secret(
            value=url,
            env="BF_WORKSHOP_DEPLOY_WEBHOOK_URL",
            file_env="BF_WORKSHOP_DEPLOY_WEBHOOK_URL_FILE",
            what="deploy webhook",
        )
        self._http = http
        self.attempts = attempts
        self.delay = delay
        self.timeout = timeout
        self._sleep = sleep

    def trigger(self, operation_id: str) -> None:
        """POST the redeploy hook; retry timeouts/5xx, fail fast on 4xx."""
        body = json.dumps({"operation": operation_id}).encode()
        last: Exception | None = None
        for attempt in range(self.attempts):
            try:
                reply = self._http(
                    "POST",
                    self._url,
                    {
                        "Content-Type": "application/json",
                        "Idempotency-Key": operation_id,
                    },
                    body,
                    self.timeout,
                )
            except Exception as error:
                last = error
                reply = None
            else:
                if 200 <= reply.status < 300:
                    return
                if 400 <= reply.status < 500:
                    raise WebhookError(f"deploy webhook rejected ({reply.status})")
                last = WebhookError(f"deploy webhook answered ({reply.status})")
            if attempt + 1 < self.attempts:
                self._sleep(self.delay * 2**attempt)
        raise WebhookError(
            "deploy webhook failed after "
            f"{self.attempts} attempts: {type(last).__name__ if last else 'unknown'}"
        ) from last


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
        )
        self.timeout = timeout
        self._socket: socket.socket | None = None

    def _packet(self, kind: int, body: bytes) -> bytes:
        return struct.pack("<iii", 4 + 4 + len(body) + 2, 1, kind) + body + b"\x00\x00"

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
            self._socket.sendall(self._packet(3, self._password.encode()))
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
        """Run one trusted console command; at most 64 response packets."""
        if self._socket is None:
            self.connect()
        try:
            assert self._socket is not None
            self._socket.sendall(self._packet(2, " ".join(words).encode()))
            self._socket.sendall(self._packet(2, b""))
            parts = []
            for _ in range(64):
                request_id, body = self._read_packet()
                if request_id == -1:
                    raise RconError("RCON session was refused mid-command")
                if body == b"":
                    # The trailing empty packet echoes back once output drained.
                    break
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
    """Workshop-only lifecycle on the approved-tag mechanism.

    Observes the mod's READY file plus an RCON liveness probe (TCP connect,
    no auth), saves through ``save-all flush``, stops through RCON ``stop``,
    and (re)starts through tag move + deploy webhook. Never touches Game:
    observe() reports Workshop/test only, so Game reads as unavailable.
    """

    def __init__(
        self,
        *,
        data: Path,
        lock_dir: Path,
        mover: GhcrTagMover,
        webhook: DeployWebhook,
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
        self.mover = mover
        self.webhook = webhook
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
        self._webhook_inflight = 0

    @classmethod
    def from_env(cls, env=os.environ, **overrides) -> "DokployBackend":
        """Build from BF_ env; secrets stay in env vars or secret files."""
        import releases as release_catalog

        return cls(
            data=Path(env.get("BF_WORKSHOP_DATA", "/srv/minecraft/workshop/data")),
            lock_dir=Path(env.get("BF_LOCK_DIR", "/srv/minecraft/backend-lock")),
            mover=GhcrTagMover(
                env.get(
                    "BF_WORKSHOP_APPROVED_REPOSITORY",
                    release_catalog.SERVER_REPOSITORY,
                )
            ),
            webhook=DeployWebhook(),
            rcon_host=env.get("BF_WORKSHOP_RCON_HOST", "blockfield-workshop"),
            rcon_port=int(env.get("BF_WORKSHOP_RCON_PORT", "25575")),
            **overrides,
        )

    @staticmethod
    def app_of(profile: str) -> str:
        return "workshop" if profile == "test" else profile

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

    def observe(self) -> list[Process]:
        try:
            selected = self.selection()
        except (ValueError, TypeError, AttributeError, OSError):
            selected = None
            corrupt = True
        else:
            corrupt = False
        if not self._alive():
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
        return [Process(goal, bool(self._alive() and expected and ready == expected))]

    def save_and_freeze(self, process: Process) -> None:
        if process.goal.profile == "game":
            raise ValueError("Workshop controller cannot save or stop Game")
        with self._rcon() as rcon:
            result = rcon.command("save-all", "flush")
        if "Saved the game" not in result:
            raise RuntimeError(f"save-all did not confirm success: {result[:300]}")

    def stop(self, process: Process) -> None:
        if process.goal.profile == "game":
            raise ValueError("Workshop controller cannot save or stop Game")
        self.save_and_freeze(process)
        with self._rcon() as rcon:
            rcon.command("stop")
        deadline = self._clock() + self.stop_timeout
        while self._alive():
            if self._clock() >= deadline:
                raise TimeoutError("workshop still running after RCON stop")
            self._sleep(self.poll)

    def fence_pending_starts(self) -> None:
        """No delayed old request may later start a backend when this returns.

        Webhook triggers complete (bounded retry) before start() returns, so
        none of ours is in flight here; stop anything still alive and wait
        until the liveness probe goes quiet. Unknown states raise.
        """
        if self._webhook_inflight:
            raise RuntimeError("a deploy trigger is still in flight")
        if self._alive():
            with self._rcon() as rcon:
                rcon.command("stop")
        deadline = self._clock() + self.stop_timeout
        while self._alive():
            if self._clock() >= deadline:
                raise TimeoutError("cannot fence the workshop backend")
            self._sleep(self.poll)

    def start(self, goal: Goal) -> None:
        self.preflight(goal)
        digest = self._digest(goal.release)
        if goal.profile == "test" and self.maps is not None:
            self.maps.extract(goal.candidate, self.data / "test-candidate")
        document = {
            "schema": 1,
            "profile": goal.profile,
            "generation": str(uuid4()),
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
        # Idempotent rollout: a retried operation finds the tag already moved
        # and the webhook dedupes on the generation key.
        self.mover.move(digest)
        self._webhook_inflight += 1
        try:
            self.webhook.trigger(document["generation"])
        finally:
            self._webhook_inflight -= 1

    def wait_ready(self, generation: str, *, timeout: float = 600.0) -> dict:
        """Poll until this generation reports READY; TimeoutError on deadline."""
        deadline = self._clock() + timeout
        while True:
            ready = self.readiness()
            if ready is not None and ready.get("generation") == generation:
                selected = self.selection()
                if (
                    selected is not None
                    and all(
                        selected.get(key) == ready.get(key)
                        for key in ("profile", "generation", "session", "release")
                    )
                    and self._alive()
                ):
                    return ready
            if self._clock() >= deadline:
                raise TimeoutError("workshop did not report READY for this generation")
            self._sleep(self.poll)
