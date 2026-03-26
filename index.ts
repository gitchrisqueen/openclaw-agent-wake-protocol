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
import {
  type LifecycleStatus,
  type AgentWakeResult,
  type DiscoveredAgent,
  parseDiscovery,
  extractAgentName,
  isNonInteractive,
  resolveLifeId,
  formatContextBlock,
} from "./src/lib.js";

const execAsync = promisify(exec);

// ── Types ──────────────────────────────────────────────────────────────────

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

/**
 * Sessions that have already received the bootstrap wake injection.
 * Prevents re-injection on tool sub-calls and follow-up turns within
 * the same session. Entries expire after 30 minutes so long-lived
 * sessions still get a refresh if the gateway restarts.
 */
const bootstrappedSessions = new Map<string, number>(); // sessionKey → injected-at ms
const BOOTSTRAP_TTL_MS = 30 * 60 * 1000; // 30 minutes

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

// ── Per-agent lifecycle handler ────────────────────────────────────────────

async function runAgentLifecycle(
  lifeAgentId: string,
  discovered: DiscoveredAgent[],
  timeoutMs: number,
  logger: any
): Promise<void> {
  const timestamp = new Date().toISOString();
  const found = discovered.find((a) => a.agent_id === lifeAgentId);

  // ── Not in shared-path discovery — try direct wake (e.g. quin-ea-v1) ─────
  if (!found) {
    logger.info(
      `[agent-wake] ${lifeAgentId}: not in discover_agents — trying direct wake`
    );
    const directWake = await mcporter(
      ["call", "life-gateway.wake", `agent_id=${lifeAgentId}`],
      timeoutMs
    );
    if (
      directWake.startsWith("ERROR") ||
      directWake.toLowerCase().includes("agent not found")
    ) {
      logger.warn(
        `[agent-wake] ${lifeAgentId}: not found in LIFE gateway — will prompt on boot`
      );
      wakeResults.set(lifeAgentId, {
        agentId: lifeAgentId,
        status: "not_registered",
        message: `Agent ${lifeAgentId} is not in the LIFE gateway registry.`,
        timestamp,
      });
    } else {
      const degraded =
        directWake.toLowerCase().includes("missing") ||
        directWake.toLowerCase().includes("degraded") ||
        directWake.toLowerCase().includes("error");
      wakeResults.set(lifeAgentId, {
        agentId: lifeAgentId,
        status: degraded ? "degraded" : "ok",
        message: `Wake complete for ${lifeAgentId}`,
        wakeOutput: directWake,
        timestamp,
      });
      logger.info(
        `[agent-wake] ${lifeAgentId}: ${degraded ? "DEGRADED" : "OK"} (direct wake)`
      );
    }
    return;
  }

  // ── Discovered but not yet registered ────────────────────────────────────
  if (!found.registered) {
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
    id: "openclaw-agent-wake-protocol",

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

    // Genesis-required: always re-inject until the agent completes the interview
    if (result.status === "genesis_required") {
      api.logger.info(
        `[agent-wake] Injecting genesis_required context for ${lifeId} (session: ${sessionKey})`
      );
      return { prependContext: formatContextBlock(result) };
    }

    // For ok/degraded/failed: bootstrappedSessions is the SOLE gate.
    // OpenClaw passes messages=[] on every hook call (each turn is stateless
    // from the hook's perspective), so checking messages.length is unreliable.
    // Inject once per session; re-inject only after TTL expires.
    const lastInjected = bootstrappedSessions.get(sessionKey) ?? 0;
    const needsBootstrap = Date.now() - lastInjected > BOOTSTRAP_TTL_MS;
    if (!needsBootstrap) return;

    bootstrappedSessions.set(sessionKey, Date.now());

    const isRefresh = lastInjected > 0;
    api.logger.info(
      `[agent-wake] Injecting ${result.status} context for ${lifeId} (session: ${sessionKey}${isRefresh ? ", ttl-refresh" : ""})`
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
