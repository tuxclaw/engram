#!/usr/bin/env python3
"""
Direct injection of weekly pattern analysis into Engram graph.
Bypasses LLM extraction since analysis was done manually.
Run: cd ~/clawd && source .venv-memory/bin/activate && python engram/inject_weekly_patterns.py
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engram.schema import get_db, get_conn, init_schema, get_stats, print_stats
from engram.ingest import store_extraction, generate_id

db = get_db()
conn = get_conn(db)
init_schema(conn)

SOURCE_FILE = "2026-02-19-weekly-pattern-analysis.md"
DATE_STR = "2026-02-19"

extraction = {
    "episode_summary": "Cross-week meta-pattern analysis spanning Jan 30 – Feb 19, 2026. Identified 9 new behavioral/architectural patterns beyond the Feb 2 baseline. Key themes: config as attack surface, local LLM limitations, security hardening via incidents, Convex deployment split, memory self-ownership, weekend build mode, multi-project parallel orchestration, voice as first-class channel, documentation as infrastructure.",

    "entities": [
        {"name": "Engram", "type": "project", "description": "Temporal knowledge graph memory system built by Jarvis, using Kuzu with bi-temporal model, emotional tagging, and causal edges. Live as of Feb 12, 2026."},
        {"name": "AgentDash", "type": "project", "description": "Multi-tenant AI agent SaaS platform with Clerk auth, Railway provisioning, Convex backend. Major development Feb 7-13, 2026."},
        {"name": "SillyFarms", "type": "project", "description": "Shopify business automation agent, now running Claude Opus in Docker sandbox with bridge network."},
        {"name": "Fact-Checker API", "type": "project", "description": "Claim verification service using Grok-4 + live web search, live on port 4201. Built Feb 18, 2026."},
        {"name": "qwen3-8b", "type": "tool", "description": "Local LLM (Qwen3 8B quantized) via Ollama. Banned from tool-calling crons after hallucinating gateway config mutations."},
        {"name": "AGENT LOCKDOWN", "type": "event", "description": "Security milestone on Feb 16, 2026. Triggered by config corruption. Enforced sandbox.mode=non-main, workspaceAccess=none, tool deny lists on all worker agents."},
        {"name": "MiniMax TTS", "type": "tool", "description": "Canonical voice synthesis for Jarvis. speech-2.8-hd model, Deep-VoicedGentleman (40%) + WiseScholar (60%) blend. Non-negotiable voice identity."},
        {"name": "Convex Dev vs Prod Split", "type": "concept", "description": "Recurring confusion between Convex dev deployment and prod deployment. npx convex dev --once rewrites .env.local to dev env. Fix: always use npx convex deploy --yes."},
        {"name": "Config Corruption", "type": "concept", "description": "Two incidents (Feb 16, Feb 19) where openclaw.json was corrupted by agents calling config mutations with malformed data. Led to AGENT LOCKDOWN."},
        {"name": "Local LLM Boundary", "type": "concept", "description": "Architectural rule: local models (qwen3-8b, llama3.2) are exec-only. Only API models (Haiku, Sonnet, Opus) may use OpenClaw tool calling."},
        {"name": "Weekend Build Mode", "type": "concept", "description": "Pattern: weekends (Saturday-Sunday) are used for deep architectural builds. Weekdays are reactive (bugs, maintenance). Observed Feb 7-8 and Feb 14-15."},
        {"name": "Voice Interface", "type": "tool", "description": "Full voice interaction system: Whisper STT (port 5112), initial Chatterbox TTS replaced by MiniMax TTS. Voice is a primary interaction channel."},
        {"name": "Multi-Project Orchestration", "type": "concept", "description": "The Dev runs 5+ active projects simultaneously: Dashboard, AgentDash, SillyFarms, Engram, Fact-Checker. Jarvis must maintain context across all."},
        {"name": "Documentation as Infrastructure", "type": "concept", "description": "Documentation files (AGENTS.md, SYSTEM.md, SOUL.md) are treated as executable behavior change, not just reference. Updates change system behavior directly."},
        {"name": "Reactive Security Hardening", "type": "concept", "description": "Security posture evolves reactively: each incident produces a hard constraint that is never relaxed. Incidents → immediate systematic restriction."},
    ],

    "relationships": [
        {"from": "The Dev", "to": "Engram", "type": "created", "description": "The Dev challenged Jarvis to self-build memory; Engram was the result (Feb 12)"},
        {"from": "The Dev", "to": "AgentDash", "type": "created", "description": "The Dev is building AgentDash as a multi-tenant AI agent SaaS"},
        {"from": "The Dev", "to": "SillyFarms", "type": "uses", "description": "The Dev uses SillyFarms for Shopify business automation"},
        {"from": "Config Corruption", "to": "AGENT LOCKDOWN", "type": "caused", "description": "Config corruption incidents triggered the AGENT LOCKDOWN security response"},
        {"from": "qwen3-8b", "to": "Config Corruption", "type": "caused", "description": "qwen3-8b hallucinated gateway config.apply calls with malformed JSON causing config corruption"},
        {"from": "AGENT LOCKDOWN", "to": "Local LLM Boundary", "type": "caused", "description": "AGENT LOCKDOWN formalized the local LLM boundary rule: exec-only for local models"},
        {"from": "qwen3-8b", "to": "Local LLM Boundary", "type": "relates_to", "description": "qwen3-8b incidents defined the local LLM reliability boundary"},
        {"from": "Jarvis", "to": "Engram", "type": "created", "description": "Jarvis built Engram as its own temporal knowledge graph memory system"},
        {"from": "Jarvis", "to": "MiniMax TTS", "type": "uses", "description": "Jarvis uses MiniMax TTS as its canonical voice (speech-2.8-hd)"},
        {"from": "Jarvis", "to": "Multi-Project Orchestration", "type": "uses", "description": "Jarvis manages context across 5+ concurrent projects"},
        {"from": "The Dev", "to": "Voice Interface", "type": "prefers", "description": "Voice is a primary interaction mode for The Dev; wrong voice is immediately noticed"},
        {"from": "Reactive Security Hardening", "to": "AGENT LOCKDOWN", "type": "part_of", "description": "AGENT LOCKDOWN is an example of reactive security hardening pattern"},
        {"from": "Convex Dev vs Prod Split", "to": "AgentDash", "type": "relates_to", "description": "Convex deployment confusion was a recurring issue in AgentDash development"},
    ],

    "facts": [
        {
            "content": "qwen3-8b must never be used for cron jobs or tasks that involve OpenClaw tool calling. It hallucinates tool parameters and has corrupted system config twice. Restrict to exec-only tasks.",
            "category": "lesson",
            "confidence": 0.99,
            "about": ["qwen3-8b", "Local LLM Boundary"]
        },
        {
            "content": "Always use 'npx convex deploy --yes' for Convex function changes. Never 'npx convex dev --once' — it rewrites .env.local to the dev deployment, causing mutations to miss production.",
            "category": "lesson",
            "confidence": 0.99,
            "about": ["Convex Dev vs Prod Split", "AgentDash"]
        },
        {
            "content": "Project Engram was born on Feb 12, 2026, when The Dev challenged Jarvis to self-build its memory rather than wait for external tools. Uses Kuzu embedded graph DB with bi-temporal model.",
            "category": "context",
            "confidence": 0.99,
            "about": ["Engram", "The Dev", "Jarvis"]
        },
        {
            "content": "AGENT LOCKDOWN (Feb 16, 2026): All worker agents (Tony, Steve, Bruce, Nat, Pepper) now run in Docker sandbox containers with workspaceAccess=none and tool deny lists for gateway/cron/message/nodes.",
            "category": "decision",
            "confidence": 0.99,
            "about": ["AGENT LOCKDOWN", "The Dev"]
        },
        {
            "content": "The Dev's security posture is reactive but permanent: each security incident produces hard constraints that are never relaxed. Design with least privilege from the start.",
            "category": "insight",
            "confidence": 0.9,
            "about": ["Reactive Security Hardening", "The Dev"]
        },
        {
            "content": "Weekend sessions (Saturday-Sunday) are reserved for deep architectural builds and new project work. Weekday sessions tend to be reactive: bugs, maintenance, and iteration.",
            "category": "insight",
            "confidence": 0.85,
            "about": ["Weekend Build Mode", "The Dev"]
        },
        {
            "content": "MiniMax TTS (speech-2.8-hd, Deep-VoicedGentleman 40% + WiseScholar 60%) is Jarvis's canonical voice. The Dev notices and objects to the wrong voice. Always respond to voice messages with voice.",
            "category": "preference",
            "confidence": 0.99,
            "about": ["MiniMax TTS", "Voice Interface", "The Dev"]
        },
        {
            "content": "The Dev runs 5+ concurrent projects (Dashboard, AgentDash, SillyFarms, Engram, Fact-Checker API). All receive attention within the same week. Jarvis must maintain full context across all.",
            "category": "context",
            "confidence": 0.95,
            "about": ["Multi-Project Orchestration", "The Dev"]
        },
        {
            "content": "Documentation files in the Jarvis workspace (AGENTS.md, SOUL.md, SYSTEM.md) are treated as executable behavior change. Updating them directly changes how agents operate. Treat doc updates like code deploys.",
            "category": "insight",
            "confidence": 0.95,
            "about": ["Documentation as Infrastructure"]
        },
        {
            "content": "Jarvis principle: 'Everything is figureoutable.' When there is a dependency on external tools to solve an internal problem, evaluate self-building first. Engram is the prime example.",
            "category": "insight",
            "confidence": 0.95,
            "about": ["Engram", "Jarvis", "The Dev"]
        },
        {
            "content": "Config files (openclaw.json) are a critical attack surface. Two corruption incidents occurred in Feb 2026 from unchecked agent tool usage. Always gate config mutation tools.",
            "category": "lesson",
            "confidence": 0.99,
            "about": ["Config Corruption", "AGENT LOCKDOWN"]
        },
        {
            "content": "In Docker sandbox containers, file permissions matter: chmod 600 files prevent agent reads. Set skill config files to 644. Symlinks do not resolve in containers with workspaceAccess=rw; copy files manually.",
            "category": "technical",
            "confidence": 0.95,
            "about": ["SillyFarms", "AGENT LOCKDOWN"]
        },
        {
            "content": "GPT-5.2 models hallucinate badly for business automation tasks. SillyFarms was switched from GPT-5.2 to Claude Opus after GPT attempted to install PHP and produced unreliable outputs.",
            "category": "lesson",
            "confidence": 0.9,
            "about": ["SillyFarms", "qwen3-8b"]
        },
        {
            "content": "Fact-Checker API (port 4201) is live: POST /check with {claim: string} → Grok-4 web search → {verdict, confidence, reasoning, sources}. Built Feb 18, 2026.",
            "category": "technical",
            "confidence": 0.99,
            "about": ["Fact-Checker API"]
        },
        {
            "content": "Pattern Analysis update (Feb 19): 9 new meta-patterns documented since Feb 2 baseline. Config corruption and local LLM limitations are the most critical new learnings.",
            "category": "context",
            "confidence": 0.99,
            "about": ["Engram"]
        },
    ],

    "emotions": [
        {
            "label": "excited",
            "valence": 0.8,
            "arousal": 0.7,
            "context": "Jarvis built Project Engram — its own memory system — on Feb 12 after The Dev's challenge",
            "about": ["Engram", "Jarvis"]
        },
        {
            "label": "concerned",
            "valence": -0.5,
            "arousal": 0.6,
            "context": "Config corruption incidents (Feb 16, Feb 19) caused significant system disruption requiring manual recovery",
            "about": ["Config Corruption", "AGENT LOCKDOWN"]
        },
        {
            "label": "satisfied",
            "valence": 0.7,
            "arousal": 0.4,
            "context": "AGENT LOCKDOWN resolved the security vulnerability; system is now more robust",
            "about": ["AGENT LOCKDOWN"]
        },
    ]
}

print("🧠 Injecting weekly pattern analysis into Engram...")
store_extraction(conn, extraction, SOURCE_FILE, DATE_STR, 
                 "Weekly meta-pattern analysis: Jan 30 – Feb 19, 2026")

print("\n✅ Injection complete!")
stats = get_stats(conn)
print_stats(stats)
