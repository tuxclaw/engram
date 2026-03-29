#!/usr/bin/env python3
"""
Engram Dream Consolidation — Memory reorganization during idle time.

Like human sleep, this process:
  1. Strengthens frequently-accessed connections
  2. Decays importance of unused memories
  3. Merges duplicate/near-duplicate entities
  4. Detects and resolves contradictions temporally
  5. Extracts meta-patterns across memories
  6. Bumps access-weighted importance scores

Run as a cron job during idle periods.

Usage:
  python engram/consolidate.py              # Full consolidation
  python engram/consolidate.py --dry-run    # Show what would change
  python engram/consolidate.py --stats      # Just show stats
"""

import json
import os
import sys
from datetime import datetime, timedelta
from collections import defaultdict

import kuzu

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from engram.backend import get_db, get_conn, get_stats, print_stats


# =========================================================
# Importance Decay
# =========================================================

def decay_importance(conn: kuzu.Connection, decay_rate: float = 0.02,
                     min_importance: float = 0.1, dry_run: bool = False) -> int:
    """Gentle exponential importance decay for entities and facts.
    
    new_importance = importance × 0.998^days_since_last_accessed
    ~0.2%/day — facts stay near full strength for months, gently
    recede over a year. Safe because extraction policy already
    filters noise at ingest time (March 2026).
    
    Falls back to created_at if last_accessed is null.
    Never decays below min_importance (default 0.1).
    
    decay_rate parameter is kept for API compatibility but ignored —
    the 0.998 base is fixed for the exponential model.
    """
    count = 0
    now = datetime.now()

    def _compute_and_apply_decay(node_label: str):
        nonlocal count
        try:
            # Fetch nodes that could decay
            result = conn.execute(
                f"MATCH (n:{node_label}) "
                f"WHERE n.importance > $p_min "
                f"RETURN n.id, n.importance, n.last_accessed, n.created_at",
                {"p_min": min_importance}
            )
            rows = []
            while result.has_next():
                rows.append(result.get_next())

            for row in rows:
                node_id, importance, last_accessed, created_at = row[0], row[1], row[2], row[3]

                # Determine reference time (last_accessed, fallback to created_at, fallback to now)
                ref_time = last_accessed or created_at
                if ref_time is None:
                    days_elapsed = 0.0
                else:
                    # Kuzu returns timestamps as datetime objects
                    if isinstance(ref_time, datetime):
                        days_elapsed = (now - ref_time).total_seconds() / 86400.0
                    else:
                        try:
                            ref_dt = datetime.fromisoformat(str(ref_time))
                            days_elapsed = (now - ref_dt).total_seconds() / 86400.0
                        except Exception:
                            days_elapsed = 0.0

                days_elapsed = max(0.0, days_elapsed)
                new_importance = importance * (0.998 ** days_elapsed)
                new_importance = max(min_importance, new_importance)

                if not dry_run:
                    try:
                        conn.execute(
                            f"MATCH (n:{node_label} {{id: $p_id}}) SET n.importance = $p_imp",
                            {"p_id": node_id, "p_imp": new_importance}
                        )
                    except Exception as e:
                        print(f"  Warning: decay update failed for {node_id}: {e}")
                count += 1

        except Exception as e:
            print(f"  Warning: {node_label} decay failed: {e}")

    _compute_and_apply_decay("Entity")
    _compute_and_apply_decay("Fact")

    return count


# =========================================================
# Centrality Boost
# =========================================================

def boost_central_entities(conn: kuzu.Connection, boost: float = 0.05,
                           max_importance: float = 1.0, min_connections: int = 5,
                           dry_run: bool = False) -> int:
    """Boost importance of entities with many connections (graph centrality)."""
    count = 0
    try:
        if not dry_run:
            result = conn.execute(
                "MATCH (e:Entity)-[r]-() "
                "WITH e, count(r) AS connections "
                "WHERE connections >= $p_min_conn AND e.importance < $p_max "
                "SET e.importance = CASE "
                "  WHEN e.importance + $p_boost > $p_max THEN $p_max "
                "  ELSE e.importance + $p_boost "
                "END "
                "RETURN count(e)",
                {"p_min_conn": min_connections, "p_max": max_importance, "p_boost": boost}
            )
            if result.has_next():
                count = result.get_next()[0]
        else:
            result = conn.execute(
                "MATCH (e:Entity)-[r]-() "
                "WITH e, count(r) AS connections "
                "WHERE connections >= $p_min_conn "
                "RETURN e.name, connections "
                "ORDER BY connections DESC LIMIT 10",
                {"p_min_conn": min_connections}
            )
            while result.has_next():
                row = result.get_next()
                print(f"  Would boost: {row[0]} ({row[1]} connections)")
                count += 1
    except Exception as e:
        print(f"  Warning: centrality boost failed: {e}")
    
    return count


# =========================================================
# Duplicate Detection
# =========================================================

def find_duplicate_entities(conn: kuzu.Connection) -> list[tuple]:
    """Find entities with similar names that might be duplicates."""
    duplicates = []
    try:
        # Get all entity names
        result = conn.execute(
            "MATCH (e:Entity) RETURN e.id, e.name, e.entity_type"
        )
        entities = []
        while result.has_next():
            row = result.get_next()
            entities.append({"id": row[0], "name": row[1], "type": row[2]})
        
        # Simple name normalization comparison
        name_map = defaultdict(list)
        for ent in entities:
            normalized = ent["name"].lower().strip().replace("_", " ").replace("-", " ")
            name_map[normalized].append(ent)
        
        for name, ents in name_map.items():
            if len(ents) > 1:
                duplicates.append((name, ents))
    
    except Exception as e:
        print(f"  Warning: duplicate detection failed: {e}")
    
    return duplicates


def merge_duplicate_entities(conn: kuzu.Connection, duplicates: list[tuple],
                             dry_run: bool = False) -> int:
    """Merge duplicate entities, keeping the one with more connections."""
    merged = 0
    
    for name, ents in duplicates:
        if dry_run:
            names = [e["name"] for e in ents]
            print(f"  Would merge: {names}")
            merged += 1
            continue
        
        # For now, just report — actual merging is complex and we want to be careful
        # TODO: Implement entity merging (transfer relationships, delete duplicate)
        print(f"  Duplicate found: {[e['name'] for e in ents]}")
        merged += 1
    
    return merged


# =========================================================
# Relationship Strengthening
# =========================================================

def strengthen_co_occurring(conn: kuzu.Connection, boost: float = 0.1,
                            dry_run: bool = False) -> int:
    """Strengthen relationships between entities that appear in the same episodes often."""
    count = 0
    try:
        # Find entity pairs that co-occur in episodes
        result = conn.execute(
            "MATCH (e1:Entity)-[:MENTIONED_IN]->(ep:Episode)<-[:MENTIONED_IN]-(e2:Entity) "
            "WHERE e1.id < e2.id "
            "WITH e1, e2, count(ep) AS co_occurrences "
            "WHERE co_occurrences >= 2 "
            "RETURN e1.name, e2.name, co_occurrences "
            "ORDER BY co_occurrences DESC LIMIT 20"
        )
        
        pairs = []
        while result.has_next():
            row = result.get_next()
            pairs.append({"e1": row[0], "e2": row[1], "count": row[2]})
        
        if dry_run:
            for p in pairs:
                print(f"  Co-occurring: {p['e1']} ↔ {p['e2']} ({p['count']} episodes)")
            count = len(pairs)
        else:
            # Boost existing relationships between co-occurring entities
            for p in pairs:
                try:
                    conn.execute(
                        "MATCH (e1:Entity {name: $p_n1})-[r:RELATES_TO]-(e2:Entity {name: $p_n2}) "
                        "SET r.strength = CASE "
                        "  WHEN r.strength + $p_boost > 1.0 THEN 1.0 "
                        "  ELSE r.strength + $p_boost "
                        "END",
                        {"p_n1": p["e1"], "p_n2": p["e2"], "p_boost": boost}
                    )
                    count += 1
                except Exception:
                    pass
    except Exception as e:
        print(f"  Warning: co-occurrence analysis failed: {e}")
    
    return count


# =========================================================
# Emotional Pattern Extraction
# =========================================================

def extract_emotional_patterns(conn: kuzu.Connection) -> list[dict]:
    """Identify emotional patterns across the graph."""
    patterns = []
    
    try:
        # Find entities that consistently evoke certain emotions
        result = conn.execute(
            "MATCH (e:Entity)-[:ENTITY_EVOKES]->(em:Emotion) "
            "WITH e, em.label AS emotion, count(*) AS freq, avg(em.valence) AS avg_valence "
            "WHERE freq >= 2 "
            "RETURN e.name, emotion, freq, avg_valence "
            "ORDER BY freq DESC LIMIT 15"
        )
        while result.has_next():
            row = result.get_next()
            patterns.append({
                "entity": row[0], "emotion": row[1],
                "frequency": row[2], "avg_valence": row[3]
            })
    except Exception as e:
        print(f"  Warning: emotional pattern extraction failed: {e}")
    
    return patterns


# =========================================================
# Graph Health Report
# =========================================================

def get_importance_tiers(conn: kuzu.Connection) -> dict:
    """Count nodes in each importance tier across Entity, Fact, and Episode tables.
    
    Tiers:
      Core       0.7+       — frequently accessed, high-value
      Active     0.4–0.7    — in regular use
      Background 0.2–0.4    — occasional relevance
      Archive    <0.2       — candidates for pruning
    """
    tiers = {
        "core": 0,        # 0.7+
        "active": 0,      # 0.4–0.7
        "background": 0,  # 0.2–0.4
        "archive": 0,     # <0.2
        "archive_ids": [] # sample of archive candidates
    }
    for label in ["Entity", "Fact", "Episode"]:
        try:
            result = conn.execute(
                f"MATCH (n:{label}) RETURN n.importance"
            )
            while result.has_next():
                imp = result.get_next()[0]
                if imp is None:
                    imp = 0.5
                if imp >= 0.7:
                    tiers["core"] += 1
                elif imp >= 0.4:
                    tiers["active"] += 1
                elif imp >= 0.2:
                    tiers["background"] += 1
                else:
                    tiers["archive"] += 1
        except Exception:
            pass

    # Get sample archive candidate names/ids
    try:
        result = conn.execute(
            "MATCH (n:Entity) WHERE n.importance < 0.2 "
            "RETURN n.name, n.importance ORDER BY n.importance ASC LIMIT 10"
        )
        while result.has_next():
            row = result.get_next()
            tiers["archive_ids"].append({"name": row[0], "importance": row[1]})
    except Exception:
        pass

    return tiers


def health_report(conn: kuzu.Connection) -> dict:
    """Generate a health report for the knowledge graph."""
    report = {
        "generated_at": datetime.now().isoformat(),
        "stats": get_stats(conn),
        "orphan_entities": 0,
        "duplicate_candidates": 0,
        "importance_tiers": {},
        "archive_candidates": [],
        "emotional_patterns": [],
        "most_connected": [],
        "least_important": []
    }
    
    # Orphan entities (no relationships)
    try:
        result = conn.execute(
            "MATCH (e:Entity) "
            "WHERE NOT EXISTS { MATCH (e)-[]-() } "
            "RETURN count(e)"
        )
        if result.has_next():
            report["orphan_entities"] = result.get_next()[0]
    except Exception:
        pass
    
    # Duplicate candidates
    duplicates = find_duplicate_entities(conn)
    report["duplicate_candidates"] = len(duplicates)

    # Importance tiers
    tiers = get_importance_tiers(conn)
    report["importance_tiers"] = {k: v for k, v in tiers.items() if k != "archive_ids"}
    report["archive_candidates"] = tiers["archive_ids"]
    
    # Most connected entities
    try:
        result = conn.execute(
            "MATCH (e:Entity)-[r]-() "
            "WITH e.name AS name, count(r) AS connections "
            "RETURN name, connections ORDER BY connections DESC LIMIT 5"
        )
        while result.has_next():
            row = result.get_next()
            report["most_connected"].append({"name": row[0], "connections": row[1]})
    except Exception:
        pass
    
    # Emotional patterns
    report["emotional_patterns"] = extract_emotional_patterns(conn)
    
    return report


# =========================================================
# Main Consolidation
# =========================================================

def consolidate(dry_run: bool = False, verbose: bool = False):
    """Run full dream consolidation cycle."""
    print("🌙 Engram Dream Consolidation")
    print(f"   Mode: {'DRY RUN' if dry_run else 'LIVE'}")
    print(f"   Time: {datetime.now().strftime('%Y-%m-%d %H:%M PST')}")
    print()
    
    db = get_db(read_only=dry_run)
    conn = get_conn(db)
    
    # 1. Importance decay
    print("1️⃣  Importance Decay")
    decayed = decay_importance(conn, dry_run=dry_run)
    print(f"   {decayed} nodes decayed")
    
    # 2. Centrality boost
    print("2️⃣  Centrality Boost")
    boosted = boost_central_entities(conn, dry_run=dry_run)
    print(f"   {boosted} entities boosted")
    
    # 3. Duplicate detection
    print("3️⃣  Duplicate Detection")
    duplicates = find_duplicate_entities(conn)
    if duplicates:
        merged = merge_duplicate_entities(conn, duplicates, dry_run=dry_run)
        print(f"   {merged} duplicate groups found")
    else:
        print("   No duplicates found")
    
    # 4. Relationship strengthening
    print("4️⃣  Relationship Strengthening")
    strengthened = strengthen_co_occurring(conn, dry_run=dry_run)
    print(f"   {strengthened} relationships strengthened")
    
    # 5. Emotional patterns
    print("5️⃣  Emotional Patterns")
    patterns = extract_emotional_patterns(conn)
    if patterns:
        for p in patterns[:5]:
            valence = "+" if p["avg_valence"] > 0 else "-"
            print(f"   {p['entity']} → {p['emotion']} ({valence}, {p['frequency']}x)")
    else:
        print("   No patterns detected yet")
    
    # 6. Health report
    print("\n📊 Health Report")
    report = health_report(conn)
    print(f"   Orphan entities: {report['orphan_entities']}")
    print(f"   Duplicate candidates: {report['duplicate_candidates']}")

    # Importance tiers
    tiers = report.get("importance_tiers", {})
    if tiers:
        print("   Importance tiers:")
        print(f"     🔴 Core       (0.7+):   {tiers.get('core', 0)}")
        print(f"     🟡 Active     (0.4–0.7): {tiers.get('active', 0)}")
        print(f"     🟢 Background (0.2–0.4): {tiers.get('background', 0)}")
        print(f"     ⚪ Archive    (<0.2):    {tiers.get('archive', 0)}")

    archive_candidates = report.get("archive_candidates", [])
    if archive_candidates:
        print(f"   ⚠️  Archive candidates ({len(archive_candidates)}):")
        for ac in archive_candidates:
            print(f"     {ac['name']}: imp={ac['importance']:.3f}")

    if report["most_connected"]:
        print("   Most connected:")
        for mc in report["most_connected"]:
            print(f"     {mc['name']}: {mc['connections']} connections")
    
    # Final stats
    print()
    stats = get_stats(conn)
    print_stats(stats)
    
    # Save health report
    report_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".health-report.json")
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2, default=str)
    print(f"\n💾 Health report saved to {report_path}")


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="Engram Dream Consolidation")
    parser.add_argument("--dry-run", action="store_true", help="Show what would change")
    parser.add_argument("--stats", action="store_true", help="Just show stats")
    parser.add_argument("--health", action="store_true", help="Health report only")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()
    
    if args.stats:
        db = get_db(read_only=True)
        conn = get_conn(db)
        stats = get_stats(conn)
        print_stats(stats)
    elif args.health:
        db = get_db(read_only=True)
        conn = get_conn(db)
        report = health_report(conn)
        print(json.dumps(report, indent=2, default=str))
    else:
        consolidate(dry_run=args.dry_run, verbose=args.verbose)
