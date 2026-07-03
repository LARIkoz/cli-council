"""Unit tests for the strict ranking parse + Borda leaderboard.

No network, no CLIs — pure logic. Run: python3 -m pytest tests/  (or unittest).
"""
import unittest

from council import aggregate as A


class TestParse(unittest.TestCase):
    labels = ["Response A", "Response B", "Response C"]

    def _ok(self, text, expected):
        order, reason = A.parse_ranking(text, self.labels)
        self.assertEqual(reason, "ok")
        self.assertEqual(order, expected)

    def _fail(self, text, needle):
        order, reason = A.parse_ranking(text, self.labels)
        self.assertIsNone(order)
        self.assertIn(needle, reason)

    def test_clean(self):
        self._ok("bla\nFINAL RANKING:\n1. Response A\n2. Response B\n3. Response C\n",
                 ["Response A", "Response B", "Response C"])

    def test_bold_and_paren(self):
        self._ok("x\nFINAL RANKING:\n1) **Response C**\n2) Response A\n3) Response B\n",
                 ["Response C", "Response A", "Response B"])

    def test_annotated_lines_accepted(self):
        self._ok("x\nFINAL RANKING:\n1. Response B — best grounding\n2. Response A: ok\n"
                 "3. Response C, thin\n",
                 ["Response B", "Response A", "Response C"])

    def test_trailing_prose_stops(self):
        self._ok("FINAL RANKING:\n1. Response A\n2. Response B\n3. Response C\n\n"
                 "Response A is clearly best overall.\n",
                 ["Response A", "Response B", "Response C"])

    def test_double_letter_rejected(self):
        self._fail("FINAL RANKING:\n1. Response AB\n2. Response B\n3. Response C\n",
                   "not a permutation")

    def test_duplicate_rejected(self):
        self._fail("FINAL RANKING:\n1. Response A\n2. Response A\n3. Response C\n", "duplicate")

    def test_missing_block(self):
        self._fail("I think A is best, then B, then C.", "no FINAL RANKING")

    def test_non_permutation(self):
        self._fail("FINAL RANKING:\n1. Response A\n2. Response B\n", "not a permutation")


class TestBorda(unittest.TestCase):
    def test_math_hand_checked(self):
        l2v = {"Response A": "alice", "Response B": "bob", "Response C": "cara"}
        # two judges, both A>B>C
        orders = {
            "alice": ["Response A", "Response B", "Response C"],
            "bob":   ["Response A", "Response B", "Response C"],
        }
        board = A.leaderboard(orders, l2v)
        by_voice = {r["voice"]: r for r in board.rows}
        # k=3: pos1→2pts, pos2→1, pos3→0; two judges
        self.assertEqual(by_voice["alice"]["borda"], 4)
        self.assertEqual(by_voice["alice"]["mean_rank"], 1.0)
        self.assertEqual(by_voice["cara"]["borda"], 0)
        self.assertEqual(by_voice["cara"]["mean_rank"], 3.0)
        self.assertEqual(board.top, "alice")

    def test_critique_prose_split(self):
        text = "A is strong.\nB is weak.\nFINAL RANKING:\n1. Response A\n2. Response B\n"
        self.assertEqual(A.critique_prose(text), "A is strong.\nB is weak.")


class TestAnonymize(unittest.TestCase):
    def test_labels_stable_and_hidden(self):
        from council.stages import _anonymize
        blocks, l2v = _anonymize({"alice": "answer one text", "bob": "answer two text"})
        self.assertEqual(sorted(l2v), ["Response A", "Response B"])
        self.assertEqual(set(l2v.values()), {"alice", "bob"})
        # voice names must not leak into the anonymized blocks
        self.assertNotIn("alice", blocks)
        self.assertNotIn("bob", blocks)


if __name__ == "__main__":
    unittest.main()
