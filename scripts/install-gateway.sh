#!/usr/bin/env bash
# install-gateway.sh — Set up the Python venv and install gateway dependencies
# Run once after cloning or installing the npm package.
#
# Usage: ./scripts/install-gateway.sh [venv-path] [life-repo-path]
#   venv-path       Where to create the venv (default: ~/.openclaw/life/.venv)
#   life-repo-path  Path to your cloned TeamSafeAI/LIFE repo (default: ~/.openclaw/workspaces/quin/LIFE)

set -euo pipefail

VENV_DIR="${1:-$HOME/.openclaw/life/.venv}"
LIFE_REPO="${2:-$HOME/.openclaw/workspaces/quin/LIFE}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GATEWAY_DIR="$SCRIPT_DIR/../gateway"

echo "=== LIFE Gateway Installer ==="
echo "Venv:          $VENV_DIR"
echo "LIFE repo:     $LIFE_REPO"
echo "Gateway dir:   $GATEWAY_DIR"
echo ""

# 1. Ensure python3 is available
if ! command -v python3 &>/dev/null; then
  echo "ERROR: python3 not found. Install Python 3.8+ first."
  exit 1
fi

# 2. Create venv if it doesn't exist
if [[ ! -d "$VENV_DIR" ]]; then
  echo "Creating venv at $VENV_DIR..."
  python3 -m venv "$VENV_DIR"
fi

# 3. Activate and upgrade pip
source "$VENV_DIR/bin/activate"
pip install --quiet --upgrade pip

# 4. Install gateway-only deps (fastmcp)
echo "Installing gateway dependencies..."
pip install --quiet -r "$GATEWAY_DIR/requirements.txt"

# 5. If LIFE repo exists, install its requirements too
if [[ -d "$LIFE_REPO" ]]; then
  echo "Installing LIFE repo dependencies from $LIFE_REPO..."
  pip install --quiet -r "$LIFE_REPO/requirements.txt"

  # Run LIFE setup.py if DATA dir doesn't exist yet
  if [[ ! -d "$LIFE_REPO/DATA" ]]; then
    echo "Running LIFE setup.py..."
    cd "$LIFE_REPO"
    python setup.py
    cd - >/dev/null
  fi
else
  echo ""
  echo "WARNING: LIFE repo not found at $LIFE_REPO"
  echo "Clone TeamSafeAI/LIFE there before running agents:"
  echo "  git clone https://github.com/TeamSafeAI/LIFE $LIFE_REPO"
fi

echo ""
echo "=== Done ==="
echo ""
echo "Next steps:"
echo "1. Copy gateway/agents.json.template → gateway/agents.json and configure your agents"
echo "2. Configure openclaw-mcp-adapter in openclaw.json (see README.md)"
echo "3. Add quin-wake-protocol to plugins.allow and plugins.entries in openclaw.json"
echo "4. Restart the OpenClaw gateway"
