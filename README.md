# cli-council

Ask one question. Several models answer, **anonymously rank each other**, and a
chairman writes the final answer — using the **official CLIs you already pay
for**, not a paid API.

Inspired by [karpathy/llm-council](https://github.com/karpathy/llm-council),
rebuilt from scratch on a different substrate: **subscriptions, not API keys.**

```
$ council "Is SQLite WAL + synchronous=NORMAL safe for a nightly batch job?"

  stage 1 · first opinions      claude ✓  codex ✓  grok ✓
  stage 2 · anonymized ranking  claude ✓  codex ✓  grok ✓   →  leaderboard
  stage 3 · chairman (claude)   ✓

  ── Final answer ──────────────────────────────────────────
  Yes, with two conditions … (synthesised from all three, weighted by peer rank)
```

## Why it's different

- **No API keys. Ever.** cli-council sends zero HTTP requests to any model
  provider and stores zero credentials. It shells out to the official
  first-party CLIs — `claude`, `codex`, `grok`, `gemini` — that you installed and
  logged into yourself. Your subscription, your machine, your auth.
- **Zero runtime dependencies.** Pure Python standard library + `subprocess`.
  Nothing to `pip install`, no SDK, no network stack.
- **Native by default.** Out of the box the council is just **Claude** (via
  Claude Code, which is built for exactly this headless use). Every other voice
  is **opt-in** — add as many as you have subscriptions for.

Provider voices:

| voice  | CLI                    | status                              |
| ------ | ---------------------- | ----------------------------------- |
| claude | Claude Code            | native default                      |
| codex  | OpenAI Codex CLI       | supported                           |
| grok   | xAI Grok CLI           | supported                           |
| gemini | Google Gemini CLI      | experimental (headless auth varies) |
| agy    | Google Antigravity CLI | experimental (newer Google CLI)     |

For a **Google voice**, pick whichever CLI you actually run: the classic
`gemini` CLI, or Google's newer **Antigravity** (`agy`). On setups where the
gemini CLI has been retired, `agy` is the live Google voice. Both are gated by
their own smoke — enrol whichever PASSes on your machine.

- **A strict install contract.** The installer is an agent-driven, gated
  sequence: it detects which CLIs you have, offers to install the ones you want
  (with each vendor's own installer), walks you through each official login, and
  **smoke-tests every voice before enrolling it** — a voice that fails its smoke
  is refused loudly, never silently half-added. See
  [installer/CONTRACT.md](installer/CONTRACT.md).

## How it uses your subscriptions (read this)

cli-council is an **orchestrator of official tools**, not a bypass. It runs the
same `claude` / `codex` / `grok` / `gemini` commands you would run by hand,
one after another, and reads their stdout. That means:

- It spends your normal subscription usage. A council of N voices makes ~2N+1
  model calls per question (opinions + rankings + one chairman), so it consumes
  quota faster than a single chat. The installer shows you the cost before you
  enrol a voice.
- It respects each CLI's own auth and rate limits — it cannot and does not
  circumvent them.
- It is for **your own** use of **your own** authenticated CLIs. Do not use it
  to pool one subscription across multiple people; that is account sharing and
  violates the vendors' terms. cli-council ships no mechanism for it.

## How it works

Three stages, mirroring the council idea:

1. **First opinions** — your question goes to every enrolled voice; answers are
   collected.
2. **Anonymized peer ranking** — each voice sees the others' answers relabeled
   `Response A / B / …` (the label→voice map never enters a prompt, so no model
   can favour its own house), and returns a strict `FINAL RANKING:` block. A
   Borda count turns the rankings into a leaderboard, and each voice's written
   critique is kept as second-order context.
3. **Chairman** — one designated voice (default: your native Claude) reads all
   answers, the leaderboard, and the critiques, and writes the final answer.

A voice that errors or fails to produce a parseable ranking is reported
**loudly** and dropped from that stage — never silently swallowed.

## Install

```bash
git clone <this repo> && cd cli-council
./install.sh          # runs the strict install contract (detect → choose → install → login → smoke)
```

The installer is designed to be driven by an agent (e.g. Claude Code) so it can
run the vendor installers and hand you each login link. You can also run the
deterministic parts by hand — see [installer/CONTRACT.md](installer/CONTRACT.md).

## Run

```bash
council "your question here"
council --chairman codex "your question"     # pick who synthesises
council --voices claude,grok "your question" # ad-hoc subset of enrolled voices
```

## Status

Early, but the full three-stage council runs end-to-end today. Verified live:
`claude`, `codex`, `grok`, and Google's `agy` — including a multi-voice council
where one slow voice timed out mid-ranking and was dropped **loudly** while the
rest carried on (that's the design). `gemini` is structurally supported but
headless auth varies by machine, so it's gated by its own smoke like everything
else. Contributions welcome.

## License

MIT — see [LICENSE](LICENSE). Not affiliated with Anthropic, OpenAI, xAI, or
Google; "Claude", "Codex", "Grok", "Gemini" are their owners' marks, used only
to name the CLIs this tool invokes.
