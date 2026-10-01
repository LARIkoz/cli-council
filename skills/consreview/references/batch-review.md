# Batch code-review panels

Use this reference only when batch review is selected by the entrypoint.
The engine runs enrolled reviewers, anonymous peer ranking, chairman synthesis,
audit and redteam panels. Its gate describes pipeline evidence quality; even a
clean pipeline is not independently verified task acceptance.

## Prepare and run

Use `bin/council` from cli-council (Python 3.11+, standard library). Resolve
`council.toml` and the enrolled voices. The installer command
`python3 installer/doctor.py enroll <voice>...` (your roster;
a re-enroll keeps the chairman already in `council.toml`) rechecks those voices
and writes review panels. A hand-copied `council.example.toml` has commented
review panels; leaving them disabled produces an unverified run.

Resolve the target in the reviewed repository:

- Current working changes: `council review`.
- Git reference: `council review <ref>`.
- File contents: `council review --files <paths...>`; include relevant tests.
- Supplied plan or code: `council review --prompt-file <path>`.

Choose a new durable output directory outside the reviewed repository.
For current changes:

```bash
REVIEW_RUN="/path/to/review-runs/change-001"
mkdir -p "$REVIEW_RUN"
council review \
  --scope "Review material correctness, security, concurrency, resource handling, regressions and missing requirements. Focal points are hints, not review boundaries." \
  --out "$REVIEW_RUN" 2>"$REVIEW_RUN/engine.log"
```

Preserve explicit options:

- A requested `--no-redteam` maps to `--redteam ' '`; it keeps audit only.
- `--no-verify` skips both panels and yields an unverified run.
- `--audit v1,v2` and `--redteam v1,v2` override configured panels.

Nonzero exit means an engine failure. Exit zero can still be degraded:
read `pipeline-status.json`, not just the return code.

## Inspect and adjudicate

Read `pipeline-status.json`, `SYNTHESIS.md`, `AUDIT_VERDICT.md`,
`REDTEAM_VERDICT.md`, `MECHANICAL.md` and `RANKINGS.md`, where produced.
Original reviews are `v-*.md`; panel outputs are `a-*.md` and `r-*.md`.

Report the engine's verdict (SHIP / SHIP-WITH-EDITS / FIX / REWORK) and status
(clean / degraded / unverified) as engine outputs, then apply the entrypoint's
independent evidence and acceptance rules. Severity headings are BLOCKER,
IMPORTANT, CHECK, ACCEPT and NOISE. Missing artifacts or unavailable panelists
remain explicit.

Ranking and worst-wins gates do not adjudicate individual defects. A redteam
HOLDS is support to investigate; WEAK or REFUTED requires inspection of the
specific counterevidence. Drop a finding only after a demonstrated refutation.
Retain unresolved minority findings when evidence cannot settle them.

For degraded finding/mixed runs, do not apply the raw synthesis. Rebuild the
assessment from original reviews and flagging panels, verify paths/scenarios,
correct attributions and retain missed findings. For infrastructure degradation,
restore the missing review evidence or explicitly report the incomplete run.

Known shared-engine gap: the chairman can use peer critiques that its auditor
does not receive. Inspect rankings/peer evidence when an auditor alleges invented
support. Later support still must not count as independent discovery. The native
session workflow does not fix this separate batch-engine issue.

## Configured fallbacks

Batch voices can declare `fallbacks` in council.toml. They may therefore differ
from the requested model/family after a failure. Inspect and report the effective
voice; never treat a fallback as the requested family. An explicit family
requirement is unmet if that family did not complete.

The coordinator's source verification and disposition ledger remain necessary
even when every panel completes. Reuse existing quota/auth configuration; do not
add an API or spending fallback as an implicit review step.
