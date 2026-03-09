#!/usr/bin/env python3
"""
Local Entity Extractor for Engram
Uses Ollama (Qwen3 8B) to extract entities and relationships from daily memory logs.
Zero API cost — runs entirely on local GPU.

Usage:
    python local-entity-extractor.py                    # Process today's log
    python local-entity-extractor.py 2026-02-14         # Process specific date
    python local-entity-extractor.py --all-recent 3     # Process last N days
    python local-entity-extractor.py --dry-run          # Show what would be extracted, don't write
"""

import json
import os
import sys
import re
import argparse
from datetime import datetime, timedelta
from pathlib import Path
import requests

# Config
OLLAMA_URL = "http://localhost:11434/v1/chat/completions"
MODEL = "qwen3:8b"
MEMORY_DIR = Path(os.path.expanduser("~/clawd/memory"))
ENTITY_DIR = Path(os.path.expanduser("~/clawd/engram/entities"))
GRAPH_DB = Path(os.path.expanduser("~/clawd/engram/graph.json"))
MAX_CHUNK_CHARS = 3000  # Split logs into chunks of this size

SYSTEM_PROMPT = """You are an entity extraction agent. Extract entities and relationships from work logs into JSON. Output ONLY valid JSON — no markdown fences, no thinking tags, no explanations.

Entity types: person, organization, project, technology, concept, decision, problem
Relationship types: works_on, decided, caused, solved, related_to, owns, uses

Rules:
- Use canonical names (e.g. "The Dev" not "Dev", "Tony" not "tony agent")
- Merge duplicates (use aliases field)
- Include specific facts, not vague descriptions
- Dates/numbers are valuable facts — always include them
- Keep facts concise (one sentence each)

Schema:
{"entities": [{"name": "string", "type": "string", "aliases": ["string"], "facts": ["string"]}], "relationships": [{"from": "string", "to": "string", "type": "string", "context": "string"}]}"""


def chunk_text(text: str, max_chars: int = MAX_CHUNK_CHARS) -> list[str]:
    """Split text into chunks at section boundaries (## headers)."""
    sections = re.split(r'\n(?=## )', text)
    chunks = []
    current = ""

    for section in sections:
        if len(current) + len(section) > max_chars and current:
            chunks.append(current.strip())
            current = section
        else:
            current += "\n" + section if current else section

    if current.strip():
        chunks.append(current.strip())

    return chunks if chunks else [text]


def repair_json(raw: str) -> dict | None:
    """Attempt to parse JSON, with progressive repair strategies."""
    # Strategy 1: direct parse
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # Strategy 2: find the outermost { ... } and try that
    brace_start = raw.find('{')
    if brace_start == -1:
        return None

    # Find matching closing brace by counting
    depth = 0
    last_close = -1
    for i in range(brace_start, len(raw)):
        if raw[i] == '{':
            depth += 1
        elif raw[i] == '}':
            depth -= 1
            if depth == 0:
                last_close = i
                break

    if last_close > brace_start:
        try:
            return json.loads(raw[brace_start:last_close + 1])
        except json.JSONDecodeError:
            pass

    # Strategy 3: line-by-line trim from end with closing suffixes
    # This handles truncated output (token limit hit mid-JSON)
    candidate = raw[brace_start:]
    lines = candidate.split('\n')

    # Possible suffixes to close truncated JSON structures
    close_suffixes = [
        '', '}', ']}', ']}]}', '"]}}', '"]}]}',
        '"}]}', '"}],"relationships":[]}',
        '"],"relationships":[]}',
        '"}], "relationships":[]}',
        '"]}, {"from":"","to":"","type":"","context":""}]}'  # won't help but harmless
    ]

    for end in range(len(lines), max(len(lines) - 20, 0), -1):
        partial = '\n'.join(lines[:end]).rstrip().rstrip(',')
        for suffix in close_suffixes:
            try:
                result = json.loads(partial + suffix)
                if isinstance(result, dict):
                    return result
            except json.JSONDecodeError:
                continue

    return None


def extract_entities(text: str, retries: int = 2) -> dict | None:
    """Call Ollama to extract entities from a text chunk."""
    # Append /no_think to suppress Qwen3's internal reasoning
    user_content = f"/no_think\nExtract entities and relationships from this text. Output ONLY valid JSON:\n\n{text}"

    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content}
        ],
        "temperature": 0.2,
        "max_tokens": 4000
    }
    # Note: after all chunks are processed, main() unloads the model to free VRAM

    for attempt in range(retries + 1):
        try:
            resp = requests.post(OLLAMA_URL, json=payload, timeout=120)
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]

            # Strip markdown fences if present
            content = re.sub(r'^```json\s*', '', content.strip())
            content = re.sub(r'\s*```$', '', content.strip())

            # Strip thinking tags if present (Qwen3 sometimes adds these)
            content = re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL).strip()

            result = repair_json(content)
            if result and "entities" in result:
                return result

            if attempt < retries:
                print(f"  Retry {attempt + 1}/{retries}: JSON repair failed")
                continue
            print(f"  Failed after {retries + 1} attempts: could not parse JSON")
            # Last resort: log the raw output for debugging
            print(f"  Raw output (first 500 chars): {content[:500]}")
            return None
        except (KeyError, requests.RequestException) as e:
            if attempt < retries:
                print(f"  Retry {attempt + 1}/{retries}: {e}")
                continue
            print(f"  Failed after {retries + 1} attempts: {e}")
            return None


def merge_entities(existing: dict, new: dict) -> dict:
    """Merge new extraction results into existing graph."""
    # Index existing entities by name
    entity_map = {}
    for e in existing.get("entities", []):
        entity_map[e["name"].lower()] = e
        for alias in e.get("aliases", []):
            entity_map[alias.lower()] = e

    # Merge new entities
    for e in new.get("entities", []):
        key = e["name"].lower()
        if key in entity_map:
            # Merge facts (deduplicate)
            existing_facts = set(entity_map[key].get("facts", []))
            for fact in e.get("facts", []):
                if fact not in existing_facts:
                    entity_map[key].setdefault("facts", []).append(fact)
            # Merge aliases
            existing_aliases = set(a.lower() for a in entity_map[key].get("aliases", []))
            for alias in e.get("aliases", []):
                if alias.lower() not in existing_aliases:
                    entity_map[key].setdefault("aliases", []).append(alias)
        else:
            entity_map[key] = e
            existing.setdefault("entities", []).append(e)

    # Merge relationships (deduplicate by from+to+type)
    existing_rels = set()
    for r in existing.get("relationships", []):
        existing_rels.add((r["from"].lower(), r["to"].lower(), r["type"]))

    for r in new.get("relationships", []):
        key = (r["from"].lower(), r["to"].lower(), r["type"])
        if key not in existing_rels:
            existing.setdefault("relationships", []).append(r)
            existing_rels.add(key)

    return existing


def write_entity_markdown(entity: dict, entity_dir: Path, date: str):
    """Write an entity as an Obsidian-compatible Markdown file with backlinks."""
    name = entity["name"]
    safe_name = re.sub(r'[<>:"/\\|?*]', '_', name)
    filepath = entity_dir / f"{safe_name}.md"

    # Build content
    lines = [f"# {name}", ""]

    if entity.get("type"):
        lines.append(f"**Type:** {entity['type']}")
        lines.append("")

    if entity.get("aliases"):
        lines.append(f"**Aliases:** {', '.join(entity['aliases'])}")
        lines.append("")

    if entity.get("facts"):
        lines.append("## Facts")
        for fact in entity["facts"]:
            lines.append(f"- {fact}")
        lines.append("")

    lines.append(f"---\n*Last updated: {date}*")

    # Write (append new facts if file exists)
    if filepath.exists():
        existing_content = filepath.read_text()
        # Extract existing facts
        existing_facts = set()
        for line in existing_content.split("\n"):
            if line.startswith("- "):
                existing_facts.add(line[2:].strip())

        # Only add truly new facts
        new_facts = [f for f in entity.get("facts", []) if f not in existing_facts]
        if new_facts:
            # Insert before the --- separator
            parts = existing_content.rsplit("---", 1)
            insert = "\n".join(f"- {f}" for f in new_facts)
            updated = f"{parts[0].rstrip()}\n{insert}\n\n---\n*Last updated: {date}*"
            filepath.write_text(updated)
            return len(new_facts)
        return 0
    else:
        filepath.write_text("\n".join(lines))
        return len(entity.get("facts", []))


def process_date(date_str: str, dry_run: bool = False) -> dict:
    """Process a single day's memory log."""
    log_path = MEMORY_DIR / f"{date_str}.md"

    if not log_path.exists():
        print(f"No log found for {date_str}")
        return {}

    print(f"\n📅 Processing {date_str}...")
    text = log_path.read_text()
    chunks = chunk_text(text)
    print(f"  Split into {len(chunks)} chunks")

    combined = {"entities": [], "relationships": []}

    for i, chunk in enumerate(chunks):
        print(f"  Extracting chunk {i + 1}/{len(chunks)} ({len(chunk)} chars)...")
        result = extract_entities(chunk)
        if result:
            n_ent = len(result.get("entities", []))
            n_rel = len(result.get("relationships", []))
            print(f"    → {n_ent} entities, {n_rel} relationships")
            combined = merge_entities(combined, result)
        else:
            print(f"    → extraction failed, skipping chunk")

    if dry_run:
        print(f"\n📋 DRY RUN — would extract:")
        print(json.dumps(combined, indent=2))
        return combined

    return combined


def main():
    parser = argparse.ArgumentParser(description="Local entity extraction for Engram")
    parser.add_argument("date", nargs="?", help="Date to process (YYYY-MM-DD), default: today")
    parser.add_argument("--all-recent", type=int, metavar="N", help="Process last N days")
    parser.add_argument("--dry-run", action="store_true", help="Show results without writing")
    args = parser.parse_args()

    # Ensure directories exist
    ENTITY_DIR.mkdir(parents=True, exist_ok=True)

    # Load or initialize graph
    if GRAPH_DB.exists():
        graph = json.loads(GRAPH_DB.read_text())
    else:
        graph = {"entities": [], "relationships": [], "meta": {"created": datetime.now().isoformat()}}

    # Determine dates to process
    if args.all_recent:
        dates = [(datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(args.all_recent)]
    elif args.date:
        dates = [args.date]
    else:
        dates = [datetime.now().strftime("%Y-%m-%d")]

    total_entities = 0
    total_relationships = 0
    total_facts_written = 0

    for date_str in dates:
        result = process_date(date_str, dry_run=args.dry_run)

        if result and not args.dry_run:
            # Merge into graph
            graph = merge_entities(graph, result)

            # Write entity Markdown files
            for entity in result.get("entities", []):
                n_facts = write_entity_markdown(entity, ENTITY_DIR, date_str)
                total_facts_written += n_facts

            total_entities += len(result.get("entities", []))
            total_relationships += len(result.get("relationships", []))

    # Unload model from VRAM (frees ~5.7GB for other GPU tasks)
    if not args.dry_run:
        try:
            requests.post("http://localhost:11434/api/generate",
                          json={"model": MODEL, "keep_alive": 0}, timeout=10)
            print("  Unloaded Qwen3 from VRAM")
        except Exception:
            pass

    if not args.dry_run and (total_entities or total_relationships):
        # Save graph
        graph["meta"] = graph.get("meta", {})
        graph["meta"]["last_updated"] = datetime.now().isoformat()
        graph["meta"]["total_entities"] = len(graph["entities"])
        graph["meta"]["total_relationships"] = len(graph["relationships"])
        GRAPH_DB.write_text(json.dumps(graph, indent=2))

        print(f"\n✅ Done!")
        print(f"  Entities processed: {total_entities}")
        print(f"  Relationships found: {total_relationships}")
        print(f"  Facts written to Markdown: {total_facts_written}")
        print(f"  Graph saved: {GRAPH_DB}")
        print(f"  Entity files: {ENTITY_DIR}")
    elif args.dry_run:
        print(f"\n🔍 Dry run complete — no files written")


if __name__ == "__main__":
    import functools
    print = functools.partial(print, flush=True)
    main()
