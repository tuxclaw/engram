#!/usr/bin/env python3
"""
Engram Batch Extractor — Scans recent session JSONL logs and runs LLM extraction
on user messages that haven't been processed yet.

Designed to run periodically (e.g. every 30 min) to catch messages that were
missed by the real-time afterTurn hook (gateway restarts, errors, etc.).

Usage:
  python engram/batch_extract.py [--hours 1] [--agent main] [--dry-run]
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engram.context_query import (
    extract_and_store_llm,
    _strip_envelope,
    _is_noise,
    _mostly_non_alpha,
    LIVE_MIN_CHARS,
)


def find_session_files(agents_dir: str, max_age_hours: float = 1.0) -> list[Path]:
    """Find session JSONL files modified within the last N hours."""
    cutoff = time.time() - (max_age_hours * 3600)
    results = []
    agents_path = Path(agents_dir)
    if not agents_path.exists():
        return results
    for agent_dir in agents_path.iterdir():
        if not agent_dir.is_dir():
            continue
        sessions_dir = agent_dir / "sessions"
        if not sessions_dir.exists():
            continue
        for f in sessions_dir.glob("*.jsonl"):
            try:
                if f.stat().st_mtime >= cutoff:
                    results.append(f)
            except OSError:
                continue
    return results


def extract_user_messages(session_file: Path, max_age_hours: float = 1.0) -> list[dict]:
    """Extract user messages from a session JSONL file."""
    cutoff_ts = (datetime.now() - timedelta(hours=max_age_hours)).isoformat()
    messages = []
    try:
        with open(session_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                msg = entry.get("message", {})
                if msg.get("role") != "user":
                    continue
                # Check timestamp if available
                ts = entry.get("timestamp", "")
                if ts and ts < cutoff_ts:
                    continue
                content = ""
                if isinstance(msg.get("content"), str):
                    content = msg["content"]
                elif isinstance(msg.get("content"), list):
                    content = "\n".join(
                        p.get("text", "") for p in msg["content"]
                        if isinstance(p, dict) and p.get("text")
                    )
                content = content.strip()
                if not content or len(content) < LIVE_MIN_CHARS:
                    continue
                # Strip envelope and check quality
                clean = _strip_envelope(content)
                if not clean or len(clean) < LIVE_MIN_CHARS:
                    continue
                if _mostly_non_alpha(clean) or _is_noise(clean):
                    continue
                # Extract agent ID from path
                agent_id = session_file.parent.parent.name
                session_id = session_file.stem
                messages.append({
                    "text": content,
                    "agent_id": agent_id,
                    "session_id": session_id,
                })
    except Exception as e:
        print(f"[batch_extract] Error reading {session_file}: {e}", file=sys.stderr)
    return messages


def main():
    parser = argparse.ArgumentParser(description="Engram Batch Extractor")
    parser.add_argument("--hours", type=float, default=1.0,
                        help="Look back this many hours (default: 1)")
    parser.add_argument("--agent", type=str, default=None,
                        help="Only process this agent (default: all)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be processed without storing")
    parser.add_argument("--agents-dir", type=str,
                        default=os.path.join(os.environ.get("HOME", ""), ".openclaw", "agents"),
                        help="Path to agents directory")
    args = parser.parse_args()

    session_files = find_session_files(args.agents_dir, args.hours)
    if args.agent:
        session_files = [f for f in session_files if f.parent.parent.name == args.agent]

    total_messages = 0
    total_stored = 0
    total_skipped = 0
    total_errors = 0

    print(f"[batch_extract] Scanning {len(session_files)} session files (last {args.hours}h)")

    for sf in session_files:
        messages = extract_user_messages(sf, args.hours)
        if not messages:
            continue

        agent_id = sf.parent.parent.name
        session_id = sf.stem
        print(f"[batch_extract] {agent_id}/{session_id}: {len(messages)} messages")

        for msg in messages:
            total_messages += 1
            if args.dry_run:
                preview = msg["text"][:80].replace("\n", " ")
                print(f"  [DRY-RUN] Would extract: {preview}...")
                continue

            try:
                result = extract_and_store_llm(
                    msg["text"],
                    agent_id=msg["agent_id"],
                    session_id=msg["session_id"],
                    role="user",
                )
                stored = result.get("stored", 0)
                total_stored += stored
                if stored == 0:
                    total_skipped += 1
                # Small delay between extractions to not overwhelm Ollama
                if stored > 0:
                    time.sleep(0.5)
            except Exception as e:
                total_errors += 1
                print(f"  [ERROR] {e}", file=sys.stderr)

    summary = {
        "ok": True,
        "session_files_scanned": len(session_files),
        "messages_processed": total_messages,
        "facts_stored": total_stored,
        "messages_skipped": total_skipped,
        "errors": total_errors,
        "dry_run": args.dry_run,
    }
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
