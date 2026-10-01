"""Required-voices check: every voice selected for a run is required. A voice that
ends stage 1 in opinion_errors (after its whole fallback chain) degrades the run
[infra] — never clean, never a benign 'unverified' — and is reported by name. All
offline: fakes only, no CLIs / network / git."""
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from council import decide, pipeline  # noqa: E402
from council.providers import Provider, reset_dead_cache  # noqa: E402


def _v(name, family="", fallbacks=None):
    return Provider(name=name, transport="cli", bin="x", argv=["x"], family=family,
                    fallbacks=list(fallbacks or []))


class _FakeCouncil(unittest.TestCase):
    """Patches the stage and panel invoke_chain with scripted answers. `dead` voices
    fail at stage 1 with `dead_error`; panels answer `audit_verdict` / HOLDS."""

    def setUp(self):
        reset_dead_cache()
        self.addCleanup(reset_dead_cache)
        self.dead = {}                    # voice -> stage-1 error text
        self.audit_verdict = "CLEAN"

        def stage(name, providers, prompt, timeout=None, log=lambda *_: None):
            if "to rank" in prompt:       # review "Reviews to rank" / decide "Recommendations to rank"
                labels = re.findall(r"### (Response [A-Z])", prompt)
                return True, "FINAL RANKING:\n" + "\n".join(
                    f"{i}. {l}" for i, l in enumerate(labels, 1))
            if "lead reviewer" in prompt:
                return True, "FIX\n\n## BLOCKER\nf.py:1 — bad"
            if "chair of a decision council" in prompt:
                return True, "Adopt Postgres.\n\n## IMPORTANT\nmigrate — scales better."
            if name in self.dead:
                return False, self.dead[name]
            return True, f"SHIP-WITH-EDITS\nanswer by {name}"

        def panel(name, providers, prompt, timeout=None, log=lambda *_: None):
            if "auditor" in prompt:
                return True, f"{self.audit_verdict}\nchecked"
            return True, "HOLDS\nstands"

        for target, fake in (("council.stages.invoke_chain", stage),
                             ("council.panel.invoke_chain", panel)):
            p = mock.patch(target, fake)
            p.start()
            self.addCleanup(p.stop)


class TestRequiredVoicesReview(_FakeCouncil):
    VOICES = ["agy", "grok", "muse", "astra"]

    def _run(self, **kw):
        providers = {v: _v(v) for v in self.VOICES}
        return pipeline.run_review_pipeline("SUBJECT", "t", self.VOICES, "astra", providers,
                                            log=lambda *_: None, **kw)

    def _missing(self, res):
        return [r for r in res.degraded_reasons if r.startswith("required voice ")]

    def test_all_voices_answer_stays_clean(self):
        res = self._run(audit_voices=["agy", "grok", "muse"], redteam_voices=["agy", "muse"])
        self.assertEqual(res.status, "clean")
        self.assertEqual(self._missing(res), [])

    def test_one_missing_voice_degrades_infra_despite_clean_panels(self):
        self.dead = {"grok": "grok: exit 1: HTTP 402 usage balance exhausted"}
        res = self._run(audit_voices=["agy", "muse"], redteam_voices=["agy", "muse"])
        self.assertEqual(res.verification.audit_verdict, "CLEAN")     # panels were happy
        self.assertEqual(res.status, "degraded")
        self.assertEqual(res.degraded_kind, "infra")
        self.assertEqual(self._missing(res), [
            "required voice grok missing (grok: exit 1: HTTP 402 usage balance exhausted) [infra]"])
        self.assertNotIn("grok", res.review.reviewers)
        self.assertIn("grok", res.review.council.opinion_errors)

    def test_missing_voice_without_panels_is_degraded_not_unverified(self):
        self.dead = {"muse": "muse: timeout after 900s"}
        res = self._run()
        self.assertEqual(res.status, "degraded")
        self.assertEqual(res.degraded_kind, "infra")
        self.assertIsNone(res.verification)
        self.assertTrue(any("required voice muse missing" in r for r in res.degraded_reasons))

    def test_missing_voice_plus_audit_finding_is_mixed(self):
        self.dead = {"astra": "astra: empty output"}
        self.audit_verdict = "ISSUES"
        res = self._run(audit_voices=["agy", "grok", "muse"])
        self.assertEqual(res.status, "degraded")
        self.assertEqual(res.degraded_kind, "mixed")
        self.assertTrue(any("required voice astra missing" in r for r in res.degraded_reasons))

    def test_several_missing_voices_reported_sorted(self):
        self.dead = {"muse": "muse: dead", "agy": "agy: dead"}
        res = self._run(audit_voices=["grok"])
        self.assertEqual([r.split()[2] for r in self._missing(res)], ["agy", "muse"])

    def test_long_chain_error_is_cut_and_flattened(self):
        self.dead = {"grok": "grok: " + "x" * 500 + "\n → grok-lara: no route"}
        res = self._run()
        (reason,) = self._missing(res)
        self.assertLess(len(reason), pipeline.MISSING_VOICE_ERROR_CHARS + 60)
        self.assertIn("...) [infra]", reason)
        self.assertNotIn("\n", reason)
        # the full text is still kept where the artifacts read it
        self.assertIn("grok-lara: no route", res.review.council.opinion_errors["grok"])


class TestRequiredVoicesDecide(_FakeCouncil):
    def test_decide_missing_voice_degrades_after_quorum_clears(self):
        providers = {"agy": _v("agy", "google"), "grok": _v("grok", "xai"),
                     "muse": _v("muse", "meta"), "astra": _v("astra", "openai")}
        self.dead = {"grok": "grok: not authenticated"}
        subject, target = decide.build_decide_prompt("Postgres or MySQL?")
        res = pipeline.run_decide_pipeline(subject, target, list(providers), "astra",
                                           providers, audit_voices=["agy", "muse"],
                                           log=lambda *_: None)
        self.assertEqual(res.verification.audit_verdict, "CLEAN")
        self.assertEqual(res.status, "degraded")
        self.assertEqual(res.degraded_kind, "infra")
        self.assertTrue(any("required voice grok missing" in r for r in res.degraded_reasons))


class TestRequiredVoicesWithFallbackChain(unittest.TestCase):
    """The real invoke_chain: a voice whose fallback answered is NOT missing; a voice
    whose whole chain failed IS, with every hop's error in the reason."""

    def setUp(self):
        reset_dead_cache()
        self.addCleanup(reset_dead_cache)
        self.failing = set()

        def fake_invoke(p, prompt, timeout=300):
            if p.name in self.failing:
                return False, f"{p.name}: no route"
            if "to rank" in prompt:
                labels = re.findall(r"### (Response [A-Z])", prompt)
                return True, "FINAL RANKING:\n" + "\n".join(
                    f"{i}. {l}" for i, l in enumerate(labels, 1))
            if "lead reviewer" in prompt:
                return True, "SHIP\n\nno findings"
            if "auditor" in prompt:
                return True, "CLEAN\nok"
            return True, f"SHIP\nreview by {p.name}"

        import council.providers as PV
        p = mock.patch.object(PV, "invoke", side_effect=fake_invoke)
        p.start()
        self.addCleanup(p.stop)
        self.providers = {
            "agy": _v("agy", "google"),
            "muse": _v("muse", "meta"),
            "grok": _v("grok", "xai", fallbacks=["grok-lan", "grok-ts"]),
            "grok-lan": _v("grok-lan", "xai"),
            "grok-ts": _v("grok-ts", "xai"),
        }

    def _run(self):
        return pipeline.run_review_pipeline("SUBJECT", "t", ["agy", "muse", "grok"], "agy",
                                            self.providers, audit_voices=["muse"],
                                            log=lambda *_: None)

    def test_fallback_answer_counts_as_present(self):
        self.failing = {"grok", "grok-lan"}          # grok-ts carries the voice
        res = self._run()
        self.assertEqual(res.status, "clean")
        self.assertIn("grok", res.review.reviewers)

    def test_whole_chain_failed_is_missing_with_every_hop(self):
        self.failing = {"grok", "grok-lan", "grok-ts"}
        res = self._run()
        self.assertEqual(res.status, "degraded")
        (reason,) = [r for r in res.degraded_reasons if r.startswith("required voice ")]
        for hop in ("grok: no route", "grok-lan: no route", "grok-ts: no route"):
            self.assertIn(hop, reason)


if __name__ == "__main__":
    unittest.main()
