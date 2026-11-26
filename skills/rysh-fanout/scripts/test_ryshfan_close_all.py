#!/usr/bin/env python3
"""ryshfan children / close --all: only panes WE started, never ourselves, never unasked.

Run (from the rysh-fanout skill directory):
    python3 scripts/test_ryshfan_close_all.py

A pane is our child only when its claude.parent equals our own pane id
(RYSH_PANE). A pane with claude.session_id and no claude.parent was started by
something else (an org spawn script, a launcher), so it is nobody's child and
is never closed. The caller's own pane is never a child, whatever its meta.
`close --all` prints the panes it would close and deletes nothing unless
--yes is given.
"""

import io
import json
import os
import sys
import unittest
from contextlib import redirect_stdout
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ryshfan  # noqa: E402

ME = "aaaaaaaa-0000-0000-0000-000000000001"
OTHER = "bbbbbbbb-0000-0000-0000-000000000002"


def pane(pid, name, meta, me=ME):
    return {"id": pid, "name": name, "meta": meta, "self": pid == me,
            "active": False, "program": "claude"}


def fleet():
    return [
        # The caller: has a session id (a launcher set it) and no parent.
        pane(ME, "lead", {"claude.session_id": "s-lead"}),
        # An org pane from a spawn script: session id, worker.parent, no claude.parent.
        pane("cccccccc-0000-0000-0000-000000000003", "org-worker",
             {"claude.session_id": "s-org", "worker.parent": "root"}),
        # Our real child.
        pane("dddddddd-0000-0000-0000-000000000004", "my-child",
             {"claude.session_id": "s-child", "claude.parent": ME}),
        # A sibling supervisor's child.
        pane("eeeeeeee-0000-0000-0000-000000000005", "their-child",
             {"claude.session_id": "s-theirs", "claude.parent": OTHER}),
        # No claude at all.
        pane("ffffffff-0000-0000-0000-000000000006", "shell", {}),
    ]


def names(ps):
    return sorted(p["name"] for p in ps)


class Children(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"RYSH_PANE": ME})
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def test_mine_only_is_exactly_our_children(self):
        self.assertEqual(names(ryshfan.children(fleet(), True)), ["my-child"])

    def test_no_parent_is_never_a_child(self):
        for mine_only in (True, False):
            got = names(ryshfan.children(fleet(), mine_only))
            self.assertNotIn("org-worker", got, mine_only)

    def test_never_the_caller(self):
        ps = fleet()
        ps[0]["meta"]["claude.parent"] = ME  # even when it claims to be ours
        for mine_only in (True, False):
            self.assertNotIn("lead", names(ryshfan.children(ps, mine_only)), mine_only)

    def test_any_parent_adds_only_parented_panes(self):
        self.assertEqual(names(ryshfan.children(fleet(), False)), ["my-child", "their-child"])

    def test_unknown_caller_has_no_children(self):
        with mock.patch.dict(os.environ, {"RYSH_PANE": ""}):
            self.assertEqual(ryshfan.children(fleet(), True), [])


class CloseAll(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"RYSH_PANE": ME})
        self.env.start()
        self.execs = []
        self.p1 = mock.patch.object(ryshfan, "list_panes", lambda env, pane_id=None: fleet())
        self.p2 = mock.patch.object(ryshfan, "rysh_exec",
                                    lambda env, cmd, pane_id=None, timeout=30: self.execs.append(cmd) or "")
        self.p3 = mock.patch.object(ryshfan, "discover", lambda session: {})
        for p in (self.p1, self.p2, self.p3):
            p.start()

    def tearDown(self):
        for p in (self.p1, self.p2, self.p3):
            p.stop()
        self.env.stop()

    def run_main(self, *argv):
        out, code = io.StringIO(), 0
        with mock.patch.object(sys, "argv", ["ryshfan.py", *argv]), redirect_stdout(out), \
                mock.patch("sys.stderr", io.StringIO()):
            try:
                ryshfan.main()
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else 1
        return code, out.getvalue()

    def deleted(self):
        return [c for c in self.execs if "##pane delete" in c]

    def test_without_yes_lists_and_deletes_nothing(self):
        code, out = self.run_main("close", "--all")
        self.assertEqual(self.deleted(), [])
        self.assertNotEqual(code, 0, "close --all without --yes must not report success")
        self.assertIn("my-child", out)
        self.assertNotIn("org-worker", out)

    def test_with_yes_deletes_only_our_children(self):
        code, out = self.run_main("close", "--all", "--yes")
        self.assertEqual(code, 0)
        self.assertEqual(self.deleted(), ["##pane delete dddddddd-0000-0000-0000-000000000004"])
        self.assertEqual(json.loads(out)["count"], 1)

    def test_any_parent_with_yes_never_takes_org_panes_or_self(self):
        self.run_main("close", "--all", "--any-parent", "--yes")
        got = self.deleted()
        self.assertEqual(sorted(got), ["##pane delete dddddddd-0000-0000-0000-000000000004",
                                       "##pane delete eeeeeeee-0000-0000-0000-000000000005"])


if __name__ == "__main__":
    unittest.main()
