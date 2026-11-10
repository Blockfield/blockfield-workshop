"""Controller-written AI sidecar key (server#321 U-W1)."""

import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

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
        with mock.patch("controller.os.fchown", side_effect=PermissionError):
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

    def test_rejects_symlinked_directory(self):
        """A dir-writer replacing blockfield-workshop with a symlink must not
        redirect the root chown into another directory (r4520435669)."""
        directory = self.base / "blockfield-workshop"
        directory.mkdir(parents=True)
        victim = self.dir / "victim-dir"
        victim.mkdir()
        directory.rmdir()
        directory.symlink_to(victim, target_is_directory=True)
        with self.assertRaises(ValueError):
            ensure_ai_sidecar_key(self.base)
        self.assertEqual(list(victim.iterdir()), [])

    def test_fifo_key_fails_fast(self):
        """A FIFO at the key name must fail closed, never block startup
        (r4520435675): the workshop user can plant one in the key directory."""
        path = self.key_path()
        path.parent.mkdir(parents=True)
        os.mkfifo(path)
        outcome: list = []
        worker = threading.Thread(
            target=lambda: outcome.append(self._ensure_outcome()),
            daemon=True,
        )
        worker.start()
        worker.join(timeout=2)
        self.assertFalse(worker.is_alive(), "key open blocked on a FIFO")
        self.assertIsInstance(outcome[0], ValueError)

    def _ensure_outcome(self):
        try:
            ensure_ai_sidecar_key(self.base)
        except BaseException as error:  # collected, re-raised by the caller
            return error
        return None

    def test_directory_key_fails_closed(self):
        """A directory at the key name is not a key (guard: already rejected
        before this round via the fstat regular-file check)."""
        path = self.key_path()
        path.parent.mkdir(parents=True)
        path.mkdir()
        with self.assertRaises(ValueError):
            ensure_ai_sidecar_key(self.base)

    def test_rejects_oversized_key(self):
        """The read is bounded: a huge regular file is invalid, not loaded."""
        path = self.key_path()
        path.parent.mkdir(parents=True)
        path.write_bytes(b"A" * 70000)
        with self.assertRaises(ValueError):
            ensure_ai_sidecar_key(self.base)

    def test_symlink_swap_during_harden_leaves_victim_alone(self):
        """A dir-writer swapping the directory for a symlink mid-harden must
        not move ownership/mode changes onto the target (r4520336542, r3).

        The directory fd is pinned before any chown, so the swap lands on a
        path nothing else uses; the victim keeps content and 0644.
        """
        path = self.key_path()
        path.parent.mkdir(parents=True)
        path.write_text("operator-managed-secret-0123456789-_AB")
        victim = self.dir / "victim.txt"
        victim.write_text("victim-content-here-0123456789ABCD")
        os.chmod(victim, 0o644)
        real_fchown = os.fchown
        swapped: list = []

        def swapping_fchown(fd, *args):
            if not swapped:
                swapped.append(True)
                directory = path.parent
                directory.rename(self.dir / "stashed-workshop-dir")
                directory.symlink_to(victim)
            return real_fchown(fd, *args)

        with mock.patch("controller.os.fchown", side_effect=swapping_fchown):
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
        real_link = os.link

        def recording_link(source, destination, *args, **kwargs):
            real_link(source, destination, *args, **kwargs)
            if Path(destination).name != AI_SIDECAR_KEY_NAME:
                return
            src_fd = kwargs.get("src_dir_fd")
            if src_fd is not None and not os.path.isabs(source):
                fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=src_fd)
                try:
                    payload = []
                    while True:
                        block = os.read(fd, 65536)
                        if not block:
                            break
                        payload.append(block)
                finally:
                    os.close(fd)
                data = b"".join(payload)
            else:
                data = Path(source).read_bytes()
            with commit_lock:
                committed.append(data)

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
