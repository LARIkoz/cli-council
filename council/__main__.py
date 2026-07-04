"""`council "question"` — ask the council · `council review [ref]` — review a diff.

`review` (and an explicit `ask`) are subcommands; anything else is treated as a
question, so `council "…"` keeps working exactly as before.
"""
from __future__ import annotations

import argparse
import json
import re
import sys

from . import __version__, config, pipeline, review, stages


def _safe(name: str) -> str:
    """A voice name becomes part of a filename (v-/a-/r-<name>.md). Names come from
    council.toml keys, so collapse anything outside [A-Za-z0-9._-] to '_' and strip
    leading dots/underscores — a name like "a/b" or "../x" can't escape the out dir."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", name).strip("._") or "voice"


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "review":
        return _review_main(argv[1:])
    if argv and argv[0] == "ask":
        argv = argv[1:]
    return _ask_main(argv)


def _shared_selection(args, cfg):
    """voices / chairman / timeout, resolved against the config, with a loud error
    on an unknown voice. Shared by both subcommands."""
    voices = [v.strip() for v in args.voices.split(",")] if args.voices else cfg.voices
    unknown = [v for v in voices if v not in cfg.providers]
    if unknown:
        raise SystemExit(f"unknown voices {unknown}; known: {sorted(cfg.providers)}")
    chairman = args.chairman or cfg.chairman
    if chairman not in cfg.providers:
        raise SystemExit(f"unknown chairman '{chairman}'; known: {sorted(cfg.providers)}")
    return voices, chairman, (args.timeout or cfg.timeout)


def _ask_main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        prog="council",
        description="Ask a council of your subscription CLIs; a chairman synthesises the answer.",
    )
    ap.add_argument("question", nargs="*", help="the question (or pipe it on stdin)")
    ap.add_argument("--voices", help="comma-separated subset of enrolled voices to use")
    ap.add_argument("--chairman", help="which voice synthesises the final answer")
    ap.add_argument("--config", help="path to council.toml")
    ap.add_argument("--timeout", type=float,
                    help="per-call timeout seconds (overrides the per-voice defaults for every voice)")
    ap.add_argument("--json", action="store_true", help="print the full result as JSON")
    ap.add_argument("--version", action="version", version=f"cli-council {__version__}")
    args = ap.parse_args(argv)

    question = " ".join(args.question).strip() or (sys.stdin.read().strip() if not sys.stdin.isatty() else "")
    if not question:
        ap.error("no question given (as arguments or on stdin)")

    cfg = config.load(args.config)
    voices, chairman, timeout = _shared_selection(args, cfg)

    tmode = "per-voice" if timeout is None else f"{timeout:.0f}s (forced)"
    print(f"cli-council {__version__} · voices: {', '.join(voices)} · chairman: {chairman} "
          f"· timeout: {tmode} · config: {cfg.source}", file=sys.stderr)

    try:
        res = stages.run_council(question, voices, chairman, cfg.providers, timeout,
                                 log=lambda m: print(m, file=sys.stderr))
    except RuntimeError as e:
        print(f"council failed: {e}", file=sys.stderr)
        return 1

    if args.json:
        board = res.board
        print(json.dumps({
            "question": res.question,
            "opinions": res.opinions,
            "opinion_errors": res.opinion_errors,
            "leaderboard": board.rows if board else [],
            "rank_errors": res.rank_errors,
            "chairman": res.chairman,
            "final": res.final,
        }, indent=2, ensure_ascii=False))
        return 0

    _print_leaderboard(res)
    print("\n── Final answer ──────────────────────────────────────────\n")
    print(res.final)
    return 0


def _review_main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        prog="council review",
        description="Review a code change with the council: voices review, peer-rank, "
                    "a chairman synthesises one severity-classified review.",
    )
    ap.add_argument("ref", nargs="?", help="git ref to diff (default: HEAD = uncommitted changes)")
    ap.add_argument("--files", nargs="+", help="review these files' contents instead of a diff")
    ap.add_argument("--prompt-file", help="use this file verbatim as the review prompt")
    ap.add_argument("--scope", help="what to focus the review on (default: all material issues)")
    ap.add_argument("--voices", help="comma-separated subset of enrolled voices to use")
    ap.add_argument("--chairman", help="which voice synthesises the final review")
    ap.add_argument("--config", help="path to council.toml")
    ap.add_argument("--timeout", type=float, help="per-call timeout seconds (overrides per-voice defaults)")
    ap.add_argument("--audit", metavar="V1,V2,...",
                    help="audit PANEL: comma-separated voices; each independently compares the "
                         "synthesis against the raw reviews (worst verdict wins). "
                         "Defaults to [review].audit in council.toml")
    ap.add_argument("--redteam", metavar="V1,V2,...",
                    help="redteam PANEL: comma-separated voices; each independently attacks the "
                         "findings (worst verdict wins). Defaults to [review].redteam in council.toml")
    ap.add_argument("--no-verify", action="store_true",
                    help="skip audit/redteam even if council.toml configures panels")
    ap.add_argument("--out", help="write SYNTHESIS.md + v-/a-/r-<voice>.md + verdicts + pipeline-status.json here")
    ap.add_argument("--json", action="store_true", help="print the full result as JSON")
    args = ap.parse_args(argv)

    try:
        subject, target = review.build_review_prompt(
            diff_ref=args.ref, files=args.files, prompt_file=args.prompt_file, scope=args.scope)
    except ValueError as e:
        print(f"nothing to review: {e}", file=sys.stderr)
        return 2

    cfg = config.load(args.config)
    voices, chairman, timeout = _shared_selection(args, cfg)

    tmode = "per-voice" if timeout is None else f"{timeout:.0f}s (forced)"
    print(f"cli-council {__version__} · review {target} · voices: {', '.join(voices)} · "
          f"chairman: {chairman} · timeout: {tmode} · config: {cfg.source}", file=sys.stderr)

    # Verification panels: CLI overrides toml; --no-verify kills both.
    split = lambda s: [v.strip() for v in s.split(",") if v.strip()]  # noqa: E731
    audit_voices = split(args.audit) if args.audit else list(cfg.review_audit)
    redteam_voices = split(args.redteam) if args.redteam else list(cfg.review_redteam)
    if args.no_verify:
        audit_voices, redteam_voices = [], []
    bad = [v for v in audit_voices + redteam_voices if v not in cfg.providers]
    if bad:
        print(f"unknown audit/redteam voices {bad}; known: {sorted(cfg.providers)}", file=sys.stderr)
        return 2

    try:
        res = pipeline.run_review_pipeline(
            subject, target, voices, chairman, cfg.providers,
            audit_voices=audit_voices, redteam_voices=redteam_voices,
            timeout=timeout, log=lambda m: print(m, file=sys.stderr))
    except RuntimeError as e:
        print(f"review failed: {e}", file=sys.stderr)
        return 1
    rev, ver = res.review, res.verification

    if args.out:
        _write_artifacts(args.out, subject, res)

    if args.json:
        board = rev.council.board
        out_data = {
            "target": rev.target,
            "verdict": rev.verdict,
            "status": res.status,
            "degraded_kind": res.degraded_kind,
            "degraded_reasons": res.degraded_reasons,
            "reviewers": rev.reviewers,
            "opinion_errors": rev.council.opinion_errors,
            "leaderboard": board.rows if board else [],
            "review": rev.review,
        }
        if ver:
            out_data["audit"] = {"verdict": ver.audit_verdict,
                                 "per_voice": ver.audit_panel.verdicts if ver.audit_panel else {},
                                 "errors": ver.audit_panel.errors if ver.audit_panel else {}}
            out_data["redteam"] = {"verdict": ver.redteam_verdict,
                                   "per_voice": ver.redteam_panel.verdicts if ver.redteam_panel else {},
                                   "errors": ver.redteam_panel.errors if ver.redteam_panel else {}}
            out_data["mechanical"] = ver.mechanical
        print(json.dumps(out_data, indent=2, ensure_ascii=False))
        return 0

    _print_leaderboard(rev.council)
    status_txt = res.status.upper() + (f" ({res.degraded_kind})" if res.degraded_kind else "")
    verdict_line = f"verdict: {rev.verdict} | {status_txt}"
    if ver:
        verdict_line += f" | audit: {ver.audit_verdict} | redteam: {ver.redteam_verdict}"
    print(f"\n── Review · {rev.target} · {verdict_line} ──────────────────\n")
    print(rev.review)
    if ver and ver.audit_verdict not in ("CLEAN", "SKIPPED") and ver.audit_panel:
        worst = [v for v, verd in ver.audit_panel.verdicts.items()
                 if verd == ver.audit_verdict]
        for v in worst:
            print(f"\n── Audit ({v}: {ver.audit_verdict}) ──\n\n{ver.audit_panel.texts[v]}")
    if ver and ver.redteam_verdict == "REFUTED" and ver.redteam_panel:
        worst = [v for v, verd in ver.redteam_panel.verdicts.items() if verd == "REFUTED"]
        for v in worst:
            print(f"\n── Redteam ({v}: REFUTED) ──\n\n{ver.redteam_panel.texts[v]}")
    return 0


def _print_leaderboard(res) -> None:
    if res.board and res.board.rows:
        print("\n  peer leaderboard:", file=sys.stderr)
        for i, r in enumerate(res.board.rows, 1):
            print(f"    {i}. {r['voice']}  (mean {r['mean_rank']})", file=sys.stderr)
    if res.rank_errors:
        print("  rank issues: " + "; ".join(f"{e['voice']}: {e['reason']}" for e in res.rank_errors),
              file=sys.stderr)


def _panel_verdict_md(title: str, verdict: str, panel) -> str:
    """One verdict file per panel: the rule-aggregated verdict up top, then every
    panelist's verdict + full text (raw always kept — a lone dissent stays visible)."""
    lines = [f"# {verdict}", "", f"Panel: {title} · aggregation: worst-wins · "
             f"{len(panel.verdicts)} verdicts, {len(panel.errors)} errors", ""]
    for v, verd in panel.verdicts.items():
        lines += [f"## {v} · {verd}", "", panel.texts[v], ""]
    for v, err in panel.errors.items():
        lines += [f"## {v} · ERROR", "", err, ""]
    return "\n".join(lines)


def _write_artifacts(out: str, subject: str, pres) -> None:
    """Persist the pipeline the way the orchestration contract expects:
    SYNTHESIS.md · v-<voice>.md (reviews) · a-/r-<voice>.md (panelists) ·
    AUDIT_VERDICT.md / REDTEAM_VERDICT.md / MECHANICAL.md · RANKINGS.md ·
    pipeline-status.json · PIPELINE_DEGRADED.md marker when not clean."""
    from pathlib import Path
    d = Path(out)
    d.mkdir(parents=True, exist_ok=True)
    rev, ver = pres.review, pres.verification
    council = rev.council

    (d / "review_prompt.md").write_text(subject)
    (d / "SYNTHESIS.md").write_text(rev.review)
    for voice, text in council.opinions.items():
        (d / f"v-{_safe(voice)}.md").write_text(text)

    lines = [f"target: {rev.target}", f"verdict: {rev.verdict}", ""]
    if council.board and council.board.rows:
        lines.append("## Leaderboard (Borda; signal, never a filter)")
        for i, r in enumerate(council.board.rows, 1):
            lines.append(f"{i}. {r['voice']}  mean_rank={r['mean_rank']} borda={r['borda']} firsts={r['firsts']}")
    if council.critiques:
        lines += ["", "## Peer critiques"]
        for v, c in council.critiques.items():
            lines += [f"\n— {v}:", c]
    if council.rank_errors:
        lines += ["", "## Rank issues"] + [f"- {e['voice']}: {e['reason']}" for e in council.rank_errors]
    (d / "RANKINGS.md").write_text("\n".join(lines) + "\n")

    status = {
        "target": rev.target,
        "verdict": rev.verdict,
        "status": pres.status,                     # clean | degraded | unverified
        "degraded_kind": pres.degraded_kind,       # "" | finding | infra | mixed
        "degraded_reasons": pres.degraded_reasons,
        "reviewers": rev.reviewers,
        "chairman": council.chairman or "",
        "opinion_errors": council.opinion_errors,
    }
    if ver:
        if ver.audit_panel:
            (d / "AUDIT_VERDICT.md").write_text(
                _panel_verdict_md("audit", ver.audit_verdict, ver.audit_panel))
            for v, text in ver.audit_panel.texts.items():
                (d / f"a-{_safe(v)}.md").write_text(text)
        if ver.redteam_panel:
            (d / "REDTEAM_VERDICT.md").write_text(
                _panel_verdict_md("redteam", ver.redteam_verdict, ver.redteam_panel))
            for v, text in ver.redteam_panel.texts.items():
                (d / f"r-{_safe(v)}.md").write_text(text)
        mech_lines = ["# Mechanical checks", ""]
        for c in ver.mechanical:
            mech_lines.append(f"- [{'PASS' if c['pass'] else 'FAIL'}] {c['check']}: {c['detail']}")
        (d / "MECHANICAL.md").write_text("\n".join(mech_lines) + "\n")
        status["audit"] = {"verdict": ver.audit_verdict,
                           "per_voice": ver.audit_panel.verdicts if ver.audit_panel else {},
                           "errors": ver.audit_panel.errors if ver.audit_panel else {}}
        status["redteam"] = {"verdict": ver.redteam_verdict,
                             "per_voice": ver.redteam_panel.verdicts if ver.redteam_panel else {},
                             "errors": ver.redteam_panel.errors if ver.redteam_panel else {}}
        status["mechanical"] = ver.mechanical
    (d / "pipeline-status.json").write_text(json.dumps(status, indent=2, ensure_ascii=False) + "\n")

    marker = d / "PIPELINE_DEGRADED.md"
    if pres.status == "degraded":
        # Two very different meanings, spelled out so they're never conflated:
        # a finding is a real problem to hand-verify; infra just means a verifier
        # couldn't run and the synthesis may well be fine — re-run, don't panic.
        if pres.degraded_kind == "infra":
            advice = ("Verification could NOT complete (infra — a checker was unavailable or "
                      "errored). This is NOT a finding: the synthesis may be fine. Re-run "
                      "verification (check keys / voices); don't hand-verify blind.")
        elif pres.degraded_kind == "mixed":
            advice = ("A verifier caught a problem AND another could not run. Treat the finding "
                      "as real — hand-verify SYNTHESIS.md against a-*.md / r-*.md — and re-run "
                      "the unavailable checker.")
        else:  # finding
            advice = ("A verifier caught a problem. Do NOT present SYNTHESIS.md as final without "
                      "hand-verification; the per-voice evidence is in a-*.md / r-*.md.")
        marker.write_text(f"# PIPELINE DEGRADED ({pres.degraded_kind or 'finding'})\n\n"
                          + "\n".join(f"- {r}" for r in pres.degraded_reasons)
                          + f"\n\n{advice}\n")
    else:
        marker.unlink(missing_ok=True)

    print(f"  wrote artifacts → {d}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
