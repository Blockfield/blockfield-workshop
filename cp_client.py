"""Control-plane v2 subscriber for the Workshop controller (units CP-S6a/S6b).

Consumes the frozen v2 wire contract (control-plane origin/main ``6df1f95``
+ ``cf660e4``, ``docs/V2.md`` "Private delivery"): target snapshot,
per-consumer SSE events, explicit ACKs. The wire (endpoints, envelope
fields, ACK states ``received -> validated -> applied -> effective`` with
terminal ``rejected``/``unknown``) is fixed by CP; this module only
interprets the Workshop target payload (the existing inbox kinds).

Every Workshop operation (coordination#64) arrives here: the site, proxy and
release CI talk only to CP. ``run_stream`` holds the agent SSE stream open
in a thread and queues each frame as it arrives, waking the controller loop
at once (push, no polling); ``sync`` applies and ACKs the queued frames on
the loop thread. ``StateReporter`` sends the controller's documents (status,
catalogs, operation results) back to CP for the site and the release CI.

The snapshot carries target-scoped settings, not a Workshop ACL: membership
is checked against the locally stored members, which only CP members
operations (or the operator bootstrap) can change. Release records arrive
inside the release.select operation itself and are verified against the
local catalog.

Trust model: frames arrive over the mutually authenticated CP channel
(TLS + audience-bound bearer pinned to this agent id), so the payload
actor/grantor is CP-attested. The payload hash is verified over canonical
JSON (sort_keys, compact separators); the CP-side Workshop producer must
hash the same canonical form when committing the outbox row.

Disabled unless ``BLOCKFIELD_CP_URL``, ``BLOCKFIELD_CP_AGENT_ID`` and
``BLOCKFIELD_CP_AGENT_TOKEN`` are all set. Release records arrive in the
release.select operation and are verified against the local catalog before
merging.
"""

from __future__ import annotations

from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import ipaddress
import json
import os
import re
import socket
import sys
import threading
import time
import uuid
import urllib.request
import urllib.error
import urllib.parse

from controller import CommitIncompleteError, CP_RELEASE_LIMIT, member

AGENT_PATH = "/v2/agents/{agent}"
SNAPSHOT_TTL_S = 3600
EVENT_TIMEOUT_S = 5.0
# CP heartbeats every 10 s: a silent stream past this is dead, reconnect.
STREAM_IDLE_S = 35.0
STREAM_RETRY_MAX_S = 30.0
# Map publish frames carry merge resolutions (CP caps the request at 1 MiB).
MAX_FRAME_BYTES = 2 << 20
MAX_ACL_ENTRIES = 4096
MAX_ACL_ENTRY_BYTES = 256
MAX_SNAPSHOT_BYTES = MAX_ACL_ENTRIES * MAX_ACL_ENTRY_BYTES
RECEIPT_LIMIT = 500


class SnapshotRequired(Exception):
    """Stream epoch/cursor moved on: refetch the snapshot before applying."""


class CPHttpError(ValueError):
    """CP answered non-2xx: carries the status so callers can branch on it."""

    def __init__(self, what: str, status: int):
        super().__init__(f"cp {what} failed: http {status}")
        self.status = status


# A DNS label that starts with a letter: never a numeric IPv4 form like 134744072.
_SERVICE_NAME = re.compile(r"[a-z]([a-z0-9-]*[a-z0-9])?")


# RFC 6598 shared space: Python calls it neither private nor global.
_SHARED_SPACE = ipaddress.ip_network("100.64.0.0/10")


def _resolves_privately(host: str) -> bool:
    # A search domain could map a bare name to a public host; plaintext is only
    # for names that resolve to overlay/loopback addresses alone.
    # An unresolvable name is a transient start-up failure (OSError, the
    # orchestrator restarts us), not a configuration error.
    infos = socket.getaddrinfo(host, None)
    addresses = {ipaddress.ip_address(info[4][0].split("%")[0]) for info in infos}
    return bool(addresses) and all(
        a.is_private or a.is_loopback or a in _SHARED_SPACE for a in addresses
    )


def _check_cp_url(base: str) -> None:
    # CP has no public domain: prod reaches it over the private Docker overlay by
    # its single-label service name, like the game server and proxy do. A dotted
    # host could resolve publicly, so plaintext stays refused for it.
    parts = urllib.parse.urlsplit(base)
    host = parts.hostname or ""
    if parts.scheme.lower() == "https":
        return
    if parts.scheme.lower() == "http" and (
        host in ("127.0.0.1", "localhost", "::1")
        or (_SERVICE_NAME.fullmatch(host) and _resolves_privately(host))
    ):
        return
    raise ValueError(
        "BLOCKFIELD_CP_URL must be https (http only for loopback or an internal service name)"
    )


@dataclass(frozen=True)
class CPConfig:
    base_url: str
    agent_id: str
    token: str
    generation: str = ""
    poll_timeout_s: float = EVENT_TIMEOUT_S

    @classmethod
    def from_env(cls, env=os.environ) -> "CPConfig | None":
        base = (env.get("BLOCKFIELD_CP_URL") or "").strip().rstrip("/")
        agent = (env.get("BLOCKFIELD_CP_AGENT_ID") or "").strip()
        token = env.get("BLOCKFIELD_CP_AGENT_TOKEN") or ""
        if not base or not agent or not token:
            return None
        _check_cp_url(base)
        try:
            timeout = float(env.get("BLOCKFIELD_CP_POLL_TIMEOUT_S", EVENT_TIMEOUT_S))
        except (TypeError, ValueError):
            raise ValueError("BLOCKFIELD_CP_POLL_TIMEOUT_S must be a number")
        if not 0 < timeout <= 60:
            raise ValueError("BLOCKFIELD_CP_POLL_TIMEOUT_S must be within (0, 60]")
        return cls(
            base_url=base,
            agent_id=agent,
            token=token,
            generation=(env.get("BLOCKFIELD_CP_GENERATION") or "").strip(),
            poll_timeout_s=timeout,
        )

    def snapshot_url(self) -> str:
        return self.base_url + AGENT_PATH.format(agent=self.agent_id) + "/snapshot"

    def events_url(self, epoch: str, cursor: int) -> str:
        return (
            self.base_url
            + AGENT_PATH.format(agent=self.agent_id)
            + f"/events?epoch={epoch}&cursor={cursor}"
        )

    def acks_url(self) -> str:
        return self.base_url + AGENT_PATH.format(agent=self.agent_id) + "/acks"


def canonical(payload: object) -> bytes:
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()


def payload_hash(payload: object) -> str:
    return hashlib.sha256(canonical(payload)).hexdigest()


def _need(mapping: dict, key: str, types: tuple, what: str):
    value = mapping.get(key)
    if not isinstance(value, types) or (isinstance(value, str) and not value):
        raise ValueError(f"invalid cp frame: {what}")
    return value


def check_frame(
    frame: dict, *, agent_id: str, epoch: str, generation: str, now_ms: int
) -> dict:
    """Validate one SSE envelope against the frozen v2 fields.

    Returns the typed operation payload. Raises SnapshotRequired when the
    stream epoch or generation moved on, ValueError for anything that must
    never execute.
    """
    if not isinstance(frame, dict):
        raise ValueError("invalid cp frame: not an object")
    audience = _need(frame, "audience", (str,), "audience")
    if audience != agent_id:
        raise ValueError("cp frame addressed elsewhere")
    frame_epoch = _need(frame, "epoch", (str,), "epoch")
    if frame_epoch != epoch:
        raise SnapshotRequired("cp stream epoch moved on")
    sequence = frame.get("sequence")
    if type(sequence) is not int or sequence < 1:
        raise ValueError("invalid cp frame: sequence")
    _need(frame, "operationId", (str,), "operationId")
    frame_generation = _need(frame, "generation", (str,), "generation")
    if frame_generation != generation:
        raise SnapshotRequired("cp stream generation moved on")
    revision = frame.get("revision")
    if revision is not None and type(revision) is not int:
        raise ValueError("invalid cp frame: revision")
    try:
        expires_at = datetime.fromisoformat(frame["expiresAt"])
        if expires_at.tzinfo is None:
            raise ValueError("naive expiry")
        expires_ms = int(expires_at.timestamp() * 1000)
    except (KeyError, TypeError, ValueError):
        raise ValueError("invalid cp frame: expiresAt")
    if now_ms > expires_ms:
        raise ValueError("cp operation expired")
    want = _need(frame, "payloadHash", (str,), "payloadHash")
    operation = frame.get("payload")
    if not isinstance(operation, dict):
        raise ValueError("invalid cp frame: payload")
    if payload_hash(operation) != want:
        raise ValueError("cp payload hash mismatch")
    return operation


def _read_capped(response, cap: int, what: str) -> bytes:
    """Read a whole document to EOF under an explicit cap.

    Oversize is an error, never silent truncation: a cut snapshot would fail
    downstream as invalid JSON and wedge the stream on every refresh.
    """
    body = response.read(cap + 1)
    if len(body) > cap:
        raise ValueError(f"cp {what} exceeds {cap} bytes")
    return body


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects: following one would forward the agent bearer."""

    def _refuse(self, req, fp, code, msg, headers):
        raise urllib.error.HTTPError(
            req.get_full_url(), code, f"cp refuses redirect ({code})", headers, fp
        )

    http_error_301 = _refuse
    http_error_302 = _refuse
    http_error_303 = _refuse
    http_error_307 = _refuse
    http_error_308 = _refuse
    https_error_301 = _refuse
    https_error_302 = _refuse
    https_error_303 = _refuse
    https_error_307 = _refuse
    https_error_308 = _refuse


def _request_body(opener, request, timeout: float, what: str, cap: int) -> bytes:
    """One helper for whole-document CP calls: any non-2xx raises CPHttpError,
    so no caller can mistake a failed call for a delivered one."""
    try:
        with opener.open(request, timeout=timeout) as response:
            body = _read_capped(response, cap, what)
            status = response.status
    except urllib.error.HTTPError as error:
        try:
            error.close()
        except Exception:
            pass
        raise CPHttpError(what, error.code) from error
    if not 200 <= status <= 299:
        raise CPHttpError(what, status)
    return body


class _UrllibTransport:
    def __init__(self, token: str):
        self.token = token
        # A plaintext (overlay) bearer must never go through HTTP_PROXY; an
        # HTTPS CP keeps the environment's HTTPS proxy.
        proxies = {k: v for k, v in urllib.request.getproxies().items() if k != "http"}
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler(proxies), _NoRedirect
        )

    def _headers(self, accept: str) -> dict:
        return {"Authorization": f"Bearer {self.token}", "Accept": accept}

    def get_json(
        self, url: str, timeout: float, *, cap: int = MAX_SNAPSHOT_BYTES
    ) -> bytes:
        request = urllib.request.Request(url, headers=self._headers("application/json"))
        return _request_body(self.opener, request, timeout, "snapshot", cap)

    def post_json(self, url: str, body: dict, timeout: float) -> bytes:
        data = canonical(body)
        request = urllib.request.Request(
            url,
            data=data,
            headers={
                **self._headers("application/json"),
                "Content-Type": "application/json",
            },
            method="POST",
        )
        return _request_body(self.opener, request, timeout, "acks", MAX_FRAME_BYTES)

    def patch_json(self, url: str, body: dict, timeout: float) -> bytes:
        data = canonical(body)
        request = urllib.request.Request(
            url,
            data=data,
            headers={
                **self._headers("application/json"),
                "Content-Type": "application/json",
            },
            method="PATCH",
        )
        return _request_body(self.opener, request, timeout, "gate", MAX_FRAME_BYTES)

    def put_json(self, url: str, body: dict, timeout: float) -> bytes:
        request = urllib.request.Request(
            url,
            data=canonical(body),
            headers={
                **self._headers("application/json"),
                "Content-Type": "application/json",
            },
            method="PUT",
        )
        return _request_body(self.opener, request, timeout, "state", MAX_FRAME_BYTES)

    def stream_events(self, url: str, idle_s: float):
        """Yield frames from one long-lived SSE connection until it ends."""
        request = urllib.request.Request(
            url, headers=self._headers("text/event-stream")
        )
        try:
            response = self.opener.open(request, timeout=idle_s)
        except urllib.error.HTTPError as error:
            if error.code == 409:
                raise SnapshotRequired("cp snapshot required") from error
            try:
                error.close()
            except Exception:
                pass
            raise CPHttpError("events", error.code) from error
        with response:
            if not 200 <= response.status <= 299:
                raise CPHttpError("events", response.status)
            yield from _iter_sse(response)


def _iter_sse(response):
    """Yield SSE data blocks as JSON objects until the stream ends.

    A socket timeout (no heartbeat within the idle window), a resync event
    or an undecodable data block ends the stream: the caller reconnects from
    the durable cursor, which heals transient truncation (the cursor never
    jumps a hole). Lines carry no per-line limit; a single data block past
    MAX_FRAME_BYTES is an explicit error, never silent truncation.
    """
    data: list[str] = []
    data_len = 0
    event = ""
    while True:
        try:
            line = response.readline()
        except TimeoutError:
            return
        if not line:
            return
        text = line.decode("utf-8", "replace").rstrip("\r\n")
        if text == "":
            if event == "resync":
                return
            if data:
                try:
                    document = json.loads("\n".join(data))
                except ValueError:
                    return
                if isinstance(document, dict):
                    yield document
            data, data_len, event = [], 0, ""
        elif text.startswith("data:"):
            data.append(text[5:].lstrip())
            data_len += len(line)
            if data_len > MAX_FRAME_BYTES:
                raise ValueError(f"cp frame exceeds {MAX_FRAME_BYTES} bytes")
        elif text.startswith("event:"):
            event = text[6:].strip()
        # "id:" (epoch:sequence) and retry/comments carry no payload here.


class GateReporter:
    """Reports the controller gate leg to CP (unit CPP3).

    Reads the current (gate, workshop) settings, PATCHes only when the leg
    differs (idempotent, no write when unchanged). A 409 (someone else
    reported meanwhile) refetches once and retries against the fresh
    revision. Transport failures raise; callers must not let a failed
    report block reconciliation. Disabled unless BLOCKFIELD_CP_GATE_TOKEN
    (a service credential holding the workshop.gate scope) is set.
    """

    path = "/v2/settings/gate/workshop"

    def __init__(
        self,
        base_url: str,
        token: str,
        transport=None,
        timeout_s: float = EVENT_TIMEOUT_S,
    ):
        self.base_url = base_url.rstrip("/")
        self.transport = transport or _UrllibTransport(token)
        self.timeout_s = timeout_s

    @classmethod
    def from_env(cls, env=os.environ, base_url=None):
        token = (env.get("BLOCKFIELD_CP_GATE_TOKEN") or "").strip()
        if not token:
            return None
        base = (base_url or env.get("BLOCKFIELD_CP_URL") or "").strip().rstrip("/")
        if not base:
            raise ValueError("BLOCKFIELD_CP_URL must be set for gate reporting")
        _check_cp_url(base)
        return cls(base, token)

    def _url(self) -> str:
        return self.base_url + self.path

    def read(self) -> tuple[int, dict]:
        """(revision, value) of the current leg; raises on transport failure."""
        try:
            raw = self.transport.get_json(self._url(), timeout=self.timeout_s)
        except CPHttpError as error:
            if error.status in (401, 403):
                raise ValueError(
                    "cp gate report denied: check gate credential"
                ) from error
            raise
        try:
            document = json.loads(raw.decode())
        except ValueError:
            raise ValueError("cp gate settings is not JSON")
        if not isinstance(document, dict):
            raise ValueError("cp gate settings is not an object")
        revision = document.get("revision")
        value = document.get("value")
        if type(revision) is not int or not isinstance(value, dict):
            raise ValueError("cp gate settings misses revision/value")
        return revision, value

    def _patch(self, revision: int, leg: dict) -> dict:
        body = {
            "idempotencyKey": f"workshop-gate-{revision}-{payload_hash(leg)[:16]}",
            "environment": "prod",
            "backend": "game",
            "expectedRevision": revision,
            "patch": leg,
        }
        raw = self.transport.patch_json(self._url(), body, timeout=self.timeout_s)
        try:
            document = json.loads(raw.decode())
        except ValueError:
            raise ValueError("cp gate report answer is not JSON") from None
        if (
            not isinstance(document, dict)
            or type(document.get("revision")) is not int
            or not document.get("operationId")
        ):
            raise ValueError("cp gate report answer misses revision/operationId")
        return {
            "reported": True,
            "revision": document["revision"],
            "operationId": document["operationId"],
        }

    def report(self, leg: dict) -> dict:
        """PATCH the leg when it differs from CP; returns reported/revision."""
        revision, current = self.read()
        if current == leg:
            return {"reported": False, "revision": revision}
        try:
            return self._patch(revision, leg)
        except CPHttpError as error:
            if error.status != 409:
                raise
            revision, current = self.read()
            if current == leg:
                return {"reported": False, "revision": revision}
            return self._patch(revision, leg)


class StateReporter:
    """Pushes controller documents to CP (``/v2/workshop/state/<name>``) and
    reads/streams Workshop state documents (including ``game-desired``,
    ``game-ready``, and ``game-activity``).

    Rides the gate reporter's credential (scopes ``workshop.gate``,
    ``workshop.report``, and optionally ``workshop.read``/``workshop.game``):
    transport failures raise so the caller keeps state fail-safe and retries.
    """

    STATE_STREAM_NAMES = ("game-desired", "game-ready", "game-activity")

    def __init__(self, base_url: str, transport, timeout_s: float = EVENT_TIMEOUT_S):
        self.base_url = base_url.rstrip("/")
        self.transport = transport
        self.timeout_s = timeout_s
        self._lock = threading.Lock()
        self._docs: dict[str, dict] = {}
        self._cursor = 0
        self._stream_live = False

    def _cache_doc(self, name: str, document: dict) -> None:
        if not isinstance(document, dict) or not isinstance(name, str) or not name:
            return
        with self._lock:
            self._docs[name] = dict(document)
            seq = document.get("seq")
            if type(seq) is int and seq > self._cursor:
                self._cursor = seq

    def put(self, name: str, value) -> None:
        url = self.base_url + "/v2/workshop/state/" + urllib.parse.quote(name, safe=":")
        raw = self.transport.put_json(url, {"value": value}, timeout=self.timeout_s)
        parsed = None
        if raw:
            try:
                parsed = json.loads(raw)
            except ValueError:
                parsed = None
        if isinstance(parsed, dict) and type(parsed.get("seq")) is int:
            self._cache_doc(
                name,
                {
                    "name": name,
                    "value": value,
                    "revision": parsed.get("revision"),
                    "seq": parsed["seq"],
                    "updatedAt": int(time.time() * 1000),
                },
            )
        else:
            with self._lock:
                self._docs[name] = {
                    "name": name,
                    "value": value,
                    "updatedAt": int(time.time() * 1000),
                }

    def get(self, names: tuple[str, ...] | list[str] | None = None) -> dict[str, dict]:
        query = ""
        if names:
            query = "?names=" + urllib.parse.quote(",".join(names), safe=":,")
        url = f"{self.base_url}/v2/workshop/state{query}"
        raw = self.transport.get_json(
            url, timeout=self.timeout_s, cap=MAX_FRAME_BYTES * 2
        )
        payload = json.loads(raw)
        documents = payload.get("documents") if isinstance(payload, dict) else None
        if not isinstance(documents, dict):
            raise ValueError("cp workshop state answer misses documents")
        with self._lock:
            if names is not None:
                for wanted in names:
                    if wanted not in documents:
                        self._docs.pop(wanted, None)
            for name, doc in documents.items():
                if isinstance(doc, dict):
                    self._docs[name] = dict(doc)
                    seq = doc.get("seq")
                    if type(seq) is int and seq > self._cursor:
                        self._cursor = seq
            if names is not None:
                return {k: dict(self._docs[k]) for k in names if k in self._docs}
            return {k: dict(v) for k, v in self._docs.items()}

    def doc(self, name: str, *, refresh: bool = False) -> dict | None:
        if refresh:
            return self.get((name,)).get(name)
        with self._lock:
            cached = self._docs.get(name)
            live = self._stream_live
        if cached is not None or live:
            return dict(cached) if cached is not None else None
        return self.get((name,)).get(name)

    def read_state_stream(self, wake: threading.Event | None = None) -> bool:
        """Follow ``/v2/workshop/state/events`` SSE and cache every state update."""
        self.get(self.STATE_STREAM_NAMES)
        with self._lock:
            after = self._cursor
            self._stream_live = True
        url = f"{self.base_url}/v2/workshop/state/events?after={after}"
        try:
            for item in self.transport.stream_events(url, STREAM_IDLE_S):
                if not isinstance(item, dict):
                    continue
                name = item.get("name")
                if not isinstance(name, str) or not name:
                    continue
                self._cache_doc(name, item)
                if wake is not None:
                    wake.set()
        finally:
            with self._lock:
                self._stream_live = False
        return True

    def run_state_stream(
        self, stop: threading.Event, wake: threading.Event | None = None
    ) -> None:
        """Hold the Workshop state SSE stream open until stop; reconnect with backoff."""
        delay = 1.0
        while not stop.is_set():
            try:
                if self.read_state_stream(wake=wake):
                    delay = 1.0
            except Exception as error:
                print(f"cp: state stream failed: {error!r}", file=sys.stderr)
            stop.wait(delay)
            delay = min(delay * 2, STREAM_RETRY_MAX_S)


class WorkshopGameClient:
    """Dispatches Workshop game lifecycle ops through CP and awaits SSE settlement."""

    def __init__(
        self,
        base_url: str,
        consumer_id: str,
        generation: str,
        transport,
        *,
        environment: str = "prod",
        backend: str = "game",
        timeout_s: float = EVENT_TIMEOUT_S,
        clock=time.monotonic,
    ):
        self.base_url = base_url.rstrip("/")
        self.consumer_id = consumer_id
        self.generation = str(uuid.UUID(generation))
        self.transport = transport
        self.environment = environment
        self.backend = backend
        self.timeout_s = timeout_s
        self._clock = clock

    @classmethod
    def from_env(
        cls, env=os.environ, base_url=None, transport=None
    ) -> "WorkshopGameClient | None":
        token = (env.get("BLOCKFIELD_CP_GATE_TOKEN") or "").strip()
        generation = (env.get("BLOCKFIELD_CP_WORKSHOP_GAME_GENERATION") or "").strip()
        consumer_id = (
            env.get("BLOCKFIELD_CP_WORKSHOP_GAME_CONSUMER_ID") or "ws-game"
        ).strip()
        base = (base_url or env.get("BLOCKFIELD_CP_URL") or "").strip().rstrip("/")
        if not token or not generation or not base or not consumer_id:
            return None
        if transport is None:
            _check_cp_url(base)
            transport = _UrllibTransport(token)
        return cls(
            base,
            consumer_id,
            generation,
            transport,
            environment=(env.get("BLOCKFIELD_CP_ENVIRONMENT") or "prod").strip(),
            backend=(env.get("BLOCKFIELD_CP_BACKEND") or "game").strip(),
        )

    def command(
        self,
        game_op: str,
        *,
        job: str | None = None,
        expires_in_s: int = 60,
        idempotency_key: str | None = None,
    ) -> dict:
        body: dict = {
            "idempotencyKey": idempotency_key or str(uuid.uuid4()),
            "environment": self.environment,
            "backend": self.backend,
            "consumerId": self.consumer_id,
            "generation": self.generation,
            "gameOp": game_op,
            "expiresInSeconds": int(expires_in_s),
        }
        if job is not None:
            body["job"] = str(uuid.UUID(job))
        raw = self.transport.post_json(
            f"{self.base_url}/v2/workshop/game", body, timeout=self.timeout_s
        )
        receipt = json.loads(raw)
        op_id = receipt.get("operationId") if isinstance(receipt, dict) else None
        if not isinstance(op_id, str) or not op_id:
            raise ValueError("cp workshop.game receipt misses operationId")
        events_url = f"{self.base_url}/v2/workshop/game/{op_id}/events"
        deadline = self._clock() + float(expires_in_s) + 5.0
        while True:
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise TimeoutError(
                    f"workshop game {game_op} timed out waiting for settlement"
                )
            for event in self.transport.stream_events(
                events_url, min(STREAM_IDLE_S, max(1.0, remaining))
            ):
                if isinstance(event, dict) and event.get("operationId") == op_id:
                    if event.get("state") == "effective" and event.get("code") == "ok":
                        return event
                    raise RuntimeError(
                        f"workshop game {game_op} failed: state={event.get('state')} "
                        f"code={event.get('code')} output={event.get('output')}"
                    )


DEFAULT_READY_TTL_MS = 60_000


def parse_game_desired(doc: dict | None, *, allowed_profiles: set[str]) -> dict | None:
    """Validate CP ``game-desired`` envelope or raw selection doc."""
    if doc is None:
        return None
    if not isinstance(doc, dict):
        raise ValueError("invalid game-desired state")
    data = doc.get("value") if "value" in doc and "name" in doc else doc
    if not isinstance(data, dict):
        raise ValueError("invalid game-desired state")
    if data.get("selected") is None and set(data) <= {"schema", "selected"}:
        return None
    if (
        data.get("schema") != 1
        or data.get("profile") not in allowed_profiles
        or not isinstance(data.get("release"), str)
        or not data["release"].strip()
    ):
        raise ValueError("invalid game-desired state")
    uuid.UUID(data.get("generation"))
    if data.get("session") is not None:
        uuid.UUID(data["session"])
    return dict(data)


def parse_game_ready(
    doc: dict | None,
    *,
    allowed_profiles: set[str],
    now_ms: int | None = None,
    ready_ttl_ms: int | None = DEFAULT_READY_TTL_MS,
) -> tuple[dict | None, bool]:
    """Validate CP ``game-ready`` state doc; returns ``(ready, running_and_fresh)``."""
    if not isinstance(doc, dict):
        return None, False
    data = doc.get("value") if "value" in doc else doc
    if not isinstance(data, dict):
        return None, False
    if data.get("schema") != 1 or type(data.get("running")) is not bool:
        return None, False
    running = data["running"]
    profile = data.get("profile")
    release = data.get("release")
    generation = data.get("generation")
    session = data.get("session")
    updated = (
        doc.get("updatedAt")
        if type(doc.get("updatedAt")) is int
        else data.get("updatedAt")
    )
    try:
        if profile not in allowed_profiles:
            return None, False
        if not isinstance(release, str) or not release.strip():
            return None, False
        if generation is not None and str(uuid.UUID(generation)) != generation:
            return None, False
        if profile == "game":
            if session is not None:
                return None, False
        elif str(uuid.UUID(session)) != session:
            return None, False
        if type(updated) is not int:
            return None, False
    except (ValueError, TypeError, AttributeError):
        return None, False
    fresh = True
    if ready_ttl_ms is not None and now_ms is not None:
        age = now_ms - updated
        fresh = -5000 <= age <= ready_ttl_ms
    alive = bool(running and fresh)
    if not alive:
        return None, False
    return {
        "profile": profile,
        "generation": generation,
        "session": session,
        "release": release,
    }, True


def parse_game_activity(doc: dict | None) -> dict | None:
    """Validate CP ``game-activity`` doc; None keeps the editor awake."""
    if not isinstance(doc, dict):
        return None
    data = doc.get("value") if "value" in doc else doc
    if (
        not isinstance(data, dict)
        or data.get("schema") != 1
        or not isinstance(data.get("generation"), str)
        or not data["generation"]
        or type(data.get("players")) is not int
        or type(data.get("busy")) is not bool
        or type(data.get("updatedAt")) is not int
    ):
        return None
    return dict(data)


@contextmanager
def cp_saves_held(game_client: WorkshopGameClient):
    """Hold Workshop autosave off and flush chunks via CP agent ops during hot snapshot."""
    game_client.command("save-on")
    try:
        game_client.command("save-off")
        game_client.command("save-flush")
        yield
    finally:
        try:
            game_client.command("save-on")
        except Exception:
            game_client.command("save-on")


class CPClient:
    """Durable v2 subscriber: snapshot, addressed apply, generation ACKs."""

    def __init__(
        self,
        config: CPConfig,
        state_dir: Path,
        transport=None,
        now_ms=None,
    ):
        self.config = config
        self.state_dir = Path(state_dir)
        self.transport = transport or _UrllibTransport(config.token)
        self._now_ms = now_ms or (lambda: int(time.time() * 1000))
        self._state: dict | None = None
        # Frames pushed by the stream thread, applied on the loop thread.
        self._frames: deque = deque()
        self._held: list = []
        self._snapshot_due = False
        self.wake = threading.Event()

    def has_pending(self) -> bool:
        return bool(self._frames or self._held)

    def run_stream(self, stop: threading.Event) -> None:
        """Hold the agent SSE stream open until stop; reconnect with backoff."""
        delay = 1.0
        while not stop.is_set():
            try:
                if self.read_stream():
                    delay = 1.0
            except Exception as error:
                print(f"cp: event stream failed: {error!r}", file=sys.stderr)
            stop.wait(delay)
            delay = min(delay * 2, STREAM_RETRY_MAX_S)

    def read_stream(self) -> bool:
        """One SSE connection from the durable cursor; queue frames as they
        arrive and wake the loop. False when a snapshot must come first (the
        loop thread takes it in sync)."""
        state = self._load_state()
        epoch, cursor = state.get("epoch"), state.get("cursor")
        if not epoch or type(cursor) is not int or self._snapshot_due:
            self._snapshot_due = True
            self.wake.set()
            return False
        try:
            for frame in self.transport.stream_events(
                self.config.events_url(epoch, cursor), STREAM_IDLE_S
            ):
                self._frames.append(frame)
                self.wake.set()
        except SnapshotRequired:
            self._snapshot_due = True
            self.wake.set()
            return False
        return True

    @property
    def _state_file(self) -> Path:
        return self.state_dir / "cp_state.json"

    @property
    def _receipts_file(self) -> Path:
        return self.state_dir / "cp_receipts.json"

    def _load_state(self) -> dict:
        if self._state is None:
            try:
                document = json.loads(self._state_file.read_bytes()[:65536])
                self._state = document if isinstance(document, dict) else {}
            except (OSError, ValueError):
                self._state = {}
        return self._state

    def _store_state(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._state_file.write_bytes(canonical(self._load_state()))

    def _load_receipts(self) -> dict:
        try:
            document = json.loads(self._receipts_file.read_bytes()[:1048576])
            return document if isinstance(document, dict) else {}
        except (OSError, ValueError):
            return {}

    def _store_receipt(self, operation_id: str, record: dict) -> None:
        receipts = self._load_receipts()
        receipts[operation_id] = record
        while len(receipts) > RECEIPT_LIMIT:
            receipts.pop(next(iter(receipts)))
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._receipts_file.write_bytes(canonical(receipts))

    def snapshot(self) -> dict:
        try:
            raw = self.transport.get_json(
                self.config.snapshot_url(), timeout=self.config.poll_timeout_s
            )
        except CPHttpError as error:
            if error.status in (401, 403):
                raise ValueError(
                    "cp snapshot denied: check agent credential"
                ) from error
            raise
        try:
            document = json.loads(raw.decode())
        except ValueError:
            raise ValueError("cp snapshot is not JSON")
        if not isinstance(document, dict):
            raise ValueError("cp snapshot is not an object")
        epoch = document.get("epoch")
        generation = document.get("generation")
        cursor = document.get("cursor", 0)
        if (
            not isinstance(epoch, str)
            or not epoch
            or not isinstance(generation, str)
            or not generation
            or type(cursor) is not int
            or cursor < 0
        ):
            raise ValueError("cp snapshot misses epoch/generation/cursor")
        if self.config.generation and generation != self.config.generation:
            raise ValueError("cp snapshot generation mismatch")
        # The frozen snapshot carries settings, not a Workshop ACL or release
        # list; absent keys stay None and sync() falls back to local state.
        acl = document.get("acl")
        if acl is not None:
            if not isinstance(acl, list) or len(acl) > MAX_ACL_ENTRIES:
                raise ValueError("cp snapshot acl is not a list")
            acl = [member(entry) for entry in acl]
        releases = document.get("releases")
        if releases is not None:
            if (
                not isinstance(releases, list)
                or len(releases) > CP_RELEASE_LIMIT
                or not all(isinstance(entry, dict) for entry in releases)
            ):
                raise ValueError("cp snapshot releases is not a list")
        state = {
            "schema": 1,
            "epoch": epoch,
            "generation": generation,
            "cursor": cursor,
            "acl": acl,
            "releases": releases,
            "takenAt": self._now_ms(),
        }
        self._state = state
        self._store_state()
        return state

    def _ensure_state(self) -> dict:
        state = self._load_state()
        if not state or not state.get("epoch") or not state.get("generation"):
            return self.snapshot()
        taken = state.get("takenAt", 0)
        if type(taken) is not int or self._now_ms() - taken > SNAPSHOT_TTL_S * 1000:
            return self.snapshot()
        return state

    def _post_ack(self, frame: dict, state: str, result: str = "ok") -> None:
        """POST one v2 ACK with the envelope identity CP matches receipts by.

        Any non-2xx raises (CPHttpError via the transport helper) so the
        cursor holds and the redelivery path runs on the next pass.
        """
        self.transport.post_json(
            self.config.acks_url(),
            {
                "epoch": frame.get("epoch"),
                "sequence": frame.get("sequence"),
                "generation": frame.get("generation"),
                "revision": frame.get("revision"),
                "payloadHash": frame.get("payloadHash"),
                "state": state,
                "actualAt": datetime.now(timezone.utc).isoformat(),
                "result": result,
            },
            timeout=self.config.poll_timeout_s,
        )

    @staticmethod
    def _reject_result(frame: dict, generation: str) -> str:
        if (
            isinstance(frame.get("generation"), str)
            and frame["generation"] != generation
        ):
            return "stale_generation"
        return "invalid_schema"  # expired/unaddressed/corrupt: nothing closer fits

    @staticmethod
    def _acl(state, controller):
        acl = state.get("acl")
        if acl is None:
            # The frozen snapshot carries settings, not a Workshop ACL: fall
            # back to the local members, which only CP members operations or
            # the operator bootstrap can change. Resolved per frame so a
            # members op earlier in the same pass already guards the next one;
            # without a bootstrap every kind fails closed below.
            acl = controller.members()
        return acl

    def _validated_operation(self, frame, state, controller):
        """check_frame with one snapshot refetch on a stream move.

        Returns (operation, state); (None, state) when the frame must never
        execute. A generation/epoch mismatch refetches once; a persistent
        mismatch fails closed below instead of dropping the frame silently.
        """
        try:
            return (
                check_frame(
                    frame,
                    agent_id=self.config.agent_id,
                    epoch=state["epoch"],
                    generation=state["generation"],
                    now_ms=self._now_ms(),
                ),
                state,
            )
        except SnapshotRequired:
            state = self.snapshot()
        except ValueError:
            return None, state
        try:
            return (
                check_frame(
                    frame,
                    agent_id=self.config.agent_id,
                    epoch=state["epoch"],
                    generation=state["generation"],
                    now_ms=self._now_ms(),
                ),
                state,
            )
        except (SnapshotRequired, ValueError):
            return None, state

    @staticmethod
    def _pending_marker(controller) -> dict:
        """Identity a held ACK waits for: the current pending record, or seq
        0 when it cannot be read (unknown: covered by any delivery, held
        while anything is pending)."""
        pending_fn = getattr(controller, "gate_pending_identity", None)
        if callable(pending_fn):
            try:
                marker = pending_fn()
            except OSError:
                marker = None
            if isinstance(marker, dict):
                return marker
        return {"seq": 0, "nonce": ""}

    @staticmethod
    def _gate_covers(controller, marker) -> bool:
        """True when the delivered record covers the held leg identity: a
        newer seq (in-order reporting delivered the held leg before
        superseding it), or the exact (seq, nonce). Absent, unreadable,
        older, or malformed records hold instead of going effective."""
        if type(marker.get("seq")) is not int or type(marker.get("nonce")) is not str:
            return False
        delivered_fn = getattr(controller, "gate_delivered_identity", None)
        if not callable(delivered_fn):
            return True
        try:
            delivered = delivered_fn()
        except OSError:
            return False
        if not isinstance(delivered, dict):
            return False
        if (
            type(delivered.get("seq")) is not int
            or type(delivered.get("nonce")) is not str
        ):
            return False
        return delivered["seq"] > marker["seq"] or (
            delivered["seq"] == marker["seq"] and delivered["nonce"] == marker["nonce"]
        )

    def _nack(self, frame, state, controller=None) -> tuple[int, int]:
        """Best-effort rejected receipt for an unauthenticatable frame.

        Returns (rejected, acked). The local cursor advances only past a
        delivered nack, so a failed one is retried on the next pass instead
        of being counted and skipped. A local receipt-write failure holds
        (returns zeros) instead of escaping: the frame never executed, so a
        retry re-nacks safely.
        """
        if not isinstance(frame, dict):
            return 0, 0
        operation_id = frame.get("operationId")
        sequence = frame.get("sequence")
        if not (type(operation_id) is str and operation_id and type(sequence) is int):
            return 0, 0
        result = self._reject_result(frame, state["generation"])
        try:
            self._store_receipt(
                operation_id,
                {"state": "rejected", "sequence": sequence, "result": result},
            )
        except OSError:
            return 0, 0
        _report_result(controller, operation_id, error=ValueError(result))
        try:
            self._post_ack(frame, "rejected", result)
        except Exception:
            return 1, 0
        if type(state["cursor"]) is int and sequence > state["cursor"]:
            state["cursor"] = sequence
            self._store_state()
        return 1, 1

    def _replay_ack(self, frame, terminal: str, result: str = "ok") -> None:
        """Re-send ACKs for an already-recorded op without re-applying it.

        Walks the full progression so the replay converges whatever prefix
        the server already holds (a failed first post leaves it empty; a
        mid-walk failure leaves a prefix). A 409 means that state is already
        recorded server-side; anything else raises.
        """
        if terminal == "rejected":
            wanted = ("rejected",)
        else:
            wanted = ("received", "validated", "applied")
            if terminal == "effective":
                wanted += ("effective",)
        for state in wanted:
            try:
                self._post_ack(frame, state, result if state == terminal else "ok")
            except CPHttpError as error:
                if error.status != 409:
                    raise

    def sync(self, controller) -> dict:
        """One pass over the frames the stream pushed: apply, ACK, report.

        Never raises for per-operation failures; a transport failure raises
        so the caller can log it without blocking reconciliation. Frames a
        pass must hold (lock contention, undelivered gate leg) stay queued
        for the next pass.
        """
        if self._snapshot_due:
            self._snapshot_due = False
            self.snapshot()
        state = self._ensure_state()
        pending = state.get("releases") or []
        if pending:
            try:
                controller.apply_cp_releases(pending)
            except ValueError:
                # Snapshot records are advisory availability; the explicit
                # release.select operation carries its own ACK, so a bad
                # snapshot entry must not block other operations.
                pass
        frames, self._held = self._held, []
        while self._frames:
            frames.append(self._frames.popleft())
        applied = rejected = acked = 0
        for index, frame in enumerate(frames):
            operation, state = self._validated_operation(frame, state, controller)
            if operation is None:
                # Unauthenticatable frame (foreign audience, stale generation
                # or epoch, expired, hash mismatch): never execute, but still
                # nack by envelope id so the stream can move on.
                delta_rejected, delta_acked = self._nack(frame, state, controller)
                rejected += delta_rejected
                acked += delta_acked
                continue
            operation_id = frame["operationId"]
            sequence = frame["sequence"]
            if type(state["cursor"]) is int and sequence <= state["cursor"]:
                continue
            receipts = self._load_receipts()
            if operation_id in receipts and receipts[operation_id].get("state") in (
                "applied",
                "effective",
                "rejected",
            ):
                stored = receipts[operation_id]
                pending_gate = getattr(controller, "gate_report_pending", None)
                marker = stored.get("gate")
                if (
                    stored.get("state") == "effective"
                    and isinstance(marker, dict)
                    and callable(pending_gate)
                    and pending_gate()
                ):
                    # Retry the delivery first (no controller lock is held
                    # here): a pass that only replays must still converge
                    # once CP is reachable again.
                    flush = getattr(controller, "_flush_gate_report", None)
                    if callable(flush):
                        try:
                            flush()
                        except Exception:
                            pass  # stays pending; the hold below retries later
                    if pending_gate():
                        # Leg still undelivered: re-send progress ACKs only
                        # and hold the cursor, so effective follows delivery
                        # instead of preceding it. The receipt is already
                        # terminal: the op is never re-applied (cancel/wake
                        # are not idempotent).
                        self._replay_ack(frame, "applied", stored.get("result", "ok"))
                        break
                if stored.get("state") == "effective" and isinstance(marker, dict):
                    # I5: effective replays only once the exact staged leg is
                    # delivered (or a newer leg superseded it in order). A
                    # held receipt whose leg is neither pending nor delivered
                    # holds instead of going effective.
                    if not self._gate_covers(controller, marker):
                        self._replay_ack(frame, "applied", stored.get("result", "ok"))
                        break
                self._replay_ack(
                    frame,
                    stored["state"],
                    stored.get("result", "ok"),
                )
                acked += 1
                state["cursor"] = max(state["cursor"], sequence)
                self._store_state()
                continue
            try:
                self._store_receipt(
                    operation_id, {"state": "received", "sequence": sequence}
                )
            except OSError:
                # Nothing applied yet; hold so the retry starts clean.
                break
            try:
                result = controller.apply_cp_operation(
                    operation_id, operation, self._acl(state, controller)
                )
            except BlockingIOError:
                # Transient controller lock contention: hold the cursor and
                # retry the whole pass later instead of skipping this frame.
                # Every mutating op takes the controller lock before any
                # commit, so contention raises pre-commit and the retry
                # re-applies a never-started op (cancel included).
                break
            except CommitIncompleteError:
                # The commit stands but its derivatives failed: never rejected
                # (CP must retry, visibly held here), and the retry is safe.
                # Commit sites with non-idempotent compensation ERROR-drove
                # first (a retry lands on the vacuous "nothing to cancel"
                # path, or matches its journal key); members/message re-apply
                # idempotently; wake re-exports through its op-id dedup. No
                # receipt is stored: the pre-existing received receipt routes
                # the retry back through apply, never past it. (A receipt
                # store itself can fail after a commit — thread 4507741042 —
                # so the hold must not depend on one succeeding here.)
                break
            except Exception as error:
                # Rejected, cursor advances: schema violations and permanent
                # local I/O failures (anything but lock contention) alike. A
                # terminal receipt is stored first, so a same-op retry replays
                # instead of re-applying a never-started op.
                _report_result(controller, operation_id, error=error)
                try:
                    self._store_receipt(
                        operation_id,
                        {
                            "state": "rejected",
                            "sequence": sequence,
                            "result": "invalid_schema",
                        },
                    )
                except OSError:
                    # Pre-commit failure, so the retry re-applies a
                    # never-started op safely: hold instead of escaping.
                    break
                self._post_ack(frame, "rejected", "invalid_schema")
                rejected += 1
                acked += 1
            else:
                _report_result(controller, operation_id, result)
                # Fail-closed gate delivery: a members/gate op whose leg is
                # still undelivered is never ACKed effective. Hold the cursor
                # and retry the whole pass later (like lock contention above),
                # so a narrowing change cannot read effective while CP still
                # admits the old leg.
                # CP only advances the durable cursor across contiguous
                # receipts in state order, so walk it explicitly.
                terminal = "applied" if result.get("started") is True else "effective"
                if result.get("kind") in ("members", "message", "wake", "cancel"):
                    pending_gate = getattr(controller, "gate_report_pending", None)
                    if callable(pending_gate) and pending_gate():
                        # Persist the terminal receipt before holding: the
                        # next pass replays ACKs from it instead of
                        # re-applying the op (cancel/wake are not idempotent).
                        # The gate marker records which exact leg effective
                        # waits for (I5). If the store itself fails, hold
                        # anyway: the op is fully applied and its leg staged,
                        # so the retry re-applies safely (cancel matches its
                        # journal key and returns the state; members/message
                        # re-apply idempotently; wake dedups by op id).
                        try:
                            self._store_receipt(
                                operation_id,
                                {
                                    "state": terminal,
                                    "sequence": sequence,
                                    "result": "ok",
                                    "gate": self._pending_marker(controller),
                                },
                            )
                        except OSError:
                            break
                        break
                # Durable first: if an ACK post below fails, the replay path
                # re-sends ACKs for this stored receipt instead of running the
                # op again (cancel matches its journal key; members/message
                # re-apply idempotently; wake dedups by op id).
                try:
                    self._store_receipt(
                        operation_id,
                        {"state": terminal, "sequence": sequence, "result": "ok"},
                    )
                except OSError:
                    # Fully applied but the receipt is lost: hold. The retry
                    # re-applies safely (see above) and converges from there.
                    break
                for progress in ("received", "validated", "applied"):
                    self._post_ack(frame, progress)
                if terminal == "effective":
                    self._post_ack(frame, "effective")
                if result.get("kind") == "members":
                    # Membership changed under us: refresh the snapshot ACL so
                    # later frames in this pass check the new list, not the
                    # stale snapshot (which lives up to the TTL otherwise).
                    state["acl"] = controller.members()
                    self._store_state()
                applied += 1
                acked += 1
            state["cursor"] = max(state["cursor"], sequence)
            self._store_state()
        else:
            index = len(frames)
        # Held frames (and everything after them) retry on the next pass.
        self._held = frames[index:]
        flush = getattr(controller, "flush_reports", None)
        if callable(flush):
            flush()
        return {"applied": applied, "rejected": rejected, "acked": acked}


def _report_result(controller, operation_id, outcome=None, error=None) -> None:
    """Stage the site-facing result of one operation (controller.cp_result)."""
    report = getattr(controller, "cp_result", None)
    if callable(report):
        report(operation_id, outcome, error)


RENEW_MAX_LEAD_S = 7 * 86400
RENEW_RETRY_S = 30.0
RENEW_MAX_SLEEP_S = 3600.0
RENEWED_KEYS = ("BLOCKFIELD_CP_AGENT_TOKEN", "BLOCKFIELD_CP_GATE_TOKEN")


def renew_at(issued: float, expires: float) -> float:
    """~7 days before expiry, or a quarter of a short (test) lifetime."""
    return expires - min(RENEW_MAX_LEAD_S, max(0.0, expires - issued) / 4)


def _epoch(text: str) -> float:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class CredentialRenewer:
    """Self-renewing CP service credentials (coordination#33).

    Renews each token ~7 days before expiry, persists the successor to a 0600
    state file and only then confirms (which revokes the old token). A
    persisted successor wins over env while it descends from the current env
    token; a re-issued env token starts a new chain.
    """

    def __init__(self, base_url, state_file, env_tokens, call=None, now=time.time):
        self.base_url = base_url.rstrip("/")
        self.state_file = Path(state_file)
        self.original = {k: v.strip() for k, v in env_tokens.items() if v and v.strip()}
        self.tokens = dict(self.original)
        self.pending: set[str] = set()
        self.due = {k: 0.0 for k in self.tokens}
        self.listeners: dict = {}
        self._call = call or self._http
        self._now = now
        state = self._read()
        for key, env in self.original.items():
            entry = state.get(key)
            if (
                isinstance(entry, dict)
                and entry.get("base") == _sha256(env)
                and isinstance(entry.get("token"), str)
                and entry["token"]
            ):
                self.tokens[key] = entry["token"]
                # May be unconfirmed (crash before confirm); confirm is idempotent.
                self.pending.add(key)

    def _read(self) -> dict:
        try:
            document = json.loads(self.state_file.read_bytes()[:65536])
            return document if isinstance(document, dict) else {}
        except (OSError, ValueError):
            return {}

    def _persist(self, key: str, token: str) -> None:
        state = self._read()
        state[key] = {"token": token, "base": _sha256(self.original[key])}
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self._write(state)

    def _forget(self, key: str) -> None:
        state = self._read()
        if state.pop(key, None) is not None:
            self._write(state)

    def _write(self, state: dict) -> None:
        # Unique per write: overlapping deploys share the volume and often the pid.
        temp = self.state_file.with_name(
            f".{self.state_file.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        )
        try:
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(canonical(state))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, self.state_file)
        finally:
            temp.unlink(missing_ok=True)

    def _http(self, method: str, path: str, token: str) -> tuple[int, dict | None]:
        # Never send the bearer through an http proxy (same as _UrllibTransport).
        proxies = {k: v for k, v in urllib.request.getproxies().items() if k != "http"}
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler(proxies), _NoRedirect
        )
        request = urllib.request.Request(
            self.base_url + path,
            data=b"" if method == "POST" else None,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            method=method,
        )
        try:
            with opener.open(request, timeout=10) as response:
                status, raw = response.status, response.read(65536)
        except urllib.error.HTTPError as error:
            status, raw = error.code, b""
            error.close()
        try:
            return status, json.loads(raw) if raw else None
        except ValueError:
            return status, None

    def token(self, key: str) -> str:
        return self.tokens.get(key, "")

    def _adopt(self, key: str, token: str) -> None:
        self.tokens[key] = token
        listener = self.listeners.get(key)
        if listener is not None:
            listener(token)

    def tick(self) -> None:
        """Run every due key; each schedules its own next check."""
        for key in self.tokens:
            now = self._now()
            if now < self.due[key]:
                continue
            try:
                delay = self._step(key)
            except Exception as error:  # renewal must never block the loop
                print(f"cp: {key} renewal check failed: {error!r}", file=sys.stderr)
                delay = RENEW_RETRY_S
            self.due[key] = now + delay

    def _step(self, key: str) -> float:
        token = self.tokens[key]
        just_confirmed = False
        if key in self.pending:
            status, _ = self._call("POST", "/v2/credentials/renew/confirm", token)
            # 404: nothing pending for this token; 401: /v2/me below falls back.
            if status not in (200, 401, 404):
                return RENEW_RETRY_S
            self.pending.discard(key)
            just_confirmed = status == 200
        status, me = self._call("GET", "/v2/me", token)
        env = self.original[key]
        # Right after a confirmed renewal a 401 is replication lag: the env token is revoked.
        if status == 401 and token != env and not just_confirmed:
            print(
                f"cp: {key} persisted credential rejected, using env", file=sys.stderr
            )
            self._adopt(key, env)
            self._forget(key)  # else a restart would adopt the rejected successor again
            return 0.0
        if status != 200 or not isinstance(me, dict):
            return RENEW_RETRY_S
        if not me.get("issuedAt") or not me.get("expiresAt"):
            return RENEW_MAX_SLEEP_S  # CP without renewal support: check back hourly
        try:
            due = renew_at(_epoch(me["issuedAt"]), _epoch(me["expiresAt"]))
        except (ValueError, TypeError, AttributeError):
            print(f"cp: {key} unusable credential dates", file=sys.stderr)
            return RENEW_RETRY_S
        now = self._now()
        if now < due:
            return max(1.0, min(RENEW_MAX_SLEEP_S, due - now))
        status, renewed = self._call("POST", "/v2/credentials/renew", token)
        if status != 201 or not isinstance(renewed, dict) or not renewed.get("token"):
            print(f"cp: {key} renew status {status}", file=sys.stderr)
            return RENEW_RETRY_S
        # Persist before confirm: confirm revokes the old token. A failed write
        # raises and the retry replays the same successor.
        self._persist(key, renewed["token"])
        self._adopt(key, renewed["token"])
        self.pending.add(key)
        print(
            f"cp: {key} renewed, valid until {renewed.get('expiresAt')}",
            file=sys.stderr,
        )
        return 0.0
