"""Site admin inbox: requests, results, status/members export and persisted maintenance message."""
import contextlib
import fcntl
import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from uuid import uuid4
from controller import Controller, DEFAULT_MESSAGE, Goal, Process
from test_controller import FakeBackend

DRASL = "e10438e4797b49718cfecec6a8fa171b"
ADMIN = {"draslId": DRASL, "name": "Nether", "role": "admin"}


class InboxTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)
        self.inbox = self.dir / "inbox"
        self.inbox.mkdir()
        self.admin = self.dir / "admin"
        self.backend = FakeBackend()
        self.controller = Controller(self.dir / "state", self.backend, gate_file=self.dir / "gate.json",
                                     public_file=self.dir / "public.json", admin_dir=self.admin, inbox_dir=self.inbox)
        self.controller.request(str(uuid4()), Goal("game", "v1"), 0)
        self.controller.step()
        self.log = io.StringIO()

    def test_workshop_key_created_once_and_private(self):
        key = self.admin / "workshop.key"
        text = key.read_text()
        self.assertRegex(text.strip(), r"^[0-9a-f]{64}$")
        self.assertEqual(key.stat().st_mode & 0o777, 0o640)
        Controller(self.dir / "state", self.backend, gate_file=self.dir / "gate.json",
                   public_file=self.dir / "public.json", admin_dir=self.admin, inbox_dir=self.inbox)
        self.assertEqual(key.read_text(), text, "never rotated by a restart")

    def process(self, **kwargs):
        with contextlib.redirect_stderr(self.log):
            return self.controller.process_inbox(**kwargs)

    def put(self, kind, created=None, **fields):
        request_id = str(uuid4())
        document = {"schema": 1, "id": request_id, "kind": kind, "actor": {"draslId": DRASL, "name": "Nether"},
                    "createdAt": int(time.time() * 1000) if created is None else created, **fields}
        path = self.inbox / f"{request_id}.json"
        path.write_text(json.dumps(document))
        # The controller picks its batch by mtime (the site renames files in order); coarse
        # filesystem timestamps on CI runners tie otherwise and make the batch order random.
        os.utime(path, ns=(document["createdAt"] * 1_000_000,) * 2)
        return request_id

    def result(self, request_id):
        return json.loads((self.admin / "results" / f"{request_id}.json").read_text())

    def read(self, name):
        return json.loads((self.dir / name).read_text())

    def test_mode_to_workshop_and_back(self):
        request_id = self.put("mode", profile="workshop", expectedRevision=1)
        self.assertEqual(self.process(), 1)
        result = self.result(request_id)
        self.assertEqual((result["ok"], result["error"], result["revision"]), (True, None, 2))
        self.assertEqual(set(result), {"id", "ok", "error", "revision", "doneAt"})
        self.assertEqual(os.listdir(self.inbox), [])
        state = self.controller.status()
        self.assertEqual((state["operation"], state["desired"]["profile"], state["desired"]["release"]), (request_id, "workshop", "v1"))
        self.assertIn(f"inbox {request_id} kind='mode' actor=Nether ok", self.log.getvalue())
        self.backend.is_drained = True
        self.controller.step()
        self.backend.processes = [Process(Goal(**state["desired"]), True)]
        self.assertEqual(self.controller.step()["gate"], "builders")
        back = self.put("mode", profile="game", expectedRevision=self.controller.status()["revision"])
        self.process()
        self.assertTrue(self.result(back)["ok"])
        self.assertEqual(self.controller.status()["desired"], {"profile": "game", "release": "v1", "session": None, "candidate": None})

    def test_stale_revision_and_recover_rules(self):
        stale = self.put("mode", profile="workshop", expectedRevision=0)
        bad = self.put("mode", profile="workshop", expectedRevision=1, recover=True)
        self.process()
        self.assertEqual((self.result(stale)["ok"], self.result(stale)["error"]), (False, "stale state revision"))
        self.assertEqual(self.result(bad)["error"], "recover is only allowed toward game")
        self.assertEqual(self.controller.status()["revision"], 1)

    def test_recover_toward_game_steps_explicitly(self):
        self.put("mode", profile="workshop", expectedRevision=1)
        self.process()
        self.backend.is_drained = True
        self.backend.fail_start = True
        self.assertEqual(self.controller.step()["phase"], "ERROR")
        status = json.loads((self.admin / "status.json").read_text())
        self.assertEqual((status["phase"], status["gate"], status["error"]), ("ERROR", "closed", "OSError"))
        self.backend.fail_start = False
        request_id = self.put("mode", profile="game", expectedRevision=status["revision"], recover=True)
        self.process()
        self.assertTrue(self.result(request_id)["ok"])
        self.assertEqual(self.controller.status()["phase"], "STARTING")
        self.assertIn("fence", self.backend.events)

    def test_members_replacement_and_lockout_protection(self):
        request_id = self.put("members", members=[ADMIN, {"draslId": "a" * 32, "name": "Builder"}])
        self.process()
        self.assertTrue(self.result(request_id)["ok"])
        exported = json.loads((self.admin / "members.json").read_text())
        self.assertEqual(exported, self.controller.members())
        self.assertEqual(len(exported), 2)
        lockout = self.put("members", members=[{"draslId": "a" * 32, "name": "Builder", "role": "lead"}])
        empty = self.put("members", members=[])
        self.process()
        self.assertEqual(self.result(lockout)["error"], "members must keep at least one admin")
        self.assertFalse(self.result(empty)["ok"])
        self.assertEqual(len(self.controller.members()), 2)

    def test_message_persists_into_exports(self):
        request_id = self.put("message", message="Строим новую карту")
        self.process()
        self.assertTrue(self.result(request_id)["ok"])
        self.assertEqual(self.read("public.json")["message"], "")
        self.put("mode", profile="workshop", expectedRevision=1)
        self.process()
        self.controller.step()
        self.assertEqual(self.read("gate.json")["message"], "Строим новую карту")
        self.assertEqual(self.read("public.json")["message"], "Строим новую карту")
        restarted = Controller(self.dir / "state", self.backend, admin_dir=self.admin)
        self.assertEqual(restarted.message, "Строим новую карту")
        for bad in ("x" * 201, "line\nbreak", 5):
            self.put("message", message=bad)
        self.put("message", message="")
        before = self.controller.message
        self.process()
        self.assertEqual(before, "Строим новую карту")
        self.assertEqual(self.controller.message, DEFAULT_MESSAGE)
        self.assertEqual(json.loads((self.admin / "status.json").read_text())["message"], DEFAULT_MESSAGE)

    def test_invalid_requests_get_one_result_each(self):
        target = self.dir / "outside.json"
        target.write_text("{}")
        cases = {}
        for label, content in (("json", b"{nope"), ("oversize", b" " * 65537), ("schema", b'{"schema":2}')):
            request_id = str(uuid4())
            (self.inbox / f"{request_id}.json").write_bytes(content)
            cases[label] = request_id
        cases["symlink"] = str(uuid4())
        (self.inbox / f"{cases['symlink']}.json").symlink_to(target)
        cases["directory"] = str(uuid4())
        (self.inbox / f"{cases['directory']}.json").mkdir()
        cases["expired"] = self.put("message", created=int(time.time() * 1000) - 11 * 60 * 1000, message="old")
        cases["kind"] = self.put("reboot")
        mismatched = str(uuid4())
        (self.inbox / f"{mismatched}.json").write_text(json.dumps({"schema": 1, "id": str(uuid4()), "kind": "message",
                                                                    "actor": {"draslId": DRASL, "name": "Nether"},
                                                                    "createdAt": int(time.time() * 1000), "message": "x"}))
        cases["mismatch"] = mismatched
        (self.inbox / ".tmp-ignored.tmp").write_text("{}")
        self.process()
        expected = {"json": "invalid json", "oversize": "request too large", "schema": "unsupported schema",
                    "symlink": "not a regular file", "directory": "not a regular file", "expired": "request expired",
                    "kind": "unknown kind", "mismatch": "id does not match file name"}
        for label, request_id in cases.items():
            with self.subTest(label):
                self.assertEqual((self.result(request_id)["ok"], self.result(request_id)["error"]), (False, expected[label]))
        self.assertEqual(os.listdir(self.inbox), [".tmp-ignored.tmp"])
        self.assertEqual(target.read_text(), "{}")
        self.assertEqual(self.controller.message, DEFAULT_MESSAGE)

    def test_duplicate_id_only_removes_inbox_file(self):
        request_id = self.put("message", message="first")
        self.process()
        done = self.result(request_id)
        (self.inbox / f"{request_id}.json").write_text(json.dumps({"schema": 1, "id": request_id, "kind": "message",
                                                                   "actor": {"draslId": DRASL, "name": "Nether"},
                                                                   "createdAt": int(time.time() * 1000), "message": "second"}))
        self.assertEqual(self.process(), 0)
        self.assertEqual(self.result(request_id), done)
        self.assertEqual(os.listdir(self.inbox), [])
        self.assertEqual(self.controller.message, "first")

    def test_busy_lock_leaves_request(self):
        request_id = self.put("message", message="later")
        with open(self.dir / "state" / "controller.lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.assertEqual(self.process(), 0)
        self.assertTrue((self.inbox / f"{request_id}.json").exists())
        self.assertFalse((self.admin / "results" / f"{request_id}.json").exists())
        self.process()
        self.assertTrue(self.result(request_id)["ok"])

    def test_oldest_first_batch_of_twenty(self):
        now = int(time.time() * 1000)
        ids = [self.put("message", created=now - 1000 + i, message=f"m{i}") for i in range(25)]
        self.assertEqual(self.process(), 20)
        self.assertEqual(self.controller.message, "m19")
        self.assertEqual(sorted(os.listdir(self.inbox)), sorted(f"{i}.json" for i in ids[20:]))

    def test_results_are_pruned_to_newest_200(self):
        results = self.admin / "results"
        old = []
        for i in range(205):
            path = results / f"{uuid4()}.json"
            path.write_text("{}")
            os.utime(path, (1_000_000 + i, 1_000_000 + i))
            old.append(path)
        request_id = self.put("message", message="prune")
        self.process()
        self.assertEqual(len(list(results.glob("*.json"))), 200)
        self.assertTrue((results / f"{request_id}.json").exists())
        self.assertFalse(any(p.exists() for p in old[:6]))

    def test_status_export_content_and_stable_timestamp(self):
        status = json.loads((self.admin / "status.json").read_text())
        self.assertEqual({k: v for k, v in status.items() if k not in ("updatedAt", "since")},
                         {"schema": 1, "revision": 1, "phase": "game", "gate": "public", "error": None,
                          "errorMessage": None, "deadline": None,
                          "desired": {"profile": "game", "release": "v1", "session": None, "candidate": None},
                          "message": DEFAULT_MESSAGE})
        inode = (self.admin / "status.json").stat().st_ino
        for _ in range(3):
            self.controller.step()
        self.assertEqual((self.admin / "status.json").stat().st_ino, inode)
        self.assertEqual(oct(self.admin.stat().st_mode & 0o777), "0o755")
        self.assertEqual(oct((self.admin / "status.json").stat().st_mode & 0o777), "0o644")

    def test_deeply_nested_json_gets_error_result(self):
        request_id = str(uuid4())
        (self.inbox / f"{request_id}.json").write_text("[" * 60000)
        self.assertEqual(self.process(), 1)
        self.assertEqual(self.result(request_id)["error"], "invalid json")
        self.assertEqual(os.listdir(self.inbox), [])

    def test_failed_recovery_reports_error(self):
        self.put("mode", profile="workshop", expectedRevision=1)
        self.process()
        self.backend.is_drained = True
        self.backend.fail_start = True
        self.controller.step()
        request_id = self.put("mode", profile="game", expectedRevision=self.controller.status()["revision"], recover=True)
        self.process()
        result = self.result(request_id)
        self.assertEqual((result["ok"], result["error"], result["revision"]), (False, "recovery failed: OSError", None))

    def test_accepted_request_is_not_expired_after_busy_recovery_step(self):
        self.put("mode", profile="workshop", expectedRevision=1)
        self.process()
        self.backend.is_drained = True
        self.backend.fail_start = True
        self.controller.step()
        self.backend.fail_start = False
        request_id = self.put("mode", profile="game", expectedRevision=self.controller.status()["revision"], recover=True)
        step = self.controller.step
        def busy(**kwargs):
            raise BlockingIOError()
        self.controller.step = busy
        self.assertEqual(self.process(), 0)
        self.assertEqual(self.controller.status()["operation"], request_id)
        self.controller.step = step
        self.process(now_ms=int(time.time() * 1000) + 11 * 60 * 1000)
        self.assertTrue(self.result(request_id)["ok"])
        self.assertEqual(self.controller.status()["phase"], "STARTING")

    def test_prune_tolerates_vanished_result(self):
        (self.admin / "results" / f"{uuid4()}.json").symlink_to(self.dir / "missing")
        request_id = self.put("message", message="ok")
        self.process()
        self.assertTrue(self.result(request_id)["ok"])

    def test_only_batch_is_read_and_leftover_directory_is_skipped(self):
        stuck = str(uuid4())
        (self.inbox / f"{stuck}.json").mkdir()
        (self.inbox / f"{stuck}.json" / "child").write_text("x")
        (self.admin / "results" / f"{stuck}.json").write_text("{}")
        for i in range(25):
            self.put("message", message=f"m{i}")
        reads = []
        load = self.controller._load_request
        self.controller._load_request = lambda name: reads.append(name) or load(name)
        self.assertEqual(self.process(), 20)
        self.assertEqual(len(reads), 20)
        self.assertNotIn(f"{stuck}.json", reads)
        self.assertEqual(self.process(), 5)

    def test_message_accepts_format_characters_like_the_site(self):
        text = "Стройка 👩‍💻 идёт­"
        request_id = self.put("message", message=text)
        section = self.put("message", message="§cred")
        self.process()
        self.assertTrue(self.result(request_id)["ok"])
        self.assertFalse(self.result(section)["ok"])
        self.assertEqual(self.controller.message, text)


if __name__ == "__main__":
    unittest.main()
