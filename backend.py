#!/usr/bin/env python3
"""
Engram Backend Switcher

Reads config.json to determine which DB backend to use (kuzu or neo4j).
All other modules import from here instead of directly from schema.py or schema_neo4j.py.

Usage:
    from engram.backend import get_db, get_conn, init_schema, get_stats, print_stats
"""

import json
import os
from pathlib import Path

ENGRAM_DIR = Path(os.path.dirname(os.path.abspath(__file__)))


def _get_backend() -> str:
    """Read backend from config.json. Default: 'kuzu'."""
    cfg_path = ENGRAM_DIR / "config.json"
    if cfg_path.exists():
        with open(cfg_path) as f:
            cfg = json.load(f)
        return cfg.get("backend", "kuzu")
    return os.environ.get("ENGRAM_BACKEND", "kuzu")


_backend = _get_backend()

if _backend == "neo4j":
    from engram.schema_neo4j import get_db, get_conn, init_schema, get_stats, print_stats, close
else:
    from engram.schema import get_db, get_conn, init_schema, get_stats, print_stats
    def close():
        pass

__all__ = ["get_db", "get_conn", "init_schema", "get_stats", "print_stats", "close"]
