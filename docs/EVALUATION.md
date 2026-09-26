# Evaluation — measurements, defects found, and fixes

Companion to `README.md` (kept to one page per challenge-brief.md §7.3).

## Measured results

Against a live instance (`judge_simulator.py`'s structural scenarios + direct HTTP):

| | |
|---|---|
| Structural scenarios (warmup / auto-reply / intent / hostile) | 4/4 pass |
| `/v1/tick` latency | p50 15.5ms (budget 30,000ms) |
| `/v1/reply` latency | p50 15.8ms |
| 25 triggers -> 25 actions | 0 malformed, 0 duplicate bodies, 0 multi-CTA |
| Re-tick same triggers | 0 actions (suppression works) |
| Expected operational penalties | 0 |

The full LLM-graded score needs an API key in `judge_simulator.py`. Self-scoring the 30
canonical messages against the same 5-dimension rubric (my own strict pass, so treat it as
diagnostic rather than a predicted grade) gave **34.3/50 (69%)**, up from 30.4/50 after a
first pass surfaced and fixed five real defects:

1. a sign bug that reported a *rise* in calls as a drop when a `perf_dip` payload had no numbers
2. a `chronic_refill_due` trigger on a dentist producing a contentless "refill delivery" message
3. internal slugs (`summer_2026`, `winback_eligible`, `free_for_members`) reaching the merchant
4. customer-facing messages ignoring `language_pref` and not identifying the business
5. timing/logic slips — a festival 188 days out pitched as "push now", an approaching
   milestone described as already crossed, a first-timer offer pitched at a 22-visit member

Each is now covered by a regression test. Scores split sharply by input richness:
**38.6/50 for triggers with real payloads vs 28.8/50 for the dataset's generated
`{"placeholder": true}` triggers** — with no payload to anchor on, there's a ceiling on how
specific any message can honestly be.

