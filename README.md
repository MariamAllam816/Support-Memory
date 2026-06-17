# Support Memory Reliability Layer

A small, local memory service for Aster Support account managers. It ingests raw support events, derives evidence-linked facts, scopes them by account / contact / ticket / policy, resolves contradictions and staleness with an explainable scoring rule, flags identity ambiguity for human review, and returns compact, evidence-linked context for a rep question.

**No external services, no API keys, no extra dependencies** — Python 3 standard library only (`sqlite3`, `argparse`, `hashlib`, `json`).

## Setup

```bash
cd support-memory
python3 --version   # 3.9+ is fine, no pip install needed
```

## Run it

```bash
# 1. Ingest the seed events and derive facts (creates memory.db)
python3 -m src.cli ingest --reset

# 2. Ask: "what should the rep know before calling Helios?"
python3 -m src.cli context acct_helios_478

# 3. Ask the same thing as JSON (for a UI / API caller)
python3 -m src.cli context acct_helios_478 --json

# 4. Explain why the system believes Helios is on the Enterprise Support plan
python3 -m src.cli list-facts acct_helios_478          # find a fact_id
python3 -m src.cli explain "account:acct_helios_478:plan:evt-1003"

# 5. See every conflict, duplicate, and ambiguous-identity flag at once
python3 -m src.cli flags

# 6. Build context with diff since last time
python3 -m src.cli context acct_helios_478 --diff
```

Run it again with `acct_nova_478` or `acct_delta_478` to see policy scoping in action (Delta never inherits Nova's privacy policy).

## Verification

```bash
python3 -m unittest tests.test_memory -v
```

19 tests, zero dependencies, ~0.5s. They cover the four business questions in the brief directly (see the docstring at the top of `tests/test_memory.py` for the mapping from test to question).

## What's in this repo

| Path | Purpose |
|---|---|
| `src/store.py` | SQLite schema: raw events, idempotency index, conflicts, facts, ambiguous identities, context snapshots |
| `src/extract.py` | Turns one raw event into candidate facts (whitelisted payload keys) + signal flags (`correction`, `supersedes`, `old_fact`, ...) |
| `src/resolve.py` | Scores competing facts in the same (entity, attribute) thread; decides current / superseded / contradicted / unverified |
| `src/identity.py` | Flags possible-same-entity pairs (shared phone, text mentions); never merges |
| `src/ingest.py` | Orchestrates ingestion (idempotency/conflict handling) and full fact rebuild |
| `src/context.py` | Builds compact account context, computes diff-since-last-build, and the explain view |
| `src/cli.py` | Command-line entry point |
| `tests/test_memory.py` | 19-test verification suite covering all edge cases |
| `sample_outputs/` | Real output from a clean run (9 files) |
| `ARCHITECTURE.md` | Event/fact/entity/context model, reliability assumptions, what was intentionally skipped |
| `DAILY_UPDATE.md` | End-of-session status update |
| `NEXT.md` | What I'd build next for production |
| `AI_USAGE.md` | How AI was used as a thinking partner, not a contractor |

## Design summary (short version — full detail in ARCHITECTURE.md)

**Four layers, kept structurally separate:**
1. `raw_events` — immutable evidence, every ingested row preserved (even duplicates and conflicts), never edited.
2. Idempotency/conflict tables — same `idempotency_key` + same content = no-op; same key + different content = flagged conflict, both versions kept.
3. `facts` — one row per (entity, attribute, source event), each carrying its own `status` (`current` / `superseded` / `contradicted` / `corroborating` / `unverified`), a numeric `confidence`, and a plain `reasoning` string written at derivation time.
4. `context` — a read-time view that joins facts + relationships + ambiguity flags + system integrity flags into one compact answer, plus a stored snapshot so the next build can diff against it.

**Resolution rule (used for the "which source should win" question):**
score = reliability weight (high=30/medium=20/low=10) + recency rank within the thread, with explicit author signals overriding the default (`correction: true` or `supersedes: ...` is +50, a self-flagged `old_fact: true` is −50, `unverified_policy_guess: true` is −9999 — unverified facts never win). Ties with disagreeing values are marked `contradicted` rather than guessed. Every score and its components are stored as the fact's `reasoning`, so `explain` never has to recompute anything — it just prints what was already decided and why.

**The deliberate trap (evt-1008):**
evt-1008's *text* says "Retry of evt-1004 with the same idempotency key," but its actual `idempotency_key` field (`idem-1008`) is *not* the same as evt-1004's (`idem-1004`). The system trusts the structured field, not the narrative text. This produces the correct result: evt-1008 is treated as a brand-new event (flagged only as a soft `duplicate_candidate`), while the *actual* conflict the system raises an alarm on is evt-1008 vs evt-1009 (same key `idem-1008`, different body/ticket).

## Tradeoffs / what I'd flag in review

- **Extraction is rule-based, not LLM-based.** For 20 fixed-shape events this is more accurate and 100% traceable; it will not generalize to arbitrary free text without an LLM extraction step layered on top (see `NEXT.md`).
- **Staleness is mostly signal-driven (`old_fact: true`), not time-decay.** A real system needs an actual TTL/decay policy per attribute type. I implemented the mechanism for explicit self-flagged staleness because the data demonstrates it; pure time-decay is specified in `NEXT.md` but not built, since 9 days of seed data can't meaningfully exercise it.
- **`build_facts` does a full rebuild from `raw_events` every time**, not an incremental update. That's the right tradeoff at this scale (deterministic, easy to audit, no drift) and the wrong one past a few thousand events (see `NEXT.md` for the incremental design).
- **Ambiguous identity detection is two simple heuristics** (shared phone number, text mentions of another contact's name near ambiguity language). It deliberately never merges — only flags. A production version would add a scored similarity model and a human merge-approval workflow.
