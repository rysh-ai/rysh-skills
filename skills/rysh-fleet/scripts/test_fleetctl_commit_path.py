#!/usr/bin/env python3
"""Regression test for F-28: `commit` must commit the agent's OWN file.

Run (from the rysh-fleet skill directory):
    python3 scripts/test_fleetctl_commit_path.py

`op_commit` joins the member's roadmap path onto its worktree root. The manifest
stores that path ABSOLUTE whenever `up` was given an absolute `--roadmap-dir`,
and `os.path.join(root, <absolute>)` returns the absolute path — so the root is
dropped and a path-limited commit lands in the SHARED main checkout instead.

Found 2026-08-10 on a live four-agent fleet. The shape is worse than "wrong
tree", which is why the first assertion below is about CONTENT: the worktree
held its own copy of the same relative path, untracked and divergent, so the
agent edited one file while `commit` committed a different one. Its real work
was committed nowhere and its branch never left the seed.

No live rysh session is needed — `op_commit` takes an env dict and shells out to
git, so the whole defect reproduces in a temp repo.
"""

import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fleetctl  # noqa: E402

AGENT_COPY = "the agent's own roadmap, written in its worktree\n"
SHARED_COPY = "a DIFFERENT file at the same relative path in the shared tree\n"
ROADMAP_REL = os.path.join("fleet-roadmap", "managers", "unit-01.md")


def write(path, text):
    with open(path, "w") as fh:
        fh.write(text)


def git(cwd, *args):
    r = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} in {cwd}: {r.stderr.strip()}")
    return r.stdout.strip()


class CommitGoesToTheAgentsOwnTree(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        ws = self.ws = os.path.realpath(self.tmp.name)
        git(ws, "init", "-q", "-b", "main")
        git(ws, "config", "user.email", "t@example.com")
        git(ws, "config", "user.name", "t")
        write(os.path.join(ws, "seed"), "seed\n")
        git(ws, "add", "seed")
        git(ws, "commit", "-qm", "seed")
        self.seed = git(ws, "rev-parse", "HEAD")

        # The shared main checkout carries a file at the roadmap's relative
        # path — this is what an absolute path resolves to, and committing it
        # is the defect.
        shared = os.path.join(ws, ROADMAP_REL)
        os.makedirs(os.path.dirname(shared), exist_ok=True)
        write(shared, SHARED_COPY)

        # The agent works in its own worktree, on its own branch.
        self.wt = os.path.join(ws, "worktrees", "agent")
        git(ws, "worktree", "add", "-q", "-b", "agent", self.wt, "main")
        mine = os.path.join(self.wt, ROADMAP_REL)
        os.makedirs(os.path.dirname(mine), exist_ok=True)
        write(mine, AGENT_COPY)

        self.env = {"workspace": ws}
        self.member = {
            "label": "mgr-01",
            "worktree": self.wt,
            # Absolute, exactly as `up --roadmap-dir <abs>` records it.
            "roadmap": os.path.join(ws, ROADMAP_REL),
        }

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_commit_lands_on_the_agents_branch(self):
        res = fleetctl.op_commit(self.env, {}, self.member, "docs(fleet): mine")
        self.assertTrue(res.get("committed"), f"commit failed: {res}")
        self.assertEqual(res.get("branch"), "agent",
                         "the agent committed outside its own worktree")

    def test_the_committed_CONTENT_is_the_agents_own_file(self):
        """The assertion that separates this from a cosmetic path fix.

        Both trees hold the same relative path with different content. Landing
        on the right branch is not enough — what must be committed is the file
        the agent actually edited.
        """
        fleetctl.op_commit(self.env, {}, self.member, "docs(fleet): mine")
        committed = git(self.wt, "show", f"HEAD:{ROADMAP_REL}")
        self.assertEqual(committed, AGENT_COPY.strip(),
                         "commit took the shared tree's copy, not the agent's")

    def test_the_shared_checkout_is_left_alone(self):
        fleetctl.op_commit(self.env, {}, self.member, "docs(fleet): mine")
        self.assertEqual(git(self.ws, "rev-parse", "main"), self.seed,
                         "the shared branch moved; sibling agents share this tree")

    def test_a_roadmap_outside_the_workspace_is_refused_not_guessed(self):
        """Refuse rather than relativise into `../..` — no worktree holds it."""
        outside = dict(self.member, roadmap="/etc/hosts")
        with self.assertRaises(SystemExit):
            fleetctl.op_commit(self.env, {}, outside, "nope")

    def test_a_relative_roadmap_still_works(self):
        """The default `--roadmap-dir new_roadmap/fleet` is relative already;
        the fix must not disturb the path that was never broken."""
        rel = dict(self.member, roadmap=ROADMAP_REL)
        res = fleetctl.op_commit(self.env, {}, rel, "docs(fleet): mine")
        self.assertTrue(res.get("committed"), f"commit failed: {res}")
        self.assertEqual(res.get("branch"), "agent")


    def test_a_commit_into_the_SHARED_tree_says_so(self):
        """The fallback is legitimate; being silent about it is not.

        `root` falls back to the workspace when the manifest holds no worktree
        for the member — which is what actually happened on the fleet that found
        F-28: agents created their own worktrees, nothing recorded them, and
        `commit` wrote to the shared tree with a receipt identical to the
        correct one.
        """
        no_wt = dict(self.member, roadmap=ROADMAP_REL)
        no_wt.pop("worktree")
        res = fleetctl.op_commit(self.env, {}, no_wt, "docs(fleet): shared")
        self.assertTrue(res.get("committed"), f"commit failed: {res}")
        self.assertTrue(res.get("shared_tree"),
                        "a commit into the shared checkout must be distinguishable")

    def test_a_worktree_commit_does_NOT_claim_the_shared_tree(self):
        res = fleetctl.op_commit(self.env, {}, self.member, "docs(fleet): mine")
        self.assertTrue(res.get("committed"), f"commit failed: {res}")
        self.assertNotIn("shared_tree", res)


if __name__ == "__main__":
    unittest.main(verbosity=2)
