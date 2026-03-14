#!/usr/bin/env python3
"""Safe reset / cleanup helper for Engram's Neo4j backend.

Modes:
- full: delete all Engram nodes/relationships (labels known to Engram)
- archive-only: delete only archive-tier sludge (exported session ingest)
- by-source-type: delete Episodes/Facts by source_type and clean dangling nodes

This script is intentionally conservative by default and supports --dry-run.
"""

from __future__ import annotations

import argparse
import sys
from typing import Iterable

from engram.backend import get_db, get_conn, init_schema, get_stats, print_stats, close

ENGRAM_LABELS = ["Entity", "Episode", "Emotion", "SessionState", "Fact"]


def _scalar(conn, query: str, params: dict | None = None) -> int:
    result = conn.execute(query, params or {})
    if result.has_next():
        return int(result.get_next()[0] or 0)
    return 0


def _run(conn, query: str, params: dict | None = None) -> None:
    conn.execute(query, params or {})


def _count_full(conn) -> tuple[int, int]:
    nodes = _scalar(conn, "MATCH (n) WHERE any(lbl IN labels(n) WHERE lbl IN $labels) RETURN count(n)", {"labels": ENGRAM_LABELS})
    rels = _scalar(conn, "MATCH (a)-[r]->(b) WHERE any(lbl IN labels(a) WHERE lbl IN $labels) OR any(lbl IN labels(b) WHERE lbl IN $labels) RETURN count(r)", {"labels": ENGRAM_LABELS})
    return nodes, rels


def _delete_full(conn, dry_run: bool) -> None:
    nodes, rels = _count_full(conn)
    print(f"[full] Engram-scoped graph: {nodes} nodes, {rels} relationships")
    if dry_run:
        return
    _run(conn, "MATCH (n) WHERE any(lbl IN labels(n) WHERE lbl IN $labels) DETACH DELETE n", {"labels": ENGRAM_LABELS})


def _count_archive(conn) -> tuple[int, int]:
    nodes = _scalar(conn, "MATCH (n) WHERE (n.memory_tier = 'archive' OR n.source_type = 'exported_session') RETURN count(n)")
    rels = _scalar(conn, "MATCH (a)-[r]->(b) WHERE (a.memory_tier = 'archive' OR a.source_type = 'exported_session' OR b.memory_tier = 'archive' OR b.source_type = 'exported_session') RETURN count(r)")
    return nodes, rels


def _delete_archive(conn, dry_run: bool) -> None:
    nodes, rels = _count_archive(conn)
    print(f"[archive-only] Archive/exported-session graph: {nodes} nodes, {rels} relationships")
    if dry_run:
        return
    _run(conn, "MATCH (n) WHERE (n.memory_tier = 'archive' OR n.source_type = 'exported_session') DETACH DELETE n")
    _cleanup_orphans(conn, dry_run=False)


def _delete_by_source_type(conn, source_types: Iterable[str], dry_run: bool) -> None:
    source_types = list(source_types)
    nodes = _scalar(conn, "MATCH (n) WHERE n.source_type IN $types RETURN count(n)", {"types": source_types})
    rels = _scalar(conn, "MATCH (a)-[r]->(b) WHERE a.source_type IN $types OR b.source_type IN $types RETURN count(r)", {"types": source_types})
    print(f"[by-source-type] source_type in {source_types}: {nodes} nodes, {rels} relationships")
    if dry_run:
        return
    _run(conn, "MATCH (n) WHERE n.source_type IN $types DETACH DELETE n", {"types": source_types})
    _cleanup_orphans(conn, dry_run=False)


def _cleanup_orphans(conn, dry_run: bool) -> None:
    q = "MATCH (e:Entity) WHERE NOT (e)--() RETURN count(e)"
    count = _scalar(conn, q)
    print(f"[cleanup] Orphan entities: {count}")
    if dry_run or count == 0:
        return
    _run(conn, "MATCH (e:Entity) WHERE NOT (e)--() DELETE e")


def main() -> int:
    ap = argparse.ArgumentParser(description="Reset/cleanup Engram Neo4j data safely")
    ap.add_argument("mode", choices=["full", "archive-only", "by-source-type"], help="Cleanup mode")
    ap.add_argument("--source-type", action="append", default=[], help="For by-source-type mode; can be repeated")
    ap.add_argument("--dry-run", action="store_true", help="Show what would be deleted")
    args = ap.parse_args()

    db = get_db()
    conn = get_conn(db)
    init_schema(conn)

    print("Before:")
    print_stats(get_stats(conn))

    if args.mode == "full":
        _delete_full(conn, dry_run=args.dry_run)
    elif args.mode == "archive-only":
        _delete_archive(conn, dry_run=args.dry_run)
    else:
        if not args.source_type:
            print("--source-type is required for by-source-type mode", file=sys.stderr)
            return 2
        _delete_by_source_type(conn, args.source_type, dry_run=args.dry_run)

    if not args.dry_run:
        print("\nAfter:")
        print_stats(get_stats(conn))

    close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
