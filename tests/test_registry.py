import unittest
from unittest import mock

from eternal import registry
from eternal.facts import Fact, FactKind, Row, facts, turn_files
from eternal.kinds import Kind
from eternal.registry import BUDGETS, LIMIT, Policy

TS = "2026-10-01T00:00:00Z"


def fact(i: int, text: str = "x" * 80, kind: FactKind = FactKind.REQUEST) -> Fact:
    return Fact(kind, f"p{i}", i, i, i, f"u{i}", TS, text)


def human(i: int, text: str, epoch: int = 0) -> Row:
    return Row(i, i, Kind.HUMAN, epoch, f"u{i}", TS, None, None, None, text)


def write(i: int, path: str, epoch: int = 0) -> Row:
    return Row(i, i, Kind.TOOL_USE, epoch, f"u{i}", TS, "Write", f"t{i}", {"file_path": path}, f"Write: file_path={path}")


class Select(unittest.TestCase):
    def test_newest_respects_budget(self):
        fs = [fact(i) for i in range(100)]
        chosen = registry.select(fs, 1_000, Policy.NEWEST)
        self.assertEqual(chosen, set(range(100 - len(chosen), 100)))
        self.assertLessEqual(sum(registry.entry_size(f) for f in fs if f.row in chosen), 1_000)

    def test_log_reaches_older_entries(self):
        fs = [fact(i) for i in range(200)]
        newest, log = registry.select(fs, 1_500, Policy.NEWEST), registry.select(fs, 1_500, Policy.LOG)
        self.assertLess(min(log), min(newest))
        self.assertLessEqual(sum(registry.entry_size(f) for f in fs if f.row in log), 1_500)


class Render(unittest.TestCase):
    def test_entry_is_one_line_with_address(self):
        self.assertEqual(registry.entry(fact(7, "почини\n  курсор")), "L7 10-01 почини курсор")
        f = fact(8, "/home/u/projects/eternal/eternal/registry.py", FactKind.FILE)
        self.assertEqual(registry.entry(f), "L8 10-01 ~/projects/eternal/eternal/registry.py")
        self.assertEqual(len(registry.entry(fact(9, "я" * 500))), len("L9 10-01 ") + registry.ENTRY)
        scratch = "/tmp/claude-1000/-home-u/1cef4f2f-0000-4000-8000-000000000000/scratchpad/sim/a.py"
        self.assertEqual(registry.short(scratch), "scratchpad/sim/a.py")

    def test_build_keeps_latest_occurrence_in_order(self):
        rows = [human(1, "первый промпт достаточной длины"), write(2, "/tmp/a.py"),
                human(3, "второй промпт достаточной длины"), human(10, "первый промпт достаточной длины", epoch=1)]
        b = registry.build(rows, "1cef4f2f-aaaa")
        lines = b.text.splitlines()
        self.assertIn("Session 1cef4f2f:", lines[0])
        self.assertEqual(lines[1:], ["Prompts (2 of 2):", "L3 10-01 второй промпт достаточной длины",
                                     "L10 10-01 первый промпт достаточной длины", "Files written (1 of 1):",
                                     "L2 10-01 /tmp/a.py"])
        self.assertEqual((b.totals[FactKind.REQUEST], b.shown[FactKind.FILE]), (2, 1))

    def test_files_take_newest_prompts_take_log(self):
        rows = [write(i, f"/p/{i}.py") for i in range(400)] + [human(1000 + i, f"{i} промпт достаточной длины")
                                                                 for i in range(400)]
        text = registry.build(rows, "s1").text
        files = [int(x.split()[0][1:]) for x in text.split("Files written")[1].splitlines()[1:]]
        prompts = [int(x.split()[0][1:]) for x in text.split("Files written")[0].splitlines()[2:]]
        self.assertEqual(files, list(range(400 - len(files), 400)))
        self.assertLess(min(prompts), 1000 + 400 - len(prompts))

    def test_worst_case_fits_hook_limit(self):
        rows = [human(10_000_000 + i, f"{i} " + "я" * 1_000) for i in range(5_000)]
        rows += [write(20_000_000 + i, f"/home/u/{'d' * 80}/{i}.py") for i in range(3_000)]
        b = registry.build(rows, "s" * 36)
        self.assertLessEqual(len(b.text), LIMIT)
        self.assertGreater(len(b.text), sum(BUDGETS.values()) * 0.95)
        self.assertEqual(b.text.count("\n"), 2 + sum(b.shown.values()))

    def test_worst_case_with_annotations_fits_hook_limit(self):
        rows = []
        for i in range(3_000):
            rows += [human(3 * i, f"{i} " + "я" * 1_000)]
            rows += [write(3 * i + j, f"/p/{'f' * 40}{i}_{j}.py") for j in (1, 2)]
        b = registry.build(rows, "s" * 36)
        self.assertLessEqual(len(b.text), LIMIT)
        self.assertEqual(b.text.count("\n"), 2 + sum(b.shown.values()))
        prompts = b.text.split("Files written")[0].splitlines()[2:]
        self.assertTrue(prompts and all(" → " in x for x in prompts))

    def test_empty_session_has_no_entries(self):
        b = registry.build([], "s1")
        self.assertEqual((b.text.count("\n"), sum(b.shown.values())), (0, 0))


class Turns(unittest.TestCase):
    def setUp(self):
        registry.entry_size.cache_clear()

    tearDown = setUp

    def test_turn_ends_at_any_human_message_and_epoch(self):
        rows = [human(1, "первый промпт достаточной длины"), write(2, "/p/a.py"), write(3, "/q/b.md"),
                write(4, "/r/a.py"), human(5, "ок"), write(6, "/p/c.py"),
                human(7, "второй промпт достаточной длины"), write(8, "/p/d.py", epoch=1)]
        self.assertEqual(turn_files(rows), {1: ("a.py", "b.md"), 5: ("c.py",)})
        self.assertEqual([f.files for f in facts(rows) if f.kind == FactKind.REQUEST], [("a.py", "b.md"), ()])

    def test_ide_context_is_not_the_prompt(self):
        ide = "<ide_opened_file>The user opened the file /p/x.py in the IDE.</ide_opened_file>"
        sel = "<ide_selection>The user selected the lines 1 to 2 from /p/y.py:\nx = 1\n</ide_selection>"
        rows = [human(1, ide + "Почини тест в этом файле пожалуйста"), human(2, sel + ide + "Паажи, постой."),
                human(3, "<b>обычный</b> промпт с тегом в начале строки")]
        self.assertEqual([f.text for f in facts(rows)],
                         ["Почини тест в этом файле пожалуйста", "<b>обычный</b> промпт с тегом в начале строки"])

    def test_annotation_renders_capped_and_can_be_hidden(self):
        f = Fact(FactKind.REQUEST, "p", 0, 1, 1, "u", TS, "почини курсор", None, ("a.py", "b.md"))
        self.assertEqual(registry.entry(f), "L1 10-01 почини курсор → a.py, b.md")
        many = Fact(FactKind.REQUEST, "p", 0, 1, 1, "u", TS, "x", None, tuple(f"{i:02}.py" for i in range(30)))
        self.assertEqual(len(registry.entry(many).split(" → ")[1]), registry.ACTIONS)
        rows = [human(1, "первый промпт достаточной длины"), write(2, "/p/a.py")]
        self.assertIn("L1 10-01 первый промпт достаточной длины → a.py", registry.build(rows, "s1").text)
        on = registry.params()
        with mock.patch.object(registry, "ACTIONS", 0):
            registry.entry_size.cache_clear()
            self.assertEqual(registry.entry(f), "L1 10-01 почини курсор")
            self.assertNotIn("→", registry.build(rows, "s1").text)
            off = registry.params()
        self.assertEqual((on["actions"], off["actions"]), (registry.ACTIONS, 0))
        self.assertNotEqual(on["header"], off["header"])


if __name__ == "__main__":
    unittest.main()
