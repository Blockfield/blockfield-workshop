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
import workshopctl

DRASL = "e10438e4797b49718cfecec6a8fa171b"


class WorkshopctlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.backend = FakeBackend()
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
            self.ctl("request", "game", "--release", "v1")[1]["phase"], "REQUESTED"
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
                "release": "v1",
                "session": session,
                "candidate": None,
            },
        )
        self.assertEqual(self.ctl("step")[1]["phase"], "STARTING")
        self.backend.ready(Goal("workshop", "v1", session))
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

    def test_recover_runs_explicit_step(self):
        self.ctl("request", "game", "--release", "v1")
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
        self.ctl("request", "game", "--release", "v1")
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
        self.ctl("request", "game", "--release", "v1")
        original = workshopctl.Controller.process_inbox

        def broken(controller):
            raise RecursionError("boom")

        workshopctl.Controller.process_inbox = broken
        self.addCleanup(setattr, workshopctl.Controller, "process_inbox", original)
        code, state = self.ctl("step")
        self.assertEqual((code, state["phase"]), (0, "STARTING"))
        gate = json.loads((self.root / "gate/gate.json").read_bytes())
        self.assertEqual(gate["backends"]["game"]["gate"], "public")

    def test_step_bootstraps_the_persistent_editor(self):
        code, state = self.ctl("step")
        self.assertEqual((code, state["phase"]), (0, "STARTING"))
        self.assertEqual(state["desired"]["profile"], "workshop")
        self.assertEqual(self.backend.events, ["start"])
        self.assertEqual(self.backend.processes[0].goal.profile, "game")

    def test_named_branch_session_commit_compare_and_preview(self):
        store = workshopctl._map_store(os.environ, self.backend)
        before = store.versions.head("konstaxa")["versionDigest"]
        code, branch = self.ctl(
            "branch-create",
            "--map-id",
            "konstaxa",
            "--branch",
            "editing",
            "--version-digest",
            before,
        )
        self.assertEqual(code, 0, branch)
        self.assertEqual(branch["name"], "editing")
        copy_id = str(uuid4())
        code, state = self.ctl(
            "request",
            "workshop",
            "--release",
            "v1",
            "--session",
            copy_id,
            "--maps",
            "konstaxa",
            "--branches",
            "konstaxa=editing",
        )
        self.assertEqual(code, 0, state)
        selected = json.loads((self.root / "controller/session.json").read_text())
        self.assertEqual(selected["branches"], {"konstaxa": "editing"})
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
            "--branch",
            "editing",
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
        code, preview = self.ctl(
            "merge-preview",
            "--map-id",
            "konstaxa",
            "--version-digest",
            committed["versionDigest"],
            "--expected-head",
            before,
        )
        self.assertEqual(code, 0, preview)
        self.assertEqual(preview["unresolved"], [])
        self.assertEqual(preview["targetVersion"], before)

    def test_branch_selection_validation_preserves_existing_editor(self):
        self.ctl("step")
        self.ready_editor()
        previous = (self.root / "controller/session.json").read_bytes()
        events = list(self.backend.events)
        for branches in (
            "konstaxa=main",
            "konstaxa=../bad",
            "konstaxa",
            "konstaxa=missing",
        ):
            code, error = self.ctl(
                "request",
                "workshop",
                "--session",
                str(uuid4()),
                "--maps",
                "konstaxa",
                "--branches",
                branches,
            )
            self.assertEqual(code, 1, error)
            self.assertEqual(
                (self.root / "controller/session.json").read_bytes(), previous
            )
            self.assertEqual(self.backend.events, events)

    def test_request_old_session_uuid_resumes_its_frozen_selection(self):
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
        self.ctl("step")
        self.ready_editor()
        game = self.backend.processes[0]
        code, state = self.ctl("request", "workshop", "--session", original["id"])
        self.assertEqual(code, 0, state)
        selected = json.loads((self.root / "controller/session.json").read_text())
        self.assertEqual(selected["maps"], original["maps"])
        self.assertEqual(selected["branches"], original["branches"])
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
                self.ctl("request", "game", "--release", "v1")[0], workshopctl.BUSY
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


if __name__ == "__main__":
    unittest.main()
