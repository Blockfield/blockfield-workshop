"""Control-plane v2 subscriber for the Workshop controller (unit CP-S6a).

Consumes the frozen v2 wire contract (control-plane origin/main ``6df1f95``
+ ``cf660e4``, ``docs/V2.md`` "Private delivery"): target snapshot,
per-consumer SSE events, explicit ACKs. The wire (endpoints, envelope
fields, ACK states ``received -> validated -> applied -> effective`` with
terminal ``rejected``/``unknown``) is fixed by CP; this module only
interprets the Workshop target payload (the existing inbox kinds) and the
Workshop ACL carried in the snapshot.

Trust model: frames arrive over the mutually authenticated CP channel
(TLS + audience-bound bearer pinned to this agent id), so the payload
actor/grantor is CP-attested -- unlike the file inbox, whose ``actor``
field is self-asserted (E4). The payload hash is verified over canonical
JSON (sort_keys, compact separators); the CP-side Workshop producer must
hash the same canonical form when committing the outbox row.

Disabled unless ``BLOCKFIELD_CP_URL``, ``BLOCKFIELD_CP_AGENT_ID`` and
``BLOCKFIELD_CP_AGENT_TOKEN`` are all set. The file inbox keeps working
either way; it is retired by S6b, not here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import os
import time
import urllib.request
import urllib.error
import urllib.parse

from controller import member

AGENT_PATH = "/v2/agents/{agent}"
SNAPSHOT_TTL_S = 3600
EVENT_TIMEOUT_S = 5.0
MAX_FRAME_BYTES = 65536
MAX_ACL_ENTRIES = 4096
MAX_ACL_ENTRY_BYTES = 256
MAX_SNAPSHOT_BYTES = MAX_ACL_ENTRIES * MAX_ACL_ENTRY_BYTES
RECEIPT_LIMIT = 500
CP_KINDS = ("members", "message", "wake", "cancel")


class SnapshotRequired(Exception):
    """Stream epoch/cursor moved on: refetch the snapshot before applying."""


class CPHttpError(ValueError):
    """CP answered non-2xx: carries the status so callers can branch on it."""

    def __init__(self, what: str, status: int):
        super().__init__(f"cp {what} failed: http {status}")
        self.status = status


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
        scheme = urllib.parse.urlsplit(base).scheme.lower()
        host = urllib.parse.urlsplit(base).hostname or ""
        if scheme != "https" and not (
            scheme == "http" and host in ("127.0.0.1", "localhost", "::1")
        ):
            raise ValueError("BLOCKFIELD_CP_URL must be https (http only for loopback)")
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
    if type(sequence) is not int or sequence < 0:
        raise ValueError("invalid cp frame: sequence")
    _need(frame, "operationId", (str,), "operationId")
    frame_generation = _need(frame, "generation", (str,), "generation")
    if frame_generation != generation:
        raise SnapshotRequired("cp stream generation moved on")
    expires = frame.get("expiresAt")
    if type(expires) is not int or expires <= 0:
        raise ValueError("invalid cp frame: expiresAt")
    if now_ms > expires:
        raise ValueError("cp operation expired")
    want = _need(frame, "payloadHash", (str,), "payloadHash")
    operation = frame.get("operation")
    if not isinstance(operation, dict):
        raise ValueError("invalid cp frame: operation")
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
        self.opener = urllib.request.build_opener(_NoRedirect)

    def _headers(self, accept: str) -> dict:
        return {"Authorization": f"Bearer {self.token}", "Accept": accept}

    def get_json(self, url: str, timeout: float) -> bytes:
        request = urllib.request.Request(url, headers=self._headers("application/json"))
        return _request_body(
            self.opener, request, timeout, "snapshot", MAX_SNAPSHOT_BYTES
        )

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

    def get_events(self, url: str, timeout: float) -> tuple[int, list[dict]]:
        request = urllib.request.Request(
            url, headers=self._headers("text/event-stream")
        )
        try:
            with self.opener.open(request, timeout=timeout) as response:
                if response.status == 409:
                    raise SnapshotRequired("cp snapshot required")
                if not 200 <= response.status <= 299:
                    raise CPHttpError("events", response.status)
                return response.status, _read_sse(response, timeout)
        except urllib.error.HTTPError as error:
            if error.code == 409:
                raise SnapshotRequired("cp snapshot required") from error
            try:
                error.close()
            except Exception:
                pass
            raise CPHttpError("events", error.code) from error


def _read_sse(response, timeout: float) -> list[dict]:
    """Read bounded SSE frames until the deadline; one JSON object per data block.

    A socket timeout after the last queued event ends the read with the
    frames parsed so far instead of discarding them, and so does an
    undecodable data block. Lines carry no
    per-line limit (a members frame can approach MAX_FRAME_BYTES); a single
    data block past that is an explicit error, never silent truncation.
    """
    frames: list[dict] = []
    data: list[str] = []
    data_len = 0
    deadline = time.monotonic() + timeout
    total = 0
    while time.monotonic() < deadline and len(frames) < 20:
        try:
            line = response.readline()
        except TimeoutError:
            break
        if not line:
            break
        total += len(line)
        if total > MAX_FRAME_BYTES * 20:
            break
        text = line.decode("utf-8", "replace").rstrip("\r\n")
        if text == "":
            if data:
                try:
                    document = json.loads("\n".join(data))
                except ValueError:
                    # Undecodable block: stop here and keep the frames parsed
                    # so far. The cursor never jumps a hole (the server would
                    # 409 a jumped cursor and loop), so later frames stay
                    # queued; the next pass re-reads them over a fresh
                    # connection, which heals transient truncation. Receipts
                    # of applied frames persist in the receipts file.
                    break
                if isinstance(document, dict):
                    frames.append(document)
                data = []
                data_len = 0
        elif text.startswith("data:"):
            data.append(text[5:].lstrip())
            data_len += len(line)
            if data_len > MAX_FRAME_BYTES:
                raise ValueError(f"cp frame exceeds {MAX_FRAME_BYTES} bytes")
        # "id:" (epoch:sequence) and retry/comments carry no payload here.
    return frames


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
        acl = document.get("acl")
        if acl is not None:
            if not isinstance(acl, list) or len(acl) > MAX_ACL_ENTRIES:
                raise ValueError("cp snapshot acl is not a list")
            acl = [member(entry) for entry in acl]
        state = {
            "schema": 1,
            "epoch": epoch,
            "generation": generation,
            "cursor": cursor,
            "acl": acl,
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

    def _ack(
        self, controller, operation_id: str, epoch: str, sequence: int, terminal: str
    ) -> None:
        try:
            actual = controller._readiness_generation()
        except Exception:
            actual = None
        self.transport.post_json(
            self.config.acks_url(),
            {
                "operationId": operation_id,
                "epoch": epoch,
                "sequence": sequence,
                "state": terminal,
                "actualAt": datetime.now(timezone.utc).isoformat(),
                "actualGeneration": actual,
                "result": "ok" if terminal in ("applied", "effective") else terminal,
            },
            timeout=self.config.poll_timeout_s,
        )

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
    def _acl(state, controller):
        acl = state.get("acl")
        if acl is None:
            # A snapshot without an ACL list must not skip the membership
            # check: fall back to the local members, which only CP members
            # operations or the operator bootstrap can change. Resolved per
            # frame so a members op earlier in the same pass already guards
            # the next one; without a bootstrap every kind fails closed.
            acl = controller.members()
        return acl

    def _nack(self, controller, frame, state) -> tuple[int, int]:
        """Best-effort rejected receipt for an unauthenticatable frame.

        Returns (rejected, acked). The local cursor advances only past a
        delivered nack, so a failed one is retried on the next pass instead
        of being counted and skipped.
        """
        if not isinstance(frame, dict):
            return 0, 0
        operation_id = frame.get("operationId")
        sequence = frame.get("sequence")
        if not (type(operation_id) is str and operation_id and type(sequence) is int):
            return 0, 0
        self._store_receipt(operation_id, {"state": "rejected", "sequence": sequence})
        try:
            self._ack(controller, operation_id, state["epoch"], sequence, "rejected")
        except Exception:
            return 1, 0
        if type(state["cursor"]) is int and sequence > state["cursor"]:
            state["cursor"] = sequence
            self._store_state()
        return 1, 1

    def _reject_applied(self, controller, operation_id, sequence, state) -> None:
        self._store_receipt(operation_id, {"state": "rejected", "sequence": sequence})
        self._ack(controller, operation_id, state["epoch"], sequence, "rejected")

    def sync(self, controller) -> dict:
        """One bounded pass: snapshot if needed, apply addressed frames, ACK.

        Never raises for per-operation failures; a transport failure raises
        so the caller can log it without blocking reconciliation.
        """
        state = self._ensure_state()
        try:
            _, frames = self.transport.get_events(
                self.config.events_url(state["epoch"], state["cursor"]),
                timeout=self.config.poll_timeout_s,
            )
        except SnapshotRequired:
            state = self.snapshot()
            _, frames = self.transport.get_events(
                self.config.events_url(state["epoch"], state["cursor"]),
                timeout=self.config.poll_timeout_s,
            )
        applied = rejected = acked = 0
        for frame in frames:
            operation, state = self._validated_operation(frame, state, controller)
            if operation is None:
                # Unauthenticatable frame (foreign audience, stale generation
                # or epoch, expired, hash mismatch): never execute, but still
                # nack by envelope id so the stream can move on.
                delta_rejected, delta_acked = self._nack(controller, frame, state)
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
                self._ack(
                    controller,
                    operation_id,
                    state["epoch"],
                    sequence,
                    receipts[operation_id]["state"],
                )
                acked += 1
                state["cursor"] = max(state["cursor"], sequence)
                self._store_state()
                continue
            self._store_receipt(
                operation_id, {"state": "received", "sequence": sequence}
            )
            try:
                result = controller.apply_cp_operation(
                    operation_id, operation, self._acl(state, controller)
                )
            except BlockingIOError:
                # Transient controller lock contention: hold the cursor and
                # retry the whole pass later instead of skipping this frame.
                break
            except Exception:
                self._reject_applied(controller, operation_id, sequence, state)
                rejected += 1
                acked += 1
            else:
                terminal = "applied" if result.get("started") is True else "effective"
                self._store_receipt(
                    operation_id, {"state": terminal, "sequence": sequence}
                )
                self._ack(controller, operation_id, state["epoch"], sequence, terminal)
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
        return {"applied": applied, "rejected": rejected, "acked": acked}
