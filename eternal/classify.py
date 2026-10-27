"""Transcript entry -> archive events. Provenance comes from Claude Code's own fields, not from text."""
import json
import re
from collections.abc import Iterator

from .kinds import Boundary, Event, Kind, Record, Request, Snapshot, Trigger, Via

INTERRUPTS = frozenset({"[Request interrupted by user]", "[Request interrupted by user for tool use]"})
COMMAND_TAGS = ("<command-name>", "<command-message>", "<command-args>", "<bash-input>")
OUTPUT_TAGS = ("<local-command-stdout>", "<local-command-stderr>", "<bash-stdout>", "<bash-stderr>")
HUMAN_SOURCES = frozenset({"typed", "queued", "sdk"})
WRITE_TOOLS = frozenset({"Edit", "Write", "NotebookEdit", "MultiEdit"})
ARG_KEYS = ("file_path", "notebook_path", "subject", "description", "taskId", "task_id", "status",
            "command", "pattern", "path", "url", "query", "prompt", "skill", "subagent_type")
REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)
ARG_CAP = 300
TOOL_USE_CAP = 2_000
RESULT_HEAD, RESULT_TAIL = 1_500, 500


def clip(text: str, cap: int) -> str:
    return text if len(text) <= cap else text[: cap - 1] + "…"


def head_tail(text: str) -> str:
    if len(text) <= RESULT_HEAD + RESULT_TAIL:
        return text
    return text[:RESULT_HEAD] + " … " + text[-RESULT_TAIL:]


def text_of(content) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    out = []
    for b in content:
        if isinstance(b, dict):
            if b.get("type") == "text":
                out.append(b.get("text") or "")
            elif b.get("type") == "image":
                out.append("[image]")
        elif isinstance(b, str):
            out.append(b)
    return "\n".join(out)


def _origin(e: dict) -> str | None:
    o = e.get("origin")
    return o.get("kind") if isinstance(o, dict) else None


def prompt_kind(e: dict, text: str) -> Kind:
    """Kind of a non-tool-result user message from origin, promptSource, isMeta and leading tags."""
    origin, source, s = _origin(e), e.get("promptSource"), text.lstrip()
    if e.get("isMeta"):
        if origin == "peer":
            return Kind.PEER
        return Kind.SCHEDULED if source == "system" else Kind.META
    if origin == "task-notification" or s.startswith("<task-notification>"):
        return Kind.TASK_NOTIFICATION
    if origin == "peer":
        return Kind.PEER
    if text.strip() in INTERRUPTS:
        return Kind.INTERRUPT
    if s.startswith(COMMAND_TAGS):
        return Kind.COMMAND
    if s.startswith(OUTPUT_TAGS):
        return Kind.COMMAND_OUTPUT
    if origin == "human":
        return Kind.HUMAN
    if origin is not None:
        return Kind.META
    if source in HUMAN_SOURCES:
        return Kind.HUMAN
    if source is not None or s.startswith("<"):
        return Kind.META
    return Kind.HUMAN  # versions before origin/promptSource existed


def via(e: dict) -> Via:
    try:
        return Via(e.get("promptSource"))
    except ValueError:
        return Via.TYPED if _origin(e) == "human" else Via.LEGACY


def queued_kind(a: dict, text: str) -> Kind:
    origin = a.get("origin", {}).get("kind") if isinstance(a.get("origin"), dict) else None
    if origin == "peer":
        return Kind.PEER
    if (a.get("commandMode") == "task-notification" or origin == "task-notification"
            or text.lstrip().startswith("<task-notification>")):
        return Kind.TASK_NOTIFICATION
    if a.get("isMeta"):
        return Kind.META
    if a.get("commandMode") == "prompt" and origin in (None, "human"):
        return Kind.HUMAN
    return Kind.META


def tool_args(inp: dict) -> dict:
    return {k: clip(str(inp[k]), ARG_CAP) for k in ARG_KEYS if k in inp and inp[k] not in (None, "")}


def render_tool_use(name: str, args: dict) -> str:
    return clip(f"{name}: " + ", ".join(f"{k}={v}" for k, v in args.items()), TOOL_USE_CAP)


CAPS: dict[Kind, int] = {
    Kind.TASK_NOTIFICATION: 4_000, Kind.COMMAND: 4_000, Kind.COMMAND_OUTPUT: 4_000,
    Kind.META: 2_000, Kind.PEER: 20_000, Kind.SCHEDULED: 4_000,
}  # kinds not listed keep full text; tool results keep head and tail


def _record(line: int, block: int, kind: Kind, text: str, e: dict, **kw) -> Record:
    if kind == Kind.HUMAN:
        text = REMINDER.sub("", text).strip()
        if not text:
            kind = Kind.META
    if kind != Kind.HUMAN:
        kw.pop("via", None)
    if kind == Kind.TOOL_RESULT:
        kept = head_tail(text)
    else:
        kept = clip(text, CAPS[kind]) if kind in CAPS else text
    return Record(line, block, kind, kept, len(text), e.get("uuid"), e.get("timestamp"), **kw)


def _user(e: dict, line: int) -> Iterator[Record]:
    content = (e.get("message") or {}).get("content")
    if e.get("isCompactSummary"):
        yield _record(line, 0, Kind.COMPACT_SUMMARY, text_of(content), e)
        return
    results = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_result"] if isinstance(content, list) else []
    if results:
        tur = e.get("toolUseResult")
        task = tur.get("task") if isinstance(tur, dict) else None
        task_id = str(task["id"]) if isinstance(task, dict) and task.get("id") is not None and len(results) == 1 else None
        for k, b in enumerate(results):
            body = text_of(b.get("content"))
            if b.get("is_error"):
                body = "error: " + body
            yield _record(line, k, Kind.TOOL_RESULT, body, e, tool_use_id=b.get("tool_use_id"),
                          args={"task_id": task_id} if task_id else None)
        return
    text = text_of(content)
    if text.strip():
        yield _record(line, 0, prompt_kind(e, text), text, e, via=via(e))


def _assistant(e: dict, line: int) -> Iterator[Event]:
    m = e.get("message") or {}
    synthetic = m.get("model") == "<synthetic>"
    for k, b in enumerate(m.get("content") or []):
        if not isinstance(b, dict):
            continue
        if b.get("type") == "text" and (b.get("text") or "").strip():
            yield _record(line, k, Kind.META if synthetic else Kind.ASSISTANT_TEXT, b["text"], e)
        elif b.get("type") == "tool_use":
            name = b.get("name") or "?"
            args = tool_args(b.get("input") or {})
            yield _record(line, k, Kind.TOOL_USE, render_tool_use(name, args), e, tool=name,
                          tool_use_id=b.get("id"), args=args)
    u = m.get("usage")
    if isinstance(u, dict) and m.get("id") and not synthetic:
        yield Request(line, m["id"], e.get("timestamp"), m.get("model"), e.get("version"),
                      u.get("input_tokens") or 0, u.get("cache_creation_input_tokens") or 0,
                      u.get("cache_read_input_tokens") or 0, u.get("output_tokens") or 0)


def _boundary(e: dict, line: int) -> Boundary:
    md = e.get("compactMetadata") or {}
    pm = md.get("preservedMessages") or {}
    uuids = tuple(dict.fromkeys([*(pm.get("uuids") or []), *(pm.get("allUuids") or [])]))
    try:
        trigger = Trigger(md.get("trigger"))
    except ValueError:
        trigger = Trigger.UNKNOWN
    pre, post = md.get("preTokens"), md.get("postTokens")
    return Boundary(line, e.get("uuid"), e.get("timestamp"), trigger,
                    pre if isinstance(pre, int) else None, post if isinstance(post, int) else None, uuids,
                    e.get("version"))


def _size(x) -> int:
    if isinstance(x, str):
        return len(x)
    return len(json.dumps(x, ensure_ascii=False)) if x is not None else 0


def classify(e: dict, line: int) -> Iterator[Event]:
    """Events carried by one transcript entry, in block order."""
    t = e.get("type")
    if t == "user":
        yield from _user(e, line)
    elif t == "assistant":
        yield from _assistant(e, line)
    elif t == "system":
        if e.get("subtype") == "compact_boundary":
            yield _boundary(e, line)
        elif e.get("subtype") == "away_summary" and isinstance(e.get("content"), str):
            yield _record(line, 0, Kind.AWAY_SUMMARY, e["content"], e)
    elif t == "attachment":
        a = e.get("attachment") or {}
        if a.get("type") == "queued_command":
            text = text_of(a.get("prompt"))
            if text.strip():
                yield _record(line, 0, queued_kind(a, text), text, e, via=Via.ATTACHMENT)
        elif a.get("type") == "prompt_snapshot":
            sp, tools = a.get("systemPrompt"), a.get("tools")
            sys_chars = sum(_size(x) for x in sp) if isinstance(sp, list) else _size(sp)
            yield Snapshot(line, e.get("timestamp"), sys_chars, _size(tools) if tools else 0, len(tools) if isinstance(tools, list) else 0)
