"""Decide mode: mode wiring, prompt building, the family quorum (NFR5), the full
decide council + pipeline on a faked council, the decision-audit, and config —
all offline (fakes only, no CLIs / network / git). Covers AC1/2/3/5/7/9."""
import re
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from council import audit, config, decide, pipeline, review, stages  # noqa: E402
from council.providers import Provider, family_of  # noqa: E402


def _v(name, family=""):
    return Provider(name=name, transport="cli", bin="x", argv=["x"], family=family)


class TestModeWiring(unittest.TestCase):
    def test_decide_mode_is_distinct(self):
        self.assertEqual(decide.DECIDE.name, "decide")
        self.assertIn("decision council", decide.DECIDE.rank_prompt)
        self.assertIn("chair of a decision council", decide.DECIDE.chairman_prompt)
        # NOT the ask or review templates
        self.assertNotEqual(decide.DECIDE.rank_prompt, stages.ASK.rank_prompt)
        self.assertNotEqual(decide.DECIDE.chairman_prompt, review.REVIEW.chairman_prompt)

    def test_chairman_prompt_carries_action_tiers(self):
        # FR4 — the synthesis must group into the owner's tiers.
        for tier in ("## BLOCKER", "## IMPORTANT", "## CHECK", "## ACCEPT", "## NOISE"):
            self.assertIn(tier, decide.DECIDE.chairman_prompt)


class TestBuildDecidePrompt(unittest.TestCase):
    def test_wraps_question_in_contract(self):
        subject, target = decide.build_decide_prompt("Postgres or MySQL for this?")
        self.assertIn("Postgres or MySQL for this?", subject)
        self.assertIn("decision council", subject)     # the contract framing
        self.assertIn("## Decision", subject)
        self.assertTrue(target.startswith("decide:"))

    def test_long_question_label_truncated(self):
        q = "should we " + "x" * 200
        _, target = decide.build_decide_prompt(q)
        self.assertTrue(target.endswith("…"))
        self.assertLessEqual(len(target), len("decide:") + 61)

    def test_empty_question_raises(self):
        with self.assertRaises(ValueError):
            decide.build_decide_prompt("   \n  ")


class TestFamilyQuorum(unittest.TestCase):
    """NFR5 — a decision needs ≥3 model families among the voices that answered.
    Two voices of one house count once (family_of)."""

    def setUp(self):
        self.providers = {
            "opus": _v("opus", "anthropic"),
            "sonnet": _v("sonnet", "anthropic"),
            "codex": _v("codex", "openai"),
            "grok": _v("grok", "xai"),
        }

    def test_three_families_ok(self):
        answered = {"opus": "a", "codex": "b", "grok": "c"}      # 3 families
        self.assertIsNone(decide.family_quorum_error(answered, self.providers))

    def test_two_families_aborts_even_with_three_voices(self):
        # opus + sonnet + codex = anthropic + openai = only 2 families → abort.
        answered = {"opus": "a", "sonnet": "b", "codex": "c"}
        err = decide.family_quorum_error(answered, self.providers)
        self.assertIsNotNone(err)
        self.assertIn("family quorum not met", err)
        self.assertIn("2 model families", err)

    def test_unlabeled_voice_is_its_own_family(self):
        provs = {"a": _v("a"), "b": _v("b"), "c": _v("c")}      # no family → name
        self.assertEqual(family_of(provs["a"]), "a")
        self.assertIsNone(decide.family_quorum_error({"a": "1", "b": "2", "c": "3"}, provs))


class TestRunDecide(unittest.TestCase):
    """Full decide council over a faked, family-diverse roster."""

    def setUp(self):
        def fake_invoke(p, prompt, timeout):
            if "Recommendations to rank" in prompt:                  # stage 2
                labels = re.findall(r"### (Response [A-Z])", prompt)
                bullets = "\n".join(f"- {l}: grounded" for l in labels)
                ranking = "\n".join(f"{i}. {l}" for i, l in enumerate(labels, 1))
                return True, f"{bullets}\nFINAL RANKING:\n{ranking}"
            if "chair of a decision council" in prompt:              # stage 3
                return True, ("Adopt Postgres.\n\n## IMPORTANT\n"
                              "plan the migration — because the write path scales.\n")
            return True, f"Recommend Postgres\nreasoning from {p.name}"  # stage 1

        self._patch = mock.patch.object(stages, "invoke", fake_invoke)
        self._patch.start()
        self.addCleanup(self._patch.stop)
        self.providers = {"opus": _v("opus", "anthropic"),
                          "codex": _v("codex", "openai"),
                          "agy": _v("agy", "google")}

    def test_decide_run_end_to_end(self):
        subject, target = decide.build_decide_prompt("Postgres or MySQL?")
        res = decide.run_decide(subject, target, voices=["opus", "codex", "agy"],
                                chairman="opus", providers=self.providers)
        self.assertEqual(res.verdict, "DECISION")
        self.assertIn("Adopt Postgres", res.review)
        self.assertIn("## IMPORTANT", res.review)                    # action tier present
        self.assertEqual(sorted(res.reviewers), ["agy", "codex", "opus"])
        self.assertIsNotNone(res.council.board)

    def test_decide_aborts_below_family_quorum(self):
        # only 2 families enrolled → abort BEFORE synthesis (AC7).
        two_fam = {"opus": _v("opus", "anthropic"), "sonnet": _v("sonnet", "anthropic"),
                   "codex": _v("codex", "openai")}
        subject, target = decide.build_decide_prompt("go or no-go?")
        with self.assertRaises(RuntimeError) as e:
            decide.run_decide(subject, target, voices=["opus", "sonnet", "codex"],
                              chairman="opus", providers=two_fam)
        self.assertIn("family quorum not met", str(e.exception))


class TestDecidePipeline(unittest.TestCase):
    """run_decide_pipeline: decide council → DECISION audit panel → gate."""

    def setUp(self):
        self.providers = {"opus": _v("opus", "anthropic"), "codex": _v("codex", "openai"),
                          "agy": _v("agy", "google")}
        self.audit_seen = []
        self.chairman_fails = False       # flip on to simulate a chairman/synth timeout
        self.chairman_empty = False       # flip on for a rc=0-but-empty synthesis
        self.audit_dies = False           # flip on to kill the whole audit panel
        self.audit_dead_voices = set()    # kill only these audit panelists (partial death)
        self.audit_garbles = set()        # these return LIVE but unparseable output (ERROR)
        self.rankers_fail = False         # flip on to make ALL peer rankings unparseable

        def fake_stage_invoke(p, prompt, timeout):
            if "Recommendations to rank" in prompt:
                if self.rankers_fail:
                    return True, "I have thoughts but no FINAL RANKING block."   # unparseable
                labels = re.findall(r"### (Response [A-Z])", prompt)
                return True, "FINAL RANKING:\n" + "\n".join(
                    f"{i}. {l}" for i, l in enumerate(labels, 1))
            if "chair of a decision council" in prompt:
                if self.chairman_fails:
                    return False, "timeout after 600s"
                if self.chairman_empty:
                    return True, "   "
                return True, "Adopt Postgres.\n\n## IMPORTANT\nmigrate — scales better."
            return True, f"Recommend Postgres\nby {p.name}"

        self.audit_verdict = "CLEAN"

        def fake_panel_invoke(p, prompt, timeout):
            self.audit_seen.append(prompt)
            if self.audit_dies or p.name in self.audit_dead_voices:
                return False, f"{p.name}: auditor process died"
            if p.name in self.audit_garbles:
                return True, "Some rambling analysis with no leading verdict word at all."
            return True, f"{self.audit_verdict}\nchecked"

        self._p1 = mock.patch("council.stages.invoke", fake_stage_invoke)
        self._p2 = mock.patch("council.panel.invoke", fake_panel_invoke)
        self._p1.start(); self._p2.start()
        self.addCleanup(self._p1.stop)
        self.addCleanup(self._p2.stop)

    def _run(self, **kw):
        subject, target = decide.build_decide_prompt("Postgres or MySQL?")
        return pipeline.run_decide_pipeline(
            subject, target, ["opus", "codex", "agy"], "opus", self.providers,
            log=lambda *_: None, **kw)

    def test_clean_decide_with_audit(self):
        # AC1/AC5 — recommendation + tiers, audit CLEAN, verification present.
        res = self._run(audit_voices=["codex", "agy"])
        self.assertEqual(res.status, "clean")
        self.assertIn("Adopt Postgres", res.review.review)
        self.assertEqual(res.verification.audit_verdict, "CLEAN")
        self.assertEqual(res.verification.redteam_verdict, "SKIPPED")   # redteam off

    def test_decision_audit_prompt_was_used_not_review(self):
        # FR2 wiring — the DECISION audit prompt (not the code-review AUDIT_PROMPT)
        # reached the panel.
        self._run(audit_voices=["codex"])
        joined = "\n".join(self.audit_seen)
        self.assertIn("decision council", joined)          # decision-audit wording
        self.assertIn("Raw advisor answers", joined)
        self.assertNotIn("change under review", joined)     # NOT the review audit prompt

    def test_planted_convergence_flagged_issues(self):
        # AC2 (plumbing) — an auditor returning ISSUES degrades the decide pipeline
        # and surfaces AUDIT_VERDICT=ISSUES (never a clean pass).
        self.audit_verdict = "ISSUES"
        res = self._run(audit_voices=["codex", "agy"])
        self.assertEqual(res.verification.audit_verdict, "ISSUES")
        self.assertEqual(res.status, "degraded")
        self.assertEqual(res.verification.degraded_kind, "finding")

    def test_unverified_without_panels(self):
        # AC3 — no verification configured → status "unverified", never "clean".
        res = self._run()
        self.assertEqual(res.status, "unverified")
        self.assertIsNone(res.verification)

    def test_pipeline_enforces_family_quorum(self):
        # AC7 at the pipeline layer — 2-family roster aborts before synthesis.
        two_fam = {"opus": _v("opus", "anthropic"), "sonnet": _v("sonnet", "anthropic")}
        subject, target = decide.build_decide_prompt("go?")
        with self.assertRaises(RuntimeError) as e:
            pipeline.run_decide_pipeline(subject, target, ["opus", "sonnet"], "opus",
                                         two_fam, audit_voices=["opus"], log=lambda *_: None)
        self.assertIn("family quorum", str(e.exception))

    def test_chairman_failure_degrades_infra(self):
        # #1 — a chairman/synthesis failure must NOT read as clean. The fallback is a
        # raw voice (which trivially passes audit), so the gate folds the failure in.
        self.chairman_fails = True
        res = self._run(audit_voices=["codex", "agy"])
        self.assertEqual(res.status, "degraded")
        self.assertEqual(res.degraded_kind, "infra")
        self.assertEqual(res.review.council.synthesis_error, "timeout after 600s")
        self.assertTrue(any("synthesis failed" in r for r in res.degraded_reasons))
        # the audit panel itself was CLEAN on the fallback text — degraded is despite it.
        self.assertEqual(res.verification.audit_verdict, "CLEAN")

    def test_chairman_failure_degrades_without_panels(self):
        # #1 at the no-verification path — must not read as a bare 'unverified' either.
        self.chairman_fails = True
        res = self._run()
        self.assertEqual(res.status, "degraded")
        self.assertEqual(res.degraded_kind, "infra")
        self.assertIsNone(res.verification)

    def test_decide_skips_phantom_file_mechanical_check(self):
        # #2 — decide passes check_files=False (a decision has no diff), so a real repo
        # file a voice names is not a "phantom". Only severity_headings runs.
        res = self._run(audit_voices=["codex"])
        names = [c["check"] for c in res.verification.mechanical]
        self.assertNotIn("phantom_files", names)
        self.assertIn("severity_headings", names)

    def test_empty_chairman_synthesis_degrades(self):
        # a rc=0-but-empty synthesis is still a synthesis failure — never clean.
        self.chairman_empty = True
        res = self._run(audit_voices=["codex", "agy"])
        self.assertEqual(res.status, "degraded")
        self.assertEqual(res.degraded_kind, "infra")
        self.assertIn("empty synthesis", res.review.council.synthesis_error)

    def test_synth_fail_and_audit_issue_is_mixed(self):
        # a synthesis failure AND a real audit finding together → degraded_kind "mixed".
        self.chairman_fails = True
        self.audit_verdict = "ISSUES"
        res = self._run(audit_voices=["codex", "agy"])
        self.assertEqual(res.status, "degraded")
        self.assertEqual(res.degraded_kind, "mixed")

    def test_all_auditors_dead_degrades_infra(self):
        # the mandatory audit panel fails closed: every auditor dead → UNAVAILABLE →
        # degraded [infra], never a silent clean.
        self.audit_dies = True
        res = self._run(audit_voices=["codex", "agy"])
        self.assertEqual(res.verification.audit_verdict, "UNAVAILABLE")
        self.assertEqual(res.status, "degraded")
        self.assertEqual(res.degraded_kind, "infra")

    def test_partial_audit_panel_death_degrades_infra(self):
        # #7 — one auditor dead, the other CLEAN: the surviving verdict is CLEAN, but a
        # configured verifier could not run → degraded [infra], never a silent clean.
        self.audit_dead_voices = {"agy"}
        res = self._run(audit_voices=["codex", "agy"])
        self.assertEqual(res.verification.audit_verdict, "CLEAN")      # codex survived
        self.assertIn("agy", res.verification.audit_panel.errors)      # agy's death recorded
        self.assertEqual(res.status, "degraded")
        self.assertEqual(res.degraded_kind, "infra")
        self.assertTrue(any("could not run" in r for r in res.degraded_reasons))

    def test_unparseable_auditor_degrades_infra(self):
        # #10 — a LIVE auditor whose output has no parseable verdict ("ERROR") is excluded
        # from the worst-wins vote but is NOT a dead voice; counting only survivors would
        # read CLEAN. It's a coverage gap → degraded [infra], not a silent clean.
        self.audit_garbles = {"agy"}
        res = self._run(audit_voices=["codex", "agy"])
        self.assertEqual(res.verification.audit_verdict, "CLEAN")        # codex parsed CLEAN
        self.assertEqual(res.verification.audit_panel.verdicts["agy"], "ERROR")
        self.assertEqual(res.status, "degraded")
        self.assertEqual(res.degraded_kind, "infra")
        self.assertTrue(any("run/parse" in r for r in res.degraded_reasons))

    def test_ranking_failure_alone_stays_clean(self):
        # DEFENDED boundary (NFR4): peer-ranking is advisory. When it fails to parse, the
        # chairman synthesizes "on merits" (a supported fallback) and the audit still guards
        # faithfulness. A rank failure is visible in RANKINGS.md but must NOT degrade a run
        # whose synthesis is otherwise clean — else valid runs over-fire. (Two dogfood-review
        # BLOCKERs claimed this should gate; refuted against NFR4 + the code.)
        self.rankers_fail = True
        res = self._run(audit_voices=["codex", "agy"])
        self.assertEqual(res.review.council.orders, {})           # nothing parsed
        self.assertTrue(res.review.council.rank_errors)           # but recorded (visible)
        self.assertEqual(res.status, "clean")                     # advisory ≠ gate


class TestDecideConfig(unittest.TestCase):
    """[decide] panels + `family` parsing + the roster shape AC9 depends on."""

    def _toml(self, body: str) -> str:
        fd = tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False)
        fd.write(textwrap.dedent(body))
        fd.close()
        self.addCleanup(lambda: Path(fd.name).unlink(missing_ok=True))
        return fd.name

    def test_decide_section_and_family_parsed(self):
        cfg = config.load(self._toml("""
            [providers.opus]
            type = "cli"
            bin  = "claude"
            argv = ["claude", "-p", "--model", "opus"]
            family = "anthropic"

            [providers.qwen]
            type     = "http"
            endpoint = "https://example/v1/chat/completions"
            model    = "qwen-max"
            key_env  = "DASHSCOPE_API_KEY"
            family   = "alibaba"

            [council]
            voices   = ["opus", "codex", "grok"]
            chairman = "opus"

            [decide]
            audit   = ["codex", "grok"]
            redteam = []
        """))
        self.assertEqual(cfg.decide_audit, ["codex", "grok"])
        self.assertEqual(cfg.decide_redteam, [])              # redteam off by default
        self.assertEqual(cfg.providers["opus"].family, "anthropic")
        self.assertEqual(cfg.providers["qwen"].family, "alibaba")
        # AC9 — the http house is DEFINED but NOT enrolled in the default council.
        self.assertNotIn("qwen", cfg.voices)
        self.assertIn("qwen", cfg.providers)

    def test_unkeyed_http_voice_is_not_installed(self):
        # AC9 — a selected-but-unkeyed http house reports not-installed (→ skipped
        # gracefully at invoke time, never a crash).
        from council import providers as PV
        qwen = PV.Provider(name="qwen", transport="http", endpoint="https://x/v1",
                           model="qwen-max", key_env="CLI_COUNCIL_NO_SUCH_KEY_XYZ")
        self.assertNotIn("CLI_COUNCIL_NO_SUCH_KEY_XYZ", __import__("os").environ)
        self.assertFalse(PV.is_installed(qwen))
        # and invoke fails loudly (dropped from the run), rather than raising.
        ok, msg = PV.invoke(qwen, "hi", timeout=1)
        self.assertFalse(ok)
        self.assertIn("qwen", msg)

    def test_bad_decide_voice_rejected_loudly(self):
        with self.assertRaises(ValueError) as e:
            config.load(self._toml("""
                [council]
                voices = ["claude"]
                [decide]
                audit = ["ghostvoice"]
            """))
        self.assertIn("ghostvoice", str(e.exception))

    def test_explicit_missing_config_raises_not_silent(self):
        # #11 — an explicit --config that doesn't exist must FAIL LOUD, not silently fall
        # back to native-only Claude (the wrong-roster footgun).
        with self.assertRaises(ValueError) as e:
            config.load("/no/such/dir/council-does-not-exist.toml")
        self.assertIn("not a file", str(e.exception))


class TestEmptyOutputGuard(unittest.TestCase):
    """rc=0-but-empty output is a failure, not an answer — stage 1 AND the chairman."""

    def test_empty_opinion_is_dropped_not_counted(self):
        provs = {"opus": _v("opus", "anthropic"), "codex": _v("codex", "openai"),
                 "agy": _v("agy", "google"), "grok": _v("grok", "xai")}

        def fake(p, prompt, timeout):
            if "Recommendations to rank" in prompt:
                labels = re.findall(r"### (Response [A-Z])", prompt)
                return True, "FINAL RANKING:\n" + "\n".join(
                    f"{i}. {l}" for i, l in enumerate(labels, 1))
            if "chair of a decision council" in prompt:
                return True, "Do it.\n\n## IMPORTANT\nx"
            return (True, "  ") if p.name == "agy" else (True, f"answer from {p.name}")

        with mock.patch.object(stages, "invoke", fake):
            subject, target = decide.build_decide_prompt("go?")
            res = decide.run_decide(subject, target, voices=["opus", "codex", "agy", "grok"],
                                    chairman="opus", providers=provs)
        # agy returned empty → dropped from opinions, recorded as an error (not padding
        # the family quorum or the raw-voice set).
        self.assertNotIn("agy", res.council.opinions)
        self.assertEqual(res.council.opinion_errors.get("agy"), "returned empty output")
        self.assertIn("opus", res.council.opinions)
        self.assertEqual(sorted(res.reviewers), ["codex", "grok", "opus"])


class TestRankingFallback(unittest.TestCase):
    """#9 — when NO ranking parses, the chairman gets the honest '(no parseable
    rankings)' fallback, not a bogus mean_rank=None leaderboard."""

    def test_all_rankings_fail_gives_no_ranking_fallback(self):
        provs = {"opus": _v("opus", "anthropic"), "codex": _v("codex", "openai"),
                 "agy": _v("agy", "google")}
        seen = {}

        def fake(p, prompt, timeout):
            if "Recommendations to rank" in prompt:
                return True, "I refuse to give a ranking block."     # unparseable → rank_error
            if "chair of a decision council" in prompt:
                seen["chair"] = prompt
                return True, "Do it.\n\n## IMPORTANT\nx"
            return True, f"answer from {p.name}"

        with mock.patch.object(stages, "invoke", fake):
            subject, target = decide.build_decide_prompt("go?")
            res = decide.run_decide(subject, target, voices=["opus", "codex", "agy"],
                                    chairman="opus", providers=provs)
        # no ranking parsed → orders empty, and the chairman gets the honest fallback,
        # not a "mean rank None" leaderboard.
        self.assertEqual(res.council.orders, {})
        self.assertIn("no parseable rankings this run", seen["chair"])
        self.assertNotIn("mean rank None", seen["chair"])


if __name__ == "__main__":
    unittest.main()
