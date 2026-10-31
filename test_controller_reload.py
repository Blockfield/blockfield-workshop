"""Hot catalog reload and sleeping release staging (workshop-select).

The long-running controller must see releases.json updates without a
restart; a release change requested while sleeping stays staged until wake.
"""

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from release_fixtures import catalog, release_record

from controller import Controller, Goal
from test_controller import FakeBackend
import releases as release_catalog


def write_catalog(path, records):
    path.write_text(json.dumps(records, sort_keys=True))
    # A reload keys on mtime/size: guarantee the next write looks newer.
    info = path.stat()
    os.utime(path, ns=(info.st_mtime_ns, info.st_mtime_ns + 1_000_000))


class ReloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)
        self.catalog_file = self.dir / "releases.json"
        write_catalog(self.catalog_file, catalog())
        self.backend = FakeBackend()
        self.controller = Controller(
            self.dir / "state",
            self.backend,
            releases=release_catalog.load_catalog(self.catalog_file),
            releases_path=self.catalog_file,
        )
        self.log = io.StringIO()

    def test_new_record_visible_without_restart(self):
        session = str(uuid4())
        goal = Goal("workshop", "v9.0.0", session)
        with self.assertRaisesRegex(ValueError, "not a verified published release"):
            self.controller.request(str(uuid4()), goal, 0)
        records = catalog()
        records["v9.0.0"] = release_record("v9.0.0", "d")
        with contextlib.redirect_stderr(self.log):
            write_catalog(self.catalog_file, records)
            state = self.controller.request(str(uuid4()), goal, 0)
        self.assertEqual(state["approvedTarget"]["release"], "v9.0.0")

    def test_invalid_catalog_keeps_previous_and_logs(self):
        session = str(uuid4())
        before = self.controller.request(
            str(uuid4()), Goal("workshop", "v1.0.0", session), 0
        )
        self.catalog_file.write_text("{invalid json")
        info = self.catalog_file.stat()
        os.utime(self.catalog_file, ns=(info.st_mtime_ns, info.st_mtime_ns + 1_000_000))
        with contextlib.redirect_stderr(self.log):
            state = self.controller.request(
                str(uuid4()), Goal("workshop", "v1.0.0", session), 1
            )
        self.assertEqual(state["approvedTarget"]["release"], "v1.0.0")
        self.assertIn("previous", self.log.getvalue())
        self.assertEqual(before["approvedTarget"], state["approvedTarget"])

    def test_missing_catalog_keeps_previous(self):
        session = str(uuid4())
        self.controller.request(str(uuid4()), Goal("workshop", "v1.0.0", session), 0)
        self.catalog_file.unlink()
        with contextlib.redirect_stderr(self.log):
            state = self.controller.request(
                str(uuid4()), Goal("workshop", "v1.0.0", session), 1
            )
        self.assertEqual(state["approvedTarget"]["release"], "v1.0.0")

    def test_changed_record_under_same_tag_refused(self):
        session = str(uuid4())
        self.controller.request(str(uuid4()), Goal("workshop", "v1.0.0", session), 0)
        records = catalog()
        changed = release_record("v1.0.0", "d")
        changed["revision"] = 2
        records["v1.0.0"] = changed
        write_catalog(self.catalog_file, records)
        with self.assertRaisesRegex(ValueError, "identity changed"):
            with contextlib.redirect_stderr(self.log):
                self.controller.request(
                    str(uuid4()), Goal("workshop", "v1.0.0", session), 1
                )


class SleepingStagingTests(unittest.TestCase):
    """A release change while sleeping stages only; wake activates it."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)
        self.catalog_file = self.dir / "releases.json"
        write_catalog(self.catalog_file, catalog())
        self.backend = FakeBackend()
        self.controller = Controller(
            self.dir / "state",
            self.backend,
            idle_sleep_s=0.0,
            releases=release_catalog.load_catalog(self.catalog_file),
            releases_path=self.catalog_file,
        )
        self.log = io.StringIO()

    def test_sleeping_release_change_stays_staged_until_wake(self):
        session = str(uuid4())
        self.controller.request(str(uuid4()), Goal("workshop", "v1.0.0", session), 0)
        # Force the sleeping phase the same way the timer leaves it.
        with self.controller._locked():
            state = self.controller._read()
            state.update(
                phase="sleeping", gate="closed", revision=state["revision"] + 1
            )
            self.controller._write(state)
        records = catalog()
        records["v2.0.0"] = release_record("v2.0.0", "e")
        write_catalog(self.catalog_file, records)
        with contextlib.redirect_stderr(self.log):
            staged = self.controller.request(
                str(uuid4()), Goal("workshop", "v2.0.0", session), state["revision"]
            )
        self.assertEqual(staged["phase"], "sleeping")
        self.assertTrue(staged["selection"]["deferred"])
        self.assertEqual(staged["approvedTarget"]["release"], "v2.0.0")
        self.assertEqual(staged["desired"]["release"], "v1.0.0")
        woken = self.controller.wake(str(uuid4()))
        self.assertEqual(woken["phase"], "REQUESTED")
        self.assertEqual(self.controller.status()["desired"]["release"], "v2.0.0")


if __name__ == "__main__":
    unittest.main()
