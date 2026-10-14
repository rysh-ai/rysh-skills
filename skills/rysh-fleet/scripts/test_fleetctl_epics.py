#!/usr/bin/env python3
"""The epic driver (design 028 §6.8, `E-43`).

One fleet per epic doc, registered for free and promoted into a cap the DAEMON
holds. The tests fake the registry, which is the point: the driver's job is to
respect a refusal it did not compute, and faking the answer is the only way to
exercise the refused path without standing up four fleets.

Run: python3 test_fleetctl_epics.py
"""

import argparse
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "..", "rysh-fanout", "scripts"))

import fleetctl as F  # noqa: E402

ENV = {"workspace": "/tmp/ws", "session": "s", "bin": "rysh"}


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


class TestFleetNames(unittest.TestCase):
    def test_a_fleet_name_is_always_a_legal_board_id(self):
        # A fleet's board id defaults to its name and a board id is a NATS
        # subject token. An illegal one would address a board nobody subscribes
        # to — a silently empty stream rather than an error.
        for u in [{"unit": "07", "title": "Epic 07 — `agents-board`: a Slack-like view"},
                  {"unit": "01", "title": "A16Z / memo (draft!)"},
                  {"unit": "12", "title": "  spaces   and.dots  "}]:
            name = F.epic_fleet_name(u)
            self.assertRegex(name, r"^[a-z0-9][a-z0-9_-]*$", f"{name!r} is not a board id")
            self.assertLessEqual(len(name), 32)
            self.assertNotIn("..", name)

    def test_names_are_distinct_per_epic(self):
        names = {F.epic_fleet_name(u) for u in UNITS}
        self.assertEqual(len(names), len(UNITS))


class TestRegisterAllPromoteSome(unittest.TestCase):
    def setUp(self):
        self.calls = []

    def fake_registry(self, results=None):
        results = results or {}

        def _registry(env, *argv):
            self.calls.append(argv)
            key = (argv[0], argv[1] if len(argv) > 1 else "")
            return results.get(key, results.get(argv[0], {}))
        return _registry

    def test_every_epic_is_registered_and_only_promote_come_up(self):
        with mock.patch.object(F, "discover_units", return_value=UNITS), \
             mock.patch.object(F, "registry", side_effect=self.fake_registry()), \
             mock.patch.object(F, "op_up", return_value={"panes": 6}) as up:
            res = F.op_epics(ENV, args(promote=2))

        self.assertEqual(len(res["registered"]), 5, "all five epics must be registered")
        self.assertEqual(len(res["up"]), 2, "only --promote fleets may open panes")
        self.assertEqual(up.call_count, 2)
        self.assertEqual(res["failed"], [])

    def test_registering_opens_no_panes_at_all(self):
        # The whole reason registered/up is split: twenty-five registrations
        # must cost a row each, not 150 panes.
        with mock.patch.object(F, "discover_units", return_value=UNITS), \
             mock.patch.object(F, "registry", side_effect=self.fake_registry()), \
             mock.patch.object(F, "op_up") as up:
            F.op_epics(ENV, args(promote=0))
        up.assert_not_called()

    def test_a_cap_refusal_is_reported_and_opens_nothing(self):
        # THE CAP IS THE DAEMON'S. The driver asks for the slot BEFORE opening a
        # pane, so a refused fleet costs nothing — a fleet that opened six panes
        # and was then refused has already spent what the cap protects.
        refuse = {("state", "epic-03-thing"): {"error": "3 fleets are already up"}}
        reg = self.fake_registry(refuse)
        with mock.patch.object(F, "discover_units", return_value=UNITS), \
             mock.patch.object(F, "registry", side_effect=reg), \
             mock.patch.object(F, "op_up", return_value={"panes": 6}) as up:
            res = F.op_epics(ENV, args(promote=5))

        names_up = {u["fleet"] for u in res["up"]}
        self.assertNotIn("epic-03-thing", names_up)
        self.assertTrue(any(r["fleet"] == "epic-03-thing" for r in res["refused_by_cap"]),
                        f"the refusal was not reported: {res}")
        self.assertEqual(up.call_count, 4, "a refused fleet must not open panes")

    def test_a_failed_up_hands_the_slot_back(self):
        # Otherwise the cap leaks a place nobody is using, and the next promote
        # is refused for a fleet that does not exist.
        reg = self.fake_registry()
        with mock.patch.object(F, "discover_units", return_value=UNITS), \
             mock.patch.object(F, "registry", side_effect=reg), \
             mock.patch.object(F, "op_up", side_effect=SystemExit(1)):
            res = F.op_epics(ENV, args(promote=1))

        self.assertEqual(res["up"], [])
        self.assertEqual(len(res["failed"]), 1)
        released = [c for c in self.calls if c[0] == "state" and c[2] == "registered"]
        self.assertTrue(released, "the slot was never handed back")

    def test_a_failed_registration_is_not_promoted(self):
        reg = self.fake_registry({("register", "epic-01-thing"): {"error": "nope"}})
        with mock.patch.object(F, "discover_units", return_value=UNITS), \
             mock.patch.object(F, "registry", side_effect=reg), \
             mock.patch.object(F, "op_up", return_value={"panes": 6}):
            res = F.op_epics(ENV, args(promote=5))
        self.assertNotIn("epic-01-thing", res["registered"])
        self.assertTrue(any(f["fleet"] == "epic-01-thing" for f in res["failed"]))
        self.assertNotIn("epic-01-thing", {u["fleet"] for u in res["up"]})

    def test_dry_run_touches_nothing(self):
        with mock.patch.object(F, "discover_units", return_value=UNITS), \
             mock.patch.object(F, "registry") as reg, \
             mock.patch.object(F, "op_up") as up:
            res = F.op_epics(ENV, args(dry_run=True))
        reg.assert_not_called()
        up.assert_not_called()
        self.assertEqual(res["epics"], 5)


class TestPromoteTheNext(unittest.TestCase):
    LISTING = {"fleets": [
        {"name": "a", "state": "up"}, {"name": "b", "state": "up"},
        {"name": "c", "state": "up"}, {"name": "d", "state": "registered",
                                       "source": "epic04.md"}]}

    def test_it_promotes_the_first_waiting_fleet(self):
        def reg(env, *argv):
            return self.LISTING if argv[0] == "ls" else {}
        with mock.patch.object(F, "registry", side_effect=reg), \
             mock.patch.object(F, "op_up", return_value={"panes": 6}):
            res = F.op_epics_promote(ENV, args())
        self.assertEqual(res["promoted"], "d")

    def test_a_full_cap_is_reported_not_forced(self):
        def reg(env, *argv):
            if argv[0] == "ls":
                return self.LISTING
            return {"error": "3 fleets are already up (a, b, c) and the cap is 3"}
        with mock.patch.object(F, "registry", side_effect=reg), \
             mock.patch.object(F, "op_up") as up:
            res = F.op_epics_promote(ENV, args())
        self.assertIsNone(res["promoted"])
        self.assertIn("cap is 3", res["reason"])
        up.assert_not_called()

    def test_nothing_waiting_is_not_an_error(self):
        listing = {"fleets": [{"name": "a", "state": "up"}]}

        def reg(env, *argv):
            return listing if argv[0] == "ls" else {}
        with mock.patch.object(F, "registry", side_effect=reg), \
             mock.patch.object(F, "op_up") as up:
            res = F.op_epics_promote(ENV, args())
        self.assertIsNone(res["promoted"])
        self.assertIn("waiting", res["reason"])
        up.assert_not_called()

    def test_an_unreadable_registry_stops_rather_than_guessing(self):
        # "I could not read the registry" and "no fleet is waiting" are
        # different facts; promoting on the second while meaning the first would
        # start a fleet that may already be up.
        with mock.patch.object(F, "registry", return_value={"error": "no answer"}):
            with self.assertRaises(SystemExit):
                F.op_epics_promote(ENV, args())




class TestTheParserWiresFromToSource(unittest.TestCase):
    """Found LIVE on 2026-08-11, not by a test.

    `--from` has ONE destination in this tool — `source` — because that is what
    discover_units reads. The epics parser gave it its own dest, so the driver
    saw an empty path and refused a directory that was sitting right there. The
    other tests all mock discover_units, which is exactly why they could not see
    it: the mock made the argument irrelevant.
    """

    def test_discover_units_reads_the_same_field_the_parser_writes(self):
        # The contract in one line: whatever --from is parsed into, this is the
        # attribute discover_units will look at.
        ns = args()
        ns.source = "/definitely/not/here"
        with self.assertRaises(SystemExit):
            F.discover_units(ns)


class TestTheDriverPicksEpicsNotFurniture(unittest.TestCase):
    """Found LIVE on 2026-08-11 against the real 25 docs.

    The old `new_roadmap/tracks/fleet/` held MERGE-NOTES.md beside the epics, and
    the driver made a fleet for it — a cap slot, an org chart and a board spent on
    a notes file. The unit tests could not see it: they hand op_epics a fabricated
    unit list, so the discovery pattern never ran.

    2026-08-12: the epics consolidated to `new_roadmap/epics/` as E1..E9, whose
    furniture is README.md. The default widened from `epic*.md` to `[Ee]*.md` so
    both namings work; the exclusion property is what must not regress.
    """

    def test_the_default_pattern_excludes_non_epic_files(self):
        import fnmatch
        pattern = "[Ee]*.md"
        for name in ("epic01-launch-readiness.md", "epic07-a16z-memo.md",
                     "E1-ship-and-launch.md", "E9-agent-fleet.md"):
            self.assertTrue(fnmatch.fnmatch(name, pattern), name)
        for name in ("MERGE-NOTES.md", "README.md", "oss-freshness.md"):
            self.assertFalse(fnmatch.fnmatch(name, pattern), name)
        for name in ("MERGE-NOTES.md", "README.md", "notes.md"):
            self.assertFalse(fnmatch.fnmatch(name, pattern),
                             f"{name} would become a fleet of its own")


class TestOneEpicIsOneUnit(unittest.TestCase):
    """Found LIVE on 2026-08-11 — the worst find of the proof run.

    `epics-promote` built a child `up` for one epic and let it re-discover its
    units from the doc. With no `--split-on`, `re.compile("")` matches every
    line, so a single epic became **248 units** and `up` set out to open 248
    panes. The pane limit refuses late, after the session is wedged, and the
    registry cap cannot help: it is one fleet.

    Two fixes, and both are tested here because either alone leaves the trap
    reachable: the driver hands over the unit it already has, and
    units_from_file refuses an empty pattern.
    """

    def test_an_empty_split_pattern_is_refused(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as fh:
            fh.write("# Title\nline\nline\n## Section\nmore\n")
            path = fh.name
        with self.assertRaises(SystemExit):
            F.units_from_file(path, "")
        # A real pattern still works.
        units = F.units_from_file(path, "^## ")
        self.assertEqual(len(units), 1)

    def test_the_driver_hands_the_unit_to_the_child(self):
        captured = {}

        def fake_up(env, a, units=None):
            captured["units"] = units
            return {"panes": 6}

        with mock.patch.object(F, "discover_units", return_value=UNITS[:1]), \
             mock.patch.object(F, "registry", return_value={}), \
             mock.patch.object(F, "op_up", side_effect=fake_up):
            F.op_epics(ENV, args(promote=1))

        self.assertIsNotNone(captured["units"], "the child was left to re-discover its units")
        self.assertEqual(len(captured["units"]), 1, "one epic must be exactly one unit")

    def test_promote_also_hands_over_one_unit(self):
        captured = {}

        def fake_up(env, a, units=None):
            captured["units"] = units
            return {"panes": 6}

        listing = {"fleets": [{"name": "d", "state": "registered", "source": "epic04.md"}]}

        def reg(env, *argv):
            return listing if argv[0] == "ls" else {}

        with mock.patch.object(F, "registry", side_effect=reg), \
             mock.patch.object(F, "op_up", side_effect=fake_up):
            F.op_epics_promote(ENV, args())
        self.assertEqual(len(captured["units"] or []), 1)


if __name__ == "__main__":
    unittest.main(verbosity=1)
