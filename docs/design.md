# Eternal session: merge design

The goal is a Claude Code session with no end of life: the process never dies, the context stays bounded in size and cost, raw material is never lost, and recall of old material is measured by benchmarks.

## What we take and from where

| component | source | what exactly | why |
|---|---|---|---|
| Process survival | claudev | session registry, `cv-<sid8>` holders in tmux, restore | already works; needs a cgroup fix (slice 9) |
| Immutable archive with its own ids | Klavdia | autoincrement instead of external ids, dedup, durable background queue | Klavdia's cursor over external ids overflowed INT4 |
| Provenance by code | Klavdia (permissions in code), the UniiChat verdict | record author from `origin`, `promptSource`, `isMeta`, `queued_command` (`eternal/classify.py`) | the model never restates the author label, hence protection against poisoning |
| Log tree and view | UniiChat | `merge` with priority `(T+1)/2^l − i`, immutable nodes, view sawtooth (`eternal/tree.py`) | context stays bounded for any history length |
| ids and dates in view lines, line expansion | Klavdia | a view line carries an id range and a date; `zoom(id, n)` | UniiChat's view has no dates |
| Search | OptMem, the UniiChat verdict | FTS5 `unicode61` + `trigram` over the archive | without search, the threads back to old material decay. Index check: a lost prompt is found by its own words in the top-5 in 99% (phrase) and 100% (trigram) of cases. This is not model recall; questions in other words are measured by B5 |
| Fact registry across epochs | claudevs v2 | prompts and written files from all epochs, selection within a budget (`eternal/registry.py`, the same code for the hook and the bench) | on compactions by versions 2.1.250+, the summary keeps 53% of prompts after 1 generation, 30% after 5, 23% after 10; on recent compactions the registry gives 100% of prompts up to +5 and 87% at +10, and 99–100% of files up to +5 and 82% at +10 |
| Delivery without delay | Claude Code hooks | SessionStart(`compact`), up to 10,000 characters (`eternal/hook.py`) | UserPromptSubmit (the claudevs approach) is too late: the next compaction overtakes 120 to 422 of 603 auto-compactions (20–70%), and delivery lag p50 is 14–22 tool calls. SessionStart(`compact`) is verified live: the registry reaches the very first request after both manual and auto-compaction |
| Anchors for long runs | a private project | facts are pinned early, then N compactions, then all anchors are asked again; an UNCLEAR answer counts as a failure | Tier-2 (B6) |
| Stable prefix | UniiChat | nothing volatile at the start of the prompt | 96.7% of input is read from cache today |

## What we leave out

- Tiers that overwrite: Klavdia's L2. Old material is lost irreversibly.
- A cursor over external ids, role by name, API errors as utterances (Klavdia). A window of recent messages without authors.
- A ban on search, and limits in UTF-8 bytes (UniiChat). Russian prose runs at ≈2 characters per token, so limits are set in characters and converted to tokens.
- The `claudev-patch-resume` binary patch: it does not apply on 2.1.295 (no-match).

## Option A — on top of interactive Claude Code (the starting point)

1. Archive: incremental ingest of main and subagent transcripts, FTS, request telemetry.
2. Tree: Haiku builds nodes in the background from a durable queue; the view is a size sawtooth.
3. Memory MCP server (`eternal/mcp.py`, stdio, no dependencies):
   - `search(query, exact, author, session, since, k)` — all words, then any word; words of 6+ letters match by prefix (for inflected endings); `exact` is a trigram substring match;
   - `zoom(address, before, after, brief, kind)` — the record at an address like `ce3cab9e:L81` in full, neighbors truncated. `brief` gives a feed: one line of up to 160 characters per neighbor, up to 100 neighbors; paths are shortened so that the file's basename fits. `kind` keeps neighbors of one kind, e.g. `tool_use`;
   - `view()` waits for the tree (slice 5).

   It searches the live archive of all projects (`python -m eternal live`, `/var/tmp/<user>-eternal-live/archive.db`). Before each call the server appends new transcript lines: under `flock`, without waiting if an ingest is already running. Files with no new bytes are not read. If an already ingested tail has changed (checked against the last 64 KiB), the file is skipped instead of failing the ingest; it goes into the `diverged` list, and `audit` reports it. Calls and results of the server's own `mcp__eternal__*` tools are stored but not indexed: otherwise every search would find past searches instead of the originals. `ETERNAL_ARCHIVE` pins a frozen archive read-only. `ETERNAL_BEFORE` hides records from a given moment on, `ETERNAL_EXCLUDE` hides sessions by prefix; Tier-2 needs both.
4. Hooks:
   - SessionStart(`compact`) injects the registry (done), later the view tail.
   - PostCompact receives `compact_summary`: selecting "from the missing" (files on recent +10: 82% → 97%) is only possible here, since at SessionStart the summary does not exist yet.
   - PostToolBatch is not needed: an auto-compaction in the middle of a tool loop also fires SessionStart(`compact`), and the registry reaches the next request (selftest `mid_loop`).

   Verified live (2.1.295): SessionStart(`compact`) fires before `compact_boundary` and the summary are written. The order in the transcript is: boundary → summary → `hook_additional_context` → the next model request. `python -m eternal audit` checks this at every boundary: currently 3 of 3 delivered. With a 100K window, auto-compaction fires at about 70K tokens and needs at least two turns: with a single turn it is silently skipped.

   Precomputed compaction is the main auto-compaction path in 2.1.295. The summary is computed ahead of time in the background and swapped in at the threshold. The swap goes through the same finalization as a synchronous compaction. SessionStart(`compact`) fires at the swap, so the registry is fresh, and the record order after the boundary is the same. This path cannot be reproduced via `-p`: precomputation does not turn on there. So selftest covers only synchronous compaction, and `audit` counts precomputed boundaries separately. Precomputation requires a configured window of 200K or more; with a smaller window compaction is always synchronous.

   The registry survives `--resume`. A process started with `--resume` and no tools quotes the registry header, so there is no need to inject the registry on `resume`.

   The hook reads the transcript incrementally. The scan is cached in `live_dir()/scan/` (0600) and validated by size, by the 64 KiB tail before the offset, and by the digest of the modules that build the scan. On 380 MB a warm compaction takes 0.02 s, a cold one 4.5–13 s; the hook timeout is 120 s. Every hook log record carries the code digest, and `doctor` counts errors of the current version only.
5. `CLAUDE_CODE_AUTO_COMPACT_WINDOW` (tokens, 100K–1M) caps the window. Memory compensates for the losses from more frequent compactions. B3 measures the cost, Tier-2 the quality.

   Per B3, the context size gain is 1.7–2.0×, not 6×: the Claude Code start at p50 44K tokens plus a 15.6–31.2K view plus within-turn growth give a median of 82–97K against the actual 162K.

## Option B — our own turn driver (experiment)

`claude -p` with `--system-prompt-file`: the request holds the view plus the raw current turn, with no Claude Code compaction. The interactive TUI is lost. It is measured by the same Tier-1 and Tier-2. Every request carries Claude Code's own start context (B3).

## Benchmarks

- **Tier-1** (`python -m eternal bench`): no models, deterministic, on a frozen corpus.
  - B1 — compaction replay: retention by generation, `preservedMessages`, the share of the "All user messages" section, registry coverage under a budget, delivery lag, an upper bound for search;
  - B2 — view resolution by age;
  - B3 — prefix, context and cache;
  - B4 — prefix stability across a batch.

  `python -m eternal check` compares a recomputation with `baseline/tier1.json`.
- **Tier-2** (needs a model budget):
  - B5 — QA by age: which file was written first after an old prompt. The question quotes the prompt verbatim; there are no paraphrased questions yet. Systems S0–S4, graded by basename without a judge, UNCLEAR is a failure (the "B5" section below);
  - B6 — a long run with anchors;
  - B7 — memory poisoning;
  - B8 — a LongMemEval-M subset.

## Tier-1 baseline

Manifest `d77bf51c3a0f` (160 main sessions, 1.31 GB, 705 compactions), files `baseline/tier1.json` and `baseline/tier1.md`. The pipeline is deterministic: a fresh `ingest` into an empty directory followed by `check` gives 0 differences from the baseline.

- Retention depends strongly on the Claude Code version at compaction. After 1 generation the summary keeps 3–4% of prompts on 2.1.100–249 and 53% on 2.1.250–299 (21 sessions). After 5 generations, 0% versus 30%.
- The overall share after 1 generation (20%) is dragged down by a few large old sessions. Averaged per session it is 50%, and 36% after 5 generations.
- Deep generations rest on few sessions: 5 at +20, 3 at +50. There the whole prompt list fits in the budget, so 100% coverage says nothing about selection quality. At +3…+10 the whole list fits in the budget for only 41–67% of prompts, and there the registry gives 67–95% across all compactions.
- Selecting only from the missing adds 2–8 pp at +2…+5 across all compactions (at +3 with log quotas, 84% → 92%). On recent compactions it has no effect.
- B1 measures coverage on exactly the text the hook injects. A line is `L<line> MM-DD <text ≤100 chars>`; a prompt carries the suffix `→ a.py, b.md` of up to 45 characters: the files written in response to it before the next user message. Budgets: 7,500 characters for prompts, 1,800 for files, at most 10,000 in total. The policy is chosen per fact kind. Prompts use log quotas: on recent +10, 87% versus 85% for newest-first; across all compactions, 74% versus 67%; at +2…+5 across all compactions, newest-first is 1–6 pp higher. Files use newest-first: on recent +10, 82% versus 56% for log quotas.
- Why the suffix. A file line carries the line number of the file's first write in the last epoch where it was written, so line numbers recover the "prompt → file" link for only 68/73/50% of B5 questions at +1/+3/+10 (recent compactions). In the B5 pilot the answer was inferred this way in 3 of 5 questions, and S1 answered one of them correctly. The cost is prompt coverage on old compactions (log, +2/+3/+5/+10): 97/87/79/76% → 96/84/73/74%; on recent ones, −1 prompt of 158 at +10. The length barely matters: at 30 characters +5 gives 73.6%, at 60 it gives 72.4%; at 45 the first file name is whole in 99% of cases.
- Delivery: SessionStart(`compact`) is verified on live sessions. With UserPromptSubmit, 20–70% of auto-compactions are overtaken.
- Log tree with truncation: a view line at age 1K–10K messages covers 174 messages, 1.5 characters each. Details of old material live only in the archive, hence search is mandatory.
- Across a tree batch, 4% of the view's byte prefix survives (p50). There are 6.7 batches per 1K messages, so cache rewrites are rare.
- Cache: 96.7% of input is read from cache; input costs ×0.137 of uncached.

## Slices

| # | slice | points | status | depends on |
|---|---|---|---|---|
| 1 | repo, hygiene, port of the replay scripts | 2 | done | — |
| 2 | archive, provenance, FTS | 5 | done for main sessions; subagents via the `--sub` flag | 1 |
| 3 | Tier-1 and the frozen baseline | 5 | done: `baseline/tier1.*` | 2 |
| 4 | fact registry v2 and delivery hooks | 5 | done: hook registry = bench registry, SessionStart verified live, `audit` | 2, 3 |
| 5 | tree: Haiku summarizer, queue, nodes in the archive | 8 | 3 of 8: merge port and B2 with truncation | 2 |
| 6 | memory MCP server and integration | 5 | 4 of 5: `search`, `zoom`, live archive, pilot via `claude -p`; `view()` waits for slice 5 | 4, 5 |
| 7 | Tier-2 harness | 8 | 7 of 8: B5 S0–S4 (questions, ceiling, strata, `--restricted` isolation, retry of API failures, runs `b5-v2`, S4@20 and `b5-max` on `--effort max`, pairwise report merging runs with `--merge`); left: paraphrased questions | 6 |
| 8 | B6 and B7 | 5 | — | 7 |
| 9 | claudev fixes: cgroup, streaming `_count_turns`, `test_launcher` isolation | 3 | — | — |
| 10 | option B prototype | 8 | — | 5, 7 |
| 11 | nightly regression | 3 | — | 3, 7 |
| 12 | daily use: hook scan cache, live selftest (`recall`, `mid_loop`), `doctor`, shared memory, test sessions hidden from search | 8 | done | 4, 6 |

Total 65 points, 39 done; 114 tests. A claudev cgroup fix can only take effect at the next boot: restarting the `claudev-headless.service` unit would kill every session in the holders.

## B5 (Tier-2): which file was written after an old prompt

`python -m eternal qa questions|ceiling|run|report` (`eternal/bench/qa.py`), on the frozen baseline.

- **Question.** A quote of the prompt's beginning (up to 120 characters, cut at a word boundary, without embedded IDE context): "which file was written first after this prompt, before the user's next prompt?". It is asked after the compaction of generation +1, +3 or +10, in the B1 sense. Like most of the corpus, the question is in Russian.
- **Gold.** The first Write/Edit/NotebookEdit in the same epoch before the next human prompt. A question is dropped if the basename is ambiguous within the epoch (several paths), guessable (`README.md`, `__init__.py`, `settings.json` and the like) or named in the prompt itself.
- **Systems.**
  - S0 — the compaction summary;
  - S1 — the summary plus the registry built from lines before the boundary: exactly what the hook sees;
  - S2 — S1 plus `search` and `zoom` over the archive with `ETERNAL_BEFORE` = the boundary ts and the bench author's session excluded;
  - S3 — the summary plus search, no registry: what search gives on its own;
  - S4 — the summary plus Read and Grep over a copy of the transcript cut at the boundary. This is how plain Claude Code recalls: the summary footer ("read the full transcript at: …") points to the full transcript in all 705 compactions, and to its own session in 703. The copy sits alone in a fresh per-call directory. The source path, both absolute and with `~`, is replaced with the copy's path in the copy and in the footer. If the footer points to another session, S4 skips the question and records `skipped`.
- **Strata.** The B5 gold and the registry's "→ files" suffix are defined the same way. So if the registry at the cut shows the prompt ("shown"), S1 answers from the suffix by construction, and such a row only tests reading the format. Comparing S1 and S2 makes sense on hidden prompts ("hidden"), the ones the registry dropped while thinning. Every call records `shown` and `registry.params()` (budgets, `actions`, header digest); the report splits by stratum and lists the formats it met.
- **Grading.** Correct only if the answer has exactly one file name and it matches the gold basename case-insensitively. An UNCLEAR answer counts as a failure. No judge is needed.
- **Run.** `claude-wrapped -p` (Haiku, `--effort low` by default, `--restricted --permission-prompts none --strict-mcp-config`), log `calls.jsonl` (0600) outside the repo.
  - Every call runs in a fresh `/var/tmp/eternal-b5-*` directory, which is deleted after the call.
  - `--restricted` confines file tools to that directory, removes Bash and ignores user/project/local settings (`permissions`, plugins, `env`). Canary: Read and Grep outside the directory were denied, reads inside went through. MCP from `--mcp-config` still connects.
  - S0/S1 get 2 turns: headroom to answer after a denied call. S2–S4 get 8 (`--turns`). A run with another ceiling or effort goes to its own directory; `qa report --merge` merges directories, and the system gets a label (`S4@20`, `S4@20/max`).
  - A call in which a tool was denied (`permission_denials`) does not count and is shown separately (`denied`).
  - A call that failed outside the model (API, network, auth, timeout; `qa.failed`) does not count. It is written to `failed.jsonl` and retried when the run resumes; three such failures in a row stop the run. `error_max_turns` is an answer and counts.
  - Every call records its argv and `registry.params()`.
  - A run resumes where it stopped; the cap is a number of calls, and the run stops when the `utilization` of any rate-limit window reaches ≥ 0.5.

Recent compactions (since 2026-09-22), session 1cef4f2f excluded: 117 questions in 12 sessions (+1: 44, +3: 33, +10: 40). From prompt to write, p50 is 42 records or 16 tool calls; p90 is 104 records or 49 calls.

Hidden prompts are few and sit in a few large sessions. On recent compactions 8 questions are hidden at +10 (one session), none at +1 and +3. Across all compactions, of 673 questions 4 / 51 / 32 are hidden at +1 / +3 / +10, in 3 / 3 / 2 sessions. Generation +20 adds no hidden ones: the whole list fits in the budget there. So conclusions on the hidden stratum rest on 4 sessions.

Free ceiling for S2 (`qa ceiling`), 0 leaks past the boundary:

| stage | +1 | +3 | +10 |
|---|---|---|---|
| prompt found by its own words in the top-10 | 100% | 100% | 100% |
| answer within a `zoom` of 20 neighbors (before `brief`) | 39% | 36% | 45% |
| answer within `zoom brief`, 100 neighbors | 89% | 91% | 95% |
| answer within `zoom brief kind=tool_use` | 100% | 100% | 100% |

The `zoom` window was the bottleneck: before `brief`, the answer was lost beyond 20 neighbors, and short lines cut off the basename of a long path.

**Result `b5-v2` (2026-10-26).** 195 calls, $1.25, plus S4 with 20 turns (`b5-v2-s4t20`): 39 calls, $0.33. All numbers below come from `python -m eternal qa report --out /var/tmp/$USER-eternal-qa/b5-v2 --merge /var/tmp/$USER-eternal-qa/b5-v2-s4t20`; the logs live outside the repo. The report takes the `S4@20` label from the call's argv. Rate-limit utilization peaked at 0.07 (5-hour window) and 0.02 (weekly). One registry format (`actions` 45); `denied` and `skipped` are 0. Mid-run there was a network failure (`api_retry`, then `Connection refused` and `403 Request not allowed`). 8 calls of the main run and 8 of S4@20 failed outside the model and had been counted as UNCLEAR or wrong. They were moved to `failed.jsonl` and retried. Since then `qa.failed` filters such calls out.

Shown (recent compactions, 15 questions, 11 sessions):

| system | +1 | +3 | +10 | total | 95% CI | no search | out of turns | searched: correct |
|---|---|---|---|---|---|---|---|---|
| S0 summary | 0/5 | 0/5 | 0/5 | 0/15 | 0–20% | — | — | — |
| S1 + registry | 4/5 | 3/5 | 4/5 | 11/15 | 48–89% | — | — | — |
| S2 + registry + search | 5/5 | 4/5 | 5/5 | 14/15 | 70–99% | 1 | 0 | answers from the suffix |
| S3 + search | 5/5 | 5/5 | 5/5 | 15/15 | 80–100% | 0 | 0 | 15/15 |
| S4@8 transcript | 4/5 | 2/5 | 1/5 | 7/15 | 25–70% | 5 | 2 | 7/10 |
| S4@20 | 1/5 | 3/5 | 2/5 | 6/15 | 20–64% | 7 | 0 | 6/8 |

Hidden (all compactions, 24 questions, 4 sessions):

| system | +1 | +3 | +10 | total | 95% CI | no search | out of turns | searched: correct |
|---|---|---|---|---|---|---|---|---|
| S0 | 0/4 | 0/10 | 0/10 | 0/24 | 0–14% | — | — | — |
| S1 | 0/4 | 0/10 | 0/10 | 0/24 | 0–14% | — | — | — |
| S2 | 3/4 | 10/10 | 10/10 | 23/24 | 80–99% | 1 | 0 | 21/21 |
| S3 | 3/4 | 10/10 | 10/10 | 23/24 | 80–99% | 0 | 0 | 23/24 |
| S4@8 | 1/4 | 7/10 | 3/10 | 11/24 | 28–65% | 7 | 5 | 11/17 |
| S4@20 | 3/4 | 7/10 | 7/10 | 17/24 | 51–85% | 7 | 0 | 17/17 |

Pairwise on the same questions, "A correct and B not : the reverse" (the `pairs` field):

| pair | shown | hidden |
|---|---|---|
| S3 vs S4@20 | 9 : 0 | 7 : 1 |
| S2 vs S4@20 | 8 : 0 | 6 : 0 |
| S2 vs S3 | 0 : 1 | 1 : 1 |
| S1 vs S2 | 1 : 4 | 0 : 23 |
| S4@8 vs S4@20 | 4 : 3 | 4 : 10 |

Conclusions (all on Haiku with `--effort low`):

- The headline number on low. On shown questions (15 questions, 11 sessions), eternal search (S3) versus plain Claude Code (S4@20) is 15/15 versus 6/15, pairwise 9 : 0. On hidden ones it is 23/24 versus 17/24, 7 : 1, but that is only 4 sessions. The gap comes not from search quality but from the willingness to search and the number of turns; see the next two points. On max the first cause disappears, leaving turns and cost (`b5-max` below).
- S4 often does not open the transcript. All 7 S4@20 failures on hidden questions and 7 of 9 on shown ones are UNCLEAR without a single tool call, even though the summary footer points to the file. This decision is random and not tied to the question: of the 21 questions where S4@8 or S4@20 did not search, both runs failed to search on 5 (`gave_up_*` for the S4-S4@20 pair). With `search` and `zoom` the model always searches: S3's "no search" is 0 of 39.
- If S4 searches and has enough turns, the raw transcript is enough: 23 of 25. A correct answer takes more turns: on average 6.7 and 8.0 (up to 18) versus 4.1 and 3.6 (up to 6) for S3, on shown and hidden respectively. With a ceiling of 8, S4 hit it in 7 calls of 39, S3 never. Cost per call: S4@20 $0.0082 and $0.0084, S3 $0.0082 and $0.0062.
- In B5 the registry adds nothing to recall: S2 and S3 are equal within noise. On shown questions S2 answers from the suffix without searching (not a single tool call, $0.0060 versus $0.0082 for S3). On hidden ones the registry without search gives 0/24 (2/24 on max, from repeated prompts; see below).
- S1 on shown questions tests reading the suffix: 11/15. Haiku with `--effort low` missed an answer sitting in its prompt in 4 cases; on one of them the same text gave S2 a correct answer.

**Result `b5-max` (2026-10-26).** The same 39 questions as in `b5-v2` (the keys match), all five systems on Haiku with `--effort max`, S2–S4 with a ceiling of 20 turns. 195 calls, $2.60. The numbers below come from `python -m eternal qa report --out /var/tmp/$USER-eternal-qa/b5-max --merge /var/tmp/$USER-eternal-qa/b5-v2 /var/tmp/$USER-eternal-qa/b5-v2-s4t20`. `denied`, `skipped` and failures outside the model are 0; MCP is `connected` in all 78 S2 and S3 calls; the registry format is the same. Rate-limit utilization by the end of the run was 0.48 (5-hour window) and 0.33 (weekly), including the account's other usage.

| system | shown | hidden | total | no search | out of turns | turns to a correct answer, shown / hidden | $ for 39 questions |
|---|---|---|---|---|---|---|---|
| S0/max | 0/15 | 0/24 | 0/39 | — | — | — | 0.21 |
| S1/max | 15/15 | 2/24 | 17/39 | — | — | — | 0.32 |
| S2@20/max | 15/15 | 24/24 | 39/39 | 0 | 0 | 2.8 / 4.7 | 0.40 |
| S3@20/max | 15/15 | 24/24 | 39/39 | 0 | 0 | 6.3 / 5.1 | 0.45 |
| S4@20/max | 11/15 | 24/24 | 35/39 | 0 | 4 | 9.7 / 10.2 | 1.22 |

Pairwise, as above:

| pair | shown | hidden |
|---|---|---|
| S3@20/max vs S4@20/max | 4 : 0 | 0 : 0 |
| S2@20/max vs S3@20/max | 0 : 0 | 0 : 0 |
| S4@20 vs S4@20/max | 2 : 7 | 0 : 7 |
| S3 vs S3@20/max | 0 : 0 | 0 : 1 |
| S1 vs S1/max | 0 : 4 | 0 : 2 |

Conclusions on max:

- The propensity to search depends on effort. On max S4 opened the transcript in all 39 calls; on low it did not in 14. All 14 wins of S4@20/max over S4@20 are questions where S4@20 gave up without searching. Among the systems with tools, this is the only pair where only effort changed: otherwise the argv is the same, and Claude Code is 2.1.295 in both runs.
- The accuracy gap nearly vanished: S3@20/max 39/39 versus 35/39 for S4@20/max, pairwise 4 : 0. All four S4 failures are `error_max_turns` at 20 turns, two each at +3 and +10, all on shown questions. The strata differ by session, so this speaks about those transcripts, not about the stratum.
- What remains is cost and turns. On the same 39 questions S4@20/max cost $1.22 versus $0.45 for S3@20/max and $0.40 for S2@20/max, i.e. 2.7 and 3 times as much. It spent about 10 turns on average to a correct answer versus 5–6 for S3. Of S4's $0.75 on shown questions, $0.54 went to the four calls that hit the ceiling, one of them $0.35. On Haiku with max, eternal beats plain Claude Code on cost and turns, not on accuracy; B5 does not measure search across other sessions.
- Registry. S1/max on shown questions is 15/15 (11/15 on low, and all 4 low losses are UNCLEAR): on max Haiku reads the suffix reliably and without tools, $0.0078 per call. S2@20/max and S3@20/max agreed on all 39 questions. On shown questions the registry shortens the search: 2.8 turns versus 6.3, $0.0100 versus $0.0126 per call.
- S1/max on hidden questions is 2/24. Hiddenness is defined by the prompt's line, and in 5 of 24 questions the same prompt beginning (40 characters) is in the registry under another line: the prompt was repeated. Both hits come from those five. For one, the repeat's suffix names the same file; for the other, the gold file is in the "Files written" list. The registry is built from lines before the boundary, so this is not a leak.
- Max costs more per call: S3 $0.0082 and $0.0062 on low versus $0.0126 and $0.0107 on max (with a ceiling of 20); S4@20 $0.0082 and $0.0084 versus $0.050 and $0.0195.

Pilots on 5 recent shown questions (+1: 2, +3: 2, +10: 1):

- Without the suffix: S0 0/5, S1 1/5, S2 5/5, $0.084 for 15 calls.
- With the suffix (45 characters): S1 4/5, S3 5/5.
- S4 (8 turns): 2/5. Two calls ran out of turns: Grep over raw jsonl with `-o`, `head_limit` and `offset`. One answered UNCLEAR without a single tool call. Paths in all calls stayed inside the call directory, 0 denials. Input up to 270K tokens from cache per call, $0.005–0.013.
- The `b5-v1` run was stopped at 20 calls and does not count: S0 there ran with one turn and without `--restricted`, and Haiku tried to grep the transcript via the summary footer.

Caveats:

- All systems are Haiku: effort is measured (low and max), the model is not. A claudev session would recall with Opus at `--effort max`.
- S4 has no Bash: plain Claude Code would also parse the jsonl with `jq` or Python. So S4 is a lower bound for plain Claude Code.
- On low, S4 is measured at 8 and 20 turns, S2 and S3 at 8 (no more than 6 to a correct answer). On max all three run at 20, and S3 went up to 15 turns. S2 and S3 on max with a ceiling of 8 were not measured: both effort and ceiling changed for them at once.
- The propensity to search depends on effort (on max S4 always searched). Dependence on the model was not measured.
- The hidden stratum rests on 4 sessions; samples are small and intervals wide. Hiddenness is defined by line: a repeated prompt can sit in the registry under another line (5 of 24).
- Every call probably gets the global `~/.claude/CLAUDE.md`: `--restricted` does not disable CLAUDE.md, and `--bare` requires an API key. This is the same for all systems; not verified.

## Live archive and MCP: measurements

- Initial fill: 164 sessions, 436,571 lines, 744 compactions, 105 s, 494 MB, RSS 47 MB. An incremental update takes 0.1–0.2 s, of which walking the directory tree is 0.11 s.
- Search over a 468 MB archive: 1–51 ms per query.
- `claude -p` pilot (Haiku, `--strict-mcp-config --mcp-config`, neutral cwd): the model found the autotest code word in 7 turns, $0.0072. The first query missed: prefix matching does not reach inside a word, so the Russian for "compaction" did not match "auto-compaction". B5 measures search quality.
- **Update 2026-10-26.** Trust for the directory was accepted in an interactive `claude`. In `-p` the project's `.mcp.json` is now picked up on its own: status `connected`, source `project`.
- **Test sessions and search.** Selftest sessions end up in the live archive because their own searches update the archive. After a run their ids are written to `live_dir()/excluded`. The server reads that file on every call, except in pinned-archive mode, so it does not affect B5.
- The project's `.mcp.json` needs approval. Verified: `claude mcp list` shows "Pending approval" with `enabledMcpjsonServers` in `.claude/settings.local.json`; the directory's `hasTrustDialogAccepted` is false. The causal link is inferred from strings in the binary ("workspace not explicitly trusted; .claude/settings.local.json is gated"). The expected fix is one interactive `claude` launch in the project directory. `-p` needs no approval: the server is passed via `--mcp-config`; in the pilot the status was `connected`, source `dynamic`.
