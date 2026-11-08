"""Vendored toolchain core must match the public workspace origin/main.

scripts/toolchain.py + scripts/tools.lock.json are byte-identical copies of
the workspace core (refresh with workspace scripts/sync_tools.py);
scripts/terminal.py + scripts/dev_cache.py are verbatim copies of its
helpers. This test fetches the reference from the public workspace repo (no
token) and fails on drift in either direction: re-sync, never hand-edit.
"""

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "scripts"))
from toolchain import read_url, retried  # noqa: E402

WORKSPACE_MAIN = (
    "https://raw.githubusercontent.com/Blockfield/blockfield-workspace/main"
)
# Sync-managed core: refresh with `python scripts/sync_tools.py <checkout>`
# from the workspace. Helpers: copy verbatim from the workspace revision.
VENDORED = ("scripts/toolchain.py", "scripts/tools.lock.json")
HELPERS = ("scripts/terminal.py", "scripts/dev_cache.py")


class VendoredCore(unittest.TestCase):
    def fetch_reference(self, relative):
        url = WORKSPACE_MAIN + "/" + relative
        return retried(lambda: read_url(url))

    def test_vendored_files_equal_workspace_origin_main(self):
        for relative in (*VENDORED, *HELPERS):
            with self.subTest(relative):
                local = (ROOT / relative).read_bytes()
                try:
                    reference = self.fetch_reference(relative)
                except OSError as error:
                    self.fail(
                        f"{relative}: cannot fetch workspace origin/main: {error}"
                    )
                fix = (
                    "re-run workspace scripts/sync_tools.py"
                    if relative in VENDORED
                    else "copy verbatim from workspace origin/main"
                )
                self.assertEqual(local, reference, f"{relative} drifted: {fix}")

    def test_lock_pins_the_tools_quality_uses(self):
        # No network: the lock must pin every tool scripts/quality.py resolves
        # through the core, or `just lint` downloads unpinned bytes.
        lock = json.loads((ROOT / "scripts/tools.lock.json").read_text())
        self.assertEqual(lock["version"], 1)
        for name in (
            "ruff",
            "shellcheck",
            "shfmt",
            "actionlint",
            "prettier",
            "pnpm",
            "just",
            "ktlint",
            "gjf",
            "asm",
            "asm-tree",
        ):
            self.assertIn(name, lock["tools"], f"{name} not pinned")


if __name__ == "__main__":
    unittest.main()
