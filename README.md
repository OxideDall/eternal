# eternal

Memory for an "eternal" Claude Code session. The package builds an immutable transcript archive in which code determines the author of every record, and searches it with FTS5. On top of the archive sits a log tree with a UniiChat-style view. Benchmarks run on a frozen corpus. Design and plan: `docs/design.md`.

## Enabling

```sh
cd ~/projects/eternal && claudev   # the hook and the MCP server are picked up from the project
```

- **Shortcut: `eternal`**, a function in `~/.bashrc` that does the same in a subshell, so your shell stays in its directory. Arguments go to claudev: `eternal -c` continues the last eternal session.
- **SessionStart hook** (`.claude/settings.json`) fires after every compaction, manual or automatic, including one in the middle of a tool loop. It puts the registry into context: this session's prompts and written files.
- **MCP `eternal`** (`.mcp.json`) provides `search` and `zoom` over the archive of all sessions on the machine. In a running session, `/mcp` shows its state.
- **Memory** is shared with sessions in `~`: `~/.claude/projects/-home-<user>-projects-eternal/memory` is a symlink to `-home-<user>/memory`. To roll back, `rm` the symlink; then drop the `shared_memory` check from `doctor` and selftest.

```sh
python3 -m eternal doctor     # free: hook and server on synthetic data, trust, approval, memory, live archive, hook log, selftest freshness
python3 -m eternal selftest   # paid (Haiku, about $0.3): a live session here compacts, gets the registry, finds what it wrote via search
python3 -m eternal audit      # whether the registry reached the model after each compaction in this project's sessions
```

- After a Claude Code update, `doctor` stays red until selftest passes on the new version.
- Auto-compaction usually swaps in a summary computed ahead of time (`precomputed` in `audit`). selftest does not cover this path: precomputation does not turn on under `-p`. The registry is injected at the swap here too; check with `audit` after the first such compaction.
- The hook log is `/var/tmp/$USER-eternal-live/hook.jsonl`; the live archive sits next to it, `archive.db`.
- Selftest sessions are moved out of the project into `selftest/` and hidden from search: their ids are written to `excluded`.
- An edit to `eternal/mcp.py` reaches a running session after `/mcp` → reconnect. An edit to the hook takes effect at the next compaction.

## Development

```sh
python3 -m eternal freeze --out baseline/manifest.json         # pin transcript prefixes
ETERNAL_DATA=/var/tmp/$USER-eternal python3 -m eternal ingest --manifest baseline/manifest.json
ETERNAL_DATA=... python3 -m eternal bench --manifest baseline/manifest.json --out baseline/tier1.json --md baseline/tier1.md
ETERNAL_DATA=... python3 -m eternal check --manifest baseline/manifest.json --baseline baseline/tier1.json
ETERNAL_DATA=... python3 -m eternal search "cursor" [--trigram]
python3 -m unittest discover -s tests -t .
```

- **The archive** holds transcript text, and therefore possibly secrets. Masking in `archive.redact` lowers the risk but does not guarantee that no secrets remain. So the data lives outside the repo, in a directory with mode 700. `/var/tmp` is cleaned after 30 days, but the archive can be rebuilt from the manifest.
- **The manifest** stores transcript paths and lengths. JSONL files are append-only, so the corpus is pinned by prefix length and the prefix's sha256. If a file is rewritten, `verify` catches it. The manifest is not in the repo because it lists the author's transcript paths; `freeze` writes your own. `baseline/tier1.*` are numbers for the author's corpus.
- **Ingest** will not start with less than 8 GiB free on the data volume.
- **`legacy/`** holds the original offline replay scripts behind the decay figures 81% → 28% → 19% → 6%. They come from a different population and a different probe; the current baseline is `baseline/tier1.md`.
