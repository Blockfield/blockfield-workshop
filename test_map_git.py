"""Linear map history: single-parent commits, fast-forward publish, named snapshots."""

import hashlib
import json
import unittest
from pathlib import Path
from unittest import mock
from uuid import uuid4

import candidates
import test_candidates as fixtures

ACTOR = fixtures.ACTOR


def seed_config(map_id):
    return {
        "id": map_id,
        "name": "Base",
        "kind": "arena",
        "bounds": {"minX": 0, "maxX": 512, "minZ": 0, "maxZ": 512},
        "mode_data": {},
        "supported_modes": [],
    }


class MapLinearTests(unittest.TestCase):
    def setUp(self):
        fixtures.CandidateTests.setUp(self)
        (self.data / "blockfield/maps/konstaxa.json").write_text(
            json.dumps(seed_config("konstaxa"))
        )
        meta = self.store.create("konstaxa", ACTOR)
        self.store.publish(meta["id"], ACTOR, expected_head=None)
        self.base = meta["versionDigest"]
        self.versions = self.store.versions

    def draft(self, version=None):
        base = version or self.base
        copy_id = str(uuid4())
        copy = self.store.checkout("konstaxa", base, copy_id)
        return copy_id, Path(copy["bundle"])

    def commit(self, copy_id, head, note="Map change"):
        self.clock[0] += 1
        return self.store.commit(
            "konstaxa",
            ACTOR,
            note,
            working_copy_id=copy_id,
            expected_head=head,
        )["versionDigest"]

    def history_parents(self, map_id="konstaxa"):
        return {
            version["versionDigest"]: version.get("parent")
            for version in self.versions.history(map_id)
        }

    def assert_single_parent_history(self, map_id="konstaxa"):
        for version in self.versions.history(map_id):
            parents = version.get("parents", [version["parent"]])
            self.assertEqual(len(parents), 1, version["versionDigest"])

    def test_sequential_commits_parent_the_head_and_head_moves_only_on_publish(self):
        copy_id, bundle = self.draft()
        (bundle / "world/region/r.0.0.mca").write_bytes(b"linear one")
        first = self.commit(copy_id, self.base)
        (bundle / "world/region/r.0.0.mca").write_bytes(b"linear two")
        second = self.commit(copy_id, self.base)
        self.assertEqual(self.versions.read("konstaxa", first)["parents"], [self.base])
        self.assertEqual(self.versions.read("konstaxa", second)["parents"], [self.base])
        self.assertEqual(self.versions.head("konstaxa")["versionDigest"], self.base)
        self.assert_single_parent_history()
        review = self.store.review(
            "konstaxa", ACTOR, "", working_copy_id=copy_id, expected_head=first
        )
        self.store.publish(review["id"], ACTOR, expected_head=self.base)
        self.assertEqual(self.versions.head("konstaxa")["versionDigest"], first)

    def test_no_changes_is_refused_and_records_nothing(self):
        copy_id, _bundle = self.draft()
        before = set(self.history_parents())
        with self.assertRaisesRegex(ValueError, "no changes"):
            self.commit(copy_id, self.base)
        self.assertEqual(set(self.history_parents()), before)

    def test_stale_working_copy_fails_before_recording(self):
        copy_id, bundle = self.draft()
        other_id, other = self.draft()
        (bundle / "world/region/r.0.0.mca").write_bytes(b"fresh")
        first = self.commit(copy_id, self.base)
        review = self.store.review(
            "konstaxa", ACTOR, "", working_copy_id=copy_id, expected_head=first
        )
        self.store.publish(review["id"], ACTOR, expected_head=self.base)
        (Path(other) / "world/region/r.0.0.mca").write_bytes(b"stale")
        before = set(self.history_parents())
        with self.assertRaisesRegex(ValueError, "behind|stale"):
            self.commit(other_id, self.base)
        self.assertEqual(set(self.history_parents()), before)
        self.assertEqual(
            (Path(other) / "world/region/r.0.0.mca").read_bytes(), b"stale"
        )

    def test_forked_publish_fails_without_ever_merging(self):
        first_id, first_bundle = self.draft()
        second_id, second_bundle = self.draft()
        (first_bundle / "world/region/r.0.0.mca").write_bytes(b"first")
        (second_bundle / "world/region/r.0.0.mca").write_bytes(b"second")
        first = self.commit(first_id, self.base)
        second = self.commit(second_id, self.base)
        first_review = self.store.review(
            "konstaxa", ACTOR, "", working_copy_id=first_id, expected_head=first
        )
        second_review = self.store.review(
            "konstaxa", ACTOR, "", working_copy_id=second_id, expected_head=second
        )
        self.store.publish(first_review["id"], ACTOR, expected_head=self.base)
        with self.assertRaisesRegex(ValueError, "stale|target changed"):
            self.store.publish(second_review["id"], ACTOR, expected_head=self.base)
        for version in self.versions.history("konstaxa"):
            self.assertLessEqual(len(version.get("parents", [1])), 1)
        self.assertEqual(self.versions.head("konstaxa")["versionDigest"], first)

    def test_publish_requires_explicit_head_and_force_never_moves_a_stale_head(self):
        copy_id, bundle = self.draft()
        (bundle / "world/region/r.0.0.mca").write_bytes(b"edit")
        committed = self.commit(copy_id, self.base)
        review = self.store.review(
            "konstaxa", ACTOR, "", working_copy_id=copy_id, expected_head=committed
        )
        with self.assertRaisesRegex(ValueError, "explicit expectedHead"):
            self.store.publish(review["id"], ACTOR)
        with self.assertRaisesRegex(ValueError, "conflict choices"):
            self.store.publish(
                review["id"], ACTOR, expected_head=self.base, resolutions={}
            )
        forced = self.store.publish(
            review["id"], ACTOR, expected_head=self.base, force=True
        )
        self.assertTrue(forced["forced"])
        self.assertEqual(self.versions.head("konstaxa")["versionDigest"], committed)
        # Force waives the template-base guard, never a stale head.
        with self.assertRaisesRegex(ValueError, "stale map head"):
            self.store.publish(review["id"], ACTOR, expected_head=self.base, force=True)

    def test_review_of_head_or_stale_version_is_refused(self):
        with self.assertRaisesRegex(ValueError, "commit changes"):
            self.store.review(
                "konstaxa",
                ACTOR,
                "",
                working_copy_id=self.draft()[0],
                expected_head=self.base,
            )
        copy_id, bundle = self.draft()
        (bundle / "world/region/r.0.0.mca").write_bytes(b"edit")
        committed = self.commit(copy_id, self.base)
        review = self.store.review(
            "konstaxa", ACTOR, "", working_copy_id=copy_id, expected_head=committed
        )
        self.store.publish(review["id"], ACTOR, expected_head=self.base)
        other_id, other = self.draft(version=self.base)
        (other / "world/region/r.0.0.mca").write_bytes(b"older base")
        with self.assertRaisesRegex(ValueError, "behind|stale"):
            self.commit(other_id, self.base)

    def test_restore_appends_linear_commit_without_rewinding_head(self):
        copy_id, bundle = self.draft()
        (bundle / "world/region/r.0.0.mca").write_bytes(b"new")
        second = self.commit(copy_id, self.base)
        review = self.store.review(
            "konstaxa", ACTOR, "", working_copy_id=copy_id, expected_head=second
        )
        self.store.publish(review["id"], ACTOR, expected_head=self.base)
        restored = self.store.rollback(
            "konstaxa", ACTOR, version_digest=self.base, expected_head=second
        )
        self.assertNotEqual(restored["versionDigest"], self.base)
        manifest = self.versions.read("konstaxa", restored["versionDigest"])
        self.assertEqual(manifest["parents"], [second])
        self.assertEqual(manifest["sourceId"], self.base)
        self.assertEqual(
            manifest["files"], self.versions.read("konstaxa", self.base)["files"]
        )
        self.assert_single_parent_history()

    def test_snapshot_names_and_unsafe_paths_fail_closed(self):
        for name in ("main", "../escape", "a/b", "", "x" * 65):
            with self.assertRaises(ValueError):
                self.versions.create_snapshot("konstaxa", name, self.base, ACTOR)
        directory = self.versions.archive / "snapshots"
        directory.symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "unsafe"):
            self.versions.create_snapshot("konstaxa", "safe", self.base, ACTOR)

    def test_snapshot_roundtrip_resolves_and_delete_keeps_history(self):
        snap = self.versions.create_snapshot("konstaxa", "pre-event", self.base, ACTOR)
        self.assertEqual(snap["versionDigest"], self.base)
        self.assertEqual(
            self.versions.snapshot("konstaxa", "pre-event")["versionDigest"], self.base
        )
        # Idempotent for the same version; conflicting versions never overwrite.
        self.assertEqual(
            self.versions.create_snapshot("konstaxa", "pre-event", self.base, ACTOR),
            snap,
        )
        copy_id, bundle = self.draft()
        (bundle / "world/region/r.0.0.mca").write_bytes(b"edit")
        committed = self.commit(copy_id, self.base)
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.versions.create_snapshot("konstaxa", "pre-event", committed, ACTOR)
        self.versions.delete_snapshot("konstaxa", "pre-event")
        with self.assertRaisesRegex(ValueError, "unknown map snapshot"):
            self.versions.snapshot("konstaxa", "pre-event")
        self.assertIn(self.base, self.history_parents())

    def test_review_write_failure_changes_nothing(self):
        copy_id, bundle = self.draft()
        (bundle / "world/region/r.0.0.mca").write_bytes(b"committed")
        committed = self.commit(copy_id, self.base)
        (bundle / "world/region/r.0.0.mca").write_bytes(b"dirty after commit")
        before = set(self.history_parents())
        with mock.patch.object(
            self.store, "_write_meta", side_effect=OSError("disk full")
        ):
            with self.assertRaisesRegex(OSError, "disk full"):
                self.store.review(
                    "konstaxa",
                    ACTOR,
                    "",
                    working_copy_id=copy_id,
                    expected_head=committed,
                )
        self.assertEqual(set(self.history_parents()), before)
        self.assertEqual(self.versions.head("konstaxa")["versionDigest"], self.base)
        self.assertEqual(
            (bundle / "world/region/r.0.0.mca").read_bytes(), b"dirty after commit"
        )

    def test_session_binding_has_no_branches_and_rebind_needs_same_maps(self):
        session_id = str(uuid4())
        self.store.prepare_session(session_id, ["konstaxa"], actor=ACTOR)
        session = self.store.session(session_id)
        self.assertNotIn("branches", session)
        # Same maps rebind to the same session; different maps are refused.
        self.assertEqual(
            self.store.bind_session(session_id, ["konstaxa"], ACTOR)["id"], session_id
        )
        with self.assertRaisesRegex(ValueError, "already bound"):
            self.store.bind_session(session_id, ["other"], ACTOR)
        # Legacy branch selections are validated for shape, then ignored.
        self.store.prepare_session(
            session_id,
            ["konstaxa"],
            branches={"konstaxa": "work-old"},
            actor=ACTOR,
        )
        with self.assertRaisesRegex(ValueError, "must match"):
            self.store.prepare_session(
                session_id, ["konstaxa"], branches={"other": "x"}, actor=ACTOR
            )

    def test_failed_session_checkout_archives_nothing(self):
        session_id = str(uuid4())
        with mock.patch.object(
            self.store, "checkout", side_effect=OSError("disk full")
        ):
            with self.assertRaisesRegex(OSError, "disk full"):
                self.store.prepare_session(session_id, ["konstaxa"], actor=ACTOR)
        self.assertIsNone(self.store.session(session_id))
        self.assertFalse(self.versions.working_copy("konstaxa", session_id).exists())

    def test_archive_sync_failure_keeps_complete_copy_and_session(self):
        session_id = str(uuid4())
        real_sync = candidates._sync_directory

        def fail_after_archive_rename(path):
            if path == self.store._session_path(session_id).parent:
                raise OSError("archive sync failed")
            return real_sync(path)

        with mock.patch.object(
            candidates, "_sync_directory", side_effect=fail_after_archive_rename
        ):
            with self.assertRaisesRegex(OSError, "archive sync failed"):
                self.store.prepare_session(session_id, ["konstaxa"], actor=ACTOR)
        # The rename won before the failed fsync: the session ref and the
        # complete working copy survive; a retry preserves them.
        self.assertEqual(self.store.session(session_id)["maps"], ["konstaxa"])
        self.versions.validate_working_copy("konstaxa", session_id)

    def test_permission_repair_skips_symlinks_and_settled_files(self):
        copy_id, bundle = self.draft()
        outside = self.data / "outside-secret"
        outside.write_text("x")
        outside.chmod(0o600)
        (bundle / "world/level.dat").symlink_to(outside)
        (bundle / "world/dangling").symlink_to(self.data / "missing")
        region = bundle / "world/region/r.0.0.mca"
        region.chmod(0o644)
        import os

        owner = (os.getuid(), os.getgid())
        with mock.patch.object(candidates, "OWNER", owner):
            self.versions.validate_working_copy("konstaxa", copy_id)
            self.assertEqual(outside.stat().st_mode & 0o777, 0o600)
            self.assertEqual(region.stat().st_mode & 0o777, 0o664)
            with mock.patch("os.fchmod") as fchmod, mock.patch("os.fchown") as fchown:
                self.versions.validate_working_copy("konstaxa", copy_id)
            fchmod.assert_not_called()
            fchown.assert_not_called()

    # -- migration: prod-shaped fixture (24 branches incl. one silent fork) --

    def _record_version(self, map_id, payload, parent):
        files = {
            "map.json": self.versions._put_blob(json.dumps(payload).encode()),
            "world/region/r.0.0.mca": self.versions._put_blob(
                hashlib.sha256(payload["id"].encode()).digest()
            ),
        }
        return self.versions._record(
            map_id,
            files,
            parent=parent,
            author=ACTOR,
            created_at=1000,
            source="created" if parent is None else "commit",
            source_id=None,
            note="fixture",
            parents=[parent] if parent else [],
        )

    def _write_branch_ref(self, map_id, name, version, generation=1):
        path = self.versions.archive / "branches" / map_id / f"{name}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "schema": 1,
                    "mapId": map_id,
                    "name": name,
                    "versionDigest": version,
                    "generation": generation,
                    "workingCopyId": None,
                    "createdAt": 1000,
                    "createdBy": ACTOR,
                }
            )
        )

    def _prod_shaped_archive(self):
        """24 maps like prod: 21 untouched, one silent fork (train), one
        linear published (awp_ice), one unpublished tip (snipers_towers)."""
        import shutil

        shutil.rmtree(self.versions.archive / "versions", ignore_errors=True)
        shutil.rmtree(self.versions.archive / "branches", ignore_errors=True)
        (self.versions.archive / "snapshots").mkdir(parents=True, exist_ok=True)
        heads, tips = {}, {}

        def make_map(map_id):
            payload = dict(
                seed_config(map_id),
                bounds={"minX": 0, "maxX": 100, "minZ": 0, "maxZ": 100},
                mode_data={},
            )
            return self._record_version(map_id, payload, None)

        for index in range(21):
            map_id = f"quiet{index:02d}"
            root = make_map(map_id)
            self.versions.activate(map_id, root, ACTOR, None)
            self._write_branch_ref(map_id, f"work-{map_id}", root)
            heads[map_id] = root
        # train: import root, branch tip and head are different children (fork).
        train_root = make_map("train")
        train_tip = self._record_version(
            "train", dict(seed_config("train"), name="Fork"), train_root
        )
        train_head = self._record_version(
            "train", dict(seed_config("train"), name="Head"), train_root
        )
        self.versions.activate("train", train_head, ACTOR, None)
        self._write_branch_ref("train", "work-train", train_tip, generation=2)
        heads["train"], tips["train"] = train_head, train_tip
        # awp_ice: linear, head == branch tip.
        awp_root = make_map("awp_ice")
        awp_mid = self._record_version(
            "awp_ice", dict(seed_config("awp_ice"), name="Mid"), awp_root
        )
        awp_head = self._record_version(
            "awp_ice", dict(seed_config("awp_ice"), name="Top"), awp_mid
        )
        self.versions.activate("awp_ice", awp_head, ACTOR, None)
        self._write_branch_ref("awp_ice", "work-awp", awp_head, generation=3)
        heads["awp_ice"] = awp_head
        # snipers_towers: unpublished tip above the head.
        snip_root = make_map("snipers_towers")
        self.versions.activate("snipers_towers", snip_root, ACTOR, None)
        snip_tip = self._record_version(
            "snipers_towers", dict(seed_config("snipers_towers"), name="Tip"), snip_root
        )
        self._write_branch_ref("snipers_towers", "work-snip", snip_tip, generation=2)
        heads["snipers_towers"], tips["snipers_towers"] = snip_root, snip_tip
        return heads, tips

    def _archive_counts(self):
        versions = list((self.versions.archive / "versions").rglob("*.json"))
        blobs = list((self.versions.archive / "blobs").rglob("*"))
        blobs = [path for path in blobs if path.is_file()]
        return len(versions), len(blobs)

    def test_migration_preserves_every_version_and_resolves_every_snapshot(self):
        heads, tips = self._prod_shaped_archive()
        branch_files = list((self.versions.archive / "branches").rglob("*.json"))
        self.assertEqual(len(branch_files), 24)
        versions_before, blobs_before = self._archive_counts()
        digests_before = {
            path.stem for path in (self.versions.archive / "versions").rglob("*.json")
        }
        summary = self.versions.migrate_branches_to_snapshots()
        self.assertEqual(
            {(item["mapId"], item["name"]) for item in summary["migrated"]},
            {("train", "work-train"), ("snipers_towers", "work-snip")},
        )
        self.assertEqual(len(summary["atHead"]), 22)
        # Zero version/blob loss.
        self.assertEqual(self._archive_counts(), (versions_before, blobs_before))
        self.assertEqual(
            {
                path.stem
                for path in (self.versions.archive / "versions").rglob("*.json")
            },
            digests_before,
        )
        # Every snapshot pointer resolves; heads untouched (incl. the fork head).
        expected_snaps = {"train": "work-train", "snipers_towers": "work-snip"}
        for map_id, tip in tips.items():
            snap = self.versions.snapshot(map_id, expected_snaps[map_id])
            self.assertEqual(snap["versionDigest"], tip)
            self.assertEqual(self.versions.read(map_id, tip)["mapId"], map_id)
        for map_id, head in heads.items():
            self.assertEqual(self.versions.head(map_id)["versionDigest"], head)
        self.assertFalse((self.versions.archive / "branches").exists())
        # Second run is a no-op.
        again = self.versions.migrate_branches_to_snapshots()
        self.assertEqual(again, {"migrated": [], "atHead": []})
        self.assertEqual(self._archive_counts(), (versions_before, blobs_before))

    def test_live_legacy_session_keeps_working_after_migration(self):
        heads, _tips = self._prod_shaped_archive()
        session_id = str(uuid4())
        legacy_branches = {"train": "work-train"}
        self.store.checkout("train", heads["train"], session_id)
        session_path = self.store._session_path(session_id)
        session_path.parent.mkdir(parents=True, exist_ok=True)
        value = {
            "schema": 1,
            "id": session_id,
            "maps": ["train"],
            "branches": legacy_branches,
            "startedAt": 1000,
            "startedBy": ACTOR,
        }
        value["selectionDigest"] = self.store._selection_digest(value)
        session_path.write_text(json.dumps(value))
        self.versions.migrate_branches_to_snapshots()
        # The legacy session still reads, resumes, and commits to the head.
        self.assertEqual(self.store.session(session_id)["maps"], ["train"])
        copies = self.store.prepare_session(
            session_id, ["train"], branches=legacy_branches, actor=ACTOR
        )
        self.assertTrue(copies)
        region = (
            self.versions.working_copy("train", session_id) / "world/region/r.0.0.mca"
        )
        region.write_bytes(b"live work preserved")
        committed = self.store.commit(
            "train",
            ACTOR,
            "Live edit",
            working_copy_id=session_id,
            expected_head=heads["train"],
        )["versionDigest"]
        self.assertEqual(
            self.versions.read("train", committed)["parents"], [heads["train"]]
        )


if __name__ == "__main__":
    unittest.main()
