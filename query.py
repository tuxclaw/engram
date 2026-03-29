#!/usr/bin/env python3
"""
Engram Query Layer — Unified search across the knowledge graph.

Combines:
  1. Graph traversal (Kuzu) - entity lookup + relationship walking
  2. Semantic search (Chroma) - embedding similarity 
  3. Temporal filtering - time-range queries
  4. Importance weighting - prioritize high-value memories

Usage:
  python engram/query.py "search terms"
  python engram/query.py "search terms" --type entity|fact|episode
  python engram/query.py "search terms" --since 2025-01-01
  python engram/query.py --entity "The Dev"  # Get everything about an entity
"""

import json
import os
import sys
from datetime import datetime
from typing import Optional

try:
    import kuzu
except ImportError:
    kuzu = None

# =========================================================
# Importance Tier Weights (for result ordering)
# =========================================================
# Core (0.7+) → weight 1.0, Active (0.4–0.7) → 0.85,
# Background (0.2–0.4) → 0.65, Archive (<0.2) → 0.4
def _tier_weight(importance: float) -> float:
    """Return a tier-based weight multiplier for search result ordering."""
    if importance is None:
        return 0.7
    if importance >= 0.7:
        return 1.0
    if importance >= 0.4:
        return 0.85
    if importance >= 0.2:
        return 0.65
    return 0.4

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from engram.backend import get_db, get_conn, get_stats, print_stats


# =========================================================
# Access Reinforcement
# =========================================================

def _reinforce_nodes(conn: kuzu.Connection, node_label: str, ids: list[str]):
    """Update last_accessed and increment access_count + importance for accessed nodes.
    
    Reinforcement formula: new_importance = old + (1 - old) × 0.15
    Caps at 1.0. Non-blocking — failure does not raise.
    """
    if not ids:
        return
    now = datetime.now()
    for node_id in ids:
        try:
            # Fetch current importance
            result = conn.execute(
                f"MATCH (n:{node_label} {{id: $p_id}}) "
                f"RETURN n.importance, n.access_count",
                {"p_id": node_id}
            )
            if not result.has_next():
                continue
            row = result.get_next()
            old_imp = row[0] if row[0] is not None else 0.5
            old_count = row[1] if row[1] is not None else 0

            new_imp = min(1.0, old_imp + (1.0 - old_imp) * 0.15)
            new_count = old_count + 1

            conn.execute(
                f"MATCH (n:{node_label} {{id: $p_id}}) "
                f"SET n.last_accessed = $p_ts, "
                f"    n.access_count = $p_cnt, "
                f"    n.importance = $p_imp",
                {"p_id": node_id, "p_ts": now, "p_cnt": new_count, "p_imp": new_imp}
            )
        except Exception:
            # Non-blocking — never interrupt a search result
            pass


def search_entities(conn: kuzu.Connection, query: str, limit: int = 10,
                    reinforce: bool = True, agent_id: Optional[str] = None,
                    since: Optional[str] = None, until: Optional[str] = None) -> list[dict]:
    """Search entities by name (case-insensitive partial match).
    
    Applies importance tier weighting to result ordering.
    Reinforces accessed nodes (last_accessed + access_count + importance).
    """
    results = []
    try:
        if agent_id:
            agent_filter = " AND (e.agent_id = $p_agent OR e.agent_id = 'shared')"
            params = {"p_q": query, "p_lim": limit, "p_agent": agent_id}
        else:
            agent_filter = ""
            params = {"p_q": query, "p_lim": limit}
        time_filter = ""
        if since:
            time_filter += " AND e.created_at >= timestamp($p_since)"
            params["p_since"] = since
        if until:
            time_filter += " AND e.created_at <= timestamp($p_until)"
            params["p_until"] = until
        result = conn.execute(
            "MATCH (e:Entity) "
            "WHERE (lower(e.name) CONTAINS lower($p_q) OR lower(e.description) CONTAINS lower($p_q))"
            " AND coalesce(e.retrievable, true) = true"
            + agent_filter + time_filter +
            " RETURN e.id, e.name, e.entity_type, e.description, e.importance, e.access_count, e.memory_tier, e.quality_score, e.contamination_score, e.retrievable "
            "ORDER BY coalesce(e.is_canonical, false) DESC, coalesce(e.quality_score, e.importance, 0) DESC, e.importance DESC LIMIT $p_lim",
            params
        )
        while result.has_next():
            row = result.get_next()
            results.append({
                "id": row[0], "name": row[1], "type": row[2],
                "description": row[3], "importance": row[4], "access_count": row[5],
                "memory_tier": row[6], "quality_score": row[7], "contamination_score": row[8], "retrievable": row[9]
            })
    except Exception as e:
        print(f"Entity search error: {e}")

    # Apply tier-weighted sort (Core memories bubble up), but prefer cleaner canonical memory first
    results.sort(
        key=lambda r: (
            1 if r.get("memory_tier") == "canonical" else (0 if r.get("memory_tier") == "candidate" else -1),
            (r.get("quality_score") or 0),
            -1 * (r.get("contamination_score") or 0),
            _tier_weight(r.get("importance", 0.5)) * (r.get("importance") or 0.5),
        ),
        reverse=True,
    )

    # Reinforce accessed nodes (non-blocking)
    if reinforce and results:
        _reinforce_nodes(conn, "Entity", [r["id"] for r in results])

    return results


def search_facts(conn: kuzu.Connection, query: str, limit: int = 10,
                 reinforce: bool = True, agent_id: Optional[str] = None,
                 since: Optional[str] = None, until: Optional[str] = None) -> list[dict]:
    """Search facts by content.
    
    Applies importance tier weighting to result ordering.
    Reinforces accessed nodes (last_accessed + access_count + importance).
    """
    results = []
    try:
        if agent_id:
            agent_filter = " AND (f.agent_id = $p_agent OR f.agent_id = 'shared')"
            params = {"p_q": query, "p_lim": limit, "p_agent": agent_id}
        else:
            agent_filter = ""
            params = {"p_q": query, "p_lim": limit}
        time_filter = ""
        if since:
            time_filter += " AND f.created_at >= timestamp($p_since)"
            params["p_since"] = since
        if until:
            time_filter += " AND f.created_at <= timestamp($p_until)"
            params["p_until"] = until
        result = conn.execute(
            "MATCH (f:Fact) "
            "WHERE lower(f.content) CONTAINS lower($p_q)"
            " AND coalesce(f.retrievable, true) = true"
            + agent_filter + time_filter +
            " RETURN f.id, f.content, f.category, f.confidence, f.importance, f.valid_at, f.memory_tier, f.quality_score, f.contamination_score, f.retrievable, f.source_type, f.created_at "
            "ORDER BY coalesce(f.is_canonical, false) DESC, coalesce(f.quality_score, f.importance, 0) DESC, f.importance DESC LIMIT $p_lim",
            params
        )
        while result.has_next():
            row = result.get_next()
            results.append({
                "id": row[0], "content": row[1], "category": row[2],
                "confidence": row[3], "importance": row[4], "valid_at": str(row[5]),
                "memory_tier": row[6], "quality_score": row[7], "contamination_score": row[8], "retrievable": row[9],
                "source_type": row[10] if len(row) > 10 else None, "created_at": row[11] if len(row) > 11 else None
            })
    except Exception as e:
        print(f"Fact search error: {e}")

    # Apply tier-weighted sort, but prefer cleaner canonical memory first
    results.sort(
        key=lambda r: (
            1 if r.get("memory_tier") == "canonical" else (0 if r.get("memory_tier") == "candidate" else -1),
            (r.get("quality_score") or 0),
            -1 * (r.get("contamination_score") or 0),
            _tier_weight(r.get("importance", 0.5)) * (r.get("importance") or 0.5),
        ),
        reverse=True,
    )

    # Reinforce accessed facts (non-blocking)
    if reinforce and results:
        _reinforce_nodes(conn, "Fact", [r["id"] for r in results])

    return results


def search_episodes(conn: kuzu.Connection, query: str, limit: int = 10, agent_id: Optional[str] = None,
                    since: Optional[str] = None, until: Optional[str] = None) -> list[dict]:
    """Search episodes by content or summary."""
    results = []
    try:
        if agent_id:
            agent_filter = " AND (ep.agent_id = $p_agent OR ep.agent_id = 'shared')"
            params = {"p_q": query, "p_lim": limit, "p_agent": agent_id}
        else:
            agent_filter = ""
            params = {"p_q": query, "p_lim": limit}
        time_filter = ""
        if since:
            time_filter += " AND ep.occurred_at >= timestamp($p_since)"
            params["p_since"] = since
        if until:
            time_filter += " AND ep.occurred_at <= timestamp($p_until)"
            params["p_until"] = until
        result = conn.execute(
            "MATCH (ep:Episode) "
            "WHERE (lower(ep.summary) CONTAINS lower($p_q) OR lower(ep.content) CONTAINS lower($p_q))"
            " AND coalesce(ep.retrievable, true) = true"
            + agent_filter + time_filter +
            " RETURN ep.id, ep.summary, ep.source_file, ep.occurred_at, ep.importance, ep.memory_tier, ep.quality_score, ep.contamination_score, ep.retrievable "
            "ORDER BY coalesce(ep.is_canonical, false) DESC, coalesce(ep.quality_score, ep.importance, 0) DESC, ep.occurred_at DESC LIMIT $p_lim",
            params
        )
        while result.has_next():
            row = result.get_next()
            results.append({
                "id": row[0], "summary": row[1], "source_file": row[2],
                "occurred_at": str(row[3]), "importance": row[4],
                "memory_tier": row[5], "quality_score": row[6], "contamination_score": row[7], "retrievable": row[8]
            })
    except Exception as e:
        print(f"Episode search error: {e}")
    return results


def get_entity_context(conn: kuzu.Connection, entity_name: str) -> dict:
    """Get full context for an entity — all relationships, facts, episodes, and emotions."""
    context = {"entity": None, "relationships": [], "facts": [], "episodes": [], "emotions": []}
    
    # Find entity
    try:
        result = conn.execute(
            "MATCH (e:Entity) WHERE lower(e.name) = lower($p_name) "
            "RETURN e.id, e.name, e.entity_type, e.description, e.importance",
            {"p_name": entity_name}
        )
        if result.has_next():
            row = result.get_next()
            context["entity"] = {
                "id": row[0], "name": row[1], "type": row[2],
                "description": row[3], "importance": row[4]
            }
        else:
            return context
    except Exception as e:
        print(f"Entity lookup error: {e}")
        return context

    # Reinforce the accessed entity (non-blocking)
    _reinforce_nodes(conn, "Entity", [context["entity"]["id"]])
    
    eid = context["entity"]["id"]
    
    # Get outgoing relationships
    try:
        result = conn.execute(
            "MATCH (e:Entity {id: $p_id})-[r:RELATES_TO]->(other:Entity) "
            "RETURN other.name, r.relation_type, r.description, r.strength",
            {"p_id": eid}
        )
        while result.has_next():
            row = result.get_next()
            context["relationships"].append({
                "target": row[0], "type": row[1], "description": row[2], "strength": row[3]
            })
    except Exception:
        pass
    
    # Get incoming relationships
    try:
        result = conn.execute(
            "MATCH (other:Entity)-[r:RELATES_TO]->(e:Entity {id: $p_id}) "
            "RETURN other.name, r.relation_type, r.description, r.strength",
            {"p_id": eid}
        )
        while result.has_next():
            row = result.get_next()
            context["relationships"].append({
                "source": row[0], "type": row[1], "description": row[2], "strength": row[3]
            })
    except Exception:
        pass
    
    # Get CAUSED relationships
    try:
        result = conn.execute(
            "MATCH (e:Entity {id: $p_id})-[r:CAUSED]->(other:Entity) "
            "RETURN other.name, r.description, r.confidence",
            {"p_id": eid}
        )
        while result.has_next():
            row = result.get_next()
            context["relationships"].append({
                "target": row[0], "type": "caused", "description": row[1], "confidence": row[2]
            })
    except Exception:
        pass
    
    # Get PART_OF relationships
    try:
        result = conn.execute(
            "MATCH (e:Entity {id: $p_id})-[r:PART_OF]->(other:Entity) "
            "RETURN other.name, r.role",
            {"p_id": eid}
        )
        while result.has_next():
            row = result.get_next()
            context["relationships"].append({
                "target": row[0], "type": "part_of", "role": row[1]
            })
    except Exception:
        pass
    
    # Get facts about this entity
    try:
        result = conn.execute(
            "MATCH (f:Fact)-[:ABOUT]->(e:Entity {id: $p_id}) "
            "RETURN f.id, f.content, f.category, f.confidence, f.valid_at",
            {"p_id": eid}
        )
        fact_ids = []
        while result.has_next():
            row = result.get_next()
            fact_ids.append(row[0])
            context["facts"].append({
                "id": row[0], "content": row[1], "category": row[2],
                "confidence": row[3], "valid_at": str(row[4])
            })
        # Reinforce accessed facts (non-blocking)
        _reinforce_nodes(conn, "Fact", fact_ids)
    except Exception:
        pass
    
    # Get episodes mentioning this entity
    try:
        result = conn.execute(
            "MATCH (e:Entity {id: $p_id})-[:MENTIONED_IN]->(ep:Episode) "
            "RETURN ep.summary, ep.source_file, ep.occurred_at "
            "ORDER BY ep.occurred_at DESC LIMIT 10",
            {"p_id": eid}
        )
        while result.has_next():
            row = result.get_next()
            context["episodes"].append({
                "summary": row[0], "source_file": row[1], "occurred_at": str(row[2])
            })
    except Exception:
        pass
    
    # Get emotions linked to this entity
    try:
        result = conn.execute(
            "MATCH (e:Entity {id: $p_id})-[:ENTITY_EVOKES]->(em:Emotion) "
            "RETURN em.label, em.valence, em.arousal, em.description",
            {"p_id": eid}
        )
        while result.has_next():
            row = result.get_next()
            context["emotions"].append({
                "label": row[0], "valence": row[1], "arousal": row[2], "context": row[3]
            })
    except Exception:
        pass
    
    return context


def unified_search(conn: kuzu.Connection, query: str, limit: int = 10, agent_id: Optional[str] = None,
                   since: Optional[str] = None, until: Optional[str] = None) -> dict:
    """Unified search across all memory types.
    
    Args:
        agent_id: Optional agent scope. When provided, returns results where
                  agent_id matches OR agent_id = 'shared'. When None, returns all.
    """
    return {
        "entities": search_entities(conn, query, limit, agent_id=agent_id, since=since, until=until),
        "facts": search_facts(conn, query, limit, agent_id=agent_id, since=since, until=until),
        "episodes": search_episodes(conn, query, limit, agent_id=agent_id, since=since, until=until)
    }


def print_results(results: dict, verbose: bool = False):
    """Pretty-print search results."""
    entities = results.get("entities", [])
    facts = results.get("facts", [])
    episodes = results.get("episodes", [])
    
    if not entities and not facts and not episodes:
        print("No results found.")
        return
    
    if entities:
        print(f"\n🔵 Entities ({len(entities)})")
        print("-" * 50)
        for e in entities:
            imp = f"[imp:{e['importance']:.1f}]" if e.get('importance') else ""
            print(f"  {e['name']} ({e['type']}) {imp}")
            if e.get('description'):
                print(f"    {e['description']}")
    
    if facts:
        print(f"\n📋 Facts ({len(facts)})")
        print("-" * 50)
        for f in facts:
            cat = f"[{f['category']}]" if f.get('category') else ""
            conf = f"({f['confidence']:.0%})" if f.get('confidence') else ""
            print(f"  {cat} {f['content']} {conf}")
    
    if episodes:
        print(f"\n📖 Episodes ({len(episodes)})")
        print("-" * 50)
        for ep in episodes:
            date = ep.get('occurred_at', 'unknown')[:10]
            src = ep.get('source_file', '')
            print(f"  [{date}] {ep.get('summary', '(no summary)')}")
            if verbose and src:
                print(f"    Source: {src}")


def print_entity_context(context: dict):
    """Pretty-print entity context."""
    ent = context.get("entity")
    if not ent:
        print("Entity not found.")
        return
    
    print(f"\n🔵 {ent['name']} ({ent['type']})")
    if ent.get('description'):
        print(f"   {ent['description']}")
    print(f"   Importance: {ent.get('importance', 0):.2f}")
    
    rels = context.get("relationships", [])
    if rels:
        print(f"\n🔗 Relationships ({len(rels)})")
        for r in rels:
            direction = f"→ {r.get('target', '?')}" if 'target' in r else f"← {r.get('source', '?')}"
            rtype = r.get('type', 'relates_to')
            print(f"   {direction} [{rtype}] {r.get('description', '')}")
    
    facts = context.get("facts", [])
    if facts:
        print(f"\n📋 Facts ({len(facts)})")
        for f in facts:
            print(f"   [{f.get('category', '')}] {f['content']}")
    
    episodes = context.get("episodes", [])
    if episodes:
        print(f"\n📖 Recent Episodes ({len(episodes)})")
        for ep in episodes:
            date = ep.get('occurred_at', '')[:10]
            print(f"   [{date}] {ep.get('summary', '(no summary)')}")
    
    emotions = context.get("emotions", [])
    if emotions:
        print(f"\n💫 Emotions ({len(emotions)})")
        for em in emotions:
            valence = "positive" if em.get('valence', 0) > 0 else "negative" if em.get('valence', 0) < 0 else "neutral"
            print(f"   {em['label']} ({valence}) - {em.get('context', '')}")


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="Engram Memory Query")
    parser.add_argument("query", nargs="?", help="Search query")
    parser.add_argument("--entity", type=str, help="Get full context for an entity")
    parser.add_argument("--type", choices=["entity", "fact", "episode", "all"], default="all")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--since", type=str, default=None, help="ISO date filter (>=)")
    parser.add_argument("--until", type=str, default=None, help="ISO date filter (<=)")
    parser.add_argument("--stats", action="store_true", help="Show database stats")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--json", "-j", action="store_true", help="Output as JSON")
    args = parser.parse_args()
    
    # Open writable so reinforcement updates (last_accessed, access_count) can persist.
    # Use read_only=True only when --stats is requested (no writes needed).
    if args.stats:
        db = get_db(read_only=True)
    else:
        db = get_db(read_only=False)
    conn = get_conn(db)
    
    if args.stats:
        stats = get_stats(conn)
        print_stats(stats)
        sys.exit(0)

    
    if args.entity:
        context = get_entity_context(conn, args.entity)
        if args.json:
            print(json.dumps(context, indent=2, default=str))
        else:
            print_entity_context(context)
        sys.exit(0)
    
    if not args.query:
        parser.print_help()
        sys.exit(1)
    
    if args.type == "all":
        results = unified_search(conn, args.query, args.limit, since=args.since, until=args.until)
    elif args.type == "entity":
        results = {"entities": search_entities(conn, args.query, args.limit, since=args.since, until=args.until)}
    elif args.type == "fact":
        results = {"facts": search_facts(conn, args.query, args.limit, since=args.since, until=args.until)}
    elif args.type == "episode":
        results = {"episodes": search_episodes(conn, args.query, args.limit, since=args.since, until=args.until)}
    
    if args.json:
        print(json.dumps(results, indent=2, default=str))
    else:
        print_results(results, verbose=args.verbose)
