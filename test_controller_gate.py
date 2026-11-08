"""Gate/public export, no-flap reconciliation and explicit ERROR recovery toward game."""

from release_fixtures import catalog

import hashlib
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock
from uuid import uuid4
from controller import MAP_IDS, Controller, DEFAULT_MESSAGE, Goal, Process, member
from test_controller import FakeBackend

DRASL = "e10438e4797b49718cfecec6a8fa171b"


class GateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)
        self.backend = FakeBackend()
        self.controller = Controller(
            self.dir / "state",
            self.backend,
            gate_file=self.dir / "gate.json",
            public_file=self.dir / "public.json",
            releases=catalog(),
        )
        self.goal = Goal("workshop", "v1.0.0", str(uuid4()))

    def gate(self, backend="workshop"):
        document = json.loads((self.dir / "gate.json").read_text())
        return {
            "schema": document["schema"],
            "revision": document["revision"],
            "backend": backend,
            **document["backends"][backend],
        }

    def public(self, backend="workshop"):
        document = json.loads((self.dir / "public.json").read_text())
        if backend == "workshop":
            return {**document["workshop"], "since": document["since"]}
        return {k: document[k] for k in ("mode", "message", "since")}

    def stamps(self):
        return [
            os.stat(p).st_ino
            for p in (
                self.controller.file,
                self.dir / "gate.json",
                self.dir / "public.json",
            )
        ]

    def to_workshop(self):
        self.controller.request(
            str(uuid4()), self.goal, self.controller.status()["revision"]
        )
        self.controller.step()
        self.backend.ready(self.goal)
        return self.controller.step()

    def test_concurrent_package_publish_never_shares_a_temp(self):
        admin = self.dir / "admin"
        admin.mkdir()
        packages = admin / "packages"
        packages.mkdir()
        data = b"P" * 4096
        source = self.dir / ("c" * 32 + ".zip")
        source.write_bytes(data)
        digest = hashlib.sha256(data).hexdigest()
        meta = {"id": "c" * 32, "status": "accepted", "native": False, "sha256": digest}

        class StubMaps:
            canonical_sessions = False

            def __init__(self, packages):
                self.packages = packages

            def retire_merged_overlays(self):
                return []

            def list(self):
                return [dict(meta)]

            def archive_path(self, cid):
                return source

            def export(self):
                return []

            def sessions(self):
                return {"sessions": [], "total": 0, "next": None}

            def catalog(self):
                return []

        first_done = threading.Event()
        gate = threading.Lock()
        calls = {"n": 0}
        real_digest = hashlib.file_digest

        def gated_digest(handle, *args, **kwargs):
            with gate:
                calls["n"] += 1
                second = calls["n"] > 1
            if second:
                # The other run already replaced and unlinked the shared temp
                # (old code): this read must not race a missing file.
                self.assertTrue(first_done.wait(30))
            return real_digest(handle, *args, **kwargs)

        errors = []

        def run(controller):
            try:
                controller.export_maps()
            except Exception as error:  # noqa: BLE001 - collected across threads
                errors.append(error)
            finally:
                first_done.set()

        controllers = [
            Controller(
                self.dir / f"state{i}",
                FakeBackend(),
                admin_dir=admin,
                maps=StubMaps(packages),
                releases=catalog(),
            )
            for i in (1, 2)
        ]
        with mock.patch("hashlib.file_digest", gated_digest):
            threads = [
                threading.Thread(target=run, args=(controller,))
                for controller in controllers
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(60)
            self.assertFalse(
                [thread for thread in threads if thread.is_alive()],
                "package publish threads hung",
            )
        self.assertEqual(errors, [])
        self.assertEqual((packages / (digest + ".zip")).read_bytes(), data)
        self.assertEqual(
            [path for path in packages.iterdir() if path.suffix == ".tmp"], []
        )

    def test_game_adoption_exports_public(self):
        self.controller.request(str(uuid4()), Goal("game", "v1.0.0"), 0)
        self.assertEqual(self.controller.step()["gate"], "closed")
        self.assertEqual(
            self.gate("game"),
            {
                "schema": 1,
                "revision": 1,
                "gate": "public",
                "backend": "game",
                "message": "",
                "allowed": [],
            },
        )
        self.assertEqual(self.public("game")["mode"], "game")
        self.assertEqual(self.public("game")["message"], "")
        legacy = json.loads((self.dir / "gate.json").read_bytes())
        self.assertEqual(
            (legacy["gate"], legacy["backend"], legacy["allowed"]),
            ("public", "game", []),
        )
        self.assertNotIn("drain", self.backend.events)

    def test_stable_steps_do_not_rewrite_anything(self):
        self.controller.set_members([{"draslId": DRASL, "name": "Nether"}])
        self.to_workshop()
        before = self.stamps()
        for _ in range(5):
            self.assertEqual(self.controller.step()["gate"], "builders")
        self.assertEqual(self.stamps(), before)

    def test_workshop_start_never_closes_game_entry(self):
        self.controller.request(str(uuid4()), self.goal, 0)
        self.assertEqual(self.controller.step()["phase"], "STARTING")
        before = self.stamps()
        for _ in range(5):
            self.controller.step()
            self.assertEqual(self.gate("game")["gate"], "public")
            # Starting never admits; the proxy shows members the waking message.
            self.assertEqual(self.gate()["gate"], "starting")
            self.assertEqual(self.public("game")["mode"], "game")
        self.assertEqual(self.stamps(), before)

    def test_workshop_gate_carries_acl_and_message(self):
        self.controller.set_members(
            [
                {
                    "draslId": "e10438e4-797b-4971-8cfe-cec6a8fa171b",
                    "name": "Nether",
                    "role": "admin",
                }
            ]
        )
        self.controller.request(str(uuid4()), self.goal, 0)
        self.controller.step()
        starting = self.gate()
        self.assertEqual(
            (starting["gate"], starting["backend"], starting["allowed"]),
            (
                "starting",
                "workshop",
                [{"draslId": DRASL, "name": "Nether", "role": "admin"}],
            ),
        )
        self.assertEqual(starting["message"], DEFAULT_MESSAGE)
        self.backend.ready(self.goal)
        self.controller.step()
        self.assertEqual(
            self.gate()["allowed"],
            [{"draslId": DRASL, "name": "Nether", "role": "admin"}],
        )
        self.assertEqual(
            self.public(),
            {
                "mode": "workshop",
                "message": DEFAULT_MESSAGE,
                "since": self.public()["since"],
            },
        )

    def test_member_changes_revoke_online(self):
        self.controller.set_members([{"draslId": DRASL, "name": "Nether"}])
        self.to_workshop()
        self.controller.set_members([])
        self.assertEqual(self.gate()["allowed"], [])

    def test_invalid_members_admit_nobody(self):
        for bad in (
            {"draslId": "x", "name": "Nether"},
            {"draslId": DRASL, "name": "no spaces"},
            {"draslId": DRASL, "name": "Nether", "role": "op"},
            {"draslId": DRASL, "name": "Nether", "extra": 1},
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                member(bad)
        with self.assertRaises(ValueError):
            self.controller.set_members(
                [
                    {"draslId": DRASL, "name": "Nether"},
                    {"draslId": DRASL, "name": "Other"},
                ]
            )
        self.to_workshop()
        self.controller.members_file.write_text("{corrupt")
        self.controller._write(self.controller.status())
        self.assertEqual(self.gate()["allowed"], [])

    def test_member_maps_validated_and_exported(self):
        builder = {
            "draslId": DRASL,
            "name": "Nether",
            "role": "builder",
            "maps": ["konstaxa", "fields"],
        }
        self.assertEqual(member(builder), builder)
        with self.assertRaises(ValueError):
            member({**builder, "maps": ["nope"]})
        with self.assertRaises(ValueError):
            member({**builder, "maps": ["fields", "fields"]})
        with self.assertRaises(ValueError):
            member({**builder, "maps": "fields"})
        # lead/admin always have all maps: an explicit list is dropped, not rejected.
        self.assertEqual(
            member({**builder, "role": "lead"}),
            {"draslId": DRASL, "name": "Nether", "role": "lead"},
        )
        self.assertNotIn("maps", member({"draslId": DRASL, "name": "Nether"}))
        # The workshop hub is an access scope too (blockfield-mod WorkshopMembers.HUB), not a map file.
        self.assertEqual(
            member({**builder, "maps": ["hub", "fields"]})["maps"], ["hub", "fields"]
        )
        # Explicit [] is not the same as missing: the builder may enter the hub but edit nothing.
        self.assertEqual(member({**builder, "maps": []})["maps"], [])
        self.controller.set_members([builder])
        self.to_workshop()
        self.assertEqual(self.gate()["allowed"], [builder])

    def test_map_ids_match_lobby_maps(self):
        maps = Path(__file__).resolve().with_name("map_ids.json")
        self.assertEqual(MAP_IDS, set(json.loads(maps.read_text())))

    def test_since_survives_unchanged_mode(self):
        self.controller.request(str(uuid4()), self.goal, 0)
        self.controller.step()
        since = self.public()["since"]
        self.controller.step()
        self.assertEqual(self.public()["mode"], "maintenance")
        maintenance_since = self.public()["since"]
        self.assertGreaterEqual(maintenance_since, since)
        self.controller.step()
        self.assertEqual(self.public()["since"], maintenance_since)
        self.assertEqual(self.public("game")["mode"], "game")

    def test_deleted_export_is_restored_by_next_step(self):
        self.to_workshop()
        (self.dir / "gate.json").unlink()
        self.controller.step()
        self.assertEqual(self.gate()["gate"], "builders")

    def test_error_exports_closed(self):
        self.controller.request(str(uuid4()), self.goal, 0)
        self.backend.fail_start = True
        self.assertEqual(self.controller.step()["phase"], "ERROR")
        self.assertEqual(self.gate()["gate"], "closed")
        self.assertEqual(self.public()["mode"], "maintenance")
        self.assertEqual(self.gate("game")["gate"], "public")
        self.assertEqual(self.public("game")["mode"], "game")

    def test_error_without_observed_game_preserves_game_entry(self):
        # Cut-over failure: a container backend never observes the host Game,
        # so an ERROR export must leave the shared Game entry untouched.
        self.backend.processes = []
        self.controller.gate_file.write_text(
            json.dumps(
                {
                    "schema": 1,
                    "revision": 77,
                    "gate": "public",
                    "message": "",
                    "backend": "game",
                    "allowed": [],
                    "backends": {
                        "game": {"gate": "public", "message": "", "allowed": []},
                        "workshop": {
                            "gate": "sleeping",
                            "message": "m",
                            "allowed": [],
                        },
                    },
                }
            )
        )
        self.controller.public_file.write_text(
            json.dumps(
                {
                    "mode": "game",
                    "message": "",
                    "since": 1,
                    "workshop": {"mode": "sleeping", "message": "m"},
                }
            )
        )
        self.controller.request(str(uuid4()), self.goal, 0)
        self.backend.fail_start = True
        self.assertEqual(self.controller.step()["phase"], "ERROR")
        self.assertEqual(self.gate("game")["gate"], "public")
        self.assertEqual(self.public("game")["mode"], "game")

    def test_unobserved_game_preserves_status_game_entry(self):
        # Site admin reads status.json, not the gate: an unobserved Game with
        # a preserved public entry must still report game/public there, or the
        # admin shows "readiness unconfirmed" while a match runs (site#82).
        controller = Controller(
            self.dir / "state2",
            self.backend,
            gate_file=self.dir / "gate.json",
            public_file=self.dir / "public.json",
            admin_dir=self.dir / "admin",
            releases=catalog(),
        )
        self.backend.processes = []
        controller.gate_file.write_text(
            json.dumps(
                {
                    "schema": 1,
                    "revision": 77,
                    "gate": "public",
                    "message": "",
                    "backend": "game",
                    "allowed": [],
                    "backends": {
                        "game": {"gate": "public", "message": "", "allowed": []},
                        "workshop": {"gate": "sleeping", "message": "m", "allowed": []},
                    },
                }
            )
        )
        controller.public_file.write_text(
            json.dumps(
                {
                    "mode": "game",
                    "message": "",
                    "since": 1,
                    "workshop": {"mode": "sleeping", "message": "m"},
                }
            )
        )
        controller.request(str(uuid4()), self.goal, 0)
        self.backend.fail_start = True
        self.assertEqual(controller.step()["phase"], "ERROR")
        status = json.loads((self.dir / "admin" / "status.json").read_text())
        self.assertEqual(
            status["game"], {"phase": "game", "gate": "public", "release": None}
        )
        self.assertEqual(self.gate()["gate"], "closed")

    def test_legacy_sleeping_error_preserves_game_entry(self):
        # Prod rev77 shape: sleeping state with desired but no selection
        # journal; the missing-identity ERROR must not close Game entry.
        self.backend.processes = []
        self.controller.gate_file.write_text(
            json.dumps(
                {
                    "schema": 1,
                    "revision": 77,
                    "gate": "public",
                    "message": "",
                    "backend": "game",
                    "allowed": [],
                    "backends": {
                        "game": {"gate": "public", "message": "", "allowed": []},
                        "workshop": {
                            "gate": "sleeping",
                            "message": "m",
                            "allowed": [],
                        },
                    },
                }
            )
        )
        self.controller.public_file.write_text(
            json.dumps(
                {
                    "mode": "game",
                    "message": "",
                    "since": 1,
                    "workshop": {"mode": "sleeping", "message": "m"},
                }
            )
        )
        self.controller.file.write_text(
            json.dumps(
                {
                    "schema": 1,
                    "phase": "sleeping",
                    "gate": "closed",
                    "operation": str(uuid4()),
                    "revision": 77,
                    "desired": {
                        "profile": "workshop",
                        "release": "v1.0.0",
                        "session": self.goal.session,
                        "candidate": None,
                    },
                    "error": None,
                    "errorMessage": None,
                    "rollback": None,
                }
            )
        )
        with self.assertRaisesRegex(
            ValueError, "missing committed active release identity"
        ):
            self.controller.step()
        self.assertEqual(self.gate("game")["gate"], "public")
        self.assertEqual(self.public("game")["mode"], "game")

    def test_workshop_message_never_replaces_game_unavailable_message(self):
        self.to_workshop()
        self.controller.set_message("Строим новую карту")
        self.backend.processes[0] = Process(Goal("game", "v1.0.0"), False)
        self.controller.step()
        self.assertEqual(self.gate("game")["message"], DEFAULT_MESSAGE)
        self.assertEqual(self.public("game")["message"], DEFAULT_MESSAGE)
        self.assertEqual(self.gate()["message"], "Строим новую карту")
        self.assertEqual(self.gate()["gate"], "builders")

    def test_recover_toward_game_only_and_only_explicitly(self):
        self.to_workshop()
        self.backend.fail_save = True
        self.controller.request(
            str(uuid4()),
            Goal("test", "v1.0.0", self.goal.session, "a" * 64),
            self.controller.status()["revision"],
        )
        self.assertEqual(self.controller.step()["phase"], "ERROR")
        revision = self.controller.status()["revision"]
        with self.assertRaises(RuntimeError):
            self.controller.request(str(uuid4()), Goal("game", "v1.0.0"), revision)
        with self.assertRaises(RuntimeError):
            self.controller.request(str(uuid4()), self.goal, revision, recover=True)
        state = self.controller.request(
            str(uuid4()), Goal("game", "v2.0.0"), revision, recover=True
        )
        self.assertEqual(
            (state["phase"], state["gate"], state["desired"]["release"]),
            ("ERROR", "closed", "v2.0.0"),
        )
        self.backend.fail_save = False
        events = list(self.backend.events)
        self.assertEqual(self.controller.step()["phase"], "ERROR")
        self.assertEqual(self.backend.events, events)
        self.assertEqual(self.controller.step(recover=True)["phase"], "STARTING")
        new = self.backend.events[len(events) :]
        self.assertEqual(new, ["fence", "save", "stop", "start"])
        self.backend.ready(Goal("workshop", "v2.0.0", self.goal.session))
        self.assertEqual(self.controller.step()["gate"], "builders")


class GateReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)
        self.backend = FakeBackend()
        self.controller = Controller(
            self.dir / "state",
            self.backend,
            gate_file=self.dir / "gate.json",
            public_file=self.dir / "public.json",
            releases=catalog(),
        )
        self.goal = Goal("workshop", "v1.0.0", str(uuid4()))

    def test_gate_leg_reported_with_projected_members(self):
        reported = []

        class StubReporter:
            def report(self, leg):
                reported.append(leg)
                return {"reported": True, "revision": 1}

        self.controller.gate_reporter = StubReporter()
        self.controller.set_members(
            [
                {
                    "draslId": DRASL,
                    "name": "Builder1",
                    "role": "builder",
                    "maps": ["hub"],
                }
            ]
        )
        self.controller.request(
            str(uuid4()), self.goal, self.controller.status()["revision"]
        )
        self.controller.step()
        # CP only accepts the projected shape (state/message/plain members).
        self.assertEqual(
            reported[-1],
            {
                "state": "starting",
                "message": self.controller.message,
                "allowed": [{"draslId": DRASL, "name": "Builder1"}],
            },
        )

    def test_failed_report_never_breaks_exports(self):
        class BoomReporter:
            def report(self, leg):
                raise RuntimeError("cp down")

        self.controller.gate_reporter = BoomReporter()
        self.controller.request(
            str(uuid4()), self.goal, self.controller.status()["revision"]
        )
        self.controller.step()
        document = json.loads((self.dir / "gate.json").read_text())
        self.assertEqual(document["backends"]["workshop"]["gate"], "starting")

    def test_slow_report_does_not_hold_lock(self):
        """A stalled gate report must not hold controller.lock: a second
        request commits while the first report is still in flight."""
        calls = []
        entered = threading.Event()
        release = threading.Event()

        class SlowReporter:
            def report(self, leg):
                calls.append(dict(leg))
                if len(calls) == 1:
                    entered.set()
                    assert release.wait(10), "stalled report released too early"
                return {"reported": True, "revision": len(calls)}

        self.controller.gate_reporter = SlowReporter()
        worker = threading.Thread(
            target=self.controller.set_members,
            args=(
                [
                    {
                        "draslId": DRASL,
                        "name": "Builder1",
                        "role": "builder",
                        "maps": ["hub"],
                    }
                ],
            ),
        )
        worker.start()
        try:
            self.assertTrue(entered.wait(10))
            # The members change committed and is now stalled in HTTPS outside
            # the lock: a request still goes through while it is stalled.
            self.controller.request(
                str(uuid4()), self.goal, self.controller.status()["revision"]
            )
            self.assertTrue(worker.is_alive())
            # Serialized by the report lock: exactly the first report is in
            # flight, and the newer leg waits for it instead of racing it.
            self.assertEqual(len(calls), 1)
            wanted = {
                "state": "starting",
                "message": self.controller.message,
                "allowed": [{"draslId": DRASL, "name": "Builder1"}],
            }
        finally:
            release.set()
            worker.join(10)
        self.assertFalse(worker.is_alive())
        # The newest leg is delivered last, so the server converges on it.
        self.assertEqual(calls[-1], wanted)
        self.assertFalse(self.controller.gate_report_pending())
        self.assertFalse((self.dir / "state" / "gate_pending.json").exists())

    def test_local_write_failure_drives_error_and_raises(self):
        """Finding 4507740327 / I1: the local gate is written BEFORE any
        pending staging. When gate.json itself is unwritable during a removal,
        the op is not swallowed: a closed best-effort gate, ERROR state, and
        CommitIncompleteError (never effective). Fail-before: the old code
        stages first and swallows (no raise, no ERROR)."""
        from controller import CommitIncompleteError

        class BoomReporter:
            def report(self, leg):
                raise RuntimeError("cp down")

        self.controller.gate_reporter = BoomReporter()
        admin = {
            "draslId": DRASL,
            "name": "Nether",
            "role": "admin",
            "maps": ["hub"],
        }
        builder = {
            "draslId": "bb" * 16,
            "name": "Builder1",
            "role": "builder",
            "maps": ["hub"],
        }
        self.controller.set_members([admin, builder])
        self.controller.request(
            str(uuid4()), self.goal, self.controller.status()["revision"]
        )
        self.controller.step()
        self.backend.ready(self.goal)
        self.controller.step()
        staged = self.controller._gate_pending_file.read_bytes()
        with mock.patch.object(
            Controller, "_replace_if_changed", side_effect=OSError("ENOSPC")
        ):
            with self.assertRaises(CommitIncompleteError):
                self.controller.set_members([admin])
        # The best-effort closed rewrite used the same failing writer, so no
        # local gate file advanced; the old pending leg is untouched and the
        # workshop parks in ERROR instead of staying open and silent.
        self.assertEqual(self.controller._gate_pending_file.read_bytes(), staged)
        self.assertTrue(self.controller.gate_report_pending())
        self.assertEqual(self.controller.status()["phase"], "ERROR")

    def test_staging_failure_narrows_local_gate_before_raise(self):
        """Finding 4507740327 / I1: removal + pending-write ENOSPC. The local
        gate (gate.json, public.json) is already narrowed before the raise, so
        a staging failure never leaves the wider gate. Fail-before: the old
        stage-first order raises before any local write (gate.json still
        admits the removed builder, and the error is a bare OSError)."""
        from controller import CommitIncompleteError

        class BoomReporter:
            def report(self, leg):
                raise RuntimeError("cp down")

        self.controller.gate_reporter = BoomReporter()
        admin = {
            "draslId": DRASL,
            "name": "Nether",
            "role": "admin",
            "maps": ["hub"],
        }
        builder = {
            "draslId": "bb" * 16,
            "name": "Builder1",
            "role": "builder",
            "maps": ["hub"],
        }
        self.controller.set_members([admin, builder])
        self.controller.request(
            str(uuid4()), self.goal, self.controller.status()["revision"]
        )
        self.controller.step()
        self.backend.ready(self.goal)
        self.controller.step()
        real_atomic_write = Controller._stage_gate_leg.__globals__["atomic_write"]

        def fail_pending(path, *args, **kwargs):
            if Path(path).name == "gate_pending.json":
                raise OSError("ENOSPC")
            return real_atomic_write(path, *args, **kwargs)

        with mock.patch("controller.atomic_write", side_effect=fail_pending):
            with self.assertRaises(CommitIncompleteError):
                self.controller.set_members([admin])
        gate = json.loads((self.dir / "gate.json").read_text())
        allowed = gate["backends"]["workshop"]["allowed"]
        self.assertNotIn("bb" * 16, {entry["draslId"] for entry in allowed})
        public = json.loads((self.dir / "public.json").read_text())
        self.assertEqual(public["workshop"]["mode"], "workshop")

    def test_pending_write_failure_raises_instead_of_ghosting(self):
        """A staging write failure raises CommitIncompleteError (never a bare
        ghost-ACK): the op is held below effective until the leg is staged
        and delivered (finding 4507194521, I1/I3)."""
        from controller import CommitIncompleteError

        class BoomReporter:
            def report(self, leg):
                raise AssertionError("no delivery while staging fails")

        self.controller.gate_reporter = BoomReporter()
        real_atomic_write = Controller._stage_gate_leg.__globals__["atomic_write"]

        def fail_pending(path, *args, **kwargs):
            if Path(path).name == "gate_pending.json":
                raise OSError("ENOSPC")
            return real_atomic_write(path, *args, **kwargs)

        with mock.patch("controller.atomic_write", side_effect=fail_pending):
            with self.assertRaises(CommitIncompleteError):
                self.controller.set_members(
                    [
                        {
                            "draslId": DRASL,
                            "name": "Builder1",
                            "role": "builder",
                            "maps": ["hub"],
                        }
                    ]
                )
        self.assertFalse((self.dir / "state" / "gate_pending.json").exists())
        self.assertFalse(self.controller.gate_report_pending())

    def test_unreadable_state_stages_closed_leg(self):
        """state.json unreadable in set_members: a closed leg is staged so
        the hold keeps the members op from going effective (finding 4507194513)."""

        class BoomReporter:
            def report(self, leg):
                raise RuntimeError("cp down")

        self.controller.gate_reporter = BoomReporter()
        builder = {
            "draslId": DRASL,
            "name": "Builder1",
            "role": "builder",
            "maps": ["hub"],
        }
        self.controller.set_members([builder])
        self.controller.request(
            str(uuid4()), self.goal, self.controller.status()["revision"]
        )
        # Corrupt the state file after a healthy export, then remove a member
        # while CP is down: the export cannot run, but the closed leg holds.
        (self.dir / "state" / "state.json").write_bytes(b"{corrupt")
        self.controller.set_members([])
        self.assertTrue(self.controller.gate_report_pending())
        record = json.loads((self.dir / "state" / "gate_pending.json").read_text())
        self.assertEqual(
            record["leg"],
            {"state": "closed", "message": self.controller.message, "allowed": []},
        )

    def test_unreadable_state_narrows_local_gate_before_staging(self):
        """Edge (a) / I1: members commit live but state.json unreadable. The
        staged closed leg alone is not enough: gate.json/public.json must
        narrow BEFORE staging, or a just-removed member is still admitted
        locally until the next successful export. Fail-before: the old code
        stages without touching the local gate (builder still admitted)."""

        class BoomReporter:
            def report(self, leg):
                raise RuntimeError("cp down")

        self.controller.gate_reporter = BoomReporter()
        admin = {
            "draslId": DRASL,
            "name": "Nether",
            "role": "admin",
            "maps": ["hub"],
        }
        builder = {
            "draslId": "bb" * 16,
            "name": "Builder1",
            "role": "builder",
            "maps": ["hub"],
        }
        self.controller.set_members([admin, builder])
        self.controller.request(
            str(uuid4()), self.goal, self.controller.status()["revision"]
        )
        self.controller.step()
        self.backend.ready(self.goal)
        self.controller.step()
        gate = json.loads((self.dir / "gate.json").read_text())
        self.assertIn(
            "bb" * 16,
            {entry["draslId"] for entry in gate["backends"]["workshop"]["allowed"]},
        )
        # Corrupt the state file, then remove the member while CP is down.
        (self.dir / "state" / "state.json").write_bytes(b"{corrupt")
        self.controller.set_members([admin])
        gate = json.loads((self.dir / "gate.json").read_text())
        self.assertNotIn(
            "bb" * 16,
            {entry["draslId"] for entry in gate["backends"]["workshop"]["allowed"]},
        )
        public = json.loads((self.dir / "public.json").read_text())
        self.assertEqual(public["workshop"]["mode"], "maintenance")
        # The closed leg is still staged (the hold below effective).
        self.assertTrue(self.controller.gate_report_pending())
        record = json.loads((self.dir / "state" / "gate_pending.json").read_text())
        self.assertEqual(
            record["leg"],
            {"state": "closed", "message": self.controller.message, "allowed": []},
        )

    def test_duplicate_request_staging_failure_raises_commit_incomplete(self):
        """Edge (b) / I3: same-op retry re-exports to stage the committed
        selection. A staging OSError there is commit-then-fail (the first
        attempt's selection stands): CommitIncompleteError so sync holds,
        never a bare OSError that sync would store as terminal rejected.
        Fail-before: the old code lets the OSError escape."""
        from controller import CommitIncompleteError

        class BoomReporter:
            def report(self, leg):
                raise RuntimeError("cp down")

        self.controller.gate_reporter = BoomReporter()
        operation = str(uuid4())
        revision = self.controller.status()["revision"]
        real_atomic_write = Controller._stage_gate_leg.__globals__["atomic_write"]

        def fail_pending(path, *args, **kwargs):
            if Path(path).name == "gate_pending.json":
                raise OSError("ENOSPC")
            return real_atomic_write(path, *args, **kwargs)

        with mock.patch("controller.atomic_write", side_effect=fail_pending):
            # First attempt commits the selection, then fails staging.
            with self.assertRaises(CommitIncompleteError):
                self.controller.request(operation, self.goal, revision)
            # Same-op retry re-exports the committed selection: a still-bad
            # staging write must hold too, never escape as a bare OSError.
            with self.assertRaises(CommitIncompleteError):
                self.controller.request(operation, self.goal, revision)

    def test_set_message_read_failure_holds_after_commit(self):
        """Edge (c) / I3: message.json committed but state.json unreadable.
        The op holds (CommitIncompleteError; message re-apply is idempotent)
        instead of escaping as a read ValueError/OSError that sync would
        store as terminal rejected. Fail-before: the old code raises the
        raw read error."""
        from controller import CommitIncompleteError

        self.controller.set_message("hello")
        (self.dir / "state" / "state.json").write_bytes(b"{corrupt")
        with self.assertRaises(CommitIncompleteError):
            self.controller.set_message("again")
        self.assertEqual(self.controller.message, "again")

    def test_delivered_leg_replaces_stale_sidecar(self):
        """leg == delivered.leg with a stale pending record: staging replaces
        it with the delivered leg under gate_state.lock (the stale leg is
        never PATCHed back over what CP holds), and the drain clears it."""

        class StubReporter:
            def report(self, leg):
                reported.append(dict(leg))
                return {"reported": True, "revision": 9}

        reported = []
        self.controller.gate_reporter = StubReporter()
        delivered_leg = {
            "state": "builders",
            "message": "m",
            "allowed": [{"draslId": DRASL, "name": "Nether"}],
        }
        self.controller._gate_delivered_file.write_bytes(
            json.dumps({"seq": 7, "nonce": "n7", "leg": delivered_leg}).encode()
        )
        stale = {
            "state": "builders",
            "message": "m",
            "allowed": [
                {"draslId": DRASL, "name": "Nether"},
                {"draslId": "aa" * 16, "name": "Removed"},
            ],
        }
        self.controller._gate_pending_file.write_bytes(
            json.dumps({"seq": 8, "nonce": "n8", "leg": stale}).encode()
        )
        self.controller._stage_gate_report(
            {
                "gate": "builders",
                "message": "m",
                "allowed": [{"draslId": DRASL, "name": "Nether", "role": "builder"}],
            }
        )
        # The stale leg is gone from the record: only the delivered leg
        # remains staged, with a bumped seq.
        record = json.loads(self.controller._gate_pending_file.read_text())
        self.assertEqual(record["leg"], delivered_leg)
        self.assertGreater(record["seq"], 8)
        self.controller._flush_gate_report()
        self.assertEqual(reported, [delivered_leg])
        delivered = json.loads(self.controller._gate_delivered_file.read_text())
        self.assertEqual(
            delivered,
            {"seq": record["seq"], "nonce": record["nonce"], "leg": delivered_leg},
        )
        self.assertFalse(self.controller.gate_report_pending())
        self.assertFalse(self.controller._gate_pending_file.exists())

    def test_file_delivered_leg_never_drops_unrelated_pending(self):
        """Finding 4507194510: the delivered FILE (not a process cache)
        decides. Delivered L2 on CP + desired L1 while CP is down stages L1
        instead of unlinking it: CP converges on L1 once it recovers."""

        class BoomReporter:
            def report(self, leg):
                raise RuntimeError("cp down")

        self.controller.gate_reporter = BoomReporter()
        old_leg = {
            "state": "builders",
            "message": "m",
            "allowed": [{"draslId": DRASL, "name": "Nether"}],
        }
        new_leg = {
            "state": "builders",
            "message": "m",
            "allowed": [
                {"draslId": DRASL, "name": "Nether"},
                {"draslId": "bb" * 16, "name": "Fresh"},
            ],
        }
        # CP holds new_leg (delivered file), desired flips back to old_leg.
        self.controller._gate_delivered_file.write_bytes(
            json.dumps({"seq": 4, "nonce": "n4", "leg": new_leg}).encode()
        )
        self.controller._stage_gate_report(
            {
                "gate": "builders",
                "message": "m",
                "allowed": [{"draslId": DRASL, "name": "Nether", "role": "builder"}],
            }
        )
        self.assertTrue(self.controller.gate_report_pending())
        record = json.loads(self.controller._gate_pending_file.read_text())
        self.assertEqual(record["leg"], old_leg)
        self.assertGreater(record["seq"], 4)
        # CP recovers: the staged leg is delivered and recorded.
        reported = []

        class StubReporter:
            def report(self, leg):
                reported.append(leg)
                return {"reported": True, "revision": 5}

        self.controller.gate_reporter = StubReporter()
        self.controller._flush_gate_report()
        self.assertEqual(reported, [old_leg])
        delivered = json.loads(self.controller._gate_delivered_file.read_text())
        self.assertEqual(delivered["leg"], old_leg)
        self.assertEqual(delivered["seq"], record["seq"])
        self.assertFalse(self.controller.gate_report_pending())

    def test_absent_sidecar_sends_nothing(self):
        """Finding 4507194517: an absent sidecar means nothing is pending.
        No in-memory leg exists anymore, so a flush can never send a stale
        leg another process already delivered over."""

        class BoomReporter:
            def report(self, leg):
                raise AssertionError("must not report with no pending marker")

        self.controller.gate_reporter = BoomReporter()
        self.assertFalse(self.controller._gate_pending_file.exists())
        self.controller._flush_gate_report()
        self.assertFalse(self.controller.gate_report_pending())

    def test_concurrent_stage_and_delivery_delivers_newest_last(self):
        """Two processes staging/delivering concurrently end on the newest
        seq delivered with no lost pending (findings 4507194524/4507194521):
        the compare-seq unlink under gate_state.lock drops only the record
        just sent, and a mid-report stage is delivered last."""
        leg_a = {
            "state": "builders",
            "message": "m",
            "allowed": [{"draslId": DRASL, "name": "Nether"}],
        }
        leg_b = {
            "state": "builders",
            "message": "m",
            "allowed": [
                {"draslId": DRASL, "name": "Nether"},
                {"draslId": "bb" * 16, "name": "Fresh"},
            ],
        }
        state_dir = self.dir / "state"
        # Two processes open the same workshop directory (same state path,
        # same records, same report lock). Files are the only shared truth.
        first = Controller(
            state_dir,
            FakeBackend(),
            gate_file=self.dir / "gate.json",
            public_file=self.dir / "public.json",
            releases=catalog(),
        )
        second = Controller(
            state_dir,
            FakeBackend(),
            gate_file=self.dir / "gate.json",
            public_file=self.dir / "public.json",
            releases=catalog(),
        )
        self.assertEqual(first._gate_pending_file, second._gate_pending_file)
        entered = threading.Event()
        release = threading.Event()
        calls = []
        in_flight = {"n": 0, "max": 0}
        guard = threading.Lock()

        class BarrierReporter:
            def report(self, leg):
                with guard:
                    in_flight["n"] += 1
                    in_flight["max"] = max(in_flight["max"], in_flight["n"])
                    calls.append(dict(leg))
                entered.set()
                try:
                    assert release.wait(15), "stalled report released too early"
                finally:
                    with guard:
                        in_flight["n"] -= 1
                return {"reported": True, "revision": 1}

        first.gate_reporter = second.gate_reporter = BarrierReporter()
        # First process stages leg A; delivery starts sending it.
        first._stage_gate_leg(leg_a)
        seq_a = json.loads(first._gate_pending_file.read_text())["seq"]
        worker = threading.Thread(target=first._flush_gate_report)
        worker.start()
        try:
            self.assertTrue(entered.wait(15))
            # The second process stages leg B mid-report; a rival flush must
            # skip (never block, never a second concurrent report).
            second._stage_gate_leg(leg_b)
            seq_b = json.loads(second._gate_pending_file.read_text())["seq"]
            nonce_b = json.loads(second._gate_pending_file.read_text())["nonce"]
            self.assertGreater(seq_b, seq_a)
            rival = threading.Thread(target=second._flush_gate_report)
            rival.start()
            rival.join(5)
            self.assertFalse(rival.is_alive(), "rival flush blocked on the report")
            self.assertEqual(calls, [leg_a])
            self.assertEqual(in_flight["max"], 1)
        finally:
            release.set()
            worker.join(15)
        self.assertFalse(worker.is_alive())
        # The drain loop delivered the newer stage last: newest seq wins.
        self.assertEqual(calls, [leg_a, leg_b])
        delivered = json.loads(first._gate_delivered_file.read_text())
        self.assertEqual(delivered, {"seq": seq_b, "nonce": nonce_b, "leg": leg_b})
        self.assertFalse(first.gate_report_pending())
        self.assertFalse(first._gate_pending_file.exists())

    def test_unchanged_leg_skips_transport(self):
        from test_cp_client import GateStubTransport

        from cp_client import GateReporter

        leg = {"state": "starting", "message": DEFAULT_MESSAGE, "allowed": []}
        transport = GateStubTransport(revision=7, value=dict(leg))
        gets = {"n": 0}
        orig_get = transport.get_json

        def counting_get(url, timeout):
            gets["n"] += 1
            return orig_get(url, timeout)

        transport.get_json = counting_get
        self.controller.gate_reporter = GateReporter(
            "https://cp.invalid", "gate-token", transport=transport
        )
        self.controller.request(
            str(uuid4()), self.goal, self.controller.status()["revision"]
        )
        self.assertEqual(gets["n"], 1)
        self.assertEqual(transport.patches, [])
        # Stable steps re-export without touching the transport at all.
        self.controller.step()
        self.controller.step()
        self.assertEqual(gets["n"], 1)
        self.assertFalse(self.controller.gate_report_pending())

    def test_corrupt_pending_reads_as_pending_not_absent(self):
        """Finding 4507647307: a truncated or schema-invalid pending record is
        pending, not absent. The drain keeps it without sending, and the next
        stage overwrites (heals) it."""
        leg = {
            "state": "builders",
            "message": "m",
            "allowed": [{"draslId": DRASL, "name": "Nether"}],
        }

        class BoomReporter:
            def report(self, leg):
                raise AssertionError("must not report an unreadable leg")

        self.controller.gate_reporter = BoomReporter()
        delivered_leg = {
            "state": "closed",
            "message": "",
            "allowed": [],
        }
        for payload, delivered_seq, want_seq in (
            (b"{truncated", None, 1),
            (b'{"seq": "x", "nonce": "n", "leg": {}}', 4, 5),
        ):
            with self.subTest(payload=payload):
                for record in (
                    self.controller._gate_pending_file,
                    self.controller._gate_delivered_file,
                ):
                    record.unlink(missing_ok=True)
                if delivered_seq is not None:
                    self.controller._gate_delivered_file.write_bytes(
                        json.dumps(
                            {"seq": delivered_seq, "nonce": "nd", "leg": delivered_leg}
                        ).encode()
                    )
                self.controller._gate_pending_file.write_bytes(payload)
                self.assertTrue(self.controller.gate_report_pending())
                self.controller._flush_gate_report()
                self.assertTrue(self.controller._gate_pending_file.exists())
                self.assertTrue(self.controller.gate_report_pending())
                self.controller._stage_gate_leg(leg)
                healed = json.loads(self.controller._gate_pending_file.read_text())
                self.assertEqual(healed["seq"], want_seq)
                self.assertEqual(healed["leg"], leg)
                self.assertIs(type(healed["nonce"]), str)
                self.assertTrue(healed["nonce"])

    def test_eio_pending_reads_as_pending(self):
        """Finding 4507647307, read-error arm: EIO on the pending file holds
        ACKs and keeps the marker instead of reading as absent."""
        leg = {
            "state": "builders",
            "message": "m",
            "allowed": [{"draslId": DRASL, "name": "Nether"}],
        }

        class BoomReporter:
            def report(self, leg):
                raise AssertionError("must not report an unreadable leg")

        self.controller.gate_reporter = BoomReporter()
        self.controller._gate_pending_file.write_bytes(
            json.dumps({"seq": 2, "nonce": "n2", "leg": leg}).encode()
        )
        real_read_bytes = Path.read_bytes

        def eio_on_pending(path):
            if path.name == "gate_pending.json":
                raise OSError("EIO")
            return real_read_bytes(path)

        with mock.patch.object(Path, "read_bytes", eio_on_pending):
            self.assertTrue(self.controller.gate_report_pending())
            self.controller._flush_gate_report()
            self.assertTrue(self.controller._gate_pending_file.exists())
        self.assertTrue(self.controller.gate_report_pending())

    def test_staging_failure_with_old_pending_raises(self):
        """Finding 4507647314 (I3 round 6: the raise is now
        CommitIncompleteError, a hold — never rejected, never effective):
        the swallow tracks this op's staging, not any older record. A
        pre-existing pending leg never masks a failed write for the new leg."""
        from controller import CommitIncompleteError

        old_leg = {
            "state": "builders",
            "message": "m",
            "allowed": [{"draslId": DRASL, "name": "Nether"}],
        }

        class BoomReporter:
            def report(self, leg):
                raise RuntimeError("cp down")

        self.controller.gate_reporter = BoomReporter()
        self.controller._stage_gate_leg(old_leg)
        staged = self.controller._gate_pending_file.read_bytes()
        real_atomic_write = Controller._stage_gate_leg.__globals__["atomic_write"]

        def fail_pending(path, *args, **kwargs):
            if Path(path).name == "gate_pending.json":
                raise OSError("ENOSPC")
            return real_atomic_write(path, *args, **kwargs)

        builder = {
            "draslId": DRASL,
            "name": "Builder1",
            "role": "builder",
            "maps": ["hub"],
        }
        with mock.patch("controller.atomic_write", side_effect=fail_pending):
            with self.assertRaises(CommitIncompleteError):
                self.controller.set_members([builder])
        # The old record is untouched: nothing staged over it.
        self.assertEqual(self.controller._gate_pending_file.read_bytes(), staged)

    def test_reporter_less_drain_keeps_foreign_pending(self):
        """Finding 4507647317: without a gate reporter the drain never unlinks
        another process's pending record, pending reads as pending, and no
        staging happens from here either."""
        wanted = {
            "gate": "builders",
            "message": "m",
            "allowed": [{"draslId": DRASL, "name": "Nether", "role": "builder"}],
        }

        class StubReporter:
            def report(self, leg):
                raise AssertionError("token-less test never delivers")

        holder = Controller(
            self.dir / "state",
            FakeBackend(),
            gate_file=self.dir / "gate.json",
            public_file=self.dir / "public.json",
            releases=catalog(),
        )
        holder.gate_reporter = StubReporter()
        holder._stage_gate_report(wanted)
        staged = self.controller._gate_pending_file.read_bytes()
        self.assertNotEqual(
            getattr(self.controller, "gate_reporter", None),
            holder.gate_reporter,
        )
        # No reporter here: pending still reads as pending ...
        self.assertTrue(self.controller.gate_report_pending())
        # ... the drain keeps it ...
        self.controller._flush_gate_report()
        self.assertEqual(self.controller._gate_pending_file.read_bytes(), staged)
        self.assertTrue(self.controller.gate_report_pending())
        # ... and nothing stages from a process that cannot deliver.
        self.controller._stage_gate_report(wanted)
        self.assertEqual(self.controller._gate_pending_file.read_bytes(), staged)

    def test_report_lock_holds_single_flight(self):
        """I4 (confirm still true): a rival flush while gate_report.lock is
        held skips instead of sending a second concurrent report; the marker
        survives for the retry."""
        import fcntl

        leg = {
            "state": "builders",
            "message": "m",
            "allowed": [{"draslId": DRASL, "name": "Nether"}],
        }
        calls = []

        class CountingReporter:
            def report(self, leg):
                calls.append(dict(leg))
                return {"reported": True, "revision": 1}

        self.controller.gate_reporter = CountingReporter()
        self.controller._stage_gate_leg(leg)
        fd = os.open(
            self.controller._gate_report_lock_file, os.O_CREAT | os.O_RDWR, 0o600
        )
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.controller._flush_gate_report()
            self.assertEqual(calls, [])
            self.assertTrue(self.controller.gate_report_pending())
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        self.controller._flush_gate_report()
        self.assertEqual(calls, [leg])
        self.assertFalse(self.controller.gate_report_pending())

    def test_in_flight_seq_reuse_never_unlinks_newer_record(self):
        """Finding 4507740321 / I2: P1 reports (N, B) while P2 stages (N, C)
        with a fresh nonce because the pending read failed. P1 must not unlink
        C on the reused seq; the drain delivers C last. Fail-before: the
        seq-only unlink drops C unsent."""
        leg_a = {
            "state": "builders",
            "message": "m",
            "allowed": [{"draslId": DRASL, "name": "Nether"}],
        }
        leg_b = {
            "state": "builders",
            "message": "m",
            "allowed": [
                {"draslId": DRASL, "name": "Nether"},
                {"draslId": "bb" * 16, "name": "Fresh"},
            ],
        }
        state_dir = self.dir / "state"
        first = Controller(
            state_dir,
            FakeBackend(),
            gate_file=self.dir / "gate.json",
            public_file=self.dir / "public.json",
            releases=catalog(),
        )
        second = Controller(
            state_dir,
            FakeBackend(),
            gate_file=self.dir / "gate.json",
            public_file=self.dir / "public.json",
            releases=catalog(),
        )
        entered = threading.Event()
        release = threading.Event()
        calls = []

        class BarrierReporter:
            def report(self, leg):
                calls.append(dict(leg))
                entered.set()
                assert release.wait(15), "stalled report released too early"
                return {"reported": True, "revision": 1}

        first.gate_reporter = second.gate_reporter = BarrierReporter()
        first._stage_gate_leg(leg_a)
        worker = threading.Thread(target=first._flush_gate_report)
        worker.start()
        try:
            self.assertTrue(entered.wait(15))
            # The second stage cannot read the pending record (EIO): it must
            # still stage under a fresh identity, never reusing the record.
            real_read_bytes = Path.read_bytes

            def eio_on_pending(path):
                if path.name == "gate_pending.json":
                    raise OSError("EIO")
                return real_read_bytes(path)

            with mock.patch.object(Path, "read_bytes", eio_on_pending):
                second._stage_gate_leg(leg_b)
            staged = json.loads(second._gate_pending_file.read_text())
            self.assertEqual(staged["leg"], leg_b)
            self.assertIs(type(staged["nonce"]), str)
            self.assertTrue(staged["nonce"])
        finally:
            release.set()
            worker.join(15)
        self.assertFalse(worker.is_alive())
        # Both legs reached CP in order; the newer record survived P1's
        # unlink check and was delivered last.
        self.assertEqual(calls, [leg_a, leg_b])
        delivered = json.loads(first._gate_delivered_file.read_text())
        self.assertEqual(delivered, staged)
        self.assertFalse(first._gate_pending_file.exists())
        self.assertFalse(first.gate_report_pending())

    def test_stage_issues_unique_nonce_identities(self):
        """I2: every stage mints a fresh (seq, nonce) identity; an identical
        leg re-staged is a no-op (no churn); a nonce-less legacy record heals
        to a nonce record on the next stage."""
        leg_a = {
            "state": "builders",
            "message": "m",
            "allowed": [{"draslId": DRASL, "name": "Nether"}],
        }
        leg_b = {
            "state": "builders",
            "message": "m",
            "allowed": [
                {"draslId": DRASL, "name": "Nether"},
                {"draslId": "bb" * 16, "name": "Fresh"},
            ],
        }
        self.controller.gate_reporter = mock.sentinel.reporter
        self.controller._stage_gate_leg(leg_a)
        first = json.loads(self.controller._gate_pending_file.read_text())
        self.assertIs(type(first["nonce"]), str)
        self.assertTrue(first["nonce"])
        # Identical leg: no churn, same identity.
        self.controller._stage_gate_leg(leg_a)
        self.assertEqual(
            json.loads(self.controller._gate_pending_file.read_text()), first
        )
        # New leg: new seq, fresh nonce.
        self.controller._stage_gate_leg(leg_b)
        second = json.loads(self.controller._gate_pending_file.read_text())
        self.assertGreater(second["seq"], first["seq"])
        self.assertNotEqual(second["nonce"], first["nonce"])
        # Legacy nonce-less record heals to a nonce record on next stage.
        self.controller._gate_pending_file.write_bytes(
            json.dumps({"seq": 3, "leg": leg_a}).encode()
        )
        self.controller._gate_delivered_file.unlink(missing_ok=True)
        self.controller._stage_gate_leg(leg_b)
        healed = json.loads(self.controller._gate_pending_file.read_text())
        self.assertEqual(healed["leg"], leg_b)
        self.assertIs(type(healed["nonce"]), str)
        self.assertTrue(healed["nonce"])

    def test_unreadable_legacy_record_holds_ack(self):
        """I2 migration: a nonce-less pending record reads as pending (fail
        closed), never as absent, until the next stage heals it."""
        leg = {
            "state": "builders",
            "message": "m",
            "allowed": [{"draslId": DRASL, "name": "Nether"}],
        }
        self.controller.gate_reporter = mock.sentinel.reporter
        self.controller._gate_pending_file.write_bytes(
            json.dumps({"seq": 3, "leg": leg}).encode()
        )
        self.assertTrue(self.controller.gate_report_pending())


if __name__ == "__main__":
    unittest.main()
