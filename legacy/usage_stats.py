"""Per-request context size and cache shares from Claude Code transcript usage fields (deduped by message id)."""
import json
import os
import sys
from collections import defaultdict

ROOT = os.path.expanduser(sys.argv[1])


def pct(xs: list, q: float) -> int:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))] if xs else 0


ctx = []  # total input tokens per request, main sessions
tot = defaultdict(int)
models = defaultdict(int)
per_session = {}
for dirpath, _, files in os.walk(ROOT):
    if "/subagents" in dirpath:
        continue
    for name in files:
        if not name.endswith(".jsonl"):
            continue
        seen = set()
        s = defaultdict(int)
        with open(os.path.join(dirpath, name), "rb") as f:
            for raw in f:
                try:
                    e = json.loads(raw)
                except ValueError:
                    continue
                if e.get("type") != "assistant":
                    continue
                m = e.get("message") or {}
                key = m.get("id") or e.get("requestId")
                u = m.get("usage")
                if not key or not isinstance(u, dict) or key in seen:
                    continue
                seen.add(key)
                inp = u.get("input_tokens") or 0
                cw = u.get("cache_creation_input_tokens") or 0
                cr = u.get("cache_read_input_tokens") or 0
                out = u.get("output_tokens") or 0
                if inp + cw + cr == 0:
                    continue
                ctx.append(inp + cw + cr)
                for k, v in (("input", inp), ("write", cw), ("read", cr), ("output", out), ("requests", 1)):
                    tot[k] += v
                    s[k] += v
                models[m.get("model") or "?"] += 1
        if s["requests"]:
            per_session[name[:8]] = s

allin = tot["input"] + tot["write"] + tot["read"]
print(f"main requests {tot['requests']}, input tokens {allin / 1e9:.2f}B: read {100 * tot['read'] / allin:.1f}%, write {100 * tot['write'] / allin:.1f}%, uncached {100 * tot['input'] / allin:.1f}%; output {tot['output'] / 1e6:.1f}M")
print(f"context per request: p10 {pct(ctx, .1)} p50 {pct(ctx, .5)} p90 {pct(ctx, .9)} max {max(ctx, default=0)}")
cost = (tot["read"] * 0.1 + tot["write"] * 1.25 + tot["input"]) / allin
print(f"input cost vs uncached (5-min write price): x{cost:.3f}")
print("models:", ", ".join(f"{k} {v}" for k, v in sorted(models.items(), key=lambda kv: -kv[1])[:6]))
for sid in ("641b7769", "5112c94b", "b722fba6", "11353283"):
    s = per_session.get(sid)
    if s:
        a = s["input"] + s["write"] + s["read"]
        print(f"  {sid}: {s['requests']} req, avg context {a // s['requests']} tok, read {100 * s['read'] / a:.1f}%, write {100 * s['write'] / a:.1f}%")
