"""Workshop deadlines and rollback leave the independently running Game untouched."""

import json
import tempfile
import unittest
from unittest import mock
from pathlib import Path
from uuid import uuid4

from controller import Controller, Goal, Process
from test_controller import FakeBackend


class Clock:
    def __init__(self, start=1_700_000_000.0):
        self.t = start

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


class TimeoutTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.dir = Path(temporary.name)
        self.backend = FakeBackend()
        self.clock = Clock()
        self.editor = Goal("workshop", "v1", str(uuid4()))
        self.test = Goal("test", "v1", self.editor.session, "a" * 64)
        self.controller = Controller(
            self.dir, self.backend, now=self.clock, start_timeout=100
        )

    def stable_editor(self):
        self.controller.request(str(uuid4()), self.editor, 0)
        self.controller.step()
        self.backend.ready(self.editor)
        self.assertEqual(self.controller.step()["phase"], "workshop")

    def start_test(self):
        self.controller.request(
            str(uuid4()), self.test, self.controller.status()["revision"]
        )
        return self.controller.step()

    def test_game_restart_is_observed_without_interrupting_editor(self):
        self.stable_editor()
        events = list(self.backend.events)
        self.backend.processes[0] = Process(Goal("game", "v2"), False)
        self.assertEqual(self.controller.step()["phase"], "workshop")
        self.clock.advance(101)
        self.backend.processes = [
            p for p in self.backend.processes if p.goal.profile != "game"
        ]
        restarted = Controller(
            self.dir, self.backend, now=self.clock, start_timeout=100
        )
        self.assertEqual(restarted.step()["phase"], "workshop")
        self.assertEqual(self.backend.events, events)
        gate = json.loads((self.dir / "gate.json").read_text())
        self.assertEqual(gate["backends"]["game"]["gate"], "closed")
        self.assertEqual(gate["backends"]["workshop"]["gate"], "builders")

    def test_start_timeout_restores_previous_editor_only(self):
        self.stable_editor()
        game = self.backend.processes[0]
        self.assertEqual(self.start_test()["phase"], "STARTING")
        self.clock.advance(101)
        state = self.controller.step()
        self.assertEqual(state["phase"], "STARTING")
        self.assertEqual(state["desired"]["profile"], "workshop")
        self.assertIn("timed out", state["errorMessage"])
        self.assertIn(game, self.backend.processes)
        self.backend.ready(self.editor)
        self.assertEqual(self.controller.step()["gate"], "builders")
        self.assertNotIn("drain", self.backend.events)

    def test_initial_start_past_deadline_is_not_reissued(self):
        self.controller.request(str(uuid4()), self.editor, 0)

        def delayed(goal):
            self.backend.events.append("start")

        self.backend.start = delayed
        self.controller.step()
        self.clock.advance(101)
        self.assertEqual(self.controller.step()["phase"], "STARTING")
        restarted = Controller(
            self.dir, self.backend, now=self.clock, start_timeout=100
        )
        self.assertEqual(restarted.step()["phase"], "STARTING")
        self.assertEqual(self.backend.events, ["start"])

    def test_unexpected_workshop_during_start_is_fenced(self):
        self.stable_editor()
        self.start_test()
        self.backend.ready(self.editor)
        state = self.controller.step()
        self.assertEqual(state["phase"], "STARTING")
        self.assertIn("fence", self.backend.events)
        self.clock.advance(101)
        self.assertEqual(self.controller.step()["desired"]["profile"], "workshop")
        self.assertEqual(self.backend.processes[0].goal.profile, "game")

    def test_cancel_restores_editor_without_game_mutation(self):
        self.stable_editor()
        self.start_test()
        state = self.controller.cancel()
        self.assertEqual(state["phase"], "STARTING")
        self.assertEqual(state["desired"]["profile"], "workshop")
        self.assertIn("cancelled", state["errorMessage"])
        self.assertEqual(self.backend.processes[0].goal.profile, "game")

    def test_cancel_refuses_stable_editor(self):
        self.stable_editor()
        with self.assertRaises(ValueError):
            self.controller.cancel()

    def test_cancel_refuses_an_old_test_release_before_stopping_current_editor(self):
        self.stable_editor()
        self.start_test()
        self.backend.ready(self.test)
        self.controller.step()
        self.backend.processes[0] = Process(Goal("game", "v2"), True)
        editor = Goal("workshop", "v2", str(uuid4()))
        self.controller.request(
            str(uuid4()), editor, self.controller.status()["revision"]
        )
        self.controller.step()
        processes, events = list(self.backend.processes), list(self.backend.events)

        def refuse_old_test(goal):
            if goal.profile == "test":
                raise ValueError("playtest release differs from the ready Game release")

        self.backend.preflight = refuse_old_test
        state = self.controller.cancel()
        self.assertEqual(state["phase"], "ERROR")
        self.assertIn("playtest release differs", state["errorMessage"])
        self.assertEqual(self.backend.processes, processes)
        self.assertEqual(self.backend.events, events)

    def test_duplicate_game_fails_closed_without_stopping_it(self):
        self.stable_editor()
        self.backend.processes.append(Process(Goal("game", "v1"), True))
        events = list(self.backend.events)
        state = self.controller.step()
        self.assertEqual(state["phase"], "ERROR")
        self.assertEqual(state["error"], "RuntimeError")
        self.assertIn("same app", state["errorMessage"])
        self.assertEqual(self.backend.events, events)

    def test_error_message_clears_after_explicit_editor_recovery(self):
        self.stable_editor()
        self.backend.fail_save = True
        self.start_test()
        state = self.controller.status()
        self.assertEqual(state["phase"], "ERROR")
        self.assertTrue(state["errorMessage"])
        self.backend.fail_save = False
        self.controller.request(
            str(uuid4()), Goal("game", "v1"), state["revision"], recover=True
        )
        state = self.controller.step(recover=True)
        self.assertEqual(state["phase"], "workshop")
        self.assertIsNone(state["errorMessage"])
        self.assertEqual(self.backend.processes[0].goal.profile, "game")

    def follow_release(self, *, fail_start=False):
        self.stable_editor()
        self.session_bytes = self.controller.session_file.read_bytes()
        self.backend.processes[0] = Process(Goal("game", "v2"), True)
        self.backend.fail_start = fail_start
        return self.controller.step()

    def fence_workshop(self):
        self.backend.events.append("fence")
        self.backend.processes = [
            p for p in self.backend.processes if p.goal.profile == "game"
        ]

    def test_failed_automatic_follow_restores_exact_session_after_game_rollback(self):
        failed = self.follow_release(fail_start=True)
        self.assertEqual(failed["phase"], "ERROR")
        self.assertEqual(failed["releaseFollow"]["previous"]["release"], "v1")
        self.backend.fail_start = False
        old_game = Process(Goal("game", "v1"), True)
        self.backend.processes[0] = old_game
        self.backend.fence_pending_starts = self.fence_workshop
        restarted = Controller(self.dir, self.backend, now=self.clock)
        state = restarted.step()
        self.assertEqual(state["phase"], "STARTING")
        self.assertEqual(state["desired"], failed["releaseFollow"]["previous"])
        self.assertIsNone(state["releaseFollow"])
        self.assertIn(old_game, self.backend.processes)
        self.assertEqual(self.controller.session_file.read_bytes(), self.session_bytes)
        self.assertEqual(restarted.step()["gate"], "closed")
        self.backend.ready(self.editor)
        self.assertEqual(restarted.step()["gate"], "builders")

    def test_pending_automatic_follow_is_fenced_before_rollback_start(self):
        self.assertEqual(self.follow_release()["phase"], "STARTING")
        self.backend.processes[0] = Process(Goal("game", "v1"), True)
        self.backend.fence_pending_starts = self.fence_workshop
        state = self.controller.step()
        self.assertEqual(state["phase"], "STARTING")
        self.assertEqual(state["desired"]["release"], "v1")
        self.assertEqual(self.backend.events[-2:], ["fence", "start"])
        self.assertEqual(len(self.backend.processes), 2)

    def test_follow_preflight_failure_is_restorable_without_losing_editor(self):
        self.stable_editor()
        self.backend.processes[0] = Process(Goal("game", "v2"), True)
        with mock.patch.object(self.backend, "preflight", side_effect=OSError("image")):
            state = self.controller.step()
        self.assertEqual(state["phase"], "ERROR")
        self.assertIsNotNone(state["releaseFollow"])
        self.assertEqual(self.backend.events, ["start"])
        self.backend.processes[0] = Process(Goal("game", "v1"), True)
        self.backend.fence_pending_starts = self.fence_workshop
        self.assertEqual(self.controller.step()["desired"]["release"], "v1")

    def test_rollback_requires_ready_game_at_exact_previous_release(self):
        self.follow_release(fail_start=True)
        events = list(self.backend.events)
        for game in (
            Process(Goal("game", "v1"), False),
            Process(Goal("game", "v3"), True),
        ):
            self.backend.processes[0] = game
            self.assertEqual(self.controller.step()["phase"], "ERROR")
            self.assertEqual(self.backend.events, events)

    def test_changed_session_prevents_rollback_and_consumes_automatic_retry(self):
        self.follow_release(fail_start=True)
        selected = self.controller.session()
        selected["maps"] = ["changed"]
        self.controller._save_session(selected)
        self.backend.processes[0] = Process(Goal("game", "v1"), True)
        events = list(self.backend.events)
        state = self.controller.step()
        self.assertEqual(state["phase"], "ERROR")
        self.assertIn("exact original", state["errorMessage"])
        self.assertIsNone(state["releaseFollow"])
        self.assertEqual(self.backend.events, events)
        self.controller.session_file.write_bytes(self.session_bytes)
        self.assertEqual(self.controller.step()["phase"], "ERROR")
        self.assertEqual(self.backend.events, events)

    def test_failed_rollback_preflight_requires_manual_recovery(self):
        self.follow_release(fail_start=True)
        self.backend.processes[0] = Process(Goal("game", "v1"), True)
        events = list(self.backend.events)
        with mock.patch.object(self.backend, "preflight", side_effect=OSError("disk")):
            state = self.controller.step()
        self.assertEqual(state["phase"], "ERROR")
        self.assertIsNone(state["releaseFollow"])
        self.assertEqual(self.backend.events, events)
        self.assertEqual(self.controller.step()["phase"], "ERROR")
        self.assertEqual(self.backend.events, events)

    def test_unrelated_error_is_never_recovered_by_game_rollback(self):
        self.stable_editor()
        self.backend.fail_save = True
        self.start_test()
        events = list(self.backend.events)
        self.assertEqual(self.controller.step()["phase"], "ERROR")
        self.assertEqual(self.backend.events, events)

    def test_corrupt_workshop_selection_prevents_automatic_rollback_fence(self):
        self.follow_release()
        self.backend.processes = [
            Process(Goal("game", "v1"), True),
            Process(Goal("workshop", "unknown", self.editor.session), False),
        ]
        events = list(self.backend.events)
        state = self.controller.step()
        self.assertEqual(state["phase"], "ERROR")
        self.assertIsNone(state["releaseFollow"])
        self.assertEqual(self.backend.events, events)

    def test_malformed_follow_fails_closed_without_overwriting_state(self):
        self.follow_release(fail_start=True)
        state = json.loads(self.controller.file.read_text())
        state["releaseFollow"]["previous"]["session"] = str(uuid4())
        self.controller.file.write_text(json.dumps(state))
        original = self.controller.file.read_bytes()
        events = list(self.backend.events)
        with self.assertRaisesRegex(ValueError, "invalid release follow"):
            self.controller.step()
        self.assertEqual(self.controller.file.read_bytes(), original)
        self.assertEqual(self.backend.events, events)
        gates = json.loads((self.dir / "gate.json").read_text())
        self.assertEqual(gates["backends"]["game"]["gate"], "public")
        self.assertEqual(gates["backends"]["workshop"]["gate"], "closed")


if __name__ == "__main__":
    unittest.main()
