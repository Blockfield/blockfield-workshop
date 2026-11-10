import contextlib
import fcntl
import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock
from uuid import uuid4
from controller import Goal
from test_controller import FakeBackend
from release_fixtures import release_record
import workshopctl

DRASL = "e10438e4797b49718cfecec6a8fa171b"


class WorkshopctlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.backend = FakeBackend()
        self.write_catalog("v1.0.0", "v2.0.0", "v3.0.0")
        # The fake backend never becomes ready by itself: following a switch would only burn the real-time budget.
        os.environ["BF_STEP_FOLLOW_S"] = "0"
        self.addCleanup(os.environ.pop, "BF_STEP_FOLLOW_S", None)
        templates = self.root / "templates"
        configurations = self.root / "game-maps"
        configurations.mkdir()
        for map_id in ("konstaxa", "fields"):
            (templates / map_id / "region").mkdir(parents=True)
            (templates / map_id / "region/r.0.0.mca").write_bytes(map_id.encode())
            (configurations / f"{map_id}.json").write_text(
                json.dumps(
                    {
                        "id": map_id,
                        "name": map_id,
                        "bounds": {"minX": 0, "maxX": 10, "minZ": 0, "maxZ": 10},
                        "mode_data": {},
                    }
                )
            )
        environment = mock.patch.dict(
            os.environ,
            {
                "BF_MAP_ARCHIVE_DIR": str(self.root / "history"),
                "BF_MAP_ACTIVE_DIR": str(self.root / "active"),
                "BF_TEMPLATES_DIR": str(templates),
                "BF_CANDIDATES_DIR": str(self.root / "candidates"),
                "BF_LEGACY_CANDIDATES_DIR": str(self.root / "legacy"),
                "BF_WORKSHOP_DATA": str(self.root / "workshop-data"),
                "BF_PUBLISHED_MAPS_DIR": str(self.root / "published"),
                "BF_GAME_MAPS_DIR": str(self.root / "game-maps"),
            },
        )
        environment.start()
        self.addCleanup(environment.stop)
        store = workshopctl._map_store(os.environ, self.backend)
        for map_id in ("konstaxa", "fields"):
            version = store.versions.import_template(
                map_id, templates / map_id, configurations / f"{map_id}.json"
            )
            store.versions.activate(
                map_id, version, {"draslId": "fixture", "name": "fixture"}, None
            )

    def ctl(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = workshopctl.main(
                [
                    "--state-dir",
                    str(self.root / "controller"),
                    "--gate-file",
                    str(self.root / "gate" / "gate.json"),
                    "--public-file",
                    str(self.root / "public" / "public.json"),
                    "--admin-dir",
                    str(self.root / "admin"),
                    "--inbox-dir",
                    str(self.root / "inbox"),
                    *argv,
                ],
                lambda: self.backend,
            )
        return code, json.loads(out.getvalue()) if out.getvalue() else err.getvalue()

    def ready_editor(self):
        state = self.ctl("status")[1]["state"]
        self.backend.ready(Goal(**state["desired"]))
        return self.ctl("step")

    def test_switch_round_trip(self):
        code, state = self.ctl("status")
        self.assertEqual((code, state["state"]["phase"]), (0, "UNINITIALIZED"))
        self.assertEqual(self.ctl("request", "game")[0], 1)
        self.assertEqual(
            self.ctl("request", "game", "--release", "v1.0.0")[1]["phase"], "REQUESTED"
        )
        self.assertEqual(self.ctl("step")[1]["gate"], "closed")
        self.assertEqual(self.ready_editor()[1]["gate"], "builders")
        self.assertEqual(
            oct((self.root / "controller").stat().st_mode & 0o777), "0o700"
        )

        session = str(uuid4())
        self.assertEqual(
            self.ctl("members", f"{DRASL}:Nether:admin")[1]["members"][0]["role"],
            "admin",
        )
        state = self.ctl("request", "workshop", "--session", session)[1]
        self.assertEqual(
            state["desired"],
            {
                "profile": "workshop",
                "release": "v1.0.0",
                "session": session,
                "candidate": None,
                "modeId": None,
            },
        )
        self.assertEqual(self.ctl("step")[1]["phase"], "STARTING")
        self.backend.ready(Goal("workshop", "v1.0.0", session))
        self.assertEqual(self.ctl("step")[1]["gate"], "builders")
        gate = json.loads((self.root / "gate" / "gate.json").read_text())
        self.assertEqual(
            (
                gate["backends"]["workshop"]["gate"],
                gate["backends"]["workshop"]["allowed"][0]["name"],
            ),
            ("builders", "Nether"),
        )
        self.assertEqual(
            json.loads((self.root / "public" / "public.json").read_text())["mode"],
            "game",
        )

    def test_members_cli_parses_maps(self):
        code, result = self.ctl(
            "members",
            f"{DRASL}:Nether:admin",
            f"{'a' * 32}:Builder:builder:konstaxa+fields",
        )
        self.assertEqual(code, 0)
        self.assertEqual(result["members"][1]["maps"], ["konstaxa", "fields"])
        self.assertEqual(
            self.ctl(
                "members", f"{DRASL}:Nether:admin", f"{'a' * 32}:Builder:builder:nope"
            )[0],
            1,
        )

    def test_step_follows_only_committable_phases(self):
        # DRAINING is never committed by the controller; waiting on it (or
        # offering cancel for it) only confuses operators during incidents.
        self.assertEqual(
            workshopctl.FOLLOWED_PHASES, {"REQUESTED", "SAVING", "STARTING"}
        )

    def test_recover_runs_explicit_step(self):
        self.ctl("request", "game", "--release", "v1.0.0")
        self.ctl("request", "game", "--release", "v1.0.0")
        self.ctl("step")
        self.ready_editor()
        self.ctl("request", "workshop", "--session", str(uuid4()))
        self.backend.fail_start = True
        code, state = self.ctl("step")
        self.assertEqual((code, state["phase"]), (2, "ERROR"))
        self.assertEqual(self.ctl("request", "game")[0], 1)
        self.backend.fail_start = False
        code, state = self.ctl("request", "game", "--recover")
        self.assertEqual((code, state["phase"]), (0, "STARTING"))
        self.assertIn("fence", self.backend.events)

    def test_step_applies_inbox_before_reconcile(self):
        self.ctl("request", "game", "--release", "v1.0.0")
        self.ctl("request", "game", "--release", "v1.0.0")
        self.ctl("step")
        self.ready_editor()
        request_id = str(uuid4())
        (self.root / "inbox").mkdir()
        (self.root / "inbox" / f"{request_id}.json").write_text(
            json.dumps(
                {
                    "schema": 1,
                    "id": request_id,
                    "kind": "mode",
                    "actor": {"draslId": DRASL, "name": "Nether"},
                    "createdAt": int(time.time() * 1000),
                    "profile": "workshop",
                    "expectedRevision": 1,
                }
            )
        )
        code, state = self.ctl("step")
        self.assertEqual(
            (code, state["phase"], state["operation"]), (0, "STARTING", request_id)
        )
        self.assertTrue(
            json.loads(
                (self.root / "admin" / "results" / f"{request_id}.json").read_text()
            )["ok"]
        )
        self.assertEqual(
            json.loads((self.root / "admin" / "status.json").read_text())["phase"],
            "STARTING",
        )

    def test_inbox_failure_does_not_block_reconcile(self):
        self.ctl("request", "game", "--release", "v1.0.0")
        original = workshopctl.Controller.process_inbox

        def broken(controller):
            raise RecursionError("boom")

        workshopctl.Controller.process_inbox = broken
        self.addCleanup(setattr, workshopctl.Controller, "process_inbox", original)
        code, state = self.ctl("step")
        self.assertEqual((code, state["phase"]), (0, "STARTING"))
        gate = json.loads((self.root / "gate/gate.json").read_bytes())
        self.assertEqual(gate["backends"]["game"]["gate"], "public")

    def test_step_runs_cp_sync_when_configured(self):
        self.ctl("request", "game", "--release", "v1.0.0")
        calls = []

        class FakeSync:
            def __init__(self, *args, **kwargs):
                pass

            def sync(self, controller):
                calls.append(controller)
                return {"applied": 0, "rejected": 0, "acked": 0}

        with (
            mock.patch.dict(
                os.environ,
                {
                    "BLOCKFIELD_CP_URL": "https://cp.invalid",
                    "BLOCKFIELD_CP_AGENT_ID": "workshop-test",
                    "BLOCKFIELD_CP_AGENT_TOKEN": "t",
                },
            ),
            mock.patch("cp_client.CPClient", FakeSync),
        ):
            code, state = self.ctl("step")
        self.assertEqual((code, state["phase"]), (0, "STARTING"))
        self.assertEqual(len(calls), 1)

    def test_cp_failure_does_not_block_reconcile(self):
        self.ctl("request", "game", "--release", "v1.0.0")

        class BrokenSync:
            def __init__(self, *args, **kwargs):
                pass

            def sync(self, controller):
                raise RecursionError("boom")

        with (
            mock.patch.dict(
                os.environ,
                {
                    "BLOCKFIELD_CP_URL": "https://cp.invalid",
                    "BLOCKFIELD_CP_AGENT_ID": "workshop-test",
                    "BLOCKFIELD_CP_AGENT_TOKEN": "t",
                },
            ),
            mock.patch("cp_client.CPClient", BrokenSync),
        ):
            code, state = self.ctl("step")
        self.assertEqual((code, state["phase"]), (0, "STARTING"))
        gate = json.loads((self.root / "gate/gate.json").read_bytes())
        self.assertEqual(gate["backends"]["game"]["gate"], "public")

    def test_step_requires_explicit_initial_target_import(self):
        self.assertEqual(self.ctl("step")[1]["phase"], "UNINITIALIZED")
        self.assertEqual(self.backend.events, [])
        self.ctl("request", "game", "--release", "v1.0.0")
        code, state = self.ctl("step")
        self.assertEqual((code, state["phase"]), (0, "STARTING"))
        self.assertEqual(state["desired"]["profile"], "workshop")
        self.assertEqual(self.backend.events, ["start"])
        self.assertEqual(self.backend.processes[0].goal.profile, "game")

    def test_linear_session_commit_compare_and_snapshot(self):
        store = workshopctl._map_store(os.environ, self.backend)
        before = store.versions.head("konstaxa")["versionDigest"]
        code, snap = self.ctl(
            "snapshot-create",
            "--map-id",
            "konstaxa",
            "--name",
            "milestone",
            "--version-digest",
            before,
        )
        self.assertEqual(code, 0, snap)
        self.assertEqual(snap["versionDigest"], before)
        copy_id = str(uuid4())
        code, state = self.ctl(
            "request",
            "workshop",
            "--release",
            "v1.0.0",
            "--session",
            copy_id,
            "--maps",
            "konstaxa",
        )
        self.assertEqual(code, 0, state)
        selected = json.loads((self.root / "controller/session.json").read_text())
        self.assertEqual(selected["maps"], ["konstaxa"])
        self.assertNotIn("branches", selected)
        self.ctl("request", "game", "--release", "v1.0.0")
        self.ctl("step")
        self.ready_editor()
        region = (
            store.versions.working_copy("konstaxa", copy_id) / "world/region/r.0.0.mca"
        )
        region.write_bytes(b"CLI edit")
        game = self.backend.processes[0]
        code, committed = self.ctl(
            "commit",
            "--map-id",
            "konstaxa",
            "--working-copy-id",
            copy_id,
            "--expected-head",
            before,
            "--message",
            "Edit geometry",
        )
        self.assertEqual(code, 0, committed)
        self.assertNotEqual(committed["versionDigest"], before)
        self.assertIn(game, self.backend.processes)
        code, diff = self.ctl(
            "compare",
            "--map-id",
            "konstaxa",
            "--before",
            before,
            "--after",
            committed["versionDigest"],
        )
        self.assertEqual(code, 0, diff)
        self.assertIn(
            "world/region/r.0.0.mca", [entry["path"] for entry in diff["files"]]
        )
        code, listed = self.ctl(
            "snapshots",
            "--map-id",
            "konstaxa",
        )
        self.assertEqual(code, 0, listed)
        self.assertEqual([snap["name"] for snap in listed["snapshots"]], ["milestone"])

    def test_unknown_map_selection_preserves_existing_editor(self):
        self.ctl("request", "game", "--release", "v1.0.0")
        self.ctl("step")
        self.ready_editor()
        previous = (self.root / "controller/session.json").read_bytes()
        events = list(self.backend.events)
        code, error = self.ctl(
            "request",
            "workshop",
            "--session",
            str(uuid4()),
            "--maps",
            "missing",
        )
        self.assertEqual(code, 1, error)
        self.assertEqual((self.root / "controller/session.json").read_bytes(), previous)
        self.assertEqual(self.backend.events, events)

    def test_request_old_session_uuid_resumes_its_frozen_selection(self):
        self.ctl("request", "game", "--release", "v1.0.0")
        self.ctl("step")
        self.ready_editor()
        original = json.loads((self.root / "controller/session.json").read_text())
        store = workshopctl._map_store(os.environ, self.backend)
        old_region = (
            store.versions.working_copy("konstaxa", original["id"])
            / "world/region/r.0.0.mca"
        )
        old_region.write_bytes(b"old CLI draft")
        self.ctl("request", "workshop", "--session", str(uuid4()), "--maps", "fields")
        self.ctl("request", "game", "--release", "v1.0.0")
        self.ctl("step")
        self.ready_editor()
        game = self.backend.processes[0]
        code, state = self.ctl("request", "workshop", "--session", original["id"])
        self.assertEqual(code, 0, state)
        selected = json.loads((self.root / "controller/session.json").read_text())
        self.assertEqual(selected["maps"], original["maps"])
        self.assertNotIn("branches", selected)
        self.ctl("request", "game", "--release", "v1.0.0")
        self.ctl("step")
        self.ready_editor()
        self.assertEqual(old_region.read_bytes(), b"old CLI draft")
        self.assertIn(game, self.backend.processes)

    def test_offline_operations_hold_both_backend_locks(self):
        self.backend.processes = []
        self.backend.lock_dir = self.root / "locks"
        for profile in ("game", "workshop"):
            (self.backend.lock_dir / profile).mkdir(parents=True)
        with workshopctl._offline(self.backend):
            for profile in ("game", "workshop"):
                with (self.backend.lock_dir / profile / "backend.lock").open(
                    "a"
                ) as lock:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_bad_member_and_busy_lock(self):
        self.assertEqual(self.ctl("members", "nope")[0], 1)
        self.ctl("status")
        with open(self.root / "controller" / "controller.lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.assertEqual(
                self.ctl("request", "game", "--release", "v1.0.0")[0], workshopctl.BUSY
            )

    def test_offline_import_is_staged_and_activation_requires_exact_head(self):
        self.backend.processes = []
        world = self.root / "new-map"
        (world / "region").mkdir(parents=True)
        source = world / "region/r.0.0.mca"
        source.write_bytes(b"source retained")
        config = self.root / "new-map.json"
        config.write_text(
            json.dumps(
                {
                    "id": "custom_arena",
                    "kind": "arena",
                    "name": "Independent arena",
                    "bounds": {"minX": 0, "maxX": 175, "minZ": 0, "maxZ": 175},
                    "mode_data": {},
                    "supported_modes": [],
                }
            )
        )
        code, result = self.ctl(
            "import-map",
            "--map-id",
            "custom_arena",
            "--world",
            str(world),
            "--config",
            str(config),
        )
        self.assertEqual(code, 0, result)
        self.assertFalse(result["active"])
        self.assertIsNone(result["expectedHead"])
        self.assertFalse((self.root / "active/custom_arena/head.json").exists())
        self.assertEqual(source.read_bytes(), b"source retained")
        catalog = json.loads((self.root / "admin/maps.json").read_bytes())["maps"]
        self.assertEqual(
            next(
                entry["kind"] for entry in catalog if entry["mapId"] == "custom_arena"
            ),
            "arena",
        )
        code, head = self.ctl(
            "activate-map",
            "--map-id",
            "custom_arena",
            "--version-digest",
            result["versionDigest"],
            "--expected-head",
            "none",
        )
        self.assertEqual(code, 0, head)
        self.assertEqual(head["versionDigest"], result["versionDigest"])
        self.assertEqual(head["activatedBy"], {"draslId": "host", "name": "host"})
        code, error = self.ctl(
            "activate-map",
            "--map-id",
            "custom_arena",
            "--version-digest",
            result["versionDigest"],
            "--expected-head",
            "none",
        )
        self.assertEqual(code, 1)
        self.assertIn("stale", error)
        self.assertEqual(self.backend.events, [])

    def test_import_refuses_running_backend_and_real_lobby_spawn_mismatch(self):
        world = self.root / "game/world"
        (world / "region").mkdir(parents=True)
        (world / "region/r.0.0.mca").write_bytes(b"lobby source")
        config = self.root / "lobby.json"
        metadata = {
            "id": "lobby",
            "kind": "lobby",
            "name": "Lobby",
            "bounds": {"minX": -400, "maxX": -200, "minZ": 500, "maxZ": 700},
            "mode_data": {"lobby": {"spawn": {"x": -277.5, "y": -40, "z": 604.5}}},
            "supported_modes": [],
        }
        config.write_text(json.dumps(metadata))
        from docker_backend import App

        self.backend.apps = {"game": App("fake-game", self.root / "game")}
        self.backend.container_spec = lambda profile: {}
        self.backend.container_env = lambda spec: {}
        command = (
            "import-map",
            "--map-id",
            "lobby",
            "--world",
            str(world),
            "--config",
            str(config),
        )
        code, error = self.ctl(*command)
        self.assertEqual(code, 1)
        self.assertIn("stopped", error)
        self.backend.processes = []
        metadata["mode_data"]["lobby"]["spawn"]["x"] = 0
        config.write_text(json.dumps(metadata))
        code, error = self.ctl(*command)
        self.assertEqual(code, 1)
        self.assertIn("Hub spawn", error)
        metadata["mode_data"]["lobby"]["spawn"]["x"] = -277.5
        config.write_text(json.dumps(metadata))
        code, result = self.ctl(*command)
        self.assertEqual(code, 0, result)
        self.assertFalse(result["active"])
        self.assertEqual((world / "region/r.0.0.mca").read_bytes(), b"lobby source")
        self.assertFalse((self.root / "active/lobby/head.json").exists())

    def test_offline_gate_accepts_stopped_inventory(self):
        stopped = {
            "unknown": 0,
            "game": {"tasks": 0, "java": 0},
            "workshop": {"tasks": 0, "java": 0},
        }
        backend = mock.Mock()
        backend.inventory = lambda: dict(stopped)
        workshopctl._require_stopped(backend)

    def write_catalog(self, *releases):
        (self.root / "controller").mkdir(parents=True, exist_ok=True)
        catalog = {
            release: release_record(release, chr(ord("a") + index))
            for index, release in enumerate(releases)
        }
        (self.root / "controller/releases.json").write_text(json.dumps(catalog))
        return catalog

    def test_verified_release_catalog_gates_offline_staging(self):
        catalog = self.write_catalog("v1.0.0", "v2.0.0")
        session = str(uuid4())
        self.backend.processes = []
        code, state = self.ctl(
            "request", "workshop", "--session", session, "--release", "v1.0.0"
        )
        self.assertEqual(code, 0, state)
        self.assertEqual(state["desired"]["release"], "v1.0.0")
        code, target = self.ctl("target")
        self.assertEqual(code, 0, target)
        self.assertEqual(
            {key: target["target"][key] for key in catalog["v1.0.0"]}, catalog["v1.0.0"]
        )
        code, state = self.ctl("step")
        self.assertEqual((code, state["phase"]), (0, "STARTING"))
        self.assertEqual(
            self.ctl("target")[1]["target"]["imageDigest"],
            catalog["v1.0.0"]["imageDigest"],
        )
        self.backend.ready(Goal("workshop", "v1.0.0", session))
        self.assertEqual(self.ctl("step")[1]["gate"], "builders")
        code, error = self.ctl(
            "request", "workshop", "--session", str(uuid4()), "--release", "v9"
        )
        self.assertEqual(code, 1)
        self.assertIn("verified published", error)
        self.assertEqual(
            self.ctl("target")[1]["target"]["imageDigest"],
            catalog["v1.0.0"]["imageDigest"],
        )

    def test_catalog_rejects_floating_or_foreign_digest(self):
        catalog = self.write_catalog("v1.0.0")
        catalog["v1.0.0"]["imageDigest"] = "ghcr.io/blockfield/blockfield-server:latest"
        (self.root / "controller/releases.json").write_text(json.dumps(catalog))
        code, error = self.ctl(
            "request", "workshop", "--session", str(uuid4()), "--release", "v1.0.0"
        )
        self.assertEqual(code, 1)
        self.assertIn("immutable server imageDigest", error)

    def test_desired_command_reports_owned_schema_2(self):
        code, state = self.ctl("status")
        self.assertEqual((code, state["state"]["phase"]), (0, "UNINITIALIZED"))
        code, result = self.ctl("desired")
        self.assertEqual((code, result["desired"]), (0, None))
        session = str(uuid4())
        self.assertEqual(
            self.ctl(
                "request", "workshop", "--session", session, "--release", "v1.0.0"
            )[0],
            0,
        )
        code, result = self.ctl("desired")
        self.assertEqual(code, 0)
        self.assertEqual(result["desired"]["schema"], 2)
        self.assertEqual(result["desired"]["workshop"]["release"], "v1.0.0")
        self.assertEqual(result["desired"]["revision"], 1)
        on_disk = json.loads((self.root / "controller" / "desired.json").read_text())
        self.assertEqual(on_disk, result["desired"])

    def test_desired_file_wiring(self):
        kwargs = dict(
            state_dir=str(self.root / "controller"),
            gate_file=str(self.root / "gate" / "gate.json"),
            public_file=str(self.root / "public" / "public.json"),
            admin_dir=str(self.root / "admin"),
            inbox_dir=str(self.root / "inbox"),
        )
        controller, _ = workshopctl.build_controller(
            dict(os.environ), lambda: self.backend, **kwargs
        )
        self.assertEqual(
            controller.desired_file, self.root / "controller" / "desired.json"
        )
        custom = self.root / "custom-desired.json"
        controller, _ = workshopctl.build_controller(
            {**os.environ, "BF_DESIRED_FILE": str(custom)},
            lambda: self.backend,
            **kwargs,
        )
        self.assertEqual(controller.desired_file, custom)

    def test_gate_reporter_wiring(self):
        kwargs = dict(
            state_dir=str(self.root / "controller"),
            gate_file=str(self.root / "gate" / "gate.json"),
            public_file=str(self.root / "public" / "public.json"),
            admin_dir=str(self.root / "admin"),
            inbox_dir=str(self.root / "inbox"),
        )
        controller, _ = workshopctl.build_controller(
            dict(os.environ), lambda: self.backend, **kwargs
        )
        self.assertIsNone(controller.gate_reporter)
        controller, _ = workshopctl.build_controller(
            {
                **os.environ,
                "BLOCKFIELD_CP_URL": "https://cp.invalid",
                "BLOCKFIELD_CP_GATE_TOKEN": "gate-token",
            },
            lambda: self.backend,
            **kwargs,
        )
        self.assertEqual(
            controller.gate_reporter._url(),
            "https://cp.invalid/v2/settings/gate/workshop",
        )


if __name__ == "__main__":
    unittest.main()


class DigestGoalTests(unittest.TestCase):
    def test_digest_goal_stages_offline(self):
        inner = WorkshopctlTests("test_verified_release_catalog_gates_offline_staging")
        inner.setUp()
        self.addCleanup(inner.temp.cleanup)
        # inner is never torn down: take over its environment restoration.
        self.addCleanup(os.environ.pop, "BF_STEP_FOLLOW_S", None)
        self.root, self.backend = inner.root, inner.backend
        catalog = inner.write_catalog("v1.0.0")
        digest = catalog["v1.0.0"]["imageDigest"].split("@", 1)[1]
        catalog[digest] = catalog["v1.0.0"]
        (self.root / "controller/releases.json").write_text(json.dumps(catalog))
        session = str(uuid4())
        code, state = inner.ctl(
            "request", "workshop", "--session", session, "--release", digest
        )
        self.assertEqual(code, 0, state)
        self.assertEqual(state["desired"]["release"], digest)
        code, target = inner.ctl("target")
        self.assertEqual(code, 0, target)
        self.assertEqual(target["target"]["release"], "v1.0.0")
        self.assertEqual(
            target["target"]["imageDigest"], catalog["v1.0.0"]["imageDigest"]
        )
        self.assertEqual(inner.ctl("step")[1]["phase"], "STARTING")
        self.backend.ready(Goal("workshop", digest, session))
        self.assertEqual(inner.ctl("step")[1]["gate"], "builders")
