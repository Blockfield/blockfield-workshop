"""CP v2 subscribe/apply: addressed typed ops, local ACL fallback, envelope ACKs (CP-S6a)."""

from release_fixtures import catalog

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from cp_client import (
    CPClient,
    CPConfig,
    CPHttpError,
    GateReporter,
    SnapshotRequired,
    payload_hash,
)
from controller import Controller

DRASL = "e10438e4797b49718cfecec6a8fa171b"
STRANGER = "ffffffffffffffffffffffffffffffff"
ADMIN = {"draslId": DRASL, "name": "Nether", "role": "admin"}
BUILDER = {"draslId": DRASL, "name": "Nether", "role": "builder"}
AGENT = "workshop-1"
EPOCH = "11111111-2222-3333-4444-555555555555"
GENERATION = "gen-7"
NOW_MS = 1796409600000
IDENTITY_KEYS = ("epoch", "sequence", "generation", "revision", "payloadHash")


def frame(
    operation,
    seq,
    agent=AGENT,
    epoch=EPOCH,
    generation=GENERATION,
    expires_at=NOW_MS + 60000,
    tamper_hash=False,
    executable=True,
    revision=None,
):
    """One real-shaped v2 envelope (delivery.batch fields, V2.md §Private delivery)."""
    body = dict(operation)
    return {
        "schema": 2,
        "epoch": epoch,
        "sequence": seq,
        "operationId": str(uuid4()),
        "audience": agent,
        "generation": generation,
        "revision": revision,
        "payloadHash": "0" * 64 if tamper_hash else payload_hash(body),
        "authorizationEpoch": 1,
        "expiresAt": datetime.fromtimestamp(
            expires_at / 1000, tz=timezone.utc
        ).isoformat(),
        "executable": executable,
        "reason": None if executable else "expired",
        "payload": body if executable else None,
    }


def members_op(grantor, entries, target="*"):
    return {"kind": "members", "grantor": grantor, "members": entries, "target": target}


class FakeTransport:
    def __init__(self, snapshot_doc, event_frames=(), fail_events_once=False):
        self.snapshot_doc = snapshot_doc
        self.event_frames = list(event_frames)
        self.fail_events_once = fail_events_once
        self.acks = []
        self.events_calls = 0

    def get_json(self, url, timeout):
        return json.dumps(self.snapshot_doc).encode()

    def post_json(self, url, body, timeout):
        self.acks.append(body)
        return b"{}"

    def get_events(self, url, timeout):
        self.events_calls += 1
        if self.fail_events_once and self.events_calls == 1:
            raise SnapshotRequired("cp snapshot required")
        return 200, list(self.event_frames)


def snapshot_doc(cursor=0):
    """Minimal real snapshot: settings only, no ACL (sync falls back to local members)."""
    return {
        "schema": 2,
        "audience": AGENT,
        "epoch": EPOCH,
        "generation": GENERATION,
        "cursor": cursor,
        "settings": [],
        "hash": "0" * 64,
    }


class CPClientTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)
        from test_controller import FakeBackend

        self.backend = FakeBackend()
        self.controller = Controller(
            self.dir / "state",
            self.backend,
            gate_file=self.dir / "gate.json",
            public_file=self.dir / "public.json",
            admin_dir=self.dir / "admin",
            inbox_dir=self.dir / "inbox",
            releases=catalog(),
        )
        self.controller.set_members([ADMIN])
        self.config = CPConfig(
            base_url="https://cp.invalid",
            agent_id=AGENT,
            token="t",
            generation=GENERATION,
        )

    def client(self, transport, now_ms=NOW_MS):
        return CPClient(
            self.config,
            self.dir / "state",
            transport=transport,
            now_ms=lambda: now_ms,
        )

    def test_config_disabled_without_env(self):
        self.assertIsNone(CPConfig.from_env({}))

    def test_legacy_inbox_applies_spoofed_members(self):
        """E4 contrast: the file inbox trusts list shape, not grant authority."""
        forged = [
            dict(ADMIN),
            {"draslId": STRANGER, "name": "Stranger", "role": "admin"},
        ]
        self.controller._apply(
            str(uuid4()),
            {
                "kind": "members",
                "actor": {"draslId": STRANGER, "name": "Stranger"},
                "members": forged,
            },
        )
        self.assertEqual(
            {m["draslId"] for m in self.controller.members()},
            {ADMIN["draslId"].replace("-", "").lower(), STRANGER},
        )

    def test_spoofed_members_rejected(self):
        forged = [
            dict(ADMIN),
            {"draslId": STRANGER, "name": "Stranger", "role": "admin"},
        ]
        # Outsider grantor: not in the CP ACL at all.
        with self.assertRaisesRegex(ValueError, "not a workshop member"):
            self.controller.apply_cp_operation(
                str(uuid4()),
                members_op({"draslId": STRANGER, "name": "Stranger"}, forged),
                [ADMIN],
            )
        # Builder grantor forging an admin grant: a member, but not an admin.
        helper = {"draslId": "aa" * 16, "name": "Helper", "role": "builder"}
        with self.assertRaisesRegex(ValueError, "not a workshop admin"):
            self.controller.apply_cp_operation(
                str(uuid4()), members_op(helper, forged), [ADMIN, helper]
            )
        self.assertEqual(self.controller.members(), [ADMIN])

    def test_spoofed_members_rejected_end_to_end(self):
        forged = [
            dict(ADMIN),
            {"draslId": STRANGER, "name": "Stranger", "role": "admin"},
        ]
        transport = FakeTransport(
            snapshot_doc(),
            [frame(members_op({"draslId": STRANGER, "name": "Stranger"}, forged), 1)],
        )
        result = self.client(transport).sync(self.controller)
        self.assertEqual(result, {"applied": 0, "rejected": 1, "acked": 1})
        self.assertEqual(self.controller.members(), [ADMIN])
        self.assertEqual(transport.acks[0]["state"], "rejected")

    def test_members_applied_from_cp(self):
        newcomer = {"draslId": STRANGER, "name": "Stranger", "role": "builder"}
        envelope = frame(members_op(ADMIN, [ADMIN, newcomer]), 1)
        transport = FakeTransport(snapshot_doc(), [envelope])
        result = self.client(transport).sync(self.controller)
        self.assertEqual(result, {"applied": 1, "rejected": 0, "acked": 1})
        self.assertEqual(len(self.controller.members()), 2)
        # CP advances the durable cursor across contiguous receipts in state
        # order, so one terminal ACK is now 3-4 posts with the envelope id.
        self.assertEqual(
            [ack["state"] for ack in transport.acks],
            ["received", "validated", "applied", "effective"],
        )
        for ack in transport.acks:
            self.assertEqual(
                {key: ack[key] for key in IDENTITY_KEYS},
                {key: envelope[key] for key in IDENTITY_KEYS},
            )
            self.assertEqual(ack["result"], "ok")
            self.assertIn("actualAt", ack)

    def test_members_requires_cp_acl(self):
        with self.assertRaisesRegex(ValueError, "requires a CP ACL snapshot"):
            self.controller.apply_cp_operation(
                str(uuid4()), members_op(ADMIN, [ADMIN]), None
            )

    def test_foreign_audience_never_executes(self):
        transport = FakeTransport(
            snapshot_doc(),
            [
                frame(
                    {"kind": "message", "actor": ADMIN, "message": "hi", "target": "*"},
                    1,
                    agent="someone-else",
                )
            ],
        )
        result = self.client(transport).sync(self.controller)
        self.assertEqual(result["applied"], 0)
        self.assertEqual(self.controller.message, self.controller.default_message)

    def test_stale_generation_rejected(self):
        transport = FakeTransport(
            snapshot_doc(),
            [
                frame(
                    {"kind": "message", "actor": ADMIN, "message": "hi", "target": "*"},
                    1,
                    generation="gen-666",
                )
            ],
        )
        result = self.client(transport).sync(self.controller)
        self.assertEqual((result["applied"], result["rejected"]), (0, 1))
        self.assertEqual(transport.acks[0]["state"], "rejected")
        self.assertEqual(transport.acks[0]["result"], "stale_generation")

    def test_expired_op_rejected(self):
        transport = FakeTransport(
            snapshot_doc(),
            [
                frame(
                    {"kind": "message", "actor": ADMIN, "message": "hi", "target": "*"},
                    1,
                    expires_at=NOW_MS - 1,
                )
            ],
        )
        result = self.client(transport).sync(self.controller)
        self.assertEqual((result["applied"], result["rejected"]), (0, 1))

    def test_hash_mismatch_rejected(self):
        transport = FakeTransport(
            snapshot_doc(),
            [
                frame(
                    {"kind": "message", "actor": ADMIN, "message": "hi", "target": "*"},
                    1,
                    tamper_hash=True,
                )
            ],
        )
        result = self.client(transport).sync(self.controller)
        self.assertEqual((result["applied"], result["rejected"]), (0, 1))
        self.assertEqual(self.controller.message, self.controller.default_message)

    def test_unsupported_kind_rejected(self):
        transport = FakeTransport(
            snapshot_doc(),
            [frame({"kind": "map-create", "actor": ADMIN, "target": "*"}, 1)],
        )
        result = self.client(transport).sync(self.controller)
        self.assertEqual((result["applied"], result["rejected"]), (0, 1))

    def test_epoch_mismatch_refetches_snapshot(self):
        transport = FakeTransport(
            snapshot_doc(),
            [
                frame(
                    {"kind": "message", "actor": ADMIN, "message": "hi", "target": "*"},
                    1,
                )
            ],
            fail_events_once=True,
        )
        result = self.client(transport).sync(self.controller)
        self.assertEqual(result["applied"], 1)
        self.assertEqual(transport.events_calls, 2)
        self.assertEqual(self.controller.message, "hi")

    def test_duplicate_op_not_reapplied(self):
        envelope = frame(
            {"kind": "message", "actor": ADMIN, "message": "hi", "target": "*"}, 1
        )
        transport = FakeTransport(snapshot_doc(), [envelope, envelope])
        result = self.client(transport).sync(self.controller)
        self.assertEqual(result["applied"], 1)
        self.assertEqual(self.controller.message, "hi")

    def test_redelivered_op_resends_ack_without_effect(self):
        envelope = frame(members_op(ADMIN, [ADMIN]), 1)
        transport = FakeTransport(snapshot_doc(), [envelope])
        client = self.client(transport)
        self.assertEqual(client.sync(self.controller)["applied"], 1)
        # Simulate a restart after a lost cursor advance: same op id, still terminal.
        state = json.loads((self.dir / "state" / "cp_state.json").read_bytes())
        state["cursor"] = 0
        (self.dir / "state" / "cp_state.json").write_bytes(json.dumps(state).encode())
        transport.event_frames = [envelope]
        before = list(self.controller.members())
        client = self.client(transport)  # fresh process: no in-memory cursor
        result = client.sync(self.controller)
        self.assertEqual(result, {"applied": 0, "rejected": 0, "acked": 1})
        self.assertEqual(list(self.controller.members()), before)

    def test_ack_carries_envelope_identity(self):
        envelope = frame(
            {"kind": "message", "actor": ADMIN, "message": "yo", "target": "*"}, 1
        )
        transport = FakeTransport(snapshot_doc(), [envelope])
        result = self.client(transport).sync(self.controller)
        self.assertEqual(result["applied"], 1)
        received = transport.acks[0]
        self.assertEqual(received["state"], "received")
        self.assertEqual(
            {key: received[key] for key in IDENTITY_KEYS},
            {key: envelope[key] for key in IDENTITY_KEYS},
        )
        self.assertEqual(received["result"], "ok")
        self.assertIn("actualAt", received)

    def test_nonexecutable_frame_rejected_without_effect(self):
        envelope = frame(
            {"kind": "message", "actor": ADMIN, "message": "yo", "target": "*"},
            1,
            executable=False,
        )
        transport = FakeTransport(snapshot_doc(), [envelope])
        result = self.client(transport).sync(self.controller)
        self.assertEqual(result, {"applied": 0, "rejected": 1, "acked": 1})
        self.assertEqual(self.controller.message, self.controller.default_message)
        self.assertEqual(transport.acks[0]["state"], "rejected")

    def test_recorded_real_cp_frame_applies(self):
        """Wire proof: real CP payload + hash from the live round trip
        against cp#23 (frames2.json, Boat VM 2026-11-06), re-keyed to the
        test stream (audience/epoch/generation/expiry are envelope routing,
        the hash covers the payload only). The payload carries the new
        target field; the actor is bootstrapped as a local member."""
        envelope = {
            "audience": AGENT,
            "authorizationEpoch": 1,
            "epoch": EPOCH,
            "executable": True,
            "expiresAt": datetime.fromtimestamp(
                NOW_MS / 1000 + 60, tz=timezone.utc
            ).isoformat(),
            "generation": GENERATION,
            "operationId": "ed2f93a8-aaa3-44f1-8bcc-9d6f52506771",
            "payload": {
                "actor": {
                    "draslId": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                    "name": "Helper",
                },
                "kind": "message",
                "message": "Целевое сообщение *",
                "target": "*",
            },
            "payloadHash": "d1155838d0f967f976763f41d97d21474982a1ce41ac12aa64cb852cb92ffc75",
            "reason": None,
            "revision": None,
            "schema": 2,
            "sequence": 1,
        }
        self.controller.set_members(
            [ADMIN, {"draslId": "a" * 32, "name": "Helper", "role": "builder"}]
        )
        transport = FakeTransport(snapshot_doc(), [envelope])
        result = self.client(transport).sync(self.controller)
        self.assertEqual(result, {"applied": 1, "rejected": 0, "acked": 1})
        self.assertEqual(self.controller.message, "Целевое сообщение *")

    def test_cursor_persisted(self):
        transport = FakeTransport(
            snapshot_doc(),
            [
                frame(
                    {"kind": "message", "actor": ADMIN, "message": "a", "target": "*"},
                    1,
                )
            ],
        )
        self.client(transport).sync(self.controller)
        state = json.loads((self.dir / "state" / "cp_state.json").read_bytes())
        self.assertEqual(
            (state["epoch"], state["generation"], state["cursor"]),
            (EPOCH, GENERATION, 1),
        )

    # Bot-review findings below: each failed on the pre-fix code.

    def test_held_cancel_not_reapplied_on_retry(self):
        """A gate-held cancel stores its terminal receipt before holding the
        cursor, so the next pass re-sends ACKs instead of re-applying the op
        (a retried cancel would fence and park the workshop in ERROR)."""
        envelope = frame({"kind": "cancel", "actor": ADMIN, "target": "*"}, 1)
        transport = FakeTransport(snapshot_doc(), [envelope])

        class GateHeldController:
            def __init__(self):
                self.pending = True
                self.applies = 0

            def apply_cp_releases(self, pending):
                return None

            def members(self):
                return [ADMIN]

            def gate_report_pending(self):
                return self.pending

            def apply_cp_operation(self, operation_id, operation, acl):
                self.applies += 1
                return {"kind": "cancel"}

        stub = GateHeldController()
        first = self.client(transport).sync(stub)
        self.assertEqual(stub.applies, 1)
        self.assertEqual(first, {"applied": 0, "rejected": 0, "acked": 0})
        self.assertEqual(transport.acks, [])
        # Same frame redelivered (cursor held): no re-apply, progress re-ACKed.
        second = self.client(transport).sync(stub)
        self.assertEqual(stub.applies, 1)
        self.assertEqual(second, {"applied": 0, "rejected": 0, "acked": 0})
        self.assertEqual(
            [ack["state"] for ack in transport.acks],
            ["received", "validated", "applied"],
        )
        # Once the leg is delivered, the replay converges on effective.
        stub.pending = False
        third = self.client(transport).sync(stub)
        self.assertEqual(stub.applies, 1)
        self.assertEqual(third, {"applied": 0, "rejected": 0, "acked": 1})
        self.assertEqual(
            [ack["state"] for ack in transport.acks[-4:]],
            ["received", "validated", "applied", "effective"],
        )

    def test_cp_members_keeps_admin(self):
        only_builders = [
            {"draslId": DRASL, "name": "Nether", "role": "builder"},
            {"draslId": STRANGER, "name": "Stranger", "role": "builder"},
        ]
        transport = FakeTransport(
            snapshot_doc(), [frame(members_op(ADMIN, only_builders), 1)]
        )
        result = self.client(transport).sync(self.controller)
        self.assertEqual(result, {"applied": 0, "rejected": 1, "acked": 1})
        self.assertEqual(self.controller.members(), [ADMIN])

    def test_stranger_message_rejected_without_snapshot_acl(self):
        doc = {"epoch": EPOCH, "generation": GENERATION, "cursor": 0}
        stranger = {
            "kind": "message",
            "actor": {"draslId": STRANGER, "name": "Stranger"},
            "message": "hi",
            "target": "*",
        }
        transport = FakeTransport(doc, [frame(stranger, 1)])
        result = self.client(transport).sync(self.controller)
        self.assertEqual(result, {"applied": 0, "rejected": 1, "acked": 1})
        self.assertEqual(self.controller.message, self.controller.default_message)
        mine = frame(
            {"kind": "message", "actor": ADMIN, "message": "yo", "target": "*"}, 2
        )
        result = self.client(FakeTransport(doc, [mine])).sync(self.controller)
        self.assertEqual(result["applied"], 1)
        self.assertEqual(self.controller.message, "yo")

    def test_sse_timeout_keeps_parsed_frames(self):
        import socket

        from cp_client import _read_sse

        lines = [b'data: {"a": 1}\n', b"\n", b'data: {"b": 2}\n', b"\n"]

        class Stub:
            def __init__(self):
                self.calls = 0

            def readline(self, *args):
                if self.calls < len(lines):
                    line = lines[self.calls]
                    self.calls += 1
                    return line
                raise socket.timeout("timed out")

        self.assertEqual(_read_sse(Stub(), timeout=5.0), [{"a": 1}, {"b": 2}])

    def test_oversize_snapshot_is_explicit_error(self):
        from cp_client import MAX_SNAPSHOT_BYTES, _read_capped

        class Stub:
            def __init__(self, body):
                self.body = body

            def read(self, n=-1):
                return self.body if n < 0 else self.body[:n]

        self.assertEqual(
            _read_capped(Stub(b'{"a": 1}'), MAX_SNAPSHOT_BYTES, "snapshot"),
            b'{"a": 1}',
        )
        with self.assertRaisesRegex(ValueError, "exceeds"):
            _read_capped(
                Stub(b"x" * (MAX_SNAPSHOT_BYTES + 1)), MAX_SNAPSHOT_BYTES, "snapshot"
            )

    def test_oversize_sse_frame_is_explicit_error(self):
        from cp_client import _read_sse

        big = b"data: " + b"x" * 70000 + b"\n"

        class Stub:
            def __init__(self):
                self.calls = 0

            def readline(self, *args):
                self.calls += 1
                return big if self.calls == 1 else b""

        with self.assertRaisesRegex(ValueError, "exceeds"):
            _read_sse(Stub(), timeout=5.0)

    def test_apply_runtime_error_rejected_and_advances(self):
        from unittest import mock

        first = frame(
            {"kind": "message", "actor": ADMIN, "message": "a", "target": "*"}, 1
        )
        second = frame(
            {"kind": "message", "actor": ADMIN, "message": "b", "target": "*"}, 2
        )
        transport = FakeTransport(snapshot_doc(), [first, second])
        real_apply = self.controller.apply_cp_operation
        calls = []

        def flaky(operation_id, operation, acl):
            calls.append(operation.get("message"))
            if len(calls) == 1:
                raise RuntimeError("preflight exploded")
            return real_apply(operation_id, operation, acl)

        with mock.patch.object(
            self.controller, "apply_cp_operation", side_effect=flaky
        ):
            result = self.client(transport).sync(self.controller)
        self.assertEqual(result, {"applied": 1, "rejected": 1, "acked": 2})
        self.assertEqual(self.controller.message, "b")

    def test_apply_blocking_error_retries_next_pass(self):
        from unittest import mock

        envelope = frame(
            {"kind": "message", "actor": ADMIN, "message": "a", "target": "*"}, 1
        )
        transport = FakeTransport(snapshot_doc(), [envelope])
        with mock.patch.object(
            self.controller, "apply_cp_operation", side_effect=BlockingIOError("busy")
        ):
            result = self.client(transport).sync(self.controller)
        self.assertEqual(result, {"applied": 0, "rejected": 0, "acked": 0})
        state = json.loads((self.dir / "state" / "cp_state.json").read_bytes())
        self.assertEqual(state["cursor"], 0)
        result = self.client(transport).sync(self.controller)
        self.assertEqual(result, {"applied": 1, "rejected": 0, "acked": 1})

    def test_frame_without_operation_id_rejected(self):
        envelope = frame(
            {"kind": "message", "actor": ADMIN, "message": "hi", "target": "*"}, 1
        )
        del envelope["operationId"]
        transport = FakeTransport(snapshot_doc(), [envelope])
        result = self.client(transport).sync(self.controller)
        self.assertEqual(result["applied"], 0)
        self.assertEqual(self.controller.message, self.controller.default_message)

    def test_generation_mismatch_refetches_snapshot(self):
        gen2 = "gen-8"
        docs = [
            {"epoch": EPOCH, "generation": GENERATION, "cursor": 0, "acl": [ADMIN]},
            {"epoch": EPOCH, "generation": gen2, "cursor": 0, "acl": [ADMIN]},
        ]
        transport = FakeTransport(
            docs[0],
            [
                frame(
                    {"kind": "message", "actor": ADMIN, "message": "hi", "target": "*"},
                    1,
                    generation=gen2,
                )
            ],
        )
        calls = {"n": 0}

        def get_json(url, timeout):
            calls["n"] += 1
            return json.dumps(docs[min(calls["n"], 2) - 1]).encode()

        transport.get_json = get_json
        config = CPConfig(
            base_url="https://cp.invalid", agent_id=AGENT, token="t", generation=""
        )
        client = CPClient(
            config, self.dir / "state", transport=transport, now_ms=lambda: NOW_MS
        )
        result = client.sync(self.controller)
        self.assertEqual(result, {"applied": 1, "rejected": 0, "acked": 1})
        self.assertEqual(calls["n"], 2)
        self.assertEqual(self.controller.message, "hi")

    def test_persistent_generation_mismatch_rejected(self):
        doc = {"epoch": EPOCH, "generation": GENERATION, "cursor": 0, "acl": [ADMIN]}
        transport = FakeTransport(
            doc,
            [
                frame(
                    {"kind": "message", "actor": ADMIN, "message": "hi", "target": "*"},
                    1,
                    generation="gen-9",
                )
            ],
        )
        calls = {"n": 0}
        orig_get = transport.get_json

        def get_json(url, timeout):
            calls["n"] += 1
            return orig_get(url, timeout)

        transport.get_json = get_json
        config = CPConfig(
            base_url="https://cp.invalid", agent_id=AGENT, token="t", generation=""
        )
        client = CPClient(
            config, self.dir / "state", transport=transport, now_ms=lambda: NOW_MS
        )
        result = client.sync(self.controller)
        self.assertEqual(result, {"applied": 0, "rejected": 1, "acked": 1})
        self.assertEqual(calls["n"], 2)

    def test_scoped_target_accepted(self):
        envelope = frame(
            {
                "kind": "message",
                "actor": ADMIN,
                "message": "hub news",
                "target": "hub",
            },
            1,
        )
        transport = FakeTransport(snapshot_doc(), [envelope])
        result = self.client(transport).sync(self.controller)
        self.assertEqual(result, {"applied": 1, "rejected": 0, "acked": 1})
        self.assertEqual(self.controller.message, "hub news")

    def test_foreign_target_rejected(self):
        envelope = frame(
            {
                "kind": "message",
                "actor": ADMIN,
                "message": "hi",
                "target": "other-resource",
            },
            1,
        )
        transport = FakeTransport(snapshot_doc(), [envelope])
        result = self.client(transport).sync(self.controller)
        self.assertEqual(result, {"applied": 0, "rejected": 1, "acked": 1})
        self.assertEqual(self.controller.message, self.controller.default_message)
        self.assertEqual(transport.acks[0]["state"], "rejected")

    def test_missing_target_rejected(self):
        envelope = frame({"kind": "message", "actor": ADMIN, "message": "hi"}, 1)
        transport = FakeTransport(snapshot_doc(), [envelope])
        result = self.client(transport).sync(self.controller)
        self.assertEqual(result, {"applied": 0, "rejected": 1, "acked": 1})
        self.assertEqual(self.controller.message, self.controller.default_message)

    def test_members_foreign_target_rejected(self):
        newcomer = {"draslId": STRANGER, "name": "Stranger", "role": "builder"}
        transport = FakeTransport(
            snapshot_doc(),
            [frame(members_op(ADMIN, [ADMIN, newcomer], target="other-resource"), 1)],
        )
        result = self.client(transport).sync(self.controller)
        self.assertEqual(result, {"applied": 0, "rejected": 1, "acked": 1})
        self.assertEqual(self.controller.members(), [ADMIN])

    def test_scoped_members_maps_rejected(self):
        entries = [
            dict(ADMIN),
            {"draslId": STRANGER, "name": "Stranger", "maps": ["hub"]},
        ]
        transport = FakeTransport(
            snapshot_doc(), [frame(members_op(ADMIN, entries, target="hub"), 1)]
        )
        result = self.client(transport).sync(self.controller)
        self.assertEqual(result, {"applied": 0, "rejected": 1, "acked": 1})
        self.assertEqual(self.controller.members(), [ADMIN])

    def test_scoped_members_without_maps_applied(self):
        newcomer = {"draslId": STRANGER, "name": "Stranger", "role": "builder"}
        transport = FakeTransport(
            snapshot_doc(),
            [frame(members_op(ADMIN, [ADMIN, newcomer], target="hub"), 1)],
        )
        result = self.client(transport).sync(self.controller)
        self.assertEqual(result, {"applied": 1, "rejected": 0, "acked": 1})
        self.assertEqual(len(self.controller.members()), 2)

    # Bot-review round 2 below: each failed on the pre-fix code.

    def test_acl_refreshes_after_members_apply(self):
        newcomer = {"draslId": STRANGER, "name": "Stranger", "role": "builder"}
        transport = FakeTransport(
            snapshot_doc(),
            [
                frame(members_op(ADMIN, [ADMIN, newcomer]), 1),
                frame(
                    {
                        "kind": "message",
                        "actor": {"draslId": STRANGER, "name": "Stranger"},
                        "message": "hello",
                        "target": "*",
                    },
                    2,
                ),
            ],
        )
        result = self.client(transport).sync(self.controller)
        self.assertEqual(result, {"applied": 2, "rejected": 0, "acked": 2})
        self.assertEqual(self.controller.message, "hello")

    def test_members_export_failure_still_commits(self):
        from unittest import mock

        newcomer = {"draslId": STRANGER, "name": "Stranger", "role": "builder"}
        with mock.patch.object(
            self.controller, "_export", side_effect=OSError("disk full")
        ):
            result = self.controller.apply_cp_operation(
                str(uuid4()), members_op(ADMIN, [ADMIN, newcomer]), [ADMIN]
            )
        self.assertEqual(result, {"kind": "members", "count": 2})
        self.assertEqual(len(self.controller.members()), 2)

    def test_https_required_for_cp_url(self):
        with self.assertRaisesRegex(ValueError, "https"):
            CPConfig.from_env(
                {
                    "BLOCKFIELD_CP_URL": "http://cp.internal",
                    "BLOCKFIELD_CP_AGENT_ID": AGENT,
                    "BLOCKFIELD_CP_AGENT_TOKEN": "t",
                }
            )

    def test_loopback_http_allowed_for_cp_url(self):
        for base in ("http://127.0.0.1:9", "http://localhost:9"):
            config = CPConfig.from_env(
                {
                    "BLOCKFIELD_CP_URL": base,
                    "BLOCKFIELD_CP_AGENT_ID": AGENT,
                    "BLOCKFIELD_CP_AGENT_TOKEN": "t",
                }
            )
            self.assertEqual(config.base_url, base)

    def test_redirect_refused_without_bearer(self):
        import http.server
        import os
        import threading

        saved = {}
        for var in (
            "http_proxy",
            "https_proxy",
            "all_proxy",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "no_proxy",
        ):
            saved[var] = os.environ.pop(var, None)
        os.environ["no_proxy"] = "127.0.0.1,localhost"
        try:
            outer_seen = {}

            class Handler(http.server.BaseHTTPRequestHandler):
                def do_GET(self):
                    if self.path == "/cp":
                        self.send_response(302)
                        self.send_header(
                            "Location",
                            f"http://127.0.0.1:{self.server.server_port}/target",
                        )
                        self.end_headers()
                    else:
                        outer_seen[self.path] = self.headers.get("Authorization")
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.end_headers()
                        self.wfile.write(b"{}")

                def log_message(self, *args):
                    pass

            server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
            thread = threading.Thread(
                target=server.serve_forever, kwargs={"poll_interval": 0.05}
            )
            thread.daemon = True
            thread.start()
            try:
                from cp_client import CPHttpError, _UrllibTransport

                transport = _UrllibTransport("secret-token")
                with self.assertRaises(CPHttpError):
                    transport.get_json(
                        f"http://127.0.0.1:{server.server_port}/cp", timeout=5.0
                    )
                self.assertNotIn("/target", outer_seen)
            finally:
                server.shutdown()
                thread.join(timeout=10)
                server.server_close()
        finally:
            os.environ.pop("no_proxy", None)
            for var, value in saved.items():
                if value is not None:
                    os.environ[var] = value

    def test_request_body_raises_on_non_2xx(self):
        from cp_client import CPHttpError, MAX_SNAPSHOT_BYTES, _request_body

        class StubResponse:
            def __init__(self, status, body):
                self.status = status
                self.body = body

            def read(self, n=-1):
                return self.body if n < 0 else self.body[:n]

        class Ctx:
            def __init__(self, response):
                self.response = response

            def __enter__(self):
                return self.response

            def __exit__(self, *args):
                return False

        class StubOpener:
            def __init__(self, status, body):
                self.status = status
                self.body = body

            def open(self, request, timeout=None):
                return Ctx(StubResponse(self.status, self.body))

        body = _request_body(
            StubOpener(200, b'{"a": 1}'), None, 5.0, "snapshot", MAX_SNAPSHOT_BYTES
        )
        self.assertEqual(body, b'{"a": 1}')
        with self.assertRaises(CPHttpError) as ctx:
            _request_body(
                StubOpener(503, b"oops"), None, 5.0, "snapshot", MAX_SNAPSHOT_BYTES
            )
        self.assertEqual(ctx.exception.status, 503)
        with self.assertRaisesRegex(ValueError, "exceeds"):
            _request_body(
                StubOpener(200, b"x" * (MAX_SNAPSHOT_BYTES + 1)),
                None,
                5.0,
                "snapshot",
                MAX_SNAPSHOT_BYTES,
            )

    def test_failed_nack_holds_cursor(self):
        from cp_client import CPHttpError

        envelope = frame(
            {"kind": "message", "actor": ADMIN, "message": "hi"},
            1,
            agent="someone-else",
        )
        transport = FakeTransport(snapshot_doc(), [envelope])

        def boom(url, body, timeout):
            raise CPHttpError("acks", 503)

        transport.post_json = boom
        result = self.client(transport).sync(self.controller)
        self.assertEqual(result, {"applied": 0, "rejected": 1, "acked": 0})
        state = json.loads((self.dir / "state" / "cp_state.json").read_bytes())
        self.assertEqual(state["cursor"], 0)

    def test_applied_ack_503_surfaces(self):
        import http.server
        import os
        import threading

        saved = {}
        for var in (
            "http_proxy",
            "https_proxy",
            "all_proxy",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "no_proxy",
        ):
            saved[var] = os.environ.pop(var, None)
        os.environ["no_proxy"] = "127.0.0.1,localhost"
        try:

            class Handler(http.server.BaseHTTPRequestHandler):
                def do_POST(self):
                    length = int(self.headers.get("Content-Length", 0))
                    self.rfile.read(length)
                    self.send_response(503)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b"{}")

                def log_message(self, *args):
                    pass

            server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
            thread = threading.Thread(
                target=server.serve_forever, kwargs={"poll_interval": 0.05}
            )
            thread.daemon = True
            thread.start()
            try:
                from cp_client import CPHttpError, _UrllibTransport

                envelope = frame(
                    {"kind": "message", "actor": ADMIN, "message": "hi"}, 1
                )
                transport = FakeTransport(snapshot_doc(), [envelope])
                real = _UrllibTransport("t")
                acks_url = f"http://127.0.0.1:{server.server_port}/acks"
                transport.post_json = lambda url, body, timeout: real.post_json(
                    acks_url, body, timeout
                )
                with self.assertRaises(CPHttpError) as ctx:
                    self.client(transport).sync(self.controller)
                self.assertEqual(ctx.exception.status, 503)
                state = json.loads((self.dir / "state" / "cp_state.json").read_bytes())
                self.assertEqual(state["cursor"], 0)
            finally:
                server.shutdown()
                thread.join(timeout=10)
                server.server_close()
        finally:
            os.environ.pop("no_proxy", None)
            for var, value in saved.items():
                if value is not None:
                    os.environ[var] = value

    def test_malformed_block_stops_read_keeps_prefix(self):
        from cp_client import _read_sse

        first = frame(
            {"kind": "message", "actor": ADMIN, "message": "a", "target": "*"}, 1
        )
        second = frame(
            {"kind": "message", "actor": ADMIN, "message": "b", "target": "*"}, 2
        )
        lines = [
            "data: " + json.dumps(first),
            "",
            "data: {truncated",
            "",
            "data: " + json.dumps(second),
            "",
        ]

        class StubResponse:
            def __init__(self):
                self.calls = 0

            def readline(self, *args):
                if self.calls < len(lines):
                    line = lines[self.calls].encode()
                    self.calls += 1
                    return line + b"\n"
                return b""

        frames = _read_sse(StubResponse(), timeout=5.0)
        self.assertEqual(frames, [first])
        transport = FakeTransport(snapshot_doc(), frames)
        result = self.client(transport).sync(self.controller)
        self.assertEqual(result, {"applied": 1, "rejected": 0, "acked": 1})
        self.assertEqual(self.controller.message, "a")
        state = json.loads((self.dir / "state" / "cp_state.json").read_bytes())
        self.assertEqual(state["cursor"], 1)

    def test_ack_failure_does_not_reapply(self):
        from controller import Goal
        from cp_client import CPHttpError

        self.controller.request(
            str(uuid4()), Goal("workshop", "v1.0.0", str(uuid4())), 0
        )
        self.assertEqual(self.controller.step()["phase"], "STARTING")
        envelope = frame({"kind": "cancel", "actor": ADMIN, "target": "*"}, 1)
        transport = FakeTransport(snapshot_doc(), [envelope])
        calls = {"n": 0}
        orig_post = transport.post_json

        def flaky_post(url, body, timeout):
            calls["n"] += 1
            if calls["n"] == 2:
                raise CPHttpError("acks", 503)
            return orig_post(url, body, timeout)

        transport.post_json = flaky_post
        with self.assertRaises(CPHttpError):
            self.client(transport).sync(self.controller)
        receipts = json.loads((self.dir / "state" / "cp_receipts.json").read_bytes())
        self.assertEqual(receipts[envelope["operationId"]]["state"], "effective")
        state = json.loads((self.dir / "state" / "cp_state.json").read_bytes())
        self.assertEqual(state["cursor"], 0)
        # Second pass: the stored receipt re-sends ACKs; cancel must not run
        # again (it would raise "nothing to cancel" and record rejected).
        transport.post_json = orig_post
        result = self.client(transport).sync(self.controller)
        self.assertEqual(result, {"applied": 0, "rejected": 0, "acked": 1})

    def test_failed_gate_report_blocks_effective_ack(self):
        """A members op whose gate leg cannot be delivered is applied locally
        but never ACKed effective; the retry delivers and ACKs."""
        from controller import member as member_fn

        class BoomReporter:
            def report(self, leg):
                raise RuntimeError("cp down")

        self.controller.gate_reporter = BoomReporter()
        op = members_op(
            {"draslId": DRASL, "name": "Nether"},
            [dict(ADMIN), {"draslId": STRANGER, "name": "Stranger", "role": "builder"}],
        )
        envelope = frame(op, 1)
        transport = FakeTransport(snapshot_doc(), [envelope])
        result = self.client(transport).sync(self.controller)
        # Held: applied locally, nothing ACKed, cursor unmoved.
        self.assertEqual(result, {"applied": 0, "rejected": 0, "acked": 0})
        self.assertEqual(transport.acks, [])
        self.assertTrue(self.controller.gate_report_pending())
        self.assertTrue((self.dir / "state" / "gate_pending.json").exists())
        state = json.loads((self.dir / "state" / "cp_state.json").read_bytes())
        self.assertEqual(state["cursor"], 0)
        # Members are committed even though the ACK is held.
        self.assertEqual(
            {m["draslId"] for m in self.controller.members()},
            {
                member_fn({**ADMIN, "role": "builder"})["draslId"],
                STRANGER,
            },
        )
        # Next pass with a working reporter delivers the leg, then ACKs
        # the op effective without re-applying it.
        gate = GateStubTransport(revision=0)
        self.controller.gate_reporter = GateReporter(
            "https://cp.invalid", "gate-token", transport=gate
        )
        members_before = list(self.controller.members())
        result = self.client(transport).sync(self.controller)
        self.assertEqual(result, {"applied": 0, "rejected": 0, "acked": 1})
        self.assertEqual(
            [ack["state"] for ack in transport.acks],
            ["received", "validated", "applied", "effective"],
        )
        self.assertEqual(list(self.controller.members()), members_before)
        self.assertFalse(self.controller.gate_report_pending())
        self.assertFalse((self.dir / "state" / "gate_pending.json").exists())
        state = json.loads((self.dir / "state" / "cp_state.json").read_bytes())
        self.assertEqual(state["cursor"], 1)

    def test_stranger_message_rejected_without_acl(self):
        stranger = {
            "kind": "message",
            "actor": {"draslId": STRANGER, "name": "Stranger"},
            "message": "hi",
            "target": "*",
        }
        with self.assertRaisesRegex(ValueError, "not a workshop member"):
            self.controller.apply_cp_operation(str(uuid4()), stranger, None)
        self.assertEqual(self.controller.message, self.controller.default_message)


class GateStubTransport:
    """Stub CP settings endpoint: GET returns the stored leg, PATCH advances it."""

    url = "https://cp.invalid/v2/settings/gate/workshop"

    def __init__(self, revision=0, value=None):
        self.revision = revision
        self.value = (
            value
            if value is not None
            else {"state": "closed", "message": "", "allowed": []}
        )
        self.patches = []
        self.fail_patch_once_with = None

    def get_json(self, url, timeout):
        assert url == self.url, url
        return json.dumps(
            {
                "initialized": True,
                "revision": self.revision,
                "value": self.value,
                "hash": "0" * 64,
                "application": "see operation receipts",
            }
        ).encode()

    def patch_json(self, url, body, timeout):
        assert url == self.url, url
        self.patches.append(body)
        if self.fail_patch_once_with is not None:
            status, self.fail_patch_once_with = self.fail_patch_once_with, None
            raise CPHttpError("gate", status)
        assert body["expectedRevision"] == self.revision, body
        self.revision += 1
        self.value = body["patch"]
        return json.dumps(
            {
                "operationId": str(uuid4()),
                "revision": self.revision,
                "status": "accepted",
                "result": {"revision": self.revision},
            }
        ).encode()


class GateReporterTests(unittest.TestCase):
    LEG = {
        "state": "builders",
        "message": "night build",
        "allowed": [{"draslId": DRASL, "name": "Nether"}],
    }

    def reporter(self, transport):
        return GateReporter("https://cp.invalid", "gate-token", transport=transport)

    def test_no_write_when_unchanged(self):
        transport = GateStubTransport(revision=4, value=dict(self.LEG))
        result = self.reporter(transport).report(dict(self.LEG))
        self.assertEqual(result, {"reported": False, "revision": 4})
        self.assertEqual(transport.patches, [])

    def test_patch_on_change(self):
        transport = GateStubTransport(revision=3)
        result = self.reporter(transport).report(dict(self.LEG))
        self.assertEqual(result["reported"], True)
        self.assertEqual(result["revision"], 4)
        (body,) = transport.patches
        self.assertEqual(body["expectedRevision"], 3)
        self.assertEqual(body["patch"], self.LEG)
        self.assertEqual(body["environment"], "prod")
        self.assertEqual(body["backend"], "game")
        self.assertTrue(body["idempotencyKey"].startswith("workshop-gate-3-"))

    def test_conflict_refetches_once_and_retries(self):
        transport = GateStubTransport(revision=3)
        other = {"state": "closed", "message": "admin", "allowed": []}
        real_patch = transport.patch_json
        calls = {"n": 0}

        def racing_patch(url, body, timeout):
            calls["n"] += 1
            if calls["n"] == 1:
                # Another writer lands between our read and our patch.
                transport.revision = 4
                transport.value = dict(other)
                raise CPHttpError("gate", 409)
            return real_patch(url, body, timeout)

        transport.patch_json = racing_patch
        result = self.reporter(transport).report(dict(self.LEG))
        self.assertEqual(result["reported"], True)
        self.assertEqual(result["revision"], 5)
        self.assertEqual([b["expectedRevision"] for b in transport.patches], [4])

    def test_conflict_resolved_when_other_writer_matches(self):
        transport = GateStubTransport(revision=3)
        transport.fail_patch_once_with = 409
        calls = {"n": 0}
        orig_get = transport.get_json

        def racing_get(url, timeout):
            calls["n"] += 1
            if calls["n"] == 2:
                # The racing writer reported our exact leg: no write needed.
                transport.revision = 4
                transport.value = dict(self.LEG)
            return orig_get(url, timeout)

        transport.get_json = racing_get
        result = self.reporter(transport).report(dict(self.LEG))
        self.assertEqual(result, {"reported": False, "revision": 4})
        self.assertEqual(len(transport.patches), 1)

    def test_repeated_conflict_raises(self):
        transport = GateStubTransport(revision=3)

        def always_conflict(url, body, timeout):
            raise CPHttpError("gate", 409)

        transport.patch_json = always_conflict
        with self.assertRaises(CPHttpError):
            self.reporter(transport).report(dict(self.LEG))

    def test_denied_credential_is_explicit(self):
        transport = GateStubTransport()

        def denied(url, timeout):
            raise CPHttpError("gate", 403)

        transport.get_json = denied
        with self.assertRaisesRegex(ValueError, "denied"):
            self.reporter(transport).report(dict(self.LEG))

    def test_from_env_disabled_without_token(self):
        self.assertIsNone(GateReporter.from_env({}))

    def test_from_env_rejects_plaintext(self):
        with self.assertRaises(ValueError):
            GateReporter.from_env(
                {
                    "BLOCKFIELD_CP_URL": "http://cp.invalid",
                    "BLOCKFIELD_CP_GATE_TOKEN": "t",
                }
            )

    def test_from_env_accepts_loopback_for_tests(self):
        reporter = GateReporter.from_env(
            {
                "BLOCKFIELD_CP_URL": "http://127.0.0.1:9",
                "BLOCKFIELD_CP_GATE_TOKEN": "t",
            }
        )
        self.assertEqual(
            reporter._url(), "http://127.0.0.1:9/v2/settings/gate/workshop"
        )


if __name__ == "__main__":
    unittest.main()
