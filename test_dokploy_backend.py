"""DokployBackend against fake GHCR/webhook HTTP and a fake RCON server.

No docker.sock, no Dokploy API, no network beyond 127.0.0.1 loopback.
"""

import json
import os
import socket
import struct
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock
from uuid import uuid4

from controller import Controller, Goal
from release_fixtures import release_record as _fixture_record
from dokploy_backend import (
    DeployWebhook,
    DokployBackend,
    HttpResponse,
    RconClient,
    RconError,
    RegistryError,
    WebhookError,
    WorkflowTagMover,
)

REPO = "ghcr.io/blockfield/blockfield-server"
DIGEST = "sha256:" + "d" * 64
REF = f"{REPO}@{DIGEST}"
TOKEN = "SECRET-WORKFLOW-TOKEN"
WEBHOOK_URL = "https://hooks.example/SEKRIT-hook"
RCON_PASSWORD = "SECRET-RCON-PASSWORD"


RELEASE = "v9.0.0"


def release_record(release=RELEASE, digest="d"):
    # The W02 catalog only resolves fully evidenced published releases;
    # build the record with the shared fixture so preflight accepts it.
    return _fixture_record(release, digest)


class FakeGitHub:
    """Injectable GitHub Actions transport: dispatch + workflow runs polling.

    POST /dispatches records the digest/nonce and (on 204) stages a run;
    GET .../runs reports it in_progress for ``polls_to_settle`` polls, then
    with ``conclusion``. Scripted per-dispatch ``outcomes`` are exceptions
    (raised) or statuses (returned); exhausted scripts fall back to
    ``dispatch_status``.
    """

    def __init__(self, conclusion="success", polls_to_settle=0, dispatch_status=204):
        self.calls = []
        self.conclusion = conclusion
        self.polls_to_settle = polls_to_settle
        self.dispatch_status = dispatch_status
        self.dispatch_outcomes = None
        self.polls = 0
        self.nonce = None
        self.digest = None

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append((method, url, dict(headers), body))
        if method == "POST" and url.endswith("/dispatches"):
            outcome = None
            if self.dispatch_outcomes:
                outcome = self.dispatch_outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            status = outcome if isinstance(outcome, int) else self.dispatch_status
            if status == 204:
                payload = json.loads(body)
                self.nonce = payload["inputs"]["nonce"]
                self.digest = payload["inputs"]["digest"]
            return HttpResponse(status, {}, b"{}")
        if method == "GET" and "/runs" in url:
            self.polls += 1
            if self.polls <= self.polls_to_settle:
                run = {
                    "name": f"approved-tag {self.nonce}",
                    "status": "in_progress",
                    "conclusion": None,
                }
            else:
                run = {
                    "name": f"approved-tag {self.nonce}",
                    "status": "completed",
                    "conclusion": self.conclusion,
                }
            return HttpResponse(200, {}, json.dumps({"workflow_runs": [run]}).encode())
        raise AssertionError(f"unexpected github call {method} {url}")


class FakeWebhook:
    """Scripted webhook transport: outcomes are statuses or exceptions."""

    def __init__(self, outcomes=(200,)):
        self.calls = []
        self.outcomes = list(outcomes)

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append((method, url, dict(headers), body))
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(outcome, Exception):
            raise outcome
        return HttpResponse(outcome, {}, b"{}")


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


class MoverTests(unittest.TestCase):
    def setUp(self):
        self.github = FakeGitHub()

    def mover(self, **kwargs):
        kwargs.setdefault("sleep", lambda s: None)
        return WorkflowTagMover(REPO, token=TOKEN, http=self.github, **kwargs)

    def test_move_dispatches_digest_and_confirms_success(self):
        self.assertEqual(self.mover().move(REF), DIGEST)
        posts = [call for call in self.github.calls if call[0] == "POST"]
        self.assertEqual(len(posts), 1)
        _method, url, _headers, body = posts[0]
        self.assertTrue(
            url.endswith(
                "/repos/Blockfield/blockfield-server"
                "/actions/workflows/move-approved-tag.yml/dispatches"
            ),
            url,
        )
        payload = json.loads(body)
        self.assertEqual(payload["ref"], "main")
        self.assertEqual(payload["inputs"]["digest"], REF)
        self.assertTrue(payload["inputs"]["nonce"])
        self.assertEqual(self.github.digest, REF)
        # The credential travels in exactly one Authorization header per
        # call; it must never surface anywhere else (errors, logs).
        for _method, _url, headers, _body in self.github.calls:
            self.assertEqual(headers["Authorization"], f"Bearer {TOKEN}")
        self.assertNotIn(TOKEN, body.decode())

    def test_move_is_convergent_without_registry_reads(self):
        mover = self.mover()
        mover.move(REF)
        self.assertEqual(mover.move(REF), DIGEST)
        # No registry calls at all: the package is private and this
        # credential carries no package scope, so every move re-dispatches
        # the same digest and the workflow converges on the same tag.
        for _method, url, _headers, _body in self.github.calls:
            self.assertTrue(url.startswith("https://api.github.com/"), url)
        posts = [c for c in self.github.calls if c[0] == "POST"]
        self.assertEqual(len(posts), 2)
        self.assertTrue(all(json.loads(p[3])["inputs"]["digest"] == REF for p in posts))

    def test_wrong_digest_rejected_before_any_call(self):
        for bad in (
            "ghcr.io/blockfield/blockfield-server:latest",
            "ghcr.io/other/server@sha256:" + "f" * 64,
            "sha256:" + "d" * 64,
            REF.replace("d", "x"),
        ):
            with self.assertRaises(ValueError, msg=bad):
                self.mover().move(bad)
        self.assertEqual(self.github.calls, [])

    def test_dispatch_rejection_fails_fast_without_retry(self):
        self.github.dispatch_status = 403
        with self.assertRaises(RegistryError) as ctx:
            self.mover().move(REF)
        self.assertIn("403", str(ctx.exception))
        posts = [c for c in self.github.calls if c[0] == "POST"]
        self.assertEqual(len(posts), 1)
        self.assertNotIn(TOKEN, str(ctx.exception))

    def test_dispatch_retry_then_success(self):
        delays = []
        self.github.dispatch_outcomes = [TimeoutError("slow"), 500]
        mover = self.mover(dispatch_delay=0.5, sleep=delays.append)
        self.assertEqual(mover.move(REF), DIGEST)
        posts = [c for c in self.github.calls if c[0] == "POST"]
        self.assertEqual(len(posts), 3)
        self.assertEqual(delays, [0.5, 1.0])

    def test_workflow_failure_conclusion_raises_without_credential(self):
        self.github.conclusion = "failure"
        with self.assertRaises(RegistryError) as ctx:
            self.mover().move(REF)
        self.assertIn("failure", str(ctx.exception))
        self.assertNotIn(TOKEN, str(ctx.exception))

    def test_run_never_settles_times_out(self):
        github = FakeGitHub(polls_to_settle=10**9)
        mover = WorkflowTagMover(
            REPO,
            token=TOKEN,
            http=github,
            sleep=lambda s: None,
            poll_interval=0.001,
            confirm_timeout=0.05,
        )
        with self.assertRaises(RegistryError) as ctx:
            mover.move(REF)
        self.assertIn("in time", str(ctx.exception))

    def test_queued_run_polls_until_success(self):
        github = FakeGitHub(polls_to_settle=2)
        mover = WorkflowTagMover(REPO, token=TOKEN, http=github, sleep=lambda s: None)
        self.assertEqual(mover.move(REF), DIGEST)
        self.assertEqual(github.polls, 3)

    def test_unreadable_run_list_raises_without_credential(self):
        def broken(method, url, headers, body, timeout):
            if method == "GET":
                return HttpResponse(200, {}, b"not json")
            return self.github(method, url, headers, body, timeout)

        with self.assertRaises(RegistryError) as ctx:
            WorkflowTagMover(REPO, token=TOKEN, http=broken, sleep=lambda s: None).move(
                REF
            )
        self.assertNotIn(TOKEN, str(ctx.exception))

    def test_missing_credential_lists_no_secret(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(RegistryError) as ctx:
                WorkflowTagMover(REPO, http=self.github)
        self.assertNotIn(TOKEN, str(ctx.exception))


class WebhookTests(unittest.TestCase):
    def trigger(self, outcomes, **kwargs):
        fake = FakeWebhook(outcomes)
        hook = DeployWebhook(url=WEBHOOK_URL, http=fake, sleep=lambda s: None, **kwargs)
        return fake, hook

    def test_retry_then_success_sends_idempotency_key(self):
        fake, hook = self.trigger([TimeoutError("slow"), 500, 200])
        hook.trigger("op-1")
        self.assertEqual(len(fake.calls), 3)
        for _method, _url, headers, body in fake.calls:
            self.assertEqual(headers["Idempotency-Key"], "op-1")
            self.assertEqual(json.loads(body), {"operation": "op-1"})

    def test_partial_failure_tag_moved_but_webhook_failed(self):
        # The rollout leaves :approved moved; the bounded retry gives up here
        # and the next operation continues from the moved tag (see backend test).
        _fake, hook = self.trigger([500, 500], attempts=2)
        with self.assertRaises(WebhookError) as ctx:
            hook.trigger("op-2")
        self.assertIn("2 attempts", str(ctx.exception))
        self.assertNotIn("SEKRIT", str(ctx.exception))

    def test_rejection_fails_fast_without_retry(self):
        fake, hook = self.trigger([403])
        with self.assertRaises(WebhookError):
            hook.trigger("op-3")
        self.assertEqual(len(fake.calls), 1)

    def test_missing_url_raises_webhook_error(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(WebhookError):
                DeployWebhook(http=FakeWebhook([200]), sleep=lambda s: None)

    def test_plain_http_url_rejected(self):
        with self.assertRaises(ValueError):
            DeployWebhook(
                url="http://hooks.example/hook",
                http=FakeWebhook([200]),
                sleep=lambda s: None,
            )

    def test_loopback_http_allowed_for_tests(self):
        DeployWebhook(
            url="http://127.0.0.1:9/hook", http=FakeWebhook([200]), sleep=lambda s: None
        )

    def test_backoff_is_bounded(self):
        delays = []
        fake = FakeWebhook([500, 500, 200])
        hook = DeployWebhook(
            url=WEBHOOK_URL, http=fake, attempts=3, delay=0.5, sleep=delays.append
        )
        hook.trigger("op-4")
        self.assertEqual(delays, [0.5, 1.0])


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
        self.github = FakeGitHub()
        self.record = release_record()
        self.webhook_calls = FakeWebhook([200])
        self.server = FakeRconServer(outputs={"save-all flush": b"Saved the game"})
        self.server.start()
        self.addCleanup(self.server.shutdown)
        self.backend = DokployBackend(
            data=self.data,
            lock_dir=self.lock,
            mover=WorkflowTagMover(
                REPO, token=TOKEN, http=self.github, sleep=lambda s: None
            ),
            webhook=DeployWebhook(
                url=WEBHOOK_URL, http=self.webhook_calls, sleep=lambda s: None
            ),
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

    def test_start_dispatches_tag_move_triggers_webhook_and_reports_ready(self):
        self.backend.start(self.goal)
        posts = [c for c in self.github.calls if c[0] == "POST"]
        self.assertEqual(len(posts), 1)
        self.assertEqual(self.github.digest, self.record["imageDigest"])
        self.assertEqual(len(self.webhook_calls.calls), 1)
        selected = json.loads((self.lock / "workshop/profile.json").read_bytes())
        self.assertEqual(
            (selected["profile"], selected["release"], selected["session"]),
            ("workshop", RELEASE, self.goal.session),
        )
        self.assertEqual(
            self.webhook_calls.calls[0][2]["Idempotency-Key"],
            selected["generation"],
        )
        self.assertFalse((self.data / ".blockfield-ready.json").exists())
        self.assertEqual(self.backend.observe()[0].ready, False)
        self.ready(selected["generation"], self.goal.session)
        observed = self.backend.observe()
        self.assertEqual(len(observed), 1)
        self.assertTrue(observed[0].ready)
        self.assertEqual(observed[0].goal.release, RELEASE)
        self.assertEqual(self.backend.readiness()["generation"], selected["generation"])

    def test_retry_after_webhook_failure_redispatches_same_digest(self):
        failing = FakeWebhook([500, 500])
        self.backend.webhook = DeployWebhook(
            url=WEBHOOK_URL, http=failing, attempts=2, sleep=lambda s: None
        )
        with self.assertRaises(WebhookError):
            self.backend.start(self.goal)
        self.assertEqual(self.github.digest, self.record["imageDigest"])
        recovered = FakeWebhook([200])
        self.backend.webhook = DeployWebhook(
            url=WEBHOOK_URL, http=recovered, sleep=lambda s: None
        )
        self.backend.start(self.goal)
        posts = [c for c in self.github.calls if c[0] == "POST"]
        self.assertEqual(len(posts), 2)
        self.assertTrue(
            all(
                json.loads(p[3])["inputs"]["digest"] == self.record["imageDigest"]
                for p in posts
            )
        )
        self.assertEqual(len(recovered.calls), 1)

    def test_retry_across_starts_reuses_idempotency_key(self):
        # A retried start() for the same goal must POST with the generation
        # persisted in profile.json before the tag move, not a fresh uuid4.
        failing = FakeWebhook([TimeoutError("slow")])
        self.backend.webhook = DeployWebhook(
            url=WEBHOOK_URL, http=failing, attempts=1, sleep=lambda s: None
        )
        with self.assertRaises(WebhookError):
            self.backend.start(self.goal)
        first = json.loads((self.lock / "workshop/profile.json").read_bytes())
        recovered = FakeWebhook([200])
        self.backend.webhook = DeployWebhook(
            url=WEBHOOK_URL, http=recovered, sleep=lambda s: None
        )
        self.backend.start(self.goal)
        second = json.loads((self.lock / "workshop/profile.json").read_bytes())
        self.assertEqual(second["generation"], first["generation"])
        self.assertEqual(
            recovered.calls[0][2]["Idempotency-Key"],
            failing.calls[0][2]["Idempotency-Key"],
        )
        self.assertEqual(recovered.calls[0][2]["Idempotency-Key"], first["generation"])

    def test_new_goal_mints_fresh_generation(self):
        self.backend.start(self.goal)
        first = json.loads((self.lock / "workshop/profile.json").read_bytes())
        other = Goal("workshop", RELEASE, str(uuid4()))
        self.backend.start(other)
        second = json.loads((self.lock / "workshop/profile.json").read_bytes())
        self.assertNotEqual(second["generation"], first["generation"])

    def test_wrong_digest_never_reaches_github_or_webhook(self):
        with self.assertRaises(ValueError):
            self.backend.mover.move("ghcr.io/other/x@sha256:" + "f" * 64)
        self.assertEqual(self.github.calls, [])
        self.assertEqual(self.webhook_calls.calls, [])

    def test_unknown_release_fails_preflight_before_effects(self):
        with self.assertRaises(ValueError):
            self.backend.start(Goal("workshop", "v-absent", str(uuid4())))
        self.assertEqual(self.github.calls, [])
        self.assertEqual(self.webhook_calls.calls, [])
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

    def test_fence_stops_a_lingering_backend(self):
        self.backend.start(self.goal)
        self.assertTrue(self.backend._alive())
        self.backend.fence_pending_starts()
        self.assertEqual(self.backend.observe(), [])

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
            "BF_WORKSHOP_TAG_TOKEN": TOKEN,
            "BF_WORKSHOP_DEPLOY_WEBHOOK_URL": WEBHOOK_URL,
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
        self.github = FakeGitHub()
        self.record = release_record()
        self.webhook = FakeWebhook([200])
        # The app starts dark: nothing listens yet, as before the first deploy.
        self.server = FakeRconServer(
            outputs={"save-all flush": b"Saved the game"}, paused=True
        )
        self.server.start()
        self.addCleanup(self.server.shutdown)
        self.backend = DokployBackend(
            data=self.data,
            lock_dir=self.lock,
            mover=WorkflowTagMover(
                REPO, token=TOKEN, http=self.github, sleep=lambda s: None
            ),
            webhook=DeployWebhook(
                url=WEBHOOK_URL, http=self.webhook, sleep=lambda s: None
            ),
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
        self.assertEqual(self.github.digest, self.record["imageDigest"])
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
        self.assertEqual(self.github.calls, [])
        recovered = self.controller.step(recover=True)
        self.assertEqual(recovered["phase"], "STARTING")
        self.assertEqual(self.github.digest, self.record["imageDigest"])


class ShadowRunTests(unittest.TestCase):
    """Read-only shadow: ticks against a sanitized state export with Game off
    and no java make no GitHub dispatch/webhook call and leave the exported gate and
    public projections byte-identical once converged; no secret lands on disk."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.data, self.lock = root / "data", root / "lock"
        self.data.mkdir()
        (self.lock / "workshop").mkdir(parents=True)
        self.github = FakeGitHub()
        self.webhook = FakeWebhook([200])
        self.backend = DokployBackend(
            data=self.data,
            lock_dir=self.lock,
            mover=WorkflowTagMover(
                REPO, token=TOKEN, http=self.github, sleep=lambda s: None
            ),
            webhook=DeployWebhook(
                url=WEBHOOK_URL, http=self.webhook, sleep=lambda s: None
            ),
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
        self.assertEqual(self.github.calls, [])
        self.assertEqual(self.webhook.calls, [])
        self.assertEqual(first["phase"], "UNINITIALIZED")
        for path in (self.controller.directory).rglob("*"):
            if path.is_file():
                self.assertNotIn(
                    RCON_PASSWORD, path.read_text(encoding="utf-8", errors="replace")
                )


if __name__ == "__main__":
    unittest.main()
