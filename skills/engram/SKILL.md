---
name: engram
description: Set up and manage Engram — a graph-based memory system for OpenClaw agents. Use when installing Engram for the first time, configuring multi-agent memory, running ingest/export, managing the dashboard, or troubleshooting memory issues.
---

# Engram — Graph Memory for OpenClaw

Engram gives OpenClaw agents persistent, structured memory via a Neo4j graph database. It extracts entities, facts, relationships, and emotions from session logs and memory files, stores them in a queryable graph, and injects relevant context into every conversation turn via the context engine plugin.

## Architecture

```
Session logs → Export → Markdown → LLM Extraction → Neo4j Graph DB
                                                        ↓
                                        Context Engine Plugin → Agent turns
                                                        ↓
                                            Dashboard (optional)
```

**Components:**
- `engram/` — Core: ingest, query, schema, export, dedup, consolidation
- `dashboard/` — FastAPI + Sigma.js visualization (optional)
- `extensions/engram-context-engine/` — OpenClaw plugin for context injection

## Database Backends

Engram supports two backends, configured via `"backend"` in `config.json`:

| Backend | Default | Notes |
|---|---|---|
| `neo4j` | ✅ **Recommended** | Full graph DB, best performance, requires Neo4j running |
| `kuzu` | Legacy | Embedded, no server needed, single-writer limitation |

Set in `config.json`:
```json
{ "backend": "neo4j" }
```

## First-Time Setup

### 1. Clone and install

```bash
cd <your-openclaw-workspace>
git clone https://github.com/Atomlaunch/engram.git engram
```

### 2. Python environment

```bash
cd engram
python3 -m venv .venv-memory
source .venv-memory/bin/activate
pip install neo4j chromadb
```

### 3. Start Neo4j

Install and start Neo4j (if not already running):

```bash
# macOS
brew install neo4j && brew services start neo4j

# Linux (systemd)
sudo systemctl start neo4j

# Docker (quickest)
docker run -d --name neo4j \
  -p 7474:7474 -p 7687:7687 \
  -e NEO4J_AUTH=neo4j/your-password \
  neo4j:latest
```

Default connection: `bolt://localhost:7687` with user `neo4j`.

### 4. Configure

```bash
cp config.example.json config.json
```

Edit `config.json`:

```json
{
  "backend": "neo4j",
  "neo4j_uri": "bolt://localhost:7687",
  "neo4j_user": "neo4j",
  "neo4j_password": "your-password",

  "model": "grok-3-mini-fast",
  "xai_api_key": "your-xai-api-key",

  "main_agent_id": "main",
  "memory_dir": "~/your-workspace/memory",

  "agent_workspaces": {
    "agent-two": "~/.openclaw/workspace-agent-two/memory"
  },

  "ingest_workers": 6,

  "context_engine": {
    "workspace_root": "/full/path/to/your/workspace",
    "engram_dir": "/full/path/to/your/workspace/engram",
    "python_bin": "/full/path/to/your/workspace/engram/.venv-memory/bin/python",
    "agents_dir": "~/.openclaw/agents"
  }
}
```

**Key fields:**
- `backend` — `"neo4j"` (recommended) or `"kuzu"` (legacy)
- `main_agent_id` — Your primary agent's ID. All files in `memory_dir` default to this.
- `memory_dir` — Where your main agent's memory files live.
- `agent_workspaces` — Map of additional agent IDs to their memory directories.
- `xai_api_key` — For LLM extraction. Also reads from `XAI_API_KEY` env or OpenClaw's `skills.entries.grok.apiKey`.
- `context_engine` — Paths for the OpenClaw plugin. Leave empty to auto-detect.

### 5. Register the context engine plugin

Add to your OpenClaw config (`~/.openclaw/openclaw.json`):

```json
{
  "plugins": {
    "allow": ["engram-context-engine"],
    "load": {
      "paths": ["/path/to/engram/extensions"]
    },
    "slots": {
      "contextEngine": "engram-context-engine"
    },
    "entries": {
      "engram-context-engine": { "enabled": true }
    }
  }
}
```

> ✅ `load.paths` must point to the **parent** of the `engram-context-engine` folder — not to the folder itself.

### 6. Initial ingest

```bash
# Export existing sessions to markdown
.venv-memory/bin/python export_sessions.py

# Run ingest (parallel)
.venv-memory/bin/python ingest.py --workers 6
```

### 7. Restart OpenClaw

```bash
openclaw gateway restart
```

Verify the plugin loaded:
```bash
cat /tmp/openclaw/openclaw-$(date +%Y-%m-%d).log | grep -i engram | tail -10
```

### 8. Set up cron (hourly)

```bash
crontab -e
# Add (adjust paths):
0 * * * * cd /path/to/workspace/engram && .venv-memory/bin/python export_sessions.py >> /tmp/engram-export.log 2>&1 && .venv-memory/bin/python engram.py ingest >> /tmp/engram-ingest.log 2>&1
```

### 9. Dashboard (optional)

```bash
cd engram/dashboard
npm install && node bundle-deps.js
pm2 start ecosystem.config.js
# → http://localhost:3847
```

## Multi-Agent Memory

Each agent's facts are scoped by `agent_id`. Queries return facts matching `agent_id = '<agent>' OR agent_id = 'shared'`.

Configure agents in `config.json`:
- `main_agent_id` — files in `memory_dir` default to this
- `agent_workspaces` — additional agents mapped to their memory directories

Agent resolution order:
1. File in an `agent_workspaces` directory → that agent's ID
2. File in `memory_dir` with filename pattern `YYYY-MM-DD-<agent>-<hash>.md` → that agent
3. Any other file in `memory_dir` → `main_agent_id`
4. Unknown location → `shared`

## Key Commands

```bash
# Parallel ingest
.venv-memory/bin/python ingest.py --workers 6

# Force re-ingest all
.venv-memory/bin/python ingest.py --force --workers 6

# Query memories
.venv-memory/bin/python context_query.py query "search terms" --agent main

# Entity deduplication
.venv-memory/bin/python dedup_entities.py --dry-run
.venv-memory/bin/python dedup_entities.py --execute

# Stats
.venv-memory/bin/python engram.py stats

# Dream consolidation (nightly)
.venv-memory/bin/python engram.py dream
```

## ⚠️ Critical Gotchas

### 1. Plugin folder MUST be named `engram-context-engine`
OpenClaw resolves plugins by folder name. `load.paths` must point to the **parent directory** containing a folder named `engram-context-engine`.

> ❌ Wrong: `"paths": ["/path/to/engram/extensions/engram-context-engine"]`
> ✅ Right: `"paths": ["/path/to/engram/extensions"]`

The repo ships the plugin at `extensions/engram-context-engine/` — so just point to `extensions/`.

### 2. `python_bin` path must exist before restart
The plugin loads at OpenClaw startup. If the venv path doesn't exist, OpenClaw will crash and fail to start.

Verify before applying config:
```bash
ls /path/to/engram/.venv-memory/bin/python
```

### 3. Neo4j must be running before OpenClaw starts
If Neo4j is down when OpenClaw starts, the context engine plugin will fail to connect. Start Neo4j first, then start OpenClaw.

### 4. Apply config LAST — not mid-setup
Complete all setup steps (clone, venv, Neo4j, config.json) **before** patching `openclaw.json`. The gateway restart will fail if any paths or services are invalid.

### 5. Recovery if OpenClaw won't start
```bash
# Disable the plugin temporarily
nano ~/.openclaw/openclaw.json
# Set: "engram-context-engine": { "enabled": false }

openclaw gateway restart

# Check what failed
cat /tmp/openclaw/openclaw-$(date +%Y-%m-%d).log | grep -i "engram\|error\|plugin" | tail -30
```

## Troubleshooting

| Issue | Fix |
|---|---|
| OpenClaw won't start after adding engram | See Gotcha #5 — disable plugin, check logs |
| Plugin not loading (no context injected) | Check `load.paths` points to parent of `engram-context-engine` folder |
| `python_bin` error on startup | venv path wrong or not created yet |
| Neo4j connection refused | Start Neo4j before OpenClaw; check URI/credentials in config.json |
| Query returns 0 | Terms <3 chars are skipped. Use specific terms. |
| Cross-agent bleed | Check `config.json` — ensure `memory_dir` maps to `main_agent_id` |
| Slow ingest | Use `--workers 6` for parallel extraction |
| Dashboard wrong counts | Run dedup, verify `agent_id` on nodes |
