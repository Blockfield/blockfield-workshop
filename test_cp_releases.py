"""CP v2 central release records + inbox retire switch (CP-S6b)."""

import json
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from release_fixtures import release_record

from controller import Controller
from cp_client import CPClient, CPConfig
from test_controller import FakeBackend
from test_cp_client import ADMIN, AGENT, EPOCH, GENERATION, NOW_MS, FakeTransport, frame

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
            inbox_dir=self.dir / "inbox",
            releases={},
            releases_path=self.releases_path,
        )
        self.controller.inbox_dir.mkdir(parents=True, exist_ok=True)
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

    def test_release_select_merges_central_record(self):
        record = release_record("v9.0.0", "d")
        transport = FakeTransport(
            snapshot_doc(), [frame(select_op("v9.0.0", record), 1)]
        )
        result = self.client(transport).sync(self.controller)
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
        result = self.client(transport).sync(self.controller)
        self.assertEqual(result, {"applied": 1, "rejected": 1, "acked": 2})
        self.assertIn("v9.0.0", self.controller.releases)
        self.assertNotIn("v9.0.1", self.controller.releases)

    def test_replayed_release_select_has_no_second_effect(self):
        record = release_record("v9.0.0", "d")
        envelope = frame(select_op("v9.0.0", record), 1)
        transport = FakeTransport(snapshot_doc(), [envelope])
        first = self.client(transport).sync(self.controller)
        self.assertEqual(first, {"applied": 1, "rejected": 0, "acked": 1})
        before = self.releases_path.read_bytes()
        transport.event_frames = [envelope]
        second = self.client(transport).sync(self.controller)
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
        result = self.client(transport).sync(self.controller)
        self.assertEqual(result, {"applied": 0, "rejected": 0, "acked": 0})
        self.assertIn("v9.0.0", self.controller.releases)
        self.assertIn("v9.0.0", self.backend.releases)

    def _inbox_members_file(self):
        request_id = str(uuid4())
        document = {
            "schema": 1,
            "id": request_id,
            "kind": "members",
            "actor": {"draslId": ADMIN["draslId"], "name": ADMIN["name"]},
            "members": [ADMIN],
            "createdAt": NOW_MS,
        }
        (self.controller.inbox_dir / (request_id + ".json")).write_text(
            json.dumps(document)
        )
        return request_id

    def _inbox_result(self, request_id):
        return json.loads(
            (
                self.controller.admin_dir / "results" / (request_id + ".json")
            ).read_bytes()
        )

    def test_inbox_retired_behind_switch(self):
        self.controller.inbox_retired = True
        request_id = self._inbox_members_file()
        done = self.controller.process_inbox(now_ms=NOW_MS)
        self.assertEqual(done, 1)
        result = self._inbox_result(request_id)
        self.assertFalse(result["ok"])
        self.assertIn("retired", result["error"])
        self.assertEqual(self.controller.members(), [ADMIN])

    def test_inbox_open_without_switch(self):
        request_id = self._inbox_members_file()
        done = self.controller.process_inbox(now_ms=NOW_MS)
        self.assertEqual(done, 1)
        self.assertTrue(self._inbox_result(request_id)["ok"])

    def test_ingress_switch_wiring(self):
        import workshopctl

        env = {
            "BF_STATE_DIR": str(self.dir / "ws"),
            "BF_GATE_FILE": str(self.dir / "ws-gate.json"),
            "BF_PUBLIC_FILE": str(self.dir / "ws-public.json"),
            "BF_ADMIN_DIR": str(self.dir / "ws-admin"),
            "BF_INBOX_DIR": str(self.dir / "ws-inbox"),
            "BF_RELEASES_FILE": str(self.dir / "ws-releases.json"),
            "BF_WORKSHOP_DATA": str(self.dir / "ws-data"),
            "BLOCKFIELD_CP_INGRESS": "1",
        }
        with self.assertRaisesRegex(ValueError, "CP agent credentials"):
            workshopctl.build_controller(env, lambda: FakeBackend())
        env.update(
            {
                "BLOCKFIELD_CP_URL": "https://cp.invalid",
                "BLOCKFIELD_CP_AGENT_ID": AGENT,
                "BLOCKFIELD_CP_AGENT_TOKEN": "t",
                "BLOCKFIELD_CP_GENERATION": GENERATION,
            }
        )
        controller, _ = workshopctl.build_controller(env, lambda: FakeBackend())
        self.assertTrue(controller.inbox_retired)
        self.assertIsNotNone(controller.cp_client)

    def test_release_select_foreign_target_rejected(self):
        record = release_record("v9.0.0", "d")
        transport = FakeTransport(
            snapshot_doc(),
            [frame(select_op("v9.0.0", record, target="other-resource"), 1)],
        )
        result = self.client(transport).sync(self.controller)
        self.assertEqual(result, {"applied": 0, "rejected": 1, "acked": 1})
        self.assertNotIn("v9.0.0", self.controller.releases)


if __name__ == "__main__":
    unittest.main()
