#!/usr/bin/env python3
"""
Export OpenClaw JSONL session transcripts to daily markdown files for Engram ingestion.
Reads from ~/.openclaw/agents/*/sessions/*.jsonl
Writes to memory dir as YYYY-MM-DD-<slug>.md
"""
import json
import os
import re
import sys
from pathlib import Path
from datetime import datetime, timezone

SESSIONS_ROOT = Path(os.path.expanduser("~/.openclaw/agents"))
MEMORY_DIR = Path(os.path.dirname(os.path.abspath(__file__))) / "exported-sessions"
# Override: set ENGRAM_MEMORY_DIR env or edit config.json memory_dir
if os.environ.get("ENGRAM_MEMORY_DIR"):
    MEMORY_DIR = Path(os.path.expanduser(os.environ["ENGRAM_MEMORY_DIR"]))
PROCESSED_FILE = MEMORY_DIR / ".exported_sessions"

def load_processed():
    if PROCESSED_FILE.exists():
        return set(PROCESSED_FILE.read_text().strip().split("\n"))
    return set()

def save_processed(processed: set):
    PROCESSED_FILE.write_text("\n".join(sorted(processed)) + "\n")

def extract_messages(jsonl_path: Path):
    """Extract user/assistant message pairs from a JSONL session file."""
    messages = []
    session_date = None
    
    with open(jsonl_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            
            if entry.get("type") == "session" and "timestamp" in entry:
                session_date = entry["timestamp"][:10]  # YYYY-MM-DD
            
            if entry.get("type") == "message":
                msg = entry.get("message", {})
                role = msg.get("role", "")
                content = msg.get("content", "")
                
                # Handle content arrays
                if isinstance(content, list):
                    text_parts = []
                    for part in content:
                        if isinstance(part, dict) and part.get("type") == "text":
                            text_parts.append(part.get("text", ""))
                    content = "\n".join(text_parts)
                
                if not content or not role:
                    continue
                
                # Strip OpenClaw metadata from user messages
                # Remove "Conversation info" blocks and external untrusted content
                if role == "user":
                    # Remove metadata blocks
                    content = re.sub(r'Conversation info \(untrusted metadata\):.*?```\n', '', content, flags=re.DOTALL)
                    content = re.sub(r'Sender \(untrusted metadata\):.*?```\n', '', content, flags=re.DOTALL)
                    content = re.sub(r'<<<EXTERNAL_UNTRUSTED_CONTENT.*?<<<END_EXTERNAL_UNTRUSTED_CONTENT[^>]*>>>', '', content, flags=re.DOTALL)
                    content = re.sub(r'Untrusted context \(metadata[^)]*\):\n*', '', content)
                    content = content.strip()
                    if not content:
                        continue
                
                # Skip tool results and system messages for brevity
                if role == "system":
                    continue
                
                # Truncate very long assistant messages (tool outputs etc)
                if len(content) > 2000:
                    content = content[:2000] + "\n[...truncated]"
                
                messages.append({"role": role, "content": content})
                
                if not session_date and "timestamp" in entry:
                    session_date = entry["timestamp"][:10]
    
    return messages, session_date

def messages_to_markdown(messages, session_file: str, session_date: str):
    """Convert messages to a markdown document suitable for Engram ingestion."""
    lines = [f"# Session: {session_file}", f"Date: {session_date}", ""]
    
    for msg in messages:
        prefix = "**User:**" if msg["role"] == "user" else "**Assistant:**"
        lines.append(f"{prefix} {msg['content']}")
        lines.append("")
    
    return "\n".join(lines)

def main():
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    processed = load_processed()
    
    # Find all session JSONL files
    session_files = sorted(SESSIONS_ROOT.glob("*/sessions/*.jsonl"))
    
    new_count = 0
    skipped = 0
    
    for sf in session_files:
        file_id = sf.name  # UUID.jsonl
        if file_id in processed:
            skipped += 1
            continue
        
        messages, session_date = extract_messages(sf)
        
        if not messages or len(messages) < 2:
            # Skip empty or single-message sessions
            processed.add(file_id)
            continue
        
        if not session_date:
            session_date = datetime.now().strftime("%Y-%m-%d")
        
        # Get agent name from path
        agent = sf.parent.parent.name
        slug = f"{session_date}-{agent}-{file_id[:8]}"
        out_path = MEMORY_DIR / f"{slug}.md"
        
        md = messages_to_markdown(messages, slug, session_date)
        out_path.write_text(md)
        processed.add(file_id)
        new_count += 1
        print(f"  ✅ {slug}")
    
    save_processed(processed)
    print(f"\n📊 Export complete: {new_count} new, {skipped} already exported, {len(session_files)} total sessions")

if __name__ == "__main__":
    main()
