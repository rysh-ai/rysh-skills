#!/usr/bin/env python3
"""Founder ruling R12 (2026-10-07): fleet agents auto-resume after a daemon restart.

rysh-cli's startup sweep (nativeResumePlan) resumes every pane that carries
`agent.native`, from `agent.kind`, `agent.cwd`, `agent.args` and the pinned
`claude.session_id`. fleetctl already wrote the last two; these tests pin the
three it did not, the launch directory they depend on, and the one-shot backfill
for panes launched before this change. A deliberate stop disarms the stamp in
rysh-cli (`##ansa kill`); nothing here may arm a pane whose agent is not running.

Run: python3 test_fleetctl_r12_resume.py
"""

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "..", "rysh-fanout", "scripts"))

import fleetctl as F  # noqa: E402

# See test_fleetctl_r11_state: an earlier test module may have left a stub
# `ryshfan` behind fleetctl, so patch with create=True and never rely on it.
_spec = importlib.util.spec_from_file_location(
    "ryshfan_real_for_r12", os.path.join(HERE, "..", "..", "rysh-fanout", "scripts", "ryshfan.py"))
REAL_R = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(REAL_R)

ENV = {"workspace": "/ws", "session": "s", "bin": "rysh"}


def patch_r(name, **kw):
    return mock.patch.object(F.R, name, create=True, **kw)


class Recorder:
    """Captures fleetctl's meta writes in order: ('set', pane, key, value) or ('del', pane, key)."""

    def __init__(self):
        self.ops = []

    def set_meta(self, env, pane_id, key, value):
        self.ops.append(("set", pane_id, key, value))

    def clear_meta(self, env, pane_id, key):
        self.ops.append(("del", pane_id, key))

    def keys_set(self, pane="p1"):
        return {k: v for op, p, k, *v in self.ops if op == "set" and p == pane for v in [v[0]]}


class StampingArmsTheRestartSweep(unittest.TestCase):
    def stamp(self, agent, res):
        rec = Recorder()
        with mock.patch.object(F, "set_meta", side_effect=rec.set_meta), \
             mock.patch.object(F, "clear_meta", side_effect=rec.clear_meta, create=True), \
             patch_r("set_meta", side_effect=lambda env, pane, k, v: rec.set_meta(env, pane["id"], k, v)), \
             patch_r("META_SESSION", new="claude.session_id"), \
             patch_r("META_TASK", new="claude.task"):
            F.stamp_launch(ENV, "p1", res, agent, "--dangerously-skip-permissions", task="wkr")
        return rec

    def test_a_claude_member_is_stamped_with_what_the_sweep_reads(self):
        rec = self.stamp("claude", {"session_id": "p1", "cwd": "/ws/worktrees/e-wkr"})
        got = rec.keys_set()
        self.assertEqual(got.get("agent.native"), "1")
        self.assertEqual(got.get("agent.kind"), "claude")
        self.assertEqual(got.get("agent.cwd"), "/ws/worktrees/e-wkr")
        self.assertEqual(got.get("claude.session_id"), "p1")
        self.assertEqual(got.get("agent.args"), "--dangerously-skip-permissions")

    def test_the_arming_key_is_written_last(self):
        # A pane whose stamp is half-written must never be armed: the sweep
        # would resume it with no kind or no cwd.
        rec = self.stamp("claude", {"session_id": "p1", "cwd": "/ws"})
        sets = [k for op, p, k, *_ in rec.ops if op == "set"]
        self.assertEqual(sets[-1], "agent.native", sets)

    def test_codex_is_not_armed(self):
        # A fleet codex has no session id the sweep can resume (fleetctl types
        # it; only ##codex discovers one), so arming it would start a FRESH,
        # briefless codex on every restart — a restart, not a resume.
        rec = self.stamp("codex", {"session_id": "", "cwd": "/ws"})
        self.assertNotIn("agent.native", rec.keys_set())

    def test_a_claude_with_no_known_start_directory_is_not_armed(self):
        # Resuming from a guessed directory finds no session: better unarmed.
        rec = self.stamp("claude", {"session_id": "p1"})
        self.assertNotIn("agent.native", rec.keys_set())

    def test_a_rysh_agent_is_not_armed(self):
        rec = self.stamp("rysh", {"session_id": "", "auto_approve": True})
        self.assertNotIn("agent.native", rec.keys_set())


class TheLaunchDirectoryIsKnown(unittest.TestCase):
    """`agent.cwd` must be where claude STARTED: it files the session under that
    directory, and `--resume` from anywhere else finds nothing."""

    def launch(self, worktree):
        typed = []
        with patch_r("cmd_to_pane", side_effect=lambda env, pane, line: typed.append(line) or ""), \
             patch_r("fanout_dir", return_value=tempfile.gettempdir()), \
             patch_r("CLEAN_ENV", new="env -u CLAUDECODE"), \
             patch_r("SESSION_ID_ARG", new=REAL_R.SESSION_ID_ARG):
            res = F.start_agent(ENV, {"id": "p1"}, "brief", worktree, "--x", "claude", "e")
        return res, typed[0]

    def test_with_a_worktree_it_starts_there(self):
        res, line = self.launch("/ws/worktrees/e-wkr")
        self.assertEqual(res["cwd"], "/ws/worktrees/e-wkr")
        self.assertTrue(line.startswith("cd /ws/worktrees/e-wkr && "), line)

    def test_without_a_worktree_it_starts_in_the_workspace(self):
        res, line = self.launch(None)
        self.assertEqual(res["cwd"], "/ws")
        self.assertTrue(line.startswith("cd /ws && "), line)


def transcript(dirpath, sid, cwd):
    proj = os.path.join(dirpath, "projects", "-x")
    os.makedirs(proj, exist_ok=True)
    path = os.path.join(proj, f"{sid}.jsonl")
    with open(path, "w") as fh:
        fh.write(json.dumps({"type": "summary"}) + "\n")
        fh.write(json.dumps({"type": "user", "cwd": cwd, "sessionId": sid}) + "\n")
    return path


class Backfill(unittest.TestCase):
    """`fleetctl stamp-resume [--apply]` — arm the restart sweep on fleet panes
    launched before R12. Dry run by default."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.paths = {
            "run": transcript(self.tmp.name, "run", "/ws"),
            "wt": transcript(self.tmp.name, "wt", "/ws/worktrees/e-wkr"),
            "stamped": transcript(self.tmp.name, "stamped", "/ws"),
            "exited": transcript(self.tmp.name, "exited", "/ws"),
        }
        claude = {"fleet.name": "e", "fleet.agent": "claude"}
        self.panes = [
            {"id": "run", "name": "a", "program": "2.1.288", "meta": dict(claude, **{"claude.session_id": "run"})},
            {"id": "wt", "name": "b", "program": "2.1.288", "meta": dict(claude, **{"claude.session_id": "wt"})},
            {"id": "stamped", "name": "c", "program": "2.1.288",
             "meta": dict(claude, **{"claude.session_id": "stamped", "agent.native": "1"})},
            {"id": "exited", "name": "d", "program": "", "meta": dict(claude, **{"claude.session_id": "exited"})},
            {"id": "cx", "name": "e", "program": "node", "meta": {"fleet.name": "e", "fleet.agent": "codex"}},
            {"id": "lost", "name": "f", "program": "2.1.288", "meta": dict(claude, **{"claude.session_id": "lost"})},
            {"id": "human", "name": "g", "program": "2.1.288", "meta": {}},
        ]

    def run_backfill(self, apply):
        rec = Recorder()
        with patch_r("list_panes", return_value=self.panes), \
             patch_r("transcript_path", side_effect=lambda sid: self.paths.get(sid)), \
             mock.patch.object(F, "set_meta", side_effect=rec.set_meta):
            res = F.op_stamp_resume(ENV, apply=apply)
        return res, rec

    def test_dry_run_is_the_default_and_writes_nothing(self):
        res, rec = self.run_backfill(apply=False)
        self.assertTrue(res["dry_run"])
        self.assertEqual(rec.ops, [])
        plan = {r["pane"]: r for r in res["panes"]}
        self.assertEqual(plan["run"]["action"], "stamp")
        self.assertEqual(plan["run"]["cwd"], "/ws")
        self.assertEqual(plan["wt"]["cwd"], "/ws/worktrees/e-wkr",
                         "the cwd must come from the session's own transcript")

    def test_only_a_running_claude_with_a_transcript_is_armed(self):
        res, _ = self.run_backfill(apply=False)
        plan = {r["pane"]: r for r in res["panes"]}
        self.assertEqual(plan["stamped"]["action"], "already-stamped")
        self.assertEqual(plan["exited"]["action"], "skip")
        self.assertIn("not running", plan["exited"]["reason"])
        self.assertEqual(plan["cx"]["action"], "skip")
        self.assertIn("codex", plan["cx"]["reason"])
        self.assertEqual(plan["lost"]["action"], "skip")
        self.assertIn("transcript", plan["lost"]["reason"])
        self.assertNotIn("human", plan, "a pane in no fleet is not the backfill's")

    def test_apply_writes_kind_and_cwd_then_arms(self):
        _, rec = self.run_backfill(apply=True)
        run_ops = [(k, v) for op, p, k, v in rec.ops if p == "run"]
        self.assertEqual(run_ops, [("agent.kind", "claude"), ("agent.cwd", "/ws"),
                                   ("agent.native", "1")])
        touched = {p for _, p, *_ in rec.ops}
        self.assertEqual(touched, {"run", "wt"})


if __name__ == "__main__":
    unittest.main()
