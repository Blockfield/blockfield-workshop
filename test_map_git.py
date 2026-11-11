"""Linear map history: single-parent commits, fast-forward publish, named snapshots."""

import hashlib
import io
import json
import unittest
from contextlib import contextmanager, redirect_stderr
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

    def seed_draft_map(self, map_id="fresh"):
        world = self.data / "world" / "dimensions" / "blockfield_workshop" / map_id
        (world / "region").mkdir(parents=True)
        (world / "region" / "r.0.0.mca").write_bytes(b"fresh-region")
        (self.data / "blockfield/maps" / f"{map_id}.json").write_text(
            json.dumps(seed_config(map_id))
        )
        template = self.templates / map_id
        (template / "region").mkdir(parents=True)
        (template / "region" / "r.0.0.mca").write_bytes(b"fresh-template-region")
        (world / "blockfield-base.json").write_text(
            json.dumps(
                {
                    "schema": 1,
                    "templateDigest": candidates.template_digest(template),
                }
            )
        )
        return world

    def test_explicit_null_head_is_checked_against_the_current_head(self):
        # A caller that saw a headless map passes expectedHead null: a
        # concurrent first publish must fail it, never be silently adopted.
        world = self.seed_draft_map()
        first = self.store.create("fresh", ACTOR, expected_head=None)
        self.store.publish(first["id"], ACTOR, expected_head=None)
        head = self.versions.head("fresh")["versionDigest"]
        (world / "region" / "r.0.0.mca").write_bytes(b"concurrent edit")
        with self.assertRaisesRegex(ValueError, "stale"):
            self.store.create("fresh", ACTOR, expected_head=None)
        self.assertEqual(self.versions.head("fresh")["versionDigest"], head)

    def test_interrupted_first_publish_retries_to_converge(self):
        # r4524665112: a first publish (expectedHead null) that activated but
        # stopped before returning must recover on retry, not fail as stale.
        self.seed_draft_map()
        first = self.store.create("fresh", ACTOR, expected_head=None)
        self.store.publish(first["id"], ACTOR, expected_head=None)
        head = self.versions.head("fresh")["versionDigest"]
        again = self.store.publish(first["id"], ACTOR, expected_head=None)
        self.assertEqual(again["status"], "accepted")
        self.assertEqual(self.versions.head("fresh")["versionDigest"], head)

    def test_session_publish_then_keep_editing_then_publish_again(self):
        # Core flow: the publishing copy advances onto the new head with its
        # pending-review edits intact, so the same session can publish again.
        copy_id, bundle = self.draft()
        (bundle / "world/region/r.0.0.mca").write_bytes(b"first edit")
        first = self.commit(copy_id, self.base)
        review = self.store.review(
            "konstaxa", ACTOR, "", working_copy_id=copy_id, expected_head=first
        )
        (bundle / "world/region/r.0.0.mca").write_bytes(b"edit while review pending")
        published = self.store.publish(review["id"], ACTOR, expected_head=self.base)
        self.assertTrue(published["copyAdvanced"])
        self.assertEqual(self.versions.head("konstaxa")["versionDigest"], first)
        self.assertEqual(
            self.versions.validate_working_copy("konstaxa", copy_id)["versionDigest"],
            first,
        )
        self.assertEqual(
            (bundle / "world/region/r.0.0.mca").read_bytes(),
            b"edit while review pending",
        )
        (bundle / "world/region/r.0.0.mca").write_bytes(b"second edit")
        second = self.commit(copy_id, first)
        self.assertEqual(self.versions.read("konstaxa", second)["parents"], [first])
        review2 = self.store.review(
            "konstaxa", ACTOR, "", working_copy_id=copy_id, expected_head=second
        )
        self.store.publish(review2["id"], ACTOR, expected_head=first)
        self.assertEqual(self.versions.head("konstaxa")["versionDigest"], second)
        self.assertEqual(
            (bundle / "world/region/r.0.0.mca").read_bytes(), b"second edit"
        )

    def test_legacy_candidate_without_target_head_publishes(self):
        # Pre-upgrade records carry baseVersion but no targetHead: publishable
        # when the immutable version directly parents the current head.
        copy_id, bundle = self.draft()
        (bundle / "world/region/r.0.0.mca").write_bytes(b"legacy edit")
        committed = self.commit(copy_id, self.base)
        review = self.store.review(
            "konstaxa", ACTOR, "", working_copy_id=copy_id, expected_head=committed
        )
        meta_path = self.store.candidates / f"{review['id']}.json"
        meta = json.loads(meta_path.read_text())
        del meta["targetHead"]
        meta_path.write_text(json.dumps(meta))
        self.store.publish(review["id"], ACTOR, expected_head=self.base)
        self.assertEqual(self.versions.head("konstaxa")["versionDigest"], committed)

    # -- round 2: publish is a transaction, one atomic base record, migrated copies --

    def test_publish_stage_or_activate_failure_leaves_copy_at_old_base(self):
        # r4523970909: the copy advances only after the head is activated, so a
        # failed stage/activation leaves the copy at its old base and the head
        # unchanged (another review can still publish against it).
        copy_id, bundle = self.draft()
        (bundle / "world/region/r.0.0.mca").write_bytes(b"first edit")
        first = self.commit(copy_id, self.base)
        review = self.store.review(
            "konstaxa", ACTOR, "", working_copy_id=copy_id, expected_head=first
        )
        for step in ("prepare", "activate"):
            with mock.patch.object(
                self.versions, step, side_effect=OSError(f"{step} down")
            ):
                with self.assertRaisesRegex(OSError, f"{step} down"):
                    self.store.publish(review["id"], ACTOR, expected_head=self.base)
            self.assertEqual(self.versions.head("konstaxa")["versionDigest"], self.base)
            self.assertEqual(
                self.versions.validate_working_copy("konstaxa", copy_id)[
                    "versionDigest"
                ],
                self.base,
            )
        # The unchanged head still accepts a competing review; the failed
        # copy keeps its world edits at the old base.
        other_id, other = self.draft()
        (other / "world/region/r.0.0.mca").write_bytes(b"second edit")
        other_version = self.commit(other_id, self.base)
        other_review = self.store.review(
            "konstaxa", ACTOR, "", working_copy_id=other_id, expected_head=other_version
        )
        self.store.publish(other_review["id"], ACTOR, expected_head=self.base)
        self.assertEqual(self.versions.head("konstaxa")["versionDigest"], other_version)
        self.assertEqual(
            (bundle / "world/region/r.0.0.mca").read_bytes(), b"first edit"
        )

    def test_crash_between_base_writes_recovers_on_read(self):
        # r4523970919: manifest.json is the authoritative base; a torn marker
        # write heals on the next read instead of stranding the copy.
        copy_id, bundle = self.draft()
        (bundle / "world/region/r.0.0.mca").write_bytes(b"edit")
        committed = self.commit(copy_id, self.base)
        real_atomic = candidates._atomic_json

        def torn_write(path, value, **kwargs):
            if path.name == "blockfield-base.json":
                raise OSError("power cut between base writes")
            return real_atomic(path, value, **kwargs)

        with mock.patch.object(candidates, "_atomic_json", side_effect=torn_write):
            with self.assertRaisesRegex(OSError, "power cut"):
                self.versions.advance_working_copy("konstaxa", copy_id, committed)
        # Manifest moved, marker did not: the next read repairs the marker.
        self.assertEqual(
            self.versions.validate_working_copy("konstaxa", copy_id)["versionDigest"],
            committed,
        )
        marker = json.loads((bundle / "world" / "blockfield-base.json").read_text())
        self.assertEqual(marker["versionDigest"], committed)

    def test_stale_copy_commit_refused_without_edits(self):
        # Round 3: no automatic rebase. A copy whose base is not the head is
        # stale: committing from it is refused with one clear message and the
        # draft on disk is left untouched.
        first_id, first_bundle = self.draft()
        second_id, second_bundle = self.draft()
        (first_bundle / "world/region/r.0.0.mca").write_bytes(b"first")
        first = self.commit(first_id, self.base)
        first_review = self.store.review(
            "konstaxa", ACTOR, "", working_copy_id=first_id, expected_head=first
        )
        self.store.publish(first_review["id"], ACTOR, expected_head=self.base)
        (second_bundle / "world/region/r.0.0.mca").write_bytes(b"stale edit")
        before = set(self.history_parents())
        with self.assertRaisesRegex(ValueError, "newer version"):
            self.store.commit(
                "konstaxa", ACTOR, "stale attempt", working_copy_id=second_id
            )
        with self.assertRaisesRegex(ValueError, "newer version"):
            self.store.commit(
                "konstaxa",
                ACTOR,
                "stale attempt",
                working_copy_id=second_id,
                expected_head=first,
            )
        self.assertEqual(set(self.history_parents()), before)
        self.assertEqual(
            (second_bundle / "world/region/r.0.0.mca").read_bytes(), b"stale edit"
        )
        self.assertEqual(self.versions.head("konstaxa")["versionDigest"], first)

    def test_stale_copy_refusal_carries_machine_readable_code(self):
        # Coord#31: the site must not match the message text. The refusal
        # keeps its exact wording but carries a machine-readable code.
        from candidates import STALE_COPY_MESSAGE, StaleWorkingCopyError

        first_id, first_bundle = self.draft()
        second_id, second_bundle = self.draft()
        (first_bundle / "world/region/r.0.0.mca").write_bytes(b"first")
        first = self.commit(first_id, self.base)
        first_review = self.store.review(
            "konstaxa", ACTOR, "", working_copy_id=first_id, expected_head=first
        )
        self.store.publish(first_review["id"], ACTOR, expected_head=self.base)
        (second_bundle / "world/region/r.0.0.mca").write_bytes(b"stale edit")
        with self.assertRaises(StaleWorkingCopyError) as ctx:
            self.store.commit(
                "konstaxa",
                ACTOR,
                "stale attempt",
                working_copy_id=second_id,
                expected_head=first,
            )
        self.assertEqual(str(ctx.exception), STALE_COPY_MESSAGE)
        self.assertEqual(ctx.exception.code, "stale_working_copy")
        self.assertIsInstance(ctx.exception, ValueError)
        # The no-expectation path refuses the same way.
        with self.assertRaises(StaleWorkingCopyError):
            self.store.commit(
                "konstaxa", ACTOR, "stale attempt", working_copy_id=second_id
            )

    def test_stale_copy_snapshot_and_fresh_copy_after_hold(self):
        # Round 3 way out: an explicit snapshot preserves the stale draft and
        # a fresh copy starts from the current head. Both read the draft only
        # after hold_saves() flushed and paused the editor.
        first_id, first_bundle = self.draft()
        second_id, second_bundle = self.draft()
        (first_bundle / "world/region/r.0.0.mca").write_bytes(b"sibling change")
        first = self.commit(first_id, self.base)
        first_review = self.store.review(
            "konstaxa", ACTOR, "", working_copy_id=first_id, expected_head=first
        )
        self.store.publish(first_review["id"], ACTOR, expected_head=self.base)
        (second_bundle / "world/region/r.0.0.mca").write_bytes(b"stale draft edit")
        events = []
        real_hold, real_map_json = self.store.hold_saves, self.store._map_json

        @contextmanager
        def hold():
            events.append("hold-enter")
            with real_hold():
                yield
            events.append("hold-exit")

        def spy_map_json(map_id, working_copy_id=None):
            events.append("draft-read")
            return real_map_json(map_id, working_copy_id)

        with (
            mock.patch.object(self.store, "hold_saves", hold),
            mock.patch.object(self.store, "_map_json", side_effect=spy_map_json),
        ):
            snap = self.store.snapshot_working_copy(
                "konstaxa", "stale-draft", second_id, ACTOR
            )
        self.assertEqual(events, ["hold-enter", "draft-read", "hold-exit"])
        preserved = self.versions.read("konstaxa", snap["versionDigest"])
        self.assertEqual(
            self.versions._verify_blob(
                preserved["files"]["world/region/r.0.0.mca"]
            ).read_bytes(),
            b"stale draft edit",
        )
        # The stale copy itself is untouched: still at its old base with edits.
        self.assertEqual(
            self.versions.validate_working_copy("konstaxa", second_id)["versionDigest"],
            self.base,
        )
        self.assertEqual(
            (second_bundle / "world/region/r.0.0.mca").read_bytes(),
            b"stale draft edit",
        )
        # The way back in: a fresh copy from the current head, then publish.
        fresh_id = str(uuid4())
        with mock.patch.object(self.store, "hold_saves", hold):
            fresh = self.store.checkout_fresh_copy("konstaxa", fresh_id, ACTOR)
            self.assertEqual(events[-2:], ["hold-enter", "hold-exit"])
        self.assertEqual(fresh["versionDigest"], first)
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.store.checkout_fresh_copy("konstaxa", second_id, ACTOR)
        fresh_bundle = Path(fresh["bundle"])
        (fresh_bundle / "world/region/r.0.0.mca").write_bytes(b"rebuilt on head")
        committed = self.commit(fresh_id, first)
        review = self.store.review(
            "konstaxa", ACTOR, "", working_copy_id=fresh_id, expected_head=committed
        )
        published = self.store.publish(review["id"], ACTOR, expected_head=first)
        self.assertTrue(published["copyAdvanced"])
        self.assertEqual(self.versions.head("konstaxa")["versionDigest"], committed)

    def _publish_branch_tip(self, copy_id, bundle, note="Branch tip"):
        config = json.loads((bundle / "map.json").read_text())
        files = self.versions.world_files(bundle / "world", json.dumps(config).encode())
        tip = self.versions.commit_head(
            "konstaxa",
            files,
            ACTOR,
            note,
            self.base,
            copy_id,
            self.base,
            source="commit",
        )
        self.versions.activate("konstaxa", tip, ACTOR, self.base)
        return tip

    def test_migration_snapshots_published_branch_draft(self):
        # Round 3: a migrated copy whose legacy branch was published is stale
        # by rule 1. Migration preserves its draft as a named snapshot —
        # no rebase, no content loss, copy and head untouched.
        copy_id, bundle = self.draft()
        (bundle / "world/region/r.0.0.mca").write_bytes(b"branch work")
        tip = self._publish_branch_tip(copy_id, bundle)
        self._write_branch_ref("konstaxa", "work-old", tip, generation=2)
        (bundle / "world/region/r.0.0.mca").write_bytes(b"edit after publish")
        plain_id, _plain_bundle = self.draft()
        summary = self.store.migrate_branches_to_snapshots()
        self.assertEqual(
            summary["atHead"],
            [{"mapId": "konstaxa", "name": "work-old", "versionDigest": tip}],
        )
        self.assertEqual(len(summary["draftSnapshots"]), 1)
        snap = self.versions.snapshot("konstaxa", summary["draftSnapshots"][0]["name"])
        preserved = self.versions.read("konstaxa", snap["versionDigest"])
        self.assertEqual(
            self.versions._verify_blob(
                preserved["files"]["world/region/r.0.0.mca"]
            ).read_bytes(),
            b"edit after publish",
        )
        # The unedited copy needed no snapshot: its content is the old head.
        self.assertEqual(
            self.versions.validate_working_copy("konstaxa", plain_id)["versionDigest"],
            self.base,
        )
        self.assertEqual(self.versions.head("konstaxa")["versionDigest"], tip)
        self.assertEqual(
            self.versions.validate_working_copy("konstaxa", copy_id)["versionDigest"],
            self.base,
        )
        again = self.store.migrate_branches_to_snapshots()
        self.assertEqual(
            again,
            {
                "migrated": [],
                "atHead": [],
                "candidatesSuperseded": [],
                "draftSnapshots": [],
                "draftSkipped": [],
            },
        )

    def test_migration_hold_failure_leaves_draft_for_explicit_path(self):
        # Round 3: when the running editor cannot be paused, migration logs
        # the draft and leaves it for the explicit snapshot path.
        copy_id, bundle = self.draft()
        (bundle / "world/region/r.0.0.mca").write_bytes(b"branch work")
        tip = self._publish_branch_tip(copy_id, bundle)
        self._write_branch_ref("konstaxa", "work-old", tip, generation=2)
        (bundle / "world/region/r.0.0.mca").write_bytes(b"unsaved draft edit")

        @contextmanager
        def dead_hold():
            raise RuntimeError("save-on did not confirm autosave restored")
            yield

        log = io.StringIO()
        with (
            mock.patch.object(self.store, "hold_saves", dead_hold),
            redirect_stderr(log),
        ):
            summary = self.store.migrate_branches_to_snapshots()
        self.assertEqual(summary["draftSnapshots"], [])
        self.assertEqual(len(summary["draftSkipped"]), 1)
        self.assertIn("konstaxa", log.getvalue())
        self.assertEqual(
            summary["atHead"],
            [{"mapId": "konstaxa", "name": "work-old", "versionDigest": tip}],
        )
        self.assertEqual(self.versions.head("konstaxa")["versionDigest"], tip)
        self.assertEqual(
            (bundle / "world/region/r.0.0.mca").read_bytes(), b"unsaved draft edit"
        )

    def test_advance_failure_after_activation_still_reports_publish(self):
        # r4524172368: the publish is final at activation. A failing copy
        # advance is best effort: the publish reports success with
        # copyAdvanced False and the copy stays stale for the explicit path.
        copy_id, bundle = self.draft()
        (bundle / "world/region/r.0.0.mca").write_bytes(b"first edit")
        self.commit(copy_id, self.base)
        (bundle / "world/region/r.0.0.mca").write_bytes(b"second edit")
        second = self.commit(copy_id, self.base)
        second_review = self.store.review(
            "konstaxa", ACTOR, "", working_copy_id=copy_id, expected_head=second
        )
        with mock.patch.object(
            self.versions,
            "advance_working_copy",
            side_effect=OSError("disk full"),
        ):
            with redirect_stderr(io.StringIO()):
                published = self.store.publish(
                    second_review["id"], ACTOR, expected_head=self.base
                )
        self.assertFalse(published["copyAdvanced"])
        self.assertEqual(published["status"], "accepted")
        self.assertEqual(self.versions.head("konstaxa")["versionDigest"], second)
        self.assertEqual(
            self.versions.validate_working_copy("konstaxa", copy_id)["versionDigest"],
            self.base,
        )
        self.assertEqual(
            (bundle / "world/region/r.0.0.mca").read_bytes(), b"second edit"
        )

    def test_migration_preserves_drafts_before_removing_refs(self):
        # r4524380270: every stale draft must be preserved before any branch
        # ref is removed, so a stop between the steps loses nothing on rerun.
        copy_id, bundle = self.draft()
        (bundle / "world/region/r.0.0.mca").write_bytes(b"branch work")
        tip = self._publish_branch_tip(copy_id, bundle)
        self._write_branch_ref("konstaxa", "work-old", tip, generation=2)
        (bundle / "world/region/r.0.0.mca").write_bytes(b"edit after publish")
        real_migrate = self.versions.migrate_branches_to_snapshots
        preserved_first = []

        def spy():
            preserved_first.append(
                any(
                    entry["name"].startswith("migrated-draft-")
                    for entry in self.versions.snapshots("konstaxa")
                )
            )
            return real_migrate()

        with mock.patch.object(self.versions, "migrate_branches_to_snapshots", spy):
            self.store.migrate_branches_to_snapshots()
        self.assertEqual(preserved_first, [True])

    def test_migration_crash_between_steps_reruns_cleanly(self):
        # r4524380270: a crash after draft preservation but before ref removal
        # keeps the refs; the rerun converges with no duplicate snapshots.
        copy_id, bundle = self.draft()
        (bundle / "world/region/r.0.0.mca").write_bytes(b"branch work")
        tip = self._publish_branch_tip(copy_id, bundle)
        self._write_branch_ref("konstaxa", "work-old", tip, generation=2)
        (bundle / "world/region/r.0.0.mca").write_bytes(b"edit after publish")
        ref_path = self.versions.archive / "branches" / "konstaxa" / "work-old.json"
        with mock.patch.object(
            self.versions,
            "migrate_branches_to_snapshots",
            side_effect=RuntimeError("crash between steps"),
        ):
            with self.assertRaisesRegex(RuntimeError, "crash between steps"):
                self.store.migrate_branches_to_snapshots()
        self.assertTrue(ref_path.is_file())
        snaps = [
            entry
            for entry in self.versions.snapshots("konstaxa")
            if entry["name"].startswith("migrated-draft-")
        ]
        self.assertEqual(len(snaps), 1)
        preserved = self.versions.read("konstaxa", snaps[0]["versionDigest"])
        self.assertEqual(
            self.versions._verify_blob(
                preserved["files"]["world/region/r.0.0.mca"]
            ).read_bytes(),
            b"edit after publish",
        )
        summary = self.store.migrate_branches_to_snapshots()
        self.assertEqual(
            summary["atHead"],
            [{"mapId": "konstaxa", "name": "work-old", "versionDigest": tip}],
        )
        # Idempotent rerun: the preserved draft is reused, nothing new is
        # recorded, and the refs are gone.
        self.assertEqual(summary["draftSnapshots"], [])
        self.assertFalse((self.versions.archive / "branches" / "konstaxa").exists())
        self.assertEqual(
            len(
                [
                    entry
                    for entry in self.versions.snapshots("konstaxa")
                    if entry["name"].startswith("migrated-draft-")
                ]
            ),
            1,
        )

    def test_publish_meta_write_failure_after_activation_still_reports_success(
        self,
    ):
        # r4524380282: everything after the activation commit point is best
        # effort. A failed final metadata write reports success with a flag.
        copy_id, bundle = self.draft()
        (bundle / "world/region/r.0.0.mca").write_bytes(b"first edit")
        first = self.commit(copy_id, self.base)
        review = self.store.review(
            "konstaxa", ACTOR, "", working_copy_id=copy_id, expected_head=first
        )
        real_write = self.store._write_meta
        calls = []

        def flaky(meta):
            calls.append(True)
            if len(calls) == 2:
                raise OSError("disk full")
            return real_write(meta)

        log = io.StringIO()
        with mock.patch.object(self.store, "_write_meta", side_effect=flaky):
            with redirect_stderr(log):
                published = self.store.publish(
                    review["id"], ACTOR, expected_head=self.base
                )
        self.assertEqual(published["status"], "accepted")
        self.assertFalse(published.get("metaSynced", True))
        self.assertIn("metadata", log.getvalue())
        self.assertEqual(self.versions.head("konstaxa")["versionDigest"], first)

    def test_publish_retry_after_commit_reports_accepted(self):
        # r4524380282: the committed state derives from the head itself, so
        # retrying an already committed publication reports it as accepted.
        copy_id, bundle = self.draft()
        (bundle / "world/region/r.0.0.mca").write_bytes(b"first edit")
        first = self.commit(copy_id, self.base)
        review = self.store.review(
            "konstaxa", ACTOR, "", working_copy_id=copy_id, expected_head=first
        )
        self.store.publish(review["id"], ACTOR, expected_head=self.base)
        again = self.store.publish(review["id"], ACTOR, expected_head=self.base)
        self.assertEqual(again["status"], "accepted")
        self.assertEqual(again["versionDigest"], first)
        self.assertEqual(self.versions.head("konstaxa")["versionDigest"], first)

    # -- round 5: pre-upgrade pending reviews are not carried into the linear model --

    def _write_legacy_review(self, version, shape="branched", working_copy_id=None):
        # A genuine pending review converted to a pre-upgrade shape, so every
        # other field (base, regions, digests) stays valid. The reviewer may
        # be a real copy that did not produce the version: pre-upgrade, two
        # sessions could select the same branch.
        review = self.store.review(
            "konstaxa",
            ACTOR,
            "pre-upgrade review",
            working_copy_id=working_copy_id or str(uuid4()),
            expected_head=version,
        )
        path = self.store.candidates / f"{review['id']}.json"
        meta = json.loads(path.read_text())
        if shape == "branched":
            # Branch-era record: both fields exist but under shared-branch
            # semantics (two sessions could select the same branch).
            meta["sourceBranch"] = "work-old"
        else:
            # Ancient/non-native record without the linear-model creation
            # record at all.
            del meta["targetHead"]
            del meta["workingCopyId"]
        path.write_text(json.dumps(meta))
        return meta

    def test_migration_supersedes_legacy_reviews_sharing_a_version(self):
        # r4524516867: two pre-upgrade reviews can point at the same version;
        # migration supersedes both, keeping their versions and files.
        copy_id, bundle = self.draft()
        (bundle / "world/region/r.0.0.mca").write_bytes(b"shared tip")
        version = self.commit(copy_id, self.base)
        first_reviewer, _ = self.draft()
        second_reviewer, _ = self.draft()
        first = self._write_legacy_review(version, "branched", first_reviewer)
        second = self._write_legacy_review(version, "fieldless", second_reviewer)
        summary = self.store.migrate_branches_to_snapshots()
        self.assertEqual(
            sorted(entry["id"] for entry in summary["candidatesSuperseded"]),
            sorted([first["id"], second["id"]]),
        )
        for meta in (first, second):
            current = next(
                item for item in self.store.list() if item["id"] == meta["id"]
            )
            self.assertEqual(current["status"], "superseded")
            self.assertIn("submit again", current["supersedeReason"])
        for meta in (first, second):
            with self.assertRaisesRegex(ValueError, "not pending"):
                self.store.publish(meta["id"], ACTOR, expected_head=self.base)
        # The shared version and its files stay in the archive, untouched.
        self.assertEqual(self.versions.read("konstaxa", version)["parent"], self.base)
        again = self.store.migrate_branches_to_snapshots()
        self.assertEqual(again["candidatesSuperseded"], [])

    def test_duplicate_pending_review_is_never_treated_as_published(self):
        # r4524516867: after one pre-upgrade review of a shared version
        # publishes, publishing the duplicate must fail, not report success
        # while leaving it pending.
        copy_id, bundle = self.draft()
        (bundle / "world/region/r.0.0.mca").write_bytes(b"shared tip")
        version = self.commit(copy_id, self.base)
        first_reviewer, _ = self.draft()
        second_reviewer, _ = self.draft()
        first = self._write_legacy_review(version, "branched", first_reviewer)
        second = self._write_legacy_review(version, "branched", second_reviewer)
        self.store.publish(first["id"], ACTOR, expected_head=self.base)
        self.assertEqual(self.versions.head("konstaxa")["versionDigest"], version)
        with self.assertRaisesRegex(ValueError, "review target|stale"):
            self.store.publish(second["id"], ACTOR, expected_head=version)
        current = next(item for item in self.store.list() if item["id"] == second["id"])
        self.assertEqual(current["status"], "pending")

    def test_only_the_creating_copy_advances_on_publish(self):
        # r4524516857: a review of a version committed from another copy must
        # not advance the reviewer's unrelated copy onto the new head.
        author_id, author_bundle = self.draft()
        other_id, _other_bundle = self.draft()
        (author_bundle / "world/region/r.0.0.mca").write_bytes(b"author edit")
        version = self.commit(author_id, self.base)
        review = self.store.review(
            "konstaxa", ACTOR, "", working_copy_id=other_id, expected_head=version
        )
        published = self.store.publish(review["id"], ACTOR, expected_head=self.base)
        self.assertFalse(published["copyAdvanced"])
        self.assertEqual(
            self.versions.validate_working_copy("konstaxa", other_id)["versionDigest"],
            self.base,
        )
        self.assertEqual(
            self.versions.validate_working_copy("konstaxa", author_id)["versionDigest"],
            self.base,
        )

    def test_interrupted_publish_retries_to_converge(self):
        # r4524516857/r4524516867: the retry settles only this candidate's own
        # committed publication (marker written before activation), and the
        # interrupted copy advance still converges on retry.
        copy_id, bundle = self.draft()
        (bundle / "world/region/r.0.0.mca").write_bytes(b"first edit")
        first = self.commit(copy_id, self.base)
        review = self.store.review(
            "konstaxa", ACTOR, "", working_copy_id=copy_id, expected_head=first
        )
        with mock.patch.object(
            self.versions,
            "advance_working_copy",
            side_effect=OSError("disk full"),
        ):
            with redirect_stderr(io.StringIO()):
                published = self.store.publish(
                    review["id"], ACTOR, expected_head=self.base
                )
        self.assertFalse(published["copyAdvanced"])
        self.assertEqual(published.get("committedVersion"), first)
        again = self.store.publish(review["id"], ACTOR, expected_head=self.base)
        self.assertEqual(again["status"], "accepted")
        self.assertTrue(again["copyAdvanced"])
        self.assertEqual(self.versions.head("konstaxa")["versionDigest"], first)
        self.assertEqual(
            self.versions.validate_working_copy("konstaxa", copy_id)["versionDigest"],
            first,
        )

    def test_checkout_fresh_copy_binds_session(self):
        # r4524380287: the fresh copy goes through the normal session path, so
        # an archived session record selects it for later inbox editing.
        fresh_id = str(uuid4())
        fresh = self.store.checkout_fresh_copy("konstaxa", fresh_id, ACTOR)
        self.assertEqual(fresh["versionDigest"], self.base)
        session = self.store.session(fresh_id)
        self.assertIsNotNone(session)
        self.assertEqual(session["maps"], ["konstaxa"])
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.store.checkout_fresh_copy("konstaxa", fresh_id, ACTOR)

    def test_forked_copy_is_not_rebased(self):
        # Same-function variant: a base outside the head's parent chain is a
        # fork, never a migrated branch — it stays stale and records nothing.
        fork = self._record_version(
            "konstaxa", dict(seed_config("konstaxa"), name="Fork"), None
        )
        self.versions.activate("konstaxa", fork, ACTOR, self.base)
        copy_id, bundle = self.draft(version=self.base)
        (bundle / "world/region/r.0.0.mca").write_bytes(b"fork edit")
        before = set(self.history_parents())
        with self.assertRaisesRegex(ValueError, "behind|stale"):
            self.commit(copy_id, fork)
        self.assertEqual(set(self.history_parents()), before)


if __name__ == "__main__":
    unittest.main()
