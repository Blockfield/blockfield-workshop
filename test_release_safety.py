"""Regression gates for release selection review findings 1–6."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from uuid import uuid4

from controller import Controller, Goal, Process
from releases import load_catalog, resolve, validate_release_record
from release_fixtures import catalog, release_record
from test_controller import FakeBackend


class ReleaseSafetyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.backend = FakeBackend()
        self.catalog = catalog()
        self.controller = Controller(self.root, self.backend, releases=self.catalog)
        self.session = str(uuid4())
        self.clock = [1000.0]
        self.controller._now = lambda: self.clock[0]
        self.controller.start_timeout = 10

    def goal(self, release="v1.0.0", profile="workshop"):
        return Goal(
            profile, release, self.session, "c" * 64 if profile == "test" else None
        )

    def request(self, release="v1.0.0", profile="workshop", operation=None):
        return self.controller.request(
            operation or str(uuid4()),
            self.goal(release, profile),
            self.controller.status()["revision"],
        )

    def ready(self, release="v1.0.0", profile="workshop"):
        self.request(release, profile)
        self.controller.step()
        self.backend.ready(self.goal(release, profile))
        return self.controller.step()

    def target(self):
        return self.controller.approved_target()

    def test_missing_and_empty_catalog_block_selection(self):
        for index, records in enumerate((None, {})):
            with self.subTest(records=records):
                control = Controller(
                    self.root / str(index), self.backend, releases=records
                )
                with self.assertRaises(ValueError):
                    control.request(str(uuid4()), self.goal(), 0)
                self.assertEqual(control.status()["revision"], 0)
                self.assertFalse(control.target_file.exists())
        self.assertEqual(load_catalog(self.root / "missing.json"), {})

    def test_placeholder_publication_is_rejected(self):
        record = release_record()
        record.update(serverRuntime={}, clientContract={}, source={"unverified": True})
        with self.assertRaises(ValueError):
            validate_release_record(record)

    def test_publication_bindings_are_required(self):
        mutations = [
            ("release", "latest"),
            ("revision", -1),
            ("imageDigest", self.catalog["v2.0.0"]["imageDigest"]),
            ("source.tag", "v2.0.0"),
            ("source.draft", True),
            ("source.publishedAt", "not published"),
            ("source.commit", "0" * 40),
            ("source.imageManifest", "{}"),
            ("source.imageConfig", "{}"),
            ("source.buildJson", "{}"),
            ("source.checksums", "0" * 64 + "  build.json\n"),
            ("source.provenance.buildSha256", "0" * 64),
            ("source.provenance.imageDigest", self.catalog["v2.0.0"]["imageDigest"]),
            ("serverRuntime.fingerprint", "0" * 64),
            ("clientContract.digest", "0" * 64),
        ]
        for dotted, value in mutations:
            with self.subTest(field=dotted):
                record = release_record()
                parent = record
                *keys, key = dotted.split(".")
                for part in keys:
                    parent = parent[part]
                parent[key] = value
                with self.assertRaises(ValueError):
                    validate_release_record(record)

    def test_catalog_key_must_match_record(self):
        path = self.root / "releases.json"
        path.write_text(json.dumps({"v2.0.0": release_record()}))
        with self.assertRaises(ValueError):
            load_catalog(path)
        self.controller.releases = {"v2.0.0": release_record()}
        with self.assertRaises(ValueError):
            self.request("v2.0.0")

    def test_no_game_or_legacy_goal_bootstrap_without_approved_identity(self):
        self.backend.processes = [Process(Goal("game", "v9-unverified"), True)]
        self.controller.step()
        self.assertEqual(self.backend.events, [])
        self.assertIsNone(self.controller.status()["desired"])

    def test_legacy_game_goal_without_committed_identity_never_bootstraps(self):
        self.backend.processes = []
        self.controller.file.write_text(
            json.dumps(
                {
                    "schema": 1,
                    "phase": "game",
                    "gate": "public",
                    "operation": str(uuid4()),
                    "desired": {"profile": "game", "release": "v9-unverified"},
                    "revision": 1,
                }
            )
        )
        self.controller.step()
        self.assertEqual(self.backend.events, [])

    def test_corrupt_target_does_not_bootstrap_from_game(self):
        self.controller.target_file.write_text("{broken")
        with self.subTest(path="target read"):
            with self.assertRaises(ValueError):
                self.target()
        with self.subTest(path="bootstrap"):
            with self.assertRaises(ValueError):
                self.controller.step()
        self.assertEqual(self.backend.events, [])

    def test_preflight_and_state_write_failure_preserve_target(self):
        self.ready()
        old_state = self.controller.file.read_bytes()
        old_target = self.controller.target_file.read_bytes()
        for failure in ("preflight", "write"):
            with self.subTest(failure=failure):
                patch = (
                    mock.patch.object(
                        self.backend,
                        "preflight",
                        side_effect=OSError("preflight failed"),
                    )
                    if failure == "preflight"
                    else mock.patch(
                        "controller.atomic_write", side_effect=OSError("write failed")
                    )
                )
                with patch, self.assertRaises(OSError):
                    self.request("v2.0.0")
                self.assertEqual(self.controller.file.read_bytes(), old_state)
                self.assertEqual(self.controller.target_file.read_bytes(), old_target)

    def test_projection_failure_reconciles_from_committed_state_after_restart(self):
        self.ready()
        import controller

        write = controller.atomic_write

        def fail_projection(path, *args, **kwargs):
            if path == self.controller.target_file:
                raise OSError("projection unavailable")
            return write(path, *args, **kwargs)

        with mock.patch("controller.atomic_write", side_effect=fail_projection):
            with self.assertRaises(OSError):
                self.request("v2.0.0")
        restarted = Controller(self.root, self.backend)
        target = restarted.approved_target()
        state = restarted.status()
        self.assertEqual(target["release"], "v2.0.0")
        self.assertEqual(target["stateRevision"], state["revision"])
        self.assertEqual(target["operation"], state["selection"]["operation"])
        self.assertEqual(target["previousTarget"]["release"], "v1.0.0")
        self.assertEqual(json.loads(restarted.target_file.read_text()), target)

    def test_timeout_compensation_commits_previous_target(self):
        self.ready()
        self.request("v2.0.0")
        self.controller.step()
        self.clock[0] += 11
        state = self.controller.step()
        self.assertEqual(state["desired"]["release"], "v1.0.0")
        self.assertEqual(self.target()["release"], "v1.0.0")
        self.assertEqual(self.target()["stateRevision"], state["revision"])
        self.assertEqual(self.target()["previousTarget"]["release"], "v2.0.0")

    def test_active_test_stages_without_interrupting_then_return_activates(self):
        self.ready()
        before = self.ready(profile="test")
        self.assertEqual(before["phase"], "test")
        events = list(self.backend.events)
        session = self.controller.session_file.read_bytes()
        staged = self.request("v2.0.0")
        self.assertEqual(
            (staged["phase"], staged["operation"], staged["desired"]),
            (before["phase"], before["operation"], before["desired"]),
        )
        self.assertEqual(self.target()["release"], "v2.0.0")
        self.assertEqual(self.controller.step()["phase"], "test")
        self.assertEqual(self.backend.events, events)
        self.assertEqual(self.controller.session_file.read_bytes(), session)
        self.controller.request(
            str(uuid4()), Goal("game", "v1.0.0"), staged["revision"]
        )
        state = self.controller.step()
        self.assertEqual(state["desired"]["release"], "v2.0.0")
        self.assertEqual(self.backend.events, events + ["save", "stop", "start"])

    def test_unknown_runtime_stages_closed_and_requires_explicit_recovery(self):
        before = self.ready()
        self.backend.processes = [
            Process(Goal("workshop", "unknown", self.session), False)
        ]
        events = list(self.backend.events)
        staged = self.request("v2.0.0")
        self.assertEqual(staged["phase"], "ERROR")
        self.assertEqual(
            (staged["operation"], staged["desired"]),
            (before["operation"], before["desired"]),
        )
        self.assertEqual(self.target()["release"], "v2.0.0")
        self.assertEqual(self.controller.step()["phase"], "ERROR")
        self.assertEqual(self.backend.events, events)
        self.controller.request(
            str(uuid4()), Goal("game", "v1.0.0"), staged["revision"], recover=True
        )
        self.assertEqual(self.controller.step(recover=True)["phase"], "ERROR")
        self.assertEqual(self.backend.events, events + ["fence"])
        # Only a positive backend fence/absence proof permits the next explicit retry.
        self.backend.processes = []
        self.assertEqual(self.controller.step(recover=True)["phase"], "STARTING")
        self.assertEqual(self.backend.events, events + ["fence", "fence", "start"])
        self.assertEqual(
            self.controller.status()["activeTarget"]["imageDigest"],
            self.catalog["v2.0.0"]["imageDigest"],
        )

    def test_transition_and_error_stage_without_replacing_operation(self):
        before = self.request()
        for phase in ("REQUESTED", "ERROR"):
            with self.subTest(phase=phase):
                before["phase"] = phase
                self.controller._write(before)
                events = list(self.backend.events)
                staged = self.request("v2.0.0")
                self.assertEqual(
                    (staged["phase"], staged["desired"], staged["operation"]),
                    (before["phase"], before["desired"], before["operation"]),
                )
                self.assertEqual(self.target()["release"], "v2.0.0")
                if phase == "ERROR":
                    self.assertEqual(self.controller.step()["phase"], "ERROR")
                    self.assertEqual(self.backend.events, events)
                before = staged

    def test_staged_operation_retry_is_idempotent_and_content_bound(self):
        self.ready()
        self.ready(profile="test")
        revision = self.controller.status()["revision"]
        operation = str(uuid4())
        staged = self.controller.request(operation, self.goal("v2.0.0"), revision)
        self.assertEqual(
            self.controller.request(operation, self.goal("v2.0.0"), revision), staged
        )
        with self.assertRaises(ValueError):
            self.controller.request(operation, self.goal("v3.0.0"), revision)
        self.controller.releases["v2.0.0"] = release_record("v2.0.0", "d")
        with self.assertRaises(ValueError):
            self.controller.request(operation, self.goal("v2.0.0"), revision)

    def test_duplicate_selection_rejects_changed_session_payload(self):
        operation = str(uuid4())
        session = {"schema": 1, "id": self.session, "maps": None}
        first = self.controller.request(operation, self.goal(), 0, session=session)
        self.assertEqual(
            self.controller.request(operation, self.goal(), 0, session=session), first
        )
        with self.assertRaises(ValueError):
            self.controller.request(
                operation, self.goal(), 0, session={**session, "maps": ["fields"]}
            )

    def sleeping(self):
        state = self.ready()
        self.backend.processes = []
        state.update(phase="sleeping", gate="closed")
        self.controller._write(state)
        self.backend.events.clear()
        return state

    def test_legacy_sleeping_import_commits_running_selection(self):
        # Failed cut-over shape: a sleeping host state carries desired but no
        # selection journal. Importing the running release must stage and
        # commit its verified identity, not fail journal validation.
        self.backend.processes = []
        self.controller.file.write_text(
            json.dumps(
                {
                    "schema": 1,
                    "phase": "sleeping",
                    "gate": "closed",
                    "operation": str(uuid4()),
                    "revision": 77,
                    "desired": {
                        "profile": "workshop",
                        "release": "v2.0.0",
                        "session": self.session,
                        "candidate": None,
                    },
                    "error": None,
                    "errorMessage": None,
                    "rollback": None,
                }
            )
        )
        staged = self.controller.request(
            str(uuid4()),
            self.goal("v2.0.0"),
            77,
            session={"schema": 1, "id": self.session, "maps": None},
        )
        self.assertEqual(staged["revision"], 78)
        self.assertEqual(staged["approvedTarget"]["release"], "v2.0.0")
        self.assertEqual(staged["activeTarget"]["release"], "v2.0.0")
        self.assertTrue(staged["selection"]["deferred"])
        self.assertEqual(self.controller.step()["phase"], "sleeping")
        self.assertEqual(self.target()["release"], "v2.0.0")

    def test_sleeping_selection_stages_without_waking_or_preflight(self):
        before = self.sleeping()
        session = self.controller.session_file.read_bytes()
        with mock.patch.object(
            self.backend,
            "preflight",
            side_effect=AssertionError("sleep must not preflight Java"),
        ):
            staged = self.request("v2.0.0")
        self.assertEqual(staged["phase"], "sleeping")
        self.assertEqual(staged["desired"], before["desired"])
        self.assertEqual(self.controller.step()["phase"], "sleeping")
        self.assertEqual(self.backend.events, [])
        self.assertEqual(self.controller.session_file.read_bytes(), session)
        self.assertEqual(self.target()["release"], "v2.0.0")

    def test_wake_uses_exact_committed_record_with_game_off(self):
        self.sleeping()
        self.request("v2.0.0")
        target = self.target()
        self.controller.releases = {}
        self.controller.wake(str(uuid4()))
        state = self.controller.step()
        self.assertEqual(state["desired"], {**self.goal("v2.0.0").__dict__})
        self.assertEqual(state["activeTarget"]["imageDigest"], target["imageDigest"])
        self.assertEqual(self.backend.events, ["start"])

    def test_wake_never_follows_ready_game(self):
        self.sleeping()
        self.backend.processes = [Process(Goal("game", "v9-unverified"), True)]
        self.controller.wake(str(uuid4()))
        self.assertEqual(self.controller.status()["desired"]["release"], "v1.0.0")

    def test_staged_target_survives_test_transition_compensation(self):
        self.ready()
        self.request(profile="test")
        self.controller.step()
        self.request("v2.0.0")
        self.clock[0] += 11
        rolled_back = self.controller.step()
        self.assertEqual(rolled_back["desired"]["profile"], "workshop")
        self.assertEqual(self.target()["release"], "v2.0.0")
        self.backend.ready(self.goal())
        self.controller.step()
        self.controller.step()
        self.assertEqual(self.controller.status()["desired"]["release"], "v2.0.0")

    def test_projection_corruption_is_repaired_only_from_valid_state(self):
        self.ready()
        expected = self.target()
        for value in ("{broken", json.dumps({**expected, "stateRevision": 99}), None):
            if value is None:
                self.controller.target_file.unlink()
            else:
                self.controller.target_file.write_text(value)
            restarted = Controller(self.root, self.backend)
            self.assertEqual(restarted.approved_target(), expected)
        state = self.controller.status()
        state["activeTarget"] = self.catalog["v2.0.0"]
        self.controller.file.write_text(json.dumps(state))
        events = list(self.backend.events)
        with self.assertRaises(ValueError):
            self.controller.step()
        self.assertEqual(self.backend.events, events)

    def test_missing_catalog_reuses_only_exact_committed_target(self):
        self.ready()
        expected = self.target()["imageDigest"]
        self.controller.releases = {}
        self.request()
        self.assertEqual(self.target()["imageDigest"], expected)
        with self.assertRaises(ValueError):
            self.request("v2.0.0")
        self.controller.releases = {"v1.0.0": release_record("v1.0.0", "d")}
        with self.assertRaises(ValueError):
            self.request()

    def test_catalog_and_target_readers_reject_invalid_files(self):
        from releases import load_approved_target

        for payload in ("{bad", "null", "[]", '{"x":1,"x":2}'):
            self.controller.target_file.write_text(payload)
            with self.assertRaises(ValueError):
                load_approved_target(self.controller.target_file)
        self.controller.target_file.unlink()
        self.controller.target_file.symlink_to(self.root / "missing")
        with self.assertRaises(ValueError):
            load_approved_target(self.controller.target_file)

    def test_oci_index_must_bind_the_exported_platform_manifest(self):
        import hashlib

        record = release_record()
        source = record["source"]
        source["platformManifest"] = source["imageManifest"]
        source["imageManifest"] = json.dumps(
            {
                "schemaVersion": 2,
                "manifests": [
                    {
                        "platform": {"architecture": "amd64", "os": "linux"},
                        "digest": record["imageDigest"].split("@", 1)[1],
                    }
                ],
            }
        )
        record["imageDigest"] = (
            "ghcr.io/blockfield/blockfield-server@sha256:"
            + hashlib.sha256(source["imageManifest"].encode()).hexdigest()
        )
        source["provenance"]["imageDigest"] = record["imageDigest"]
        self.assertEqual(validate_release_record(record), record)
        source["platformManifest"] = "{}"
        with self.assertRaises(ValueError):
            validate_release_record(record)


if __name__ == "__main__":
    unittest.main()


class PublishedClientVersionTest(unittest.TestCase):
    def test_bare_client_version_from_published_build_json(self):
        validate_release_record(release_record(client_version="1.0.0"))

    def test_garbage_client_version_rejected(self):
        with self.assertRaises(ValueError):
            validate_release_record(release_record(client_version="1.0"))


class DigestReleaseTests(unittest.TestCase):
    """Goals and targets accept an image digest; the tag path keeps working.

    Journal shape never changes (Goal keeps one free-form release string), so
    new code keeps reading old tag journals — but an old reader rejects a
    journal holding a digest goal (its selection check compares the goal
    release to the record tag), so rolling the controller back after digest
    goals exist is unsupported.
    """

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.backend = FakeBackend()
        self.records = catalog()
        self.keyed = dict(self.records)
        for name, record in self.records.items():
            self.keyed[record["imageDigest"].split("@", 1)[1]] = record
        self.session = str(uuid4())
        self.controller = Controller(self.root, self.backend, releases=self.keyed)
        self.controller._now = lambda: 1000.0
        self.controller.start_timeout = 10

    def digest(self, release="v1.0.0"):
        return self.records[release]["imageDigest"].split("@", 1)[1]

    def test_resolve_accepts_digest_alias(self):
        digest = self.digest()
        self.assertEqual(resolve(self.keyed, digest)["release"], "v1.0.0")
        self.assertEqual(resolve(self.keyed, "v1.0.0")["release"], "v1.0.0")
        with self.assertRaises(ValueError):
            resolve(self.keyed, "sha256:" + "f" * 64)
        wrong = dict(self.keyed)
        wrong["v1.0.0"] = self.records["v2.0.0"]
        with self.assertRaises(ValueError):
            resolve(wrong, "v1.0.0")

    def test_digest_request_reaches_ready(self):
        digest = self.digest()
        state = self.controller.request(
            str(uuid4()), Goal("workshop", digest, self.session), 0
        )
        self.assertEqual(state["desired"]["release"], digest)
        self.controller.step()
        self.backend.ready(Goal("workshop", digest, self.session))
        state = self.controller.step()
        self.assertEqual(state["desired"]["release"], digest)
        self.assertEqual(self.controller.approved_target()["release"], "v1.0.0")
        self.assertEqual(self.controller.status()["desired"]["release"], digest)

    def test_journal_shape_unchanged_between_tag_and_digest(self):
        tag_state = self.controller.request(
            str(uuid4()), Goal("workshop", "v1.0.0", self.session), 0
        )
        other = Controller(self.root / "digest", self.backend, releases=self.keyed)
        digest_state = other.request(
            str(uuid4()), Goal("workshop", self.digest(), self.session), 0
        )
        for first, second in (
            (tag_state, digest_state),
            (tag_state["desired"], digest_state["desired"]),
            (tag_state["selection"], digest_state["selection"]),
            (tag_state["selection"]["intent"], digest_state["selection"]["intent"]),
            (tag_state["approvedTarget"], digest_state["approvedTarget"]),
        ):
            self.assertEqual(set(first), set(second))
        self.assertEqual(tag_state["desired"]["release"], "v1.0.0")
        self.assertEqual(digest_state["desired"]["release"], self.digest())
        # Old tag journal still validates and projects under the new code.
        self.assertEqual(self.controller.status()["revision"], 1)
        self.assertEqual(other.status()["revision"], 1)

    def test_tag_request_writes_no_digest_goal(self):
        # A digest appears in a goal only when explicitly requested: a tag
        # request stores the tag in every goal slot, never the digest alias.
        state = self.controller.request(
            str(uuid4()), Goal("workshop", "v1.0.0", self.session), 0
        )
        digest = self.digest()
        self.assertNotEqual(digest, "v1.0.0")
        for slot in (
            state["desired"]["release"],
            state["selection"]["intent"]["release"],
            state["selection"]["request"]["release"],
        ):
            self.assertEqual(slot, "v1.0.0")
