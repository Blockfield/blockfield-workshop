"""Gate/public export, no-flap reconciliation and explicit ERROR recovery toward game."""

import json
import os
import tempfile
import unittest
from pathlib import Path
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
        )
        self.goal = Goal("workshop", "v1", str(uuid4()))

    def gate(self):
        return json.loads((self.dir / "gate.json").read_text())

    def public(self):
        return json.loads((self.dir / "public.json").read_text())

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
        self.backend.is_drained = True
        self.controller.step()
        self.backend.processes = [Process(self.goal, True)]
        return self.controller.step()

    def test_game_adoption_exports_public(self):
        self.controller.request(str(uuid4()), Goal("game", "v1"), 0)
        self.assertEqual(self.controller.step()["gate"], "public")
        self.assertEqual(
            self.gate(),
            {
                "schema": 1,
                "revision": 1,
                "gate": "public",
                "backend": "game",
                "message": "",
                "allowed": [],
            },
        )
        self.assertEqual(self.public()["mode"], "game")
        self.assertEqual(self.public()["message"], "")
        self.assertNotIn("drain", self.backend.events)

    def test_stable_steps_do_not_rewrite_anything(self):
        self.controller.set_members([{"draslId": DRASL, "name": "Nether"}])
        self.to_workshop()
        before = self.stamps()
        for _ in range(5):
            self.assertEqual(self.controller.step()["gate"], "builders")
        self.assertEqual(self.stamps(), before)

    def test_draining_never_flaps_through_closed(self):
        self.controller.request(str(uuid4()), self.goal, 0)
        self.assertEqual(self.controller.step()["gate"], "draining")
        before = self.stamps()
        for _ in range(5):
            self.controller.step()
            self.assertEqual(self.gate()["gate"], "draining")
            self.assertEqual(self.gate()["backend"], "game")
            self.assertEqual(self.public()["mode"], "draining")
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
        self.backend.is_drained = True
        self.controller.step()
        closed = self.gate()
        self.assertEqual(
            (closed["gate"], closed["backend"], closed["allowed"]),
            ("closed", "workshop", []),
        )
        self.assertEqual(closed["message"], DEFAULT_MESSAGE)
        self.backend.processes = [Process(self.goal, True)]
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
        self.backend.is_drained = True
        self.controller.step()
        self.assertNotEqual(self.public()["mode"], "draining")
        self.controller.step()
        self.assertEqual(self.public()["mode"], "maintenance")
        maintenance_since = self.public()["since"]
        self.assertGreaterEqual(maintenance_since, since)
        self.controller.step()
        self.assertEqual(self.public()["since"], maintenance_since)

    def test_deleted_export_is_restored_by_next_step(self):
        self.to_workshop()
        (self.dir / "gate.json").unlink()
        self.controller.step()
        self.assertEqual(self.gate()["gate"], "builders")

    def test_error_exports_closed(self):
        self.controller.request(str(uuid4()), self.goal, 0)
        self.backend.is_drained = True
        self.backend.fail_start = True
        self.assertEqual(self.controller.step()["phase"], "ERROR")
        self.assertEqual(self.gate()["gate"], "closed")
        self.assertEqual(self.public()["mode"], "maintenance")

    def test_recover_toward_game_only_and_only_explicitly(self):
        self.to_workshop()
        self.backend.fail_save = True
        self.controller.request(
            str(uuid4()), Goal("game", "v1"), self.controller.status()["revision"]
        )
        self.assertEqual(self.controller.step()["phase"], "ERROR")
        revision = self.controller.status()["revision"]
        with self.assertRaises(RuntimeError):
            self.controller.request(str(uuid4()), Goal("game", "v1"), revision)
        with self.assertRaises(RuntimeError):
            self.controller.request(str(uuid4()), self.goal, revision, recover=True)
        state = self.controller.request(
            str(uuid4()), Goal("game", "v2"), revision, recover=True
        )
        self.assertEqual(
            (state["phase"], state["gate"], state["desired"]["release"]),
            ("ERROR", "closed", "v2"),
        )
        self.backend.fail_save = False
        events = list(self.backend.events)
        self.assertEqual(self.controller.step()["phase"], "ERROR")
        self.assertEqual(self.backend.events, events)
        self.assertEqual(self.controller.step(recover=True)["phase"], "STARTING")
        new = self.backend.events[len(events) :]
        self.assertEqual(new, ["fence", "save", "stop", "start"])
        self.backend.processes = [Process(Goal("game", "v2"), True)]
        self.assertEqual(self.controller.step()["gate"], "public")


if __name__ == "__main__":
    unittest.main()
