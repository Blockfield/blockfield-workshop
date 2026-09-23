"""Map candidates: workshop snapshot -> review -> publish/reject -> rollback."""
import json
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from candidates import MapStore
from controller import Controller, Goal
from test_controller import FakeBackend

ACTOR = {"draslId": "e10438e4797b49718cfecec6a8fa171b", "name": "Nether"}


class CandidateTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.data = self.root / "workshop-data"
        world = self.data / "world" / "dimensions" / "blockfield_workshop" / "konstaxa"
        (world / "region").mkdir(parents=True)
        (world / "region" / "r.0.0.mca").write_bytes(b"edited-region")
        (world / "region" / "r.1.0.mca").write_bytes(b"")  # placeholder: skipped
        (world / "entities").mkdir()
        (world / "entities" / "r.0.0.mca").write_bytes(b"entities")
        (self.data / "blockfield" / "maps").mkdir(parents=True)
        (self.data / "blockfield" / "maps" / "konstaxa.json").write_text(json.dumps({"id": "konstaxa", "name": "base"}))
        self.templates = self.root / "templates"
        (self.templates / "konstaxa" / "region").mkdir(parents=True)
        (self.templates / "konstaxa" / "region" / "r.0.0.mca").write_bytes(b"old-region")
        self.published = self.root / "published"
        self.held = []
        self.clock = [1000.0]

        @contextmanager
        def hold():
            self.held.append("off")
            yield
            self.held.append("on")

        self.store = MapStore(self.data, self.templates, self.published, self.root / "candidates",
                              self.root / "backups", hold_saves=hold, now=lambda: self.clock[0])

    def test_snapshot_holds_saves_and_prefers_the_markup_copy(self):
        overlay = self.data / "world" / "blockfield_workshop" / "maps"
        overlay.mkdir(parents=True)
        (overlay / "konstaxa.json").write_text(json.dumps({"id": "konstaxa", "name": "edited"}))
        meta = self.store.create("konstaxa", ACTOR)
        self.assertEqual(self.held, ["off", "on"])
        self.assertEqual((meta["status"], meta["regions"]), ("pending", 1))
        self.store.publish(meta["id"], ACTOR)
        self.assertEqual(json.loads((self.published / "konstaxa.json").read_text())["name"], "edited")

    def test_publish_replaces_template_and_keeps_a_backup_then_rollback_restores_it(self):
        meta = self.store.create("konstaxa", ACTOR)
        published = self.store.publish(meta["id"], ACTOR)
        self.assertEqual((self.templates / "konstaxa" / "region" / "r.0.0.mca").read_bytes(), b"edited-region")
        self.assertEqual((self.templates / "konstaxa" / "entities" / "r.0.0.mca").read_bytes(), b"entities")
        self.assertFalse((self.templates / "konstaxa" / "region" / "r.1.0.mca").exists())
        self.assertEqual(published["status"], "published")
        self.assertEqual(self.store.export()["backups"], {"konstaxa": 1})
        with self.assertRaises(ValueError):
            self.store.publish(meta["id"], ACTOR)  # not pending any more
        self.clock[0] += 1
        self.store.rollback("konstaxa", ACTOR)
        self.assertEqual((self.templates / "konstaxa" / "region" / "r.0.0.mca").read_bytes(), b"old-region")
        self.assertFalse((self.published / "konstaxa.json").exists(), "no published JSON before the first publish")
        self.assertEqual(self.store.list()[0]["status"], "rolled_back")

    def test_an_older_candidate_cannot_replace_a_newer_published_one(self):
        old = self.store.create("konstaxa", ACTOR)
        self.clock[0] += 1
        new = self.store.create("konstaxa", ACTOR)
        self.store.publish(new["id"], ACTOR)
        with self.assertRaisesRegex(ValueError, "newer"):
            self.store.publish(old["id"], ACTOR)
        self.store.reject(old["id"], ACTOR)
        self.assertEqual({m["id"]: m["status"] for m in self.store.list()}, {old["id"]: "rejected", new["id"]: "published"})

    def test_unknown_or_unopened_maps_are_refused(self):
        for bad in ("../etc", "fields", ""):
            with self.assertRaises(ValueError):
                self.store.create(bad, ACTOR)
        with self.assertRaises(ValueError):
            self.store.publish("0" * 32, ACTOR)

    def test_inbox_kinds_reach_the_store_and_export_for_the_site(self):
        state, admin, inbox = self.root / "state", self.root / "admin", self.root / "inbox"
        inbox.mkdir()
        controller = Controller(state, FakeBackend(), gate_file=self.root / "gate.json", public_file=self.root / "public.json",
                                admin_dir=admin, inbox_dir=inbox, maps=self.store)
        controller.request(str(uuid4()), Goal("game", "v1"), 0)
        controller.step()
        request_id = str(uuid4())
        document = {"schema": 1, "id": request_id, "kind": "candidate", "mapId": "konstaxa", "actor": ACTOR,
                    "createdAt": int(time.time() * 1000)}
        (inbox / f"{request_id}.json").write_text(json.dumps(document))
        self.assertEqual(controller.process_inbox(), 1)
        self.assertTrue(json.loads((admin / "results" / f"{request_id}.json").read_text())["ok"])
        exported = json.loads((admin / "candidates.json").read_text())
        self.assertEqual(exported["candidates"][0]["author"], ACTOR)


if __name__ == "__main__":
    unittest.main()
