#!/usr/bin/env python3
"""
Engram Dashboard — FastAPI Backend (Neo4j)
Serves graph data from Neo4j and static frontend.
"""

import os
import sys
import json
import traceback
from datetime import datetime, timezone
from typing import Optional

from neo4j import GraphDatabase
from fastapi import FastAPI, Query, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

# ── Configuration ─────────────────────────────────────────────────────────────
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "config.json")

def load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)

_config = load_config()
NEO4J_URI = _config.get("neo4j", {}).get("uri", "bolt://localhost:7687")
NEO4J_USER = _config.get("neo4j", {}).get("user", "neo4j")
NEO4J_PASS = _config.get("neo4j", {}).get("password", "")

PORT = int(os.environ.get("ENGRAM_DASHBOARD_PORT", "3460"))

# Node types and their properties
NODE_TABLES = ["Entity", "Episode", "Emotion", "SessionState", "Fact"]
REL_TABLES = [
    "RELATES_TO", "CAUSED", "PART_OF", "MENTIONED_IN",
    "EPISODE_EVOKES", "ENTITY_EVOKES", "SEQUENCE",
    "DERIVED_FROM", "ABOUT", "SUPERSEDES",
    "SESSION_REFS", "SESSION_EPISODE",
]

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

# ── Neo4j driver ──────────────────────────────────────────────────────────────
_driver = None

def get_driver():
    global _driver
    if _driver is None:
        _driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASS))
    return _driver


def safe_str(val):
    if val is None:
        return None
    if isinstance(val, datetime):
        return val.isoformat()
    return str(val)


def query_to_list(tx, cypher, params=None):
    """Run a Cypher query inside a transaction and return list of dicts."""
    try:
        result = tx.run(cypher, params or {})
        return [dict(record) for record in result]
    except Exception as e:
        print(f"Query error: {e}\n  Query: {cypher}\n  Params: {params}")
        traceback.print_exc()
        return []


# ============================================================
# API Endpoints
# ============================================================

@app.get("/api/status")
def api_status():
    try:
        driver = get_driver()
        with driver.session() as session:
            session.run("RETURN 1").single()
        return {"status": "ok", "backend": "neo4j", "uri": NEO4J_URI}
    except Exception as e:
        return JSONResponse(
            {"status": "error", "message": str(e)},
            status_code=503,
        )


@app.get("/api/agents")
def api_agents():
    driver = get_driver()
    with driver.session() as session:
        rows = session.run("""
            MATCH (f:Fact)
            RETURN DISTINCT f.agent_id AS agent_id, count(f) AS fact_count
            ORDER BY fact_count DESC
        """).data()
    agents = [{"id": safe_str(r["agent_id"]) or "shared", "facts": r["fact_count"]} for r in rows]
    return {"agents": agents}


@app.get("/api/stats")
def api_stats(agent_id: Optional[str] = Query(None)):
    driver = get_driver()
    stats = {"nodes": {}, "relationships": {}, "total_nodes": 0, "total_rels": 0}

    with driver.session() as session:
        for table in NODE_TABLES:
            try:
                if agent_id:
                    rows = session.run(
                        f"MATCH (n:{table}) WHERE n.agent_id = $agent OR n.agent_id = 'shared' RETURN count(n) AS cnt",
                        {"agent": agent_id},
                    ).data()
                else:
                    rows = session.run(f"MATCH (n:{table}) RETURN count(n) AS cnt").data()
                cnt = rows[0]["cnt"] if rows else 0
                stats["nodes"][table] = cnt
                stats["total_nodes"] += cnt
            except Exception:
                stats["nodes"][table] = 0

        for rel in REL_TABLES:
            try:
                if agent_id:
                    rows = session.run(f"""
                        MATCH (a)-[r:{rel}]->(b)
                        WHERE a.agent_id = $agent OR a.agent_id = 'shared'
                           OR b.agent_id = $agent OR b.agent_id = 'shared'
                        RETURN count(r) AS cnt
                    """, {"agent": agent_id}).data()
                else:
                    rows = session.run(f"MATCH ()-[r:{rel}]->() RETURN count(r) AS cnt").data()
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
    driver = get_driver()
    types_to_query = [node_type] if node_type and node_type in NODE_TABLES else NODE_TABLES
    all_nodes = []

    with driver.session() as session:
        for ntype in types_to_query:
            label_f = LABEL_FIELD.get(ntype, "id")
            agent_filter = ""
            params = {"min_conn": min_connections}
            if agent_id:
                agent_filter = " AND (n.agent_id = $agent OR n.agent_id = 'shared')"
                params["agent"] = agent_id

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
                rows = session.run(cypher, params).data()
                for row in rows:
                    row["label"] = safe_str(row.get("label")) or row["id"]
                    row["created_at"] = safe_str(row.get("created_at"))
                    row["agent_id"] = safe_str(row.get("agent_id")) or "shared"
                    all_nodes.append(row)
            except Exception as e:
                print(f"Error querying {ntype}: {e}")

        # Sort by degree
        all_nodes.sort(key=lambda x: x.get("degree", 0), reverse=True)
        total = len(all_nodes)
        page_nodes = all_nodes[offset : offset + limit]
        node_ids = [n["id"] for n in page_nodes]

        # Get edges between these nodes
        edges = []
        if node_ids:
            for rel in REL_TABLES:
                try:
                    rows = session.run(f"""
                        MATCH (a)-[r:{rel}]->(b)
                        WHERE a.id IN $ids AND b.id IN $ids
                        RETURN a.id AS source, b.id AS target, '{rel}' AS type
                    """, {"ids": node_ids}).data()
                    edges.extend(rows)
                except Exception:
                    pass

    return {
        "nodes": page_nodes,
        "edges": edges,
        "total": total,
        "offset": offset,
        "limit": limit,
    }


@app.get("/api/graph/neighbors/{node_id}")
def api_neighbors(node_id: str, depth: int = Query(1, ge=1, le=3)):
    driver = get_driver()
    neighbors = []
    edges = []
    seen_ids = set()

    with driver.session() as session:
        # Get all neighbors in both directions with a single query
        rows = session.run("""
            MATCH (a {id: $nid})-[r]-(b)
            RETURN b.id AS id, labels(b)[0] AS type, type(r) AS rel_type,
                   CASE WHEN startNode(r) = a THEN 'out' ELSE 'in' END AS direction
        """, {"nid": node_id}).data()

        for row in rows:
            nid = row["id"]
            ntype = row["type"]
            rel_type = row["rel_type"]
            direction = row["direction"]

            if nid not in seen_ids:
                seen_ids.add(nid)
                label_f = LABEL_FIELD.get(ntype, "id")
                try:
                    detail = session.run(
                        f"MATCH (n:{ntype} {{id: $nid}}) OPTIONAL MATCH (n)-[r]-() RETURN n.{label_f} AS label, n.created_at AS created_at, n.agent_id AS agent_id, count(r) AS degree",
                        {"nid": nid},
                    ).data()
                    if detail:
                        neighbors.append({
                            "id": nid,
                            "label": safe_str(detail[0].get("label")) or nid,
                            "type": ntype,
                            "degree": detail[0].get("degree", 0),
                            "created_at": safe_str(detail[0].get("created_at")),
                            "agent_id": safe_str(detail[0].get("agent_id")) or "shared",
                        })
                except Exception:
                    neighbors.append({"id": nid, "label": nid, "type": ntype, "degree": 0})

            if direction == "out":
                edges.append({"source": node_id, "target": nid, "type": rel_type})
            else:
                edges.append({"source": nid, "target": node_id, "type": rel_type})

    return {"node_id": node_id, "neighbors": neighbors, "edges": edges}


@app.get("/api/search")
def api_search(q: str = Query(..., min_length=1), limit: int = Query(50, ge=1, le=200), agent_id: Optional[str] = Query(None)):
    driver = get_driver()
    results = []
    search_term = q.lower()

    search_fields = {
        "Entity": ["name", "description", "entity_type"],
        "Episode": ["summary", "content", "source"],
        "Emotion": ["label", "description"],
        "SessionState": ["session_key", "summary"],
        "Fact": ["content", "category"],
    }

    with driver.session() as session:
        for ntype, fields in search_fields.items():
            label_f = LABEL_FIELD.get(ntype, "id")
            where_clauses = " OR ".join([f"toLower(n.{f}) CONTAINS $q" for f in fields])
            agent_filter = ""
            params = {"q": search_term, "lim": limit}
            if agent_id:
                agent_filter = " AND (n.agent_id = $agent OR n.agent_id = 'shared')"
                params["agent"] = agent_id

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
                rows = session.run(cypher, params).data()
                for row in rows:
                    row["label"] = safe_str(row.get("label")) or row["id"]
                    row["created_at"] = safe_str(row.get("created_at"))
                    row["agent_id"] = safe_str(row.get("agent_id")) or "shared"
                    results.append(row)
            except Exception as e:
                print(f"Search error in {ntype}: {e}")

    results.sort(key=lambda x: x.get("degree", 0), reverse=True)
    return {"results": results[:limit], "query": q}


@app.get("/api/node/{node_id}")
def api_node_detail(node_id: str):
    driver = get_driver()

    with driver.session() as session:
        for ntype in NODE_TABLES:
            try:
                rows = session.run(
                    f"MATCH (n:{ntype} {{id: $nid}}) OPTIONAL MATCH (n)-[r]-() RETURN properties(n) AS props, count(r) AS degree",
                    {"nid": node_id},
                ).data()
                if rows and rows[0]["props"]:
                    clean = {}
                    for k, v in rows[0]["props"].items():
                        clean[k] = safe_str(v)
                    clean["_type"] = ntype
                    clean["_degree"] = rows[0]["degree"]
                    return clean
            except Exception:
                continue

    raise HTTPException(status_code=404, detail="Node not found")


@app.get("/api/timeline")
def api_timeline():
    driver = get_driver()
    timeline = {}

    with driver.session() as session:
        for ntype in NODE_TABLES:
            try:
                rows = session.run(f"""
                    MATCH (n:{ntype})
                    WHERE n.created_at IS NOT NULL
                    RETURN date(n.created_at) AS day, count(n) AS cnt
                    ORDER BY day
                """).data()
                for row in rows:
                    day_str = safe_str(row["day"])
                    if day_str not in timeline:
                        timeline[day_str] = {"date": day_str}
                    timeline[day_str][ntype] = row["cnt"]
            except Exception as e:
                print(f"Timeline error for {ntype}: {e}")

    sorted_timeline = sorted(timeline.values(), key=lambda x: x.get("date", ""))
    return {"timeline": sorted_timeline}


# ── Static files ──────────────────────────────────────────────────────────────
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
def serve_index():
    return FileResponse("static/index.html")


@app.on_event("shutdown")
def shutdown():
    global _driver
    if _driver:
        _driver.close()
        _driver = None


if __name__ == "__main__":
    import uvicorn

    print(f"🧠 Engram Dashboard starting on port {PORT}")
    print(f"   Neo4j: {NEO4J_URI}")
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
