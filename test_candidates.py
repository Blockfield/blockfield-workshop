"""Map candidates: workshop snapshot -> review -> publish/reject -> rollback."""

from release_fixtures import catalog

import json
import os
import shutil
import tempfile
import time
import zipfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import hashlib
from unittest import mock

import candidates
from candidates import MapStore, template_digest
from controller import Controller, Goal
from map_package import seed_region
from test_controller import FakeBackend

ACTOR = {"draslId": "e10438e4797b49718cfecec6a8fa171b", "name": "Nether"}


def region_variant(marker):
    """Distinct valid .mca bytes: the timestamp area is inert to every flow."""
    data = bytearray(seed_region())
    data[5000] ^= marker
    return bytes(data)


REGION_DRAFT = region_variant(1)
REGION_ENTITIES = region_variant(2)
REGION_TEMPLATE = region_variant(3)


class CandidateTests(unittest.TestCase):
    def test_wrong_compressor_fails_before_snapshot(self):
        with mock.patch.object(candidates.zlib, "ZLIB_RUNTIME_VERSION", "1.2.13"):
            with self.assertRaisesRegex(RuntimeError, "zlib 1.3.2"):
                self.store.check("fields")

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.data = self.root / "workshop-data"
        world = self.data / "world" / "dimensions" / "blockfield_workshop" / "konstaxa"
        (world / "region").mkdir(parents=True)
        (world / "region" / "r.0.0.mca").write_bytes(REGION_DRAFT)
        (world / "region" / "r.1.0.mca").write_bytes(b"")  # placeholder: skipped
        (world / "entities").mkdir()
        (world / "entities" / "r.0.0.mca").write_bytes(REGION_ENTITIES)
        (self.data / "blockfield" / "maps").mkdir(parents=True)
        (self.data / "blockfield" / "maps" / "konstaxa.json").write_text(
            json.dumps({"id": "konstaxa", "name": "base"})
        )
        self.templates = self.root / "templates"
        (self.templates / "konstaxa" / "region").mkdir(parents=True)
        (self.templates / "konstaxa" / "region" / "r.0.0.mca").write_bytes(
            REGION_TEMPLATE
        )
        self.world = world
        (world / "blockfield-base.json").write_text(
            json.dumps(
                {
                    "schema": 1,
                    "templateDigest": template_digest(self.templates / "konstaxa"),
                }
            )
        )
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

        self.store = MapStore(
            self.data,
            self.templates,
            self.published,
            self.root / "candidates",
            self.root / "backups",
            repo_maps=self.repo,
            hold_saves=hold,
            now=lambda: self.clock[0],
        )

    def test_running_snapshot_keeps_workshop_open_and_holds_saves(self):
        backend = FakeBackend()
        goal = Goal("workshop", "v1.0.0", str(uuid4()))
        backend.ready(goal)
        control = Controller(
            self.root / "controller", backend, maps=self.store, releases=catalog()
        )
        control.request(str(uuid4()), goal, 0)
        self.assertEqual(control.step()["gate"], "builders")
        workshop = backend.processes[-1]
        create = self.store.create

        def snapshot(*args, **kwargs):
            self.assertIn(workshop, backend.processes)
            self.assertEqual(control.status()["gate"], "builders")
            return create(*args)

        with mock.patch.object(self.store, "create", side_effect=snapshot):
            control._snapshot("konstaxa", ACTOR, "")
        self.assertEqual(self.held, ["off", "on"])
        self.assertEqual(backend.events, [])
        with mock.patch.object(self.store, "create", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                control._snapshot("konstaxa", ACTOR, "")
        # A failed copy only fails the request: the editor and its drafts are untouched.
        self.assertEqual(backend.events, [])
        self.assertEqual(control.status()["phase"], "workshop")
        self.assertEqual(control.step()["gate"], "builders")

    def test_snapshot_of_a_map_without_draft_does_not_stop_the_workshop(self):
        backend = FakeBackend()
        goal = Goal("workshop", "v1.0.0", str(uuid4()))
        backend.ready(goal)
        control = Controller(
            self.root / "controller", backend, maps=self.store, releases=catalog()
        )
        control.request(str(uuid4()), goal, 0)
        self.assertEqual(control.step()["gate"], "builders")
        for bad in ("dust2", "../konstaxa", None):
            with self.assertRaises(ValueError):
                control._snapshot(bad, ACTOR, "")
        self.assertEqual(
            (control.status()["phase"], control.status()["gate"]),
            ("workshop", "builders"),
        )
        self.assertEqual(backend.events, [])
        with mock.patch.object(
            candidates.shutil, "disk_usage", return_value=mock.Mock(free=1 << 20)
        ):
            with self.assertRaisesRegex(OSError, "free space"):
                control._snapshot("konstaxa", ACTOR, "")
        self.assertEqual(
            (control.status()["phase"], control.status()["gate"]),
            ("workshop", "builders"),
        )
        self.assertEqual(backend.events, [])

    def test_snapshot_holds_saves_and_prefers_the_markup_copy(self):
        overlay = self.data / "world" / "blockfield_workshop" / "maps"
        overlay.mkdir(parents=True)
        (overlay / "konstaxa.json").write_text(
            json.dumps({"id": "konstaxa", "name": "edited"})
        )
        meta = self.store.create("konstaxa", ACTOR)
        self.assertEqual(self.held, ["off", "on"])
        self.assertEqual((meta["status"], meta["regions"]), ("pending", 1))
        self.store.publish(meta["id"], ACTOR, expected_head=None)
        with zipfile.ZipFile(self.store.archive_path(meta["id"])) as archive:
            self.assertEqual(json.loads(archive.read("map.json"))["name"], "edited")

    def test_export_reaps_stale_temp_files(self):
        meta = self.store.create("konstaxa", ACTOR)
        exports = self.store.versions.archive / "exports"
        exports.mkdir(parents=True, exist_ok=True)
        stale = exports / ".export-deadbeef.tmp"
        stale.write_bytes(b"partial")
        target = exports / "candidate.zip"
        checksum, size = self.store.versions.export_package(meta, target)
        self.assertFalse(stale.exists())
        self.assertTrue(target.is_file())
        self.assertGreater(size, 0)
        with zipfile.ZipFile(target) as archive:
            manifest = json.loads(archive.read("manifest.json"))
        self.assertEqual(manifest["schemaVersion"], 2)
        self.assertEqual(manifest["formatVersion"], 1)
        self.assertTrue(manifest["requiredBlocks"]["blocks"])
        self.assertEqual({entry["id"] for entry in manifest["requiredMods"]}, set())

    def test_acceptance_preserves_live_world_json_and_archive(self):
        meta = self.store.create("konstaxa", ACTOR)
        archive = self.store.archive_path(meta["id"])
        before = archive.read_bytes()
        accepted = self.store.publish(meta["id"], ACTOR, expected_head=None)
        self.assertEqual(accepted["status"], "accepted")
        self.assertEqual(archive.read_bytes(), before)
        self.assertEqual(
            (self.templates / "konstaxa/region/r.0.0.mca").read_bytes(), REGION_TEMPLATE
        )
        self.assertFalse(self.published.exists())
        self.assertFalse(self.store.backups.exists())
        with self.assertRaises(ValueError):
            self.store.publish(meta["id"], ACTOR)
        with self.assertRaisesRegex(ValueError, "versionDigest and expectedHead"):
            self.store.rollback("konstaxa", ACTOR)

    def test_an_older_candidate_cannot_replace_a_newer_published_one(self):
        old = self.store.create("konstaxa", ACTOR)
        self.clock[0] += 1
        new = self.store.create("konstaxa", ACTOR)
        self.store.publish(new["id"], ACTOR, expected_head=None)
        with self.assertRaisesRegex(ValueError, "stale"):
            self.store.publish(old["id"], ACTOR, expected_head=None)
        self.store.reject(old["id"], ACTOR)
        self.assertEqual(
            {m["id"]: m["status"] for m in self.store.list()},
            {old["id"]: "rejected", new["id"]: "accepted"},
        )

    def test_an_interrupted_export_leaves_no_archive_behind(self):
        # Stand acceptance 2026-09-24: a controller killed mid-export left a 1.8 MB .zip.tmp forever.
        self.store.candidates.mkdir(parents=True, exist_ok=True)
        stale = self.store.candidates / f".{'0' * 32}.zip.tmp"
        stale.write_bytes(b"partial")
        meta = self.store.create("konstaxa", ACTOR)
        self.assertFalse(stale.exists())
        self.assertEqual(
            sorted(p.name for p in self.store.candidates.iterdir()),
            [f"{meta['id']}.json"],
        )

    def test_unknown_or_unopened_maps_are_refused(self):
        for bad in ("../etc", "fields", ""):
            with self.assertRaises(ValueError):
                self.store.create(bad, ACTOR)
        with self.assertRaises(ValueError):
            self.store.publish("0" * 32, ACTOR)

    def test_inbox_kinds_reach_the_store_and_export_for_the_site(self):
        state, admin, inbox = (
            self.root / "state",
            self.root / "admin",
            self.root / "inbox",
        )
        inbox.mkdir()
        controller = Controller(
            state,
            FakeBackend(),
            gate_file=self.root / "gate.json",
            public_file=self.root / "public.json",
            admin_dir=admin,
            inbox_dir=inbox,
            maps=self.store,
            releases=catalog(),
        )
        controller.set_members([{**ACTOR, "role": "admin"}])
        controller.request(str(uuid4()), Goal("game", "v1.0.0"), 0)
        controller.step()
        controller.backend.ready(Goal(**controller.status()["desired"]))
        controller.step()
        request_id = str(uuid4())
        document = {
            "schema": 1,
            "id": request_id,
            "kind": "candidate",
            "mapId": "konstaxa",
            "actor": ACTOR,
            "createdAt": int(time.time() * 1000),
        }
        (inbox / f"{request_id}.json").write_text(json.dumps(document))
        self.assertEqual(controller.process_inbox(), 1)
        self.assertTrue(
            json.loads((admin / "results" / f"{request_id}.json").read_text())["ok"]
        )
        exported = json.loads((admin / "candidates.json").read_text())
        self.assertEqual(exported["candidates"][0]["author"], ACTOR)


class ChownTreeRaceTests(unittest.TestCase):
    def test_chown_tree_skips_file_hardlinked_after_stat(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            target = root / "entry.dat"
            target.write_bytes(b"draft")
            os.chmod(target, 0o600)  # wrong mode so the pre-open check proceeds
            real_fstat = os.fstat

            def fake_fstat(fd):
                info = real_fstat(fd)
                return os.stat_result(
                    (
                        info.st_mode,
                        info.st_ino,
                        info.st_dev,
                        2,  # swapped for a hard link to an outside inode after stat
                        info.st_uid,
                        info.st_gid,
                        info.st_size,
                        info.st_atime,
                        info.st_mtime,
                        info.st_ctime,
                    )
                )

            with (
                mock.patch.object(candidates.os, "fstat", side_effect=fake_fstat),
                mock.patch.object(candidates, "_repair") as repair,
            ):
                candidates._chown_tree(root)
            # Only the directory is repaired; the file opened after the swap is skipped.
            self.assertEqual(repair.call_count, 1)


if __name__ == "__main__":
    unittest.main()


class BaseAndPlaytestTests(unittest.TestCase):
    setUp = CandidateTests.setUp

    def test_digest_matches_the_mod_algorithm(self):
        root = self.root / "vector"
        (root / "region").mkdir(parents=True)
        (root / "region" / "r.0.0.mca").write_bytes(b"abc")
        line = "region/r.0.0.mca\0ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad\n"
        self.assertEqual(
            template_digest(root), hashlib.sha256(line.encode()).hexdigest()
        )
        self.assertIsNone(template_digest(self.root / "nothing"))

    def test_changed_or_unknown_base_needs_force_and_acceptance_keeps_draft_base(self):
        meta = self.store.create("konstaxa", ACTOR)
        self.assertEqual(self.store.export()["candidates"][0]["baseMatches"], True)
        (self.templates / "konstaxa" / "region" / "r.0.0.mca").write_bytes(
            b"someone-else-published"
        )
        self.assertEqual(self.store.export()["candidates"][0]["baseMatches"], False)
        with self.assertRaisesRegex(ValueError, "^base changed"):
            self.store.publish(meta["id"], ACTOR, expected_head=None)
        self.assertEqual(
            (self.templates / "konstaxa" / "region" / "r.0.0.mca").read_bytes(),
            b"someone-else-published",
        )
        self.assertTrue(
            self.store.publish(meta["id"], ACTOR, force=True, expected_head=None)[
                "forced"
            ]
        )
        marker = json.loads((self.world / "blockfield-base.json").read_text())[
            "templateDigest"
        ]
        self.assertNotEqual(marker, template_digest(self.templates / "konstaxa"))
        self.clock[0] += 1
        (self.world / "region" / "r.0.0.mca").write_bytes(REGION_ENTITIES)
        self.assertEqual(
            self.store.publish(
                self.store.create("konstaxa", ACTOR)["id"],
                ACTOR,
                force=True,
                expected_head=self.store.versions.head("konstaxa")["versionDigest"],
            )["status"],
            "accepted",
        )
        (self.world / "blockfield-base.json").unlink()
        self.clock[0] += 1
        (self.world / "region" / "r.0.0.mca").write_bytes(REGION_DRAFT)
        unknown = self.store.create("konstaxa", ACTOR)
        self.assertIsNone(self.store.export()["candidates"][0]["baseMatches"])
        with self.assertRaisesRegex(ValueError, "^base unknown"):
            self.store.publish(
                unknown["id"],
                ACTOR,
                expected_head=self.store.versions.head("konstaxa")["versionDigest"],
            )

    def test_failed_publish_leaves_template_and_no_staging(self):
        meta = self.store.create("konstaxa", ACTOR)
        with mock.patch.object(
            candidates, "_atomic_json", side_effect=OSError("disk full")
        ):
            with self.assertRaises(OSError):
                self.store.publish(meta["id"], ACTOR, expected_head=None)
        self.assertEqual(
            (self.templates / "konstaxa" / "region" / "r.0.0.mca").read_bytes(),
            REGION_TEMPLATE,
        )
        self.assertEqual([p.name for p in self.templates.iterdir()], ["konstaxa"])
        self.assertEqual(
            self.store.export()["backups"],
            {},
            "a failed publish leaves no rollback target",
        )
        self.assertEqual(self.store.list()[0]["status"], "pending")

    def test_extract_for_the_playtest(self):
        meta = self.store.create("konstaxa", ACTOR)
        target = self.data / "test-candidate"
        self.assertEqual(
            self.store.extract(self.store.sha256_of(meta["id"]), target), "konstaxa"
        )
        self.assertEqual((target / "map-id").read_text().strip(), "konstaxa")
        self.assertEqual(
            (target / "world" / "region" / "r.0.0.mca").read_bytes(), REGION_DRAFT
        )
        self.assertEqual(
            json.loads((target / "map.json").read_text())["id"], "konstaxa"
        )
        self.assertFalse((target / "manifest.json").exists())
        with self.assertRaises(ValueError):
            self.store.sha256_of("0" * 32)

    def test_extract_rejects_tampered_bytes(self):
        meta = self.store.create("konstaxa", ACTOR)
        target = self.data / "test-candidate-tampered"
        real_read = zipfile.ZipFile.read

        def tampered(archive, name):
            data = real_read(archive, name)
            key = name.filename if isinstance(name, zipfile.ZipInfo) else name
            return b"tampered" if key == "world/region/r.0.0.mca" else data

        with mock.patch.object(zipfile.ZipFile, "read", tampered):
            with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                self.store.extract(self.store.sha256_of(meta["id"]), target)

    def test_extract_rejects_leftover_bytes_from_a_partial_wipe(self):
        meta = self.store.create("konstaxa", ACTOR)
        target = self.data / "test-candidate-stale"
        stale = target / "world" / "region" / "r.9.9.mca"
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.write_bytes(b"stale")
        real_rmtree = shutil.rmtree

        def partial_wipe(path, *args, **kwargs):
            real_rmtree(path, *args, **kwargs)
            stale.parent.mkdir(parents=True, exist_ok=True)
            stale.write_bytes(b"stale")

        with mock.patch.object(candidates.shutil, "rmtree", partial_wipe):
            with self.assertRaisesRegex(ValueError, "does not match its manifest"):
                self.store.extract(self.store.sha256_of(meta["id"]), target)

    def test_snapshot_streams_region_files_without_loading_them(self):
        seen = []
        real_read_bytes = Path.read_bytes

        def spy_read_bytes(path):
            if path.suffix == ".mca":
                seen.append(path)
            return real_read_bytes(path)

        with mock.patch.object(Path, "read_bytes", spy_read_bytes):
            self.store.create("konstaxa", ACTOR)
        self.assertEqual(seen, [])

    def test_oversized_world_fails_before_loading_any_region(self):
        region = self.world / "region"
        for name in ("r.9.9.mca", "r.9.10.mca"):
            with open(region / name, "wb") as handle:
                handle.truncate(600 << 20)
        with mock.patch.object(
            Path, "read_bytes", side_effect=AssertionError("must not load a huge world")
        ):
            with self.assertRaisesRegex(ValueError, "exceeds package limit"):
                self.store.versions.world_files(self.world, b"{}")

    def test_extract_for_the_playtest_is_not_group_writable(self):
        meta = self.store.create("konstaxa", ACTOR)
        target = self.data / "test-candidate-locked"
        self.store.extract(self.store.sha256_of(meta["id"]), target)
        for directory, _, files in os.walk(target):
            self.assertEqual(Path(directory).stat().st_mode & 0o777, 0o755, directory)
            for name in files:
                self.assertEqual(
                    (Path(directory) / name).stat().st_mode & 0o777, 0o644, name
                )

    def test_published_json_merged_into_git_is_retired(self):
        self.published.mkdir()
        (self.published / "konstaxa.json").write_text('{"id":"konstaxa"}')
        self.assertEqual(
            self.store.export()["overlays"], {"konstaxa": {"differsFromRepo": True}}
        )
        self.assertEqual(self.store.retire_merged_overlays(), [])
        (self.repo / "konstaxa.json").write_text(
            (self.published / "konstaxa.json").read_text()
        )
        self.assertEqual(
            self.store.export()["overlays"], {"konstaxa": {"differsFromRepo": False}}
        )
        self.assertEqual(self.store.retire_merged_overlays(), ["konstaxa"])
        self.assertFalse((self.published / "konstaxa.json").exists())
