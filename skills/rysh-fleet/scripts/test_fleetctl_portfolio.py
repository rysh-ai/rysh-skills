#!/usr/bin/env python3
"""The `portfolio` template: one lead over a whole roadmap, one worker per epic.

Pins the three things that make it usable for "a fleet for every epic in
new_roadmap/, one worker each, in one stack":

  1. the template has exactly that shape, and `wkr-<epic>` / `lead` resolve;
  2. both briefs render completely, and say what is TRUE for this fleet — no
     "exactly two agents", and a "nobody is watching" paragraph only when the
     pane really is sync-paused;
  3. `--exclude` drops retired epics without losing a two-file unit.

Run: python3 test_fleetctl_portfolio.py
"""

import argparse
import os
import re
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "..", "rysh-fanout", "scripts"))

import fleetctl as F  # noqa: E402


def manifest(web_sync="root", n=3):
    lead = {"role": "portfolio-lead", "unit": "-", "pane": "p-lead", "label": "pf-lead",
            "parent": None, "roadmap": "r/portfolio-leads/pf-lead.md"}
    members = [lead]
    units = []
    for i in range(1, n + 1):
        u = f"{i + 9}"
        units.append({"unit": u, "title": f"E{u} — epic {u}",
                      "sources": [f"/ws/new_roadmap/epics/E{u}-x.md"]})
        members.append({"role": "worker", "unit": u, "pane": f"p-w{u}",
                        "label": f"wkr-{u}-epic", "parent": "p-lead",
                        "roadmap": f"r/workers/wkr-{u}-epic.md", "title": units[-1]["title"],
                        "sources": units[-1]["sources"]})
    return {"fleet": "pf", "template": "portfolio", "roadmap_dir": "r", "msg_seq": 0,
            "units": units, "members": members, "web_sync": web_sync, "agent": "claude"}


class Args:
    mission = "advance every epic"


def brief(man, label):
    env = {"workspace": "/ws", "bin": os.path.abspath(sys.executable)}
    return F.build_brief(env, man, F.resolve(man, label), Args())


class PortfolioTemplate(unittest.TestCase):
    def test_shape(self):
        specs = F.template_specs("portfolio")
        self.assertEqual([(s["role"], s["parent"], s["scope"], s["count"]) for s in specs],
                         [("portfolio-lead", None, "fleet", 1),
                          ("worker", "portfolio-lead", "unit", "workers")])

    def test_short_forms_resolve(self):
        man = manifest()
        self.assertEqual(F.resolve(man, "lead")["pane"], "p-lead")
        self.assertEqual(F.resolve(man, "wkr-11")["pane"], "p-w11")
        self.assertEqual(F.top_role(man), "portfolio-lead")

    def test_briefs_render_completely_and_truthfully(self):
        man = manifest()
        for label in ("pf-lead", "wkr-10-epic"):
            text = brief(man, label)
            self.assertIsNone(re.search(r"\{\{[A-Z_]+\}\}", text), label)
            self.assertNotIn("exactly two", text, label)
            self.assertNotIn("roadmap agent", text, label)
        w = brief(man, "wkr-10-epic")
        self.assertIn("3 workers", w)
        self.assertIn("pf-lead", w)
        self.assertIn("E10-x.md", w)
        lead = brief(man, "pf-lead")
        self.assertIn("3 epics, 3 workers", lead)
        for u in ("wkr-10-epic", "wkr-11-epic", "wkr-12-epic"):
            self.assertIn(u, lead)

    def test_visibility_paragraph_follows_the_policy(self):
        paused = manifest("root")
        self.assertIn("Nobody is watching this pane", brief(paused, "wkr-10-epic"))
        self.assertIn("3 of this fleet's panes", brief(paused, "pf-lead"))
        self.assertNotIn("Nobody is watching", brief(paused, "pf-lead"))

        live = manifest("all")
        for label in ("pf-lead", "wkr-10-epic"):
            text = brief(live, label)
            self.assertNotIn("sync-paused", text, label)


class Exclude(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        for name in ("E1-retired.md", "E10-live.md", "E26-epic.md", "E26-D-26-packet.md",
                     "E27-manual.md", "README.md"):
            with open(os.path.join(self.dir, name), "w") as fh:
                fh.write(f"# {name}\n\nbody\n")

    def units(self, *excludes):
        ns = argparse.Namespace(source=self.dir, pattern="E*.md", scan_source=False,
                                split_on="", max_units=30, exclude=list(excludes))
        return sorted(u["unit"] for u in F.discover_units(ns))

    def test_no_exclude_keeps_everything(self):
        self.assertEqual(self.units(), sorted(["1", "10", "26", "27"]))

    def test_exclude_drops_whole_units(self):
        self.assertEqual(self.units("E1-*", "E27-*"), ["10", "26"])

    def test_a_two_file_unit_survives_one_file_matching(self):
        self.assertIn("26", self.units("E26-D-26-*"))


if __name__ == "__main__":
    unittest.main()
