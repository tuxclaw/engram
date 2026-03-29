#!/usr/bin/env python3
"""
Engram Context Query — Fast query interface for the Context Engine plugin.

Usage:
  python engram/context_query.py query "search terms" [--agent main] [--limit 8] [--json]
  python engram/context_query.py store --fact "content" [--agent main] [--category preference]
  python engram/context_query.py store_live --text "..." --agent main --session sess123 [--role user]

Designed to be called from Node.js via spawnSync with JSON output.
"""

import json
import os
import re
import sys
import urllib.request
import urllib.error
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _get_conn(read_only=True):
    from engram.backend import get_db, get_conn
    db = get_db(read_only=read_only)
    return get_conn(db)


def query_memories(terms: str, agent_id: Optional[str] = None, limit: int = 8,
                   since: Optional[str] = None, until: Optional[str] = None) -> dict:
    """Query Engram for relevant memories matching search terms.

    Splits multi-word queries into individual terms and searches each,
    then deduplicates and ranks results by frequency + importance.
    """
    try:
        try:
            conn = _get_conn(read_only=False)  # prefer write for reinforcement
        except RuntimeError:
            conn = _get_conn(read_only=True)   # fall back to read-only if locked
        from engram.query import search_entities, search_facts, search_episodes

        # Split into individual search terms (skip short words)
        raw_terms = [t.strip() for t in terms.split() if len(t.strip()) >= 3]

        # Also try the full phrase and meaningful bigrams
        search_queries = list(set(raw_terms))
        if len(raw_terms) >= 2:
            search_queries.append(terms)  # full phrase

        # Search each term and collect results (deduplicate by id)
        def _score(item):
            tier = item.get("memory_tier")
            quality = item.get("quality_score") or item.get("importance") or 0
            contamination = item.get("contamination_score") or 0
            canonical_bonus = 2.0 if tier == "canonical" else (0.75 if tier == "candidate" else -1.5)
            retrievable_penalty = -2.0 if item.get("retrievable") is False else 0.0
            # Boost live facts — they're recent and high-signal
            source_type = item.get("source_type") or ""
            live_boost = 3.0 if source_type in ("live_turn", "live_llm") else 0.0
            # Recency boost for facts with created_at
            recency_boost = 0.0
            try:
                created = item.get("created_at")
                if created:
                    from datetime import datetime
                    if hasattr(created, 'timestamp'):
                        age_hours = (datetime.now() - created).total_seconds() / 3600
                    else:
                        age_hours = 999
                    if age_hours < 1:
                        recency_boost = 2.0
                    elif age_hours < 24:
                        recency_boost = 1.0
                    elif age_hours < 168:  # 1 week
                        recency_boost = 0.5
            except Exception:
                pass
            return canonical_bonus + quality - contamination + retrievable_penalty + live_boost + recency_boost

        entity_map = {}
        fact_map = {}
        episode_map = {}

        for q in search_queries:
            for e in search_entities(conn, q, limit=limit, agent_id=agent_id, since=since, until=until):
                eid = e.get("id", "")
                if eid not in entity_map:
                    e["_hits"] = 0
                    entity_map[eid] = e
                entity_map[eid]["_hits"] = entity_map[eid].get("_hits", 0) + 1

            for f in search_facts(conn, q, limit=limit, agent_id=agent_id, since=since, until=until):
                fid = f.get("id", "")
                if fid not in fact_map:
                    f["_hits"] = 0
                    fact_map[fid] = f
                fact_map[fid]["_hits"] = fact_map[fid].get("_hits", 0) + 1

            for ep in search_episodes(conn, q, limit=limit, agent_id=agent_id, since=since, until=until):
                epid = ep.get("id", "")
                if epid not in episode_map:
                    ep["_hits"] = 0
                    episode_map[epid] = ep
                episode_map[epid]["_hits"] = episode_map[epid].get("_hits", 0) + 1

        # Sort by hit count (relevance) then importance, return top N
        entities = sorted(entity_map.values(), key=lambda x: (x.get("_hits", 0), _score(x), x.get("importance", 0)), reverse=True)[:limit]
        facts = [f for f in sorted(fact_map.values(), key=lambda x: (x.get("_hits", 0), _score(x), x.get("importance", 0)), reverse=True) if f.get("retrievable", True)][:limit]
        episodes = [ep for ep in sorted(episode_map.values(), key=lambda x: (x.get("_hits", 0), _score(x), x.get("importance", 0)), reverse=True) if ep.get("retrievable", True)][:limit]

        # Clean up internal tracking field
        for item in entities + facts + episodes:
            item.pop("_hits", None)

        return {
            "ok": True,
            "entities": entities,
            "facts": facts,
            "episodes": episodes
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "entities": [], "facts": [], "episodes": []}


def _load_pinned_config() -> dict:
    """Load pinned injection config from engram/config.json."""
    try:
        cfg_path = Path(os.path.dirname(os.path.abspath(__file__))) / "config.json"
        if cfg_path.exists():
            import json as _json
            with open(cfg_path) as f:
                cfg = _json.load(f)
            return cfg.get("context_engine", {}).get("pinned_injection", {})
    except Exception:
        pass
    return {}


def query_pinned(agent_id: Optional[str] = None, channel_id: Optional[str] = None, session_id: Optional[str] = None, limit: int = 5) -> dict:
    """Return high-importance standing-rule facts for an agent plus optional scoped matches.

    Scope model:
    - global facts: no scope_type/scope_id
    - channel facts: scope_type='channel' and scope_id matches current channel
    - session facts: scope_type='session' and scope_id matches current session
    """
    pinned_cfg = _load_pinned_config()
    if pinned_cfg.get("enabled") is False:
        return {"ok": True, "facts": [], "disabled": True}

    min_importance = float(pinned_cfg.get("min_importance", 0.9))
    source_types = pinned_cfg.get("source_types", ["live_context"])
    max_pinned = int(pinned_cfg.get("max_pinned", limit))

    try:
        conn = _get_conn(read_only=True)
        source_filter = " OR ".join(f"f.source_type = '{st}'" for st in source_types)

        scope_clauses = ["(f.scope_type IS NULL OR f.scope_type = '' OR lower(f.scope_type) = 'global')"]
        params = {
            "p_agent": agent_id or "main",
            "p_min_imp": min_importance,
            "p_limit": max_pinned,
        }
        if channel_id:
            scope_clauses.append("(lower(f.scope_type) = 'channel' AND f.scope_id = $p_channel)")
            params["p_channel"] = channel_id
        if session_id:
            scope_clauses.append("(lower(f.scope_type) = 'session' AND f.scope_id = $p_session)")
            params["p_session"] = session_id

        scope_filter = " OR ".join(scope_clauses)
        results = conn.execute(
            f"MATCH (f:Fact) "
            f"WHERE f.agent_id = $p_agent "
            f"AND ({source_filter}) "
            f"AND f.importance >= $p_min_imp "
            f"AND ({scope_filter}) "
            f"AND (f.retrievable IS NULL OR f.retrievable = true) "
            f"RETURN f.id, f.content, f.category, f.importance, f.memory_tier, f.scope_type, f.scope_id "
            f"ORDER BY "
            f"CASE "
            f"  WHEN lower(coalesce(f.scope_type, 'global')) = 'session' THEN 3 "
            f"  WHEN lower(coalesce(f.scope_type, 'global')) = 'channel' THEN 2 "
            f"  ELSE 1 "
            f"END DESC, "
            f"f.importance DESC "
            f"LIMIT $p_limit",
            params,
        )
        facts = []
        while results.has_next():
            row = results.get_next()
            facts.append({
                "id": row[0],
                "content": row[1],
                "category": row[2],
                "importance": row[3],
                "memory_tier": row[4],
                "scope_type": row[5],
                "scope_id": row[6],
            })
        return {"ok": True, "facts": facts}
    except Exception as e:
        return {"ok": False, "error": str(e), "facts": []}


def store_fact(content: str, agent_id: str = "main", category: str = "preference",
               confidence: float = 0.9, importance: float = 0.7) -> dict:
    """Store a single fact into Engram's graph DB."""
    try:
        conn = _get_conn(read_only=False)
        from engram.ingest import generate_id

        fact_id = generate_id("fact", content + "_" + agent_id)
        now = datetime.now()

        # Check if fact already exists for this agent
        try:
            result = conn.execute(
                "MATCH (f:Fact {id: $p_id}) WHERE f.agent_id = $p_agent RETURN f.id",
                {"p_id": fact_id, "p_agent": agent_id}
            )
            if result.has_next():
                return {"ok": True, "stored": False, "reason": "duplicate", "id": fact_id}
        except Exception:
            pass

        conn.execute(
            "CREATE (f:Fact {"
            "  id: $p_id, content: $p_content, category: $p_cat,"
            "  confidence: $p_conf, importance: $p_imp,"
            "  valid_at: $p_ts, created_at: $p_ts, updated_at: $p_ts,"
            "  source_episode: $p_src, agent_id: $p_agent,"
            "  source_type: 'live_context', memory_tier: 'candidate',"
            "  quality_score: 0.8, contamination_score: 0.0, retrievable: true,"
            "  is_candidate: true, is_canonical: false"
            "})",
            {
                "p_id": fact_id,
                "p_content": content,
                "p_cat": category,
                "p_conf": confidence,
                "p_imp": importance,
                "p_ts": now,
                "p_src": "context-engine-live",
                "p_agent": agent_id
            }
        )

        return {"ok": True, "stored": True, "id": fact_id}
    except Exception as e:
        return {"ok": False, "error": str(e)}


LIVE_MIN_CHARS = 20
LIVE_MAX_FACTS = 3
LIVE_CONFIDENCE = 0.8
LIVE_IMPORTANCE = 0.55
LIVE_NON_ALPHA_MAX = 0.50
LIVE_DEDUP_SIMILARITY = 0.86
ENTITY_LIMIT = 8

ATTRIBUTE_PAT = re.compile(r"\b([A-Z][\w@.-]*(?:\s+[A-Z][\w@.-]*)*)\s+(is|was|has|needs?|wants?|started|stopped)\s+([^.!?\n]{4,160})", re.IGNORECASE)
REPORTED_PAT = re.compile(r"\b([A-Z][\w@.-]*(?:\s+[A-Z][\w@.-]*)*)\s+(said|told|mentioned|asked|requested)\s+([^.!?\n]{4,180})", re.IGNORECASE)
ACTION_PAT = re.compile(r"\b([A-Z][\w@.-]*(?:\s+[A-Z][\w@.-]*)*)\s+(fixed|deployed|updated|changed|built|created|added|removed|installed|migrated)\s+([^.!?\n]{2,160})", re.IGNORECASE)
DIAGNOSIS_PAT = re.compile(r"\b(?:the\s+problem\s+was|root\s+cause\s+was|root\s+cause\s+is|issue\s+is|bug\s+was|error\s+was)\s+([^.!?\n]{4,180})", re.IGNORECASE)
PREFERENCE_PAT = re.compile(r"\bI\s+(?:really\s+|kinda\s+|definitely\s+|absolutely\s+)?(love|hate|like|dislike|prefer|enjoy|want|need|dig|appreciate)\s+([^.!?\n]{2,120})", re.IGNORECASE)
DECISION_PAT = re.compile(r"\b(?:I'm going to|I'm gonna|I am going to|we should|let's|I decided to|I'm thinking about|I am thinking about|I'm planning to|I am planning to|planning to|going to|want to|wanna|need to|gotta|thinking about|thinking of)\s+([^.!?\n]{4,160})", re.IGNORECASE)
MENTION_PAT = re.compile(r"(?:@[A-Za-z0-9_\-]+|#[A-Za-z0-9_\-]+|\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\b)")
QUOTE_PAT = re.compile(r'"([^"]{3,120})"|\'([^\']{3,120})\'')
URL_PAT = re.compile(r"https?://\S+")
NUMBER_PAT = re.compile(r"\b(?:\$?\d+(?:[.,]\d+)?%?|\d+[smhdw]\b)")

# Patterns that indicate system/metadata noise — skip these messages entirely
NOISE_PATTERNS = [
    re.compile(r"message_id", re.IGNORECASE),
    re.compile(r"sender_id", re.IGNORECASE),
    re.compile(r"conversation_label", re.IGNORECASE),
    re.compile(r"untrusted metadata", re.IGNORECASE),
    re.compile(r"EXTERNAL_UNTRUSTED_CONTENT", re.IGNORECASE),
    re.compile(r"schema.*openclaw", re.IGNORECASE),
    re.compile(r"system prompt", re.IGNORECASE),
    re.compile(r"^\s*\{.*\"role\"", re.IGNORECASE),
    re.compile(r"Conversation info \(untrusted", re.IGNORECASE),
    re.compile(r"Sender \(untrusted", re.IGNORECASE),
    re.compile(r"Replied message \(untrusted", re.IGNORECASE),
    re.compile(r"Thread starter - for context", re.IGNORECASE),
    re.compile(r"session_key|sessionKey|sessionId", re.IGNORECASE),
    re.compile(r"HEARTBEAT_OK|NO_REPLY", re.IGNORECASE),
    re.compile(r"^\s*```", re.IGNORECASE),
    re.compile(r"private channel for you", re.IGNORECASE),
    re.compile(r"new session was started", re.IGNORECASE),
    re.compile(r"context limit exceeded", re.IGNORECASE),
]


def _is_noise(text: str) -> bool:
    """Check if text is ENTIRELY system metadata/noise (no human content)."""
    stripped = _strip_envelope(text)
    if not stripped or len(stripped) < 10:
        return True
    # Only filter if the stripped content itself is noise
    for pat in NOISE_PATTERNS:
        if pat.search(stripped):
            return True
    return False


def _strip_envelope(text: str) -> str:
    """Strip OpenClaw conversation envelope metadata, returning just the human content."""
    import re as _re
    result = str(text or "")
    
    # Remove "Conversation info (untrusted metadata):" + JSON block
    result = _re.sub(r'Conversation info \(untrusted metadata\):\s*```json\s*\{[^}]*\}\s*```\s*', '', result, flags=_re.DOTALL)
    # Remove "Sender (untrusted metadata):" + JSON block  
    result = _re.sub(r'Sender \(untrusted metadata\):\s*```json\s*\{[^}]*\}\s*```\s*', '', result, flags=_re.DOTALL)
    # Remove "Replied message (untrusted, for context):" + JSON block
    result = _re.sub(r'Replied message \(untrusted[^)]*\):\s*```json\s*\{[^}]*\}\s*```\s*', '', result, flags=_re.DOTALL)
    # Remove "[Thread starter - for context]" lines
    result = _re.sub(r'\[Thread starter - for context\]\s*', '', result)
    # Remove "System: [timestamp] Exec ..." prefixes
    result = _re.sub(r'System:\s*\[\d{4}-\d{2}-\d{2}[^\]]*\]\s*Exec\s+\w+\s*\([^)]*\)\s*::\s*[^\n]*\n*', '', result)
    # Remove bare JSON blocks that look like metadata
    result = _re.sub(r'```json\s*\{[^}]*(?:"message_id"|"sender_id"|"session_key")[^}]*\}\s*```', '', result, flags=_re.DOTALL)
    
    return result.strip()


def _mostly_non_alpha(text: str) -> bool:
    if not text:
        return True
    alpha = sum(1 for ch in text if ch.isalpha())
    return alpha / max(len(text), 1) < (1.0 - LIVE_NON_ALPHA_MAX)


def _clean_space(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip(" \t\n\r.,;:-")


def _normalize_fact_text(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip()).lower()


def _extract_named_entities(text: str) -> list[str]:
    seen = []
    for match in MENTION_PAT.findall(text or ""):
        name = _clean_space(match)
        if len(name) < 2:
            continue
        if name.lower() in {"the", "This", "That", "There", "Issue", "Problem", "Root Cause"}:
            continue
        if name not in seen:
            seen.append(name)
    return seen[:ENTITY_LIMIT]


def _extract_context_snippets(text: str) -> list[str]:
    snippets = []
    for match in QUOTE_PAT.finditer(text or ""):
        quoted = _clean_space(match.group(1) or match.group(2) or "")
        if quoted and quoted not in snippets:
            snippets.append(f'quoted "{quoted}"')
    for url in URL_PAT.findall(text or ""):
        url = _clean_space(url)
        if url and url not in snippets:
            snippets.append(f"url {url}")
    for num in NUMBER_PAT.findall(text or ""):
        num = _clean_space(num)
        if not num:
            continue
        window = re.search(rf"([^.!?\n]{{0,24}}{re.escape(num)}[^.!?\n]{{0,24}})", text or "", re.IGNORECASE)
        ctx = _clean_space(window.group(1) if window else num)
        if ctx and ctx not in snippets:
            snippets.append(f"number {ctx}")
    return snippets[:4]


def _build_live_candidates(text: str) -> list[dict]:
    candidates = []

    # Import never-store patterns for safety gate
    try:
        from engram.ingest import NEVER_STORE_PATTERNS as _nsp
    except ImportError:
        _nsp = []

    def add(content: str, category: str, about: list[str]):
        content = _clean_space(content)
        if len(content) < 12:
            return
        # Skip content containing secrets/tokens
        for pat in _nsp:
            if pat.search(content):
                return
        norm = _normalize_fact_text(content)
        if any(_normalize_fact_text(c["content"]) == norm for c in candidates):
            return
        candidates.append({
            "content": content,
            "category": category,
            "about": about[:ENTITY_LIMIT],
        })

    named = _extract_named_entities(text)
    snippets = _extract_context_snippets(text)

    for m in ATTRIBUTE_PAT.finditer(text):
        subj, verb, obj = _clean_space(m.group(1)), m.group(2).lower(), _clean_space(m.group(3))
        add(f"{subj} {verb} {obj}", "attribute", list(dict.fromkeys([subj] + named)))

    for m in REPORTED_PAT.finditer(text):
        subj, verb, obj = _clean_space(m.group(1)), m.group(2).lower(), _clean_space(m.group(3))
        add(f"{subj} {verb} {obj}", "reported", list(dict.fromkeys([subj] + named)))

    for m in ACTION_PAT.finditer(text):
        subj, verb, obj = _clean_space(m.group(1)), m.group(2).lower(), _clean_space(m.group(3))
        add(f"{subj} {verb} {obj}", "action", list(dict.fromkeys([subj] + named)))

    for m in DIAGNOSIS_PAT.finditer(text):
        diag = _clean_space(m.group(1))
        add(f"Root cause: {diag}", "diagnosis", named)

    for m in PREFERENCE_PAT.finditer(text):
        verb, obj = m.group(1).lower(), _clean_space(m.group(2))
        add(f"User {verb}s {obj}", "preference", named)

    for m in DECISION_PAT.finditer(text):
        decision = _clean_space(m.group(1))
        add(f"Decision: {decision}", "decision", named)

    return candidates[:LIVE_MAX_FACTS]


def _find_existing_entity_ids(conn, agent_id: str, names: list[str]) -> dict[str, str]:
    found = {}
    for name in names[:ENTITY_LIMIT]:
        try:
            result = conn.execute(
                "MATCH (e:Entity) "
                "WHERE lower(e.name) = lower($p_name) AND e.agent_id = $p_agent "
                "RETURN e.id, e.name LIMIT 1",
                {"p_name": name, "p_agent": agent_id}
            )
            if result.has_next():
                row = result.get_next()
                found[name] = row[0]
        except Exception:
            continue
    return found


def _is_live_duplicate(conn, content: str, agent_id: str) -> bool:
    needle = _normalize_fact_text(content)
    token = max((tok for tok in re.findall(r"[a-z0-9]+", needle) if len(tok) >= 4), key=len, default="")
    try:
        if token:
            result = conn.execute(
                "MATCH (f:Fact) "
                "WHERE f.agent_id = $p_agent AND lower(f.content) CONTAINS lower($p_token) "
                "RETURN f.content LIMIT 10",
                {"p_agent": agent_id, "p_token": token}
            )
        else:
            result = conn.execute(
                "MATCH (f:Fact) WHERE f.agent_id = $p_agent RETURN f.content LIMIT 10",
                {"p_agent": agent_id}
            )
        while result.has_next():
            row = result.get_next()
            existing = _normalize_fact_text(row[0] or "")
            if existing == needle:
                return True
            if existing and SequenceMatcher(None, needle, existing).ratio() >= LIVE_DEDUP_SIMILARITY:
                return True
    except Exception:
        return False
    return False


def _lookup_xai_api_key() -> Optional[str]:
    key = str(os.environ.get("XAI_API_KEY") or "").strip()
    if key:
        return key

    # Check engram/config.json first (most reliable location)
    engram_cfg = Path(os.path.dirname(os.path.abspath(__file__))) / "config.json"
    if engram_cfg.exists():
        try:
            import json as _json
            with open(engram_cfg) as f:
                cfg = _json.load(f)
            val = str(cfg.get("xai_api_key", "") or "").strip()
            if val and not val.startswith("Optional"):
                return val
        except Exception:
            pass

    # Fallback: ~/.config/openclaw/config.yaml
    cfg_path = Path.home() / ".config" / "openclaw" / "config.yaml"
    if not cfg_path.exists():
        return None

    try:
        for line in cfg_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if stripped.startswith("xai:"):
                value = stripped.split(":", 1)[1].strip().strip('"\'')
                if value:
                    return value
            if stripped.startswith("XAI_API_KEY:"):
                value = stripped.split(":", 1)[1].strip().strip('"\'')
                if value:
                    return value
    except Exception:
        return None

    return None


def _parse_llm_fact_array(content: str) -> list[str]:
    raw = str(content or "").strip()
    if not raw:
        return []

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\[[\s\S]*\]", raw)
        if not match:
            return []
        data = json.loads(match.group(0))

    if not isinstance(data, list):
        return []

    out = []
    for item in data[:LIVE_MAX_FACTS]:
        fact = _clean_space(item)
        if fact and fact not in out:
            out.append(fact)
    return out


def _store_live_candidates(conn, candidates: list[dict], agent_id: str, session_id: str, role: str, source_type: str, confidence: float, importance: float) -> dict:
    from engram.ingest import generate_id, passes_prestore_test, NEVER_STORE_PATTERNS

    all_names = []
    for cand in candidates:
        for name in cand.get("about", []):
            if name not in all_names:
                all_names.append(name)
    entity_ids = _find_existing_entity_ids(conn, agent_id, all_names)

    now = datetime.now()
    stored = []
    skipped = []

    for cand in candidates[:LIVE_MAX_FACTS]:
        content = cand["content"]
        
        # Apply extraction policy pre-store test
        if not passes_prestore_test(cand):
            skipped.append({"content": content, "reason": "failed_prestore_test"})
            continue
        
        fact_id = generate_id("fact", content)

        try:
            existing = conn.execute(
                "MATCH (f:Fact {id: $p_id}) WHERE f.agent_id = $p_agent RETURN f.id LIMIT 1",
                {"p_id": fact_id, "p_agent": agent_id}
            )
            if existing.has_next() or _is_live_duplicate(conn, content, agent_id):
                skipped.append({"content": content, "reason": "duplicate"})
                continue
        except Exception:
            if _is_live_duplicate(conn, content, agent_id):
                skipped.append({"content": content, "reason": "duplicate"})
                continue

        try:
            conn.execute(
                "MERGE (f:Fact {id: $p_id}) "
                "SET f.content = $p_content, "
                "f.category = $p_cat, "
                "f.confidence = $p_conf, "
                "f.importance = CASE WHEN f.importance IS NULL THEN $p_imp ELSE f.importance END, "
                "f.valid_at = $p_now, "
                "f.created_at = CASE WHEN f.created_at IS NULL THEN $p_now ELSE f.created_at END, "
                "f.updated_at = $p_now, "
                "f.agent_id = $p_agent, "
                "f.session_id = $p_session, "
                "f.turn_role = $p_role, "
                "f.source_episode = CASE WHEN f.source_episode IS NULL THEN $p_session ELSE f.source_episode END, "
                "f.source_type = $p_source_type, "
                "f.memory_tier = 'candidate', "
                "f.quality_score = CASE WHEN f.quality_score IS NULL THEN 0.8 ELSE f.quality_score END, "
                "f.contamination_score = CASE WHEN f.contamination_score IS NULL THEN 0.0 ELSE f.contamination_score END, "
                "f.retrievable = true, "
                "f.is_candidate = true, "
                "f.is_canonical = false",
                {
                    "p_id": fact_id,
                    "p_content": content,
                    "p_cat": cand["category"],
                    "p_conf": confidence,
                    "p_imp": importance,
                    "p_now": now,
                    "p_agent": agent_id,
                    "p_session": session_id,
                    "p_role": role,
                    "p_source_type": source_type,
                }
            )

            for about_name in cand.get("about", []):
                entity_id = entity_ids.get(about_name)
                if not entity_id:
                    continue
                try:
                    conn.execute(
                        "MATCH (f:Fact {id: $p_fid}), (e:Entity {id: $p_eid}) "
                        "WHERE e.agent_id = $p_agent "
                        "MERGE (f)-[r:ABOUT]->(e) "
                        "ON CREATE SET r.aspect = $p_aspect, r.created_at = datetime($p_now)",
                        {
                            "p_fid": fact_id,
                            "p_eid": entity_id,
                            "p_agent": agent_id,
                            "p_aspect": cand["category"],
                            "p_now": now.isoformat(),
                        }
                    )
                except Exception:
                    pass

            stored.append({"id": fact_id, "content": content, "category": cand["category"]})
        except Exception as e:
            skipped.append({"content": content, "reason": str(e)})

    return {
        "ok": True,
        "stored": len(stored),
        "facts": stored,
        "skipped_facts": skipped,
        "agent_id": agent_id,
        "session_id": session_id,
        "role": role,
    }


def store_live(text: str, agent_id: str, session_id: str, role: str = "user") -> dict:
    """Regex-based low-latency live fact extraction/write-through for a single turn."""
    text = str(text or "").strip()
    agent_id = str(agent_id or "").strip()
    session_id = str(session_id or "").strip()
    role = str(role or "user").strip()

    if not agent_id:
        return {"ok": False, "error": "agent_id required"}
    if not session_id:
        return {"ok": False, "error": "session_id required"}
    # Strip envelope metadata, extract just the human content
    clean_text = _strip_envelope(text)
    if not clean_text or len(clean_text) < LIVE_MIN_CHARS:
        return {"ok": True, "stored": 0, "skipped": True, "reason": "envelope_stripped_too_short"}
    if _mostly_non_alpha(clean_text):
        return {"ok": True, "stored": 0, "skipped": True, "reason": "mostly_non_alpha"}
    if _is_noise(clean_text):
        return {"ok": True, "stored": 0, "skipped": True, "reason": "noise_filtered"}

    try:
        conn = _get_conn(read_only=False)
    except Exception as e:
        return {"ok": False, "error": str(e)}

    candidates = _build_live_candidates(clean_text)
    if not candidates:
        return {"ok": True, "stored": 0, "skipped": True, "reason": "no_candidates"}

    return _store_live_candidates(conn, candidates, agent_id, session_id, role, "live_turn", LIVE_CONFIDENCE, LIVE_IMPORTANCE)


def extract_and_store_llm(text: str, agent_id: str, session_id: str, role: str = "user") -> dict:
    text = str(text or "").strip()
    agent_id = str(agent_id or "").strip()
    session_id = str(session_id or "").strip()
    role = str(role or "user").strip()

    if not agent_id:
        return {"ok": False, "error": "agent_id required"}
    if not session_id:
        return {"ok": False, "error": "session_id required"}
    # Strip envelope metadata
    clean_text = _strip_envelope(text)
    if not clean_text or len(clean_text) < LIVE_MIN_CHARS:
        return {"ok": True, "stored": 0, "skipped": True, "reason": "envelope_stripped_too_short"}
    if _mostly_non_alpha(clean_text):
        return {"ok": True, "stored": 0, "skipped": True, "reason": "mostly_non_alpha"}
    if _is_noise(clean_text):
        return {"ok": True, "stored": 0, "skipped": True, "reason": "noise_filtered"}

    xai_key = _lookup_xai_api_key()
    if not xai_key:
        return {"ok": False, "error": "xAI API key not found"}

    payload = json.dumps({
        "model": "grok-4-1-fast-non-reasoning",
        "messages": [
            {
                "role": "system",
                "content": (
                    "Extract key facts worth remembering from this message. "
                    "Return a JSON array of strings, max 3 facts.\n\n"
                    "STORE: decisions, milestones, agent outcomes with impact, todos/commitments, "
                    "stable relationships, preferences, operating rules, lessons from errors.\n"
                    "SKIP: reasoning/chain-of-thought, secrets/tokens, heartbeat wrappers, "
                    "casual chatter, duplicates, routine success spam, transient status.\n\n"
                    "Pre-store test — each fact must pass ALL:\n"
                    "1. Durable next week?\n"
                    "2. Actionable or explanatory?\n"
                    "3. Specific enough to retrieve later?\n"
                    "4. Safe (no secrets)?\n"
                    "5. Novel (not duplicate)?\n\n"
                    "If nothing passes, return []. No explanations, just the JSON array."
                ),
            },
            {"role": "user", "content": clean_text},
        ],
        "temperature": 0.1,
        "max_tokens": 512,
    })

    req = urllib.request.Request(
        "https://api.x.ai/v1/chat/completions",
        data=payload.encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {xai_key}",
            "User-Agent": "Engram/1.0",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8", errors="ignore")
        except Exception:
            detail = str(e)
        return {"ok": False, "error": f"xAI HTTP {e.code}: {detail[:300]}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}

    try:
        content = body["choices"][0]["message"]["content"].strip()
        # Strip markdown fences if present
        if content.startswith("```"):
            content = re.sub(r'^```\w*\n', '', content)
            content = re.sub(r'\n```$', '', content)
        facts = _parse_llm_fact_array(content)
    except Exception as e:
        return {"ok": False, "error": f"invalid xAI response: {e}"}

    if not facts:
        return {"ok": True, "stored": 0, "skipped": True, "reason": "no_candidates", "facts": []}

    try:
        conn = _get_conn(read_only=False)
    except Exception as e:
        return {"ok": False, "error": str(e)}

    named = _extract_named_entities(text)
    candidates = [
        {
            "content": fact,
            "category": "llm_extracted",
            "about": named,
        }
        for fact in facts[:LIVE_MAX_FACTS]
    ]
    return _store_live_candidates(conn, candidates, agent_id, session_id, role, "live_llm", 0.85, LIVE_IMPORTANCE)


def format_for_prompt(results: dict, max_chars: int = 4000) -> str:
    """Format query results into a compact prompt-ready string."""
    lines = []

    entities = results.get("entities", [])
    facts = results.get("facts", [])
    episodes = results.get("episodes", [])

    if not entities and not facts and not episodes:
        return ""

    if facts:
        for f in facts[:6]:
            cat = f"[{f.get('category', '')}]" if f.get('category') else ""
            lines.append(f"- {cat} {f['content']}")

    if entities:
        for e in entities[:4]:
            desc = e.get('description', '')
            if desc:
                lines.append(f"- {e['name']} ({e.get('type', '')}): {desc}")

    if episodes:
        for ep in episodes[:3]:
            date = ep.get('occurred_at', '')[:10]
            lines.append(f"- [{date}] {ep.get('summary', '')}")

    result = "\n".join(lines)
    return result[:max_chars] if len(result) > max_chars else result


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Engram Context Query")
    subparsers = parser.add_subparsers(dest="command")

    # Query command
    q_parser = subparsers.add_parser("query", help="Query memories")
    q_parser.add_argument("terms", help="Search terms")
    q_parser.add_argument("--agent", type=str, default=None, help="Agent ID scope")
    q_parser.add_argument("--limit", type=int, default=8)
    q_parser.add_argument("--since", type=str, default=None, help="ISO date filter (>=)")
    q_parser.add_argument("--until", type=str, default=None, help="ISO date filter (<=)")
    q_parser.add_argument("--json", "-j", action="store_true", help="JSON output")
    q_parser.add_argument("--prompt", action="store_true", help="Prompt-ready format")

    # Store command
    s_parser = subparsers.add_parser("store", help="Store a fact")
    s_parser.add_argument("--fact", required=True, help="Fact content")
    s_parser.add_argument("--agent", type=str, default="main")
    s_parser.add_argument("--category", type=str, default="preference")
    s_parser.add_argument("--importance", type=float, default=0.7)

    # Live store command
    sl_parser = subparsers.add_parser("store_live", help="Store live turn facts")
    sl_parser.add_argument("--text", required=True, help="Turn text")
    sl_parser.add_argument("--agent", required=True, help="Strict agent ID scope")
    sl_parser.add_argument("--session", required=True, help="Session ID provenance")
    sl_parser.add_argument("--role", type=str, default="user", help="Message role")

    # LLM live extraction command
    llm_parser = subparsers.add_parser("extract_llm", help="Extract and store LLM-based live facts")
    llm_parser.add_argument("--text", required=True, help="Turn text")
    llm_parser.add_argument("--agent", required=True, help="Strict agent ID scope")
    llm_parser.add_argument("--session", required=True, help="Session ID provenance")
    llm_parser.add_argument("--role", type=str, default="user", help="Message role")

    # Pinned facts command
    pin_parser = subparsers.add_parser("pinned", help="Get pinned/standing-rule facts")
    pin_parser.add_argument("--agent", type=str, default="main", help="Agent ID scope")
    pin_parser.add_argument("--channel", type=str, default=None, help="Optional channel scope")
    pin_parser.add_argument("--session", type=str, default=None, help="Optional session scope")
    pin_parser.add_argument("--limit", type=int, default=5)

    args = parser.parse_args()

    if args.command == "query":
        results = query_memories(args.terms, agent_id=args.agent, limit=args.limit, since=args.since, until=args.until)
        if args.prompt:
            print(format_for_prompt(results))
        elif args.json:
            print(json.dumps(results, indent=2, default=str))
        else:
            from engram.query import print_results
            print_results(results)

    elif args.command == "store":
        result = store_fact(args.fact, agent_id=args.agent, category=args.category,
                           importance=args.importance)
        print(json.dumps(result, indent=2, default=str))

    elif args.command == "store_live":
        result = store_live(args.text, agent_id=args.agent, session_id=args.session, role=args.role)
        print(json.dumps(result, indent=2, default=str))

    elif args.command == "extract_llm":
        result = extract_and_store_llm(args.text, agent_id=args.agent, session_id=args.session, role=args.role)
        print(json.dumps(result, indent=2, default=str))

    elif args.command == "pinned":
        result = query_pinned(agent_id=args.agent, channel_id=args.channel, session_id=args.session, limit=args.limit)
        print(json.dumps(result, indent=2, default=str))

    else:
        parser.print_help()
