# The install contract

cli-council installs itself through a **strict, gated sequence**. It is designed
to be driven by an agent (e.g. Claude Code) that can run commands and hand you
each vendor's login link — but every gate is a plain `doctor` command you can run
by hand. The rule is the same throughout: **nothing is enrolled that hasn't
proven it works, and nothing fails silently.**

The only guaranteed voice is the native one (**Claude**). Everything else is
opt-in — add as many as you have subscriptions for.

## Gates

Run every command below from the **repo root**, with `python3` (macOS and most
Linux ship Python 3 as `python3`, not `python`). `enroll` writes `council.toml`
into the current directory, so staying in the repo root keeps the config, the
gates, and the launcher together.

### Gate 0 — Detect (never proceed blind)

```
python3 installer/doctor.py detect
```

Lists every provider CLI and whether it's installed. The agent reads this before
proposing anything. If nothing but Claude is present, that's a complete, valid
council.

### Gate 1 — Choose (native default + opt-in)

The agent shows the menu:

> Native **Claude** is on by default. Which optional voices do you want to add?
> `[ ] codex   [ ] grok   [ ] gemini/agy (Google)`

Multi-select, no limit. Each added voice roughly **doubles a slice of the per-
question cost** (a council of N voices makes ~2N+1 model calls per question). The
agent states the cost before you confirm.

### Gate 2 — Install & log in (official flows only)

For each chosen voice that is missing or unauthenticated, the agent runs the
**vendor's own** installer and login — never a bundled binary, never a credential
prompt inside cli-council:

| voice  | install                                            | login                         |
| ------ | -------------------------------------------------- | ----------------------------- |
| claude | `npm i -g @anthropic-ai/claude-code`               | `claude` → `/login`           |
| codex  | `npm i -g @openai/codex`                           | `codex login`                 |
| grok   | `curl -fsSL https://x.ai/cli/install.sh \| bash`   | `grok login`                  |
| gemini | `npm i -g @google/gemini-cli`                      | `gemini` (browser on 1st run) |
| agy    | Google Antigravity CLI (per Google's instructions) | `agy` (sign in on 1st run)    |

For a **Google voice** pick whichever you run — the classic `gemini` CLI or the
newer **Antigravity** `agy`. If `gemini` is retired on your machine, use `agy`.
Both are experimental until their smoke PASSes.

cli-council stores no logins. Auth lives wherever each vendor CLI keeps it.

### Gate 3 — Smoke (the enrolment gate)

```
python3 installer/doctor.py smoke <voice>
```

Fires one tiny live call. **PASS** = installed AND authenticated AND actually
answering. A voice that does not PASS is **refused** — it is not written to the
config, and the agent says so out loud. No half-added voices, ever.

### Gate 4 — Enrol & dry-run

```
python3 installer/doctor.py enroll claude codex grok           # re-smokes each; writes only the PASSes
./bin/council "What are the trade-offs of WAL mode in SQLite?"  # dry-run the whole council
```

`enroll` **re-smokes every voice you name** and writes only the ones that PASS to
`council.toml` — a voice that fails is refused out loud, never half-added. (Pass
`--no-verify` to trust the smoke you just ran in Gate 3.) The dry-run uses the
`./bin/council` launcher, which puts the repo on Python's path for you; add `bin/`
to your PATH to call `council` from anywhere. It shows the three stages end-to-end
so you see the council actually work before you rely on it.

## Invariants (what the contract guarantees)

- **Native-only floor.** With zero opt-ins you still have a working council
  (Claude). It can never end up empty.
- **Smoke-gated enrolment (enforced in code).** `enroll` itself re-smokes and
  writes only the PASSes, so a voice reaches `council.toml` only after it proves
  it answers — the guarantee holds even if the caller names a broken voice. This
  covers optional token (`type = "http"`) voices too: their smoke is a real call,
  and `enroll` preserves the `[providers.*]` blocks it rewrites `[council]` around.
- **Loud failure.** Missing CLI, failed login, failed smoke, unparseable ranking
  — all surfaced, never swallowed.
- **No stored credentials.** cli-council installs official CLIs and reads their
  stdout; it never sees, stores, or forwards a CLI login. An opt-in token voice's
  API key is read from the env var you name (`key_env`) at call time and sent only
  to the endpoint you configured — never written to `council.toml`, never logged.
- **Re-runnable.** Adding or removing a voice re-runs its gate. `doctor list`
  shows the current state any time.
