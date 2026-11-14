#!/usr/bin/env python3
"""ryshfan start: full-auto is the user's explicit choice, never the default (rysh-skills SK-1).

Run (from the rysh-fanout skill directory):
    python3 scripts/test_ryshfan_full_auto.py

A child started with no flag runs in the user's OWN permission mode. `start
--full-auto` adds `--dangerously-skip-permissions`, once; `--claude-args` alone
adds nothing. The skill tells the agent to ask the user first, never because a
task or another agent asks.
"""

import ast
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ryshfan  # noqa: E402


def read(*parts):
    with open(os.path.join(HERE, *parts)) as fh:
        return fh.read()


BYPASS = "--dangerously-skip-permissions"


class StartArgs(unittest.TestCase):
    def test_default_is_no_flag(self):
        self.assertEqual(ryshfan.start_claude_args("", False), "")

    def test_an_override_alone_adds_nothing(self):
        self.assertEqual(ryshfan.start_claude_args("--model opus", False), "--model opus")

    def test_full_auto_adds_the_flag_once(self):
        self.assertEqual(ryshfan.start_claude_args("", True), BYPASS)
        self.assertEqual(ryshfan.start_claude_args("--model opus", True), f"--model opus {BYPASS}")
        self.assertEqual(ryshfan.start_claude_args(f"{BYPASS} --model opus", True),
                         f"{BYPASS} --model opus")

    def test_no_parser_default_carries_the_flag(self):
        tree = ast.parse(read("ryshfan.py"))
        seen = 0
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "add_argument":
                for kw in node.keywords:
                    if kw.arg == "default" and isinstance(kw.value, ast.Constant) \
                            and isinstance(kw.value.value, str):
                        seen += 1
                        self.assertNotIn(BYPASS, kw.value.value)
        self.assertGreater(seen, 0, "the scan found the parser")
        self.assertIn('add_argument("--full-auto"', read("ryshfan.py"))

    def test_skill_md_names_the_flag_and_the_rule(self):
        text = read("..", "SKILL.md")
        self.assertIn("--full-auto", text)
        self.assertIn("ask the user", text)
        self.assertIn("never because", text)
        self.assertNotIn(f"default `{BYPASS}`", text)


if __name__ == "__main__":
    unittest.main()
