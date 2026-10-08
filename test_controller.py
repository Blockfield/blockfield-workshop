import fcntl
import json
import tempfile
import unittest
from unittest import mock
from pathlib import Path
from uuid import uuid4
from controller import Controller, Goal, Process


class FakeBackend:
    def __init__(self):
        self.processes = [Process(Goal("game", "v1"), True)]
        self.events = []
        self.fail_save = False
        self.fail_stop = False
        self.fail_start = False
        self.fence_fails = False

    def preflight(self, goal):
        pass

    def observe(self):
        return list(self.processes)

    def save_and_freeze(self, process):
        self.events.append("save")
        if self.fail_save:
            raise OSError("save failed")

    def stop(self, process):
        assert process.goal.profile in {"workshop", "test"}, "Game must remain running"
        self.events.append("stop")
        if not self.fail_stop:
            self.processes = [p for p in self.processes if p.goal.profile == "game"]

    def fence_pending_starts(self):
        self.events.append("fence")

    def start(self, goal):
        assert goal.profile in {"workshop", "test"}, "Game is not controller-owned"
        assert not any(p.goal.profile != "game" for p in self.processes), (
            "second workshop"
        )
        self.events.append("start")
        if self.fail_start:
            raise OSError("start failed")
        self.processes.append(Process(goal, False))

    def ready(self, goal):
        self.processes = [p for p in self.processes if p.goal.profile == "game"]
        self.processes.append(Process(goal, True))


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.backend = FakeBackend()
        self.controller = Controller(Path(self.temp.name), self.backend)
        self.goal = Goal("workshop", "v1", str(uuid4()))
        self.op = str(uuid4())

    def request(self):
        return self.controller.request(self.op, self.goal, 0)

    def start_workshop(self):
        self.request()
        self.controller.step()
        self.backend.ready(self.goal)
        return self.controller.step()

    def test_low_disk_does_not_close_a_running_stable_backend(self):
        # Stand acceptance 2026-09-24: preflight ran on every timer step, so low free space put a healthy game in ERROR.
        self.start_workshop()

        def full(goal):
            raise OSError("not enough free space for the backend data")

        self.backend.preflight = full
        state = self.controller.step()
        self.assertEqual(
            (state["phase"], state["gate"], state["error"]),
            ("workshop", "builders", None),
        )
        with self.assertRaises(OSError):
            self.controller.request(str(uuid4()), self.goal, state["revision"])
        self.assertEqual(self.controller.status()["phase"], "workshop")
        self.assertEqual(self.backend.events, ["start"])

    def test_game_release_deploy_updates_only_the_editor(self):
        self.start_workshop()
        game = Process(Goal("game", "v2"), True)
        self.backend.processes[0] = game
        state = self.controller.step()
        self.assertEqual(
            (
                state["phase"],
                state["gate"],
                state["error"],
                state["desired"]["release"],
            ),
            ("STARTING", "closed", None, "v2"),
        )
        self.assertEqual(self.backend.events, ["start", "save", "stop", "start"])
        self.assertIn(game, self.backend.processes)
        self.backend.ready(Goal("workshop", "v2", self.goal.session))
        restarted = Controller(Path(self.temp.name), self.backend)
        self.assertEqual(restarted.step()["desired"]["release"], "v2")

    def test_game_campaign_does_not_delay_workshop_start(self):
        self.request()
        for _ in range(20):
            self.assertEqual(self.controller.step()["phase"], "STARTING")
        self.assertNotIn("stop", self.backend.events)
        self.assertEqual(self.backend.events, ["start"])
        self.assertEqual(self.backend.processes[0], Process(Goal("game", "v1"), True))

    def test_readiness_and_no_restart_on_repeated_steps(self):
        self.request()
        self.controller.step()
        self.assertEqual(self.controller.step()["gate"], "closed")
        self.backend.ready(self.goal)
        self.assertEqual(self.controller.step()["gate"], "builders")
        self.controller.step()
        self.assertEqual(self.backend.events.count("start"), 1)

    def test_bad_stop_does_not_start_second_backend(self):
        self.request()
        self.backend.processes.append(
            Process(Goal("workshop", "v1", str(uuid4())), True)
        )
        self.backend.fail_stop = True
        self.assertEqual(self.controller.step()["phase"], "ERROR")
        self.assertNotIn("start", self.backend.events)
        self.assertEqual(len(self.backend.processes), 2)

    def test_save_failure_keeps_draft_process(self):
        self.start_workshop()
        self.backend.fail_save = True
        self.controller.request(
            str(uuid4()), Goal("test", "v1", self.goal.session, "a" * 64), 1
        )
        previous = self.backend.events.count("stop")
        self.assertEqual(self.controller.step()["gate"], "closed")
        self.assertEqual(self.backend.events.count("stop"), previous)
        self.assertEqual(self.backend.processes[1].goal, self.goal)

    def test_restart_recovers_persisted_desired_profile(self):
        self.request()
        self.controller.step()
        self.backend.ready(self.goal)
        restarted = Controller(Path(self.temp.name), self.backend)
        self.assertEqual(restarted.step()["gate"], "builders")
        self.assertEqual(self.backend.events.count("start"), 1)

    def test_failed_start_requires_explicit_recovery(self):
        self.request()
        self.backend.fail_start = True
        self.assertEqual(self.controller.step()["phase"], "ERROR")
        self.backend.fail_start = False
        self.controller.step()
        self.assertEqual(self.backend.events.count("start"), 1)
        self.controller.step(recover=True)
        self.assertEqual(self.backend.events.count("start"), 2)

    def test_duplicate_and_conflicting_intent(self):
        first = self.request()
        self.assertEqual(first, self.request())
        with self.assertRaises(ValueError):
            self.controller.request(self.op, Goal("game", "v2"), 0)
        with self.assertRaises(RuntimeError):
            self.controller.request(str(uuid4()), Goal("game", "v2"), 1)

    def test_concurrent_process_lock(self):
        self.request()
        with open(Path(self.temp.name) / "controller.lock", "r+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            with self.assertRaises(BlockingIOError):
                self.controller.step()
        self.assertNotIn("start", self.backend.events)

    def test_multiple_workshop_backends_fail_closed(self):
        self.request()
        self.backend.processes.append(Process(self.goal, True))
        self.backend.processes.append(Process(self.goal, True))
        self.assertEqual(self.controller.step()["phase"], "ERROR")
        self.assertFalse(self.backend.events)

    def test_playtest_then_return_old_release_never_publishes(self):
        self.start_workshop()
        test_goal = Goal("test", "v1", self.goal.session, "a" * 64)
        self.controller.request(str(uuid4()), test_goal, 1)
        self.controller.step()
        self.backend.ready(test_goal)
        self.assertEqual(self.controller.step()["gate"], "testers")
        self.controller.request(str(uuid4()), Goal("game", "v1"), 2)
        self.controller.step()
        self.assertEqual(self.controller.step()["gate"], "closed")
        self.backend.ready(self.goal)
        self.assertEqual(self.controller.step()["gate"], "builders")
        self.assertEqual(
            self.controller.status()["desired"]["session"], self.goal.session
        )
        self.assertNotIn("publish", self.backend.events)

    def test_old_completed_request_cannot_replay(self):
        self.start_workshop()
        self.controller.request(str(uuid4()), Goal("game", "v1"), 1)
        self.controller.step()
        self.backend.ready(self.goal)
        self.controller.step()
        with self.assertRaises(ValueError):
            self.request()

    def test_disk_write_failure_precedes_backend_side_effect(self):
        self.request()

        def fail(_):
            raise OSError("disk full")

        self.controller._write = fail
        with self.assertRaises(OSError):
            self.controller.step()
        self.assertFalse(self.backend.events)

    def test_invalid_goal_and_schema_rejected(self):
        with self.assertRaises(ValueError):
            Goal("test", "v1", str(uuid4()), "not-a-hash")
        with self.assertRaises(ValueError):
            Goal("game", "v1", str(uuid4()))
        Path(self.temp.name, "state.json").write_text('{"schema":999}')
        with self.assertRaises(ValueError):
            self.request()

    def test_bootstrap_persists_one_editor_session_and_keeps_game(self):
        game = self.backend.processes[0]
        state = self.controller.step()
        self.assertEqual(state["phase"], "STARTING")
        session = self.controller.session()["id"]
        self.assertEqual(state["desired"]["session"], session)
        restarted = Controller(Path(self.temp.name), self.backend)
        self.assertEqual(restarted.step()["desired"]["session"], session)
        self.assertEqual(self.backend.events, ["start"])
        self.assertIn(game, self.backend.processes)

    def test_bootstrap_waits_for_an_observed_game_release(self):
        self.backend.processes = []
        self.assertEqual(self.controller.step()["phase"], "UNINITIALIZED")
        gate = json.loads(self.controller.gate_file.read_bytes())
        self.assertEqual(gate["backends"]["game"]["gate"], "closed")
        self.assertEqual(gate["backends"]["workshop"]["gate"], "closed")
        self.assertEqual(self.backend.events, [])

    def test_failed_bootstrap_preserves_the_exact_session_for_recovery(self):
        prepare = mock.Mock(side_effect=OSError("not enough space"))
        self.controller.maps = mock.Mock(
            canonical_sessions=True,
            map_ids=lambda: {"fields"},
            prepare_session=prepare,
        )
        state = self.controller.step()
        self.assertEqual(state["phase"], "ERROR")
        selected = self.controller.session()
        self.assertEqual(selected["maps"], ["fields"])
        self.assertEqual(state["desired"]["session"], selected["id"])
        self.assertEqual(self.backend.events, [])
        prepare.side_effect = None
        self.controller.request(
            str(uuid4()), Goal("game", "v1"), state["revision"], recover=True
        )
        state = self.controller.step(recover=True)
        self.assertEqual(state["phase"], "STARTING")
        self.assertEqual(self.controller.session(), selected)
        self.assertEqual(state["desired"]["session"], selected["id"])

    def test_legacy_game_and_draining_state_never_mutates_game(self):
        for phase, desired in (("game", Goal("game", "v1")), ("DRAINING", self.goal)):
            with self.subTest(phase=phase):
                self.backend.processes = [Process(Goal("game", "v1"), True)]
                self.backend.events.clear()
                state = {
                    "schema": 1,
                    "phase": phase,
                    "gate": "public" if phase == "game" else "draining",
                    "operation": str(uuid4()),
                    "revision": 4,
                    "desired": {
                        "profile": desired.profile,
                        "release": desired.release,
                        "session": desired.session,
                        "candidate": None,
                    },
                    "rollback": {
                        "profile": "game",
                        "release": "v1",
                        "session": None,
                        "candidate": None,
                    },
                }
                self.controller.file.write_text(json.dumps(state))
                result = self.controller.step()
                self.assertEqual(result["phase"], "STARTING")
                self.assertEqual(result["desired"]["profile"], "workshop")
                self.assertIsNone(result["rollback"])
                self.assertEqual(self.backend.events, ["start"])

    def test_release_update_is_deferred_during_playtest(self):
        self.start_workshop()
        test_goal = Goal("test", "v1", self.goal.session, "a" * 64)
        self.controller.request(str(uuid4()), test_goal, 1)
        self.controller.step()
        self.backend.ready(test_goal)
        self.controller.step()
        self.backend.processes[0] = Process(Goal("game", "v2"), True)
        events = list(self.backend.events)
        state = self.controller.step()
        self.assertEqual((state["phase"], state["desired"]["release"]), ("test", "v1"))
        self.assertEqual(self.backend.events, events)

    def test_release_preflight_failure_preserves_editor_and_game(self):
        self.start_workshop()
        self.backend.processes[0] = Process(Goal("game", "v2"), True)
        processes, events = list(self.backend.processes), list(self.backend.events)

        def unavailable(goal):
            raise OSError("image unavailable")

        self.backend.preflight = unavailable
        state = self.controller.step()
        self.assertEqual(state["phase"], "ERROR")
        self.assertEqual(self.backend.processes, processes)
        self.assertEqual(self.backend.events, events)
        gate = json.loads(self.controller.gate_file.read_bytes())
        self.assertEqual(gate["backends"]["game"]["gate"], "public")
        self.assertEqual(gate["backends"]["workshop"]["gate"], "closed")

    def test_corrupt_state_closes_workshop_without_touching_game(self):
        self.start_workshop()
        self.controller.file.write_text("{corrupt")
        events = list(self.backend.events)
        with self.assertRaises(ValueError):
            self.controller.step()
        self.assertEqual(self.controller.file.read_text(), "{corrupt")
        self.assertEqual(self.backend.events, events)
        gate = json.loads(self.controller.gate_file.read_bytes())
        self.assertEqual(gate["backends"]["game"]["gate"], "public")
        self.assertEqual(gate["backends"]["workshop"]["gate"], "closed")

    def test_untrusted_game_metadata_does_not_close_ready_editor(self):
        self.start_workshop()
        self.backend.processes[0] = Process(Goal("game", "unknown"), False)
        events = list(self.backend.events)
        self.assertEqual(self.controller.step()["phase"], "workshop")
        gate = json.loads(self.controller.gate_file.read_bytes())
        self.assertEqual(gate["backends"]["game"]["gate"], "closed")
        self.assertEqual(gate["backends"]["workshop"]["gate"], "builders")
        self.assertEqual(self.backend.events, events)

    def test_resume_refuses_a_lost_session_instead_of_inventing_maps(self):
        self.start_workshop()
        state = self.controller.status()
        self.controller.session_file.unlink()
        events = list(self.backend.events)
        with self.assertRaisesRegex(ValueError, "session is missing or invalid"):
            self.controller.request(str(uuid4()), Goal("game", "v1"), state["revision"])
        self.assertEqual(self.controller.status(), state)
        self.assertEqual(self.backend.events, events)
        self.assertFalse(self.controller.session_file.exists())

    def test_untrusted_workshop_metadata_requires_recovery_without_mutation(self):
        self.request()
        self.backend.processes.append(
            Process(Goal("workshop", "unknown", str(uuid4())), False)
        )
        self.assertEqual(self.controller.step()["phase"], "ERROR")
        self.assertEqual(self.backend.events, [])
        gate = json.loads(self.controller.gate_file.read_bytes())
        self.assertEqual(gate["backends"]["game"]["gate"], "public")
        self.assertEqual(gate["backends"]["workshop"]["gate"], "closed")

    def test_start_adopts_ready_editor_release_only_from_the_ready_game(self):
        self.request()
        self.controller.step()
        self.backend.processes[0] = Process(Goal("game", "v2"), True)
        actual = Goal("workshop", "v2", self.goal.session)
        self.backend.ready(actual)
        state = self.controller.step()
        self.assertEqual(
            (state["phase"], state["desired"]["release"]), ("workshop", "v2")
        )
        self.assertEqual(self.backend.events, ["start"])

    def test_start_never_adopts_unbound_editor_readiness(self):
        self.request()
        self.controller.step()
        cases = (
            (
                Process(Goal("game", "v2"), False),
                Process(Goal("workshop", "v2", self.goal.session), True),
            ),
            (
                Process(Goal("game", "v3"), True),
                Process(Goal("workshop", "v2", self.goal.session), True),
            ),
            (
                Process(Goal("game", "v2"), True),
                Process(Goal("workshop", "v2", str(uuid4())), True),
            ),
            (
                Process(Goal("game", "v2"), True),
                Process(Goal("workshop", "v2", self.goal.session), False),
            ),
            (
                Process(Goal("game", "v2"), True),
                Process(Goal("test", "v2", self.goal.session, "a" * 64), True),
            ),
        )
        for game, editor in cases:
            with self.subTest(game=game, editor=editor):
                self.backend.processes = [game, editor]
                state = self.controller.step()
                self.assertEqual(state["phase"], "STARTING")
                self.assertEqual(state["desired"]["release"], "v1")


if __name__ == "__main__":
    unittest.main()
