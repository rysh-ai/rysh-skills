#!/usr/bin/env python3
"""E9 / Q1: `via` must never name a path that could not have carried the message.

Run (from the rysh-fleet skill directory): python3 scripts/test_fleetctl_via_audit.py

WHAT THIS GUARDS. Design 026 keeps `via` in the manifest for exactly one reason:
it is the only way to answer *how was this delivered* after the fact, and it is
the field that caught the live binary lacking `ansa` while every message was
silently falling back. A field kept for forensics is worth less than nothing if
it can record a path that does not exist -- a wrong answer is read with the same
confidence as a right one, and this one would point an investigation at a
delivery mechanism that was deleted at W4-3b.

REACHABILITY, stated honestly. Today `deliver()` is the only caller of
`fleet_send`, and `fleet_send` either returns an `ansa_send` result (which always
carries `via`) or dies. So the bad default is NOT reachable through the current
production path: this is a latent lie, not a live one. It is worth a test anyway,
because what makes it latent is a property of a DIFFERENT function -- one line in
`ansa_send` -- and nothing tells the next person who adds a delivery path, or a
retry wrapper, or a queue, that `deliver()` will quietly stamp their result with
the name of a path removed at W4-3b. The test pins `deliver()`'s own contract:
record what the sender reported, and when it reported nothing, say so.
"""

import json
import os
import sys
import tempfile
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))


def _install_ryshfan_stub():
    """fleetctl imports ryshfan at module scope; the real one needs a live daemon."""
    m = types.ModuleType("ryshfan")
    m.calls = []
    m.list_panes = lambda env, pane_id=None: [{"id": "pane-recipient", "name": "wkr"}]
    m.find_pane = lambda panes, pane_id: {"id": pane_id, "name": "stub"}

    def op_send(env, pane, text, settle=1.5):
        m.calls.append(("op_send", pane["id"], text))
        return {"ok": True, "output": "sent"}

    m.op_send = op_send
    m.rysh_exec = lambda env, command, pane_id=None, timeout=30: ""
    m.annotate_ids = lambda panes: None
    m.TAB_LINE = m.LANE_LINE = m.GROUP_LINE = m.PANE_LINE = m.META_RE = m.RUNNING_RE = None
    m.vt_screen = lambda env, pane: {"lines": []}
    m.visible_solid = lambda x: x
    m.is_busy = lambda lines: False
    m.has_composer = lambda lines: True
    # NOT installed in sys.modules here: fleetctl_testing loads fleetctl against it
    # under a private name and puts sys.modules back (ORDER-7c test isolation).
    return m


STUB = _install_ryshfan_stub()
sys.path.insert(0, HERE)
from fleetctl_testing import load_fleetctl_with_stub  # noqa: E402

# A PRIVATE fleetctl bound to STUB; sys.modules["ryshfan"/"fleetctl"] stay untouched.
fleetctl = load_fleetctl_with_stub(STUB, "fleetctl__test_via_audit")

SENDER = {"role": "manager", "unit": "01", "pane": "pane-sender", "label": "mgr-01"}
RECIPIENT = {"role": "worker", "unit": "01", "pane": "pane-recipient", "label": "wkr-01"}

# The path deleted at W4-3b. Named once, here, so the assertions below read as
# "not the removed path" rather than as a bare string comparison.
REMOVED_PATH = "type"


class ViaIsNeverAPathThatCannotHaveCarriedIt(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.env = {"workspace": self.tmp, "session": "test-session",
                    "bin": "/nonexistent/rysh"}
        self.man = {"fleet": "testfleet", "msg_seq": 0, "log": [],
                    "ceo": {"role": "ceo", "unit": "-", "pane": "pane-ceo",
                            "label": "ceo"},
                    "units": []}
        os.makedirs(os.path.join(self.tmp, ".rysh", "fleet"), exist_ok=True)
        with open(os.path.join(self.tmp, ".rysh", "fleet", "testfleet.json"), "w") as fh:
            json.dump(self.man, fh)
        self._real_fleet_send = fleetctl.fleet_send
        os.environ["RYSH_FLEET_BOARD"] = "0"   # the mirror is not under test here

    def tearDown(self):
        fleetctl.fleet_send = self._real_fleet_send

    def _deliver(self):
        return fleetctl.deliver(self.env, self.man, SENDER, RECIPIENT,
                                "do the thing", "WORK ORDER", None)

    def test_a_result_with_no_via_is_not_recorded_as_the_removed_path(self):
        """THE REGRESSION. A sender that reports no path must not be credited
        with the typing path, which has not existed since W4-3b."""
        fleetctl.fleet_send = lambda env, rcpt, line: {"ok": True}
        res = self._deliver()
        for where, rec in (("returned record", res), ("manifest log", self.man["log"][0])):
            self.assertNotEqual(
                rec["via"], REMOVED_PATH,
                f"{where}: `via` names the typing path, removed at W4-3b -- it "
                f"cannot have carried this message, so this is a false audit "
                f"record in the one field design 026 keeps for auditing")

    def test_an_unrecorded_path_is_recorded_as_unknown_rather_than_guessed(self):
        """Not-a-lie is the floor; the useful answer is that we do not know.

        Asserting only `!= "type"` would be satisfied by inventing any other
        name, which is the same defect with a different string in it."""
        fleetctl.fleet_send = lambda env, rcpt, line: {"ok": True}
        self.assertEqual(self._deliver()["via"], "unknown")

    def test_an_empty_via_is_also_treated_as_unrecorded(self):
        """`{"via": ""}` is a sender that recorded nothing, not a path named ""."""
        fleetctl.fleet_send = lambda env, rcpt, line: {"ok": True, "via": ""}
        self.assertEqual(self._deliver()["via"], "unknown")

    def test_a_reported_path_is_recorded_verbatim(self):
        """Rule 4: prove the GOOD state is still reachable. A `via` hardcoded to
        "unknown" would pass every assertion above while destroying the field."""
        fleetctl.fleet_send = lambda env, rcpt, line: {"ok": True, "via": "ansa"}
        res = self._deliver()
        self.assertEqual(res["via"], "ansa")
        self.assertEqual(self.man["log"][0]["via"], "ansa",
                         "the manifest is the audit trail; it must agree with the return")

    def test_the_real_ansa_path_still_stamps_ansa_end_to_end(self):
        """Through the REAL fleet_send, so the good case is not proven only
        against a stub of the function under audit."""
        fleetctl._ansa_capable = True
        real_ansa_send = fleetctl.ansa_send
        fleetctl.ansa_send = lambda env, rcpt, line: {"ok": True, "via": "ansa", "rc": 0}
        try:
            self.assertEqual(self._deliver()["via"], "ansa")
        finally:
            fleetctl.ansa_send = real_ansa_send
            fleetctl._ansa_capable = None


if __name__ == "__main__":
    unittest.main(verbosity=2)
