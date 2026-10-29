"""Workshop deadlines and rollback leave the independently running Game untouched."""

from release_fixtures import catalog

import json
import tempfile
import unittest
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
        self.editor = Goal("workshop", "v1.0.0", str(uuid4()))
        self.test = Goal("test", "v1.0.0", self.editor.session, "a" * 64)
        self.controller = Controller(
            self.dir,
            self.backend,
            now=self.clock,
            start_timeout=100,
            releases=catalog(),
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
        self.backend.processes[0] = Process(Goal("game", "v2.0.0"), False)
        self.assertEqual(self.controller.step()["phase"], "workshop")
        self.clock.advance(101)
        self.backend.processes = [
            p for p in self.backend.processes if p.goal.profile != "game"
        ]
        restarted = Controller(
            self.dir,
            self.backend,
            now=self.clock,
            start_timeout=100,
            releases=catalog(),
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
            self.dir,
            self.backend,
            now=self.clock,
            start_timeout=100,
            releases=catalog(),
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
        self.backend.processes[0] = Process(Goal("game", "v2.0.0"), True)
        editor = Goal("game", "v2.0.0")
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
        self.backend.processes.append(Process(Goal("game", "v1.0.0"), True))
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
            str(uuid4()), Goal("game", "v1.0.0"), state["revision"], recover=True
        )
        state = self.controller.step(recover=True)
        self.assertEqual(state["phase"], "workshop")
        self.assertIsNone(state["errorMessage"])
        self.assertEqual(self.backend.processes[0].goal.profile, "game")

    def test_game_release_change_never_stages_fences_or_follows(self):
        # Retired: Workshop selection never tracks the running Game. A Game
        # rollout, stop or absence leaves the approved target alone and
        # creates no follow record.
        self.stable_editor()
        events = list(self.backend.events)
        self.backend.processes[0] = Process(Goal("game", "v2.0.0"), True)
        state = self.controller.step()
        self.assertEqual(
            (state["phase"], state["desired"]["release"]), ("workshop", "v1.0.0")
        )
        self.assertIsNone(state.get("releaseFollow"))
        self.assertEqual(self.backend.events, events)
        self.backend.processes = [
            p for p in self.backend.processes if p.goal.profile != "game"
        ]
        offline = self.controller.step()
        self.assertEqual(
            (offline["phase"], offline["desired"]["release"]), ("workshop", "v1.0.0")
        )
        self.assertEqual(self.backend.events, events)

    def test_staged_target_survives_restart_while_game_is_off(self):
        self.stable_editor()
        target_before = self.controller.session_file.read_bytes()
        self.backend.processes = [
            p for p in self.backend.processes if p.goal.profile != "game"
        ]
        restarted = Controller(
            self.dir,
            self.backend,
            now=self.clock,
            start_timeout=100,
            releases=catalog(),
        )
        state = restarted.step()
        self.assertEqual(
            (state["phase"], state["desired"]["release"]), ("workshop", "v1.0.0")
        )
        self.assertEqual(self.controller.session_file.read_bytes(), target_before)

    def test_legacy_follow_record_is_ignored_and_never_consumed(self):
        # States written before live-Game following was retired keep their
        # releaseFollow bytes, but no step consumes them anymore.
        self.stable_editor()
        self.controller.request(
            str(uuid4()),
            Goal("workshop", "v2.0.0", self.editor.session),
            self.controller.status()["revision"],
        )
        state = json.loads(self.controller.file.read_text())
        previous = dict(state["desired"])
        previous["release"] = "v1.0.0"
        state["releaseFollow"] = {"previous": previous, "sessionDigest": "0" * 64}
        self.controller.file.write_text(json.dumps(state))
        stepped = self.controller.step()
        self.assertEqual(stepped["desired"]["release"], "v2.0.0")
        self.assertEqual(stepped["phase"], "STARTING")
        self.assertEqual(stepped["releaseFollow"]["previous"]["release"], "v1.0.0")

    def test_malformed_follow_fails_closed_without_overwriting_state(self):
        self.stable_editor()
        state = json.loads(self.controller.file.read_text())
        state["releaseFollow"] = {"previous": {"profile": "workshop"}}
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
