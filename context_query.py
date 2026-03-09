#!/usr/bin/env python3
"""
Engram Context Query — Fast query interface for the Context Engine plugin.

Usage:
  python engram/context_query.py query "search terms" [--agent main] [--limit 8] [--json]
  python engram/context_query.py store --fact "content" [--agent main] [--category preference]

Designed to be called from Node.js via spawnSync with JSON output.
"""

import json
import os
import sys
from datetime import datetime
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def _get_conn(read_only=True):
    from engram.schema import get_db, get_conn
    db = get_db(read_only=read_only)
    return get_conn(db)

def query_memories(terms: str, agent_id: Optional[str] = None, limit: int = 8) -> dict:
    """Query Engram for relevant memories matching search terms.
    
    Splits multi-word queries into individual terms and searches each,
    then deduplicates and ranks results by frequency + importance.
    """
    try:
        conn = _get_conn(read_only=False)  # need write for reinforcement
        from engram.query import search_entities, search_facts, search_episodes
        
        # Split into individual search terms (skip short words)
        raw_terms = [t.strip() for t in terms.split() if len(t.strip()) >= 3]
        
        # Also try the full phrase and meaningful bigrams
        search_queries = list(set(raw_terms))
        if len(raw_terms) >= 2:
            search_queries.append(terms)  # full phrase
        
        # Search each term and collect results (deduplicate by id)
        entity_map = {}
        fact_map = {}
        episode_map = {}
        
        for q in search_queries:
            for e in search_entities(conn, q, limit=limit, agent_id=agent_id):
                eid = e.get("id", "")
                if eid not in entity_map:
                    e["_hits"] = 0
                    entity_map[eid] = e
                entity_map[eid]["_hits"] = entity_map[eid].get("_hits", 0) + 1
            
            for f in search_facts(conn, q, limit=limit, agent_id=agent_id):
                fid = f.get("id", "")
                if fid not in fact_map:
                    f["_hits"] = 0
                    fact_map[fid] = f
                fact_map[fid]["_hits"] = fact_map[fid].get("_hits", 0) + 1
            
            for ep in search_episodes(conn, q, limit=limit, agent_id=agent_id):
                epid = ep.get("id", "")
                if epid not in episode_map:
                    ep["_hits"] = 0
                    episode_map[epid] = ep
                episode_map[epid]["_hits"] = episode_map[epid].get("_hits", 0) + 1
        
        # Sort by hit count (relevance) then importance, return top N
        entities = sorted(entity_map.values(), key=lambda x: (x.get("_hits", 0), x.get("importance", 0)), reverse=True)[:limit]
        facts = sorted(fact_map.values(), key=lambda x: (x.get("_hits", 0), x.get("importance", 0)), reverse=True)[:limit]
        episodes = sorted(episode_map.values(), key=lambda x: (x.get("_hits", 0), x.get("importance", 0)), reverse=True)[:limit]
        
        # Clean up internal tracking field
        for item in entities + facts + episodes:
            item.pop("_hits", None)
        
        return {
            "ok": True,
            "entities": entities,
            "facts": facts,
            "episodes": episodes
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "entities": [], "facts": [], "episodes": []}

def store_fact(content: str, agent_id: str = "main", category: str = "preference",
               confidence: float = 0.9, importance: float = 0.7) -> dict:
    """Store a single fact into Engram's graph DB."""
    try:
        conn = _get_conn(read_only=False)
        from engram.ingest import generate_id
        
        fact_id = generate_id("fact", content)
        now = datetime.now()
        
        # Check if fact already exists
        try:
            result = conn.execute(
                "MATCH (f:Fact {id: $p_id}) RETURN f.id",
                {"p_id": fact_id}
            )
            if result.has_next():
                return {"ok": True, "stored": False, "reason": "duplicate", "id": fact_id}
        except:
            pass
        
        conn.execute(
            "CREATE (f:Fact {"
            "  id: $p_id, content: $p_content, category: $p_cat,"
            "  confidence: $p_conf, importance: $p_imp,"
            "  valid_at: $p_ts, created_at: $p_ts, updated_at: $p_ts,"
            "  source_episode: $p_src, agent_id: $p_agent"
            "})",
            {
                "p_id": fact_id,
                "p_content": content,
                "p_cat": category,
                "p_conf": confidence,
                "p_imp": importance,
                "p_ts": now,
                "p_src": "context-engine-live",
                "p_agent": agent_id
            }
        )
        
        return {"ok": True, "stored": True, "id": fact_id}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def format_for_prompt(results: dict, max_chars: int = 4000) -> str:
    """Format query results into a compact prompt-ready string."""
    lines = []
    
    entities = results.get("entities", [])
    facts = results.get("facts", [])
    episodes = results.get("episodes", [])
    
    if not entities and not facts and not episodes:
        return ""
    
    if facts:
        for f in facts[:6]:
            cat = f"[{f.get('category', '')}]" if f.get('category') else ""
            lines.append(f"- {cat} {f['content']}")
    
    if entities:
        for e in entities[:4]:
            desc = e.get('description', '')
            if desc:
                lines.append(f"- {e['name']} ({e.get('type', '')}): {desc}")
    
    if episodes:
        for ep in episodes[:3]:
            date = ep.get('occurred_at', '')[:10]
            lines.append(f"- [{date}] {ep.get('summary', '')}")
    
    result = "\n".join(lines)
    return result[:max_chars] if len(result) > max_chars else result


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="Engram Context Query")
    subparsers = parser.add_subparsers(dest="command")
    
    # Query command
    q_parser = subparsers.add_parser("query", help="Query memories")
    q_parser.add_argument("terms", help="Search terms")
    q_parser.add_argument("--agent", type=str, default=None, help="Agent ID scope")
    q_parser.add_argument("--limit", type=int, default=8)
    q_parser.add_argument("--json", "-j", action="store_true", help="JSON output")
    q_parser.add_argument("--prompt", action="store_true", help="Prompt-ready format")
    
    # Store command
    s_parser = subparsers.add_parser("store", help="Store a fact")
    s_parser.add_argument("--fact", required=True, help="Fact content")
    s_parser.add_argument("--agent", type=str, default="main")
    s_parser.add_argument("--category", type=str, default="preference")
    s_parser.add_argument("--importance", type=float, default=0.7)
    
    args = parser.parse_args()
    
    if args.command == "query":
        results = query_memories(args.terms, agent_id=args.agent, limit=args.limit)
        if args.prompt:
            print(format_for_prompt(results))
        elif args.json:
            print(json.dumps(results, indent=2, default=str))
        else:
            from engram.query import print_results
            print_results(results)
    
    elif args.command == "store":
        result = store_fact(args.fact, agent_id=args.agent, category=args.category,
                           importance=args.importance)
        print(json.dumps(result, indent=2, default=str))
    
    else:
        parser.print_help()
