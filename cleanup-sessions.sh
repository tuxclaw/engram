#!/bin/bash
# Prune stale openai/embedding sessions from OpenClaw sessions.json
# Safe to run at any time — keeps discord, telegram, cron, main, webchat sessions

SESSIONS_FILE="$HOME/.openclaw/agents/main/sessions/sessions.json"

if [ ! -f "$SESSIONS_FILE" ]; then
    echo "sessions.json not found"
    exit 1
fi

BEFORE=$(python3 -c "import json; d=json.load(open('$SESSIONS_FILE')); print(len(d))")
SIZE_BEFORE=$(du -sh "$SESSIONS_FILE" | cut -f1)

python3 -c "
import json, shutil, sys
from datetime import datetime, timezone, timedelta

path = '$SESSIONS_FILE'
shutil.copy(path, path + '.bak')

with open(path) as f:
    data = json.load(f)

# Keep everything except openai sessions (created by Engram API calls)
# Also prune cron sessions older than 7 days
cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).timestamp() * 1000

cleaned = {}
for k, v in data.items():
    parts = k.split(':')
    session_type = parts[2] if len(parts) >= 3 else ''
    if session_type == 'openai':
        continue  # always remove
    if session_type == 'cron':
        updated_at = v.get('updatedAt', 0)
        if updated_at < cutoff:
            continue  # prune old cron sessions
    cleaned[k] = v

with open(path, 'w') as f:
    json.dump(cleaned, f)

print(f'Cleaned: {len(data)} -> {len(cleaned)} entries')
"

AFTER=$(python3 -c "import json; d=json.load(open('$SESSIONS_FILE')); print(len(d))")
SIZE_AFTER=$(du -sh "$SESSIONS_FILE" | cut -f1)
echo "sessions.json: $BEFORE entries ($SIZE_BEFORE) → $AFTER entries ($SIZE_AFTER)"
