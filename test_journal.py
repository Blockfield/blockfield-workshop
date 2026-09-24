import unittest
from journal import check, lock_holder


class JournalTests(unittest.TestCase):
    def test_lock_holder_matches_inode(self):
        locks = "1: POSIX ADVISORY WRITE 10 00:1f:77 0 EOF\n2: FLOCK ADVISORY WRITE 4242 fd:01:1234 0 EOF\n2: -> FLOCK ADVISORY WRITE 9999 fd:01:1234 0 EOF\n"
        self.assertEqual(lock_holder(locks, 1234), 4242)
        self.assertIsNone(lock_holder(locks, 77))
        self.assertIsNone(lock_holder("2: -> FLOCK ADVISORY WRITE 9999 fd:01:1234 0 EOF", 1234))

    def test_violations(self):
        ok = [{"t": 1, "game_java": 1, "workshop_java": 0, "unknown_java": 0, "gate": "public", "backend": "game"},
              {"t": 2, "game_java": 1, "workshop_java": 0, "unknown_java": 0, "gate": "draining", "backend": "game"},
              {"t": 3, "game_java": 0, "workshop_java": 0, "unknown_java": 0, "gate": "closed", "backend": "workshop"},
              {"t": 4, "game_java": 0, "workshop_java": 1, "unknown_java": 0, "gate": "builders", "backend": "workshop"},
              {"t": 4.5, "game_java": 0, "workshop_java": 1, "unknown_java": 0, "gate": "testers", "backend": "workshop"},
              {"t": 5, "error": "docker hiccup", "gate": "closed"}]
        self.assertEqual(check(ok), [])
        bad = [{"t": 6, "game_java": 1, "workshop_java": 1, "gate": "closed"},
               {"t": 7, "workshop_java": 1, "gate": "public", "backend": "game"},
               {"t": 8, "game_java": 1, "gate": "builders", "backend": "workshop"},
               {"t": 9, "unknown_java": 2, "gate": "closed"},
               {"t": 10, "game_java": 1, "gate": "testers", "backend": "workshop"},
               {"t": 11, "game_java": 0, "gate": "testers", "backend": "game"}]
        self.assertEqual(len(check(bad)), 6)


if __name__ == "__main__":
    unittest.main()
