#!/usr/bin/env python3
"""
Engram Pre-Dispatch Context Builder.

Builds a compact markdown context block for agent dispatch prompts.
"""

import os
import sys
from datetime import datetime
from typing import Optional

try:
    import kuzu
except ImportError:
    kuzu = None

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from engram.backend import get_db, get_conn


def _fetch_agent_entity(conn: kuzu.Connection, agent_name: str) -> Optional[dict]:
    try:
        result = conn.execute(
            "MATCH (e:Entity) "
            "WHERE lower(e.name) = lower($p_name) "
            "RETURN e.id, e.name, e.entity_type, e.description LIMIT 1",
            {"p_name": agent_name}
        )
        if result.has_next():
            row = result.get_next()
            return {
                "id": row[0], "name": row[1], "type": row[2], "description": row[3]
            }
    except Exception:
        pass
    return None


def _fetch_project_entity(conn: kuzu.Connection, project_name: str) -> Optional[dict]:
    try:
        result = conn.execute(
            "MATCH (e:Entity) "
            "WHERE lower(e.name) CONTAINS lower($p_name) "
            "RETURN e.id, e.name, e.entity_type, e.description LIMIT 1",
            {"p_name": project_name}
        )
        if result.has_next():
            row = result.get_next()
            return {
                "id": row[0], "name": row[1], "type": row[2], "description": row[3]
            }
    except Exception:
        pass
    return None


def get_dispatch_context(conn: kuzu.Connection, agent_name: str, project_name: Optional[str] = None) -> str:
    """Return formatted markdown context for agent dispatch."""
    agent_entity = _fetch_agent_entity(conn, agent_name)
    project_entity = _fetch_project_entity(conn, project_name) if project_name else None

    agent_facts = []
    project_facts = []
    recent_episodes = []
    key_decisions = []

    # Agent history: facts about agent or by agent_id
    try:
        where_parts = ["f.agent_id = $p_agent"]
        params = {"p_agent": agent_name, "p_limit": 12}
        if agent_entity:
            where_parts.append("exists { MATCH (f)-[:ABOUT]->(:Entity {id: $p_eid}) }")
            params["p_eid"] = agent_entity["id"]
        where_clause = " OR ".join(where_parts)
        result = conn.execute(
            "MATCH (f:Fact) "
            f"WHERE {where_clause} "
            "RETURN f.content, f.category, f.created_at "
            "ORDER BY f.created_at DESC LIMIT $p_limit",
            params
        )
        while result.has_next():
            row = result.get_next()
            agent_facts.append({
                "content": row[0], "category": row[1], "created_at": str(row[2])
            })
    except Exception:
        pass

    # Project context: facts about the project entity
    if project_entity:
        try:
            result = conn.execute(
                "MATCH (f:Fact)-[:ABOUT]->(e:Entity {id: $p_eid}) "
                "RETURN f.content, f.category, f.created_at "
                "ORDER BY f.created_at DESC LIMIT $p_limit",
                {"p_eid": project_entity["id"], "p_limit": 12}
            )
            while result.has_next():
                row = result.get_next()
                project_facts.append({
                    "content": row[0], "category": row[1], "created_at": str(row[2])
                })
        except Exception:
            pass

    # Recent activity: episodes mentioning agent (+ project if provided)
    try:
        if project_entity:
            result = conn.execute(
                "MATCH (agent:Entity {id: $p_agent_id})-[:MENTIONED_IN]->(ep:Episode)"
                "<-[:MENTIONED_IN]-(project:Entity {id: $p_project_id}) "
                "RETURN ep.summary, ep.source_file, ep.occurred_at "
                "ORDER BY ep.occurred_at DESC LIMIT $p_limit",
                {"p_agent_id": agent_entity["id"] if agent_entity else "", "p_project_id": project_entity["id"], "p_limit": 10}
            )
        elif agent_entity:
            result = conn.execute(
                "MATCH (agent:Entity {id: $p_agent_id})-[:MENTIONED_IN]->(ep:Episode) "
                "RETURN ep.summary, ep.source_file, ep.occurred_at "
                "ORDER BY ep.occurred_at DESC LIMIT $p_limit",
                {"p_agent_id": agent_entity["id"], "p_limit": 10}
            )
        else:
            result = None

        if result:
            while result.has_next():
                row = result.get_next()
                recent_episodes.append({
                    "summary": row[0], "source_file": row[1], "occurred_at": str(row[2])
                })
    except Exception:
        pass

    # Key decisions: decision facts about agent or project
    try:
        params = {"p_limit": 8, "p_agent_name": agent_name}
        where_parts = ["lower(f.category) = 'decision'"]
        scoped_parts = ["f.agent_id = $p_agent_name"]
        if agent_entity:
            scoped_parts.append("exists { MATCH (f)-[:ABOUT]->(:Entity {id: $p_agent_eid}) }")
            params["p_agent_eid"] = agent_entity["id"]
        if project_entity:
            scoped_parts.append("exists { MATCH (f)-[:ABOUT]->(:Entity {id: $p_project_eid}) }")
            params["p_project_eid"] = project_entity["id"]
        where_clause = " AND (" + " OR ".join(scoped_parts) + ")"
        result = conn.execute(
            "MATCH (f:Fact) "
            f"WHERE {where_parts[0]}{where_clause} "
            "RETURN f.content, f.created_at "
            "ORDER BY f.created_at DESC LIMIT $p_limit",
            params
        )
        while result.has_next():
            row = result.get_next()
            key_decisions.append({"content": row[0], "created_at": str(row[1])})
    except Exception:
        pass

    lines = []
    lines.append(f"# Dispatch Context - {agent_name}")
    lines.append(f"*Generated: {datetime.now().strftime('%Y-%m-%d %H:%M PST')}*")
    lines.append("")

    lines.append("## Agent History")
    if agent_facts:
        seen = set()
        for fact in agent_facts:
            content = fact.get("content", "")
            if not content or content in seen:
                continue
            seen.add(content)
            cat = fact.get("category", "")
            lines.append(f"- [{cat}] {content}")
    else:
        lines.append("- None")
    lines.append("")

    lines.append("## Project Context")
    if project_facts:
        seen = set()
        for fact in project_facts:
            content = fact.get("content", "")
            if not content or content in seen:
                continue
            seen.add(content)
            cat = fact.get("category", "")
            lines.append(f"- [{cat}] {content}")
    else:
        lines.append("- None")
    lines.append("")

    lines.append("## Recent Activity")
    if recent_episodes:
        seen = set()
        for ep in recent_episodes:
            summary = ep.get("summary", "")
            if not summary or summary in seen:
                continue
            seen.add(summary)
            date = ep.get("occurred_at", "")[:10]
            lines.append(f"- **[{date}]** {summary}")
    else:
        lines.append("- None")
    lines.append("")

    lines.append("## Key Decisions")
    if key_decisions:
        seen = set()
        for item in key_decisions:
            content = item.get("content", "")
            if not content or content in seen:
                continue
            seen.add(content)
            lines.append(f"- {content}")
    else:
        lines.append("- None")

    return "\n".join(lines)


if __name__ == "__main__":
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Engram Dispatch Context")
    parser.add_argument("agent", help="Agent name")
    parser.add_argument("--project", "-p", default=None, help="Optional project name")
    parser.add_argument("--json", "-j", action="store_true", help="JSON output")
    args = parser.parse_args()

    db = get_db(read_only=True)
    conn = get_conn(db)
    context = get_dispatch_context(conn, args.agent, args.project)

    if args.json:
        print(json.dumps({"context": context, "generated_at": datetime.now().isoformat()}, default=str))
    else:
        print(context)
