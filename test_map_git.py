"""Map branches use immutable trees, frozen reviews and explicit binary conflicts."""

import json
import os
import unittest
from pathlib import Path
from unittest import mock
from uuid import uuid4

import candidates
import test_candidates as fixtures

ACTOR = fixtures.ACTOR


class MapGitTests(unittest.TestCase):
    def setUp(self):
        fixtures.CandidateTests.setUp(self)
        config = {
            "id": "konstaxa",
            "name": "Base",
            "kind": "arena",
            "bounds": {"minX": 0, "maxX": 512, "minZ": 0, "maxZ": 512},
            "mode_data": {},
            "supported_modes": [],
        }
        (self.data / "blockfield/maps/konstaxa.json").write_text(json.dumps(config))
        meta = self.store.create("konstaxa", ACTOR)
        self.store.publish(meta["id"], ACTOR, expected_head=None)
        self.base = meta["versionDigest"]
        self.versions = self.store.versions

    def draft(self, name, version=None):
        base = version or self.base
        self.versions.create_branch("konstaxa", name, base, ACTOR)
        copy_id = str(uuid4())
        copy = self.store.checkout("konstaxa", base, copy_id)
        return copy_id, Path(copy["bundle"])

    def commit(self, name, copy_id, note="Map change"):
        head = self.versions.branch("konstaxa", name)["versionDigest"]
        self.clock[0] += 1
        return self.store.commit(
            "konstaxa",
            ACTOR,
            note,
            working_copy_id=copy_id,
            branch=name,
            expected_head=head,
        )["versionDigest"]

    def test_sequential_commits_deduplicate_and_keep_dirty_copy_provenance(self):
        copy_id, bundle = self.draft("lighting")
        region = bundle / "world/region/r.0.0.mca"
        region.write_bytes(b"lighting one")
        first = self.commit("lighting", copy_id)
        region.write_bytes(b"lighting two")
        second = self.commit("lighting", copy_id)
        self.assertEqual(self.versions.read("konstaxa", second)["parents"], [first])
        self.assertEqual(self.versions.read("konstaxa", first)["parents"], [self.base])
        self.assertEqual(self.versions.head("konstaxa")["versionDigest"], self.base)
        self.assertEqual(
            json.loads((bundle / "world/blockfield-base.json").read_bytes())[
                "versionDigest"
            ],
            self.base,
        )
        before = {path.name for path in (self.versions.archive / "blobs").glob("*/*")}
        self.assertEqual(self.commit("lighting", copy_id), second)
        self.assertEqual(
            before,
            {path.name for path in (self.versions.archive / "blobs").glob("*/*")},
        )
        self.assertEqual(len(self.store.list()), 1)

    def test_stale_copy_and_failed_ref_write_preserve_branch_and_world(self):
        copy_id, bundle = self.draft("cover")
        other_id = str(uuid4())
        other = self.store.checkout("konstaxa", self.base, other_id)
        (bundle / "world/region/r.0.0.mca").write_bytes(b"cover")
        committed = self.commit("cover", copy_id)
        (Path(other["bundle"]) / "world/region/r.0.0.mca").write_bytes(b"stale")
        with self.assertRaisesRegex(ValueError, "behind"):
            self.commit("cover", other_id)
        ref = self.versions._branch_path("konstaxa", "cover")
        before = ref.read_bytes()
        (bundle / "world/region/r.0.0.mca").write_bytes(b"still dirty")
        real_write = candidates._atomic_json

        def fail_ref(path, *args, **kwargs):
            if path == ref:
                raise OSError("disk full")
            return real_write(path, *args, **kwargs)

        with mock.patch.object(candidates, "_atomic_json", side_effect=fail_ref):
            with self.assertRaisesRegex(OSError, "disk full"):
                self.commit("cover", copy_id)
        self.assertEqual(ref.read_bytes(), before)
        self.assertEqual(
            self.versions.branch("konstaxa", "cover")["versionDigest"], committed
        )
        self.assertEqual(
            (bundle / "world/region/r.0.0.mca").read_bytes(), b"still dirty"
        )

    def test_nonoverlapping_merge_keeps_both_parents_and_readable_diff(self):
        one_id, one = self.draft("lighting")
        two_id, two = self.draft("cover")
        (one / "world/region/r.0.0.mca").write_bytes(b"lights")
        source = self.commit("lighting", one_id)
        (two / "world/entities/r.0.0.mca").write_bytes(b"new entities")
        target = self.commit("cover", two_id)
        self.versions.merge("konstaxa", target, ACTOR, self.base)
        preview = self.versions.merge_preview("konstaxa", source, target)
        self.assertEqual(preview["unresolved"], [])
        merged = self.versions.merge("konstaxa", source, ACTOR, target)["versionDigest"]
        manifest = self.versions.read("konstaxa", merged)
        self.assertEqual(manifest["parents"], [target, source])
        diff = self.versions.compare("konstaxa", target, merged)
        self.assertEqual(
            [file["path"] for file in diff["files"]], ["world/region/r.0.0.mca"]
        )
        self.assertEqual(
            diff["files"][0]["area"], {"minX": 0, "minZ": 0, "maxX": 511, "maxZ": 511}
        )
        self.assertEqual(
            self.versions._verify_blob(
                manifest["files"]["world/entities/r.0.0.mca"]
            ).read_bytes(),
            b"new entities",
        )
        with self.assertRaisesRegex(ValueError, "stale"):
            self.versions.merge("konstaxa", source, ACTOR, target)

    def test_binary_conflicts_require_exact_explicit_choices(self):
        one_id, one = self.draft("first")
        two_id, two = self.draft("second")
        for bundle, data in ((one, b"one"), (two, b"two")):
            (bundle / "world/region/r.0.0.mca").write_bytes(data)
        source, target = self.commit("first", one_id), self.commit("second", two_id)
        self.versions.merge("konstaxa", target, ACTOR, self.base)
        head = self.versions.head("konstaxa")
        path = "world/region/r.0.0.mca"
        self.assertEqual(
            self.versions.merge_preview("konstaxa", source, target)["unresolved"],
            [path],
        )
        with self.assertRaisesRegex(ValueError, "merge conflicts"):
            self.versions.merge("konstaxa", source, ACTOR, target)
        for choices in ({path: "overwrite"}, {"../escape": "ours"}, {path: []}):
            with self.assertRaises(ValueError):
                self.versions.merge(
                    "konstaxa", source, ACTOR, target, resolutions=choices
                )
        self.assertEqual(self.versions.head("konstaxa"), head)
        merged = self.versions.merge(
            "konstaxa", source, ACTOR, target, resolutions={path: "theirs"}
        )
        self.assertEqual(
            (Path(merged["bundle"]) / "world/region/r.0.0.mca").read_bytes(), b"one"
        )

    def test_json_changes_merge_by_field_and_keep_boolean_identity(self):
        one_id, one = self.draft("bounds")
        two_id, two = self.draft("title")
        left, right = (
            json.loads((one / "map.json").read_bytes()),
            json.loads((two / "map.json").read_bytes()),
        )
        left["bounds"]["maxX"] = 600
        right["name"] = "New title"
        (one / "map.json").write_text(json.dumps(left))
        (two / "map.json").write_text(json.dumps(right))
        source, target = self.commit("bounds", one_id), self.commit("title", two_id)
        self.versions.merge("konstaxa", target, ACTOR, self.base)
        merged = self.versions.merge("konstaxa", source, ACTOR, target)["versionDigest"]
        diff = self.versions.compare("konstaxa", target, merged)
        self.assertEqual(
            diff["config"],
            [
                {
                    "path": "/bounds/maxX",
                    "beforePresent": True,
                    "afterPresent": True,
                    "before": 512,
                    "after": 600,
                }
            ],
        )
        self.assertFalse(self.versions._same({"enabled": True}, {"enabled": 1}))

    def test_review_stays_frozen_while_branch_keeps_committing(self):
        copy_id, bundle = self.draft("review")
        region = bundle / "world/region/r.0.0.mca"
        region.write_bytes(b"reviewed")
        committed = self.commit("review", copy_id)
        review = self.store.create(
            "konstaxa",
            ACTOR,
            "Review cover",
            working_copy_id=copy_id,
            branch="review",
            expected_branch_head=committed,
        )
        region.write_bytes(b"later edit")
        later = self.commit("review", copy_id)
        self.store.publish(review["id"], ACTOR, expected_head=self.base)
        head = self.versions.head("konstaxa")
        self.assertEqual(head["versionDigest"], review["versionDigest"])
        self.assertEqual(
            (Path(head["bundle"]) / "world/region/r.0.0.mca").read_bytes(), b"reviewed"
        )
        self.assertEqual(
            self.versions.branch("konstaxa", "review")["versionDigest"], later
        )

    def test_restore_appends_commit_without_rewinding_main(self):
        copy_id, bundle = self.draft("change")
        (bundle / "world/region/r.0.0.mca").write_bytes(b"new")
        second = self.commit("change", copy_id)
        self.versions.merge("konstaxa", second, ACTOR, self.base)
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
        self.assertIn(
            second, self.versions._ancestors("konstaxa", restored["versionDigest"])
        )

    def test_branch_names_main_and_symlinks_fail_closed(self):
        for name in ("main", "../escape", "a/b", "", "x" * 65):
            with self.assertRaises(ValueError):
                self.versions.create_branch("konstaxa", name, self.base, ACTOR)
        directory = self.versions.archive / "branches"
        directory.symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "unsafe"):
            self.versions.create_branch("konstaxa", "safe", self.base, ACTOR)

    def test_review_write_failure_never_advances_branch_or_changes_copy(self):
        copy_id, bundle = self.draft("review")
        (bundle / "world/region/r.0.0.mca").write_bytes(b"committed")
        committed = self.commit("review", copy_id)
        (bundle / "world/region/r.0.0.mca").write_bytes(b"dirty after commit")
        before = self.versions.branch("konstaxa", "review")
        with mock.patch.object(
            self.store, "_write_meta", side_effect=OSError("disk full")
        ):
            with self.assertRaisesRegex(OSError, "disk full"):
                self.store.review(
                    "konstaxa",
                    ACTOR,
                    "",
                    working_copy_id=copy_id,
                    branch="review",
                    expected_head=committed,
                )
        self.assertEqual(self.versions.branch("konstaxa", "review"), before)
        self.assertEqual(
            (bundle / "world/region/r.0.0.mca").read_bytes(), b"dirty after commit"
        )

    def test_immutable_session_binding_prevents_old_copy_rebinding(self):
        copy_id, bundle = self.draft("first")
        self.versions.create_branch("konstaxa", "second", self.base, ACTOR)
        self.store.prepare_session(
            copy_id,
            ["konstaxa"],
            branches={"konstaxa": "first"},
            actor=ACTOR,
        )
        (bundle / "world/region/r.0.0.mca").write_bytes(b"uncommitted work")
        with self.assertRaisesRegex(ValueError, "already bound"):
            self.store.prepare_session(
                copy_id,
                ["konstaxa"],
                branches={"konstaxa": "second"},
                actor=ACTOR,
            )
        session = self.store.session(copy_id)
        self.assertEqual(session["branches"], {"konstaxa": "first"})
        self.assertEqual(self.store.sessions()["sessions"], [session])
        self.assertEqual(
            (bundle / "world/region/r.0.0.mca").read_bytes(), b"uncommitted work"
        )

    def test_new_object_fields_merge_and_same_new_key_conflicts(self):
        one_id, one = self.draft("spawns")
        two_id, two = self.draft("kit")
        left, right = (
            json.loads((one / "map.json").read_bytes()),
            json.loads((two / "map.json").read_bytes()),
        )
        left["mode_data"]["deathmatch"] = {"spawns": ["one"]}
        right["mode_data"]["deathmatch"] = {"kit": {"gun": "mp5"}}
        (one / "map.json").write_text(json.dumps(left))
        (two / "map.json").write_text(json.dumps(right))
        source, target = self.commit("spawns", one_id), self.commit("kit", two_id)
        preview = self.versions.merge_preview("konstaxa", source, target)
        self.assertEqual(preview["unresolved"], [])
        self.assertEqual(
            preview["config"]["mode_data"]["deathmatch"],
            {
                "spawns": ["one"],
                "kit": {"gun": "mp5"},
            },
        )
        right["mode_data"]["deathmatch"]["spawns"] = ["two"]
        (two / "map.json").write_text(json.dumps(right))
        target = self.commit("kit", two_id)
        self.assertEqual(
            self.versions.merge_preview("konstaxa", source, target)["unresolved"],
            ["map.json#/mode_data/deathmatch/spawns"],
        )

    def test_oversized_config_never_advances_branch(self):
        copy_id, bundle = self.draft("large")
        config = json.loads((bundle / "map.json").read_bytes())
        config["mode_data"]["large"] = "x" * (1 << 20)
        (bundle / "map.json").write_text(json.dumps(config))
        with self.assertRaisesRegex(ValueError, "config too large"):
            self.commit("large", copy_id)
        self.assertEqual(
            self.versions.branch("konstaxa", "large")["versionDigest"], self.base
        )

    def test_failed_session_checkout_never_archives_a_missing_branch(self):
        previous_id, previous = self.draft("preserve")
        self.store.prepare_session(
            previous_id,
            ["konstaxa"],
            branches={"konstaxa": "preserve"},
            actor=ACTOR,
        )
        dirty = previous / "world/region/r.0.0.mca"
        dirty.write_bytes(b"unsaved previous work")
        before = self.store.sessions()
        session_id = str(uuid4())
        branch = "work-" + session_id
        with mock.patch.object(
            self.store, "checkout", side_effect=OSError("disk full")
        ):
            with self.assertRaisesRegex(OSError, "disk full"):
                self.store.prepare_session(
                    session_id,
                    ["konstaxa"],
                    branches={"konstaxa": branch},
                    actor=ACTOR,
                )
        self.assertIsNone(self.store.session(session_id))
        self.assertEqual(self.store.sessions(), before)
        self.assertFalse(self.versions._branch_path("konstaxa", branch).exists())
        self.assertFalse(self.versions.working_copy("konstaxa", session_id).exists())
        self.assertEqual(dirty.read_bytes(), b"unsaved previous work")

    def test_archive_sync_failure_keeps_its_complete_copy_and_ref(self):
        session_id = str(uuid4())
        branch = "work-" + session_id
        archive = self.store._session_path(session_id)
        real_sync = candidates._sync_directory

        def fail_after_archive_rename(path):
            if path == archive.parent:
                raise OSError("archive sync failed")
            return real_sync(path)

        with mock.patch.object(
            candidates, "_sync_directory", side_effect=fail_after_archive_rename
        ):
            with self.assertRaisesRegex(OSError, "archive sync failed"):
                self.store.prepare_session(
                    session_id,
                    ["konstaxa"],
                    branches={"konstaxa": branch},
                    actor=ACTOR,
                )
        self.assertEqual(
            self.store.session(session_id)["branches"], {"konstaxa": branch}
        )
        self.assertEqual(
            self.versions.branch("konstaxa", branch)["versionDigest"], self.base
        )
        self.versions.validate_working_copy("konstaxa", session_id)
        self.assertEqual(archive.stat().st_mode & 0o777, 0o444)

    def test_permission_repair_skips_symlinks_and_settled_files(self):
        copy_id, bundle = self.draft("links")
        outside = self.data / "outside-secret"
        outside.write_text("x")
        outside.chmod(0o600)
        (bundle / "world/level.dat").symlink_to(outside)
        (bundle / "world/dangling").symlink_to(self.data / "missing")
        region = bundle / "world/region/r.0.0.mca"
        region.chmod(0o644)
        owner = (os.getuid(), os.getgid())
        with mock.patch.object(candidates, "OWNER", owner):
            self.versions.validate_working_copy("konstaxa", copy_id)
            self.assertEqual(outside.stat().st_mode & 0o777, 0o600)
            self.assertEqual(region.stat().st_mode & 0o777, 0o664)
            with mock.patch("os.fchmod") as fchmod, mock.patch("os.fchown") as fchown:
                self.versions.validate_working_copy("konstaxa", copy_id)
            fchmod.assert_not_called()
            fchown.assert_not_called()

    def test_review_target_cannot_move_under_approval(self):
        copy_id, bundle = self.draft("review")
        (bundle / "world/region/r.0.0.mca").write_bytes(b"review")
        committed = self.commit("review", copy_id)
        review = self.store.review(
            "konstaxa",
            ACTOR,
            "",
            working_copy_id=copy_id,
            branch="review",
            expected_head=committed,
        )
        other_id, other = self.draft("other")
        (other / "world/entities/r.0.0.mca").write_bytes(b"independent")
        changed_main = self.commit("other", other_id)
        self.versions.merge("konstaxa", changed_main, ACTOR, self.base)
        with self.assertRaisesRegex(ValueError, "target changed"):
            self.store.publish(review["id"], ACTOR, expected_head=changed_main)
        self.assertEqual(self.versions.head("konstaxa")["versionDigest"], changed_main)
