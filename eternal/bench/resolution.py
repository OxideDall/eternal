"""B2/B4: a log-tree view over real sessions with a model-free summarizer: resolution by age, prefix stability."""
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from ..kinds import AUTHOR, CONTENT_KINDS
from ..tree import Tree, Truncate, View, span
from .replay import quantiles
from .timeline import Timeline

CHARS_PER_TOKEN = 2.05  # p50 over 392 Cyrillic-heavy text-only replies (legacy/shape_probe2.py)
NODE = 256  # chars; UniiChat's 512 bytes of Cyrillic
HIGH = 64_000  # chars; UniiChat's 128 KB of Cyrillic


class Age(StrEnum):
    """Age of a view line's newest message, in messages before the view's end."""

    UNDER_100 = "<100"
    TO_1K = "100-1K"
    TO_10K = "1K-10K"
    OVER_10K = ">=10K"


AGES = ((0, Age.UNDER_100), (100, Age.TO_1K), (1_000, Age.TO_10K), (10_000, Age.OVER_10K))
CHECK_EVERY = 500
MIN_LEAVES = 1_000


def leaves(tl: Timeline) -> list[str]:
    return [f"{AUTHOR[r.kind].value}: {r.text}" for r in tl.rows if r.kind in CONTENT_KINDS]


def age_bucket(age: int) -> Age:
    out = AGES[0][1]
    for low, a in AGES:
        if age >= low:
            out = a
    return out


@dataclass
class Sim:
    messages: int
    batches: int
    cells: dict[Age, list[int]]  # lines, messages, chars
    sizes: list[int]
    kept: list[float]


def simulate(texts: list[str], node: int = NODE, high: int = HIGH) -> Sim:
    tree, view = Tree(Truncate(node)), View(high)
    cells: dict[Age, list[int]] = defaultdict(lambda: [0, 0, 0])
    kept, sizes = [], []
    for n, text in enumerate(texts, 1):
        tree.append(text)
        view.reach(tree, n)
        before, size = list(view.parts), view.size
        if view.fit(tree):
            common = 0
            for p, q in zip(before, view.parts):
                if p != q:
                    break
                common += view.line(tree, p)
            kept.append(common / size)
        if n % CHECK_EVERY == 0 or n == len(texts):
            sizes.append(view.size)
            t = view.end()
            for part in view.parts:
                s, e = span(part)
                c = cells[age_bucket(t - 1 - e)]
                c[0] += 1
                c[1] += e - s + 1
                c[2] += len(tree.text(part))
    return Sim(len(texts), view.batches, dict(cells), sizes, kept)


def run(tls: Iterable[Timeline], node: int = NODE, high: int = HIGH) -> dict:
    cells: dict[Age, list[int]] = defaultdict(lambda: [0, 0, 0])
    sizes, kept, msgs, batches, sessions, largest = [], [], 0, 0, 0, None
    for tl in tls:
        texts = leaves(tl)
        if len(texts) < MIN_LEAVES:
            continue
        r = simulate(texts, node, high)
        sessions += 1
        msgs += r.messages
        batches += r.batches
        sizes += r.sizes
        kept += r.kept
        for a, v in r.cells.items():
            for j in range(3):
                cells[a][j] += v[j]
        if largest is None or r.messages > largest["messages"]:
            largest = {"session": tl.session[:8], "messages": r.messages, "batches": r.batches,
                       "cells": {a.value: v for a, v in r.cells.items()}}
    by_age = {a.value: {"lines": c[0], "msgs_per_line": round(c[1] / c[0], 1), "chars_per_msg": round(c[2] / c[1], 2)}
              for _, a in AGES if (c := cells.get(a)) and c[0]}
    return {
        "config": {"node_chars": node, "high_chars": high, "chars_per_token": CHARS_PER_TOKEN, "summarizer": "truncate"},
        "sessions": sessions, "messages": msgs,
        "batches_per_1k_msgs": round(1000 * batches / msgs, 2) if msgs else None,
        "by_age": by_age,
        "view_chars": quantiles(sizes),
        "view_tokens_est": {k: round(v / CHARS_PER_TOKEN) if k != "n" else v for k, v in quantiles(sizes).items()},
        "prefix_kept_per_batch": {k: round(v, 3) if k != "n" else v for k, v in quantiles(kept).items()},
        "largest": largest,
    }
