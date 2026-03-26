/**
 * openclaw-agent-wake-protocol — OpenClaw Extension
 *
 * General-purpose LIFE gateway lifecycle manager for any OpenClaw multi-agent system.
 * Works with any agent names and any LIFE agent_id conventions — fully driven by config.
 *
 * Manages the full lifecycle for every agent in your system:
 *
 *   1. NOT REGISTERED  → Injects registration + init instructions into agent context
 *   2. GENESIS PENDING → Injects the Genesis interview into agent context so the agent
 *                        completes it on first boot (self-directed, no human needed)
 *   3. READY           → Runs the wake protocol and injects status into context
 *
 * At gateway startup a registered service discovers all agents and runs the appropriate
 * lifecycle step for each. On every bootstrap turn the hook injects the result so the
 * LLM sees system state without needing to call any tools itself.
 *
 * Runtime dependencies (not npm):
 *   - mcporter CLI in PATH
 *   - life-gateway MCP server running via openclaw-mcp-adapter
 *   - TeamSafeAI/LIFE cloned per-agent + Python venv set up (see scripts/install-gateway.sh)
 */

import { exec } from "node:child_process";
import { promisify } from "node:util";

const execAsync = promisify(exec);

// ── Types ──────────────────────────────────────────────────────────────────

type LifecycleStatus =
  | "ok"
  | "degraded"
  | "genesis_required"
  | "not_registered"
  | "failed";

interface AgentWakeResult {
  agentId: string;
  status: LifecycleStatus;
  /** Human-readable summary injected into context */
  message: string;
  /** Raw output from wake command (status=ok/degraded) */
  wakeOutput?: string;
  /** Instructions returned by run_genesis_interview (status=genesis_required) */
  genesisInstructions?: string;
  timestamp: string;
}

interface DiscoveredAgent {
  agent_id: string;
  registered: boolean;
  genesis_completed: boolean;
  workspace?: string;
  name?: string;
}

interface PluginConfig {
  /**
   * Map of OpenClaw agent name → LIFE agent_id.
   * Example: { "main": "my-agent-v1", "finance": "finance-agent-v1" }
   * When not provided for a given agent name, falls back to the agentIdSuffix convention.
   */
  agentIdMap?: Record<string, string>;
  /**
   * Suffix appended when deriving a LIFE agent_id from an OpenClaw agent name.
   * Default: "-v1"  →  agent name "finance" becomes "finance-v1"
   */
  agentIdSuffix?: string;
  /**
   * Only inject context for sessions matching this prefix.
   * Default: "agent:" (all agents). Set to "agent:main:" for main only.
   */
  sessionPrefix?: string;
  /** Timeout per mcporter command in ms. Default: 20000 */
  commandTimeoutMs?: number;
}

// ── Module state ───────────────────────────────────────────────────────────

/** Keyed by LIFE agent_id */
const wakeResults = new Map<string, AgentWakeResult>();

// ── Helpers ────────────────────────────────────────────────────────────────

async function mcporter(
  args: string[],
  timeoutMs: number
): Promise<string> {
  try {
    const { stdout, stderr } = await execAsync(
      `mcporter ${args.map((a) => (a.includes(" ") ? `"${a}"` : a)).join(" ")}`,
      { timeout: timeoutMs, encoding: "utf8" }
    );
    return (stdout.trim() || stderr.trim() || "(no output)").slice(0, 2000);
  } catch (err: any) {
    return `ERROR: ${err?.message ?? String(err)}`.slice(0, 400);
  }
}

/**
 * Parse the output of `mcporter call life-gateway.discover_agents`.
 * FastMCP returns a JSON array; mcporter may wrap it in content blocks.
 */
function parseDiscovery(raw: string): DiscoveredAgent[] {
  // Try raw JSON array first
  try {
    const parsed = JSON.parse(raw);
    if (Array.isArray(parsed)) return parsed;
  } catch {}
  // Try extracting JSON array embedded in text
  const match = raw.match(/\[[\s\S]*\]/);
  if (match) {
    try {
      return JSON.parse(match[0]);
    } catch {}
  }
  return [];
}

function extractAgentName(sessionKey: string): string | null {
  // sessionKey: "agent:{name}:{sessionId}" | "agent:{name}:cron:{id}"
  const m = sessionKey.match(/^agent:([^:]+):/);
  return m ? m[1] : null;
}

function isNonInteractive(trigger: string, sessionKey: string): boolean {
  if (/^(cron|heartbeat|automation|schedule)$/i.test(trigger)) return true;
  if (/:cron:|:heartbeat:|:subagent:/i.test(sessionKey)) return true;
  return false;
}

function isBootstrapTurn(prompt: string, messages: unknown[]): boolean {
  // The internal bootstrap-extra-files hook appends this phrase to the first message
  if (prompt.includes("new session was started")) return true;
  if (prompt.includes("Session Startup sequence")) return true;
  return (messages?.length ?? 0) === 0;
}

function formatContextBlock(result: AgentWakeResult): string {
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

  // ok or degraded
  return [
    `<${tag} agent="${result.agentId}" status="${result.status.toUpperCase()}" checked="${result.timestamp}">`,
    result.wakeOutput ?? result.message,
    `</${tag}>`,
  ].join("\n");
}

// ── Per-agent lifecycle handler ────────────────────────────────────────────

async function runAgentLifecycle(
  lifeAgentId: string,
  discovered: DiscoveredAgent[],
  timeoutMs: number,
  logger: any
): Promise<void> {
  const timestamp = new Date().toISOString();
  const found = discovered.find((a) => a.agent_id === lifeAgentId);

  // ── Not registered ────────────────────────────────────────────────────────
  if (!found || !found.registered) {
    logger.warn(
      `[agent-wake] ${lifeAgentId}: not registered in LIFE gateway — will prompt on boot`
    );
    wakeResults.set(lifeAgentId, {
      agentId: lifeAgentId,
      status: "not_registered",
      message: `Agent ${lifeAgentId} is not in the LIFE gateway registry.`,
      timestamp,
    });
    return;
  }

  // ── Genesis pending ───────────────────────────────────────────────────────
  if (!found.genesis_completed) {
    logger.info(
      `[agent-wake] ${lifeAgentId}: genesis pending — fetching interview instructions`
    );
    const genesisRaw = await mcporter(
      ["call", "life-gateway.run_genesis_interview", `agent_id=${lifeAgentId}`],
      timeoutMs
    );
    wakeResults.set(lifeAgentId, {
      agentId: lifeAgentId,
      status: "genesis_required",
      message: `Genesis interview not yet completed for ${lifeAgentId}.`,
      genesisInstructions: genesisRaw,
      timestamp,
    });
    return;
  }

  // ── Ready — run status then wake ─────────────────────────────────────────
  logger.info(`[agent-wake] ${lifeAgentId}: running wake protocol`);

  const wakeRaw = await mcporter(
    ["call", "life-gateway.wake", `agent_id=${lifeAgentId}`],
    timeoutMs
  );

  const failed = wakeRaw.startsWith("ERROR");
  const degraded =
    !failed &&
    (wakeRaw.toLowerCase().includes("missing") ||
      wakeRaw.toLowerCase().includes("degraded") ||
      wakeRaw.toLowerCase().includes("error"));

  wakeResults.set(lifeAgentId, {
    agentId: lifeAgentId,
    status: failed ? "failed" : degraded ? "degraded" : "ok",
    message: failed ? wakeRaw : `Wake complete for ${lifeAgentId}`,
    wakeOutput: wakeRaw,
    timestamp,
  });

  logger.info(
    `[agent-wake] ${lifeAgentId}: ${failed ? "FAILED" : degraded ? "DEGRADED" : "OK"}`
  );
}

// ── Plugin entry point ─────────────────────────────────────────────────────

export default function (api: any) {
  const cfg: PluginConfig = api.pluginConfig ?? {};
  const timeoutMs = cfg.commandTimeoutMs ?? 20000;
  const sessionPrefix = cfg.sessionPrefix ?? "agent:";

  const agentIdMap: Record<string, string> = cfg.agentIdMap ?? {};
  const agentIdSuffix = cfg.agentIdSuffix ?? "-v1";

  function resolveLifeId(openclawName: string): string {
    return agentIdMap[openclawName] ?? `${openclawName}${agentIdSuffix}`;
  }

  // ── Service: discover and wake all agents at gateway startup ──────────────
  api.registerService({
    id: "agent-wake-protocol",

    async start() {
      api.logger.info(
        "[agent-wake] Gateway started — discovering LIFE agents..."
      );

      const discoveryRaw = await mcporter(
        ["call", "life-gateway.discover_agents"],
        timeoutMs
      );

      if (discoveryRaw.startsWith("ERROR")) {
        api.logger.error(
          `[agent-wake] discover_agents failed: ${discoveryRaw}`
        );
        // Store a failed result for every known agent so hook can degrade gracefully
        for (const lifeId of Object.values(agentIdMap)) {
          wakeResults.set(lifeId, {
            agentId: lifeId,
            status: "failed",
            message: `LIFE gateway unreachable: ${discoveryRaw}`,
            timestamp: new Date().toISOString(),
          });
        }
        return;
      }

      const agents = parseDiscovery(discoveryRaw);
      api.logger.info(`[agent-wake] Discovered ${agents.length} LIFE agents`);

      // Run lifecycle for every agent in the id map + any extras discovered
      const allIds = new Set([
        ...Object.values(agentIdMap),
        ...agents.map((a) => a.agent_id),
      ]);

      await Promise.all(
        [...allIds].map((id) =>
          runAgentLifecycle(id, agents, timeoutMs, api.logger)
        )
      );

      api.logger.info("[agent-wake] Boot protocol complete");
    },

    stop() {
      api.logger.info("[agent-wake] Service stopping");
    },
  });

  // ── Hook: inject wake status on bootstrap turns ───────────────────────────
  api.on("before_agent_start", async (event: any, ctx: any) => {
    const sessionKey: string = ctx?.sessionKey ?? "";
    const trigger: string = ctx?.trigger ?? "";

    if (!sessionKey.startsWith(sessionPrefix)) return;
    if (isNonInteractive(trigger, sessionKey)) return;

    const agentName = extractAgentName(sessionKey);
    if (!agentName) return;

    const lifeId = resolveLifeId(agentName);
    const result = wakeResults.get(lifeId);
    if (!result) return;

    const prompt: string = event?.prompt ?? "";
    const messages: unknown[] = event?.messages ?? [];

    // Always inject on bootstrap; also re-inject on every turn if genesis still pending
    // (so the agent keeps trying until it completes the interview)
    const inject =
      isBootstrapTurn(prompt, messages) || result.status === "genesis_required";
    if (!inject) return;

    api.logger.info(
      `[agent-wake] Injecting ${result.status} context for ${lifeId} (session: ${sessionKey})`
    );

    return { prependContext: formatContextBlock(result) };
  });

  // ── Tool: query current wake status ──────────────────────────────────────
  api.registerTool({
    name: "wake_protocol_status",
    description:
      "Returns the current LIFE gateway wake status for all known agents, " +
      "including lifecycle state (ok, degraded, genesis_required, not_registered, failed).",
    parameters: {
      type: "object",
      properties: {
        agent_id: {
          type: "string",
          description: "Limit output to a specific LIFE agent_id (optional)",
        },
      },
      additionalProperties: false,
    },
    async execute(_id: string, params: { agent_id?: string }) {
      if (wakeResults.size === 0) {
        return {
          content: [
            { type: "text", text: "Wake protocol has not run yet (gateway still starting?)." },
          ],
          isError: false,
        };
      }
      const entries =
        params?.agent_id
          ? ([[params.agent_id, wakeResults.get(params.agent_id)]] as const)
          : ([...wakeResults.entries()] as const);

      const lines = entries
        .filter(([, v]) => v)
        .map(([id, r]) =>
          `${id}: ${r!.status.toUpperCase()} (${r!.timestamp})\n  ${r!.message}`
        );

      return {
        content: [{ type: "text", text: lines.join("\n\n") || "No results." }],
        isError: false,
      };
    },
  });

  // ── Tool: re-run lifecycle for a specific agent ───────────────────────────
  api.registerTool({
    name: "wake_protocol_run",
    description:
      "Re-run the LIFE wake or Genesis lifecycle check for a specific agent. " +
      "Use after completing Genesis or after fixing a module failure.",
    parameters: {
      type: "object",
      required: ["agent_id"],
      properties: {
        agent_id: {
          type: "string",
          description: "The LIFE agent_id to process (e.g. quin-ea-v1)",
        },
      },
      additionalProperties: false,
    },
    async execute(_id: string, params: { agent_id: string }) {
      const agentId = params?.agent_id;
      if (!agentId) {
        return { content: [{ type: "text", text: "agent_id is required" }], isError: true };
      }
      api.logger.info(`[agent-wake] Manual lifecycle run requested for ${agentId}`);
      const discoveryRaw = await mcporter(
        ["call", "life-gateway.discover_agents"],
        timeoutMs
      );
      const agents = parseDiscovery(discoveryRaw);
      await runAgentLifecycle(agentId, agents, timeoutMs, api.logger);
      const result = wakeResults.get(agentId);
      return {
        content: [
          {
            type: "text",
            text: result ? formatContextBlock(result) : `No result for ${agentId}`,
          },
        ],
        isError: false,
      };
    },
  });

  // ── Tool: mark Genesis complete after agent saves answers.md ─────────────
  api.registerTool({
    name: "genesis_apply",
    description:
      "Mark the LIFE Genesis interview as complete after the agent has saved answers.md. " +
      "Call this after writing your Genesis answers. The wake protocol will run automatically.",
    parameters: {
      type: "object",
      required: ["agent_id"],
      properties: {
        agent_id: {
          type: "string",
          description: "Your LIFE agent_id (e.g. quin-ea-v1)",
        },
        answers_path: {
          type: "string",
          description:
            "Absolute path to answers.md (optional — gateway infers from workspace_dir if omitted)",
        },
      },
      additionalProperties: false,
    },
    async execute(_id: string, params: { agent_id: string; answers_path?: string }) {
      const { agent_id, answers_path } = params ?? {};
      if (!agent_id) {
        return { content: [{ type: "text", text: "agent_id is required" }], isError: true };
      }

      api.logger.info(`[agent-wake] Applying Genesis answers for ${agent_id}`);

      const applyArgs = ["call", "life-gateway.apply_genesis_answers", `agent_id=${agent_id}`];
      if (answers_path) applyArgs.push(`answers_path=${answers_path}`);

      const applyRaw = await mcporter(applyArgs, timeoutMs);

      if (applyRaw.startsWith("ERROR") || applyRaw.toLowerCase().includes('"error"')) {
        return {
          content: [{ type: "text", text: `Genesis apply failed: ${applyRaw}` }],
          isError: true,
        };
      }

      // Now run the full wake since genesis is done
      api.logger.info(`[agent-wake] Genesis applied — running wake for ${agent_id}`);
      const discoveryRaw = await mcporter(
        ["call", "life-gateway.discover_agents"],
        timeoutMs
      );
      const agents = parseDiscovery(discoveryRaw);
      await runAgentLifecycle(agent_id, agents, timeoutMs, api.logger);

      const result = wakeResults.get(agent_id);
      return {
        content: [
          {
            type: "text",
            text: [
              `Genesis complete for ${agent_id}.`,
              `Apply result: ${applyRaw}`,
              ``,
              result ? formatContextBlock(result) : "",
            ].join("\n"),
          },
        ],
        isError: false,
      };
    },
  });
}
