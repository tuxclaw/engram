#!/usr/bin/env python3
"""Todo tracking utilities for Engram."""

import os
import sys
from datetime import datetime
from typing import Optional

try:
    import kuzu
except ImportError:
    kuzu = None

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from engram.ingest import generate_id, normalize_entity_name


def get_open_todos(conn: kuzu.Connection, agent_id: Optional[str] = None) -> list[dict]:
    """Return open todo facts."""
    todos = []
    try:
        params = {"p_limit": 50}
        agent_filter = ""
        if agent_id:
            agent_filter = " AND f.agent_id = $p_agent"
            params["p_agent"] = agent_id
        result = conn.execute(
            "MATCH (f:Fact) "
            "WHERE lower(f.category) = 'todo' "
            "AND (f.status IS NULL OR f.status <> 'resolved')"
            + agent_filter +
            " RETURN f.id, f.content, f.created_at, f.status, f.agent_id "
            "ORDER BY f.created_at DESC LIMIT $p_limit",
            params
        )
        while result.has_next():
            row = result.get_next()
            todos.append({
                "id": row[0], "content": row[1], "created_at": str(row[2]),
                "status": row[3], "agent_id": row[4]
            })
    except Exception:
        pass
    return todos


def resolve_todo(conn: kuzu.Connection, todo_id: str) -> dict:
    """Mark a todo as resolved."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        conn.execute(
            "MATCH (f:Fact {id: $p_id}) "
            "SET f.status = 'resolved', "
            "f.resolved_at = timestamp($p_now), "
            "f.updated_at = timestamp($p_now)",
            {"p_id": todo_id, "p_now": now}
        )
        return {"ok": True, "id": todo_id}
    except Exception as e:
        return {"ok": False, "error": str(e), "id": todo_id}


def add_todo(conn: kuzu.Connection, content: str, agent_id: str = "main", about_entities: Optional[list] = None) -> dict:
    """Create a todo fact directly."""
    about_entities = about_entities or []
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    todo_id = generate_id("fact", f"todo:{content.lower()}:{agent_id}")

    try:
        conn.execute(
            "MERGE (f:Fact {id: $p_id}) "
            "SET f.content = $p_content, "
            "f.category = 'todo', "
            "f.status = 'open', "
            "f.confidence = $p_conf, "
            "f.importance = $p_imp, "
            "f.valid_at = timestamp($p_now), "
            "f.created_at = CASE WHEN f.created_at IS NULL THEN timestamp($p_now) ELSE f.created_at END, "
            "f.updated_at = timestamp($p_now), "
            "f.agent_id = $p_agent, "
            "f.source_type = 'manual', "
            "f.memory_tier = 'candidate', "
            "f.quality_score = 0.7, "
            "f.contamination_score = 0.0, "
            "f.retrievable = true",
            {
                "p_id": todo_id,
                "p_content": content,
                "p_conf": 0.85,
                "p_imp": 0.6,
                "p_now": now,
                "p_agent": agent_id,
            }
        )

        # Link to entities if provided
        for name in about_entities:
            name = str(name).strip()
            if not name:
                continue
            eid = generate_id("ent", normalize_entity_name(name))
            try:
                conn.execute(
                    "MERGE (e:Entity {id: $p_eid}) "
                    "SET e.name = $p_name, "
                    "e.entity_type = 'concept', "
                    "e.description = CASE WHEN e.description IS NULL THEN '' ELSE e.description END, "
                    "e.importance = CASE WHEN e.importance IS NULL THEN 0.3 ELSE e.importance END, "
                    "e.access_count = CASE WHEN e.access_count IS NULL THEN 0 ELSE e.access_count END, "
                    "e.agent_id = $p_agent, "
                    "e.created_at = CASE WHEN e.created_at IS NULL THEN timestamp($p_now) ELSE e.created_at END, "
                    "e.updated_at = timestamp($p_now)",
                    {"p_eid": eid, "p_name": name, "p_now": now, "p_agent": agent_id}
                )
                conn.execute(
                    "MATCH (f:Fact {id: $p_fid}), (e:Entity {id: $p_eid}) "
                    "MERGE (f)-[r:ABOUT]->(e) "
                    "ON CREATE SET r.aspect = 'todo', r.created_at = timestamp($p_now)",
                    {"p_fid": todo_id, "p_eid": eid, "p_now": now}
                )
            except Exception:
                pass

        return {"ok": True, "id": todo_id}
    except Exception as e:
        return {"ok": False, "error": str(e)}
