#!/usr/bin/env python3
"""ryshfan reads screens and sends keys through the rysh CLI, never its own bus link.

Run (from the rysh-fanout skill directory):
    python3 scripts/test_ryshfan_cli_bus.py

token-compare finding 1: vt_screen and send_keys opened an anonymous NATS
connection, which the daemon refuses (Authorization Violation), so `screen`,
`status` and `wait` failed and every key send was silently lost. They now run
`rysh screen --json` and `rysh keys`, which use the session's own credential.
"""

import base64
import json
import os
import subprocess
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ryshfan  # noqa: E402

ENV = {"bin": "/opt/rysh/bin/rysh", "session": "s1", "workspace": "/w",
       "nats_host": "127.0.0.1", "nats_port": 4222}
PANE = "dddddddd-0000-0000-0000-000000000004"


class CliBus(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.reply = {"stdout": "", "returncode": 0}

        def run(argv, **kw):
            self.calls.append(list(argv))
            return subprocess.CompletedProcess(argv, self.reply["returncode"], self.reply["stdout"], "boom")

        def no_bus(*a, **k):
            raise AssertionError("ryshfan opened its own bus connection")

        for p in (mock.patch.object(ryshfan.subprocess, "run", run),
                  mock.patch("socket.create_connection", no_bus),
                  mock.patch("socket.socket.connect", no_bus)):
            p.start()
            self.addCleanup(p.stop)

    def test_screen_uses_rysh_screen_json(self):
        dim = "\x1b[2mnext\x1b[0m"
        self.reply["stdout"] = json.dumps({"pane_id": PANE, "interactive": True, "rows": ["top", dim]}) + "\n"
        s = ryshfan.vt_screen(ENV, PANE)
        self.assertEqual(self.calls, [[ENV["bin"], "screen", "--session", "s1", "--pane-id", PANE, "--json"]])
        self.assertTrue(s["interactive"])
        self.assertEqual(s["raw"], ["top", dim])
        self.assertEqual(s["lines"], ["top", "next"])

    def test_shell_pane_has_no_screen(self):
        # At the shell prompt `rysh screen` shows the output; ryshfan's old VT
        # read saw no rows there, and its composer/busy checks expect that.
        self.reply["stdout"] = json.dumps({"pane_id": PANE, "interactive": False, "rows": ["esc to interrupt"]})
        s = ryshfan.vt_screen(ENV, PANE)
        self.assertFalse(s["interactive"])
        self.assertEqual(s["lines"], [])

    def test_screen_from_an_old_rysh_raises(self):
        # A rysh older than `rysh keys` prints no "interactive": every pane
        # would read as having no screen. Say so instead.
        self.reply["stdout"] = json.dumps({"pane_id": PANE, "rows": ["x"]})
        with self.assertRaises(RuntimeError):
            ryshfan.vt_screen(ENV, PANE)

    def test_screen_failure_raises(self):
        self.reply["returncode"] = 1
        with self.assertRaises(RuntimeError):
            ryshfan.vt_screen(ENV, PANE)

    def test_keys_use_rysh_keys(self):
        ryshfan.send_keys(ENV, PANE, b"\r")
        self.assertEqual(self.calls, [[ENV["bin"], "keys", "--session", "s1", "--pane-id", PANE,
                                       "--b64", base64.b64encode(b"\r").decode()]])

    def test_keys_failure_raises(self):
        # A lost Enter used to look like a child that never answered.
        self.reply["returncode"] = 1
        with self.assertRaises(RuntimeError):
            ryshfan.send_keys(ENV, PANE, b"\r")

    def test_watch_uses_rysh_wait_exited(self):
        self.reply["stdout"] = json.dumps({"status": "reached", "exit_code": 0, "pane_id": PANE,
                                           "state": "exited", "program": "claude"})
        got = ryshfan.op_watch(ENV, {"id": PANE}, 90)
        self.assertEqual(self.calls, [[ENV["bin"], "wait", "--session", "s1", "--pane-id", PANE,
                                       "--until", "exited", "--timeout", "90s", "--json"]])
        self.assertEqual(got["state"], "exited")
        self.assertEqual(got["program"], "claude")

    def test_watch_timeout(self):
        self.reply.update(returncode=5, stdout=json.dumps({"status": "timeout", "exit_code": 5, "state": "timeout"}))
        self.assertEqual(ryshfan.op_watch(ENV, {"id": PANE}, 2)["state"], "timeout")

    def test_watch_error_raises(self):
        self.reply.update(returncode=1, stdout="")
        with self.assertRaises(RuntimeError):
            ryshfan.op_watch(ENV, {"id": PANE}, 2)


if __name__ == "__main__":
    unittest.main()
