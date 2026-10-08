import unittest
import json
from pathlib import Path
import tempfile
from uuid import uuid4
from controller import Goal, Process
from journal import check, lock_holder, record_gate, record_readiness


class JournalTests(unittest.TestCase):
    def test_lock_holder_matches_inode(self):
        locks = "1: POSIX ADVISORY WRITE 10 00:1f:77 0 EOF\n2: FLOCK ADVISORY WRITE 4242 fd:01:1234 0 EOF\n2: -> FLOCK ADVISORY WRITE 9999 fd:01:1234 0 EOF\n"
        self.assertEqual(lock_holder(locks, 1234), 4242)
        self.assertIsNone(lock_holder(locks, 77))
        self.assertIsNone(
            lock_holder("2: -> FLOCK ADVISORY WRITE 9999 fd:01:1234 0 EOF", 1234)
        )

    def test_violations(self):
        ok = [
            {
                "t": 1,
                "game_java": 1,
                "workshop_java": 0,
                "unknown_java": 0,
                "gate": "public",
                "backend": "game",
            },
            {
                "t": 2,
                "game_java": 1,
                "workshop_java": 0,
                "unknown_java": 0,
                "gate": "draining",
                "backend": "game",
            },
            {
                "t": 3,
                "game_java": 0,
                "workshop_java": 0,
                "unknown_java": 0,
                "gate": "closed",
                "backend": "workshop",
            },
            {
                "t": 4,
                "game_java": 0,
                "workshop_java": 1,
                "unknown_java": 0,
                "gate": "builders",
                "backend": "workshop",
            },
            {
                "t": 4.5,
                "game_java": 0,
                "workshop_java": 1,
                "unknown_java": 0,
                "gate": "testers",
                "backend": "workshop",
            },
        ]
        self.assertEqual(check(ok), [])
        bad = [
            {"t": 6, "game_java": 1, "workshop_java": 1, "gate": "closed"},
            {"t": 7, "workshop_java": 1, "gate": "public", "backend": "game"},
            {"t": 8, "game_java": 1, "gate": "builders", "backend": "workshop"},
            {"t": 9, "unknown_java": 2, "gate": "closed"},
            {"t": 10, "game_java": 1, "gate": "testers", "backend": "workshop"},
            {"t": 11, "game_java": 0, "gate": "testers", "backend": "game"},
            {"t": 12, "error": "docker hiccup", "gate": "closed"},
        ]
        self.assertEqual(len(check(bad)), 7)

    def dual(self):
        return {
            "t": 1,
            "game_java": 1,
            "workshop_java": 1,
            "unknown_java": 0,
            "game_ready": True,
            "workshop_ready": True,
            "game_profile": "game",
            "workshop_profile": "workshop",
            "backends": {
                "game": {"gate": "public"},
                "workshop": {"gate": "builders"},
            },
        }

    def test_one_ready_java_per_backend_is_valid(self):
        record = self.dual()
        self.assertEqual(check([record]), [])
        record["workshop_profile"] = "test"
        record["backends"]["workshop"]["gate"] = "testers"
        self.assertEqual(check([record]), [])
        record["workshop_ready"] = False
        record["backends"]["workshop"]["gate"] = "closed"
        self.assertEqual(check([record]), [], "Workshop maintenance leaves Game public")

    def test_duplicate_unknown_or_incomplete_sample_is_never_accepted(self):
        for change in (
            {"game_java": 2},
            {"workshop_java": 2},
            {"unknown_java": 1},
            {"error": "inventory unavailable"},
            {"lock_error": "unreadable lock inventory"},
            {"locks": {"game": "workshop"}},
            {"game_java": True},
            {"workshop_java": -1},
            {"game_java": None},
        ):
            with self.subTest(change=change):
                self.assertTrue(check([self.dual() | change]))

    def test_open_gate_needs_matching_ready_profile(self):
        for change in (
            {"game_ready": False},
            {"workshop_ready": False},
            {"game_ready": 1},
            {"workshop_java": 0},
            {"workshop_profile": "test"},
            {"game_profile": "workshop"},
        ):
            with self.subTest(change=change):
                self.assertTrue(check([self.dual() | change]))

    def test_invalid_target_gate_never_uses_legacy_public_fields(self):
        for entries in (
            None,
            {},
            {"game": {"gate": "public"}},
            {"game": {"gate": "public"}, "workshop": {"gate": "public"}},
            {"game": {"gate": "builders"}, "workshop": {"gate": "builders"}},
        ):
            record = self.dual() | {
                "gate": "public",
                "backend": "game",
                "backends": entries,
            }
            self.assertTrue(check([record]))

    def test_recorders_keep_exact_profile_readiness_and_only_gate_states(self):
        record = {"t": 1}
        record_readiness(
            record,
            [
                Process(Goal("game", "r1"), True),
                Process(Goal("test", "r1", str(uuid4()), "a" * 64), False),
            ],
        )
        self.assertEqual(record["workshop_profile"], "test")
        self.assertFalse(record["workshop_ready"])
        self.assertTrue(record["game_ready"])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gate.json"
            path.write_text(
                json.dumps(
                    {
                        "gate": "public",
                        "backend": "game",
                        "backends": {
                            "game": {"gate": "public", "allowed": ["private member"]},
                            "workshop": {"gate": "closed", "message": "message"},
                        },
                    }
                )
            )
            record_gate(record, path)
        self.assertEqual(
            record["backends"],
            {"game": {"gate": "public"}, "workshop": {"gate": "closed"}},
        )


if __name__ == "__main__":
    unittest.main()
