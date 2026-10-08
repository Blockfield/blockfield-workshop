"""Native stand ownership and dual-backend behavior without starting any process."""

import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).parent / "e2e"))
import paths  # noqa: E402

with (
    mock.patch.object(paths, "owner", return_value="test-owner"),
    mock.patch.dict(os.environ, {"JAVA": "/fixture/java"}),
):
    stand = importlib.import_module("stand")

STAND_JAVA_INVENTORY = stand.stand_java

from controller import Goal, Process  # noqa: E402


class StandTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.apps = {name: self.root / name for name in ("game", "workshop")}
        for index, directory in enumerate(self.apps.values()):
            directory.mkdir()
            (directory / "world").mkdir()
            (directory / "world/draft").write_text("keep")
            (directory / "maps/fields").mkdir(parents=True)
            (directory / "server.properties").write_text(
                f"server-port={25590 + index}\nrcon.port={25580 + index}\n"
                "enable-rcon=true\nrcon.password=fixture-password\nlevel-name=world\n"
            )
        (self.root / "lock").mkdir()
        self.running = []
        self.statuses = {}
        self.commands = {}
        self.goal = Goal("workshop", "r1", str(uuid4()))
        changes = {
            "STAND": self.root,
            "APPS": self.apps,
            "LOCK": self.root / "lock",
            "OWNER": "test-owner",
            "JAVA": "/fixture/java",
        }
        patcher = mock.patch.multiple(stand, **changes)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.dict(os.environ, {"BF_MIN_FREE_BYTES": "0"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.output = mock.patch.object(
            stand.subprocess, "check_output", side_effect=self.command
        ).start()
        self.run = mock.patch.object(
            stand.subprocess, "run", side_effect=self.stop
        ).start()
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(
            stand, "stand_java", side_effect=lambda: self.running[:]
        ).start()
        mock.patch.object(stand, "native_identity", side_effect=self.identity).start()
        mock.patch.object(
            stand, "native_command", side_effect=lambda pid: self.commands[pid]
        ).start()
        socket = mock.patch.object(stand.socket, "socket").start()
        socket.return_value.__enter__.return_value.connect_ex.return_value = 1
        self.backend = stand.StandBackend()

    def command(self, command, **kwargs):
        if command[:2] == ["agent-process", "status"]:
            return json.dumps(self.statuses[command[2]])
        if command[0] == "ss":
            header = (
                "State Recv-Q Send-Q Local Address:Port Peer Address:Port Process\n"
            )
            port = command[-1].rsplit(":", 1)[-1]
            for pid, app in self.running:
                props = stand.properties(app)
                if port in (props["server-port"], props["rcon.port"]):
                    return header + f"LISTEN pid={pid}\n"
            return header
        if command[:2] == ["agent-process", "start"]:
            app = next(
                name
                for name, path in self.apps.items()
                if str(path) == command[command.index("--cwd") + 1]
            )
            pid = 100 if app == "game" else 200
            process_id = f"fixture-{app}"
            status = {
                "id": process_id,
                "owner": "test-owner",
                "pid": pid,
                "start_ticks": "55",
                "cwd": str(self.apps[app]),
                "command": command[command.index("--") + 1 :],
                "live": True,
            }
            self.statuses[process_id] = status
            self.commands[pid] = status["command"][:]
            self.running.append((pid, app))
            return json.dumps(status)
        raise AssertionError(command)

    def identity(self, pid):
        app = next((app for current, app in self.running if current == pid), None)
        if app is None:
            return None
        return {
            "pid": pid,
            "start_ticks": 55,
            "exe": "/fixture/java",
            "cwd": str(self.apps[app]),
        }

    def stop(self, command, **kwargs):
        self.assertEqual(
            command,
            [
                "agent-process",
                "stop",
                "--owner",
                "test-owner",
                "--grace-seconds",
                "120",
                "--no-escalate",
                "fixture-workshop",
            ],
        )
        self.assertEqual(kwargs, {"check": True, "timeout": 130})
        self.running = [p for p in self.running if p[1] != "workshop"]
        self.statuses[command[-1]]["live"] = False

    def ready(self, app):
        selection = self.backend.selection(app)
        ready = {
            key: selection[key]
            for key in ("profile", "generation", "session", "release")
        }
        (self.apps[app] / ".blockfield-ready.json").write_text(json.dumps(ready))

    def start_both(self):
        self.backend.bootstrap_game("r1")
        self.ready("game")
        self.backend.start(self.goal)
        self.ready("workshop")

    def test_both_apps_have_independent_generation_readiness_and_records(self):
        self.start_both()
        processes = self.backend.observe()
        self.assertEqual({p.goal.profile for p in processes}, {"game", "workshop"})
        self.assertTrue(all(p.ready for p in processes))
        self.assertNotEqual(
            self.backend.selection("game")["generation"],
            self.backend.selection("workshop")["generation"],
        )
        (self.apps["workshop"] / ".blockfield-ready.json").write_text("{}")
        self.assertEqual(
            {p.goal.profile: p.ready for p in self.backend.observe()},
            {"game": True, "workshop": False},
        )
        self.assertFalse((self.root / "rig-process.json").exists())
        for app in self.apps:
            self.assertTrue((self.apps[app] / "rig-process.json").exists())

    def test_invalid_game_selection_closes_only_game(self):
        self.start_both()
        (self.root / "lock/game/profile.json").write_text("{")
        self.assertEqual(
            {p.goal.profile: p.ready for p in self.backend.observe()},
            {"game": False, "workshop": True},
        )

    def test_controller_cannot_start_save_or_stop_game(self):
        game = Goal("game", "r1")
        for action in (
            lambda: self.backend.preflight(game),
            lambda: self.backend.start(game),
            lambda: self.backend.save_and_freeze(Process(game, True)),
            lambda: self.backend.stop(Process(game, True)),
        ):
            with self.assertRaises(ValueError):
                action()
        self.output.assert_not_called()
        self.run.assert_not_called()

    def test_canonical_session_is_revalidated_and_paths_stay_inside_stand(self):
        self.backend.maps = mock.Mock(canonical_sessions=True)
        admin = self.root / "workshop-admin"
        admin.mkdir()
        (admin / "session.json").write_text(
            json.dumps({"schema": 1, "id": self.goal.session, "maps": ["fields"]})
        )
        self.backend.start(self.goal)
        self.backend.maps.prepare_session.assert_called_once_with(
            self.goal.session, ["fields"], resume_only=True
        )
        launch = next(
            call
            for call in self.output.call_args_list
            if call.args[0][:2] == ["agent-process", "start"]
        )
        self.assertEqual(
            launch.kwargs["env"]["BLOCKFIELD_MEMBERS_FILE"], str(admin / "members.json")
        )
        self.assertEqual(
            launch.kwargs["env"]["BLOCKFIELD_WORKSHOP_DRAFTS_DIR"],
            str(self.root / "maps-active/drafts"),
        )
        for name in ("BF_MAP_ARCHIVE_DIR", "BF_MAP_ACTIVE_DIR", "BF_MAP_PACKAGES_DIR"):
            self.assertTrue(Path(stand.env()[name]).is_relative_to(self.root))

    def test_stop_and_fence_only_the_verified_workshop(self):
        self.start_both()
        self.backend.stop(Process(self.goal, True))
        self.assertEqual(self.running, [(100, "game")])
        self.backend.start(self.goal)
        self.backend.fence_pending_starts()
        self.assertEqual(self.running, [(100, "game")])
        self.assertEqual(self.run.call_count, 2)

    def test_rcon_uses_the_owned_target_properties_without_game_fallback(self):
        self.start_both()
        connection = mock.Mock()
        connection.cmd.return_value = "Saved the game"
        client = mock.Mock(return_value=connection)
        with mock.patch.dict(sys.modules, {"rcon": mock.Mock(Rcon=client)}):
            self.backend.save_and_freeze(Process(self.goal, True))
        client.assert_called_once_with(
            "127.0.0.1", 25581, "fixture-password", timeout=120
        )
        connection.s.close.assert_called_once()
        props = self.apps["workshop"] / "server.properties"
        props.write_text(
            props.read_text().replace("rcon.port=25581", "rcon.port=25580")
        )
        with mock.patch.dict(sys.modules, {"rcon": mock.Mock(Rcon=client)}):
            with self.assertRaisesRegex(RuntimeError, "listener"):
                self.backend.save_and_freeze(Process(self.goal, True))
        self.assertEqual(client.call_count, 1)

    def test_foreign_or_reused_process_is_never_stopped(self):
        for change in (
            {"owner": "foreign"},
            {"start_ticks": "56"},
            {"cwd": str(self.root)},
            {
                "command": [
                    "/fixture/other",
                    "-jar",
                    "fabric-server-launch.jar",
                    "nogui",
                ]
            },
        ):
            with self.subTest(change=change):
                self.running.clear()
                self.statuses.clear()
                for app in self.apps:
                    (self.apps[app] / "rig-process.json").unlink(missing_ok=True)
                self.start_both()
                self.statuses["fixture-workshop"].update(change)
                with self.assertRaises(RuntimeError):
                    self.backend.stop(Process(self.goal, True))
                self.run.assert_not_called()

    def test_unregistered_workshop_is_observed_but_never_stopped(self):
        self.running = [(201, "workshop")]
        self.assertFalse(self.backend.observe()[0].ready)
        with self.assertRaises(RuntimeError):
            self.backend.fence_pending_starts()
        self.run.assert_not_called()

    def test_changed_native_launcher_is_not_authorized_by_the_old_receipt(self):
        self.start_both()
        command = self.commands[200]
        command[command.index("-jar") + 1] = "server.jar"
        with self.assertRaisesRegex(RuntimeError, "identity changed"):
            self.backend.stop(Process(self.goal, True))
        self.run.assert_not_called()
        self.assertEqual(
            {process.goal.profile: process.ready for process in self.backend.observe()},
            {"game": True, "workshop": False},
        )

    def test_graceful_timeout_retains_workshop_and_blocks_replacement(self):
        self.start_both()
        receipt = (self.apps["workshop"] / "rig-process.json").read_bytes()
        for error in (
            subprocess.CalledProcessError(4, "agent-process"),
            subprocess.TimeoutExpired("agent-process", 130),
        ):
            with self.subTest(error=error):
                self.run.reset_mock()
                self.run.side_effect = error
                with self.assertRaises(type(error)):
                    self.backend.stop(Process(self.goal, True))
                self.assertEqual(self.running, [(100, "game"), (200, "workshop")])
                self.assertEqual(
                    (self.apps["workshop"] / "rig-process.json").read_bytes(), receipt
                )
                self.assertTrue(self.statuses["fixture-workshop"]["live"])
                self.run.assert_called_once()
                with self.assertRaisesRegex(RuntimeError, "already runs"):
                    self.backend.start(self.goal)
                self.assertTrue(
                    all(process.ready for process in self.backend.observe())
                )

    def test_duplicate_java_and_occupied_ports_fail_closed(self):
        self.running = [(100, "game"), (101, "game")]
        with self.assertRaises(RuntimeError):
            self.backend.observe()
        self.running.clear()
        self.output.side_effect = lambda *args, **kwargs: "Header\nLISTEN pid=999\n"
        with self.assertRaisesRegex(RuntimeError, "occupied"):
            self.backend.bootstrap_game("r1")
        self.assertFalse((self.apps["game"] / "rig-process.json").exists())

    def test_legacy_record_migrates_only_a_matching_owned_app(self):
        self.backend.start(self.goal)
        new = self.apps["workshop"] / "rig-process.json"
        legacy = self.root / "rig-process.json"
        new.rename(legacy)
        self.assertIsNone(stand.registered("game"))
        self.assertTrue(legacy.exists())
        self.statuses["fixture-workshop"]["owner"] = "foreign"
        with self.assertRaises(RuntimeError):
            stand.registered("workshop")
        self.assertTrue(legacy.exists())
        self.assertFalse(new.exists())
        self.statuses["fixture-workshop"]["owner"] = "test-owner"
        stand.registered("workshop")
        self.assertFalse(legacy.exists())
        self.assertTrue(new.exists())

    def test_playtest_copies_offline_seed_and_preserves_drafts(self):
        self.start_both()
        seed = self.root / "seed/world"
        seed.mkdir(parents=True)
        (seed / "seed-marker").write_text("immutable")

        def extract(candidate, destination):
            destination.mkdir()
            (destination / "map-id").write_text("fields")
            (destination / "map.json").write_text("{}")
            (destination / "world/region").mkdir(parents=True)

        self.backend.maps = mock.Mock()
        self.backend.maps.extract.side_effect = extract
        self.backend.stop(Process(self.goal, True))
        self.backend.start(Goal("test", "r1", self.goal.session, "a" * 64))
        self.assertTrue((self.apps["workshop"] / "test-world/seed-marker").exists())
        self.assertFalse((self.apps["workshop"] / "test-world/draft").exists())
        for app in self.apps:
            self.assertEqual((self.apps[app] / "world/draft").read_text(), "keep")
        with mock.patch.dict(
            os.environ, {"STAND_TEST_SEED": str(self.apps["game"] / "world")}
        ):
            with self.assertRaisesRegex(ValueError, "live backend"):
                self.backend.test_seed()

    def test_native_journal_measures_both_apps_and_skips_foreign_heap(self):
        self.start_both()
        journal_stand = importlib.import_module("journal_stand")
        controller = self.root / "controller"
        controller.mkdir()
        (controller / "gate.json").write_text(
            json.dumps(
                {
                    "backends": {
                        "game": {"gate": "public"},
                        "workshop": {"gate": "closed"},
                    }
                }
            )
        )
        self.statuses["fixture-workshop"]["owner"] = "foreign"
        output = self.root / "journal.jsonl"
        with (
            mock.patch.object(journal_stand, "STAND", self.root),
            mock.patch.object(
                journal_stand, "stand_java", side_effect=lambda: self.running[:]
            ),
            mock.patch.object(journal_stand, "StandBackend", return_value=self.backend),
            mock.patch.object(journal_stand, "other_fabric", return_value=2),
            mock.patch.object(
                journal_stand,
                "rss_kb",
                side_effect=lambda pid: 2048 if pid == 100 else 3072,
            ),
            mock.patch.object(
                journal_stand, "heap", return_value={"used_mb": 1}
            ) as heap,
            mock.patch.object(
                journal_stand.time, "monotonic", side_effect=[0, 0, 6, 6.1, 10]
            ),
            mock.patch.object(journal_stand.time, "sleep"),
        ):
            journal_stand.record(output, duration=10)
        row = json.loads(output.read_text())
        self.assertEqual(row["rss_mb"], 5)
        self.assertEqual(row["apps"]["game"]["rss_mb"], 2)
        self.assertEqual(row["apps"]["workshop"]["rss_mb"], 3)
        self.assertEqual(row["unknown_java"], 1)
        self.assertTrue(
            any(
                "unknown Java process" in failure
                for failure in journal_stand.journal.check([row])
            )
        )
        heap.assert_called_once_with(100)
        self.assertIsNone(row["apps"]["workshop"]["heap"])

    def test_alternate_launcher_with_other_ports_blocks_start_and_fails_journal(self):
        self.backend.bootstrap_game("r1")
        self.ready("game")
        self.running.append((201, "workshop"))
        proc = self.root / "proc"
        other = self.root / "other"
        nested = self.apps["workshop"] / "nested"
        for path in (proc, other, nested):
            path.mkdir()
        for pid, cwd, command in (
            (100, self.apps["game"], self.commands[100]),
            (
                201,
                self.apps["workshop"],
                ["/fixture/java", "-jar", "server.jar", "--port", "25601"],
            ),
            (202, other, ["/fixture/java", "-jar", "server.jar"]),
            (203, nested, ["/fixture/java", "-jar", "server.jar"]),
        ):
            entry = proc / str(pid)
            entry.mkdir()
            (entry / "comm").write_text("java")
            (entry / "exe").symlink_to("/fixture/java")
            (entry / "cwd").symlink_to(cwd, target_is_directory=True)
            (entry / "cmdline").write_bytes(
                b"\0".join(os.fsencode(arg) for arg in command)
            )
        controller = self.root / "controller"
        controller.mkdir()
        (controller / "gate.json").write_text(
            json.dumps(
                {
                    "backends": {
                        "game": {"gate": "public"},
                        "workshop": {"gate": "closed"},
                    }
                }
            )
        )
        journal_stand = importlib.import_module("journal_stand")
        output = self.root / "unknown.jsonl"
        with (
            mock.patch.object(
                stand,
                "Path",
                side_effect=lambda value: (
                    proc if str(value) == "/proc" else Path(value)
                ),
            ),
            mock.patch.object(stand, "stand_java", side_effect=STAND_JAVA_INVENTORY),
            mock.patch.object(
                journal_stand, "stand_java", side_effect=STAND_JAVA_INVENTORY
            ),
            mock.patch.object(journal_stand, "STAND", self.root),
            mock.patch.object(journal_stand, "StandBackend", return_value=self.backend),
            mock.patch.object(journal_stand, "other_fabric", return_value=2),
            mock.patch.object(journal_stand, "rss_kb", return_value=2048),
            mock.patch.object(
                journal_stand, "heap", return_value={"used_mb": 1}
            ) as heap,
            mock.patch.object(
                journal_stand.time, "monotonic", side_effect=[0, 0, 6, 6.1, 10]
            ),
            mock.patch.object(journal_stand.time, "sleep"),
        ):
            self.assertEqual(
                sorted(stand.stand_java()), [(100, "game"), (201, "workshop")]
            )
            calls = self.output.call_count
            with self.assertRaisesRegex(RuntimeError, "already runs"):
                self.backend.start(self.goal)
            self.assertEqual(self.output.call_count, calls)
            journal_stand.record(output, duration=10)
        row = json.loads(output.read_text())
        self.assertEqual(
            (row["game_java"], row["workshop_java"], row["unknown_java"]), (1, 1, 1)
        )
        self.assertTrue(row["game_ready"])
        self.assertFalse(row["workshop_ready"])
        self.assertTrue(
            any(
                "unknown Java process" in failure
                for failure in journal_stand.journal.check([row])
            )
        )
        heap.assert_called_once_with(100)
        self.run.assert_not_called()


class OwnerTests(unittest.TestCase):
    def test_native_owner_cannot_be_overridden(self):
        with mock.patch.dict(
            os.environ,
            {"CODEX_THREAD_ID": "native", "BF_RIG_OWNER": "foreign"},
            clear=True,
        ):
            with self.assertRaises(SystemExit):
                paths.owner()
        with mock.patch.dict(
            os.environ,
            {"CODEX_THREAD_ID": "native", "BF_RIG_OWNER": "native"},
            clear=True,
        ):
            self.assertEqual(paths.owner(), "native")


if __name__ == "__main__":
    unittest.main()
