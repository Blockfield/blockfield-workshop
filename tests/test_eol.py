"""format-check must fail while the Git index holds CRLF."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import quality


class Eol(unittest.TestCase):
    def test_clean_index_passes(self):
        quality.eol()

    def test_index_crlf_reported(self):
        real = quality.run
        quality.run = lambda *args, **kwargs: (
            "i/crlf\tw/crlf\tattr/text=auto eol=lf\tconfig/x.toml\n"
            "i/lf\tw/lf\tattr/text=auto eol=lf\tok.py\n"
        )
        try:
            with self.assertRaises(SystemExit) as ctx:
                quality.eol()
        finally:
            quality.run = real
        self.assertIn("config/x.toml", str(ctx.exception))
        self.assertNotIn("ok.py", str(ctx.exception))

    def test_notext_crlf_passes(self):
        real = quality.run
        quality.run = lambda *args, **kwargs: (
            "i/crlf\tw/crlf\tattr/-text\tgradlew.bat\n"
            "i/lf\tw/lf\tattr/text=auto eol=lf\tok.py\n"
        )
        try:
            quality.eol()
        finally:
            quality.run = real

    def test_bat_without_notext_reported(self):
        real = quality.run
        quality.run = lambda *args, **kwargs: (
            "i/lf\tw/lf\tattr/text=auto eol=lf\tgradlew.bat\n"
        )
        try:
            with self.assertRaises(SystemExit) as ctx:
                quality.eol()
        finally:
            quality.run = real
        self.assertIn("gradlew.bat", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
