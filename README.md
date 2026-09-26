# Vera-2 — magicpin AI Challenge submission

Rule-based (not LLM-backed) implementation of the Vera contract: a FastAPI server exposing the
5 endpoints from `challenge-testing-brief.md`, a pure `compose()` over the 4-context framework,
and a conversation state machine for multi-turn replies.

**Live bot**: `<paste your deployed URL here>` · **Endpoints**: `/v1/healthz` `/v1/metadata`
`/v1/context` `/v1/tick` `/v1/reply` (+ optional `/v1/teardown`)

## Run

```bash
pip install -r requirements.txt
python bot.py                  # honors $PORT (default 8080); or: uvicorn bot:app --port 8080
pytest tests/ -q               # 52 tests, no server/network needed
python submission_gen.py       # rebuilds dataset/expanded/ + writes submission.jsonl
```

Deploy: `Dockerfile`, `Procfile`, and `render.yaml` included. Set `TEAM_NAME`, `TEAM_MEMBERS`,
`CONTACT_EMAIL` as env vars — `/v1/metadata` reads them. Run **one worker**: context and
conversation state live in process memory, so two workers would each hold half the pushes.

## Layout

```
bot.py                   entrypoint (re-exports vera.server:app)
vera/composer.py         pure compose(category, merchant, trigger, customer) -> dict
vera/conversation.py     /v1/reply state machine
vera/store.py            idempotent (scope, context_id, version) store
vera/server.py           FastAPI wiring
conversation_handlers.py optional §7.4 contract
docs/EVALUATION.md       measurements, defects found + fixed
```

## Approach

**Composition is template-driven, not an LLM call.** One handler per trigger `kind` (~20 of
them, plus a fact-anchored fallback). Every handler reads real fields off the pushed
`category`/`merchant`/`trigger`/`customer` dicts; when a fact is missing it falls back to a
*different real fact* rather than inventing one. Chosen over an LLM because: (1) it makes the
rubric's biggest penalty — hallucinated research/competitors — structurally impossible;
(2) it's deterministic and ~16ms per call, with no API key needed to run or grade it. The cost
is linguistic range, and the natural next step is an LLM *rewrite* pass over this fact-locked
skeleton, validated so every number and citation survives verbatim.

**Language.** `voice_mix()` picks `hi_en` from the merchant's/customer's language preference and
swaps the closing ask for a hand-written Hinglish variant (24 of 30 canonical messages carry
code-mix). Facts stay English so numbers and citations can't be mistranslated; an ask with no
table entry stays English rather than being machine-translated.

**No internal jargon leaks.** Payload values are often machine keys (`summer_2026`,
`ORS_demand_+40`) and signals are internal names (`winback_eligible`). Everything interpolated
passes through `humanize()`; signals appear only if they have merchant-readable phrasing, so an
unrecognized signal is dropped rather than leaked.

**Replies** check in order: hostile → opt-out → auto-reply (regex *and* verbatim-repeat, counted
per **merchant_id** not per conversation, since the judge opens a fresh `conversation_id` each
turn) → explicit commitment ("let's do it" → act, never re-qualify) → off-topic curveball →
otherwise acknowledge and advance. Bodies are never repeated verbatim.

**Idempotency.** `(scope, context_id)` → `{version, payload}`; `version <= current` returns
`409 stale_version`, higher replaces atomically. `/v1/tick` also dedupes on `suppression_key`,
so a trigger never opens a second conversation.

## Results

4/4 harness scenarios pass; p50 ~16ms on `/v1/tick` and `/v1/reply`; 25 triggers → 25 actions
with 0 malformed, 0 duplicate bodies, 0 multi-CTA, 0 expected operational penalties. Self-scored
**34.3/50** on the 5-dimension rubric (diagnostic only — no API key here for the real judge),
after a first scoring pass found and fixed five real defects. Details: `docs/EVALUATION.md`.

## Trade-offs

- **No LLM rewrite pass** — biggest remaining lever on variety and fluency.
- **In-process state** — per the brief's "no Redis/Kafka needed"; a restart mid-test loses
  in-flight conversations.
- **Hinglish is ask-level**, not full bilingual generation — deliberate, to avoid a bad
  translation of a clinical or numeric claim.
- **13 of 30 canonical triggers ship `{"placeholder": true}` payloads**; those score 28.8/50 vs
  38.6/50 for triggers with real payloads. With nothing to anchor on there's a hard ceiling on
  how specific an honest message can be.

## What additional context would have helped most

1. A curated per-category Hinglish phrase bank, so code-mix could extend past the ask safely.
2. Ground-truth merchant reply distributions per trigger kind — the auto-reply/hostile/intent
   patterns here are built from the brief's examples and are necessarily incomplete.
3. A shipped, pre-computed `test_pairs.json`. The brief describes "30 specific pairs" but the
   dataset only ships a generator; `submission_gen.py` reproduces the same 30 from the fixed
   seed, though a committed copy would remove any doubt everyone is scored on one set.
