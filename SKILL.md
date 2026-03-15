# Engram — Temporal Knowledge Graph Memory

Engram is a self-hosted memory system for OpenClaw agents. It extracts entities, facts, and relationships from conversations using an LLM, stores them in a Neo4j graph database, and injects relevant memories into every conversation turn via an OpenClaw plugin.

**What it gives you:** Persistent memory across sessions and channels. Your agent wakes up knowing what happened yesterday, who it talked to, what decisions were made, and what preferences were expressed — automatically, per-agent, with no manual lookups.

---

## Architecture Overview

```
User message arrives
        │
        ▼
  OpenClaw Gateway
        │
        ├── assemble() ─── Engram plugin queries Neo4j ──► injects relevant memories into prompt
        │
        ▼
  LLM generates response
        │
        ▼
  afterTurn() ─── Engram plugin extracts facts from user message
        │
        ├── Regex fast path (~50-100ms) ──► writes to Neo4j
        │
        └── LLM fallback (async, ~7s) ──► writes to Neo4j (if regex found nothing)
        │
        ▼
  Hourly cron ─── batch ingest from memory/*.md files ──► enriches graph
```

**Two memory paths run in parallel:**
1. **Live write-through** — facts extracted and stored on every user turn
2. **Batch pipeline** — hourly cron ingests memory files, enriches and promotes facts

---

## Quick Start

### 1. Prerequisites

- **Python 3.10+**
- **Neo4j** (Community Edition, via Docker or standalone)
- **OpenClaw** running with at least one LLM provider
- **xAI API key** (for extraction model — grok-3-mini-fast recommended)

### 2. Install

```bash
# Clone the repo
git clone https://github.com/Atomlaunch/engram.git
cd engram

# One-shot setup (recommended)
bash scripts/setup.sh
```

Manual setup is still available if you want to do each step yourself:

```bash
python3 -m venv .venv-memory
source .venv-memory/bin/activate
pip install -r requirements.txt
pip install neo4j
```

### 3. Start Neo4j

```bash
# Using Docker (recommended):
docker-compose up -d

# This starts Neo4j on:
#   bolt://localhost:7687
#   http://localhost:7474 (browser)
```

Or install Neo4j standalone and start it manually.

### 4. Configure

If you used `bash scripts/setup.sh`, `config.json` is created for you automatically the first time.

Otherwise:

```bash
cp config.json.example config.json
# Edit config.json with your settings
```

Key fields in `config.json`:

| Field | What it does | Default |
|-------|-------------|---------|
| `model` | xAI model for extraction | `grok-3-mini-fast` |
| `xai_api_key` | xAI API key (or set `XAI_API_KEY` env var) | — |
| `backend` | Graph database backend | `neo4j` |
| `neo4j.uri` | Neo4j connection URI | `bolt://localhost:7687` |
| `neo4j.user` | Neo4j username | `neo4j` |
| `neo4j.password` | Neo4j password | — |
| `sessions_dir` | Where OpenClaw stores session files | `~/.openclaw/agents` |
| `memory_dir` | Where exported markdown memory files go | `~/clawd/memory` |
| `main_agent_id` | Primary agent ID | `main` |
| `agent_workspaces` | Map of agent IDs to their memory dirs | `{}` |
| `ingest_interval_minutes` | How often the cron pipeline runs | `5` |
| `max_concurrent_chunks` | Parallel LLM calls per file during ingest | `10` |

### 5. Run the pipeline manually (first time)

```bash
source .venv-memory/bin/activate

# Step 1: Export OpenClaw sessions to markdown
python export_sessions.py

# Step 2: Ingest markdown into the knowledge graph
python engram.py ingest

# Step 3: Generate a briefing
python engram.py briefing > BRIEFING.md
```

**Note:** First full ingest takes a while — each file is chunked and sent to the LLM for extraction. Subsequent runs only process new files.

### 6. Set up the cron (automated)

```bash
bash update-cron.sh
```

Installs a cron job that runs the full pipeline (export → ingest → briefing) at the interval in `config.json`.

### 7. Install the Context Engine plugin

Copy the plugin into your OpenClaw extensions directory:

```bash
cp -r extensions/engram-context-engine/ /path/to/your/workspace/extensions/
```

Add to your OpenClaw config (`~/.openclaw/openclaw.json`):

```json
{
  "plugins": {
    "allow": ["engram-context-engine"],
    "load": {
      "paths": ["/path/to/your/workspace/extensions"]
    },
    "slots": {
      "contextEngine": "engram-context-engine"
    },
    "entries": {
      "engram-context-engine": {
        "enabled": true,
        "config": {
          "workspaceRoot": "/path/to/your/workspace",
          "engramDir": "/path/to/your/workspace/engram",
          "pythonBin": "/path/to/your/workspace/.venv-memory/bin/python",
          "topK": 8,
          "maxChars": 6000,
          "includeSystemPromptAddition": true,
          "storeAssistantMessages": true,
          "storeUserMessages": true,
          "ownsCompaction": true
        }
      }
    }
  }
}
```

Restart OpenClaw. The plugin will:
- Inject relevant memories into every conversation turn
- Extract and store new facts from every user message
- Handle session compaction with durable memory flush

### 8. Multi-agent setup (optional)

For multiple agents with isolated memory, add agent workspaces to `config.json`:

```json
{
  "main_agent_id": "main",
  "agent_workspaces": {
    "loopfans": "~/.openclaw/workspace-loopfans/memory",
    "sillyfarms": "~/.openclaw/workspace-sillyfarms/memory"
  }
}
```

Each agent's facts are scoped by `agent_id` — no cross-agent leakage.

---

## Context Engine Plugin

The plugin (`extensions/engram-context-engine/`) is the core integration with OpenClaw.

### How it works

**On every turn (`assemble()`):**
1. Fetches pinned facts first:
   - global pinned facts for the agent
   - channel-scoped pinned facts for the current channel (if any)
   - session-scoped pinned facts for the current session (if any)
2. Extracts search terms from the last 6 messages
3. Queries Neo4j for matching entities, facts, and episodes
4. Deduplicates pinned facts from normal recall
5. Formats results as bullet points
6. Injects them as `systemPromptAddition` in the prompt

**After every turn (`afterTurn()`):**
1. Extracts facts from user messages via regex patterns
2. If regex finds nothing, fires async LLM extraction (non-blocking)
3. Writes facts to Neo4j with `source_type: "live_turn"` or `"live_llm"`
4. Deduplicates against existing facts

**On compaction (`compact()`):**
1. Splits transcript into older + recent messages
2. Extracts durable memories from older messages
3. Writes compaction summary to `memory/*.md` for cron pipeline
4. Returns compacted message history

### What gets extracted (regex patterns)

| Pattern | Example | Category |
|---------|---------|----------|
| `X is/was/has Y` | "TheDev is a software engineer" | attribute |
| `X said/told/mentioned Y` | "Tom said the API is ready" | reported |
| `X fixed/deployed/built Y` | "Jarvis built the dashboard" | action |
| `problem was/root cause is X` | "root cause was a null pointer" | diagnosis |
| `I love/hate/like/prefer X` | "I prefer dark mode" | preference |
| `I'm going to/planning to X` | "I'm going to refactor the API" | decision |

### Token overhead

Typical injection per turn: **~300-400 tokens** (12 bullets, ~130 chars each).
That's **~0.04%** of a 1M context window. Lean.

---

## CLI Usage

```bash
source .venv-memory/bin/activate

# Search the knowledge graph
python engram.py search "dashboard voice chat"

# Get a full briefing
python engram.py briefing

# Ingest new files
python engram.py ingest

# Run overnight consolidation
python engram.py dream
```

### Context Query CLI (used by plugin)

```bash
# Query memories
python context_query.py query "search terms" --agent main --limit 8 --json

# Store a fact manually
python context_query.py store --fact "User prefers dark mode" --agent main

# Store live turn facts (called by plugin automatically)
python context_query.py store_live --text "message text" --agent main --session sess123

# Get pinned facts for an agent/channel/session
python context_query.py pinned --agent main --channel 1477540685002440796 --limit 5

# LLM-based extraction (called by plugin as fallback)
python context_query.py extract_llm --text "message text" --agent main --session sess123
```

### Scoped pinned facts

Pinned facts are the standing rules that should inject even when search terms do not match.

Scope behavior:
- **Global**: applies everywhere for an agent
- **Channel**: applies only in one channel (good for Discord style/tone/workflow rules)
- **Session**: applies only in one session/thread

Use channel-scoped pinned facts for things like:
- Lady-channel tone/format rules
- team- or workflow-specific Discord behavior
- channel-local operational rules that should not bleed into every conversation

Helper script:

```bash
# Seed channel-scoped pinned facts
python scripts/seed_scoped_pinned.py \
  --agent main \
  --scope-type channel \
  --scope-id 1477540685002440796 \
  --category channel_rule \
  --fact "In Lady2good's channel, reply with a warm, natural, personal tone instead of operator or task-manager voice." \
  --fact "In Lady2good's channel, keep Discord formatting simple and human; avoid rigid report-style layouts unless asked."
```

---

## HTTP API

Start the server:
```bash
python http_server.py
# Runs on port 3456 (set ENGRAM_PORT to change)
```

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Node/relationship counts |
| `/stats` | GET | Counts by type |
| `/briefing` | GET | Full agent briefing |
| `/search` | POST | Search entities, facts, episodes |
| `/entity` | POST | Get entity with all relationships |
| `/recent` | POST | Recent episodes within time window |

---

## Graph Structure

**Nodes:**

| Type | Description |
|------|------------|
| Entity | People, tools, projects, concepts |
| Fact | Knowledge statements with confidence, importance, memory tier |
| Episode | Session summaries with timestamps |

**Key Fact fields:**

| Field | Description |
|-------|-------------|
| `agent_id` | Agent scope (isolation) |
| `memory_tier` | `candidate` (new) or `canonical` (promoted by cron) |
| `source_type` | `live_turn`, `live_llm`, `memory`, `live_context` |
| `scope_type` | `global`, `channel`, or `session` (optional; defaults to global behavior when absent) |
| `scope_id` | Channel ID or session key for scoped pinned facts |
| `importance` | 0.0–1.0 ranking score |
| `quality_score` | Data quality indicator |
| `contamination_score` | Noise/pollution indicator |
| `retrievable` | Whether fact should appear in queries |

**Relationships:**

| Type | Meaning |
|------|---------|
| ABOUT | Fact → Entity |
| MENTIONED_IN | Entity → Episode |
| DERIVED_FROM | Fact → Episode |
| RELATES_TO | Entity → Entity |

---

## File Reference

```
engram/
├── SKILL.md                  ← This file
├── config.json.example       ← Configuration template
├── engram.py                 ← CLI entry point
├── ingest.py                 ← LLM extraction pipeline
├── context_query.py          ← Fast query interface (used by plugin)
├── query.py                  ← Graph query functions
├── schema.py                 ← Schema definitions
├── schema_neo4j.py           ← Neo4j schema + driver
├── backend.py                ← Database backend abstraction
├── export_sessions.py        ← OpenClaw JSONL → markdown converter
├── http_server.py            ← REST API server
├── briefing.py               ← Briefing generator
├── consolidate.py            ← Memory consolidation (dream mode)
├── dedup_entities.py         ← Entity deduplication
├── reset_neo4j.py            ← Graph cleanup tools
├── inject_weekly_patterns.py ← Weekly pattern injection
├── local-entity-extractor.py ← Local entity extraction
├── run_ingest.py             ← Batch ingest runner
├── session.py                ← Session state management
├── mcp_server.py             ← MCP server (for tool-calling agents)
├── scripts/
│   ├── seed_scoped_pinned.py ← Helper to seed global/channel/session pinned facts
│   └── setup.sh             ← One-shot local setup/bootstrap script
├── update-cron.sh            ← Apply cron schedule from config
├── cleanup-sessions.sh       ← Session cleanup utility
├── requirements.txt          ← Python dependencies
├── Dockerfile                ← Neo4j Docker config
├── docker-compose.yml        ← Docker Compose for Neo4j
├── REBUILD.md                ← Graph rebuild/cleanup guide
├── extensions/
│   └── engram-context-engine/
│       ├── index.js          ← OpenClaw plugin (main logic)
│       ├── manifest.json     ← Plugin manifest
│       ├── openclaw.plugin.json ← Plugin metadata
│       └── package.json      ← Node.js package info
└── dashboard/                ← Web dashboard (optional)
    ├── server.py             ← FastAPI dashboard server
    └── static/               ← Frontend assets
```

---

## Environment Variables

| Variable | Purpose | Default |
|----------|---------|---------|
| `XAI_API_KEY` | xAI API key for extraction | config.json |
| `NEO4J_URI` | Neo4j connection URI | config.json / `bolt://localhost:7687` |
| `NEO4J_USER` | Neo4j username | config.json / `neo4j` |
| `NEO4J_PASSWORD` | Neo4j password | config.json |
| `ENGRAM_PORT` | HTTP server port | `3456` |
| `ENGRAM_AGENT_ID` | Override agent ID for queries | — |

---

## Troubleshooting

**"XAI_API_KEY not configured"**
→ Set `xai_api_key` in config.json or export `XAI_API_KEY` env var.

**Neo4j connection refused**
→ Make sure Neo4j is running: `docker-compose up -d` or check your standalone install.

**"Could not set lock on file"**
→ Another process has the database open. Wait for ingestion to finish.

**Plugin not injecting memories**
→ Check OpenClaw logs for `[engram-context-engine]` messages. Verify `plugins.slots.contextEngine` is set to `"engram-context-engine"` in your OpenClaw config.

**Empty search results**
→ Run `python engram.py ingest` to process files. Check Neo4j browser at `http://localhost:7474`.

**Ingestion is slow**
→ First run processes all files. Subsequent runs only process new ones. Increase `max_concurrent_chunks` for faster parallel processing.

**Agent memories leaking between agents**
→ Verify `agent_id` is set correctly. The plugin auto-detects from session file paths. Check with: `python context_query.py query "test" --agent <agent_id> --json`
