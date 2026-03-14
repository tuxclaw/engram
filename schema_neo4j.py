#!/usr/bin/env python3
"""
Engram Schema — Neo4j Backend

Drop-in replacement for schema.py (Kuzu) using Neo4j.
Provides the same interface: get_db(), get_conn(), init_schema(), get_stats().
"""

import os
import sys
import time
from datetime import datetime
from neo4j import GraphDatabase

# Neo4j connection settings — from config.json or env vars
def _load_neo4j_config():
    import json
    from pathlib import Path
    cfg_path = Path(os.path.dirname(os.path.abspath(__file__))) / "config.json"
    if cfg_path.exists():
        with open(cfg_path) as f:
            cfg = json.load(f)
        neo4j_cfg = cfg.get("neo4j", {})
        return neo4j_cfg
    return {}

_neo4j_cfg = _load_neo4j_config()
NEO4J_URI = os.environ.get("NEO4J_URI", _neo4j_cfg.get("uri", "bolt://localhost:7687"))
NEO4J_USER = os.environ.get("NEO4J_USER", _neo4j_cfg.get("user", "neo4j"))
NEO4J_PASSWORD = os.environ.get("NEO4J_PASSWORD", _neo4j_cfg.get("password", "neo4j"))

# Singleton driver
_driver = None


def get_driver():
    """Get or create the Neo4j driver (singleton)."""
    global _driver
    if _driver is None:
        _driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
        _driver.verify_connectivity()
    return _driver


def get_db(read_only: bool = False, **kwargs):
    """Compatibility shim — returns the Neo4j driver.
    
    read_only param is accepted but ignored (Neo4j handles concurrency natively).
    """
    return get_driver()


def get_conn(db=None):
    """Get a Neo4j session. Compatible with old code expecting a connection object."""
    driver = db or get_driver()
    return Neo4jConnection(driver)


class Neo4jConnection:
    """Wrapper that provides a Kuzu-compatible interface over Neo4j.
    
    Kuzu pattern:  result = conn.execute(cypher, params)
    Neo4j pattern: result = session.run(cypher, params)
    
    This adapter bridges the gap so existing code works with minimal changes.
    """
    
    def __init__(self, driver):
        self._driver = driver
        self._session = None
    
    def execute(self, query: str, params: dict = None):
        """Execute a Cypher query and return a Neo4jResult wrapper."""
        # Translate Kuzu-specific syntax to Neo4j
        query = _translate_cypher(query)
        # Fix datetime string formats for Neo4j
        params = sanitize_params(params) if params else params
        
        session = self._driver.session()
        try:
            result = session.run(query, params or {})
            records = list(result)
            summary = result.consume()
            return Neo4jResult(records, session)
        except Exception:
            session.close()
            raise
    
    def close(self):
        if self._session:
            self._session.close()


class Neo4jResult:
    """Wrapper that provides Kuzu-compatible result iteration."""
    
    def __init__(self, records, session):
        self._records = records
        self._index = 0
        self._session = session
    
    def has_next(self) -> bool:
        return self._index < len(self._records)
    
    def get_next(self) -> list:
        if self._index >= len(self._records):
            return None
        record = self._records[self._index]
        self._index += 1
        return list(record.values())
    
    def __del__(self):
        if self._session:
            try:
                self._session.close()
            except Exception:
                pass


def _translate_cypher(query: str) -> str:
    """Translate Kuzu-specific Cypher to Neo4j-compatible Cypher.
    
    Key differences:
    - Kuzu: CREATE NODE TABLE IF NOT EXISTS ... → Neo4j: no-op (schema-free)
    - Kuzu: CREATE REL TABLE IF NOT EXISTS ... → Neo4j: no-op (schema-free)
    - Kuzu: ALTER TABLE ... ADD ... → Neo4j: no-op (schema-free)
    - Kuzu: timestamp($var) → Neo4j: datetime($var)
    - Kuzu: PRIMARY KEY (id) → Neo4j: constraint
    """
    q = query.strip()
    
    # Skip table creation statements (Neo4j is schema-free)
    if q.upper().startswith("CREATE NODE TABLE") or q.upper().startswith("CREATE REL TABLE"):
        return "RETURN 1"  # no-op
    if q.upper().startswith("ALTER TABLE"):
        return "RETURN 1"  # no-op
    
    # Replace timestamp() with datetime() — Kuzu returns a TIMESTAMP, Neo4j's timestamp() returns millis
    q = q.replace("timestamp(", "datetime(")
    
    return q


def sanitize_params(params: dict) -> dict:
    """Convert datetime strings from Kuzu format to Neo4j format.
    
    Kuzu accepts: '2026-03-09 16:43:17'
    Neo4j needs:  '2026-03-09T16:43:17'
    """
    if not params:
        return params
    
    import re
    date_pattern = re.compile(r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}')
    
    sanitized = {}
    for k, v in params.items():
        if isinstance(v, str) and date_pattern.match(v):
            sanitized[k] = v.replace(' ', 'T', 1)
        else:
            sanitized[k] = v
    return sanitized


def init_schema(conn=None):
    """Initialize Neo4j schema — create constraints and indexes."""
    if conn is None:
        conn = get_conn()
    
    # Uniqueness constraints (equivalent to Kuzu PRIMARY KEY)
    for label in ["Entity", "Episode", "Emotion", "SessionState", "Fact"]:
        try:
            conn.execute(
                f"CREATE CONSTRAINT IF NOT EXISTS FOR (n:{label}) REQUIRE n.id IS UNIQUE"
            )
        except Exception as e:
            if "already exists" not in str(e).lower():
                print(f"  ⚠️ Constraint warning ({label}): {e}")
    
    # Indexes for common queries
    for label, prop in [
        ("Entity", "name"), ("Entity", "agent_id"),
        ("Fact", "agent_id"), ("Fact", "content"),
        ("Episode", "agent_id"), ("Episode", "occurred_at"),
        ("Emotion", "agent_id"), ("Emotion", "label"),
    ]:
        try:
            idx_name = f"idx_{label.lower()}_{prop}"
            conn.execute(
                f"CREATE INDEX {idx_name} IF NOT EXISTS FOR (n:{label}) ON (n.{prop})"
            )
        except Exception as e:
            if "already exists" not in str(e).lower():
                print(f"  ⚠️ Index warning ({label}.{prop}): {e}")
    
    # Full-text search indexes for content search
    for label in ["Fact", "Episode", "Entity"]:
        try:
            props = {
                "Fact": ["content"],
                "Episode": ["summary", "content"],
                "Entity": ["name", "description"]
            }[label]
            prop_list = ", ".join(f"n.{p}" for p in props)
            idx_name = f"ft_{label.lower()}"
            conn.execute(
                f'CREATE FULLTEXT INDEX {idx_name} IF NOT EXISTS FOR (n:{label}) ON EACH [{prop_list}]'
            )
        except Exception as e:
            if "already exists" not in str(e).lower():
                print(f"  ⚠️ FT index warning ({label}): {e}")
    
    print("✅ Neo4j schema initialized")
    return conn


def get_stats(conn=None) -> dict:
    """Get node and relationship counts."""
    if conn is None:
        conn = get_conn()
    
    stats = {}
    for label in ["Entity", "Episode", "Emotion", "SessionState", "Fact"]:
        try:
            result = conn.execute(f"MATCH (n:{label}) RETURN count(n) AS cnt")
            if result.has_next():
                stats[label] = result.get_next()[0]
        except Exception:
            stats[label] = 0
    
    for rel in ["RELATES_TO", "CAUSED", "PART_OF", "MENTIONED_IN",
                "EPISODE_EVOKES", "ENTITY_EVOKES", "SEQUENCE",
                "DERIVED_FROM", "ABOUT", "SUPERSEDES",
                "SESSION_REFS", "SESSION_EPISODE"]:
        try:
            result = conn.execute(f"MATCH ()-[r:{rel}]->() RETURN count(r) AS cnt")
            if result.has_next():
                stats[rel] = result.get_next()[0]
        except Exception:
            stats[rel] = 0
    
    return stats


def print_stats(stats: dict):
    """Pretty-print database statistics."""
    print("\n📊 Engram Database Stats (Neo4j)")
    print("=" * 40)
    
    print("\nNodes:")
    node_tables = ["Entity", "Episode", "Emotion", "SessionState", "Fact"]
    for table in node_tables:
        count = stats.get(table, 0)
        print(f"  {table:15s} {count:6d}")
    
    print("\nRelationships:")
    rel_tables = [k for k in stats if k not in node_tables]
    for rel in rel_tables:
        count = stats.get(rel, 0)
        if count > 0:
            print(f"  {rel:15s} {count:6d}")
    
    total_nodes = sum(stats.get(t, 0) for t in node_tables)
    total_rels = sum(stats.get(t, 0) for t in rel_tables)
    print(f"\n  Total: {total_nodes} nodes, {total_rels} relationships")


def close():
    """Close the Neo4j driver."""
    global _driver
    if _driver:
        _driver.close()
        _driver = None


if __name__ == "__main__":
    print("🧠 Initializing Engram schema (Neo4j)...")
    print(f"   URI: {NEO4J_URI}")
    
    conn = get_conn()
    init_schema(conn)
    
    stats = get_stats(conn)
    print_stats(stats)
    
    close()
