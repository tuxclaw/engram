#!/usr/bin/env python3
"""Contradiction / supersede detection for facts."""

import os
import re
import sys
from datetime import datetime
from typing import Optional

try:
    import kuzu
except ImportError:
    kuzu = None

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PREF_PAT = re.compile(r"^(?P<subj>[^,]{2,60})\s+prefers?\s+(?P<obj>.+)$", re.IGNORECASE)
VERSION_PAT = re.compile(r"\b(?:version|v)\s*(\d+(?:\.\d+)+)\b", re.IGNORECASE)
STATE_PAIRS = [
    ("enabled", "disabled"),
    ("true", "false"),
    ("yes", "no"),
    ("added", "removed"),
    ("installed", "removed"),
    ("active", "inactive"),
    ("open", "closed"),
    ("running", "stopped"),
]


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip()).lower()


def _extract_preference(content: str) -> Optional[tuple[str, str]]:
    match = PREF_PAT.match(content.strip())
    if not match:
        return None
    subj = _normalize(match.group("subj"))
    obj = _normalize(match.group("obj"))
    return subj, obj


def _extract_version(content: str) -> Optional[str]:
    match = VERSION_PAT.search(content)
    if not match:
        return None
    return match.group(1)


def _state_conflict(new_text: str, old_text: str) -> bool:
    for a, b in STATE_PAIRS:
        a_pat = re.compile(r'\b' + re.escape(a) + r'\b')
        b_pat = re.compile(r'\b' + re.escape(b) + r'\b')
        if a_pat.search(new_text) and b_pat.search(old_text):
            return True
        if b_pat.search(new_text) and a_pat.search(old_text):
            return True
    return False


def check_contradictions(conn: kuzu.Connection, new_fact_content: str, about_entities: Optional[list] = None) -> list[dict]:
    """Return list of potential superseded fact IDs with confidence scores."""
    about_entities = about_entities or []
    if not new_fact_content or not about_entities:
        return []

    # Resolve entity IDs
    try:
        names = [str(n).strip().lower() for n in about_entities if str(n).strip()]
        if not names:
            return []
        result = conn.execute(
            "MATCH (e:Entity) WHERE lower(e.name) IN $p_names RETURN e.id, e.name",
            {"p_names": names}
        )
        eids = []
        while result.has_next():
            row = result.get_next()
            eids.append(row[0])
    except Exception:
        eids = []

    if not eids:
        return []

    # Fetch recent facts about these entities
    facts = []
    try:
        result = conn.execute(
            "MATCH (f:Fact)-[:ABOUT]->(e:Entity) "
            "WHERE e.id IN $p_eids "
            "RETURN f.id, f.content, f.category, f.updated_at "
            "ORDER BY f.updated_at DESC LIMIT 60",
            {"p_eids": eids}
        )
        while result.has_next():
            row = result.get_next()
            facts.append({"id": row[0], "content": row[1], "category": row[2]})
    except Exception:
        return []

    new_text = _normalize(new_fact_content)
    new_pref = _extract_preference(new_fact_content)
    new_ver = _extract_version(new_fact_content)

    candidates = []
    for fact in facts:
        old_text = _normalize(fact.get("content", ""))
        if not old_text or old_text == new_text:
            continue

        # Preference contradiction
        old_pref = _extract_preference(fact.get("content", ""))
        if new_pref and old_pref and new_pref[0] == old_pref[0] and new_pref[1] != old_pref[1]:
            candidates.append({
                "fact_id": fact["id"],
                "confidence": 0.9,
                "reason": "preference_conflict"
            })
            continue

        # Version change
        old_ver = _extract_version(fact.get("content", ""))
        if new_ver and old_ver and new_ver != old_ver:
            candidates.append({
                "fact_id": fact["id"],
                "confidence": 0.85,
                "reason": "version_change"
            })
            continue

        # State change
        if _state_conflict(new_text, old_text):
            candidates.append({
                "fact_id": fact["id"],
                "confidence": 0.82,
                "reason": "state_change"
            })

    # Deduplicate by fact_id (keep highest confidence)
    best = {}
    for c in candidates:
        fid = c["fact_id"]
        if fid not in best or c["confidence"] > best[fid]["confidence"]:
            best[fid] = c

    return sorted(best.values(), key=lambda x: x["confidence"], reverse=True)


def supersede_fact(conn: kuzu.Connection, old_fact_id: str, new_fact_id: str) -> dict:
    """Create SUPERSEDES relationship and reduce old fact importance."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        conn.execute(
            "MATCH (old:Fact {id: $p_old}), (new:Fact {id: $p_new}) "
            "MERGE (new)-[r:SUPERSEDES]->(old) "
            "ON CREATE SET r.created_at = timestamp($p_now)",
            {"p_old": old_fact_id, "p_new": new_fact_id, "p_now": now}
        )
        conn.execute(
            "MATCH (f:Fact {id: $p_old}) "
            "SET f.importance = 0.1, f.updated_at = timestamp($p_now)",
            {"p_old": old_fact_id, "p_now": now}
        )
        return {"ok": True, "old_fact_id": old_fact_id, "new_fact_id": new_fact_id}
    except Exception as e:
        return {"ok": False, "error": str(e), "old_fact_id": old_fact_id, "new_fact_id": new_fact_id}
