import tempfile
import unittest
from pathlib import Path

from eternal import corpus

from . import fixtures as fx


class Freeze(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.main = fx.write(self.root / "proj/s1.jsonl", [fx.human("one"), fx.human("two")], tail='{"partial":')
        fx.write(self.root / "proj/s1/subagents/a1.jsonl", [fx.human("sub")])

    def tearDown(self):
        self.tmp.cleanup()

    def test_partial_last_line_excluded_and_subagents_optional(self):
        m = corpus.freeze(self.root)
        (f,) = m.files
        self.assertEqual(f.size, self.main.read_bytes().rfind(b"\n") + 1)
        self.assertEqual(len(list(corpus.lines(m, f))), 2)
        self.assertEqual(len(corpus.freeze(self.root, sub=True).files), 2)

    def test_append_keeps_prefix_and_reads_frozen_only(self):
        m = corpus.freeze(self.root)
        with open(self.main, "a") as fh:
            fh.write('"x"}\n')
        fx.append(self.main, [fx.human("three")])
        self.assertEqual(corpus.verify(m), [])
        self.assertEqual(len(list(corpus.lines(m, m.files[0]))), 2)

    def test_rewrite_detected(self):
        m = corpus.freeze(self.root)
        fx.write(self.main, [fx.human("ONE"), fx.human("two")])
        self.assertEqual(corpus.verify(m), ["proj/s1.jsonl"])

    def test_exclude_and_roundtrip(self):
        self.assertEqual(corpus.freeze(self.root, exclude=frozenset({"s1"})).files, ())
        m = corpus.freeze(self.root)
        p = self.root / "m.json"
        p.write_text(m.dumps())
        self.assertEqual(corpus.Manifest.load(p).digest(), m.digest())


if __name__ == "__main__":
    unittest.main()
