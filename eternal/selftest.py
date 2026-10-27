"""Live check through the real launch path: a session in this repo compacts, the hook delivers, search recalls."""
import json
import os
import queue
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import TypedDict

from . import hook, live
from .bench.qa import WRAPPER

REPO = Path(__file__).resolve().parents[1]
NAME = "eternal-selftest"
TURN_TIMEOUT = 600
SEARCH_TOOLS = ("mcp__eternal__search", "mcp__eternal__zoom")
WINDOW = 100_000  # smallest auto-compact window Claude Code accepts; it compacts near 70K
BIG_FILES, BIG_LINES = 10, 400  # mid-loop filler, about 85K tokens; 12 files pushed Haiku to 190K of 200K
INHERITED = ("CLAUDE", "TUNE_")  # env of the session running the test; a fresh terminal has none of it
LAST = "last.json"


class Scenario(StrEnum):
    RECALL = "recall"  # write a token, /compact, find it by search, resume
    MID_LOOP = "mid_loop"  # auto-compaction inside one turn of many reads


class Check(StrEnum):
    RAN = "ran"  # the session ran to its last result
    MCP = "mcp_connected"  # init lists this repo's eternal server as connected
    COMPACTED = "compacted"
    HOOK = "hook_injected"  # hook.jsonl: a compact firing of this session that returned a registry
    DELIVERED = "registry_delivered"  # audit: the registry reached the next model request after every boundary
    SEARCH = "search_found"  # a search or zoom result carried the token written before the compaction
    ANSWER = "answer_correct"
    RESUME = "registry_after_resume"  # a resumed process, with no tools, quotes the registry header
    MID_LOOP = "compacted_mid_loop"  # the boundary fell inside the turn of reads, after a tool call
    MEMORY = "shared_memory"  # the session's auto-memory is the one of sessions started in $HOME


class Result(TypedDict):
    ok: bool
    detail: str


Checks = dict[str, Result]


def user(text: str) -> str:
    return json.dumps({"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": text}]}},
                      ensure_ascii=False) + "\n"


def main_memory() -> Path:
    """Auto-memory of sessions started in $HOME."""
    return live.project_dir(Path.home(), Path.home() / ".claude/projects") / "memory"


def fresh_env(**extra: str) -> dict[str, str]:
    """This process's env without what a running Claude Code session exports to its children."""
    return {k: v for k, v in os.environ.items() if not k.startswith(INHERITED) or k == "CLAUDE_CONFIG_DIR"} | extra


def drive(argv: list[str], turns: list[str], log: Path, env: dict | None = None) -> list[dict]:
    """Feed turns one at a time to `claude -p --input-format stream-json`, waiting for each result.

    Never raises on a stalled or dead process: the events so far come back, ending with a `selftest_error` event.
    """
    p = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                         cwd=REPO, env=fresh_env() if env is None else env, text=True)
    lines: queue.Queue[str | None] = queue.Queue()

    def pump() -> None:
        for x in p.stdout:
            lines.put(x)
        lines.put(None)

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    events: list[dict] = []
    with os.fdopen(os.open(log, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        try:
            for text in turns:
                p.stdin.write(user(text))
                p.stdin.flush()
                deadline = time.monotonic() + TURN_TIMEOUT
                while True:
                    left = deadline - time.monotonic()
                    try:
                        if left <= 0:  # a process that keeps printing must not outlive the deadline
                            raise queue.Empty
                        line = lines.get(timeout=left)
                    except queue.Empty:
                        raise TimeoutError(f"no result within {TURN_TIMEOUT}s") from None
                    if line is None:
                        raise RuntimeError("claude exited before the turn's result")
                    f.write(line)
                    try:
                        e = json.loads(line)
                    except ValueError:
                        continue
                    events.append(e)
                    if e.get("type") == "result":
                        break
        except (RuntimeError, OSError) as e:
            events.append({"type": "selftest_error", "error": repr(e)[:200]})
        finally:
            try:
                p.stdin.close()  # may flush into a dead process
            except OSError:
                pass
            try:
                p.wait(timeout=60)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()
            reader.join(timeout=5)
            if not reader.is_alive():
                p.stdout.close()
    return events


def argv(model: str, *extra: str) -> list[str]:
    """claudev's flags with a cheap model; this repo's settings and .mcp.json load as in a real session."""
    return [str(WRAPPER), "-p", "--model", model, "--effort", "low", "--dangerously-skip-permissions",
            "--allow-dangerously-skip-permissions", "--input-format", "stream-json", "--output-format", "stream-json",
            "--verbose", "--include-hook-events", "-n", NAME, *extra]


def tool_uses(events: list[dict]) -> Iterator[tuple[int, str]]:
    for i, e in enumerate(events):
        if e.get("type") == "assistant":
            for b in e["message"].get("content", []):
                if b.get("type") == "tool_use":
                    yield i, b["name"]


def tool_results(events: list[dict]) -> Iterator[str]:
    for e in events:
        content = (e.get("message") or {}).get("content") if e.get("type") == "user" else None
        for b in content if isinstance(content, list) else []:
            if b.get("type") == "tool_result":
                c = b.get("content")
                yield c if isinstance(c, str) else json.dumps(c, ensure_ascii=False)


def boundaries(events: list[dict]) -> list[int]:
    return [i for i, e in enumerate(events) if e.get("type") == "system" and e.get("subtype") == "compact_boundary"]


def results(events: list[dict]) -> list[int]:
    return [i for i, e in enumerate(events) if e.get("type") == "result"]


def answer(events: list[dict]) -> str:
    return next((e.get("result") or "" for e in reversed(events) if e.get("type") == "result"), "")


def session_id(events: list[dict]) -> str:
    return next((e.get("session_id", "") for e in events if e.get("subtype") == "init"), "")


def version(events: list[dict]) -> str:
    return next((e.get("claude_code_version", "") for e in events if e.get("subtype") == "init"), "")


def inside(events: list[dict], turn: int) -> list[int]:
    """Boundaries within turn `turn` (from 0) that follow one of its tool calls."""
    r = results(events)
    if len(r) <= turn:
        return []
    lo, hi = r[turn - 1] if turn else -1, r[turn]
    calls = [i for i, _ in tool_uses(events) if lo < i < hi]
    return [b for b in boundaries(events) if lo < b < hi and calls and calls[0] < b]


def cost(events: list[dict]) -> float:
    """Claude Code reports the process's running total in each result."""
    r = results(events)
    return events[r[-1]].get("total_cost_usd") or 0.0 if r else 0.0


def firings(sid: str, source: hook.StartSource) -> list[dict]:
    log = live.live_dir() / "hook.jsonl"
    rows = [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else []
    return [r for r in rows if r.get("session") == sid[:8] and r.get("source") == source]


def transcript(sid: str) -> Path | None:
    return next((Path.home() / ".claude/projects").glob(f"*/{sid}.jsonl"), None) if sid else None


def put(checks: Checks, c: StrEnum, ok: object, detail: str) -> None:
    checks[c.value] = {"ok": bool(ok), "detail": detail}


def common(ev: list[dict], checks: Checks, turns: int) -> None:
    init = next((e for e in ev if e.get("subtype") == "init"), {})
    mine = [s for s in init.get("mcp_servers", []) if s.get("name") == "eternal"]
    err = next((e["error"] for e in ev if e.get("type") == "selftest_error"), "")
    put(checks, Check.RAN, len(results(ev)) == turns and not err, f"{len(results(ev))} of {turns} turns {err}")
    put(checks, Check.MCP, mine and mine[0].get("status") == "connected", json.dumps(mine, ensure_ascii=False))
    put(checks, Check.COMPACTED, boundaries(ev), f"{len(boundaries(ev))} boundaries")
    auto = (init.get("memory_paths") or {}).get("auto") or ""
    put(checks, Check.MEMORY, auto and Path(auto).resolve() == main_memory().resolve(), auto)
    fired = firings(session_id(ev), hook.StartSource.COMPACT)
    put(checks, Check.HOOK, fired and all(r.get("chars") for r in fired),
        json.dumps([{k: r.get(k) for k in ("chars", "reused_lines", "seconds", "error")} for r in fired]))
    path = transcript(session_id(ev))
    d = hook.audit(path) if path else []
    put(checks, Check.DELIVERED, d and all(x == hook.Delivery.DELIVERED for x in d), str([x.value for x in d]))


def recall(model: str, work: Path, out: Path, checks: Checks) -> list[list[dict]]:
    token = f"selftest-{secrets.token_hex(4)}"
    first, second = work / f"alpha_{token[-4:]}.txt", work / f"beta_{token[-4:]}.txt"
    turns = [
        f"Самопроверка eternal. Создай файл {first} и запиши в него одну строку: {token}. Ответь одним словом: готово.",
        f"Создай файл {second} с одной строкой: second. Ответь одним словом: готово.",
        "/compact",
        f"Вызови mcp__eternal__search с запросом {first.name} и найди строку, которую ты записал в этот файл в этой "
        "сессии. Ответь только этой строкой.",
    ]
    ev = drive(argv(model), turns, out / "recall.jsonl")
    common(ev, checks, len(turns))
    after = boundaries(ev)[-1] if boundaries(ev) else len(ev)
    used = [n for i, n in tool_uses(ev) if i > after and n in SEARCH_TOOLS]
    put(checks, Check.SEARCH, used and any(token in r for r in tool_results(ev[after:])), ", ".join(used) or "no call")
    put(checks, Check.ANSWER, token in answer(ev), answer(ev)[:120])
    sid = session_id(ev)
    if not sid:
        return [ev]
    probe = drive(argv(model, "--resume", sid, "--strict-mcp-config", "--tools", ""), [
        "Не вызывай инструменты. Если в твоём контексте есть текст, начинающийся с «[eternal registry]», перепиши "
        "дословно его первые 60 символов. Если такого текста нет, ответь NONE."], out / "resume.jsonl")
    put(checks, Check.RESUME, f"[eternal registry] Session {sid[:8]}" in answer(probe), answer(probe)[:120])
    return [ev, probe]


def mid_loop(model: str, work: Path, out: Path, checks: Checks) -> list[list[dict]]:
    words = "alpha beta gamma delta epsilon zeta theta kappa lambda sigma omega".split()
    files = []
    for n in range(BIG_FILES):
        p = work / f"filler_{n:02d}.txt"
        p.write_text("".join(f"{n:02d}-{i:04d} " + " ".join(words[(i * 7 + j + n) % len(words)] for j in range(9))
                             + "\n" for i in range(BIG_LINES)))
        files.append(str(p))
    turns = ["Самопроверка eternal, шаг 1. Ответь одним словом: ок.", "Шаг 2. Ответь одним словом: ок.",
             "Шаг 3. Прочитай инструментом Read все эти файлы, по одному вызову на файл, затем ответь одним словом: "
             "прочитано.\n" + "\n".join(files),
             "Шаг 4. Ответь одним словом: ок."]
    ev = drive(argv(model, "--tools", "Read", "--settings", json.dumps({"autoCompactWindow": WINDOW})), turns,
               out / "midloop.jsonl", fresh_env(CLAUDE_CODE_AUTO_COMPACT_WINDOW=str(WINDOW)))
    common(ev, checks, len(turns))
    mid = inside(ev, 2)
    put(checks, Check.MID_LOOP, mid, f"{len(mid)} of {len(boundaries(ev))} boundaries inside step 3")
    return [ev]


SCENARIOS: dict[Scenario, Callable[[str, Path, Path, Checks], list[list[dict]]]] = {
    Scenario.RECALL: recall, Scenario.MID_LOOP: mid_loop}


def stash(path: Path | None) -> str:
    """Move a test transcript out of the project, so `claude -c` and session pickers do not offer it."""
    if not path:
        return ""
    dst = live.live_dir() / "selftest" / path.name
    shutil.move(path, dst)
    hook.cache_file(live.live_dir(), path).unlink(missing_ok=True)
    return str(dst)


def run(model: str = "haiku", scenarios: tuple[Scenario, ...] = tuple(Scenario), keep: bool = False) -> dict:
    """Each scenario's checks, session and cost; its session is hidden from live search, `keep` leaves the transcript
    in the project. The report also lands in live_dir()/selftest/last.json for `doctor`."""
    out = live.live_dir() / "selftest"
    out.mkdir(mode=0o700, parents=True, exist_ok=True)
    report: dict = {"model": model, "scenarios": {}, "cost_usd": 0.0}
    for s in scenarios:
        checks: Checks = {}
        work = Path(tempfile.mkdtemp(prefix="eternal-selftest-", dir="/var/tmp"))
        try:
            runs = SCENARIOS[s](model, work, out, checks)
        finally:
            shutil.rmtree(work, ignore_errors=True)
        sid = session_id(runs[0]) if runs else ""
        live.exclude([sid])
        path = transcript(sid)
        report["scenarios"][s.value] = {"session": sid, "claude_code": version(runs[0]) if runs else "",
                                        "checks": checks, "transcript": str(path or "") if keep else stash(path)}
        report["cost_usd"] = round(report["cost_usd"] + sum(map(cost, runs)), 4)
    report["ok"] = bool(scenarios) and all(c["ok"] for v in report["scenarios"].values() for c in v["checks"].values())
    report["ts"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with os.fdopen(os.open(out / LAST, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    return report
