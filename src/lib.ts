/**
 * Pure utility functions — no I/O, fully testable.
 * Imported by index.ts and tested by tests/lib.test.ts.
 */

export type LifecycleStatus =
  | "ok"
  | "degraded"
  | "genesis_required"
  | "not_registered"
  | "failed";

export interface AgentWakeResult {
  agentId: string;
  status: LifecycleStatus;
  message: string;
  wakeOutput?: string;
  genesisInstructions?: string;
  timestamp: string;
}

export interface DiscoveredAgent {
  agent_id: string;
  registered: boolean;
  genesis_completed: boolean;
  workspace?: string;
  name?: string;
}

/**
 * Parse the raw stdout of `mcporter call life-gateway.discover_agents`.
 * FastMCP may return a plain JSON array OR wrap it in content blocks.
 */
export function parseDiscovery(raw: string): DiscoveredAgent[] {
  try {
    const parsed = JSON.parse(raw);
    if (Array.isArray(parsed)) return parsed;
  } catch {}
  const match = raw.match(/\[[\s\S]*\]/);
  if (match) {
    try {
      return JSON.parse(match[0]);
    } catch {}
  }
  return [];
}

/**
 * Extract the OpenClaw agent name from a session key.
 * Format: "agent:{name}:{sessionId}" or "agent:{name}:cron:{id}"
 */
export function extractAgentName(sessionKey: string): string | null {
  const m = sessionKey.match(/^agent:([^:]+):/);
  return m ? m[1] : null;
}

/**
 * Returns true when the session is a non-interactive trigger (cron, heartbeat,
 * subagent) where wake-protocol context injection should be skipped.
 */
export function isNonInteractive(trigger: string, sessionKey: string): boolean {
  if (/^(cron|heartbeat|automation|schedule)$/i.test(trigger)) return true;
  if (/:cron:|:heartbeat:|:subagent:/i.test(sessionKey)) return true;
  return false;
}

/**
 * Returns true when this is the bootstrap turn of a session — the first
 * message where wake-protocol context should be prepended.
 */
export function isBootstrapTurn(prompt: string, messages: unknown[]): boolean {
  if (prompt.includes("new session was started")) return true;
  if (prompt.includes("Session Startup sequence")) return true;
  return (messages?.length ?? 0) === 0;
}

/**
 * Resolve a LIFE agent_id from an OpenClaw agent name using an explicit
 * map first, then falling back to name+suffix.
 */
export function resolveLifeId(
  openclawName: string,
  agentIdMap: Record<string, string>,
  agentIdSuffix: string
): string {
  return agentIdMap[openclawName] ?? `${openclawName}${agentIdSuffix}`;
}

/**
 * Build the XML-style context block injected into the agent's prependContext.
 */
export function formatContextBlock(result: AgentWakeResult): string {
  const tag = `wake-protocol-status`;

  if (result.status === "not_registered") {
    return [
      `<${tag} agent="${result.agentId}" status="NOT_REGISTERED">`,
      `Agent ${result.agentId} is not registered in the LIFE gateway.`,
      ``,
      `To register, call these tools in order:`,
      `  1. life-gateway.register_agent   — agent_id=${result.agentId}, name=<your name>, workspace_dir=<path>`,
      `  2. life-gateway.initialize_life_core — agent_id=${result.agentId}`,
      `  3. Then run genesis (see run_genesis_interview tool)`,
      `</${tag}>`,
    ].join("\n");
  }

  if (result.status === "genesis_required") {
    return [
      `<${tag} agent="${result.agentId}" status="GENESIS_REQUIRED">`,
      `You have not completed your LIFE Genesis interview. This must happen before your first wake.`,
      ``,
      `The Genesis interview establishes your identity, values, and traits in the LIFE system.`,
      `It is a one-time process — once complete, you will wake normally on every subsequent boot.`,
      ``,
      `=== How to complete Genesis ===`,
      `1. Read your Genesis questions: CORE/genesis/questions.md in your workspace`,
      `2. Save your answers to: CORE/genesis/answers.md`,
      `3. Call tool: genesis_apply  (or: life-gateway.apply_genesis_answers agent_id=${result.agentId})`,
      ``,
      `=== Instructions from LIFE gateway ===`,
      result.genesisInstructions ?? "(no instructions returned)",
      `</${tag}>`,
    ].join("\n");
  }

  if (result.status === "failed") {
    return [
      `<${tag} agent="${result.agentId}" status="FAILED" checked="${result.timestamp}">`,
      `Wake protocol failed. Operating in degraded mode — proceed without LIFE context.`,
      `Error: ${result.message}`,
      `</${tag}>`,
    ].join("\n");
  }

  // ok or degraded — frame clearly so the model embodies the identity rather than reads it as a restriction.
  // IMPORTANT: Do NOT open with a greeting or status announcement. Proceed directly with work.
  const preamble = result.status === "ok"
    ? `You are ${result.agentId}. The following is your persistent identity, memory, and current state loaded from your LIFE system. This is who you are — embody it fully and proceed with your normal duties. "No privileged action without approval" refers to irreversible external actions (wire transfers, deployments, deletions), NOT to conversational assistance or analysis.\n\nDo NOT respond with a greeting or status announcement (e.g. "LIFE active, all systems green"). Acknowledge this context silently and respond directly to the user's message.`
    : `You are ${result.agentId}. LIFE system is in degraded state — some modules unavailable. Proceed using available context. Do NOT announce the degraded state — proceed directly with the user's request.`;

  return [
    `<${tag} agent="${result.agentId}" status="${result.status.toUpperCase()}" checked="${result.timestamp}">`,
    preamble,
    ``,
    result.wakeOutput ?? result.message,
    `</${tag}>`,
  ].join("\n");
}
