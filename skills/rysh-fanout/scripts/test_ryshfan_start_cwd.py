#!/usr/bin/env python3
"""ryshfan start: the child claude runs in the supervisor's own directory.

Run (from the rysh-fanout skill directory):
    python3 scripts/test_ryshfan_start_cwd.py

`##new stack` opens panes in the workspace root, not in the anchor pane's
directory. A supervisor working in another folder would get children in the
workspace root, which then load that folder's CLAUDE.md and memory instead of
the supervisor's. `start` therefore changes into the supervisor's directory
(or --cwd) before it launches claude.
"""

import os
import shlex
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ryshfan  # noqa: E402

PANE = {"id": "dddddddd-0000-0000-0000-000000000004", "name": "child", "meta": {},
        "self": False, "active": False, "program": "", "qid": "t1.l1.g1.p1"}


class StartCwd(unittest.TestCase):
    def setUp(self):
        self.cmds = []
        self.tmp = tempfile.mkdtemp()
        patches = [
            mock.patch.object(ryshfan, "rysh_exec", lambda env, cmd, pane_id=None, timeout=30: ""),
            mock.patch.object(ryshfan, "cmd_to_pane", lambda env, pane, command: self.cmds.append(command) or ""),
            mock.patch.object(ryshfan, "fanout_dir", lambda env: self.tmp),
            mock.patch.object(ryshfan, "clean_env", lambda env=None, kind="claude": "env -u X"),
            mock.patch.object(ryshfan, "record_session", lambda *a, **k: None),
            mock.patch.object(ryshfan, "mark_prompted", lambda *a, **k: None),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def start(self, **kw):
        ryshfan.op_start({}, dict(PANE), "audit the parser", "", panes=[dict(PANE)], **kw)
        self.assertEqual(len(self.cmds), 1)
        return self.cmds[0]

    def test_child_starts_in_the_supervisor_directory(self):
        here = os.path.join(self.tmp, "my repo")
        os.makedirs(here)
        old = os.getcwd()
        os.chdir(here)
        self.addCleanup(os.chdir, old)
        cmd = self.start()
        self.assertTrue(cmd.startswith("cd " + shlex.quote(os.getcwd()) + " && "), cmd)

    def test_cwd_override(self):
        cmd = self.start(cwd="/srv/other")
        self.assertTrue(cmd.startswith("cd /srv/other && "), cmd)


if __name__ == "__main__":
    unittest.main()
