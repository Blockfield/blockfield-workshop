"""DokployBackend against in-memory CP StateReporter and WorkshopGameClient.

No docker.sock, no RCON, no READY/profile files: lifecycle and readiness go
through CP state docs (``game-desired``, ``game-ready``, ``game-activity``) and
``WorkshopGameClient`` operations. The deploy webhook fires only when
``WORKSHOP_DEPLOY_WEBHOOK_URL`` is set, and tests mock the HTTP layer.
"""

import json
import logging
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock
from uuid import uuid4

import cp_fixtures
from controller import Controller, Goal, Process
from dokploy_backend import DokployBackend
from release_fixtures import release_record as _fixture_record

RELEASE = "v9.0.0"


def release_record(release=RELEASE, digest="d"):
    return _fixture_record(release, digest)


class DokployBackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.data, self.lock = root / "data", root / "lock"
        self.data.mkdir()
        (self.lock / "workshop").mkdir(parents=True)
        self.record = release_record()
        self.reporter = cp_fixtures.Reporter()
        self.now_ms = [1_700_000_000_000]
        self.clock = [100.0]

        def handle_game_op(game_op, **_kwargs):
            if game_op == "stop" and self._stop_goes_dark:
                self.reporter.put(
                    "game-ready",
                    {
                        **(self.reporter.docs.get("game-ready") or {}),
                        "running": False,
                        "updatedAt": self.now_ms[0],
                    },
                )
            return {"state": "effective", "code": "ok", "output": "ok"}

        self._stop_goes_dark = True
        self.game_client = cp_fixtures.FakeGameClient(handler=handle_game_op)
        self.backend = DokployBackend(
            data=self.data,
            lock_dir=self.lock,
            releases={RELEASE: self.record},
            stop_timeout=2.0,
            poll=0.1,
            min_free_bytes=0,
            ready_ttl_ms=60_000,
            sleep=lambda s: self._advance(s),
            clock=lambda: self.clock[0],
            now_ms=lambda: self.now_ms[0],
            state_reporter=self.reporter,
            game_client=self.game_client,
        )
        self.goal = Goal("workshop", RELEASE, str(uuid4()))
        self.ready(str(uuid4()), self.goal.session, running=False)

    def _advance(self, seconds):
        self.clock[0] += seconds
        self.now_ms[0] += int(seconds * 1000)

    def ready(
        self,
        generation,
        session,
        release=RELEASE,
        profile="workshop",
        *,
        running=True,
        age_ms=0,
    ):
        self.reporter.put(
            "game-ready",
            {
                "schema": 1,
                "profile": profile,
                "generation": generation,
                "session": session,
                "release": release,
                "running": running,
                "updatedAt": self.now_ms[0] - age_ms,
            },
        )

    def test_start_reports_desired_via_cp_and_observes_ready(self):
        self.backend.start(self.goal)
        selected = self.reporter.docs["game-desired"]
        self.assertEqual(
            (selected["profile"], selected["release"], selected["session"]),
            ("workshop", RELEASE, self.goal.session),
        )
        self.assertFalse((self.lock / "workshop/profile.json").exists())
        self.assertFalse((self.data / ".blockfield-ready.json").exists())
        self.assertEqual(self.backend.observe(), [])
        self.ready(selected["generation"], self.goal.session)
        observed = self.backend.observe()
        self.assertEqual(len(observed), 1)
        self.assertTrue(observed[0].ready)
        self.assertEqual(observed[0].goal.release, RELEASE)
        self.assertEqual(self.backend.readiness()["generation"], selected["generation"])
        self.assertEqual(
            self.backend.readiness_generation("workshop"), selected["generation"]
        )
        self.assertIsNone(self.backend.readiness_generation("game"))

    def test_stale_or_stopped_ready_reads_as_not_alive(self):
        self.backend.start(self.goal)
        gen = self.reporter.docs["game-desired"]["generation"]
        self.ready(gen, self.goal.session, age_ms=61_000)
        with self.assertRaisesRegex(RuntimeError, "readiness unknown"):
            self.backend.observe()
        self.ready(gen, self.goal.session, running=False)
        self.assertEqual(self.backend.observe(), [])

    def test_unknown_readiness_never_means_stopped(self):
        for kind in ("missing", "stale"):
            with self.subTest(kind=kind):
                if kind == "missing":
                    self.reporter.docs.pop("game-ready", None)
                else:
                    self.ready(str(uuid4()), self.goal.session, age_ms=61_000)
                with self.assertRaisesRegex(RuntimeError, "readiness unknown"):
                    self.backend.observe()
                with self.assertRaisesRegex(RuntimeError, "readiness unknown"):
                    with self.backend.workshop_saves_held():
                        self.fail("must not snapshot")
                with self.assertRaises(TimeoutError):
                    self.backend.fence_pending_starts()
                self._stop_goes_dark = False
                with self.assertRaises(TimeoutError):
                    self.backend._stop_and_wait("unknown")

    def test_unconfigured_cp_clients_fail_closed(self):
        bare = DokployBackend(
            data=self.data, lock_dir=self.lock, releases={RELEASE: self.record}
        )
        with self.assertRaisesRegex(RuntimeError, "state reporter"):
            bare.selection()
        with self.assertRaisesRegex(RuntimeError, "game client"):
            bare.save_and_freeze(Process(self.goal, True))

    def test_digest_selected_release_reports_tagged_ready(self):
        digest = self.record["imageDigest"].split("@", 1)[1]
        self.backend.releases = {RELEASE: self.record, digest: self.record}
        goal = Goal("workshop", digest, str(uuid4()))
        self.backend.start(goal)
        selected = self.reporter.docs["game-desired"]
        self.assertEqual(selected["release"], digest)
        self.ready(selected["generation"], goal.session, release=RELEASE)
        observed = self.backend.observe()
        self.assertEqual(len(observed), 1)
        self.assertTrue(observed[0].ready)
        self.assertEqual(observed[0].goal.release, digest)

    def test_foreign_tag_does_not_report_ready_for_digest_goal(self):
        digest = self.record["imageDigest"].split("@", 1)[1]
        self.backend.releases = {RELEASE: self.record, digest: self.record}
        goal = Goal("workshop", digest, str(uuid4()))
        self.backend.start(goal)
        selected = self.reporter.docs["game-desired"]
        self.ready(selected["generation"], goal.session, release="v9.9.9")
        self.assertFalse(self.backend.observe()[0].ready)

    def test_retry_across_starts_reuses_generation(self):
        self.backend.start(self.goal)
        first = self.reporter.docs["game-desired"]
        self.backend.start(self.goal)
        second = self.reporter.docs["game-desired"]
        self.assertEqual(second["generation"], first["generation"])

    def test_new_goal_mints_fresh_generation(self):
        self.backend.start(self.goal)
        first = self.reporter.docs["game-desired"]
        other = Goal("workshop", RELEASE, str(uuid4()))
        self.backend.start(other)
        second = self.reporter.docs["game-desired"]
        self.assertNotEqual(second["generation"], first["generation"])

    def test_rotated_candidate_mints_fresh_generation(self):
        self.backend.maps = mock.Mock()
        session = str(uuid4())
        self.backend.start(Goal("test", RELEASE, session, candidate="a" * 64))
        first = self.reporter.docs["game-desired"]
        self.backend.start(Goal("test", RELEASE, session, candidate="a" * 64))
        same = self.reporter.docs["game-desired"]
        self.assertEqual(same["generation"], first["generation"])
        self.backend.start(Goal("test", RELEASE, session, candidate="b" * 64))
        rotated = self.reporter.docs["game-desired"]
        self.assertNotEqual(rotated["generation"], first["generation"])

    def test_unknown_release_fails_preflight_before_effects(self):
        with self.assertRaises(ValueError):
            self.backend.start(Goal("workshop", "v-absent", str(uuid4())))
        self.assertNotIn("game-desired", self.reporter.docs)

    def test_save_requires_success_and_stop_goes_dark_via_cp(self):
        self.backend.start(self.goal)
        selected = self.reporter.docs["game-desired"]
        self.ready(selected["generation"], self.goal.session)
        process = self.backend.observe()[0]
        self.assertTrue(process.ready)
        self.game_client.responses.append(RuntimeError("save-flush failed"))
        with self.assertRaises(RuntimeError):
            self.backend.save_and_freeze(process)
        self.backend.stop(process)
        self.assertEqual(self.backend.observe(), [])
        self.assertEqual(
            [call[0] for call in self.game_client.calls[-2:]],
            ["save-flush", "stop"],
        )

    def test_stop_timeout_when_game_agent_never_reports_stopped(self):
        self.backend.start(self.goal)
        self.ready(self.reporter.docs["game-desired"]["generation"], self.goal.session)
        self._stop_goes_dark = False
        with self.assertRaises(TimeoutError):
            self.backend.stop(self.backend.observe()[0])

    def test_hot_snapshot_holds_saves_around_copy_and_restores_on_failure(self):
        self.backend.start(self.goal)
        self.ready(self.reporter.docs["game-desired"]["generation"], self.goal.session)
        with self.backend.workshop_saves_held():
            pass
        self.assertEqual(
            [call[0] for call in self.game_client.calls],
            ["save-on", "save-off", "save-flush", "save-on"],
        )
        ok = {"state": "effective", "code": "ok", "output": "ok"}
        self.game_client.calls.clear()
        self.game_client.responses = [ok, ok, RuntimeError("flush boom"), ok]
        with self.assertRaises(RuntimeError):
            with self.backend.workshop_saves_held():
                self.fail("must not yield")
        self.assertEqual(
            [call[0] for call in self.game_client.calls],
            ["save-on", "save-off", "save-flush", "save-on"],
        )

    def test_hot_snapshot_without_live_editor_has_no_side_effect(self):
        with self.backend.workshop_saves_held():
            pass
        self.assertEqual(self.game_client.calls, [])

    def test_fence_stops_lingering_editor_and_noops_when_dark(self):
        self.backend.fence_pending_starts()
        self.assertEqual(self.game_client.calls, [])
        self.backend.start(self.goal)
        self.ready(self.reporter.docs["game-desired"]["generation"], self.goal.session)
        self.backend.fence_pending_starts()
        self.assertEqual(self.backend.observe(), [])
        self.assertEqual([call[0] for call in self.game_client.calls], ["stop"])

    def test_game_is_never_managed(self):
        with self.assertRaises(ValueError):
            self.backend.preflight(Goal("game", RELEASE))
        with self.assertRaises(ValueError):
            self.backend.stop(Process(Goal("game", RELEASE), True))
        self.ready(None, None, profile="game")
        with self.assertRaisesRegex(RuntimeError, "readiness unknown"):
            self.backend.observe()
        self.reporter.put("game-desired", {"schema": 1, "profile": "bad"})
        self.ready(str(uuid4()), str(uuid4()))
        observed = self.backend.observe()
        self.assertEqual([p.goal.profile for p in observed], ["workshop"])
        self.assertFalse(observed[0].ready)

    def test_export_limbo_validates_uuid_and_dispatches_cp_op(self):
        job = str(uuid4())
        with self.assertRaises(ValueError):
            self.backend.export_limbo(job)
        self.backend.maps = mock.Mock(limbo=mock.Mock())
        with self.assertRaises(ValueError):
            self.backend.export_limbo("not-a-uuid")
        self.backend.export_limbo(job)
        self.assertEqual(
            [(call[0], call[1]) for call in self.game_client.calls],
            [("export-limbo", job)],
        )

    def test_activity_reads_cp_state_doc(self):
        self.assertIsNone(self.backend.activity())
        doc = {
            "schema": 1,
            "generation": str(uuid4()),
            "players": 1,
            "busy": False,
            "updatedAt": self.now_ms[0],
        }
        self.reporter.put("game-activity", doc)
        self.assertEqual(self.backend.activity(), doc)

    def test_from_env_loads_verified_catalog_and_cp_clients(self):
        catalog_path = Path(self.temp.name) / "releases.json"
        catalog_path.write_text(json.dumps({RELEASE: self.record}))
        env = {
            "BF_RELEASES_FILE": str(catalog_path),
            "BLOCKFIELD_CP_URL": "https://cp.invalid",
            "BLOCKFIELD_CP_GATE_TOKEN": "gate-tok",
            "BLOCKFIELD_CP_WORKSHOP_GAME_GENERATION": str(uuid4()),
            "BF_WORKSHOP_READY_TTL_S": "45",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            backend = DokployBackend.from_env()
        self.assertEqual(set(backend.releases), {RELEASE})
        self.assertEqual(
            backend.releases[RELEASE]["imageDigest"], self.record["imageDigest"]
        )
        self.assertIsNotNone(backend.state_reporter)
        self.assertIsNotNone(backend.game_client)
        self.assertEqual(backend.ready_ttl_ms, 45_000)
        with mock.patch.dict(os.environ, {}, clear=True):
            bare = DokployBackend.from_env()
            self.assertEqual(bare.releases, {})
            self.assertIsNone(bare.state_reporter)
            self.assertIsNone(bare.game_client)


class LifecycleIntegrationTests(unittest.TestCase):
    """Controller + DokployBackend: request, STARTING, then stable workshop."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.data, self.lock = root / "data", root / "lock"
        self.data.mkdir()
        (self.lock / "workshop").mkdir(parents=True)
        self.record = release_record()
        self.reporter = cp_fixtures.Reporter()
        self.now_ms = [1_700_000_000_000]
        self.game_client = cp_fixtures.FakeGameClient()
        self.backend = DokployBackend(
            data=self.data,
            lock_dir=self.lock,
            releases={RELEASE: self.record},
            stop_timeout=2.0,
            poll=0.01,
            min_free_bytes=0,
            now_ms=lambda: self.now_ms[0],
            state_reporter=self.reporter,
            game_client=self.game_client,
        )
        self.reporter.put(
            "game-ready",
            {
                "schema": 1,
                "profile": "workshop",
                "generation": str(uuid4()),
                "session": str(uuid4()),
                "release": RELEASE,
                "running": False,
                "updatedAt": self.backend._now_ms(),
            },
        )
        self.controller = Controller(
            root / "state", self.backend, releases={RELEASE: release_record()}
        )
        self.controller.state_reporter = self.reporter

    def ready(self, generation, session, release=RELEASE):
        self.reporter.put(
            "game-ready",
            {
                "schema": 1,
                "profile": "workshop",
                "generation": generation,
                "session": session,
                "release": release,
                "running": True,
                "updatedAt": self.now_ms[0],
            },
        )

    def test_request_start_ready_stable_without_game(self):
        goal = Goal("workshop", RELEASE, str(uuid4()))
        self.controller.request(str(uuid4()), goal, 0)
        state = self.controller.step()
        self.assertEqual(state["phase"], "STARTING")
        selected = self.reporter.docs["game-desired"]
        self.assertEqual(selected["release"], RELEASE)
        self.ready(selected["generation"], goal.session)
        state = self.controller.step()
        self.assertEqual(
            (state["phase"], state["gate"], state["error"]),
            ("workshop", "builders", None),
        )
        self.assertEqual(state["desired"]["release"], RELEASE)

    def test_alive_unselected_java_blocks_explicit_recovery(self):
        self.reporter.put("game-desired", {"schema": 1, "profile": "game"})
        self.ready(str(uuid4()), str(uuid4()))
        goal = Goal("workshop", RELEASE, str(uuid4()))
        self.controller.request(str(uuid4()), goal, 0)
        state = self.controller.step()
        self.assertEqual(state["phase"], "ERROR")
        self.assertIn("explicit recovery", state["errorMessage"])

        def stop_on_cmd(game_op, **_kwargs):
            if game_op == "stop":
                self.reporter.put(
                    "game-ready",
                    {
                        **(self.reporter.docs.get("game-ready") or {}),
                        "running": False,
                        "updatedAt": self.now_ms[0],
                    },
                )
            return {"state": "effective", "code": "ok", "output": "ok"}

        self.game_client.handler = stop_on_cmd
        recovered = self.controller.step(recover=True)
        self.assertEqual(recovered["phase"], "STARTING")
        self.assertEqual(self.reporter.docs["game-desired"]["release"], RELEASE)

    def test_recover_adopts_live_matching_editor_without_restart(self):
        goal = Goal("workshop", RELEASE, str(uuid4()))
        self.controller.request(str(uuid4()), goal, 0)
        self.assertEqual(self.controller.step()["phase"], "STARTING")
        selected = self.reporter.docs["game-desired"]
        self.ready(selected["generation"], goal.session)
        self.assertEqual(self.controller.step()["phase"], "workshop")
        self.game_client.calls.clear()
        broken = self.controller.status()
        broken.update(phase="ERROR", gate="closed", error="Boom", errorMessage="boom")
        self.controller._write(broken)
        recovered = self.controller.step(recover=True)
        self.assertEqual(
            (recovered["phase"], recovered["gate"], recovered["error"]),
            ("workshop", "builders", None),
        )
        self.assertEqual(self.game_client.calls, [])
        self.assertEqual(self.controller.status()["phase"], "workshop")


class DeployWebhookTests(unittest.TestCase):
    """Stopped editor restarts through the Workshop app's Dokploy webhook."""

    WEBHOOK = "https://dokploy.example.test/api/app/deploy/WEBHOOK-SECRET-TOKEN"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.data, self.lock = root / "data", root / "lock"
        self.data.mkdir()
        (self.lock / "workshop").mkdir(parents=True)
        self.reporter = cp_fixtures.Reporter()
        self.game_client = cp_fixtures.FakeGameClient()
        self.backend = DokployBackend(
            data=self.data,
            lock_dir=self.lock,
            releases={RELEASE: release_record()},
            stop_timeout=1.0,
            poll=0.01,
            min_free_bytes=0,
            state_reporter=self.reporter,
            game_client=self.game_client,
        )
        self.backend._sleep = mock.Mock()
        self.goal = Goal("workshop", RELEASE, str(uuid4()))
        self.reporter.put(
            "game-ready",
            {
                "schema": 1,
                "profile": "workshop",
                "generation": str(uuid4()),
                "session": str(uuid4()),
                "release": RELEASE,
                "running": False,
                "updatedAt": self.backend._now_ms(),
            },
        )
        opener = mock.patch("urllib.request.OpenerDirector.open")
        self.open = opener.start()
        self.addCleanup(opener.stop)
        response = mock.MagicMock(name="webhook-response")
        response.status = 200
        response.read.return_value = b"ok"
        response.__enter__.return_value = response
        self.open.return_value = response

    def posted_urls(self):
        return [call.args[0].get_full_url() for call in self.open.call_args_list]

    def test_start_posts_deploy_webhook_once(self):
        with mock.patch.dict(os.environ, {"WORKSHOP_DEPLOY_WEBHOOK_URL": self.WEBHOOK}):
            self.backend.start(self.goal)
        self.assertEqual(self.open.call_count, 1)
        self.assertEqual(self.posted_urls(), [self.WEBHOOK])
        self.assertEqual(self.reporter.docs["game-desired"]["release"], RELEASE)
        self.assertFalse((self.data / ".blockfield-ready.json").exists())

    def test_success_posts_nothing_to_logs(self):
        with mock.patch.dict(os.environ, {"WORKSHOP_DEPLOY_WEBHOOK_URL": self.WEBHOOK}):
            with self.assertNoLogs(logging.getLogger("dokploy_backend")):
                self.backend.start(self.goal)

    def test_webhook_retry_then_success(self):
        failures = [
            urllib.error.URLError(ConnectionRefusedError("flap 1")),
            urllib.error.URLError(ConnectionRefusedError("flap 2")),
        ]
        self.open.side_effect = [*failures, self.open.return_value]
        with mock.patch.dict(os.environ, {"WORKSHOP_DEPLOY_WEBHOOK_URL": self.WEBHOOK}):
            self.backend.start(self.goal)
        self.assertEqual(self.open.call_count, 3)
        self.assertEqual(self.backend._sleep.call_count, 2)

    def test_ambiguous_webhook_failure_is_not_retried(self):
        for error in (TimeoutError("slow"), urllib.error.URLError(TimeoutError())):
            self.open.reset_mock()
            self.open.side_effect = error
            with mock.patch.dict(
                os.environ, {"WORKSHOP_DEPLOY_WEBHOOK_URL": self.WEBHOOK}
            ):
                with self.assertLogs(logging.getLogger("dokploy_backend"), "WARNING"):
                    self.backend.start(self.goal)
            self.assertEqual(self.open.call_count, 1)

    def test_empty_configured_webhook_file_fails_closed(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as handle:
            path = handle.name
        self.addCleanup(os.unlink, path)
        env = {
            "WORKSHOP_DEPLOY_WEBHOOK_URL": "",
            "WORKSHOP_DEPLOY_WEBHOOK_URL_FILE": path,
        }
        with mock.patch.dict(os.environ, env):
            with self.assertRaisesRegex(ValueError, "empty"):
                self.backend._deploy_webhook_url()

    def test_secret_emptied_after_preflight_does_not_raise_from_start(self):
        self.backend._deploy_webhook_url = mock.Mock(
            side_effect=[self.WEBHOOK, ValueError("secret file is empty")]
        )
        with self.assertLogs(logging.getLogger("dokploy_backend"), "WARNING"):
            self.backend.start(self.goal)
        self.assertEqual(self.open.call_count, 0)
        self.assertEqual(self.reporter.docs["game-desired"]["release"], RELEASE)

    def test_webhook_failure_keeps_selection_and_warns(self):
        self.open.side_effect = urllib.error.URLError(ConnectionRefusedError("down"))
        with mock.patch.dict(os.environ, {"WORKSHOP_DEPLOY_WEBHOOK_URL": self.WEBHOOK}):
            with self.assertLogs(
                logging.getLogger("dokploy_backend"), "WARNING"
            ) as logs:
                self.backend.start(self.goal)
        self.assertEqual(self.open.call_count, 3)
        output = "\n".join(logs.output)
        self.assertNotIn(self.WEBHOOK, output)
        self.assertEqual(self.reporter.docs["game-desired"]["release"], RELEASE)

    def test_unset_env_keeps_old_path_with_warning(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertLogs(
                logging.getLogger("dokploy_backend"), "WARNING"
            ) as logs:
                self.backend.start(self.goal)
        self.assertEqual(self.open.call_count, 0)
        self.assertIn("WORKSHOP_DEPLOY_WEBHOOK_URL", "\n".join(logs.output))
        self.assertEqual(self.reporter.docs["game-desired"]["release"], RELEASE)

    def test_rejected_scheme_raises_without_url_before_effects(self):
        bad = "http://dokploy.example.test/api/app/deploy/BAD-SECRET-TOKEN"
        with mock.patch.dict(os.environ, {"WORKSHOP_DEPLOY_WEBHOOK_URL": bad}):
            with self.assertRaises(ValueError) as ctx:
                self.backend.start(self.goal)
        self.assertNotIn(bad, str(ctx.exception))
        self.assertEqual(self.open.call_count, 0)
        self.assertNotIn("game-desired", self.reporter.docs)

    def test_recover_on_stopped_editor_posts_webhook_once(self):
        controller = Controller(
            Path(self.temp.name) / "state",
            self.backend,
            releases={RELEASE: release_record()},
        )
        controller.state_reporter = self.reporter
        with mock.patch.dict(os.environ, {"WORKSHOP_DEPLOY_WEBHOOK_URL": self.WEBHOOK}):
            controller.request(str(uuid4()), self.goal, 0)
            self.assertEqual(controller.step()["phase"], "STARTING")
            self.open.reset_mock()
            broken = controller.status()
            broken.update(
                phase="ERROR", gate="closed", error="Boom", errorMessage="boom"
            )
            controller._write(broken)
            with self.assertNoLogs(logging.getLogger("dokploy_backend")):
                recovered = controller.step(recover=True)
        self.assertEqual(recovered["phase"], "STARTING")
        self.assertEqual(self.open.call_count, 1)
        self.assertEqual(self.posted_urls(), [self.WEBHOOK])


class ShadowRunTests(unittest.TestCase):
    """Read-only shadow: ticks with no live editor leave gate/public identical."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.data, self.lock = root / "data", root / "lock"
        self.data.mkdir()
        (self.lock / "workshop").mkdir(parents=True)
        self.reporter = cp_fixtures.Reporter()
        self.game_client = cp_fixtures.FakeGameClient()
        self.backend = DokployBackend(
            data=self.data,
            lock_dir=self.lock,
            releases={RELEASE: release_record()},
            stop_timeout=1.0,
            poll=0.01,
            min_free_bytes=0,
            state_reporter=self.reporter,
            game_client=self.game_client,
        )
        self.reporter.put(
            "game-ready",
            {
                "schema": 1,
                "profile": "workshop",
                "generation": str(uuid4()),
                "session": str(uuid4()),
                "release": RELEASE,
                "running": False,
                "updatedAt": self.backend._now_ms(),
            },
        )
        self.controller = Controller(
            root / "state",
            self.backend,
            gate_file=root / "gate.json",
            public_file=root / "public.json",
            releases={RELEASE: release_record()},
        )
        self.controller.state_reporter = self.reporter

    def test_shadow_ticks_make_no_external_call(self):
        first = self.controller.step()
        gate_once = self.controller.gate_file.read_bytes()
        public_once = self.controller.public_file.read_bytes()
        for _ in range(3):
            self.controller.step()
            self.controller.status()
        self.assertEqual(self.controller.gate_file.read_bytes(), gate_once)
        self.assertEqual(self.controller.public_file.read_bytes(), public_once)
        self.assertEqual(first["phase"], "UNINITIALIZED")
        self.assertEqual(self.game_client.calls, [])


if __name__ == "__main__":
    unittest.main()
