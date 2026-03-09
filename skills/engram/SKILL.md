---
name: engram
description: Set up and manage Engram — a graph-based memory system for OpenClaw agents. Use when installing Engram for the first time, configuring multi-agent memory, running ingest/export, managing the dashboard, or troubleshooting memory issues.
---

# Engram — Graph Memory for OpenClaw

Engram gives OpenClaw agents persistent, structured memory via a Kuzu graph database. It extracts entities, facts, relationships, and emotions from session logs and memory files, stores them in a queryable graph, and injects relevant context into every conversation turn via the context engine plugin.

## Architecture

```
Session logs → Export → Markdown → LLM Extraction → Kuzu Graph DB
                                                        ↓
                                        Context Engine Plugin → Agent turns
                                                        ↓
                                            Dashboard (optional)
```

**Components:**
- `engram/` — Core: ingest, query, schema, export, dedup, consolidation
- `engram-dashboard/` — FastAPI + Sigma.js visualization (optional)
- `extensions/engram-context-engine/` — OpenClaw plugin that injects graph facts into agent context

## First-Time Setup

### 1. Clone and install dependencies

```bash
cd <your-openclaw-workspace>
git clone https://github.com/Atomlaunch/engram.git engram-src
# Copy into your workspace structure:
cp -r engram-src/engram ./engram
cp -r engram-src/engram-dashboard ./engram-dashboard
cp -r engram-src/extensions/engram-context-engine ./extensions/engram-context-engine
```

### 2. Python environment

```bash
python3 -m venv .venv-memory
source .venv-memory/bin/activate
pip install kuzu chromadb
```

### 3. Configure paths

Edit these files to match your setup:

**`engram/ingest.py`** — Set memory directories:
```python
MEMORY_DIR = Path(os.path.expanduser("~/your-workspace/memory"))

AGENT_WORKSPACE_MEMORY_DIRS = {
    "your-agent-name": Path(os.path.expanduser("~/.openclaw/workspace-your-agent/memory")),
}
```

**`engram/ingest.py`** — Update `extract_agent_from_filepath()`:
- Files in your main workspace `memory/` dir → your main agent ID
- Files in agent workspace dirs → their respective agent IDs
- Update the known agent names list in the function

**`extensions/engram-context-engine/index.js`** — Set paths:
```javascript
const DEFAULTS = {
  workspaceRoot: "/path/to/your/workspace",
  engramDir: "/path/to/your/workspace/engram",
  pythonBin: "/path/to/your/workspace/.venv-memory/bin/python",
  agentsDir: "~/.openclaw/agents",
};
```

### 4. LLM for extraction

Engram uses an LLM to extract entities/facts from text. Configure via:

**Option A** — Environment variable:
```bash
export XAI_API_KEY="your-key"
export ENGRAM_MODEL="grok-3-mini-fast"  # or any model
```

**Option B** — Config file at `engram/config.json`:
```json
{
  "xai_api_key": "your-key",
  "model": "grok-3-mini-fast"
}
```

**Option C** — Auto-reads from OpenClaw config `skills.entries.grok.apiKey`

### 5. Register the context engine plugin

Add to your OpenClaw config (`~/.openclaw/openclaw.json`):

```json
{
  "plugins": {
    "allow": ["engram-context-engine"],
    "load": {
      "paths": ["/path/to/extensions/engram-context-engine"]
    },
    "slots": {
      "contextEngine": "engram-context-engine"
    },
    "entries": {
      "engram-context-engine": {
        "enabled": true
      }
    },
    "installs": {
      "engram-context-engine": {
        "source": "path",
        "sourcePath": "/path/to/extensions/engram-context-engine",
        "installPath": "/path/to/extensions/engram-context-engine",
        "version": "0.1.0"
      }
    }
  }
}
```

### 6. Initial ingest

```bash
# Export existing sessions to markdown
.venv-memory/bin/python engram/export_sessions.py

# Run ingest (parallel, 6 workers)
.venv-memory/bin/python engram/ingest.py --workers 6
```

### 7. Set up cron (hourly ingest)

```bash
crontab -e
# Add:
0 */1 * * * cd /path/to/workspace && .venv-memory/bin/python engram/export_sessions.py >> /tmp/engram-export.log 2>&1 && .venv-memory/bin/python engram/engram.py ingest >> /tmp/engram-ingest.log 2>&1
```

### 8. Dashboard (optional)

```bash
cd engram-dashboard
npm install
npm run bundle  # builds vendor.bundle.js
pm2 start ecosystem.config.js
# Dashboard at http://localhost:3847
```

**Important:** Kuzu only allows one writer at a time. If running the dashboard alongside ingest cron, stop the dashboard before ingest:
```
pm2 delete engram-dashboard; sleep 5; <run ingest>; pm2 start ecosystem.config.js
```

## Multi-Agent Memory

Each agent's facts are scoped by `agent_id` on every node. Queries return `agent_id = '<agent>' OR agent_id = 'shared'`.

**Agent resolution** (in `extract_agent_from_filepath()`):
1. Files in agent workspace memory dirs → that agent's ID
2. Files in main workspace `memory/` dir → main agent ID
3. Filename pattern `YYYY-MM-DD-<agent>-<hash>.md` → extracted agent name
4. Fallback → `shared` (avoid this — be explicit)

**Important:** Don't let main agent files fall into `shared`. Everything in your main workspace memory dir should map to your main agent.

## Key Commands

```bash
# Ingest with parallel workers
.venv-memory/bin/python engram/ingest.py --workers 6

# Force re-ingest all files
.venv-memory/bin/python engram/ingest.py --force --workers 6

# Ingest specific file
.venv-memory/bin/python engram/ingest.py --file memory/2026-03-09.md

# Query memories
.venv-memory/bin/python engram/context_query.py query "search terms" --agent main

# Entity deduplication
.venv-memory/bin/python engram/dedup_entities.py --dry-run
.venv-memory/bin/python engram/dedup_entities.py --execute

# Export sessions
.venv-memory/bin/python engram/export_sessions.py

# Stats
.venv-memory/bin/python engram/engram.py stats

# Dream consolidation (run nightly)
.venv-memory/bin/python engram/engram.py dream
```

## Troubleshooting

**DB lock error:** Another process has the Kuzu DB open. Stop the dashboard (`pm2 delete engram-dashboard`) and wait 5 seconds.

**Query returns 0 results:** The `context_query.py` splits multi-word queries into terms. Single short words (<3 chars) are skipped. Try more specific terms.

**Cross-agent contamination:** Check `extract_agent_from_filepath()` — make sure your main memory dir defaults to your main agent, not `shared`.

**Ingest slow:** Use `--workers 6` for parallel LLM extraction. Rate depends on your LLM provider.

**Dashboard shows wrong counts after agent filter:** Ensure `agent_id` is set on all node types (Entity, Fact, Episode, Emotion). Run dedup if entities are duplicated.

## Schema

**Nodes:** Entity, Fact, Episode, Emotion, SessionState
**Relationships:** RELATES_TO, CAUSED, PART_OF, MENTIONED_IN, EPISODE_EVOKES, ENTITY_EVOKES, DERIVED_FROM, ABOUT

Every node has `agent_id` (string) for multi-agent isolation.
