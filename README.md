# openclaw-quin-wake-protocol

An [OpenClaw](https://openclaw.ai) extension that manages the full **LIFE gateway lifecycle** for all agents in your multi-agent system.

Built on top of the [TeamSafeAI/LIFE](https://github.com/TeamSafeAI/LIFE) persistence framework and the custom multi-agent gateway wrapper included in this package.

---

## What It Does

At gateway startup, the extension automatically:

1. **Discovers** all agents registered with the LIFE gateway
2. For agents **not yet registered** → injects registration instructions into their first session
3. For agents with **Genesis pending** → injects the Genesis interview into their first session so they self-complete it (no human intervention needed)
4. For **ready agents** → runs the full wake protocol (`drives`, `heart`, `working`, `semantic`, `history`) and injects the result as context

Each agent's LIFE state is injected into its bootstrap session as a `<wake-protocol-status>` block — the LLM sees system health without needing to call tools itself.

---

## Architecture

```
OpenClaw Gateway
  └── quin-wake-protocol (this extension)
        ├── before_agent_start hook  → injects <wake-protocol-status> on first turn
        ├── registerService          → runs full lifecycle at gateway boot
        └── tools:
              wake_protocol_status   → query current status for any agent
              wake_protocol_run      → re-run lifecycle for a specific agent
              genesis_apply          → mark Genesis complete after answers.md is saved

  └── openclaw-mcp-adapter (peer dependency)
        └── life-gateway MCP server (gateway/server.py — bundled here)
              └── TeamSafeAI/LIFE per-agent installation
                    ├── CORE/ (16 modules: drives, heart, semantic, working, ...)
                    └── DATA/ (databases, memories, journals)
```

### Runtime Dependencies

| Dependency | Purpose |
|------------|---------|
| [mcporter](https://github.com/openclaw-ai/mcporter) | CLI for calling MCP tools from the gateway process |
| [openclaw-mcp-adapter](https://www.npmjs.com/package/openclaw-mcp-adapter) | Registers the life-gateway as an MCP server in OpenClaw |
| [TeamSafeAI/LIFE](https://github.com/TeamSafeAI/LIFE) | Per-agent persistence modules (Python) — clone once per agent |
| Python 3.8+ + venv | Runs the gateway server and LIFE modules |

---

## Installation

### 1. Install the npm package

```bash
npm install openclaw-quin-wake-protocol
# or
pnpm add openclaw-quin-wake-protocol
```

### 2. Set up the Python environment

Run the included setup script once:

```bash
./node_modules/openclaw-quin-wake-protocol/scripts/install-gateway.sh \
  ~/.openclaw/life/.venv \
  ~/.openclaw/workspaces/quin/LIFE
```

This creates a Python venv, installs `fastmcp`, and runs `setup.py` from your LIFE repo.

If you haven't cloned LIFE yet:

```bash
git clone https://github.com/TeamSafeAI/LIFE ~/.openclaw/workspaces/quin/LIFE
```

### 3. Copy the gateway server to your workspace

```bash
cp -n node_modules/openclaw-quin-wake-protocol/gateway/server.py \
      ~/.openclaw/workspaces/quin/life-gateway/server.py

cp -n node_modules/openclaw-quin-wake-protocol/gateway/genesis-questions.md \
      ~/.openclaw/workspaces/quin/life-gateway/genesis-questions.md
```

> Use `-n` (no-clobber) to avoid overwriting a customized server.

### 4. Create your agents registry

```bash
cp node_modules/openclaw-quin-wake-protocol/gateway/agents.json.template \
   ~/.openclaw/workspaces/quin/life-gateway/agents.json
# Edit agents.json and add your agent entries
```

### 5. Configure `openclaw-mcp-adapter`

In your `openclaw.json`, add the life-gateway server to `openclaw-mcp-adapter`'s config:

```json
{
  "plugins": {
    "allow": ["openclaw-mcp-adapter", "quin-wake-protocol"],
    "entries": {
      "openclaw-mcp-adapter": {
        "enabled": true,
        "config": {
          "servers": [
            {
              "name": "life-gateway",
              "type": "stdio",
              "command": "/home/YOUR_USER/.openclaw/life/.venv/bin/python",
              "args": ["/home/YOUR_USER/.openclaw/workspaces/quin/life-gateway/server.py"],
              "env": {
                "LIFE_GATEWAY_REGISTRY": "/home/YOUR_USER/.openclaw/workspaces/quin/life-gateway/agents.json",
                "LIFE_CALL_TIMEOUT_SEC": "45"
              }
            }
          ]
        }
      },
      "quin-wake-protocol": {
        "enabled": true,
        "config": {
          "agentIdMap": {
            "main": "quin-ea-v1",
            "quin-finance": "quin-finance-v1",
            "quin-platform": "quin-platform-v1",
            "quin-research": "quin-research-v1"
          }
        }
      }
    }
  }
}
```

### 6. Restart the gateway

```bash
systemctl --user restart openclaw-gateway.service
```

---

## Onboarding New Agents

For each new C-level agent, run the included script:

```bash
./node_modules/openclaw-quin-wake-protocol/scripts/onboard-agent.sh finance
./node_modules/openclaw-quin-wake-protocol/scripts/onboard-agent.sh platform
./node_modules/openclaw-quin-wake-protocol/scripts/onboard-agent.sh research
```

This handles: register → initialize LIFE core → detect Genesis status → print interview instructions if needed.

### The Genesis Interview

If an agent has not completed Genesis, it will receive interview instructions injected into its very first session. The agent reads `CORE/genesis/questions.md` from its own workspace, writes answers to `CORE/genesis/answers.md`, then calls the `genesis_apply` tool. On the next boot the full wake protocol runs automatically.

---

## Configuration Options

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `agentIdMap` | `Record<string, string>` | `{ "main": "quin-ea-v1" }` | Maps OpenClaw agent names to LIFE agent IDs |
| `sessionPrefix` | `string` | `"agent:"` | Only inject for sessions with this key prefix |
| `commandTimeoutMs` | `number` | `20000` | Per-command timeout in milliseconds |

---

## Tools Registered

| Tool | Description |
|------|-------------|
| `wake_protocol_status` | Query current lifecycle state for all (or one) agent |
| `wake_protocol_run` | Re-run lifecycle for a specific agent on demand |
| `genesis_apply` | Mark Genesis complete after agent saves `answers.md` |

---

## LIFE Gateway Wrapper

The `gateway/server.py` included in this package is a **custom FastMCP wrapper** that extends the base [TeamSafeAI/LIFE](https://github.com/TeamSafeAI/LIFE) architecture to support **multiple agents through a single MCP endpoint**.

The base LIFE repo is designed for one agent per installation. This wrapper adds:
- Central `agents.db` SQLite registry for all agents
- Tool-based lifecycle management (`register_agent`, `initialize_life_core`, `run_genesis_interview`, `apply_genesis_answers`)
- Auto-discovery of C-level agents from shared workspace paths
- Routing all LIFE module calls by `agent_id`

---

## License

MIT — Christopher Queen / [gitchrisqueen](https://github.com/gitchrisqueen)
