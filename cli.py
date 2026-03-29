#!/usr/bin/env python3
"""
Engram CLI — Unified interface for querying and managing the knowledge graph.

Usage:
  engram search "CalCity Stripe"              # Search everything
  engram entity "Woody"                       # Full context for an entity
  engram entity "Woody" --relationships       # Just relationships
  engram timeline "CalCity" [--days 30]       # Project/entity timeline
  engram agent-history "Woody" [--project CalCity]  # What an agent built
  engram facts --recent [--days 7]            # Recent facts
  engram stats                                # Graph statistics
  engram briefing [--save]                    # Generate session briefing
  engram briefing --delta                     # Delta briefing since last run
  engram dispatch Buzz --project CalCity      # Pre-dispatch context
  engram todos [--all] [--agent main]         # List todos
  engram todo-add \"Do the thing\"            # Add a todo
  engram todo-done <id>                       # Resolve a todo
  engram contradictions [--days 7]            # Recent superseded facts
  engram recent [--hours 48]                  # Recent activity across types
  engram health                               # Graph health check
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Suppress Neo4j driver warnings about missing properties/relationships
import warnings
warnings.filterwarnings("ignore", category=Warning)

import logging
logging.getLogger("neo4j").setLevel(logging.ERROR)

from engram.backend import get_db, get_conn, get_stats, print_stats, close


def cmd_search(args):
    """Unified search across all memory types."""
    from engram.query import unified_search, print_results
    conn = get_conn(get_db(read_only=False))
    results = unified_search(conn, args.query, limit=args.limit, agent_id=args.agent, since=args.since, until=args.until)
    if args.json:
        print(json.dumps(results, indent=2, default=str))
    else:
        print_results(results, verbose=args.verbose)


def cmd_entity(args):
    """Full context for a specific entity."""
    from engram.query import get_entity_context, print_entity_context
    conn = get_conn(get_db(read_only=False))
    context = get_entity_context(conn, args.name)

    if not context.get("entity"):
        # Try fuzzy match
        from engram.query import search_entities
        matches = search_entities(conn, args.name, limit=5, reinforce=False)
        if matches:
            print(f"Entity '{args.name}' not found. Did you mean:")
            for m in matches:
                print(f"  - {m['name']} ({m['type']})")
            return
        print(f"Entity '{args.name}' not found.")
        return

    if args.json:
        print(json.dumps(context, indent=2, default=str))
    elif args.relationships:
        ent = context["entity"]
        print(f"\n🔵 {ent['name']} ({ent['type']})")
        rels = context.get("relationships", [])
        if rels:
            print(f"\n🔗 Relationships ({len(rels)})")
            for r in rels:
                direction = f"→ {r.get('target', '?')}" if 'target' in r else f"← {r.get('source', '?')}"
                rtype = r.get('type') or 'relates_to'
                desc = r.get('description', '')
                print(f"   {direction} [{rtype}] {desc}")
        else:
            print("   No relationships found.")
    else:
        print_entity_context(context)


def cmd_timeline(args):
    """Timeline of activity for an entity or project."""
    conn = get_conn(get_db(read_only=True))
    name = args.name
    days = args.days
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()

    # Get episodes mentioning this entity
    episodes = []
    try:
        result = conn.execute(
            "MATCH (e:Entity)-[:MENTIONED_IN]->(ep:Episode) "
            "WHERE lower(e.name) CONTAINS lower($p_name) "
            "AND ep.occurred_at >= timestamp($p_cutoff) "
            "RETURN ep.summary, ep.source_file, ep.occurred_at, ep.importance, e.name "
            "ORDER BY ep.occurred_at DESC "
            "LIMIT $p_limit",
            {"p_name": name, "p_cutoff": cutoff, "p_limit": args.limit}
        )
        while result.has_next():
            row = result.get_next()
            episodes.append({
                "summary": row[0], "source": row[1],
                "date": str(row[2]), "importance": row[3], "entity": row[4]
            })
    except Exception as e:
        print(f"Timeline query error: {e}", file=sys.stderr)

    # Get facts about this entity, sorted by date
    facts = []
    try:
        result = conn.execute(
            "MATCH (f:Fact)-[:ABOUT]->(e:Entity) "
            "WHERE lower(e.name) CONTAINS lower($p_name) "
            "AND f.created_at >= timestamp($p_cutoff) "
            "RETURN f.content, f.category, f.created_at, f.source_type, e.name "
            "ORDER BY f.created_at DESC "
            "LIMIT $p_limit",
            {"p_name": name, "p_cutoff": cutoff, "p_limit": args.limit}
        )
        while result.has_next():
            row = result.get_next()
            facts.append({
                "content": row[0], "category": row[1],
                "date": str(row[2]), "source_type": row[3], "entity": row[4]
            })
    except Exception as e:
        print(f"Timeline facts query error: {e}", file=sys.stderr)

    if args.json:
        print(json.dumps({"episodes": episodes, "facts": facts}, indent=2, default=str))
        return

    if not episodes and not facts:
        print(f"No timeline data found for '{name}' in the last {days} days.")
        return

    print(f"\n📅 Timeline: {name} (last {days} days)")
    print("=" * 60)

    # Merge and sort by date
    items = []
    for ep in episodes:
        items.append(("episode", ep.get("date", "")[:16], ep.get("summary", "")))
    for f in facts:
        items.append(("fact", f.get("date", "")[:16], f"{f.get('content', '')}"))

    # Sort by date descending
    items.sort(key=lambda x: x[1], reverse=True)

    seen = set()
    for kind, date, text in items:
        if not text or text in seen:
            continue
        seen.add(text)
        icon = "📖" if kind == "episode" else "📋"
        date_str = date[:10] if date else "????"
        text_short = text[:120] + "..." if len(text) > 120 else text
        print(f"  {icon} [{date_str}] {text_short}")


def cmd_agent_history(args):
    """What a specific agent has worked on."""
    conn = get_conn(get_db(read_only=True))
    agent_name = args.name
    days = args.days

    # Search for facts and episodes mentioning this agent
    results = {"actions": [], "mentions": []}

    # Look for action facts about the agent
    try:
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        result = conn.execute(
            "MATCH (f:Fact)-[:ABOUT]->(e:Entity) "
            "WHERE lower(e.name) = lower($p_name) "
            "AND f.created_at >= timestamp($p_cutoff) "
            "RETURN f.content, f.category, f.created_at, f.source_type "
            "ORDER BY f.created_at DESC "
            "LIMIT $p_limit",
            {"p_name": agent_name, "p_cutoff": cutoff, "p_limit": args.limit}
        )
        while result.has_next():
            row = result.get_next()
            results["actions"].append({
                "content": row[0], "category": row[1],
                "date": str(row[2]), "source_type": row[3]
            })
    except Exception as e:
        print(f"Agent history query error: {e}", file=sys.stderr)

    # Get episodes mentioning this agent
    try:
        result = conn.execute(
            "MATCH (e:Entity)-[:MENTIONED_IN]->(ep:Episode) "
            "WHERE lower(e.name) = lower($p_name) "
            "AND ep.occurred_at >= timestamp($p_cutoff) "
            "RETURN ep.summary, ep.source_file, ep.occurred_at "
            "ORDER BY ep.occurred_at DESC "
            "LIMIT $p_limit",
            {"p_name": agent_name, "p_cutoff": cutoff, "p_limit": args.limit}
        )
        while result.has_next():
            row = result.get_next()
            results["mentions"].append({
                "summary": row[0], "source": row[1], "date": str(row[2])
            })
    except Exception as e:
        print(f"Agent mentions query error: {e}", file=sys.stderr)

    # If project filter, also search project-specific
    if args.project:
        try:
            result = conn.execute(
                "MATCH (agent:Entity)-[:MENTIONED_IN]->(ep:Episode)<-[:MENTIONED_IN]-(project:Entity) "
                "WHERE lower(agent.name) = lower($p_agent) "
                "AND lower(project.name) CONTAINS lower($p_project) "
                "RETURN ep.summary, ep.occurred_at, project.name "
                "ORDER BY ep.occurred_at DESC "
                "LIMIT $p_limit",
                {"p_agent": agent_name, "p_project": args.project, "p_limit": args.limit}
            )
            project_mentions = []
            while result.has_next():
                row = result.get_next()
                project_mentions.append({
                    "summary": row[0], "date": str(row[1]), "project": row[2]
                })
            results["project_mentions"] = project_mentions
        except Exception as e:
            print(f"Project-scoped query error: {e}", file=sys.stderr)

    if args.json:
        print(json.dumps(results, indent=2, default=str))
        return

    actions = results.get("actions", [])
    mentions = results.get("mentions", [])
    project_mentions = results.get("project_mentions", [])

    if not actions and not mentions and not project_mentions:
        print(f"No history found for agent '{agent_name}' in the last {days} days.")
        return

    print(f"\n🤖 Agent History: {agent_name} (last {days} days)")
    if args.project:
        print(f"   Filtered to project: {args.project}")
    print("=" * 60)

    if actions:
        print(f"\n📋 Actions ({len(actions)})")
        seen = set()
        for a in actions:
            text = a.get("content", "")
            if text in seen:
                continue
            seen.add(text)
            date = str(a.get("date", ""))[:10]
            print(f"  [{date}] {text[:120]}")

    if project_mentions:
        print(f"\n🔗 Project Episodes ({len(project_mentions)})")
        for pm in project_mentions:
            date = str(pm.get("date", ""))[:10]
            print(f"  [{date}] {pm.get('summary', '')[:120]}")
    elif mentions:
        print(f"\n📖 Episodes ({len(mentions)})")
        seen = set()
        for m in mentions:
            text = m.get("summary", "")
            if text in seen:
                continue
            seen.add(text)
            date = str(m.get("date", ""))[:10]
            print(f"  [{date}] {text[:120]}")


def cmd_facts(args):
    """List recent or filtered facts."""
    conn = get_conn(get_db(read_only=True))

    try:
        query_str = (
            "MATCH (f:Fact) "
            "WHERE 1=1 "
        )
        params = {"p_limit": args.limit}

        if args.since:
            query_str += "AND f.created_at >= timestamp($p_since) "
            params["p_since"] = args.since
        if args.until:
            query_str += "AND f.created_at <= timestamp($p_until) "
            params["p_until"] = args.until
        if not args.since and not args.until:
            cutoff = (datetime.now() - timedelta(days=args.days)).isoformat()
            query_str += "AND f.created_at >= timestamp($p_cutoff) "
            params["p_cutoff"] = cutoff

        if args.category:
            query_str += "AND f.category = $p_cat "
            params["p_cat"] = args.category

        if args.source:
            query_str += "AND f.source_type = $p_src "
            params["p_src"] = args.source

        query_str += "RETURN f.content, f.category, f.created_at, f.importance, f.source_type, f.agent_id "
        query_str += "ORDER BY f.created_at DESC LIMIT $p_limit"

        result = conn.execute(query_str, params)
        facts = []
        while result.has_next():
            row = result.get_next()
            facts.append({
                "content": row[0], "category": row[1], "date": str(row[2]),
                "importance": row[3], "source_type": row[4], "agent_id": row[5]
            })
    except Exception as e:
        print(f"Facts query error: {e}", file=sys.stderr)
        facts = []

    if args.json:
        print(json.dumps(facts, indent=2, default=str))
        return

    if not facts:
        if args.since or args.until:
            print("No facts found for the requested time range.")
        else:
            print(f"No facts found in the last {args.days} days.")
        return

    if args.since or args.until:
        print(f"\n📋 Facts ({len(facts)} results)")
    else:
        print(f"\n📋 Recent Facts (last {args.days} days, {len(facts)} results)")
    print("=" * 60)
    for f in facts:
        date = str(f.get("date", ""))[:10]
        cat = f.get("category", "")
        src = f.get("source_type", "")
        imp = f.get("importance") or 0
        text = f.get("content", "")[:140]
        print(f"  [{date}] [{cat}] ({src}, imp:{imp:.2f}) {text}")


def cmd_stats(args):
    """Graph statistics and health."""
    conn = get_conn(get_db(read_only=True))
    stats = get_stats(conn)

    if args.json:
        print(json.dumps(stats, indent=2, default=str))
        return

    print_stats(stats)

    # Additional useful stats
    try:
        # Fact quality breakdown
        result = conn.execute(
            "MATCH (f:Fact) "
            "RETURN f.source_type as src, count(*) as cnt "
            "ORDER BY cnt DESC"
        )
        print("\n📊 Facts by source:")
        while result.has_next():
            row = result.get_next()
            print(f"  {row[0] or 'unknown'}: {row[1]}")
    except Exception:
        pass

    try:
        # Entity type breakdown
        result = conn.execute(
            "MATCH (e:Entity) "
            "RETURN e.entity_type as type, count(*) as cnt "
            "ORDER BY cnt DESC"
        )
        print("\n🔵 Entities by type:")
        while result.has_next():
            row = result.get_next()
            print(f"  {row[0] or 'unknown'}: {row[1]}")
    except Exception:
        pass

    try:
        # Memory tier breakdown
        result = conn.execute(
            "MATCH (f:Fact) "
            "RETURN f.memory_tier as tier, count(*) as cnt "
            "ORDER BY cnt DESC"
        )
        print("\n🏷️ Facts by memory tier:")
        while result.has_next():
            row = result.get_next()
            print(f"  {row[0] or 'untiered'}: {row[1]}")
    except Exception:
        pass


def cmd_briefing(args):
    """Generate a session briefing."""
    from engram.briefing import generate_briefing, generate_delta_briefing, save_briefing
    conn = get_conn(get_db(read_only=True))
    briefing = generate_delta_briefing(conn) if args.delta else generate_briefing(conn)

    if args.json:
        print(json.dumps({"briefing": briefing, "generated_at": datetime.now().isoformat()}, default=str))
    else:
        print(briefing)

    if args.save:
        save_briefing(briefing)


def cmd_dispatch(args):
    """Generate pre-dispatch context for an agent."""
    from engram.dispatch_context import get_dispatch_context
    conn = get_conn(get_db(read_only=True))
    context = get_dispatch_context(conn, args.agent, args.project)
    if args.json:
        print(json.dumps({"context": context, "generated_at": datetime.now().isoformat()}, default=str))
    else:
        print(context)


def cmd_todos(args):
    """List open or all todos."""
    conn = get_conn(get_db(read_only=True))
    from engram.todos import get_open_todos

    todos = []
    if args.all:
        try:
            params = {"p_limit": 100}
            agent_filter = ""
            if args.agent:
                agent_filter = " AND f.agent_id = $p_agent"
                params["p_agent"] = args.agent
            result = conn.execute(
                "MATCH (f:Fact) "
                "WHERE lower(f.category) = 'todo' "
                + agent_filter +
                " RETURN f.id, f.content, f.created_at, f.status, f.resolved_at, f.agent_id "
                "ORDER BY f.created_at DESC LIMIT $p_limit",
                params
            )
            while result.has_next():
                row = result.get_next()
                todos.append({
                    "id": row[0], "content": row[1], "created_at": str(row[2]),
                    "status": row[3], "resolved_at": str(row[4]) if row[4] else None,
                    "agent_id": row[5]
                })
        except Exception as e:
            print(f"Todos query error: {e}", file=sys.stderr)
            todos = []
    else:
        todos = get_open_todos(conn, agent_id=args.agent)

    if args.json:
        print(json.dumps(todos, indent=2, default=str))
        return

    if not todos:
        print("No todos found.")
        return

    title = "📌 Todos" if args.all else "📌 Open Todos"
    print(title)
    print("=" * 60)
    for todo in todos:
        status = todo.get("status") or "open"
        date = str(todo.get("created_at", ""))[:10]
        agent = todo.get("agent_id", "main")
        print(f"  [{date}] ({agent}) [{status}] {todo.get('content', '')} [{todo.get('id', '')}]")


def cmd_todo_add(args):
    """Add a todo."""
    conn = get_conn(get_db(read_only=False))
    from engram.todos import add_todo
    result = add_todo(conn, args.content, agent_id=args.agent)
    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        if result.get("ok"):
            print(f"Added todo: {result.get('id')}")
        else:
            print(f"Todo add failed: {result.get('error')}")


def cmd_todo_done(args):
    """Resolve a todo."""
    conn = get_conn(get_db(read_only=False))
    from engram.todos import resolve_todo
    result = resolve_todo(conn, args.id)
    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        if result.get("ok"):
            print(f"Resolved todo: {result.get('id')}")
        else:
            print(f"Todo resolve failed: {result.get('error')}")


def cmd_contradictions(args):
    """List recent superseded facts."""
    conn = get_conn(get_db(read_only=True))
    cutoff = (datetime.now() - timedelta(days=args.days)).strftime("%Y-%m-%d %H:%M:%S")
    rows = []
    try:
        result = conn.execute(
            "MATCH (new:Fact)-[r:SUPERSEDES]->(old:Fact) "
            "WHERE r.created_at >= datetime($p_cutoff) "
            "RETURN new.id, new.content, old.id, old.content, r.created_at "
            "ORDER BY r.created_at DESC LIMIT $p_limit",
            {"p_cutoff": cutoff, "p_limit": 50}
        )
        while result.has_next():
            row = result.get_next()
            rows.append({
                "new_id": row[0], "new_content": row[1],
                "old_id": row[2], "old_content": row[3],
                "created_at": str(row[4])
            })
    except Exception as e:
        print(f"Contradictions query error: {e}", file=sys.stderr)
        rows = []

    if args.json:
        print(json.dumps(rows, indent=2, default=str))
        return

    if not rows:
        print(f"No superseded facts found in the last {args.days} days.")
        return

    print(f"♻️ Superseded Facts (last {args.days} days)")
    print("=" * 60)
    for row in rows:
        date = str(row.get("created_at", ""))[:16]
        old_text = (row.get("old_content") or "")[:120]
        new_text = (row.get("new_content") or "")[:120]
        print(f"  [{date}] {row.get('old_id')} → {row.get('new_id')}")
        print(f"    OLD: {old_text}")
        print(f"    NEW: {new_text}")


def cmd_recent(args):
    """Show recent activity across facts, episodes, and entities."""
    conn = get_conn(get_db(read_only=True))
    cutoff = (datetime.now() - timedelta(hours=args.hours)).strftime("%Y-%m-%d %H:%M:%S")
    results = {"facts": [], "episodes": [], "entities": []}

    try:
        result = conn.execute(
            "MATCH (f:Fact) "
            "WHERE f.created_at >= timestamp($p_cutoff) "
            "RETURN f.content, f.category, f.created_at "
            "ORDER BY f.created_at DESC LIMIT $p_limit",
            {"p_cutoff": cutoff, "p_limit": 30}
        )
        while result.has_next():
            row = result.get_next()
            results["facts"].append({
                "content": row[0], "category": row[1], "created_at": str(row[2])
            })
    except Exception as e:
        print(f"Recent facts query error: {e}", file=sys.stderr)

    try:
        result = conn.execute(
            "MATCH (ep:Episode) "
            "WHERE ep.occurred_at >= timestamp($p_cutoff) "
            "RETURN ep.summary, ep.source_file, ep.occurred_at "
            "ORDER BY ep.occurred_at DESC LIMIT $p_limit",
            {"p_cutoff": cutoff, "p_limit": 30}
        )
        while result.has_next():
            row = result.get_next()
            results["episodes"].append({
                "summary": row[0], "source_file": row[1], "occurred_at": str(row[2])
            })
    except Exception as e:
        print(f"Recent episodes query error: {e}", file=sys.stderr)

    try:
        result = conn.execute(
            "MATCH (e:Entity) "
            "WHERE e.created_at >= timestamp($p_cutoff) "
            "RETURN e.name, e.entity_type, e.description, e.created_at "
            "ORDER BY e.created_at DESC LIMIT $p_limit",
            {"p_cutoff": cutoff, "p_limit": 30}
        )
        while result.has_next():
            row = result.get_next()
            results["entities"].append({
                "name": row[0], "type": row[1], "description": row[2], "created_at": str(row[3])
            })
    except Exception as e:
        print(f"Recent entities query error: {e}", file=sys.stderr)

    if args.json:
        print(json.dumps(results, indent=2, default=str))
        return

    print(f"⏱️ Recent Activity (last {args.hours} hours)")
    print("=" * 60)
    if results["facts"]:
        print("\n📋 Facts")
        for f in results["facts"]:
            date = str(f.get("created_at", ""))[:16]
            cat = f.get("category", "")
            print(f"  [{date}] [{cat}] {f.get('content', '')[:140]}")
    if results["episodes"]:
        print("\n📖 Episodes")
        for ep in results["episodes"]:
            date = str(ep.get("occurred_at", ""))[:16]
            print(f"  [{date}] {ep.get('summary', '')[:140]}")
    if results["entities"]:
        print("\n🔵 Entities")
        for ent in results["entities"]:
            date = str(ent.get("created_at", ""))[:16]
            desc = ent.get("description", "") or ""
            print(f"  [{date}] {ent.get('name', '')} ({ent.get('type', '')}) {desc[:120]}")


def cmd_health(args):
    """Graph health check — identify data quality issues."""
    conn = get_conn(get_db(read_only=True))
    issues = []

    print("🏥 Engram Health Check")
    print("=" * 60)

    # 1. Check for untyped relationships
    try:
        result = conn.execute(
            "MATCH (e1:Entity)-[r:RELATES_TO]->(e2:Entity) "
            "WHERE r.relation_type IS NULL OR r.relation_type = '' "
            "RETURN count(*)"
        )
        if result.has_next():
            count = result.get_next()[0]
            if count > 0:
                issues.append(f"⚠️  {count} RELATES_TO edges have no relationship label")
                print(f"  ⚠️  {count} RELATES_TO edges missing relationship labels")
            else:
                print("  ✅ All relationships labeled")
    except Exception as e:
        print(f"  ❓ Could not check relationships: {e}")

    # 2. Check for duplicate facts
    try:
        result = conn.execute(
            "MATCH (f:Fact) "
            "WITH lower(f.content) as content, count(*) as cnt "
            "WHERE cnt > 1 "
            "RETURN count(*) as dupes, sum(cnt) as total_dupes"
        )
        if result.has_next():
            row = result.get_next()
            dupes = row[0] or 0
            total = row[1] or 0
            if dupes > 0:
                issues.append(f"⚠️  {dupes} duplicate fact groups ({total} total entries)")
                print(f"  ⚠️  {dupes} duplicate fact groups ({total} total entries)")
            else:
                print("  ✅ No duplicate facts")
    except Exception as e:
        print(f"  ❓ Could not check duplicates: {e}")

    # 3. Check for facts with importance = 1.0 (flat importance)
    try:
        result = conn.execute(
            "MATCH (f:Fact) "
            "WITH count(*) as total, "
            "sum(CASE WHEN f.importance >= 0.99 THEN 1 ELSE 0 END) as max_imp "
            "RETURN total, max_imp"
        )
        if result.has_next():
            row = result.get_next()
            total = row[0] or 1
            max_imp = row[1] or 0
            pct = (max_imp / total * 100) if total > 0 else 0
            if pct > 50:
                issues.append(f"⚠️  {pct:.0f}% of facts have importance ≥ 0.99 (flat distribution)")
                print(f"  ⚠️  {pct:.0f}% of facts have importance ≥ 0.99 — importance is too flat")
            else:
                print(f"  ✅ Importance distribution healthy ({pct:.0f}% at max)")
    except Exception as e:
        print(f"  ❓ Could not check importance: {e}")

    # 4. Check for orphan facts (no entity connections)
    try:
        result = conn.execute(
            "MATCH (f:Fact) "
            "WHERE NOT exists { MATCH (f)-[:ABOUT]->(:Entity) } "
            "RETURN count(*)"
        )
        if result.has_next():
            count = result.get_next()[0]
            if count > 0:
                issues.append(f"⚠️  {count} orphan facts (not linked to any entity)")
                print(f"  ⚠️  {count} orphan facts (no entity links)")
            else:
                print("  ✅ All facts linked to entities")
    except Exception as e:
        print(f"  ❓ Could not check orphans: {e}")

    # 5. Check for noise facts
    try:
        noise_patterns = [
            "facts stored",
            "scheduled reminder",
            "HEARTBEAT_OK",
            "NO_REPLY",
        ]
        total_noise = 0
        for pattern in noise_patterns:
            result = conn.execute(
                "MATCH (f:Fact) WHERE lower(f.content) CONTAINS lower($p_pat) RETURN count(*)",
                {"p_pat": pattern}
            )
            if result.has_next():
                total_noise += result.get_next()[0]
        if total_noise > 0:
            issues.append(f"⚠️  {total_noise} likely noise facts detected")
            print(f"  ⚠️  {total_noise} likely noise facts (metadata/system messages)")
        else:
            print("  ✅ No obvious noise facts")
    except Exception as e:
        print(f"  ❓ Could not check noise: {e}")

    # Summary
    print(f"\n{'='*60}")
    if issues:
        print(f"Found {len(issues)} issue(s) to address.")
    else:
        print("Graph is healthy! 🎉")

    if args.json:
        print(json.dumps({"issues": issues, "count": len(issues)}, indent=2))


def main():
    parser = argparse.ArgumentParser(
        description="Engram CLI — Query and manage the knowledge graph",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  engram search "CalCity Stripe"
  engram entity "Woody"
  engram entity "Tux" --relationships
  engram timeline "CalCity" --days 30
  engram agent-history "Buzz" --project CalCity
  engram facts --days 7
  engram stats
  engram briefing --save
  engram health
        """
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # search
    p = subparsers.add_parser("search", aliases=["s"], help="Search all memory types")
    p.add_argument("query", help="Search terms")
    p.add_argument("--limit", "-n", type=int, default=10)
    p.add_argument("--agent", type=str, default=None)
    p.add_argument("--since", type=str, default=None, help="ISO date filter (>=)")
    p.add_argument("--until", type=str, default=None, help="ISO date filter (<=)")
    p.add_argument("--json", "-j", action="store_true")
    p.add_argument("--verbose", "-v", action="store_true")

    # entity
    p = subparsers.add_parser("entity", aliases=["e"], help="Full context for an entity")
    p.add_argument("name", help="Entity name")
    p.add_argument("--relationships", "-r", action="store_true", help="Show only relationships")
    p.add_argument("--json", "-j", action="store_true")

    # timeline
    p = subparsers.add_parser("timeline", aliases=["t"], help="Timeline for an entity/project")
    p.add_argument("name", help="Entity or project name")
    p.add_argument("--days", "-d", type=int, default=30)
    p.add_argument("--limit", "-n", type=int, default=30)
    p.add_argument("--json", "-j", action="store_true")

    # agent-history
    p = subparsers.add_parser("agent-history", aliases=["ah"], help="Agent work history")
    p.add_argument("name", help="Agent name (e.g. Buzz, Woody)")
    p.add_argument("--project", "-p", type=str, default=None, help="Filter by project")
    p.add_argument("--days", "-d", type=int, default=30)
    p.add_argument("--limit", "-n", type=int, default=20)
    p.add_argument("--json", "-j", action="store_true")

    # facts
    p = subparsers.add_parser("facts", aliases=["f"], help="List recent facts")
    p.add_argument("--days", "-d", type=int, default=7)
    p.add_argument("--limit", "-n", type=int, default=20)
    p.add_argument("--category", "-c", type=str, default=None)
    p.add_argument("--source", "-s", type=str, default=None, help="Filter by source_type")
    p.add_argument("--since", type=str, default=None, help="ISO date filter (>=)")
    p.add_argument("--until", type=str, default=None, help="ISO date filter (<=)")
    p.add_argument("--json", "-j", action="store_true")

    # stats
    p = subparsers.add_parser("stats", help="Graph statistics")
    p.add_argument("--json", "-j", action="store_true")

    # briefing
    p = subparsers.add_parser("briefing", aliases=["b"], help="Generate session briefing")
    p.add_argument("--save", action="store_true", help="Save to BRIEFING.md")
    p.add_argument("--delta", action="store_true", help="Only show new items since last briefing")
    p.add_argument("--json", "-j", action="store_true")

    # dispatch
    p = subparsers.add_parser("dispatch", help="Generate pre-dispatch context")
    p.add_argument("agent", help="Agent name")
    p.add_argument("--project", "-p", type=str, default=None, help="Optional project name")
    p.add_argument("--json", "-j", action="store_true")

    # todos
    p = subparsers.add_parser("todos", help="List open todos")
    p.add_argument("--all", action="store_true", help="Include resolved todos")
    p.add_argument("--agent", type=str, default=None, help="Filter by agent")
    p.add_argument("--json", "-j", action="store_true")

    # todo-add
    p = subparsers.add_parser("todo-add", help="Add a todo")
    p.add_argument("content", help="Todo content")
    p.add_argument("--agent", type=str, default="main")
    p.add_argument("--json", "-j", action="store_true")

    # todo-done
    p = subparsers.add_parser("todo-done", help="Resolve a todo")
    p.add_argument("id", help="Todo fact id")
    p.add_argument("--json", "-j", action="store_true")

    # contradictions
    p = subparsers.add_parser("contradictions", help="Recent superseded facts")
    p.add_argument("--days", "-d", type=int, default=7)
    p.add_argument("--json", "-j", action="store_true")

    # recent
    p = subparsers.add_parser("recent", help="Recent activity across types")
    p.add_argument("--hours", "-H", type=int, default=48)
    p.add_argument("--json", "-j", action="store_true")

    # health
    p = subparsers.add_parser("health", aliases=["h"], help="Graph health check")
    p.add_argument("--json", "-j", action="store_true")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    try:
        cmd_map = {
            "search": cmd_search, "s": cmd_search,
            "entity": cmd_entity, "e": cmd_entity,
            "timeline": cmd_timeline, "t": cmd_timeline,
            "agent-history": cmd_agent_history, "ah": cmd_agent_history,
            "facts": cmd_facts, "f": cmd_facts,
            "stats": cmd_stats,
            "briefing": cmd_briefing, "b": cmd_briefing,
            "dispatch": cmd_dispatch,
            "todos": cmd_todos,
            "todo-add": cmd_todo_add,
            "todo-done": cmd_todo_done,
            "contradictions": cmd_contradictions,
            "recent": cmd_recent,
            "health": cmd_health, "h": cmd_health,
        }
        cmd_map[args.command](args)
    except KeyboardInterrupt:
        pass
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        close()


if __name__ == "__main__":
    main()
