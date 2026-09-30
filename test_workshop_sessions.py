"""Real controller session preparation and offline imports, without starting a backend."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from uuid import uuid4

from candidates import MapStore
from controller import Controller, Goal, Process
from test_controller import FakeBackend


class WorkshopSessionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = MapStore(
            self.root / "data",
            self.root / "templates",
            self.root / "published",
            self.root / "archive/candidates",
            self.root / "archive/backups",
            archive_root=self.root / "archive",
            active_root=self.root / "active",
        )
        self.heads = {}
        for map_id, kind in (
            ("arena", "arena"),
            ("lobby", "lobby"),
            ("limbo", "limbo"),
        ):
            world = self.root / "sources" / map_id
            (world / "region").mkdir(parents=True)
            (world / "region/r.0.0.mca").write_bytes(map_id.encode())
            config = world.parent / f"{map_id}.json"
            config.write_text(
                json.dumps(
                    {
                        "id": map_id,
                        "kind": kind,
                        "name": map_id,
                        "bounds": {"minX": 0, "maxX": 100, "minZ": 0, "maxZ": 100},
                        "mode_data": {},
                        "supported_modes": [],
                    }
                )
            )
            version = self.store.versions.import_template(map_id, world, config)
            self.heads[map_id] = self.store.versions.activate(
                map_id, version, {"draslId": "setup", "name": "setup"}, None
            )
        self.backend = FakeBackend()
        self.backend.is_drained = True
        self.controller = Controller(
            self.root / "state",
            self.backend,
            maps=self.store,
            admin_dir=self.root / "admin",
        )
        self.controller.request(str(uuid4()), Goal("game", "v1"), 0)
        self.controller.step()

    def request(self, copy_id=None, maps=None):
        copy_id = copy_id or str(uuid4())
        session = {"schema": 1, "id": copy_id, "maps": maps}
        self.controller.request(
            str(uuid4()),
            Goal("workshop", "v1", copy_id),
            self.controller.status()["revision"],
            session=session,
        )
        return copy_id

    def make_ready(self):
        self.controller.step()
        self.backend.processes = [
            Process(Goal(**self.controller.status()["desired"]), True)
        ]
        self.controller.step()

    def test_new_selected_heads_are_copied_and_resume_preserves_edits(self):
        copy_id = self.request(maps=["arena", "lobby", "limbo"])
        for map_id in self.heads:
            self.assertEqual(
                self.store.versions.validate_working_copy(map_id, copy_id)[
                    "versionDigest"
                ],
                self.heads[map_id]["versionDigest"],
            )
        region = (
            self.store.versions.working_copy("arena", copy_id)
            / "world/region/r.0.0.mca"
        )
        region.write_bytes(b"draft edit")
        self.make_ready()
        self.controller.request(
            str(uuid4()), Goal("game", "v1"), self.controller.status()["revision"]
        )
        self.make_ready()
        self.request(copy_id=copy_id)
        self.assertEqual(region.read_bytes(), b"draft edit")
        self.assertEqual(
            (
                Path(self.heads["arena"]["bundle"]) / "world/region/r.0.0.mca"
            ).read_bytes(),
            b"arena",
        )
        another = str(uuid4())
        self.store.prepare_session(another, ["arena"])
        self.assertEqual(
            (
                self.store.versions.working_copy("arena", another)
                / "world/region/r.0.0.mca"
            ).read_bytes(),
            b"arena",
        )

    def test_missing_head_and_corrupt_resume_do_not_change_prior_intent_or_backend(
        self,
    ):
        before = self.controller.status()
        (self.store.versions.active / "limbo/head.json").unlink()
        with self.assertRaisesRegex(ValueError, "no active head"):
            self.request(maps=["limbo"])
        self.assertEqual(self.controller.status(), before)
        self.assertIsNone(self.controller.session())
        self.assertEqual(self.backend.events, [])
        copy_id = str(uuid4())
        self.store.prepare_session(copy_id, ["arena"])
        bundle = self.store.versions.working_copy("arena", copy_id)
        (bundle / "manifest.json").write_text("{}")
        with self.assertRaises(ValueError):
            self.request(copy_id, ["arena"])
        self.assertEqual(self.controller.status(), before)
        self.assertEqual(self.backend.events, [])

    def test_enospc_and_invalid_edited_config_leave_previous_profile(self):
        before = self.controller.status()
        with mock.patch("candidates.shutil.disk_usage", return_value=mock.Mock(free=0)):
            with self.assertRaisesRegex(OSError, "space"):
                self.request(maps=["arena"])
        self.assertEqual(self.controller.status(), before)
        copy_id = str(uuid4())
        self.store.prepare_session(copy_id, ["arena"])
        config = self.store.versions.working_copy("arena", copy_id) / "map.json"
        original = config.read_bytes()
        config.write_text(original.decode().replace('"minX": 0', '"minX": 200'))
        with self.assertRaisesRegex(ValueError, "bounds"):
            self.request(copy_id, ["arena"])
        self.assertEqual(self.controller.status(), before)
        self.assertEqual(self.backend.events, [])

    def test_removed_copy_is_refused_before_drain_or_start(self):
        copy_id = self.request(maps=["arena"])
        bundle = self.store.versions.working_copy("arena", copy_id)
        (bundle / "map.json").unlink()
        state = self.controller.step()
        self.assertEqual(state["phase"], "ERROR")
        self.assertEqual(self.backend.events, [])
        self.assertEqual(self.backend.processes[0].goal.profile, "game")

    def test_failed_intent_write_restores_previous_session_exports(self):
        first = self.request(maps=["arena"])
        self.make_ready()
        original = self.controller.session()
        before = self.controller.status()
        import controller as implementation

        write = implementation.atomic_write

        def fail_state(path, *args, **kwargs):
            if path == self.controller.file:
                raise OSError("state commit unavailable")
            return write(path, *args, **kwargs)

        events = list(self.backend.events)
        with mock.patch("controller.atomic_write", side_effect=fail_state):
            with self.assertRaisesRegex(OSError, "state commit"):
                self.request(maps=["lobby"])
        self.assertEqual(self.controller.status(), before)
        self.assertEqual(self.controller.session(), original)
        self.assertEqual(
            json.loads((self.root / "admin/session.json").read_bytes())["id"], first
        )
        self.assertEqual(self.backend.events, events)

    def test_snapshot_requires_selected_copy_and_retains_saved_origin(self):
        copy_id = self.request(maps=["arena"])
        self.make_ready()
        events = list(self.backend.events)
        for selected in (None, str(uuid4())):
            with self.assertRaises((ValueError, FileNotFoundError)):
                self.controller._snapshot(
                    "arena", {"name": "tester"}, "", working_copy_id=selected
                )
            self.assertEqual(self.backend.events, events)
        self.store.prepare_session(copy_id, ["lobby"])
        with self.assertRaisesRegex(ValueError, "selected"):
            self.controller._snapshot(
                "lobby", {"name": "tester"}, "", working_copy_id=copy_id
            )
        region = (
            self.store.versions.working_copy("arena", copy_id)
            / "world/region/r.0.0.mca"
        )
        region.write_bytes(b"saved draft")
        result = self.controller._snapshot(
            "arena", {"name": "tester"}, "", working_copy_id=copy_id
        )
        self.assertEqual(result["workingCopyId"], copy_id)
        self.assertEqual(result["baseVersion"], self.heads["arena"]["versionDigest"])
        self.assertEqual(self.backend.events[-3:], ["save", "stop", "start"])

    def test_cancel_to_previous_workshop_restores_session_identity(self):
        first = self.request(maps=["arena"])
        self.make_ready()
        original = self.controller.session()
        second = self.request(maps=["lobby"])
        self.assertNotEqual(first, second)
        self.controller.step()
        state = self.controller.cancel()
        self.assertEqual(state["phase"], "STARTING")
        self.assertEqual(state["desired"]["session"], first)
        self.assertEqual(self.controller.session(), original)
        self.assertEqual(
            json.loads((self.root / "admin/session.json").read_bytes())["id"], first
        )


if __name__ == "__main__":
    unittest.main()
