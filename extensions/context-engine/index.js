import fs from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";

const DEFAULTS = {
  enabled: true,
  workspaceRoot: "",      // Auto-detected from OpenClaw workspace or engram config.json
  engramDir: "",          // Auto-detected: <workspaceRoot>/engram
  pythonBin: "",          // Auto-detected: <workspaceRoot>/.venv-memory/bin/python
  agentsDir: "",          // Auto-detected: ~/.openclaw/agents
  topK: 8,
  maxChars: 6000,
  maxMemories: 8,
  debug: false,
  storeAssistantMessages: true,
  storeUserMessages: true,
  includeSystemPromptAddition: true,
  ownsCompaction: false
};

function loadEngramConfig(engramDir) {
  try {
    const cfgPath = path.join(engramDir, "config.json");
    if (fs.existsSync(cfgPath)) {
      return JSON.parse(fs.readFileSync(cfgPath, "utf-8"));
    }
  } catch (e) { /* ignore */ }
  return {};
}

function getConfig(api) {
  const pluginCfg = api?.config ?? {};
  const merged = { ...DEFAULTS, ...pluginCfg };

  // Auto-detect paths if not explicitly set
  if (!merged.workspaceRoot) {
    // Try plugin config context_engine.workspace_root from engram config.json
    const homeDir = process.env.HOME || process.env.USERPROFILE || "";
    merged.workspaceRoot = path.join(homeDir, "clawd"); // fallback
  }
  if (!merged.engramDir) {
    merged.engramDir = path.join(merged.workspaceRoot, "engram");
  }
  if (!merged.pythonBin) {
    merged.pythonBin = path.join(merged.workspaceRoot, ".venv-memory", "bin", "python");
  }
  if (!merged.agentsDir) {
    const homeDir = process.env.HOME || process.env.USERPROFILE || "";
    merged.agentsDir = path.join(homeDir, ".openclaw", "agents");
  }

  // Override from engram config.json if available
  const engramCfg = loadEngramConfig(merged.engramDir);
  if (engramCfg.context_engine) {
    const ce = engramCfg.context_engine;
    if (ce.workspace_root) merged.workspaceRoot = ce.workspace_root;
    if (ce.engram_dir) merged.engramDir = ce.engram_dir;
    if (ce.python_bin) merged.pythonBin = ce.python_bin;
    if (ce.agents_dir) merged.agentsDir = ce.agents_dir;
  }

  return merged;
}

function ensureDir(dir) {
  fs.mkdirSync(dir, { recursive: true });
}

function safeString(value) {
  if (value == null) return "";
  if (typeof value === "string") return value;
  try { return JSON.stringify(value, null, 2); } catch { return String(value); }
}

function flattenContent(content) {
  if (typeof content === "string") return content;
  if (Array.isArray(content)) {
    return content
      .map((part) => {
        if (typeof part === "string") return part;
        if (part?.text) return part.text;
        if (part?.content) return safeString(part.content);
        return safeString(part);
      })
      .join("\n")
      .trim();
  }
  if (content && typeof content === "object") {
    if (typeof content.text === "string") return content.text;
    return safeString(content);
  }
  return "";
}

function messageText(message) {
  return flattenContent(message?.content).trim();
}

function normalizeMessages(messages) {
  return (messages || [])
    .map((m, idx) => ({ index: idx, role: m?.role || "unknown", text: messageText(m) }))
    .filter((m) => m.text);
}

// ─── Session resolution (for agent scoping) ───────────────────────────

const _liveCache = new Map();

function extractAgentFromPath(sessionFile) {
  if (!sessionFile) return null;
  const match = sessionFile.match(/agents\/([^/]+)\/sessions\//);
  return match ? match[1] : null;
}

function resolveAgentId(cfg, sessionId, sessionFile) {
  const cached = _liveCache.get(sessionId);
  if (cached) return cached;

  // From sessionFile path
  const fromPath = extractAgentFromPath(sessionFile);
  if (fromPath) {
    _liveCache.set(sessionId, fromPath);
    return fromPath;
  }

  // Scan sessions.json files
  try {
    const agents = fs.readdirSync(cfg.agentsDir, { withFileTypes: true });
    for (const a of agents) {
      if (!a.isDirectory()) continue;
      const sessionsFile = path.join(cfg.agentsDir, a.name, "sessions", "sessions.json");
      if (!fs.existsSync(sessionsFile)) continue;
      try {
        const data = JSON.parse(fs.readFileSync(sessionsFile, "utf8"));
        for (const meta of Object.values(data)) {
          if (meta?.sessionId === sessionId) {
            _liveCache.set(sessionId, a.name);
            return a.name;
          }
        }
      } catch { /* skip */ }
    }
  } catch { /* skip */ }

  // Fallback: scan disk for session file
  try {
    const agents = fs.readdirSync(cfg.agentsDir, { withFileTypes: true });
    for (const a of agents) {
      if (!a.isDirectory()) continue;
      if (fs.existsSync(path.join(cfg.agentsDir, a.name, "sessions", `${sessionId}.jsonl`))) {
        _liveCache.set(sessionId, a.name);
        return a.name;
      }
    }
  } catch { /* skip */ }

  return "main"; // safe default
}

// ─── Engram query interface ───────────────────────────────────────────

function queryEngram(cfg, searchTerms, agentId) {
  const scriptPath = path.join(cfg.engramDir, "context_query.py");
  if (!fs.existsSync(scriptPath) || !fs.existsSync(cfg.pythonBin)) {
    return { ok: false, entities: [], facts: [], episodes: [] };
  }

  const args = [scriptPath, "query", searchTerms, "--json", "--limit", String(cfg.topK)];
  if (agentId) {
    args.push("--agent", agentId);
  }

  try {
    const result = spawnSync(cfg.pythonBin, args, {
      cwd: cfg.workspaceRoot,
      timeout: 5000,
      encoding: "utf8",
      stdio: ["pipe", "pipe", "pipe"]
    });

    if (result.status === 0 && result.stdout) {
      return JSON.parse(result.stdout.trim());
    }
  } catch { /* ignore */ }

  return { ok: false, entities: [], facts: [], episodes: [] };
}

function storeFact(cfg, content, agentId) {
  const scriptPath = path.join(cfg.engramDir, "context_query.py");
  if (!fs.existsSync(scriptPath) || !fs.existsSync(cfg.pythonBin)) return;

  try {
    spawnSync(cfg.pythonBin, [
      scriptPath, "store",
      "--fact", content,
      "--agent", agentId || "shared",
      "--category", "preference",
      "--importance", "0.8"
    ], {
      cwd: cfg.workspaceRoot,
      timeout: 5000,
      encoding: "utf8",
      stdio: "pipe"
    });
  } catch { /* ignore */ }
}

// ─── JSONL live store (session-local working memory) ──────────────────

function storageBase(cfg) {
  const base = path.join(cfg.workspaceRoot, ".engram-live-memory");
  ensureDir(base);
  ensureDir(path.join(base, "session"));
  return base;
}

function sessionStorePath(cfg, sessionId) {
  const safeId = String(sessionId || "unknown").replace(/[^a-zA-Z0-9._-]+/g, "_");
  return path.join(storageBase(cfg), "session", `${safeId}.jsonl`);
}

function appendJsonl(file, record) {
  ensureDir(path.dirname(file));
  fs.appendFileSync(file, `${JSON.stringify(record)}\n`, "utf8");
}

// ─── Durable fact detection ───────────────────────────────────────────

function looksDurable(text) {
  const lower = String(text || "").toLowerCase();
  return [
    "remember this", "remember ", "favorite", "prefers", "likes",
    "birthday", "policy", "always", "never", "project",
    "deploy", "important", "phone number", "address",
    "marketplace", "working on", "best friend", "aka",
    "anniversary", "password", "allergic", "maiden name"
  ].some((needle) => lower.includes(needle));
}

function extractDurableFact(text) {
  const cleaned = stripEnvelope(text);
  if (!cleaned) return null;
  // Find the best durable line
  const lines = cleaned.split("\n").map(l => l.trim()).filter(l => l.length > 5);
  for (const line of lines) {
    if (line.toLowerCase().startsWith("remember")) return line;
    if (looksDurable(line) && line.length > 10 && line.length < 300) return line;
  }
  return lines[lines.length - 1] || cleaned.slice(0, 200);
}

// ─── Build retrieval query from recent messages ───────────────────────

function stripEnvelope(text) {
  // OpenClaw wraps Discord messages in metadata envelopes.
  // Format:
  //   Conversation info (untrusted metadata):
  //   ```json
  //   { ... }
  //   ```
  //   Sender (untrusted metadata):
  //   ```json
  //   { ... }
  //   ```
  //   [optional: Replied message (untrusted, for context):]
  //   [```json ... ```]
  //   <actual user message>
  //   [optional: Untrusted context ...]
  //   [<<<EXTERNAL_UNTRUSTED_CONTENT ...>>>]

  let result = text;

  // 1) Remove all ```json ... ``` code blocks (metadata JSON)
  result = result.replace(/```json[\s\S]*?```/g, "");

  // 2) Remove envelope headers
  result = result.replace(/^Conversation info \(untrusted metadata\):\s*/gm, "");
  result = result.replace(/^Sender \(untrusted metadata\):\s*/gm, "");
  result = result.replace(/^Replied message \(untrusted.*?\):\s*/gm, "");
  result = result.replace(/^Untrusted context.*:\s*/gm, "");

  // 3) Remove EXTERNAL_UNTRUSTED_CONTENT blocks
  result = result.replace(/<<<EXTERNAL_UNTRUSTED_CONTENT[\s\S]*?<<<END_EXTERNAL_UNTRUSTED_CONTENT[^>]*>>>/g, "");
  // Also catch opening tags without closing
  result = result.replace(/<<<EXTERNAL_UNTRUSTED_CONTENT[\s\S]*/g, "");

  // 4) Remove Discord channel topic metadata
  result = result.replace(/UNTRUSTED channel metadata.*[\s\S]*?Discord channel topic:.*$/gm, "");

  // 5) Clean up remaining artifacts
  result = result.replace(/```/g, "");
  result = result.replace(/@\w+/g, ""); // strip @mentions

  // 6) Collapse whitespace
  result = result.replace(/\n{2,}/g, "\n").trim();

  return result;
}

function buildSearchTerms(messages) {
  const recent = normalizeMessages(messages).slice(-4);
  const userMsgs = recent.filter(m => m.role === "user");
  const lastUser = userMsgs[userMsgs.length - 1];
  if (!lastUser) return "";

  const cleaned = stripEnvelope(lastUser.text);
  if (!cleaned || cleaned.length < 3) return "";

  // Extract meaningful keywords — remove stop words and punctuation
  const stopWords = new Set([
    "what", "when", "where", "who", "how", "which", "why",
    "is", "are", "was", "were", "the", "a", "an", "and", "or",
    "my", "your", "his", "her", "its", "our", "their",
    "do", "does", "did", "has", "have", "had",
    "this", "that", "these", "those", "it", "they",
    "from", "with", "for", "about", "into", "of", "to", "in", "on", "at",
    "can", "could", "would", "should", "will", "shall",
    "not", "no", "yes", "just", "also", "very", "much",
    "tell", "check", "know", "said", "says", "please", "hey",
    "jarvis", "me", "you", "we", "him", "them"
  ]);

  const words = cleaned
    .toLowerCase()
    .replace(/['']/g, "") // remove apostrophes
    .replace(/[^a-z0-9\s-]/g, " ")
    .split(/\s+/)
    .filter(w => w.length >= 3 && !stopWords.has(w));

  // Return unique keywords joined by space (for Engram CONTAINS matching)
  const unique = [...new Set(words)].slice(0, 8);
  return unique.join(" ");
}

function queryEngramMulti(cfg, searchTerms, agentId) {
  // Query Engram with individual keywords for better CONTAINS matching
  const words = searchTerms.split(/\s+/).filter(w => w.length >= 3);
  if (!words.length) return { ok: false, entities: [], facts: [], episodes: [] };

  // Try the full phrase first, then individual important words
  const queries = [searchTerms];
  for (const word of words.slice(0, 4)) {
    if (word.length >= 4) queries.push(word);
  }

  const allFacts = new Map();
  const allEntities = new Map();
  const allEpisodes = new Map();

  for (const q of queries) {
    const results = queryEngram(cfg, q, agentId);
    if (!results.ok) continue;
    for (const f of results.facts || []) { allFacts.set(f.id, f); }
    for (const e of results.entities || []) { allEntities.set(e.id, e); }
    for (const ep of results.episodes || []) { allEpisodes.set(ep.id, ep); }
  }

  return {
    ok: true,
    facts: [...allFacts.values()].sort((a, b) => (b.importance || 0) - (a.importance || 0)),
    entities: [...allEntities.values()].sort((a, b) => (b.importance || 0) - (a.importance || 0)),
    episodes: [...allEpisodes.values()]
  };
}

// ─── Format Engram results for system prompt ──────────────────────────

function formatEngramResults(results) {
  if (!results || !results.ok) return "";
  const lines = [];

  const facts = results.facts || [];
  const entities = results.entities || [];
  const episodes = results.episodes || [];

  if (!facts.length && !entities.length && !episodes.length) return "";

  lines.push("Relevant long-term memory recalled for this session:");

  for (const f of facts.slice(0, 6)) {
    const cat = f.category ? `[${f.category}]` : "";
    lines.push(`- ${cat} ${f.content}`);
  }

  for (const e of entities.slice(0, 4)) {
    if (e.description) {
      lines.push(`- ${e.name} (${e.type || ""}): ${e.description}`);
    }
  }

  for (const ep of episodes.slice(0, 3)) {
    const date = (ep.occurred_at || "").slice(0, 10);
    lines.push(`- [${date}] ${ep.summary || ""}`);
  }

  return lines.join("\n").slice(0, DEFAULTS.maxChars);
}

// ─── Persist messages ─────────────────────────────────────────────────

function persistMessages(cfg, sessionId, agentId, messages, source) {
  const normalized = normalizeMessages(messages);
  const sessionFile = sessionStorePath(cfg, sessionId);
  let count = 0;

  for (const msg of normalized) {
    if (msg.role === "assistant" && !cfg.storeAssistantMessages) continue;
    if (msg.role === "user" && !cfg.storeUserMessages) continue;

    // Session-local store (working memory)
    appendJsonl(sessionFile, {
      ts: Date.now(),
      sessionId,
      agentId,
      role: msg.role,
      source,
      text: msg.text.slice(0, 500)
    });

    // If durable, store directly into Engram graph
    if (msg.role === "user" && looksDurable(msg.text)) {
      const fact = extractDurableFact(msg.text);
      if (fact) {
        storeFact(cfg, fact, agentId);
      }
    }

    count += 1;
  }
  return count;
}

// ─── Plugin registration ──────────────────────────────────────────────

export default function register(api) {
  const cfg = getConfig(api);

  api.registerContextEngine("engram-context-engine", () => ({
    info: {
      id: "engram-context-engine",
      name: "Engram Context Engine",
      version: "1.0.0",
      ownsCompaction: Boolean(cfg.ownsCompaction)
    },

    async bootstrap({ sessionId, sessionFile }) {
      resolveAgentId(cfg, sessionId, sessionFile);
      return { bootstrapped: true, importedMessages: 0, reason: "engram-direct" };
    },

    async ingest({ sessionId, message }) {
      const agentId = resolveAgentId(cfg, sessionId, null);
      const count = persistMessages(cfg, sessionId, agentId, [message], "ingest");
      return { ingested: count > 0 };
    },

    async ingestBatch({ sessionId, messages }) {
      const agentId = resolveAgentId(cfg, sessionId, null);
      const count = persistMessages(cfg, sessionId, agentId, messages, "ingest-batch");
      return { ingestedCount: count };
    },

    async afterTurn({ sessionId, sessionFile, messages, prePromptMessageCount }) {
      const agentId = resolveAgentId(cfg, sessionId, sessionFile);
      const newMessages = Array.isArray(messages)
        ? messages.slice(Math.max(0, prePromptMessageCount || 0))
        : [];
      if (newMessages.length) {
        persistMessages(cfg, sessionId, agentId, newMessages, "afterTurn");
      }
    },

    async assemble({ sessionId, messages }) {
      const searchTerms = buildSearchTerms(messages);
      if (!searchTerms || searchTerms.length < 3) {
        return { messages, estimatedTokens: 0 };
      }

      // Query Engram directly — one memory system, multi-term search
      const agentId = resolveAgentId(cfg, sessionId, null);
      const results = queryEngramMulti(cfg, searchTerms, agentId);
      const addition = formatEngramResults(results);

      return {
        messages,
        estimatedTokens: 0,
        systemPromptAddition: cfg.includeSystemPromptAddition ? addition : undefined
      };
    },

    async compact({ sessionId }) {
      return {
        ok: true,
        compacted: false,
        reason: `Engram v1.0 active for ${sessionId}; using legacy runtime compaction.`
      };
    },

    async prepareSubagentSpawn() {
      return { rollback: async () => {} };
    },

    async onSubagentEnded() { return; }
  }));
}
