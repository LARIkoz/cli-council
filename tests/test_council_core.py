"""Regression lock on the Karpathy llm-council core: the three stages, and the
one invariant that makes peer-ranking meaningful — a ranker never sees which
voice produced which answer. All the mode/parallel/audit work must NOT break this.
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from council import stages  # noqa: E402
from council.providers import Provider  # noqa: E402


def _dummy(name):
    return Provider(name=name, transport="cli", bin="x", argv=["x"])


class TestKarpathyCoreInvariants(unittest.TestCase):
    """Three distinctly-named voices; capture every prompt each one receives."""

    def setUp(self):
        self.captured = []   # (voice, prompt)
        self.voices = ["alice", "bob", "carol"]
        self.providers = {v: _dummy(v) for v in self.voices}

        # Distinct answers that do NOT embed the voice name — so the invariant
        # test checks the STRUCTURAL anonymization, not a voice self-identifying
        # inside its own prose (which anonymization neither can nor should hide).
        answers = {"alice": "The result is 42.",
                   "bob": "I make it forty-two.",
                   "carol": "Two dozen and eighteen."}

        def fake_invoke(p, prompt, timeout):
            self.captured.append((p.name, prompt))
            if "FINAL RANKING" in prompt and "Answers to rank" in prompt:   # stage 2
                import re
                labels = re.findall(r"### (Response [A-Z])", prompt)
                return True, "notes\nFINAL RANKING:\n" + "\n".join(
                    f"{i}. {l}" for i, l in enumerate(labels, 1))
            if "chairman of a council" in prompt:                            # stage 3
                return True, "SYNTHESIZED FINAL ANSWER"
            return True, answers[p.name]                                     # stage 1

        self._patch = mock.patch.object(stages, "invoke", fake_invoke)
        self._patch.start()
        self.addCleanup(self._patch.stop)
        self.res = stages.run_council("What is X?", self.voices, "alice", self.providers)

    def test_all_three_stages_ran(self):
        # 3 opinions + 3 rankings + 1 chairman = 7 invoke calls
        self.assertEqual(len(self.captured), 7)
        self.assertEqual(len(self.res.opinions), 3)
        self.assertTrue(self.res.board and self.res.board.rows)   # ranking produced a board
        self.assertEqual(self.res.final, "SYNTHESIZED FINAL ANSWER")

    def test_stage1_voices_get_the_raw_question(self):
        stage1 = [pr for (_v, pr) in self.captured if pr == "What is X?"]
        self.assertEqual(len(stage1), 3)   # each voice answered the question verbatim

    def test_ranking_prompt_hides_voice_identity(self):
        # THE core invariant: no ranker's prompt may contain any voice name or the
        # label→voice map — only anonymized "Response A/B/C".
        rank_prompts = [pr for (_v, pr) in self.captured
                        if "FINAL RANKING" in pr and "Answers to rank" in pr]
        self.assertEqual(len(rank_prompts), 3)
        for pr in rank_prompts:
            for name in self.voices:
                self.assertNotIn(name, pr, f"voice name '{name}' leaked into a ranking prompt")
            self.assertIn("Response A", pr)   # anonymized labels ARE present

    def test_anonymization_is_deterministic_despite_parallelism(self):
        # labels are assigned by content hash, not arrival order, so a parallel
        # council still maps the same voice → same label every run.
        blocks, l2v = stages._anonymize(self.res.opinions)
        blocks2, l2v2 = stages._anonymize(self.res.opinions)
        self.assertEqual(l2v, l2v2)

    def test_chairman_sees_attributed_answers(self):
        # Karpathy's design: rankers are blind, but the chairman DOES see who said
        # what (attributed), plus the leaderboard.
        chair_prompts = [pr for (_v, pr) in self.captured if "chairman of a council" in pr]
        self.assertEqual(len(chair_prompts), 1)
        cp = chair_prompts[0]
        for name in self.voices:
            self.assertIn(f"[{name}]", cp)   # attributed blocks present for the chairman


class TestAggregateUntouched(unittest.TestCase):
    def test_borda_module_still_present(self):
        # the ranking math (Borda leaderboard) is imported and used unchanged.
        from council import aggregate
        board = aggregate.leaderboard(
            orders={"a": ["Response A", "Response B"], "b": ["Response B", "Response A"]},
            label_to_voice={"Response A": "x", "Response B": "y"},
            failed=[],
        )
        self.assertTrue(board.rows)
        self.assertEqual({r["voice"] for r in board.rows}, {"x", "y"})


if __name__ == "__main__":
    unittest.main()
