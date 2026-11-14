"""Controller-owned desired state: schema-2 document, single writer (B3 U3).

The host game-deploy desired.json keeps its exact schema-1 shape for the
deploy runner's schema-1 readers; the controller owns this versioned
desired-state document (approved Workshop release + revision/operation
binding) beside its state. Schema-1 release records stay valid inside it,
and a standalone schema-1 record is accepted as the initial import.
"""

import json
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

import desired
from controller import Controller, Goal
from release_fixtures import catalog, release_record
from test_controller import FakeBackend


class NoGameBackend(FakeBackend):
    """The deploy tail stages with Game absent: no Game process at all."""

    def __init__(self):
        super().__init__()
        self.processes = []
        self.generation = None

    def readiness_generation(self, profile):
        assert profile == "workshop"
        return self.generation


class Clock:
    def __init__(self, start=1_700_000_000.0):
        self.t = start

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


class DesiredHelperTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "desired.json"
        self.records = catalog()

    def document(self, release="v1.0.0", operation=None, revision=1, selection=1):
        return desired.build_desired_document(
            revision=revision,
            operation=operation or str(uuid4()),
            selection_revision=selection,
            workshop=self.records[release],
        )

    def test_schema_2_round_trip(self):
        doc = self.document()
        self.assertEqual(desired.project_desired_state(self.path, doc), doc)
        read = desired.read_desired_state(self.path)
        self.assertEqual(read, doc)
        self.assertEqual(read["schema"], 2)
        self.assertEqual(json.loads(self.path.read_bytes())["schema"], 2)

    def test_missing_file_reads_none(self):
        self.assertIsNone(desired.read_desired_state(self.path))

    def test_schema_2_rejects_bad_digest(self):
        record = dict(self.records["v1.0.0"])
        record["imageDigest"] = (
            "ghcr.io/blockfield/blockfield-server@sha256:" + "0" * 64
        )
        with self.assertRaises(ValueError):
            desired.build_desired_document(
                revision=1,
                operation=str(uuid4()),
                selection_revision=1,
                workshop=record,
            )

    def test_schema_2_rejects_bad_metadata(self):
        for mutate in (
            lambda doc: doc.pop("workshop"),
            lambda doc: doc.update(extra=1),
            lambda doc: doc.update(revision=0),
            lambda doc: doc.update(operation="not-a-uuid"),
            lambda doc: doc.update(selectionRevision=2, revision=1),
            lambda doc: doc.update(schema=1),
        ):
            doc = self.document()
            mutate(doc)
            with self.assertRaises(ValueError, msg=str(doc)):
                desired.project_desired_state(self.path, doc)

    def test_schema_2_rejects_unbound_release(self):
        record = dict(self.records["v1.0.0"])
        del record["source"]
        with self.assertRaises(ValueError):
            desired.build_desired_document(
                revision=1,
                operation=str(uuid4()),
                selection_revision=1,
                workshop=record,
            )

    def test_schema_1_record_is_adopted(self):
        record = release_record("v1.0.0", "a")
        self.path.write_text(json.dumps(record))
        read = desired.read_desired_state(self.path)
        self.assertEqual(read["schema"], 2)
        self.assertIsNone(read["operation"])
        self.assertEqual(read["workshop"], record)
        self.assertIsNone(read["previousWorkshop"])

    def test_schema_1_host_shape_is_not_a_desired_document(self):
        self.path.write_text(
            json.dumps(
                {
                    "schema": 1,
                    "game_tag": "v1.0.0",
                    "game_digest": "sha256:" + "a" * 64,
                    "client_tag": None,
                }
            )
        )
        with self.assertRaises(ValueError):
            desired.read_desired_state(self.path)

    def test_changed_body_under_same_operation_rejected(self):
        operation = str(uuid4())
        first = self.document("v1.0.0", operation=operation)
        desired.project_desired_state(self.path, first)
        changed = self.document("v2.0.0", operation=operation, revision=1, selection=1)
        with self.assertRaises(ValueError):
            desired.project_desired_state(self.path, changed)
        self.assertEqual(desired.read_desired_state(self.path), first)

    def test_same_operation_same_body_is_idempotent(self):
        operation = str(uuid4())
        doc = self.document("v1.0.0", operation=operation)
        desired.project_desired_state(self.path, doc)
        before = self.path.read_bytes()
        self.assertEqual(desired.project_desired_state(self.path, doc), doc)
        self.assertEqual(self.path.read_bytes(), before)

    def test_duplicate_keys_rejected(self):
        self.path.write_text('{"schema": 2, "schema": 2, "revision": 1}')
        with self.assertRaises(ValueError):
            desired.read_desired_state(self.path)


class ControllerDesiredTests(unittest.TestCase):
    def setUp(self):
        import cp_fixtures

        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = Path(temp.name)
        self.backend = NoGameBackend()
        self.clock = Clock()
        self.controller = Controller(
            self.dir,
            self.backend,
            now=self.clock,
            start_timeout=100,
            idle_sleep_s=60.0,
            workshop_data_dir=self.dir / "data",
            releases=catalog(),
        )
        self.controller.state_reporter = cp_fixtures.Reporter()
        (self.dir / "data").mkdir(exist_ok=True)
        self.session = str(uuid4())
        self.desired_file = self.dir / "desired.json"

    def request(self, release, revision):
        return self.controller.request(
            str(uuid4()), Goal("workshop", release, self.session), revision
        )

    def stable(self, release="v1.0.0"):
        self.request(release, self.controller.status()["revision"])
        self.controller.step()
        self.backend.ready(Goal("workshop", release, self.session))
        self.backend.generation = "11111111-2222-3333-4444-555555555555"
        state = self.controller.step()
        self.assertEqual(state["phase"], "workshop")
        return state

    def test_initialize_projects_schema_2_without_game(self):
        self.assertFalse(self.desired_file.exists())
        operation = str(uuid4())
        self.controller.request(operation, Goal("workshop", "v1.0.0", self.session), 0)
        doc = desired.read_desired_state(self.desired_file)
        self.assertEqual(doc["schema"], 2)
        self.assertEqual(
            (doc["revision"], doc["operation"], doc["selectionRevision"]),
            (1, operation, 1),
        )
        self.assertEqual(doc["workshop"]["release"], "v1.0.0")
        self.assertIsNone(doc["previousWorkshop"])
        self.assertEqual(self.controller.desired_state(), doc)

    def test_standalone_file_without_journal_is_rejected(self):
        self.desired_file.write_text(
            json.dumps(self.document_body("v1.0.0", str(uuid4())))
        )
        with self.assertRaisesRegex(ValueError, "import via request"):
            self.controller.desired_state()

    def document_body(self, release, operation):
        return desired.build_desired_document(
            revision=1,
            operation=operation,
            selection_revision=1,
            workshop=catalog()[release],
        )

    def test_stage_updates_desired_with_previous(self):
        self.request("v1.0.0", 0)
        self.controller.step()
        self.request("v2.0.0", 1)
        doc = self.controller.desired_state()
        self.assertEqual(doc["workshop"]["release"], "v2.0.0")
        self.assertEqual(doc["previousWorkshop"]["release"], "v1.0.0")
        self.assertEqual((doc["revision"], doc["selectionRevision"]), (2, 2))

    def test_rollback_restores_previous_release(self):
        self.stable("v1.0.0")
        first_op = self.controller.status()["operation"]
        self.request("v2.0.0", self.controller.status()["revision"])
        self.assertEqual(self.controller.step()["phase"], "STARTING")
        self.clock.advance(101)
        state = self.controller.step()
        self.assertEqual(state["desired"]["release"], "v1.0.0")
        doc = self.controller.desired_state()
        self.assertEqual(doc["workshop"]["release"], "v1.0.0")
        self.assertEqual(doc["previousWorkshop"]["release"], "v2.0.0")
        self.assertNotEqual(doc["operation"], first_op)
        self.assertEqual(doc["revision"], state["revision"])

    def session_bytes(self):
        return (self.dir / "session.json").read_bytes()

    def test_wake_revisions_desired_and_keeps_session(self):
        self.stable("v1.0.0")
        (self.dir / "data" / "draft-canary.bin").write_bytes(b"draft")
        before = self.session_bytes()
        sleeping_revision = self.sleep()
        wake_op = str(uuid4())
        result = self.controller.wake(wake_op)
        self.assertTrue(result["started"])
        self.controller.step()
        doc = self.controller.desired_state()
        self.assertEqual(doc["workshop"]["release"], "v1.0.0")
        self.assertEqual(doc["operation"], wake_op)
        self.assertEqual(doc["revision"], sleeping_revision + 1)
        self.assertEqual(self.session_bytes(), before)
        self.assertEqual(
            (self.dir / "data" / "draft-canary.bin").read_bytes(), b"draft"
        )

    def test_failed_start_keeps_session_and_drafts(self):
        self.stable("v1.0.0")
        (self.dir / "data" / "draft-canary.bin").write_bytes(b"draft")
        before = self.session_bytes()
        self.request("v2.0.0", self.controller.status()["revision"])
        self.backend.fail_start = True
        self.assertEqual(self.controller.step()["phase"], "ERROR")
        self.assertEqual(self.session_bytes(), before)
        self.assertEqual(
            (self.dir / "data" / "draft-canary.bin").read_bytes(), b"draft"
        )
        doc = self.controller.desired_state()
        self.assertEqual(doc["workshop"]["release"], "v2.0.0")
        self.assertEqual(doc["previousWorkshop"]["release"], "v1.0.0")

    def test_stale_revision_and_reused_operation_still_rejected(self):
        operation = str(uuid4())
        goal = Goal("workshop", "v1.0.0", self.session)
        self.controller.request(operation, goal, 0)
        with self.assertRaisesRegex(ValueError, "reused with different intent"):
            self.controller.request(
                operation, Goal("workshop", "v2.0.0", self.session), 1
            )
        with self.assertRaisesRegex(ValueError, "stale state revision"):
            self.controller.request(str(uuid4()), goal, 0)
        doc = self.controller.desired_state()
        self.assertEqual((doc["workshop"]["release"], doc["revision"]), ("v1.0.0", 1))

    def sleep(self):
        """Idle steps until the gameless editor sleeps; returns the revision."""
        generation = self.backend.generation
        activity = {
            "schema": 1,
            "generation": generation,
            "players": 0,
            "busy": False,
            "updatedAt": int(self.clock() * 1000),
        }
        self.controller.state_reporter.put("game-activity", activity)
        self.controller.step()
        self.clock.advance(61)
        self.controller.state_reporter.put(
            "game-activity", {**activity, "updatedAt": int(self.clock() * 1000)}
        )
        state = self.controller.step()
        self.assertEqual(state["phase"], "sleeping")
        return state["revision"]


if __name__ == "__main__":
    unittest.main()
