"""Control-plane v2 subscriber for the Workshop controller (units CP-S6a/S6b).

Consumes the frozen v2 wire contract (control-plane origin/main ``6df1f95``
+ ``cf660e4``, ``docs/V2.md`` "Private delivery"): target snapshot,
per-consumer SSE events, explicit ACKs. The wire (endpoints, envelope
fields, ACK states ``received -> validated -> applied -> effective`` with
terminal ``rejected``/``unknown``) is fixed by CP; this module only
interprets the Workshop target payload (the existing inbox kinds).

The snapshot carries target-scoped settings, not a Workshop ACL: membership
is checked against the locally stored members, which only CP members
operations (or the operator bootstrap) can change. Release records arrive
inside the release.select operation itself and are verified against the
local catalog.

Trust model: frames arrive over the mutually authenticated CP channel
(TLS + audience-bound bearer pinned to this agent id), so the payload
actor/grantor is CP-attested -- unlike the file inbox, whose ``actor``
field is self-asserted (E4). The payload hash is verified over canonical
JSON (sort_keys, compact separators); the CP-side Workshop producer must
hash the same canonical form when committing the outbox row.

Disabled unless ``BLOCKFIELD_CP_URL``, ``BLOCKFIELD_CP_AGENT_ID`` and
``BLOCKFIELD_CP_AGENT_TOKEN`` are all set. The file inbox keeps working unless BLOCKFIELD_CP_INGRESS=1 retires the
CP-covered kinds (members/message/wake/cancel); map operations stay
inbox-only. Release records arrive in the release.select operation and are
verified against the local catalog before merging.
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

from controller import CP_RELEASE_LIMIT, member

AGENT_PATH = "/v2/agents/{agent}"
SNAPSHOT_TTL_S = 3600
EVENT_TIMEOUT_S = 5.0
MAX_FRAME_BYTES = 65536
MAX_ACL_ENTRIES = 4096
MAX_ACL_ENTRY_BYTES = 256
MAX_SNAPSHOT_BYTES = MAX_ACL_ENTRIES * MAX_ACL_ENTRY_BYTES
RECEIPT_LIMIT = 500
CP_KINDS = ("members", "message", "wake", "cancel", "release.select")


class SnapshotRequired(Exception):
    """Stream epoch/cursor moved on: refetch the snapshot before applying."""


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


class _UrllibTransport:
    def __init__(self, token: str):
        self.token = token

    def _headers(self, accept: str) -> dict:
        return {"Authorization": f"Bearer {self.token}", "Accept": accept}

    def get_json(self, url: str, timeout: float) -> tuple[int, bytes]:
        request = urllib.request.Request(url, headers=self._headers("application/json"))
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, _read_capped(
                    response, MAX_SNAPSHOT_BYTES, "snapshot"
                )
        except urllib.error.HTTPError as error:
            return error.code, _read_capped(error, MAX_SNAPSHOT_BYTES, "snapshot")

    def post_json(self, url: str, body: dict, timeout: float) -> tuple[int, bytes]:
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
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, _read_capped(response, MAX_FRAME_BYTES, "ack")
        except urllib.error.HTTPError as error:
            return error.code, _read_capped(error, MAX_FRAME_BYTES, "ack")

    def get_events(self, url: str, timeout: float) -> tuple[int, list[dict]]:
        request = urllib.request.Request(
            url, headers=self._headers("text/event-stream")
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                if response.status == 409:
                    raise SnapshotRequired("cp snapshot required")
                return response.status, _read_sse(response, timeout)
        except urllib.error.HTTPError as error:
            if error.code == 409:
                raise SnapshotRequired("cp snapshot required")
            return error.code, []


def _read_sse(response, timeout: float) -> list[dict]:
    """Read bounded SSE frames until the deadline; one JSON object per data block.

    A socket timeout after the last queued event ends the read with the
    frames parsed so far instead of discarding them. Lines carry no
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
                    raise ValueError("invalid cp frame: bad data JSON")
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
        status, raw = self.transport.get_json(
            self.config.snapshot_url(), timeout=self.config.poll_timeout_s
        )
        if status == 401 or status == 403:
            raise ValueError("cp snapshot denied: check agent credential")
        if status != 200:
            raise ValueError(f"cp snapshot failed: http {status}")
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

    def _post_ack(self, frame: dict, state: str, result: str = "ok") -> int:
        """POST one v2 ACK with the envelope identity CP matches receipts by."""
        status, _ = self.transport.post_json(
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
        return status

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

    def _nack(self, frame, state) -> tuple[int, int]:
        """Best-effort rejected receipt for an unauthenticatable frame.

        Returns (rejected, acked) and advances the local cursor past it so
        the stream can move on.
        """
        if not isinstance(frame, dict):
            return 0, 0
        operation_id = frame.get("operationId")
        sequence = frame.get("sequence")
        if not (type(operation_id) is str and operation_id and type(sequence) is int):
            return 0, 0
        result = self._reject_result(frame, state["generation"])
        self._store_receipt(
            operation_id, {"state": "rejected", "sequence": sequence, "result": result}
        )
        try:
            status = self._post_ack(frame, "rejected", result)
        except Exception:
            acked = 0
        else:
            acked = int(status == 200)
        if type(state["cursor"]) is int and sequence > state["cursor"]:
            state["cursor"] = sequence
            self._store_state()
        return 1, acked

    def sync(self, controller) -> dict:
        """One bounded pass: snapshot if needed, apply addressed frames, ACK.

        Never raises for per-operation failures; a transport failure raises
        so the caller can log it without blocking reconciliation.
        """
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
                delta_rejected, delta_acked = self._nack(frame, state)
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
                status = self._post_ack(
                    frame,
                    receipts[operation_id]["state"],
                    receipts[operation_id].get("result", "ok"),
                )
                if status != 200:
                    raise ValueError(f"cp ack failed: http {status}")
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
                self._store_receipt(
                    operation_id,
                    {
                        "state": "rejected",
                        "sequence": sequence,
                        "result": "invalid_schema",
                    },
                )
                status = self._post_ack(frame, "rejected", "invalid_schema")
                if status != 200:
                    raise ValueError(f"cp ack failed: http {status}")
                rejected += 1
                acked += 1
            else:
                # CP only advances the durable cursor across contiguous
                # receipts in state order, so walk it explicitly.
                terminal = "applied" if result.get("started") is True else "effective"
                for progress in ("received", "validated", "applied"):
                    status = self._post_ack(frame, progress)
                    if status != 200:
                        raise ValueError(f"cp ack failed: http {status}")
                if terminal == "effective":
                    status = self._post_ack(frame, "effective")
                    if status != 200:
                        raise ValueError(f"cp ack failed: http {status}")
                self._store_receipt(
                    operation_id,
                    {"state": terminal, "sequence": sequence, "result": "ok"},
                )
                applied += 1
                acked += 1
            state["cursor"] = max(state["cursor"], sequence)
            self._store_state()
        return {"applied": applied, "rejected": rejected, "acked": acked}
