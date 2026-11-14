"""CP v2 central release records and the release.select operation (CP-S6b)."""

import json
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from release_fixtures import release_record

from controller import Controller, Goal
from cp_client import CPClient, CPConfig
from test_controller import FakeBackend
from test_cp_client import (
    ADMIN,
    AGENT,
    EPOCH,
    GENERATION,
    NOW_MS,
    FakeTransport,
    drive,
    frame,
)

BUILDER = {
    "draslId": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "name": "Helper",
    "role": "builder",
}


def snapshot_doc(cursor=0, releases=None):
    """Minimal real snapshot; releases stay accepted when present (tolerant read)."""
    doc = {
        "schema": 2,
        "audience": AGENT,
        "epoch": EPOCH,
        "generation": GENERATION,
        "cursor": cursor,
        "settings": [],
        "hash": "0" * 64,
    }
    if releases is not None:
        doc["releases"] = releases
    return doc


def select_op(release, record=None, target="*"):
    operation = {
        "kind": "release.select",
        "actor": dict(ADMIN),
        "release": release,
        "target": target,
    }
    if record is not None:
        operation["record"] = record
    return operation


class CPReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)
        self.backend = FakeBackend()
        self.backend.releases = {}
        self.releases_path = self.dir / "releases.json"
        self.controller = Controller(
            self.dir / "state",
            self.backend,
            gate_file=self.dir / "gate.json",
            public_file=self.dir / "public.json",
            admin_dir=self.dir / "admin",
            releases={},
            releases_path=self.releases_path,
        )
        self.controller.set_members([ADMIN])
        # A stable editor on v1.0.0: release.select switches it in its session.
        self.controller.apply_cp_releases([release_record("v1.0.0", "a")])
        self.controller.request(str(uuid4()), Goal("game", "v1.0.0"), 0)
        self.controller.step()
        self.backend.ready(Goal(**self.controller.status()["desired"]))
        self.controller.step()
        self.assertEqual(self.controller.status()["phase"], "workshop")
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

    def test_release_select_merges_central_record(self):
        record = release_record("v9.0.0", "d")
        transport = FakeTransport(
            snapshot_doc(), [frame(select_op("v9.0.0", record), 1)]
        )
        result = drive(self.client(transport), self.controller)
        self.assertEqual(result, {"applied": 1, "rejected": 0, "acked": 1})
        self.assertIn("v9.0.0", self.controller.releases)
        on_disk = json.loads(self.releases_path.read_bytes())
        self.assertEqual(on_disk["v9.0.0"]["imageDigest"], record["imageDigest"])
        self.assertIn("v9.0.0", self.backend.releases)
        self.assertEqual(
            [ack["state"] for ack in transport.acks],
            ["received", "validated", "applied", "effective"],
        )

    def test_expired_release_select_refused(self):
        record = release_record("v9.0.0", "d")
        valid = frame(select_op("v9.0.0", record), 1)
        stale = frame(
            select_op("v9.0.1", release_record("v9.0.1", "e")),
            2,
            expires_at=NOW_MS - 1000,
        )
        transport = FakeTransport(snapshot_doc(), [valid, stale])
        result = drive(self.client(transport), self.controller)
        self.assertEqual(result, {"applied": 1, "rejected": 1, "acked": 2})
        self.assertIn("v9.0.0", self.controller.releases)
        self.assertNotIn("v9.0.1", self.controller.releases)

    def test_replayed_release_select_has_no_second_effect(self):
        record = release_record("v9.0.0", "d")
        envelope = frame(select_op("v9.0.0", record), 1)
        transport = FakeTransport(snapshot_doc(), [envelope])
        first = drive(self.client(transport), self.controller)
        self.assertEqual(first, {"applied": 1, "rejected": 0, "acked": 1})
        before = self.releases_path.read_bytes()
        transport.event_frames = [envelope]
        second = drive(self.client(transport), self.controller)
        self.assertEqual(second, {"applied": 0, "rejected": 0, "acked": 0})
        self.assertEqual(self.releases_path.read_bytes(), before)

    def test_conflicting_record_refused(self):
        record = release_record("v9.0.0", "d")
        self.controller.apply_cp_operation(
            str(uuid4()), select_op("v9.0.0", record), [ADMIN]
        )
        rival = release_record("v9.0.0", "e")
        with self.assertRaisesRegex(ValueError, "identity changed"):
            self.controller.apply_cp_operation(
                str(uuid4()), select_op("v9.0.0", rival), [ADMIN]
            )
        self.assertEqual(
            self.controller.releases["v9.0.0"]["imageDigest"], record["imageDigest"]
        )

    def test_unknown_release_without_record_refused(self):
        with self.assertRaisesRegex(ValueError, "not a verified published release"):
            self.controller.apply_cp_operation(
                str(uuid4()), select_op("v9.9.9"), [ADMIN]
            )

    def test_release_select_needs_admin(self):
        record = release_record("v9.0.0", "d")
        outsider = {"draslId": "b" * 32, "name": "Outsider"}
        with self.assertRaisesRegex(ValueError, "not a workshop member"):
            self.controller.apply_cp_operation(
                str(uuid4()),
                dict(select_op("v9.0.0", record), actor=outsider),
                [ADMIN],
            )
        with self.assertRaisesRegex(ValueError, "not a workshop admin"):
            self.controller.apply_cp_operation(
                str(uuid4()),
                dict(select_op("v9.0.0", record), actor=BUILDER),
                [ADMIN, BUILDER],
            )
        with self.assertRaisesRegex(ValueError, "requires a CP ACL snapshot"):
            self.controller.apply_cp_operation(
                str(uuid4()), select_op("v9.0.0", record), None
            )
        self.assertNotIn("v9.0.0", self.controller.releases)

    def test_snapshot_releases_adopted(self):
        record = release_record("v9.0.0", "d")
        transport = FakeTransport(snapshot_doc(releases=[record]), [])
        result = drive(self.client(transport), self.controller)
        self.assertEqual(result, {"applied": 0, "rejected": 0, "acked": 0})
        self.assertIn("v9.0.0", self.controller.releases)
        self.assertIn("v9.0.0", self.backend.releases)

    def test_release_select_switches_the_session_and_reports(self):
        record = release_record("v9.0.0", "d")
        session = self.controller.status()["desired"]["session"]
        outcome = self.controller.apply_cp_operation(
            str(uuid4()), select_op("v9.0.0", record), [ADMIN]
        )
        self.assertEqual(outcome["payload"], {"release": "v9.0.0", "selected": True})
        desired = self.controller.status()["desired"]
        self.assertEqual((desired["release"], desired["session"]), ("v9.0.0", session))
        again = self.controller.apply_cp_operation(
            str(uuid4()), select_op("v9.0.0"), [ADMIN]
        )
        self.assertFalse(again["payload"]["selected"])

    def test_actorless_release_select_is_the_release_ci(self):
        record = release_record("v9.0.0", "d")
        operation = {"kind": "release.select", "target": "*", "release": "v9.0.0"}
        outcome = self.controller.apply_cp_operation(
            str(uuid4()), {**operation, "record": record}, None
        )
        self.assertTrue(outcome["payload"]["selected"])
        # The rollback path names the digest of an already approved release.
        digest = release_record("v1.0.0", "a")["imageDigest"].split("@", 1)[1]
        self.controller.step()
        self.backend.ready(Goal(**self.controller.status()["desired"]))
        self.controller.step()
        outcome = self.controller.apply_cp_operation(
            str(uuid4()), {**operation, "release": digest}, None
        )
        self.assertEqual(self.controller.status()["desired"]["release"], digest)

    def test_release_select_refuses_a_transition_in_flight(self):
        self.controller.apply_cp_operation(
            str(uuid4()), select_op("v9.0.0", release_record("v9.0.0", "d")), [ADMIN]
        )
        self.assertNotEqual(self.controller.status()["phase"], "workshop")
        with self.assertRaisesRegex(ValueError, "stable or sleeping workshop"):
            self.controller.apply_cp_operation(
                str(uuid4()), select_op("v1.0.0"), [ADMIN]
            )

    def test_cp_wiring(self):
        import workshopctl

        env = {
            "BF_STATE_DIR": str(self.dir / "ws"),
            "BF_GATE_FILE": str(self.dir / "ws-gate.json"),
            "BF_PUBLIC_FILE": str(self.dir / "ws-public.json"),
            "BF_ADMIN_DIR": str(self.dir / "ws-admin"),
            "BF_RELEASES_FILE": str(self.dir / "ws-releases.json"),
            "BF_WORKSHOP_DATA": str(self.dir / "ws-data"),
            "BLOCKFIELD_CP_URL": "https://cp.invalid",
            "BLOCKFIELD_CP_AGENT_ID": AGENT,
            "BLOCKFIELD_CP_AGENT_TOKEN": "t",
            "BLOCKFIELD_CP_GENERATION": GENERATION,
        }
        controller, _ = workshopctl.build_controller(env, lambda: FakeBackend())
        self.assertIsNotNone(controller.cp_client)
        self.assertIsNone(controller.state_reporter)
        env["BLOCKFIELD_CP_GATE_TOKEN"] = "g"
        controller, _ = workshopctl.build_controller(env, lambda: FakeBackend())
        self.assertIs(
            controller.state_reporter.transport, controller.gate_reporter.transport
        )

    def test_release_select_foreign_target_rejected(self):
        record = release_record("v9.0.0", "d")
        transport = FakeTransport(
            snapshot_doc(),
            [frame(select_op("v9.0.0", record, target="other-resource"), 1)],
        )
        result = drive(self.client(transport), self.controller)
        self.assertEqual(result, {"applied": 0, "rejected": 1, "acked": 1})
        self.assertNotIn("v9.0.0", self.controller.releases)

    def test_batch_refuses_conflict_without_partial_apply(self):
        good = release_record("v9.0.0", "d")
        rival = release_record("v9.0.0", "e")
        before = (
            self.releases_path.read_bytes() if self.releases_path.exists() else None
        )
        with self.assertRaisesRegex(ValueError, "identity changed"):
            self.controller.apply_cp_releases([good, rival])
        self.assertNotIn("v9.0.0", self.controller.releases)
        self.assertNotIn("v9.0.0", self.backend.releases)
        after = self.releases_path.read_bytes() if self.releases_path.exists() else None
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
