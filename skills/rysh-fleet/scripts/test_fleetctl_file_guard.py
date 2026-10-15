#!/usr/bin/env python3
"""Regression test: `--file` must name a real, readable file.

Run (from the rysh-fleet skill directory): python3 scripts/test_fleetctl_file_guard.py

fleetctl NEVER reads the --file body; deliver() records the path and tells the
recipient "Read <path> in full". So a path whose content was never persisted --
/dev/stdin fed by a heredoc, a process substitution, a pipe -- produces a send
that reports SUCCESS and delivers an unreadable order.

Three consecutive work orders (msg-0071, msg-0073, msg-0075) were lost exactly
that way, with no error at either end. This pins the guard that makes it loud.
"""

import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
FLEETCTL = os.path.join(HERE, "fleetctl.py")


# ORDER-7c: HERMETIC. fleetctl's main() resolves the session (ryshfan.discover)
# BEFORE it validates --file, and discover reads the daemons running on this
# machine: with two or more running and no RYSH_SESSION it refuses, so these
# tests passed or failed depending on what other agents had started (e.g. a
# throwaway `iso-probe-*` or `story-014` session). The launcher runs the SAME
# fleetctl.py as __main__, in its own process, with the real ryshfan loaded by
# path and only `discover` pinned to a temp workspace. No live daemon is read.
LAUNCH = r"""
import importlib.util, os, runpy, sys
here, ws = sys.argv[1], sys.argv[2]
spec = importlib.util.spec_from_file_location(
    "ryshfan", os.path.join(here, "..", "..", "rysh-fanout", "scripts", "ryshfan.py"))
R = importlib.util.module_from_spec(spec)
spec.loader.exec_module(R)
R.discover = lambda session: {"workspace": ws, "session": "file-guard-test",
                              "bin": "/nonexistent/rysh", "nats_host": "127.0.0.1",
                              "nats_port": 1}
sys.modules["ryshfan"] = R
sys.argv = [os.path.join(here, "fleetctl.py")] + sys.argv[3:]
runpy.run_path(sys.argv[0], run_name="__main__")
"""


def run(args, cwd=None, input=None):
    ws = tempfile.mkdtemp(prefix="fleetctl-file-guard-")
    try:
        return subprocess.run([sys.executable, "-c", LAUNCH, HERE, ws] + args,
                              capture_output=True, text=True, cwd=cwd, timeout=60,
                              input=input, env={k: v for k, v in os.environ.items()
                                                if not k.startswith("RYSH_")})
    finally:
        import shutil
        shutil.rmtree(ws, ignore_errors=True)


class FileGuard(unittest.TestCase):
    """The guard runs during argument handling, before any fleet is resolved,
    so these need no live session — which is the point: a lost order must be
    refused at the earliest possible moment, not after a manifest write."""

    def test_devstdin_is_refused_as_a_pipe(self):
        r = run(["--fleet", "nosuch", "msg", "someone", "--file", "/dev/stdin"])
        combined = r.stdout + r.stderr
        self.assertNotEqual(r.returncode, 0, "a pipe body must not be accepted")
        self.assertIn("silently lost", combined,
                      "the error must say WHY, or the next agent retries the same thing")

    def test_devstdin_is_refused_even_when_it_IS_a_regular_file(self):
        """The hole an isfile() check alone leaves, found by a live run.

        On macOS a heredoc/herestring backs /dev/stdin with a real temp file, so
        os.path.isfile() returns TRUE and the naive guard passes -- while the
        recipient still cannot read it, because that temp file dies with the
        sender. The path being process-local is the property that matters."""
        r = run(["--fleet", "nosuch", "msg", "someone", "--file", "/dev/stdin"],
                input="a heredoc body\n")
        combined = r.stdout + r.stderr
        self.assertNotEqual(r.returncode, 0,
                            "a heredoc-backed /dev/stdin must still be refused")
        self.assertIn("PROCESS-LOCAL", combined)

    def test_missing_path_is_refused(self):
        r = run(["--fleet", "nosuch", "msg", "someone",
                 "--file", "/tmp/definitely-not-here-9c1f2a.md"])
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("regular file", r.stdout + r.stderr)

    def test_a_directory_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            r = run(["--fleet", "nosuch", "msg", "someone", "--file", d])
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("regular file", r.stdout + r.stderr)

    def test_a_real_file_passes_the_guard(self):
        """A real path must get PAST this guard. It then fails on the unknown
        fleet, which is how we know the guard let it through rather than the
        test passing for the wrong reason."""
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
            f.write("# a real order\n")
            path = f.name
        try:
            r = run(["--fleet", "nosuch", "msg", "someone", "--file", path])
            combined = r.stdout + r.stderr
            self.assertNotIn("regular file", combined,
                             "a real file must not trip the --file guard")
            self.assertIn("nosuch", combined,
                          "expected to reach fleet resolution, which proves the guard passed")
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
