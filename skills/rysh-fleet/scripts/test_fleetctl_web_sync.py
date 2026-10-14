#!/usr/bin/env python3
"""Which fleet panes stream repaints to web clients (rysh-cli `##pane sync`).

A browser holding a fleet of twenty-plus agent panes stalls under their
combined repaint stream. `up` therefore stamps `web.sync=off` on every member
below the head of the chain by default; the head (the orchestrator the human
talks to) and the fleet's board keep streaming.

Run: python3 test_fleetctl_web_sync.py
"""

import argparse
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "..", "rysh-fanout", "scripts"))

import fleetctl as F  # noqa: E402

HEAD = {"role": "ceo", "pane": "p-ceo", "parent": None}
MGR = {"role": "manager", "pane": "p-mgr", "parent": "p-ceo"}
WKR = {"role": "worker", "pane": "p-wkr", "parent": "p-mgr"}


class WebSyncPolicy(unittest.TestCase):
    def paused(self, policy):
        man = {} if policy is None else {"web_sync": policy}
        return [F.web_sync_off(man, m) for m in (HEAD, MGR, WKR)]

    def test_root_keeps_only_the_head_live(self):
        self.assertEqual(self.paused("root"), [False, True, True])

    def test_all_keeps_everyone_live(self):
        self.assertEqual(self.paused("all"), [False, False, False])

    def test_none_pauses_the_head_too(self):
        self.assertEqual(self.paused("none"), [True, True, True])

    def test_a_manifest_from_before_the_policy_changes_nothing(self):
        # A subfleet added to an old fleet must not go dark on its own.
        self.assertEqual(self.paused(None), [False, False, False])

    def test_up_defaults_to_root(self):
        for cmd in (["up", "--from", "x"], ["epics", "--from", "x"], ["epics-promote"]):
            ns = self.parse(cmd)
            self.assertEqual(ns.web_sync, "root", cmd)

    def parse(self, argv):
        # main() builds the parser inline; rebuild just enough of it here by
        # calling it with a parser we can intercept.
        captured = {}
        real = argparse.ArgumentParser.parse_args

        def grab(self_, args=None, namespace=None):
            ns = real(self_, argv, namespace)
            captured["ns"] = ns
            raise SystemExit(0)

        argparse.ArgumentParser.parse_args = grab
        try:
            with self.assertRaises(SystemExit):
                F.main()
        finally:
            argparse.ArgumentParser.parse_args = real
        return captured["ns"]


if __name__ == "__main__":
    unittest.main()
