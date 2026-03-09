#!/usr/bin/env python3
"""
Engram Schema — Temporal Knowledge Graph for Jarvis Memory

Node types:
  - Entity: People, projects, tools, concepts, places
  - Episode: Raw events/conversations with timestamps
  - Emotion: Emotional states tied to episodes/entities

Relationship types:
  - RELATES_TO: General association between entities
  - CAUSED: Causal link (X caused Y)
  - PART_OF: Hierarchical (component belongs to project)
  - MENTIONED_IN: Entity appeared in an episode
  - EVOKES: Episode/entity triggers an emotion
  - SUPERSEDES: New fact replaces old fact (temporal)
  - SEQUENCE: Temporal ordering of episodes

All relationships carry temporal metadata (valid_at, invalid_at)
and importance scores.
"""

import kuzu
import os
import sys
import time
from pathlib import Path
from datetime import datetime

# Database location
DB_PATH = os.environ.get("ENGRAM_DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), ".engram-db"))


def get_db(read_only: bool = False, retries: int = 10, delay: float = 3.0) -> kuzu.Database:
    """Get or create the Engram database. Retries on lock contention."""
    last_err = None
    for attempt in range(retries):
        try:
            return kuzu.Database(DB_PATH, read_only=read_only)
        except RuntimeError as e:
            if "lock" in str(e).lower() and attempt < retries - 1:
                if attempt == 0:
                    print(f"⏳ DB locked, retrying up to {retries * delay:.0f}s...")
                time.sleep(delay)
                last_err = e
            else:
                raise
    raise last_err


def get_conn(db: kuzu.Database = None) -> kuzu.Connection:
    """Get a connection to the database."""
    if db is None:
        db = get_db()
    return kuzu.Connection(db)


def init_schema(conn: kuzu.Connection = None):
    """Initialize the graph schema. Safe to run multiple times (idempotent)."""
    if conn is None:
        db = get_db()
        conn = kuzu.Connection(db)
    
    # =========================================================
    # NODE TABLES
    # =========================================================
    
    # Entity: People, projects, tools, concepts, places, etc.
    # The core building block of memory.
    conn.execute("""
        CREATE NODE TABLE IF NOT EXISTS Entity (
            id STRING,
            name STRING,
            entity_type STRING,
            description STRING,
            importance FLOAT DEFAULT 0.5,
            access_count INT64 DEFAULT 0,
            created_at TIMESTAMP,
            updated_at TIMESTAMP,
            metadata STRING,
            PRIMARY KEY (id)
        )
    """)
    
    # Episode: A discrete event or conversation segment.
    # Raw episodic memory — "what happened."
    conn.execute("""
        CREATE NODE TABLE IF NOT EXISTS Episode (
            id STRING,
            content STRING,
            summary STRING,
            source STRING,
            source_file STRING,
            occurred_at TIMESTAMP,
            duration_minutes FLOAT DEFAULT 0.0,
            importance FLOAT DEFAULT 0.5,
            access_count INT64 DEFAULT 0,
            created_at TIMESTAMP,
            metadata STRING,
            PRIMARY KEY (id)
        )
    """)
    
    # Emotion: Emotional states tied to episodes or entities.
    # The missing piece nobody else has built.
    conn.execute("""
        CREATE NODE TABLE IF NOT EXISTS Emotion (
            id STRING,
            label STRING,
            valence FLOAT,
            arousal FLOAT,
            description STRING,
            created_at TIMESTAMP,
            PRIMARY KEY (id)
        )
    """)
    
    # SessionState: Snapshot of working context at session boundaries.
    # Solves the cold-boot problem.
    conn.execute("""
        CREATE NODE TABLE IF NOT EXISTS SessionState (
            id STRING,
            session_key STRING,
            summary STRING,
            open_threads STRING,
            mood STRING,
            started_at TIMESTAMP,
            ended_at TIMESTAMP,
            message_count INT64 DEFAULT 0,
            created_at TIMESTAMP,
            metadata STRING,
            PRIMARY KEY (id)
        )
    """)
    
    # Fact: A consolidated, verified piece of knowledge.
    # Extracted from episodes, with temporal validity.
    conn.execute("""
        CREATE NODE TABLE IF NOT EXISTS Fact (
            id STRING,
            content STRING,
            category STRING,
            confidence FLOAT DEFAULT 0.8,
            valid_at TIMESTAMP,
            invalid_at TIMESTAMP,
            source_episode STRING,
            importance FLOAT DEFAULT 0.5,
            access_count INT64 DEFAULT 0,
            created_at TIMESTAMP,
            updated_at TIMESTAMP,
            PRIMARY KEY (id)
        )
    """)
    
    # =========================================================
    # RELATIONSHIP TABLES
    # =========================================================
    
    # Entity <-> Entity relationships
    conn.execute("""
        CREATE REL TABLE IF NOT EXISTS RELATES_TO (
            FROM Entity TO Entity,
            relation_type STRING,
            description STRING,
            strength FLOAT DEFAULT 0.5,
            valid_at TIMESTAMP,
            invalid_at TIMESTAMP,
            created_at TIMESTAMP
        )
    """)
    
    conn.execute("""
        CREATE REL TABLE IF NOT EXISTS CAUSED (
            FROM Entity TO Entity,
            description STRING,
            confidence FLOAT DEFAULT 0.7,
            valid_at TIMESTAMP,
            created_at TIMESTAMP
        )
    """)
    
    conn.execute("""
        CREATE REL TABLE IF NOT EXISTS PART_OF (
            FROM Entity TO Entity,
            role STRING,
            valid_at TIMESTAMP,
            invalid_at TIMESTAMP,
            created_at TIMESTAMP
        )
    """)
    
    # Entity <-> Episode relationships
    conn.execute("""
        CREATE REL TABLE IF NOT EXISTS MENTIONED_IN (
            FROM Entity TO Episode,
            context STRING,
            role STRING,
            created_at TIMESTAMP
        )
    """)
    
    # Entity/Episode -> Emotion relationships
    conn.execute("""
        CREATE REL TABLE IF NOT EXISTS EPISODE_EVOKES (
            FROM Episode TO Emotion,
            intensity FLOAT DEFAULT 0.5,
            created_at TIMESTAMP
        )
    """)
    
    conn.execute("""
        CREATE REL TABLE IF NOT EXISTS ENTITY_EVOKES (
            FROM Entity TO Emotion,
            context STRING,
            intensity FLOAT DEFAULT 0.5,
            valid_at TIMESTAMP,
            invalid_at TIMESTAMP,
            created_at TIMESTAMP
        )
    """)
    
    # Episode -> Episode temporal sequence
    conn.execute("""
        CREATE REL TABLE IF NOT EXISTS SEQUENCE (
            FROM Episode TO Episode,
            gap_minutes FLOAT,
            same_session BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP
        )
    """)
    
    # Fact relationships
    conn.execute("""
        CREATE REL TABLE IF NOT EXISTS DERIVED_FROM (
            FROM Fact TO Episode,
            extraction_method STRING,
            created_at TIMESTAMP
        )
    """)
    
    conn.execute("""
        CREATE REL TABLE IF NOT EXISTS ABOUT (
            FROM Fact TO Entity,
            aspect STRING,
            created_at TIMESTAMP
        )
    """)
    
    conn.execute("""
        CREATE REL TABLE IF NOT EXISTS SUPERSEDES (
            FROM Fact TO Fact,
            reason STRING,
            created_at TIMESTAMP
        )
    """)
    
    # SessionState relationships
    conn.execute("""
        CREATE REL TABLE IF NOT EXISTS SESSION_REFS (
            FROM SessionState TO Entity,
            relevance FLOAT DEFAULT 0.5,
            created_at TIMESTAMP
        )
    """)
    
    conn.execute("""
        CREATE REL TABLE IF NOT EXISTS SESSION_EPISODE (
            FROM SessionState TO Episode,
            created_at TIMESTAMP
        )
    """)
    
    # Run migrations for existing databases
    migrate_add_last_accessed(conn)
    migrate_add_agent_id(conn)

    print("✅ Engram schema initialized")
    return conn


def migrate_add_last_accessed(conn: kuzu.Connection):
    """Migration: add last_accessed TIMESTAMP to Entity, Fact, and Episode tables.
    
    Safe to run multiple times — checks if column exists before adding.
    Defaults last_accessed to created_at for all existing records.
    """
    tables = ["Entity", "Fact", "Episode"]
    for table in tables:
        # Check if column already exists by trying a probe query
        try:
            conn.execute(f"MATCH (n:{table}) RETURN n.last_accessed LIMIT 1")
            # Column exists, skip
        except Exception:
            # Column doesn't exist — add it
            try:
                conn.execute(f"ALTER TABLE {table} ADD last_accessed TIMESTAMP")
                # Backfill: set last_accessed = created_at for existing rows
                conn.execute(
                    f"MATCH (n:{table}) "
                    f"WHERE n.last_accessed IS NULL AND n.created_at IS NOT NULL "
                    f"SET n.last_accessed = n.created_at"
                )
                print(f"  ✅ Migration: added last_accessed to {table}")
            except Exception as e:
                print(f"  ⚠️  Migration warning ({table}): {e}")


def migrate_add_agent_id(conn: kuzu.Connection):
    """Migration: add agent_id STRING to Entity, Fact, Episode, Emotion tables.
    
    Safe to run multiple times — checks if column exists before adding.
    Defaults agent_id to 'shared' for all existing records.
    """
    tables = ["Entity", "Fact", "Episode", "Emotion"]
    for table in tables:
        try:
            conn.execute(f"MATCH (n:{table}) RETURN n.agent_id LIMIT 1")
            # Column exists, skip
        except Exception:
            try:
                conn.execute(f"ALTER TABLE {table} ADD agent_id STRING DEFAULT 'shared'")
                conn.execute(
                    f"MATCH (n:{table}) "
                    f"WHERE n.agent_id IS NULL "
                    f"SET n.agent_id = 'shared'"
                )
                print(f"  ✅ Migration: added agent_id to {table}")
            except Exception as e:
                print(f"  ⚠️  Migration warning ({table}): {e}")


def get_stats(conn: kuzu.Connection = None) -> dict:
    """Get node and relationship counts."""
    if conn is None:
        db = get_db(read_only=True)
        conn = kuzu.Connection(db)
    
    stats = {}
    for table in ["Entity", "Episode", "Emotion", "SessionState", "Fact"]:
        try:
            result = conn.execute(f"MATCH (n:{table}) RETURN count(n) AS cnt")
            while result.has_next():
                stats[table] = result.get_next()[0]
        except Exception:
            stats[table] = 0
    
    for rel in ["RELATES_TO", "CAUSED", "PART_OF", "MENTIONED_IN", 
                "EPISODE_EVOKES", "ENTITY_EVOKES", "SEQUENCE",
                "DERIVED_FROM", "ABOUT", "SUPERSEDES",
                "SESSION_REFS", "SESSION_EPISODE"]:
        try:
            result = conn.execute(f"MATCH ()-[r:{rel}]->() RETURN count(r) AS cnt")
            while result.has_next():
                stats[rel] = result.get_next()[0]
        except Exception:
            stats[rel] = 0
    
    return stats


def print_stats(stats: dict):
    """Pretty-print database statistics."""
    print("\n📊 Engram Database Stats")
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


if __name__ == "__main__":
    print("🧠 Initializing Engram schema...")
    print(f"   Database: {DB_PATH}")
    
    db = get_db()
    conn = get_conn(db)
    init_schema(conn)
    
    stats = get_stats(conn)
    print_stats(stats)
