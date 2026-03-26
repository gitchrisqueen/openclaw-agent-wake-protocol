#!/usr/bin/env bash
# onboard-agent.sh — Register, initialize, and schedule Genesis interview for a new LIFE agent
# Run this once per new agent after the gateway is running.
#
# Usage: ./scripts/onboard-agent.sh <agent-name> [workspace-dir]
#   agent-name     Short name used in OpenClaw (e.g. "finance", "platform")
#   workspace-dir  Path to the agent's shared workspace (default: auto-detected)
#
# The script will:
#   1. Detect the agent_id from IDENTITY.md if present
#   2. Register the agent in the LIFE gateway DB
#   3. Initialize the LIFE core (DATA dirs, traits.db, genesis questions)
#   4. Apply Genesis answers if answers.md already exists, else print interview instructions

set -euo pipefail

AGENT_NAME="${1:-}"
if [[ -z "$AGENT_NAME" ]]; then
  echo "Usage: $0 <agent-name> [workspace-dir]"
  echo "  Examples: $0 finance"
  echo "            $0 platform /path/to/workspace"
  exit 1
fi

DEFAULT_SHARED_DIR="$HOME/.openclaw/workspaces/quin/quin-workflows/agents/shared/$AGENT_NAME"
SHARED_DIR="${2:-$DEFAULT_SHARED_DIR}"
IDENTITY_PATH="$SHARED_DIR/IDENTITY.md"
ANSWERS_PATH="$SHARED_DIR/CORE/genesis/answers.md"

# Derive agent_id: read from IDENTITY.md or fall back to name
AGENT_ID=""
if [[ -f "$IDENTITY_PATH" ]]; then
  AGENT_ID=$(sed -n 's/.*\*\*Agent ID:\*\*[[:space:]]*//p' "$IDENTITY_PATH" | head -n1 | tr -d '\r' | xargs 2>/dev/null || true)
fi
AGENT_ID="${AGENT_ID:-$AGENT_NAME}"

echo "=== Onboarding LIFE Agent ==="
echo "  OpenClaw name: $AGENT_NAME"
echo "  LIFE agent_id: $AGENT_ID"
echo "  Workspace:     $SHARED_DIR"
echo ""

if [[ ! -d "$SHARED_DIR" ]]; then
  echo "ERROR: Workspace directory not found: $SHARED_DIR"
  echo "Ensure Antfarm or your workflow provisioning has created this directory first."
  exit 1
fi

# Helper: call mcporter and show output
mcall() {
  local server_tool="$1"; shift
  echo "  mcporter call $server_tool $*"
  mcporter call "$server_tool" "$@" 2>&1 || echo "  (command failed — check gateway logs)"
  echo ""
}

echo "Step 1: Registering agent in LIFE gateway DB..."
mcall "life-gateway.register_agent" \
  "agent_id=$AGENT_ID" \
  "name=$AGENT_NAME" \
  "workspace_dir=$SHARED_DIR"

echo "Step 2: Initializing LIFE core (DATA dirs, traits DB, genesis questions)..."
mcall "life-gateway.initialize_life_core" "agent_id=$AGENT_ID"

if [[ -f "$ANSWERS_PATH" ]]; then
  echo "Step 3: Genesis answers found — applying..."
  mcall "life-gateway.apply_genesis_answers" \
    "agent_id=$AGENT_ID" \
    "answers_path=$ANSWERS_PATH"
  echo "Step 4: Running wake protocol..."
  mcall "life-gateway.wake" "agent_id=$AGENT_ID"
  echo "=== Agent $AGENT_ID is live ==="
else
  echo "Step 3: Genesis interview required."
  echo ""
  echo "  The agent must complete the Genesis interview before it can wake."
  echo "  Questions are at: $SHARED_DIR/CORE/genesis/questions.md"
  echo ""
  echo "  To run the interview via the main agent:"
  echo "    openclaw agent --agent main --message \\"
  echo "      'Complete the LIFE Genesis interview for $AGENT_ID. Read CORE/genesis/questions.md"
  echo "       in $SHARED_DIR and save answers to $ANSWERS_PATH based on the agent persona.'"
  echo ""
  echo "  After answers.md is saved, run:"
  echo "    mcporter call life-gateway.apply_genesis_answers agent_id=$AGENT_ID answers_path=$ANSWERS_PATH"
  echo "    mcporter call life-gateway.wake agent_id=$AGENT_ID"
fi
