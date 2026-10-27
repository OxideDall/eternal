"""Counts-only probe of Claude Code transcript entry shapes in main sessions."""
import json
import os
import sys
from collections import Counter

ROOT = os.path.expanduser(sys.argv[1])
types, users, systems, blocks, meta_keys, cm_keys, attach = Counter(), Counter(), Counter(), Counter(), Counter(), Counter(), Counter()
for dirpath, _, files in os.walk(ROOT):
    if "/subagents" in dirpath:
        continue
    for name in files:
        if not name.endswith(".jsonl"):
            continue
        with open(os.path.join(dirpath, name), "rb") as f:
            for raw in f:
                try:
                    e = json.loads(raw)
                except ValueError:
                    continue
                t = e.get("type")
                types[t] += 1
                if t == "user":
                    c = (e.get("message") or {}).get("content")
                    tr = isinstance(c, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c)
                    o = e.get("origin")
                    ok = o.get("kind") if isinstance(o, dict) else None
                    text = c if isinstance(c, str) else "".join(b.get("text", "") for b in (c or []) if isinstance(b, dict) and b.get("type") == "text")
                    tag = next((x for x in ("<command-name>", "<local-command-stdout>", "<task-notification>", "<system-reminder>", "<bash-input>", "<bash-stdout>", "<user-prompt-submit-hook>") if text.lstrip().startswith(x)), "")
                    users[(bool(e.get("isMeta")), bool(e.get("isCompactSummary")), ok, e.get("promptSource"), tr, type(c).__name__, tag)] += 1
                elif t == "system":
                    systems[e.get("subtype")] += 1
                    if e.get("subtype") == "compact_boundary":
                        cm_keys.update((e.get("compactMetadata") or {}).keys())
                elif t == "assistant":
                    for b in (e.get("message") or {}).get("content") or []:
                        if isinstance(b, dict):
                            blocks[b.get("type")] += 1
                elif t == "attachment":
                    attach[(e.get("attachment") or {}).get("type")] += 1
                meta_keys.update(k for k in e.keys())
print("types:", types.most_common())
print("system subtypes:", systems.most_common(12))
print("compactMetadata keys:", sorted(cm_keys))
print("assistant blocks:", blocks.most_common())
print("attachments:", attach.most_common(12))
print("user shapes (isMeta, isCompactSummary, origin.kind, promptSource, has_tool_result, content_type, leading_tag): count")
for k, v in users.most_common(30):
    print("  ", k, v)
print("top-level keys:", sorted(k for k, _ in meta_keys.most_common(60)))
