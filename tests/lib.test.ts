import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import {
  parseDiscovery,
  extractAgentName,
  isNonInteractive,
  isBootstrapTurn,
  resolveLifeId,
  formatContextBlock,
  type AgentWakeResult,
} from "../src/lib.js";

const __dirname = dirname(fileURLToPath(import.meta.url));
const root = resolve(__dirname, "..");

// ── parseDiscovery ─────────────────────────────────────────────────────────

describe("parseDiscovery", () => {
  it("parses a plain JSON array", () => {
    const raw = JSON.stringify([
      { agent_id: "quin-ea-v1", registered: true, genesis_completed: true },
    ]);
    const result = parseDiscovery(raw);
    expect(result).toHaveLength(1);
    expect(result[0].agent_id).toBe("quin-ea-v1");
  });

  it("extracts a JSON array embedded in text output", () => {
    const raw = `Some mcporter preamble\n[{"agent_id":"finance-v1","registered":false,"genesis_completed":false}]\ntrailing text`;
    const result = parseDiscovery(raw);
    expect(result).toHaveLength(1);
    expect(result[0].registered).toBe(false);
  });

  it("returns an empty array for ERROR output", () => {
    expect(parseDiscovery("ERROR: connection refused")).toEqual([]);
  });

  it("returns an empty array for garbage input", () => {
    expect(parseDiscovery("not json at all")).toEqual([]);
  });
});

// ── extractAgentName ───────────────────────────────────────────────────────

describe("extractAgentName", () => {
  it("extracts name from a standard session key", () => {
    expect(extractAgentName("agent:main:abc123")).toBe("main");
  });

  it("extracts name from a cron session key", () => {
    expect(extractAgentName("agent:quin-finance:cron:daily")).toBe("quin-finance");
  });

  it("returns null for non-agent session keys", () => {
    expect(extractAgentName("system:heartbeat")).toBeNull();
    expect(extractAgentName("")).toBeNull();
  });
});

// ── isNonInteractive ───────────────────────────────────────────────────────

describe("isNonInteractive", () => {
  it("detects cron trigger type", () => {
    expect(isNonInteractive("cron", "agent:main:abc")).toBe(true);
  });

  it("detects heartbeat trigger type (case-insensitive)", () => {
    expect(isNonInteractive("Heartbeat", "agent:main:abc")).toBe(true);
  });

  it("detects :cron: in session key", () => {
    expect(isNonInteractive("message", "agent:main:cron:daily")).toBe(true);
  });

  it("returns false for normal interactive sessions", () => {
    expect(isNonInteractive("message", "agent:main:abc123")).toBe(false);
  });
});

// ── isBootstrapTurn ────────────────────────────────────────────────────────

describe("isBootstrapTurn", () => {
  it("detects 'new session was started' phrase", () => {
    expect(isBootstrapTurn("A new session was started for you.", [])).toBe(true);
  });

  it("detects 'Session Startup sequence' phrase", () => {
    expect(isBootstrapTurn("Session Startup sequence begins", [{ role: "user" }])).toBe(true);
  });

  it("returns true when messages array is empty (first turn)", () => {
    expect(isBootstrapTurn("Hello", [])).toBe(true);
  });

  it("returns false for mid-session prompts with history", () => {
    expect(isBootstrapTurn("What is the weather?", [{ role: "user" }, { role: "assistant" }])).toBe(false);
  });
});

// ── resolveLifeId ──────────────────────────────────────────────────────────

describe("resolveLifeId", () => {
  const map = { main: "quin-ea-v1", finance: "quin-finance-v1" };

  it("returns the mapped id when present", () => {
    expect(resolveLifeId("main", map, "-v1")).toBe("quin-ea-v1");
  });

  it("falls back to name+suffix for unmapped agents", () => {
    expect(resolveLifeId("research", map, "-v1")).toBe("research-v1");
  });

  it("respects a custom suffix", () => {
    expect(resolveLifeId("ops", {}, "-agent")).toBe("ops-agent");
  });
});

// ── formatContextBlock ─────────────────────────────────────────────────────

describe("formatContextBlock", () => {
  const base: AgentWakeResult = {
    agentId: "quin-ea-v1",
    status: "ok",
    message: "Agent is awake",
    wakeOutput: "LIFE wake complete",
    timestamp: "2026-01-01T00:00:00.000Z",
  };

  it("includes NOT_REGISTERED status and registration instructions", () => {
    const block = formatContextBlock({ ...base, status: "not_registered" });
    expect(block).toContain('status="NOT_REGISTERED"');
    expect(block).toContain("register_agent");
    expect(block).toContain("initialize_life_core");
  });

  it("includes GENESIS_REQUIRED status and interview steps", () => {
    const block = formatContextBlock({
      ...base,
      status: "genesis_required",
      genesisInstructions: "Answer 80 questions",
    });
    expect(block).toContain('status="GENESIS_REQUIRED"');
    expect(block).toContain("genesis_apply");
    expect(block).toContain("Answer 80 questions");
  });

  it("includes FAILED status and error message", () => {
    const block = formatContextBlock({ ...base, status: "failed", message: "timeout" });
    expect(block).toContain('status="FAILED"');
    expect(block).toContain("timeout");
  });

  it("includes OK status and wake output", () => {
    const block = formatContextBlock(base);
    expect(block).toContain('status="OK"');
    expect(block).toContain("LIFE wake complete");
  });

  it("ok block includes preamble framing the identity", () => {
    const block = formatContextBlock(base);
    expect(block).toContain("embody it fully");
    expect(block).toContain("You are");
  });

  it("degraded block includes degraded preamble", () => {
    const block = formatContextBlock({ ...base, status: "degraded" });
    expect(block).toContain('status="DEGRADED"');
    expect(block).toContain("degraded state");
  });
});

// ── Manifest & package integrity ───────────────────────────────────────────

describe("package integrity", () => {
  const pkg = JSON.parse(readFileSync(resolve(root, "package.json"), "utf8"));
  const manifest = JSON.parse(readFileSync(resolve(root, "openclaw.plugin.json"), "utf8"));

  it("plugin manifest has required fields", () => {
    expect(manifest.id).toBeTruthy();
    expect(manifest.name).toBeTruthy();
    expect(manifest.configSchema).toBeTruthy();
  });

  it("manifest id matches npm package name", () => {
    expect(manifest.id).toBe(pkg.name);
  });

  it("package.json files array includes src/ for runtime imports", () => {
    expect(pkg.files).toContain("src/");
  });

  it("package.json has a test script", () => {
    expect(pkg.scripts?.test).toBeTruthy();
  });
});
