"""Map history, coherent SSD activation and isolated editable copies; no live backend."""

from release_fixtures import catalog as release_catalog_fixture

import json
import os
import shutil
import stat
import time
import unittest
from pathlib import Path
from unittest import mock
from uuid import uuid4

import candidates
from candidates import MapStore, template_digest
from controller import Controller, Goal
import test_candidates as fixtures
from test_controller import FakeBackend


ACTOR = fixtures.ACTOR


class MapVersionTests(unittest.TestCase):
    setUp = fixtures.CandidateTests.setUp

    def blobs(self):
        return {
            path.name
            for path in (self.store.versions.archive / "blobs").glob("*/*")
            if path.is_file()
        }

    def candidate(self, map_id="konstaxa", working_copy_id=None):
        self.clock[0] += 1
        return self.store.create(map_id, ACTOR, working_copy_id=working_copy_id)

    def activate(self, meta, expected=None):
        self.store.publish(meta["id"], ACTOR, expected_head=expected)
        return self.store.versions.head(meta["mapId"])

    def another_map(self, map_id, kind="arena"):
        draft = self.data / "world/dimensions/blockfield_workshop" / map_id
        (draft / "region").mkdir(parents=True)
        (draft / "region/r.0.0.mca").write_bytes(fixtures.region_variant(4))
        template = self.templates / map_id
        shutil.copytree(draft, template)
        (draft / "blockfield-base.json").write_text(
            json.dumps({"templateDigest": template_digest(template)})
        )
        (self.data / "blockfield/maps" / f"{map_id}.json").write_text(
            json.dumps({"id": map_id, "kind": kind})
        )

    def test_independent_histories_reuse_blobs_and_restore_deletes(self):
        self.another_map("fields")
        first = self.candidate()
        first_head = self.activate(first)
        other = self.candidate("fields")
        other_head = self.activate(other)
        before = self.blobs()
        copy_id = str(uuid4())
        copy = self.store.checkout("konstaxa", first["versionDigest"], copy_id)
        world = Path(copy["bundle"]) / "world"
        (world / "region/r.0.0.mca").write_bytes(fixtures.region_variant(5))
        (world / "entities/r.0.0.mca").unlink()
        second = self.candidate(working_copy_id=copy_id)
        version = self.store.versions.read("konstaxa", second["versionDigest"])
        self.assertEqual(len(self.blobs() - before), 1)
        self.assertEqual(version["parent"], first["versionDigest"])
        self.assertEqual(version["changed"], ["world/region/r.0.0.mca"])
        self.assertEqual(version["deleted"], ["world/entities/r.0.0.mca"])
        second_head = self.activate(second, first_head["versionDigest"])
        self.assertFalse(
            (Path(second_head["bundle"]) / "world/entities/r.0.0.mca").exists()
        )
        restored = self.store.rollback(
            "konstaxa",
            ACTOR,
            version_digest=first["versionDigest"],
            expected_head=second["versionDigest"],
        )
        self.assertEqual(
            (Path(restored["bundle"]) / "world/entities/r.0.0.mca").read_bytes(),
            fixtures.REGION_ENTITIES,
        )
        self.assertEqual(self.store.versions.head("fields"), other_head)
        restore = self.store.versions.read("konstaxa", restored["versionDigest"])
        self.assertNotEqual(restored["versionDigest"], first["versionDigest"])
        self.assertEqual(restore["parent"], second["versionDigest"])
        self.assertEqual(
            restore["files"],
            self.store.versions.read("konstaxa", first["versionDigest"])["files"],
        )
        self.assertEqual(len(self.store.versions.history("konstaxa")), 4)
        with self.assertRaisesRegex(ValueError, "missing"):
            self.store.rollback(
                "fields",
                ACTOR,
                version_digest=first["versionDigest"],
                expected_head=other["versionDigest"],
            )
        self.assertEqual(self.store.versions.head("fields"), other_head)

    def test_checkout_has_own_writable_inodes_and_is_not_overwritten(self):
        first = self.candidate()
        version = self.store.versions.read("konstaxa", first["versionDigest"])
        digest = version["files"]["world/region/r.0.0.mca"]
        blob = self.store.versions._blob(digest)
        copy_id = str(uuid4())
        copy = self.store.checkout("konstaxa", first["versionDigest"], copy_id)
        writable = Path(copy["bundle"]) / "world/region/r.0.0.mca"
        if os.name != "nt":
            self.assertTrue(writable.stat().st_mode & stat.S_IWGRP)
            self.assertTrue(writable.parent.stat().st_mode & stat.S_IWGRP)
            self.assertFalse(blob.stat().st_mode & (stat.S_IWUSR | stat.S_IWGRP))
        self.assertNotEqual(
            (blob.stat().st_dev, blob.stat().st_ino),
            (writable.stat().st_dev, writable.stat().st_ino),
        )
        writable.write_bytes(b"writer")
        self.assertEqual(blob.read_bytes(), fixtures.REGION_DRAFT)
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.store.checkout("konstaxa", first["versionDigest"], copy_id)
        self.assertEqual(writable.read_bytes(), b"writer")

    def test_stale_or_corrupt_activation_never_changes_head(self):
        first = self.candidate()
        head = self.activate(first)
        copy_id = str(uuid4())
        copy = self.store.checkout("konstaxa", first["versionDigest"], copy_id)
        (Path(copy["bundle"]) / "world/region/r.0.0.mca").write_bytes(b"second")
        second = self.candidate(working_copy_id=copy_id)
        head_file = self.store.versions.active / "konstaxa/head.json"
        before = head_file.read_bytes()
        with self.assertRaisesRegex(ValueError, "stale"):
            self.store.publish(second["id"], ACTOR, expected_head=None)
        manifest = self.store.versions.read("konstaxa", second["versionDigest"])
        blob = self.store.versions._blob(manifest["files"]["world/region/r.0.0.mca"])
        original = blob.read_bytes()
        blob.chmod(0o644)
        blob.write_bytes(b"corrupted")
        with self.assertRaisesRegex(ValueError, "checksum"):
            self.store.publish(second["id"], ACTOR, expected_head=head["versionDigest"])
        self.assertEqual(head_file.read_bytes(), before)
        self.assertEqual(self.store._pending(second["id"])["status"], "pending")
        blob.write_bytes(original)
        blob.unlink()
        with self.assertRaisesRegex(ValueError, "blob missing"):
            self.store.publish(second["id"], ACTOR, expected_head=head["versionDigest"])
        self.assertEqual(head_file.read_bytes(), before)
        self.store.versions._put_blob(original)
        with mock.patch.object(
            candidates.shutil, "disk_usage", return_value=mock.Mock(free=1)
        ):
            with self.assertRaisesRegex(OSError, "free space"):
                self.store.publish(
                    second["id"], ACTOR, expected_head=head["versionDigest"]
                )
        self.assertEqual(head_file.read_bytes(), before)
        with mock.patch.object(
            candidates, "_atomic_json", side_effect=OSError("disk full")
        ):
            with self.assertRaises(OSError):
                self.store.rollback(
                    "konstaxa",
                    ACTOR,
                    version_digest=second["versionDigest"],
                    expected_head=head["versionDigest"],
                )
        self.assertEqual(head_file.read_bytes(), before)
        manifest_path = self.store.versions._manifest_path(
            "konstaxa", second["versionDigest"]
        )
        manifest_path.chmod(0o644)
        manifest_path.write_bytes(manifest_path.read_bytes() + b" ")
        with self.assertRaisesRegex(ValueError, "manifest checksum"):
            self.store.rollback(
                "konstaxa",
                ACTOR,
                version_digest=second["versionDigest"],
                expected_head=head["versionDigest"],
            )
        self.assertEqual(head_file.read_bytes(), before)
        self.assertEqual(list(head_file.parent.glob("versions/.staging-*")), [])

    def test_base_uses_head_not_latest_accepted_and_every_version_is_retained(self):
        first = self.candidate()
        first_head = self.activate(first)
        # A resumed draft tracks the live head: refresh the stale template marker.
        live_world = Path(first_head["bundle"]) / "world"
        (self.world / "blockfield-base.json").write_text(
            json.dumps({"schema": 1, "templateDigest": template_digest(live_world)})
        )
        (self.world / "region" / "r.0.0.mca").write_bytes(fixtures.region_variant(9))
        second = self.candidate()
        self.assertEqual(second["baseVersion"], first["versionDigest"])
        self.activate(second, first_head["versionDigest"])
        copy_id = str(uuid4())
        self.store.checkout("konstaxa", second["versionDigest"], copy_id)
        (
            self.store.versions.working_copy("konstaxa", copy_id)
            / "world/region/r.0.0.mca"
        ).write_bytes(fixtures.region_variant(10))
        third = self.candidate(working_copy_id=copy_id)
        self.assertEqual(third["baseVersion"], second["versionDigest"])
        for _ in range(32):
            rejected = self.candidate(working_copy_id=copy_id)
            self.store.reject(rejected["id"], ACTOR)
        self.assertEqual(len(self.store.versions.history("konstaxa")), 36)
        self.assertEqual(len(self.store.list()), 35)
        self.assertEqual(len(list(self.store.candidates.glob("*.zip"))), 0)
        self.assertEqual(
            len(list((self.store.versions.archive / "exports").glob("*.zip"))), 0
        )

    def test_all_map_kinds_share_anvil_manifest_and_blob_layout(self):
        for map_id, kind in (
            ("konstaxa", "arena"),
            ("lobby", "lobby"),
            ("limbo", "limbo"),
            ("custom_arena", "arena"),
        ):
            if map_id != "konstaxa":
                self.another_map(map_id, kind)
            meta = self.candidate(map_id)
            if kind == "limbo":
                # Limbo activation needs the stopped-proxy backend; the layout
                # check below only needs the materialized head bundle.
                head = self.store.versions.activate(
                    map_id, meta["versionDigest"], ACTOR, None
                )
            else:
                head = self.activate(meta)
            manifest = self.store.versions.read(map_id, meta["versionDigest"])
            self.assertEqual(head["kind"], kind)
            self.assertEqual(manifest["kind"], kind)
            self.assertTrue((Path(head["bundle"]) / "world/region/r.0.0.mca").is_file())
            self.assertFalse(any(name.endswith(".schem") for name in manifest["files"]))
        controller = Controller(
            self.root / "catalog-state",
            FakeBackend(),
            maps=self.store,
            releases=release_catalog_fixture(),
        )
        controller.set_members(
            [
                ACTOR | {"role": "admin"},
                {
                    "draslId": "b" * 32,
                    "name": "Builder",
                    "role": "builder",
                    "maps": ["lobby", "limbo", "custom_arena"],
                },
            ]
        )
        self.assertEqual(
            controller.members()[1]["maps"], ["lobby", "limbo", "custom_arena"]
        )
        session = controller._session_for(
            {"session": "new", "maps": ["lobby", "limbo", "custom_arena"]}, ACTOR
        )
        self.assertEqual(session["maps"], ["lobby", "limbo", "custom_arena"])

    def test_old_zip_history_is_read_without_moving_or_deleting_archives(self):
        first = self.candidate()
        old_archive = self.store.archive_path(first["id"])
        before = old_archive.read_bytes()
        old_copy = self.store.candidates / f"{first['id']}.zip"
        shutil.copyfile(old_archive, old_copy)
        legacy = next(meta for meta in self.store.list() if meta["id"] == first["id"])
        for field in ("native", "versionDigest"):
            legacy.pop(field)
        self.store._write_meta(legacy)
        relocated = MapStore(
            self.data,
            self.templates,
            self.published,
            self.root / "hdd/candidates",
            self.root / "backups",
            archive_root=self.root / "hdd/history",
            legacy_candidates=self.store.candidates,
        )
        self.assertEqual(relocated.sha256_of(first["id"]), legacy["sha256"])
        relocated.publish(first["id"], ACTOR, expected_head=None)
        self.assertEqual(old_copy.read_bytes(), before)
        self.assertEqual(relocated.list()[0]["status"], "accepted")

    def test_export_cache_regenerates_identical_zip_and_preserves_all_history(self):
        first = self.candidate()
        self.assertIsNone(first["sha256"])
        archive = self.store.archive_path(first["id"])
        before = archive.read_bytes()
        checksum = self.store.sha256_of(first["id"])
        original_id = uuid4().hex
        original = next(
            meta for meta in self.store.list() if meta["id"] == first["id"]
        ) | {"id": original_id, "native": False}
        self.store._write_meta(original)
        old_zip = self.store.candidates / f"{original_id}.zip"
        old_zip.write_bytes(before)
        self.store.packages.mkdir(parents=True)
        old_package = self.store.packages / f"{checksum}.zip"
        old_package.write_bytes(before)
        archive.unlink()
        self.clock[0] += 1
        self.assertEqual(self.store.archive_path(first["id"]).read_bytes(), before)
        self.assertEqual(self.store.sha256_of(first["id"]), checksum)
        for index in range(candidates.EXPORTS_KEPT):
            (self.world / "region" / "r.0.0.mca").write_bytes(
                fixtures.region_variant(index % 250 + 1)
            )
            meta = self.candidate()
            self.store.archive_path(meta["id"])
        self.assertFalse(archive.exists())
        self.assertEqual(
            len(list((self.store.versions.archive / "exports").glob("*.zip"))),
            candidates.EXPORTS_KEPT,
        )
        self.assertEqual(old_zip.read_bytes(), before)
        self.assertEqual(old_package.read_bytes(), before)
        self.assertEqual(
            len(self.store.versions.history("konstaxa")), candidates.EXPORTS_KEPT + 2
        )
        self.assertEqual(len(self.store.list()), candidates.EXPORTS_KEPT + 2)

    def test_external_anvil_chunks_survive_snapshot_activation_and_export(self):
        external = self.world / "region/c.0.0.mcc"
        external.write_bytes(b"external chunk" * 1024)
        with mock.patch.object(
            candidates.shutil,
            "disk_usage",
            return_value=mock.Mock(free=candidates.SNAPSHOT_MARGIN + 1024),
        ):
            with self.assertRaisesRegex(OSError, "free space"):
                self.store.check("konstaxa")
        external.write_bytes(b"external chunk")
        meta = self.candidate()
        manifest = self.store.versions.read("konstaxa", meta["versionDigest"])
        self.assertIn("world/region/c.0.0.mcc", manifest["files"])
        head = self.activate(meta)
        self.assertEqual(
            (Path(head["bundle"]) / "world/region/c.0.0.mcc").read_bytes(),
            b"external chunk",
        )
        archive = self.store.archive_path(meta["id"])
        checksum = self.store.sha256_of(meta["id"])
        self.assertIn(
            "world/region/c.0.0.mcc",
            candidates.map_package.validate(archive, checksum)["files"],
        )
        (self.world / "region/r.1.0.mcc").write_bytes(b"wrong external name")
        with self.assertRaisesRegex(ValueError, "sidecar"):
            self.candidate()

    def test_real_inbox_snapshot_accept_and_targeted_rollback_export_history(self):
        inbox, admin = self.root / "inbox", self.root / "admin"
        inbox.mkdir()
        controller = Controller(
            self.root / "state",
            FakeBackend(),
            admin_dir=admin,
            inbox_dir=inbox,
            maps=self.store,
            releases=release_catalog_fixture(),
        )
        controller.set_members([{**ACTOR, "role": "admin"}])
        controller.request(str(uuid4()), Goal("game", "v1.0.0"), 0)
        controller.step()
        controller.backend.ready(Goal(**controller.status()["desired"]))
        controller.step()

        def send(kind, **data):
            request_id = str(uuid4())
            document = {
                "schema": 1,
                "id": request_id,
                "kind": kind,
                "actor": ACTOR,
                "createdAt": int(time.time() * 1000),
                **data,
            }
            (inbox / f"{request_id}.json").write_text(json.dumps(document))
            self.assertEqual(controller.process_inbox(), 1)
            result = json.loads((admin / "results" / f"{request_id}.json").read_text())
            self.assertTrue(result["ok"], result["error"])

        send("candidate", mapId="konstaxa")
        first = self.store.list()[0]
        self.assertIsNone(first["sha256"])
        self.assertFalse((self.store.versions.archive / "exports").exists())
        send("publish", candidateId=first["id"], expectedHead=None)
        self.assertFalse((self.store.versions.archive / "exports").exists())
        send("export", candidateId=first["id"])
        exported = json.loads((admin / "candidates.json").read_text())["candidates"][0]
        self.assertTrue(exported["exportReady"])
        self.assertIsNotNone(exported["sha256"])
        send(
            "rollback",
            mapId="konstaxa",
            versionDigest=first["baseVersion"],
            expectedHead=first["versionDigest"],
        )
        catalog = json.loads((admin / "maps.json").read_text())["maps"]
        restored = self.store.versions.read(
            "konstaxa", catalog[0]["head"]["versionDigest"]
        )
        self.assertEqual(restored["parent"], first["versionDigest"])
        self.assertEqual(
            restored["files"],
            self.store.versions.read("konstaxa", first["baseVersion"])["files"],
        )
        self.assertEqual(len(catalog[0]["versions"]), 3)
        copy_id = str(uuid4())
        send(
            "checkout",
            mapId="konstaxa",
            versionDigest=catalog[0]["head"]["versionDigest"],
            workingCopyId=copy_id,
        )
        self.clock[0] += 1
        controller.backend.ready(Goal(**controller.status()["desired"]))
        controller.step()
        checked = self.store.versions.working_copy("konstaxa", copy_id)
        (checked / "world/region/r.0.0.mca").write_bytes(b"post-restore edit")
        send("candidate", mapId="konstaxa", workingCopyId=copy_id)
        self.assertEqual(self.store.list()[0]["workingCopyId"], copy_id)

    def test_export_cleanup_preserves_preexisting_packages_without_claiming_them(self):
        controller = Controller(
            self.root / "state",
            FakeBackend(),
            admin_dir=self.root / "admin",
            maps=self.store,
            releases=release_catalog_fixture(),
        )
        first = self.candidate()
        first_head = self.activate(first)
        live_world = Path(first_head["bundle"]) / "world"
        (self.world / "blockfield-base.json").write_text(
            json.dumps({"schema": 1, "templateDigest": template_digest(live_world)})
        )
        (self.world / "region" / "r.0.0.mca").write_bytes(fixtures.region_variant(11))
        original = self.store.archive_path(first["id"])
        checksum = self.store.sha256_of(first["id"])
        self.store.packages.mkdir(parents=True)
        historical = self.store.packages / f"{checksum}.zip"
        shutil.copyfile(original, historical)
        controller.export_maps()
        self.assertFalse(self.store.list()[0].get("derivedPackage", False))
        second = self.candidate()
        self.activate(second, first_head["versionDigest"])
        self.store.archive_path(second["id"])
        controller.export_maps()
        accepted = self.store.list()[0]
        created = self.store.packages / f"{accepted['sha256']}.zip"
        self.assertTrue(accepted["derivedPackage"])
        self.assertTrue(created.exists())
        for index in range(candidates.EXPORTS_KEPT):
            (self.world / "region" / "r.0.0.mca").write_bytes(
                fixtures.region_variant((index + 20) % 250 + 1)
            )
            meta = self.candidate()
            self.store.archive_path(meta["id"])
        self.assertTrue(historical.exists())
        self.assertFalse(created.exists())

    def test_cold_ssd_bundles_reclaimed_only_after_verified_backend_stop(self):
        first = self.candidate()
        self.activate(first)
        warm = Path(self.store.versions.head("konstaxa")["bundle"])
        self.store.rollback(
            "konstaxa",
            ACTOR,
            version_digest=first["baseVersion"],
            expected_head=first["versionDigest"],
        )
        current = Path(self.store.versions.head("konstaxa")["bundle"])
        copy_id = str(uuid4())
        draft = Path(
            self.store.checkout("konstaxa", first["versionDigest"], copy_id)["bundle"]
        )
        backend = FakeBackend()
        controller = Controller(
            self.root / "state",
            backend,
            maps=self.store,
            releases=release_catalog_fixture(),
        )
        controller.request(str(uuid4()), Goal("game", "v1.0.0"), 0)
        controller.step()
        backend.ready(Goal(**controller.status()["desired"]))
        controller.step()
        self.assertTrue(warm.exists())
        goal = Goal("workshop", "v1.0.0", str(uuid4()))
        controller.request(str(uuid4()), goal, 1)
        with mock.patch.object(
            self.store.versions, "reclaim_unheaded_bundles"
        ) as cleanup:
            controller.step()
        cleanup.assert_not_called()
        self.assertTrue(
            warm.exists(), "the running Game may still have captured this bundle"
        )
        backend.ready(goal)
        controller.step()
        backend.processes = [p for p in backend.processes if p.goal.profile != "game"]
        controller.request(str(uuid4()), Goal("workshop", "v1.0.0", str(uuid4())), 2)
        reclaim = self.store.versions.reclaim_unheaded_bundles

        def after_stop():
            self.assertEqual(backend.processes, [])
            reclaim()

        with mock.patch.object(
            self.store.versions, "reclaim_unheaded_bundles", side_effect=after_stop
        ):
            controller.step()
        self.assertFalse(warm.exists())
        self.assertTrue(current.exists())
        self.assertTrue(draft.exists())
        self.assertEqual(len(self.store.versions.history("konstaxa")), 3)


if __name__ == "__main__":
    unittest.main()
