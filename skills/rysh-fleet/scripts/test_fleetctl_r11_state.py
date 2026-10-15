#!/usr/bin/env python3
"""Founder ruling R11 (2026-10-07): no fleet cap; fleet state is recorded truthfully.

`up` writes `up` only after a launch that succeeded, a failed launch never says
`up`, `down` writes `down`, and the limit on spend is the session token-ceiling
check made BEFORE anything launches. The live daemon may still run a binary that
enforces D-14's three-fleet cap, so every path here must also be right when the
registry refuses `up`: report it, and leave the panes alone.

Run: python3 test_fleetctl_r11_state.py
"""

import argparse
import io
import os
import sys
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "..", "rysh-fanout", "scripts"))

import fleetctl as F  # noqa: E402

# The REAL budget check, loaded by path. In a full `unittest` run an earlier
# module (test_fleetctl_ansa, _board, _via_audit) installs a stub `ryshfan` in
# sys.modules before fleetctl is first imported, so `F.R` may be that stub, with
# no `discover` and a stub `budget_blocked`. Loading the real one here, and
# patching with create=True, keeps these tests meaning the same thing in any
# order.
import importlib.util  # noqa: E402
_spec = importlib.util.spec_from_file_location(
    "ryshfan_real_for_r11", os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                         "..", "..", "rysh-fanout", "scripts", "ryshfan.py"))
REAL_R = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(REAL_R)


def patch_r(name, **kw):
    return mock.patch.object(F.R, name, create=True, **kw)


def real_budget():
    return patch_r("budget_blocked", side_effect=REAL_R.budget_blocked)

ENV = {"workspace": "/tmp/ws", "session": "s", "bin": "rysh"}
OLD_DAEMON_REFUSAL = ("3 fleets are already up (a, b, c) and the cap is 3 — take one "
                      "down first, or register \"e\" and promote it when a slot frees.")


def manifest(n=3, **extra):
    members = [{"role": "worker", "unit": f"{i:02d}", "pane": f"pane-{i}",
                "label": f"wkr-{i}", "parent": None, "roadmap": "r.md"} for i in range(n)]
    man = {"fleet": "e", "members": members, "units": [], "lanes": {}, "log": []}
    man.update(extra)
    return man


def panes_for(man, missing=()):
    return [{"id": m["pane"], "name": m["label"], "program": "2.1.288"}
            for m in man["members"] if m["pane"] not in missing]


def args(**kw):
    base = dict(pattern="*.md", split_on="", scan_source=False, max_units=30,
                limit=0, promote=3, template="epic", workers=1, mission="",
                roadmap_dir="new_roadmap/fleet", worktrees=False, base_branch="dev",
                ceo_here=False, claude_args="", force=False, dry_run=False, source="")
    base.update(kw)
    ns = argparse.Namespace(**base)
    setattr(ns, "from", kw.get("from_", "new_roadmap/tracks/fleet"))
    return ns


UNITS = [{"unit": f"{i:02d}", "title": f"Epic {i} — a thing", "sources": [f"epic{i:02d}.md"]}
         for i in range(1, 6)]
NO_CEILING = {"tokens": 1.0, "dollars": 0.0, "ceilings": []}
AT_CEILING = {"tokens": 1.0, "dollars": 0.0,
              "ceilings": [{"pane": "abc", "spent": "1m", "ceiling": "1m", "pct": 100}]}


class TheLaunchIsJudgedBeforeItIsRecorded(unittest.TestCase):
    """The success signal: every planned member got a pane, every member's pane
    still exists after the launch, and no member's worktree failed."""

    def test_a_complete_launch_is_ok(self):
        man = manifest()
        with patch_r("list_panes", return_value=panes_for(man)):
            res = F.launch_outcome(ENV, man, planned=3)
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["problems"], [])

    def test_a_vanished_pane_is_a_failed_launch(self):
        man = manifest()
        with patch_r("list_panes", return_value=panes_for(man, {"pane-1"})):
            res = F.launch_outcome(ENV, man, planned=3)
        self.assertFalse(res["ok"])
        self.assertTrue(any("wkr-1" in p for p in res["problems"]), res)

    def test_a_member_with_no_pane_is_a_failed_launch(self):
        man = manifest(3)
        with patch_r("list_panes", return_value=panes_for(man)):
            res = F.launch_outcome(ENV, man, planned=4)
        self.assertFalse(res["ok"])
        self.assertTrue(any("3 of 4" in p for p in res["problems"]), res)

    def test_a_failed_worktree_is_a_failed_launch(self):
        # An agent with no worktree edits the shared main checkout — the one
        # failure the worktree rule exists to prevent.
        man = manifest()
        man["members"][2]["worktree_error"] = "branch already checked out"
        with patch_r("list_panes", return_value=panes_for(man)):
            res = F.launch_outcome(ENV, man, planned=3)
        self.assertFalse(res["ok"])
        self.assertTrue(any("wkr-2" in p and "worktree" in p for p in res["problems"]), res)


class UpIsWrittenOnlyAfterASuccessfulLaunch(unittest.TestCase):
    def test_success_records_up(self):
        with mock.patch.object(F, "registry_sync",
                               return_value={"registered": True, "members": 3, "errors": []}) as rs:
            st = F.record_launch_state(ENV, manifest(), {"ok": True, "problems": []})
        rs.assert_called_once()
        self.assertEqual(rs.call_args.args[2], "up")
        self.assertEqual(st["wanted"], "up")
        self.assertTrue(st["recorded"])

    def test_failure_never_records_up(self):
        with mock.patch.object(F, "registry_sync",
                               return_value={"registered": True, "members": 3, "errors": []}) as rs:
            st = F.record_launch_state(ENV, manifest(), {"ok": False, "problems": ["x"]})
        self.assertNotIn("up", [c.args[2] for c in rs.call_args_list])
        self.assertEqual(st["wanted"], "registered")

    def test_an_old_daemon_refusing_up_is_reported_and_the_panes_are_left_running(self):
        # The live daemon runs a pre-R11 binary until somebody restarts it. Its
        # refusal is a fact about the RECORD, not about the fleet: the agents are
        # up, so tearing them down would destroy working agents to make a row
        # agree with an obsolete cap.
        reg = {"registered": True, "members": 3, "errors": [OLD_DAEMON_REFUSAL],
               "state_error": OLD_DAEMON_REFUSAL}
        with mock.patch.object(F, "registry_sync", return_value=reg), \
             patch_r("rysh_exec") as rx:
            st = F.record_launch_state(ENV, manifest(), {"ok": True, "problems": []})
        self.assertFalse(st["recorded"])
        self.assertIn("cap is 3", st["reason"])
        self.assertIn("restart", st["note"])
        for c in rx.call_args_list:
            self.assertNotIn("delete", " ".join(map(str, c.args)), "a refusal must not take panes down")

    def test_a_launch_that_dies_midway_records_registered_and_reraises(self):
        man = manifest()
        calls = {"n": 0}

        def start(*a, **k):
            calls["n"] += 1
            if calls["n"] == 2:
                raise SystemExit(1)
            return {"session_id": "sid"}
        with mock.patch.object(F, "start_agent", side_effect=start), \
             mock.patch.object(F, "build_brief", return_value="brief"), \
             mock.patch.object(F, "stamp_launch"), \
             mock.patch.object(F, "save_manifest"), \
             patch_r("list_panes", return_value=panes_for(man)), \
             mock.patch.object(F, "registry_sync",
                               return_value={"registered": True, "members": 3, "errors": []}) as rs:
            with self.assertRaises(SystemExit):
                F.launch_roster(ENV, man, args(), "claude", "")
        states = [c.args[2] for c in rs.call_args_list]
        self.assertEqual(states, ["registered"], f"a dead launch recorded {states}")

    def test_op_up_is_wired_to_the_judged_path(self):
        # Tested code nothing calls counts as not done.
        import inspect
        src = inspect.getsource(F.op_up)
        for fn in ("launch_roster(", "launch_outcome(", "record_launch_state("):
            self.assertIn(fn, src, f"op_up does not call {fn}")
        self.assertNotIn('registry_sync(env, man, "up")', src,
                         "op_up still writes up unconditionally")

    def test_a_failed_up_exits_non_zero(self):
        failed = {"fleet": "e", "ok": False, "launch": {"ok": False, "problems": ["x"]}}
        with patch_r("discover", return_value=ENV), \
             mock.patch.object(F, "op_up", return_value=failed), \
             mock.patch.object(sys, "argv", ["fleetctl", "--fleet", "e", "up", "--from", "x"]):
            with redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit) as cm:
                    F.main()
        self.assertNotEqual(cm.exception.code, 0)


class DownIsRecorded(unittest.TestCase):
    def test_down_writes_down(self):
        man = manifest()
        calls = []
        with patch_r("discover", return_value=ENV), \
             mock.patch.object(F, "load_manifest", return_value=man), \
             mock.patch.object(F, "op_down", return_value={"closed": []}), \
             mock.patch.object(F, "registry", side_effect=lambda env, *a: calls.append(a) or {}), \
             mock.patch.object(F.os, "replace"), \
             mock.patch.object(sys, "argv", ["fleetctl", "--fleet", "e", "down"]):
            with redirect_stdout(io.StringIO()):
                F.main()
        self.assertIn(("state", "e", "down"), calls)


class EpicsWithoutACap(unittest.TestCase):
    def setUp(self):
        self.calls = []

    def reg(self, env, *argv):
        self.calls.append(argv)
        return {}

    def test_no_slot_is_reserved_before_a_launch(self):
        # Reserving `up` before anything launched is the lie R11 ends: the row
        # said up while the panes did not yet exist.
        with mock.patch.object(F, "discover_units", return_value=UNITS), \
             mock.patch.object(F, "registry", side_effect=self.reg), \
             real_budget(), \
             patch_r("session_cost", return_value=NO_CEILING), \
             mock.patch.object(F, "op_up", return_value={"panes": 6, "ok": True}):
            res = F.op_epics(ENV, args(promote=5))
        self.assertEqual(len(res["up"]), 5, "with no cap, all five may come up")
        self.assertFalse([c for c in self.calls if c[0] == "state" and c[2] == "up"],
                         f"the driver wrote up itself: {self.calls}")

    def test_the_token_ceiling_is_checked_before_each_launch_and_stops_the_run(self):
        costs = [NO_CEILING, NO_CEILING, AT_CEILING]
        with mock.patch.object(F, "discover_units", return_value=UNITS), \
             mock.patch.object(F, "registry", side_effect=self.reg), \
             real_budget(), \
             patch_r("session_cost", side_effect=costs + [AT_CEILING] * 5), \
             mock.patch.object(F, "op_up", return_value={"panes": 6, "ok": True}) as up:
            res = F.op_epics(ENV, args(promote=5))
        self.assertEqual(up.call_count, 2)
        self.assertTrue(res["budget_blocked"], res)
        self.assertIn("token ceiling", res["budget_blocked"])

    def test_force_bypasses_the_ceiling_as_up_does(self):
        with mock.patch.object(F, "discover_units", return_value=UNITS), \
             mock.patch.object(F, "registry", side_effect=self.reg), \
             real_budget(), \
             patch_r("session_cost", return_value=AT_CEILING), \
             mock.patch.object(F, "op_up", return_value={"panes": 6, "ok": True}) as up:
            F.op_epics(ENV, args(promote=2, force=True))
        self.assertEqual(up.call_count, 2)

    def test_a_launch_that_was_not_ok_is_failed_not_up(self):
        with mock.patch.object(F, "discover_units", return_value=UNITS), \
             mock.patch.object(F, "registry", side_effect=self.reg), \
             real_budget(), \
             patch_r("session_cost", return_value=NO_CEILING), \
             mock.patch.object(F, "op_up", return_value={"panes": 6, "ok": False,
                                                         "launch": {"problems": ["x"]}}):
            res = F.op_epics(ENV, args(promote=1))
        self.assertEqual(res["up"], [])
        self.assertEqual(len(res["failed"]), 1)

    def test_promote_is_a_driver_flag_not_a_registry_cap(self):
        with mock.patch.object(F, "discover_units", return_value=UNITS), \
             mock.patch.object(F, "registry", side_effect=self.reg), \
             real_budget(), \
             patch_r("session_cost", return_value=NO_CEILING), \
             mock.patch.object(F, "op_up", return_value={"panes": 6, "ok": True}) as up:
            F.op_epics(ENV, args(promote=2))
        self.assertEqual(up.call_count, 2)


class PromoteSkipsFleetsThatAreAlreadyRunning(unittest.TestCase):
    def test_a_registered_fleet_whose_panes_are_live_is_not_rebuilt(self):
        # On a pre-R11 daemon every live fleet past the third reads
        # `registered`. Promoting one would rebuild a running fleet over itself.
        listing = {"fleets": [
            {"name": "live", "state": "registered", "source": "a.md",
             "members": [{"pane_id": "p1"}]},
            {"name": "idle", "state": "registered", "source": "b.md",
             "members": [{"pane_id": "gone"}]}]}

        def reg(env, *argv):
            return listing if argv[0] == "ls" else {}
        with mock.patch.object(F, "registry", side_effect=reg), \
             patch_r("list_panes", return_value=[{"id": "p1", "program": "2.1.288"}]), \
             real_budget(), \
             patch_r("session_cost", return_value=NO_CEILING), \
             mock.patch.object(F, "op_up", return_value={"panes": 6, "ok": True}):
            res = F.op_epics_promote(ENV, args())
        self.assertEqual(res["promoted"], "idle")

    def test_promote_respects_the_token_ceiling(self):
        listing = {"fleets": [{"name": "idle", "state": "registered", "source": "b.md",
                               "members": []}]}

        def reg(env, *argv):
            return listing if argv[0] == "ls" else {}
        with mock.patch.object(F, "registry", side_effect=reg), \
             patch_r("list_panes", return_value=[]), \
             real_budget(), \
             patch_r("session_cost", return_value=AT_CEILING), \
             mock.patch.object(F, "op_up") as up:
            res = F.op_epics_promote(ENV, args())
        self.assertIsNone(res["promoted"])
        self.assertIn("token ceiling", res["reason"])
        up.assert_not_called()


class Reconcile(unittest.TestCase):
    LISTING = {"fleets": [
        {"name": "running-but-registered", "state": "registered",
         "members": [{"pane_id": "a"}, {"pane_id": "b"}]},
        {"name": "running-and-up", "state": "up", "members": [{"pane_id": "c"}]},
        {"name": "gone-but-up", "state": "up", "members": [{"pane_id": "zz"}]},
        {"name": "never-started", "state": "registered", "members": []}]}
    PANES = [{"id": "a", "program": "2.1.288"}, {"id": "b", "program": ""},
             {"id": "c", "program": "2.1.288"}]

    def run_reconcile(self, apply, state_reply=None):
        calls = []

        def reg(env, *argv):
            calls.append(argv)
            if argv[0] == "ls":
                return self.LISTING
            return state_reply or {}
        with mock.patch.object(F, "registry", side_effect=reg), \
             patch_r("list_panes", return_value=self.PANES):
            res = F.op_reconcile(ENV, apply=apply)
        return res, calls

    def test_dry_run_is_the_default_and_writes_nothing(self):
        res, calls = self.run_reconcile(apply=False)
        self.assertTrue(res["dry_run"])
        self.assertEqual([c for c in calls if c[0] != "ls"], [])
        plan = {r["fleet"]: r for r in res["fleets"]}
        self.assertEqual(plan["running-but-registered"]["to"], "up")
        self.assertEqual(plan["running-but-registered"]["panes_live"], 2)
        self.assertEqual(plan["running-but-registered"]["agents_running"], 1)
        self.assertIsNone(plan["running-and-up"]["to"])
        self.assertEqual(plan["gone-but-up"]["to"], "down")
        self.assertIsNone(plan["never-started"]["to"], "a fleet never brought up stays registered")

    def test_apply_writes_only_the_changes(self):
        res, calls = self.run_reconcile(apply=True)
        writes = sorted(c for c in calls if c[0] == "state")
        self.assertEqual(writes, [("state", "gone-but-up", "down"),
                                  ("state", "running-but-registered", "up")])
        self.assertFalse(res["dry_run"])

    def test_an_old_daemon_refusal_is_reported_per_fleet(self):
        res, _ = self.run_reconcile(apply=True, state_reply={"error": OLD_DAEMON_REFUSAL})
        refused = [r for r in res["fleets"] if r.get("error")]
        self.assertTrue(refused)
        self.assertIn("cap is 3", refused[0]["error"])


if __name__ == "__main__":
    unittest.main()
