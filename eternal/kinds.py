"""Closed vocabularies and record shapes shared by the archive, the tree and the benches."""
from dataclasses import dataclass
from enum import StrEnum


class Kind(StrEnum):
    """What a record is; assigned by code from transcript fields, never by a model."""

    HUMAN = "human"
    COMMAND = "command"
    COMMAND_OUTPUT = "command_output"
    INTERRUPT = "interrupt"
    TASK_NOTIFICATION = "task_notification"
    PEER = "peer"
    SCHEDULED = "scheduled"
    META = "meta"
    COMPACT_SUMMARY = "compact_summary"
    AWAY_SUMMARY = "away_summary"
    ASSISTANT_TEXT = "assistant_text"
    TOOL_USE = "tool_use"
    TOOL_RESULT = "tool_result"


class Author(StrEnum):
    USER = "user"
    MODEL = "model"
    TOOL = "tool"
    SYSTEM = "system"
    PEER = "peer"


AUTHOR: dict[Kind, Author] = {
    Kind.HUMAN: Author.USER,
    Kind.COMMAND: Author.USER,
    Kind.INTERRUPT: Author.USER,
    Kind.COMMAND_OUTPUT: Author.TOOL,
    Kind.TASK_NOTIFICATION: Author.SYSTEM,
    Kind.PEER: Author.PEER,
    Kind.SCHEDULED: Author.SYSTEM,
    Kind.META: Author.SYSTEM,
    Kind.COMPACT_SUMMARY: Author.MODEL,
    Kind.AWAY_SUMMARY: Author.MODEL,
    Kind.ASSISTANT_TEXT: Author.MODEL,
    Kind.TOOL_USE: Author.MODEL,
    Kind.TOOL_RESULT: Author.TOOL,
}

# Kinds that carry conversation content: indexed for search and fed to the tree.
CONTENT_KINDS = frozenset({
    Kind.HUMAN, Kind.COMMAND, Kind.TASK_NOTIFICATION, Kind.PEER, Kind.SCHEDULED,
    Kind.ASSISTANT_TEXT, Kind.TOOL_USE, Kind.TOOL_RESULT, Kind.AWAY_SUMMARY,
})
INDEXED_KINDS = CONTENT_KINDS | {Kind.COMPACT_SUMMARY}
TURN_STARTS = frozenset({Kind.HUMAN, Kind.TASK_NOTIFICATION, Kind.PEER, Kind.SCHEDULED})


class Via(StrEnum):
    """How a human prompt reached the transcript."""

    TYPED = "typed"
    QUEUED = "queued"
    SDK = "sdk"
    ATTACHMENT = "attachment"  # queued_command attachment
    LEGACY = "legacy"  # versions without origin/promptSource


class Trigger(StrEnum):
    AUTO = "auto"
    MANUAL = "manual"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class Record:
    line: int
    block: int
    kind: Kind
    text: str
    chars: int  # length before capping
    uuid: str | None
    ts: str | None
    tool: str | None = None
    tool_use_id: str | None = None
    args: dict | None = None
    via: Via | None = None


@dataclass(frozen=True, slots=True)
class Boundary:
    line: int
    uuid: str | None
    ts: str | None
    trigger: Trigger
    pre_tokens: int | None
    post_tokens: int | None
    preserved: tuple[str, ...]
    version: str | None = None


@dataclass(frozen=True, slots=True)
class Request:
    """One model request's usage, deduplicated by message id."""

    line: int
    msg_id: str
    ts: str | None
    model: str | None
    version: str | None
    input: int
    cache_write: int
    cache_read: int
    output: int

    @property
    def context(self) -> int:
        return self.input + self.cache_write + self.cache_read


@dataclass(frozen=True, slots=True)
class Snapshot:
    """Size of the system prompt and tool schemas Claude Code recorded for a request."""

    line: int
    ts: str | None
    system_chars: int
    tools_chars: int
    tools: int


Event = Record | Boundary | Request | Snapshot
