# Engram Extraction Policy

> Canonical reference for what Engram stores, skips, and how it scores importance.
> Both batch ingest (`ingest.py`) and live extraction (`context_query.py`) enforce this policy.

## Store by Default (High Signal)
- Decisions that change future behavior
- Project milestones
- Agent outcomes with impact
- Todos / reminders / commitments
- Stable relationships (people ↔ projects ↔ repos ↔ tools)
- Preferences / operating rules

## Store Selectively (Medium Signal)
- Operational run summaries — **only** when they materially changed something, exposed a systemic issue, or validated a new setup
- Errors — **only** when they taught us something or are recurring

## Never Store
- Internal reasoning / think / chain-of-thought
- Secrets, passwords, tokens, raw auth-bearing commands
- Reminder wrappers / heartbeat envelopes / transport boilerplate
- Casual chatter, duplicates, routine success spam

## Importance Scoring
| Level  | Score     | What qualifies                                           |
|--------|-----------|----------------------------------------------------------|
| High   | 0.80–1.0  | Decisions, milestones, durable preferences, major lessons |
| Medium | 0.50–0.70 | Agent outcomes, meaningful run summaries, useful todos     |
| Low    | 0.10–0.30 | Transient status, repetitive cron output, scratch text     |

Low-importance facts should generally be **skipped**, not stored at low importance.

## Pre-Store Test (5 Gates)

Every candidate fact must pass **all five** before it gets written to the graph:

1. **Durable next week?** — Will this still matter in 7+ days?
2. **Actionable or explanatory?** — Does it drive a future action or explain a past decision?
3. **Specific enough to retrieve?** — Could someone search for this and find it useful?
4. **Safe?** — No secrets, no raw reasoning, no auth tokens.
5. **Novel?** — Not a duplicate or near-duplicate of something already stored.

If it doesn't clear that bar, skip it.

## Goal

Engram should answer:
- **What changed?**
- **Why did we decide that?**
- **Who worked on it?**
- **What's still pending?**

Not: every intermediate thought or cron wrapper.
