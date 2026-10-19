#!/usr/bin/env python3
"""`fleetctl broadcast` takes its target roles from the fleet's TEMPLATE
(the same idea as the subfleet fix).

The CLI hard-coded `choices=["manager", "worker"]` (three-tier names), so an
epic lead could not `broadcast fleet-manager …` — argparse refused it before the
fleet was even read. A role the sender has nothing under also used to "succeed"
with zero deliveries; it now says so.

Run: python3 test_fleetctl_broadcast.py
"""

import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "..", "rysh-fanout", "scripts"))

import fleetctl as F  # noqa: E402

# Never rely on F.R (an earlier module may have left a stub ryshfan).
_spec = importlib.util.spec_from_file_location(
    "ryshfan_real_for_broadcast", os.path.join(HERE, "..", "..", "rysh-fanout", "scripts", "ryshfan.py"))
REAL_R = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(REAL_R)


def epic_quad():
    return {"fleet": "eq", "template": "epic-quad", "msg_seq": 0, "log": [],
            "units": [{"unit": "1", "title": "a"}, {"unit": "2", "title": "b"}],
            "members": [
                {"role": "roadmap-manager", "unit": "-", "pane": "lead", "label": "eq-roadmap", "parent": None},
                {"role": "fleet-manager", "unit": "1", "pane": "fm1", "label": "fmgr-1-a", "parent": "lead"},
                {"role": "fleet-manager", "unit": "2", "pane": "fm2", "label": "fmgr-2-b", "parent": "lead"},
                {"role": "worker", "unit": "1", "pane": "w1", "label": "wkr-1-a", "parent": "fm1"},
                {"role": "worker", "unit": "2", "pane": "w2", "label": "wkr-2-b", "parent": "fm2"},
            ]}


def three_tier():
    return {"fleet": "tt", "template": "three-tier", "msg_seq": 0, "log": [],
            "units": [{"unit": "01", "title": "a"}, {"unit": "02", "title": "b"}],
            "members": [
                {"role": "ceo", "unit": "-", "pane": "ceo", "label": "tt-ceo", "parent": None},
                {"role": "manager", "unit": "01", "pane": "m1", "label": "mgr-01-a", "parent": "ceo"},
                {"role": "manager", "unit": "02", "pane": "m2", "label": "mgr-02-b", "parent": "ceo"},
                {"role": "worker", "unit": "01", "pane": "w1", "label": "wkr-01-a", "parent": "m1"},
                {"role": "worker", "unit": "02", "pane": "w2", "label": "wkr-02-b", "parent": "m2"},
            ]}


class Broadcast(unittest.TestCase):

    def run_cli(self, man, *argv):
        """Drive the real CLI; return (exit code, delivered recipient labels, stdout)."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        os.makedirs(os.path.join(tmp.name, ".rysh", "fleet"))
        with open(os.path.join(tmp.name, ".rysh", "fleet", f"{man['fleet']}.json"), "w") as fh:
            json.dump(man, fh)
        env = {"workspace": tmp.name, "session": "s", "bin": "rysh"}
        sent, buf, code = [], io.StringIO(), 0
        with mock.patch.object(F, "R", REAL_R), \
             mock.patch.object(REAL_R, "discover", lambda session: env), \
             mock.patch.object(F, "deliver", lambda env, man, s, r, body, kind, bf:
                               sent.append(r["label"]) or {"to": r["label"]}), \
             mock.patch.object(sys, "argv", ["fleetctl.py", "--fleet", man["fleet"], *argv]), \
             contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            try:
                F.main()
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else 1
        return code, sent, buf.getvalue()

    # -- the epic case that used to be refused ---------------------------------

    def test_an_epic_lead_broadcasts_to_its_fleet_managers(self):
        code, sent, out = self.run_cli(epic_quad(), "broadcast", "fleet-manager", "hello", "--as", "eq-roadmap")
        self.assertEqual(code, 0, out)
        self.assertEqual(sorted(sent), ["fmgr-1-a", "fmgr-2-b"])

    def test_the_short_form_fmgr_works_too(self):
        code, sent, out = self.run_cli(epic_quad(), "broadcast", "fmgr", "hello", "--as", "eq-roadmap")
        self.assertEqual(code, 0, out)
        self.assertEqual(sorted(sent), ["fmgr-1-a", "fmgr-2-b"])

    def test_a_fleet_manager_broadcasts_to_its_own_workers_only(self):
        code, sent, out = self.run_cli(epic_quad(), "broadcast", "worker", "hi", "--as", "fmgr-1-a")
        self.assertEqual(code, 0, out)
        self.assertEqual(sent, ["wkr-1-a"])

    # -- three-tier keeps working exactly as before ----------------------------

    def test_three_tier_ceo_to_every_manager(self):
        code, sent, out = self.run_cli(three_tier(), "broadcast", "manager", "go", "--as", "tt-ceo")
        self.assertEqual(code, 0, out)
        self.assertEqual(sorted(sent), ["mgr-01-a", "mgr-02-b"])

    def test_three_tier_manager_to_its_own_workers(self):
        code, sent, out = self.run_cli(three_tier(), "broadcast", "worker", "go", "--as", "mgr-02-b")
        self.assertEqual(code, 0, out)
        self.assertEqual(sent, ["wkr-02-b"])

    def test_three_tier_ceo_to_every_worker_skips_the_chain_as_before(self):
        code, sent, out = self.run_cli(three_tier(), "broadcast", "worker", "go", "--as", "tt-ceo")
        self.assertEqual(code, 0, out)
        self.assertEqual(sorted(sent), ["wkr-01-a", "wkr-02-b"])

    # -- refusals that name the template's roles -------------------------------

    def test_an_unknown_role_is_refused_with_the_valid_roles(self):
        code, sent, out = self.run_cli(epic_quad(), "broadcast", "wizard", "x", "--as", "eq-roadmap")
        self.assertNotEqual(code, 0)
        self.assertEqual(sent, [])
        self.assertIn("epic-quad", out)
        self.assertIn("fleet-manager", out)
        self.assertIn("worker", out)

    def test_a_role_above_the_sender_is_refused(self):
        code, sent, out = self.run_cli(three_tier(), "broadcast", "manager", "x", "--as", "wkr-01-a")
        self.assertNotEqual(code, 0, "a worker has no managers under it")
        self.assertEqual(sent, [])

    def test_a_valid_role_with_nobody_directly_under_the_sender_says_so(self):
        # The lead's workers report to their fleet-managers, not to the lead.
        code, sent, out = self.run_cli(epic_quad(), "broadcast", "worker", "x", "--as", "eq-roadmap")
        self.assertNotEqual(code, 0, "zero recipients must not look like success")
        self.assertEqual(sent, [])
        self.assertIn("fleet-manager", out, "say who to send it to instead")


if __name__ == "__main__":
    unittest.main()
