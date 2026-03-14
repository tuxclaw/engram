# Engram cleanup / forced re-ingest workflow

## What changed

- **Default ingest is now hygiene-first**:
  - `engram/ingest.py` no longer ingests `engram/exported-sessions/*.md` unless you explicitly opt in with `--include-exported-sessions`.
  - This prevents transcript/archive sludge from being mixed into the main graph during normal rebuilds.
- Added **safe Neo4j cleanup tool**:
  - `engram/reset_neo4j.py`
  - Supports:
    - `full`
    - `archive-only`
    - `by-source-type --source-type ...`
  - Includes `--dry-run`
- Added **wrapper script**:
  - `scripts/engram-rebuild.sh`
  - Handles backup, reset, rebuild, archive backfill, and full-cycle workflows.
- Updated `scripts/run-engram-ingest.sh` to call `python -m engram.ingest` with configurable worker count.

## Recommended practical cleanup path

### 1) Backup first
```bash
cd ~/clawd
bash scripts/engram-rebuild.sh backup
```

### 2) Remove old sludge but keep the rest of the graph shape conservative
If the problem is mainly polluted archive/session ingest:
```bash
bash scripts/engram-rebuild.sh reset-archive
```

### 3) Force rebuild from curated memory only
```bash
bash scripts/engram-rebuild.sh rebuild
```

This rebuild path uses:
- `memory/`
- configured agent workspace memory dirs
- **not** `engram/exported-sessions/` unless explicitly requested

## Optional archive backfill
If you later want the exported sessions ingested as archive-tier evidence again:
```bash
bash scripts/engram-rebuild.sh backfill-archive
```

## Full nuke-and-rebuild
If the graph is too far gone and you want a clean rebuild after backup:
```bash
bash scripts/engram-rebuild.sh full-cycle
```

Or manually:
```bash
bash scripts/engram-rebuild.sh backup
bash scripts/engram-rebuild.sh reset-full
bash scripts/engram-rebuild.sh rebuild
```

## Dry-run examples
Archive-only preview:
```bash
source ~/clawd/.venv-memory/bin/activate
python ~/clawd/engram/reset_neo4j.py archive-only --dry-run
```

Delete only `exported_session` source-type nodes:
```bash
python ~/clawd/engram/reset_neo4j.py by-source-type --source-type exported_session --dry-run
```

## Notes

- `archive-only` cleanup targets nodes with:
  - `memory_tier = 'archive'`
  - or `source_type = 'exported_session'`
- Cleanup also removes orphan `Entity` nodes afterward.
- Normal ingest remains classified/hygienic without blindly deleting the database every time.
