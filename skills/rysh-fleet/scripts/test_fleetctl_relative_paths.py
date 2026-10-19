#!/usr/bin/env python3
"""fleetctl DISPLAYS and RECORDS a
message-body path RELATIVE TO THE WORKSPACE ROOT, never an absolute path; a
body file OUTSIDE the workspace is REFUSED.

Every generated text is checked: the envelope, the typed `msg --file` and
`report --file` lines, the auto-written body file (its header and content),
the over-long-inline pointer line, the returned record, the manifest log
record, the board-send argv (polling-stream) and the board-mirror post text.
In each: 0 `/Users/`, 0 occurrences of the username and 0 of the real home
directory. These three are COUNTED inside the test and never printed (a
failure message shows a label and a count only).

Controls: a needle (the counter finds planted synthetic
occurrences) and a non-empty-text check, so a 0 can never be vacuous.

No real ANSA send and no pane: `subprocess.run` is replaced by a recorder for
every delivery. Run: python3 test_fleetctl_relative_paths.py
"""

import io
import json
import os
import pwd
import shutil
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "..", "rysh-fanout", "scripts"))

import fleetctl as F  # noqa: E402

_PW = pwd.getpwuid(os.getuid())
_USER = _PW.pw_name          # never printed
_REAL_HOME = _PW.pw_dir      # never printed (from the passwd entry, not $HOME)
FLEET = "e9-o80-fleet"


def leak_counts(text: str) -> tuple:
    """(count of '/Users/', of the username, of the real home dir) in text."""
    return (text.count("/Users/"), text.count(_USER) if _USER else 0,
            text.count(_REAL_HOME) if _REAL_HOME else 0)


class Recorder:
    """Stands in for subprocess.run: records every argv; never runs anything."""

    def __init__(self):
        self.argvs = []

    def run(self, argv, **kw):
        self.argvs.append([str(a) for a in argv])
        return F.subprocess.CompletedProcess(argv, 0, stdout="delivered (fake)\n", stderr="")


class RelativePathsBase(unittest.TestCase):
    def setUp(self):
        # mktemp -d "${TMPDIR:-/tmp}/e9-o80.XXXXXX" (the bare macOS form ignores TMPDIR)
        base = os.environ.get("TMPDIR") or "/tmp"
        self.ws = os.path.realpath(tempfile.mkdtemp(prefix="e9-o80.", dir=base))
        self.addCleanup(shutil.rmtree, self.ws, True)
        self.assertEqual(leak_counts(self.ws), (0, 0, 0),
                         "the fake workspace path itself must carry no username/home")
        os.makedirs(os.path.join(self.ws, ".rysh", "fleet"))
        self.wt = os.path.join(self.ws, "worktrees", "e9-o80-sender-wt")
        os.makedirs(os.path.join(self.wt, "out"))
        self.env = {"workspace": self.ws, "session": "s", "bin": "rysh"}
        self.rec = Recorder()
        for p in (mock.patch.object(F.subprocess, "run", self.rec.run),
                  mock.patch.object(F, "require_ansa_capable", lambda env: None)):
            p.start()
            self.addCleanup(p.stop)
        self.mgr = {"label": "fmgr-o80", "role": "fleet-manager", "unit": "9",
                    "pane": "5029a9ed-0000-0000-0000-000000000000", "parent": None,
                    "worktree": os.path.join(self.ws, "worktrees", "e9-o80-mgr-wt")}
        self.wkr = {"label": "wkr-o80", "role": "worker", "unit": "9",
                    "pane": "e7ef9768-0000-0000-0000-000000000000",
                    "parent": self.mgr["pane"], "worktree": self.wt}
        os.makedirs(self.mgr["worktree"])
        self.man = {"fleet": FLEET, "members": [self.mgr, self.wkr], "msg_seq": 0, "log": []}
        self.save_manifest()

    def save_manifest(self):
        with open(os.path.join(self.ws, ".rysh", "fleet", f"{FLEET}.json"), "w") as fh:
            json.dump(self.man, fh)

    def manifest_text(self):
        with open(os.path.join(self.ws, ".rysh", "fleet", f"{FLEET}.json")) as fh:
            return fh.read()

    def body_in(self, d, name="order.md"):
        p = os.path.join(d, name)
        with open(p, "w") as fh:
            fh.write("# an order body\nline two\n")
        return p

    def assert_clean(self, label, text):
        self.assertTrue(text and text.strip(), f"{label}: generated text is empty (vacuous)")
        u, n, h = leak_counts(text)
        self.assertEqual((u, n, h), (0, 0, 0),
                         f"{label}: '/Users/', username, real-home counts")
        # the fake workspace has no username in it, so its own ABSOLUTE path is the
        # proxy that proves the path was made relative (not merely username-free)
        self.assertEqual(text.count(self.ws), 0, f"{label}: absolute workspace path count")


class Controls(RelativePathsBase):
    """the counter is not vacuous."""

    def test_needle_the_counter_finds_planted_occurrences(self):
        planted = f"x /Users/{_USER}/w y {_REAL_HOME}/z"
        u, n, h = leak_counts(planted)
        self.assertGreaterEqual(u, 1)
        self.assertGreaterEqual(n, 1)
        self.assertGreaterEqual(h, 1)

    def test_needle_the_workspace_proxy_counts_an_absolute_path(self):
        self.assertEqual(f"Read {self.ws}/x.md".count(self.ws), 1)

    def test_the_username_and_home_are_known(self):
        self.assertTrue(len(_USER) >= 1 and len(_REAL_HOME) >= 2)


class GeneratedTextsCarryNoAbsolutePath(RelativePathsBase):

    def test_msg_file_line_record_and_manifest_are_relative(self):
        bf = self.body_in(self.mgr["worktree"])
        rec = F.deliver(self.env, self.man, self.mgr, self.wkr, "", "WORK ORDER", bf)
        line = self.rec.argvs[-1][-1]
        rel = os.path.relpath(bf, self.ws)
        self.assertIn(f"Read {rel} in full", line)
        self.assertEqual(rec["body_file"], rel)
        self.assert_clean("envelope", rec["envelope"])
        self.assert_clean("msg --file typed line", line)
        self.assert_clean("msg record", json.dumps(rec))
        # the delivery's own record in the manifest log (the member roster's
        # `worktree` fields are written at launch, not by msg/report: out of scope)
        self.assert_clean("manifest log", json.dumps(json.loads(self.manifest_text())["log"]))

    def test_report_file_line_and_record_are_relative(self):
        bf = self.body_in(self.wt, "report.md")
        rec = F.deliver(self.env, self.man, self.wkr, self.mgr, "", "REPORT", bf)
        line = self.rec.argvs[-1][-1]
        self.assertIn(f"Read {os.path.relpath(bf, self.ws)} in full", line)
        self.assert_clean("report envelope", rec["envelope"])
        self.assert_clean("report --file typed line", line)
        self.assert_clean("report record", json.dumps(rec))

    def test_relative_path_resolves_from_the_workspace_root(self):
        bf = self.body_in(self.wt, "resolve.md")
        rec = F.deliver(self.env, self.man, self.wkr, self.mgr, "", "REPORT", bf)
        self.assertFalse(os.path.isabs(rec["body_file"]))
        with open(os.path.join(self.ws, rec["body_file"])) as fh:
            self.assertEqual(fh.read(), "# an order body\nline two\n")

    def test_overlong_inline_body_pointer_and_body_file_are_relative(self):
        body = "an inline body that is too long for one typed line " * 12
        rec = F.deliver(self.env, self.man, self.mgr, self.wkr, body, "WORK ORDER", None)
        line = self.rec.argvs[-1][-1]
        self.assertTrue(rec["body_file"] and not os.path.isabs(rec["body_file"]))
        self.assertIn(f"Read {rec['body_file']} in full", line)
        self.assert_clean("pointer line", line)
        self.assert_clean("auto record", json.dumps(rec))
        with open(os.path.join(self.ws, rec["body_file"])) as fh:
            written = fh.read()
        self.assertEqual(written, body, "the body file is the body, verbatim")
        self.assert_clean("auto body file (header + content)", written)

    def test_board_send_argv_is_relative(self):
        man = dict(self.man, comm=sorted(F.BOARD_COMMS)[0])
        bf = self.body_in(self.mgr["worktree"], "board.md")
        rec = F.deliver(self.env, man, self.mgr, self.wkr, "", "WORK ORDER", bf)
        argv = self.rec.argvs[-1]
        self.assertIn("board", argv)
        self.assertEqual(argv[argv.index("--file") + 1], os.path.relpath(bf, self.ws))
        self.assert_clean("board send argv", " ".join(argv))
        self.assert_clean("board record", json.dumps(rec))

    def test_board_mirror_text_is_relative(self):
        with mock.patch.object(F, "board_mirror_enabled", lambda: True):
            bf = self.body_in(self.mgr["worktree"], "mirror.md")
            F.deliver(self.env, self.man, self.mgr, self.wkr, "", "WORK ORDER", bf)
        mirror = [a for a in self.rec.argvs if "ansa" not in a]
        self.assertTrue(mirror, "the mirror post ran (fake)")
        self.assertIn(os.path.relpath(bf, self.ws), " ".join(mirror[-1]))
        self.assert_clean("board mirror argv", " ".join(mirror[-1]))


class OutsideWorkspaceIsRefused(RelativePathsBase):
    """RED at the old file: it delivered `Read /abs/outside/...`."""

    def setUp(self):
        super().setUp()
        base = os.environ.get("TMPDIR") or "/tmp"
        self.outside = os.path.realpath(tempfile.mkdtemp(prefix="e9-o80-out.", dir=base))
        self.addCleanup(shutil.rmtree, self.outside, True)

    def test_a_file_body_outside_the_workspace_is_refused_before_delivery(self):
        bf = self.body_in(self.outside)
        with mock.patch.object(F.sys, "stdout", new_callable=io.StringIO) as err:
            with self.assertRaises(SystemExit):
                F.deliver(self.env, self.man, self.mgr, self.wkr, "", "WORK ORDER", bf)
        self.assertIn("outside the workspace", err.getvalue())
        self.assertIn("outside the workspace", err.getvalue())
        self.assertEqual(self.rec.argvs, [], "nothing was sent")
        self.assertEqual(json.loads(self.manifest_text())["log"], [], "nothing was logged")

    def test_a_sender_worktree_outside_the_workspace_is_refused(self):
        self.mgr["worktree"] = self.outside
        body = "too long for one line " * 30
        with mock.patch.object(F.sys, "stdout", new_callable=io.StringIO):
            with self.assertRaises(SystemExit):
                F.deliver(self.env, self.man, self.mgr, self.wkr, body, "WORK ORDER", None)
        self.assertEqual(self.rec.argvs, [], "nothing was sent")
        self.assertEqual(os.listdir(self.outside), [], "no body file was written outside")


if __name__ == "__main__":
    unittest.main(verbosity=2)
