"""Limbo derivatives bind one accepted Anvil version; activation never controls the proxy."""

from release_fixtures import catalog

import hashlib
import json
import unittest
from unittest import mock
from uuid import uuid4

from limbo import LimboArtifacts, metadata
from controller import Controller, Goal, Process
from test_controller import FakeBackend
import test_candidates as fixtures

ACTOR = fixtures.ACTOR


class LimboTests(unittest.TestCase):
    setUp = fixtures.CandidateTests.setUp

    def version(self, suffix="first"):
        world = self.root / suffix
        (world / "region").mkdir(parents=True)
        (world / "region/r.0.0.mca").write_bytes(suffix.encode())
        config = self.root / f"{suffix}.json"
        config.write_text(json.dumps(self.config()))
        return self.store.versions.import_template("limbo", world, config)

    @staticmethod
    def config():
        return {
            "id": "limbo",
            "kind": "limbo",
            "name": "Limbo",
            "supported_modes": [],
            "bounds": {"minX": 0, "maxX": 2, "minZ": 0, "maxZ": 2},
            "mode_data": {
                "limbo": {
                    "width": 2,
                    "height": 1,
                    "length": 2,
                    "spawn": {"x": 0.5, "y": 0, "z": 0.5},
                }
            },
        }

    def adapter(self, stopped=lambda: True):
        self.store.limbo = LimboArtifacts(self.root / "derived", stopped)
        return self.store.limbo

    def job(self, adapter, version, data=b"validated by the native WorldEdit producer"):
        job, directory = adapter.prepare_request(self.store.versions, version)
        request = json.loads((directory / "request.json").read_bytes())
        result = {
            **request,
            "schematicSha256": hashlib.sha256(data).hexdigest(),
            "size": len(data),
        }
        (directory / "lobby.schem").write_bytes(data)
        (directory / "result.json").write_text(json.dumps(result))
        return job, directory

    def install(self, adapter, version):
        job, _ = self.job(adapter, version)
        return adapter.install_job(self.store.versions, version, job)

    def test_failed_install_keeps_the_job_for_forensics(self):
        version = self.version()
        adapter = self.adapter()
        job, directory = self.job(adapter, version)
        (directory / "lobby.schem").write_bytes(b"tampered after the native result")
        with self.assertRaisesRegex(ValueError, "checksum/size mismatch"):
            adapter.install_job(self.store.versions, version, job)
        self.assertTrue(directory.is_dir())
        self.assertIsNone(self.store.versions.head("limbo"))

    def test_independent_immutable_derivative_and_stopped_selection(self):
        version = self.version()
        adapter = self.adapter()
        job, directory = self.job(adapter, version)
        selection = adapter.install_job(self.store.versions, version, job)
        # A successful install reaps the job: request/result/schematic must not
        # accumulate, and the installed derivative stands on its own.
        self.assertFalse(directory.exists())
        artifact = adapter.artifacts / selection["artifact"]
        self.assertEqual(artifact.stat().st_mode & 0o777, 0o444)
        self.assertEqual(adapter.ready(self.store.versions, version), selection)
        head = self.store.activate("limbo", version, ACTOR, None)
        target = self.store.versions.active / "limbo/selection.json"
        self.assertEqual(json.loads(target.read_bytes()), selection)
        second = self.version("second")
        self.install(adapter, second)
        self.store.activate("limbo", second, ACTOR, version)
        rolled = self.store.rollback(
            "limbo", ACTOR, version_digest=version, expected_head=second
        )
        restored = rolled["versionDigest"]
        self.assertNotEqual(restored, version)
        manifest = self.store.versions.read("limbo", restored)
        self.assertEqual(manifest["parents"], [second])
        self.assertEqual(
            manifest["files"], self.store.versions.read("limbo", version)["files"]
        )
        self.assertGreater(rolled["generation"], head["generation"])
        restored_selection = json.loads(target.read_bytes())
        self.assertEqual(restored_selection["versionDigest"], restored)
        self.assertEqual(
            restored_selection["schematicSha256"], selection["schematicSha256"]
        )
        self.assertEqual(adapter.ready(self.store.versions, version), selection)

    def test_missing_corrupt_stale_or_running_proxy_never_changes_head(self):
        version = self.version()
        with self.assertRaisesRegex(ValueError, "configured"):
            self.store.activate("limbo", version, ACTOR, None)
        adapter = self.adapter()
        with self.assertRaisesRegex(ValueError, "missing"):
            self.store.activate("limbo", version, ACTOR, None)
        selected = self.install(adapter, version)
        head = self.store.activate("limbo", version, ACTOR, None)
        head_path = self.store.versions.active / "limbo/head.json"
        selection_path = head_path.with_name("selection.json")
        before = head_path.read_bytes(), selection_path.read_bytes()
        second = self.version("second")
        next_selection = self.install(adapter, second)
        with self.assertRaisesRegex(ValueError, "stale"):
            self.store.activate("limbo", second, ACTOR, None)
        adapter.proxy_stopped = lambda: False
        with self.assertRaisesRegex(ValueError, "stopped"):
            self.store.activate("limbo", second, ACTOR, head["versionDigest"])
        adapter.proxy_stopped = lambda: True
        artifact = adapter.artifacts / next_selection["artifact"]
        artifact.chmod(0o644)
        artifact.write_bytes(b"corrupt")
        with self.assertRaisesRegex(ValueError, "corrupt"):
            self.store.activate("limbo", second, ACTOR, version)
        self.assertEqual((head_path.read_bytes(), selection_path.read_bytes()), before)
        self.assertEqual(adapter.ready(self.store.versions, version), selected)

    def test_failed_head_commit_or_proxy_race_restores_exact_previous_selection(self):
        first, second = self.version(), self.version("second")
        adapter = self.adapter()
        self.install(adapter, first)
        self.install(adapter, second)
        self.store.activate("limbo", first, ACTOR, None)
        selection = self.store.versions.active / "limbo/selection.json"
        before = selection.read_bytes()
        with mock.patch.object(
            self.store.versions, "activate", side_effect=OSError("disk full")
        ):
            with self.assertRaisesRegex(OSError, "disk full"):
                self.store.activate("limbo", second, ACTOR, first)
        self.assertEqual(selection.read_bytes(), before)
        adapter.proxy_stopped = mock.Mock(side_effect=[True, False])
        with self.assertRaisesRegex(ValueError, "started"):
            self.store.activate("limbo", second, ACTOR, first)
        self.assertEqual(selection.read_bytes(), before)
        self.assertEqual(self.store.versions.head("limbo")["versionDigest"], first)

    def test_wrong_native_job_provenance_or_symlinks_are_rejected(self):
        first, second = self.version(), self.version("second")
        adapter = self.adapter()
        job, directory = self.job(adapter, first)
        with self.assertRaisesRegex(ValueError, "source version"):
            adapter.install_job(self.store.versions, second, job)
        result_file = directory / "result.json"
        result = json.loads(result_file.read_bytes())
        result["schema"] = True
        result_file.write_text(json.dumps(result))
        with self.assertRaisesRegex(ValueError, "source version"):
            adapter.install_job(self.store.versions, first, job)
        result["schema"] = 1
        result_file.write_text(json.dumps(result))
        data = directory / "lobby.schem"
        data.rename(directory / "kept.schem")
        data.symlink_to(directory / "kept.schem")
        with self.assertRaisesRegex(ValueError, "unsafe"):
            adapter.install_job(self.store.versions, first, job)
        self.assertFalse((adapter.artifacts / f"{first}.json").exists())
        self.assertIsNone(self.store.versions.head("limbo"))

    def test_redirected_artifact_directory_is_rejected_before_any_write(self):
        version = self.version()
        adapter = self.adapter()
        job, _ = self.job(adapter, version)
        outside = self.root / "outside"
        outside.mkdir()
        adapter.artifacts.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "unsafe"):
            adapter.install_job(self.store.versions, version, job)
        self.assertEqual(list(outside.iterdir()), [])
        self.assertIsNone(self.store.versions.head("limbo"))

    def test_export_is_cached_only_after_verified_native_result(self):
        version = self.version()
        adapter = self.adapter()
        backend = mock.Mock()

        def export(job):
            directory = adapter.jobs / job
            request = json.loads((directory / "request.json").read_bytes())
            data = b"verified native export"
            (directory / "lobby.schem").write_bytes(data)
            (directory / "result.json").write_text(
                json.dumps(
                    {
                        **request,
                        "size": len(data),
                        "schematicSha256": hashlib.sha256(data).hexdigest(),
                    }
                )
            )

        backend.export_limbo.side_effect = export
        selected = self.store.export_limbo(version, backend)
        self.assertEqual(self.store.export_limbo(version, backend), selected)
        backend.export_limbo.assert_called_once()
        stamp = adapter.artifacts / f"{version}.json"
        stamp.chmod(0o644)
        stamp.write_text("{}")
        with self.assertRaisesRegex(ValueError, "source config"):
            self.store.export_limbo(version, backend)
        backend.export_limbo.assert_called_once()

    def test_limbo_metadata_rejects_unrepresentable_dimensions_and_spawn(self):
        config = self.config()
        self.assertEqual(metadata(config)["spawn"]["yaw"], 0)
        for key, value in (("width", True), ("height", 257), ("length", "2")):
            broken = self.config()
            broken["mode_data"]["limbo"][key] = value
            with self.assertRaises(ValueError):
                metadata(broken)
        for key, value in (("x", 2), ("y", -1), ("z", float("nan")), ("pitch", 91)):
            broken = self.config()
            broken["mode_data"]["limbo"]["spawn"][key] = value
            with self.assertRaises(ValueError):
                metadata(broken)

    def test_controller_inbox_export_requires_one_ready_workshop_without_profile_switch(
        self,
    ):
        import time

        version = self.version()
        adapter = self.adapter()
        backend = FakeBackend()
        control = Controller(
            self.root / "state",
            backend,
            maps=self.store,
            admin_dir=self.root / "admin",
            inbox_dir=self.root / "inbox",
            releases=catalog(),
        )
        with self.assertRaisesRegex(ValueError, "ready workshop"):
            control.export_limbo(version)
        goal = Goal("workshop", "v1.0.0", str(uuid4()))
        backend.processes = [Process(goal, True)]
        control.request(str(uuid4()), goal, 0)
        control.step()
        self.assertEqual(control.status()["phase"], "workshop")
        export = mock.Mock()

        def produce(job):
            directory = adapter.jobs / job
            request = json.loads((directory / "request.json").read_bytes())
            data = b"native console result"
            (directory / "lobby.schem").write_bytes(data)
            (directory / "result.json").write_text(
                json.dumps(
                    {
                        **request,
                        "size": len(data),
                        "schematicSha256": hashlib.sha256(data).hexdigest(),
                    }
                )
            )

        export.side_effect = produce
        backend.export_limbo = export
        control.set_members([ACTOR | {"role": "admin"}])
        request_id = str(uuid4())
        control.inbox_dir.mkdir()
        (control.inbox_dir / f"{request_id}.json").write_text(
            json.dumps(
                {
                    "schema": 1,
                    "id": request_id,
                    "createdAt": int(time.time() * 1000),
                    "actor": ACTOR,
                    "kind": "limbo-export",
                    "versionDigest": version,
                }
            )
        )
        self.assertEqual(control.process_inbox(), 1)
        result = json.loads(
            (control.admin_dir / "results" / f"{request_id}.json").read_bytes()
        )
        self.assertTrue(result["ok"], result)
        export.assert_called_once()
        self.assertEqual(backend.events, [])
        self.assertIsNone(self.store.versions.head("limbo"))
        adapter.ready(self.store.versions, version)
        backend.processes = [Process(goal, False)]
        with self.assertRaisesRegex(ValueError, "ready workshop"):
            control.export_limbo(version)


if __name__ == "__main__":
    unittest.main()
