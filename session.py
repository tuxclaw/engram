#!/usr/bin/env python3
"""
Engram Session State Manager — Solve the cold-boot problem.

Captures:
  - What was being worked on
  - Open threads and unresolved questions
  - Conversational mood/tone
  - Key entities referenced in the session

Restores:
  - Injects last session context into startup briefing
  - Links session to relevant graph entities

Usage:
  python engram/session.py save --summary "..." --threads "..." --mood "..."
  python engram/session.py restore   # Get last session state
  python engram/session.py list      # List recent sessions
"""

import json
import os
import sys
import hashlib
from datetime import datetime
from typing import Optional

import kuzu

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from engram.backend import get_db, get_conn


def generate_session_id(session_key: str) -> str:
    """Generate deterministic session ID."""
    h = hashlib.sha256(f"{session_key}:{datetime.now().isoformat()}".encode()).hexdigest()[:12]
    return f"sess_{h}"


def save_session_state(conn: kuzu.Connection, session_key: str, summary: str,
                       open_threads: str = "", mood: str = "neutral",
                       message_count: int = 0, entity_names: list[str] = None) -> str:
    """Save session state to the graph."""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    sid = generate_session_id(session_key)
    
    try:
        conn.execute(
            "MERGE (s:SessionState {id: $p_id}) "
            "SET s.session_key = $p_sk, "
            "s.summary = $p_sum, "
            "s.open_threads = $p_threads, "
            "s.mood = $p_mood, "
            "s.message_count = $p_mc, "
            "s.ended_at = timestamp($p_now), "
            "s.created_at = CASE WHEN s.created_at IS NULL THEN timestamp($p_now) ELSE s.created_at END",
            {
                "p_id": sid,
                "p_sk": session_key,
                "p_sum": summary,
                "p_threads": open_threads,
                "p_mood": mood,
                "p_mc": message_count,
                "p_now": now_str
            }
        )
        
        # Link to referenced entities
        if entity_names:
            for name in entity_names:
                try:
                    conn.execute(
                        "MATCH (s:SessionState {id: $p_sid}), (e:Entity) "
                        "WHERE lower(e.name) = lower($p_name) "
                        "MERGE (s)-[:SESSION_REFS {relevance: 0.8, "
                        "created_at: timestamp($p_now)}]->(e)",
                        {"p_sid": sid, "p_name": name, "p_now": now_str}
                    )
                except Exception:
                    pass
        
        print(f"✅ Session state saved: {sid}")
        return sid
        
    except Exception as e:
        print(f"❌ Failed to save session state: {e}")
        return ""


def get_last_session(conn: kuzu.Connection) -> Optional[dict]:
    """Get the most recent session state."""
    try:
        result = conn.execute(
            "MATCH (s:SessionState) "
            "RETURN s.id, s.session_key, s.summary, s.open_threads, "
            "s.mood, s.message_count, s.ended_at "
            "ORDER BY s.ended_at DESC LIMIT 1"
        )
        if result.has_next():
            row = result.get_next()
            session = {
                "id": row[0], "session_key": row[1], "summary": row[2],
                "open_threads": row[3], "mood": row[4],
                "message_count": row[5], "ended_at": str(row[6])
            }
            
            # Get referenced entities
            try:
                ref_result = conn.execute(
                    "MATCH (s:SessionState {id: $p_id})-[:SESSION_REFS]->(e:Entity) "
                    "RETURN e.name, e.entity_type",
                    {"p_id": session["id"]}
                )
                session["entities"] = []
                while ref_result.has_next():
                    ref_row = ref_result.get_next()
                    session["entities"].append({"name": ref_row[0], "type": ref_row[1]})
            except Exception:
                session["entities"] = []
            
            return session
    except Exception as e:
        print(f"Error getting last session: {e}")
    return None


def list_sessions(conn: kuzu.Connection, limit: int = 10) -> list[dict]:
    """List recent session states."""
    sessions = []
    try:
        result = conn.execute(
            "MATCH (s:SessionState) "
            "RETURN s.id, s.session_key, s.summary, s.mood, s.ended_at, s.message_count "
            "ORDER BY s.ended_at DESC LIMIT $p_lim",
            {"p_lim": limit}
        )
        while result.has_next():
            row = result.get_next()
            sessions.append({
                "id": row[0], "session_key": row[1], "summary": row[2],
                "mood": row[3], "ended_at": str(row[4]), "message_count": row[5]
            })
    except Exception as e:
        print(f"Error listing sessions: {e}")
    return sessions


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="Engram Session State Manager")
    subparsers = parser.add_subparsers(dest="command")
    
    # Save command
    save_parser = subparsers.add_parser("save", help="Save session state")
    save_parser.add_argument("--key", default="main", help="Session key")
    save_parser.add_argument("--summary", required=True, help="Session summary")
    save_parser.add_argument("--threads", default="", help="Open threads")
    save_parser.add_argument("--mood", default="neutral", help="Session mood")
    save_parser.add_argument("--messages", type=int, default=0, help="Message count")
    save_parser.add_argument("--entities", nargs="*", help="Referenced entity names")
    
    # Restore command
    restore_parser = subparsers.add_parser("restore", help="Get last session state")
    restore_parser.add_argument("--json", "-j", action="store_true")
    
    # List command
    list_parser = subparsers.add_parser("list", help="List recent sessions")
    list_parser.add_argument("--limit", type=int, default=10)
    
    args = parser.parse_args()
    
    db = get_db(read_only=(args.command != "save"))
    conn = get_conn(db)
    
    if args.command == "save":
        save_session_state(
            conn, args.key, args.summary,
            open_threads=args.threads,
            mood=args.mood,
            message_count=args.messages,
            entity_names=args.entities or []
        )
    
    elif args.command == "restore":
        session = get_last_session(conn)
        if session:
            if hasattr(args, 'json') and args.json:
                print(json.dumps(session, indent=2, default=str))
            else:
                print(f"📋 Last Session: {session.get('ended_at', 'unknown')[:16]}")
                print(f"   Summary: {session.get('summary', '(none)')}")
                print(f"   Open threads: {session.get('open_threads', '(none)')}")
                print(f"   Mood: {session.get('mood', 'neutral')}")
                if session.get("entities"):
                    ents = ", ".join(e["name"] for e in session["entities"])
                    print(f"   Key entities: {ents}")
        else:
            print("No previous session found.")
    
    elif args.command == "list":
        sessions = list_sessions(conn, args.limit)
        if sessions:
            for s in sessions:
                date = s.get("ended_at", "")[:16]
                mood = s.get("mood", "")
                summary = s.get("summary", "")[:80]
                print(f"  [{date}] ({mood}) {summary}")
        else:
            print("No sessions recorded.")
    
    else:
        parser.print_help()
