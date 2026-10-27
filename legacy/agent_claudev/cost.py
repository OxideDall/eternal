# Read-only: time state_guard-style full-transcript passes on the largest top-level transcript.
import json, os, glob, time
ROOT = os.path.expanduser("~/.claude/projects")
p = max(glob.glob(f"{ROOT}/*/*.jsonl"), key=os.path.getsize)
t0 = time.monotonic(); n = 0
with open(p, encoding="utf-8", errors="replace") as fh:
    for line in fh:
        try: json.loads(line); n += 1
        except ValueError: pass
t1 = time.monotonic()
with open(p, encoding="utf-8", errors="replace") as fh:
    c = sum(1 for line in fh if '"isCompactSummary":true' in line.replace(" ", ""))
t2 = time.monotonic()
print(f"size={os.path.getsize(p)/1e6:.1f}MB lines={n} parse_all={t1-t0:.1f}s scan_summaries_pass={t2-t1:.1f}s summaries={c}")
