import fs from "node:fs";
import path from "node:path";
import { spawn, spawnSync } from "node:child_process";

const DEFAULTS = {
  enabled: true,
  workspaceRoot: "",
  engramDir: "",
  pythonBin: "",
  agentsDir: "",
  topK: 8,
  maxChars: 6000,
  maxMemories: 8,
  debug: false,
  storeAssistantMessages: true,
  storeUserMessages: true,
  includeSystemPromptAddition: true,
  ownsCompaction: false,
  keepRecentMessages: 12,
};

function log(cfg, level, message, extra = undefined) {
  if (!cfg?.debug && level === "debug") return;
  const suffix = extra ? ` ${JSON.stringify(extra)}` : "";
  console[level === "error" ? "error" : "log"](`[engram-context-engine] ${message}${suffix}`);
}

function safeRun(cfg, label, fallback, fn) {
  try {
    return fn();
  } catch (err) {
    log(cfg, "error", `${label} failed`, { error: String(err?.stack || err) });
    return fallback;
  }
}

function loadEngramConfig(engramDir) {
  return safeRun({}, "loadEngramConfig", {}, () => {
    const cfgPath = path.join(engramDir, "config.json");
    if (fs.existsSync(cfgPath)) return JSON.parse(fs.readFileSync(cfgPath, "utf-8"));
    return {};
  });
}

function getConfig(api) {
  // api.config may be the full openclaw.json or the plugin-scoped config block.
  // Handle both: extract the plugin entry config if we detect the full config shape.
  let pluginCfg = api?.config ?? {};
  if (pluginCfg.plugins?.entries?.["engram-context-engine"]?.config) {
    pluginCfg = pluginCfg.plugins.entries["engram-context-engine"].config;
  } else if (pluginCfg.gateway || pluginCfg.channels || pluginCfg.agents) {
    // This is clearly the full config, but our entry is missing — use defaults only
    pluginCfg = {};
  }
  const merged = { ...DEFAULTS, ...pluginCfg };
  const homeDir = process.env.HOME || process.env.USERPROFILE || "";

  if (!merged.workspaceRoot) merged.workspaceRoot = path.join(homeDir, "clawd");
  if (!merged.engramDir) merged.engramDir = path.join(merged.workspaceRoot, "engram");
  if (!merged.pythonBin) merged.pythonBin = path.join(merged.workspaceRoot, ".venv-memory", "bin", "python");
  if (!merged.agentsDir) merged.agentsDir = path.join(homeDir, ".openclaw", "agents");

  // Engram's own config is fallback-only. OpenClaw plugin config remains authoritative.
  const ce = loadEngramConfig(merged.engramDir)?.context_engine || {};
  if (!pluginCfg.workspaceRoot && ce.workspace_root) merged.workspaceRoot = ce.workspace_root;
  if (!pluginCfg.engramDir && ce.engram_dir) merged.engramDir = ce.engram_dir;
  if (!pluginCfg.pythonBin && ce.python_bin) merged.pythonBin = ce.python_bin;
  if (!pluginCfg.agentsDir && ce.agents_dir) merged.agentsDir = ce.agents_dir;

  return merged;
}

const _liveSessionAgentCache = new Map();

function extractAgentFromPath(sessionFile) {
  if (!sessionFile) return null;
  return String(sessionFile).match(/[\\/]agents[\\/]([^\\/]+)[\\/]sessions[\\/]/)?.[1] || null;
}

function resolveAgentId(cfg, sessionId, sessionFile) {
  const cached = _liveSessionAgentCache.get(sessionId);
  if (cached) return cached;
  const fromPath = extractAgentFromPath(sessionFile);
  if (fromPath) {
    _liveSessionAgentCache.set(sessionId, fromPath);
    return fromPath;
  }
  return safeRun(cfg, "resolveAgentId", "main", () => {
    const agents = fs.readdirSync(cfg.agentsDir, { withFileTypes: true });
    for (const entry of agents) {
      if (!entry.isDirectory()) continue;
      const sessFile = path.join(cfg.agentsDir, entry.name, "sessions", `${sessionId}.jsonl`);
      if (fs.existsSync(sessFile)) {
        _liveSessionAgentCache.set(sessionId, entry.name);
        return entry.name;
      }
    }
    return "main";
  });
}

function normalizeMessages(messages) {
  return (Array.isArray(messages) ? messages : [])
    .map((msg) => {
      const role = msg?.role || "user";
      let text = "";
      if (typeof msg?.text === "string") text = msg.text;
      else if (typeof msg?.content === "string") text = msg.content;
      else if (Array.isArray(msg?.content)) {
        text = msg.content
          .map((part) => typeof part?.text === "string" ? part.text : "")
          .filter(Boolean)
          .join("\n");
      }
      return { role, text: String(text || "").trim() };
    })
    .filter((m) => m.text.length > 0);
}

function appendJsonl(file, record) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.appendFileSync(file, `${JSON.stringify(record)}\n`, "utf8");
}

function summarizeRecord(msg) {
  const text = String(msg.text || "").replace(/\s+/g, " ").trim();
  return text.length > 240 ? `${text.slice(0, 237)}...` : text;
}

function shouldStoreLiveTurn(msg) {
  const role = String(msg?.role || "").trim();
  const text = String(msg?.text || "").trim();
  if (!text || text.length < 20) return false;
  if (role !== "user") return false;
  if (text === "NO_REPLY" || text === "HEARTBEAT_OK") return false;
  const lower = text.toLowerCase();
  if (lower.includes("heartbeat") && text.length < 80) return false;
  // Don't filter based on envelope metadata here — Python side strips it.
  // Only filter pure noise that has no human content at all.
  if (lower.includes("toolcall") || lower.includes("toolresult")) return false;
  return true;
}

function storeLiveTurn(cfg, sessionId, agentId, msg) {
  return safeRun(cfg, "storeLiveTurn", undefined, () => {
    if (!shouldStoreLiveTurn(msg)) return undefined;
    const script = path.join(cfg.workspaceRoot, "engram", "context_query.py");
    const env = { ...process.env, PYTHONPATH: cfg.workspaceRoot, ENGRAM_AGENT_ID: agentId };
    const res = spawnSync(
      cfg.pythonBin,
      [script, "store_live", "--text", msg.text, "--agent", agentId, "--session", sessionId, "--role", msg.role],
      { encoding: "utf8", env, timeout: 5000 }
    );
    if (res.error) {
      log(cfg, "error", "store_live spawn failed", { error: String(res.error) });
      return undefined;
    }
    if (res.status !== 0) {
      log(cfg, "error", "store_live exited non-zero", { status: res.status, stderr: String(res.stderr || "").trim().slice(0, 500) });
    }
    const out = String(res.stdout || "").trim();
    if (!out) return undefined;
    try {
      return JSON.parse(out);
    } catch (err) {
      log(cfg, "error", "store_live returned invalid JSON", { error: String(err), stdout: out.slice(0, 500) });
      return undefined;
    }
  });
}

function storeLiveLLM(cfg, sessionId, agentId, msg) {
  return safeRun(cfg, "storeLiveLLM", undefined, () => {
    if (msg?.role !== "user") return undefined;
    if (!shouldStoreLiveTurn(msg)) return undefined;
    if (String(msg?.text || "").length <= 30) return undefined;
    const script = path.join(cfg.workspaceRoot, "engram", "context_query.py");
    const env = { ...process.env, PYTHONPATH: cfg.workspaceRoot, ENGRAM_AGENT_ID: agentId };
    const child = spawn(
      cfg.pythonBin,
      [script, "extract_llm", "--text", msg.text, "--agent", agentId, "--session", sessionId],
      { env, stdio: "ignore", detached: true }
    );
    child.on("error", (err) => {
      log(cfg, "error", "extract_llm spawn failed", { error: String(err) });
    });
    child.unref();
    return { started: true };
  });
}

function looksDurable(text) {
  const lower = String(text || "").toLowerCase();
  return ["remember this", "remember ", "favorite", "prefers", "likes", "birthday", "policy", "always", "never", "project", "important", "working on", "best friend", "aka"].some((n) => lower.includes(n));
}

function storageBase(cfg) {
  const base = path.join(cfg.workspaceRoot, ".engram-live-memory");
  fs.mkdirSync(path.join(base, "session"), { recursive: true });
  fs.mkdirSync(path.join(base, "shared"), { recursive: true });
  return base;
}

function safeKey(value) {
  return String(value || "unknown").replace(/[^a-zA-Z0-9._-]+/g, "_");
}

function sessionStorePath(cfg, sessionId) {
  return path.join(storageBase(cfg), "session", `${safeKey(sessionId)}.jsonl`);
}

function sharedStorePath(cfg, agentId) {
  return path.join(storageBase(cfg), "shared", `${safeKey(agentId)}.jsonl`);
}

function persistMessages(cfg, sessionId, agentId, messages, source = "turn") {
  return safeRun(cfg, "persistMessages", 0, () => {
    const normalized = normalizeMessages(messages);
    const sessionFile = sessionStorePath(cfg, sessionId);
    const sharedFile = sharedStorePath(cfg, agentId);
    let count = 0;
    for (const msg of normalized) {
      if (msg.role === "assistant" && !cfg.storeAssistantMessages) continue;
      if (msg.role === "user" && !cfg.storeUserMessages) continue;
      const record = { ts: Date.now(), sessionId, agentId, role: msg.role, source, text: msg.text, summary: summarizeRecord(msg) };
      appendJsonl(sessionFile, record);
      if (looksDurable(msg.text)) appendJsonl(sharedFile, record);
      count += 1;
    }
    return count;
  });
}

function buildSearchTerms(messages) {
  const recent = normalizeMessages(messages).slice(-6);
  const text = recent.map((m) => m.text).join(" \n ").toLowerCase();
  return [...new Set(text.split(/[^a-z0-9_#@.-]+/).filter((t) => t.length >= 4))].slice(0, 24);
}

function queryEngramMulti(cfg, searchTerms, agentId) {
  return safeRun(cfg, "queryEngramMulti", [], () => {
    const script = path.join(cfg.workspaceRoot, "engram", "context_query.py");
    const env = { ...process.env, PYTHONPATH: cfg.workspaceRoot, ENGRAM_AGENT_ID: agentId || "main" };
    const res = spawnSync(cfg.pythonBin, [script, "query", searchTerms.join(" "), "--agent", agentId || "main", "--limit", String(cfg.topK || 8), "--json"], { encoding: "utf8", env, timeout: 15000 });
    if (res.status !== 0) return [];
    const out = String(res.stdout || "").trim();
    if (!out) return [];
    return JSON.parse(out);
  });
}

function formatEngramResults(results) {
  if (!results || typeof results !== "object") return "";
  // Handle structured response: {ok, entities, facts, episodes}
  const entities = Array.isArray(results.entities) ? results.entities : [];
  const facts = Array.isArray(results.facts) ? results.facts : [];
  const episodes = Array.isArray(results.episodes) ? results.episodes : [];
  // Also handle legacy flat array
  const flatItems = Array.isArray(results) ? results : [];
  const allItems = [...facts, ...entities, ...episodes, ...flatItems];
  if (!allItems.length) return "";
  const lines = ["Relevant Engram memory:"];
  const seen = new Set();
  for (const r of allItems.slice(0, 12)) {
    const text = String(r?.content || r?.text || r?.description || r?.summary || "").trim();
    if (!text || seen.has(text)) continue;
    seen.add(text);
    const name = r?.name ? `[${r.name}] ` : "";
    lines.push(`- ${name}${text}`);
  }
  return lines.length > 1 ? lines.join("\n") : "";
}

function sessionFileFor(cfg, sessionId, sessionFile) {
  if (sessionFile && fs.existsSync(sessionFile)) return sessionFile;
  const agentId = resolveAgentId(cfg, sessionId, sessionFile);
  const byAgent = path.join(cfg.agentsDir, agentId, "sessions", `${sessionId}.jsonl`);
  if (fs.existsSync(byAgent)) return byAgent;
  return null;
}

function loadSessionTranscript(cfg, sessionId, sessionFile) {
  return safeRun(cfg, "loadSessionTranscript", [], () => {
    const file = sessionFileFor(cfg, sessionId, sessionFile);
    if (!file || !fs.existsSync(file)) return [];
    return fs.readFileSync(file, "utf8")
      .split(/\r?\n/)
      .map((line) => line.trim())
      .filter(Boolean)
      .map((line) => { try { return JSON.parse(line); } catch { return null; } })
      .filter(Boolean);
  });
}

function normalizeTranscriptMessages(entries) {
  const out = [];
  for (const entry of Array.isArray(entries) ? entries : []) {
    const msg = entry?.message;
    if (!msg) continue;
    const role = msg.role || "user";
    let text = "";
    if (typeof msg.content === "string") text = msg.content;
    else if (Array.isArray(msg.content)) {
      text = msg.content
        .map((part) => typeof part?.text === "string" ? part.text : "")
        .filter(Boolean)
        .join("\n");
    }
    text = String(text || "").trim();
    if (!text) continue;
    out.push({ role, text, timestamp: entry.timestamp || null });
  }
  return out;
}

function stripCompactionNoise(messages) {
  return (Array.isArray(messages) ? messages : []).filter((m) => {
    const text = String(m.text || "").trim();
    if (!text) return false;
    if (text.startsWith("Compaction skipped: Engram")) return false;
    if (text.length > 12000 && (text.includes("toolCall") || text.includes("toolResult"))) return false;
    return true;
  });
}

function splitTranscriptForCompaction(messages, opts = {}) {
  const keepRecent = Math.max(8, Number(opts.keepRecentMessages || 12));
  if (!Array.isArray(messages) || messages.length <= keepRecent + 4) return { olderMessages: [], recentTail: messages || [] };
  return { olderMessages: messages.slice(0, -keepRecent), recentTail: messages.slice(-keepRecent) };
}

function extractExplicitDurableMemories(messages) {
  const needles = ["remember this", "remember ", "always ", "never ", "favorite", "prefers", "likes ", "birthday", "anniversary", "policy", "working on", "project is"];
  return (Array.isArray(messages) ? messages : [])
    .filter((m) => m.role === "user" || m.role === "assistant")
    .map((m) => ({ ...m, lower: String(m.text || "").toLowerCase() }))
    .filter((m) => needles.some((n) => m.lower.includes(n)))
    .map((m) => ({ role: m.role, text: m.text, summary: summarizeRecord(m) }));
}

function dedupeDurableRecords(records) {
  const seen = new Set();
  const out = [];
  for (const rec of Array.isArray(records) ? records : []) {
    const key = String(rec.summary || rec.text || "").toLowerCase();
    if (!key || seen.has(key)) continue;
    seen.add(key);
    out.push(rec);
  }
  return out;
}

function estimateTokenCountFromText(text) {
  return Math.ceil(String(text || "").length / 4);
}

function bulletize(lines) {
  const clean = lines.map((s) => String(s || "").trim()).filter(Boolean);
  if (!clean.length) return ["- None"];
  return clean.slice(0, 8).map((s) => `- ${s}`);
}

function buildStructuredCompactionSummary(messages) {
  const items = Array.isArray(messages) ? messages : [];
  const userMsgs = items.filter((m) => m.role === "user").map((m) => m.text);
  const asstMsgs = items.filter((m) => m.role === "assistant").map((m) => m.text);
  const objective = bulletize(userMsgs.slice(-3).map((t) => summarizeRecord({ text: t })));
  const established = bulletize(asstMsgs.slice(-5).map((t) => summarizeRecord({ text: t })));
  const decisions = bulletize(items.map((m) => m.text).filter((t) => /decid|will do|going to|fixed|changed|disabled|enabled/i.test(t)).map((t) => summarizeRecord({ text: t })).slice(-6));
  const openLoops = bulletize(items.map((m) => m.text).filter((t) => /todo|next|need to|follow up|pending|block|later/i.test(t)).map((t) => summarizeRecord({ text: t })).slice(-6));
  return ["Compacted session state", "", "Objective:", ...objective, "", "Established facts:", ...established, "", "Decisions made:", ...decisions, "", "Open loops:", ...openLoops].join("\n");
}

function buildCompactedMessages(summary, recentTail) {
  const msgs = [{ role: "system", content: `Compacted context generated by Engram. Use it as working memory summary for this session.\n\n${String(summary || "")}` }];
  for (const m of Array.isArray(recentTail) ? recentTail : []) msgs.push({ role: m.role || "user", content: m.text || "" });
  return msgs;
}

export default function register(api) {
  console.log("[engram-context-engine] DEBUG api.config raw:", JSON.stringify(api?.config ?? "UNDEFINED"));
  const cfg = getConfig(api);
  log(cfg, "log", "register called", { ownsCompaction: !!cfg.ownsCompaction });

  api.registerContextEngine("engram-context-engine", () => {
    const engine = {
      info: {
        id: "engram-context-engine",
        name: "Engram Context Engine",
        version: "1.0.2",
        ownsCompaction: Boolean(cfg.ownsCompaction)
      },

      async bootstrap({ sessionId, sessionFile }) {
        return safeRun(cfg, "bootstrap", { bootstrapped: true, importedMessages: 0, reason: "engram-fallback" }, () => {
          resolveAgentId(cfg, sessionId, sessionFile);
          return { bootstrapped: true, importedMessages: 0, reason: "engram-direct" };
        });
      },

      async ingest({ sessionId, message }) {
        return safeRun(cfg, "ingest", { ingested: false }, () => {
          const agentId = resolveAgentId(cfg, sessionId, null);
          const count = persistMessages(cfg, sessionId, agentId, [message], "ingest");
          return { ingested: count > 0 };
        });
      },

      async ingestBatch({ sessionId, messages }) {
        return safeRun(cfg, "ingestBatch", { ingestedCount: 0 }, () => {
          const agentId = resolveAgentId(cfg, sessionId, null);
          const count = persistMessages(cfg, sessionId, agentId, messages, "ingest-batch");
          return { ingestedCount: count };
        });
      },

      async afterTurn({ sessionId, sessionFile, messages, prePromptMessageCount }) {
        return safeRun(cfg, "afterTurn", undefined, () => {
          const agentId = resolveAgentId(cfg, sessionId, sessionFile);
          const newMessages = Array.isArray(messages) ? messages.slice(Math.max(0, prePromptMessageCount || 0)) : [];
          const normalized = normalizeMessages(newMessages);
          if (normalized.length) {
            persistMessages(cfg, sessionId, agentId, normalized, "afterTurn");
            for (const msg of normalized) {
              // Only store user messages via live extraction — assistant messages are our own output
              if (msg?.role !== "user") continue;
              const liveResult = storeLiveTurn(cfg, sessionId, agentId, msg);
              if (
                String(msg?.text || "").length > 30 &&
                shouldStoreLiveTurn(msg) &&
                Number(liveResult?.stored || 0) === 0 &&
                liveResult?.reason === "no_candidates"
              ) {
                storeLiveLLM(cfg, sessionId, agentId, msg);
              }
            }
          }
          return undefined;
        });
      },

      async assemble({ sessionId, messages }) {
        return safeRun(cfg, "assemble", { messages, estimatedTokens: 0 }, () => {
          const searchTerms = buildSearchTerms(messages);
          if (!searchTerms || searchTerms.length < 3) return { messages, estimatedTokens: 0 };
          const agentId = resolveAgentId(cfg, sessionId, null);
          const results = queryEngramMulti(cfg, searchTerms, agentId);
          const addition = formatEngramResults(results);
          return { messages, estimatedTokens: 0, systemPromptAddition: cfg.includeSystemPromptAddition ? addition : undefined };
        });
      },

      async compact({ sessionId, sessionFile }) {
        return safeRun(cfg, "compact", { ok: true, compacted: false, reason: `Engram compaction fallback for ${sessionId}.` }, () => {
          if (!cfg.ownsCompaction) {
            return { ok: true, compacted: false, reason: `Engram compaction ownership disabled for ${sessionId}; defer to default runtime compaction.` };
          }
          const agentId = resolveAgentId(cfg, sessionId, sessionFile || null);
          const transcript = loadSessionTranscript(cfg, sessionId, sessionFile || null);
          const normalized = stripCompactionNoise(normalizeTranscriptMessages(transcript));
          const { olderMessages, recentTail } = splitTranscriptForCompaction(normalized, { keepRecentMessages: cfg.keepRecentMessages || 12 });
          if (!olderMessages.length) {
            return { ok: true, compacted: false, reason: `Engram found too little older history to compact for ${sessionId}.` };
          }
          const durable = dedupeDurableRecords(extractExplicitDurableMemories(olderMessages));
          if (durable.length) persistMessages(cfg, sessionId, agentId, durable.map((d) => ({ role: d.role, text: d.text })), "compaction-durable");
          const summary = buildStructuredCompactionSummary(olderMessages);
          const messages = buildCompactedMessages(summary, recentTail);
          const tokensBefore = estimateTokenCountFromText(olderMessages.map((m) => m.text).join("\n"));
          const tokensAfter = estimateTokenCountFromText(messages.map((m) => String(m.content || "")).join("\n"));

          // Write compaction summary to memory/*.md so cron pipeline can ingest it
          try {
            const memoryDir = path.join(cfg.workspaceRoot, "memory");
            fs.mkdirSync(memoryDir, { recursive: true });
            const now = new Date();
            const dateStr = now.toISOString().slice(0, 10);
            const hash = sessionId.replace(/[^a-zA-Z0-9]/g, "").slice(0, 8);
            const memFile = path.join(memoryDir, `${dateStr}-${agentId}-${hash}.md`);

            const lines = [];
            lines.push(`# ${dateStr} — Compaction flush (${agentId})`);
            lines.push(`Session: ${sessionId}`);
            lines.push(`Agent: ${agentId}`);
            lines.push(`Compacted: ${olderMessages.length} older messages → ${recentTail.length} recent kept`);
            lines.push(`Tokens: ${tokensBefore} → ${tokensAfter}`);
            lines.push("");

            if (durable.length) {
              lines.push("## Durable Memories");
              for (const d of durable) {
                const text = String(d.text || d.summary || "").trim();
                if (text) lines.push(`- ${text.slice(0, 300)}`);
              }
              lines.push("");
            }

            lines.push("## Session Summary");
            lines.push(String(summary || "No summary generated."));
            lines.push("");

            // Include key user messages for richer context
            const userMsgs = olderMessages.filter((m) => m.role === "user");
            if (userMsgs.length) {
              lines.push("## Key Messages");
              for (const m of userMsgs.slice(-8)) {
                const text = String(m.text || "").trim();
                if (text && text.length > 20 && text.length < 500) {
                  lines.push(`- [${m.role}] ${text.slice(0, 250)}`);
                }
              }
              lines.push("");
            }

            fs.writeFileSync(memFile, lines.join("\n"), "utf8");
            log(cfg, "log", `compact: wrote memory flush to ${memFile}`, { bytes: lines.join("\n").length, durable: durable.length });
          } catch (flushErr) {
            log(cfg, "error", "compact: memory flush write failed", { error: String(flushErr) });
          }

          return { ok: true, compacted: true, result: { tokensBefore, tokensAfter, summary, messages, durableMemoriesPersisted: durable.length, recentTailMessages: recentTail.length, compactedOlderMessages: olderMessages.length } };
        });
      },

      async prepareSubagentSpawn() {
        return { rollback: async () => {} };
      },

      async onSubagentEnded() { return; }
    };

    return engine;
  });
}
