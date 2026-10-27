import unittest

from eternal import registry
from eternal.bench import replay
from eternal.bench.replay import Cell, Column, FactKind, Slice, Wake
from eternal.bench.timeline import Cut, Timeline, aum_span
from eternal.facts import Row, facts, norm
from eternal.kinds import Kind, Trigger, Via

P1 = "Первый промпт: почини курсор в memory.ts и добавь тесты"
P2 = "Второй промпт: проверь кэш после длинного хода, нужен отчёт"


def row(i: int, kind: Kind, epoch: int, text: str = "", tool: str | None = None, args: dict | None = None,
        tuid: str | None = None) -> Row:
    return Row(i, i, kind, epoch, f"u{i}", "2026-10-01T00:00:00Z", tool, tuid, args, text)


def cut(epoch: int, line: int, summary: str | None, preserved=(), trigger=Trigger.AUTO) -> Cut:
    s = norm(summary) if summary else None
    return Cut(epoch, line, trigger, frozenset(preserved), s, aum_span(s) if s else None)


def timeline() -> Timeline:
    rows = (
        row(1, Kind.HUMAN, 0, P1),
        row(2, Kind.TOOL_USE, 0, tool="Edit", args={"file_path": "/x/memory.ts"}),
        row(3, Kind.TOOL_USE, 0, tool="TaskCreate", args={"subject": "Написать тесты курсора"}, tuid="t1"),
        row(4, Kind.TOOL_RESULT, 0, tuid="t1", args={"task_id": "1"}),
        row(5, Kind.HUMAN, 0, P2),
        row(11, Kind.TOOL_USE, 1, tool="Bash"),
        row(12, Kind.TOOL_USE, 1, tool="Bash"),
        row(13, Kind.HUMAN, 1, "третий промпт достаточной длины"),
        row(14, Kind.TOOL_USE, 1, tool="TaskUpdate", args={"taskId": "1", "status": "completed"}),
    )
    cuts = (
        cut(0, 10, f"1. Intent: x\n6. All user messages:\n- {P1}\n7. Pending Tasks: написать тесты курсора; memory.ts",
            preserved=("u5",)),
        cut(1, 20, f"Primary: {P1[:70]}\n6. All user messages:\n- третий\n7. Pending Tasks: нет"),
    )
    return Timeline(1, "s1", "p", rows, cuts)


class Retention(unittest.TestCase):
    def test_generations_preserved_and_aum(self):
        r = replay.retention([timeline()])
        req = r[Slice(FactKind.REQUEST)]
        self.assertEqual(req[0].sessions, 1)
        self.assertEqual((req[0].total, req[0].summary, req[0].kept, req[0].aum), (3, 1, 2, 1))
        self.assertEqual((req[1].total, req[1].summary, req[1].kept, req[1].aum), (2, 1, 1, 0))
        files = r[Slice(FactKind.FILE)]
        self.assertEqual((files[0].summary, files[1].summary), (1, 1))  # basename also in P1

    def test_tasks_close_on_update(self):
        open_ = replay.open_tasks(timeline())
        self.assertEqual([len(x) for x in open_], [1, 0])
        self.assertEqual(replay.retention([timeline()])[Slice(FactKind.TASK)][0].summary, 1)

    def test_delivery_lag_counts_tool_calls(self):
        d = replay.delivery([timeline()])
        self.assertEqual(d["fate"], {"delivered": 1, "overwritten": 0, "eof": 1})
        self.assertEqual(d["lag_tool_calls"]["max"], 2)

    def test_delivery_on_any_turn_start(self):
        rows = (row(1, Kind.HUMAN, 0, P1), row(11, Kind.TOOL_USE, 1, tool="Bash"),
                row(12, Kind.TASK_NOTIFICATION, 1, "done"), row(13, Kind.TOOL_USE, 1, tool="Bash"),
                row(14, Kind.HUMAN, 1, P2))
        tl = Timeline(1, "s1", "p", rows, (cut(0, 10, P1),))
        lag = lambda w: replay.delivery([tl], replay.WAKES[w])["lag_tool_calls"]["max"]
        self.assertEqual((lag(Wake.HUMAN), lag(Wake.TURN)), (2, 1))


class Slices(unittest.TestCase):
    def test_via_and_recent_slices(self):
        tl = timeline()
        rows = tuple(Row(r.id, r.line, r.kind, r.epoch, r.uuid, r.ts, r.tool, r.tool_use_id, r.args, r.text,
                         Via.QUEUED if r.kind == Kind.HUMAN else None) for r in tl.rows)
        cuts = (tl.cuts[0], Cut(**{**{f: getattr(tl.cuts[1], f) for f in Cut.__slots__}, "ts": "2026-10-20"}))
        r = replay.retention([Timeline(1, "s1", "p", rows, cuts)], cutoff="2026-10-10")
        self.assertEqual(r[Slice(FactKind.REQUEST, via=Via.QUEUED)][0].total, 3)
        recent = r[Slice(FactKind.REQUEST, recent=True)]
        self.assertEqual((recent[0].total, recent[1].total), (1, 2))

    def test_macro_and_version_slice(self):
        a = Timeline(1, "s1", "p", (row(1, Kind.HUMAN, 0, P1),), (cut(0, 10, P1),))
        b = Timeline(2, "s2", "p", (row(1, Kind.HUMAN, 0, P2), row(2, Kind.HUMAN, 0, "ещё один промпт, которого нет"),
                                    row(3, Kind.HUMAN, 0, "и третий промпт тоже без следа")),
                     (Cut(**{**{f: getattr(cut(0, 10, "пусто"), f) for f in Cut.__slots__}, "version": "2.1.295"}),))
        r = replay.retention([a, b])
        t = r[Slice(FactKind.REQUEST)][0]
        self.assertEqual((t.summary, t.total, t.sessions, t.macro), (1, 4, 2, 0.5))
        self.assertEqual(r[Slice(FactKind.REQUEST, version="2.1.250-299")][0].total, 3)
        self.assertEqual(r[Slice(FactKind.REQUEST, version="?")][0].total, 1)

    def test_slice_keys(self):
        keys = {s.key() for s in (Slice(FactKind.REQUEST), Slice(FactKind.REQUEST, via=Via.SDK),
                                  Slice(FactKind.REQUEST, version="2.1.0-49"), Slice(FactKind.REQUEST, recent=True),
                                  Slice(FactKind.FILE))}
        self.assertEqual(len(keys), 5)
        self.assertIn("request", keys)
        self.assertEqual(Cell(Column.SUMMARY, recent=True).key(), "recent/summary")

    def test_version_bucket_order(self):
        self.assertEqual(replay.version_bucket("2.1.295"), "2.1.250-299")
        self.assertEqual(sorted(["2.1.100-149", "2.1.50-99", "?"], key=replay.version_order),
                         ["2.1.50-99", "2.1.100-149", "?"])

    def test_whole_words(self):
        self.assertEqual(replay.whole_words("почини курсор в memory.ts", 20), "почини курсор в")
        self.assertEqual(replay.whole_words("коротко", 20), "коротко")


class Coverage(unittest.TestCase):
    def test_coverage_adds_registry(self):
        cov = replay.coverage([timeline()], budgets={FactKind.REQUEST: 10_000})
        self.assertEqual(cov[Cell(Column.SUMMARY)][1], [1, 2])
        self.assertEqual(cov[Cell(Column.NEWEST)][1], [2, 2])
        self.assertEqual(cov[Cell(Column.LOG_MISSING)][1], [2, 2])
        self.assertEqual(cov[Cell(Column.FITS)][1], [2, 2])
        self.assertEqual(replay.coverage([timeline()], budgets={FactKind.REQUEST: 10})[Cell(Column.FITS)][1], [0, 2])

    def test_file_cells(self):
        cov = replay.coverage([timeline()])
        self.assertEqual(cov[Cell(Column.SUMMARY, kind=FactKind.FILE)][1], [1, 1])  # memory.ts is in P1
        self.assertEqual(Cell(Column.LOG, True, FactKind.FILE).key(), "recent/file/summary+log")

    def test_missing_registry_spends_budget_only_on_lost(self):
        a, b = "старый промпт, который сводка потеряла", "новый промпт, который сводка сохранила"
        tl = Timeline(1, "s1", "p", (row(1, Kind.HUMAN, 0, a), row(2, Kind.HUMAN, 0, b)), (cut(0, 10, f"- {b}"),))
        one = max(registry.entry_size(f) for f in facts(tl.rows))
        cov = replay.coverage([tl], budgets={FactKind.REQUEST: one})
        self.assertEqual((cov[Cell(Column.NEWEST)][0], cov[Cell(Column.NEWEST_MISSING)][0]), ([1, 2], [2, 2]))

    def test_recent_cut_slice(self):
        tl = timeline()
        fresh = Timeline(1, "s1", "p", tl.rows, tuple(Cut(**{**{f: getattr(c, f) for f in Cut.__slots__},
                                                              "ts": "2026-10-20"}) for c in tl.cuts))
        self.assertNotIn(Cell(Column.SUMMARY, recent=True), replay.coverage([tl], cutoff="2026-10-10"))
        self.assertEqual(replay.coverage([fresh], cutoff="2026-10-10")[Cell(Column.SUMMARY, recent=True)][1], [1, 2])


if __name__ == "__main__":
    unittest.main()
