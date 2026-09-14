"""DockerBackend against fake `docker`/`pgrep` executables on PATH and a fake /proc."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from uuid import uuid4
from controller import Controller, Goal, Process
from docker_backend import App, DockerBackend, JAVA_PATTERN, READY_FILE

GAME_CID, WORKSHOP_CID, OTHER_CID = "a" * 64, "b" * 64, "c" * 64

FAKE = r'''
import json, os, sys
from pathlib import Path
state_path, log = Path(os.environ["BF_FAKE"]), Path(os.environ["BF_FAKE_LOG"])
proc = Path(os.environ["BF_FAKE_PROC"])
state = json.loads(state_path.read_text())
argv = [Path(sys.argv[0]).name] + sys.argv[1:]
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
        if state["java"]:
            with log.open("a") as handle:
                handle.write(json.dumps(["OVERLAP"]) + "\n")
        pid = str(1000 + int(service["container"][0], 16))
        state["java"][pid] = service["container"]
        (proc / pid).mkdir(parents=True, exist_ok=True)
        (proc / pid / "cgroup").write_text("0::/system.slice/docker-" + service["container"] + ".scope\n")
        (proc / pid / "comm").write_text("java\n")
elif argv[1] == "exec":
    print(state.get("rcon", {}).get(" ".join(argv[4:]), ""))
state_path.write_text(json.dumps(state))
'''


class DockerBackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.bin, self.proc, self.lock = root / "bin", root / "proc", root / "lock"
        self.data = {"game": root / "game", "workshop": root / "workshop"}
        for path in (self.bin, self.proc, self.lock, *self.data.values()):
            path.mkdir()
        for name in ("docker", "pgrep"):
            (self.bin / name).write_text(f"#!{sys.executable}\n" + FAKE)
            (self.bin / name).chmod(0o755)
        self.state_file, self.log = root / "state.json", root / "calls.log"
        self.state = {"services": {"bf-game": {"replicas": 0, "container": GAME_CID},
                                   "bf-workshop": {"replicas": 0, "container": WORKSHOP_CID}},
                      "java": {}, "rcon": {}}
        self.save()
        env = {"PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}", "BF_FAKE": str(self.state_file),
               "BF_FAKE_LOG": str(self.log), "BF_FAKE_PROC": str(self.proc)}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.backend = DockerBackend({"game": App("bf-game", self.data["game"]),
                                      "workshop": App("bf-workshop", self.data["workshop"])},
                                     self.lock, proc=self.proc, stop_timeout=0.5, poll=0.01, min_free_bytes=0)

    def save(self):
        self.state_file.write_text(json.dumps(self.state))

    def load(self):
        self.state = json.loads(self.state_file.read_text())

    def java(self, pid, cid, comm="java"):
        self.load()
        self.state["java"][str(pid)] = cid
        (self.proc / str(pid)).mkdir(exist_ok=True)
        (self.proc / str(pid) / "cgroup").write_text(f"0::/system.slice/docker-{cid}.scope\n" if cid else "0::/user.slice\n")
        (self.proc / str(pid) / "comm").write_text(comm + "\n")
        self.save()

    def running(self, profile, pid=100):
        self.load()
        service = self.state["services"][f"bf-{profile}"]
        service["replicas"] = 1
        self.save()
        self.java(pid, service["container"])

    def ready(self, profile, **document):
        (self.data[profile] / READY_FILE).write_text(json.dumps({"profile": profile, **document}))

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def test_only_allowlisted_commands(self):
        good = [["docker", "service", "scale", "--detach", "bf-game=0"],
                ["docker", "exec", GAME_CID, "rcon-cli", "bf", "maintenance", "status"],
                ["pgrep", "-f", JAVA_PATTERN]]
        bad = [["docker", "rm", "-f", "bf-game"], ["docker", "service", "scale", "--detach", "bf-game=2"],
               ["docker", "service", "scale", "--detach", "other=1"],
               ["docker", "exec", GAME_CID, "rcon-cli", "op", "Nether"], ["docker", "exec", "bf-game", "rcon-cli", "save-all", "flush"],
               ["docker", "exec", GAME_CID, "sh", "-c", "id"], ["pgrep", "-f", "java"], ["sh", "-c", "docker ps"]]
        for argv in good:
            self.assertTrue(self.backend.allowed(argv), argv)
        for argv in bad:
            self.assertFalse(self.backend.allowed(argv), argv)
            with self.assertRaises(PermissionError):
                self.backend.run(argv)
        self.assertEqual(self.calls(), [])

    def test_legacy_game_is_adopted_only_with_readiness(self):
        self.running("game")
        self.assertEqual(self.backend.observe(), [Process(Goal("game", "unknown"), False)])
        self.ready("game", generation=None, session=None, release="v2.27.0")
        self.assertEqual(self.backend.observe(), [Process(Goal("game", "v2.27.0"), True)])

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
        self.load(); del self.state["java"]["200"]; self.save()
        self.java(201, "")
        with self.assertRaises(RuntimeError):
            self.backend.observe()

    def test_waiting_task_of_unselected_profile_is_not_a_backend(self):
        (self.lock / "profile.json").write_text(json.dumps(
            {"schema": 1, "profile": "workshop", "generation": str(uuid4()), "session": str(uuid4()), "release": "v1"}))
        self.load(); self.state["services"]["bf-game"]["replicas"] = 1; self.save()
        self.assertEqual(self.backend.observe(), [])

    def test_corrupt_selection_fails_closed(self):
        (self.lock / "profile.json").write_text('{"schema":1,"profile":"game"}')
        self.running("game")
        with self.assertRaises(ValueError):
            self.backend.observe()

    def test_start_selects_profile_and_readiness_needs_generation(self):
        session = str(uuid4())
        self.ready("workshop", generation=None, session=session, release="v1")
        self.backend.start(Goal("workshop", "v1", session))
        selection = json.loads((self.lock / "profile.json").read_text())
        self.assertEqual((selection["profile"], selection["session"], selection["release"]), ("workshop", session, "v1"))
        self.assertEqual(oct((self.lock / "profile.json").stat().st_mode & 0o777), "0o644")
        self.assertFalse((self.data["workshop"] / READY_FILE).exists())
        self.assertIn(["docker", "service", "scale", "--detach", "bf-workshop=1"], self.calls())
        goal = Goal("workshop", "v1", session)
        self.assertEqual(self.backend.observe(), [Process(goal, False)])
        self.ready("workshop", generation=str(uuid4()), session=session, release="v1")
        self.assertEqual(self.backend.observe(), [Process(goal, False)])
        self.ready("workshop", generation=selection["generation"], session=session, release="v2")
        self.assertEqual(self.backend.observe(), [Process(goal, False)])
        self.ready("workshop", generation=selection["generation"], session=session, release="v1")
        self.assertEqual(self.backend.observe(), [Process(goal, True)])

    def test_game_follows_release_deployed_outside_controller(self):
        self.backend.start(Goal("game", "v1"))
        selection = json.loads((self.lock / "profile.json").read_text())
        self.running("game")
        self.ready("game", generation=selection["generation"], session=None, release="v2")
        self.assertEqual(self.backend.observe(), [Process(Goal("game", "v2"), True)])
        self.ready("game", generation=str(uuid4()), session=None, release="v2")
        self.assertEqual(self.backend.observe(), [Process(Goal("game", "v2"), False)])

    def test_drain_status_parsing(self):
        self.running("game")
        status = "bf maintenance status"
        for line, expected in (("draining=true rooms=0 ranked=0 pending=0 healthy=true drained=true", True),
                               ("draining=true rooms=1 ranked=1 pending=0 healthy=true drained=false", False),
                               ("draining=false rooms=0 ranked=0 pending=0 healthy=true drained=false", False)):
            self.load(); self.state["rcon"][status] = line; self.save()
            self.assertIs(self.backend.drained(), expected)
        self.load(); self.state["rcon"][status] = "Unknown or incomplete command"; self.save()
        with self.assertRaises(RuntimeError):
            self.backend.drained()
        self.backend.request_drain()
        self.assertIn(["docker", "exec", GAME_CID, "rcon-cli", "bf", "maintenance", "drain"], self.calls())

    def test_nothing_to_drain_or_save_without_java(self):
        self.load(); self.state["services"]["bf-game"]["replicas"] = 1; self.save()
        self.assertTrue(self.backend.drained())
        self.backend.save_and_freeze(Process(Goal("game", "v1"), False))
        self.assertFalse([c for c in self.calls() if c[:2] == ["docker", "exec"]])

    def test_stop_waits_for_java_exit(self):
        self.running("game")
        self.backend.stop(Process(Goal("game", "v1"), True))
        self.assertEqual(self.backend.inventory()["game"], {"tasks": 0, "java": 0})
        self.running("game")
        self.load(); self.state["stop_leaves_java"] = True; self.save()
        with self.assertRaises(TimeoutError):
            self.backend.stop(Process(Goal("game", "v1"), True))

    def test_fence_scales_unselected_app(self):
        (self.lock / "profile.json").write_text(json.dumps(
            {"schema": 1, "profile": "game", "generation": str(uuid4()), "session": None, "release": "v1"}))
        self.load(); self.state["services"]["bf-workshop"]["replicas"] = 1; self.save()
        self.backend.fence_pending_starts()
        self.assertIn(["docker", "service", "scale", "--detach", "bf-workshop=0"], self.calls())
        self.assertNotIn(["docker", "service", "scale", "--detach", "bf-game=0"], self.calls())

    def test_preflight(self):
        with self.assertRaises(ValueError):
            self.backend.preflight(Goal("test", "v1", str(uuid4()), "a" * 64))
        self.lock.rmdir()
        with self.assertRaises(FileNotFoundError):
            self.backend.preflight(Goal("game", "v1"))

    def test_controller_round_trip_keeps_one_java(self):
        self.running("game")
        self.ready("game", generation=None, session=None, release="v1")
        controller = Controller(Path(self.temp.name) / "controller", self.backend)
        controller.request(str(uuid4()), Goal("game", "v1"), 0)
        self.assertEqual(controller.step()["gate"], "public")

        session = str(uuid4())
        workshop = Goal("workshop", "v1", session)
        controller.request(str(uuid4()), workshop, 1)
        status = "bf maintenance status"
        self.load(); self.state["rcon"][status] = "draining=true rooms=1 ranked=1 pending=0 healthy=true drained=false"; self.save()
        self.assertEqual(controller.step()["gate"], "draining")
        self.load(); self.state["rcon"][status] = "draining=true rooms=0 ranked=0 pending=0 healthy=true drained=true"; self.save()
        self.assertEqual(controller.step()["phase"], "STARTING")
        self.assertEqual(controller.step()["gate"], "closed")
        generation = json.loads((self.lock / "profile.json").read_text())["generation"]
        self.ready("workshop", generation=generation, session=session, release="v1")
        self.assertEqual(controller.step()["gate"], "builders")

        controller.request(str(uuid4()), Goal("game", "v1"), 2)
        self.assertEqual(controller.step()["phase"], "STARTING")
        self.assertFalse((self.data["game"] / READY_FILE).exists())
        generation = json.loads((self.lock / "profile.json").read_text())["generation"]
        self.ready("game", generation=generation, session=None, release="v1")
        self.assertEqual(controller.step()["gate"], "public")

        calls = self.calls()
        self.assertNotIn(["OVERLAP"], calls)
        self.assertIn(["docker", "exec", WORKSHOP_CID, "rcon-cli", "save-all", "flush"], calls)
        self.assertLess(calls.index(["docker", "service", "scale", "--detach", "bf-game=0"]),
                        calls.index(["docker", "service", "scale", "--detach", "bf-workshop=1"]))
        self.assertLess(calls.index(["docker", "service", "scale", "--detach", "bf-workshop=0"]),
                        calls.index(["docker", "service", "scale", "--detach", "bf-game=1"]))


if __name__ == "__main__":
    unittest.main()
