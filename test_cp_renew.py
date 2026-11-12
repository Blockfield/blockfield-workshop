"""Self-renewing CP credentials of the Workshop controller (coordination#33)."""

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from cp_client import CredentialRenewer, renew_at

DAY = 86400
KEY = "BLOCKFIELD_CP_AGENT_TOKEN"
NOW = datetime(2026, 11, 12, tzinfo=timezone.utc).timestamp()


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat()


class FakeCp:
    def __init__(self, issued, expires, state_file, rejected=""):
        self.issued, self.expires = issued, expires
        self.state_file, self.rejected = state_file, rejected
        self.calls, self.state_at_confirm = [], None

    def __call__(self, method, path, token):
        self.calls.append(f"{path} {token}")
        if token == self.rejected:
            return 401, None
        if path == "/v2/me":
            return 200, {"issuedAt": iso(self.issued), "expiresAt": iso(self.expires)}
        if path == "/v2/credentials/renew":
            return 201, {"token": f"next-{token}", "expiresAt": "later"}
        if self.state_file.exists():
            self.state_at_confirm = json.loads(self.state_file.read_text())
        return 200, {"confirmed": True}


class CredentialRenewerTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.state = self.dir / "nested" / "cp_credentials.json"
        self.now = NOW

    def renewer(self, cp, env="env"):
        return CredentialRenewer(
            "https://cp.test", self.state, {KEY: env}, call=cp, now=lambda: self.now
        )

    def test_renew_window_is_seven_days_or_a_quarter_of_a_short_lifetime(self):
        self.assertEqual(renew_at(0, 30 * DAY), 23 * DAY)
        self.assertEqual(renew_at(0, 480), 360)
        cp = FakeCp(NOW - 22 * DAY, NOW + 8 * DAY, self.state)
        r = self.renewer(cp)
        r.tick()
        self.assertEqual(cp.calls, ["/v2/me env"])
        self.assertEqual(r.due[KEY], NOW + 3600)
        self.now += DAY
        r.tick()
        self.assertEqual(r.token(KEY), "next-env")

    def test_persists_the_successor_before_confirming_it(self):
        cp = FakeCp(NOW - 29 * DAY, NOW + DAY, self.state)
        r = self.renewer(cp)
        swapped = []
        r.listeners[KEY] = swapped.append
        r.tick()
        cp.issued, cp.expires = NOW, NOW + 30 * DAY
        r.tick()
        self.assertEqual(
            cp.calls,
            [
                "/v2/me env",
                "/v2/credentials/renew env",
                "/v2/credentials/renew/confirm next-env",
                "/v2/me next-env",
            ],
        )
        self.assertEqual(cp.state_at_confirm[KEY]["token"], "next-env")
        self.assertEqual(os.stat(self.state).st_mode & 0o777, 0o600)
        self.assertEqual(swapped, ["next-env"])

    def test_restart_reads_the_state_file_and_confirms_it(self):
        self.renewer(FakeCp(NOW, NOW, self.state))._persist(KEY, "next-env")
        cp = FakeCp(NOW, NOW + 30 * DAY, self.state)
        r = self.renewer(cp)
        self.assertEqual(r.token(KEY), "next-env")
        r.tick()
        self.assertEqual(
            cp.calls, ["/v2/credentials/renew/confirm next-env", "/v2/me next-env"]
        )

    def test_falls_back_to_env(self):
        cp = FakeCp(NOW, NOW + 30 * DAY, self.state)
        self.assertEqual(self.renewer(cp).token(KEY), "env")
        # A re-issued env token: the old chain's state no longer applies.
        self.renewer(cp, env="old-env")._persist(KEY, "next-old")
        self.assertEqual(self.renewer(cp).token(KEY), "env")
        self.state.write_text("not json")
        self.assertEqual(self.renewer(cp).token(KEY), "env")
        # A persisted token the CP rejects falls back at runtime.
        self.renewer(cp)._persist(KEY, "next-env")
        cp.rejected = "next-env"
        r = self.renewer(cp)
        self.assertEqual(r.token(KEY), "next-env")
        r.tick()
        self.assertEqual(r.token(KEY), "env")

    def test_cp_without_renewal_support_is_checked_hourly(self):
        calls = []
        r = CredentialRenewer(
            "https://cp.test",
            self.state,
            {KEY: "env"},
            call=lambda *a: calls.append(a) or (200, {"client": "workshop"}),
            now=lambda: self.now,
        )
        r.tick()
        self.assertEqual(r.due[KEY], NOW + 3600)
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
