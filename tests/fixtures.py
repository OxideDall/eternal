"""Synthetic Claude Code transcript entries for tests."""
import itertools
import json
from pathlib import Path

_n = itertools.count()


def _uid() -> str:
    return f"u{next(_n)}"


def ts(day: int = 1, minute: int = 0) -> str:
    return f"2026-10-{day:02d}T00:{minute:02d}:00Z"


def human(text: str, uuid: str | None = None, **kw) -> dict:
    return {"type": "user", "uuid": uuid or _uid(), "timestamp": ts(), "origin": {"kind": "human"},
            "promptSource": "typed", "message": {"role": "user", "content": text}, **kw}


def user_raw(content, **kw) -> dict:
    return {"type": "user", "uuid": _uid(), "timestamp": ts(), "message": {"role": "user", "content": content}, **kw}


def text(t: str, mid: str, usage: dict | None = None, model: str = "claude-opus-5-5") -> dict:
    return {"type": "assistant", "uuid": _uid(), "timestamp": ts(), "version": "2.1.295",
            "message": {"id": mid, "model": model, "content": [{"type": "text", "text": t}],
                        "usage": usage or {"input_tokens": 1, "cache_read_input_tokens": 100, "output_tokens": 5}}}


def tool_use(name: str, inp: dict, mid: str, tuid: str, usage: dict | None = None) -> dict:
    return {"type": "assistant", "uuid": _uid(), "timestamp": ts(), "version": "2.1.295",
            "message": {"id": mid, "model": "claude-opus-5-5",
                        "content": [{"type": "tool_use", "id": tuid, "name": name, "input": inp}],
                        "usage": usage or {"input_tokens": 1, "cache_read_input_tokens": 100, "output_tokens": 5}}}


def tool_result(tuid: str, body: str, tur: dict | None = None, error: bool = False) -> dict:
    e = user_raw([{"type": "tool_result", "tool_use_id": tuid, "content": body, "is_error": error}])
    if tur:
        e["toolUseResult"] = tur
    return e


def boundary(preserved: tuple[str, ...] = (), trigger: str = "auto", pre: int = 200_000,
             precomputed: bool = False) -> dict:
    return {"type": "system", "subtype": "compact_boundary", "uuid": _uid(), "timestamp": ts(),
            "compactMetadata": {"trigger": trigger, "preTokens": pre, "postTokens": 30_000,
                                "preservedMessages": {"uuids": list(preserved), "anchorUuid": "x"},
                                **({"precomputed": True} if precomputed else {})}}


def summary(t: str) -> dict:
    return user_raw(t, isCompactSummary=True)


def queued(prompt, mode: str = "prompt", origin: str | None = None, meta: bool = False) -> dict:
    a = {"type": "queued_command", "prompt": prompt, "commandMode": mode}
    if origin:
        a["origin"] = {"kind": origin}
    if meta:
        a["isMeta"] = True
    return {"type": "attachment", "uuid": _uid(), "timestamp": ts(), "attachment": a}


def hook_context(text: str, event: str = "SessionStart") -> dict:
    return {"type": "attachment", "uuid": _uid(), "timestamp": ts(), "attachment": {
        "type": "hook_additional_context", "content": [text], "hookName": event, "hookEvent": event, "toolUseID": event}}


def write(path: Path, entries: list[dict], tail: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in entries) + tail)
    return path


def append(path: Path, entries: list[dict]) -> None:
    with open(path, "a") as f:
        f.write("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in entries))
