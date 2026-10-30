"""W04 packaging tests: static capability scan, wiring, lock and health.

Docker build/run checks (non-root writes, read-only refusal, restart) run on
Boat against synthetic named volumes; see deployment/docker-verify.sh. These
unit tests cover everything verifiable without a daemon: the spec files must
carry no forbidden capability, the backend switch must resolve, the second
writer must fail closed, and /healthz must stay 200 with Game off.
"""

import contextlib
import io
import json
import os
import re
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent
DOCKERFILE = ROOT / "Dockerfile"
COMPOSE = ROOT / "deployment" / "workshop-controller.compose.example.yaml"

# Forbidden on any effective (non full-line comment) line of the packaging
# files: no socket, privilege, extra capability, host namespace or host path.
FORBIDDEN = (
    r"docker\.sock",
    r"privileged\s*:\s*true",
    r"cap_add",
    r"network_mode\s*:\s*[\"']?host",
    r"\bpid\s*:\s*[\"']?host",
    r"\bipc\s*:\s*[\"']?host",
    r"^\s*devices\s*:",
    r"/proc(?=/|\s|$)",
    r"/sys(?=/|\s|$)",
    r"/var/run",
    r"\bnsenter\b",
)


def effective_lines(path):
    lines = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("#"):
            continue
        lines.append(line)
    return lines


class PackagingScanTests(unittest.TestCase):
    def test_no_forbidden_capability(self):
        for path in (DOCKERFILE, COMPOSE):
            with self.subTest(path=path.name):
                for lineno, line in enumerate(effective_lines(path), 1):
                    for pattern in FORBIDDEN:
                        self.assertIsNone(
                            re.search(pattern, line),
                            f"{path.name}:{lineno}: forbidden {pattern!r}: {line!r}",
                        )

    def test_volumes_are_named_only(self):
        entries = [
            line.strip().lstrip("-").strip()
            for line in effective_lines(COMPOSE)
            if re.match(r"\s*-\s+\S+:/\S", line)
        ]
        self.assertGreater(len(entries), 0, "compose spec must declare volumes")
        for entry in entries:
            self.assertRegex(entry, r"^[a-z0-9][a-z0-9-]*:/[a-z/]+(?::ro)?$")
            self.assertNotRegex(entry, r"/(srv|mnt|proc|sys|etc|var|home|root)(/|$)")

    def test_base_image_pinned_by_digest(self):
        froms = [
            line
            for line in effective_lines(DOCKERFILE)
            if line.strip().upper().startswith("FROM")
        ]
        self.assertEqual(len(froms), 1)
        self.assertRegex(froms[0], r"FROM\s+\S+@sha256:[0-9a-f]{64}(\s|$)")

    def test_toolchain_pinned_and_zlib_asserted(self):
        text = DOCKERFILE.read_text(encoding="utf-8")
        self.assertIn("3.12.14", text)
        self.assertIn("zlib", text)

    def test_runs_as_non_root(self):
        users = [
            line.split(None, 1)[1].strip()
            for line in effective_lines(DOCKERFILE)
            if line.strip().upper().startswith("USER")
        ]
        self.assertEqual(len(users), 1)
        self.assertNotIn(users[0], ("0", "0:0", "root", "root:root"))
        text = DOCKERFILE.read_text(encoding="utf-8")
        self.assertRegex(text, r"chown.*1000:1000\s+/state\s+/data\s+/locks")

    def test_entrypoint_needs_no_docker_or_shell(self):
        text = (ROOT / "entrypoint.py").read_text(encoding="utf-8")
        for pattern in (
            r"^import docker_backend",
            r"^from docker_backend",
            r"\bsubprocess\b",
            r"\bos\.system\b",
            r"\bdocker\.sock\b",
            r"cap_add",
            r"privileged",
        ):
            self.assertIsNone(
                re.search(pattern, text, re.M), f"entrypoint.py: {pattern!r}"
            )


class BackendWiringTests(unittest.TestCase):
    def test_backend_selection(self):
        import workshopctl
        from docker_backend import DockerBackend
        from dokploy_backend import DokployBackend

        self.assertEqual(
            workshopctl.backend_factory_from_name("docker").__func__,
            DockerBackend.from_env.__func__,
        )
        self.assertEqual(
            workshopctl.backend_factory_from_name("dokploy").__func__,
            DokployBackend.from_env.__func__,
        )
        with self.assertRaises(ValueError):
            workshopctl.backend_factory_from_name("swarm-admin")


class PackagingFunctionalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        from release_fixtures import catalog
        from test_controller import FakeBackend

        self.backend_factory = lambda: FakeBackend()
        (self.root / "controller").mkdir()
        (self.root / "controller" / "releases.json").write_text(json.dumps(catalog()))
        environment = mock.patch.dict(
            os.environ,
            {
                "BF_MAP_ARCHIVE_DIR": str(self.root / "history"),
                "BF_MAP_ACTIVE_DIR": str(self.root / "active"),
                "BF_TEMPLATES_DIR": str(self.root / "templates"),
                "BF_CANDIDATES_DIR": str(self.root / "candidates"),
                "BF_LEGACY_CANDIDATES_DIR": str(self.root / "legacy"),
                "BF_WORKSHOP_DATA": str(self.root / "workshop-data"),
                "BF_PUBLISHED_MAPS_DIR": str(self.root / "published"),
                "BF_GAME_MAPS_DIR": str(self.root / "game-maps"),
            },
        )
        environment.start()
        self.addCleanup(environment.stop)

    def argv(self, *command):
        return [
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
            *command,
        ]

    def test_second_writer_gets_busy(self):
        """Restart/single-writer gate: a held controller lock fails closed."""
        import workshopctl
        from controller import Controller

        first = Controller(self.root / "controller", self.backend_factory())
        out, err = io.StringIO(), io.StringIO()
        with first._locked():
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = workshopctl.main(self.argv("target"), self.backend_factory)
        self.assertEqual(code, workshopctl.BUSY)
        self.assertIn("holds the lock", err.getvalue())

    def test_health_ok_with_game_off(self):
        """Sleeping Game cannot fail the health endpoint: empty observe is 200."""
        import entrypoint
        import workshopctl

        backend = self.backend_factory()
        backend.processes = []  # Game off, Workshop sleeping
        controller, _ = workshopctl.build_controller(
            os.environ,
            lambda: backend,
            state_dir=str(self.root / "controller"),
            gate_file=str(self.root / "gate" / "gate.json"),
            public_file=str(self.root / "public" / "public.json"),
            admin_dir=str(self.root / "admin"),
            inbox_dir=str(self.root / "inbox"),
        )
        entrypoint.run_loop.tick = 1234.0
        server = None
        try:
            from http.server import HTTPServer

            server = HTTPServer(
                ("127.0.0.1", 0),
                entrypoint.make_handler(
                    lambda: controller, lambda: entrypoint.run_loop.tick
                ),
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            with urllib.request.urlopen(
                f"http://127.0.0.1:{server.server_port}/healthz", timeout=10
            ) as reply:
                body = json.loads(reply.read())
            self.assertEqual(reply.status, 200)
            self.assertEqual(body["status"], "ok")
            self.assertEqual(body["tick"], 1234.0)
        finally:
            if server is not None:
                server.shutdown()
                server.server_close()


if __name__ == "__main__":
    unittest.main()
