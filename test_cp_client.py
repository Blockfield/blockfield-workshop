"""CP v2 subscribe/apply: addressed typed ops, CP ACL, generation ACKs (CP-S6a)."""

from release_fixtures import catalog

import json
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from cp_client import CPClient, CPConfig, SnapshotRequired, payload_hash
from controller import Controller

DRASL = "e10438e4797b49718cfecec6a8fa171b"
STRANGER = "ffffffffffffffffffffffffffffffff"
ADMIN = {"draslId": DRASL, "name": "Nether", "role": "admin"}
BUILDER = {"draslId": DRASL, "name": "Nether", "role": "builder"}
AGENT = "workshop-1"
EPOCH = "11111111-2222-3333-4444-555555555555"
GENERATION = "gen-7"
NOW_MS = 1796409600000


def frame(
    operation,
    seq,
    agent=AGENT,
    epoch=EPOCH,
    generation=GENERATION,
    expires_at=NOW_MS + 60000,
    tamper_hash=False,
):
    body = dict(operation)
    envelope = {
        "schema": 1,
        "epoch": epoch,
        "sequence": seq,
        "operationId": str(uuid4()),
        "audience": agent,
        "generation": generation,
        "revision": seq,
        "payloadHash": "0" * 64 if tamper_hash else payload_hash(body),
        "authEpoch": 1,
        "expiresAt": expires_at,
        "operation": body,
    }
    return envelope


def members_op(grantor, entries):
    return {"kind": "members", "grantor": grantor, "members": entries}


class FakeTransport:
    def __init__(self, snapshot_doc, event_frames=(), fail_events_once=False):
        self.snapshot_doc = snapshot_doc
        self.event_frames = list(event_frames)
        self.fail_events_once = fail_events_once
        self.acks = []
        self.events_calls = 0

    def get_json(self, url, timeout):
        return 200, json.dumps(self.snapshot_doc).encode()

    def post_json(self, url, body, timeout):
        self.acks.append(body)
        return 200, b"{}"

    def get_events(self, url, timeout):
        self.events_calls += 1
        if self.fail_events_once and self.events_calls == 1:
            raise SnapshotRequired("cp snapshot required")
        return 200, list(self.event_frames)


def snapshot_doc(cursor=0, acl=None):
    return {
        "epoch": EPOCH,
        "generation": GENERATION,
        "cursor": cursor,
        "acl": [ADMIN] if acl is None else acl,
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
        transport = FakeTransport(
            snapshot_doc(),
            [frame(members_op(ADMIN, [ADMIN, newcomer]), 1)],
        )
        result = self.client(transport).sync(self.controller)
        self.assertEqual(result, {"applied": 1, "rejected": 0, "acked": 1})
        self.assertEqual(len(self.controller.members()), 2)
        ack = transport.acks[0]
        self.assertEqual(ack["state"], "effective")
        self.assertIsNone(ack["actualGeneration"])  # FakeBackend has no readiness probe

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
                    {"kind": "message", "actor": ADMIN, "message": "hi"},
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
                    {"kind": "message", "actor": ADMIN, "message": "hi"},
                    1,
                    generation="gen-666",
                )
            ],
        )
        result = self.client(transport).sync(self.controller)
        self.assertEqual((result["applied"], result["rejected"]), (0, 1))
        self.assertEqual(transport.acks[0]["state"], "rejected")

    def test_expired_op_rejected(self):
        transport = FakeTransport(
            snapshot_doc(),
            [
                frame(
                    {"kind": "message", "actor": ADMIN, "message": "hi"},
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
                    {"kind": "message", "actor": ADMIN, "message": "hi"},
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
            [frame({"kind": "map-create", "actor": ADMIN}, 1)],
        )
        result = self.client(transport).sync(self.controller)
        self.assertEqual((result["applied"], result["rejected"]), (0, 1))

    def test_epoch_mismatch_refetches_snapshot(self):
        transport = FakeTransport(
            snapshot_doc(),
            [frame({"kind": "message", "actor": ADMIN, "message": "hi"}, 1)],
            fail_events_once=True,
        )
        result = self.client(transport).sync(self.controller)
        self.assertEqual(result["applied"], 1)
        self.assertEqual(transport.events_calls, 2)
        self.assertEqual(self.controller.message, "hi")

    def test_duplicate_op_not_reapplied(self):
        envelope = frame({"kind": "message", "actor": ADMIN, "message": "hi"}, 1)
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

    def test_ack_carries_readiness_generation(self):
        class ReadyBackend:
            def readiness_generation(self, profile):
                return "gen-7"

        controller = Controller(
            self.dir / "state2",
            ReadyBackend(),
            releases=catalog(),
        )
        controller.set_members([ADMIN])
        transport = FakeTransport(
            snapshot_doc(),
            [frame({"kind": "message", "actor": ADMIN, "message": "yo"}, 1)],
        )
        result = self.client(transport).sync(controller)
        self.assertEqual(result["applied"], 1)
        ack = transport.acks[0]
        self.assertEqual(ack["state"], "effective")
        self.assertEqual(ack["actualGeneration"], "gen-7")
        self.assertIn("actualAt", ack)

    def test_cursor_persisted(self):
        transport = FakeTransport(
            snapshot_doc(),
            [frame({"kind": "message", "actor": ADMIN, "message": "a"}, 1)],
        )
        self.client(transport).sync(self.controller)
        state = json.loads((self.dir / "state" / "cp_state.json").read_bytes())
        self.assertEqual(
            (state["epoch"], state["generation"], state["cursor"]),
            (EPOCH, GENERATION, 1),
        )

    # Bot-review findings below: each failed on the pre-fix code.

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
        }
        transport = FakeTransport(doc, [frame(stranger, 1)])
        result = self.client(transport).sync(self.controller)
        self.assertEqual(result, {"applied": 0, "rejected": 1, "acked": 1})
        self.assertEqual(self.controller.message, self.controller.default_message)
        mine = frame({"kind": "message", "actor": ADMIN, "message": "yo"}, 2)
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

        first = frame({"kind": "message", "actor": ADMIN, "message": "a"}, 1)
        second = frame({"kind": "message", "actor": ADMIN, "message": "b"}, 2)
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

        envelope = frame({"kind": "message", "actor": ADMIN, "message": "a"}, 1)
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
        envelope = frame({"kind": "message", "actor": ADMIN, "message": "hi"}, 1)
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
                    {"kind": "message", "actor": ADMIN, "message": "hi"},
                    1,
                    generation=gen2,
                )
            ],
        )
        calls = {"n": 0}

        def get_json(url, timeout):
            calls["n"] += 1
            return 200, json.dumps(docs[min(calls["n"], 2) - 1]).encode()

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
                    {"kind": "message", "actor": ADMIN, "message": "hi"},
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


if __name__ == "__main__":
    unittest.main()
