"""Real controller session preparation and offline imports, without starting a backend."""

import cp_fixtures
from release_fixtures import catalog

import json
import tempfile
import unittest
from contextlib import contextmanager
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
        self.controller = Controller(
            self.root / "state",
            self.backend,
            maps=self.store,
            admin_dir=self.root / "admin",
            releases=catalog(),
        )
        self.controller.request(str(uuid4()), Goal("game", "v1.0.0"), 0)
        self.controller.step()
        self.backend.ready(Goal(**self.controller.status()["desired"]))
        self.controller.step()
        self.backend.events.clear()

    def request(self, copy_id=None, maps=None):
        copy_id = copy_id or str(uuid4())
        session = {"schema": 1, "id": copy_id, "maps": maps}
        self.controller.request(
            str(uuid4()),
            Goal("workshop", "v1.0.0", copy_id),
            self.controller.status()["revision"],
            session=session,
        )
        return copy_id

    def make_ready(self):
        self.controller.step()
        self.backend.ready(Goal(**self.controller.status()["desired"]))
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
            str(uuid4()), Goal("game", "v1.0.0"), self.controller.status()["revision"]
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
        previous_session = self.controller.session()
        (self.store.versions.active / "limbo/head.json").unlink()
        with self.assertRaisesRegex(ValueError, "no active head"):
            self.request(maps=["limbo"])
        self.assertEqual(self.controller.status(), before)
        self.assertEqual(self.controller.session(), previous_session)
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

    def test_failed_new_session_checkout_preserves_catalog_and_same_uuid_retry(self):
        previous = self.controller.session()
        before = self.controller.status()
        self.assertEqual(before["phase"], "workshop")
        dirty = (
            self.store.versions.working_copy("arena", previous["id"])
            / "world/region/r.0.0.mca"
        )
        dirty.write_bytes(b"previous unsaved edits")
        self.controller.state_reporter = cp_fixtures.Reporter()
        self.controller.export_maps()
        self.controller.flush_reports()
        catalog_before = self.controller.state_reporter.docs["maps"]
        copy_id = str(uuid4())
        with mock.patch.object(
            self.store, "checkout", side_effect=OSError("ENOSPC injected")
        ):
            with self.assertRaisesRegex(OSError, "ENOSPC"):
                self.request(copy_id, ["lobby"])
        self.assertEqual(self.controller.status(), before)
        self.assertEqual(self.controller.session(), previous)
        self.assertEqual(self.backend.events, [])
        self.assertIsNone(self.store.session(copy_id))
        self.controller.export_maps()
        self.controller.flush_reports()
        self.assertEqual(self.controller.state_reporter.docs["maps"], catalog_before)

        self.request(copy_id, ["lobby"])
        self.make_ready()
        self.assertEqual(self.controller.session()["id"], copy_id)
        self.assertEqual(self.controller.status()["phase"], "workshop")
        self.assertEqual(dirty.read_bytes(), b"previous unsaved edits")
        self.controller.export_maps()
        self.controller.flush_reports()
        catalog = self.controller.state_reporter.docs["maps"]
        self.assertIn(copy_id, {session["id"] for session in catalog["sessions"]})
        for entry in catalog["maps"]:
            self.assertIn("snapshots", entry)
            self.assertNotIn("branches", entry)

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
        actor = {"draslId": "t" * 32, "name": "tester"}
        for selected in (None, str(uuid4())):
            with self.assertRaises((ValueError, FileNotFoundError)):
                self.controller._snapshot("arena", actor, "", working_copy_id=selected)
            self.assertEqual(self.backend.events, events)
        self.store.prepare_session(copy_id, ["lobby"])
        with self.assertRaisesRegex(ValueError, "selected"):
            self.controller._snapshot("lobby", actor, "", working_copy_id=copy_id)
        region = (
            self.store.versions.working_copy("arena", copy_id)
            / "world/region/r.0.0.mca"
        )
        region.write_bytes(b"saved draft")
        result = self.controller._snapshot("arena", actor, "", working_copy_id=copy_id)
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


class GitCpTests(unittest.TestCase):
    def setUp(self):
        WorkshopSessionTests.setUp(self)
        self.controller = Controller(
            self.root / "state",
            self.backend,
            maps=self.store,
            admin_dir=self.root / "admin",
            releases=catalog(),
        )
        self.controller.state_reporter = cp_fixtures.Reporter()
        self.builder = {"draslId": "b" * 32, "name": "Builder"}
        self.admin = {"draslId": "a" * 32, "name": "Admin"}
        self.controller.set_members(
            [
                {**self.builder, "role": "builder", "maps": ["arena"]},
                {**self.admin, "role": "admin"},
            ]
        )
        self.session = self.controller.session()
        self.copy = self.store.versions.working_copy("arena", self.session["id"])
        self.region = self.copy / "world/region/r.0.0.mca"
        self.game = self.backend.processes[0]

    make_ready = WorkshopSessionTests.make_ready

    def send(self, kind, *, actor=None, **fields):
        return cp_fixtures.apply(
            self.controller, kind, self.builder if actor is None else actor, **fields
        )

    def head(self):
        return self.store.versions.head("arena")["versionDigest"]

    def commit(self, content, **options):
        self.region.write_bytes(content)
        expected = self.head()
        result = self.send(
            "commit",
            mapId="arena",
            workingCopyId=self.session["id"],
            expectedHead=expected,
            note="Update arena",
            **options,
        )
        self.assertTrue(result["ok"], result["error"])
        self.make_ready()
        return result["payload"]

    def test_sequential_commits_and_frozen_review_keep_game_and_editable_copy(self):
        self.region.write_bytes(b"first edit")
        submitted = self.send(
            "candidate",
            mapId="arena",
            workingCopyId=self.session["id"],
            expectedHead=self.heads["arena"]["versionDigest"],
            note="Review first edit",
            commit=True,
        )
        self.assertTrue(submitted["ok"], submitted["error"])
        review = submitted["payload"]
        first = review["versionDigest"]
        self.assertEqual(review["targetHead"], self.heads["arena"]["versionDigest"])
        self.make_ready()
        second = self.commit(b"second edit")
        self.assertNotEqual(first, second["versionDigest"])
        # Linear history: every commit parents the head, which only publish moves.
        self.assertEqual(
            self.store.versions.read("arena", second["versionDigest"])["parent"],
            self.heads["arena"]["versionDigest"],
        )
        persisted_review = next(
            item for item in self.store.list() if item["id"] == review["id"]
        )
        self.assertEqual(persisted_review["versionDigest"], first)
        accepted = self.send(
            "publish",
            actor=self.admin,
            candidateId=review["id"],
            expectedHead=review["targetHead"],
        )
        self.assertTrue(accepted["ok"], accepted["error"])
        head = self.store.versions.head("arena")
        self.assertEqual(
            (Path(head["bundle"]) / "world/region/r.0.0.mca").read_bytes(),
            b"first edit",
        )
        self.assertEqual(self.region.read_bytes(), b"second edit")
        self.assertEqual(head["versionDigest"], first)
        self.assertIn(self.game, self.backend.processes)
        self.assertEqual(self.backend.events, [])

    def test_stale_head_and_wrong_copy_fail_before_backend_mutation(self):
        first = self.commit(b"committed")
        events = list(self.backend.events)
        base = dict(
            mapId="arena",
            workingCopyId=self.session["id"],
            expectedHead=first["versionDigest"],
            note="Update",
        )
        for change in (
            {"expectedHead": "f" * 64},
            {"workingCopyId": str(uuid4())},
            {"note": ""},
        ):
            with self.subTest(change=change):
                result = self.send("commit", **(base | change))
                self.assertFalse(result["ok"])
                self.assertEqual(self.backend.events, events)
                self.assertEqual(self.controller.status()["phase"], "workshop")

    def test_stale_copy_inbox_refusal_carries_machine_readable_code(self):
        # Coord#31: the site must key recovery buttons off `code`, not the
        # message text. The wording stays byte-identical for older readers.
        from candidates import STALE_COPY_MESSAGE

        first = self.commit(b"reviewed draft")
        # Rollback moves the head without advancing the selected copy, so
        # its base is now stale for the commit below.
        restored = self.send(
            "rollback",
            actor=self.admin,
            mapId="arena",
            versionDigest=first["versionDigest"],
            expectedHead=self.heads["arena"]["versionDigest"],
        )
        self.assertTrue(restored["ok"], restored["error"])
        self.region.write_bytes(b"stale draft")
        refused = self.send(
            "commit",
            mapId="arena",
            workingCopyId=self.session["id"],
            expectedHead=self.store.versions.head("arena")["versionDigest"],
            note="Update arena",
        )
        self.assertFalse(refused["ok"])
        self.assertEqual(refused["error"], STALE_COPY_MESSAGE)
        self.assertEqual(refused.get("code"), "stale_working_copy")

    def test_scope_and_exact_membership_are_authoritative(self):
        before = self.heads["arena"]["versionDigest"]
        self.assertTrue(
            self.send("compare", mapId="arena", before=before, after=before)["ok"]
        )
        for actor, map_id in (
            (self.builder, "lobby"),
            ({**self.builder, "name": "Spoofed"}, "arena"),
            ({"draslId": "c" * 32, "name": "Unknown"}, "arena"),
        ):
            result = self.send(
                "compare", actor=actor, mapId=map_id, before=before, after=before
            )
            self.assertFalse(result["ok"])
        self.assertEqual(self.backend.events, [])
        self.assertIn(self.game, self.backend.processes)

    def test_builder_cannot_publish_reject_or_restore_main(self):
        self.region.write_bytes(b"edit")
        submitted = self.send(
            "candidate",
            mapId="arena",
            workingCopyId=self.session["id"],
            expectedHead=self.heads["arena"]["versionDigest"],
            note="Review",
            commit=True,
        )
        self.assertTrue(submitted["ok"], submitted["error"])
        self.make_ready()
        review = submitted["payload"]
        for kind, fields in (
            (
                "publish",
                {"candidateId": review["id"], "expectedHead": review["targetHead"]},
            ),
            ("reject", {"candidateId": review["id"]}),
            (
                "rollback",
                {
                    "mapId": "arena",
                    "versionDigest": review["targetHead"],
                    "expectedHead": review["targetHead"],
                },
            ),
        ):
            result = self.send(kind, **fields)
            self.assertFalse(result["ok"])
            self.assertIn("admin", result["error"])
        self.assertEqual(self.store.versions.head("arena"), self.heads["arena"])

    def test_checkout_fresh_copy_is_bound_to_a_selectable_session(self):
        # r4524380287 via the inbox: the fresh copy is selectable through an
        # archived resume and editable through a later inbox commit.
        fresh_id = str(uuid4())
        fresh = self.send("checkout-fresh", mapId="arena", workingCopyId=fresh_id)
        self.assertTrue(fresh["ok"], fresh["error"])
        self.assertEqual(
            fresh["payload"]["versionDigest"],
            self.heads["arena"]["versionDigest"],
        )
        archived = self.store.session(fresh_id)
        self.assertIsNotNone(archived)
        self.assertEqual(archived["maps"], ["arena"])
        resumed = self.send(
            "mode",
            actor=self.admin,
            profile="workshop",
            session="resume",
            sessionId=fresh_id,
            expectedRevision=self.controller.status()["revision"],
        )
        self.assertTrue(resumed["ok"], resumed["error"])
        self.make_ready()
        fresh_region = (
            self.store.versions.working_copy("arena", fresh_id)
            / "world/region/r.0.0.mca"
        )
        fresh_region.write_bytes(b"rebuilt on head")
        committed = self.send(
            "commit",
            mapId="arena",
            workingCopyId=fresh_id,
            expectedHead=self.heads["arena"]["versionDigest"],
            note="Rebuild on head",
        )
        self.assertTrue(committed["ok"], committed["error"])

    def test_snapshot_copy_and_checkout_fresh_recover_stale_copy(self):
        # Round 3 way out through the inbox: a stale session copy is refused,
        # snapshot-copy preserves its draft, checkout-fresh restarts from head.
        head = self.heads["arena"]["versionDigest"]
        other = str(uuid4())
        self.store.checkout("arena", head, other)
        other_region = (
            self.store.versions.working_copy("arena", other) / "world/region/r.0.0.mca"
        )
        other_region.write_bytes(b"sibling change")
        sibling = {"draslId": "b" * 32, "name": "Builder"}
        committed = self.store.commit(
            "arena", sibling, "Sibling", working_copy_id=other, expected_head=head
        )["versionDigest"]
        review = self.store.review(
            "arena", sibling, "", working_copy_id=other, expected_head=committed
        )
        self.store.publish(
            review["id"], {"draslId": "a" * 32, "name": "Admin"}, expected_head=head
        )
        new_head = self.store.versions.head("arena")["versionDigest"]
        self.assertEqual(new_head, committed)
        self.region.write_bytes(b"stale session edit")
        refused = self.send(
            "commit",
            mapId="arena",
            workingCopyId=self.session["id"],
            expectedHead=new_head,
            note="Stale attempt",
        )
        self.assertFalse(refused["ok"])
        self.assertIn("newer version", refused["error"])
        builder_copy = self.send(
            "snapshot-copy",
            mapId="arena",
            name="stale-arena",
            workingCopyId=self.session["id"],
        )
        self.assertFalse(builder_copy["ok"])
        self.assertIn("admin", builder_copy["error"])
        preserved = self.send(
            "snapshot-copy",
            actor=self.admin,
            mapId="arena",
            name="stale-arena",
            workingCopyId=self.session["id"],
        )
        self.assertTrue(preserved["ok"], preserved["error"])
        manifest = self.store.versions.read(
            "arena", preserved["payload"]["versionDigest"]
        )
        self.assertEqual(manifest["parent"], head)
        blob = self.store.versions._verify_blob(
            manifest["files"]["world/region/r.0.0.mca"]
        )
        self.assertEqual(blob.read_bytes(), b"stale session edit")
        fresh_id = str(uuid4())
        fresh = self.send("checkout-fresh", mapId="arena", workingCopyId=fresh_id)
        self.assertTrue(fresh["ok"], fresh["error"])
        self.assertEqual(fresh["payload"]["versionDigest"], new_head)
        # The stale copy is untouched by the whole way out.
        self.assertEqual(self.region.read_bytes(), b"stale session edit")
        self.assertEqual(
            self.store.versions.validate_working_copy("arena", self.session["id"])[
                "versionDigest"
            ],
            head,
        )

    def test_candidate_snapshots_current_draft_instead_of_reusing_commit(self):
        first = self.commit(b"committed bytes")
        self.region.write_bytes(b"uncommitted bytes")
        events = list(self.backend.events)
        snapshotted = self.send(
            "candidate",
            mapId="arena",
            workingCopyId=self.session["id"],
            expectedHead=self.heads["arena"]["versionDigest"],
        )
        self.assertTrue(snapshotted["ok"], snapshotted["error"])
        # Linear candidates always snapshot the current draft; the earlier
        # commit is superseded, not reused.
        self.assertNotEqual(
            snapshotted["payload"]["versionDigest"], first["versionDigest"]
        )
        manifest = self.store.versions.read(
            "arena", snapshotted["payload"]["versionDigest"]
        )
        self.assertEqual(manifest["parents"], [self.heads["arena"]["versionDigest"]])
        self.assertEqual(self.region.read_bytes(), b"uncommitted bytes")
        self.assertEqual(self.backend.events, events)
        self.assertEqual(self.controller.status()["phase"], "workshop")

    def test_sleeping_workshop_commits_and_reviews_from_disk_without_starting(self):
        self.make_ready()
        self.backend.processes = [self.game]
        state = self.controller._read()
        state.update(phase="sleeping", gate="closed")
        self.controller._write(state)
        self.backend.events.clear()
        self.region.write_bytes(b"edited before sleep")
        base = dict(mapId="arena", workingCopyId=self.session["id"])
        committed = self.send(
            "commit",
            **base,
            expectedHead=self.heads["arena"]["versionDigest"],
            note="Update arena",
        )
        self.assertTrue(committed["ok"], committed["error"])
        digest = committed["payload"]["versionDigest"]
        reviewed = self.send(
            "candidate",
            **base,
            expectedHead=self.heads["arena"]["versionDigest"],
            commit=True,
            note="Review",
        )
        self.assertTrue(reviewed["ok"], reviewed["error"])
        self.assertNotEqual(reviewed["payload"]["versionDigest"], digest)
        self.assertEqual(
            reviewed["payload"]["targetHead"], self.heads["arena"]["versionDigest"]
        )
        self.assertEqual(self.backend.events, [])
        self.assertEqual(self.backend.processes, [self.game])
        self.assertEqual(self.controller.status()["phase"], "sleeping")
        # A Workshop that came up behind the controller's back may still be writing the drafts.
        self.backend.processes.append(
            Process(Goal(**self.controller.status()["desired"]), True)
        )
        refused = self.send("commit", **base, expectedHead=digest, note="Again")
        self.assertFalse(refused["ok"])
        self.assertEqual(self.backend.events, [])

    def test_tablet_submit_commits_and_reviews_without_restarting(self):
        self.make_ready()
        self.backend.events.clear()
        base = dict(
            mapId="arena",
            workingCopyId=self.session["id"],
            expectedHead=self.heads["arena"]["versionDigest"],
            note="Submitted from the tablet",
            commit=True,
        )
        unchanged = self.send("candidate", **base)
        self.assertFalse(unchanged["ok"])
        self.assertEqual(unchanged["error"], "no changes since the current map head")
        self.region.write_bytes(b"built in game")
        submitted = self.send("candidate", **base)
        self.assertTrue(submitted["ok"], submitted["error"])
        review = submitted["payload"]
        self.assertEqual(review["status"], "pending")
        self.assertEqual(review["targetHead"], self.heads["arena"]["versionDigest"])
        self.assertEqual(self.backend.events, [])
        self.assertEqual(self.controller.status()["phase"], "workshop")

    def test_hot_commit_and_submit_matrix_and_precise_refusals(self):
        self.make_ready()
        workshop = self.backend.processes[-1]
        for phase in ("workshop", "sleeping"):
            self.backend.processes = [self.game] + (
                [workshop] if phase == "workshop" else []
            )
            state = self.controller._read()
            state.update(
                phase=phase, gate="builders" if phase == "workshop" else "closed"
            )
            self.controller._write(state)
            for kind in ("commit", "candidate"):
                with self.subTest(phase=phase, kind=kind):
                    head = self.heads["arena"]["versionDigest"]
                    # Reset the draft to the head so the refusal cases below
                    # start from a clean copy.
                    self.region.write_bytes(
                        self.store.versions._verify_blob(
                            self.store.versions.read("arena", head)["files"][
                                "world/region/r.0.0.mca"
                            ]
                        ).read_bytes()
                    )
                    base = dict(
                        mapId="arena",
                        workingCopyId=self.session["id"],
                        expectedHead=head,
                        note="Built arena",
                        commit=True,
                    )
                    for fields, error in (
                        ({}, "no changes"),
                        ({"note": " "}, "commit message required"),
                        ({"commit": "true"}, "commit must be a boolean"),
                    ):
                        refused = self.send(kind, **(base | fields))
                        self.assertFalse(refused["ok"])
                        self.assertIn(error, refused["error"])
                        self.assertEqual(
                            self.store.versions.head("arena")["versionDigest"],
                            head,
                        )
                    self.region.write_bytes(f"{phase}/{kind}".encode())
                    with mock.patch.object(
                        self.controller, "_write", wraps=self.controller._write
                    ) as write:
                        result = self.send(kind, **base)
                        write.assert_not_called()
                    self.assertTrue(result["ok"], result["error"])
                    digest = result["payload"]["versionDigest"]
                    self.assertNotEqual(digest, head)
                    manifest = self.store.versions.read("arena", digest)
                    self.assertEqual(manifest["author"], self.builder)
                    # Both paths record through the linear commit step first.
                    self.assertEqual(manifest["source"], "commit")
                    self.assertEqual(manifest["note"], base["note"])
                    if kind == "candidate":
                        self.assertEqual(result["payload"]["status"], "pending")
                        self.assertEqual(
                            result["payload"]["targetHead"],
                            self.heads["arena"]["versionDigest"],
                        )
                    # The same draft commits again as a sibling: linear history
                    # never silently drops a snapshot.
                    again = self.send(kind, **base)
                    self.assertTrue(again["ok"], again["error"])
                    self.assertNotEqual(again["payload"]["versionDigest"], digest)
                    self.assertEqual(self.controller.status()["phase"], phase)
        self.assertEqual(self.backend.events, [])

    def test_save_guard_failures_and_concurrent_snapshot_preserve_head(self):
        self.make_ready()
        head = self.heads["arena"]["versionDigest"]
        self.region.write_bytes(b"built in game")
        base = dict(
            mapId="arena",
            workingCopyId=self.session["id"],
            expectedHead=head,
            note="Submit",
            commit=True,
        )

        @contextmanager
        def restore_failure():
            yield
            raise RuntimeError("save-on did not confirm autosave restored")

        with mock.patch.object(self.store, "hold_saves", restore_failure):
            failed = self.send("candidate", **base)
        self.assertFalse(failed["ok"])
        self.assertIn("save-on", failed["error"])
        self.assertEqual(self.store.versions.head("arena")["versionDigest"], head)
        other = Controller(
            self.root / "state", self.backend, maps=self.store, releases=catalog()
        )

        @contextmanager
        def concurrent():
            with self.assertRaises(BlockingIOError):
                other._snapshot(
                    "arena",
                    self.builder,
                    "Concurrent",
                    working_copy_id=self.session["id"],
                    expected_head=head,
                    commit=True,
                )
            yield

        with mock.patch.object(self.store, "hold_saves", concurrent):
            result = self.send("candidate", **base)
        self.assertTrue(result["ok"], result["error"])
        self.assertEqual(self.backend.events, [])

    def test_submit_review_failure_retains_commit_for_review_retry(self):
        self.make_ready()
        head = self.heads["arena"]["versionDigest"]
        self.region.write_bytes(b"built in game")
        base = dict(mapId="arena", workingCopyId=self.session["id"])
        with mock.patch.object(
            self.store, "_write_meta", side_effect=OSError("disk full")
        ):
            failed = self.send(
                "candidate",
                **base,
                commit=True,
                note="Submit",
                expectedHead=head,
            )
        self.assertFalse(failed["ok"])
        self.assertIn("disk full", failed["error"])
        # The commit is retained: files were recorded before the metadata write.
        versions = [
            version
            for version in self.store.versions.history("arena")
            if version["versionDigest"] != head
        ]
        self.assertEqual(len(versions), 1)
        retry = self.send("candidate", **base, expectedHead=head)
        self.assertTrue(retry["ok"], retry["error"])
        self.assertEqual(retry["payload"]["baseVersion"], head)
        self.assertEqual(self.backend.events, [])

    def test_review_noop_and_metadata_failure_leave_head_and_backend_unchanged(self):
        base = dict(mapId="arena", workingCopyId=self.session["id"])
        noop = self.send(
            "candidate", **base, expectedHead=self.heads["arena"]["versionDigest"]
        )
        self.assertFalse(noop["ok"])
        self.assertIn("no changes", noop["error"])
        self.assertEqual(self.backend.events, [])
        self.commit(b"edited")
        events = list(self.backend.events)
        head = self.heads["arena"]["versionDigest"]
        with mock.patch.object(
            self.store, "_write_meta", side_effect=OSError("disk full")
        ):
            failed = self.send(
                "candidate",
                **base,
                expectedHead=head,
                commit=True,
                note="Review",
            )
        self.assertFalse(failed["ok"])
        self.assertIn("disk full", failed["error"])
        self.assertEqual(self.store.versions.head("arena")["versionDigest"], head)
        self.assertEqual(self.backend.events, events)
        self.assertEqual(self.controller.status()["phase"], "workshop")

    def test_snapshot_create_compare_and_list_return_pinned_payloads(self):
        first = self.commit(b"edit")
        before = self.heads["arena"]["versionDigest"]
        created = self.send(
            "snapshot-create",
            mapId="arena",
            name="milestone",
            versionDigest=before,
            actor=self.admin,
        )
        self.assertTrue(created["ok"], created["error"])
        self.assertEqual(created["payload"]["versionDigest"], before)
        self.assertEqual(created["payload"]["createdBy"], self.admin)
        clashing = self.send(
            "snapshot-create",
            mapId="arena",
            name="milestone",
            versionDigest=first["versionDigest"],
            actor=self.admin,
        )
        self.assertFalse(clashing["ok"])
        listed = self.send("snapshots", mapId="arena")
        self.assertTrue(listed["ok"], listed["error"])
        self.assertEqual(
            [snap["name"] for snap in listed["payload"]["snapshots"]], ["milestone"]
        )
        compared = self.send(
            "compare", mapId="arena", before=before, after=first["versionDigest"]
        )
        self.assertTrue(compared["ok"], compared["error"])
        change = next(
            item
            for item in compared["payload"]["files"]
            if item["path"] == "world/region/r.0.0.mca"
        )
        self.assertEqual(change["change"], "modified")
        self.assertEqual(
            change["area"], {"minX": 0, "minZ": 0, "maxX": 511, "maxZ": 511}
        )
        for removed in (
            {"kind": "branch-create"},
            {"kind": "merge-preview"},
        ):
            result = self.send(**{"mapId": "arena", **removed})
            self.assertFalse(result["ok"])
            self.assertIn("unsupported cp operation kind", result["error"])

    def test_review_target_and_force_cannot_be_replaced_on_publish(self):
        self.region.write_bytes(b"edit")
        submitted = self.send(
            "candidate",
            mapId="arena",
            workingCopyId=self.session["id"],
            expectedHead=self.heads["arena"]["versionDigest"],
            note="Review",
            commit=True,
        )
        self.assertTrue(submitted["ok"], submitted["error"])
        review = submitted["payload"]
        first = review["versionDigest"]
        self.make_ready()
        # Force waives only the template-base guard: it publishes a fresh
        # review fine and still records that the operator forced it.
        forced = self.send(
            "publish",
            actor=self.admin,
            candidateId=review["id"],
            expectedHead=review["targetHead"],
            force=True,
        )
        self.assertTrue(forced["ok"], forced["error"])
        self.assertTrue(forced["payload"]["forced"])
        self.assertEqual(self.store.versions.head("arena")["versionDigest"], first)
        # A moved target still fails, force or not.
        stale = self.send(
            "publish",
            actor=self.admin,
            candidateId=review["id"],
            expectedHead=first,
            force=True,
        )
        self.assertFalse(stale["ok"])
        self.assertIn("review target changed", stale["error"])
        self.assertEqual(
            next(item for item in self.store.list() if item["id"] == review["id"])[
                "status"
            ],
            "accepted",
        )

    def test_map_switch_requires_new_session_and_keeps_old_draft(self):
        self.region.write_bytes(b"unsaved old maps")
        state = self.controller.status()
        with self.assertRaisesRegex(ValueError, "already bound"):
            self.controller.request(
                str(uuid4()),
                Goal(**state["desired"]),
                state["revision"],
                session={
                    "schema": 1,
                    "id": self.session["id"],
                    "maps": ["lobby"],
                },
            )
        selected = self.controller._session_for(
            {"session": "new", "maps": ["arena"]}, self.builder
        )
        self.controller.request(
            str(uuid4()),
            Goal("workshop", "v1.0.0", selected["id"]),
            state["revision"],
            session=selected,
        )
        self.assertEqual(self.region.read_bytes(), b"unsaved old maps")
        next_copy = self.store.versions.working_copy("arena", selected["id"])
        self.assertEqual((next_copy / "world/region/r.0.0.mca").read_bytes(), b"arena")
        self.assertEqual(self.controller.session()["maps"], ["arena"])
        self.assertNotIn("branches", self.controller.session())

    def test_game_release_change_preserves_maps_and_draft_bytes(self):
        # Game rollouts never retarget Workshop: maps and unsaved drafts
        # survive; only an explicit operation selects another release.
        original = self.controller.session_file.read_bytes()
        self.region.write_bytes(b"preserved draft")
        self.backend.processes[0] = Process(Goal("game", "v2.0.0"), True)
        self.backend.fail_start = True
        state = self.controller.step()
        self.assertEqual(state["phase"], "workshop")
        self.assertIsNone(state.get("releaseFollow"))
        self.assertEqual(self.controller.session_file.read_bytes(), original)
        self.assertEqual(self.region.read_bytes(), b"preserved draft")
        self.assertIn(Process(Goal("game", "v2.0.0"), True), self.backend.processes)
        self.backend.fail_start = False
        self.make_ready()
        self.assertEqual(self.controller.status()["gate"], "builders")

    def test_explicit_release_change_needs_recovery_after_failed_start(self):
        self.backend.fail_start = True
        self.controller.request(
            str(uuid4()),
            Goal("workshop", "v2.0.0", self.session["id"]),
            self.controller.status()["revision"],
        )
        self.assertEqual(self.controller.step()["phase"], "ERROR")
        self.backend.fail_start = False
        events = list(self.backend.events)
        state = self.controller.step()
        self.assertEqual(state["phase"], "ERROR")
        self.assertIsNone(state.get("releaseFollow"))
        self.assertEqual(self.backend.events, events)

    def new_session(self):
        selected = self.controller._session_for(
            {"session": "new", "maps": ["arena"]}, self.admin
        )
        self.controller.request(
            str(uuid4()),
            Goal("workshop", "v1.0.0", selected["id"]),
            self.controller.status()["revision"],
            session=selected,
        )
        self.make_ready()
        return self.controller.session()

    def test_archived_session_page_and_resume_preserve_original_dirty_copy(self):
        self.region.write_bytes(b"old dirty session")
        original = self.store.session(self.session["id"])
        newer = self.new_session()
        self.controller.export_maps()
        self.controller.flush_reports()
        catalog = self.controller.state_reporter.docs["maps"]
        self.assertEqual(catalog["sessionsTotal"], 2)
        self.assertIsNone(catalog["sessionsNext"])
        first = self.send("sessions", offset=0, limit=1)
        self.assertTrue(first["ok"], first["error"])
        self.assertEqual(first["payload"]["total"], 2)
        self.assertEqual(first["payload"]["next"], 1)
        second = self.send("sessions", offset=1, limit=1)
        self.assertTrue(second["ok"], second["error"])
        self.assertEqual(
            {
                entry["id"]
                for entry in first["payload"]["sessions"]
                + second["payload"]["sessions"]
            },
            {original["id"], newer["id"]},
        )
        resumed = self.send(
            "mode",
            actor=self.admin,
            profile="workshop",
            session="resume",
            sessionId=original["id"],
            expectedRevision=self.controller.status()["revision"],
        )
        self.assertTrue(resumed["ok"], resumed["error"])
        self.assertEqual(self.controller.session(), original)
        self.make_ready()
        self.assertEqual(self.region.read_bytes(), b"old dirty session")
        self.assertIn(self.game, self.backend.processes)

    def test_archived_resume_rejects_overrides_missing_sessions_and_missing_copies(
        self,
    ):
        self.new_session()
        events = list(self.backend.events)
        state = self.controller.status()
        base = dict(
            profile="workshop",
            session="resume",
            sessionId=self.session["id"],
            expectedRevision=state["revision"],
        )
        for changes in (
            {"session": "new"},
            {"maps": ["arena"]},
            {"sessionId": str(uuid4())},
        ):
            result = self.send("mode", actor=self.admin, **(base | changes))
            self.assertFalse(result["ok"])
            self.assertEqual(self.backend.events, events)
            self.assertEqual(self.controller.status(), state)
        # Pre-linear branches on a resume are ignored, not rejected: the live
        # session keeps working against its session-UUID working copy.
        ignored = self.send(
            "mode",
            actor=self.admin,
            **(base | {"branches": {"arena": "work-legacy"}}),
        )
        self.assertTrue(ignored["ok"], ignored["error"])
        self.assertNotIn("branches", self.controller.session())
        # The successful resume above advanced the state: settle back to the
        # running workshop before the final missing-copy refusal.
        self.make_ready()
        state = self.controller.status()
        base = dict(base, expectedRevision=state["revision"])
        events = list(self.backend.events)
        (self.copy / "map.json").unlink()
        result = self.send("mode", actor=self.admin, **base)
        self.assertFalse(result["ok"])
        self.assertEqual(self.backend.events, events)
        self.assertEqual(self.controller.status(), state)

    def test_session_pages_require_membership_and_valid_bounds(self):
        for fields in ({"offset": -1}, {"offset": True}, {"limit": 101}):
            self.assertFalse(self.send("sessions", **fields)["ok"])
        unknown = self.send("sessions", actor={"draslId": "c" * 32, "name": "Unknown"})
        self.assertFalse(unknown["ok"])
        self.assertNotIn("payload", unknown)
        self.assertEqual(self.backend.events, [])

    def test_old_session_uuid_cannot_rebind_after_switching_away(self):
        self.new_session()
        before = self.controller.status()
        events = list(self.backend.events)
        with self.assertRaisesRegex(ValueError, "already bound"):
            self.controller.request(
                str(uuid4()),
                Goal("workshop", "v1.0.0", self.session["id"]),
                before["revision"],
                session={
                    "schema": 1,
                    "id": self.session["id"],
                    "maps": ["lobby"],
                },
            )
        self.assertEqual(self.controller.status(), before)
        self.assertEqual(self.backend.events, events)

    def test_only_publish_accepts_large_payloads(self):
        choices = {f"world/region/r.{index}.0.mca": "ours" for index in range(10000)}
        self.assertGreater(len(json.dumps(choices).encode()), 65536)
        big_publish = self.send(
            "publish",
            actor=self.admin,
            candidateId="0" * 32,
            resolutions=choices,
        )
        self.assertFalse(big_publish["ok"])
        self.assertIn("unknown candidate", big_publish["error"])
        self.assertEqual(self.backend.events, [])
        self.assertEqual(self.store.versions.head("arena"), self.heads["arena"])


if __name__ == "__main__":
    unittest.main()
