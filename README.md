# Engram (AE) ⚡ — Andy's Edition

> Graph memory for [OpenClaw](https://github.com/openclaw/openclaw) agents.  
> Fork of [Atomlaunch/engram](https://github.com/Atomlaunch/engram) with Neo4j backend, extraction policy enforcement, CLI tooling, and automated pipeline.

Engram (AE) is a temporal knowledge graph memory system for OpenClaw agents. It extracts entities, facts, relationships, and emotions from conversations and daily logs, stores them in a [Neo4j](https://neo4j.com) graph database, and injects relevant context into every agent turn.

## What Makes AE Different

This fork diverges significantly from upstream Engram:

### Extraction Policy
The core difference. AE enforces a strict extraction policy ([`EXTRACTION_POLICY.md`](EXTRACTION_POLICY.md)) that ensures only durable, actionable knowledge enters the graph.

**Store by default:** Decisions, milestones, agent outcomes, todos, stable relationships, preferences, operating rules.

**Never store:** Reasoning/chain-of-thought, secrets/tokens, heartbeat wrappers, casual chatter, routine success spam.

**Every fact passes a 5-gate pre-store test:**
1. Durable next week?
2. Actionable or explanatory?
3. Specific enough to retrieve?
4. Safe (no secrets)?
5. Novel (not duplicate)?

**Result:** 90% noise reduction vs. unfiltered ingestion (3,590 → 354 nodes on identical source data).

### Extraction Model
AE uses **xAI `grok-4-1-fast-non-reasoning`** exclusively for extraction. No Ollama, no local models. Local models lack the judgment needed for policy enforcement. Non-reasoning mode is used because extraction doesn't need chain-of-thought — it needs good judgment on a clear rubric.

### Security
13 compiled secret-detection patterns catch and redact sensitive content before it reaches the LLM or the graph:
- API keys (`sk-`, `ghp_`, `gho_`, `github_pat_`, `xai-`, `AKIA`)
- Auth tokens (`Bearer`, quoted passwords/tokens)
- PEM private keys
- Content is stripped pre-extraction AND facts are re-scanned pre-storage (defense-in-depth)

### Additional Components (vs. Upstream)
- **Neo4j backend** — Richer graph queries and Cypher support (upstream uses Kuzu)
- **Engram CLI** (`cli.py`) — 8 commands: search, entity, timeline, agent-history, facts, stats, briefing, health
- **Batch extractor** (`batch_extract.py`) — Catches missed messages from session logs
- **Dream consolidation** (`consolidate.py`) — Gentle importance decay (0.2%/day), centrality boost, dedup, emotional patterns
- **Assembly cache** — Session-scoped caching in the context engine plugin (3-min TTL for queries, 10-min for pinned facts)
- **Channel-scoped pinned context** — Inject standing rules per-channel or per-session
- **Importance scoring** — LLM assigns `high`/`medium` labels, converted to numeric scores with category bonuses

## Architecture

```
  ┌─────────────────────────────────────────────────────────┐
  │                    Live Pipeline                         │
  │                                                          │
  │  User message → afterTurn hook → Regex + LLM extract     │
  │                                    ↓                      │
  │                         Pre-store test (5 gates)          │
  │                                    ↓                      │
  │                             Neo4j Graph DB                │
  │                                    ↑                      │
  │  Agent turn  ← assemble hook ← Context query (cached)     │
  └─────────────────────────────────────────────────────────┘

  ┌─────────────────────────────────────────────────────────┐
  │                  Background Pipeline                     │
  │                                                          │
  │  Daily logs → ingest.py → grok-4-1-fast-non-reasoning    │
  │                             ↓                             │
  │                    Secret redaction + policy filter        │
  │                             ↓                             │
  │                        Neo4j Graph DB                     │
  │                                                          │
  │  Session JSONL → batch_extract.py (every 30 min)         │
  │  Graph DB → consolidate.py (daily)                       │
  │    → gentle decay, centrality boost, dedup               │
  └─────────────────────────────────────────────────────────┘
```

## Components

| Component | Description |
|---|---|
| `cli.py` | **CLI** — search, entity, timeline, agent-history, facts, stats, briefing, health |
| `ingest.py` | **Bulk ingest** — parallel LLM extraction with policy enforcement |
| `context_query.py` | **Context queries** — semantic + graph search, live extraction (regex + LLM) |
| `batch_extract.py` | **Batch extractor** — scans session logs, re-extracts missed messages |
| `consolidate.py` | **Dream consolidation** — gentle decay, centrality boost, dedup |
| `schema_neo4j.py` | **Neo4j schema** — node/relationship definitions |
| `briefing.py` | **Briefings** — generate session briefings from graph state |
| `EXTRACTION_POLICY.md` | **Policy** — canonical reference for what gets stored, skipped, and scored |
| `extensions/context-engine/` | **OpenClaw plugin** — assemble() + afterTurn() hooks with assembly cache |
| `dashboard/` | **Dashboard** — Sigma.js graph visualization (optional) |

## Quick Start

### 1. Neo4j

```bash
podman run -d --name neo4j-engram \
  -p 7474:7474 -p 7687:7687 \
  -e NEO4J_AUTH=neo4j/your-password \
  -v neo4j-data:/data \
  neo4j:5-community
```

### 2. Python Dependencies

```bash
python3 -m venv .venv-memory
source .venv-memory/bin/activate
pip install -r requirements.txt
pip install neo4j
```

### 3. Configuration

```bash
cp config.json.example config.json
# Edit config.json:
#   backend: "neo4j"
#   neo4j.uri: "bolt://localhost:7687"
#   neo4j.user: "neo4j"
#   neo4j.password: "your-password"
#   xai_api_key: "your-xai-key"
```

### 4. Initial Ingest

```bash
# Run bulk ingest with parallel workers
python ingest.py --workers 6

# Check graph stats
python cli.py stats

# Check graph health
python cli.py health
```

### 5. Context Engine Plugin (OpenClaw)

Copy or symlink `extensions/context-engine/` into your OpenClaw extensions directory, then configure the plugin in your OpenClaw config. See [`SKILL.md`](SKILL.md) for detailed setup.

## CLI Usage

```bash
engram search "CalCity Stripe"                  # Search everything
engram entity "Woody"                           # Full context for an entity
engram timeline "CalCity" --days 30             # Project/entity timeline
engram agent-history "Buzz" --project CalCity   # What an agent built
engram facts --recent --days 7                  # Recent facts
engram stats                                    # Graph statistics
engram briefing --save                          # Generate session briefing
engram health                                   # Graph health check
```

All commands support `--json` for structured output.

## Extraction Policy

The extraction policy is the heart of AE. See [`EXTRACTION_POLICY.md`](EXTRACTION_POLICY.md) for the full spec.

**Goal:** Engram should answer:
- What changed?
- Why did we decide that?
- Who worked on it?
- What's still pending?

Not: every intermediate thought or cron wrapper.

**Importance scoring:**

| Level | Score | What qualifies |
|---|---|---|
| High | 0.80–1.0 | Decisions, milestones, durable preferences, major lessons |
| Medium | 0.50–0.70 | Agent outcomes, meaningful run summaries, useful todos |
| Low | Skip | Transient status, repetitive output, scratch text |

## Dream Consolidation

Runs daily. Maintains graph health:

- **Gentle importance decay** — 0.2%/day (~0.998^days). Facts stay near full strength for months, gently recede over a year.
- **Centrality boost** — highly-connected entities get importance bumps
- **Deduplication** — merge near-duplicate facts
- **Relationship strengthening** — reinforce frequently co-occurring entity links
- **Emotional pattern tracking** — track frustration, satisfaction, and other patterns over time

## Schema

**Nodes:** `Entity`, `Fact`, `Episode`, `Emotion`, `SessionState`

**Relationships:** `RELATES_TO`, `CAUSED`, `PART_OF`, `MENTIONED_IN`, `EPISODE_EVOKES`, `ENTITY_EVOKES`, `DERIVED_FROM`, `ABOUT`

Every node carries an `agent_id` field for multi-agent memory isolation.

## Requirements

- Python 3.10+
- [Neo4j](https://neo4j.com) 5+ (Community Edition)
- xAI API key (for `grok-4-1-fast-non-reasoning` extraction)
- [OpenClaw](https://github.com/openclaw/openclaw) (for context engine plugin)
- Node.js 18+ (dashboard only)

## Upstream

Fork of [Atomlaunch/engram](https://github.com/Atomlaunch/engram). Upstream uses Kuzu as the default graph backend and Ollama for extraction. AE uses Neo4j and xAI exclusively.

## License

MIT
