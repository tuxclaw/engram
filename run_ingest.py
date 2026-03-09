#!/usr/bin/env python3
"""
Engram batch ingest runner — processes all memory files sequentially.
Designed to run as a pm2 managed process.
"""
import os
import sys
import time
import glob

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engram.schema import get_db, get_conn, init_schema, get_stats, print_stats
from engram.ingest import find_memory_files, ingest_file, get_processed_files, save_processed_files
from pathlib import Path

MEMORY_DIR = Path(os.environ.get("ENGRAM_MEMORY_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "exported-sessions")))

def main():
    db = get_db()
    conn = get_conn(db)
    init_schema(conn)
    
    # Get ALL memory files, check which need processing
    all_files = sorted(MEMORY_DIR.glob("*.md"))
    processed = get_processed_files()
    
    todo = []
    for f in all_files:
        mtime = str(f.stat().st_mtime)
        key = str(f)
        if key not in processed or processed[key] != mtime:
            todo.append(f)
    
    print(f"🧠 Engram Batch Ingest")
    print(f"   Total files: {len(all_files)}")
    print(f"   Already done: {len(all_files) - len(todo)}")
    print(f"   To process: {len(todo)}")
    print()
    
    if not todo:
        print("✅ All files already processed")
        stats = get_stats(conn)
        print_stats(stats)
        return
    
    for i, filepath in enumerate(todo):
        print(f"\n[{i+1}/{len(todo)}] ", end="")
        try:
            ingest_file(conn, filepath)
            processed[str(filepath)] = str(filepath.stat().st_mtime)
            save_processed_files(processed)
        except Exception as e:
            print(f"   ❌ Error: {e}")
        
        # Brief pause for API rate limits
        time.sleep(0.5)
    
    print("\n\n" + "=" * 50)
    print("✅ INGESTION COMPLETE")
    stats = get_stats(conn)
    print_stats(stats)

if __name__ == "__main__":
    main()
