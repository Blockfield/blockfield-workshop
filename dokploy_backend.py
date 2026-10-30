"""Workshop lifecycle without docker.sock or a Dokploy API token.

Mechanism (owner decision 2026-10-30): the Workshop Dokploy app points at a
fixed GHCR tag (``<repository>:approved``). After the controller verifies a
published immutable release (see releases.py), this backend triggers a
workflow_dispatch in the repository that owns the image package, passing the
verified manifest digest; that workflow retags ``:approved`` with its own
GITHUB_TOKEN, and the controller polls the workflow run to success before
triggering the Workshop app's deploy webhook with bounded retry. Start/stop/
save/status go through direct private-network RCON plus the mod's READY
file. No docker CLI, no Dokploy API token, no package-scoped registry token:
the controller holds only a single-repository fine-grained PAT (Actions
scope) that cannot touch any package directly.

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

READY_FILE = ".blockfield-ready.json"
UNKNOWN_SESSION = "00000000-0000-0000-0000-000000000000"


class RegistryError(RuntimeError):
    """Approved-tag move failed; the message never carries a credential."""


class WebhookError(RuntimeError):
    """Deploy webhook trigger failed; the message never carries the URL."""


class RconError(RuntimeError):
    """Private-network RCON command failed; never carries the password."""


def read_secret(
    *,
    value: str | None,
    env: str,
    file_env: str,
    what: str,
    error: type = RegistryError,
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


class WorkflowTagMover:
    """Move a fixed :approved tag via a workflow_dispatch in the image's repo.

    The credential (``token`` / ``BF_WORKSHOP_TAG_TOKEN`` /
    ``BF_WORKSHOP_TAG_TOKEN_FILE``) is a fine-grained PAT restricted to the
    single repository that owns the image package, with Actions read/write
    only (no package, org or other repo scope). move() validates the digest,
    dispatches the retag workflow with it, then polls the workflow run to
    success; the workflow itself verifies the digest belongs to a published
    verified release, retags :approved with its own GITHUB_TOKEN and reads
    the tag back. Direct registry reads are impossible here (the package is
    private and fine-grained PATs carry no package scope), so a successful
    run conclusion is the confirmation. The token travels in one
    Authorization header per call and never appears in errors or logs.
    """

    API = "https://api.github.com"

    def __init__(
        self,
        repository: str,
        *,
        owner_repo: str = "Blockfield/blockfield-server",
        workflow: str = "move-approved-tag.yml",
        ref: str = "main",
        token: str | None = None,
        http=http_request,
        timeout: float = 15.0,
        poll_interval: float = 10.0,
        confirm_timeout: float = 600.0,
        dispatch_attempts: int = 3,
        dispatch_delay: float = 1.0,
        sleep=time.sleep,
        clock=time.monotonic,
    ):
        _registry_base(repository)  # allowlist shape for digest validation
        parts = owner_repo.split("/")
        if len(parts) != 2 or not all(
            part and part not in (".", "..") for part in parts
        ):
            raise ValueError("tag workflow needs <owner>/<repo>")
        if dispatch_attempts < 1:
            raise ValueError("dispatch needs at least one attempt")
        self.repository = repository
        self.owner_repo = owner_repo
        self.workflow = workflow
        self.ref = ref
        self._token = read_secret(
            value=token,
            env="BF_WORKSHOP_TAG_TOKEN",
            file_env="BF_WORKSHOP_TAG_TOKEN_FILE",
            what="tag workflow",
        )
        self._http = http
        self.timeout = timeout
        self.poll_interval = poll_interval
        self.confirm_timeout = confirm_timeout
        self.dispatch_attempts = dispatch_attempts
        self.dispatch_delay = dispatch_delay
        self._sleep = sleep
        self._clock = clock

    def _call(self, method: str, path: str, body: bytes | None = None) -> HttpResponse:
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self._token}",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        try:
            return self._http(
                method,
                f"{self.API}/repos/{self.owner_repo}{path}",
                headers,
                body,
                self.timeout,
            )
        except Exception as error:
            raise RegistryError(
                f"tag workflow {method} failed: {type(error).__name__}"
            ) from error

    def _dispatch(self, digest: str, nonce: str) -> None:
        """POST the dispatch; retry transport errors/5xx, fail fast on 4xx."""
        body = json.dumps(
            {"ref": self.ref, "inputs": {"digest": digest, "nonce": nonce}}
        ).encode()
        last: Exception | None = None
        path = f"/actions/workflows/{self.workflow}/dispatches"
        for attempt in range(self.dispatch_attempts):
            try:
                reply = self._call("POST", path, body)
            except RegistryError as error:
                last = error
                reply = None
            else:
                if reply.status == 204:
                    return
                if 400 <= reply.status < 500:
                    raise RegistryError(
                        f"tag workflow dispatch rejected ({reply.status})"
                    )
                last = RegistryError(f"tag workflow dispatch answered ({reply.status})")
            if attempt + 1 < self.dispatch_attempts:
                self._sleep(self.dispatch_delay * 2**attempt)
        raise RegistryError(
            "tag workflow dispatch failed after "
            f"{self.dispatch_attempts} attempts: "
            f"{type(last).__name__ if last else 'unknown'}"
        ) from last

    def _find_run(self, nonce: str) -> dict | None:
        """Newest dispatch run carrying our nonce, or None while queued."""
        reply = self._call(
            "GET",
            f"/actions/workflows/{self.workflow}/runs"
            "?event=workflow_dispatch&per_page=20",
        )
        if reply.status != 200:
            raise RegistryError(f"tag workflow poll failed ({reply.status})")
        try:
            runs = json.loads(reply.body).get("workflow_runs", [])
        except ValueError as error:
            raise RegistryError("tag workflow status unreadable") from error
        for run in runs:
            # The nonce is a fresh uuid4 per move(), so a name match is
            # unambiguous without comparing clocks.
            if nonce in str(run.get("name", "")):
                return run
        return None

    def move(self, digest: str) -> str:
        """Point :approved at ``digest`` (``<repo>@sha256:<64hex>``).

        Dispatches the retag workflow and waits for its successful run; the
        workflow does the actual retag and read-back verification. A
        re-dispatch of the same digest converges on the same tag. Returns
        the confirmed ``sha256:<hex>`` digest.
        """
        match = re.fullmatch(
            re.escape(self.repository) + r"@sha256:([0-9a-f]{64})", digest
        )
        if match is None:
            # Never accept :latest, a foreign registry or a bare image ID here.
            raise ValueError("approved target needs this repository's digest")
        want = f"sha256:{match.group(1)}"
        nonce = uuid4().hex
        self._dispatch(digest, nonce)
        deadline = self._clock() + self.confirm_timeout
        while True:
            run = self._find_run(nonce)
            if run is not None and run.get("status") == "completed":
                if run.get("conclusion") == "success":
                    return want
                raise RegistryError(f"tag workflow ended: {run.get('conclusion')}")
            if self._clock() >= deadline:
                raise RegistryError("tag workflow did not confirm the tag move in time")
            self._sleep(self.poll_interval)


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
            error=WebhookError,
        )
        from urllib.parse import urlsplit

        parts = urlsplit(self._url)
        host = parts.hostname or ""
        loopback = host == "localhost" or host == "::1" or host.startswith("127.")
        if parts.scheme != "https" and not (parts.scheme == "http" and loopback):
            # The secret URL must not cross the wire in cleartext; plain http
            # is only acceptable for loopback test doubles.
            raise ValueError("deploy webhook URL must be https")
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
        mover: WorkflowTagMover,
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

        releases_path = env.get("BF_RELEASES_FILE")
        return cls(
            data=Path(env.get("BF_WORKSHOP_DATA", "/srv/minecraft/workshop/data")),
            lock_dir=Path(env.get("BF_LOCK_DIR", "/srv/minecraft/backend-lock")),
            mover=WorkflowTagMover(
                env.get(
                    "BF_WORKSHOP_APPROVED_REPOSITORY",
                    release_catalog.SERVER_REPOSITORY,
                ),
                owner_repo=env.get(
                    "BF_WORKSHOP_TAG_REPO", "Blockfield/blockfield-server"
                ),
                workflow=env.get("BF_WORKSHOP_TAG_WORKFLOW", "move-approved-tag.yml"),
            ),
            webhook=DeployWebhook(),
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
        return [Process(goal, bool(alive and expected and ready == expected))]

    def save_and_freeze(self, process: Process) -> None:
        if process.goal.profile == "game":
            raise ValueError("Workshop controller cannot save or stop Game")
        with self._rcon() as rcon:
            result = rcon.command("save-all", "flush")
        if "Saved the game" not in result:
            raise RuntimeError(f"save-all did not confirm success: {result[:300]}")

    def _stop_and_wait(self, timeout_message: str) -> None:
        """RCON stop, then wait dark. Shutdown tears down the reply path, so
        a mid-command RCON error only matters if the backend stays alive;
        connect/auth errors still raise immediately."""
        error: RconError | None = None
        with self._rcon() as rcon:
            try:
                rcon.command("stop")
            except RconError as exc:
                error = exc
        deadline = self._clock() + self.stop_timeout
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

        Webhook triggers complete (bounded retry) before start() returns, so
        none of ours is in flight here; stop anything still alive and wait
        until the liveness probe goes quiet. Unknown states raise.
        """
        if self._webhook_inflight:
            raise RuntimeError("a deploy trigger is still in flight")
        if self._alive():
            self._stop_and_wait("cannot fence the workshop backend")

    def _operation_id(self, goal: Goal) -> str:
        """Generation for this start; a retry reuses the persisted intent's."""
        # A retry with a rotated candidate/modeId mints a fresh generation:
        # safe, the tag move is to the same approved release digest and the
        # webhook dedupes each generation key convergently.
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

    def start(self, goal: Goal) -> None:
        self.preflight(goal)
        digest = self._digest(goal.release)
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
        # Idempotent rollout: the generation persists in profile.json before
        # the tag move, so a retried start() reuses it and the webhook dedupes
        # on the same key across retries, not just within one trigger() call.
        self.mover.move(digest)
        self._webhook_inflight += 1
        try:
            self.webhook.trigger(document["generation"])
        finally:
            self._webhook_inflight -= 1
