"""B3: Claude Code's own context per request (start prefix, within-turn growth, cache) vs a bounded view."""
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from enum import IntEnum

from ..kinds import TURN_STARTS, Kind
from .replay import quantiles, recent_cutoff, version_bucket, version_order
from .resolution import CHARS_PER_TOKEN, HIGH


class Mark(IntEnum):
    HUMAN = 0
    PROMPT = 1  # a non-human message that starts a turn
    CUT = 2


READ, WRITE = 0.1, 1.25  # cache price multipliers vs uncached input


@dataclass
class Tokens:
    input: int = 0
    write: int = 0
    read: int = 0
    output: int = 0


def run(db: sqlite3.Connection, created: str, view_chars: tuple[int, int] = (HIGH // 2, HIGH)) -> dict:
    cutoff = recent_cutoff(created)
    first_by_ver: dict[str, list[int]] = defaultdict(list)
    first_recent, ctx_all, ctx_recent, growth_recent, pre_recent = [], [], [], [], []
    tot = Tokens()
    for (sid,) in db.execute("SELECT id FROM source WHERE sub = 0"):
        marks = sorted([(line, Mark.HUMAN if kind == Kind.HUMAN.value else Mark.PROMPT) for line, kind in db.execute(
            f"SELECT line, kind FROM record WHERE source_id = ? AND kind IN ({','.join('?' * len(TURN_STARTS))})",
            (sid, *[k.value for k in TURN_STARTS]))] +
            [(line, Mark.CUT) for (line,) in db.execute("SELECT line FROM boundary WHERE source_id = ?", (sid,))])
        reqs = list(db.execute("SELECT line, ts, version, input, cache_write, cache_read, output FROM request "
                               "WHERE source_id = ? ORDER BY line", (sid,)))
        k, base, first = 0, None, True
        for line, ts, ver, inp, cw, cr, out in reqs:
            ctx = inp + cw + cr
            if not ctx:
                continue
            new_turn = False
            humans_before = 0
            cut_before = False
            while k < len(marks) and marks[k][0] < line:
                new_turn = True
                humans_before += marks[k][1] == Mark.HUMAN
                cut_before |= marks[k][1] == Mark.CUT
                k += 1
            if first:
                if humans_before == 1 and not cut_before:
                    first_by_ver[version_bucket(ver)].append(ctx)
                    if (ts or "") >= cutoff:
                        first_recent.append(ctx)
                first = False
            if new_turn or base is None:
                base = ctx
            ctx_all.append(ctx)
            tot.input += inp
            tot.write += cw
            tot.read += cr
            tot.output += out
            if (ts or "") >= cutoff:
                ctx_recent.append(ctx)
                growth_recent.append(max(0, ctx - base))
    for (pre,) in db.execute("SELECT b.pre_tokens FROM boundary b JOIN source s ON s.id = b.source_id "
                             "WHERE s.sub = 0 AND b.pre_tokens IS NOT NULL AND b.ts >= ?", (cutoff,)):
        pre_recent.append(pre)
    snaps = list(db.execute("SELECT system_chars, tools_chars, tools FROM snapshot WHERE ts >= ?", (cutoff,)))
    allin = tot.input + tot.write + tot.read
    start = quantiles(first_recent)
    est = {}
    for v in view_chars:
        vt = round(v / CHARS_PER_TOKEN)
        xs = [start.get("p50", 0) + vt + g for g in growth_recent]
        q = quantiles(xs)
        est[f"view_{v}_chars"] = {"view_tokens": vt, "context": q,
                                  "ratio_p50": round(quantiles(ctx_recent)["p50"] / q["p50"], 2) if xs else None}
    return {
        "recent_since": cutoff[:10],
        "start_context_by_version": {b: quantiles(first_by_ver[b]) for b in sorted(first_by_ver, key=version_order)},
        "start_context_recent": start,
        "prompt_snapshot_recent": {"system_chars": quantiles([s[0] for s in snaps]),
                                   "tools_chars": quantiles([s[1] for s in snaps if s[1]]),
                                   "tools": quantiles([s[2] for s in snaps if s[2]])},
        "context_all": quantiles(ctx_all),
        "context_recent": quantiles(ctx_recent),
        "within_turn_growth_recent": quantiles(growth_recent),
        "pre_compact_tokens_recent": quantiles(pre_recent),
        "cache": {"requests": len(ctx_all), "input_tokens": allin,
                  "read_share": round(tot.read / allin, 4) if allin else None,
                  "write_share": round(tot.write / allin, 4) if allin else None,
                  "input_cost_vs_uncached": round((tot.read * READ + tot.write * WRITE + tot.input) / allin, 3)
                  if allin else None, "output_tokens": tot.output},
        "bounded_view_estimate": est,
    }
