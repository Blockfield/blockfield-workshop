"""Site admin inbox: requests, results, status/members export and persisted maintenance message."""

from release_fixtures import catalog

import contextlib
import fcntl
import hashlib
import io
import json
import os
import stat
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path
from uuid import uuid4
from controller import Controller, DEFAULT_MESSAGE, Goal, Process, INBOX_MERGE_MAX_BYTES
from test_controller import FakeBackend
from controller import (
    AI_KEY_UID as PRODUCTION_KEY_UID,
    AI_KEY_GID as PRODUCTION_KEY_GID,
    ensure_workshop_key,
)

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
        self.controller = Controller(
            self.dir / "state",
            self.backend,
            gate_file=self.dir / "gate.json",
            public_file=self.dir / "public.json",
            admin_dir=self.admin,
            inbox_dir=self.inbox,
            releases=catalog(),
        )
        self.controller.request(str(uuid4()), Goal("game", "v1.0.0"), 0)
        self.controller.step()
        self.backend.ready(Goal(**self.controller.status()["desired"]))
        self.controller.step()
        self.log = io.StringIO()

    def test_workshop_key_created_once_and_private(self):
        key = self.admin / "workshop.key"
        text = key.read_text()
        self.assertRegex(text.strip(), r"^[0-9a-f]{64}$")
        self.assertEqual(key.stat().st_mode & 0o777, 0o640)
        Controller(
            self.dir / "state",
            self.backend,
            gate_file=self.dir / "gate.json",
            public_file=self.dir / "public.json",
            admin_dir=self.admin,
            inbox_dir=self.inbox,
            releases=catalog(),
        )
        self.assertEqual(key.read_text(), text, "never rotated by a restart")

    def process(self, **kwargs):
        with contextlib.redirect_stderr(self.log):
            return self.controller.process_inbox(**kwargs)

    def put(self, kind, created=None, **fields):
        request_id = str(uuid4())
        document = {
            "schema": 1,
            "id": request_id,
            "kind": kind,
            "actor": {"draslId": DRASL, "name": "Nether"},
            "createdAt": int(time.time() * 1000) if created is None else created,
            **fields,
        }
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
        self.assertEqual(
            (result["ok"], result["error"], result["revision"]), (True, None, 2)
        )
        self.assertEqual(set(result), {"id", "ok", "error", "revision", "doneAt"})
        self.assertEqual(os.listdir(self.inbox), [])
        state = self.controller.status()
        self.assertEqual(
            (
                state["operation"],
                state["desired"]["profile"],
                state["desired"]["release"],
            ),
            (request_id, "workshop", "v1.0.0"),
        )
        self.assertIn(
            f"inbox {request_id} kind='mode' actor=Nether ok", self.log.getvalue()
        )
        self.controller.step()
        self.backend.ready(Goal(**state["desired"]))
        self.assertEqual(self.controller.step()["gate"], "builders")
        back = self.put(
            "mode",
            profile="game",
            expectedRevision=self.controller.status()["revision"],
        )
        self.process()
        self.assertTrue(self.result(back)["ok"])
        self.assertEqual(self.controller.status()["desired"], state["desired"])
        self.assertEqual(self.backend.processes[0].goal.profile, "game")

    def test_stale_revision_and_recover_rules(self):
        stale = self.put("mode", profile="workshop", expectedRevision=0)
        bad = self.put("mode", profile="workshop", expectedRevision=1, recover=True)
        self.process()
        self.assertEqual(
            (self.result(stale)["ok"], self.result(stale)["error"]),
            (False, "stale state revision"),
        )
        self.assertEqual(
            self.result(bad)["error"], "recover is only allowed toward game"
        )
        self.assertEqual(self.controller.status()["revision"], 1)

    def test_recover_toward_game_steps_explicitly(self):
        self.put("mode", profile="workshop", expectedRevision=1)
        self.process()
        self.backend.fail_start = True
        self.assertEqual(self.controller.step()["phase"], "ERROR")
        status = json.loads((self.admin / "status.json").read_text())
        self.assertEqual(
            (status["phase"], status["gate"], status["error"]),
            ("ERROR", "closed", "OSError"),
        )
        self.backend.fail_start = False
        request_id = self.put(
            "mode", profile="game", expectedRevision=status["revision"], recover=True
        )
        self.process()
        self.assertTrue(self.result(request_id)["ok"])
        self.assertEqual(self.controller.status()["phase"], "STARTING")
        self.assertIn("fence", self.backend.events)

    def test_members_replacement_and_lockout_protection(self):
        request_id = self.put(
            "members", members=[ADMIN, {"draslId": "a" * 32, "name": "Builder"}]
        )
        self.process()
        self.assertTrue(self.result(request_id)["ok"])
        exported = json.loads((self.admin / "members.json").read_text())
        self.assertEqual(exported, self.controller.members())
        self.assertEqual(len(exported), 2)
        lockout = self.put(
            "members",
            members=[{"draslId": "a" * 32, "name": "Builder", "role": "lead"}],
        )
        empty = self.put("members", members=[])
        self.process()
        self.assertEqual(
            self.result(lockout)["error"], "members must keep at least one admin"
        )
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
        self.assertEqual(
            self.read("gate.json")["backends"]["workshop"]["message"],
            "Строим новую карту",
        )
        self.assertEqual(
            self.read("public.json")["workshop"]["message"], "Строим новую карту"
        )
        self.assertEqual(self.read("public.json")["message"], "")
        restarted = Controller(
            self.dir / "state", self.backend, admin_dir=self.admin, releases=catalog()
        )
        self.assertEqual(restarted.message, "Строим новую карту")
        for bad in ("x" * 201, "line\nbreak", 5):
            self.put("message", message=bad)
        self.put("message", message="")
        before = self.controller.message
        self.process()
        self.assertEqual(before, "Строим новую карту")
        self.assertEqual(self.controller.message, DEFAULT_MESSAGE)
        self.assertEqual(
            json.loads((self.admin / "status.json").read_text())["message"],
            DEFAULT_MESSAGE,
        )

    def test_invalid_requests_get_one_result_each(self):
        target = self.dir / "outside.json"
        target.write_text("{}")
        cases = {}
        for label, content in (
            ("json", b"{nope"),
            ("oversize", b" " * 65537),
            ("schema", b'{"schema":2}'),
        ):
            request_id = str(uuid4())
            (self.inbox / f"{request_id}.json").write_bytes(content)
            cases[label] = request_id
        cases["symlink"] = str(uuid4())
        (self.inbox / f"{cases['symlink']}.json").symlink_to(target)
        cases["directory"] = str(uuid4())
        (self.inbox / f"{cases['directory']}.json").mkdir()
        cases["expired"] = self.put(
            "message", created=int(time.time() * 1000) - 11 * 60 * 1000, message="old"
        )
        cases["kind"] = self.put("reboot")
        mismatched = str(uuid4())
        (self.inbox / f"{mismatched}.json").write_text(
            json.dumps(
                {
                    "schema": 1,
                    "id": str(uuid4()),
                    "kind": "message",
                    "actor": {"draslId": DRASL, "name": "Nether"},
                    "createdAt": int(time.time() * 1000),
                    "message": "x",
                }
            )
        )
        cases["mismatch"] = mismatched
        (self.inbox / ".tmp-ignored.tmp").write_text("{}")
        self.process()
        expected = {
            "json": "invalid json",
            "oversize": "request too large",
            "schema": "unsupported schema",
            "symlink": "not a regular file",
            "directory": "not a regular file",
            "expired": "request expired",
            "kind": "unknown kind",
            "mismatch": "id does not match file name",
        }
        for label, request_id in cases.items():
            with self.subTest(label):
                self.assertEqual(
                    (self.result(request_id)["ok"], self.result(request_id)["error"]),
                    (False, expected[label]),
                )
        self.assertEqual(os.listdir(self.inbox), [".tmp-ignored.tmp"])
        self.assertEqual(target.read_text(), "{}")
        self.assertEqual(self.controller.message, DEFAULT_MESSAGE)

    def test_duplicate_id_only_removes_inbox_file(self):
        request_id = self.put("message", message="first")
        self.process()
        done = self.result(request_id)
        (self.inbox / f"{request_id}.json").write_text(
            json.dumps(
                {
                    "schema": 1,
                    "id": request_id,
                    "kind": "message",
                    "actor": {"draslId": DRASL, "name": "Nether"},
                    "createdAt": int(time.time() * 1000),
                    "message": "second",
                }
            )
        )
        self.assertEqual(self.process(), 0)
        self.assertEqual(self.result(request_id), done)
        self.assertEqual(os.listdir(self.inbox), [])
        self.assertEqual(self.controller.message, "first")

    def test_only_merge_requests_accept_large_exact_conflict_choices(self):
        choices = {f"world/region/r.{index}.0.mca": "ours" for index in range(10000)}
        for kind in ("merge-preview", "publish"):
            request_id = self.put(kind, resolutions=choices)
            path = self.inbox / f"{request_id}.json"
            self.assertGreater(path.stat().st_size, 65536)
            document, error = self.controller._load_request(path.name)
            self.assertIsNone(error)
            self.assertEqual(document["resolutions"], choices)
        for kind in (
            "candidate",
            "commit",
            "message",
            "sessions",
            "unknown",
            ["publish"],
            {"kind": "publish"},
        ):
            request_id = self.put(kind, resolutions=choices)
            document, error = self.controller._load_request(f"{request_id}.json")
            self.assertIsNone(document)
            self.assertEqual(error, "request too large")
        self.assertEqual(self.backend.events, ["start"])

    def test_extended_merge_limit_and_duplicate_json_keys_fail_closed(self):
        for kind in ("merge-preview", "publish"):
            request_id = self.put(kind, padding="x" * INBOX_MERGE_MAX_BYTES)
            document, error = self.controller._load_request(f"{request_id}.json")
            self.assertIsNone(document)
            self.assertEqual(error, "request too large")
        request_id = str(uuid4())
        (self.inbox / f"{request_id}.json").write_bytes(
            b'{"schema":1,"kind":"message","kind":"publish"}'
        )
        document, error = self.controller._load_request(f"{request_id}.json")
        self.assertIsNone(document)
        self.assertEqual(error, "invalid json")

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
        ids = [
            self.put("message", created=now - 1000 + i, message=f"m{i}")
            for i in range(25)
        ]
        self.assertEqual(self.process(), 20)
        self.assertEqual(self.controller.message, "m19")
        self.assertEqual(
            sorted(os.listdir(self.inbox)), sorted(f"{i}.json" for i in ids[20:])
        )

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
        self.assertEqual(
            {k: v for k, v in status.items() if k not in ("updatedAt", "since")},
            {
                "schema": 1,
                "revision": 1,
                "operation": self.controller.status()["operation"],
                "selection": {
                    key: self.controller.status()["selection"][key]
                    for key in ("operation", "revision", "deferred")
                },
                "phase": "workshop",
                "gate": "builders",
                "error": None,
                "errorMessage": None,
                "deadline": None,
                "desired": {
                    "profile": "workshop",
                    "release": "v1.0.0",
                    "session": self.controller.session()["id"],
                    "candidate": None,
                    "modeId": None,
                },
                "message": DEFAULT_MESSAGE,
                "game": {"phase": "game", "gate": "public", "release": "v1.0.0"},
            },
        )
        inode = (self.admin / "status.json").stat().st_ino
        for _ in range(3):
            self.controller.step()
        self.assertEqual((self.admin / "status.json").stat().st_ino, inode)
        self.assertEqual(oct(self.admin.stat().st_mode & 0o777), "0o755")
        self.assertEqual(
            oct((self.admin / "status.json").stat().st_mode & 0o777), "0o644"
        )

    def test_deeply_nested_json_gets_error_result(self):
        request_id = str(uuid4())
        (self.inbox / f"{request_id}.json").write_text("[" * 60000)
        self.assertEqual(self.process(), 1)
        self.assertEqual(self.result(request_id)["error"], "invalid json")
        self.assertEqual(os.listdir(self.inbox), [])

    def test_members_write_reports_lost_workshop_readiness_without_closing_game(self):
        goal = Goal(**self.controller.status()["desired"])
        self.backend.processes[-1] = Process(goal, False)
        self.controller.set_members([ADMIN])
        status = json.loads((self.admin / "status.json").read_bytes())
        self.assertEqual((status["phase"], status["gate"]), ("workshop", "closed"))
        self.assertEqual(
            status["game"], {"phase": "game", "gate": "public", "release": "v1.0.0"}
        )

    def test_failed_recovery_reports_error(self):
        self.put("mode", profile="workshop", expectedRevision=1)
        self.process()
        self.backend.fail_start = True
        self.controller.step()
        request_id = self.put(
            "mode",
            profile="game",
            expectedRevision=self.controller.status()["revision"],
            recover=True,
        )
        self.process()
        result = self.result(request_id)
        self.assertEqual(
            (result["ok"], result["error"], result["revision"]),
            (False, "recovery failed: OSError", None),
        )

    def test_accepted_request_is_not_expired_after_busy_recovery_step(self):
        self.put("mode", profile="workshop", expectedRevision=1)
        self.process()
        self.backend.fail_start = True
        self.controller.step()
        self.backend.fail_start = False
        request_id = self.put(
            "mode",
            profile="game",
            expectedRevision=self.controller.status()["revision"],
            recover=True,
        )
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


class AiKeyInboxTests(unittest.TestCase):
    def setUp(self):
        InboxTests.setUp(self)
        # Production writes 1000:1000; tests own the file as whatever user runs them.
        for name, value in (("AI_KEY_UID", os.getuid()), ("AI_KEY_GID", os.getgid())):
            patcher = unittest.mock.patch(f"controller.{name}", value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.workshop_data = self.dir / "workshop-data"
        self.workshop_data.mkdir()
        self.controller.workshop_data_dir = self.workshop_data
        self.key = "sk-or-v1-" + "Ab12" * 10
        self.other_key = "sk-or-v1-" + "Zz99" * 10

    def process(self, **kwargs):
        with contextlib.redirect_stderr(self.log):
            return self.controller.process_inbox(**kwargs)

    result = InboxTests.result
    read = InboxTests.read

    def put(self, request_id=None, **fields):
        request_id = request_id or str(uuid4())
        document = {
            "schema": 1,
            "id": request_id,
            "kind": "ai-key",
            "actor": {"draslId": DRASL, "name": "Nether"},
            "createdAt": int(time.time() * 1000),
            **fields,
        }
        path = self.inbox / f"{request_id}.json"
        path.write_text(json.dumps(document))
        return request_id

    def key_file(self):
        return self.workshop_data / "blockfield-workshop/openrouter.key"

    def test_set_replace_delete_and_status(self):
        request_id = self.put(key=self.key)
        self.assertEqual(self.process(), 1)
        result = self.result(request_id)
        self.assertEqual(
            (result["ok"], result["error"], result["kind"], result["set"]),
            (True, None, "ai-key", True),
        )
        self.assertEqual(
            (result["last4"], result["sha256Prefix"]),
            (self.key[-4:], hashlib.sha256(self.key.encode()).hexdigest()[:8]),
        )
        self.assertEqual(
            result["payload"],
            {
                "kind": "ai-key",
                "set": True,
                "last4": self.key[-4:],
                "sha256Prefix": hashlib.sha256(self.key.encode()).hexdigest()[:8],
                "appliedAt": result["appliedAt"],
            },
        )
        self.assertIs(type(result["appliedAt"]), int)
        key_path = self.key_file()
        self.assertEqual(key_path.read_text(), self.key)
        info = key_path.stat()
        self.assertEqual(
            (
                stat.S_IMODE(info.st_mode),
                info.st_uid,
                info.st_gid,
                info.st_nlink,
            ),
            (0o600, os.getuid(), os.getgid(), 1),
        )
        status = json.loads((self.admin / "ai-key.json").read_text())
        self.assertEqual(
            status,
            {
                "schema": 1,
                "set": True,
                "last4": self.key[-4:],
                "sha256Prefix": hashlib.sha256(self.key.encode()).hexdigest()[:8],
                "appliedAt": result["appliedAt"],
            },
        )
        self.assertEqual(os.listdir(self.inbox), [])
        self.assertIn(
            f"inbox {request_id} kind='ai-key' actor=Nether ok", self.log.getvalue()
        )

        replace = self.put(key=self.other_key)
        self.process()
        self.assertEqual(self.key_file().read_text(), self.other_key)
        self.assertTrue(self.result(replace)["set"])

        remove = self.put(key=None)
        self.process()
        result = self.result(remove)
        self.assertEqual(
            (result["ok"], result["set"], result["last4"], result["sha256Prefix"]),
            (True, False, "", ""),
        )
        self.assertFalse(key_path.exists())
        status = json.loads((self.admin / "ai-key.json").read_text())
        self.assertEqual(status["set"], False)
        # Removing an absent key is still a successful no-op.
        self.process()
        again = self.put(key=None)
        self.process()
        self.assertTrue(self.result(again)["ok"])
        self.assertFalse(key_path.exists())

    def test_invalid_keys_are_refused_generically(self):
        for label, fields in (
            ("too short", {"key": "x" * 19}),
            ("too long", {"key": "x" * 2049}),
            ("space", {"key": "sk-or-v1-abc def-ghi-jkl-mno"}),
            ("newline", {"key": "sk-or-v1-abc\n" + "x" * 20}),
            ("non-ascii", {"key": "sk-or-v1-ключ-" + "x" * 20}),
            ("empty", {"key": ""}),
            ("non-string", {"key": 123}),
            ("missing", {}),
        ):
            with self.subTest(label):
                request_id = self.put(**fields)
                self.process()
                result = self.result(request_id)
                self.assertEqual(
                    (result["ok"], result["error"]), (False, "invalid ai-key request")
                )
                self.assertNotIn("kind", result)
        self.assertFalse(self.key_file().exists())
        self.assertFalse((self.admin / "ai-key.json").exists())

    def test_symlinks_on_the_key_path_are_refused(self):
        target = self.dir / "outside.txt"
        target.write_text("untouched")
        directory = self.workshop_data / "blockfield-workshop"
        directory.mkdir()
        cases = []
        link = directory / "openrouter.key"
        link.symlink_to(target)
        cases.append(self.put(key=self.key))
        self.process()
        self.assertEqual(self.result(cases[-1])["error"], "unsafe ai-key path")
        link.unlink()
        linked = self.dir / "linked-dir"
        linked.mkdir()
        directory.rmdir()
        directory.symlink_to(linked, target_is_directory=True)
        cases.append(self.put(key=self.key))
        self.process()
        self.assertEqual(self.result(cases[-1])["error"], "unsafe ai-key path")
        self.assertEqual(target.read_text(), "untouched")
        self.assertEqual(list(linked.iterdir()), [])
        self.assertFalse(self.key_file().exists())
        self.assertFalse((self.admin / "ai-key.json").exists())

    def test_existing_directory_must_be_a_plain_owned_directory(self):
        directory = self.workshop_data / "blockfield-workshop"
        directory.mkdir()
        real_stat = os.stat

        class FakeStat:
            def __init__(self, values):
                self.st_mode, self.st_uid, self.st_gid, self.st_ino = values

        def patched(path, *args, **kwargs):
            info = real_stat(path, *args, **kwargs)
            if Path(path) == directory:
                return FakeStat((info.st_mode, 4242, 4242, info.st_ino))
            return info

        with unittest.mock.patch("os.stat", side_effect=patched):
            request_id = self.put(key=self.key)
            self.process()
            self.assertEqual(
                self.result(request_id)["error"], "unsafe ai-key directory"
            )
        (directory / "marker.txt").write_text("x")
        for child in directory.iterdir():
            child.unlink()
        directory.rmdir()
        directory.write_text("not a directory")
        request_id = self.put(key=self.key)
        self.process()
        self.assertEqual(self.result(request_id)["error"], "unsafe ai-key directory")
        self.assertFalse((self.admin / "ai-key.json").exists())

    def test_key_file_gets_exact_owner_and_mode(self):
        with (
            unittest.mock.patch("os.chown") as chown,
            unittest.mock.patch("os.fchown") as fchown,
        ):
            request_id = self.put(key=self.key)
            self.process()
            self.assertTrue(self.result(request_id)["ok"])
        directory = self.workshop_data / "blockfield-workshop"
        chown.assert_called_once_with(directory, os.getuid(), os.getgid())
        self.assertEqual(fchown.call_args[0][1:], (os.getuid(), os.getgid()))
        self.assertEqual((PRODUCTION_KEY_UID, PRODUCTION_KEY_GID), (1000, 1000))
        self.assertEqual(self.key_file().read_text(), self.key)

    def test_failed_key_write_leaves_no_partial_key(self):
        real_fsync = os.fsync

        def full_disk(fd):
            if "openrouter.key" in os.readlink(f"/proc/self/fd/{fd}"):
                raise OSError(28, "No space left on device")
            return real_fsync(fd)

        with unittest.mock.patch("os.fsync", side_effect=full_disk):
            request_id = self.put(key=self.key)
            self.process()
        self.assertFalse(self.result(request_id)["ok"])
        self.assertEqual(
            list((self.workshop_data / "blockfield-workshop").iterdir()), []
        )

    def test_idempotent_replay_keeps_first_result(self):
        request_id = self.put(key=self.key)
        self.process()
        done = self.result(request_id)
        (self.inbox / f"{request_id}.json").write_text(
            json.dumps(
                {
                    "schema": 1,
                    "id": request_id,
                    "kind": "ai-key",
                    "actor": {"draslId": DRASL, "name": "Nether"},
                    "createdAt": int(time.time() * 1000),
                    "key": self.other_key,
                }
            )
        )
        self.assertEqual(self.process(), 0)
        self.assertEqual(self.result(request_id), done)
        self.assertEqual(self.key_file().read_text(), self.key)
        self.assertEqual(os.listdir(self.inbox), [])

    def test_key_never_appears_outside_its_file(self):
        second = "sk-or-v1-" + "Qq77" * 10
        self.put(key=self.key)
        self.put(key=second)
        self.put(key="way-too-short")
        self.process()
        self.process()
        log = self.log.getvalue()
        self.assertNotIn(self.key, log)
        self.assertNotIn(second, log)
        for path in self.dir.rglob("*"):
            if path.is_file() and path != self.key_file():
                with self.subTest(path=path):
                    data = path.read_bytes()
                    self.assertNotIn(self.key.encode(), data)
                    self.assertNotIn(second.encode(), data)
        state = json.loads((self.dir / "state" / "state.json").read_text())
        self.assertNotIn(self.key, json.dumps(state))


class AiGatewayKeyInboxTests(unittest.TestCase):
    """The gateway key mirrors the OpenRouter flow into gateway.key."""

    def setUp(self):
        InboxTests.setUp(self)
        # Production writes 1000:1000; tests own the file as whatever user runs them.
        for name, value in (("AI_KEY_UID", os.getuid()), ("AI_KEY_GID", os.getgid())):
            patcher = unittest.mock.patch(f"controller.{name}", value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.workshop_data = self.dir / "workshop-data"
        self.workshop_data.mkdir()
        self.controller.workshop_data_dir = self.workshop_data
        self.key = "sk-or-v1-" + "Ab12" * 10
        self.other_key = "sk-or-v1-" + "Zz99" * 10

    def process(self, **kwargs):
        with contextlib.redirect_stderr(self.log):
            return self.controller.process_inbox(**kwargs)

    result = InboxTests.result
    read = InboxTests.read

    def put(self, request_id=None, **fields):
        request_id = request_id or str(uuid4())
        document = {
            "schema": 1,
            "id": request_id,
            "kind": "ai-gateway-key",
            "actor": {"draslId": DRASL, "name": "Nether"},
            "createdAt": int(time.time() * 1000),
            **fields,
        }
        path = self.inbox / f"{request_id}.json"
        path.write_text(json.dumps(document))
        return request_id

    def key_file(self):
        return self.workshop_data / "blockfield-workshop/gateway.key"

    def test_set_replace_delete_and_status(self):
        request_id = self.put(key=self.key)
        self.assertEqual(self.process(), 1)
        result = self.result(request_id)
        self.assertEqual(
            (result["ok"], result["error"], result["kind"], result["set"]),
            (True, None, "ai-gateway-key", True),
        )
        self.assertEqual(
            (
                result["last4"],
                result["sha256Prefix"],
                result["payload"],
            ),
            (
                self.key[-4:],
                hashlib.sha256(self.key.encode()).hexdigest()[:8],
                {
                    "kind": "ai-gateway-key",
                    "set": True,
                    "last4": self.key[-4:],
                    "sha256Prefix": hashlib.sha256(self.key.encode()).hexdigest()[:8],
                    "appliedAt": result["appliedAt"],
                },
            ),
        )
        self.assertIs(type(result["appliedAt"]), int)
        key_path = self.key_file()
        self.assertEqual(key_path.read_text(), self.key)
        info = key_path.stat()
        self.assertEqual(
            (
                stat.S_IMODE(info.st_mode),
                info.st_uid,
                info.st_gid,
                info.st_nlink,
            ),
            (0o600, os.getuid(), os.getgid(), 1),
        )
        status = json.loads((self.admin / "ai-gateway-key.json").read_text())
        self.assertEqual(status["set"], True)
        self.assertEqual(status["schema"], 1)
        self.assertEqual(os.listdir(self.inbox), [])
        self.assertIn(
            f"inbox {request_id} kind='ai-gateway-key' actor=Nether ok",
            self.log.getvalue(),
        )

        replace = self.put(key=self.other_key)
        self.process()
        self.assertEqual(self.key_file().read_text(), self.other_key)
        self.assertTrue(self.result(replace)["set"])

        remove = self.put(key=None)
        self.process()
        result = self.result(remove)
        self.assertEqual(
            (result["ok"], result["set"], result["last4"], result["sha256Prefix"]),
            (True, False, "", ""),
        )
        self.assertFalse(key_path.exists())
        status = json.loads((self.admin / "ai-gateway-key.json").read_text())
        self.assertEqual(status["set"], False)
        # Removing an absent key is still a successful no-op.
        self.process()

    def test_invalid_keys_are_refused_generically(self):
        for label, fields in (
            ("too short", {"key": "x" * 19}),
            ("missing", {}),
        ):
            with self.subTest(label):
                request_id = self.put(**fields)
                self.process()
                result = self.result(request_id)
                self.assertEqual(
                    (result["ok"], result["error"]),
                    (False, "invalid ai-gateway-key request"),
                )
                self.assertNotIn("kind", result)
        self.assertFalse(self.key_file().exists())
        self.assertFalse((self.admin / "ai-gateway-key.json").exists())

    def test_key_never_appears_outside_its_file(self):
        self.put(key=self.key)
        self.put(key=self.other_key)
        self.process()
        self.process()
        log = self.log.getvalue()
        self.assertNotIn(self.key, log)
        self.assertNotIn(self.other_key, log)
        for path in self.dir.rglob("*"):
            if path.is_file() and path != self.key_file():
                with self.subTest(path=path):
                    data = path.read_bytes()
                    self.assertNotIn(self.key.encode(), data)
                    self.assertNotIn(self.other_key.encode(), data)


class AiProviderKeyInboxTests(unittest.TestCase):
    """Generic provider key: any registry id; presets keep their file/status names."""

    CUSTOM = "acme-stt"

    def setUp(self):
        InboxTests.setUp(self)
        # Production writes 1000:1000; tests own the file as whatever user runs them.
        for name, value in (("AI_KEY_UID", os.getuid()), ("AI_KEY_GID", os.getgid())):
            patcher = unittest.mock.patch(f"controller.{name}", value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.workshop_data = self.dir / "workshop-data"
        self.workshop_data.mkdir()
        self.controller.workshop_data_dir = self.workshop_data
        self.key = "sk-or-v1-" + "Ab12" * 10
        self.other_key = "sk-or-v1-" + "Zz99" * 10

    def process(self, **kwargs):
        with contextlib.redirect_stderr(self.log):
            return self.controller.process_inbox(**kwargs)

    result = InboxTests.result
    read = InboxTests.read

    def put(self, request_id=None, **fields):
        request_id = request_id or str(uuid4())
        document = {
            "schema": 1,
            "id": request_id,
            "kind": "ai-provider-key",
            "actor": {"draslId": DRASL, "name": "Nether"},
            "createdAt": int(time.time() * 1000),
            **fields,
        }
        path = self.inbox / f"{request_id}.json"
        path.write_text(json.dumps(document))
        return request_id

    def key_file(self, provider=CUSTOM):
        return self.workshop_data / f"blockfield-workshop/{provider}.key"

    def test_set_replace_delete_custom_provider(self):
        request_id = self.put(provider=self.CUSTOM, key=self.key)
        self.assertEqual(self.process(), 1)
        result = self.result(request_id)
        self.assertEqual(
            (result["ok"], result["error"], result["kind"], result["set"]),
            (True, None, "ai-provider-key", True),
        )
        self.assertEqual(
            (result["last4"], result["sha256Prefix"]),
            (self.key[-4:], hashlib.sha256(self.key.encode()).hexdigest()[:8]),
        )
        key_path = self.key_file()
        self.assertEqual(key_path.read_text(), self.key)
        info = key_path.stat()
        self.assertEqual(
            (
                stat.S_IMODE(info.st_mode),
                info.st_uid,
                info.st_gid,
                info.st_nlink,
            ),
            (0o600, os.getuid(), os.getgid(), 1),
        )
        status = json.loads((self.admin / f"{self.CUSTOM}-key.json").read_text())
        self.assertEqual(status["set"], True)
        self.assertEqual(status["schema"], 1)

        replace = self.put(provider=self.CUSTOM, key=self.other_key)
        self.process()
        self.assertEqual(self.key_file().read_text(), self.other_key)
        self.assertTrue(self.result(replace)["set"])

        remove = self.put(provider=self.CUSTOM, key=None)
        self.process()
        result = self.result(remove)
        self.assertEqual(
            (result["ok"], result["set"], result["last4"], result["sha256Prefix"]),
            (True, False, "", ""),
        )
        self.assertFalse(key_path.exists())
        status = json.loads((self.admin / f"{self.CUSTOM}-key.json").read_text())
        self.assertEqual(status["set"], False)

    def test_preset_ids_keep_their_file_and_status_names(self):
        request_id = self.put(provider="openrouter", key=self.key)
        self.assertEqual(self.process(), 1)
        self.assertTrue(self.result(request_id)["ok"])
        self.assertEqual(
            (self.workshop_data / "blockfield-workshop/openrouter.key").read_text(),
            self.key,
        )
        self.assertTrue((self.admin / "ai-key.json").exists())
        gateway = self.put(provider="vercel-gateway", key=self.other_key)
        self.process()
        self.assertTrue(self.result(gateway)["ok"])
        self.assertEqual(
            (self.workshop_data / "blockfield-workshop/gateway.key").read_text(),
            self.other_key,
        )
        self.assertTrue((self.admin / "ai-gateway-key.json").exists())

    def test_invalid_provider_ids_are_refused_generically(self):
        for label, fields in (
            ("uppercase", {"provider": "OpenRouter", "key": self.key}),
            ("slash", {"provider": "a/b", "key": self.key}),
            ("dotdot", {"provider": "..", "key": self.key}),
            ("empty", {"provider": "", "key": self.key}),
            ("too long", {"provider": "a" * 33, "key": self.key}),
            ("non-string", {"provider": 7, "key": self.key}),
            ("missing", {"key": self.key}),
            ("reserved key stem", {"provider": "gateway", "key": self.key}),
            ("reserved sidecar key", {"provider": "ai-sidecar", "key": self.key}),
            ("reserved status stem", {"provider": "ai", "key": self.key}),
            ("bad key", {"provider": self.CUSTOM, "key": "x" * 19}),
        ):
            with self.subTest(label):
                request_id = self.put(**fields)
                self.process()
                result = self.result(request_id)
                self.assertEqual(
                    (result["ok"], result["error"]),
                    (False, "invalid ai-provider-key request"),
                )
                self.assertNotIn("kind", result)
        self.assertFalse(self.key_file().exists())
        self.assertFalse((self.admin / f"{self.CUSTOM}-key.json").exists())

    def test_key_never_appears_outside_its_file(self):
        self.put(provider=self.CUSTOM, key=self.key)
        self.put(provider=self.CUSTOM, key=self.other_key)
        self.process()
        self.process()
        log = self.log.getvalue()
        self.assertNotIn(self.key, log)
        self.assertNotIn(self.other_key, log)
        for path in self.dir.rglob("*"):
            if path.is_file() and path != self.key_file():
                with self.subTest(path=path):
                    data = path.read_bytes()
                    self.assertNotIn(self.key.encode(), data)
                    self.assertNotIn(self.other_key.encode(), data)


if __name__ == "__main__":
    unittest.main()


class WorkshopKeyTests(unittest.TestCase):
    def test_failed_write_leaves_no_partial_key(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "workshop.key"
            with (
                unittest.mock.patch(
                    "os.write", side_effect=OSError(28, "No space left")
                ),
                self.assertRaises(OSError),
            ):
                ensure_workshop_key(path)
            self.assertEqual(list(Path(directory).iterdir()), [])


class SessionAndPlaytestTests(unittest.TestCase):
    setUp = InboxTests.setUp
    process, put, result = InboxTests.process, InboxTests.put, InboxTests.result

    def enter_workshop(self, **fields):
        request_id = self.put(
            "mode",
            profile="workshop",
            expectedRevision=self.controller.status()["revision"],
            **fields,
        )
        self.process()
        self.assertTrue(self.result(request_id)["ok"], self.result(request_id))
        self.controller.step()
        self.backend.ready(Goal(**self.controller.status()["desired"]))
        self.controller.step()
        return json.loads((self.admin / "session.json").read_text())

    def test_new_session_offers_chosen_maps_and_resume_keeps_it(self):
        first = self.enter_workshop(session="new", maps=["dust2", "mirage"])
        self.assertEqual(
            (first["maps"], first["startedBy"]["name"]), (["dust2", "mirage"], "Nether")
        )
        self.assertEqual(self.controller.status()["desired"]["session"], first["id"])
        back = self.put(
            "mode",
            profile="game",
            expectedRevision=self.controller.status()["revision"],
        )
        self.process()
        self.assertTrue(self.result(back)["ok"])
        self.controller.step()
        resumed = self.enter_workshop(session="resume")
        self.assertEqual(
            (resumed["id"], resumed["maps"]), (first["id"], ["dust2", "mirage"])
        )

    def test_session_maps_are_validated(self):
        previous = (self.admin / "session.json").read_bytes()
        for fields in (
            {"maps": ["atlantis"]},
            {"maps": []},
            {"maps": ["dust2", "dust2"]},
            {"session": "later"},
        ):
            request_id = self.put(
                "mode", profile="workshop", expectedRevision=1, **fields
            )
            self.process()
            self.assertFalse(self.result(request_id)["ok"], fields)
        self.assertEqual((self.admin / "session.json").read_bytes(), previous)

    def test_playtest_only_from_the_workshop_and_gate_testers(self):
        sha = "ab" * 32
        self.controller.set_members([ADMIN])
        self.controller.maps = unittest.mock.Mock(
            sha256_of=lambda candidate: sha, map_ids=lambda: {"konstaxa", "fields"}
        )
        state = self.controller.status()
        state.update(phase="STARTING", gate="closed")
        self.controller._write(state)
        early = self.put(
            "mode", profile="test", expectedRevision=1, candidateId="c" * 32
        )
        self.process()
        self.assertIn("only from the running workshop", self.result(early)["error"])
        self.controller.step()
        session = self.enter_workshop()
        test = self.put(
            "mode",
            profile="test",
            expectedRevision=self.controller.status()["revision"],
            candidateId="c" * 32,
        )
        self.process()
        self.assertTrue(self.result(test)["ok"], self.result(test))
        desired = self.controller.status()["desired"]
        self.assertEqual(
            (desired["profile"], desired["session"], desired["candidate"]),
            ("test", session["id"], sha),
        )
        self.controller.step()
        self.backend.ready(Goal(**desired))
        self.assertEqual(self.controller.step()["gate"], "testers")
        gate = json.loads((self.dir / "gate.json").read_text())
        self.assertEqual(
            (
                gate["backends"]["workshop"]["gate"],
                "workshop",
                [m["name"] for m in gate["backends"]["workshop"]["allowed"]],
            ),
            ("testers", "workshop", ["Nether"]),
        )
        self.assertEqual(
            json.loads((self.dir / "public.json").read_text())["workshop"]["mode"],
            "workshop",
        )
