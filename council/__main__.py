"""`python3 -m council "question"` / `council "question"` — run the council."""
from __future__ import annotations

import argparse
import json
import sys

from . import __version__, config, stages


def main(argv: list[str] | None = None) -> int:
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
    voices = [v.strip() for v in args.voices.split(",")] if args.voices else cfg.voices
    unknown = [v for v in voices if v not in cfg.providers]
    if unknown:
        ap.error(f"unknown voices {unknown}; known: {sorted(cfg.providers)}")
    chairman = args.chairman or cfg.chairman
    timeout = args.timeout or cfg.timeout

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

    if res.board and res.board.rows:
        print("\n  peer leaderboard:", file=sys.stderr)
        for i, r in enumerate(res.board.rows, 1):
            print(f"    {i}. {r['voice']}  (mean {r['mean_rank']})", file=sys.stderr)
    if res.rank_errors:
        print("  rank issues: " + "; ".join(f"{e['voice']}: {e['reason']}" for e in res.rank_errors),
              file=sys.stderr)

    print("\n── Final answer ──────────────────────────────────────────\n")
    print(res.final)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
