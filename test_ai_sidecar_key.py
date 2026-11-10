"""Controller-written AI sidecar key (server#321 U-W1)."""

import os
import tempfile
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
