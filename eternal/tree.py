"""Log-resolution memory: a binary tree of line summaries and a view whose resolution falls with age.

Port of UniiChat's Memory.merge/reach with limits in characters instead of UTF-8 bytes.
"""
import heapq
import re
from dataclasses import dataclass, field
from typing import Protocol

Part = tuple[int, int]  # (level, index): covers messages [index << level, (index + 1) << level)
SPACE = re.compile(r"\s+")


def flat(text: str) -> str:
    return SPACE.sub(" ", text).strip()


def clip(text: str, cap: int) -> str:
    return text if len(text) <= cap else text[: max(cap - 1, 0)] + "…"


def span(part: Part) -> tuple[int, int]:
    l, i = part
    return i << l, ((i + 1) << l) - 1


class Summarizer(Protocol):
    def leaf(self, text: str) -> str: ...

    def merge(self, left: str, right: str) -> str: ...


@dataclass(frozen=True, slots=True)
class Truncate:
    """Model-free summarizer: clips leaves, splices clipped halves of two children."""

    node: int = 256

    def leaf(self, text: str) -> str:
        return clip(flat(text), self.node)

    def merge(self, left: str, right: str) -> str:
        half = (self.node - 3) // 2
        return clip(left, half) + " | " + clip(right, half)


class Tree:
    """Leaves are messages; a parent is built as soon as both children exist."""

    def __init__(self, summarizer: Summarizer):
        self.summarizer = summarizer
        self.levels: list[list[str]] = [[]]

    @property
    def count(self) -> int:
        return len(self.levels[0])

    def append(self, text: str) -> None:
        lv = self.levels
        lv[0].append(self.summarizer.leaf(text))
        l, i = 0, len(lv[0]) - 1
        while i % 2 == 1:
            if len(lv) == l + 1:
                lv.append([])
            lv[l + 1].append(self.summarizer.merge(lv[l][i - 1], lv[l][i]))
            l, i = l + 1, i // 2

    def built(self, l: int, i: int) -> bool:
        return l < len(self.levels) and i < len(self.levels[l])

    def text(self, part: Part) -> str:
        return self.levels[part[0]][part[1]]


@dataclass
class View:
    """Ordered cover of messages [0, end); past `high` chars a batch merges it down to `high / 2`."""

    high: int
    head: int = 24  # per-line overhead for ids and date
    parts: list[Part] = field(default_factory=list)
    size: int = 0
    batches: int = 0

    def line(self, tree: Tree, part: Part) -> int:
        return len(tree.text(part)) + self.head

    def end(self) -> int:
        return span(self.parts[-1])[1] + 1 if self.parts else 0

    def reach(self, tree: Tree, end: int) -> None:
        for i in range(self.end(), end):
            self.parts.append((0, i))
            self.size += self.line(tree, (0, i))

    def fit(self, tree: Tree) -> bool:
        if self.size <= self.high:
            return False
        self.parts, self.size = merge(tree, self, self.high // 2)
        self.batches += 1
        return True


def merge(tree: Tree, view: View, low: int) -> tuple[list[Part], int]:
    """Most due pairs merged until size <= low; due = (T + 1) / 2**l - i, ties to the older pair."""
    t = view.end()
    parts: list[Part | None] = list(view.parts)
    n = len(parts)
    nxt, prv = list(range(1, n + 1)), list(range(-1, n - 1))
    heap: list[tuple[float, int, Part, Part]] = []

    def offer(k: int) -> None:
        if k < 0 or nxt[k] >= n:
            return
        a, b = parts[k], parts[nxt[k]]
        if a is None or b is None or a[1] % 2 or b[0] != a[0] or b[1] != a[1] + 1 or not tree.built(a[0] + 1, a[1] // 2):
            return
        heapq.heappush(heap, (-((t + 1) / 2 ** a[0] - a[1]), k, a, b))

    size = view.size
    for k in range(n):
        offer(k)
    while heap and size > low:
        _, k, a, b = heapq.heappop(heap)
        j = nxt[k]
        if j >= n or parts[k] != a or parts[j] != b:
            continue
        up = (a[0] + 1, a[1] // 2)
        size += view.line(tree, up) - view.line(tree, a) - view.line(tree, b)
        parts[k], parts[j] = up, None
        nxt[k] = nxt[j]
        if nxt[j] < n:
            prv[nxt[j]] = k
        offer(prv[k])
        offer(k)
    return [p for p in parts if p is not None], size
