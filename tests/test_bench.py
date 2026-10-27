import tempfile
import unittest
from pathlib import Path

from eternal import archive, corpus
from eternal.__main__ import tier1
from eternal.bench import prefix
from eternal.bench.report import markdown
from eternal.bench.replay import FactKind, Slice

from . import fixtures as fx


def usage(ctx: int) -> dict:
    return {"input_tokens": 10, "cache_creation_input_tokens": 0, "cache_read_input_tokens": ctx - 10,
            "output_tokens": 3}


class EndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name) / "projects"
        first = fx.human("первый промпт: собери архив и прогони бенч")
        fx.write(root / "proj/s1.jsonl", [
            first,
            fx.text("начинаю", "m1", usage(30_010)),
            fx.tool_use("Bash", {"command": "ls"}, "m2", "t1", usage(31_000)),
            fx.tool_result("t1", "file"),
            fx.text("готово", "m3", usage(33_000)),
            fx.user_raw("<task-notification>done</task-notification>", origin={"kind": "task-notification"}),
            fx.text("принял", "m4", usage(34_000)),
            fx.boundary((first["uuid"],)),
            fx.summary("Summary: 6. All user messages: - первый промпт: собери архив и прогони бенч 7. Pending Tasks:"),
            fx.human("второй промпт после компакта"),
            fx.text("ок", "m5", usage(20_000)),
        ])
        self.m = corpus.freeze(root)
        self.db = archive.connect(Path(self.tmp.name) / "data/archive.db")
        archive.ingest(self.db, self.m)
        self.db.execute("INSERT INTO meta VALUES ('manifest', ?)", (self.m.digest(),))

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_prefix_start_and_turn_growth(self):
        r = prefix.run(self.db, self.m.created)
        self.assertEqual(r["start_context_recent"]["p50"], 30_010)
        self.assertEqual(r["within_turn_growth_recent"]["max"], 2_990)  # notification starts a new turn
        self.assertEqual(r["context_recent"]["n"], 5)

    def test_tier1_and_markdown(self):
        r = tier1(self.db, self.m)
        self.assertEqual(r["b1"]["retention"][Slice(FactKind.REQUEST).key()]["0"]["summary"], 1)
        self.assertEqual(r["corpus"]["kinds"]["task_notification"], 1)
        md = markdown(r)
        self.assertIn("B3", md)
        self.assertIn("Index check", md)
        self.assertEqual(set(r["b1"]["delivery"]), {"human", "turn"})


if __name__ == "__main__":
    unittest.main()
