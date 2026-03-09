# Engram — Graph Memory for OpenClaw Agents

Engram is a temporal knowledge graph memory system for [OpenClaw](https://github.com/openclaw/openclaw) agents. It extracts entities, facts, relationships, and emotions from session logs, stores them in a [Kuzu](https://kuzudb.com) graph database, and injects relevant context into every conversation turn.

## Features

- **Multi-agent memory isolation** — Each agent's facts scoped by `agent_id`
- **Parallel ingest** — 6+ workers for LLM extraction (~10 files/min)
- **Context engine plugin** — Injects graph facts into OpenClaw agent context per turn
- **Entity deduplication** — Normalize and merge duplicate entities
- **Interactive dashboard** — Sigma.js graph visualization with per-agent filtering
- **Hourly sync** — Cron-based export → ingest → briefing pipeline
- **Semantic + graph search** — Kuzu graph queries + Chroma vector embeddings

## Architecture

```
Session JSONL → Export → Markdown → LLM Extraction → Kuzu Graph DB
                                                          ↓
                                          Context Engine Plugin → Agent turns
                                                          ↓
                                              Dashboard (optional)
```

## Components

| Directory | Description |
|---|---|
| `*.py` (root) | Core: ingest, query, schema, export, dedup, consolidation |
| `dashboard/` | FastAPI + Sigma.js visualization with agent filtering |
| `extensions/context-engine/` | OpenClaw plugin for context injection |
| `skills/engram/` | Setup guide as an OpenClaw skill |

## Quick Start

See [`skills/engram/SKILL.md`](skills/engram/SKILL.md) for full setup instructions.

```bash
# Install dependencies
python3 -m venv .venv-memory
source .venv-memory/bin/activate
pip install kuzu chromadb

# Export sessions and run ingest
python export_sessions.py
python ingest.py --workers 6

# Query
python context_query.py query "search terms" --agent main
```

## Schema

**Nodes:** Entity, Fact, Episode, Emotion, SessionState  
**Relationships:** RELATES_TO, CAUSED, PART_OF, MENTIONED_IN, EPISODE_EVOKES, ENTITY_EVOKES, DERIVED_FROM, ABOUT

Every node has an `agent_id` field for multi-agent isolation.

## Dashboard

```bash
cd dashboard
npm install && npm run bundle
pm2 start ecosystem.config.js
# → http://localhost:3847
```

Features: graph visualization, per-agent filtering, entity search, node detail view, connection explorer.

## Requirements

- Python 3.10+
- Node.js 18+ (dashboard only)
- [Kuzu](https://kuzudb.com) (via pip)
- LLM API access (xAI/Grok by default, configurable)
- [OpenClaw](https://github.com/openclaw/openclaw) (for context engine plugin)

## License

MIT
