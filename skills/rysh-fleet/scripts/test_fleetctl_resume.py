#!/usr/bin/env python3
"""The kill/resume invariants fleetctl must uphold (founder ruling 2026-08-11).

Run (from the rysh-fleet skill directory):
    python3 scripts/test_fleetctl_resume.py

`##ansa kill` stops a fleet agent by signal — a stop that HOLDS, where ESC did
not (F-41: a pending background task re-wakes an interrupted turn, never a dead
process). `##ansa resume` brings it back. The kill is agent-agnostic (it signals
a process group), but the LAUNCH and the RESUME are CLI-specific, so for resume
to work without a lookup fleetctl must, at LAUNCH:

  1. pin the claude session id to the PANE ID (one fact, one source) — where the
     agent has an id at launch at all; codex does not,
  2. stamp `fleet.agent`, so resume knows WHICH CLI to run, and
  3. stash the launch args, so the agent comes back in the permission mode it
     was started with,

at BOTH launch sites — the main roster AND a subfleet — or a killed subfleet
worker resumes wrong (f8824f5's shape).

start_agent only shells out through cmd_to_pane; stub that and the launch logic
is testable with no daemon.
"""

import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fleetctl  # noqa: E402


class LaunchStub(unittest.TestCase):
    """cmd_to_pane is start_agent's only outside effect — capture it."""

    def setUp(self):
        self.sent = {}
        self._orig = fleetctl.R.cmd_to_pane
        fleetctl.R.cmd_to_pane = lambda env, pane, cmd: self.sent.setdefault("cmd", cmd) or ""
        self._fanout = fleetctl.R.fanout_dir
        fleetctl.R.fanout_dir = lambda env: "/tmp"

    def tearDown(self):
        fleetctl.R.cmd_to_pane = self._orig
        fleetctl.R.fanout_dir = self._fanout


class SessionIdIsThePaneId(LaunchStub):
    def test_session_id_defaults_to_the_pane_id(self):
        pane = {"id": "1234abcd-0000-1111-2222-333344445555"}
        res = fleetctl.start_agent({}, pane, "brief", None, "--dangerously-skip-permissions")
        self.assertEqual(res["session_id"], pane["id"],
                         "resume needs the id and the pane id is the one rysh always has — "
                         "so it must be the default, not a random uuid")
        self.assertIn(f"--session-id {pane['id']}", self.sent["cmd"],
                      "the pinned id must reach the actual claude launch, or resume targets nothing")

    def test_a_caller_supplied_session_id_still_wins(self):
        pane = {"id": "1234abcd-0000-1111-2222-333344445555"}
        given = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        res = fleetctl.start_agent({}, pane, "brief", None, f"--session-id {given}")
        self.assertEqual(res["session_id"], given,
                         "an explicit --session-id is a deliberate choice and must not be overridden")


class CodexLaunchesAsCodex(LaunchStub):
    """The agent is a fleet PARAMETER. Getting this wrong is not a wrong
    permission mode, it is the wrong PROGRAM in the pane."""

    def setUp(self):
        super().setUp()
        self.trusted = []
        self._trust = fleetctl.codex_trust
        fleetctl.codex_trust = lambda cwd, fleet="": (
            self.trusted.append((cwd, fleet)) or {"path": cwd, "added": True})

    def tearDown(self):
        fleetctl.codex_trust = self._trust
        super().tearDown()

    def test_codex_is_launched_with_no_session_id_pin(self):
        pane = {"id": "1234abcd-0000-1111-2222-333344445555"}
        res = fleetctl.start_agent({"workspace": "/w"}, pane, "brief", "/w/wt",
                                   "--dangerously-bypass-approvals-and-sandbox", "codex")
        self.assertTrue(self.sent["cmd"].split("&&")[-1].strip().startswith("env "),
                        f"expected a clean-env launch line: {self.sent['cmd']!r}")
        self.assertIn("codex --dangerously-bypass-approvals-and-sandbox", self.sent["cmd"],
                      f"a codex fleet must actually run codex: {self.sent['cmd']!r}")
        self.assertNotIn("--session-id", self.sent["cmd"],
                         "codex has no launch-time id pin; passing one is an error at the pane")
        self.assertEqual(res["session_id"], "",
                         "an id codex never issued must not be invented — resume uses --last")

    def test_the_cwd_is_pre_trusted_before_launch(self):
        # Without the grant codex asks "Do you trust the contents of this
        # directory?" and BLOCKS. A fleet agent has nobody to answer it, so an
        # untrusted launch hangs forever looking exactly like a slow start.
        pane = {"id": "1234abcd-0000-1111-2222-333344445555"}
        fleetctl.start_agent({"workspace": "/w"}, pane, "brief", "/w/worktrees/f-wkr-1",
                             "--dangerously-bypass-approvals-and-sandbox", "codex", "f")
        self.assertEqual(self.trusted, [("/w/worktrees/f-wkr-1", "f")],
                         "the trust grant must name the cwd the agent will RUN in "
                         "(its worktree), not the workspace")

    def test_an_unknown_agent_is_refused_not_guessed(self):
        with self.assertRaises(SystemExit):
            fleetctl.start_agent({}, {"id": "p"}, "brief", None, "", "gemini")


class DefaultArgsArePerAgent(unittest.TestCase):
    """codex's full-auto flag is not claude's. Handing codex
    `--dangerously-skip-permissions` is an immediate hard error at the pane;
    handing it nothing is the approval prompt that hangs the agent."""

    class Args:
        def __init__(self, **kw):
            self.agent = kw.get("agent", "claude")
            self.agent_args = kw.get("agent_args", "")
            self.claude_args = kw.get("claude_args", "--dangerously-skip-permissions")

    def test_claude_default(self):
        self.assertEqual(fleetctl.agent_args_for(self.Args()),
                         ("claude", "--dangerously-skip-permissions"))

    def test_codex_default_is_its_own_bypass_flag(self):
        agent, args = fleetctl.agent_args_for(self.Args(agent="codex"))
        self.assertEqual((agent, args), ("codex", "--dangerously-bypass-approvals-and-sandbox"),
                         "a codex fleet left on claude's default flag dies at every pane")

    def test_an_explicit_override_wins_in_either_spelling(self):
        self.assertEqual(fleetctl.agent_args_for(self.Args(agent="codex", agent_args="--foo"))[1],
                         "--foo")
        self.assertEqual(
            fleetctl.agent_args_for(self.Args(agent="codex", claude_args="--bar"))[1], "--bar",
            "--claude-args predates --agent and must keep working as the override it was")

    def test_an_unknown_agent_is_refused(self):
        with self.assertRaises(SystemExit):
            fleetctl.agent_args_for(self.Args(agent="gemini"))


class BothLaunchSitesStampWhatResumeNeeds(unittest.TestCase):
    """A property test on the source, because the defect is a MISSING call at
    ONE of two sites — exactly the shape a behavioural test of the other site
    passes straight through (the F-23 lesson, one skill over)."""

    def test_every_launch_site_stamps_agent_and_args(self):
        src = open(fleetctl.__file__).read()
        launches = src.count("res = start_agent(")
        stamps = src.count("stamp_launch(env,")
        self.assertGreaterEqual(launches, 2, "expected the roster and subfleet launch sites")
        self.assertEqual(
            stamps, launches,
            f"{launches} launch sites but {stamps} stamps — an unstamped site leaves a pane "
            f"`##ansa resume` cannot bring back correctly (f8824f5)")

    def test_the_stamp_records_the_agent_the_args_and_the_id(self):
        stamped = []
        env, orig = {}, fleetctl.R.set_meta
        fleetctl.R.set_meta = lambda e, p, k, v: stamped.append((k, v))
        try:
            fleetctl.stamp_launch(env, "pane-1", {"session_id": "sid-1"}, "claude",
                                  "--dangerously-skip-permissions", task="wkr-1")
        finally:
            fleetctl.R.set_meta = orig
        keys = dict(stamped)
        self.assertEqual(keys.get(fleetctl.META_AGENT), "claude")
        self.assertEqual(keys.get(fleetctl.META_AGENT_ARGS), "--dangerously-skip-permissions")
        self.assertEqual(keys.get("claude.session_id"), "sid-1")

    def test_codex_gets_an_agent_stamp_and_no_empty_session_id(self):
        stamped = []
        orig = fleetctl.R.set_meta
        fleetctl.R.set_meta = lambda e, p, k, v: stamped.append((k, v))
        try:
            fleetctl.stamp_launch({}, "pane-1", {"session_id": ""}, "codex",
                                  "--dangerously-bypass-approvals-and-sandbox")
        finally:
            fleetctl.R.set_meta = orig
        keys = dict(stamped)
        self.assertEqual(keys.get(fleetctl.META_AGENT), "codex",
                         "without fleet.agent, resume runs a claude in a codex pane")
        self.assertNotIn("claude.session_id", keys,
                         "codex issues no id at launch; an empty stamp is a fact that isn't")


class CodexTrustIsGrantedAndGivenBack(unittest.TestCase):
    """The grant is real and persistent in the USER's config, so the fleet that
    made it takes it back — and takes back nothing else."""

    def setUp(self):
        import tempfile
        self.home = tempfile.mkdtemp()
        os.environ["CODEX_HOME"] = self.home
        self.cfg = os.path.join(self.home, "config.toml")

    def tearDown(self):
        os.environ.pop("CODEX_HOME", None)

    def test_trust_is_written_once_and_revoked_by_fleet(self):
        with open(self.cfg, "w") as fh:
            fh.write('[projects."/pre/existing"]\ntrust_level = "trusted"\n')
        first = fleetctl.codex_trust("/w/worktrees/f-wkr-1", "f")
        again = fleetctl.codex_trust("/w/worktrees/f-wkr-1", "f")
        self.assertTrue(first["added"])
        self.assertFalse(again["added"], "a second launch must not re-append the same grant")
        body = open(self.cfg).read()
        self.assertIn('[projects."/w/worktrees/f-wkr-1"]', body)

        removed = fleetctl.codex_untrust("f")
        self.assertEqual(removed, ["/w/worktrees/f-wkr-1"])
        body = open(self.cfg).read()
        self.assertNotIn("/w/worktrees/f-wkr-1", body)
        self.assertIn('[projects."/pre/existing"]', body,
                      "teardown must not revoke a grant this fleet did not make")
        self.assertIn('trust_level = "trusted"', body,
                      "the surviving entry must keep its body")

    def test_another_fleets_grant_survives(self):
        fleetctl.codex_trust("/w/a", "fleet-a")
        fleetctl.codex_trust("/w/b", "fleet-b")
        fleetctl.codex_untrust("fleet-a")
        body = open(self.cfg).read()
        self.assertNotIn("/w/a", body)
        self.assertIn('[projects."/w/b"]', body)


class ANativeAgentIsNotAProcess(LaunchStub):
    """`--agent rysh` runs the pane's OWN agent — rysh's LLMPromptExecutionActor,
    inside the daemon. Nothing is spawned, so every assumption the CLI launcher
    makes is wrong for it, and each one is a separate way to break a fleet."""

    def setUp(self):
        super().setUp()
        self.published = []
        self._nats = fleetctl.R.Nats
        outer = self

        class FakeNats:
            def __init__(self, host, port):
                pass

            def publish(self, subject, payload):
                outer.published.append((subject, json.loads(payload)))

            def close(self):
                pass

        fleetctl.R.Nats = FakeNats

    def tearDown(self):
        fleetctl.R.Nats = self._nats
        super().tearDown()

    def test_the_brief_is_delivered_as_a_prompt_and_nothing_is_spawned(self):
        pane = {"id": "1234abcd-0000-1111-2222-333344445555"}
        res = fleetctl.start_agent({"session": "s", "nats_host": "h", "nats_port": 1},
                                   pane, "BRIEF BODY", None, "", "rysh")
        self.assertNotIn("cmd", self.sent,
                         "a rysh agent must not launch a process — there is no CLI to run")
        self.assertEqual([e["t"] for _, e in self.published],
                         ["MsgSetRunBudget", "MsgPaneExecPrompt"],
                         "APPROVALS ARE ARMED FIRST, and the order is the assertion. The flag "
                         "is copied onto each orchestrator when the LEG SPAWNS, so arming after "
                         "the prompt changes nothing and every agent stops at its first bash "
                         "on an approval prompt nobody is watching (verified live 2026-08-12)")
        self.assertEqual(self.published[0][0],
                         "s.pane.1234abcd-0000-1111-2222-333344445555.llm_prompt_execution.inbox")
        self.assertTrue(self.published[0][1]["p"]["auto_approve"],
                        "a gated fleet agent waits forever for a human who is not coming")
        self.assertTrue(self.published[0][1]["p"]["auto_approve_persist"],
                        "the grant must be PERSISTENT: the daemon disarms the run budget on "
                        "every terminal outcome (clean finishes included), so a per-run arm "
                        "leaves the agent approval-free for exactly one turn and its second "
                        "work order stalls gated")
        self.assertFalse(self.published[0][1]["p"]["auto_continue"],
                         "auto-continue would re-wake a stopped agent; only approvals are armed")
        subject, env = self.published[1]
        self.assertEqual(subject, "s.pane.1234abcd-0000-1111-2222-333344445555.inbox")
        self.assertEqual(env["t"], "MsgPaneExecPrompt",
                         "the brief must arrive as a PROMPT; a shell line would be typed at "
                         "a pane with no program to read it")
        self.assertEqual(env["p"]["prompt"], "BRIEF BODY",
                         "the whole brief must survive — that is why this goes over the bus "
                         "and not through a single-line ## command")
        self.assertEqual(res["session_id"], "",
                         "a rysh agent has no session id to pin; inventing one would put a "
                         "fact that is not a fact into the pane meta")

    def test_the_worktree_becomes_the_agents_cwd(self):
        # A rysh agent's tools run in the PANE's working directory, so without
        # this every agent in a --worktrees fleet edits the shared checkout.
        pane = {"id": "1234abcd-0000-1111-2222-333344445555"}
        fleetctl.start_agent({"session": "s", "nats_host": "h", "nats_port": 1},
                             pane, "BRIEF", "/w/worktrees/f-wkr-1", "", "rysh")
        self.assertEqual(self.sent.get("cmd"), "cd /w/worktrees/f-wkr-1",
                         "the pane must be moved into the agent's worktree first")


class ANativeAgentRefusesLaunchArgs(unittest.TestCase):
    """An ignored flag is how a fleet runs in a mode nobody chose."""

    class Args:
        def __init__(self, **kw):
            self.agent = kw.get("agent", "rysh")
            self.agent_args = kw.get("agent_args", "")
            self.claude_args = kw.get("claude_args", "--dangerously-skip-permissions")

    def test_rysh_takes_no_args(self):
        agent, launch = fleetctl.agent_args_for(self.Args())
        self.assertEqual((agent, launch), ("rysh", ""),
                         "a native agent has no command line to carry args on")

    def test_an_explicit_arg_for_a_native_agent_is_refused_not_ignored(self):
        with self.assertRaises(SystemExit):
            fleetctl.agent_args_for(self.Args(agent_args="--dangerously-skip-permissions"))

    def test_the_stamp_records_the_agent_and_no_empty_args(self):
        stamped = []
        orig = fleetctl.R.set_meta
        fleetctl.R.set_meta = lambda e, p, k, v: stamped.append((k, v))
        try:
            fleetctl.stamp_launch({}, "pane-1", {"session_id": "", "auto_approve": True},
                                  "rysh", "")
        finally:
            fleetctl.R.set_meta = orig
        keys = dict(stamped)
        self.assertEqual(keys.get(fleetctl.META_AUTO_APPROVE), "true",
                         "resume reads this to put the agent back in the mode it was launched "
                         "in — a cancel disarms auto-approve, so an un-restamped agent resumes "
                         "gated and stalls (f8824f5, native dress)")
        self.assertEqual(keys.get(fleetctl.META_AGENT), "rysh",
                         "without fleet.agent, the stop verb cannot tell this pane from an "
                         "idle shell and the agent keeps working through a 'kill'")
        self.assertNotIn("claude.session_id", keys)
        self.assertNotIn(fleetctl.META_AGENT_ARGS, keys)


if __name__ == "__main__":
    unittest.main(verbosity=2)
