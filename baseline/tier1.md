# Tier-1: baseline

Manifest `d77bf51c3a0f`, frozen 2026-10-22T09:50:47+00:00. Counts only.

## Corpus

Main transcripts 160, records 135851, compactions 705 (without a summary 0), requests 49917; secret masks {'assign': 401, 'aws': 278, 'bearer': 4, 'jwt': 6, 'openai': 9}.

| record kind | records |
|---|---|
| tool_result | 55184 |
| tool_use | 55152 |
| assistant_text | 16857 |
| task_notification | 2693 |
| human | 2620 |
| away_summary | 1188 |
| compact_summary | 705 |
| meta | 496 |
| peer | 294 |
| command | 240 |
| command_output | 227 |
| scheduled | 123 |
| interrupt | 72 |

## B1. Prompts across summary generations

Sessions with compactions 49, compactions 705, recent among them 509. The probe is the first 60 characters of a human prompt of length ≥ 20 without embedded IDE context (`<ide_opened_file>`, `<ide_selection>`), verbatim after folding whitespace and case. "Per-session mean" is the share in the summary computed for each session and averaged.

| generation | prompts | sessions | in summary | per-session mean | in summary or preserved | of hits, in "All user messages" |
|---|---|---|---|---|---|---|
| +0 | 1421 | 48 | 75% | 90% | 77% | 96% |
| +1 | 1215 | 32 | 20% | 50% | 20% | 85% |
| +2 | 1080 | 27 | 19% | 40% | 19% | 78% |
| +3 | 932 | 20 | 15% | 38% | 15% | 68% |
| +5 | 790 | 17 | 15% | 36% | 15% | 68% |
| +10 | 368 | 10 | 19% | 28% | 19% | 55% |
| +20 | 160 | 5 | 12% | 11% | 12% | 53% |
| +50 | 71 | 3 | 6% | 5% | 6% | 25% |

By date and Claude Code version at compaction — share in the summary, with prompts and sessions in parentheses:

| slice | +0 | +1 | +5 | +10 |
|---|---|---|---|---|
| recent (since 2026-09-22) | 89% (212; 20 sess.) | 69% (204; 15 sess.) | 46% (203; 8 sess.) | 37% (158; 7 sess.) |
| version 2.1.100-149 | 55% (186; 3 sess.) | 3% (115; 2 sess.) | — | — |
| version 2.1.150-199 | 68% (609; 14 sess.) | 4% (568; 10 sess.) | 0% (350; 5 sess.) | 0% (58; 1 sess.) |
| version 2.1.200-249 | 91% (210; 12 sess.) | 3% (143; 5 sess.) | 0% (39; 1 sess.) | — |
| version 2.1.250-299 | 87% (416; 28 sess.) | 53% (389; 21 sess.) | 30% (401; 12 sess.) | 23% (310; 9 sess.) |

By prompt delivery path — share in the summary, with prompts and sessions in parentheses:

| slice | +0 | +1 | +5 | +10 |
|---|---|---|---|---|
| typed | 89% (687; 46 sess.) | 34% (532; 27 sess.) | 24% (395; 14 sess.) | 22% (268; 10 sess.) |
| queued | 89% (36; 16 sess.) | 48% (27; 11 sess.) | 35% (20; 7 sess.) | 23% (13; 7 sess.) |
| sdk | 53% (173; 2 sess.) | 2% (161; 2 sess.) | 0% (95; 1 sess.) | 0% (45; 1 sess.) |
| attachment | 73% (199; 24 sess.) | 15% (177; 18 sess.) | 17% (99; 12 sess.) | 21% (42; 9 sess.) |
| legacy | 60% (326; 7 sess.) | 4% (318; 7 sess.) | 0% (181; 3 sess.) | — |

Written files (basename):

| generation | facts | in summary | or preserved |
|---|---|---|---|
| +0 | 2514 | 78% | 82% |
| +1 | 2276 | 48% | 48% |
| +2 | 2078 | 32% | 32% |
| +3 | 1848 | 25% | 25% |
| +5 | 1642 | 21% | 21% |
| +10 | 1131 | 16% | 16% |
| +20 | 932 | 15% | 15% |
| +50 | 647 | 7% | 7% |

Open Task* tasks (they also live outside the context: TaskList, reminders):

| generation | facts | in summary | or preserved |
|---|---|---|---|
| +0 | 27 | 4% | 22% |
| +1 | 24 | 0% | 0% |
| +2 | 23 | 0% | 0% |
| +3 | 17 | 0% | 0% |
| +5 | 17 | 0% | 0% |
| +10 | 6 | 0% | 0% |
| +20 | 6 | 0% | 0% |

## B1. Coverage with the registry after each compaction

Same population as in the retention table. The registry is rendered the way the SessionStart hook injects it: a line "L<line> MM-DD text" of up to 100 characters, a prompt carries the suffix "→ files written in response to it" of up to 45 characters, a budget of 7500 characters for prompts and 1800 for files, at most 10000 in total. "Log" means equal quotas per octave of age, "newest" means the most recent up to the budget; the hook uses "log" for prompts and "newest" for files. "From the missing" selects only among facts absent from this summary and from preserved. The last column is the share of facts at compactions whose whole list fits the budget.

Prompts, all compactions:

| generation | n | summary+preserved | + newest | + log | + newest from the missing | + log from the missing | whole list fits the budget |
|---|---|---|---|---|---|---|---|
| +0 | 1421 | 77% | 100% | 100% | 100% | 100% | 74% |
| +1 | 1215 | 20% | 100% | 100% | 100% | 100% | 64% |
| +2 | 1080 | 19% | 98% | 95% | 100% | 98% | 57% |
| +3 | 932 | 15% | 90% | 84% | 95% | 92% | 50% |
| +5 | 790 | 15% | 74% | 73% | 81% | 78% | 41% |
| +10 | 368 | 19% | 67% | 74% | 67% | 73% | 67% |
| +20 | 160 | 12% | 100% | 100% | 100% | 100% | 100% |
| +50 | 71 | 6% | 100% | 100% | 100% | 100% | 100% |

Prompts, recent compactions (since 2026-09-22):

| generation | n | summary+preserved | + newest | + log | + newest from the missing | + log from the missing | whole list fits the budget |
|---|---|---|---|---|---|---|---|
| +0 | 212 | 96% | 100% | 100% | 100% | 100% | 98% |
| +1 | 204 | 69% | 100% | 100% | 100% | 100% | 100% |
| +2 | 197 | 61% | 100% | 100% | 100% | 100% | 98% |
| +3 | 189 | 58% | 100% | 100% | 100% | 100% | 98% |
| +5 | 203 | 46% | 100% | 100% | 100% | 100% | 85% |
| +10 | 158 | 37% | 85% | 87% | 85% | 87% | 85% |
| +20 | 114 | 16% | 100% | 100% | 100% | 100% | 100% |
| +50 | 71 | 6% | 100% | 100% | 100% | 100% | 100% |

Written files, all compactions:

| generation | n | summary+preserved | + newest | + log | + newest from the missing | + log from the missing | whole list fits the budget |
|---|---|---|---|---|---|---|---|
| +0 | 2514 | 82% | 94% | 94% | 98% | 97% | 20% |
| +1 | 2276 | 48% | 82% | 80% | 92% | 90% | 15% |
| +2 | 2078 | 32% | 76% | 74% | 84% | 82% | 13% |
| +3 | 1848 | 25% | 75% | 73% | 79% | 79% | 13% |
| +5 | 1642 | 21% | 76% | 71% | 79% | 79% | 10% |
| +10 | 1131 | 16% | 78% | 57% | 91% | 76% | 6% |
| +20 | 932 | 15% | 32% | 32% | 53% | 47% | 0% |
| +50 | 647 | 7% | 12% | 15% | 17% | 20% | 0% |

Written files, recent compactions (since 2026-09-22):

| generation | n | summary+preserved | + newest | + log | + newest from the missing | + log from the missing | whole list fits the budget |
|---|---|---|---|---|---|---|---|
| +0 | 1096 | 100% | 100% | 100% | 100% | 100% | 20% |
| +1 | 1072 | 73% | 100% | 100% | 100% | 100% | 17% |
| +2 | 1067 | 45% | 99% | 98% | 99% | 99% | 14% |
| +3 | 993 | 34% | 100% | 97% | 100% | 100% | 13% |
| +5 | 965 | 27% | 99% | 90% | 100% | 99% | 9% |
| +10 | 909 | 16% | 82% | 56% | 97% | 79% | 4% |
| +20 | 802 | 12% | 27% | 28% | 49% | 42% | 0% |
| +50 | 647 | 7% | 12% | 15% | 17% | 20% | 0% |

## B1. Delivery via UserPromptSubmit after auto-compaction

"Overwritten" means the next compaction came before the hook had a reason to fire. Whether UserPromptSubmit fires on notifications and peer messages is not visible in the transcript, so both variants are given.

- the hook fires only on a human prompt: auto-compactions 603, {'delivered': 166, 'overwritten': 422, 'eof': 15}; tool calls before delivery p10 0 · p50 22 · p90 68 (n=166).
- the hook fires at any turn start (human, notification, peer, scheduled): auto-compactions 603, {'delivered': 474, 'overwritten': 120, 'eof': 9}; tool calls before delivery p10 1 · p50 14 · p90 42 (n=474).

## B1. Index check: a query built from the lost prompt's own words

Checks that FTS finds a prompt by its own words. This is not model recall: questions in other words are measured by B5 (Tier-2). Lost after one generation: 978, sampled 978; top-5 among human messages of all sessions.

| query | found in top-k |
|---|---|
| phrase | 100% (975/978) |
| rare3 | 95% (926/976) |
| stem5 | 96% (938/976) |
| trigram | 100% (977/978) |

## B2. Log tree: view resolution by age (summarizer = truncation)

Node 256 characters, view sawtooth 32000–64000 characters; sessions with ≥1000 messages 23, messages 115568; batches per 1K messages 6.72.

| age, messages | lines measured | messages per line | characters per message |
|---|---|---|---|
| <100 | 21715 | 1.1 | 184.48 |
| 100-1K | 19636 | 10.8 | 22.11 |
| 1K-10K | 5182 | 174.5 | 1.46 |
| >=10K | 339 | 1095.0 | 0.23 |

View size, estimated in tokens: p10 17 372 · p50 23 667 · p90 29 149 (n=242). Byte prefix kept across a batch: p10 0% · p50 4% · p90 13% (n=777).

## B3. Claude Code context (recent = since 2026-09-22)

| measure | tokens |
|---|---|
| first request of a fresh session, recent | p10 40 376 · p50 44 397 · p90 56 466 (n=24) |
| first request, version 2.1.100-149 | p10 23 149 · p50 24 214 · p90 28 780 (n=5) |
| first request, version 2.1.150-199 | p10 28 853 · p50 31 336 · p90 35 775 (n=17) |
| first request, version 2.1.200-249 | p10 33 179 · p50 37 344 · p90 46 064 (n=55) |
| first request, version 2.1.250-299 | p10 32 955 · p50 44 659 · p90 57 481 (n=42) |
| every request, all time | p10 87 627 · p50 165 196 · p90 375 732 (n=49529) |
| every request, recent | p10 100 347 · p50 162 172 · p90 246 777 (n=25015) |
| growth within a turn, recent | p10 395 · p50 21 836 · p90 86 284 (n=25015) |
| context at compaction, recent | p10 167 413 · p50 234 307 · p90 270 835 (n=509) |
| start + view (15610 tok.) + turn growth | p10 60 402 · p50 81 843 · p90 146 291 (n=25015); actual p50 / estimate p50 = 1.98 |
| start + view (31220 tok.) + turn growth | p10 76 012 · p50 97 453 · p90 161 901 (n=25015); actual p50 / estimate p50 = 1.66 |

prompt_snapshot, recent: system prompt, characters p10 6 108 · p50 6 531 · p90 15 253 (n=1072); tool schemas, characters p10 110 902 · p50 117 545 · p90 138 921 (n=540); tools p10 14 · p50 15 · p90 15 (n=540).

Cache: read 0.9674, write 0.0322 of 10 088 312 069 input tokens; input cost ×0.137 of uncached.

