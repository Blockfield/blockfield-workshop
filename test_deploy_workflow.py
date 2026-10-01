"""Exercise the actual workflow shell step without contacting Dokploy."""

import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import textwrap
import unittest


class DockerContextTests(unittest.TestCase):
    def test_copy_sources_match_the_restricted_context(self):
        repository = Path(__file__).resolve().parent
        sources = {
            source
            for line in (repository / "Dockerfile").read_text().splitlines()
            if line.startswith("COPY ")
            for source in shlex.split(line)[1:-1]
        }
        allowed = sources | {"Dockerfile"}
        allowed.update(
            f"{Path(source).parent.as_posix()}/"
            for source in sources
            if Path(source).parent != Path(".")
        )
        rules = [
            line.strip()
            for line in (repository / ".dockerignore").read_text().splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        self.assertCountEqual(rules, ["**", *(f"!{path}" for path in allowed)])
        for source in sources:
            with self.subTest(source=source):
                self.assertTrue((repository / source).is_file())


@unittest.skipUnless(
    shutil.which("sh"), "The Linux publisher runs its webhook step in a shell"
)
class DeployWorkflowTests(unittest.TestCase):
    def test_webhook_is_required_and_only_success_records_a_queued_deploy(self):
        workflow = Path(__file__).with_name(".github") / "workflows/publish-image.yml"
        step = workflow.read_text().split(
            "      - name: Queue deployment through Dokploy webhook\n", 1
        )[1]
        script = textwrap.dedent(step.split("        run: |\n", 1)[1])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            curl = root / "curl"
            curl.write_text(
                '#!/bin/sh\nprintf "%s\\n" "$@" > "$TEST_CURL_LOG"\nexit "$TEST_CURL_EXIT"\n'
            )
            curl.chmod(0o755)
            log, summary = root / "curl.log", root / "summary.md"
            env = dict(
                os.environ,
                PATH=str(root) + os.pathsep + os.environ["PATH"],
                GITHUB_STEP_SUMMARY=str(summary),
                TEST_CURL_LOG=str(log),
            )
            for webhook, exit_code, expected in [
                ("", "0", 1),
                ("https://deploy.invalid/hook?x=1&y=2", "22", 22),
                ("https://deploy.invalid/hook?x=1&y=2", "0", 0),
            ]:
                log.unlink(missing_ok=True)
                summary.unlink(missing_ok=True)
                result = subprocess.run(
                    [shutil.which("sh"), "-e", "-c", script],
                    env=dict(env, WEBHOOK=webhook, TEST_CURL_EXIT=exit_code),
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self.assertEqual(result.returncode, expected)
                if not webhook:
                    self.assertFalse(log.exists())
                else:
                    self.assertEqual(log.read_text().splitlines()[-1], webhook)
                    self.assertIn("--max-time\n30\n", log.read_text())
                self.assertEqual(summary.exists(), expected == 0)
                if summary.exists():
                    self.assertIn("queued", summary.read_text())
                    self.assertNotIn(webhook, summary.read_text())
