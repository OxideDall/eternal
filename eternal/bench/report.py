"""Markdown rendering of a Tier-1 JSON report (counts only, no transcript content)."""
from ..kinds import Via
from ..registry import Policy
from .replay import Cell, Column, FactKind, Slice, Wake

SHOWN = ("0", "1", "5", "10")
COLUMNS = {
    Column.SUMMARY: "summary+preserved",
    Column.NEWEST: "+ newest",
    Column.LOG: "+ log",
    Column.NEWEST_MISSING: "+ newest from the missing",
    Column.LOG_MISSING: "+ log from the missing",
    Column.FITS: "whole list fits the budget",
}
POLICY_NAMES = {Policy.LOG: "log", Policy.NEWEST: "newest"}
WAKES = {
    Wake.HUMAN: "the hook fires only on a human prompt",
    Wake.TURN: "the hook fires at any turn start (human, notification, peer, scheduled)",
}


def _pct(a: int, b: int) -> str:
    return f"{100 * a / b:.0f}%" if b else "—"


def _q(q: dict, scale: float = 1, unit: str = "") -> str:
    if not q or not q.get("n"):
        return "—"
    f = lambda k: f"{q[k] / scale:,.0f}{unit}".replace(",", " ")
    return f"p10 {f('p10')} · p50 {f('p50')} · p90 {f('p90')} (n={q['n']})"


def _sliced(title: str, rows: list[tuple[str, dict]]) -> list[str]:
    cell = lambda t: f"{_pct(t['summary'], t['total'])} ({t['total']}; {t['sessions']} sess.)" if t else "—"
    out = [f"\n{title} — share in the summary, with prompts and sessions in parentheses:\n",
           "| slice | " + " | ".join(f"+{g}" for g in SHOWN) + " |\n|---|" + "---|" * len(SHOWN)]
    out += [f"| {name} | " + " | ".join(cell(d.get(g)) for g in SHOWN) + " |" for name, d in rows if d]
    return out


def markdown(r: dict) -> str:
    c, b1, b2, b3 = r["corpus"], r["b1"], r["b2"], r["b3"]
    ret, cov = b1["retention"], b1["coverage"]
    out = [f"# Tier-1: baseline\n\nManifest `{r['manifest'][:12]}`, frozen {r['created']}. Counts only.\n",
           "## Corpus\n",
           f"Main transcripts {c['files']}, records {c['records']}, compactions {c['boundaries']} "
           f"(without a summary {b1['cuts_without_summary']}), requests {c['requests']}; secret masks {c.get('redacted', '—')}.\n",
           "| record kind | records |\n|---|---|"]
    out += [f"| {k} | {v} |" for k, v in sorted(c["kinds"].items(), key=lambda kv: -kv[1])]
    out.append("\n## B1. Prompts across summary generations\n")
    out.append(f"Sessions with compactions {b1['sessions']}, compactions {b1['cuts']}, recent among them {b1['cuts_recent']}. "
               "The probe is the first 60 characters of a human prompt of length ≥ 20 without embedded IDE context "
               "(`<ide_opened_file>`, `<ide_selection>`), verbatim after folding whitespace and "
               "case. \"Per-session mean\" is the share in the summary computed for each session and averaged.\n")
    out.append("| generation | prompts | sessions | in summary | per-session mean | in summary or preserved | "
               "of hits, in \"All user messages\" |\n|---|---|---|---|---|---|---|")
    for g, t in ret.get(Slice(FactKind.REQUEST).key(), {}).items():
        out.append(f"| +{g} | {t['total']} | {t['sessions']} | {_pct(t['summary'], t['total'])} | "
                   f"{100 * t['macro']:.0f}% | {_pct(t['kept'], t['total'])} | {_pct(t['aum'], t['summary'])} |")
    out += _sliced("By date and Claude Code version at compaction",
                   [(f"recent (since {b3['recent_since']})", ret.get(Slice(FactKind.REQUEST, recent=True).key(), {}))] +
                   [(f"version {v}", ret.get(Slice(FactKind.REQUEST, version=v).key(), {})) for v in b1["versions"]])
    out += _sliced("By prompt delivery path",
                   [(v.value, ret.get(Slice(FactKind.REQUEST, via=v).key(), {})) for v in Via])
    for kind, title in ((FactKind.FILE, "Written files (basename)"),
                        (FactKind.TASK, "Open Task* tasks (they also live outside the context: TaskList, reminders)")):
        rows = ret.get(Slice(kind).key(), {})
        if rows:
            out.append(f"\n{title}:\n\n| generation | facts | in summary | or preserved |\n|---|---|---|---|")
            out += [f"| +{g} | {t['total']} | {_pct(t['summary'], t['total'])} | {_pct(t['kept'], t['total'])} |"
                    for g, t in rows.items()]
    reg = b1["registry"]
    out.append("\n## B1. Coverage with the registry after each compaction\n\n"
               "Same population as in the retention table. The registry is rendered the way the SessionStart hook injects it: "
               f"a line \"L<line> MM-DD text\" of up to {reg['entry']} characters"
               + (f", a prompt carries the suffix \"→ files written in response to it\" of up to {reg['actions']} characters"
                  if reg.get("actions") else "") +
               f", a budget of {reg['budgets']['request']} characters "
               f"for prompts and {reg['budgets']['file']} for files, at most {reg['limit']} in total. \"Log\" means equal quotas "
               "per octave of age, \"newest\" means the most recent up to the budget; the hook uses "
               f"\"{POLICY_NAMES[Policy(reg['policy']['request'])]}\" for prompts and \"{POLICY_NAMES[Policy(reg['policy']['file'])]}\" for files. "
               "\"From the missing\" selects only among "
               "facts absent from this summary and from preserved. The last column is the share of facts at compactions "
               "whose whole list fits the budget.")
    for kind, label in ((FactKind.REQUEST, "Prompts"), (FactKind.FILE, "Written files")):
        for recent, title in ((False, "all compactions"), (True, f"recent compactions (since {b3['recent_since']})")):
            base = cov.get(Cell(Column.SUMMARY, recent, kind).key())
            if not base:
                continue
            out.append(f"\n{label}, {title}:\n\n| generation | n | " + " | ".join(COLUMNS.values()) + " |\n|---|---|" +
                       "---|" * len(COLUMNS))
            for g in base:
                cells = [cov[Cell(col, recent, kind).key()][g] for col in COLUMNS]
                out.append(f"| +{g} | {cells[0][1]} | " + " | ".join(_pct(a, b) for a, b in cells) + " |")
    out.append("\n## B1. Delivery via UserPromptSubmit after auto-compaction\n\n"
               "\"Overwritten\" means the next compaction came before the hook had a reason to fire. Whether "
               "UserPromptSubmit fires on notifications and peer messages is not visible in the transcript, so both variants are given.\n")
    for w in Wake:
        d = b1["delivery"][w.value]
        out.append(f"- {WAKES[w]}: auto-compactions {d['auto_cuts']}, {d['fate']}; tool calls before delivery "
                   f"{_q(d['lag_tool_calls'])}.")
    s = b1["index_check"]
    out.append(f"\n## B1. Index check: a query built from the lost prompt's own words\n\n"
               f"Checks that FTS finds a prompt by its own words. This is not model recall: questions in other words "
               f"are measured by B5 (Tier-2). Lost after one generation: {s['lost_after_1']}, sampled {s['sampled']}; "
               f"top-{s['k']} among human messages of all sessions.\n\n| query | found in top-k |\n|---|---|")
    out += [f"| {q} | {_pct(h, n)} ({h}/{n}) |" for q, (h, n) in s["recall"].items()]
    cfg = b2["config"]
    out.append(f"\n## B2. Log tree: view resolution by age (summarizer = truncation)\n\n"
               f"Node {cfg['node_chars']} characters, view sawtooth {cfg['high_chars'] // 2}–{cfg['high_chars']} characters; "
               f"sessions with ≥1000 messages {b2['sessions']}, messages {b2['messages']}; "
               f"batches per 1K messages {b2['batches_per_1k_msgs']}.\n")
    out.append("| age, messages | lines measured | messages per line | characters per message |\n"
               "|---|---|---|---|")
    out += [f"| {a} | {v['lines']} | {v['msgs_per_line']} | {v['chars_per_msg']} |" for a, v in b2["by_age"].items()]
    out.append(f"\nView size, estimated in tokens: {_q(b2['view_tokens_est'])}. "
               f"Byte prefix kept across a batch: {_q(b2['prefix_kept_per_batch'], 0.01, '%')}.\n")
    out.append(f"## B3. Claude Code context (recent = since {b3['recent_since']})\n")
    out.append("| measure | tokens |\n|---|---|")
    out.append(f"| first request of a fresh session, recent | {_q(b3['start_context_recent'])} |")
    for v, q in b3["start_context_by_version"].items():
        out.append(f"| first request, version {v} | {_q(q)} |")
    out.append(f"| every request, all time | {_q(b3['context_all'])} |")
    out.append(f"| every request, recent | {_q(b3['context_recent'])} |")
    out.append(f"| growth within a turn, recent | {_q(b3['within_turn_growth_recent'])} |")
    out.append(f"| context at compaction, recent | {_q(b3['pre_compact_tokens_recent'])} |")
    for e in b3["bounded_view_estimate"].values():
        out.append(f"| start + view ({e['view_tokens']} tok.) + turn growth | {_q(e['context'])}; "
                   f"actual p50 / estimate p50 = {e['ratio_p50']} |")
    ps, ch = b3["prompt_snapshot_recent"], b3["cache"]
    out.append(f"\nprompt_snapshot, recent: system prompt, characters {_q(ps['system_chars'])}; tool schemas, "
               f"characters {_q(ps['tools_chars'])}; tools {_q(ps['tools'])}.\n")
    tokens = f"{ch['input_tokens']:,}".replace(",", " ")
    out.append(f"Cache: read {ch['read_share']}, write {ch['write_share']} of {tokens} input tokens; "
               f"input cost ×{ch['input_cost_vs_uncached']} of uncached.\n")
    return "\n".join(out) + "\n"
