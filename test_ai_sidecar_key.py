"""Controller-written AI sidecar key (server#321 U-W1)."""

import os
import stat
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
        # Production pins the key owner to uid 1000; tests run as whoever.
        owner = mock.patch("controller.AI_KEY_UID", os.getuid())
        owner.start()
        self.addCleanup(owner.stop)

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

    def test_never_changes_ownership(self):
        """No privileged repair anywhere in the AI key code: any chown/fchown
        attempt fails the test. Create and adopt run caller-owned, 0600."""

        def no_privileged_repair(*args, **kwargs):
            raise AssertionError("controller must not change ownership")

        with (
            mock.patch("os.chown", side_effect=no_privileged_repair),
            mock.patch("os.fchown", side_effect=no_privileged_repair),
        ):
            first = ensure_ai_sidecar_key(self.base)
            self.assertTrue(AI_SIDECAR_KEY_PATTERN.fullmatch(first.read_text()))
            self.assertEqual(first.stat().st_mode & 0o777, 0o600)
            second = ensure_ai_sidecar_key(self.base)
            self.assertEqual(second.read_text(), first.read_text())

    def test_unprivileged_run_keeps_caller_owned_directory(self):
        directory = self.base / "blockfield-workshop"
        directory.mkdir(parents=True)
        with mock.patch("controller.os.geteuid", return_value=os.getuid()):
            ensure_ai_sidecar_key(self.base)  # must not raise for the caller-owned dir

    def test_other_user_touches_nothing_and_is_not_blocked(self):
        """r4521776267: a controller built by a user who is not the key owner
        (a root operator running one command) must not fail on the key and
        must not perform any file operation in the key directory."""
        directory = self.base / "blockfield-workshop"
        directory.mkdir(parents=True)

        def forbidden(*args, **kwargs):
            raise AssertionError("no file operation for a non-owner")

        for euid in (0, os.getuid() + 1):
            with (
                mock.patch("controller.os.geteuid", return_value=euid),
                mock.patch("controller._open_pinned_ai_key_dir", side_effect=forbidden),
            ):
                path = ensure_ai_sidecar_key(self.base)
            self.assertEqual(path, self.key_path())
        self.assertEqual(list(directory.iterdir()), [])

    def test_key_is_only_created_by_its_owner_uid(self):
        """r4521776261: a non-root controller whose uid is not the readers'
        uid used to create a 0600 key the sidecar cannot open."""
        with mock.patch("controller.AI_KEY_UID", os.getuid() + 1):
            ensure_ai_sidecar_key(self.base)
        self.assertFalse(self.key_path().exists())

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

        The directory fd is pinned before any harden, so the swap lands on a
        path nothing else uses; the victim keeps content and 0644. The swap
        now fires inside os.fchmod (the only fd-based harden left: no chown
        exists anymore to hook).
        """
        path = self.key_path()
        path.parent.mkdir(parents=True)
        path.write_text("operator-managed-secret-0123456789-_AB")
        victim = self.dir / "victim.txt"
        victim.write_text("victim-content-here-0123456789ABCD")
        os.chmod(victim, 0o644)
        real_fchmod = os.fchmod
        swapped: list = []

        def swapping_fchmod(fd, *args, **kwargs):
            if not swapped:
                swapped.append(True)
                directory = path.parent
                directory.rename(self.dir / "stashed-workshop-dir")
                directory.symlink_to(victim)
            return real_fchmod(fd, *args, **kwargs)

        with mock.patch("controller.os.fchmod", side_effect=swapping_fchmod):
            ensure_ai_sidecar_key(self.base)
        self.assertTrue(swapped)
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
            # The winner is linked by descriptor (/proc/self/fd/N), still
            # open at link time, so the payload is read straight through it.
            data = Path(str(source)).read_bytes()
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

    def test_foreign_owned_directory_fails_closed_without_chown(self):
        """A dir owned by another uid is refused WITHOUT any chown attempt
        (r4521621942): the old code fchown-ed first and checked after. The
        fake fstat seam simulates foreign ownership; no real root needed."""
        directory = self.base / "blockfield-workshop"
        directory.mkdir(parents=True)
        real_fstat = os.fstat

        class FakeStat:
            def __init__(self, values):
                self.st_mode, self.st_uid, self.st_gid, self.st_ino = values

        def patched(fd, *args, **kwargs):
            info = real_fstat(fd, *args, **kwargs)
            try:
                resolved = os.readlink(f"/proc/self/fd/{fd}")
            except OSError:
                return info
            if Path(resolved) == directory:
                return FakeStat((info.st_mode, 4242, 4242, info.st_ino))
            return info

        def no_privileged_repair(*args, **kwargs):
            raise AssertionError("controller must not change ownership")

        with (
            mock.patch("os.fstat", side_effect=patched),
            mock.patch("os.chown", side_effect=no_privileged_repair),
            mock.patch("os.fchown", side_effect=no_privileged_repair),
        ):
            with self.assertRaises(ValueError) as caught:
                ensure_ai_sidecar_key(self.base)
        message = str(caught.exception)
        self.assertIn("unsafe ai-sidecar-key directory", message)
        self.assertIn("4242", message)  # found owner is reported
        self.assertIn(str(self.base / "blockfield-workshop"), message)

    def test_directory_mode_is_never_changed(self):
        """r4521707966: the dir's mode was fchmod-ed before the ownership
        check, also when another process had created it. No chmod at all."""
        real_fchmod = os.fchmod

        def guarded(fd, mode):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise AssertionError("controller must not chmod the key directory")
            return real_fchmod(fd, mode)

        with mock.patch("os.fchmod", side_effect=guarded):
            ensure_ai_sidecar_key(self.base)

    def test_group_writable_directory_fails_closed(self):
        """Group/other write on the key dir is refused even when owned by us:
        a writable dir lets another local user swap the key under us."""
        directory = self.base / "blockfield-workshop"
        directory.mkdir(parents=True)
        os.chmod(directory, 0o775)
        with self.assertRaises(ValueError) as caught:
            ensure_ai_sidecar_key(self.base)
        self.assertIn("unsafe ai-sidecar-key directory", str(caught.exception))

    def test_create_publishes_no_named_tmp(self):
        """The secret is staged unnamed (O_TMPFILE) and linked by descriptor:
        at link time the directory holds no attacker-swappable tmp name
        (r4521621949). The hook lists the pinned dir from inside os.link."""
        (self.base / "blockfield-workshop").mkdir(parents=True)
        real_link = os.link
        observations: list = []

        def observing_link(source, destination, *args, **kwargs):
            observations.append((source, destination, kwargs.get("dst_dir_fd")))
            dst_fd = kwargs.get("dst_dir_fd")
            entries = os.listdir(dst_fd) if dst_fd is not None else []
            self.assertEqual(entries, [], f"named tmp visible: {entries}")
            self.assertTrue(
                str(source).startswith("/proc/self/fd/"),
                f"key staged by name, not descriptor: {source}",
            )
            return real_link(source, destination, *args, **kwargs)

        with mock.patch("controller.os.link", side_effect=observing_link):
            ensure_ai_sidecar_key(self.base)
        self.assertTrue(observations)
        self.assertIsNotNone(
            AI_SIDECAR_KEY_PATTERN.fullmatch(self.key_path().read_text())
        )

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


class ProviderKeyAsOwnerTests(unittest.TestCase):
    """r4521877438: a root controller applies provider keys as the key owner."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name) / "workshop-data"
        (self.base / "blockfield-workshop").mkdir(parents=True)
        owner = mock.patch("controller.AI_KEY_UID", os.getuid())
        owner.start()
        self.addCleanup(owner.stop)

    def run_as_owner(self, key):
        import subprocess

        real_run = subprocess.run
        seen = {}

        def drop_check(args, **kwargs):
            seen["kwargs"] = dict(kwargs)
            seen["args"] = list(args)
            # A test cannot setuid; the child must agree on the owner uid.
            script = args[3].replace(
                "import controller\n",
                "import controller, os\ncontroller.AI_KEY_UID = os.geteuid()\n",
            )
            for name in ("user", "group", "extra_groups"):
                kwargs.pop(name)
            return real_run([*args[:3], script, *args[4:]], **kwargs)

        with (
            mock.patch("controller.os.geteuid", return_value=0),
            mock.patch("subprocess.run", side_effect=drop_check),
        ):
            stored = controller._write_ai_key_as_owner(
                self.base, "openrouter.key", "ai-key", key
            )
        return stored, seen

    def test_root_writes_and_removes_through_owner_child(self):
        key = "sk-or-" + "a" * 40
        stored, seen = self.run_as_owner(key)
        self.assertEqual(seen["kwargs"]["user"], os.getuid())
        self.assertEqual(seen["kwargs"]["extra_groups"], [])
        self.assertNotIn(key, " ".join(seen["args"]))  # key only on stdin
        self.assertEqual(stored["last4"], key[-4:])
        path = self.base / "blockfield-workshop" / "openrouter.key"
        self.assertEqual(path.read_text(), key)
        stored, _ = self.run_as_owner(None)
        self.assertFalse(stored["set"])
        self.assertFalse(path.exists())

    def test_child_failure_is_a_value_error_without_the_key(self):
        os.chmod(self.base / "blockfield-workshop", 0o777)
        key = "sk-or-" + "b" * 40
        with self.assertRaises(ValueError) as caught:
            self.run_as_owner(key)
        self.assertIn("unsafe ai-key directory", str(caught.exception))
        self.assertNotIn(key, str(caught.exception))


if __name__ == "__main__":
    unittest.main()
