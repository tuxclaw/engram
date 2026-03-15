#!/usr/bin/env python3
"""Seed scoped pinned facts into Engram.

Examples:
  python scripts/seed_scoped_pinned.py \
    --agent main \
    --scope-type channel \
    --scope-id 1477540685002440796 \
    --category channel_rule \
    --fact "In Lady2good's channel, reply with a warm, natural, personal tone instead of operator or task-manager voice." \
    --fact "In Lady2good's channel, avoid sounding automated, templated, or therapy-scripted."
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from engram.backend import get_db, get_conn  # type: ignore
from engram.ingest import generate_id  # type: ignore


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed scoped pinned facts")
    parser.add_argument("--agent", default="main")
    parser.add_argument("--scope-type", choices=["global", "channel", "session"], default="global")
    parser.add_argument("--scope-id", default=None)
    parser.add_argument("--category", default="channel_rule")
    parser.add_argument("--source-type", default="live_context")
    parser.add_argument("--importance", type=float, default=0.95)
    parser.add_argument("--confidence", type=float, default=0.95)
    parser.add_argument("--source-episode", default="seed_scoped_pinned")
    parser.add_argument("--fact", action="append", required=True, help="Repeat for each fact to store")
    args = parser.parse_args()

    if args.scope_type != "global" and not args.scope_id:
        raise SystemExit("--scope-id is required for channel/session scope")

    conn = get_conn(get_db(read_only=False))
    now = datetime.now()
    stored = []

    for content in args.fact:
        content = str(content).strip()
        if not content:
            continue
        scope_suffix = f"_{args.scope_type}_{args.scope_id or 'global'}"
        fact_id = generate_id("fact", content + "_" + args.agent + scope_suffix)
        conn.execute(
            "MERGE (f:Fact {id: $p_id}) "
            "SET f.content = $p_content, "
            "f.category = $p_cat, "
            "f.confidence = $p_conf, "
            "f.importance = $p_imp, "
            "f.valid_at = $p_now, "
            "f.created_at = CASE WHEN f.created_at IS NULL THEN $p_now ELSE f.created_at END, "
            "f.updated_at = $p_now, "
            "f.source_episode = $p_src, "
            "f.agent_id = $p_agent, "
            "f.source_type = $p_source_type, "
            "f.memory_tier = 'candidate', "
            "f.quality_score = 0.95, "
            "f.contamination_score = 0.0, "
            "f.retrievable = true, "
            "f.is_candidate = true, "
            "f.is_canonical = false, "
            "f.scope_type = $p_scope_type, "
            "f.scope_id = $p_scope_id",
            {
                "p_id": fact_id,
                "p_content": content,
                "p_cat": args.category,
                "p_conf": args.confidence,
                "p_imp": args.importance,
                "p_now": now,
                "p_src": args.source_episode,
                "p_agent": args.agent,
                "p_source_type": args.source_type,
                "p_scope_type": args.scope_type,
                "p_scope_id": None if args.scope_type == "global" else args.scope_id,
            },
        )
        stored.append({
            "id": fact_id,
            "content": content,
            "agent": args.agent,
            "scope_type": args.scope_type,
            "scope_id": None if args.scope_type == "global" else args.scope_id,
        })

    print(json.dumps({"ok": True, "stored": stored}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
