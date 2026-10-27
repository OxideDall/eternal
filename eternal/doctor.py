"""Free check of what a session in this repo relies on; nothing from real transcripts reaches the output."""
import json
import shutil
import sqlite3
import subprocess
import tempfile
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from . import archive, hook, live, mcp, selftest
from .selftest import Checks, put

KEEP_DAYS = 3650  # Claude Code deletes transcripts older than cleanupPeriodDays (default 30) at startup
LOG_WINDOW = 50  # latest firings of the current hook code checked for errors
TIMEOUT = 60
SERVER = "eternal"
PROBE = "doctor probe: a prompt the registry must list"
SID = "d0c70000-0000-4000-8000-000000000000"
TS = "2026-01-01T00:00:00Z"


class Check(StrEnum):
    HOOK = "hook_configured"  # project settings run eternal.hook on SessionStart, compactions included
    HOOK_RUNS = "hook_runs"  # that command, given a compaction of a synthetic transcript, returns a registry
    MCP = "mcp_configured"  # .mcp.json declares the server and settings approve it
    HANDSHAKE = "mcp_handshake"  # that server, on an empty archive, answers initialize, tools/list and a search
    TRUST = "trusted"  # this folder or a parent accepted the trust dialog
    MEMORY = "shared_memory"  # sessions here use the auto-memory of sessions started in $HOME
    KEPT = "transcripts_kept"
    LIVE = "live_archive"  # exists and follows every transcript
    HOOK_LOG = "hook_log"  # no errors among the latest firings of the current hook code
    DELIVERY = "deliveries"  # every compaction of sessions here delivered the registry in time
    SELFTEST = "selftest"  # the last selftest ran every scenario on this Claude Code version and passed


@dataclass(frozen=True)
class Ctx:
    repo: Path
    home: Path

    @property
    def root(self) -> Path:
        return self.home / ".claude/projects"


Outcome = tuple[bool, str]


def read_json(path: Path, tries: int = 3) -> dict | None:
    """None if missing; ValueError only after `tries` parses, since live sessions rewrite their config files."""
    for i in range(tries):
        try:
            d = json.loads(path.read_text())
            return d if isinstance(d, dict) else {}
        except FileNotFoundError:
            return None
        except ValueError:
            if i == tries - 1:
                raise
            time.sleep(0.2)
    return None


def hook_command(repo: Path) -> tuple[str, int] | None:
    for group in ((read_json(repo / ".claude/settings.json") or {}).get("hooks") or {}).get(hook.HookEvent.SESSION_START, []):
        if group.get("matcher") not in (None, "", "*") and hook.StartSource.COMPACT not in group["matcher"]:
            continue
        for h in group.get("hooks", []):
            if h.get("type") == "command" and "eternal.hook" in h.get("command", ""):
                return h["command"], int(h.get("timeout") or TIMEOUT)
    return None


def synthetic() -> list[dict]:
    """A prompt and a file write, then a compaction."""
    usage = {"input_tokens": 1, "output_tokens": 1}
    write = {"type": "tool_use", "id": "t1", "name": "Write", "input": {"file_path": "/tmp/doctor_probe.txt"}}
    return [
        {"type": "user", "uuid": "d1", "timestamp": TS, "origin": {"kind": "human"}, "promptSource": "typed",
         "message": {"role": "user", "content": PROBE}},
        {"type": "assistant", "uuid": "d2", "timestamp": TS,
         "message": {"id": "m1", "model": "probe", "content": [write], "usage": usage}},
        {"type": "system", "subtype": "compact_boundary", "uuid": "d3", "timestamp": TS,
         "compactMetadata": {"trigger": "manual", "preTokens": 1, "postTokens": 1}},
        {"type": "user", "uuid": "d4", "timestamp": TS, "isCompactSummary": True,
         "message": {"role": "user", "content": "Summary"}},
    ]


def check_hook(c: Ctx) -> Outcome:
    found = hook_command(c.repo)
    if not found:
        return False, "no SessionStart command hook running eternal.hook on compaction in .claude/settings.json"
    return True, f"{found[0]} (timeout {found[1]}s)"


def check_hook_runs(c: Ctx) -> Outcome:
    found = hook_command(c.repo)
    if not found:
        return False, "not configured"
    cmd, timeout = found
    with tempfile.TemporaryDirectory(prefix="eternal-doctor-") as d:
        t = Path(d) / f"{SID}.jsonl"
        t.write_text("".join(json.dumps(e) + "\n" for e in synthetic()))
        inp = {"session_id": SID, "transcript_path": str(t), "cwd": str(c.repo),
               "hook_event_name": hook.HookEvent.SESSION_START.value, "source": hook.StartSource.COMPACT.value}
        t0 = time.monotonic()
        p = subprocess.run(["sh", "-c", cmd], input=json.dumps(inp), capture_output=True, text=True, timeout=timeout,
                           env=selftest.fresh_env(CLAUDE_PROJECT_DIR=str(c.repo), ETERNAL_LIVE=str(Path(d) / "live")))
        secs = time.monotonic() - t0
    try:
        ctx = json.loads(p.stdout)["hookSpecificOutput"]["additionalContext"]
    except (ValueError, KeyError, TypeError):
        ctx = ""
    ok = p.returncode == 0 and ctx.startswith(hook.MARK) and PROBE in ctx
    return ok, f"exit {p.returncode}, registry of {len(ctx)} chars in {secs:.2f}s" + (
        "" if ok else f": {p.stderr.strip()[-200:]}")


def mcp_server(repo: Path) -> dict | None:
    return ((read_json(repo / ".mcp.json") or {}).get("mcpServers") or {}).get(SERVER)


def approval(c: Ctx) -> Outcome:
    found: list[tuple[str, dict]] = []
    for name, path in ((".claude/settings.local.json", c.repo / ".claude/settings.local.json"),
                       (".claude/settings.json", c.repo / ".claude/settings.json"),
                       ("~/.claude/settings.json", c.home / ".claude/settings.json")):
        try:
            found.append((name, read_json(path) or {}))
        except ValueError:
            continue
    try:
        found.append(("~/.claude.json", ((read_json(c.home / ".claude.json") or {}).get("projects") or {})
                      .get(str(c.repo)) or {}))
    except ValueError:
        pass
    off = [n for n, d in found if SERVER in (d.get("disabledMcpjsonServers") or [])]
    on = [n for n, d in found if SERVER in (d.get("enabledMcpjsonServers") or []) or d.get("enableAllProjectMcpServers") is True]
    if off:
        return False, f"disabled in {', '.join(off)}"
    return bool(on), f"enabled in {', '.join(on)}" if on else "not approved: start claude here and approve it, or /mcp"


def check_mcp(c: Ctx) -> Outcome:
    server = mcp_server(c.repo)
    if not server:
        return False, f".mcp.json declares no {SERVER} server"
    ok, where = approval(c)
    return ok, f"{server.get('type', 'stdio')} {server.get('command')} {' '.join(server.get('args', []))}; {where}"


def check_handshake(c: Ctx) -> Outcome:
    server = mcp_server(c.repo)
    if not server:
        return False, "not configured"
    msgs = [{"jsonrpc": "2.0", "id": 1, "method": mcp.Method.INITIALIZE, "params": {"protocolVersion": mcp.PROTOCOL}},
            {"jsonrpc": "2.0", "method": mcp.Method.INITIALIZED},
            {"jsonrpc": "2.0", "id": 2, "method": mcp.Method.TOOLS_LIST},
            {"jsonrpc": "2.0", "id": 3, "method": mcp.Method.TOOLS_CALL,
             "params": {"name": mcp.Tool.SEARCH, "arguments": {"query": "doctor"}}}]
    with tempfile.TemporaryDirectory(prefix="eternal-doctor-") as d:
        db = Path(d) / "archive.db"
        archive.connect(db).close()
        env = selftest.fresh_env(CLAUDE_PROJECT_DIR=str(c.repo), ETERNAL_ARCHIVE=str(db),
                                 **{k: str(v) for k, v in (server.get("env") or {}).items()})
        p = subprocess.run([server["command"], *server.get("args", [])], cwd=c.repo, env=env, capture_output=True,
                           text=True, timeout=TIMEOUT, input="".join(json.dumps(m) + "\n" for m in msgs))
    got: dict = {}
    for line in p.stdout.splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if isinstance(r, dict):
            got[r.get("id")] = r.get("result") or r.get("error") or {}
    name = (got.get(1, {}).get("serverInfo") or {}).get("name")
    tools = [t.get("name") for t in got.get(2, {}).get("tools", [])]
    found = got.get(3, {}).get("isError") is False
    ok = name == SERVER and {mcp.Tool.SEARCH, mcp.Tool.ZOOM} <= set(tools) and found
    return ok, f"server {name}, tools {tools}, search {'answers' if found else 'failed'}" + (
        "" if ok else f": {p.stderr.strip()[-200:]}")


def check_trust(c: Ctx) -> Outcome:
    try:
        projects = (read_json(c.home / ".claude.json") or {}).get("projects") or {}
    except ValueError:
        return True, "unknown: ~/.claude.json did not parse, probably being rewritten"
    for p in (c.repo, *c.repo.parents):
        if (projects.get(str(p)) or {}).get("hasTrustDialogAccepted") is True:
            return True, f"accepted for {p}"
    return False, "start claude here once and accept the trust dialog"


def check_memory(c: Ctx) -> Outcome:
    mine, main = live.project_dir(c.repo, c.root) / "memory", live.project_dir(c.home, c.root) / "memory"
    if mine.resolve() == main.resolve():
        return True, f"{mine} -> {main}"
    return False, f"{mine} is separate from {main}; to share: ln -sT ../{main.parent.name}/memory {mine}"


def check_kept(c: Ctx) -> Outcome:
    days = (read_json(c.home / ".claude/settings.json") or {}).get("cleanupPeriodDays")
    return isinstance(days, int) and days >= KEEP_DAYS, f"cleanupPeriodDays {days}, want at least {KEEP_DAYS}"


def check_live(c: Ctx) -> Outcome:
    path = live.db_path()
    if not path.exists():
        return False, f"{path} missing; a session here builds it in the background, or: python3 -m eternal live"
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        ingested = dict(db.execute("SELECT path, bytes FROM source"))
        lost = live.diverged(db)
    finally:
        db.close()
    behind = sum(p.stat().st_size > ingested.get(str(p.relative_to(c.root)), -1) for p in c.root.glob("*/*.jsonl"))
    return not lost, (f"{len(ingested)} transcripts, {behind} with lines a search has yet to take in, {len(lost)} "
                      f"diverged, {len(live.excluded())} sessions hidden, {path.stat().st_size >> 20} MiB")


def check_hook_log(c: Ctx) -> Outcome:
    log = live.live_dir() / "hook.jsonl"
    rows = []
    for line in log.read_text().splitlines() if log.exists() else []:
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    cur = [r for r in rows if isinstance(r, dict) and r.get("code") == hook.code_digest()][-LOG_WINDOW:]
    errors = [r["error"] for r in cur if r.get("error")]
    slow = max((r.get("seconds", 0.0) for r in cur), default=0.0)
    return not errors, (f"{len(cur)} firings of this code {dict(Counter(r.get('source') for r in cur))}, slowest "
                        f"{slow}s" + (f"; {len(errors)} errors, last: {errors[-1][:160]}" if errors else ""))


def check_delivery(c: Ctx) -> Outcome:
    total: Counter = Counter()
    for p in live.project_dir(c.repo, c.root).glob("*.jsonl"):
        total.update(hook.audit(p))
    return not (total[hook.Delivery.MISSING] or total[hook.Delivery.LATE]), json.dumps(
        {k.value: total[k] for k in hook.Delivery})


def claude_version() -> str:
    exe = shutil.which("claude")
    if not exe:
        return ""
    out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=TIMEOUT).stdout.split()
    return out[0] if out else ""


def check_selftest(c: Ctx) -> Outcome:
    try:
        rep = read_json(live.live_dir() / "selftest" / selftest.LAST) or {}
    except ValueError:
        rep = {}
    if not rep:
        return False, "never ran: python3 -m eternal selftest (paid, Haiku)"
    ran = {k: v.get("claude_code") for k, v in (rep.get("scenarios") or {}).items()}
    missing = [s.value for s in selftest.Scenario if s.value not in ran]
    now = claude_version()
    ok = rep.get("ok") is True and not missing and set(ran.values()) == {now}
    return ok, (f"{rep.get('ts')} ok={rep.get('ok')} on {sorted(set(map(str, ran.values())))}, claude now {now}"
                + (f", scenarios not run: {missing}" if missing else "")
                + ("" if ok else "; rerun: python3 -m eternal selftest"))


CHECKS: dict[Check, Callable[[Ctx], Outcome]] = {
    Check.HOOK: check_hook, Check.HOOK_RUNS: check_hook_runs, Check.MCP: check_mcp, Check.HANDSHAKE: check_handshake,
    Check.TRUST: check_trust, Check.MEMORY: check_memory, Check.KEPT: check_kept, Check.LIVE: check_live,
    Check.HOOK_LOG: check_hook_log, Check.DELIVERY: check_delivery, Check.SELFTEST: check_selftest}


def run(repo: Path = selftest.REPO, home: Path | None = None) -> dict:
    """Every check, in order; a check that raises fails with the exception."""
    c = Ctx(repo, home or Path.home())
    checks: Checks = {}
    for k, fn in CHECKS.items():
        try:
            ok, detail = fn(c)
        except Exception as e:
            ok, detail = False, f"{type(e).__name__}: {e}"[:300]
        put(checks, k, ok, detail)
    return {"ok": all(v["ok"] for v in checks.values()), "checks": checks}
