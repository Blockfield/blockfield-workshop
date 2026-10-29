from release_fixtures import catalog
import fcntl
import json
import tempfile
import unittest
from unittest import mock
from pathlib import Path
from uuid import uuid4
from controller import Controller, Goal, Process
from releases import validate_release_record


class FakeBackend:
    def __init__(self):
        self.processes = [Process(Goal("game", "v1.0.0"), True)]
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
        self.controller = Controller(
            Path(self.temp.name), self.backend, releases=catalog()
        )
        self.goal = Goal("workshop", "v1.0.0", str(uuid4()))
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

    def test_game_release_change_does_not_retarget_workshop(self):
        # Selection never follows live Game: a Game rollout leaves the
        # approved Workshop target alone (retires automatic live-Game image following).
        self.start_workshop()
        game = Process(Goal("game", "v2.0.0"), True)
        self.backend.processes[0] = game
        events = list(self.backend.events)
        state = self.controller.step()
        self.assertEqual(
            (
                state["phase"],
                state["gate"],
                state["error"],
                state["desired"]["release"],
            ),
            ("workshop", "builders", None, "v1.0.0"),
        )
        self.assertIsNone(state.get("releaseFollow"))
        self.assertEqual(self.backend.events, events)
        self.assertIn(game, self.backend.processes)
        restarted = Controller(Path(self.temp.name), self.backend, releases=catalog())
        self.assertEqual(restarted.step()["desired"]["release"], "v1.0.0")

    def test_game_campaign_does_not_delay_workshop_start(self):
        self.request()
        for _ in range(20):
            self.assertEqual(self.controller.step()["phase"], "STARTING")
        self.assertNotIn("stop", self.backend.events)
        self.assertEqual(self.backend.events, ["start"])
        self.assertEqual(
            self.backend.processes[0], Process(Goal("game", "v1.0.0"), True)
        )

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
            Process(Goal("workshop", "v1.0.0", str(uuid4())), True)
        )
        self.backend.fail_stop = True
        self.assertEqual(self.controller.step()["phase"], "ERROR")
        self.assertNotIn("start", self.backend.events)
        self.assertEqual(len(self.backend.processes), 2)

    def test_save_failure_keeps_draft_process(self):
        self.start_workshop()
        self.backend.fail_save = True
        self.controller.request(
            str(uuid4()), Goal("test", "v1.0.0", self.goal.session, "a" * 64), 1
        )
        previous = self.backend.events.count("stop")
        self.assertEqual(self.controller.step()["gate"], "closed")
        self.assertEqual(self.backend.events.count("stop"), previous)
        self.assertEqual(self.backend.processes[1].goal, self.goal)

    def test_restart_recovers_persisted_desired_profile(self):
        self.request()
        self.controller.step()
        self.backend.ready(self.goal)
        restarted = Controller(Path(self.temp.name), self.backend, releases=catalog())
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
            self.controller.request(self.op, Goal("game", "v2.0.0"), 0)
        with self.assertRaises(RuntimeError):
            self.controller.request(str(uuid4()), Goal("game", "v2.0.0"), 1)

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
        test_goal = Goal("test", "v1.0.0", self.goal.session, "a" * 64)
        self.controller.request(str(uuid4()), test_goal, 1)
        self.controller.step()
        self.backend.ready(test_goal)
        self.assertEqual(self.controller.step()["gate"], "testers")
        self.controller.request(str(uuid4()), Goal("game", "v1.0.0"), 2)
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
        self.controller.request(str(uuid4()), Goal("game", "v1.0.0"), 1)
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
            Goal("test", "v1.0.0", str(uuid4()), "not-a-hash")
        with self.assertRaises(ValueError):
            Goal("game", "v1.0.0", str(uuid4()))
        Path(self.temp.name, "state.json").write_text('{"schema":999}')
        with self.assertRaises(ValueError):
            self.request()

    def test_bootstrap_persists_one_editor_session_and_keeps_game(self):
        game = self.backend.processes[0]
        self.controller.request(str(uuid4()), Goal("game", "v1.0.0"), 0)
        state = self.controller.step()
        self.assertEqual(state["phase"], "STARTING")
        session = self.controller.session()["id"]
        self.assertEqual(state["desired"]["session"], session)
        restarted = Controller(Path(self.temp.name), self.backend, releases=catalog())
        self.assertEqual(restarted.step()["desired"]["session"], session)
        self.assertEqual(self.backend.events, ["start"])
        self.assertIn(game, self.backend.processes)

    def test_bootstrap_waits_for_explicit_approved_target_import(self):
        self.backend.processes = []
        self.assertEqual(self.controller.step()["phase"], "UNINITIALIZED")
        gate = json.loads(self.controller.gate_file.read_bytes())
        self.assertEqual(gate["backends"]["game"]["gate"], "closed")
        self.assertEqual(gate["backends"]["workshop"]["gate"], "closed")
        self.assertEqual(self.backend.events, [])

    def test_failed_initial_import_preserves_uninitialized_state(self):
        self.controller.maps = mock.Mock(
            canonical_sessions=True,
            map_ids=lambda: {"fields"},
            prepare_session=mock.Mock(side_effect=OSError("not enough space")),
        )
        with self.assertRaises(OSError):
            self.controller.request(str(uuid4()), Goal("game", "v1.0.0"), 0)
        self.assertEqual(self.controller.status()["phase"], "UNINITIALIZED")
        self.assertIsNone(self.controller.approved_target())
        self.assertEqual(self.backend.events, [])

    def test_legacy_game_and_draining_state_never_mutates_game(self):
        for phase, desired in (
            ("game", Goal("game", "v1.0.0")),
            ("DRAINING", self.goal),
        ):
            with self.subTest(phase=phase):
                self.backend.processes = [Process(Goal("game", "v1.0.0"), True)]
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
                        "release": "v1.0.0",
                        "session": None,
                        "candidate": None,
                    },
                }
                self.controller.file.write_text(json.dumps(state))
                if phase == "game":
                    self.assertEqual(self.controller.step()["phase"], "game")
                else:
                    with self.assertRaisesRegex(
                        ValueError, "missing committed active release identity"
                    ):
                        self.controller.step()
                self.assertEqual(self.backend.events, [])

    def test_release_update_is_deferred_during_playtest(self):
        self.start_workshop()
        test_goal = Goal("test", "v1.0.0", self.goal.session, "a" * 64)
        self.controller.request(str(uuid4()), test_goal, 1)
        self.controller.step()
        self.backend.ready(test_goal)
        self.controller.step()
        self.backend.processes[0] = Process(Goal("game", "v2.0.0"), True)
        events = list(self.backend.events)
        state = self.controller.step()
        self.assertEqual(
            (state["phase"], state["desired"]["release"]), ("test", "v1.0.0")
        )
        self.assertEqual(self.backend.events, events)

    def test_release_preflight_failure_preserves_editor_and_game(self):
        # A Game rollout alone never stages: preflight failure only surfaces
        # on the explicit new-release request, leaving the stable editor alone.
        self.start_workshop()
        self.backend.processes[0] = Process(Goal("game", "v2.0.0"), True)
        processes, events = list(self.backend.processes), list(self.backend.events)
        state = self.controller.step()
        self.assertEqual(state["phase"], "workshop")
        self.assertEqual(self.backend.events, events)

        def unavailable(goal):
            raise OSError("image unavailable")

        self.backend.preflight = unavailable
        with self.assertRaises(OSError):
            self.controller.request(
                str(uuid4()),
                Goal("workshop", "v2.0.0", self.goal.session),
                state["revision"],
            )
        self.assertEqual(self.controller.status()["phase"], "workshop")
        self.assertEqual(self.controller.status()["desired"]["release"], "v1.0.0")
        self.assertEqual(self.backend.processes, processes)
        self.assertEqual(self.backend.events, events)
        gate = json.loads(self.controller.gate_file.read_bytes())
        self.assertEqual(gate["backends"]["game"]["gate"], "public")
        self.assertEqual(gate["backends"]["workshop"]["gate"], "builders")

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
            self.controller.request(
                str(uuid4()), Goal("game", "v1.0.0"), state["revision"]
            )
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

    def test_start_never_adopts_game_release(self):
        # A ready editor at another release is not adopted from live Game:
        # the staged target stays until an explicit new operation selects it.
        self.request()
        self.controller.step()
        self.backend.processes[0] = Process(Goal("game", "v2.0.0"), True)
        actual = Goal("workshop", "v2.0.0", self.goal.session)
        self.backend.ready(actual)
        state = self.controller.step()
        self.assertEqual(
            (state["phase"], state["desired"]["release"]), ("STARTING", "v1.0.0")
        )
        # The unexpected ready backend at another release is fenced, never adopted.
        self.assertEqual(self.backend.events, ["start", "fence"])

    def test_start_never_adopts_unbound_editor_readiness(self):
        self.request()
        self.controller.step()
        cases = (
            (
                Process(Goal("game", "v2.0.0"), False),
                Process(Goal("workshop", "v2.0.0", self.goal.session), True),
            ),
            (
                Process(Goal("game", "v3.0.0"), True),
                Process(Goal("workshop", "v2.0.0", self.goal.session), True),
            ),
            (
                Process(Goal("game", "v2.0.0"), True),
                Process(Goal("workshop", "v2.0.0", str(uuid4())), True),
            ),
            (
                Process(Goal("game", "v2.0.0"), True),
                Process(Goal("workshop", "v2.0.0", self.goal.session), False),
            ),
            (
                Process(Goal("game", "v2.0.0"), True),
                Process(Goal("test", "v2.0.0", self.goal.session, "a" * 64), True),
            ),
        )
        for game, editor in cases:
            with self.subTest(game=game, editor=editor):
                self.backend.processes = [game, editor]
                state = self.controller.step()
                self.assertEqual(state["phase"], "STARTING")
                self.assertEqual(state["desired"]["release"], "v1.0.0")


class ReleaseSelectionTests(unittest.TestCase):
    """Desired release = verified published immutable server release.

    The last approved target persists while Game is off; staging never
    reads live Game readiness.
    """

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.dir = Path(temporary.name)
        self.backend = FakeBackend()
        self.catalog = catalog()
        self.controller = Controller(self.dir, self.backend, releases=self.catalog)
        self.session = str(uuid4())

    def request(self, release="v1.0.0"):
        return self.controller.request(
            str(uuid4()), Goal("workshop", release, self.session), 0
        )

    def test_records_reject_floating_or_foreign_images(self):
        bad = dict(self.catalog["v1.0.0"])
        for digest in (
            "ghcr.io/blockfield/blockfield-server:latest",
            "docker.io/library/other@sha256:" + "a" * 64,
            "sha256:" + "a" * 64,
            "",
        ):
            with self.subTest(digest=digest):
                with self.assertRaises(ValueError):
                    validate_release_record({**bad, "imageDigest": digest})
        with self.assertRaises(ValueError):
            validate_release_record({**bad, "source": {}})

    def test_unknown_release_is_rejected_before_any_backend_effect(self):
        with self.assertRaisesRegex(ValueError, "not a verified published release"):
            self.controller.request(
                str(uuid4()), Goal("workshop", "v9", self.session), 0
            )
        self.assertEqual(self.controller.status()["phase"], "UNINITIALIZED")
        self.assertIsNone(self.controller.approved_target())
        self.assertEqual(self.backend.events, [])

    def test_offline_stage_persists_approved_target_without_game(self):
        self.backend.processes = []
        state = self.request("v1.0.0")
        self.assertEqual(state["phase"], "REQUESTED")
        target = self.controller.approved_target()
        self.assertEqual(
            (target["release"], target["imageDigest"]),
            ("v1.0.0", self.catalog["v1.0.0"]["imageDigest"]),
        )
        staged = self.controller.step()
        self.assertEqual(staged["phase"], "STARTING")
        self.assertEqual(self.controller.approved_target(), target)
        restarted = Controller(self.dir, self.backend, releases=self.catalog)
        self.assertEqual(restarted.status()["desired"]["release"], "v1.0.0")
        self.assertEqual(restarted.approved_target(), target)
        self.backend.ready(Goal("workshop", "v1.0.0", self.session))
        self.assertEqual(restarted.step()["gate"], "builders")

    def test_duplicate_operation_is_idempotent_changed_intent_rejected(self):
        operation = str(uuid4())
        first = self.controller.request(
            operation, Goal("workshop", "v1.0.0", self.session), 0
        )
        self.assertEqual(
            self.controller.request(
                operation, Goal("workshop", "v1.0.0", self.session), 0
            ),
            first,
        )
        with self.assertRaisesRegex(ValueError, "reused with different intent"):
            self.controller.request(
                operation, Goal("workshop", "v2.0.0", self.session), 0
            )
        with self.assertRaisesRegex(ValueError, "stale state revision"):
            self.controller.request(
                str(uuid4()), Goal("workshop", "v2.0.0", self.session), 0
            )

    def test_explicit_release_change_keeps_session_and_draft(self):
        self.backend.processes = []
        self.request("v1.0.0")
        self.controller.step()
        self.backend.ready(Goal("workshop", "v1.0.0", self.session))
        self.assertEqual(self.controller.step()["phase"], "workshop")
        session_before = self.controller.session()
        selected = Goal("workshop", "v2.0.0", self.session)
        self.controller.request(
            str(uuid4()), selected, self.controller.status()["revision"]
        )
        self.assertEqual(self.controller.approved_target()["release"], "v2.0.0")
        self.controller.step()
        self.backend.ready(selected)
        state = self.controller.step()
        self.assertEqual(
            (state["phase"], state["desired"]["release"]), ("workshop", "v2.0.0")
        )
        self.assertEqual(self.controller.session()["id"], session_before["id"])
        self.assertEqual(self.controller.session()["id"], self.session)


if __name__ == "__main__":
    unittest.main()
