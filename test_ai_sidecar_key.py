"""Controller-written AI sidecar key (server#321 U-W1)."""

import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import controller
from controller import (
    AI_SIDECAR_KEY_NAME,
    AI_SIDECAR_KEY_PATTERN,
    Controller,
    ensure_ai_sidecar_key,
)
from release_fixtures import catalog
from test_controller import FakeBackend


class EnsureAiSidecarKeyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)
        self.base = self.dir / "workshop-data"

    def key_path(self):
        return self.base / "blockfield-workshop" / AI_SIDECAR_KEY_NAME

    def test_creates_once_with_sidecar_only_permissions(self):
        first = ensure_ai_sidecar_key(self.base)
        self.assertEqual(first, self.key_path())
        text = first.read_text()
        self.assertRegex(text, r"\A[A-Za-z0-9_-]{20,2048}\Z")
        self.assertEqual(first.stat().st_mode & 0o777, 0o600)
        self.assertEqual(
            (first.stat().st_uid, first.stat().st_gid), (os.getuid(), os.getgid())
        )
        directory = self.base / "blockfield-workshop"
        self.assertEqual(directory.stat().st_mode & 0o777, 0o755)
        second = ensure_ai_sidecar_key(self.base)
        self.assertEqual(second.read_text(), text, "never rotated here")

    def test_leaves_existing_file_alone(self):
        path = self.key_path()
        path.parent.mkdir(parents=True)
        path.write_text("operator-managed-secret-0123456789-_AB")
        self.assertEqual(
            ensure_ai_sidecar_key(self.base).read_text(),
            "operator-managed-secret-0123456789-_AB",
        )

    def test_hardens_existing_key_permissions(self):
        path = self.key_path()
        path.parent.mkdir(parents=True)
        path.write_text("operator-managed-secret-0123456789-_AB")
        os.chmod(path, 0o644)
        self.assertEqual(
            ensure_ai_sidecar_key(self.base).read_text(),
            "operator-managed-secret-0123456789-_AB",
        )
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_rejects_symlink_key(self):
        target = self.dir / "elsewhere.key"
        target.write_text("operator-managed-secret-0123456789-_AB")
        path = self.key_path()
        path.parent.mkdir(parents=True)
        path.symlink_to(target)
        with self.assertRaises(ValueError):
            ensure_ai_sidecar_key(self.base)

    def test_rejects_invalid_existing_key(self):
        path = self.key_path()
        path.parent.mkdir(parents=True)
        path.write_text("too short")
        with self.assertRaises(ValueError):
            ensure_ai_sidecar_key(self.base)

    def test_tolerates_unprivileged_chown(self):
        with mock.patch("controller.os.chown", side_effect=PermissionError):
            first = ensure_ai_sidecar_key(self.base)
            self.assertTrue(AI_SIDECAR_KEY_PATTERN.fullmatch(first.read_text()))
            self.assertEqual(first.stat().st_mode & 0o777, 0o600)
            second = ensure_ai_sidecar_key(self.base)
            self.assertEqual(second.read_text(), first.read_text())

    def test_unprivileged_run_keeps_caller_owned_directory(self):
        directory = self.base / "blockfield-workshop"
        directory.mkdir(parents=True)
        with (
            mock.patch("controller.AI_KEY_UID", 1999),
            mock.patch("controller.AI_KEY_GID", 1999),
            mock.patch("controller.os.geteuid", return_value=1000),
        ):
            ensure_ai_sidecar_key(self.base)  # must not raise for the caller-owned dir

    def test_privileged_run_still_fails_closed_on_foreign_directory(self):
        directory = self.base / "blockfield-workshop"
        directory.mkdir(parents=True)
        with (
            mock.patch("controller.AI_KEY_UID", 1999),
            mock.patch("controller.AI_KEY_GID", 1999),
            mock.patch("controller.os.geteuid", return_value=0),
        ):
            with self.assertRaises(ValueError):
                ensure_ai_sidecar_key(self.base)

    def test_symlink_swap_during_harden_leaves_victim_alone(self):
        """A dir-writer swapping the key for a symlink mid-harden must not
        move ownership/mode changes onto the target (r4520336542)."""
        path = self.key_path()
        path.parent.mkdir(parents=True)
        path.write_text("operator-managed-secret-0123456789-_AB")
        victim = self.dir / "victim.txt"
        victim.write_text("victim-content-here-0123456789ABCD")
        os.chmod(victim, 0o644)
        real_chown = os.chown

        def swapping_chown(target, *args):
            if Path(target) == path:
                path.unlink()
                path.symlink_to(victim)
            return real_chown(target, *args)

        with mock.patch("controller.os.chown", side_effect=swapping_chown):
            ensure_ai_sidecar_key(self.base)
        self.assertEqual(victim.read_text(), "victim-content-here-0123456789ABCD")
        self.assertEqual(victim.stat().st_mode & 0o777, 0o644)

    def test_concurrent_creators_commit_single_key(self):
        """Racers starting with the key absent end with one key: the loser
        reads the winner instead of rotating it (r4520336551)."""
        workers = 8
        (self.base / "blockfield-workshop").mkdir(parents=True)
        gate = threading.Barrier(workers)
        lock = threading.Lock()
        given = iter(f"race-{i:04d}-" + "k" * 24 for i in range(workers))

        def token_urlsafe(_size):
            with lock:
                value = next(given)
            gate.wait(timeout=30)
            return value

        key = self.key_path()
        committed: list[bytes] = []
        commit_lock = threading.Lock()
        real_atomic_write = controller.atomic_write
        real_link = os.link

        def recording_atomic_write(target, data, *args, **kwargs):
            if Path(target) == key:
                with commit_lock:
                    committed.append(bytes(data))
            return real_atomic_write(target, data, *args, **kwargs)

        def recording_link(source, destination, *args, **kwargs):
            real_link(source, destination, *args, **kwargs)
            if Path(destination) == key:
                with commit_lock:
                    committed.append(Path(source).read_bytes())

        errors: list[BaseException] = []
        seen: list[str] = []

        def work():
            try:
                ensure_ai_sidecar_key(self.base)
                seen.append(key.read_text())
            except BaseException as error:  # collected, re-raised below
                errors.append(error)

        with (
            mock.patch("controller.secrets.token_urlsafe", side_effect=token_urlsafe),
            mock.patch("controller.atomic_write", side_effect=recording_atomic_write),
            mock.patch("controller.os.link", side_effect=recording_link),
        ):
            threads = [threading.Thread(target=work) for _ in range(workers)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=60)
        self.assertEqual(errors, [])
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(
            len(set(committed)), 1, "exactly one secret reaches the key path"
        )
        for text in seen:
            self.assertIsNotNone(AI_SIDECAR_KEY_PATTERN.fullmatch(text))

    def test_controller_init_ensures_key(self):
        inbox = self.dir / "inbox"
        inbox.mkdir()
        before = None
        for _ in range(2):
            Controller(
                self.dir / "state",
                FakeBackend(),
                gate_file=self.dir / "gate.json",
                public_file=self.dir / "public.json",
                admin_dir=self.dir / "admin",
                inbox_dir=inbox,
                workshop_data_dir=self.base,
                releases=catalog(),
            )
            text = self.key_path().read_text()
            self.assertTrue(AI_SIDECAR_KEY_PATTERN.fullmatch(text) is not None)
            if before is None:
                before = text
            else:
                self.assertEqual(text, before, "restart never rotates the key")


if __name__ == "__main__":
    unittest.main()
