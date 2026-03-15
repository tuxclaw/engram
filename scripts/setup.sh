#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$ROOT/.venv-memory"
CONFIG_EXAMPLE="$ROOT/config.json.example"
CONFIG_FILE="$ROOT/config.json"

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "Missing required command: $1" >&2
    exit 1
  }
}

need_cmd python3

if ! command -v docker >/dev/null 2>&1; then
  echo "Warning: docker not found. Neo4j/docker-compose startup will be skipped."
  HAVE_DOCKER=0
else
  HAVE_DOCKER=1
fi

echo "==> Engram setup starting"
echo "Repo: $ROOT"

if [ ! -d "$VENV" ]; then
  echo "==> Creating virtualenv at $VENV"
  python3 -m venv "$VENV"
else
  echo "==> Reusing existing virtualenv at $VENV"
fi

# shellcheck disable=SC1091
source "$VENV/bin/activate"

echo "==> Installing Python dependencies"
pip install --upgrade pip >/dev/null
pip install -r "$ROOT/requirements.txt"
pip install neo4j >/dev/null

if [ ! -f "$CONFIG_FILE" ]; then
  echo "==> Creating config.json from config.json.example"
  cp "$CONFIG_EXAMPLE" "$CONFIG_FILE"
  echo "Created $CONFIG_FILE"
else
  echo "==> Leaving existing config.json in place"
fi

if [ "$HAVE_DOCKER" -eq 1 ]; then
  if command -v docker-compose >/dev/null 2>&1; then
    echo "==> Starting services with docker-compose"
    (cd "$ROOT" && docker-compose up -d)
  elif docker compose version >/dev/null 2>&1; then
    echo "==> Starting services with docker compose"
    (cd "$ROOT" && docker compose up -d)
  else
    echo "Warning: docker is installed but docker compose is unavailable. Start Neo4j manually."
  fi
fi

cat <<EOF

Setup complete.

Next steps:
1. Edit $CONFIG_FILE
   - set neo4j.password
   - set xai_api_key
   - verify context_engine paths
2. Activate the venv when working locally:
   source "$VENV/bin/activate"
3. Test memory query path:
   python "$ROOT/context_query.py" query "test" --agent main --json
4. If you want automated ingest scheduling:
   bash "$ROOT/update-cron.sh"

Optional:
- Seed scoped pinned facts:
  python "$ROOT/scripts/seed_scoped_pinned.py" --help
EOF
