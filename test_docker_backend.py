"""DockerBackend against fake `docker`/`pgrep` executables on PATH and a fake /proc."""

import json
import os
import shlex
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from uuid import uuid4
from controller import Controller, Goal, Process
from docker_backend import (
    App,
    DockerBackend,
    JAVA_PATTERN,
    READY_FILE,
    _check_readonly_view,
)

GAME_CID, WORKSHOP_CID, OTHER_CID = "a" * 64, "b" * 64, "c" * 64
GAME_IMAGE = "ghcr.io/blockfield/blockfield-server@sha256:" + "d" * 64
GAME_IMAGE_ID = "sha256:" + "e" * 64
FOREIGN_DIGEST = "ghcr.io/other/server@sha256:" + "f" * 64

FAKE = r"""
import json, os, sys
from pathlib import Path
state_path, log = Path(os.environ["BF_FAKE"]), Path(os.environ["BF_FAKE_LOG"])
proc = Path(os.environ["BF_FAKE_PROC"])
state = json.loads(state_path.read_text())
argv = [Path(sys.argv[1]).name] + sys.argv[2:]
with log.open("a") as handle:
    handle.write(json.dumps(argv) + "\n")
services = state["services"]
def java_of(cid):
    return [pid for pid, owner in state["java"].items() if owner == cid]
if argv[0] == "pgrep":
    print("\n".join(state["java"]))
    sys.exit(0 if state["java"] else 1)
if argv[1:3] == ["service", "ps"]:
    print("\n".join(["Running 1 second ago"] * services[argv[3]]["replicas"]))
elif argv[1:3] == ["service", "inspect"]:
    if argv[-1] == "{{json .Spec.Mode}}":
        print(json.dumps(services[argv[3]].get("mode", {"Replicated": {"Replicas": services[argv[3]]["replicas"]}})))
    else:
        print(json.dumps(services[argv[3]].get("spec", {})))
elif argv[1] == "ps":
    name = argv[4].split("=", 2)[2]
    cid = services[name]["container"]
    if services[name]["replicas"] or java_of(cid):
        print(cid)
elif argv[1:3] == ["service", "scale"]:
    name, replicas = argv[4].split("=")
    service = services[name]
    service["replicas"] = int(replicas)
    if replicas == "0" and not state.get("stop_leaves_java"):
        for pid in java_of(service["container"]):
            del state["java"][pid]
    if replicas == "1" and state.get("autostart", True) and not java_of(service["container"]):
        pid = str(1000 + int(service["container"][0], 16))
        state["java"][pid] = service["container"]
        (proc / pid).mkdir(parents=True, exist_ok=True)
        (proc / pid / "cgroup").write_text("0::/system.slice/docker-" + service["container"] + ".scope\n")
        (proc / pid / "comm").write_text("java\n")
elif argv[1:3] == ["service", "update"]:
    services[argv[-1]].setdefault("spec", {})["Image"] = argv[-2]
elif argv[1] == "inspect":
    print(state["container_images"].get(argv[-1], ""))
elif argv[1:3] == ["image", "inspect"]:
    print(json.dumps(state["repo_digests"].get(argv[-1])))
elif argv[1] == "exec":
    print(state.get("rcon", {}).get(" ".join(argv[4:]), ""))
state_path.write_text(json.dumps(state))
"""


class DockerBackendTests(unittest.TestCase):
    def test_explicit_playtest_mode_survives_selection_and_four_key_readiness(self):
        session = str(uuid4())
        sha = "ab" * 32
        self.running("game")
        self.ready("game", generation=None, session=None, release="v1")
        self.backend.maps = mock.Mock(extract=lambda digest, target: "zero_map")
        goal = Goal("test", "v1", session, sha, "training")
        self.backend.start(goal)
        selected = self.backend.selection()
        self.assertEqual(selected["modeId"], "training")
        self.backend.maps.validate_test.assert_called_with(sha, "training")
        self.ready(
            "workshop", generation=selected["generation"], session=session, release="v1"
        )
        ready = self.data["workshop"] / READY_FILE
        document = json.loads(ready.read_bytes()) | {"profile": "test"}
        ready.write_text(json.dumps(document))
        self.assertEqual(set(document), {"profile", "generation", "session", "release"})
        self.assertEqual(
            self.backend.observe(),
            [Process(Goal("game", "v1"), True), Process(goal, True)],
        )
        path = self.lock / "workshop/profile.json"
        path.write_text(json.dumps(selected | {"modeId": "unknown"}))
        observed = self.backend.observe()
        self.assertTrue(observed[0].ready)
        self.assertFalse(observed[1].ready)
        self.assertEqual(observed[1].goal.release, "unknown")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.bin, self.proc, self.lock = root / "bin", root / "proc", root / "lock"
        self.data = {"game": root / "game", "workshop": root / "workshop"}
        for path in (self.bin, self.proc, self.lock, *self.data.values()):
            path.mkdir()
        for name in ("docker", "pgrep"):
            # Kernel shebang parsing splits paths at spaces; never fall through to real docker/pgrep.
            (self.bin / name).write_text(
                f'#!/bin/sh\nexec {shlex.quote(sys.executable)} -c {shlex.quote(FAKE)} "$0" "$@"\n'
            )
            (self.bin / name).chmod(0o755)
        self.state_file, self.log = root / "state.json", root / "calls.log"
        self.state = {
            "services": {
                "bf-game": {
                    "replicas": 0,
                    "container": GAME_CID,
                    # Dokploy's spec tracks :latest; only the running container knows the digest.
                    "spec": {"Image": "ghcr.io/blockfield/blockfield-server:latest"},
                },
                "bf-workshop": {
                    "replicas": 0,
                    "container": WORKSHOP_CID,
                    "spec": {"Image": GAME_IMAGE},
                },
            },
            "java": {},
            "rcon": {"save-all flush": "Saved the game"},
            "container_images": {GAME_CID: GAME_IMAGE_ID},
            "repo_digests": {GAME_IMAGE_ID: [GAME_IMAGE]},
        }
        self.save()
        env = {
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "BF_FAKE": str(self.state_file),
            "BF_FAKE_LOG": str(self.log),
            "BF_FAKE_PROC": str(self.proc),
        }
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.backend = DockerBackend(
            {
                "game": App("bf-game", self.data["game"]),
                "workshop": App("bf-workshop", self.data["workshop"]),
            },
            self.lock,
            proc=self.proc,
            stop_timeout=0.5,
            poll=0.01,
            min_free_bytes=0,
        )

    def save(self):
        self.state_file.write_text(json.dumps(self.state))

    def load(self):
        self.state = json.loads(self.state_file.read_text())

    def java(self, pid, cid, comm="java"):
        self.load()
        self.state["java"][str(pid)] = cid
        (self.proc / str(pid)).mkdir(exist_ok=True)
        (self.proc / str(pid) / "cgroup").write_text(
            f"0::/system.slice/docker-{cid}.scope\n" if cid else "0::/user.slice\n"
        )
        (self.proc / str(pid) / "comm").write_text(comm + "\n")
        self.save()

    def running(self, profile, pid=None):
        pid = pid if pid is not None else 100 if profile == "game" else 200
        self.load()
        service = self.state["services"][f"bf-{profile}"]
        service["replicas"] = 1
        self.save()
        self.java(pid, service["container"])

    def ready(self, profile, **document):
        (self.data[profile] / READY_FILE).write_text(
            json.dumps({"profile": profile, **document})
        )

    def calls(self):
        return (
            [json.loads(line) for line in self.log.read_text().splitlines()]
            if self.log.exists()
            else []
        )

    def test_only_allowlisted_commands(self):
        good = [
            [
                "docker",
                "service",
                "inspect",
                "bf-workshop",
                "--format",
                "{{json .Spec.TaskTemplate.ContainerSpec}}",
            ],
            ["docker", "service", "scale", "--detach", "bf-workshop=0"],
            [
                "docker",
                "service",
                "update",
                "--detach",
                "--image",
                GAME_IMAGE,
                "bf-workshop",
            ],
            ["docker", "exec", WORKSHOP_CID, "rcon-cli", "save-all", "flush"],
            ["pgrep", "-f", JAVA_PATTERN],
            ["docker", "inspect", "--format", "{{.Image}}", GAME_CID],
            [
                "docker",
                "image",
                "inspect",
                "--format",
                "{{json .RepoDigests}}",
                GAME_IMAGE_ID,
            ],
        ]
        bad = [
            ["docker", "inspect", GAME_CID],
            ["docker", "inspect", "--format", "{{json .Config.Env}}", GAME_CID],
            ["docker", "inspect", "--format", "{{.Image}}", "bf-game"],
            ["docker", "image", "inspect", GAME_IMAGE_ID],
            [
                "docker",
                "image",
                "inspect",
                "--format",
                "{{json .Config}}",
                GAME_IMAGE_ID,
            ],
            [
                "docker",
                "image",
                "inspect",
                "--format",
                "{{json .RepoDigests}}",
                GAME_IMAGE,
            ],
            [
                "docker",
                "service",
                "inspect",
                "other",
                "--format",
                "{{json .Spec.TaskTemplate.ContainerSpec}}",
            ],
            ["docker", "rm", "-f", "bf-game"],
            ["docker", "service", "scale", "--detach", "bf-game=2"],
            ["docker", "service", "scale", "--detach", "bf-game=0"],
            [
                "docker",
                "service",
                "update",
                "--detach",
                "--image",
                GAME_IMAGE,
                "bf-game",
            ],
            [
                "docker",
                "service",
                "update",
                "--detach",
                "--image",
                "ghcr.io/blockfield/blockfield-server:latest",
                "bf-workshop",
            ],
            ["docker", "service", "scale", "--detach", "other=1"],
            ["docker", "exec", GAME_CID, "rcon-cli", "op", "Nether"],
            ["docker", "exec", "bf-game", "rcon-cli", "save-all", "flush"],
            ["docker", "exec", GAME_CID, "sh", "-c", "id"],
            ["pgrep", "-f", "java"],
            ["sh", "-c", "docker ps"],
        ]
        for argv in good:
            self.assertTrue(self.backend.allowed(argv), argv)
        for argv in bad:
            self.assertFalse(self.backend.allowed(argv), argv)
            with self.assertRaises(PermissionError):
                self.backend.run(argv)
        self.assertEqual(self.calls(), [])

    def test_canonical_workshop_mounts_and_session_must_agree_before_start(self):
        from candidates import MapStore

        # Before the spec is built: running() reloads state and would detach the spec dict mutated below.
        self.running("game")
        root = Path(self.temp.name)
        self.backend.maps = MapStore(
            self.data["workshop"],
            root / "templates",
            root / "published",
            root / "archive/candidates",
            root / "archive/backups",
            archive_root=root / "archive",
            active_root=root / "active",
        )
        self.backend.admin_dir = root / "admin"
        self.backend.admin_dir.mkdir()
        spec = {
            "Env": [
                "BLOCKFIELD_PROFILE=workshop",
                "BF_MAP_ACTIVE_DIR=/maps-active",
                "BLOCKFIELD_WORKSHOP_DRAFTS_DIR=/maps-active/drafts",
            ],
            "Mounts": [
                {
                    "Type": "bind",
                    "Source": str(root / "active"),
                    "Target": "/maps-active",
                    "ReadOnly": True,
                },
                {
                    "Type": "bind",
                    "Source": str(root / "active/drafts"),
                    "Target": "/maps-active/drafts",
                    "ReadOnly": False,
                },
                {
                    "Type": "bind",
                    "Source": str(root / "admin"),
                    "Target": "/workshop-admin",
                    "ReadOnly": True,
                },
                {
                    "Type": "bind",
                    "Source": str(self.lock),
                    "Target": "/backend-lock",
                    "ReadOnly": False,
                },
            ],
        }
        self.state["services"]["bf-workshop"]["spec"] = spec
        self.save()
        goal = Goal("workshop", "v1", str(uuid4()))
        self.backend.preflight(goal)
        spec["Mounts"][0]["ReadOnly"] = False
        self.save()
        with self.assertRaisesRegex(ValueError, "mount"):
            self.backend.preflight(goal)
        self.assertFalse((self.lock / "profile.json").exists())
        spec["Mounts"][0]["ReadOnly"] = True
        self.save()
        (self.backend.admin_dir / "session.json").write_text(
            json.dumps({"schema": 1, "id": str(uuid4()), "maps": ["limbo"]})
        )
        with self.assertRaisesRegex(ValueError, "mismatch"):
            self.backend.start(goal)
        self.assertFalse((self.lock / "profile.json").exists())
        self.assertFalse(
            any(call[1:3] == ["service", "scale"] for call in self.calls())
        )

    def test_proxy_stopped_proof_is_configured_read_only_and_checks_orphan_containers(
        self,
    ):
        with self.assertRaisesRegex(ValueError, "BF_PROXY_SERVICE"):
            self.backend.proxy_stopped()
        self.backend.proxy_service = "bf-proxy"
        self.state["services"]["bf-proxy"] = {"replicas": 0, "container": OTHER_CID}
        self.save()
        self.assertTrue(self.backend.proxy_stopped())
        self.java(200, OTHER_CID)
        self.assertFalse(self.backend.proxy_stopped())
        self.load()
        self.state["java"] = {}
        self.state["services"]["bf-proxy"]["replicas"] = 1
        self.save()
        self.assertFalse(self.backend.proxy_stopped())
        self.assertFalse(
            self.backend.allowed(
                ["docker", "service", "scale", "--detach", "bf-proxy=0"]
            )
        )
        self.assertFalse(
            any(call[1:3] == ["service", "scale"] for call in self.calls())
        )

    def test_malformed_proxy_mode_never_counts_as_stopped(self):
        self.backend.proxy_service = "bf-proxy"
        self.state["services"]["bf-proxy"] = {"replicas": 0, "container": OTHER_CID}
        for mode in (
            {"Global": {}},
            {"Replicated": None},
            {"Replicated": {"Replicas": False}},
            {"Replicated": {"Replicas": "0"}},
        ):
            self.state["services"]["bf-proxy"]["mode"] = mode
            self.save()
            self.assertFalse(self.backend.proxy_stopped())

    def test_native_export_command_is_one_canonical_job_not_arbitrary_rcon(self):
        job = str(uuid4())
        self.assertTrue(
            self.backend.allowed(
                [
                    "docker",
                    "exec",
                    WORKSHOP_CID,
                    "rcon-cli",
                    "bf",
                    "workshop",
                    "export-limbo",
                    job,
                ]
            )
        )
        for bad in ("../job", job.upper(), job + " extra"):
            self.assertFalse(
                self.backend.allowed(
                    [
                        "docker",
                        "exec",
                        WORKSHOP_CID,
                        "rcon-cli",
                        "bf",
                        "workshop",
                        "export-limbo",
                        bad,
                    ]
                )
            )
        self.assertFalse(
            self.backend.allowed(
                [
                    "docker",
                    "exec",
                    WORKSHOP_CID,
                    "rcon-cli",
                    "bf",
                    "workshop",
                    "import-schematic",
                    job,
                    "lobby.schem",
                ]
            )
        )

    def test_legacy_game_is_adopted_only_with_readiness(self):
        self.running("game")
        self.assertEqual(
            self.backend.observe(), [Process(Goal("game", "unknown"), False)]
        )
        self.ready("game", generation=None, session=None, release="v2.27.0")
        self.assertEqual(
            self.backend.observe(), [Process(Goal("game", "v2.27.0"), True)]
        )

    def test_mc_server_runner_wrapper_is_not_a_second_java(self):
        # Real itzg container on homeserver: pgrep -f matches mc-server-runner and its java child, same cgroup.
        self.running("game", pid=100)
        self.java(99, GAME_CID, comm="mc-server-runne")
        self.assertEqual(self.backend.java_pids(), [100])
        inventory = self.backend.inventory()
        self.assertEqual((inventory["game"]["java"], inventory["unknown"]), (1, 0))
        self.backend.observe()

    def test_unknown_java_fails_closed(self):
        self.running("game")
        self.java(200, OTHER_CID)
        with self.assertRaises(RuntimeError):
            self.backend.observe()
        self.load()
        del self.state["java"]["200"]
        self.save()
        self.java(201, "")
        with self.assertRaises(RuntimeError):
            self.backend.observe()

    def test_game_pending_task_is_observed_independently(self):
        (self.lock / "profile.json").write_text(
            json.dumps(
                {
                    "schema": 1,
                    "profile": "workshop",
                    "generation": str(uuid4()),
                    "session": str(uuid4()),
                    "release": "v1",
                }
            )
        )
        self.load()
        self.state["services"]["bf-game"]["replicas"] = 1
        self.save()
        self.assertEqual(
            self.backend.observe(), [Process(Goal("game", "unknown"), False)]
        )

    def test_corrupt_selection_fails_closed(self):
        (self.lock / "profile.json").write_text('{"schema":1,"profile":"game"}')
        self.running("game")
        self.assertTrue(all(not process.ready for process in self.backend.observe()))

    def test_bad_game_selection_leaves_workshop_readiness_intact(self):
        session = str(uuid4())
        goal = Goal("workshop", "v1", session)
        self.running("game")
        self.backend.start(goal)
        selected = self.backend.selection()
        self.ready(
            "workshop", generation=selected["generation"], session=session, release="v1"
        )
        (self.lock / "game").mkdir()
        (self.lock / "game/profile.json").write_text('{"schema":1,"profile":"game"}')
        self.running("game")
        self.assertEqual(
            self.backend.observe(),
            [Process(Goal("game", "unknown"), False), Process(goal, True)],
        )

    def test_malformed_readiness_cannot_adopt_a_game(self):
        self.running("game")
        for ready in (
            {"profile": "game"},
            {"profile": "game", "generation": None, "session": None, "release": ""},
            {
                "profile": "game",
                "generation": None,
                "session": str(uuid4()),
                "release": "v1",
            },
        ):
            (self.data["game"] / READY_FILE).write_text(json.dumps(ready))
            self.assertEqual(
                self.backend.observe(), [Process(Goal("game", "unknown"), False)]
            )

    def test_start_selects_profile_and_readiness_needs_generation(self):
        session = str(uuid4())
        self.running("game")
        self.ready("workshop", generation=None, session=session, release="v1")
        self.backend.start(Goal("workshop", "v1", session))
        selection = json.loads((self.lock / "workshop/profile.json").read_text())
        self.assertEqual(
            (selection["profile"], selection["session"], selection["release"]),
            ("workshop", session, "v1"),
        )
        self.assertEqual(
            oct((self.lock / "workshop/profile.json").stat().st_mode & 0o777), "0o644"
        )
        self.assertFalse((self.data["workshop"] / READY_FILE).exists())
        self.assertIn(
            ["docker", "service", "scale", "--detach", "bf-workshop=1"], self.calls()
        )
        goal = Goal("workshop", "v1", session)
        game = Process(Goal("game", "unknown"), False)
        self.assertEqual(self.backend.observe(), [game, Process(goal, False)])
        self.ready("workshop", generation=str(uuid4()), session=session, release="v1")
        self.assertEqual(self.backend.observe(), [game, Process(goal, False)])
        self.ready(
            "workshop",
            generation=selection["generation"],
            session=session,
            release="v2",
        )
        self.assertEqual(self.backend.observe(), [game, Process(goal, False)])
        self.ready(
            "workshop",
            generation=selection["generation"],
            session=session,
            release="v1",
        )
        self.assertEqual(self.backend.observe(), [game, Process(goal, True)])

    def test_game_follows_release_deployed_outside_controller(self):
        self.running("game")
        generation = str(uuid4())
        (self.lock / "game").mkdir()
        (self.lock / "game/profile.json").write_text(
            json.dumps(
                {
                    "schema": 1,
                    "profile": "game",
                    "generation": generation,
                    "session": None,
                    "release": "v1",
                }
            )
        )
        self.ready("game", generation=generation, session=None, release="v2")
        self.assertEqual(self.backend.observe(), [Process(Goal("game", "v2"), True)])
        self.ready("game", generation=str(uuid4()), session=None, release="v2")
        self.assertEqual(self.backend.observe(), [Process(Goal("game", "v2"), False)])

    def test_game_lifecycle_is_not_owned_by_workshop(self):
        goal = Goal("game", "v1")
        for action in (self.backend.start, self.backend.preflight):
            with self.assertRaises(ValueError):
                action(goal)
        for action in (self.backend.stop, self.backend.save_and_freeze):
            with self.assertRaises(ValueError):
                action(Process(goal, True))
        self.assertEqual(self.calls(), [])

    def test_workshop_follows_the_running_game_digest(self):
        self.running("game")
        self.state["services"]["bf-workshop"]["spec"]["Image"] = "old"
        self.state["repo_digests"][GAME_IMAGE_ID] = [FOREIGN_DIGEST, GAME_IMAGE]
        self.save()
        self.assertEqual(self.backend.game_image(), GAME_IMAGE)
        self.backend.start(Goal("workshop", "v1", str(uuid4())))
        self.assertIn(
            [
                "docker",
                "service",
                "update",
                "--detach",
                "--image",
                GAME_IMAGE,
                "bf-workshop",
            ],
            self.calls(),
        )

    def test_game_image_fails_closed_on_ambiguous_runtime(self):
        self.running("game")
        second_digest = "ghcr.io/blockfield/blockfield-server@sha256:" + "a" * 64
        digest_cases = {
            "tag without digest": ["ghcr.io/blockfield/blockfield-server:latest"],
            "foreign repository only": [FOREIGN_DIGEST],
            "two server digests": [GAME_IMAGE, second_digest],
            "null repo digests": None,
            "non-list repo digests": "x",
        }
        for name, digests in digest_cases.items():
            with self.subTest(name):
                self.load()
                self.state["repo_digests"][GAME_IMAGE_ID] = digests
                self.save()
                with self.assertRaises(ValueError):
                    self.backend.game_image()
        id_cases = {"short id": "sha256:abc", "container name": "bf-game"}
        for name, image_id in id_cases.items():
            with self.subTest(name):
                self.load()
                self.state["container_images"][GAME_CID] = image_id
                self.save()
                with self.assertRaisesRegex(ValueError, "image ID"):
                    self.backend.game_image()

    def test_game_image_requires_exactly_one_running_game_container(self):
        with self.assertRaisesRegex(RuntimeError, "exactly one"):
            self.backend.game_image()
        with self.assertRaisesRegex(RuntimeError, "exactly one"):
            self.backend.preflight(Goal("workshop", "v1", str(uuid4())))
        self.running("game")
        with mock.patch.object(
            self.backend, "containers", return_value=[GAME_CID, OTHER_CID]
        ):
            with self.assertRaisesRegex(RuntimeError, "exactly one"):
                self.backend.game_image()

    def test_inventory_never_ignores_a_malformed_container_id(self):
        with mock.patch.object(self.backend, "run", return_value=GAME_CID + "\nshort"):
            with self.assertRaisesRegex(ValueError, "invalid container ID"):
                self.backend.containers("game")

    def test_save_without_workshop_java_has_no_side_effect(self):
        self.running("game")
        self.backend.save_and_freeze(
            Process(Goal("workshop", "v1", str(uuid4())), False)
        )
        self.assertFalse(any(call[1] == "exec" for call in self.calls()))

    def test_stop_waits_for_java_exit(self):
        self.running("game")
        self.running("workshop")
        process = Process(Goal("workshop", "v1", str(uuid4())), True)
        self.backend.stop(process)
        self.assertEqual(self.backend.inventory()["workshop"], {"tasks": 0, "java": 0})
        self.assertEqual(self.backend.inventory()["game"], {"tasks": 1, "java": 1})
        self.running("workshop")
        self.load()
        self.state["stop_leaves_java"] = True
        self.save()
        with self.assertRaises(TimeoutError):
            self.backend.stop(process)

    def test_save_requires_success_and_snapshot_requires_no_workshop_java(self):
        self.running("workshop")
        with self.assertRaises(RuntimeError):
            with self.backend.workshop_saves_held():
                self.fail("live snapshot")
        for response in ("", "Saving failed", "Saved the game"):
            self.load()
            self.state["rcon"]["save-all flush"] = response
            self.save()
            process = Process(Goal("workshop", "v1", str(uuid4())), True)
            if response == "Saved the game":
                self.backend.save_and_freeze(process)
            else:
                with self.assertRaises(RuntimeError):
                    self.backend.save_and_freeze(process)

    def test_fence_scales_unselected_app(self):
        (self.lock / "profile.json").write_text(
            json.dumps(
                {
                    "schema": 1,
                    "profile": "game",
                    "generation": str(uuid4()),
                    "session": None,
                    "release": "v1",
                }
            )
        )
        self.load()
        self.state["services"]["bf-workshop"]["replicas"] = 1
        self.save()
        self.backend.fence_pending_starts()
        self.assertIn(
            ["docker", "service", "scale", "--detach", "bf-workshop=0"], self.calls()
        )
        self.assertNotIn(
            ["docker", "service", "scale", "--detach", "bf-game=0"], self.calls()
        )

    def test_preflight(self):
        with self.assertRaises(ValueError):
            self.backend.preflight(Goal("test", "v1", str(uuid4()), "a" * 64))
        self.lock.rmdir()
        with self.assertRaises(FileNotFoundError):
            self.backend.preflight(Goal("workshop", "v1", str(uuid4())))

    def test_workshop_restart_keeps_game_running_and_its_generation(self):
        self.running("game")
        self.ready("game", generation=None, session=None, release="v1")
        session = str(uuid4())
        goal = Goal("workshop", "v1", session)
        self.backend.start(goal)
        selected = self.backend.selection()
        self.ready(
            "workshop", generation=selected["generation"], session=session, release="v1"
        )
        self.assertEqual(
            self.backend.observe(),
            [Process(Goal("game", "v1"), True), Process(goal, True)],
        )
        self.backend.stop(Process(goal, True))
        self.backend.start(goal)
        self.assertNotEqual(
            selected["generation"], self.backend.selection()["generation"]
        )
        self.assertEqual(self.backend.inventory()["game"], {"tasks": 1, "java": 1})
        self.assertEqual(self.backend.observe()[0], Process(Goal("game", "v1"), True))
        self.assertFalse(
            any(
                call[1:3] == ["service", "scale"] and call[-1].startswith("bf-game=")
                for call in self.calls()
            )
        )

    def test_playtest_runs_in_the_workshop_app_and_returns(self):
        session, sha = str(uuid4()), "ab" * 32
        self.running("game")
        self.ready("game", generation=None, session=None, release="v1")
        extracted = []
        self.backend.maps = mock.Mock(
            extract=lambda digest, target: extracted.append((digest, target)) or "dust2"
        )
        self.running("workshop")
        self.backend.start(Goal("workshop", "v1", session))
        generation = json.loads((self.lock / "workshop/profile.json").read_text())[
            "generation"
        ]
        self.ready("workshop", generation=generation, session=session, release="v1")
        controller = Controller(Path(self.temp.name) / "controller", self.backend)
        controller.request(str(uuid4()), Goal("workshop", "v1", session), 0)
        self.assertEqual(controller.step()["gate"], "builders")

        test = Goal("test", "v1", session, sha)
        controller.request(str(uuid4()), test, 1)
        self.assertEqual(controller.step()["phase"], "STARTING")
        selection = json.loads((self.lock / "workshop/profile.json").read_text())
        self.assertEqual((selection["profile"], selection["candidate"]), ("test", sha))
        self.assertEqual(extracted, [(sha, self.data["workshop"] / "test-candidate")])
        self.assertEqual(
            self.backend.observe(),
            [Process(Goal("game", "v1"), True), Process(test, False)],
        )
        (self.data["workshop"] / READY_FILE).write_text(
            json.dumps(
                {
                    "profile": "test",
                    "generation": selection["generation"],
                    "session": session,
                    "release": "v1",
                }
            )
        )
        self.assertEqual(controller.step()["gate"], "testers")

        controller.request(str(uuid4()), Goal("workshop", "v1", session), 2)
        self.assertEqual(controller.step()["phase"], "STARTING")
        generation = json.loads((self.lock / "workshop/profile.json").read_text())[
            "generation"
        ]
        self.ready("workshop", generation=generation, session=session, release="v1")
        self.assertEqual(controller.step()["gate"], "builders")
        calls = self.calls()
        self.assertNotIn(["OVERLAP"], calls)
        self.assertNotIn(["docker", "service", "scale", "--detach", "bf-game=1"], calls)
        self.assertEqual(
            calls.count(["docker", "service", "scale", "--detach", "bf-workshop=0"]), 2
        )

    def _setup_workshop_fixture(self, active_ro=None, admin_ro=None):
        from candidates import MapStore

        root = Path(self.temp.name)
        active, admin = root / "active", root / "admin"
        active.mkdir(parents=True, exist_ok=True)
        admin.mkdir(parents=True, exist_ok=True)
        self.backend.maps = MapStore(
            self.data["workshop"],
            root / "templates",
            root / "published",
            root / "archive/candidates",
            root / "archive/backups",
            archive_root=root / "archive",
            active_root=active,
        )
        self.backend.admin_dir = admin
        self.backend.map_active_readonly_dir = active_ro
        self.backend.admin_readonly_dir = admin_ro
        spec = {
            "Env": [
                "BLOCKFIELD_PROFILE=workshop",
                "BF_MAP_ACTIVE_DIR=/maps-active",
                "BLOCKFIELD_WORKSHOP_DRAFTS_DIR=/maps-active/drafts",
            ],
            "Mounts": [
                {
                    "Type": "bind",
                    "Source": str(active_ro or active),
                    "Target": "/maps-active",
                    "ReadOnly": False if active_ro else True,
                },
                {
                    "Type": "bind",
                    "Source": str(active / "drafts"),
                    "Target": "/maps-active/drafts",
                    "ReadOnly": False,
                },
                {
                    "Type": "bind",
                    "Source": str(admin_ro or admin),
                    "Target": "/workshop-admin",
                    "ReadOnly": False if admin_ro else True,
                },
                {
                    "Type": "bind",
                    "Source": str(self.lock),
                    "Target": "/backend-lock",
                    "ReadOnly": False,
                },
            ],
        }
        self.state["services"]["bf-workshop"]["spec"] = spec
        self.save()
        return active, admin

    def test_check_readonly_view_core_validation(self):
        root = Path(self.temp.name)
        source = root / "canonical"
        view = root / "view"
        source.mkdir(parents=True, exist_ok=True)
        view.mkdir(parents=True, exist_ok=True)
        stat_ro = mock.Mock(f_flag=os.ST_RDONLY)

        with (
            mock.patch("os.path.samefile", return_value=True),
            mock.patch("os.statvfs", return_value=stat_ro),
        ):
            _check_readonly_view(source, view)

        sym_parent = root / "sym_dir"
        sym_parent.symlink_to(root)
        (root / "sym_target").symlink_to(source)
        cases = [
            ("target symlink", root / "sym_target", view, True, stat_ro),
            ("ancestor symlink", source, sym_parent / "view", True, stat_ro),
            ("RW kernel", source, view, True, mock.Mock(f_flag=0)),
            ("different inode", source, view, False, stat_ro),
            ("same path", source, source, True, stat_ro),
            ("relative path", Path("relative/source"), view, True, stat_ro),
            ("path with ..", root / "foo/../canonical", view, True, stat_ro),
            ("root directory", Path("/"), view, True, stat_ro),
        ]
        for desc, src, vw, same, st in cases:
            with (
                self.subTest(case=desc),
                mock.patch("os.path.samefile", return_value=same),
                mock.patch("os.statvfs", return_value=st),
            ):
                with self.assertRaises(ValueError):
                    _check_readonly_view(src, vw)

    def test_readonly_views_from_env_defaults(self):
        base_env = {"BF_GAME_SERVICE": "bf-game", "BF_WORKSHOP_SERVICE": "bf-workshop"}
        unconf = DockerBackend.from_env(base_env)
        self.assertIsNone(unconf.map_active_readonly_dir)
        self.assertIsNone(unconf.admin_readonly_dir)

        conf = DockerBackend.from_env(
            {
                **base_env,
                "BF_MAP_ACTIVE_READONLY_DIR": "/views/maps",
                "BF_ADMIN_READONLY_DIR": "/views/admin",
            }
        )
        self.assertEqual(conf.map_active_readonly_dir, Path("/views/maps"))
        self.assertEqual(conf.admin_readonly_dir, Path("/views/admin"))

    def test_workshop_mounts_adapter_wiring(self):
        root = Path(self.temp.name)
        v_active, v_admin = root / "views/maps-active", root / "views/admin"
        v_active.mkdir(parents=True, exist_ok=True)
        v_admin.mkdir(parents=True, exist_ok=True)
        goal = Goal("workshop", "v1", str(uuid4()))
        fake_stat = mock.Mock(
            f_flag=os.ST_RDONLY,
            f_bavail=10**8,
            f_frsize=4096,
            f_blocks=10**8,
            f_bfree=10**8,
            f_bsize=4096,
        )

        # Positive preflight: Dokploy generated mounts accepted with configured readonly views
        self._setup_workshop_fixture(active_ro=v_active, admin_ro=v_admin)
        self.running("game")
        with (
            mock.patch("os.path.samefile", return_value=True),
            mock.patch("os.statvfs", return_value=fake_stat),
        ):
            self.backend.preflight(goal)

        spec = self.state["services"]["bf-workshop"]["spec"]

        def run_preflight_expect_err():
            self.save()
            with (
                mock.patch("os.path.samefile", return_value=True),
                mock.patch("os.statvfs", return_value=fake_stat),
            ):
                with self.assertRaisesRegex(ValueError, "mount"):
                    self.backend.preflight(goal)

        # 1. Unconfigured view rejects ReadOnly: False on active mount
        self._setup_workshop_fixture()
        spec = self.state["services"]["bf-workshop"]["spec"]
        spec["Mounts"][0]["ReadOnly"] = False
        run_preflight_expect_err()

        # 2. Configured view rejects original source when spec has ReadOnly: False
        self.backend.map_active_readonly_dir = v_active
        run_preflight_expect_err()

        # 3. Configured view rejects spec with wrong path
        spec["Mounts"][0]["Source"] = str(root / "wrong")
        run_preflight_expect_err()

        # 4. Exact drafts RW: ReadOnly=True rejected
        self._setup_workshop_fixture(active_ro=v_active, admin_ro=v_admin)
        spec = self.state["services"]["bf-workshop"]["spec"]
        spec["Mounts"][1]["ReadOnly"] = True
        run_preflight_expect_err()

        # 5. Exact lock RW: ReadOnly=True rejected
        self._setup_workshop_fixture(active_ro=v_active, admin_ro=v_admin)
        spec = self.state["services"]["bf-workshop"]["spec"]
        spec["Mounts"][3]["ReadOnly"] = True
        run_preflight_expect_err()


if __name__ == "__main__":
    unittest.main()
