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
    APPROVED_TAG,
    DeployWebhook,
    DokployBackend,
    GhcrTagMover,
    HttpResponse,
    RconClient,
    RconError,
    RegistryError,
    WebhookError,
)

REPO = "ghcr.io/blockfield/blockfield-server"
DIGEST = "sha256:" + "d" * 64
REF = f"{REPO}@{DIGEST}"
OTHER_DIGEST = "sha256:" + "e" * 64
MANIFEST = b'{"schemaVersion":2,"fake":"manifest"}'
MEDIA = "application/vnd.oci.image.manifest.v1+json"
TOKEN = "SECRET-PACKAGE-TOKEN"
WEBHOOK_URL = "https://hooks.example/SEKRIT-hook"
RCON_PASSWORD = "SECRET-RCON-PASSWORD"


RELEASE = "v9.0.0"


def release_record(release=RELEASE, digest="d"):
    # The W02 catalog only resolves fully evidenced published releases;
    # build the record with the shared fixture so preflight accepts it.
    return _fixture_record(release, digest)


def seed_registry(registry, record):
    """Serve the record's real manifest bytes under its verified digest."""
    digest = record["imageDigest"].split("@", 1)[1]
    registry.manifests = {digest: (MEDIA, record["source"]["imageManifest"].encode())}
    return digest


class FakeRegistry:
    """Injectable GHCR transport: manifests by digest, one :approved tag."""

    def __init__(self):
        self.calls = []
        self.manifests = {DIGEST: (MEDIA, MANIFEST)}
        self.tag = None
        self.sabotage_tag = None
        self.fail_with = None

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append((method, url, dict(headers), body))
        if self.fail_with is not None:
            raise self.fail_with
        reference = url.rsplit("/", 1)[-1]
        if method == "GET" and reference == APPROVED_TAG:
            if self.tag is None:
                return HttpResponse(404, {}, b"")
            _media, content = self.manifests.get(self.tag, (MEDIA, b'{"unknown":true}'))
            return HttpResponse(
                200,
                {"docker-content-digest": self.tag, "content-type": MEDIA},
                content,
            )
        if method == "GET":
            if reference in self.manifests:
                media, content = self.manifests[reference]
                return HttpResponse(
                    200,
                    {"docker-content-digest": reference, "content-type": media},
                    content,
                )
            return HttpResponse(404, {}, b"")
        if method == "PUT" and reference == APPROVED_TAG:
            for known, (_media, content) in self.manifests.items():
                if content == body:
                    self.tag = known
                    break
            else:
                self.tag = "sha256:" + "0" * 64
            if self.sabotage_tag is not None:
                self.tag = self.sabotage_tag
            return HttpResponse(201, {}, b"")
        raise AssertionError(f"unexpected registry call {method} {url}")


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
    """Loopback Source RCON server: canned outputs, optional stop-to-dark."""

    def __init__(
        self,
        password=RCON_PASSWORD,
        outputs=None,
        stop_closes=True,
        mute_commands=(),
        paused=False,
    ):
        super().__init__(daemon=True)
        self.password = password
        self.outputs = outputs or {}
        self.stop_closes = stop_closes
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
                    _rid, _kind = struct.unpack("<ii", payload[:8])
                    words = payload[8:-2].decode()
                    if words == "":
                        continue  # trailing drain marker, not a command
                    self.commands.append(words)
                    if words in self.mute_commands:
                        threading.Event().wait(30)
                        return
                    conn.sendall(self.packet(1, self.outputs.get(words, b"")))
                    conn.sendall(self.packet(1, b""))
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
        self.registry = FakeRegistry()

    def mover(self, **kwargs):
        return GhcrTagMover(REPO, token=TOKEN, http=self.registry, **kwargs)

    def test_move_verifies_digest_after_put(self):
        self.assertEqual(self.mover().move(REF), DIGEST)
        methods = [call[0] for call in self.registry.calls]
        self.assertEqual(methods, ["GET", "GET", "PUT", "GET"])
        put = self.registry.calls[2]
        self.assertEqual(put[3], MANIFEST)
        # The credential travels in exactly one Authorization header per
        # call; it must never surface anywhere else (errors, logs).
        for _method, _url, headers, _body in self.registry.calls:
            self.assertEqual(headers["Authorization"], f"Bearer {TOKEN}")

    def test_move_is_idempotent_without_extra_put(self):
        mover = self.mover()
        mover.move(REF)
        self.assertEqual(mover.move(REF), DIGEST)
        puts = [c for c in self.registry.calls if c[0] == "PUT"]
        self.assertEqual(len(puts), 1)

    def test_wrong_digest_rejected_before_any_call(self):
        for bad in (
            "ghcr.io/blockfield/blockfield-server:latest",
            "ghcr.io/other/server@sha256:" + "f" * 64,
            "sha256:" + "d" * 64,
            REF.replace("d", "x"),
        ):
            with self.assertRaises(ValueError, msg=bad):
                self.mover().move(bad)
        self.assertEqual(self.registry.calls, [])

    def test_tag_moved_but_verification_mismatch_fails(self):
        self.registry.sabotage_tag = OTHER_DIGEST
        self.registry.manifests[OTHER_DIGEST] = (MEDIA, b'{"other":true}')
        with self.assertRaises(RegistryError):
            self.mover().move(REF)

    def test_registry_timeout_raises_without_credential(self):
        self.registry.fail_with = TimeoutError("slow")
        with self.assertRaises(RegistryError) as ctx:
            self.mover().move(REF)
        self.assertNotIn(TOKEN, str(ctx.exception))

    def test_missing_credential_lists_no_secret(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(RegistryError) as ctx:
                GhcrTagMover(REPO, http=self.registry)
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
        self.registry = FakeRegistry()
        self.record = release_record()
        self.digest = seed_registry(self.registry, self.record)
        self.webhook_calls = FakeWebhook([200])
        self.server = FakeRconServer(outputs={"save-all flush": b"Saved the game"})
        self.server.start()
        self.addCleanup(self.server.shutdown)
        self.backend = DokployBackend(
            data=self.data,
            lock_dir=self.lock,
            mover=GhcrTagMover(REPO, token=TOKEN, http=self.registry),
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

    def test_start_moves_tag_triggers_webhook_and_reports_ready(self):
        self.backend.start(self.goal)
        puts = [c for c in self.registry.calls if c[0] == "PUT"]
        self.assertEqual(len(puts), 1)
        self.assertEqual(self.registry.tag, self.digest)
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

    def test_retry_after_webhook_failure_keeps_single_tag_move(self):
        failing = FakeWebhook([500, 500])
        self.backend.webhook = DeployWebhook(
            url=WEBHOOK_URL, http=failing, attempts=2, sleep=lambda s: None
        )
        with self.assertRaises(WebhookError):
            self.backend.start(self.goal)
        self.assertEqual(self.registry.tag, self.digest)
        recovered = FakeWebhook([200])
        self.backend.webhook = DeployWebhook(
            url=WEBHOOK_URL, http=recovered, sleep=lambda s: None
        )
        self.backend.start(self.goal)
        puts = [c for c in self.registry.calls if c[0] == "PUT"]
        self.assertEqual(len(puts), 1)
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

    def test_wrong_digest_never_reaches_registry_or_webhook(self):
        with self.assertRaises(ValueError):
            self.backend.mover.move("ghcr.io/other/x@sha256:" + "f" * 64)
        self.assertEqual(self.registry.calls, [])
        self.assertEqual(self.webhook_calls.calls, [])

    def test_unknown_release_fails_preflight_before_effects(self):
        with self.assertRaises(ValueError):
            self.backend.start(Goal("workshop", "v-absent", str(uuid4())))
        self.assertEqual(self.registry.calls, [])
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
            "BF_GHCR_PACKAGE_TOKEN": TOKEN,
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
        self.registry = FakeRegistry()
        self.record = release_record()
        self.digest = seed_registry(self.registry, self.record)
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
            mover=GhcrTagMover(REPO, token=TOKEN, http=self.registry),
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
        self.assertEqual(self.registry.tag, self.digest)
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
        self.assertEqual(self.registry.calls, [])
        recovered = self.controller.step(recover=True)
        self.assertEqual(recovered["phase"], "STARTING")
        self.assertEqual(self.registry.tag, self.digest)


class ShadowRunTests(unittest.TestCase):
    """Read-only shadow: ticks against a sanitized state export with Game off
    and no java make no registry/webhook call and leave the exported gate and
    public projections byte-identical once converged; no secret lands on disk."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.data, self.lock = root / "data", root / "lock"
        self.data.mkdir()
        (self.lock / "workshop").mkdir(parents=True)
        self.registry = FakeRegistry()
        self.webhook = FakeWebhook([200])
        self.backend = DokployBackend(
            data=self.data,
            lock_dir=self.lock,
            mover=GhcrTagMover(REPO, token=TOKEN, http=self.registry),
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
        self.assertEqual(self.registry.calls, [])
        self.assertEqual(self.webhook.calls, [])
        self.assertEqual(first["phase"], "UNINITIALIZED")
        for path in (self.controller.directory).rglob("*"):
            if path.is_file():
                self.assertNotIn(
                    RCON_PASSWORD, path.read_text(encoding="utf-8", errors="replace")
                )


if __name__ == "__main__":
    unittest.main()
