"""The three council stages: first opinions -> anonymized peer ranking -> chairman.

A voice that errors is recorded in the stage's `errors` and dropped from that
stage — never silently swallowed. Rankings weight the chairman's synthesis; they
never remove a voice from the council.

The stages are mode-agnostic: what changes between "ask a question" and "review a
diff" is the *wording* of the ranking + chairman prompts, carried by a `Mode`.
The engine (anonymize → rank → Borda → synthesize, loud-fail throughout) is the
same. `ASK` is the default; `review.REVIEW` is the code-review mode.
"""
from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from . import aggregate
from .providers import Provider, invoke, resolve_timeout

RANK_PROMPT = """\
You are a peer-ranking judge. Several assistants answered the SAME question. Their
answers are shown below, anonymized as "Response A", "Response B", etc. Judge the
QUALITY of each answer — grounding, usefulness, insight, honesty — NOT its length
or tone.

First, for EACH response, write 2-3 bullets: its strongest point and its weakest
or least-grounded point. Then END your reply with a ranking block in EXACTLY this
format — the line "FINAL RANKING:" followed by a numbered list of every response
label, best first, one per line, each line exactly "N. Response X", every label
appearing exactly once, nothing after the list:

FINAL RANKING:
1. Response C
2. Response A
3. Response B

Question:
{subject}

Answers to rank:
{blocks}
"""

CHAIRMAN_PROMPT = """\
You are the chairman of a council of AI assistants. Each answered the user's
question, then anonymously ranked each other's answers. Synthesize ONE clear,
correct, well-reasoned final answer for the user.

Weigh the peer leaderboard as a signal of answer quality, but it is only a signal
— a lower-ranked answer can still hold the one correct point. Do not invent
agreement that isn't there. Do not mention "Response A/B" labels or the ranking
mechanics in your final answer; just give the best answer.

User's question:
{subject}

Answers (attributed):
{answers}

Peer leaderboard (higher = better-ranked by peers):
{leaderboard}

Peer critiques (each judge's written evaluation; second-order context, not proof):
{critiques}

Now write the final answer:
"""


@dataclass(frozen=True)
class Mode:
    """What makes 'ask' differ from 'review': the ranking + chairman prompt
    wording. Templates take {subject} (the question or the review target) plus the
    same stage fields. The engine below is identical across modes."""
    name: str
    rank_prompt: str        # {subject}, {blocks}
    chairman_prompt: str    # {subject}, {answers}, {leaderboard}, {critiques}


ASK = Mode(name="ask", rank_prompt=RANK_PROMPT, chairman_prompt=CHAIRMAN_PROMPT)


@dataclass
class CouncilResult:
    question: str
    opinions: dict = field(default_factory=dict)        # voice -> answer
    opinion_errors: dict = field(default_factory=dict)  # voice -> error
    label_to_voice: dict = field(default_factory=dict)  # "Response A" -> voice
    orders: dict = field(default_factory=dict)          # ranker voice -> [labels]
    rank_errors: list = field(default_factory=list)     # [{voice, reason}]
    critiques: dict = field(default_factory=dict)       # ranker voice -> prose
    board: object = None                                # aggregate.Leaderboard | None
    final: str = ""
    chairman: str = ""
    synthesis_error: str = ""                           # set = chairman synth stage failed (final = loud fallback)


def _anonymize(opinions: dict[str, str]) -> tuple[str, dict[str, str]]:
    """Relabel answers A/B/C ordered by content hash, so label position never
    correlates with which voice produced it. Returns (blocks_text, label->voice)."""
    items = sorted(opinions.items(), key=lambda kv: hashlib.md5(kv[1].encode()).hexdigest())
    label_to_voice, blocks = {}, []
    for i, (voice, answer) in enumerate(items):
        label = f"Response {chr(65 + i)}"
        label_to_voice[label] = voice
        blocks.append(f"### {label}\n{answer}\n")
    return "\n".join(blocks), label_to_voice


def run_council(question: str, voices: list[str], chairman: str,
                providers: dict[str, Provider], timeout: float | None = None,
                mode: Mode = ASK, quorum=None, log=lambda *_: None) -> CouncilResult:
    # `timeout` (from --timeout / council.toml) is an explicit global override;
    # when None, each voice uses its own ceiling (resolve_timeout). Slow voices
    # (codex/grok) thus get their headroom without making fast natives wait.
    #
    # `quorum` is an optional pre-synthesis gate: called with the voices that
    # actually ANSWERED stage 1; returning a message aborts the run loudly BEFORE
    # any ranking or synthesis (decide mode uses it for the family quorum — a
    # decision must not be synthesized from too few model families). None = no gate,
    # so ask/review are unaffected.
    res = CouncilResult(question=question, chairman=chairman)

    # Stage 1 — first opinions (parallel: voices are I/O-bound, not CPU-bound).
    log("stage 1 · first opinions")
    with ThreadPoolExecutor(max_workers=len(voices)) as pool:
        futs = {pool.submit(invoke, providers[v], question,
                            resolve_timeout(providers[v], timeout)): v
                for v in voices}
        for fut in as_completed(futs):
            v = futs[fut]
            ok, out = fut.result()
            if ok:
                res.opinions[v] = out
                log(f"    {v} ✓")
            else:
                res.opinion_errors[v] = out
                log(f"    {v} ✗ {out}")
    if not res.opinions:
        raise RuntimeError("all voices failed at stage 1: " + "; ".join(res.opinion_errors.values()))

    # Pre-synthesis gate (decide's family quorum): abort before spending the
    # ranking + chairman calls if the answering voices don't clear the bar.
    if quorum is not None:
        gate_err = quorum(res.opinions)
        if gate_err:
            raise RuntimeError(gate_err)

    # A one-voice council needs no ranking; the single answer stands.
    if len(res.opinions) == 1:
        only = next(iter(res.opinions))
        res.final = res.opinions[only]
        res.chairman = only
        log("only one voice answered — returning its answer directly")
        return res

    # Stage 2 — anonymized peer ranking (parallel, same reason as stage 1).
    log("stage 2 · anonymized ranking")
    blocks, res.label_to_voice = _anonymize(res.opinions)
    labels = sorted(res.label_to_voice)
    rank_prompt = mode.rank_prompt.format(subject=question, blocks=blocks)
    rankers = list(res.opinions)  # only voices that produced an answer may rank
    with ThreadPoolExecutor(max_workers=len(rankers)) as pool:
        futs = {pool.submit(invoke, providers[v], rank_prompt,
                            resolve_timeout(providers[v], timeout)): v
                for v in rankers}
        for fut in as_completed(futs):
            v = futs[fut]
            ok, out = fut.result()
            if not ok:
                res.rank_errors.append({"voice": v, "reason": out})
                log(f"    {v} ✗ {out}")
                continue
            res.critiques[v] = aggregate.critique_prose(out)
            order, reason = aggregate.parse_ranking(out, labels)
            if order is None:
                res.rank_errors.append({"voice": v, "reason": reason})
                log(f"    {v} ⚠ ranking unparseable: {reason}")
            else:
                res.orders[v] = order
                log(f"    {v} ✓")
    res.board = aggregate.leaderboard(res.orders, res.label_to_voice, res.rank_errors)

    # Stage 3 — chairman synthesis.
    log(f"stage 3 · chairman ({chairman})")
    chair = chairman if chairman in res.opinions else next(iter(res.opinions))
    if chair != chairman:
        log(f"    chairman '{chairman}' had no answer; using '{chair}'")
        res.chairman = chair
    ok, out = invoke(providers[chair], _chairman_prompt(res, mode), resolve_timeout(providers[chair], timeout))
    if ok:
        res.final = out
        log("    ✓")
    else:
        # Loud fallback: no synthesis, hand back the peer-top answer, labeled — AND
        # record the error. The fallback IS a raw voice, so it trivially passes audit;
        # without this signal the pipeline gate would read the failed run as "clean".
        top = res.board.top if res.board else next(iter(res.opinions))
        res.final = (f"[chairman '{chair}' failed: {out}]\n\n"
                     f"Peer-top answer ({top}):\n{res.opinions.get(top, '')}")
        res.synthesis_error = out
        log(f"    ✗ {out} — fell back to peer-top answer")
    return res


def _chairman_prompt(res: CouncilResult, mode: Mode = ASK) -> str:
    answers = "\n\n".join(f"[{v}]\n{a}" for v, a in res.opinions.items())
    if res.board and res.board.rows:
        lb = "\n".join(f"{i}. {r['voice']}  (mean rank {r['mean_rank']}, "
                       f"borda {r['borda']}, {r['firsts']} firsts)"
                       for i, r in enumerate(res.board.rows, 1))
    else:
        lb = "(no parseable rankings this run — weigh answers on their merits)"
    crit = "\n\n".join(f"— {v} wrote:\n{c}" for v, c in res.critiques.items()) or "(none)"
    return mode.chairman_prompt.format(subject=res.question, answers=answers,
                                       leaderboard=lb, critiques=crit)
