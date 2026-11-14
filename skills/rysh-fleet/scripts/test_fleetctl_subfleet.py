#!/usr/bin/env python3
"""`fleetctl subfleet`: a manager-TIER member grows its
own team, in every template and in the manifest shape fleets are built with.

Two defects pinned here:
  1. The role test was the literal `"manager"` (68a2836c, three-tier only), so
     every epic template's `fleet-manager` was refused: "only a manager builds a
     sub-fleet; the CEO delegates to managers".
  2. Past that test, op_subfleet read `man["units"][i]["manager"]` and appended
     to `unit["workers"]` — the older manifest shape (the members manifest moved the roster to
     `members`). Every fleet built since then has no such keys, so a sub-fleet
     could not be built in ANY template.

Run: python3 test_fleetctl_subfleet.py
"""

import argparse
import importlib.util
import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "..", "rysh-fanout", "scripts"))

import fleetctl as F  # noqa: E402

# An earlier module may have left a STUB ryshfan behind fleetctl:
# load the real one by path and point fleetctl at it per test.
_spec = importlib.util.spec_from_file_location(
    "ryshfan_real_for_subfleet", os.path.join(HERE, "..", "..", "rysh-fanout", "scripts", "ryshfan.py"))
REAL_R = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(REAL_R)

ENV = {"workspace": "/ws", "session": "s", "bin": "rysh"}


def args(**kw):
    a = dict(agent="", agent_args="", worktrees=False, base_branch="dev")
    a.update(kw)
    return argparse.Namespace(**a)


def epic_quad():
    return {"fleet": "epic-9-example", "template": "epic-quad", "tab": "tab-9",
            "roadmap_dir": "docs/fleet", "agent": "claude", "agent_args": "--x",
            "units": [{"unit": "9", "title": "Example epic", "sources": ["docs/epics/example.md"]}],
            "members": [
                {"role": "roadmap-manager", "unit": "-", "pane": "lead-0000", "label": "epic-9-example-roadmap", "parent": None},
                {"role": "fleet-manager", "unit": "9", "pane": "fmgr-0000", "label": "fmgr-9-example-epic",
                 "parent": "lead-0000", "title": "Example epic"},
                {"role": "worker", "unit": "9", "pane": "wkr1-0000", "label": "wkr-9-example-epic",
                 "parent": "fmgr-0000", "title": "Example epic"},
            ]}


def three_tier_members():
    return {"fleet": "f", "template": "three-tier", "tab": "tab-1", "roadmap_dir": "rd",
            "units": [{"unit": "01", "title": "Sell revenue", "sources": []}],
            "members": [
                {"role": "ceo", "unit": "-", "pane": "ceo-0000", "label": "f-ceo", "parent": None},
                {"role": "manager", "unit": "01", "pane": "mgr-0000", "label": "mgr-01-sell-revenue", "parent": "ceo-0000"},
                {"role": "worker", "unit": "01", "pane": "w1-0000", "label": "wkr-01-sell-revenue", "parent": "mgr-0000"},
            ]}


def three_tier_legacy():
    return {"fleet": "f", "template": "three-tier", "tab": "tab-1", "roadmap_dir": "rd",
            "ceo": {"role": "ceo", "unit": "-", "pane": "ceo-0000", "label": "f-ceo", "parent": None},
            "units": [{"unit": "01", "title": "Sell revenue",
                       "manager": {"role": "manager", "unit": "01", "pane": "mgr-0000",
                                   "label": "mgr-01-sell-revenue", "parent": "ceo-0000"},
                       "workers": [{"role": "worker", "unit": "01", "pane": "w1-0000",
                                    "label": "wkr-01-sell-revenue", "parent": "mgr-0000"}]}]}


class Harness:
    """Every daemon and launch effect of op_subfleet, faked and recorded."""

    def __init__(self, man):
        self.man = man
        self.meta, self.saved, self.started, self.comms = [], 0, [], []
        self.panes = [{"id": p["pane"], "name": p["label"], "lane": 1, "tab": man["tab"]}
                      for p in F.roster(man)]

    def new_group_in_lane(self, env, tab, lane, count):
        fresh = [{"id": f"new{i}-{len(self.panes)}", "name": f"auto-{i}", "lane": lane, "tab": tab}
                 for i in range(count)]
        self.panes += fresh
        return fresh

    def start_agent(self, env, pane, prompt, worktree, launch_args, agent, fleet, comm=None,
                    full_auto=False):
        self.started.append(pane["id"])
        self.comms.append(comm)
        return {"session_id": pane["id"], "cwd": "/ws"}

    def __enter__(self):
        self.ps = [
            mock.patch.object(F, "R", REAL_R),
            mock.patch.object(REAL_R, "list_panes", lambda env, pane_id=None: list(self.panes)),
            mock.patch.object(F, "new_group_in_lane", self.new_group_in_lane),
            mock.patch.object(F, "rename", lambda env, pane, label: None),
            mock.patch.object(F, "set_meta", lambda env, pane, k, v: self.meta.append((pane, k, v))),
            mock.patch.object(F, "build_brief", lambda env, man, w, a: "brief"),
            mock.patch.object(F, "start_agent", self.start_agent),
            mock.patch.object(F, "stamp_launch", lambda *a, **k: None),
            mock.patch.object(F, "save_manifest", lambda env, man: setattr(self, "saved", self.saved + 1)),
        ]
        for p in self.ps:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in reversed(self.ps):
            p.stop()


def member(man, role):
    return next(p for p in F.roster(man) if p["role"] == role)


class AManagerTierMemberBuildsASubfleet(unittest.TestCase):

    def check_builds(self, man, role, unit):
        boss = member(man, role)
        before = len(F.roster(man))
        with Harness(man) as h:
            res = F.op_subfleet(ENV, man, boss, 2, args())
        added = res["added"]
        self.assertEqual(len(added), 2, res)
        roster = F.roster(man)
        self.assertEqual(len(roster), before + 2, "the new workers must be IN the roster")
        for a in added:
            w = next(p for p in roster if p["pane"] == a["pane"])
            self.assertEqual(w["role"], "worker")
            self.assertEqual(str(w["unit"]), unit)
            self.assertEqual(w["parent"], boss["pane"], "a sub-fleet worker reports to its builder")
            self.assertEqual(F.parent_of(man, w)["pane"], boss["pane"], "report goes up to the builder")
            self.assertIn((w["pane"], F.META_PARENT, boss["pane"]), h.meta)
            self.assertIn((w["pane"], F.META_ROLE, "worker"), h.meta)
        self.assertEqual(len({a["label"] for a in added} | {p["label"] for p in roster if p["pane"] not in
                                                            {a["pane"] for a in added}}),
                         len(roster), "labels stay unique")
        self.assertEqual(h.started, [a["pane"] for a in added])
        self.assertEqual(h.comms, [F.fleet_comm(man)] * len(added), "each sub-fleet worker is launched with the fleet's comm")
        self.assertEqual(h.saved, 1)
        # The builder now sees them as its children, so `msg` down the chain works.
        kids = {p["pane"] for p in F.children_of(man, boss)}
        self.assertTrue({a["pane"] for a in added} <= kids)
        return res

    def test_an_epic_fleet_manager_builds_a_subfleet(self):
        res = self.check_builds(epic_quad(), "fleet-manager", "9")
        self.assertEqual(res["unit"], "9")

    def test_a_three_tier_manager_in_the_members_shape_builds_a_subfleet(self):
        self.check_builds(three_tier_members(), "manager", "01")

    def test_the_legacy_manifest_shape_still_works(self):
        man = three_tier_legacy()
        boss = man["units"][0]["manager"]
        with Harness(man):
            res = F.op_subfleet(ENV, man, boss, 1, args())
        self.assertEqual(len(man["units"][0]["workers"]), 2)
        self.assertEqual(man["units"][0]["workers"][-1]["parent"], "mgr-0000")
        self.assertEqual(res["unit"], "01")


class OnlyTheManagerTierDoes(unittest.TestCase):

    def refused(self, man, role):
        with Harness(man), self.assertRaises(SystemExit), \
             mock.patch("builtins.print") as pr:
            F.op_subfleet(ENV, man, member(man, role), 1, args())
        return " ".join(str(c.args[0]) for c in pr.call_args_list)

    def test_a_worker_is_refused(self):
        self.assertIn("manager-tier", self.refused(epic_quad(), "worker"))

    def test_the_head_of_the_chain_is_refused(self):
        self.assertIn("manager-tier", self.refused(epic_quad(), "roadmap-manager"))
        self.assertIn("manager-tier", self.refused(three_tier_members(), "ceo"))

    def test_the_tier_comes_from_the_template_not_a_name_list(self):
        want = {"three-tier": {"manager"}, "epic": {"fleet-manager"}, "epic-flat": {"fleet-manager"},
                "epic-quad": {"fleet-manager"}, "duo": set(), "portfolio": set()}
        for name, roles in want.items():
            with self.subTest(template=name):
                self.assertEqual(F.manager_tier_roles({"template": name}), roles)
        self.assertEqual(set(F.TEMPLATES), set(want), "a new template must be classified here too")


if __name__ == "__main__":
    unittest.main()
