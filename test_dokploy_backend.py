"""DokployBackend against a fake RCON server.

No docker.sock, no Dokploy API, no GitHub credentials: no network beyond
127.0.0.1 loopback. The deploy webhook fires only when
WORKSHOP_DEPLOY_WEBHOOK_URL is set, and tests mock the HTTP layer.
"""

import json
import logging
import os
import socket
import struct
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock
from uuid import uuid4

from controller import Controller, Goal
from release_fixtures import release_record as _fixture_record
from dokploy_backend import DokployBackend, RconClient, RconError

RCON_PASSWORD = "SECRET-RCON-PASSWORD"


RELEASE = "v9.0.0"


def release_record(release=RELEASE, digest="d"):
    # The W02 catalog only resolves fully evidenced published releases;
    # build the record with the shared fixture so preflight accepts it.
    return _fixture_record(release, digest)


class FakeRconServer(threading.Thread):
    """Loopback Source RCON server with vanilla framing: canned outputs,
    optional stop-to-dark, and an error-text reply (never an empty echo)
    to the empty drain marker."""

    VANILLA_DRAIN_ERROR = b"Unknown or incomplete command, see below for error<--[HERE]"

    def __init__(
        self,
        password=RCON_PASSWORD,
        outputs=None,
        stop_closes=True,
        mute_commands=(),
        paused=False,
        drop_stop_reply=False,
    ):
        super().__init__(daemon=True)
        self.password = password
        self.outputs = outputs or {}
        self.stop_closes = stop_closes
        self.drop_stop_reply = drop_stop_reply
        self.mute_commands = set(mute_commands)
        self.commands = []
        self.errors = []
        self._guard = threading.Lock()
        self.listener = socket.socket()
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(8)
        self.listener.settimeout(0.2)
        self.port = self.listener.getsockname()[1]
        self._halt = threading.Event()
        if paused:
            self.go_dark()

    def go_dark(self):
        """Closed port: TCP liveness probes fail, as after RCON stop."""
        with self._guard:
            if self.listener is not None:
                try:
                    self.listener.close()
                except OSError:
                    pass
                self.listener = None

    def come_back(self):
        """Rebind the same loopback port after go_dark()."""
        with self._guard:
            if self.listener is None:
                self.listener = socket.socket()
                self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                self.listener.bind(("127.0.0.1", self.port))
                self.listener.listen(8)
                self.listener.settimeout(0.2)

    def run(self):
        while not self._halt.is_set():
            with self._guard:
                listener = self.listener
            if listener is None:
                self._halt.wait(0.05)
                continue
            try:
                conn, _ = listener.accept()
            except (OSError, socket.timeout):
                continue
            threading.Thread(target=self.session, args=(conn,), daemon=True).start()

    def read_exact(self, conn, count):
        data = b""
        while len(data) < count:
            chunk = conn.recv(count - len(data))
            if not chunk:
                raise ConnectionError("eof")
            data += chunk
        return data

    def packet(self, request_id, body):
        return (
            struct.pack("<iii", 4 + 4 + len(body) + 2, request_id, 2)
            + body
            + b"\x00\x00"
        )

    def session(self, conn):
        with conn:
            conn.settimeout(2.0)
            try:
                (size,) = struct.unpack("<i", self.read_exact(conn, 4))
                payload = self.read_exact(conn, size)
                _rid, kind = struct.unpack("<ii", payload[:8])
                body = payload[8:-2]
                if kind != 3:
                    return
                if body.decode() != self.password:
                    conn.sendall(self.packet(-1, b""))
                    return
                conn.sendall(self.packet(1, b""))
                while True:
                    (size,) = struct.unpack("<i", self.read_exact(conn, 4))
                    payload = self.read_exact(conn, size)
                    rid, kind = struct.unpack("<ii", payload[:8])
                    if kind == 3:
                        # Re-auth fence: always answered, no game effect.
                        if payload[8:-2].decode() != self.password:
                            conn.sendall(self.packet(-1, b""))
                        else:
                            conn.sendall(self.packet(rid, b""))
                        continue
                    words = payload[8:-2].decode()
                    if words == "":
                        # Vanilla answers the empty drain marker with error
                        # text under its own id, never an empty echo.
                        conn.sendall(self.packet(rid, self.VANILLA_DRAIN_ERROR))
                        continue
                    self.commands.append(words)
                    if words in self.mute_commands:
                        threading.Event().wait(30)
                        return
                    body = self.outputs.get(words, b"")
                    if words == "stop" and self.drop_stop_reply:
                        # Mid-shutdown reset: the reply path dies with the
                        # reply unsent. Dark or alive afterwards decides it.
                        try:
                            conn.shutdown(socket.SHUT_RDWR)
                        except OSError:
                            pass
                        if self.stop_closes:
                            self.go_dark()
                        return
                    # Vanilla splits large replies into 4096 B packets.
                    for offset in range(0, max(len(body), 1), 4096):
                        conn.sendall(self.packet(rid, body[offset : offset + 4096]))
                    if words == "stop" and self.stop_closes:
                        self.go_dark()
                        return
            except (ConnectionError, OSError, socket.timeout):
                return

    def shutdown(self):
        self._halt.set()
        self.go_dark()
        self.join(timeout=5)


class RconTests(unittest.TestCase):
    def setUp(self):
        self.server = FakeRconServer(outputs={"save-all flush": b"Saved the game"})
        self.server.start()

    def tearDown(self):
        self.server.shutdown()

    def client(self, password=RCON_PASSWORD, **kwargs):
        return RconClient("127.0.0.1", self.server.port, password=password, **kwargs)

    def test_command_round_trip(self):
        with self.client() as rcon:
            self.assertEqual(rcon.command("save-all", "flush"), "Saved the game")
        self.assertEqual(self.server.commands, ["save-all flush"])

    def test_wrong_password_never_leaks(self):
        with self.assertRaises(RconError) as ctx:
            with self.client(password="WRONG-SECRET-PW") as rcon:
                rcon.command("save-all", "flush")
        self.assertNotIn("WRONG-SECRET-PW", str(ctx.exception))

    def test_missing_password_raises_rcon_error(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(RconError):
                RconClient("127.0.0.1", self.server.port)

    def test_multi_packet_output_concatenates(self):
        big = FakeRconServer(outputs={"help": b"x" * 5000})
        big.start()
        self.addCleanup(big.shutdown)
        with RconClient("127.0.0.1", big.port, password=RCON_PASSWORD) as rcon:
            self.assertEqual(rcon.command("help"), "x" * 5000)

    def test_silent_server_hits_timeout(self):
        quiet = FakeRconServer(mute_commands=("save-all flush",))
        quiet.start()
        self.addCleanup(quiet.shutdown)
        with self.assertRaises(RconError):
            with RconClient(
                "127.0.0.1", quiet.port, password=RCON_PASSWORD, timeout=0.2
            ) as rcon:
                rcon.command("save-all", "flush")


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.data, self.lock = root / "data", root / "lock"
        self.data.mkdir()
        (self.lock / "workshop").mkdir(parents=True)
        self.record = release_record()
        self.server = FakeRconServer(outputs={"save-all flush": b"Saved the game"})
        self.server.start()
        self.addCleanup(self.server.shutdown)
        self.backend = DokployBackend(
            data=self.data,
            lock_dir=self.lock,
            rcon_host="127.0.0.1",
            rcon_port=self.server.port,
            rcon_password=RCON_PASSWORD,
            releases={RELEASE: self.record},
            stop_timeout=2.0,
            poll=0.01,
            min_free_bytes=0,
        )
        self.goal = Goal("workshop", RELEASE, str(uuid4()))

    def ready(self, generation, session, release=RELEASE, profile="workshop"):
        (self.data / ".blockfield-ready.json").write_text(
            json.dumps(
                {
                    "profile": profile,
                    "generation": generation,
                    "session": session,
                    "release": release,
                }
            )
        )

    def test_start_persists_selection_and_reports_ready(self):
        # No tag move, no webhook configured: start() persists the verified
        # selection; the approved release is read back from the running
        # Workshop for reporting.
        self.backend.start(self.goal)
        selected = json.loads((self.lock / "workshop/profile.json").read_bytes())
        self.assertEqual(
            (selected["profile"], selected["release"], selected["session"]),
            ("workshop", RELEASE, self.goal.session),
        )
        self.assertFalse((self.data / ".blockfield-ready.json").exists())
        self.assertEqual(self.backend.observe()[0].ready, False)
        self.ready(selected["generation"], self.goal.session)
        observed = self.backend.observe()
        self.assertEqual(len(observed), 1)
        self.assertTrue(observed[0].ready)
        self.assertEqual(observed[0].goal.release, RELEASE)
        self.assertEqual(self.backend.readiness()["generation"], selected["generation"])

    def test_digest_selected_release_reports_tagged_ready(self):
        # move-approved-tag selects by image digest while the server attests
        # its baked build tag: the same verified record must read as ready.
        digest = self.record["imageDigest"].split("@", 1)[1]
        self.backend.releases = {RELEASE: self.record, digest: self.record}
        goal = Goal("workshop", digest, str(uuid4()))
        self.backend.start(goal)
        selected = json.loads((self.lock / "workshop/profile.json").read_bytes())
        self.assertEqual(selected["release"], digest)
        self.ready(selected["generation"], goal.session, release=RELEASE)
        observed = self.backend.observe()
        self.assertEqual(len(observed), 1)
        self.assertTrue(observed[0].ready)
        self.assertEqual(observed[0].goal.release, digest)

    def test_foreign_tag_does_not_report_ready_for_digest_goal(self):
        # Identity stays strict: another tag is not the selected record.
        digest = self.record["imageDigest"].split("@", 1)[1]
        self.backend.releases = {RELEASE: self.record, digest: self.record}
        goal = Goal("workshop", digest, str(uuid4()))
        self.backend.start(goal)
        selected = json.loads((self.lock / "workshop/profile.json").read_bytes())
        self.ready(selected["generation"], goal.session, release="v9.9.9")
        self.assertFalse(self.backend.observe()[0].ready)

    def test_retry_across_starts_reuses_generation(self):
        # A retried start() for the same goal reuses the generation
        # persisted in profile.json, not a fresh uuid4.
        self.backend.start(self.goal)
        first = json.loads((self.lock / "workshop/profile.json").read_bytes())
        self.backend.start(self.goal)
        second = json.loads((self.lock / "workshop/profile.json").read_bytes())
        self.assertEqual(second["generation"], first["generation"])

    def test_new_goal_mints_fresh_generation(self):
        self.backend.start(self.goal)
        first = json.loads((self.lock / "workshop/profile.json").read_bytes())
        other = Goal("workshop", RELEASE, str(uuid4()))
        self.backend.start(other)
        second = json.loads((self.lock / "workshop/profile.json").read_bytes())
        self.assertNotEqual(second["generation"], first["generation"])

    def test_rotated_candidate_mints_fresh_generation(self):
        # Same session, rotated candidate: the persisted generation belongs
        # to the old candidate, so start() must mint a fresh one, while a
        # retry of the same candidate reuses it.
        self.backend.maps = mock.Mock()
        session = str(uuid4())
        self.backend.start(Goal("test", RELEASE, session, candidate="a" * 64))
        first = json.loads((self.lock / "workshop/profile.json").read_bytes())
        self.backend.start(Goal("test", RELEASE, session, candidate="a" * 64))
        same = json.loads((self.lock / "workshop/profile.json").read_bytes())
        self.assertEqual(same["generation"], first["generation"])
        self.backend.start(Goal("test", RELEASE, session, candidate="b" * 64))
        rotated = json.loads((self.lock / "workshop/profile.json").read_bytes())
        self.assertNotEqual(rotated["generation"], first["generation"])

    def test_unknown_release_fails_preflight_before_effects(self):
        with self.assertRaises(ValueError):
            self.backend.start(Goal("workshop", "v-absent", str(uuid4())))
        self.assertFalse((self.lock / "workshop/profile.json").exists())

    def test_save_requires_success_and_stop_goes_dark(self):
        self.backend.start(self.goal)
        selected = json.loads((self.lock / "workshop/profile.json").read_bytes())
        self.ready(selected["generation"], self.goal.session)
        process = self.backend.observe()[0]
        self.assertTrue(process.ready)
        refusing = FakeRconServer(outputs={"save-all flush": b"nope"})
        refusing.start()
        self.addCleanup(refusing.shutdown)
        self.backend.rcon_port = refusing.port
        with self.assertRaises(RuntimeError):
            self.backend.save_and_freeze(process)
        self.backend.rcon_port = self.server.port
        self.backend.stop(process)
        self.assertEqual(self.backend.observe(), [])

    def test_stop_timeout_when_java_ignores_stop(self):
        stubborn = FakeRconServer(
            outputs={"save-all flush": b"Saved the game"}, stop_closes=False
        )
        stubborn.start()
        self.addCleanup(stubborn.shutdown)
        self.backend.rcon_port = stubborn.port
        self.backend.start(self.goal)
        with self.assertRaises(TimeoutError):
            self.backend.stop(self.backend.observe()[0])

    def test_stop_reset_then_dark_succeeds(self):
        resetting = FakeRconServer(
            outputs={"save-all flush": b"Saved the game"},
            drop_stop_reply=True,
            stop_closes=True,
        )
        resetting.start()
        self.addCleanup(resetting.shutdown)
        self.backend.rcon_port = resetting.port
        self.backend.start(self.goal)
        process = self.backend.observe()[0]
        self.backend.stop(process)
        self.assertEqual(self.backend.observe(), [])

    def test_stop_reset_while_alive_raises_chained_timeout(self):
        resetting = FakeRconServer(
            outputs={"save-all flush": b"Saved the game"},
            drop_stop_reply=True,
            stop_closes=False,
        )
        resetting.start()
        self.addCleanup(resetting.shutdown)
        self.backend.rcon_port = resetting.port
        self.backend.start(self.goal)
        with self.assertRaises(TimeoutError) as ctx:
            self.backend.stop(self.backend.observe()[0])
        self.assertIsInstance(ctx.exception.__cause__, RconError)

    def held_commands(self, *replies):
        """Run the hold with canned per-call RCON replies; return the command words."""
        calls = []
        replies = list(replies)

        def fake_command(instance, words, *rest):
            calls.append(" ".join((words, *rest)))
            reply = replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return reply

        with mock.patch.object(RconClient, "command", fake_command):
            with self.backend.workshop_saves_held():
                pass
        return calls

    def test_hot_snapshot_holds_saves_around_copy(self):
        self.assertEqual(
            self.held_commands(
                "Automatic saving is now enabled",
                "Automatic saving is now disabled",
                "Saved the game",
                "Automatic saving is now enabled",
            ),
            ["save-on", "save-off", "save-all flush", "save-on"],
        )

    def test_hot_snapshot_without_java_has_no_side_effect(self):
        with mock.patch.object(self.backend, "_alive", return_value=False):
            with mock.patch.object(
                RconClient, "command", side_effect=AssertionError("must not call RCON")
            ):
                with self.backend.workshop_saves_held():
                    pass

    def test_hot_snapshot_refuses_unflushed_copy_and_restores_autosave(self):
        with self.assertRaises(RuntimeError):
            self.held_commands(
                "Automatic saving is now enabled",
                "Automatic saving is now disabled",
                "nope",
                "Automatic saving is now enabled",
            )

    def test_hot_snapshot_retries_an_unconfirmed_save_on(self):
        with self.assertRaisesRegex(RuntimeError, "autosave restored"):
            self.held_commands(
                "Automatic saving is now enabled",
                "Automatic saving is now disabled",
                "Saved the game",
                "nope",
                "nope",
            )

    def test_hot_snapshot_entry_refuses_when_autosave_state_unknown(self):
        with self.assertRaisesRegex(RuntimeError, "autosave enabled"):
            self.held_commands("nope", "Automatic saving is now enabled")

    def test_fence_stops_a_lingering_backend(self):
        self.backend.start(self.goal)
        self.assertTrue(self.backend._alive())
        self.backend.fence_pending_starts()
        self.assertEqual(self.backend.observe(), [])

    def test_fence_rcon_flap_then_accepts_stop(self):
        # Booting java answers TCP before RCON auth: refused attempts retry
        # until one gets through, then the fence stops the backend.
        self.assertTrue(self.backend._alive())
        new_client = self.backend._rcon
        attempts = []

        def flaky():
            attempts.append(1)
            if len(attempts) <= 2:
                raise RconError("RCON connect failed: ConnectionRefusedError")
            return new_client()

        with mock.patch.object(self.backend, "_rcon", side_effect=flaky):
            self.backend.fence_pending_starts()
        self.assertEqual(self.backend.observe(), [])

    def test_fence_rcon_refused_until_dark_returns(self):
        # The fence must never turn a boot race into ERROR: a backend that
        # goes dark on its own is fenced, RCON errors notwithstanding.
        with mock.patch.object(
            self.backend, "_rcon", side_effect=RconError("RCON refused")
        ):
            with mock.patch.object(
                self.backend, "_alive", side_effect=[True, True, False, False]
            ):
                self.backend.fence_pending_starts()

    def test_fence_rcon_refused_while_alive_times_out(self):
        refusing = FakeRconServer(password="wrong-password", stop_closes=False)
        refusing.start()
        self.addCleanup(refusing.shutdown)
        self.backend.rcon_port = refusing.port
        with self.assertRaises(TimeoutError) as ctx:
            self.backend.fence_pending_starts()
        self.assertIsInstance(ctx.exception.__cause__, RconError)

    def test_game_is_never_managed(self):
        with self.assertRaises(ValueError):
            self.backend.preflight(Goal("game", RELEASE))
        from controller import Process

        with self.assertRaises(ValueError):
            self.backend.stop(Process(Goal("game", RELEASE), True))
        # No selection yet, but the fake java is alive: an unknown unready
        # workshop process, never anything game-shaped.
        observed = self.backend.observe()
        self.assertEqual([p.goal.profile for p in observed], ["workshop"])
        self.assertFalse(observed[0].ready)

    def test_observe_probes_tcp_once(self):
        self.backend.start(self.goal)
        with mock.patch.object(self.backend, "_alive", return_value=True) as alive:
            self.backend.observe()
        self.assertEqual(alive.call_count, 1)

    def test_from_env_loads_verified_catalog(self):
        catalog_path = Path(self.temp.name) / "releases.json"
        catalog_path.write_text(json.dumps({RELEASE: self.record}))
        env = {
            "BF_RELEASES_FILE": str(catalog_path),
        }
        with mock.patch.dict(os.environ, env, clear=True):
            backend = DokployBackend.from_env()
        self.assertEqual(set(backend.releases), {RELEASE})
        self.assertEqual(
            backend.releases[RELEASE]["imageDigest"], self.record["imageDigest"]
        )
        del env["BF_RELEASES_FILE"]
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(DokployBackend.from_env().releases, {})


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
        # The app starts dark: nothing listens yet, as before the first deploy.
        self.server = FakeRconServer(
            outputs={"save-all flush": b"Saved the game"}, paused=True
        )
        self.server.start()
        self.addCleanup(self.server.shutdown)
        self.backend = DokployBackend(
            data=self.data,
            lock_dir=self.lock,
            rcon_host="127.0.0.1",
            rcon_port=self.server.port,
            rcon_password=RCON_PASSWORD,
            releases={RELEASE: self.record},
            stop_timeout=2.0,
            poll=0.01,
            min_free_bytes=0,
        )
        self.controller = Controller(
            root / "state", self.backend, releases={RELEASE: release_record()}
        )

    def test_request_start_ready_stable_without_game(self):
        goal = Goal("workshop", RELEASE, str(uuid4()))
        self.controller.request(str(uuid4()), goal, 0)
        state = self.controller.step()
        self.assertEqual(state["phase"], "STARTING")
        selected = json.loads((self.lock / "workshop/profile.json").read_bytes())
        self.assertEqual(selected["release"], RELEASE)
        # The deploy brings java up; the mod reports READY for this generation.
        self.server.come_back()
        (self.data / ".blockfield-ready.json").write_text(
            json.dumps(
                {
                    "profile": "workshop",
                    "generation": selected["generation"],
                    "session": goal.session,
                    "release": RELEASE,
                }
            )
        )
        state = self.controller.step()
        self.assertEqual(
            (state["phase"], state["gate"], state["error"]),
            ("workshop", "builders", None),
        )
        self.assertEqual(state["desired"]["release"], RELEASE)

    def test_alive_unselected_java_blocks_explicit_recovery(self):
        # Fail closed like unknown host java: a living backend nobody
        # selected is not adopted; recovery fences it first.
        self.server.come_back()
        goal = Goal("workshop", RELEASE, str(uuid4()))
        self.controller.request(str(uuid4()), goal, 0)
        state = self.controller.step()
        self.assertEqual(state["phase"], "ERROR")
        self.assertIn("explicit recovery", state["errorMessage"])
        recovered = self.controller.step(recover=True)
        self.assertEqual(recovered["phase"], "STARTING")
        selected = json.loads((self.lock / "workshop/profile.json").read_bytes())
        self.assertEqual(selected["release"], RELEASE)

    def test_recover_adopts_live_matching_editor_without_restart(self):
        # Incident 2026-11-10 (coord#30): ERROR state with a healthy
        # editor; recover fenced/saved/stopped it, then sat in STARTING
        # forever because start() only persists the selection. Recovery
        # must adopt a live backend that already matches the goal.
        goal = Goal("workshop", RELEASE, str(uuid4()))
        self.controller.request(str(uuid4()), goal, 0)
        self.assertEqual(self.controller.step()["phase"], "STARTING")
        selected = json.loads((self.lock / "workshop/profile.json").read_bytes())
        # The deploy brings java up; the mod reports READY for this generation.
        self.server.come_back()
        (self.data / ".blockfield-ready.json").write_text(
            json.dumps(
                {
                    "profile": "workshop",
                    "generation": selected["generation"],
                    "session": goal.session,
                    "release": RELEASE,
                }
            )
        )
        self.assertEqual(self.controller.step()["phase"], "workshop")
        self.server.commands.clear()
        # ERROR with the editor still healthy: keep the valid journal,
        # flip only the phase, like the unreadable-state incident did.
        broken = self.controller.status()
        broken.update(phase="ERROR", gate="closed", error="Boom", errorMessage="boom")
        self.controller._write(broken)
        recovered = self.controller.step(recover=True)
        self.assertEqual(
            (recovered["phase"], recovered["gate"], recovered["error"]),
            ("workshop", "builders", None),
        )
        self.assertNotIn("stop", self.server.commands)
        self.assertEqual(self.controller.status()["phase"], "workshop")


class DeployWebhookTests(unittest.TestCase):
    """Stopped editor restarts through the Workshop app's Dokploy webhook.

    The HTTP layer is mocked: no real network beyond 127.0.0.1 loopback.
    """

    WEBHOOK = "https://dokploy.example.test/api/app/deploy/WEBHOOK-SECRET-TOKEN"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.data, self.lock = root / "data", root / "lock"
        self.data.mkdir()
        (self.lock / "workshop").mkdir(parents=True)
        # Dark backend: nothing listens, as after RCON stop.
        self.server = FakeRconServer(
            outputs={"save-all flush": b"Saved the game"}, paused=True
        )
        self.server.start()
        self.addCleanup(self.server.shutdown)
        self.backend = DokployBackend(
            data=self.data,
            lock_dir=self.lock,
            rcon_host="127.0.0.1",
            rcon_port=self.server.port,
            rcon_password=RCON_PASSWORD,
            releases={RELEASE: release_record()},
            stop_timeout=1.0,
            poll=0.01,
            min_free_bytes=0,
        )
        self.backend._sleep = mock.Mock()
        self.goal = Goal("workshop", RELEASE, str(uuid4()))
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
        selected = json.loads((self.lock / "workshop/profile.json").read_bytes())
        self.assertEqual(selected["release"], RELEASE)
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
        # A timeout may follow an accepted deploy: retrying could queue a second
        # deployment that restarts a ready editor.
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

    def test_webhook_failure_keeps_selection_and_warns(self):
        self.open.side_effect = urllib.error.URLError(ConnectionRefusedError("down"))
        with mock.patch.dict(os.environ, {"WORKSHOP_DEPLOY_WEBHOOK_URL": self.WEBHOOK}):
            with self.assertLogs(
                logging.getLogger("dokploy_backend"), "WARNING"
            ) as logs:
                self.backend.start(self.goal)  # must not raise: no ERROR loop
        self.assertEqual(self.open.call_count, 3)  # bounded retry, then give up
        output = "\n".join(logs.output)
        self.assertNotIn(self.WEBHOOK, output)
        selected = json.loads((self.lock / "workshop/profile.json").read_bytes())
        self.assertEqual(selected["release"], RELEASE)

    def test_unset_env_keeps_old_path_with_warning(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertLogs(
                logging.getLogger("dokploy_backend"), "WARNING"
            ) as logs:
                self.backend.start(self.goal)
        self.assertEqual(self.open.call_count, 0)
        self.assertIn("WORKSHOP_DEPLOY_WEBHOOK_URL", "\n".join(logs.output))
        selected = json.loads((self.lock / "workshop/profile.json").read_bytes())
        self.assertEqual(selected["release"], RELEASE)

    def test_rejected_scheme_raises_without_url_before_effects(self):
        bad = "http://dokploy.example.test/api/app/deploy/BAD-SECRET-TOKEN"
        with mock.patch.dict(os.environ, {"WORKSHOP_DEPLOY_WEBHOOK_URL": bad}):
            with self.assertRaises(ValueError) as ctx:
                self.backend.start(self.goal)
        self.assertNotIn(bad, str(ctx.exception))
        self.assertEqual(self.open.call_count, 0)
        self.assertFalse((self.lock / "workshop/profile.json").exists())

    def test_recover_on_stopped_editor_posts_webhook_once(self):
        # Incident behind coord#30: ERROR with a stopped editor; explicit
        # recovery must bring it back with exactly one webhook POST.
        controller = Controller(
            Path(self.temp.name) / "state",
            self.backend,
            releases={RELEASE: release_record()},
        )
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
    """Read-only shadow: ticks against a sanitized state export with Game off
    and no java make no external call at all and leave the exported gate and
    public projections byte-identical once converged; no secret lands on disk."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.data, self.lock = root / "data", root / "lock"
        self.data.mkdir()
        (self.lock / "workshop").mkdir(parents=True)
        self.backend = DokployBackend(
            data=self.data,
            lock_dir=self.lock,
            rcon_host="127.0.0.1",
            rcon_port=1,  # nothing listens: TCP probe refuses, no auth sent
            rcon_password=RCON_PASSWORD,
            releases={RELEASE: release_record()},
            stop_timeout=1.0,
            poll=0.01,
            min_free_bytes=0,
        )
        self.controller = Controller(
            root / "state",
            self.backend,
            gate_file=root / "gate.json",
            public_file=root / "public.json",
            inbox_dir=root / "inbox",
            releases={RELEASE: release_record()},
        )

    def test_shadow_ticks_make_no_external_call(self):
        (self.controller.inbox_dir).mkdir(parents=True, exist_ok=True)
        self.controller.process_inbox()
        first = self.controller.step()
        gate_once = (self.controller.gate_file).read_bytes()
        public_once = (self.controller.public_file).read_bytes()
        for _ in range(3):
            self.controller.process_inbox()
            self.controller.step()
            self.controller.status()
        self.assertEqual((self.controller.gate_file).read_bytes(), gate_once)
        self.assertEqual((self.controller.public_file).read_bytes(), public_once)
        self.assertEqual(first["phase"], "UNINITIALIZED")
        for path in (self.controller.directory).rglob("*"):
            if path.is_file():
                self.assertNotIn(
                    RCON_PASSWORD, path.read_text(encoding="utf-8", errors="replace")
                )


if __name__ == "__main__":
    unittest.main()
