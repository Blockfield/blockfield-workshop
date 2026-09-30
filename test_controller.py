import fcntl
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4
from controller import Controller, Goal, Process


class FakeBackend:
    def __init__(self):
        self.processes = [Process(Goal("game", "v1"), True)]
        self.events = []
        self.is_drained = False
        self.drain_rooms = self.drain_ranked = self.drain_pending = 1
        self.drain_healthy = True
        self.fail_save = False
        self.fail_stop = False
        self.fail_start = False
        self.fence_fails = False

    def preflight(self, goal):
        pass

    def observe(self):
        return list(self.processes)

    def request_drain(self):
        self.events.append("drain")

    def drain_progress(self):
        return {
            "draining": True,
            "drained": self.is_drained,
            "rooms": self.drain_rooms,
            "ranked": self.drain_ranked,
            "pending": self.drain_pending,
            "healthy": self.drain_healthy,
        }

    def save_and_freeze(self, process):
        self.events.append("save")
        if self.fail_save:
            raise OSError("save failed")

    def stop(self, process):
        self.events.append("stop")
        if not self.fail_stop:
            self.processes = []

    def fence_pending_starts(self):
        self.events.append("fence")

    def start(self, goal):
        assert not self.processes, "second backend"
        self.events.append("start")
        if self.fail_start:
            raise OSError("start failed")
        self.processes = [Process(goal, False)]


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
        self.backend.is_drained = True
        self.controller.step()
        self.backend.processes = [Process(self.goal, True)]
        return self.controller.step()

    def test_low_disk_does_not_close_a_running_stable_backend(self):
        # Stand acceptance 2026-09-24: preflight ran on every timer step, so low free space put a healthy game in ERROR.
        self.controller.request(self.op, Goal("game", "v1"), 0)
        self.assertEqual(self.controller.step()["gate"], "public")

        def full(goal):
            raise OSError("not enough free space for the backend data")

        self.backend.preflight = full
        state = self.controller.step()
        self.assertEqual(
            (state["phase"], state["gate"], state["error"]), ("game", "public", None)
        )
        with self.assertRaises(OSError):
            self.controller.request(str(uuid4()), self.goal, state["revision"])
        self.assertEqual(self.controller.status()["phase"], "game")
        self.assertEqual(self.backend.events, [])

    def test_game_release_deploy_is_followed_without_restart(self):
        self.controller.request(self.op, Goal("game", "v1"), 0)
        self.assertEqual(self.controller.step()["gate"], "public")
        self.backend.processes = [Process(Goal("game", "v2"), True)]
        state = self.controller.step()
        self.assertEqual(
            (
                state["phase"],
                state["gate"],
                state["error"],
                state["desired"]["release"],
            ),
            ("game", "public", None, "v2"),
        )
        self.assertEqual(self.backend.events, [])
        restarted = Controller(Path(self.temp.name), self.backend)
        self.assertEqual(restarted.step()["desired"]["release"], "v2")

    def test_drain_waits_entire_campaign_without_timeout(self):
        self.request()
        for _ in range(20):
            self.assertEqual(self.controller.step()["phase"], "DRAINING")
        self.assertNotIn("stop", self.backend.events)
        self.backend.is_drained = True
        self.assertEqual(self.controller.step()["phase"], "STARTING")
        self.assertEqual(self.backend.events[-4:], ["drain", "save", "stop", "start"])

    def test_readiness_and_no_restart_on_repeated_steps(self):
        self.request()
        self.backend.is_drained = True
        self.controller.step()
        self.assertEqual(self.controller.step()["gate"], "closed")
        self.backend.processes = [Process(self.goal, True)]
        self.assertEqual(self.controller.step()["gate"], "builders")
        self.controller.step()
        self.assertEqual(self.backend.events.count("start"), 1)

    def test_bad_stop_does_not_start_second_backend(self):
        self.request()
        self.backend.is_drained = True
        self.backend.fail_stop = True
        self.assertEqual(self.controller.step()["phase"], "ERROR")
        self.assertNotIn("start", self.backend.events)
        self.assertEqual(len(self.backend.processes), 1)

    def test_save_failure_keeps_draft_process(self):
        self.start_workshop()
        self.backend.fail_save = True
        self.controller.request(str(uuid4()), Goal("game", "v1"), 1)
        previous = self.backend.events.count("stop")
        self.assertEqual(self.controller.step()["gate"], "closed")
        self.assertEqual(self.backend.events.count("stop"), previous)
        self.assertEqual(self.backend.processes[0].goal, self.goal)

    def test_restart_recovers_persisted_desired_profile(self):
        self.request()
        self.backend.is_drained = True
        self.controller.step()
        self.backend.processes = [Process(self.goal, True)]
        restarted = Controller(Path(self.temp.name), self.backend)
        self.assertEqual(restarted.step()["gate"], "builders")
        self.assertEqual(self.backend.events.count("start"), 1)

    def test_failed_start_requires_explicit_recovery(self):
        self.request()
        self.backend.is_drained = True
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

    def test_multiple_existing_backends_fail_closed(self):
        self.request()
        self.backend.processes.append(Process(self.goal, True))
        self.assertEqual(self.controller.step()["phase"], "ERROR")
        self.assertFalse(self.backend.events)

    def test_playtest_then_return_old_release_never_publishes(self):
        self.start_workshop()
        test_goal = Goal("test", "v1", self.goal.session, "a" * 64)
        self.controller.request(str(uuid4()), test_goal, 1)
        self.controller.step()
        self.backend.processes = [Process(test_goal, True)]
        self.assertEqual(self.controller.step()["gate"], "testers")
        self.controller.request(str(uuid4()), Goal("game", "v1"), 2)
        self.controller.step()
        self.assertEqual(self.controller.step()["gate"], "closed")
        self.backend.processes = [Process(Goal("game", "v1"), True)]
        self.assertEqual(self.controller.step()["gate"], "public")
        self.assertNotIn("publish", self.backend.events)

    def test_old_completed_request_cannot_replay(self):
        self.start_workshop()
        self.controller.request(str(uuid4()), Goal("game", "v1"), 1)
        self.controller.step()
        self.backend.processes = [Process(Goal("game", "v1"), True)]
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


if __name__ == "__main__":
    unittest.main()
