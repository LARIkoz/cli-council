"""Verification layer: panel primitive, worst-wins aggregation rules, multi-voice
audit/redteam, the gate, and the pipeline composition. All offline — fakes only."""
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from council import audit, panel, pipeline, review  # noqa: E402
from council.providers import Provider  # noqa: E402


def _dummy(name):
    return Provider(name=name, transport="cli", bin="x", argv=["x"])


class TestPanelPrimitive(unittest.TestCase):
    def test_every_voice_asked_in_parallel_and_independent(self):
        seen = []

        def fake_invoke(p, prompt, timeout):
            seen.append((p.name, prompt))
            return True, f"CLEAN from {p.name}"

        with mock.patch("council.panel.invoke", fake_invoke):
            res = panel.run_panel("PROMPT", ["a", "b", "c"],
                                  {v: _dummy(v) for v in "abc"},
                                  parse=lambda t: t.split()[0])
        self.assertEqual(len(res.verdicts), 3)
        # independence: every panelist got the SAME prompt (no cross-contamination)
        self.assertEqual({pr for _, pr in seen}, {"PROMPT"})

    def test_errors_recorded_loudly(self):
        def fake_invoke(p, prompt, timeout):
            if p.name == "b":
                return False, "b: timeout after 90s"
            return True, "CLEAN"

        with mock.patch("council.panel.invoke", fake_invoke):
            res = panel.run_panel("P", ["a", "b"], {v: _dummy(v) for v in "ab"},
                                  parse=lambda t: "CLEAN")
        self.assertIn("b", res.errors)
        self.assertEqual(res.verdicts, {"a": "CLEAN"})


class TestWorstWinsAggregation(unittest.TestCase):
    ORDER = audit.AUDIT_ORDER  # (INVALID, ISSUES, CLEAN)

    def test_all_clean(self):
        self.assertEqual(panel.aggregate_verdicts({"a": "CLEAN", "b": "CLEAN"}, self.ORDER), "CLEAN")

    def test_one_issues_beats_majority_clean(self):
        v = {"a": "CLEAN", "b": "CLEAN", "c": "ISSUES"}
        self.assertEqual(panel.aggregate_verdicts(v, self.ORDER), "ISSUES")

    def test_one_invalid_beats_everything(self):
        v = {"a": "CLEAN", "b": "ISSUES", "c": "INVALID"}
        self.assertEqual(panel.aggregate_verdicts(v, self.ORDER), "INVALID")

    def test_parse_error_excluded_not_fatal(self):
        # one auditor's prose didn't parse — its text stays in artifacts, but the
        # vote proceeds on the parseable verdicts.
        v = {"a": "CLEAN", "b": "ERROR"}
        self.assertEqual(panel.aggregate_verdicts(v, self.ORDER), "CLEAN")

    def test_all_unparseable_is_unavailable(self):
        self.assertEqual(panel.aggregate_verdicts({"a": "ERROR", "b": "ERROR"}, self.ORDER),
                         "UNAVAILABLE")

    def test_empty_is_unavailable(self):
        self.assertEqual(panel.aggregate_verdicts({}, self.ORDER), "UNAVAILABLE")

    def test_redteam_order(self):
        v = {"a": "HOLDS", "b": "WEAK"}
        self.assertEqual(panel.aggregate_verdicts(v, audit.REDTEAM_ORDER), "WEAK")
        v["c"] = "REFUTED"
        self.assertEqual(panel.aggregate_verdicts(v, audit.REDTEAM_ORDER), "REFUTED")


class TestMechanicalChecks(unittest.TestCase):
    def test_phantom_files_detected(self):
        synthesis = "Found a bug in `ghost.py:42` — divide by zero."
        subject = "diff for real.py only"
        checks = audit._mechanical_checks(synthesis, subject)
        phantom = next(c for c in checks if c["check"] == "phantom_files")
        self.assertFalse(phantom["pass"])
        self.assertIn("ghost.py", phantom["detail"])

    def test_real_files_pass(self):
        synthesis = "Found a bug in `real.py:10`."
        subject = "diff --git a/real.py b/real.py\n-old\n+new\n real.py | 2 +-\n"
        checks = audit._mechanical_checks(synthesis, subject)
        phantom = next(c for c in checks if c["check"] == "phantom_files")
        self.assertTrue(phantom["pass"])

    def test_severity_headings(self):
        with_h = "FIX\n\n## BLOCKER\nfoo:1 — bad"
        without = "SHIP\n\nLooks good."
        self.assertTrue(next(c for c in audit._mechanical_checks(with_h, "x")
                             if c["check"] == "severity_headings")["pass"])
        self.assertFalse(next(c for c in audit._mechanical_checks(without, "x")
                              if c["check"] == "severity_headings")["pass"])

    def test_phantom_check_skipped_when_no_change(self):
        # decide passes check_files=False — a decision has no diff to compare against,
        # so real files a voice names aren't phantoms; only severity_headings runs.
        synthesis = "Use `council.toml`; see `providers.py`."
        checks = audit._mechanical_checks(synthesis, "a question, no diff", check_files=False)
        names = [c["check"] for c in checks]
        self.assertNotIn("phantom_files", names)
        self.assertIn("severity_headings", names)


class TestVerdictParse(unittest.TestCase):
    def test_audit_verdicts(self):
        for v in audit.AUDIT_ORDER:
            self.assertEqual(audit._parse_first_word(f"{v}\n\nbody", audit.AUDIT_ORDER), v)

    def test_fenced(self):
        self.assertEqual(audit._parse_first_word("```\nINVALID\n```", audit.AUDIT_ORDER), "INVALID")

    def test_prose_is_error(self):
        self.assertEqual(audit._parse_first_word("Some prose...", audit.AUDIT_ORDER), "ERROR")

    def test_heading_then_verdict_regression(self):
        # EXACT shape a live redteam model returned — '## VERDICT' heading, then the
        # verdict on the next line. The old parser read the heading and said ERROR.
        live = "## VERDICT\nREFUTED\n\n## Per-finding breakdown\n### BLOCKER\n..."
        self.assertEqual(audit._parse_first_word(live, audit.REDTEAM_ORDER), "REFUTED")

    def test_label_value_line(self):
        self.assertEqual(audit._parse_first_word("Verdict: INVALID", audit.AUDIT_ORDER), "INVALID")
        self.assertEqual(audit._parse_first_word("**Overall verdict:** HOLDS", audit.REDTEAM_ORDER), "HOLDS")

    def test_still_stops_at_prose_not_mining_body(self):
        # a verdict word buried after real prose is NOT the verdict (must lead).
        self.assertEqual(audit._parse_first_word("Here are my notes.\nHOLDS", audit.REDTEAM_ORDER), "ERROR")


def _panel_invoke(audit_map, redteam_map):
    """Fake invoke routing by prompt kind + voice name."""
    def fake(p, prompt, timeout):
        table = audit_map if "synthesis auditor" in prompt else redteam_map
        out = table.get(p.name)
        if out is None:
            return False, f"{p.name}: no route"
        return True, out
    return fake


class TestRunAuditPanels(unittest.TestCase):
    def setUp(self):
        self.providers = {v: _dummy(v) for v in ("a", "b", "c")}

    def test_clean_pipeline_all_voices(self):
        fake = _panel_invoke({"a": "CLEAN\nok", "b": "CLEAN\nok"},
                             {"a": "HOLDS\nstands", "b": "HOLDS\nstands"})
        with mock.patch("council.panel.invoke", fake):
            res = audit.run_audit("FIX\n## BLOCKER\nx", {"a": "rev"}, "subject",
                                  audit_voices=["a", "b"], redteam_voices=["a", "b"],
                                  providers=self.providers)
        self.assertTrue(res.pipeline_clean)
        self.assertEqual(res.audit_verdict, "CLEAN")
        self.assertEqual(res.redteam_verdict, "HOLDS")
        self.assertEqual(len(res.audit_panel.verdicts), 2)
        self.assertEqual(len(res.redteam_panel.verdicts), 2)

    def test_single_dissent_degrades_audit(self):
        # 2 CLEAN + 1 INVALID → INVALID (worst-wins: one credible catch can't be outvoted)
        fake = _panel_invoke({"a": "CLEAN\n", "b": "CLEAN\n", "c": "INVALID\nhallucinated F"},
                             {"a": "HOLDS\n"})
        with mock.patch("council.panel.invoke", fake):
            res = audit.run_audit("S", {"a": "r"}, "subj",
                                  audit_voices=["a", "b", "c"], redteam_voices=["a"],
                                  providers=self.providers)
        self.assertEqual(res.audit_verdict, "INVALID")
        self.assertFalse(res.pipeline_clean)
        self.assertTrue(any("audit=INVALID" in r for r in res.degraded_reasons))

    def test_weak_redteam_degrades_contract_parity(self):
        # orchestration contract: "REDTEAM says WEAK or REFUTED → do not present as final"
        fake = _panel_invoke({"a": "CLEAN\n"}, {"a": "WEAK\none refuted"})
        with mock.patch("council.panel.invoke", fake):
            res = audit.run_audit("S", {"a": "r"}, "subj",
                                  audit_voices=["a"], redteam_voices=["a"],
                                  providers=self.providers)
        self.assertEqual(res.redteam_verdict, "WEAK")
        self.assertFalse(res.pipeline_clean)
        self.assertTrue(any("redteam=WEAK" in r for r in res.degraded_reasons))

    def test_redteam_skipped_can_still_be_clean(self):
        fake = _panel_invoke({"a": "CLEAN\n"}, {})
        with mock.patch("council.panel.invoke", fake):
            res = audit.run_audit("S", {"a": "r"}, "subj",
                                  audit_voices=["a"], redteam_voices=[],
                                  providers=self.providers)
        self.assertEqual(res.redteam_verdict, "SKIPPED")
        self.assertTrue(res.pipeline_clean)

    def test_audit_skipped_never_clean(self):
        fake = _panel_invoke({}, {"a": "HOLDS\n"})
        with mock.patch("council.panel.invoke", fake):
            res = audit.run_audit("S", {"a": "r"}, "subj",
                                  audit_voices=[], redteam_voices=["a"],
                                  providers=self.providers)
        self.assertEqual(res.audit_verdict, "SKIPPED")
        self.assertFalse(res.pipeline_clean)

    def test_all_auditors_dead_is_unavailable_degraded(self):
        def fake(p, prompt, timeout):
            return False, f"{p.name}: connection failed"
        with mock.patch("council.panel.invoke", fake):
            res = audit.run_audit("S", {"a": "r"}, "subj",
                                  audit_voices=["a", "b"], redteam_voices=[],
                                  providers=self.providers)
        self.assertEqual(res.audit_verdict, "UNAVAILABLE")
        self.assertFalse(res.pipeline_clean)


class TestDegradedKind(unittest.TestCase):
    """A degradation must say WHY: a finding (real problem) vs infra (a checker
    couldn't run). Conflating them is what causes alarm fatigue."""

    def _run(self, amap, rmap, av, rv):
        with mock.patch("council.panel.invoke", _panel_invoke(amap, rmap)):
            return audit.run_audit("S", {"a": "r"}, "subj", audit_voices=av,
                                   redteam_voices=rv, providers={v: _dummy(v) for v in ("a", "b")})

    def test_finding_when_verifier_catches_something(self):
        r = self._run({"a": "INVALID\nhallucinated"}, {}, ["a"], [])
        self.assertEqual(r.degraded_kind, "finding")
        self.assertFalse(r.pipeline_clean)

    def test_infra_when_verifier_cannot_run(self):
        # audit CLEAN, but redteam voice 'b' has no route → dies → UNAVAILABLE
        r = self._run({"a": "CLEAN\n"}, {}, ["a"], ["b"])
        self.assertEqual(r.redteam_verdict, "UNAVAILABLE")
        self.assertEqual(r.degraded_kind, "infra")

    def test_infra_when_audit_skipped(self):
        r = self._run({}, {"a": "HOLDS\n"}, [], ["a"])   # audit mandatory but not configured
        self.assertEqual(r.audit_verdict, "SKIPPED")
        self.assertEqual(r.degraded_kind, "infra")

    def test_mixed_when_finding_and_infra(self):
        r = self._run({"a": "INVALID\n"}, {}, ["a"], ["b"])   # audit finding + redteam dead
        self.assertEqual(r.audit_verdict, "INVALID")
        self.assertEqual(r.redteam_verdict, "UNAVAILABLE")
        self.assertEqual(r.degraded_kind, "mixed")

    def test_clean_has_no_kind(self):
        r = self._run({"a": "CLEAN\n"}, {"a": "HOLDS\n"}, ["a"], ["a"])
        self.assertTrue(r.pipeline_clean)
        self.assertEqual(r.degraded_kind, "")


class TestSafeFilename(unittest.TestCase):
    def test_voice_name_cannot_escape_out_dir(self):
        from council.__main__ import _safe
        self.assertEqual(_safe("claude"), "claude")
        self.assertEqual(_safe("gpt-4.1"), "gpt-4.1")
        self.assertEqual(_safe("a/b"), "a_b")          # slash neutralised
        self.assertEqual(_safe("../etc"), "etc")        # traversal stripped
        self.assertEqual(_safe("..."), "voice")         # degenerate → fallback


class TestPipelineComposition(unittest.TestCase):
    """run_review_pipeline: council → panels → gate, with self-audit warn."""

    def setUp(self):
        self.providers = {v: _dummy(v) for v in ("a", "b")}
        self.logs = []
        self.chairman_fails = False       # flip on to simulate a chairman/synth timeout

        def fake_stage_invoke(p, prompt, timeout):
            if "Reviews to rank" in prompt:
                import re
                labels = re.findall(r"### (Response [A-Z])", prompt)
                return True, "FINAL RANKING:\n" + "\n".join(
                    f"{i}. {l}" for i, l in enumerate(labels, 1))
            if "lead reviewer" in prompt:
                if self.chairman_fails:
                    return False, "timeout after 600s"
                return True, "FIX\n\n## BLOCKER\nf.py:1 — bad"
            return True, f"SHIP-WITH-EDITS\nreview by {p.name}"

        def fake_panel_invoke(p, prompt, timeout):
            if "synthesis auditor" in prompt:
                return True, "CLEAN\nok"
            return True, "HOLDS\nstands"

        self._p1 = mock.patch("council.stages.invoke", fake_stage_invoke)
        self._p2 = mock.patch("council.panel.invoke", fake_panel_invoke)
        self._p1.start(); self._p2.start()
        self.addCleanup(self._p1.stop)
        self.addCleanup(self._p2.stop)

    def _run(self, **kw):
        return pipeline.run_review_pipeline(
            "SUBJECT", "t", ["a", "b"], "a", self.providers,
            log=self.logs.append, **kw)

    def test_full_clean_pipeline(self):
        res = self._run(audit_voices=["a", "b"], redteam_voices=["a", "b"])
        self.assertEqual(res.status, "clean")
        self.assertEqual(res.review.verdict, "FIX")
        self.assertEqual(res.verification.audit_verdict, "CLEAN")

    def test_unverified_without_panels(self):
        res = self._run()
        self.assertEqual(res.status, "unverified")
        self.assertIsNone(res.verification)

    def test_self_audit_warning_logged(self):
        self._run(audit_voices=["a"], redteam_voices=[])  # chairman 'a' audits itself
        self.assertTrue(any("its own audit panel" in m for m in self.logs))

    def test_chairman_failure_degrades_infra(self):
        # #1 (review side of the shared gate) — a synthesis failure never reads clean,
        # even when the audit panel is CLEAN on the raw-voice fallback.
        self.chairman_fails = True
        res = self._run(audit_voices=["a", "b"], redteam_voices=["a", "b"])
        self.assertEqual(res.status, "degraded")
        self.assertEqual(res.degraded_kind, "infra")
        self.assertTrue(any("synthesis failed" in r for r in res.degraded_reasons))


if __name__ == "__main__":
    unittest.main()
