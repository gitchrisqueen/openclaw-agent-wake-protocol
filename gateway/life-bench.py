#!/usr/bin/env python3
"""
LIFE / Agent Soul Benchmark
Measures soul coherence, module health, wake latency, and growth indicators
for all registered LIFE agents. Writes results to ~/data/bench/life-bench-latest.json
for display on the Live Status dashboard (port 8054).

Usage:
  python3 life-bench.py                   # benchmark all agents
  python3 life-bench.py quin-ea-v1        # single agent
  python3 life-bench.py --dry-run         # print without writing

Output: ~/data/bench/life-bench-latest.json
"""
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ── Config ─────────────────────────────────────────────────────────────────
LIFE_GATEWAY_URL = "http://localhost:18888/mcp"
AGENTS_JSON = Path.home() / ".openclaw/workspaces/quin/life-gateway/agents.json"
AGENTS_DB = Path.home() / ".openclaw/workspaces/quin/life-gateway/agents.db"
OUTPUT_PATH = Path.home() / "data/bench/life-bench-latest.json"
MCPORTER_TIMEOUT = 25  # seconds per mcporter call

WAKE_MODULES = [
    ("drives", "start"),
    ("heart", "search"),
    ("working", "view"),
    ("semantic", "search"),
    ("history", "discover"),
    ("patterns", "recall"),
    ("state", "want"),
    ("journal", "read"),
]

# ── Helpers ─────────────────────────────────────────────────────────────────

def ts_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def mcporter_call(tool: str, *args: str, timeout: int = MCPORTER_TIMEOUT) -> tuple[bool, str]:
    """Call a life-gateway tool via mcporter. Returns (success, output)."""
    cmd = ["mcporter", "call", f"life-gateway.{tool}"] + list(args)
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=timeout, encoding="utf-8"
        )
        output = (result.stdout.strip() or result.stderr.strip() or "(no output)")[:1000]
        success = result.returncode == 0 and not output.startswith("ERROR")
        return success, output
    except subprocess.TimeoutExpired:
        return False, f"TIMEOUT after {timeout}s"
    except FileNotFoundError:
        return False, "mcporter not found in PATH"
    except Exception as e:
        return False, f"ERROR: {e}"


def parse_json_from_output(raw: str) -> dict | list | None:
    """Try to extract a JSON value from mcporter output (may be wrapped in content blocks)."""
    raw = raw.strip()
    # Direct parse
    try:
        return json.loads(raw)
    except Exception:
        pass
    # Extract first {...} or [...]
    for pattern in [r'\{[\s\S]*\}', r'\[[\s\S]*\]']:
        import re
        m = re.search(pattern, raw)
        if m:
            try:
                return json.loads(m.group())
            except Exception:
                pass
    return None


def load_agents(all_agents: bool = False) -> list[dict]:
    """Load registered agents from SQLite DB, falling back to agents.json.
    By default only returns agents with a valid LIFE installation (life_root
    containing DATA/drives.db or CORE/genesis/). Pass all_agents=True to
    include uninitialized agents.
    """
    agents = []
    if AGENTS_DB.exists():
        try:
            conn = sqlite3.connect(AGENTS_DB)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT agent_id, name, life_root, workspace_dir, genesis_completed, enabled_modules "
                "FROM agents ORDER BY agent_id"
            ).fetchall()
            conn.close()
            for r in rows:
                life_root = r["life_root"] or r["workspace_dir"] or ""
                agents.append({
                    "agent_id": r["agent_id"],
                    "name": r["name"],
                    "life_root": life_root,
                    "genesis_completed": bool(r["genesis_completed"]),
                    "enabled_modules": json.loads(r["enabled_modules"] or "[]"),
                })
        except Exception:
            pass

    if not agents and AGENTS_JSON.exists():
        try:
            data = json.loads(AGENTS_JSON.read_text())
            for aid, cfg in data.get("agents", {}).items():
                agents.append({
                    "agent_id": aid,
                    "name": cfg.get("name", aid),
                    "life_root": cfg.get("life_root", ""),
                    "genesis_completed": False,
                    "enabled_modules": cfg.get("enabled_modules", []),
                })
        except Exception:
            pass

    if not all_agents:
        # Only agents with a real LIFE installation
        def has_life(a: dict) -> bool:
            root = Path(a["life_root"]) if a["life_root"] else None
            if not root:
                return False
            return (root / "DATA" / "drives.db").exists() or (root / "CORE" / "genesis").exists()
        agents = [a for a in agents if has_life(a)]

    return agents


# ── Per-agent benchmarks ────────────────────────────────────────────────────

def bench_gateway_reachable() -> dict:
    """Check if life-gateway MCP server is reachable."""
    ok, out = mcporter_call("discover_agents", timeout=10)
    return {"ok": ok, "detail": out[:200]}


def bench_module_health(agent_id: str, life_root: str, enabled_modules: list) -> dict:
    """Test each enabled module responds to a simple call."""
    module_tests = {
        "drives":   ("drives", "start"),
        "heart":    ("heart", "search"),
        "working":  ("working", "view"),
        "semantic": ("semantic", "search"),
        "history":  ("history", "discover"),
        "patterns": ("patterns", "recall"),
        "state":    ("state", "want"),
        "journal":  ("journal", "read"),
    }
    results = {}
    pass_count = 0
    for mod in enabled_modules:
        if mod not in module_tests:
            continue
        _, tool = module_tests[mod]
        ok, detail = mcporter_call("call",
            f"agent_id={agent_id}", f"module={mod}", f"tool={tool}",
            timeout=15
        )
        results[mod] = "ok" if ok else f"error: {detail[:80]}"
        if ok:
            pass_count += 1

    return {
        "modules": results,
        "pass": pass_count,
        "total": len(enabled_modules),
        "score": round(pass_count / max(len(enabled_modules), 1) * 100),
    }


def bench_wake_latency(agent_id: str) -> dict:
    """Run full wake protocol and measure latency."""
    start = time.time()
    ok, output = mcporter_call("wake", f"agent_id={agent_id}", timeout=60)
    elapsed_ms = round((time.time() - start) * 1000)

    # Step headers appear as "--- module:tool_name ---" in the output
    step_found = {}
    for mod, tool in WAKE_MODULES:
        key = f"{mod}:{tool}"
        step_found[key] = f"--- {key} ---" in output

    return {
        "ok": ok,
        "latency_ms": elapsed_ms,
        "latency_grade": (
            "FAST" if elapsed_ms < 3000
            else "OK" if elapsed_ms < 6000
            else "SLOW" if elapsed_ms < 12000
            else "TIMEOUT"
        ),
        "steps_found": step_found,
        "steps_pass": sum(1 for v in step_found.values() if v),
        "steps_total": len(step_found),
    }


def bench_soul_coherence(agent_id: str) -> dict:
    """Call soul_coherence_check and return parsed result."""
    ok, raw = mcporter_call("soul_coherence_check", f"agent_id={agent_id}", timeout=20)
    if not ok:
        return {"ok": False, "score": 0, "grade": "FAILED", "error": raw[:200]}

    parsed = parse_json_from_output(raw)
    if isinstance(parsed, dict) and "score" in parsed:
        return {
            "ok": True,
            "score": parsed.get("score", 0),
            "grade": parsed.get("grade", "?"),
            "dimensions": parsed.get("dimensions", {}),
            "issues": parsed.get("issues", []),
        }
    return {"ok": False, "score": 0, "grade": "PARSE_ERROR", "raw": raw[:200]}


def bench_growth_indicators(life_root: str) -> dict:
    """Check growth metrics from SQLite databases directly."""
    life_path = Path(life_root)
    result = {}

    # Patterns count
    patterns_db = life_path / "DATA" / "patterns.db"
    if patterns_db.exists():
        try:
            conn = sqlite3.connect(patterns_db)
            total = conn.execute("SELECT COUNT(*) FROM patterns").fetchone()[0]
            conn.close()
            result["patterns_total"] = total
            result["patterns_grade"] = (
                "GROWING" if total >= 10
                else "EARLY" if total >= 3
                else "EMPTY"
            )
        except Exception as e:
            result["patterns_error"] = str(e)[:60]
    else:
        result["patterns_total"] = None
        result["patterns_grade"] = "NO_DB"

    # Semantic memories (total + recent 7d)
    semantic_db = life_path / "DATA" / "semantic.db"
    if semantic_db.exists():
        try:
            conn = sqlite3.connect(semantic_db)
            tables = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()]
            tbl = "memories" if "memories" in tables else (tables[0] if tables else None)
            if tbl:
                total = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
                cols = [r[1] for r in conn.execute(f"PRAGMA table_info({tbl})").fetchall()]
                recent = 0
                for col in ("created_at", "timestamp", "updated_at", "last_accessed"):
                    if col in cols:
                        try:
                            recent = conn.execute(
                                f"SELECT COUNT(*) FROM {tbl} WHERE {col} >= datetime('now','-7 days')"
                            ).fetchone()[0]
                            break
                        except Exception:
                            pass
                conn.close()
                result["memories_total"] = total
                result["memories_7d"] = recent
                result["memories_grade"] = (
                    "ACTIVE" if recent >= 5
                    else "LIGHT" if recent >= 1
                    else "STALE"
                )
            else:
                conn.close()
                result["memories_total"] = 0
                result["memories_grade"] = "NO_TABLE"
        except Exception as e:
            result["memories_error"] = str(e)[:60]
    else:
        result["memories_total"] = None
        result["memories_grade"] = "NO_DB"

    # Journal entries (files in DATA/journal/)
    journal_dir = life_path / "DATA" / "journal"
    if journal_dir.exists():
        entries = sorted(journal_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
        recent_7d = sum(
            1 for e in entries
            if (time.time() - e.stat().st_mtime) < 7 * 86400
        )
        result["journal_total"] = len(entries)
        result["journal_7d"] = recent_7d
        result["journal_grade"] = (
            "ACTIVE" if recent_7d >= 1 else "INACTIVE"
        )
    else:
        result["journal_total"] = 0
        result["journal_grade"] = "NO_DIR"

    # Drives DB age
    drives_db = life_path / "DATA" / "drives.db"
    if drives_db.exists():
        age_days = (time.time() - drives_db.stat().st_mtime) / 86400
        result["drives_db_age_days"] = round(age_days, 1)
        result["drives_grade"] = (
            "FRESH" if age_days <= 1
            else "OK" if age_days <= 7
            else "STALE"
        )
    else:
        result["drives_grade"] = "NO_DB"

    return result


# ── Main ────────────────────────────────────────────────────────────────────

def run_bench(target_agents: list[str] | None = None, dry_run: bool = False,
              all_agents: bool = False) -> dict:
    timestamp = ts_now()
    print(f"\n🧬 LIFE / Agent Soul Benchmark  [{timestamp}]")
    print("=" * 60)

    # Gateway reachability
    print("\n⚡ Checking life-gateway...")
    gw = bench_gateway_reachable()
    if not gw["ok"]:
        print(f"  🔴 UNREACHABLE: {gw['detail']}")
        result = {
            "timestamp": timestamp,
            "overall": "FAIL",
            "score": 0,
            "gateway_ok": False,
            "gateway_error": gw["detail"],
            "agents": {},
        }
        if not dry_run:
            OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
            OUTPUT_PATH.write_text(json.dumps(result, indent=2))
        return result
    print(f"  ✅ Gateway reachable")

    agents = load_agents(all_agents=all_agents)
    if target_agents:
        agents = [a for a in agents if a["agent_id"] in target_agents]
    if not agents:
        print("  ⚠️  No agents found")

    agent_results = {}
    all_scores = []

    for agent in agents:
        aid = agent["agent_id"]
        life_root = agent.get("life_root", "")
        enabled = agent.get("enabled_modules", [])
        print(f"\n🤖 Agent: {aid} ({agent.get('name', '?')})")

        # Module health
        print(f"  📋 Module health...")
        mh = bench_module_health(aid, life_root, enabled)
        print(f"     {mh['pass']}/{mh['total']} modules OK (score: {mh['score']})")

        # Wake latency
        print(f"  ⏱️  Wake latency...")
        wl = bench_wake_latency(aid)
        latency_emoji = {"FAST": "🟢", "OK": "🟡", "SLOW": "🟠", "TIMEOUT": "🔴"}.get(wl["latency_grade"], "❓")
        print(f"     {latency_emoji} {wl['latency_ms']}ms ({wl['latency_grade']}) — {wl['steps_pass']}/{wl['steps_total']} steps found")

        # Soul coherence
        print(f"  🧬 Soul coherence...")
        sc = bench_soul_coherence(aid)
        grade_emoji = {"EXCELLENT": "🟢", "GOOD": "🟢", "FAIR": "🟡", "DEGRADED": "🔴"}.get(sc.get("grade", ""), "❓")
        print(f"     {grade_emoji} {sc.get('score', 0)}/100 ({sc.get('grade', '?')})")
        if sc.get("issues"):
            for issue in sc["issues"]:
                print(f"     ⚠️  {issue}")

        # Growth indicators
        print(f"  🌱 Growth indicators...")
        growth = bench_growth_indicators(life_root) if life_root else {}
        print(f"     patterns: {growth.get('patterns_total', '?')} ({growth.get('patterns_grade', '?')})")
        print(f"     memories: {growth.get('memories_total', '?')} total, {growth.get('memories_7d', '?')} this week")
        print(f"     journal: {growth.get('journal_total', '?')} entries, {growth.get('journal_7d', '?')} this week")

        # Composite agent score
        agent_score = round(
            0.25 * mh["score"]
            + 0.25 * (100 if wl["ok"] and wl["latency_grade"] in ("FAST", "OK") else 50 if wl["ok"] else 0)
            + 0.50 * sc.get("score", 0)
        )
        all_scores.append(agent_score)

        agent_results[aid] = {
            "name": agent.get("name", aid),
            "genesis_completed": agent.get("genesis_completed", False),
            "module_health": mh,
            "wake_latency": wl,
            "soul_coherence": sc,
            "growth": growth,
            "score": agent_score,
            "overall": (
                "PASS" if agent_score >= 70 and wl["ok"]
                else "WARN" if agent_score >= 50
                else "FAIL"
            ),
        }

    overall_score = round(sum(all_scores) / max(len(all_scores), 1))
    overall = (
        "PASS" if overall_score >= 70 and all(
            r["overall"] != "FAIL" for r in agent_results.values()
        )
        else "WARN" if overall_score >= 50
        else "FAIL"
    )

    result = {
        "timestamp": timestamp,
        "overall": overall,
        "score": overall_score,
        "gateway_ok": True,
        "agents_tested": len(agent_results),
        "agents": agent_results,
    }

    print(f"\n{'=' * 60}")
    print(f"Overall: {overall} ({overall_score}/100) across {len(agent_results)} agent(s)")

    if not dry_run:
        OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT_PATH.write_text(json.dumps(result, indent=2))
        print(f"Results written to {OUTPUT_PATH}")

    return result


if __name__ == "__main__":
    args = sys.argv[1:]
    dry_run = "--dry-run" in args
    all_agents = "--all" in args
    agent_filter = [a for a in args if not a.startswith("--")] or None
    run_bench(target_agents=agent_filter, dry_run=dry_run, all_agents=all_agents)
