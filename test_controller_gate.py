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


if __name__ == "__main__":
    unittest.main()
