#!/usr/bin/env python3
"""Roles as data (design 028 §6.7, `E-42`).

The shape of a fleet used to be STRUCTURE: `man["ceo"]` was an object and each
unit had `manager` + `workers`, so a fourth role was a schema change and a
different org chart was a fork of fleetctl. These tests pin the three things
that had to become true for that to stop:

  1. a template is a list of role specs, and the epic template has four roles;
  2. the roster is FLAT and each member names its parent, so the chain is
     walkable without knowing how many tiers there are;
  3. a manifest written before this change still loads — a fleet that has been
     running for a day must not become unaddressable because the tool learned a
     new shape.

Run: python3 test_fleetctl_roles.py
"""

import os
import re
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "..", "rysh-fanout", "scripts"))

import fleetctl as F  # noqa: E402


def legacy_manifest() -> dict:
    """A manifest in the pre-E-42 shape, as one sits on disk today."""
    ceo = {"role": "ceo", "unit": "-", "pane": "p-ceo", "label": "old-ceo",
           "parent": None, "roadmap": "r/ceo.md"}
    mgr = {"role": "manager", "unit": "01", "pane": "p-mgr", "label": "mgr-01-x",
           "parent": "p-ceo", "roadmap": "r/managers/unit-01.md"}
    wkr = {"role": "worker", "unit": "01", "pane": "p-wkr", "label": "wkr-01-x",
           "parent": "p-mgr", "roadmap": "r/workers/unit-01-w1.md"}
    return {"fleet": "old", "roadmap_dir": "r", "msg_seq": 0,
            "ceo": ceo,
            "units": [{"unit": "01", "title": "Unit one", "sources": ["a.md"],
                       "manager": mgr, "workers": [wkr]}]}


def flat_manifest(template="epic") -> dict:
    members = [
        {"role": "roadmap-manager", "unit": "-", "pane": "p-rm", "label": "e-roadmap",
         "parent": None, "roadmap": "r/roadmap-managers/e-roadmap.md", "title": ""},
        {"role": "ceo", "unit": "-", "pane": "p-ceo", "label": "e-ceo",
         "parent": "p-rm", "roadmap": "r/ceos/e-ceo.md", "title": ""},
        {"role": "fleet-manager", "unit": "01", "pane": "p-fm", "label": "fmgr-01-x",
         "parent": "p-ceo", "roadmap": "r/fleet-managers/fmgr-01-x.md",
         "title": "Unit one", "sources": ["a.md"]},
        {"role": "worker", "unit": "01", "pane": "p-w1", "label": "wkr-01-x",
         "parent": "p-fm", "roadmap": "r/workers/wkr-01-x.md",
         "title": "Unit one", "sources": ["a.md"]},
    ]
    return {"fleet": "e", "roadmap_dir": "r", "msg_seq": 0, "template": template,
            "units": [{"unit": "01", "title": "Unit one", "sources": ["a.md"]}],
            "members": members}


class TestTemplatesAreData(unittest.TestCase):
    def test_the_epic_template_has_the_four_roles_the_founder_asked_for(self):
        roles = [sp["role"] for sp in F.template_specs("epic")]
        self.assertEqual(
            roles, ["roadmap-manager", "ceo", "fleet-manager", "worker"],
            "the epic fleet is roadmap manager -> CEO -> fleet manager -> workers")

    def test_the_three_tier_template_is_unchanged(self):
        roles = [sp["role"] for sp in F.template_specs("three-tier")]
        self.assertEqual(roles, ["ceo", "manager", "worker"])

    def test_specs_are_listed_top_down_and_each_names_its_parent(self):
        # ORDER IS THE CHAIN: `msg` refuses to skip a link by walking `parent`,
        # so a spec whose parent appears BELOW it would describe an org chart
        # that cannot be walked.
        for name in F.TEMPLATES:
            seen = set()
            for sp in F.template_specs(name):
                if sp["parent"] is not None:
                    self.assertIn(sp["parent"], seen,
                                  f"{name}: {sp['role']} reports to {sp['parent']}, "
                                  f"which is not defined above it")
                seen.add(sp["role"])

    def test_exactly_one_head_per_template(self):
        for name in F.TEMPLATES:
            heads = [sp for sp in F.template_specs(name) if sp["parent"] is None]
            self.assertEqual(len(heads), 1,
                             f"{name} has {len(heads)} roles reporting to nobody")

    def test_an_unknown_template_is_refused(self):
        with self.assertRaises(SystemExit):
            F.template_specs("no-such-template")

    def test_pane_count_scales_with_units_and_workers(self):
        specs = F.template_specs("epic")
        units, workers = 3, 2
        total = sum(units * F.spec_count(sp, workers) if sp["scope"] == "unit"
                    else F.spec_count(sp, workers) for sp in specs)
        # roadmap manager + ceo + 3 fleet managers + 6 workers
        self.assertEqual(total, 11)


class TestTheRosterIsFlat(unittest.TestCase):
    def test_members_are_the_roster(self):
        man = flat_manifest()
        self.assertEqual([m["label"] for m in F.roster(man)],
                         ["e-roadmap", "e-ceo", "fmgr-01-x", "wkr-01-x"])

    def test_the_chain_is_walkable_without_knowing_the_tiers(self):
        man = flat_manifest()
        worker = F.resolve(man, "wkr-01-x")
        fm = F.parent_of(man, worker)
        ceo = F.parent_of(man, fm)
        rm = F.parent_of(man, ceo)
        self.assertEqual([fm["role"], ceo["role"], rm["role"]],
                         ["fleet-manager", "ceo", "roadmap-manager"])

    def test_the_head_of_the_chain_reports_to_the_human(self):
        man = flat_manifest()
        with self.assertRaises(SystemExit):
            F.parent_of(man, F.resolve(man, "e-roadmap"))

    def test_children_of_reads_the_parent_pointers(self):
        man = flat_manifest()
        fm = F.resolve(man, "fmgr-01-x")
        self.assertEqual([c["label"] for c in F.children_of(man, fm)], ["wkr-01-x"])

    def test_top_role_follows_the_template(self):
        self.assertEqual(F.top_role(flat_manifest()), "roadmap-manager")
        self.assertEqual(F.top_role({"template": "three-tier"}), "ceo")
        # A manifest with no template recorded is a pre-E-42 one, and those were
        # all three-tier.
        self.assertEqual(F.top_role({}), "ceo")


class TestAddressing(unittest.TestCase):
    def test_a_role_name_addresses_its_single_holder(self):
        man = flat_manifest()
        self.assertEqual(F.resolve(man, "ceo")["label"], "e-ceo")
        self.assertEqual(F.resolve(man, "rm")["label"], "e-roadmap")
        self.assertEqual(F.resolve(man, "roadmap")["label"], "e-roadmap")

    def test_a_role_with_several_holders_is_REFUSED_not_guessed(self):
        # An ambiguous address delivers a work order to the wrong agent, which
        # is not a wasted command — it is an agent doing the wrong job and
        # reporting success. Refuse-do-not-guess, the same rule ANSA applies.
        man = flat_manifest()
        man["members"].append(
            {"role": "fleet-manager", "unit": "02", "pane": "p-fm2",
             "label": "fmgr-02-y", "parent": "p-ceo", "roadmap": "r/x.md"})
        with self.assertRaises(SystemExit):
            F.resolve(man, "fleet-manager")

    def test_role_plus_unit_addresses_across_role_names(self):
        man = flat_manifest()
        self.assertEqual(F.resolve(man, "fmgr-01")["label"], "fmgr-01-x")
        self.assertEqual(F.resolve(man, "wkr-01")["label"], "wkr-01-x")

    def test_the_three_tier_short_forms_still_work(self):
        man = legacy_manifest()
        self.assertEqual(F.resolve(man, "mgr-01")["label"], "mgr-01-x")
        self.assertEqual(F.resolve(man, "wkr-01")["label"], "wkr-01-x")
        self.assertEqual(F.resolve(man, "ceo")["label"], "old-ceo")


class TestLegacyManifestsStillLoad(unittest.TestCase):
    """A fleet running against yesterday's manifest must stay addressable.

    This is not politeness about file formats: manifests are on disk right now
    with live agents attached to them, and a tool that could no longer name
    those agents would strand them exactly the way a deleted manifest did.
    """

    def test_the_roster_is_derived_from_the_nested_shape(self):
        man = legacy_manifest()
        self.assertEqual([m["label"] for m in F.roster(man)],
                         ["old-ceo", "mgr-01-x", "wkr-01-x"])

    def test_the_chain_still_walks(self):
        man = legacy_manifest()
        wkr = F.resolve(man, "wkr-01-x")
        self.assertEqual(F.parent_of(man, wkr)["label"], "mgr-01-x")
        self.assertEqual(F.parent_of(man, F.resolve(man, "mgr-01"))["label"], "old-ceo")

    def test_unit_metadata_is_readable_in_both_shapes(self):
        self.assertEqual(F.unit_meta(legacy_manifest(), "01")["title"], "Unit one")
        self.assertEqual(F.unit_meta(flat_manifest(), "01")["title"], "Unit one")


class TestBriefs(unittest.TestCase):
    class Args:
        mission = "ship the epic"

    # A REAL `bin`, because that is what `up` passes. An env without one makes
    # agent_bin fall back, and a brief test that silently exercises the fallback
    # proves nothing about the path an agent is actually handed.
    BIN = os.path.abspath(sys.executable)

    def _brief(self, man, label, env=None):
        env = env or {"workspace": os.getcwd(), "bin": self.BIN}
        return F.build_brief(env, man, F.resolve(man, label), self.Args())

    def test_every_role_in_every_template_has_a_brief_that_renders(self):
        # A MISSING OR HALF-RENDERED BRIEF IS FATAL, and this is where that is
        # proved rather than discovered by an agent reading `{{MANAGER}}` as its
        # instructions.
        man = flat_manifest()
        for label in ("e-roadmap", "e-ceo", "fmgr-01-x", "wkr-01-x"):
            text = self._brief(man, label)
            self.assertNotIn("{{", text, f"{label}'s brief has unrendered variables")
            self.assertIn(label, text)

    def test_the_legacy_three_tier_briefs_still_render(self):
        man = legacy_manifest()
        for label in ("old-ceo", "mgr-01-x", "wkr-01-x"):
            text = self._brief(man, label)
            self.assertNotIn("{{", text, f"{label}'s brief has unrendered variables")

    def test_a_brief_names_the_fleets_own_board(self):
        # The payoff of per-fleet boards (E-39): an agent that does not know its
        # board posts to the session one, and twenty-five fleets share a stream.
        text = self._brief(flat_manifest(), "wkr-01-x")
        self.assertIn("board", text.lower())
        self.assertIn(f"{self.BIN} board post --as p-w1", text)

    def test_every_brief_binds_the_agent_to_the_status_contract(self):
        # Design 054: the fleet actor's ledger is only as good as the
        # declarations it gets, so every role in every template is told the
        # exact command, with its own pane and fleet spelled out.
        for man, labels in ((flat_manifest(), ("e-roadmap", "e-ceo", "fmgr-01-x", "wkr-01-x")),
                            (legacy_manifest(), ("old-ceo", "mgr-01-x", "wkr-01-x"))):
            for label in labels:
                me = F.resolve(man, label)
                text = self._brief(man, label)
                self.assertIn("## Your status: the fleet ledger", text, label)
                self.assertIn(f"{self.BIN} fleet status progress --as {me['pane']} --fleet {man['fleet']}",
                              text, label)
                self.assertIn("--blocked-by", text, label)
                self.assertIn("CHECK-IN", text, label)
                self.assertIn("rysh-fleet-agent/SKILL.md", text, label)
                self.assertTrue(os.path.isfile(re.search(r"`(/[^`]*rysh-fleet-agent/SKILL\.md)`", text).group(1)),
                                f"{label}: the skill path in the brief does not exist")

    def test_a_worker_is_told_who_its_parent_is(self):
        text = self._brief(flat_manifest(), "wkr-01-x")
        self.assertIn("fmgr-01-x", text)

    def test_only_the_fleet_head_is_briefed_on_the_global_board(self):
        # The global board is a board of FLEETS, not of agents (founder order
        # 2026-10-03), and every post there wakes every fleet reading it. A
        # manager or worker told to post there turns one fleet's chatter into
        # every fleet's interrupt, so only the head of the chain gets the section.
        section = "## The global fleet board: `fleet`"
        for man, head, others in (
                (flat_manifest(), "e-roadmap", ("e-ceo", "fmgr-01-x", "wkr-01-x")),
                (legacy_manifest(), "old-ceo", ("mgr-01-x", "wkr-01-x"))):
            man["global_board"] = "fleet"
            self.assertIn(section, self._brief(man, head),
                          f"{head} heads the fleet and must be told about the global board")
            for label in others:
                self.assertNotIn(section, self._brief(man, label),
                                 f"{label} is not the fleet head and must keep to its own board")




class TestATabOfItsOwn(unittest.TestCase):
    """`E-44` — a fleet may open its own tab, and must close only that one.

    Tab-per-fleet was deferred out of two waves on the belief that no tab
    teardown verb existed. `##tab delete <id>` did exist; it was missing from
    `##tab`'s help, so reading the live surface said otherwise. What the deferral
    was really protecting against is still real and is what these tests pin: a
    fleet must never close a tab it did not open.
    """

    def test_a_fleet_records_whether_it_opened_its_tab(self):
        # `owns_tab` is the whole permission model for the teardown. Without it
        # `down` would have to guess, and the wrong guess closes the human's own
        # tab with their panes inside it.
        man_own = {"fleet": "f", "tab": "t-1", "owns_tab": True}
        man_borrowed = {"fleet": "f", "tab": "t-1", "owns_tab": False}
        self.assertTrue(man_own.get("owns_tab"))
        self.assertFalse(man_borrowed.get("owns_tab"))

    def test_down_closes_only_a_tab_the_fleet_opened(self):
        import fleetctl as FF
        calls = []

        class FakeR:
            META_SESSION = "x"
            META_TASK = "y"

            @staticmethod
            def rysh_exec(env, cmd, **kw):
                calls.append(cmd)
                return "deleted"

            @staticmethod
            def list_panes(env):
                return []

            @staticmethod
            def find_pane_opt(panes, pid):
                return None

        man = {"fleet": "f", "tab": "t-1", "owns_tab": True, "members": [],
               "lanes": {}, "roadmap_dir": "r"}
        with mock.patch.object(FF, "R", FakeR), \
             mock.patch.object(FF, "git", return_value=type("P", (), {"stdout": ""})()):
            res = FF.op_down({"workspace": "/tmp/ws"}, man, False, True)
        self.assertIsNotNone(res.get("tab_closed"), "a fleet did not close its own tab")
        self.assertIn("##tab delete t-1", calls)

        calls.clear()
        man["owns_tab"] = False
        with mock.patch.object(FF, "R", FakeR), \
             mock.patch.object(FF, "git", return_value=type("P", (), {"stdout": ""})()):
            res = FF.op_down({"workspace": "/tmp/ws"}, man, False, True)
        self.assertIsNone(res.get("tab_closed"))
        self.assertFalse([c for c in calls if c.startswith("##tab delete")],
                         "a fleet closed a tab it did not open")

    def test_keep_ceo_leaves_the_tab_alone(self):
        # `--keep-ceo` means the fleet is not fully gone; closing its tab would
        # take the survivor's pane with it.
        import fleetctl as FF
        calls = []

        class FakeR:
            META_SESSION = "x"
            META_TASK = "y"

            @staticmethod
            def rysh_exec(env, cmd, **kw):
                calls.append(cmd)
                return "deleted"

            @staticmethod
            def list_panes(env):
                return []

            @staticmethod
            def find_pane_opt(panes, pid):
                return None

        man = {"fleet": "f", "tab": "t-1", "owns_tab": True, "members": [],
               "lanes": {}, "roadmap_dir": "r"}
        with mock.patch.object(FF, "R", FakeR), \
             mock.patch.object(FF, "git", return_value=type("P", (), {"stdout": ""})()):
            FF.op_down({"workspace": "/tmp/ws"}, man, True, True)
        self.assertFalse([c for c in calls if c.startswith("##tab delete")],
                         "--keep-ceo still closed the tab the survivor lives in")




class TestPathsAreUnambiguous(unittest.TestCase):
    """`F-33` — a report that names a path the reader cannot resolve.

    Every agent works in its own worktree, so `out/note-1.md` is one file PER
    AGENT and the workspace copy a human opens is usually empty. Observed twice,
    from two different fleets: *"both notes on disk"* was true and still sent the
    reader to an empty directory.

    The row's own ruling is that the fix belongs in the brief rather than the
    binary, so the brief half is asserted as CONTENT — and the envelope half,
    which does not depend on an agent remembering, is asserted as behaviour.
    """

    def test_every_brief_carries_the_absolute_path_rule(self):
        import glob
        briefs = glob.glob(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                        "..", "references", "brief-*.md"))
        self.assertGreaterEqual(len(briefs), 5, "briefs are missing")
        for path in briefs:
            with open(path) as fh:
                text = fh.read()
            name = os.path.basename(path)
            self.assertIn("F-33", text, f"{name} does not carry the path rule")
            self.assertIn("ABSOLUTE", text, f"{name} does not say absolute")

    def test_the_protocol_explains_why(self):
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "..", "references", "protocol.md")
        with open(p) as fh:
            text = fh.read()
        self.assertIn("F-33", text)
        # The REASON, not just the rule: a rule without its cause is one a
        # future editor deletes as noise.
        self.assertIn("one file *per agent*", text)

    def test_the_envelope_names_the_senders_worktree(self):
        man = {"fleet": "f"}
        sender = {"role": "worker", "label": "wkr-01", "unit": "01",
                  "pane": "aaaaaaaa-1111", "worktree": "/repo/worktrees/f-wkr-01"}
        recipient = {"role": "manager", "label": "mgr-01", "pane": "bbbbbbbb-2222"}
        line = F.envelope_line(man, sender, recipient, "REPORT", "msg-0001")
        self.assertIn("/repo/worktrees/f-wkr-01", line,
                      "the recipient cannot resolve a relative path without it")

    def test_no_worktree_means_no_empty_field(self):
        # A blank where a path belongs reads as "the workspace", which is the
        # confusion this is removing. The human has no worktree.
        man = {"fleet": "f"}
        sender = {"role": "human", "label": "human", "unit": "-", "pane": "cccccccc-3333"}
        recipient = {"role": "ceo", "label": "ceo", "pane": "dddddddd-4444"}
        line = F.envelope_line(man, sender, recipient, "WORK ORDER", "msg-0002")
        self.assertNotIn(" at )", line)
        self.assertNotIn(" at |", line)
        self.assertIn("FROM human human", line)


class TestBriefNamesARunnableBinary(unittest.TestCase):
    """`{{BIN}}`, 2026-08-14 — the board was empty because the brief lied.

    Every brief said the literal `rysh board post`. There is no `rysh` on this
    machine (`rysh_local` is what installs; design 025 §8d), so an agent that
    followed its brief got `bash: rysh: command not found`, exit 127, and
    posted nothing — while ABLA, ANSA, the store and the board view all worked.
    Two real claudes in an isolated session did exactly that, then posted fine
    the moment the binary name was corrected and nothing else changed.

    025 §8d had already found the three-names problem and filed it against
    ONBOARDING. Nobody carried it to the agent path. These tests are that
    crossing, so the next person to add a brief cannot reintroduce it.
    """

    def test_no_brief_hardcodes_a_binary_name(self):
        # The source-level guard, and the one that would have gone red before
        # the fix: `rysh board post` in a brief is a command an agent cannot run.
        import glob
        refs = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "..", "references")
        briefs = sorted(glob.glob(os.path.join(refs, "brief-*.md")))
        self.assertGreaterEqual(len(briefs), 9, "briefs are missing")
        for path in briefs:
            with open(path) as fh:
                text = fh.read()
            for verb in ("board post", "board tail", "board reply",
                         "ansa send", "ansa prompt"):
                self.assertNotIn(
                    f"rysh {verb}", text,
                    f"{os.path.basename(path)} hardcodes `rysh {verb}` — use "
                    f"`{{{{BIN}}}} {verb}`; there is no `rysh` on this machine")

    def test_the_rendered_brief_carries_the_real_binary(self):
        env = {"workspace": os.getcwd(), "bin": os.path.abspath(sys.executable)}
        text = F.build_brief(env, flat_manifest(), F.resolve(flat_manifest(), "wkr-01-x"),
                             TestBriefs.Args())
        self.assertIn(f"{env['bin']} board post", text)
        self.assertNotIn("rysh board post", text)

    def test_agent_bin_absolutises_a_relative_argv0(self):
        # THE SAME BUG ONE LEVEL DOWN. A daemon started as `./rysh_local` gives
        # a relative argv[0]: correct for fleetctl, which runs in the workspace,
        # and wrong in the brief, which is read from the agent's WORKTREE.
        here = os.path.dirname(os.path.abspath(__file__))
        rel = os.path.relpath(sys.executable, here)
        cwd = os.getcwd()
        try:
            os.chdir(here)
            resolved = F.agent_bin({"bin": rel})
        finally:
            os.chdir(cwd)
        self.assertTrue(os.path.isabs(resolved), f"{resolved!r} is not absolute")
        self.assertTrue(os.access(resolved, os.X_OK))

    def test_agent_bin_prefers_an_absolute_executable_verbatim(self):
        self.assertEqual(F.agent_bin({"bin": sys.executable}), sys.executable)

    def test_agent_bin_returns_the_raw_name_rather_than_guessing(self):
        # Fail loudly, not creatively. `up` has already proved this binary runs
        # (require_ansa_capable, before any pane exists), so an unresolvable
        # value here means something is wrong that a substituted name would hide.
        self.assertEqual(F.agent_bin({"bin": "no-such-rysh-binary"}),
                         "no-such-rysh-binary")

    def test_up_refuses_an_unrunnable_brief_binary_BEFORE_the_first_pane(self):
        """E9 Q2 gap B, applied to the binary the BRIEFS name.

        `require_ansa_capable` proves `env["bin"]` runs; it does not prove that
        what agent_bin writes into every brief runs, and they may differ. A
        check that is right but late is a standing fleet nobody can reach — and
        this failure is quieter than ANSA's, because a board post dying with
        exit 127 is swallowed by the agent's own shell and surfaces only as a
        board that never fills.
        """
        import types
        opened = []

        def forbidden(name):
            def f(*a, **k):
                opened.append(name)
                raise AssertionError(f"{name} ran: up opened panes before "
                                     "checking the brief binary")
            return f

        patched = {k: forbidden(k) for k in
                   ("new_tab", "new_lane", "stack_onto", "make_worktree", "start_agent")}
        real = {k: getattr(F, k) for k in patched}
        for k, v in patched.items():
            setattr(F, k, v)

        args = types.SimpleNamespace(
            fleet="binprobe", limit=None, force=False, replace=True,
            template="three-tier", workers=1, dry_run=False,
            roadmap_dir="new_roadmap/fleets/binprobe", new_tab=True,
            ceo_here=False, worktrees=True, base_branch="dev", source="test")
        units = [{"unit": "01", "title": "unit one", "sources": []}]
        env = {"workspace": "/tmp", "session": "demo", "bin": "no-such-rysh-binary"}

        try:
            with mock.patch.object(F, "require_ansa_capable", lambda e: None), \
                 mock.patch.object(F.R, "session_cost", lambda e: 0.0), \
                 mock.patch.object(F.R, "budget_blocked", lambda c: None):
                with self.assertRaises(SystemExit,
                                       msg="up accepted a brief binary nobody can run"):
                    F.op_up(env, args, units=units)
        finally:
            for k, v in real.items():
                setattr(F, k, v)

        self.assertEqual(opened, [],
                         f"up must refuse before opening anything; it reached {opened}")

    def test_the_refusal_names_the_binary_and_the_consequence(self):
        captured = {}

        def fake_die(m):
            captured["msg"] = m
            raise SystemExit(1)

        with mock.patch.object(F, "die", fake_die):
            with self.assertRaises(SystemExit):
                F.require_brief_binary({"bin": "no-such-rysh-binary"})
        msg = captured.get("msg", "")
        self.assertIn("no-such-rysh-binary", msg, "the refusal must name the binary")
        self.assertIn("127", msg, "it must name the failure an agent would actually see")
        self.assertIn("board", msg.lower(), "it must say what goes wrong downstream")

    def test_a_runnable_brief_binary_passes_the_gate(self):
        # The optimistic path has to be REACHABLE, not merely safe (025 §5.4a):
        # a gate that refuses everything is indistinguishable from one that works.
        F.require_brief_binary({"bin": os.path.abspath(sys.executable)})

    def test_a_missing_BIN_is_fatal_rather_than_literal(self):
        # The existing unrendered-variable guard has to cover the new variable
        # too: `{{BIN}} board post` reaching an agent verbatim is the same
        # class of failure as the wrong name, just louder.
        with mock.patch.object(F, "agent_bin", side_effect=KeyError("bin")):
            with self.assertRaises(KeyError):
                F.build_brief({"workspace": os.getcwd()}, flat_manifest(),
                              F.resolve(flat_manifest(), "wkr-01-x"),
                              TestBriefs.Args())


if __name__ == "__main__":
    unittest.main(verbosity=1)
