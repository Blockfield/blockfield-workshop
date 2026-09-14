import contextlib
import fcntl
import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from uuid import uuid4
from controller import Goal, Process
from test_controller import FakeBackend
import workshopctl

DRASL = "e10438e4797b49718cfecec6a8fa171b"


class WorkshopctlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.backend = FakeBackend()
        self.backend.is_drained = True

    def ctl(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = workshopctl.main(["--state-dir", str(self.root / "controller"), "--gate-file", str(self.root / "gate" / "gate.json"),
                                     "--public-file", str(self.root / "public" / "public.json"),
                                     "--admin-dir", str(self.root / "admin"), "--inbox-dir", str(self.root / "inbox"), *argv], lambda: self.backend)
        return code, json.loads(out.getvalue()) if out.getvalue() else err.getvalue()

    def test_switch_round_trip(self):
        code, state = self.ctl("status")
        self.assertEqual((code, state["state"]["phase"]), (0, "UNINITIALIZED"))
        self.assertEqual(self.ctl("step")[1]["phase"], "UNINITIALIZED")
        self.assertEqual(self.ctl("request", "game")[0], 1)
        self.assertEqual(self.ctl("request", "game", "--release", "v1")[1]["phase"], "REQUESTED")
        self.assertEqual(self.ctl("step")[1]["gate"], "public")
        self.assertEqual(oct((self.root / "controller").stat().st_mode & 0o777), "0o700")

        session = str(uuid4())
        self.assertEqual(self.ctl("members", f"{DRASL}:Nether:admin")[1]["members"][0]["role"], "admin")
        state = self.ctl("request", "workshop", "--session", session)[1]
        self.assertEqual(state["desired"], {"profile": "workshop", "release": "v1", "session": session, "candidate": None})
        self.assertEqual(self.ctl("step")[1]["phase"], "STARTING")
        self.backend.processes = [Process(Goal("workshop", "v1", session), True)]
        self.assertEqual(self.ctl("step")[1]["gate"], "builders")
        gate = json.loads((self.root / "gate" / "gate.json").read_text())
        self.assertEqual((gate["backend"], gate["allowed"][0]["name"]), ("workshop", "Nether"))
        self.assertEqual(json.loads((self.root / "public" / "public.json").read_text())["mode"], "workshop")

    def test_recover_runs_explicit_step(self):
        self.ctl("request", "game", "--release", "v1")
        self.ctl("step")
        self.ctl("request", "workshop", "--session", str(uuid4()))
        self.backend.fail_start = True
        code, state = self.ctl("step")
        self.assertEqual((code, state["phase"]), (2, "ERROR"))
        self.assertEqual(self.ctl("request", "game")[0], 1)
        self.backend.fail_start = False
        code, state = self.ctl("request", "game", "--recover")
        self.assertEqual((code, state["phase"]), (0, "STARTING"))
        self.assertIn("fence", self.backend.events)

    def test_step_applies_inbox_before_reconcile(self):
        self.ctl("request", "game", "--release", "v1")
        self.ctl("step")
        request_id = str(uuid4())
        (self.root / "inbox").mkdir()
        (self.root / "inbox" / f"{request_id}.json").write_text(json.dumps(
            {"schema": 1, "id": request_id, "kind": "mode", "actor": {"draslId": DRASL, "name": "Nether"},
             "createdAt": int(time.time() * 1000), "profile": "workshop", "expectedRevision": 1}))
        code, state = self.ctl("step")
        self.assertEqual((code, state["phase"], state["operation"]), (0, "STARTING", request_id))
        self.assertTrue(json.loads((self.root / "admin" / "results" / f"{request_id}.json").read_text())["ok"])
        self.assertEqual(json.loads((self.root / "admin" / "status.json").read_text())["phase"], "STARTING")

    def test_bad_member_and_busy_lock(self):
        self.assertEqual(self.ctl("members", "nope")[0], 1)
        self.ctl("status")
        with open(self.root / "controller" / "controller.lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.assertEqual(self.ctl("request", "game", "--release", "v1")[0], workshopctl.BUSY)


if __name__ == "__main__":
    unittest.main()
