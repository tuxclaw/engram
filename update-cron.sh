#!/bin/bash
# Update Engram cron schedule from config.json
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CONFIG="$SCRIPT_DIR/config.json"

if [ ! -f "$CONFIG" ]; then
  echo "❌ config.json not found at $CONFIG"
  exit 1
fi

INTERVAL=$(python3 -c "import json; print(json.load(open('$CONFIG')).get('ingest_interval_minutes', 60))")
echo "Setting Engram ingest interval to every ${INTERVAL} minutes"

# Build cron expression
if [ "$INTERVAL" -lt 60 ]; then
  CRON_EXPR="*/${INTERVAL} * * * *"
else
  HOURS=$((INTERVAL / 60))
  if [ "$HOURS" -ge 24 ]; then
    CRON_EXPR="0 3 * * *"  # Once daily at 3am
  else
    CRON_EXPR="0 */${HOURS} * * *"
  fi
fi

# Remove old engram ingest cron, add new one
crontab -l 2>/dev/null | grep -v "engram.*ingest\|engram.*briefing" > /tmp/crontab.tmp || true
WORKSPACE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
echo "${CRON_EXPR} cd ${WORKSPACE_DIR} && .venv-memory/bin/python engram/export_sessions.py >> /tmp/engram-export.log 2>&1 && .venv-memory/bin/python engram/engram.py ingest >> /tmp/engram-ingest.log 2>&1 && .venv-memory/bin/python engram/engram.py briefing > BRIEFING.md 2>&1" >> /tmp/crontab.tmp
crontab /tmp/crontab.tmp
rm /tmp/crontab.tmp

echo "✅ Cron updated: ${CRON_EXPR}"
echo "   Pipeline: export sessions → ingest → update briefing"
