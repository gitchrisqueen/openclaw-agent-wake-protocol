#!/usr/bin/env python3
"""
Unified LIFE Gateway MCP Server

Single FastMCP endpoint that serves Quin main + all C-level agents.
Handles agent lifecycle (discovery, registration, Genesis interview, wake)
via tool calls. Routes LIFE module calls by agent_id via subprocess.
"""

import json
import os
import select
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastmcp import FastMCP

sys.stdout.reconfigure(encoding="utf-8")

BASE_DIR = Path(__file__).resolve().parent
REGISTRY_PATH = Path(os.getenv("LIFE_GATEWAY_REGISTRY", str(BASE_DIR / "agents.json")))
PYTHON_BIN = os.getenv("LIFE_PYTHON_BIN", sys.executable)
CALL_TIMEOUT_SEC = int(os.getenv("LIFE_CALL_TIMEOUT_SEC", "45"))

# Central SQLite DB for all agent registrations (C-level + Quin main)
LIFE_DB_PATH = BASE_DIR / "agents.db"

# Genesis questions (pulled once, shared across agents)
GENESIS_QUESTIONS_PATH = BASE_DIR / "genesis-questions.md"

# Shared agents directory (where Antfarm provisions C-level personas)
SHARED_AGENTS_PATH = (
    Path.home()
    / ".openclaw"
    / "workspaces"
    / "quin"
    / "quin-workflows"
    / "agents"
    / "shared"
)

# FastMCP instance
mcp = FastMCP("life-gateway")

ALLOWED_MODULE_TOOLS = {
    "drives": {"start", "snapshot"},
    "heart": {"feel", "search", "check", "wall"},
    "semantic": {"store", "search", "expand"},
    "working": {"create", "add", "view", "see"},
    "journal": {"write", "read"},
    "state": {"want", "horizon"},
    "history": {"recall", "discover"},
    "patterns": {"learn", "recall", "forget"},
}

# Full wake sequence — restores drives, relationships, momentum, memory,
# long-term arc, distilled lessons, active goals, and narrative continuity.
WAKE_SEQUENCE = [
    ("drives", "start", {}),
    ("heart", "search", {}),
    ("working", "view", {}),
    ("semantic", "search", {}),
    ("history", "discover", {"section": "self"}),
    ("patterns", "recall", {}),                  # distilled lessons learned
    ("state", "want", {}),                        # active goals and horizons
    ("journal", "read", {"limit": 3}),            # recent narrative continuity
]


def _init_life_db() -> None:
    """Initialize central SQLite DB schema. Safe to call multiple times."""
    conn = sqlite3.connect(LIFE_DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS agents (
            agent_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            role TEXT NOT NULL,
            clickup_email TEXT DEFAULT '',
            clickup_user_id TEXT DEFAULT '',
            workspace_dir TEXT NOT NULL,
            life_root TEXT DEFAULT '',
            enabled_modules TEXT DEFAULT '[]',
            genesis_completed INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS traits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_id TEXT NOT NULL,
            trait_key TEXT NOT NULL,
            trait_value TEXT,
            active INTEGER DEFAULT 1,
            FOREIGN KEY (agent_id) REFERENCES agents(agent_id)
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_id TEXT NOT NULL,
            history_type TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (agent_id) REFERENCES agents(agent_id)
        )
    """)
    conn.commit()
    conn.close()


def _migrate_json_registry() -> None:
    """Seed SQLite DB from agents.json on first run (preserves quin-ea-v1)."""
    if not REGISTRY_PATH.exists():
        return
    try:
        data = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
        agents = data.get("agents", {})
    except Exception:
        return

    conn = sqlite3.connect(LIFE_DB_PATH)
    c = conn.cursor()
    for agent_id, cfg in agents.items():
        c.execute("SELECT 1 FROM agents WHERE agent_id = ?", (agent_id,))
        if c.fetchone():
            continue  # already migrated
        life_root = cfg.get("life_root", "")
        c.execute("""
            INSERT INTO agents (agent_id, name, role, workspace_dir, life_root, enabled_modules)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            agent_id,
            cfg.get("name", agent_id),
            cfg.get("name", agent_id),
            life_root,
            life_root,
            json.dumps(cfg.get("enabled_modules", [])),
        ))
    conn.commit()
    conn.close()


def _db_get_agent(agent_id: str) -> Optional[Dict[str, Any]]:
    conn = sqlite3.connect(LIFE_DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM agents WHERE agent_id = ?", (agent_id,))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None


def _identity_agent_id(agent_dir: Path) -> str:
    """Resolve canonical agent_id from IDENTITY.md (fallback: directory name)."""
    identity_file = agent_dir / "IDENTITY.md"
    if identity_file.exists():
        try:
            for line in identity_file.read_text(encoding="utf-8").splitlines():
                if "**Agent ID:**" in line:
                    candidate = line.split("**Agent ID:**", 1)[1].strip()
                    if candidate:
                        return candidate
        except Exception:
            pass
    return agent_dir.name


# ===== Lifecycle tools (FastMCP) =====

@mcp.tool()
def discover_agents() -> List[Dict]:
    """Auto-discover C-level agents from Antfarm-provisioned shared paths."""
    found = []
    if not SHARED_AGENTS_PATH.exists():
        return found

    for agent_dir in sorted(SHARED_AGENTS_PATH.iterdir()):
        if not agent_dir.is_dir():
            continue

        identity_file = agent_dir / "IDENTITY.md"
        if not identity_file.exists():
            continue

        agent_name = agent_dir.name
        agent_id = _identity_agent_id(agent_dir)

        clickup_email = ""
        with open(identity_file) as f:
            for line in f:
                if line.startswith("clickup_email:"):
                    clickup_email = line.split(":", 1)[1].strip()
                    break

        db_row = _db_get_agent(agent_id)
        if not db_row:
            legacy_id = _short_to_legacy(agent_id)
            if legacy_id:
                db_row = _db_get_agent(legacy_id)
        found.append({
            "agent_id": agent_id,
            "name": agent_name,
            "workspace": str(agent_dir),
            "clickup_email": clickup_email,
            "registered": db_row is not None,
            "genesis_completed": bool(db_row.get("genesis_completed")) if db_row else False,
        })

    return found


@mcp.tool()
def register_agent(
    agent_id: str,
    name: str,
    workspace_dir: str,
    clickup_email: str = "",
    life_root: str = "",
) -> Dict:
    """Register a C-level agent in the central LIFE database."""
    conn = sqlite3.connect(LIFE_DB_PATH)
    c = conn.cursor()
    c.execute("""
        INSERT OR REPLACE INTO agents
            (agent_id, name, role, clickup_email, workspace_dir, life_root, enabled_modules, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
    """, (
        agent_id, name, name, clickup_email, workspace_dir,
        life_root or _legacy_runtime_life_root() or workspace_dir,
        json.dumps(["drives", "heart", "semantic", "working", "journal", "state", "history"]),
    ))
    conn.commit()
    conn.close()
    return {"status": "registered", "agent_id": agent_id, "name": name}


@mcp.tool()
def initialize_life_core(agent_id: str) -> Dict:
    """Initialize LIFE core for an agent: create DATA dirs and traits DB."""
    row = _db_get_agent(agent_id)
    if not row:
        return {"error": f"Agent not found: {agent_id}"}

    workspace_dir = Path(row["workspace_dir"])
    life_data_dir = workspace_dir / "LIFE" / "DATA"
    life_data_dir.mkdir(parents=True, exist_ok=True)
    (life_data_dir / "history").mkdir(exist_ok=True)
    (life_data_dir / "traits").mkdir(exist_ok=True)

    # Copy Genesis questions into agent workspace
    questions_dest = workspace_dir / "CORE" / "genesis" / "questions.md"
    questions_dest.parent.mkdir(parents=True, exist_ok=True)
    if GENESIS_QUESTIONS_PATH.exists() and not questions_dest.exists():
        questions_dest.write_text(GENESIS_QUESTIONS_PATH.read_text())

    # Per-agent traits DB
    traits_db = life_data_dir / "traits" / "traits.db"
    if not traits_db.exists():
        agent_conn = sqlite3.connect(traits_db)
        agent_conn.execute("""
            CREATE TABLE IF NOT EXISTS traits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trait_key TEXT NOT NULL,
                trait_value TEXT,
                active INTEGER DEFAULT 1
            )
        """)
        agent_conn.commit()
        agent_conn.close()

    return {
        "status": "initialized",
        "agent_id": agent_id,
        "life_data_dir": str(life_data_dir),
        "traits_db": str(traits_db),
        "questions_copied": questions_dest.exists(),
    }


@mcp.tool()
def run_genesis_interview(agent_id: str) -> Dict:
    """Return instructions for spawning an agent to complete its Genesis interview."""
    answers_path = ""
    row = _db_get_agent(agent_id)
    if row:
        answers_path = str(Path(row["workspace_dir"]) / "CORE" / "genesis" / "answers.md")
    return {
        "status": "interview_required",
        "agent_id": agent_id,
        "answers_path": answers_path,
        "command": (
            "Use any configured OpenClaw agent to create answers.md at the path above. "
            "Recommended: openclaw agent --agent main --message "
            f"'Write LIFE Genesis answers for agent_id={agent_id}. Read the agent persona files in its workspace and save to CORE/genesis/answers.md.'"
        ),
    }


@mcp.tool()
def apply_genesis_answers(agent_id: str, answers_path: str = "") -> Dict:
    """Mark Genesis as complete after agent has saved answers.md."""
    row = _db_get_agent(agent_id)
    if not row:
        return {"error": f"Agent not found: {agent_id}"}

    resolved_path = answers_path or str(
        Path(row["workspace_dir"]) / "CORE" / "genesis" / "answers.md"
    )
    if not Path(resolved_path).exists():
        return {"error": f"answers.md not found at: {resolved_path}"}

    conn = sqlite3.connect(LIFE_DB_PATH)
    conn.execute(
        "UPDATE agents SET genesis_completed = 1, updated_at = CURRENT_TIMESTAMP WHERE agent_id = ?",
        (agent_id,),
    )
    conn.commit()
    conn.close()

    return {
        "status": "applied",
        "agent_id": agent_id,
        "answers_path": resolved_path,
        "message": "Genesis complete. Run wake_agent to activate.",
    }


@mcp.tool()
def get_agent_clickup_info(agent_id: str) -> Dict:
    """Retrieve ClickUp credentials for an agent."""
    row = _db_get_agent(agent_id)
    if not row:
        return {"error": f"Agent not found: {agent_id}"}
    return {
        "agent_id": agent_id,
        "clickup_email": row.get("clickup_email", ""),
        "clickup_user_id": row.get("clickup_user_id", ""),
    }


@mcp.resource("life://agents")
def list_registered_agents() -> str:
    """List all agents registered in the LIFE gateway."""
    conn = sqlite3.connect(LIFE_DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT agent_id, name, clickup_email, genesis_completed FROM agents ORDER BY agent_id"
    ).fetchall()
    conn.close()

    lines = ["LIFE Gateway — Registered Agents:", ""]
    for r in rows:
        status = "✓ Active" if r["genesis_completed"] else "○ Pending Genesis"
        lines.append(f"  {r['agent_id']} ({r['name']}) — {r['clickup_email'] or 'no email'} — {status}")
    return "\n".join(lines) if rows else "No agents registered yet."


# ===== Module-routing helpers (subprocess-based LIFE module calls) =====


def _load_registry(allow_missing: bool = False) -> Dict[str, Any]:
    if not REGISTRY_PATH.exists():
        if allow_missing:
            return {}
        raise FileNotFoundError(f"Registry file not found: {REGISTRY_PATH}")

    data = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    agents_json = data.get("agents", {})
    if not isinstance(agents_json, dict):
        if allow_missing:
            return {}
        raise ValueError("Registry must contain object: agents")
    if not agents_json and not allow_missing:
        raise ValueError("Registry must contain non-empty object: agents")
    return agents_json


def _parse_enabled_modules(value: Any) -> List[str]:
    if isinstance(value, list) and value:
        return [str(v) for v in value]
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list) and parsed:
                return [str(v) for v in parsed]
        except Exception:
            pass
    return list(ALLOWED_MODULE_TOOLS.keys())


def _legacy_to_short(agent_id: str) -> Optional[str]:
    if agent_id == "quin-ea-v1":
        return None
    if agent_id.startswith("quin-") and agent_id.endswith("-v1") and len(agent_id) > len("quin--v1"):
        return agent_id[len("quin-"):-len("-v1")]
    return None


def _short_to_legacy(agent_id: str) -> Optional[str]:
    if agent_id == "quin-ea-v1" or agent_id.startswith("quin-"):
        return None
    return f"quin-{agent_id}-v1"


def _resolve_agent_id(agent_id: str, registry: Dict[str, Any]) -> Optional[str]:
    candidates: List[str] = [agent_id]
    short = _legacy_to_short(agent_id)
    if short:
        candidates.append(short)
    legacy = _short_to_legacy(agent_id)
    if legacy:
        candidates.append(legacy)

    seen = set()
    ordered = []
    for c in candidates:
        if c and c not in seen:
            ordered.append(c)
            seen.add(c)

    for c in ordered:
        if _db_get_agent(c):
            return c
    for c in ordered:
        if c in registry:
            return c
    return None


def _legacy_runtime_life_root(registry: Optional[Dict[str, Any]] = None) -> str:
    row = _db_get_agent("quin-ea-v1")
    if row and row.get("life_root"):
        return str(row.get("life_root"))

    reg = registry or _load_registry(allow_missing=True)
    root = reg.get("quin-ea-v1", {}).get("life_root", "")
    return str(root or "")


def _get_agent_cfg(agent_id: str) -> Dict[str, Any]:
    """Resolve agent config with DB-first lookup and legacy ID compatibility."""
    registry = _load_registry(allow_missing=True)
    resolved_id = _resolve_agent_id(agent_id, registry)
    if not resolved_id:
        raise ValueError(f"Unknown agent_id: {agent_id}")

    row = _db_get_agent(resolved_id)
    if row:
        cfg: Dict[str, Any] = {
            "name": row.get("name", resolved_id),
            "life_root": row.get("life_root", "") or "",
            "enabled_modules": _parse_enabled_modules(row.get("enabled_modules")),
            "voice_enabled": False,
            "workspace_dir": row.get("workspace_dir", "") or "",
            "clickup_email": row.get("clickup_email", "") or "",
        }
        reg_cfg = registry.get(resolved_id, {})
        if reg_cfg:
            if not cfg["life_root"]:
                cfg["life_root"] = reg_cfg.get("life_root", "") or ""
            if not cfg["enabled_modules"]:
                cfg["enabled_modules"] = _parse_enabled_modules(reg_cfg.get("enabled_modules"))
            cfg["voice_enabled"] = bool(reg_cfg.get("voice_enabled", False))
            cfg["name"] = cfg.get("name") or reg_cfg.get("name", resolved_id)
        cfg["_requested_agent_id"] = agent_id
        cfg["_resolved_agent_id"] = resolved_id
        return cfg

    # Registry-only fallback (legacy compatibility)
    reg_cfg = registry.get(resolved_id)
    if reg_cfg:
        cfg = dict(reg_cfg)
        cfg.setdefault("name", resolved_id)
        cfg.setdefault("life_root", "")
        cfg["enabled_modules"] = _parse_enabled_modules(cfg.get("enabled_modules"))
        cfg["voice_enabled"] = bool(cfg.get("voice_enabled", False))
        cfg["_requested_agent_id"] = agent_id
        cfg["_resolved_agent_id"] = resolved_id
        return cfg

    raise ValueError(f"Unknown agent_id: {agent_id}")


def _candidate_life_roots(agent_cfg: Dict[str, Any]) -> List[Path]:
    roots: List[str] = []
    primary = str(agent_cfg.get("life_root", "") or "")
    if primary:
        roots.append(primary)

    legacy_root = _legacy_runtime_life_root()
    if legacy_root and legacy_root not in roots:
        roots.append(legacy_root)

    return [Path(r) for r in roots if r]


def _module_script(agent_cfg: Dict[str, Any], module: str) -> Path:
    tried: List[str] = []
    for root in _candidate_life_roots(agent_cfg):
        script = root / "CORE" / module / "server.py"
        tried.append(str(script))
        if script.exists():
            return script

    requested = agent_cfg.get("_requested_agent_id") or agent_cfg.get("_resolved_agent_id") or "(unknown)"
    raise FileNotFoundError(
        f"Module server missing for agent_id={requested} module={module}. Tried: " + "; ".join(tried)
    )


def _send(proc: subprocess.Popen, payload: Dict[str, Any]) -> None:
    assert proc.stdin is not None
    proc.stdin.write(json.dumps(payload) + "\n")
    proc.stdin.flush()


def _read_for_id(
    proc: subprocess.Popen, rid: int, timeout_sec: int
) -> Tuple[Dict[str, Any], List[str]]:
    assert proc.stdout is not None
    assert proc.stderr is not None

    deadline = time.time() + timeout_sec
    stderr_lines: List[str] = []

    while time.time() < deadline:
        remaining = max(0.0, deadline - time.time())
        ready, _, _ = select.select([proc.stdout, proc.stderr], [], [], remaining)
        if not ready:
            continue

        for stream in ready:
            line = stream.readline()
            if not line:
                continue
            if stream is proc.stderr:
                stderr_lines.append(line.rstrip("\n"))
                continue
            try:
                msg = json.loads(line)
            except Exception:
                continue
            if msg.get("id") == rid:
                return msg, stderr_lines

    tail = " | ".join(stderr_lines[-5:]) if stderr_lines else "(no stderr)"
    raise TimeoutError(f"Timed out waiting for MCP response id={rid}. stderr tail: {tail}")


def _invoke_module(
    agent_cfg: Dict[str, Any], module: str, tool: str, args: Dict[str, Any]
) -> List[Dict[str, Any]]:
    script = _module_script(agent_cfg, module)
    proc = subprocess.Popen(
        [PYTHON_BIN, str(script)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    try:
        _send(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        _read_for_id(proc, 1, timeout_sec=CALL_TIMEOUT_SEC)
        _send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        _send(proc, {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": tool, "arguments": args or {}},
        })
        msg, stderr_lines = _read_for_id(proc, 2, timeout_sec=CALL_TIMEOUT_SEC)
        if "error" in msg:
            raise RuntimeError(f"{module}:{tool} error: {msg['error']}")

        result = msg.get("result", {})
        content = result.get("content", [{"type": "text", "text": "(no content)"}])
        if stderr_lines:
            content = list(content) + [{
                "type": "text",
                "text": "[life-gateway note] module stderr:\n" + "\n".join(stderr_lines[-3:]),
            }]
        return content
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


def _content_to_text(content: List[Dict[str, Any]], max_chars: int = 500000) -> str:
    chunks = [
        item.get("text", "")
        for item in content
        if isinstance(item, dict) and item.get("type") == "text"
    ]
    text = "\n".join(c for c in chunks if c).strip()
    # CQC - Removed truncation for now since it can cut off important info. Consider smarter truncation if needed.
    #if len(text) > max_chars:
    #    return text[:max_chars] + "\n…[truncated]"
    return text or "(no text output)"


def _validate_access(agent_cfg: Dict[str, Any], module: str, tool: str) -> None:
    enabled_modules = set(agent_cfg.get("enabled_modules", list(ALLOWED_MODULE_TOOLS.keys())))
    if module not in ALLOWED_MODULE_TOOLS:
        raise ValueError(f"Module not allowed by gateway policy: {module}")
    if module not in enabled_modules:
        raise ValueError(f"Module disabled for this agent: {module}")
    if tool not in ALLOWED_MODULE_TOOLS[module]:
        raise ValueError(f"Tool not allowed: {module}:{tool}")


# ===== Module-routing tools (FastMCP) =====


@mcp.tool()
def agents() -> str:
    """List registered LIFE agent identities and enabled modules."""
    registry = _load_registry(allow_missing=True)
    lines: List[str] = []

    conn = sqlite3.connect(LIFE_DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT agent_id, name, life_root, enabled_modules FROM agents ORDER BY agent_id").fetchall()
    conn.close()

    db_ids = set()
    for row in rows:
        aid = row["agent_id"]
        db_ids.add(aid)
        name = row["name"] or aid
        life_root = row["life_root"] or ""
        modules = ",".join(_parse_enabled_modules(row["enabled_modules"]))
        lines.append(f"{aid} | {name} | root={life_root} | modules=[{modules}] | source=db")

    for aid, cfg in registry.items():
        if aid in db_ids:
            continue
        name = cfg.get("name", aid)
        life_root = cfg.get("life_root", "")
        modules = ",".join(_parse_enabled_modules(cfg.get("enabled_modules", [])))
        lines.append(f"{aid} | {name} | root={life_root} | modules=[{modules}] | source=registry")

    return "\n".join(lines) if lines else "No agents registered."


@mcp.tool()
def status(agent_id: str) -> str:
    """Check LIFE module availability for an agent_id."""
    agent_cfg = _get_agent_cfg(agent_id)
    resolved = agent_cfg.get("_resolved_agent_id", agent_id)
    lines = [
        f"agent_id: {agent_id}",
        f"resolved_agent_id: {resolved}",
        f"name: {agent_cfg.get('name', resolved)}",
        f"life_root: {agent_cfg.get('life_root')}",
    ]

    roots = _candidate_life_roots(agent_cfg)
    for module in agent_cfg.get("enabled_modules", []):
        found_path = None
        found_root = None
        for root in roots:
            candidate = root / "CORE" / module / "server.py"
            if candidate.exists():
                found_path = candidate
                found_root = root
                break
        if found_path:
            mode = "primary" if str(found_root) == str(agent_cfg.get("life_root", "")) else "legacy-runtime"
            lines.append(f"module:{module} -> ok ({mode}) [{found_path}]")
        else:
            lines.append(f"module:{module} -> missing")

    lines.append(f"voice_enabled: {bool(agent_cfg.get('voice_enabled', False))}")
    return "\n".join(lines)


@mcp.tool()
def wake(agent_id: str) -> str:
    """Run wake protocol for an agent_id (drives, heart, working, semantic, history, patterns, state, journal)."""
    agent_cfg = _get_agent_cfg(agent_id)
    resolved = agent_cfg.get("_resolved_agent_id", agent_id)
    out_parts = [f"Wake protocol for {agent_id} (resolved={resolved})"]
    enabled = set(agent_cfg.get("enabled_modules", []))

    for module, tool_name, tool_args in WAKE_SEQUENCE:
        if module not in enabled:
            continue
        _validate_access(agent_cfg, module, tool_name)
        content = _invoke_module(agent_cfg, module, tool_name, tool_args)
        out_parts.append(f"\n--- {module}:{tool_name} ---")
        out_parts.append(_content_to_text(content))

    return "\n".join(out_parts)


@mcp.tool()
def call(
    agent_id: str,
    module: str,
    tool: str,
    args: Optional[Dict[str, Any]] = None,
) -> str:
    """Route a LIFE module tool call by agent_id/module/tool."""
    if not agent_id:
        raise ValueError("agent_id is required")
    if not module:
        raise ValueError("module is required")
    if not tool:
        raise ValueError("tool is required")
    agent_cfg = _get_agent_cfg(agent_id)
    _validate_access(agent_cfg, module, tool)
    content = _invoke_module(agent_cfg, module, tool, args or {})
    return _content_to_text(content)


@mcp.tool()
def soul_coherence_check(agent_id: str) -> Dict[str, Any]:
    """
    Validate soul coherence for an agent. Returns a composite score (0-100)
    and per-dimension breakdown covering identity, genesis, patterns, memory,
    and drive stability.
    """
    import sqlite3 as _sqlite3
    from datetime import datetime as _dt, timezone as _tz

    result: Dict[str, Any] = {
        "agent_id": agent_id,
        "timestamp": _dt.now(_tz.utc).isoformat(),
        "score": 0,
        "dimensions": {},
        "issues": [],
    }

    # 1. Identity invariants — self.md and origin.md present and non-empty (25 pts)
    identity_score = 0
    try:
        agent_cfg = _get_agent_cfg(agent_id)
        life_root = agent_cfg.get("life_root") or ""
        if life_root:
            self_path = Path(life_root) / "DATA" / "history" / "self.md"
            origin_path = Path(life_root) / "DATA" / "history" / "origin.md"
            self_ok = self_path.exists() and self_path.stat().st_size > 50
            origin_ok = origin_path.exists() and origin_path.stat().st_size > 20
            if self_ok:
                identity_score += 12
            else:
                result["issues"].append("self.md missing or empty")
            if origin_ok:
                identity_score += 13
            else:
                result["issues"].append("origin.md missing or empty")
        else:
            result["issues"].append("life_root not configured")
        result["dimensions"]["identity_invariants"] = identity_score
    except Exception as e:
        result["issues"].append(f"identity check error: {e}")
        result["dimensions"]["identity_invariants"] = 0

    # 2. Genesis completeness (20 pts)
    genesis_score = 0
    try:
        row = _db_get_agent(agent_id)
        if row and row.get("genesis_completed"):
            genesis_score = 20
            # Bonus: check answers.md trait count from life_root
            if life_root:
                answers = Path(life_root) / "CORE" / "genesis" / "answers.md"
                if answers.exists():
                    text = answers.read_text(encoding="utf-8", errors="ignore")
                    trait_count = len([line for line in text.splitlines() if "(" in line and ")" in line and line.strip().startswith(("Name:", "1 ", "2 ", "10 ") or line[0].isdigit())])
                    result["dimensions"]["genesis_traits_detected"] = trait_count
        else:
            result["issues"].append("genesis not completed")
        result["dimensions"]["genesis_complete"] = genesis_score
    except Exception as e:
        result["issues"].append(f"genesis check error: {e}")
        result["dimensions"]["genesis_complete"] = 0

    # 3. Patterns growth — distilled lessons stored (20 pts)
    patterns_score = 0
    try:
        if life_root:
            patterns_db = Path(life_root) / "DATA" / "patterns.db"
            if patterns_db.exists():
                conn = _sqlite3.connect(patterns_db)
                count = conn.execute("SELECT COUNT(*) FROM patterns").fetchone()[0]
                conn.close()
                result["dimensions"]["patterns_count"] = count
                if count >= 10:
                    patterns_score = 20
                elif count >= 5:
                    patterns_score = 15
                elif count >= 1:
                    patterns_score = 8
                else:
                    result["issues"].append("no patterns stored yet — agent not learning from experience")
            else:
                result["issues"].append("patterns.db not found")
        result["dimensions"]["patterns_growth"] = patterns_score
    except Exception as e:
        result["issues"].append(f"patterns check error: {e}")
        result["dimensions"]["patterns_growth"] = 0

    # 4. Memory freshness — recent semantic memories (20 pts)
    memory_score = 0
    try:
        if life_root:
            semantic_db = Path(life_root) / "DATA" / "semantic.db"
            if semantic_db.exists():
                conn = _sqlite3.connect(semantic_db)
                tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
                memories_table = "memories" if "memories" in tables else (tables[0] if tables else None)
                if memories_table:
                    count = conn.execute(f"SELECT COUNT(*) FROM {memories_table}").fetchone()[0]
                    # Try to get recently-added ones (last 7 days) if there's a timestamp column
                    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({memories_table})").fetchall()]
                    recent = 0
                    for ts_col in ("created_at", "timestamp", "updated_at", "last_accessed"):
                        if ts_col in cols:
                            try:
                                recent = conn.execute(
                                    f"SELECT COUNT(*) FROM {memories_table} WHERE {ts_col} >= datetime('now','-7 days')"
                                ).fetchone()[0]
                                break
                            except Exception:
                                pass
                    conn.close()
                    result["dimensions"]["semantic_total"] = count
                    result["dimensions"]["semantic_recent_7d"] = recent
                    if recent >= 5:
                        memory_score = 20
                    elif recent >= 1:
                        memory_score = 12
                    elif count >= 1:
                        memory_score = 8  # has memories but nothing recent
                    else:
                        result["issues"].append("no semantic memories stored")
                else:
                    conn.close()
                    result["issues"].append("semantic.db has no tables")
            else:
                result["issues"].append("semantic.db not found")
        result["dimensions"]["memory_freshness"] = memory_score
    except Exception as e:
        result["issues"].append(f"memory check error: {e}")
        result["dimensions"]["memory_freshness"] = 0

    # 5. Drive stability — drives.db exists and has been written recently (20 pts)
    drive_score = 0
    try:
        if life_root:
            drives_db = Path(life_root) / "DATA" / "drives.db"
            if drives_db.exists():
                age_days = (time.time() - drives_db.stat().st_mtime) / 86400
                result["dimensions"]["drives_db_age_days"] = round(age_days, 1)
                if age_days <= 1:
                    drive_score = 20
                elif age_days <= 7:
                    drive_score = 15
                elif age_days <= 30:
                    drive_score = 8
                else:
                    result["issues"].append("drives.db not updated in >30 days")
            else:
                result["issues"].append("drives.db not found")
        result["dimensions"]["drive_stability"] = drive_score
    except Exception as e:
        result["issues"].append(f"drives check error: {e}")
        result["dimensions"]["drive_stability"] = 0

    total = (
        result["dimensions"].get("identity_invariants", 0)
        + result["dimensions"].get("genesis_complete", 0)
        + result["dimensions"].get("patterns_growth", 0)
        + result["dimensions"].get("memory_freshness", 0)
        + result["dimensions"].get("drive_stability", 0)
    )
    result["score"] = total
    result["grade"] = (
        "EXCELLENT" if total >= 90
        else "GOOD" if total >= 75
        else "FAIR" if total >= 50
        else "DEGRADED"
    )
    return result


# ===== Entry point =====

if __name__ == "__main__":
    _init_life_db()
    _migrate_json_registry()

    # Auto-discover and register C-level agents from shared paths
    if SHARED_AGENTS_PATH.exists():
        for agent_dir in sorted(SHARED_AGENTS_PATH.iterdir()):
            if not agent_dir.is_dir() or not (agent_dir / "IDENTITY.md").exists():
                continue
            agent_name = agent_dir.name
            agent_id = _identity_agent_id(agent_dir)
            if not _db_get_agent(agent_id):
                clickup_email = ""
                with open(agent_dir / "IDENTITY.md") as f:
                    for line in f:
                        if line.startswith("clickup_email:"):
                            clickup_email = line.split(":", 1)[1].strip()
                            break
                register_agent(agent_id, agent_name, str(agent_dir), clickup_email)
                initialize_life_core(agent_id)
                sys.stderr.write(f"life-gateway: registered {agent_id}\n")

    # Run with HTTP transport on port 18888
    # FastMCP will expose the MCP endpoint at /mcp by default
    mcp.run(
        transport="http",
        host="localhost",
        port=18888
    )

