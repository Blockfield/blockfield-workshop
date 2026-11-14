"""CP operations: results, status/members export and persisted maintenance message."""

from release_fixtures import catalog

import contextlib
import hashlib
import io
import json
import os
import stat
import tempfile
import unittest
import unittest.mock
from pathlib import Path
from uuid import uuid4
import cp_fixtures
from controller import Controller, DEFAULT_MESSAGE, Goal, Process
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
        self.queue = []
        self.admin = self.dir / "admin"
        self.backend = FakeBackend()
        self.controller = Controller(
            self.dir / "state",
            self.backend,
            gate_file=self.dir / "gate.json",
            public_file=self.dir / "public.json",
            admin_dir=self.admin,
            releases=catalog(),
        )
        self.controller.state_reporter = cp_fixtures.Reporter()
        self.controller.set_members([ADMIN])
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
            releases=catalog(),
        )
        self.assertEqual(key.read_text(), text, "never rotated by a restart")

    def process(self):
        """Apply the queued operations in order, like one CP sync pass."""
        done = 0
        with contextlib.redirect_stderr(self.log):
            while self.queue:
                request_id, kind, fields = self.queue.pop(0)
                cp_fixtures.apply(
                    self.controller, kind, ADMIN, operation_id=request_id, **fields
                )
                done += 1
        return done

    def put(self, kind, **fields):
        request_id = str(uuid4())
        self.queue.append((request_id, kind, fields))
        return request_id

    def result(self, request_id):
        return self.controller.state_reporter.docs[f"result:{request_id}"]

    def status(self):
        self.controller.flush_reports()
        return self.controller.state_reporter.docs["status"]

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
        state = self.controller.status()
        self.assertEqual(
            (
                state["operation"],
                state["desired"]["profile"],
                state["desired"]["release"],
            ),
            (request_id, "workshop", "v1.0.0"),
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
        status = self.status()
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
            self.status()["message"],
            DEFAULT_MESSAGE,
        )

    def test_status_report_content(self):
        status = self.status()
        state = self.controller.status()
        self.assertEqual(
            {k: v for k, v in status.items() if k != "since"},
            {
                "schema": 1,
                "revision": 1,
                "operation": state["operation"],
                "selection": {
                    key: state["selection"][key]
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
                "approvedTarget": state["approvedTarget"],
            },
        )
        # Unchanged documents are not sent again.
        sent = []
        self.controller.state_reporter.put = lambda name, value: sent.append(name)
        for _ in range(3):
            self.controller.step()
            self.controller.flush_reports()
        self.assertEqual(sent, [])
        self.assertFalse((self.admin / "status.json").exists())

    def test_members_write_reports_lost_workshop_readiness_without_closing_game(self):
        goal = Goal(**self.controller.status()["desired"])
        self.backend.processes[-1] = Process(goal, False)
        self.controller.set_members([ADMIN])
        status = self.status()
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

    process = InboxTests.process
    result = InboxTests.result
    read = InboxTests.read

    def put(self, request_id=None, **fields):
        request_id = request_id or str(uuid4())
        self.queue.append((request_id, "ai-key", fields))
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
        status = self.controller.ai_key_statuses()["ai-key.json"]
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
        status = self.controller.ai_key_statuses()["ai-key.json"]
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
        self.assertNotIn("ai-key.json", self.controller.ai_key_statuses())

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
        self.assertEqual(self.result(cases[-1])["error"], "unsafe ai-key directory")
        self.assertEqual(target.read_text(), "untouched")
        self.assertEqual(list(linked.iterdir()), [])
        self.assertFalse(self.key_file().exists())
        self.assertNotIn("ai-key.json", self.controller.ai_key_statuses())

    def test_existing_directory_must_be_a_plain_owned_directory(self):
        directory = self.workshop_data / "blockfield-workshop"
        directory.mkdir()
        real_fstat = os.fstat

        class FakeStat:
            def __init__(self, values):
                self.st_mode, self.st_uid, self.st_gid, self.st_ino = values

        def patched(fd, *args, **kwargs):
            info = real_fstat(fd, *args, **kwargs)
            try:
                resolved = os.readlink(f"/proc/self/fd/{fd}")
            except OSError:
                return info
            if Path(resolved) == directory:
                return FakeStat((info.st_mode, 4242, 4242, info.st_ino))
            return info

        with unittest.mock.patch("os.fstat", side_effect=patched):
            request_id = self.put(key=self.key)
            self.process()
            self.assertTrue(
                self.result(request_id)["error"].startswith("unsafe ai-key directory")
            )
        (directory / "marker.txt").write_text("x")
        for child in directory.iterdir():
            child.unlink()
        directory.rmdir()
        directory.write_text("not a directory")
        request_id = self.put(key=self.key)
        self.process()
        self.assertEqual(self.result(request_id)["error"], "unsafe ai-key directory")
        self.assertNotIn("ai-key.json", self.controller.ai_key_statuses())

    def test_key_file_gets_exact_owner_and_mode(self):
        # No privileged repair: the controller never changes ownership, so
        # any chown attempt fails the test; the caller-owned file is 0600.
        def no_privileged_repair(*args, **kwargs):
            raise AssertionError("controller must not change ownership")

        with (
            unittest.mock.patch("os.chown", side_effect=no_privileged_repair),
            unittest.mock.patch("os.fchown", side_effect=no_privileged_repair),
        ):
            request_id = self.put(key=self.key)
            self.process()
            self.assertTrue(self.result(request_id)["ok"])
        self.assertEqual((PRODUCTION_KEY_UID, PRODUCTION_KEY_GID), (1000, 1000))
        info = self.key_file().stat()
        self.assertEqual(
            (stat.S_IMODE(info.st_mode), info.st_uid, info.st_gid),
            (0o600, os.getuid(), os.getgid()),
        )
        self.assertEqual(self.key_file().read_text(), self.key)

    def test_failed_key_write_leaves_no_partial_key(self):
        real_fsync = os.fsync

        def full_disk(fd):
            # The secret is staged unnamed (O_TMPFILE): its /proc/self/fd
            # link carries "(deleted)" until linked. Failing fsync there must
            # leave no published key and no staged remnant behind.
            if "(deleted)" in os.readlink(f"/proc/self/fd/{fd}"):
                raise OSError(28, "No space left on device")
            return real_fsync(fd)

        with unittest.mock.patch("os.fsync", side_effect=full_disk):
            request_id = self.put(key=self.key)
            self.process()
        self.assertFalse(self.result(request_id)["ok"])
        self.assertEqual(
            list((self.workshop_data / "blockfield-workshop").iterdir()), []
        )

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

    process = InboxTests.process
    result = InboxTests.result
    read = InboxTests.read

    def put(self, request_id=None, **fields):
        request_id = request_id or str(uuid4())
        self.queue.append((request_id, "ai-gateway-key", fields))
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
        status = self.controller.ai_key_statuses()["ai-gateway-key.json"]
        self.assertEqual(status["set"], True)
        self.assertEqual(status["schema"], 1)

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
        status = self.controller.ai_key_statuses()["ai-gateway-key.json"]
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
        self.assertNotIn("ai-gateway-key.json", self.controller.ai_key_statuses())

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

    process = InboxTests.process
    result = InboxTests.result
    read = InboxTests.read

    def put(self, request_id=None, **fields):
        request_id = request_id or str(uuid4())
        self.queue.append((request_id, "ai-provider-key", fields))
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
        status = self.controller.ai_key_statuses()[f"{self.CUSTOM}-key.json"]
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
        status = self.controller.ai_key_statuses()[f"{self.CUSTOM}-key.json"]
        self.assertEqual(status["set"], False)

    def test_preset_ids_keep_their_file_and_status_names(self):
        request_id = self.put(provider="openrouter", key=self.key)
        self.assertEqual(self.process(), 1)
        self.assertTrue(self.result(request_id)["ok"])
        self.assertEqual(
            (self.workshop_data / "blockfield-workshop/openrouter.key").read_text(),
            self.key,
        )
        self.assertIn("ai-key.json", self.controller.ai_key_statuses())
        gateway = self.put(provider="vercel-gateway", key=self.other_key)
        self.process()
        self.assertTrue(self.result(gateway)["ok"])
        self.assertEqual(
            (self.workshop_data / "blockfield-workshop/gateway.key").read_text(),
            self.other_key,
        )
        self.assertIn("ai-gateway-key.json", self.controller.ai_key_statuses())

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
        self.assertNotIn(f"{self.CUSTOM}-key.json", self.controller.ai_key_statuses())

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
