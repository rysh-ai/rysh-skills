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
    # `up` consults spend before it opens anything; neither is what these tests
    # are about, so both say "go" and let the ANSA gate be the only refusal.
    m.session_cost = lambda env: {"total": 0.0}
    m.budget_blocked = lambda cost: None
    m.self_pane = lambda panes: {"id": "pane-anchor", "name": "me", "tab": "tab-1"}
    m.active_pane = lambda panes: {"id": "pane-anchor", "name": "me", "tab": "tab-1"}
    # NOT installed in sys.modules here: fleetctl_testing loads fleetctl against it
    # under a private name and puts sys.modules back (ORDER-7c test isolation).
    return m


STUB = _stub_ryshfan()
sys.path.insert(0, HERE)
from fleetctl_testing import load_fleetctl_with_stub  # noqa: E402

# A PRIVATE fleetctl bound to STUB; sys.modules["ryshfan"/"fleetctl"] stay untouched.
fleetctl = load_fleetctl_with_stub(STUB, "fleetctl__test_ansa")

ENV = {"workspace": "/tmp", "session": "demo", "bin": "/nonexistent/rysh"}
RCPT = {"role": "worker", "unit": "01", "pane": "pane-recipient", "label": "wkr-01"}


class Delivery(unittest.TestCase):
    """W4-3b: ANSA is the only path. The property is unchanged and now harder to
    hold, because there is no second path to catch a mistake: A WORK ORDER NEVER
    VANISHES. It is delivered, or the caller is told loudly. Never a receipt
    without a delivery."""

    def setUp(self):
        STUB.calls.clear()
        STUB.fail_type = False
        fleetctl._ansa_capable = True   # assume a capable binary unless a test says otherwise
        self._real_run = subprocess.run

    def tearDown(self):
        fleetctl.subprocess.run = self._real_run
        fleetctl._ansa_capable = None
        fleetctl._ansa_probe_detail = ""  # a diagnostic belongs to its own probe

    def test_delivery_goes_via_ansa_and_nothing_types(self):
        fleetctl.subprocess.run = lambda argv, **k: types.SimpleNamespace(
            returncode=0, stdout="delivered", stderr="")
        res = fleetctl.fleet_send(ENV, RCPT, "hello")
        self.assertEqual(res["via"], "ansa")
        self.assertEqual(STUB.calls, [], "the typing path is gone; nothing may type")

    def test_via_survives_even_though_only_one_value_is_possible(self):
        """`via` is now redundant and stays anyway. It is the only way to answer
        HOW WAS THIS DELIVERED after the fact -- and it is the exact field that
        caught the live binary lacking `ansa` while every message silently fell
        back. The moment to drop an audit trail is never, and least of all when
        it stops being redundant."""
        fleetctl.subprocess.run = lambda argv, **k: types.SimpleNamespace(
            returncode=0, stdout="", stderr="")
        self.assertEqual(fleetctl.fleet_send(ENV, RCPT, "x")["via"], "ansa")

    def test_ansa_addresses_by_pane_id_never_by_name(self):
        seen = {}
        fleetctl.subprocess.run = lambda argv, **k: (
            seen.update(argv=argv) or types.SimpleNamespace(returncode=0, stdout="", stderr=""))
        fleetctl.fleet_send(ENV, RCPT, "hello")
        self.assertIn("pane-recipient", seen["argv"])
        self.assertNotIn("wkr-01", seen["argv"], "a label must never travel as an address")

    def test_a_failed_send_raises_rather_than_returning_a_receipt(self):
        """There is no fallback now, so this is the ONLY safe behaviour."""
        fleetctl.subprocess.run = lambda argv, **k: types.SimpleNamespace(
            returncode=1, stdout="", stderr="ansa: no route to pane")
        with self.assertRaises(SystemExit):
            fleetctl.fleet_send(ENV, RCPT, "hello")
        self.assertEqual(STUB.calls, [], "a failure must not silently type instead")

    def test_a_timeout_raises_too(self):
        def boom(*a, **k):
            raise subprocess.TimeoutExpired(cmd="rysh", timeout=20.0)
        fleetctl.subprocess.run = boom
        with self.assertRaises(SystemExit):
            fleetctl.fleet_send(ENV, RCPT, "hello")

    def test_an_old_binary_is_REFUSED_with_a_message_naming_the_cause(self):
        """The deployment prerequisite that stopped the live cutover.

        `macmini` had ANSA merged while the running sidecar predated it, so every
        message quietly took the fallback. With no fallback left, deploying onto
        such a binary must fail ONCE, clearly, naming the binary -- not produce a
        router error per message that the reader has to diagnose from a dead
        channel."""
        fleetctl._ansa_capable = False
        # A send would also exit if it merely FAILED, so asserting SystemExit
        # alone would pass whether or not the refusal exists. The message is the
        # test: it must name the cause and the binary.
        captured = {}
        def fake_die(m):
            captured["msg"] = m
            raise SystemExit(1)
        real_die, fleetctl.die = fleetctl.die, fake_die
        try:
            with self.assertRaises(SystemExit):
                fleetctl.fleet_send(ENV, RCPT, "hello")
        finally:
            fleetctl.die = real_die
        self.assertIn("ansa", captured.get("msg", ""))
        self.assertIn("subcommand", captured.get("msg", ""),
                      "the refusal must name the CAPABILITY, not read like a routing failure")
        self.assertIn(ENV["bin"], captured.get("msg", ""),
                      "name the binary, or the reader has to guess which one is wrong")
        self.assertEqual(STUB.calls, [], "nothing may be delivered by any other means")

    def test_capability_probe_detects_an_old_binary(self):
        fleetctl._ansa_capable = None
        fleetctl.subprocess.run = lambda argv, **k: types.SimpleNamespace(
            returncode=1, stdout="", stderr="rysh: unknown command: ansa")
        self.assertFalse(fleetctl.ansa_capable(ENV))

    def test_capability_probe_accepts_a_current_DEPLOYMENT(self):
        """Rule 4: prove the GOOD state is reachable. A probe that always said
        `not capable` would pass every refusal test above while making delivery
        impossible.

        AMENDED FOR E9 Q2. This test used to hand the probe local usage text —
        `usage: rysh ansa send <target> -- <text>` — and assert capable. That
        made it the test that certified GAP A: it asserted that a binary whose
        own --help mentions `ansa` is a working deployment, which is precisely
        the inference that let a new binary against a pre-ANSA daemon pass. The
        assertion was not merely weak, it was the wrong question written down.

        A current deployment is now one whose DAEMON answers the route. See
        StaleDaemon below for the negative this leaves reachable."""
        fleetctl._ansa_capable = None
        fleetctl.subprocess.run = lambda argv, **k: types.SimpleNamespace(
            returncode=1, stdout="",
            stderr='[unknown_target] no pane matches "probe" — address it by '
                   'full pane id or by @given-name')
        self.assertTrue(fleetctl.ansa_capable(ENV))

    def test_local_usage_text_alone_is_NOT_a_working_deployment(self):
        """The inference the amended test above stopped making, pinned so it
        cannot come back: `ansa` parsing locally proves the file on disk, and
        the file on disk is not the running session."""
        fleetctl._ansa_capable = None
        fleetctl.subprocess.run = lambda argv, **k: types.SimpleNamespace(
            returncode=1, stdout="", stderr="usage: rysh ansa send <target> -- <text>")
        self.assertFalse(fleetctl.ansa_capable(ENV))


class StaleDaemon(unittest.TestCase):
    """E9 Q2. The check EXISTED and measured the wrong thing.

    Q2 was filed as "a stale sidecar silently changes behaviour and nothing
    warns". Half of that is false — `ansa_capable`/`require_ansa_capable` have
    been there since W4-3b. The dangerous half is the half nobody wrote down:

    GAP A — the probe ran `<bin> ansa` and looked for the string "unknown
    command" in the LOCAL process's own usage output. That answers *is there an
    `ansa` subcommand in this file on disk*. The hazard Q2 names is the RUNNING
    DAEMON predating ANSA: a session is a long-lived process, so replacing the
    binary on disk changes nothing about the daemon already in memory. A new
    binary against an old daemon passes the old probe and fails every send.

    GAP B — the only caller was `fleet_send`, so the refusal landed at the FIRST
    MESSAGE: after every pane was opened, every worktree cut and every brief
    written. The check was right and arrived after the entire cost.

    The live daemon on this machine IS current, so a passing probe here proves
    nothing. Both negatives below are forced.
    """

    def setUp(self):
        STUB.calls.clear()
        fleetctl._ansa_capable = None
        self._real_run = subprocess.run

    def tearDown(self):
        fleetctl.subprocess.run = self._real_run
        fleetctl._ansa_capable = None
        fleetctl._ansa_probe_detail = ""  # a diagnostic belongs to its own probe

    # ---- GAP A ---------------------------------------------------------
    def test_a_current_binary_against_a_daemon_that_never_answers_is_NOT_capable(self):
        """The exact stale-daemon shape, and the one the old probe called capable.

        The binary is new: `ansa` parses, so nothing says "unknown command".
        The daemon is old: it has no MsgCLIAnsaSend case, drops the request, and
        the CLI's request/reply times out at the transport. No routing verdict
        ever comes back — which is the only thing that proves ANSA is live.
        """
        seen = {}

        def fake_run(argv, **k):
            seen["argv"] = argv
            # A NEW binary reaching an OLD daemon: the subcommand parsed fine,
            # the round trip died at the transport.
            return types.SimpleNamespace(
                returncode=1, stdout="", stderr="rysh: nats: timeout")

        fleetctl.subprocess.run = fake_run
        capable = fleetctl.ansa_capable(ENV)

        self.assertIn("send", seen.get("argv", []),
                      "the probe must attempt a real route, not read local --help text")
        self.assertIn(ENV["session"], seen.get("argv", []),
                      "the probe must name the SESSION — the question is whether the "
                      "running daemon speaks ANSA, and a probe that never names a "
                      "session cannot have asked it")
        self.assertFalse(capable,
                         "a binary that has `ansa` but a daemon that never answers is "
                         "NOT capable: every send will fail")

    def test_a_live_daemon_answering_the_route_IS_capable(self):
        """Rule 4: the good state must stay reachable.

        A probe that always said "not capable" would satisfy every refusal test
        while making delivery impossible. The probe addresses a sentinel target
        that cannot exist, so nothing is ever delivered; a LIVE daemon still
        answers it with a routing verdict carrying an ANSA error code. That
        refusal is the proof of life — verified against this machine's daemon,
        which returns `[unknown_target] no pane matches ...`.
        """
        fleetctl.subprocess.run = lambda argv, **k: types.SimpleNamespace(
            returncode=1, stdout="",
            stderr='[unknown_target] no pane matches "probe" — address it by '
                   'full pane id or by @given-name')
        self.assertTrue(fleetctl.ansa_capable(ENV))

    # ---- GAP B ---------------------------------------------------------
    def test_up_refuses_BEFORE_it_opens_the_first_pane(self):
        """The cost, not the correctness, was the bug.

        `up` opens a lane per role, stacks a pane per unit, cuts a worktree each
        and writes a brief each — and only then does the first `msg` discover
        ANSA is dead. The whole fleet is standing, unreachable, and tearing it
        down is manual. A deployment prerequisite has to be checked while
        refusing is still free.
        """
        fleetctl._ansa_capable = False
        opened = []

        def forbidden(name):
            def f(*a, **k):
                opened.append(name)
                raise AssertionError(f"{name} ran: up opened panes before checking ANSA")
            return f

        patched = {"new_tab": forbidden("new_tab"),
                   "new_lane": forbidden("new_lane"),
                   "stack_onto": forbidden("stack_onto"),
                   "make_worktree": forbidden("make_worktree"),
                   "start_agent": forbidden("start_agent")}
        real = {k: getattr(fleetctl, k) for k in patched}
        for k, v in patched.items():
            setattr(fleetctl, k, v)

        args = types.SimpleNamespace(
            fleet="q2probe", limit=None, force=False, replace=True,
            template="three-tier", workers=1, dry_run=False,
            roadmap_dir="new_roadmap/fleets/q2probe", new_tab=True,
            ceo_here=False, worktrees=True, base_branch="macmini",
            source="test")
        units = [{"unit": "01", "title": "unit one", "sources": []}]

        try:
            with self.assertRaises(SystemExit,
                                   msg="up accepted a dead ANSA deployment"):
                fleetctl.op_up(ENV, args, units=units)
        finally:
            for k, v in real.items():
                setattr(fleetctl, k, v)

        self.assertEqual(opened, [],
                         "up must refuse before it opens anything; it reached "
                         f"{opened}")

    def test_the_refusal_still_names_the_cause_and_says_nothing_landed(self):
        """Do not regress the one thing the current message does well.

        It has to keep naming the binary and the capability, and keep saying
        plainly that nothing was delivered — and it now also has to name the
        SESSION, because after GAP A the cause may be the daemon rather than
        the file on disk, and those have different fixes.
        """
        fleetctl._ansa_capable = False
        captured = {}

        def fake_die(m):
            captured["msg"] = m
            raise SystemExit(1)

        real_die, fleetctl.die = fleetctl.die, fake_die
        try:
            with self.assertRaises(SystemExit):
                fleetctl.require_ansa_capable(ENV)
        finally:
            fleetctl.die = real_die

        m = captured.get("msg", "")
        self.assertIn("ansa", m)
        self.assertIn("subcommand", m)
        self.assertIn(ENV["bin"], m)
        self.assertIn(ENV["session"], m,
                      "name the session: a stale DAEMON is fixed by restarting the "
                      "session, a stale BINARY by replacing the file — the reader "
                      "cannot pick the fix without knowing which is suspect")
        self.assertIn("Nothing has been delivered", m)
        self.assertEqual(STUB.calls, [], "nothing may be delivered by any other means")


if __name__ == "__main__":
    unittest.main(verbosity=2)
