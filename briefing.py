#!/usr/bin/env python3
"""
Engram Session Briefing Engine — Wake up remembering.

Generates a dense, targeted briefing for session startup.
Pulls from the knowledge graph to create context that feels
like memory, not a database dump.

Output: A markdown briefing suitable for injection into
the agent's system prompt or workspace context.

Usage:
  python engram/briefing.py              # Generate briefing to stdout
  python engram/briefing.py --save       # Save to BRIEFING.md
  python engram/briefing.py --json       # Output as JSON
"""

import json
import os
import sys
from datetime import datetime, timedelta
from typing import Optional

import kuzu

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from engram.backend import get_db, get_conn, get_stats

ENGRAM_DIR = os.path.dirname(os.path.abspath(__file__))
BRIEFING_PATH = os.environ.get("ENGRAM_BRIEFING_PATH", os.path.join(ENGRAM_DIR, "..", "BRIEFING.md"))
LAST_BRIEFING_TS_PATH = os.path.join(ENGRAM_DIR, ".last_briefing_ts")


def get_recent_episodes(conn: kuzu.Connection, days: int = 3, limit: int = 15) -> list[dict]:
    """Get recent episodes, ordered by recency."""
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    results = []
    try:
        result = conn.execute(
            "MATCH (ep:Episode) "
            "WHERE ep.occurred_at >= timestamp($p_cutoff) "
            "RETURN ep.summary, ep.source_file, ep.occurred_at, ep.importance "
            "ORDER BY ep.occurred_at DESC LIMIT $p_lim",
            {"p_cutoff": cutoff, "p_lim": limit}
        )
        while result.has_next():
            row = result.get_next()
            results.append({
                "summary": row[0], "source_file": row[1],
                "occurred_at": str(row[2]), "importance": row[3]
            })
    except Exception as e:
        print(f"  Warning: recent episodes query failed: {e}", file=sys.stderr)
    return results


def get_high_importance_entities(conn: kuzu.Connection, limit: int = 15) -> list[dict]:
    """Get entities with highest importance or most connections."""
    results = []
    try:
        # Get entities sorted by connection count (proxy for importance)
        result = conn.execute(
            "MATCH (e:Entity)-[r]-() "
            "WITH e, count(r) AS connections "
            "RETURN e.name, e.entity_type, e.description, e.importance, connections "
            "ORDER BY connections DESC LIMIT $p_lim",
            {"p_lim": limit}
        )
        while result.has_next():
            row = result.get_next()
            results.append({
                "name": row[0], "type": row[1], "description": row[2],
                "importance": row[3], "connections": row[4]
            })
    except Exception as e:
        print(f"  Warning: entity importance query failed: {e}", file=sys.stderr)
    return results


def get_recent_facts(conn: kuzu.Connection, days: int = 7, limit: int = 20) -> list[dict]:
    """Get recently created/updated facts."""
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    results = []
    try:
        result = conn.execute(
            "MATCH (f:Fact) "
            "WHERE f.valid_at >= timestamp($p_cutoff) "
            "RETURN f.content, f.category, f.confidence, f.valid_at "
            "ORDER BY f.valid_at DESC LIMIT $p_lim",
            {"p_cutoff": cutoff, "p_lim": limit}
        )
        while result.has_next():
            row = result.get_next()
            results.append({
                "content": row[0], "category": row[1],
                "confidence": row[2], "valid_at": str(row[3])
            })
    except Exception as e:
        print(f"  Warning: recent facts query failed: {e}", file=sys.stderr)
    return results


def get_active_emotions(conn: kuzu.Connection, limit: int = 10) -> list[dict]:
    """Get recent emotional context."""
    results = []
    try:
        result = conn.execute(
            "MATCH (ep:Episode)-[:EPISODE_EVOKES]->(em:Emotion) "
            "RETURN em.label, em.valence, em.arousal, em.description, ep.occurred_at "
            "ORDER BY ep.occurred_at DESC LIMIT $p_lim",
            {"p_lim": limit}
        )
        while result.has_next():
            row = result.get_next()
            results.append({
                "label": row[0], "valence": row[1], "arousal": row[2],
                "context": row[3], "when": str(row[4])
            })
    except Exception as e:
        print(f"  Warning: emotions query failed: {e}", file=sys.stderr)
    return results


def get_last_session(conn: kuzu.Connection) -> Optional[dict]:
    """Get the most recent session state snapshot."""
    try:
        result = conn.execute(
            "MATCH (s:SessionState) "
            "RETURN s.summary, s.open_threads, s.mood, s.ended_at "
            "ORDER BY s.ended_at DESC LIMIT 1"
        )
        if result.has_next():
            row = result.get_next()
            return {
                "summary": row[0], "open_threads": row[1],
                "mood": row[2], "ended_at": str(row[3])
            }
    except Exception:
        pass
    return None


def get_open_threads(conn: kuzu.Connection) -> list[dict]:
    """Infer open threads from recent high-importance facts and episodes."""
    results = []
    try:
        # Look for recent episodes that suggest ongoing work
        result = conn.execute(
            "MATCH (ep:Episode) "
            "WHERE ep.importance >= 0.5 "
            "RETURN ep.summary, ep.source_file, ep.occurred_at "
            "ORDER BY ep.occurred_at DESC LIMIT 5"
        )
        while result.has_next():
            row = result.get_next()
            results.append({
                "summary": row[0], "source": row[1], "when": str(row[2])
            })
    except Exception:
        pass
    return results


def generate_briefing(conn: kuzu.Connection) -> str:
    """Generate a session briefing from the knowledge graph."""
    stats = get_stats(conn)
    
    # Gather all context
    recent_episodes = get_recent_episodes(conn, days=7, limit=10)
    key_entities = get_high_importance_entities(conn, limit=10)
    recent_facts = get_recent_facts(conn, days=14, limit=15)
    emotions = get_active_emotions(conn, limit=8)
    last_session = get_last_session(conn)
    
    # Build the briefing
    lines = []
    lines.append("# 🧠 Engram Session Briefing")
    lines.append(f"*Generated: {datetime.now().strftime('%Y-%m-%d %H:%M PST')}*")
    lines.append(f"*Graph: {sum(stats.get(t, 0) for t in ['Entity','Episode','Emotion','SessionState','Fact'])} nodes, "
                 f"{sum(v for k, v in stats.items() if k not in ['Entity','Episode','Emotion','SessionState','Fact'])} relationships*")
    lines.append("")
    
    # Last session context
    if last_session:
        lines.append("## Last Session")
        if last_session.get("ended_at"):
            lines.append(f"Ended: {last_session['ended_at'][:16]}")
        if last_session.get("summary"):
            lines.append(f"{last_session['summary']}")
        if last_session.get("open_threads"):
            lines.append(f"**Open threads:** {last_session['open_threads']}")
        if last_session.get("mood"):
            lines.append(f"**Mood:** {last_session['mood']}")
        lines.append("")
    
    # Recent activity
    if recent_episodes:
        lines.append("## Recent Activity")
        seen = set()
        for ep in recent_episodes:
            summary = ep.get("summary", "")
            if not summary or summary in seen:
                continue
            seen.add(summary)
            date = ep.get("occurred_at", "")[:10]
            # Truncate long summaries
            if len(summary) > 150:
                summary = summary[:147] + "..."
            lines.append(f"- **[{date}]** {summary}")
        lines.append("")
    
    # Key entities (who/what matters most)
    if key_entities:
        lines.append("## Key Entities")
        for ent in key_entities[:8]:
            desc = ent.get("description", "")
            if len(desc) > 100:
                desc = desc[:97] + "..."
            lines.append(f"- **{ent['name']}** ({ent['type']}) — {desc} [{ent['connections']} connections]")
        lines.append("")
    
    # Recent facts
    if recent_facts:
        lines.append("## Recent Knowledge")
        seen = set()
        for fact in recent_facts:
            content = fact.get("content", "")
            if not content or content in seen:
                continue
            seen.add(content)
            cat = fact.get("category", "")
            # Clean up multi-category entries
            if "|" in cat:
                cat = cat.split("|")[0]
            lines.append(f"- [{cat}] {content}")
        lines.append("")
    
    # Emotional context
    if emotions:
        lines.append("## Emotional Context")
        seen = set()
        for em in emotions:
            label = em.get("label", "")
            ctx = em.get("context", "")
            key = f"{label}:{ctx}"
            if key in seen:
                continue
            seen.add(key)
            valence = "+" if em.get("valence", 0) > 0 else "-" if em.get("valence", 0) < 0 else "~"
            lines.append(f"- {label} ({valence}) — {ctx}")
        lines.append("")
    
    return "\n".join(lines)


def _load_last_briefing_ts() -> Optional[datetime]:
    """Load last briefing timestamp from disk."""
    if not os.path.exists(LAST_BRIEFING_TS_PATH):
        return None
    try:
        raw = ""
        with open(LAST_BRIEFING_TS_PATH, "r") as f:
            raw = f.read().strip()
        if not raw:
            return None
        return datetime.fromisoformat(raw)
    except Exception:
        return None


def _save_last_briefing_ts(ts: datetime) -> None:
    """Persist last briefing timestamp to disk."""
    try:
        with open(LAST_BRIEFING_TS_PATH, "w") as f:
            f.write(ts.isoformat())
    except Exception as e:
        print(f"  Warning: failed to save last briefing timestamp: {e}", file=sys.stderr)


def generate_delta_briefing(conn: kuzu.Connection) -> str:
    """Generate a delta briefing since the last briefing."""
    last_ts = _load_last_briefing_ts()
    now = datetime.now()

    if not last_ts:
        briefing = generate_briefing(conn)
        _save_last_briefing_ts(now)
        return briefing

    results = {"facts": [], "episodes": [], "entities": []}

    try:
        result = conn.execute(
            "MATCH (f:Fact) "
            "WHERE f.created_at > timestamp($p_last) "
            "RETURN f.content, f.category, f.created_at "
            "ORDER BY f.created_at DESC LIMIT 25",
            {"p_last": last_ts.isoformat()}
        )
        while result.has_next():
            row = result.get_next()
            results["facts"].append({
                "content": row[0], "category": row[1], "created_at": str(row[2])
            })
    except Exception as e:
        print(f"  Warning: delta facts query failed: {e}", file=sys.stderr)

    try:
        result = conn.execute(
            "MATCH (ep:Episode) "
            "WHERE ep.occurred_at > timestamp($p_last) "
            "RETURN ep.summary, ep.source_file, ep.occurred_at "
            "ORDER BY ep.occurred_at DESC LIMIT 25",
            {"p_last": last_ts.isoformat()}
        )
        while result.has_next():
            row = result.get_next()
            results["episodes"].append({
                "summary": row[0], "source_file": row[1], "occurred_at": str(row[2])
            })
    except Exception as e:
        print(f"  Warning: delta episodes query failed: {e}", file=sys.stderr)

    try:
        result = conn.execute(
            "MATCH (e:Entity) "
            "WHERE e.created_at > timestamp($p_last) "
            "RETURN e.name, e.entity_type, e.description, e.created_at "
            "ORDER BY e.created_at DESC LIMIT 25",
            {"p_last": last_ts.isoformat()}
        )
        while result.has_next():
            row = result.get_next()
            results["entities"].append({
                "name": row[0], "type": row[1], "description": row[2], "created_at": str(row[3])
            })
    except Exception as e:
        print(f"  Warning: delta entities query failed: {e}", file=sys.stderr)

    lines = []
    lines.append("# 🧠 Delta Briefing — What's New")
    lines.append(f"*Since: {last_ts.strftime('%Y-%m-%d %H:%M')}*")
    lines.append(f"*Generated: {now.strftime('%Y-%m-%d %H:%M PST')}*")
    lines.append("")

    lines.append("## New Facts")
    if results["facts"]:
        seen = set()
        for fact in results["facts"]:
            content = fact.get("content", "")
            if not content or content in seen:
                continue
            seen.add(content)
            cat = fact.get("category", "")
            if "|" in cat:
                cat = cat.split("|")[0]
            lines.append(f"- [{cat}] {content}")
    else:
        lines.append("- None")
    lines.append("")

    lines.append("## New Episodes")
    if results["episodes"]:
        seen = set()
        for ep in results["episodes"]:
            summary = ep.get("summary", "")
            if not summary or summary in seen:
                continue
            seen.add(summary)
            date = ep.get("occurred_at", "")[:10]
            if len(summary) > 150:
                summary = summary[:147] + "..."
            lines.append(f"- **[{date}]** {summary}")
    else:
        lines.append("- None")
    lines.append("")

    lines.append("## New Entities")
    if results["entities"]:
        for ent in results["entities"]:
            desc = ent.get("description", "")
            if len(desc) > 100:
                desc = desc[:97] + "..."
            lines.append(f"- **{ent['name']}** ({ent['type']}) — {desc}")
    else:
        lines.append("- None")
    lines.append("")

    _save_last_briefing_ts(now)
    return "\n".join(lines)


def save_briefing(briefing: str):
    """Save briefing to workspace."""
    with open(BRIEFING_PATH, "w") as f:
        f.write(briefing)
    print(f"💾 Briefing saved to {BRIEFING_PATH}", file=sys.stderr)


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="Engram Session Briefing")
    parser.add_argument("--save", action="store_true", help="Save to BRIEFING.md")
    parser.add_argument("--json", "-j", action="store_true", help="Output as JSON")
    args = parser.parse_args()
    
    db = get_db(read_only=True)
    conn = get_conn(db)
    
    briefing = generate_briefing(conn)
    
    if args.json:
        print(json.dumps({"briefing": briefing, "generated_at": datetime.now().isoformat()}))
    else:
        print(briefing)
    
    if args.save:
        save_briefing(briefing)
