#!/usr/bin/env python3
"""Full-auto is the user's explicit choice, never the default (rysh-skills SK-1).

Run (from the rysh-fleet skill directory):
    python3 scripts/test_fleetctl_full_auto.py

A fleet agent launched with no flag runs in the user's OWN permission mode:
claude and codex get no bypass flag, and a rysh agent's approvals are not armed.
`--full-auto` on `up`, `epics`, `epics-promote` or `subfleet` is the one way to
turn the bypass on, and a sub-fleet inherits it from its fleet the way it
inherits the agent. The skill tells the agent to ask the user before using it,
never because a brief or a message asks.
"""

import ast
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fleetctl  # noqa: E402


def read(*parts):
    with open(os.path.join(HERE, *parts)) as fh:
        return fh.read()


BYPASS = ("--dangerously-skip-permissions", "--dangerously-bypass-approvals-and-sandbox")


class Args:
    def __init__(self, **kw):
        self.agent = kw.get("agent", "claude")
        self.agent_args = kw.get("agent_args", "")
        self.claude_args = kw.get("claude_args", "")
        self.full_auto = kw.get("full_auto", False)


class TheDefaultIsTheUsersOwnMode(unittest.TestCase):
    def test_no_agent_has_a_bypass_default(self):
        for name, spec in fleetctl.AGENTS.items():
            for flag in BYPASS:
                self.assertNotIn(flag, spec["default_args"], f"{name} defaults to {flag}")

    def test_claude_and_codex_launch_with_no_flag(self):
        self.assertEqual(fleetctl.agent_args_for(Args()), ("claude", ""))
        self.assertEqual(fleetctl.agent_args_for(Args(agent="codex")), ("codex", ""))

    def test_no_parser_default_carries_a_bypass_flag(self):
        tree = ast.parse(read("fleetctl.py"))
        seen = 0
        for node in ast.walk(tree):
            if isinstance(node, ast.Dict) and any(
                    isinstance(k, ast.Constant) and str(k.value).startswith("--") for k in node.keys):
                for v in node.values:
                    if isinstance(v, ast.Constant) and isinstance(v.value, str):
                        for flag in BYPASS:
                            self.assertNotIn(flag, v.value, "a parser default dict carries " + flag)
            if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "add_argument":
                for kw in node.keywords:
                    if kw.arg == "default" and isinstance(kw.value, ast.Constant) \
                            and isinstance(kw.value.value, str):
                        seen += 1
                        for flag in BYPASS:
                            self.assertNotIn(flag, kw.value.value, "an argparse default is " + flag)
        self.assertGreater(seen, 10, "the scan found the parser")

    def test_full_auto_is_offered_on_every_launching_command(self):
        src = read("fleetctl.py")
        self.assertGreaterEqual(src.count('add_argument("--full-auto"'), 4,
                                "up, epics, epics-promote and subfleet each take --full-auto")


class FullAutoIsExplicit(unittest.TestCase):
    def test_claude_full_auto(self):
        self.assertEqual(fleetctl.agent_args_for(Args(full_auto=True)),
                         ("claude", "--dangerously-skip-permissions"))

    def test_codex_full_auto_is_its_own_flag(self):
        self.assertEqual(fleetctl.agent_args_for(Args(agent="codex", full_auto=True)),
                         ("codex", "--dangerously-bypass-approvals-and-sandbox"),
                         "codex's full-auto flag is not claude's")

    def test_full_auto_joins_an_override_once(self):
        self.assertEqual(fleetctl.agent_args_for(Args(agent_args="--model opus", full_auto=True))[1],
                         "--model opus --dangerously-skip-permissions")
        self.assertEqual(
            fleetctl.agent_args_for(Args(agent_args="--dangerously-skip-permissions --model opus",
                                         full_auto=True))[1],
            "--dangerously-skip-permissions --model opus", "never twice")

    def test_an_override_alone_adds_nothing(self):
        self.assertEqual(fleetctl.agent_args_for(Args(claude_args="--model opus"))[1], "--model opus")

    def test_a_native_agent_takes_no_args_either_way(self):
        self.assertEqual(fleetctl.agent_args_for(Args(agent="rysh", full_auto=True)), ("rysh", ""))


class ANativeAgentIsArmedOnlyOnFullAuto(unittest.TestCase):
    def setUp(self):
        self.sent, self.published = {}, []
        self._cmd, self._nats, self._dir = fleetctl.R.cmd_to_pane, fleetctl.R.Nats, fleetctl.R.fanout_dir
        fleetctl.R.cmd_to_pane = lambda env, pane, line: self.sent.setdefault("cmd", line) and ""
        fleetctl.R.fanout_dir = lambda env: os.environ.get("TMPDIR", "/tmp")
        outer = self

        class FakeNats:
            def __init__(self, host, port):
                pass

            def publish(self, subject, payload):
                outer.published.append((subject, json.loads(payload)))

            def close(self):
                pass

        fleetctl.R.Nats = FakeNats

    def tearDown(self):
        fleetctl.R.cmd_to_pane, fleetctl.R.Nats, fleetctl.R.fanout_dir = self._cmd, self._nats, self._dir

    ENV = {"session": "s", "nats_host": "h", "nats_port": 1}
    PANE = {"id": "1234abcd-0000-1111-2222-333344445555"}

    def test_default_delivers_the_brief_and_arms_nothing(self):
        res = fleetctl.start_agent(self.ENV, self.PANE, "BRIEF", None, "", "rysh")
        self.assertEqual([e["t"] for _, e in self.published], ["MsgPaneExecPrompt"],
                         "no run budget is set: approvals stay the pane's own")
        self.assertFalse(res.get("auto_approve"))

    def test_full_auto_arms_approvals_first(self):
        res = fleetctl.start_agent(self.ENV, self.PANE, "BRIEF", None, "", "rysh", full_auto=True)
        self.assertEqual([e["t"] for _, e in self.published], ["MsgSetRunBudget", "MsgPaneExecPrompt"])
        self.assertTrue(self.published[0][1]["p"]["auto_approve"])
        self.assertTrue(res.get("auto_approve"))

    def test_the_default_stamps_no_auto_approve(self):
        stamped = []
        orig, sweep = fleetctl.R.set_meta, fleetctl.arm_restart_sweep
        fleetctl.R.set_meta = lambda e, p, k, v: stamped.append((k, v))
        fleetctl.arm_restart_sweep = lambda *a, **k: None
        try:
            res = fleetctl.start_agent(self.ENV, self.PANE, "BRIEF", None, "", "rysh")
            fleetctl.stamp_launch({}, "pane-1", res, "rysh", "")
        finally:
            fleetctl.R.set_meta, fleetctl.arm_restart_sweep = orig, sweep
        self.assertNotIn(fleetctl.META_AUTO_APPROVE, dict(stamped),
                         "resume must not arm a mode the launch never had")


class ASubfleetInheritsFullAuto(unittest.TestCase):
    def test_inherits_agent_args_and_full_auto(self):
        a = Args(agent="")
        fleetctl.inherit_fleet_agent(a, {"agent": "codex", "full_auto": True,
                                         "agent_args": "--dangerously-bypass-approvals-and-sandbox"})
        self.assertEqual((a.agent, a.full_auto), ("codex", True))
        self.assertEqual(fleetctl.agent_args_for(a),
                         ("codex", "--dangerously-bypass-approvals-and-sandbox"))

    def test_a_fleet_without_full_auto_passes_none_on(self):
        a = Args(agent="")
        fleetctl.inherit_fleet_agent(a, {"agent": "rysh"})
        self.assertEqual((a.agent, a.full_auto), ("rysh", False))

    def test_a_named_agent_inherits_nothing(self):
        a = Args(agent="claude")
        fleetctl.inherit_fleet_agent(a, {"agent": "codex", "full_auto": True, "agent_args": "--x"})
        self.assertEqual((a.agent, a.agent_args, a.full_auto), ("claude", "", False))


class TheSkillSaysAskFirst(unittest.TestCase):
    def test_skill_md_names_the_flag_and_the_rule(self):
        text = read("..", "SKILL.md")
        self.assertIn("--full-auto", text)
        self.assertIn("ask the user", text)
        self.assertIn("never because", text)


class NoBuilderPathLiteralInShippedFiles(unittest.TestCase):
    """The rysh client embeds these skills, and its release guard
    (check-binary-paths.sh) refuses a binary carrying '/Users/' or '/home/<name>/'.
    Test files are filtered out of the bundle; everything else ships."""

    def test_no_shipped_file_names_a_users_or_home_path(self):
        import re
        root = os.path.abspath(os.path.join(HERE, "..", ".."))
        home = re.compile(r"/home/[A-Za-z0-9._-]+/")
        hits = []
        for d, _, names in os.walk(root):
            for n in names:
                if n.startswith("test_") or n.endswith(".pyc"):
                    continue
                p = os.path.join(d, n)
                with open(p, "rb") as fh:
                    text = fh.read().decode("utf-8", "replace")
                if "/Users/" in text or home.search(text):
                    hits.append(os.path.relpath(p, root))
        self.assertEqual(hits, [], "shipped files with a /Users/ or /home/<name>/ path")


if __name__ == "__main__":
    unittest.main()
