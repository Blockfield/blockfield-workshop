"""Entrypoint shutdown: a stop set mid-wait or mid-follow (SIGTERM) exits
promptly, and an unset stop iterates on the interval timeout.

No sockets, no controller: a fake controller plus a threading.Event standing
in for the SIGTERM handler.
"""

import threading
import time
import unittest
from unittest import mock

import entrypoint


def loop_env(**overrides):
    env = {
        "BF_STEP_INTERVAL_S": "30",
        "BF_STEP_FOLLOW_S": "5",
        "BF_STEP_POLL_S": "0.01",
    }
    env.update(overrides)
    return env


class RunLoopShutdownTests(unittest.TestCase):
    def fake_controller(self, phase="workshop"):
        controller = mock.Mock()
        controller.process_inbox.return_value = None
        controller.step.return_value = {"phase": phase}
        return controller

    def run_until_passes(self, controller, stop, env, passes):
        """Run the loop until the controller has done N passes, then stop it."""
        thread = threading.Thread(
            target=entrypoint.run_loop, args=(controller, stop, env), daemon=True
        )
        thread.start()
        deadline = time.monotonic() + 10
        while controller.step.call_count < passes and time.monotonic() < deadline:
            time.sleep(0.01)
        stop.set()  # SIGTERM handler only sets this event
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        return thread

    def test_sigterm_mid_wait_exits_before_interval(self):
        controller = self.fake_controller()
        stop = threading.Event()
        started = time.monotonic()
        thread = threading.Thread(
            target=entrypoint.run_loop,
            args=(controller, stop, loop_env()),
            daemon=True,
        )
        thread.start()
        deadline = time.monotonic() + 10
        while controller.step.call_count < 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(controller.step.call_count, 1)
        stop.set()
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        # A 30 s interval: a prompt exit proves the wait woke on the event.
        self.assertLess(time.monotonic() - started, 10)

    def test_interval_timeout_iterates_without_signal(self):
        controller = self.fake_controller()
        self.run_until_passes(
            controller, threading.Event(), loop_env(BF_STEP_INTERVAL_S="0.05"), 3
        )
        self.assertGreaterEqual(controller.step.call_count, 3)

    def test_sigterm_mid_follow_aborts_the_deadline(self):
        controller = self.fake_controller(phase="STARTING")
        stop = threading.Event()
        started = time.monotonic()
        thread = threading.Thread(
            target=entrypoint.run_loop,
            args=(controller, stop, loop_env()),
            daemon=True,
        )
        thread.start()
        deadline = time.monotonic() + 10
        while controller.step.call_count < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertGreaterEqual(controller.step.call_count, 2)
        stop.set()
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        # A 5 s follow deadline: a prompt exit proves the follow loop polls stop.
        self.assertLess(time.monotonic() - started, 5)


class MainShutdownTests(unittest.TestCase):
    def test_sigterm_mid_run_shuts_down_and_returns_zero(self):
        controller = mock.Mock()
        controller.process_inbox.return_value = None
        controller.step.return_value = {"phase": "workshop"}
        server = mock.Mock()
        handlers = {}
        with (
            mock.patch.object(
                entrypoint.workshopctl,
                "build_controller",
                return_value=(controller, mock.Mock()),
            ),
            mock.patch.object(entrypoint, "HTTPServer", return_value=server),
            mock.patch.object(
                entrypoint.signal,
                "signal",
                side_effect=lambda name, handler: handlers.setdefault(
                    name.name, handler
                ),
            ),
        ):
            result = {}
            thread = threading.Thread(
                target=lambda: result.setdefault(
                    "rc",
                    entrypoint.main(loop_env(BF_STEP_INTERVAL_S="30")),
                ),
                daemon=True,
            )
            thread.start()
            deadline = time.monotonic() + 10
            while controller.step.call_count < 1 and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(controller.step.call_count, 1)
            handlers["SIGTERM"](entrypoint.signal.SIGTERM, None)
            handlers["SIGTERM"](entrypoint.signal.SIGTERM, None)
            thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result.get("rc"), 0)
        server.shutdown.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
