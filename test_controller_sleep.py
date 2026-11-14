"""Cold Workshop controller: idle sleep, wake, sleeping exports and refusals."""

from release_fixtures import catalog

import contextlib
import io
import json
import os
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path
from uuid import uuid4

import cp_fixtures
from controller import (
    SLEEP_MESSAGE,
    Controller,
    Goal,
    Process,
)
from journal import check
from test_controller import FakeBackend

DRASL = "e10438e4797b49718cfecec6a8fa171b"
ADMIN = {"draslId": DRASL, "name": "Nether", "role": "admin"}
GENERATION = "11111111-2222-3333-4444-555555555555"


class SleepBackend(FakeBackend):
    def __init__(self):
        super().__init__()
        self.generation = None
        self._activity = None

    def readiness_generation(self, profile):
        assert profile == "workshop"
        return self.generation

    def activity(self):
        return self._activity


class SleepTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)
        self.data = self.dir / "workshop-data"
        self.data.mkdir()
        self.backend = SleepBackend()
        self.clock = [1_000_000.0]
        self.controller = Controller(
            self.dir / "state",
            self.backend,
            gate_file=self.dir / "gate.json",
            public_file=self.dir / "public.json",
            admin_dir=self.dir / "admin",
            idle_sleep_s=60.0,
            workshop_data_dir=self.data,
            now=lambda: self.clock[0],
            releases=catalog(),
        )
        self.controller.state_reporter = cp_fixtures.Reporter()
        self.goal = Goal("workshop", "v1.0.0", str(uuid4()))
        self.controller.request(str(uuid4()), self.goal, 0)
        self.controller.step()
        self.backend.ready(self.goal)
        self.backend.generation = GENERATION
        self.controller.step()
        self.assertEqual(self.controller.status()["phase"], "workshop")
        self.backend.events.clear()
        self.log = io.StringIO()

    def advance(self, seconds):
        self.clock[0] += seconds

    def now_ms(self):
        return int(self.clock[0] * 1000)

    def write_activity(self, generation=GENERATION, players=0, busy=False, age_ms=0):
        self.backend._activity = {
            "schema": 1,
            "generation": generation,
            "players": players,
            "busy": busy,
            "updatedAt": self.now_ms() - age_ms,
        }

    def apply(self, kind, actor=ADMIN, **fields):
        with contextlib.redirect_stderr(self.log):
            return cp_fixtures.apply(self.controller, kind, actor, **fields)

    def sleep(self):
        """Idle steps until the editor sleeps; returns the sleeping state."""
        self.write_activity()
        self.controller.step()
        self.assertEqual(self.controller.status()["idleSince"], self.now_ms())
        self.advance(61)
        self.write_activity()
        state = self.controller.step()
        self.assertEqual(state["phase"], "sleeping")
        return state

    def test_idle_accumulates_then_sleeps_through_save_stop(self):
        self.write_activity()
        self.assertEqual(self.controller.step()["phase"], "workshop")
        idle_since = self.controller.status()["idleSince"]
        self.assertEqual(idle_since, self.now_ms())
        self.advance(30)
        self.write_activity()
        self.assertEqual(self.controller.step()["phase"], "workshop")
        self.assertEqual(self.controller.status()["idleSince"], idle_since)
        self.advance(31)
        self.write_activity()
        gates = []
        save = self.backend.save_and_freeze
        self.backend.save_and_freeze = lambda process: (
            gates.append(json.loads((self.dir / "gate.json").read_text())),
            save(process),
        )
        state = self.controller.step()
        self.assertEqual(state["phase"], "sleeping")
        # The proxy stops admitting before the save, not after the stop.
        self.assertEqual(gates[0]["backends"]["workshop"]["gate"], "closed")
        self.assertEqual(
            state["desired"],
            {
                "profile": "workshop",
                "release": "v1.0.0",
                "session": self.goal.session,
                "candidate": None,
                "modeId": None,
            },
        )
        self.assertIsNone(state["idleSince"])
        # The existing graceful path, nothing else.
        self.assertEqual(self.backend.events, ["save", "stop"])
        self.assertEqual(
            self.backend.processes, [Process(Goal("game", "v1.0.0"), True)]
        )
        gate = json.loads((self.dir / "gate.json").read_text())
        self.assertEqual(gate["backends"]["workshop"]["gate"], "sleeping")
        public = json.loads((self.dir / "public.json").read_text())
        self.assertEqual(public["workshop"]["mode"], "sleeping")
        self.controller.flush_reports()
        status = self.controller.state_reporter.docs["status"]
        self.assertEqual((status["phase"], status["gate"]), ("sleeping", "sleeping"))

    def test_activity_resets_the_timer(self):
        self.write_activity()
        self.controller.step()
        self.advance(30)
        self.write_activity(players=1)
        self.assertEqual(self.controller.step()["phase"], "workshop")
        self.assertIsNone(self.controller.status()["idleSince"])
        self.advance(45)
        self.write_activity(busy=True)
        self.assertEqual(self.controller.step()["phase"], "workshop")
        self.assertIsNone(self.controller.status()["idleSince"])
        self.assertEqual(self.backend.events, [])

    def test_foreign_generation_never_sleeps(self):
        for _ in range(5):
            self.write_activity(generation="deadbeef-dead-dead-dead-deaddeaddead")
            self.advance(61)
            self.assertEqual(self.controller.step()["phase"], "workshop")
        self.assertIsNone(self.controller.status()["idleSince"])
        self.assertEqual(self.backend.events, [])

    def test_stale_missing_or_broken_activity_never_sleeps(self):
        cases = {
            "stale": lambda: self.write_activity(age_ms=121_000),
            "missing": lambda: setattr(self.backend, "_activity", None),
            "invalid type": lambda: setattr(self.backend, "_activity", "not-a-dict"),
            "wrong schema": lambda: setattr(
                self.backend,
                "_activity",
                {
                    "schema": 2,
                    "generation": GENERATION,
                    "players": 0,
                    "busy": False,
                    "updatedAt": self.now_ms(),
                },
            ),
            "bad types": lambda: setattr(
                self.backend,
                "_activity",
                {
                    "schema": 1,
                    "generation": GENERATION,
                    "players": "none",
                    "busy": 0,
                    "updatedAt": "now",
                },
            ),
            "future": lambda: setattr(
                self.backend,
                "_activity",
                {
                    "schema": 1,
                    "generation": GENERATION,
                    "players": 0,
                    "busy": False,
                    "updatedAt": self.now_ms() + 300_000,
                },
            ),
        }
        for name, make in cases.items():
            with self.subTest(name=name):
                self.backend._activity = None
                for _ in range(3):
                    self.advance(61)
                    make()
                    self.assertEqual(self.controller.step()["phase"], "workshop", name)
                self.assertIsNone(self.controller.status()["idleSince"], name)
        self.assertEqual(self.backend.events, [])

    def test_state_reporter_activity_fallback_still_drives_idle(self):
        # When backend.activity isn't exposed, controller reads game-activity
        # from state_reporter.doc("game-activity").
        self.backend._activity = None
        self.backend.activity = None
        activity = {
            "schema": 1,
            "generation": GENERATION,
            "players": 0,
            "busy": False,
            "updatedAt": self.now_ms(),
        }
        self.controller.state_reporter.put("game-activity", activity)
        self.controller.step()
        self.advance(61)
        self.controller.state_reporter.put(
            "game-activity", {**activity, "updatedAt": self.now_ms()}
        )
        self.assertEqual(self.controller.step()["phase"], "sleeping")

    def test_unknown_readiness_generation_never_sleeps(self):
        self.backend.generation = None
        for _ in range(3):
            self.write_activity()
            self.advance(61)
            self.assertEqual(self.controller.step()["phase"], "workshop")
        self.assertEqual(self.backend.events, [])

    def test_sleep_disabled_when_zero(self):
        backend = SleepBackend()
        controller = Controller(
            self.dir / "state2",
            backend,
            gate_file=self.dir / "gate2.json",
            public_file=self.dir / "public2.json",
            idle_sleep_s=0,
            workshop_data_dir=self.data,
            now=lambda: self.clock[0],
            releases=catalog(),
        )
        goal = Goal("workshop", "v1.0.0", str(uuid4()))
        controller.request(str(uuid4()), goal, 0)
        controller.step()
        backend.ready(goal)
        backend.generation = GENERATION
        for _ in range(4):
            self.advance(900)
            self.write_activity()
            state = controller.step()
            self.assertEqual(state["phase"], "workshop")
            self.assertIsNone(state.get("idleSince"))
        self.assertNotIn("stop", backend.events)

    def test_cp_pending_backlog_blocks_sleep(self):
        self.controller.set_members([dict(ADMIN)])
        pending = [True]
        self.controller.cp_client = unittest.mock.Mock(has_pending=lambda: pending[0])
        self.write_activity()
        self.controller.step()
        self.advance(61)
        self.write_activity()
        self.assertEqual(self.controller.step()["phase"], "workshop")
        self.assertIsNone(self.controller.status()["idleSince"])
        pending[0] = False
        result = self.apply("message", message="hello")
        self.assertTrue(result["ok"])
        state = self.sleep()
        self.assertEqual(state["phase"], "sleeping")

    def test_no_sleep_while_error_or_transition(self):
        broken = Goal("workshop", "v1.0.0", str(uuid4()))
        self.controller.request(str(uuid4()), broken, 1)
        self.backend.fail_start = True
        self.assertEqual(self.controller.step()["phase"], "ERROR")
        self.backend.fail_start = False
        for _ in range(3):
            self.write_activity()
            self.advance(61)
            self.assertEqual(self.controller.step()["phase"], "ERROR")
        with self.assertRaises(ValueError):
            self.controller.cancel()
        # Explicit recovery still works; sleeping never interferes with it.
        self.controller.step(recover=True)
        self.assertIn("start", self.backend.events)

    def test_no_sleep_for_test_profile(self):
        testing = Goal("test", "v1.0.0", self.goal.session, "a" * 64)
        self.controller.request(str(uuid4()), testing, 1)
        self.controller.step()
        self.backend.ready(testing)
        self.assertEqual(self.controller.step()["phase"], "test")
        self.backend.events.clear()
        for _ in range(3):
            self.write_activity()
            self.advance(61)
            self.assertEqual(self.controller.step()["phase"], "test")
        self.assertEqual(self.backend.events, [])

    def test_sleep_wake_round_trip_keeps_session_goal(self):
        self.controller.set_members([dict(ADMIN)])
        sleeping = self.sleep()
        operation = sleeping["operation"]
        # A sleeping editor stays asleep; the timer never restarts it.
        for _ in range(3):
            self.advance(61)
            self.assertEqual(self.controller.step()["phase"], "sleeping")
        self.assertEqual(self.backend.events, ["save", "stop"])
        # Game rolled on while asleep: wake keeps the approved digest.
        self.backend.processes[0] = Process(Goal("game", "v2.0.0"), True)
        request_id = str(uuid4())
        result = self.apply("wake", operation_id=request_id)
        self.assertEqual(
            (result["ok"], result["payload"]),
            (
                True,
                {
                    "kind": "wake",
                    "phase": "REQUESTED",
                    "revision": sleeping["revision"] + 1,
                    "started": True,
                },
            ),
        )
        woken = self.controller.status()
        self.assertEqual(
            (woken["phase"], woken["operation"]),
            ("REQUESTED", request_id),
        )
        self.assertNotEqual(operation, request_id)
        self.assertEqual(
            woken["desired"],
            {
                "profile": "workshop",
                "release": "v1.0.0",
                "session": self.goal.session,
                "candidate": None,
                "modeId": None,
            },
        )
        self.assertEqual(self.controller.step()["phase"], "STARTING")
        self.assertEqual(self.backend.events, ["save", "stop", "start"])
        # The proxy keeps showing members the waking message until the editor is ready.
        workshop = json.loads((self.dir / "gate.json").read_text())["backends"][
            "workshop"
        ]
        self.assertEqual(
            (workshop["gate"], [m["name"] for m in workshop["allowed"]]),
            ("starting", ["Nether"]),
        )
        self.backend.ready(Goal("workshop", "v1.0.0", self.goal.session))
        self.backend.generation = GENERATION
        restarted = self.controller.step()
        self.assertEqual(
            (restarted["phase"], restarted["gate"]), ("workshop", "builders")
        )

    def test_wake_is_idempotent_when_awake_or_starting(self):
        self.controller.set_members([dict(ADMIN)])
        revision = self.controller.status()["revision"]
        result = self.apply("wake")
        self.assertTrue(result["ok"])
        self.assertEqual(result["payload"]["phase"], "workshop")
        self.assertIsNone(result["revision"])
        self.assertFalse(result["payload"]["started"])
        self.assertEqual(self.controller.status()["revision"], revision)
        self.assertEqual(self.backend.events, [])
        # While STARTING a wake also reports ok without a second start.
        switching = Goal("workshop", "v1.0.0", str(uuid4()))
        self.controller.request(str(uuid4()), switching, revision)
        self.controller.step()
        self.assertEqual(self.controller.status()["phase"], "STARTING")
        starts = self.backend.events.count("start")
        result = self.apply("wake")
        self.assertEqual((result["ok"], result["payload"]["phase"]), (True, "STARTING"))
        self.assertFalse(result["payload"]["started"])
        self.assertEqual(self.backend.events.count("start"), starts)

    def test_repeated_wake_while_starting_is_single_start(self):
        self.controller.set_members([dict(ADMIN)])
        self.sleep()
        self.assertTrue(self.apply("wake")["ok"])
        result = self.apply("wake")
        self.assertEqual(
            (result["ok"], result["payload"]["phase"]), (True, "REQUESTED")
        )
        self.controller.step()
        self.assertEqual(self.backend.events.count("start"), 1)

    def test_wake_retry_reexports_staged_leg(self):
        """Round 8 / ruling 3: a wake whose first attempt committed
        REQUESTED but failed staging must stage on same-op retry, not
        return started:False with the leg unstaged (which sync would ACK
        effective). Fail-before: the retry returns without staging."""
        from controller import CommitIncompleteError

        self.controller.set_members([dict(ADMIN)])

        class BoomReporter:
            def report(self, leg):
                raise RuntimeError("cp down")

        self.controller.gate_reporter = BoomReporter()
        self.sleep()
        # The sleep steps staged a sleeping leg that CP never got (Boom);
        # drop it so only the wake retry can stage anything below.
        (self.dir / "state" / "gate_pending.json").unlink(missing_ok=True)
        operation = str(uuid4())
        real_atomic_write = Controller._stage_gate_leg.__globals__["atomic_write"]

        def fail_pending(path, *args, **kwargs):
            if Path(path).name == "gate_pending.json":
                raise OSError("ENOSPC")
            return real_atomic_write(path, *args, **kwargs)

        with unittest.mock.patch("controller.atomic_write", side_effect=fail_pending):
            with self.assertRaises(CommitIncompleteError):
                self.controller.wake(operation)
            # The failed staging left nothing behind: prove the retry is
            # what stages, not a leftover leg.
            self.assertFalse(self.controller.gate_report_pending())
        result = self.controller.wake(operation)
        self.assertEqual((result["started"], result["phase"]), (False, "REQUESTED"))
        self.assertTrue(self.controller.gate_report_pending())
        record = json.loads((self.dir / "state" / "gate_pending.json").read_text())
        self.assertEqual(record["leg"]["state"], "starting")

    def test_wake_rejects_strangers(self):
        result = self.apply("wake", actor={"draslId": "0" * 32, "name": "Stranger"})
        self.assertFalse(result["ok"])
        self.assertIn("member", result["error"])

    def test_live_only_kinds_refused_while_sleeping(self):
        self.controller.set_members([dict(ADMIN)])
        self.sleep()
        for kind, fields in (
            ("limbo-export", {"versionDigest": "v"}),
            ("map-create", {"mapId": "fresh"}),
            ("mode", {"profile": "test", "expectedRevision": 99}),
        ):
            with self.subTest(kind=kind):
                result = self.apply(kind, **fields)
                self.assertFalse(result["ok"], kind)
                self.assertEqual(result["error"], SLEEP_MESSAGE, kind)
        self.assertEqual(self.controller.status()["phase"], "sleeping")
        # Offline-safe kinds still work.
        self.assertTrue(self.apply("message", message="note")["ok"])
        self.assertTrue(self.apply("members", members=[dict(ADMIN)])["ok"])

    def test_sleeping_gate_carries_acl_for_proxy(self):
        self.controller.set_members([dict(ADMIN)])
        self.sleep()
        gate = json.loads((self.dir / "gate.json").read_text())
        workshop = gate["backends"]["workshop"]
        self.assertEqual(workshop["gate"], "sleeping")
        self.assertEqual(
            workshop["allowed"], [{"draslId": DRASL, "name": "Nether", "role": "admin"}]
        )
        self.assertEqual(gate["backends"]["game"]["gate"], "public")

    def test_sleeping_journal_record_is_valid_but_needs_no_backend(self):
        record = {
            "t": 1,
            "game_java": 1,
            "workshop_java": 0,
            "unknown_java": 0,
            "game_ready": True,
            "workshop_ready": False,
            "game_profile": "game",
            "workshop_profile": None,
            "backends": {"game": {"gate": "public"}, "workshop": {"gate": "sleeping"}},
        }
        self.assertEqual(check([record]), [])
        self.assertTrue(check([{**record, "workshop_java": 1}]))
        # A waking editor has a not-yet-ready backend, or none at all.
        for java in (0, 1):
            starting = {**record, "workshop_java": java}
            starting["backends"] = {
                **record["backends"],
                "workshop": {"gate": "starting"},
            }
            self.assertEqual(check([starting]), [])
        self.assertTrue(
            check(
                [
                    {
                        **record,
                        "backends": {
                            "game": {"gate": "sleeping"},
                            "workshop": {"gate": "sleeping"},
                        },
                    }
                ]
            )
        )


class CliWakeTests(unittest.TestCase):
    """workshopctl wake: operator wake plus an immediate start, idempotent."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.backend = SleepBackend()
        (self.root / "controller").mkdir()
        (self.root / "controller/releases.json").write_text(json.dumps(catalog()))
        self.data = self.root / "workshop-data"
        self.data.mkdir()
        self.environment = unittest.mock.patch.dict(
            os.environ,
            {
                "BF_WORKSHOP_IDLE_SLEEP_S": "1",
                "BF_WORKSHOP_DATA": str(self.data),
                "BF_STEP_FOLLOW_S": "0",
            },
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        store = unittest.mock.patch("workshopctl._map_store", return_value=None)
        store.start()
        self.addCleanup(store.stop)

    def ctl(self, *argv):
        import workshopctl

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = workshopctl.main(
                [
                    "--state-dir",
                    str(self.root / "controller"),
                    "--gate-file",
                    str(self.root / "gate" / "gate.json"),
                    "--public-file",
                    str(self.root / "public" / "public.json"),
                    "--admin-dir",
                    str(self.root / "admin"),
                    *argv,
                ],
                lambda: self.backend,
            )
        return code, json.loads(out.getvalue()) if out.getvalue() else err.getvalue()

    def write_activity(self):
        self.backend._activity = {
            "schema": 1,
            "generation": GENERATION,
            "players": 0,
            "busy": False,
            "updatedAt": int(time.time() * 1000),
        }

    def test_wake_starts_the_same_sleeping_session(self):
        self.assertEqual(self.ctl("request", "game", "--release", "v1.0.0")[0], 0)
        self.assertEqual(self.ctl("step")[1]["phase"], "STARTING")
        state = self.ctl("status")[1]["state"]
        session = state["desired"]["session"]
        self.backend.ready(Goal(**state["desired"]))
        self.backend.generation = GENERATION
        self.assertEqual(self.ctl("step")[1]["gate"], "builders")
        self.write_activity()
        self.assertEqual(self.ctl("step")[1]["phase"], "workshop")
        time.sleep(1.1)
        self.write_activity()
        self.assertEqual(self.ctl("step")[1]["phase"], "sleeping")
        code, result = self.ctl("wake")
        self.assertEqual(code, 0)
        self.assertEqual(result["wake"]["phase"], "REQUESTED")
        self.assertTrue(result["wake"]["started"])
        self.assertEqual(result["state"]["phase"], "STARTING")
        self.assertEqual(result["state"]["desired"]["session"], session)
        self.backend.ready(Goal(**result["state"]["desired"]))
        self.assertEqual(self.ctl("step")[1]["gate"], "builders")
        # Already awake: wake is a no-op with the same session.
        code, result = self.ctl("wake")
        self.assertEqual((code, result["phase"]), (0, "workshop"))


if __name__ == "__main__":
    unittest.main()
