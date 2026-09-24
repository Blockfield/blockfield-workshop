"""Map candidates: workshop snapshot -> review -> publish/reject -> rollback."""
import json
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import hashlib
from unittest import mock

import candidates
from candidates import MapStore, template_digest
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
        self.world = world
        (world / "blockfield-base.json").write_text(json.dumps({"schema": 1, "templateDigest": template_digest(self.templates / "konstaxa")}))
        self.published = self.root / "published"
        self.repo = self.root / "repo-maps"
        self.repo.mkdir()
        self.held = []
        self.clock = [1000.0]

        @contextmanager
        def hold():
            self.held.append("off")
            yield
            self.held.append("on")

        self.store = MapStore(self.data, self.templates, self.published, self.root / "candidates",
                              self.root / "backups", repo_maps=self.repo, hold_saves=hold, now=lambda: self.clock[0])

    def test_running_snapshot_stops_before_copy_and_waits_for_readiness(self):
        backend = FakeBackend()
        goal = Goal("workshop", "v1", str(uuid4()))
        from controller import Process
        backend.processes = [Process(goal, True)]
        control = Controller(self.root / "controller", backend, maps=self.store)
        control.request(str(uuid4()), goal, 0)
        control.step()
        create = self.store.create

        def snapshot(*args):
            self.assertEqual(backend.processes, [])
            self.assertEqual(control.status()["gate"], "closed")
            return create(*args)

        with mock.patch.object(self.store, "create", side_effect=snapshot):
            control._snapshot("konstaxa", ACTOR, "")
        self.assertEqual(backend.events, ["save", "stop", "start"])
        self.assertEqual(control.status()["phase"], "STARTING")
        self.assertEqual(control.step()["gate"], "closed")
        backend.processes = [Process(goal, True)]
        self.assertEqual(control.step()["gate"], "builders")

        for failure in ("save", "stop", "copy", "start"):
            backend.fail_save = failure == "save"
            backend.fail_stop = failure == "stop"
            backend.fail_start = failure == "start"
            with mock.patch.object(self.store, "create", side_effect=OSError("disk full") if failure == "copy" else snapshot):
                with self.assertRaises((OSError, RuntimeError)):
                    control._snapshot("konstaxa", ACTOR, "")
            self.assertEqual(control.status()["phase"], "ERROR")
            self.assertEqual(control.step()["gate"], "closed")
            self.assertLessEqual(len(backend.processes), 1)
            # Restore a ready workshop fixture for the next independent fault.
            backend.processes = [Process(goal, True)]
            state = control.status()
            state.update(phase="workshop", gate="builders", error=None, errorMessage=None)
            control._write(state)

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


class BaseAndPlaytestTests(unittest.TestCase):
    setUp = CandidateTests.setUp

    def test_digest_matches_the_mod_algorithm(self):
        root = self.root / "vector"
        (root / "region").mkdir(parents=True)
        (root / "region" / "r.0.0.mca").write_bytes(b"abc")
        line = "region/r.0.0.mca\0ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad\n"
        self.assertEqual(template_digest(root), hashlib.sha256(line.encode()).hexdigest())
        self.assertIsNone(template_digest(self.root / "nothing"))

    def test_changed_or_unknown_base_needs_force_and_a_publish_rebases_the_draft(self):
        meta = self.store.create("konstaxa", ACTOR)
        self.assertEqual(self.store.export()["candidates"][0]["baseMatches"], True)
        (self.templates / "konstaxa" / "region" / "r.0.0.mca").write_bytes(b"someone-else-published")
        self.assertEqual(self.store.export()["candidates"][0]["baseMatches"], False)
        with self.assertRaisesRegex(ValueError, "^base changed"):
            self.store.publish(meta["id"], ACTOR)
        self.assertEqual((self.templates / "konstaxa" / "region" / "r.0.0.mca").read_bytes(), b"someone-else-published")
        self.assertTrue(self.store.publish(meta["id"], ACTOR, force=True)["forced"])
        marker = json.loads((self.world / "blockfield-base.json").read_text())["templateDigest"]
        self.assertEqual(marker, template_digest(self.templates / "konstaxa"))
        self.clock[0] += 1
        self.assertEqual(self.store.publish(self.store.create("konstaxa", ACTOR)["id"], ACTOR)["status"], "published")
        (self.world / "blockfield-base.json").unlink()
        self.clock[0] += 1
        unknown = self.store.create("konstaxa", ACTOR)
        self.assertIsNone(self.store.export()["candidates"][0]["baseMatches"])
        with self.assertRaisesRegex(ValueError, "^base unknown"):
            self.store.publish(unknown["id"], ACTOR)

    def test_failed_publish_leaves_template_and_no_staging(self):
        meta = self.store.create("konstaxa", ACTOR)
        with mock.patch.object(candidates.shutil, "disk_usage", return_value=mock.Mock(free=0)):
            with self.assertRaises(OSError):
                self.store.publish(meta["id"], ACTOR)
        with mock.patch.object(candidates, "_atomic_json", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.store.publish(meta["id"], ACTOR)
        self.assertEqual((self.templates / "konstaxa" / "region" / "r.0.0.mca").read_bytes(), b"old-region")
        self.assertEqual([p.name for p in self.templates.iterdir()], ["konstaxa"])
        self.assertEqual(self.store.export()["backups"], {}, "a failed publish leaves no rollback target")
        self.assertEqual(self.store.list()[0]["status"], "pending")

    def test_extract_for_the_playtest(self):
        meta = self.store.create("konstaxa", ACTOR)
        target = self.data / "test-candidate"
        self.assertEqual(self.store.extract(self.store.sha256_of(meta["id"]), target), "konstaxa")
        self.assertEqual((target / "map-id").read_text().strip(), "konstaxa")
        self.assertEqual((target / "world" / "region" / "r.0.0.mca").read_bytes(), b"edited-region")
        self.assertEqual(json.loads((target / "map.json").read_text())["id"], "konstaxa")
        self.assertFalse((target / "manifest.json").exists())
        with self.assertRaises(ValueError):
            self.store.sha256_of("0" * 32)

    def test_published_json_merged_into_git_is_retired(self):
        self.store.publish(self.store.create("konstaxa", ACTOR)["id"], ACTOR)
        self.assertEqual(self.store.export()["overlays"], {"konstaxa": {"differsFromRepo": True}})
        self.assertEqual(self.store.retire_merged_overlays(), [])
        (self.repo / "konstaxa.json").write_text((self.published / "konstaxa.json").read_text())
        self.assertEqual(self.store.export()["overlays"], {"konstaxa": {"differsFromRepo": False}})
        self.assertEqual(self.store.retire_merged_overlays(), ["konstaxa"])
        self.assertFalse((self.published / "konstaxa.json").exists())
