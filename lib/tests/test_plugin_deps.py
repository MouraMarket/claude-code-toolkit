#!/usr/bin/env python3
"""Tests for lib/plugin_deps.py `plan_removal`: which dropped plugins are kept, and the order of the rest."""

import os
import sys
import unittest

LIB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, LIB)
import plugin_deps  # noqa: E402

MARKET = "@claude-code-toolkit"


def ids(*names):
    return [n + MARKET for n in names]


def plan(dropped, stays, deps):
    """plan_removal over bare-name inputs; `deps` maps a plugin name to its dependency list."""
    drop = {n + MARKET: "profile-" + n for n in dropped}
    return plugin_deps.plan_removal(
        drop, [s + MARKET for s in stays], lambda key: deps.get(key.partition("@")[0], []))


class PlanRemovalTests(unittest.TestCase):
    def test_no_deps_all_removed_in_order(self):
        order, kept = plan(["b", "a"], ["z"], {})
        self.assertEqual(order, ids("a", "b"))
        self.assertEqual(kept, {})

    def test_stays_dependent_keeps_dependency(self):
        order, kept = plan(["a"], ["c"], {"c": ["a"]})
        self.assertEqual(order, [])
        self.assertEqual(kept, {"a" + MARKET: "c%s depends on it" % MARKET})

    def test_chain_kept_to_a_fixed_point(self):
        # c (stays) -> a -> b: b is only needed once a is kept. "a" sorts before "b",
        # so a single pass would already catch this; the reverse chain below is the hard one.
        order, kept = plan(["a", "b"], ["c"], {"c": ["a"], "a": ["b"]})
        self.assertEqual(order, [])
        self.assertEqual(sorted(kept), ids("a", "b"))
        self.assertEqual(kept["b" + MARKET], "a%s depends on it" % MARKET)

    def test_reverse_chain_needs_a_second_pass(self):
        # c (stays) -> b -> a: sorted order visits a first, when only b could need it.
        order, kept = plan(["a", "b"], ["c"], {"c": ["b"], "b": ["a"]})
        self.assertEqual(order, [])
        self.assertEqual(sorted(kept), ids("a", "b"))
        self.assertEqual(kept["a" + MARKET], "b%s depends on it" % MARKET)

    def test_dependent_goes_first(self):
        order, kept = plan(["a", "b"], ["z"], {"b": ["a"]})
        self.assertEqual(order, ids("b", "a"))
        self.assertEqual(kept, {})

    def test_dependent_first_the_other_way(self):
        order, _ = plan(["a", "b"], ["z"], {"a": ["b"]})
        self.assertEqual(order, ids("a", "b"))

    def test_dependency_named_by_full_key_and_bare_name(self):
        order, kept = plan(["a"], ["c"], {"c": ["a" + MARKET]})
        self.assertEqual((order, sorted(kept)), ([], ids("a")))
        order, kept = plan(["a"], ["c"], {"c": ["a"]})
        self.assertEqual((order, sorted(kept)), ([], ids("a")))

    def test_dependency_by_full_key_orders_dependents_first(self):
        order, _ = plan(["a", "b"], ["z"], {"b": ["a" + MARKET]})
        self.assertEqual(order, ids("b", "a"))

    def test_cycle_still_terminates(self):
        order, kept = plan(["a", "b"], ["z"], {"a": ["b"], "b": ["a"]})
        self.assertEqual(order, ids("a", "b"))
        self.assertEqual(kept, {})


if __name__ == "__main__":
    unittest.main()
