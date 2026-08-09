#!/usr/bin/env python3
"""W4 step 1: fleetctl delivery over ANSA, opt-in, with a fallback that cannot lose a message.

Run (from the rysh-fleet skill directory): python3 scripts/test_fleetctl_ansa.py

THE PROPERTY UNDER TEST is not "ANSA delivers". It is **a work order never
vanishes**. This fleet lost three of them to a confident `ok: True`, and the
lesson was that the receipt is the dangerous part, not the failure. So every
test below asks the same question: when something goes wrong, does the caller
find out?
"""

import os
import subprocess
import sys
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))


def _stub_ryshfan():
    m = types.ModuleType("ryshfan")
    m.calls = []
    m.fail_type = False

    def list_panes(env, pane_id=None):
        return [{"id": "pane-recipient", "name": "wkr"}]

    def find_pane(panes, pane_id):
        if m.fail_type:
            raise RuntimeError("no such pane")
        return {"id": pane_id, "name": "stub"}

    def op_send(env, pane, text, settle=1.5):
        m.calls.append(("type", pane["id"], text))
        return {"ok": True, "output": "typed"}

    m.list_panes, m.find_pane, m.op_send = list_panes, find_pane, op_send
    m.rysh_exec = lambda env, command, pane_id=None, timeout=30: ""
    m.annotate_ids = lambda panes: None
    m.TAB_LINE = m.LANE_LINE = m.GROUP_LINE = m.PANE_LINE = m.META_RE = m.RUNNING_RE = None
    m.vt_screen = lambda env, pane: {"lines": []}
    m.visible_solid = lambda x: x
    m.is_busy = lambda lines: False
    m.has_composer = lambda lines: True
    sys.modules["ryshfan"] = m
    return m


STUB = _stub_ryshfan()
os.environ.setdefault("RYSHFAN_DIR", HERE)
sys.path.insert(0, HERE)
import fleetctl  # noqa: E402

ENV = {"workspace": "/tmp", "session": "demo", "bin": "/nonexistent/rysh"}
RCPT = {"role": "worker", "unit": "01", "pane": "pane-recipient", "label": "wkr-01"}


class Delivery(unittest.TestCase):
    def setUp(self):
        STUB.calls.clear()
        STUB.fail_type = False
        os.environ.pop("RYSH_FLEET_ANSA", None)
        self._real_run = subprocess.run

    def tearDown(self):
        fleetctl.subprocess.run = self._real_run
        os.environ.pop("RYSH_FLEET_ANSA", None)

    def test_default_is_now_ansa(self):
        """W4-3a: the default is flipped. ANSA carries delivery."""
        fleetctl.subprocess.run = lambda argv, **k: types.SimpleNamespace(
            returncode=0, stdout="delivered", stderr="")
        res = fleetctl.fleet_send(ENV, RCPT, "hello")
        self.assertEqual(res["via"], "ansa")
        self.assertEqual(STUB.calls, [], "the default path must not also type")

    def test_typing_is_still_reachable_by_opting_out(self):
        """The flip is NOT the deletion. Until W4-3b lands, an operator can
        still return to the path that has carried this fleet all epic -- which
        is the difference between a migration and a leap."""
        os.environ["RYSH_FLEET_ANSA"] = "0"
        res = fleetctl.fleet_send(ENV, RCPT, "hello")
        self.assertEqual(res["via"], "type")
        self.assertEqual(len(STUB.calls), 1)

    def test_ansa_path_when_opted_in(self):
        os.environ["RYSH_FLEET_ANSA"] = "1"
        fleetctl.subprocess.run = lambda argv, **k: types.SimpleNamespace(
            returncode=0, stdout="delivered", stderr="")
        res = fleetctl.fleet_send(ENV, RCPT, "hello")
        self.assertEqual(res["via"], "ansa")
        self.assertEqual(STUB.calls, [], "ANSA delivery must not also type")

    def test_ansa_addresses_by_pane_id_never_by_name(self):
        """Given-names are unique per LANE, not per session: a name is a label,
        an id is an address (design 026 5.1)."""
        os.environ["RYSH_FLEET_ANSA"] = "1"
        seen = {}
        fleetctl.subprocess.run = lambda argv, **k: (
            seen.update(argv=argv) or types.SimpleNamespace(returncode=0, stdout="", stderr=""))
        fleetctl.fleet_send(ENV, RCPT, "hello")
        self.assertIn("pane-recipient", seen["argv"])
        self.assertNotIn("wkr-01", seen["argv"],
                         "a label must never travel as an address")

    def test_ansa_failure_falls_back_and_SAYS_SO(self):
        """The headline. A failed ANSA send must not vanish and must not look
        like a clean ANSA delivery."""
        os.environ["RYSH_FLEET_ANSA"] = "1"
        fleetctl.subprocess.run = lambda argv, **k: types.SimpleNamespace(
            returncode=1, stdout="", stderr="ansa: no route to pane")
        res = fleetctl.fleet_send(ENV, RCPT, "hello")

        self.assertTrue(res.get("ok"), "the message must still be delivered")
        self.assertEqual(res["via"], "type", "it went the old way")
        self.assertEqual(res["fallback_from"], "ansa",
                         "a fallback that does not admit it is a silent success")
        self.assertIn("no route", res["ansa_error"])
        self.assertEqual(len(STUB.calls), 1, "exactly one delivery, not zero and not two")

    def test_ansa_timeout_falls_back(self):
        os.environ["RYSH_FLEET_ANSA"] = "1"
        def boom(*a, **k):
            raise subprocess.TimeoutExpired(cmd="rysh", timeout=20.0)
        fleetctl.subprocess.run = boom
        res = fleetctl.fleet_send(ENV, RCPT, "hello")
        self.assertEqual(res["fallback_from"], "ansa")
        self.assertEqual(len(STUB.calls), 1)

    def test_both_paths_dead_raises_rather_than_returning_a_receipt(self):
        """A work order that cannot be delivered must NOT return a receipt.
        Exiting non-zero is the correct outcome; `ok: True` is the bug that cost
        this fleet three orders."""
        os.environ["RYSH_FLEET_ANSA"] = "1"
        STUB.fail_type = True
        fleetctl.subprocess.run = lambda argv, **k: types.SimpleNamespace(
            returncode=1, stdout="", stderr="ansa down")
        with self.assertRaises(SystemExit):
            fleetctl.fleet_send(ENV, RCPT, "hello")


if __name__ == "__main__":
    unittest.main(verbosity=2)
