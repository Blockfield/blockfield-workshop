"""Phase deadlines, automatic rollback, the unattended-restart convergence path, and cancel()."""

import tempfile
import unittest
from pathlib import Path
from uuid import uuid4
from controller import Controller, Goal, Process
from test_controller import FakeBackend


class Clock:
    """Controller-injectable fake time.time, so deadlines fire without a real sleep."""

    def __init__(self, start: float = 1_700_000_000.0):
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class TimeoutTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)
        self.backend = FakeBackend()
        self.clock = Clock()
        self.session = str(uuid4())

    def controller(self, **kwargs) -> Controller:
        return Controller(self.dir, self.backend, now=self.clock, **kwargs)

    def to_stable_game(self, controller: Controller) -> None:
        controller.request(str(uuid4()), Goal("game", "v1"), 0)
        self.assertEqual(controller.step()["phase"], "game")

    def start_switch_to_workshop(self, controller: Controller) -> dict:
        controller.request(
            str(uuid4()),
            Goal("workshop", "v1", self.session),
            controller.status()["revision"],
        )
        return controller.step()

    def test_drain_timeout_rolls_back_without_touching_the_running_game(self):
        controller = self.controller(drain_timeout=100, drain_timeout_ranked=100)
        self.to_stable_game(controller)
        state = self.start_switch_to_workshop(controller)
        self.assertEqual(state["phase"], "DRAINING")
        self.clock.advance(50)
        self.assertEqual(
            controller.step()["phase"], "DRAINING"
        )  # still within the deadline
        self.clock.advance(51)
        state = controller.step()
        self.assertEqual(
            (state["phase"], state["gate"], state["desired"]["profile"]),
            ("game", "public", "game"),
        )
        self.assertIn("timed out", state["errorMessage"])
        self.assertNotIn(
            "save", self.backend.events
        )  # DRAINING never touched the old backend
        self.assertNotIn("stop", self.backend.events)

    def test_drain_health_grace_rolls_back_fast_when_nothing_is_at_risk(self):
        # The 2026-09-18 incident: rooms=ranked=pending=0 but the stats-persistence flag stuck unhealthy.
        self.backend.drain_rooms = self.backend.drain_ranked = (
            self.backend.drain_pending
        ) = 0
        self.backend.drain_healthy = False
        controller = self.controller(
            drain_health_grace=30, drain_timeout=10_000, drain_timeout_ranked=10_000
        )
        self.to_stable_game(controller)
        self.start_switch_to_workshop(controller)
        self.clock.advance(31)
        state = controller.step()
        self.assertEqual(state["phase"], "game")
        self.assertIn("unhealthy", state["errorMessage"])

    def test_start_timeout_rolls_back_and_restarts_the_previous_profile(self):
        controller = self.controller(start_timeout=100)
        self.to_stable_game(controller)
        self.backend.is_drained = True
        self.start_switch_to_workshop(
            controller
        )  # DRAINING -> SAVING -> STARTING, workshop not ready
        state = controller.step()
        self.assertEqual(state["phase"], "STARTING")
        self.clock.advance(101)
        state = controller.step()
        self.assertEqual(
            state["phase"], "STARTING"
        )  # rolling back = starting "game" again
        self.assertEqual(state["desired"]["profile"], "game")
        self.assertIn("timed out", state["errorMessage"])
        self.backend.processes = [Process(Goal("game", "v1"), True)]
        state = controller.step()
        self.assertEqual((state["phase"], state["gate"]), ("game", "public"))

    def test_unexpected_backend_during_start_is_fenced_not_fatal(self):
        # 2026-09-18 root cause: an external restart resurrected a backend of a different profile
        # while STARTING was unresolved. That used to raise RuntimeError; now it fences and keeps waiting.
        controller = self.controller(start_timeout=100)
        self.to_stable_game(controller)
        self.backend.is_drained = True
        self.start_switch_to_workshop(controller)
        self.assertEqual(controller.step()["phase"], "STARTING")
        self.backend.processes = [
            Process(Goal("game", "v1"), True)
        ]  # stray, external restart
        state = controller.step()
        self.assertEqual(state["phase"], "STARTING")
        self.assertNotEqual(state.get("error"), "RuntimeError")
        self.assertIn("fence", self.backend.events)
        # It keeps converging up to the deadline rather than getting stuck in ERROR:
        self.clock.advance(101)
        state = controller.step()
        self.assertNotEqual(state["phase"], "ERROR")

    def test_error_carries_a_human_readable_message(self):
        controller = self.controller()
        self.to_stable_game(controller)
        self.backend.processes.append(
            Process(Goal("game", "v1"), True)
        )  # a second full backend
        state = controller.step()
        self.assertEqual(state["phase"], "ERROR")
        self.assertEqual(state["error"], "RuntimeError")
        self.assertEqual(state["errorMessage"], "multiple full backends observed")

    def test_cancel_rolls_back_a_stuck_drain_without_root(self):
        controller = self.controller(drain_timeout=10_000, drain_timeout_ranked=10_000)
        self.to_stable_game(controller)
        self.start_switch_to_workshop(controller)
        self.assertEqual(controller.step()["phase"], "DRAINING")
        state = controller.cancel()
        self.assertEqual((state["phase"], state["gate"]), ("game", "public"))
        self.assertIn("cancelled", state["errorMessage"])

    def test_cancel_refuses_when_nothing_is_in_progress(self):
        controller = self.controller()
        self.to_stable_game(controller)
        with self.assertRaises(ValueError):
            controller.cancel()

    def test_error_message_clears_once_recovery_reaches_a_stable_phase(self):
        controller = self.controller()
        self.to_stable_game(controller)
        self.backend.processes.append(
            Process(Goal("game", "v1"), True)
        )  # force a real ERROR
        state = controller.step()
        self.assertEqual(state["phase"], "ERROR")
        self.assertTrue(state["errorMessage"])
        self.backend.processes = self.backend.processes[:1]  # clear the second backend
        controller.request(
            str(uuid4()), Goal("game", "v1"), state["revision"], recover=True
        )
        state = controller.step(recover=True)
        self.assertEqual(state["phase"], "game")
        self.assertIsNone(state["error"])
        self.assertIsNone(state["errorMessage"])


if __name__ == "__main__":
    unittest.main()
