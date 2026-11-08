"""The isolated Python dispatcher must still resolve a script's adjacent modules."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "scripts"))
from toolchain import java_home  # noqa: E402


class ToolchainScript(unittest.TestCase):
    def test_isolated_script_imports_adjacent_modules_and_preserves_arguments(self):
        # Dispatch is `quality.py run python` (the shared toolchain core has
        # no script wrapper; `toolchain.py <script>` no longer puts the
        # script's directory on sys.path).
        repository = Path(__file__).resolve().parent
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "installation without git"
            (root / "scripts").mkdir(parents=True)
            for name in (
                "toolchain.py",
                "toolchain_local.py",
                "tools.lock.json",
                "terminal.py",
                "dev_cache.py",
                "quality.py",
            ):
                shutil.copyfile(repository / "scripts" / name, root / "scripts" / name)
            directory = Path(temporary) / "script with spaces"
            directory.mkdir()
            (directory / "adjacent.py").write_text("VALUE = 42\n")
            script = directory / "entry.py"
            script.write_text(
                "import adjacent,json,os,sys\n"
                "from pathlib import Path\n"
                "assert sys.flags.isolated\n"
                "assert 'PYTHONPATH' not in os.environ\n"
                "Path(sys.argv[1]).write_text(json.dumps([adjacent.VALUE,sys.argv[2:]]))\n"
            )
            output = directory / "result.json"
            env = os.environ.copy()
            env["PYTHONPATH"] = str(directory / "untrusted")
            subprocess.run(
                [
                    sys.executable,
                    str(root / "scripts/quality.py"),
                    "run",
                    "python",
                    str(script),
                    str(output),
                    "space & $literal",
                ],
                cwd=temporary,
                env=env,
                check=True,
                timeout=30,
                capture_output=True,
            )
            self.assertEqual(json.loads(output.read_text()), [42, ["space & $literal"]])


class JavaHome(unittest.TestCase):
    def test_shim_ahead_of_jdk_picks_jdk(self):
        with tempfile.TemporaryDirectory() as tmp:
            shim = Path(tmp) / "shim"
            shim.mkdir()
            fake = shim / "java"
            fake.write_text("#!/bin/sh\nexit 3\n")
            fake.chmod(0o755)
            home = Path(tmp) / "jdk"
            (home / "bin").mkdir(parents=True)
            real = home / "bin" / "java"
            real.write_text("#!/bin/sh\nexit 0\n")
            real.chmod(0o755)
            env = os.environ.copy()
            env["PATH"] = os.pathsep.join((str(shim), str(home / "bin")))
            env.pop("JAVA_HOME", None)
            with unittest.mock.patch.dict(os.environ, env, clear=True):
                self.assertEqual(java_home(), real.resolve().parents[1])
