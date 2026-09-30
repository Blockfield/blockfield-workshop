"""Regression tests for uncertain starts and persisted-state validation; no live backend."""

import json
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4
from controller import Controller, Goal, Process
from test_controller import FakeBackend


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.backend = FakeBackend()
        self.backend.is_drained = True
        self.controller = Controller(self.directory, self.backend)
        self.goal = Goal("workshop", "v1", str(uuid4()))
        self.operation = str(uuid4())
        self.controller.request(self.operation, self.goal, 0)

    def test_delayed_visibility_never_reissues_start(self):
        def delayed_start(goal):
            self.backend.events.append("start")
            # The infrastructure accepted the start, but no process is observable yet.

        self.backend.start = delayed_start
        self.assertEqual(self.controller.step()["phase"], "STARTING")
        for _ in range(5):
            self.assertEqual(self.controller.step()["gate"], "closed")
        self.assertEqual(self.backend.events.count("start"), 1)
        restarted = Controller(self.directory, self.backend)
        self.assertEqual(restarted.step()["gate"], "closed")
        self.assertEqual(self.backend.events.count("start"), 1)
        self.backend.processes = [Process(self.goal, True)]
        self.assertEqual(restarted.step()["gate"], "builders")

    def test_crash_after_start_intent_needs_explicit_fenced_recovery(self):
        self.backend.processes = []
        state = self.controller._read()
        state["phase"] = "STARTING"
        self.controller._write(state)
        self.assertEqual(self.controller.step()["gate"], "closed")
        self.assertNotIn("start", self.backend.events)
        # Recovery must ask the adapter to fence old accepted starts before retrying.
        self.controller.step(recover=True)
        self.assertEqual(self.backend.events.count("start"), 1)
        self.assertLess(
            self.backend.events.index("fence"), self.backend.events.index("start")
        )

    def test_failed_recovery_fence_prevents_retry(self):
        self.backend.processes = []
        state = self.controller._read()
        state["phase"] = "STARTING"
        self.controller._write(state)

        def cannot_fence():
            raise OSError("cannot establish cancellation")

        self.backend.fence_pending_starts = cannot_fence
        self.assertEqual(self.controller.step(recover=True)["phase"], "ERROR")
        self.assertNotIn("start", self.backend.events)

    def test_unknown_or_inconsistent_state_rejected_before_side_effects(self):
        baseline = self.controller._read()
        mutations = [
            {"schema": True},
            {"phase": "surprise"},
            {"gate": "public"},
            {"phase": "ERROR", "gate": "builders"},
            {"operation": "not-a-uuid"},
            {"desired": None},
            {"revision": 0},
            {"phase": "game"},
            {"desired": []},
            {"revision": True},
        ]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                self.controller.file.write_text(json.dumps(baseline | mutation))
                with self.assertRaises(ValueError):
                    self.controller.step()
                self.assertEqual(self.backend.events, [])
        self.controller._write(baseline)

    def test_duplicate_state_keys_rejected(self):
        data = self.controller.file.read_text()
        self.controller.file.write_text('{"phase":"ERROR",' + data[1:])
        with self.assertRaises(ValueError):
            self.controller.step()
        self.assertEqual(self.backend.events, [])

    def test_dangling_symlink_is_not_uninitialized_state(self):
        self.controller.file.unlink()
        self.controller.file.symlink_to(self.directory / "missing-target")
        with self.assertRaises(ValueError):
            self.controller._read()

    def test_closed_gate_is_valid_during_reconciliation_of_stable_state(self):
        self.controller.step()
        self.backend.processes = [Process(self.goal, True)]
        state = self.controller.step()
        state["gate"] = "closed"
        self.controller._write(state)
        self.assertEqual(self.controller.step()["gate"], "builders")


if __name__ == "__main__":
    unittest.main()
