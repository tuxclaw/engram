# Engram — Graph Memory for OpenClaw Agents

> Fork of [Atomlaunch/engram](https://github.com/Atomlaunch/engram) with Neo4j backend, CLI tooling, automated extraction pipeline, and dream consolidation.

Engram is a temporal knowledge graph memory system for [OpenClaw](https://github.com/openclaw/openclaw) agents. It extracts entities, facts, relationships, and emotions from conversations, stores them in a [Neo4j](https://neo4j.com) graph database, and injects relevant context into every agent turn.

## What's Different in This Fork

This fork adds several components on top of the upstream Engram project:

- **Neo4j backend** — Switched from Kuzu to Neo4j for richer graph queries and Cypher support
- **Engram CLI** (`cli.py`) — Unified command-line interface for searching, querying, and managing the graph
- **Batch extractor** (`batch_extract.py`) — Catches missed messages by scanning session logs on a cron schedule
- **Always-on LLM extraction** — Both regex AND LLM extraction run on every message (not gated)
- **Dream consolidation** (`consolidate.py`) — Nightly pipeline for importance decay, centrality boost, dedup, and emotional pattern tracking
- **Context Engine plugin** — OpenClaw plugin that injects graph memories into agent context and extracts new facts after each turn
- **Channel-scoped pinned context** — Inject pinned facts per-channel for targeted memory

## Architecture

```
  ┌─────────────────────────────────────────────────────┐
  │                   Live Pipeline                      │
  │                                                      │
  │  User message → afterTurn hook → Regex + LLM extract │
  │                                    ↓                  │
  │                              Neo4j Graph DB           │
  │                                    ↑                  │
  │  Agent turn  ← assemble hook ← Context query          │
  └─────────────────────────────────────────────────────┘

  ┌─────────────────────────────────────────────────────┐
  │                 Background Pipeline                  │
  │                                                      │
  │  Session JSONL → batch_extract.py (every 30 min)     │
  │  Graph DB → consolidate.py (daily 4 AM)              │
  │    → importance decay, centrality boost, dedup        │
  │    → relationship strengthening, emotional patterns   │
  └─────────────────────────────────────────────────────┘
```

## Components

| Component | Description |
|---|---|
| `cli.py` | **CLI** — search, entity, timeline, agent-history, facts, stats, briefing, health |
| `batch_extract.py` | **Batch extractor** — scans session logs, re-extracts missed messages |
| `consolidate.py` | **Dream consolidation** — nightly maintenance (decay, boost, dedup) |
| `schema_neo4j.py` | **Neo4j schema** — node/relationship definitions for Neo4j backend |
| `context_query.py` | **Context queries** — semantic + graph search for agent context injection |
| `ingest.py` | **Bulk ingest** — parallel LLM extraction from markdown/session exports |
| `dedup_entities.py` | **Entity dedup** — normalize and merge duplicate entities |
| `briefing.py` | **Briefings** — generate session briefings from graph state |
| `extensions/context-engine/` | **OpenClaw plugin** — hooks into assemble() + afterTurn() |
| `dashboard/` | **Dashboard** — Sigma.js graph visualization (optional) |
| `skills/engram/` | **Skill** — OpenClaw setup guide |

## Quick Start

### 1. Neo4j

```bash
# Start Neo4j via Podman (or Docker)
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
# Plus: neo4j (driver)
pip install neo4j
```

### 3. Configuration

```bash
cp config.example.json config.json
# Edit config.json:
#   backend: "neo4j"
#   neo4j.uri: "bolt://localhost:7687"
#   neo4j.user: "neo4j"
#   neo4j.password: "your-password"
#   llm.provider: "xai" (or "ollama" for local models)
```

### 4. Initial Ingest

```bash
# Export OpenClaw session logs
python export_sessions.py

# Run bulk ingest with parallel workers
python ingest.py --workers 6

# Check graph health
python cli.py health
```

### 5. Context Engine Plugin (OpenClaw)

Copy or symlink `extensions/context-engine/` into your OpenClaw extensions directory, then patch your OpenClaw config to load the plugin. See [`SKILL.md`](SKILL.md) for detailed setup.

## CLI Usage

Install the CLI wrapper for easy access:

```bash
# Create a wrapper script at ~/.local/bin/engram
# that activates the venv and runs cli.py
```

### Commands

```bash
engram search "CalCity Stripe"                  # Search everything
engram entity "Woody"                           # Full context for an entity
engram entity "Woody" --relationships           # Just relationships
engram timeline "CalCity" --days 30             # Project/entity timeline
engram agent-history "Buzz" --project CalCity   # What an agent built
engram facts --recent --days 7                  # Recent facts
engram stats                                    # Graph statistics
engram briefing --save                          # Generate session briefing
engram health                                   # Graph health check
```

All commands support `--json` for structured output.

### Aliases

| Alias | Command |
|---|---|
| `s` | search |
| `e` | entity |
| `t` | timeline |
| `ah` | agent-history |
| `f` | facts |
| `b` | briefing |
| `h` | health |

## Automated Pipeline

### Live Extraction (every turn)
The Context Engine plugin runs after each agent turn:
1. Regex extraction — fast pattern matching for common fact structures
2. LLM extraction — deeper semantic extraction (both run, not gated)
3. Facts stored in Neo4j with entity linking and timestamps

### Batch Catch-Up (every 30 min)
```bash
python batch_extract.py --hours 1 --agent main
```
Scans session JSONL logs for messages the live hook missed (gateway restarts, errors, etc.). Built-in dedup prevents duplicate facts.

### Dream Consolidation (daily at 4 AM)
```bash
python consolidate.py
```
- **Importance decay** — older facts gradually lose weight
- **Centrality boost** — highly-connected entities get importance bumps
- **Deduplication** — merge near-duplicate facts
- **Relationship strengthening** — reinforce frequently co-occurring entity links
- **Emotional pattern tracking** — track frustration, satisfaction, and other patterns over time

## Schema

**Nodes:**
- `Entity` — people, projects, tools, concepts
- `Fact` — atomic knowledge units with timestamps and importance scores
- `Episode` — session-level groupings
- `Emotion` — tracked emotional states

**Relationships:**
`RELATES_TO`, `CAUSED`, `PART_OF`, `MENTIONED_IN`, `EPISODE_EVOKES`, `ENTITY_EVOKES`, `DERIVED_FROM`, `ABOUT`, `SUPERSEDES`

Every node carries an `agent_id` field for multi-agent memory isolation.

## Dashboard (Optional)

```bash
cd dashboard
npm install && npm run bundle
pm2 start ecosystem.config.js
# → http://localhost:3847
```

Sigma.js graph visualization with per-agent filtering, entity search, node detail view, and connection explorer.

## Requirements

- Python 3.10+
- [Neo4j](https://neo4j.com) 5+ (Community Edition works)
- Node.js 18+ (dashboard only)
- LLM API access (xAI/Grok by default, or local via Ollama)
- [OpenClaw](https://github.com/openclaw/openclaw) (for context engine plugin)

## Upstream

This is a fork of [Atomlaunch/engram](https://github.com/Atomlaunch/engram). Upstream uses Kuzu as the default graph backend. This fork switched to Neo4j and added the CLI, batch extraction, and consolidation pipeline.

## License

MIT
