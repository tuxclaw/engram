#!/usr/bin/env python3
"""
Engram Dashboard — FastAPI Backend
Serves graph data from Kuzu DB and static frontend.
"""

import os
import sys
import json
import traceback
from datetime import datetime, timezone
from typing import Optional

import kuzu
from fastapi import FastAPI, Query, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

# Database path
DB_PATH = os.environ.get(
    "ENGRAM_DB_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), ".engram-db"),
)

PORT = int(os.environ.get("ENGRAM_DASHBOARD_PORT", "3847"))

# Node types and their properties we care about
NODE_TABLES = ["Entity", "Episode", "Emotion", "SessionState", "Fact"]
REL_TABLES = [
    "RELATES_TO", "CAUSED", "PART_OF", "MENTIONED_IN",
    "EPISODE_EVOKES", "ENTITY_EVOKES", "SEQUENCE",
    "DERIVED_FROM", "ABOUT", "SUPERSEDES",
    "SESSION_REFS", "SESSION_EPISODE",
]

# Label fields per node type
LABEL_FIELD = {
    "Entity": "name",
    "Episode": "summary",
    "Emotion": "label",
    "SessionState": "session_key",
    "Fact": "content",
}

app = FastAPI(title="Engram Dashboard API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


import threading
import time

# ── DB connection pool ────────────────────────────────────────────────────────
# Kuzu read_only still needs a lock — we retry on failure (cron holds lock
# for a few seconds during ingest) and keep a module-level connection alive.

_db_lock = threading.Lock()
_db_instance = None
_conn_instance = None


def _open_db(retries: int = 120, delay: float = 3.0):
    """Open DB with retries — ingest cron can hold lock for several minutes."""
    global _db_instance, _conn_instance
    last_err = None
    for attempt in range(retries):
        try:
            db = kuzu.Database(DB_PATH, read_only=True)
            conn = kuzu.Connection(db)
            _db_instance = db
            _conn_instance = conn
            print(f"✅ DB opened after {attempt} retries")
            return db, conn
        except RuntimeError as e:
            last_err = e
            if attempt == 0:
                print(f"⏳ DB locked (ingest running?), waiting up to {retries * delay:.0f}s...")
            if attempt < retries - 1:
                time.sleep(delay)
    raise last_err


def get_conn():
    """Return a live connection. Raises RuntimeError if DB is locked (ingest running)."""
    global _db_instance, _conn_instance
    with _db_lock:
        # Try reusing existing connection
        if _conn_instance is not None:
            try:
                _conn_instance.execute("RETURN 1")
                return _db_instance, _conn_instance
            except Exception:
                _db_instance = None
                _conn_instance = None
        # Open fresh — will raise if cron has lock
        return _open_db(retries=3, delay=2.0)


def safe_str(val):
    """Convert a value to a JSON-safe string."""
    if val is None:
        return None
    if isinstance(val, datetime):
        return val.isoformat()
    return str(val)


def query_to_list(conn, cypher, params=None):
    """Run a Cypher query and return list of dicts."""
    try:
        if params:
            result = conn.execute(cypher, params)
        else:
            result = conn.execute(cypher)
        rows = []
        col_names = result.get_column_names()
        while result.has_next():
            row = result.get_next()
            rows.append({col_names[i]: row[i] for i in range(len(col_names))})
        return rows
    except Exception as e:
        print(f"Query error: {e}\n  Query: {cypher}\n  Params: {params}")
        traceback.print_exc()
        return []


# ============================================================
# API Endpoints
# ============================================================

@app.get("/api/status")
def api_status():
    """Health check — tells the frontend if the DB is available."""
    try:
        db, conn = get_conn()
        conn.execute("RETURN 1")
        return {"status": "ok", "db": DB_PATH}
    except RuntimeError:
        from fastapi.responses import JSONResponse as JR
        return JR({"status": "locked", "message": "Memory indexing in progress — check back in a minute"}, status_code=503)


@app.get("/api/agents")
def api_agents():
    """Return list of agent IDs with fact counts."""
    db, conn = get_conn()
    try:
        rows = query_to_list(conn, """
            MATCH (f:Fact)
            RETURN DISTINCT f.agent_id AS agent_id, count(f) AS fact_count
            ORDER BY fact_count DESC
        """)
        agents = [{"id": safe_str(r["agent_id"]) or "shared", "facts": r["fact_count"]} for r in rows]
        return {"agents": agents}
    except Exception as e:
        return {"agents": [], "error": str(e)}


@app.get("/api/stats")
def api_stats(agent_id: Optional[str] = Query(None)):
    """Live stats: node counts, relationship counts. Optionally filtered by agent_id."""
    db, conn = get_conn()
    stats = {"nodes": {}, "relationships": {}, "total_nodes": 0, "total_rels": 0}

    agent_filter = ""
    params = {}
    if agent_id:
        agent_filter = " WHERE (n.agent_id = $p_agent OR n.agent_id = 'shared')"
        params["p_agent"] = agent_id

    for table in NODE_TABLES:
        try:
            rows = query_to_list(conn, f"MATCH (n:{table}){agent_filter} RETURN count(n) AS cnt", params)
            cnt = rows[0]["cnt"] if rows else 0
            stats["nodes"][table] = cnt
            stats["total_nodes"] += cnt
        except Exception:
            stats["nodes"][table] = 0

    for rel in REL_TABLES:
        try:
            if agent_id:
                # Filter relationships where at least one endpoint matches the agent
                rows = query_to_list(conn, f"""
                    MATCH (a)-[r:{rel}]->(b)
                    WHERE (a.agent_id = $p_agent OR a.agent_id = 'shared'
                        OR b.agent_id = $p_agent OR b.agent_id = 'shared')
                    RETURN count(r) AS cnt
                """, params)
            else:
                rows = query_to_list(conn, f"MATCH ()-[r:{rel}]->() RETURN count(r) AS cnt")
            cnt = rows[0]["cnt"] if rows else 0
            stats["relationships"][rel] = cnt
            stats["total_rels"] += cnt
        except Exception:
            stats["relationships"][rel] = 0

    return stats


@app.get("/api/graph")
def api_graph(
    limit: int = Query(500, ge=1, le=5000),
    offset: int = Query(0, ge=0),
    node_type: Optional[str] = Query(None),
    min_connections: int = Query(0, ge=0),
    agent_id: Optional[str] = Query(None),
):
    """
    Return top nodes by connection count + their edges.
    Designed for initial load — gets the most-connected nodes first.
    """
    db, conn = get_conn()

    # Build union query across all node types (or filtered)
    types_to_query = [node_type] if node_type and node_type in NODE_TABLES else NODE_TABLES

    # Step 1: Get node IDs ranked by degree (connection count)
    # We'll query each table and union the results in Python
    all_nodes = []

    for ntype in types_to_query:
        label_f = LABEL_FIELD.get(ntype, "id")
        # Build agent filter clause
        agent_filter = ""
        params = {"min_conn": min_connections}
        if agent_id:
            agent_filter = " AND (n.agent_id = $p_agent OR n.agent_id = 'shared')"
            params["p_agent"] = agent_id
        
        # Count connections for each node
        cypher = f"""
            MATCH (n:{ntype})
            WHERE true{agent_filter}
            WITH n
            OPTIONAL MATCH (n)-[r]-()
            WITH n, count(r) AS degree
            WHERE degree >= $min_conn
            RETURN n.id AS id, n.{label_f} AS label, '{ntype}' AS type, degree,
                   n.created_at AS created_at, n.agent_id AS agent_id
            ORDER BY degree DESC
        """
        try:
            rows = query_to_list(conn, cypher, params)
            for row in rows:
                row["label"] = safe_str(row.get("label")) or row["id"]
                row["created_at"] = safe_str(row.get("created_at"))
                row["agent_id"] = safe_str(row.get("agent_id")) or "shared"
                all_nodes.append(row)
        except Exception as e:
            print(f"Error querying {ntype}: {e}")

    # Sort all by degree descending
    all_nodes.sort(key=lambda x: x.get("degree", 0), reverse=True)

    # Paginate
    total = len(all_nodes)
    page_nodes = all_nodes[offset : offset + limit]
    node_ids = {n["id"] for n in page_nodes}

    # Step 2: Get edges between these nodes
    edges = []
    if node_ids:
        for rel in REL_TABLES:
            try:
                # We need to find edges where both endpoints are in our node set
                # Kuzu doesn't support IN with lists well, so we'll get all edges
                # from our nodes and filter
                cypher = f"""
                    MATCH (a)-[r:{rel}]->(b)
                    WHERE a.id IN $ids AND b.id IN $ids
                    RETURN a.id AS source, b.id AS target, '{rel}' AS type
                """
                rows = query_to_list(conn, cypher, {"ids": list(node_ids)})
                edges.extend(rows)
            except Exception as e:
                print(f"Error querying edges {rel}: {e}")

    return {
        "nodes": page_nodes,
        "edges": edges,
        "total": total,
        "offset": offset,
        "limit": limit,
    }


@app.get("/api/graph/neighbors/{node_id}")
def api_neighbors(node_id: str, depth: int = Query(1, ge=1, le=3)):
    """Get a node's direct neighbors (click-to-expand)."""
    db, conn = get_conn()

    neighbors = []
    edges = []
    seen_ids = set()

    for rel in REL_TABLES:
        # Outgoing
        try:
            cypher = f"""
                MATCH (a {{id: $nid}})-[r:{rel}]->(b)
                RETURN b.id AS id, label(b) AS type, '{rel}' AS rel_type, 'out' AS direction
            """
            rows = query_to_list(conn, cypher, {"nid": node_id})
            for row in rows:
                nid = row["id"]
                ntype = row["type"]
                if nid not in seen_ids:
                    seen_ids.add(nid)
                    # Get label for this node
                    label_f = LABEL_FIELD.get(ntype, "id")
                    try:
                        detail = query_to_list(
                            conn,
                            f"MATCH (n:{ntype} {{id: $nid}}) RETURN n.{label_f} AS label, n.created_at AS created_at",
                            {"nid": nid},
                        )
                        label = safe_str(detail[0]["label"]) if detail else nid
                        created = safe_str(detail[0].get("created_at")) if detail else None
                    except Exception:
                        label = nid
                        created = None
                    # Count degree
                    try:
                        deg = query_to_list(
                            conn,
                            f"MATCH (n:{ntype} {{id: $nid}})-[r]-() RETURN count(r) AS degree",
                            {"nid": nid},
                        )
                        degree = deg[0]["degree"] if deg else 0
                    except Exception:
                        degree = 0
                    neighbors.append({
                        "id": nid,
                        "label": label,
                        "type": ntype,
                        "degree": degree,
                        "created_at": created,
                    })
                edges.append({
                    "source": node_id,
                    "target": nid,
                    "type": row["rel_type"],
                })
        except Exception:
            pass

        # Incoming
        try:
            cypher = f"""
                MATCH (a)-[r:{rel}]->(b {{id: $nid}})
                RETURN a.id AS id, label(a) AS type, '{rel}' AS rel_type, 'in' AS direction
            """
            rows = query_to_list(conn, cypher, {"nid": node_id})
            for row in rows:
                nid = row["id"]
                ntype = row["type"]
                if nid not in seen_ids:
                    seen_ids.add(nid)
                    label_f = LABEL_FIELD.get(ntype, "id")
                    try:
                        detail = query_to_list(
                            conn,
                            f"MATCH (n:{ntype} {{id: $nid}}) RETURN n.{label_f} AS label, n.created_at AS created_at",
                            {"nid": nid},
                        )
                        label = safe_str(detail[0]["label"]) if detail else nid
                        created = safe_str(detail[0].get("created_at")) if detail else None
                    except Exception:
                        label = nid
                        created = None
                    try:
                        deg = query_to_list(
                            conn,
                            f"MATCH (n:{ntype} {{id: $nid}})-[r]-() RETURN count(r) AS degree",
                            {"nid": nid},
                        )
                        degree = deg[0]["degree"] if deg else 0
                    except Exception:
                        degree = 0
                    neighbors.append({
                        "id": nid,
                        "label": label,
                        "type": ntype,
                        "degree": degree,
                        "created_at": created,
                    })
                edges.append({
                    "source": nid,
                    "target": node_id,
                    "type": row["rel_type"],
                })
        except Exception:
            pass

    return {"node_id": node_id, "neighbors": neighbors, "edges": edges}


@app.get("/api/search")
def api_search(q: str = Query(..., min_length=1), limit: int = Query(50, ge=1, le=200), agent_id: Optional[str] = Query(None)):
    """Search nodes by name/content (case-insensitive substring)."""
    db, conn = get_conn()
    results = []
    search_term = q.lower()

    search_fields = {
        "Entity": ["name", "description", "entity_type"],
        "Episode": ["summary", "content", "source"],
        "Emotion": ["label", "description"],
        "SessionState": ["session_key", "summary"],
        "Fact": ["content", "category"],
    }

    for ntype, fields in search_fields.items():
        label_f = LABEL_FIELD.get(ntype, "id")
        where_clauses = " OR ".join([f"lower(n.{f}) CONTAINS $q" for f in fields])
        agent_filter = ""
        params = {"q": search_term, "lim": limit}
        if agent_id:
            agent_filter = " AND (n.agent_id = $p_agent OR n.agent_id = 'shared')"
            params["p_agent"] = agent_id
        cypher = f"""
            MATCH (n:{ntype})
            WHERE ({where_clauses}){agent_filter}
            OPTIONAL MATCH (n)-[r]-()
            WITH n, count(r) AS degree
            RETURN n.id AS id, n.{label_f} AS label, '{ntype}' AS type, degree,
                   n.created_at AS created_at, n.agent_id AS agent_id
            ORDER BY degree DESC
            LIMIT $lim
        """
        try:
            rows = query_to_list(conn, cypher, params)
            for row in rows:
                row["label"] = safe_str(row.get("label")) or row["id"]
                row["created_at"] = safe_str(row.get("created_at"))
                results.append(row)
        except Exception as e:
            print(f"Search error in {ntype}: {e}")

    results.sort(key=lambda x: x.get("degree", 0), reverse=True)
    return {"results": results[:limit], "query": q}


@app.get("/api/node/{node_id}")
def api_node_detail(node_id: str):
    """Get full details for a single node."""
    db, conn = get_conn()

    for ntype in NODE_TABLES:
        try:
            cypher = f"MATCH (n:{ntype} {{id: $nid}}) RETURN n"
            rows = query_to_list(conn, cypher, {"nid": node_id})
            if rows:
                node_data = rows[0]["n"]
                # Convert all values to safe strings
                clean = {}
                if isinstance(node_data, dict):
                    for k, v in node_data.items():
                        clean[k] = safe_str(v)
                else:
                    clean = {"raw": str(node_data)}
                clean["_type"] = ntype

                # Get degree
                try:
                    deg = query_to_list(
                        conn,
                        f"MATCH (n:{ntype} {{id: $nid}})-[r]-() RETURN count(r) AS degree",
                        {"nid": node_id},
                    )
                    clean["_degree"] = deg[0]["degree"] if deg else 0
                except Exception:
                    clean["_degree"] = 0

                return clean
        except Exception:
            continue

    raise HTTPException(status_code=404, detail="Node not found")


@app.get("/api/timeline")
def api_timeline():
    """Node counts grouped by date for timeline visualization."""
    db, conn = get_conn()
    timeline = {}

    for ntype in NODE_TABLES:
        try:
            cypher = f"""
                MATCH (n:{ntype})
                WHERE n.created_at IS NOT NULL
                RETURN cast(n.created_at, 'DATE') AS day, count(n) AS cnt
                ORDER BY day
            """
            rows = query_to_list(conn, cypher)
            for row in rows:
                day_str = safe_str(row["day"])
                if day_str not in timeline:
                    timeline[day_str] = {"date": day_str}
                timeline[day_str][ntype] = row["cnt"]
        except Exception as e:
            print(f"Timeline error for {ntype}: {e}")

    # Sort by date
    sorted_timeline = sorted(timeline.values(), key=lambda x: x.get("date", ""))
    return {"timeline": sorted_timeline}


# Serve static files
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
def serve_index():
    return FileResponse("static/index.html")


if __name__ == "__main__":
    import uvicorn

    print(f"🧠 Engram Dashboard starting on port {PORT}")
    print(f"   DB: {DB_PATH}")
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
