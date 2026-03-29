#!/usr/bin/env python3
"""
Engram Ingestor — Extract entities, relationships, and emotions from memory files.

Pipeline:
  1. Read daily log / memory file
  2. Chunk into episode-sized segments
  3. LLM extracts entities, relationships, facts, emotions
  4. Store in Kuzu graph with temporal metadata
  5. Update Chroma embeddings for semantic search

Uses Claude via OpenClaw's API for extraction (falls back to local if unavailable).
"""

import json
import os
import re
import sys
import hashlib
import subprocess
from difflib import SequenceMatcher
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


CONTAMINATION_PATTERNS = [
    r'"entities"\s*:\s*\[',
    r'"relationships"\s*:\s*\[',
    r'"facts"\s*:\s*\[',
    r'"episode_summary"\s*:',
    r'Conversation info \(untrusted metadata\):',
    r'Sender \(untrusted metadata\):',
    r'EXTERNAL_UNTRUSTED_CONTENT',
    r'<think>',
    r'You are a memory extraction system',
    r'You are a knowledge extraction assistant',
    r'\[TOOL_RESULT\]',
    r'^\s*```json\s*\n\s*\{.*"entities"',
]

try:
    import kuzu
except ImportError:
    kuzu = None  # Neo4j backend doesn't need kuzu

# Add parent to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engram.backend import get_db, get_conn, init_schema, get_stats, print_stats

# =========================================================
# Configuration
# =========================================================

ENGRAM_DIR = Path(os.path.dirname(os.path.abspath(__file__)))

# Load config
def _load_config() -> dict:
    """Load engram config.json, falling back to sensible defaults."""
    cfg_path = ENGRAM_DIR / "config.json"
    if cfg_path.exists():
        with open(cfg_path) as f:
            return json.load(f)
    return {}

_CFG = _load_config()

MEMORY_DIR = Path(os.path.expanduser(_CFG.get("memory_dir", "~/clawd/memory")))
MAIN_AGENT_ID = _CFG.get("main_agent_id", "main")

# Build agent workspace memory dirs from config
AGENT_WORKSPACE_MEMORY_DIRS = {}
for agent_id, mem_path in _CFG.get("agent_workspaces", {}).items():
    AGENT_WORKSPACE_MEMORY_DIRS[agent_id] = Path(os.path.expanduser(mem_path))
PROCESSED_LOG = ENGRAM_DIR / ".processed_files.json"

# Extraction prompt for the LLM
EXTRACTION_PROMPT = """You are a memory extraction system. Analyze the following text and extract structured information.

TEXT:
{text}

SOURCE FILE: {source_file}
DATE: {date}

## What to Extract (STORE)
- Decisions that change future behavior
- Project milestones and completions
- Agent outcomes that had real impact
- Todos, reminders, commitments (use category \"todo\")
- Stable relationships (people ↔ projects ↔ repos ↔ tools)
- Preferences and operating rules
- Errors ONLY if they taught a lesson or are recurring
- Operational summaries ONLY if they materially changed something or exposed a systemic issue

## What to SKIP (never extract)
- Internal reasoning / chain-of-thought / think blocks
- Secrets, passwords, tokens, API keys, auth commands
- Heartbeat envelopes, reminder wrappers, transport boilerplate
- Casual chatter, duplicates, routine success messages ("deployed OK", "build passed")
- Transient status updates that won't matter next week

## Pre-Store Test (every fact must pass ALL five)
1. Durable next week? — Will this still matter in 7+ days?
2. Actionable or explanatory? — Does it drive a future action or explain a past decision?
3. Specific enough to retrieve? — Could someone search for this and find it useful?
4. Safe? — No secrets, no reasoning traces, no auth tokens.
5. Novel? — Not a duplicate of commonly known information.

If a fact doesn't pass, DO NOT include it.

Extract the following as JSON (and nothing else):

{{
  "entities": [
    {{
      "name": "entity name (canonical form)",
      "type": "person|project|tool|concept|place|event|organization",
      "description": "brief description (1 sentence)"
    }}
  ],
  "relationships": [
    {{
      "from": "entity name",
      "to": "entity name", 
      "type": "relates_to|caused|part_of|created|uses|prefers|replaced|blocked_by|depends_on",
      "description": "brief description of the relationship"
    }}
  ],
  "facts": [
    {{
      "content": "A clear, standalone factual statement",
      "category": "preference|decision|lesson|milestone|action|technical|todo",
      "importance": "high|medium",
      "confidence": 0.9,
      "about": ["entity name(s) this fact concerns"]
    }}
  ],
  "emotions": [
    {{
      "label": "emotion name (frustrated|excited|satisfied|curious|concerned|amused|urgent|calm)",
      "valence": 0.0,
      "arousal": 0.0,
      "context": "what triggered this emotion",
      "about": ["entity name(s) involved"]
    }}
  ],
  "episode_summary": "1-2 sentence summary of what happened in this text"
}}

Rules:
- Valence: -1.0 (very negative) to 1.0 (very positive)
- Arousal: 0.0 (calm) to 1.0 (intense)
- Entity names should be canonical (e.g., "The Dev" not "the dev" or "Dev")
- Only extract entities that are meaningful and recurring, not throwaway mentions
- Facts should be standalone — understandable without the source text
- Only include facts with importance "high" or "medium" — skip low-value/transient facts entirely
- If no emotions are apparent, return an empty emotions array
- Return ONLY valid JSON, no markdown formatting"""


# =========================================================
# Extraction Policy Enforcement
# =========================================================

# Patterns that indicate never-store content
NEVER_STORE_PATTERNS = [
    re.compile(r'<think>.*?</think>', re.DOTALL),           # chain-of-thought
    re.compile(r'HEARTBEAT_OK|NO_REPLY', re.IGNORECASE),    # heartbeat envelopes
    re.compile(r"password\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|\S+)", re.IGNORECASE),  # passwords (quoted or bare)
    re.compile(r"token\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|\S+)", re.IGNORECASE),     # tokens (quoted or bare)
    re.compile(r"api[_-]?key\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|\S+)", re.IGNORECASE), # API keys (quoted or bare)
    re.compile(r'Bearer\s+[A-Za-z0-9._\-]+', re.IGNORECASE),# auth headers
    re.compile(r'xai-[A-Za-z0-9]{20,}'),                    # xAI keys
    re.compile(r'sk-[A-Za-z0-9]{20,}'),                     # OpenAI keys
    re.compile(r'ghp_[A-Za-z0-9]{20,}'),                    # GitHub PATs
    re.compile(r'gho_[A-Za-z0-9]{20,}'),                    # GitHub OAuth tokens
    re.compile(r'github_pat_[A-Za-z0-9_]{20,}'),            # GitHub fine-grained PATs
    re.compile(r'AKIA[0-9A-Z]{16}'),                        # AWS access keys
    re.compile(r'-----BEGIN[\s\S]*?PRIVATE KEY-----'),       # PEM private keys
]

# Fact content patterns that should never be stored
NEVER_STORE_FACT_PATTERNS = [
    re.compile(r'routine\s+(success|check|pass|ok)', re.IGNORECASE),
    re.compile(r'heartbeat\s+(ok|ack|received)', re.IGNORECASE),
    re.compile(r'cron\s+(ran|executed|completed)\s+(successfully|ok)', re.IGNORECASE),
    re.compile(r'^(deployed|built|passed|ok|done|success|complete)\.?$', re.IGNORECASE),
]

TODO_COMPLETION_PAT = re.compile(r"\b(done|completed|resolved|finished|closed)\b", re.IGNORECASE)
TODO_COMPLETION_PREFIX = re.compile(r"\b(?:done|completed|resolved|finished|closed)[:\s]+(.+)$", re.IGNORECASE)
TODO_COMPLETION_SUFFIX = re.compile(r"(.+?)\s+(?:is|was)\s+(?:done|completed|resolved|finished|closed)\b", re.IGNORECASE)

IMPORTANCE_MAP = {
    "high": 0.85,
    "medium": 0.60,
    "low": 0.25,
}


def strip_never_store_content(text: str) -> str:
    """Remove content that should never be ingested before it hits the LLM."""
    result = text
    # Remove think blocks
    result = re.sub(r'<think>.*?</think>', '', result, flags=re.DOTALL)
    # Remove heartbeat/transport envelopes
    result = re.sub(r'HEARTBEAT_OK|NO_REPLY', '', result, flags=re.IGNORECASE)
    # Redact inline secrets (replace with [REDACTED])
    for pattern in NEVER_STORE_PATTERNS[3:]:  # skip think/heartbeat (handled above), redact secrets
        result = pattern.sub('[REDACTED]', result)
    return result.strip()


def passes_prestore_test(fact: dict) -> bool:
    """Apply the 5-gate pre-store test to a candidate fact.
    
    Gates:
    1. Durable next week?
    2. Actionable or explanatory?
    3. Specific enough to retrieve?
    4. Safe (no secrets)?
    5. Novel (not boilerplate)?
    
    Gates 1-2 are partially enforced by the LLM prompt; gates 3-5 are mechanical.
    """
    content = fact.get("content", "").strip()
    
    # Gate 3: Specific enough to retrieve? (too short = too vague)
    if len(content) < 15:
        return False
    
    # Gate 4: Safe? No secrets or auth tokens
    for pattern in NEVER_STORE_PATTERNS:
        if pattern.search(content):
            return False
    
    # Gate 5: Not routine boilerplate
    for pattern in NEVER_STORE_FACT_PATTERNS:
        if pattern.search(content):
            return False
    
    # LLM-assigned importance: skip anything explicitly marked low
    importance = fact.get("importance", "medium")
    if isinstance(importance, str) and importance.lower() == "low":
        return False
    # Also catch numeric importance below threshold (LLMs sometimes return floats)
    if isinstance(importance, (int, float)) and importance < 0.3:
        return False
    
    return True


def fact_importance_score(fact: dict, source_meta: Optional[dict] = None) -> float:
    """Convert LLM importance label + source metadata into a numeric score."""
    source_meta = source_meta or {}
    
    # Use LLM-assigned importance if available
    imp_label = fact.get("importance", "medium")
    if isinstance(imp_label, str):
        base = IMPORTANCE_MAP.get(imp_label.lower(), 0.60)
    elif isinstance(imp_label, (int, float)):
        base = float(imp_label)
    else:
        base = 0.60
    
    # Boost canonical sources
    if source_meta.get("is_canonical"):
        base = max(base, 0.75)
    
    # Category-based adjustments
    category = fact.get("category", "context")
    if category in ("decision", "preference", "lesson", "milestone"):
        base = min(base + 0.10, 1.0)
    
    return round(base, 2)


def _completion_snippet(content: str) -> Optional[str]:
    if not content or not TODO_COMPLETION_PAT.search(content):
        return None
    match = TODO_COMPLETION_PREFIX.search(content)
    if match:
        return match.group(1).strip()
    match = TODO_COMPLETION_SUFFIX.search(content)
    if match:
        return match.group(1).strip()
    return content.strip()


def _resolve_related_todos(conn: kuzu.Connection, content: str, about: list[str], agent_id: str):
    """Resolve open todos that match a completion fact."""
    snippet = _completion_snippet(content)
    if not snippet:
        return

    token = max((tok for tok in re.findall(r"[a-z0-9]+", snippet.lower()) if len(tok) >= 4), key=len, default="")
    if not token:
        return

    params = {"p_token": token, "p_agent": agent_id}
    todo_rows = []

    try:
        if about:
            name_list = [str(n).strip().lower() for n in about if str(n).strip()]
            if name_list:
                result = conn.execute(
                    "MATCH (f:Fact)-[:ABOUT]->(e:Entity) "
                    "WHERE lower(f.category) = 'todo' "
                    "AND (f.status IS NULL OR f.status <> 'resolved') "
                    "AND f.agent_id = $p_agent "
                    "AND lower(f.content) CONTAINS lower($p_token) "
                    "AND lower(e.name) IN $p_names "
                    "RETURN f.id, f.content LIMIT 10",
                    {**params, "p_names": name_list}
                )
            else:
                result = conn.execute(
                    "MATCH (f:Fact) "
                    "WHERE lower(f.category) = 'todo' "
                    "AND (f.status IS NULL OR f.status <> 'resolved') "
                    "AND f.agent_id = $p_agent "
                    "AND lower(f.content) CONTAINS lower($p_token) "
                    "RETURN f.id, f.content LIMIT 10",
                    params
                )
        else:
            result = conn.execute(
                "MATCH (f:Fact) "
                "WHERE lower(f.category) = 'todo' "
                "AND (f.status IS NULL OR f.status <> 'resolved') "
                "AND f.agent_id = $p_agent "
                "AND lower(f.content) CONTAINS lower($p_token) "
                "RETURN f.id, f.content LIMIT 10",
                params
            )
        while result.has_next():
            row = result.get_next()
            todo_rows.append((row[0], row[1]))
    except Exception:
        return

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for tid, tcontent in todo_rows:
        ratio = SequenceMatcher(None, snippet.lower(), str(tcontent or "").lower()).ratio()
        if ratio < 0.75:
            continue
        try:
            conn.execute(
                "MATCH (f:Fact {id: $p_id}) "
                "SET f.status = 'resolved', "
                "f.resolved_at = timestamp($p_now), "
                "f.updated_at = timestamp($p_now)",
                {"p_id": tid, "p_now": now}
            )
        except Exception:
            continue


def normalize_entity_name(name: str) -> str:
    """Normalize entity name to prevent duplicates like 'TheDev' vs 'The Dev'.
    
    Strips spaces, lowercases, removes common punctuation for ID generation.
    The original display name is preserved on the entity node.
    """
    # Lowercase and strip
    n = name.lower().strip()
    # Remove spaces between words (so "The Dev" == "TheDev")
    n = re.sub(r'\s+', '', n)
    # Remove common trailing punctuation
    n = re.sub(r'[._\-]+$', '', n)
    return n


def generate_id(prefix: str, content: str) -> str:
    """Generate a deterministic ID from content."""
    h = hashlib.sha256(content.encode()).hexdigest()[:12]
    return f"{prefix}_{h}"


def get_processed_files() -> dict:
    """Load the set of already-processed files with their modification times."""
    if PROCESSED_LOG.exists():
        with open(PROCESSED_LOG) as f:
            return json.load(f)
    return {}


def save_processed_files(processed: dict):
    """Save the processed files log."""
    with open(PROCESSED_LOG, "w") as f:
        json.dump(processed, f, indent=2)


def _iter_candidate_files(include_exported_sessions: bool = False) -> list[Path]:
    """Return candidate source files in deterministic order."""
    files = []

    for f in sorted(MEMORY_DIR.glob("*.md")):
        files.append(f)

    for _agent_id, mem_dir in AGENT_WORKSPACE_MEMORY_DIRS.items():
        if not mem_dir.exists():
            continue
        for f in sorted(mem_dir.glob("*.md")):
            files.append(f)

    if include_exported_sessions:
        exported_dir = ENGRAM_DIR / "exported-sessions"
        if exported_dir.exists():
            for f in sorted(exported_dir.glob("*.md")):
                files.append(f)

    return files


def find_memory_files(include_exported_sessions: bool = False) -> list[Path]:
    """Find all memory files that need processing across all memory directories.

    Default behavior is intentionally conservative: exported session dumps are
    excluded unless explicitly requested for archive/backfill workflows.
    """
    processed = get_processed_files()
    files = []

    for f in _iter_candidate_files(include_exported_sessions=include_exported_sessions):
        mtime = str(f.stat().st_mtime)
        key = str(f)
        if key not in processed or processed[key] != mtime:
            files.append(f)

    return files


def extract_date_from_filename(filepath: Path) -> Optional[str]:
    """Extract date from YYYY-MM-DD.md filename pattern."""
    match = re.match(r"(\d{4}-\d{2}-\d{2})", filepath.stem)
    if match:
        return match.group(1)
    return None


def chunk_text(text: str, max_chars: int = 4000) -> list[str]:
    """Split text into chunks at natural boundaries (headers, blank lines)."""
    if len(text) <= max_chars:
        return [text]
    
    chunks = []
    current = []
    current_len = 0
    
    # Split by double newlines (paragraphs/sections)
    sections = re.split(r'\n\n+', text)
    
    for section in sections:
        section = section.strip()
        if not section:
            continue
            
        if current_len + len(section) > max_chars and current:
            chunks.append("\n\n".join(current))
            current = [section]
            current_len = len(section)
        else:
            current.append(section)
            current_len += len(section)
    
    if current:
        chunks.append("\n\n".join(current))
    
    return chunks


def call_llm(prompt: str) -> Optional[dict]:
    """Call xAI Grok for entity/relationship extraction.

    Uses grok-4-1-fast-non-reasoning directly — no Ollama.
    Local models lack the judgment needed for extraction policy enforcement.
    """
    try:
        return _call_xai(prompt)
    except Exception as e:
        print(f"  ⚠️  xAI extraction failed: {e}")
        return None


def _load_engram_config():
    """Load Engram config from engram/config.json."""
    config_path = Path(__file__).parent / "config.json"
    if config_path.exists():
        with open(config_path) as f:
            return json.load(f)
    return {}


def _get_xai_key() -> str:
    """Get xAI API key from env, then Engram config, then OpenClaw config."""
    key = os.environ.get("XAI_API_KEY", "")
    if key:
        return key
    # Try Engram config
    engram_cfg = _load_engram_config()
    key = engram_cfg.get("xai_api_key", "")
    if key:
        return key
    # Fall back to OpenClaw config
    try:
        config_path = Path(os.path.expanduser("~/.openclaw/openclaw.json"))
        if config_path.exists():
            with open(config_path) as f:
                cfg = json.load(f)
            # xAI key lives under skills.entries.grok.apiKey or auth profiles
            key = cfg.get("skills", {}).get("entries", {}).get("grok", {}).get("apiKey", "")
            if key:
                return key
    except Exception:
        pass
    raise RuntimeError("xAI API key not found. Set XAI_API_KEY env var, xai_api_key in engram/config.json, or skills.entries.grok.apiKey in openclaw.json")


def _call_xai(prompt: str) -> Optional[dict]:
    """Call xAI grok API directly. Does NOT route through OpenClaw gateway,
    so it won't pollute sessions.json with ephemeral session entries."""
    import urllib.request
    
    xai_key = _get_xai_key()
    if not xai_key:
        raise RuntimeError("XAI_API_KEY not set")
    
    payload = json.dumps({
        "model": "grok-4-1-fast-non-reasoning",
        "messages": [
            {"role": "system", "content": "You are a knowledge extraction assistant. Always respond with valid JSON only, no markdown fences."},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.1,
        "max_tokens": 4096
    })
    
    req = urllib.request.Request(
        "https://api.x.ai/v1/chat/completions",
        data=payload.encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {xai_key}", "User-Agent": "Engram/1.0"}
    )
    
    with urllib.request.urlopen(req, timeout=120) as resp:
        result = json.loads(resp.read().decode())
        text = result["choices"][0]["message"]["content"].strip()
        
        # Strip thinking tags if present (Qwen3 quirk)
        if "<think>" in text:
            text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL).strip()
        
        # Strip markdown fences
        if text.startswith("```"):
            text = re.sub(r'^```\w*\n', '', text)
            text = re.sub(r'\n```$', '', text)
        
        parsed = json.loads(text)
        
        if not isinstance(parsed, dict):
            raise ValueError("Response is not a dict")
        if "entities" not in parsed:
            raise ValueError("Missing 'entities' key")
        
        return parsed



def classify_source(filepath: Path, text: str = "") -> dict:
    """Classify source quality and contamination risk for first-pass memory hygiene.

    This is intentionally heuristic and reviewable.
    """
    path_str = str(filepath).lower()
    name = filepath.name.lower()
    text_sample = (text or "")[:12000]

    source_type = "memory"
    memory_tier = "candidate"
    contamination_flags = []
    contamination_score = 0.0
    quality_score = 0.7
    retrievable = True

    if "/exported-sessions/" in path_str:
        source_type = "exported_session"
        memory_tier = "archive"
        quality_score = 0.2
        contamination_score += 0.45
    elif "/memory/" in path_str:
        source_type = "memory"
        memory_tier = "candidate"
        quality_score = 0.65

    if re.match(r"\d{4}-\d{2}-\d{2}(-[a-z0-9_-]+-[a-f0-9]+)?\.md$", name):
        contamination_score += 0.05

    for pattern in CONTAMINATION_PATTERNS:
        if re.search(pattern, text_sample, re.IGNORECASE | re.DOTALL):
            contamination_flags.append(pattern)

    if contamination_flags:
        contamination_score += min(0.45, 0.08 * len(contamination_flags))

    lower_text = text_sample.lower()
    if "session summary" in lower_text or "summary of" in lower_text:
        contamination_score += 0.1
    if "tool call" in lower_text or "tool output" in lower_text:
        contamination_score += 0.12
    if lower_text.count("```") >= 2:
        contamination_score += 0.08

    contamination_score = min(1.0, contamination_score)

    if source_type == "memory" and contamination_score <= 0.2:
        memory_tier = "canonical"
        quality_score = max(quality_score, 0.9)
    elif contamination_score >= 0.6:
        memory_tier = "archive"
        quality_score = min(quality_score, 0.15)
        retrievable = False
    else:
        quality_score = max(0.05, quality_score - (contamination_score * 0.6))

    return {
        "source_type": source_type,
        "memory_tier": memory_tier,
        "quality_score": round(quality_score, 3),
        "contamination_score": round(contamination_score, 3),
        "contamination_flags": contamination_flags[:8],
        "retrievable": retrievable,
        "is_canonical": memory_tier == "canonical",
        "is_candidate": memory_tier == "candidate",
    }


def extract_agent_from_filepath(filepath: Path) -> str:
    """Extract agent ID from memory file — checks workspace path first, then filename pattern.
    
    Resolution order:
    1. Agent workspace memory dirs (from config.json agent_workspaces)
    2. Known agent names in filename: YYYY-MM-DD-<agent>-<hash>.md
    3. Files in main memory dir (config.json memory_dir) default to main_agent_id
    4. Everything else → 'shared'
    """
    filepath_str = str(filepath.resolve())
    
    # Check if file is in an agent workspace memory directory
    for agent_id, mem_dir in AGENT_WORKSPACE_MEMORY_DIRS.items():
        if mem_dir.exists() and filepath_str.startswith(str(mem_dir.resolve())):
            return agent_id
    
    # Check if file is in main agent memory directory
    if MEMORY_DIR.exists() and filepath_str.startswith(str(MEMORY_DIR.resolve())):
        # Check for known non-main agent names in filename pattern
        match = re.match(r'\d{4}-\d{2}-\d{2}-([a-zA-Z][a-zA-Z0-9_-]*)-[a-f0-9]+\.md$', filepath.name)
        if match:
            agent_name = match.group(1).lower()
            # If it matches a configured agent workspace, use that ID
            if agent_name in AGENT_WORKSPACE_MEMORY_DIRS:
                return agent_name
        # Everything else in main memory dir belongs to the main agent
        return MAIN_AGENT_ID
    
    # Fall back to filename pattern for files from unknown locations (e.g. exported-sessions/)
    match = re.match(r'\d{4}-\d{2}-\d{2}-([a-zA-Z][a-zA-Z0-9_-]*)-[a-f0-9]+\.md$', filepath.name)
    if match:
        agent_name = match.group(1).lower()
        # Known agent IDs
        if agent_name in AGENT_WORKSPACE_MEMORY_DIRS:
            return agent_name
        if agent_name == MAIN_AGENT_ID:
            return MAIN_AGENT_ID
        # Subagent names (tony, pepper, steve, bruce, etc.) → main agent
        return MAIN_AGENT_ID
    
    return "shared"


def store_extraction(conn: kuzu.Connection, extraction: dict, 
                     source_file: str, date_str: str, chunk_text_content: str,
                     agent_id: str = "shared", source_meta: Optional[dict] = None):
    """Store extracted entities, relationships, facts, and emotions in the graph.
    
    Note: Kuzu reserves certain keywords (desc, type, etc.) so we use 
    prefixed parameter names like $p_desc, $p_type to avoid collisions.
    """
    now = datetime.now()
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    source_meta = source_meta or {}
    
    # Parse date for temporal context
    if date_str:
        try:
            episode_date = datetime.strptime(date_str, "%Y-%m-%d")
            episode_ts = episode_date.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            episode_ts = now_str
    else:
        episode_ts = now_str
    
    # --- Store Episode ---
    episode_id = generate_id("ep", f"{source_file}:{chunk_text_content[:200]}")
    summary = extraction.get("episode_summary", "")
    
    try:
        conn.execute(
            "MERGE (e:Episode {id: $p_id}) "
            "SET e.content = $p_content, "
            "e.summary = $p_summary, "
            "e.source = $p_source, "
            "e.source_file = $p_src, "
            "e.source_type = $p_source_type, "
            "e.memory_tier = $p_memory_tier, "
            "e.quality_score = $p_quality, "
            "e.contamination_score = $p_contam, "
            "e.retrievable = $p_retrievable, "
            "e.is_canonical = $p_is_canonical, "
            "e.is_candidate = $p_is_candidate, "
            "e.occurred_at = timestamp($p_occ), "
            "e.importance = $p_imp, "
            "e.agent_id = $p_agent, "
            "e.created_at = timestamp($p_now)",
            {
                "p_id": episode_id,
                "p_content": chunk_text_content[:2000],
                "p_summary": summary,
                "p_src": source_file,
                "p_source": source_meta.get("source_type", "memory"),
                "p_source_type": source_meta.get("source_type", "memory"),
                "p_memory_tier": source_meta.get("memory_tier", "candidate"),
                "p_quality": source_meta.get("quality_score", 0.5),
                "p_contam": source_meta.get("contamination_score", 0.0),
                "p_retrievable": source_meta.get("retrievable", True),
                "p_is_canonical": source_meta.get("is_canonical", False),
                "p_is_candidate": source_meta.get("is_candidate", True),
                "p_occ": episode_ts,
                "p_imp": 0.7 if source_meta.get("is_canonical") else (0.45 if source_meta.get("is_candidate", True) else 0.15),
                "p_agent": agent_id,
                "p_now": now_str
            }
        )
    except Exception as e:
        print(f"    ⚠️  Episode store failed: {e}")
    
    # --- Store Entities ---
    entity_ids = {}  # name -> id mapping for relationship creation
    
    for ent in extraction.get("entities", []):
        name = ent.get("name", "").strip()
        if not name:
            continue
        
        eid = generate_id("ent", normalize_entity_name(name))
        entity_ids[name] = eid
        
        try:
            conn.execute(
                "MERGE (e:Entity {id: $p_id}) "
                "SET e.name = $p_name, "
                "e.entity_type = $p_type, "
                "e.description = $p_desc, "
                "e.importance = CASE WHEN e.importance IS NULL THEN 0.5 ELSE e.importance END, "
                "e.access_count = CASE WHEN e.access_count IS NULL THEN 0 ELSE e.access_count END, "
                "e.agent_id = $p_agent, "
                "e.updated_at = timestamp($p_now), "
                "e.created_at = CASE WHEN e.created_at IS NULL THEN timestamp($p_now) ELSE e.created_at END",
                {
                    "p_id": eid,
                    "p_name": name,
                    "p_type": ent.get("type", "concept"),
                    "p_desc": ent.get("description", ""),
                    "p_agent": agent_id,
                    "p_now": now_str
                }
            )
            
            # Link entity to episode
            conn.execute(
                "MATCH (ent:Entity {id: $p_eid}), (ep:Episode {id: $p_epid}) "
                "MERGE (ent)-[r:MENTIONED_IN]->(ep) "
                "ON CREATE SET r.created_at = datetime($p_now)",
                {"p_eid": eid, "p_epid": episode_id, "p_now": now_str}
            )
            
        except Exception as e:
            print(f"    ⚠️  Entity '{name}' store failed: {e}")
    
    # --- Store Relationships ---
    for rel in extraction.get("relationships", []):
        from_name = rel.get("from", "").strip()
        to_name = rel.get("to", "").strip()
        rel_type = rel.get("type", "relates_to").lower()
        
        if not from_name or not to_name:
            continue
        
        # Ensure entities exist
        from_id = entity_ids.get(from_name, generate_id("ent", normalize_entity_name(from_name)))
        to_id = entity_ids.get(to_name, generate_id("ent", normalize_entity_name(to_name)))
        
        # Create entities if they don't exist yet
        for eid, ename in [(from_id, from_name), (to_id, to_name)]:
            if ename not in entity_ids:
                try:
                    conn.execute(
                        "MERGE (e:Entity {id: $p_id}) "
                        "SET e.name = $p_name, "
                        "e.entity_type = 'concept', "
                        "e.description = '', "
                        "e.importance = CASE WHEN e.importance IS NULL THEN 0.3 ELSE e.importance END, "
                        "e.access_count = CASE WHEN e.access_count IS NULL THEN 0 ELSE e.access_count END, "
                        "e.created_at = CASE WHEN e.created_at IS NULL THEN timestamp($p_now) ELSE e.created_at END, "
                        "e.updated_at = timestamp($p_now)",
                        {"p_id": eid, "p_name": ename, "p_now": now_str}
                    )
                    entity_ids[ename] = eid
                except Exception:
                    pass
        
        try:
            if rel_type == "caused":
                conn.execute(
                    "MATCH (a:Entity {id: $p_fid}), (b:Entity {id: $p_tid}) "
                    "MERGE (a)-[r:CAUSED]->(b) "
                    "ON CREATE SET r.description = $p_desc, r.confidence = 0.7, "
                    "r.valid_at = datetime($p_vat), r.created_at = datetime($p_now)",
                    {
                        "p_fid": from_id, "p_tid": to_id,
                        "p_desc": rel.get("description", ""),
                        "p_vat": episode_ts, "p_now": now_str
                    }
                )
            elif rel_type == "part_of":
                conn.execute(
                    "MATCH (a:Entity {id: $p_fid}), (b:Entity {id: $p_tid}) "
                    "MERGE (a)-[r:PART_OF]->(b) "
                    "ON CREATE SET r.role = $p_role, "
                    "r.valid_at = datetime($p_vat), r.created_at = datetime($p_now)",
                    {
                        "p_fid": from_id, "p_tid": to_id,
                        "p_role": rel.get("description", ""),
                        "p_vat": episode_ts, "p_now": now_str
                    }
                )
            else:
                conn.execute(
                    "MATCH (a:Entity {id: $p_fid}), (b:Entity {id: $p_tid}) "
                    "MERGE (a)-[r:RELATES_TO {relation_type: $p_rtype}]->(b) "
                    "ON CREATE SET r.description = $p_desc, "
                    "r.strength = 0.5, r.valid_at = datetime($p_vat), r.created_at = datetime($p_now)",
                    {
                        "p_fid": from_id, "p_tid": to_id,
                        "p_rtype": rel_type,
                        "p_desc": rel.get("description", ""),
                        "p_vat": episode_ts, "p_now": now_str
                    }
                )
        except Exception as e:
            print(f"    ⚠️  Relationship '{from_name}'->{to_name}' failed: {e}")
    
    # --- Store Facts (with pre-store test) ---
    skipped_facts = 0
    for fact in extraction.get("facts", []):
        content = fact.get("content", "").strip()
        if not content:
            continue

        category = str(fact.get("category", "context") or "context").lower()
        
        # Apply extraction policy pre-store test
        if not passes_prestore_test(fact):
            skipped_facts += 1
            continue
        
        fid = generate_id("fact", content.lower())
        
        try:
            conn.execute(
                "MERGE (f:Fact {id: $p_id}) "
                "SET f.content = $p_content, "
                "f.category = $p_cat, "
                "f.confidence = $p_conf, "
                "f.valid_at = timestamp($p_vat), "
                "f.agent_id = $p_agent, "
                "f.source_type = $p_source_type, "
                "f.memory_tier = $p_memory_tier, "
                "f.quality_score = $p_quality, "
                "f.contamination_score = $p_contam, "
                "f.retrievable = $p_retrievable, "
                "f.updated_at = timestamp($p_now), "
                "f.importance = CASE WHEN f.importance IS NULL THEN $p_initial_importance ELSE f.importance END, "
                "f.access_count = CASE WHEN f.access_count IS NULL THEN 0 ELSE f.access_count END, "
                "f.source_episode = CASE WHEN f.source_episode IS NULL THEN $p_epid ELSE f.source_episode END, "
                "f.created_at = CASE WHEN f.created_at IS NULL THEN timestamp($p_now) ELSE f.created_at END",
                {
                    "p_id": fid,
                    "p_content": content,
                    "p_cat": category,
                    "p_conf": fact.get("confidence", 0.8),
                    "p_vat": episode_ts,
                    "p_agent": agent_id,
                    "p_source_type": source_meta.get("source_type", "memory"),
                    "p_memory_tier": source_meta.get("memory_tier", "candidate"),
                    "p_quality": source_meta.get("quality_score", 0.5),
                    "p_contam": source_meta.get("contamination_score", 0.0),
                    "p_retrievable": source_meta.get("retrievable", True),
                    "p_initial_importance": fact_importance_score(fact, source_meta),
                    "p_epid": episode_id,
                    "p_now": now_str
                }
            )

            if category == "todo":
                try:
                    conn.execute(
                        "MATCH (f:Fact {id: $p_id}) "
                        "SET f.status = CASE WHEN f.status IS NULL OR f.status = '' THEN 'open' ELSE f.status END",
                        {"p_id": fid}
                    )
                except Exception:
                    pass
            
            # Link fact to episode
            conn.execute(
                "MATCH (f:Fact {id: $p_fid}), (ep:Episode {id: $p_epid}) "
                "MERGE (f)-[r:DERIVED_FROM]->(ep) "
                "ON CREATE SET r.extraction_method = 'llm', r.created_at = datetime($p_now)",
                {"p_fid": fid, "p_epid": episode_id, "p_now": now_str}
            )
            
            # Link fact to entities it's about
            for about_name in fact.get("about", []):
                about_id = entity_ids.get(about_name, generate_id("ent", normalize_entity_name(about_name)))
                try:
                    conn.execute(
                        "MATCH (f:Fact {id: $p_fid}), (e:Entity {id: $p_eid}) "
                        "MERGE (f)-[r:ABOUT]->(e) "
                        "ON CREATE SET r.aspect = $p_asp, r.created_at = datetime($p_now)",
                        {
                            "p_fid": fid, "p_eid": about_id,
                            "p_asp": category,
                            "p_now": now_str
                        }
                    )
                except Exception:
                    pass

            # Auto-resolve related todos if this fact signals completion
            _resolve_related_todos(conn, content, fact.get("about", []), agent_id)

            # Check for contradictions / supersedes
            try:
                from engram.contradictions import check_contradictions, supersede_fact
                candidates = check_contradictions(conn, content, fact.get("about", []))
                for c in candidates:
                    if c.get("confidence", 0) >= 0.8:
                        supersede_fact(conn, c.get("fact_id"), fid)
            except Exception:
                pass
                    
        except Exception as e:
            print(f"    ⚠️  Fact store failed: {e}")
    
    # --- Store Emotions ---
    for emotion in extraction.get("emotions", []):
        label = emotion.get("label", "").strip()
        if not label:
            continue
        
        emid = generate_id("em", f"{label}:{episode_id}")
        
        try:
            conn.execute(
                "MERGE (em:Emotion {id: $p_id}) "
                "SET em.label = $p_label, "
                "em.valence = $p_val, "
                "em.arousal = $p_aro, "
                "em.description = $p_desc, "
                "em.agent_id = $p_agent, "
                "em.created_at = timestamp($p_now)",
                {
                    "p_id": emid,
                    "p_label": label,
                    "p_val": emotion.get("valence", 0.0),
                    "p_aro": emotion.get("arousal", 0.5),
                    "p_desc": emotion.get("context", ""),
                    "p_agent": agent_id,
                    "p_now": now_str
                }
            )
            
            # Link emotion to episode
            conn.execute(
                "MATCH (ep:Episode {id: $p_epid}), (em:Emotion {id: $p_emid}) "
                "MERGE (ep)-[r:EPISODE_EVOKES]->(em) "
                "ON CREATE SET r.intensity = $p_int, r.created_at = datetime($p_now)",
                {
                    "p_epid": episode_id, "p_emid": emid,
                    "p_int": emotion.get("arousal", 0.5),
                    "p_now": now_str
                }
            )
            
            # Link emotion to related entities
            for about_name in emotion.get("about", []):
                about_id = entity_ids.get(about_name, generate_id("ent", normalize_entity_name(about_name)))
                try:
                    conn.execute(
                        "MATCH (e:Entity {id: $p_eid}), (em:Emotion {id: $p_emid}) "
                        "MERGE (e)-[r:ENTITY_EVOKES]->(em) "
                        "ON CREATE SET r.context = $p_ctx, r.intensity = $p_int, "
                        "r.valid_at = datetime($p_vat), r.created_at = datetime($p_now)",
                        {
                            "p_eid": about_id, "p_emid": emid,
                            "p_ctx": emotion.get("context", ""),
                            "p_int": emotion.get("arousal", 0.5),
                            "p_vat": episode_ts, "p_now": now_str
                        }
                    )
                except Exception:
                    pass
                    
        except Exception as e:
            print(f"    ⚠️  Emotion store failed: {e}")


def ingest_file(conn: kuzu.Connection, filepath: Path, force: bool = False):
    """Ingest a single memory file into the graph."""
    print(f"\n📄 Processing: {filepath.name}")
    
    text = filepath.read_text(encoding="utf-8")
    if not text.strip():
        print("   (empty file, skipping)")
        return
    
    date_str = extract_date_from_filename(filepath)
    chunks = chunk_text(text)
    
    print(f"   {len(chunks)} chunk(s), ~{len(text)} chars")
    
    for i, chunk in enumerate(chunks):
        if len(chunk.strip()) < 50:
            continue
        
        # Strip never-store content before extraction
        clean_chunk = strip_never_store_content(chunk)
        if len(clean_chunk.strip()) < 50:
            continue
            
        print(f"   Chunk {i+1}/{len(chunks)}... ", end="", flush=True)
        
        prompt = EXTRACTION_PROMPT.format(
            text=clean_chunk,
            source_file=filepath.name,
            date=date_str or "unknown"
        )
        
        extraction = call_llm(prompt)
        
        if extraction:
            n_ent = len(extraction.get("entities", []))
            n_rel = len(extraction.get("relationships", []))
            n_fact = len(extraction.get("facts", []))
            n_emo = len(extraction.get("emotions", []))
            print(f"✅ {n_ent} entities, {n_rel} relations, {n_fact} facts, {n_emo} emotions")
            
            file_agent = extract_agent_from_filepath(filepath)
            store_extraction(conn, extraction, filepath.name, date_str, chunk, agent_id=file_agent)
        else:
            print("⚠️  extraction failed")


def _extract_file(filepath: Path) -> dict:
    """Extract entities from a single file (thread-safe, no DB access).
    Returns dict with filepath, agent_id, date_str, and list of (chunk, extraction) pairs."""
    text = ""
    result = {
        "filepath": filepath,
        "agent_id": extract_agent_from_filepath(filepath),
        "date_str": extract_date_from_filename(filepath),
        "extractions": [],
        "source_meta": {},
        "error": None
    }
    
    try:
        text = filepath.read_text(encoding="utf-8")
        if not text.strip():
            return result

        result["source_meta"] = classify_source(filepath, text)
        
        chunks = chunk_text(text)
        
        for chunk in chunks:
            if len(chunk.strip()) < 50:
                continue
            
            # Strip never-store content before extraction
            clean_chunk = strip_never_store_content(chunk)
            if len(clean_chunk.strip()) < 50:
                continue
            
            prompt = EXTRACTION_PROMPT.format(
                text=clean_chunk,
                source_file=filepath.name,
                date=result["date_str"] or "unknown"
            )
            
            extraction = call_llm(prompt)
            if extraction:
                result["extractions"].append((chunk, extraction))
    except Exception as e:
        result["error"] = str(e)
    
    return result


def ingest_all(force: bool = False, limit: int = None, workers: int = 6,
               include_exported_sessions: bool = False):
    """Ingest all unprocessed memory files with parallel LLM extraction."""
    db = get_db()
    conn = get_conn(db)
    
    # Ensure schema exists
    init_schema(conn)
    
    files = find_memory_files(include_exported_sessions=include_exported_sessions)
    
    if not files:
        print("✅ All memory files already processed")
        return
    
    if limit:
        files = files[:limit]
    
    total = len(files)
    print(f"🧠 Engram Ingestor (parallel, {workers} workers)")
    print(f"   Found {total} file(s) to process")
    
    processed = get_processed_files()
    done = 0
    failed = 0
    
    # Process in parallel: LLM extraction is the bottleneck, DB writes are fast
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(_extract_file, f): f for f in files}
        
        for future in as_completed(futures):
            filepath = futures[future]
            done += 1
            
            try:
                result = future.result()
                
                if result["error"]:
                    print(f"   ❌ [{done}/{total}] {filepath.name}: {result['error']}")
                    failed += 1
                    continue
                
                n_ext = len(result["extractions"])
                if n_ext == 0:
                    print(f"   ⏭️  [{done}/{total}] {filepath.name} (empty/skipped)")
                else:
                    total_ent = sum(len(e.get("entities", [])) for _, e in result["extractions"])
                    total_fact = sum(len(e.get("facts", [])) for _, e in result["extractions"])
                    print(f"   ✅ [{done}/{total}] {filepath.name} -> {total_ent} entities, {total_fact} facts [agent:{result['agent_id']}]")
                    
                    # Store all extractions (serialized DB writes)
                    for chunk, extraction in result["extractions"]:
                        store_extraction(
                            conn, extraction, filepath.name,
                            result["date_str"], chunk,
                            agent_id=result["agent_id"],
                            source_meta=result.get("source_meta") or {}
                        )
                
                # Mark as processed
                processed[str(filepath)] = str(filepath.stat().st_mtime)
                save_processed_files(processed)
                
            except Exception as e:
                print(f"   ❌ [{done}/{total}] {filepath.name}: {e}")
                failed += 1
    
    print(f"\n📊 Complete: {done - failed} succeeded, {failed} failed")
    
    # Print final stats
    stats = get_stats(conn)
    print_stats(stats)


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="Engram Memory Ingestor")
    parser.add_argument("--force", action="store_true", help="Re-process all files")
    parser.add_argument("--limit", type=int, help="Max files to process")
    parser.add_argument("--workers", type=int, default=6, help="Parallel workers (default: 6)")
    parser.add_argument("--file", type=str, help="Process a specific file")
    parser.add_argument("--include-exported-sessions", action="store_true",
                        help="Also ingest engram/exported-sessions/*.md archive dumps")
    args = parser.parse_args()
    
    if args.force:
        # Clear processed log
        if PROCESSED_LOG.exists():
            PROCESSED_LOG.unlink()
    
    if args.file:
        db = get_db()
        conn = get_conn(db)
        init_schema(conn)
        ingest_file(conn, Path(args.file))
        stats = get_stats(conn)
        print_stats(stats)
    else:
        ingest_all(
            force=args.force,
            limit=args.limit,
            workers=args.workers,
            include_exported_sessions=args.include_exported_sessions,
        )
