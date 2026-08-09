#!/usr/bin/env python3
"""Regression tests for the agents-board mirror in fleetctl.deliver().

Run:  python3 .claude/skills/rysh-fleet/scripts/test_fleetctl_board.py

Why this file exists at all. `board_mirror` runs inside the code path that every
`msg`, `report` and `broadcast` in every live fleet on this machine goes through.
A defect there does not lose a board post — it loses the fleet's ability to talk
to itself, which is how the fleet is driven and how it reports. So the headline
test is not "the mirror works"; it is **"the mirror cannot break delivery"**, and
it asserts that against a mirror rigged to fail in each way a real one can.

There is no pytest in this repo, so this is stdlib unittest and imports fleetctl
with a stub `ryshfan` — the real one shells out to a live daemon.
"""

import json
import os
import subprocess
import sys
import tempfile
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))


def _install_ryshfan_stub():
    """Stand in for the rysh-fanout substrate.

    fleetctl imports ryshfan at module scope and calls it to find and type into
    panes. Every one of those is a live-daemon operation, so the stub records
    calls instead of performing them.
    """
    m = types.ModuleType("ryshfan")
    m.calls = []

    def list_panes(env, pane_id=None):
        return [{"id": "pane-sender", "name": "mgr"}, {"id": "pane-recipient", "name": "wkr"}]

    def find_pane(panes, pane_id):
        return {"id": pane_id, "name": "stub"}

    def op_send(env, pane, text, settle=1.5):
        m.calls.append(("op_send", pane["id"], text))
        return {"ok": True, "output": "sent"}

    def rysh_exec(env, command, pane_id=None, timeout=30):
        m.calls.append(("rysh_exec", command, pane_id))
        return ""

    m.list_panes, m.find_pane, m.op_send, m.rysh_exec = (
        list_panes, find_pane, op_send, rysh_exec)
    m.annotate_ids = lambda panes: None
    m.TAB_LINE = m.LANE_LINE = m.GROUP_LINE = m.PANE_LINE = m.META_RE = m.RUNNING_RE = None
    m.vt_screen = lambda env, pane: {"lines": []}
    m.visible_solid = lambda x: x
    m.is_busy = lambda lines: False
    m.has_composer = lambda lines: True
    sys.modules["ryshfan"] = m
    return m


STUB = _install_ryshfan_stub()
os.environ.setdefault("RYSHFAN_DIR", HERE)
sys.path.insert(0, HERE)
import fleetctl  # noqa: E402


def make_env_and_manifest(tmp):
    """Write a REAL manifest to disk.

    deliver() now read-modify-writes the manifest under a lock (F-22), reading a
    FRESH copy inside the critical section — so an in-memory-only manifest is no
    longer a valid stand-in. Stubbing save_manifest to a no-op would defeat the
    fix rather than test around it.
    """
    env = {"workspace": tmp, "session": "test-session", "bin": "/nonexistent/rysh"}
    man = {
        "fleet": "testfleet",
        "msg_seq": 0,
        "log": [],
        "ceo": {"role": "ceo", "unit": "-", "pane": "pane-ceo", "label": "ceo"},
        "units": [],
    }
    os.makedirs(os.path.join(tmp, ".rysh", "fleet"), exist_ok=True)
    with open(os.path.join(tmp, ".rysh", "fleet", "testfleet.json"), "w") as fh:
        json.dump(man, fh)
    return env, man


SENDER = {"role": "manager", "unit": "01", "pane": "pane-sender-uuid-full",
          "label": "mgr-01"}
RECIPIENT = {"role": "worker", "unit": "01", "pane": "pane-recipient",
             "label": "wkr-01"}


class MirrorCannotBreakDelivery(unittest.TestCase):
    """The headline. Each test rigs the mirror to fail a different real way."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.env, self.man = make_env_and_manifest(self.tmp)
        STUB.calls.clear()
        os.environ["RYSH_FLEET_BOARD"] = "1"

    def _deliver(self):
        return fleetctl.deliver(self.env, self.man, SENDER, RECIPIENT,
                                "do the thing", "WORK ORDER", None)

    def _assert_delivered(self, res):
        self.assertEqual(res["msg_id"], "msg-0001")
        self.assertTrue(res["submitted"])
        self.assertEqual(res["to"], "wkr-01")
        self.assertIn("[FLEET testfleet", res["envelope"])
        sends = [c for c in STUB.calls if c[0] == "op_send"]
        self.assertEqual(len(sends), 1, "the recipient must still be typed into exactly once")
        self.assertEqual(len(self.man["log"]), 1, "the message must still be logged")

    def test_delivery_survives_mirror_raising(self):
        def boom(*a, **k):
            raise RuntimeError("board exploded")
        fleetctl.subprocess.run = boom
        try:
            self._assert_delivered(self._deliver())
        finally:
            fleetctl.subprocess.run = subprocess.run

    def test_delivery_survives_mirror_timeout(self):
        def slow(*a, **k):
            raise subprocess.TimeoutExpired(cmd="rysh", timeout=2.0)
        fleetctl.subprocess.run = slow
        try:
            self._assert_delivered(self._deliver())
        finally:
            fleetctl.subprocess.run = subprocess.run

    def test_delivery_survives_no_rysh_binary(self):
        # env["bin"] points at a path that does not exist: the real failure when
        # fleetctl runs somewhere the daemon is not installed.
        self._assert_delivered(self._deliver())

    def test_mirror_itself_never_raises_on_malformed_input(self):
        """The mirror's contract, asserted directly.

        Called with a sender that has no "pane" key and a record with no
        "kind" — the shapes a future refactor could hand it. board_mirror must
        swallow it, because it runs after the message is already delivered and
        there is nothing useful to fail into.

        Note this is asserted against board_mirror and NOT through deliver():
        a sender with no "pane" fails earlier, in envelope_line, which is
        pre-existing behaviour and not something this hook may claim to fix.
        """
        fleetctl.board_mirror(self.env, self.man,
                              {"role": "manager", "label": "mgr-01"},
                              RECIPIENT, {}, "body", None)  # must not raise


class MirrorShape(unittest.TestCase):
    """What the mirror sends, when it does send."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.env, self.man = make_env_and_manifest(self.tmp)
        STUB.calls.clear()
        os.environ["RYSH_FLEET_BOARD"] = "1"
        # Pin DELIVERY to the typing path so `self.ran` contains only the
        # MIRROR's subprocess call. Since W4-3a flipped ANSA on by default,
        # fleet_send also shells out, and a counter that cannot tell delivery
        # from mirroring would silently start measuring the wrong thing.
        # Delivery-path behaviour is tested in test_fleetctl_ansa.py; these
        # tests are about the mirror.
        os.environ["RYSH_FLEET_ANSA"] = "0"
        self.ran = []
        fleetctl.subprocess.run = lambda argv, **k: self.ran.append((argv, k)) or types.SimpleNamespace(
            stdout="", stderr="", returncode=0)

    def tearDown(self):
        fleetctl.subprocess.run = subprocess.run
        os.environ.pop("RYSH_FLEET_BOARD", None)
        os.environ.pop("RYSH_FLEET_ANSA", None)

    def test_f21_uses_the_agent_door_not_the_human_one(self):
        """F-21, and the reason it is tested ENABLED.

        The mirror used `rysh exec -- '##board post'`. That is the HUMAN door:
        it routes through runRyshCommand, which ECHOES the command line into a
        pane's output buffers, and with no --pane-id the target is the ambient
        active pane -- so every mirrored fleet message printed into whichever
        pane a human was looking at.

        Nobody caught it because the mirror is off by default and every test
        asserted dormancy. Default-off is a SCHEDULING property, not a safety
        property: it defers the risk to whoever flips the flag.
        """
        fleetctl.deliver(self.env, self.man, SENDER, RECIPIENT, "hello", "PROGRESS", None)
        self.assertEqual(len(self.ran), 1)
        argv = self.ran[0][0]
        self.assertIn("board", argv)
        self.assertIn("post", argv)
        joined = " ".join(argv)
        self.assertNotIn("exec", argv,
                         "the ## path echoes into a bystander's pane; use the agent door")
        self.assertNotIn("##board", joined,
                         "## is the human door -- it echoes; MsgCLIBoardPost does not")

    def test_never_passes_pane_id_which_would_steal_focus(self):
        """Hazard 2, pinned.

        `rysh exec --pane-id X` reaches focusPaneByID in the WorkspaceActor and
        switches the active tab under whoever is watching. The mirror must carry
        its poster as `--as` inside the board command instead. If this assertion
        ever fails, every fleet message yanks the human's focus.
        """
        fleetctl.deliver(self.env, self.man, SENDER, RECIPIENT, "hello", "PROGRESS", None)
        self.assertEqual(len(self.ran), 1)
        argv = self.ran[0][0]
        self.assertNotIn("--pane-id", argv)
        joined = " ".join(argv)
        self.assertIn("--as", argv)
        self.assertIn("pane-sender-uuid-full", argv)

    def test_carries_full_pane_uuid_not_the_envelope_truncation(self):
        """The envelope shortens pane ids to 8 chars; identity needs the whole
        uuid, because given-names are only lane-unique (design 025 §5)."""
        fleetctl.deliver(self.env, self.man, SENDER, RECIPIENT, "hello", "PROGRESS", None)
        joined = " ".join(self.ran[0][0])
        self.assertIn(SENDER["pane"], joined)
        self.assertIn("pane pane-sen", " ".join(c[2] for c in STUB.calls if c[0] == "op_send"))

    def test_gate4_the_fleet_envelope_never_reaches_the_board(self):
        """Founder ruling, gate 4: a post carries persona + thread, NOT the
        routing envelope — and the hook must not smuggle it back in via the
        post text either. Asserted on the whole argv, not just a named flag."""
        fleetctl.deliver(self.env, self.man, SENDER, RECIPIENT, "hello", "PROGRESS", None)
        joined = " ".join(self.ran[0][0])
        self.assertNotIn("--envelope", joined)
        self.assertNotIn("[FLEET", joined)
        self.assertNotIn("FROM manager", joined)
        self.assertNotIn(" TO ", joined)
        self.assertNotIn("msg-0001", joined)
        self.assertIn("hello", joined)          # what was said still travels

    def test_mirror_is_off_unless_opted_in(self):
        """Default-off, because an automatic post is the opposite of the
        founder's working default that an agent makes an explicit call."""
        os.environ.pop("RYSH_FLEET_BOARD", None)
        res = fleetctl.deliver(self.env, self.man, SENDER, RECIPIENT, "hi", "REPORT", None)
        self.assertEqual(self.ran, [], "the mirror must not fire by default")
        self.assertTrue(res["submitted"])

    def test_body_file_is_referenced_when_there_is_no_inline_body(self):
        fleetctl.deliver(self.env, self.man, SENDER, RECIPIENT, "", "WORK ORDER",
                         "/tmp/order.md")
        joined = " ".join(self.ran[0][0])
        self.assertIn("/tmp/order.md", joined)

    def test_explicit_zero_disables_the_mirror(self):
        os.environ["RYSH_FLEET_BOARD"] = "0"
        res = fleetctl.deliver(self.env, self.man, SENDER, RECIPIENT, "hi", "REPORT", None)
        self.assertEqual(self.ran, [], "no board call may be made when disabled")
        self.assertTrue(res["submitted"], "delivery must still work with the mirror off")

    def test_mirror_runs_after_the_manifest_is_saved(self):
        """Ordering matters: if the mirror ever does hang for its full timeout,
        the message must already be delivered and recorded."""
        order = []
        real_save = fleetctl.save_manifest
        fleetctl.save_manifest = lambda env, man: (order.append("save"), real_save(env, man))[1]
        fleetctl.subprocess.run = lambda argv, **k: order.append("mirror") or types.SimpleNamespace(
            stdout="", stderr="", returncode=0)
        fleetctl.deliver(self.env, self.man, SENDER, RECIPIENT, "hi", "REPORT", None)
        # TWO saves since F-22: the msg-id is allocated in one locked
        # read-modify-write and the log record appended in another, with the
        # send in between so a file lock never spans a network delivery.
        # Asserting the ORDER rather than the count keeps the intent -- the
        # mirror runs last -- without pinning an implementation detail.
        self.assertEqual(order[-1], "mirror", f"mirror must run last, got {order}")
        self.assertIn("save", order, f"the manifest must be saved before mirroring: {order}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
