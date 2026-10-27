import unittest

from eternal.tree import Tree, Truncate, View, merge, span


def run(n: int, node: int, high: int, size: int = 200) -> tuple[Tree, View]:
    tree, view = Tree(Truncate(node)), View(high)
    for i in range(n):
        tree.append(f"m{i} " + "я" * size)
        view.reach(tree, i + 1)
        view.fit(tree)
    return tree, view


class TreeShape(unittest.TestCase):
    def test_parents_built_when_pairs_complete(self):
        t = Tree(Truncate(10))
        for i in range(5):
            t.append(str(i))
        self.assertEqual([len(level) for level in t.levels], [5, 2, 1])
        self.assertTrue(t.built(2, 0))
        self.assertFalse(t.built(1, 2))

    def test_truncate_bounds(self):
        s = Truncate(20)
        self.assertLessEqual(len(s.leaf("x" * 100)), 20)
        self.assertLessEqual(len(s.merge("a" * 50, "b" * 50)), 20)


class ViewInvariants(unittest.TestCase):
    def test_cover_contiguous_and_size_bounded(self):
        tree, view = run(5_000, 256, 20_000)
        covered = [i for p in view.parts for i in range(span(p)[0], span(p)[1] + 1)]
        self.assertEqual(covered, list(range(5_000)))
        self.assertEqual(view.size, sum(view.line(tree, p) for p in view.parts))
        self.assertLessEqual(view.size, view.high)
        self.assertGreater(view.batches, 0)

    def test_resolution_falls_with_age(self):
        _, view = run(20_000, 256, 20_000)
        levels = [p[0] for p in view.parts]
        self.assertGreater(levels[0], 6)
        self.assertEqual(levels[-1], 0)
        self.assertGreaterEqual(min(levels[: len(levels) // 4]), max(levels[-len(levels) // 4:]))

    def test_oldest_pair_merges_first(self):
        tree, view = Tree(Truncate(10)), View(high=4 * 34 - 1)
        for i in range(4):
            tree.append("0123456789")
        view.reach(tree, 4)
        self.assertTrue(view.fit(tree))
        self.assertEqual(view.parts, [(1, 0), (1, 1)])
        tree2, view2 = Tree(Truncate(10)), View(high=10_000)
        for i in range(8):
            tree2.append("0123456789")
        view2.reach(tree2, 8)
        parts, _ = merge(tree2, view2, 8 * 34 - 1)
        self.assertEqual(parts[0], (1, 0))
        self.assertEqual(parts[1:], [(0, i) for i in range(2, 8)])


if __name__ == "__main__":
    unittest.main()
