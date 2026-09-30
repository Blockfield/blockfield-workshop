"""The isolated Python dispatcher must still resolve a script's adjacent modules."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class ToolchainScript(unittest.TestCase):
    def test_isolated_script_imports_adjacent_modules_and_preserves_arguments(self):
        root = Path(__file__).resolve().parent
        with tempfile.TemporaryDirectory() as temporary:
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
            for entry in (
                [str(root / "scripts/toolchain.py")],
                [str(root / "scripts/quality.py"), "run", "python"],
            ):
                with self.subTest(entry=entry):
                    subprocess.run(
                        [
                            sys.executable,
                            *entry,
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
                    self.assertEqual(
                        json.loads(output.read_text()), [42, ["space & $literal"]]
                    )
