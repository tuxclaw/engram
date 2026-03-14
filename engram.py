#!/usr/bin/env python3
"""
Engram CLI — The interface to Jarvis's mind.

Usage:
  engram search <query>           Search across all memory types
  engram entity <name>            Get full context for an entity
  engram briefing                 Generate session startup briefing
  engram stats                    Show graph statistics
  engram health                   Health report
  engram dream                    Run dream consolidation
  engram ingest [--file FILE]     Ingest memory files
  engram session save             Save session state
  engram session restore          Restore last session state
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    
    command = sys.argv[1]
    
    if command == "search":
        from engram.query import unified_search, print_results
        from engram.backend import get_db, get_conn
        if len(sys.argv) < 3:
            print("Usage: engram search <query>")
            return
        query = " ".join(sys.argv[2:])
        db = get_db(read_only=True)
        conn = get_conn(db)
        results = unified_search(conn, query)
        print_results(results)
    
    elif command == "entity":
        from engram.query import get_entity_context, print_entity_context
        from engram.backend import get_db, get_conn
        if len(sys.argv) < 3:
            print("Usage: engram entity <name>")
            return
        name = " ".join(sys.argv[2:])
        db = get_db(read_only=True)
        conn = get_conn(db)
        context = get_entity_context(conn, name)
        print_entity_context(context)
    
    elif command == "briefing":
        from engram.briefing import generate_briefing, save_briefing
        from engram.backend import get_db, get_conn
        db = get_db(read_only=True)
        conn = get_conn(db)
        briefing = generate_briefing(conn)
        print(briefing)
        if "--save" in sys.argv:
            save_briefing(briefing)
    
    elif command == "stats":
        from engram.backend import get_db, get_conn, get_stats, print_stats
        db = get_db(read_only=True)
        conn = get_conn(db)
        stats = get_stats(conn)
        print_stats(stats)
    
    elif command == "health":
        from engram.consolidate import health_report
        from engram.backend import get_db, get_conn
        import json
        db = get_db(read_only=True)
        conn = get_conn(db)
        report = health_report(conn)
        print(json.dumps(report, indent=2, default=str))
    
    elif command == "dream":
        from engram.consolidate import consolidate
        dry_run = "--dry-run" in sys.argv
        consolidate(dry_run=dry_run)
    
    elif command == "ingest":
        if "--file" in sys.argv:
            idx = sys.argv.index("--file")
            if idx + 1 < len(sys.argv):
                from engram.ingest import ingest_file
                from engram.backend import get_db, get_conn, init_schema, get_stats, print_stats
                from pathlib import Path
                db = get_db()
                conn = get_conn(db)
                init_schema(conn)
                ingest_file(conn, Path(sys.argv[idx + 1]))
                stats = get_stats(conn)
                print_stats(stats)
        else:
            from engram.ingest import ingest_all
            ingest_all()
    
    elif command == "session":
        if len(sys.argv) < 3:
            print("Usage: engram session save|restore|list")
            return
        subcmd = sys.argv[2]
        from engram.backend import get_db, get_conn
        
        if subcmd == "save":
            from engram.session import save_session_state
            db = get_db()
            conn = get_conn(db)
            # Parse args
            summary = ""
            threads = ""
            mood = "neutral"
            entities = []
            i = 3
            while i < len(sys.argv):
                if sys.argv[i] == "--summary" and i + 1 < len(sys.argv):
                    summary = sys.argv[i + 1]; i += 2
                elif sys.argv[i] == "--threads" and i + 1 < len(sys.argv):
                    threads = sys.argv[i + 1]; i += 2
                elif sys.argv[i] == "--mood" and i + 1 < len(sys.argv):
                    mood = sys.argv[i + 1]; i += 2
                elif sys.argv[i] == "--entities" and i + 1 < len(sys.argv):
                    entities = sys.argv[i + 1].split(","); i += 2
                else:
                    i += 1
            save_session_state(conn, "main", summary, threads, mood, entity_names=entities)
        
        elif subcmd == "restore":
            from engram.session import get_last_session
            db = get_db(read_only=True)
            conn = get_conn(db)
            session = get_last_session(conn)
            if session:
                print(f"📋 Last Session: {session.get('ended_at', 'unknown')[:16]}")
                print(f"   Summary: {session.get('summary', '(none)')}")
                print(f"   Open threads: {session.get('open_threads', '(none)')}")
                print(f"   Mood: {session.get('mood', 'neutral')}")
            else:
                print("No previous session found.")
        
        elif subcmd == "list":
            from engram.session import list_sessions
            db = get_db(read_only=True)
            conn = get_conn(db)
            sessions = list_sessions(conn)
            for s in sessions:
                date = s.get("ended_at", "")[:16]
                print(f"  [{date}] ({s.get('mood', '')}) {s.get('summary', '')[:80]}")
    
    else:
        print(f"Unknown command: {command}")
        print(__doc__)


if __name__ == "__main__":
    main()
