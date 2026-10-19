#!/usr/bin/env python3
"""The agent-exec env scrub (formerly the static CLEAN_ENV list, which gained 5
AWS names).

Run (from the rysh-fleet skill directory):  python3 scripts/test_clean_env_aws.py

ryshfan no longer carries the list: `clean_env(env, kind)` asks the DAEMON's own
binary (`env["bin"]`) for it. claude gets `<bin> exec-clean --agent claude --`
(the full rule), codex the generated `exec-clean --print-static` prefix (exact
names only, until codex is verified), and a binary that predates exec-clean the
legacy static list. `ryshfan.CLEAN_ENV` stays an attribute (PEP 562) for
existing importers.

FAKE values only, in environments built FROM SCRATCH (the parent's environment
is never copied, so no real value can enter). No stub is named like a tool or
put on PATH: stubs are invoked by absolute path, and the agent word
in a captured spawn string is swapped for a stub by absolute path.

The end-to-end case needs a real rysh binary with exec-clean: set
RYSH_EXEC_CLEAN_BIN=/abs/path/to/that/binary (else that one case is skipped).
"""

import importlib.util
import os
import re
import shlex
import subprocess
import sys
import tempfile
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
RYSHFAN = os.path.join(HERE, "..", "..", "rysh-fanout", "scripts", "ryshfan.py")

OLD_8 = ["CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_EXECPATH",
         "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_SESSION_ID",
         "CLAUDE_SESSION_ID", "CLAUDE_PID", "CLAUDE_EFFORT"]
NEW_5 = ["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_DEFAULT_REGION",
         "S3_ENDPOINT_URL", "AWS_SESSION_TOKEN"]
SYS_PATH = "/usr/bin:/bin"
PANE_ID = "0e9c1ea0-0416-4419-8000-000000000038"


def _load_real_ryshfan():
    """The real ryshfan.py under a private name; it has no import-time side effects."""
    spec = importlib.util.spec_from_file_location("ryshfan__clean_env_real", RYSHFAN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


REAL = _load_real_ryshfan()


def _write(path, body):
    with open(path, "w") as fh:
        fh.write(body)
    os.chmod(path, 0o755)


class CleanEnvChoosesByBinary(unittest.TestCase):
    """Which prefix clean_env types, decided by what the daemon binary can do."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        REAL._STATIC_PREFIX.clear()
        # an OLD binary: no exec-clean subcommand (exits 2, as rysh does for an unknown command)
        self.old = os.path.join(self.tmp.name, "rysh-old-stub")
        _write(self.old, "#!/bin/sh\necho 'unknown command' >&2\nexit 2\n")
        # a NEW binary: answers --print-static with a generated list (names only)
        self.new = os.path.join(self.tmp.name, "rysh-new-stub")
        _write(self.new, '#!/bin/sh\n[ "$1 $2" = "exec-clean --print-static" ] && { echo "env -u FAKE_A -u FAKE_B"; exit 0; }\nexit 2\n')

    def tearDown(self):
        REAL._STATIC_PREFIX.clear()
        self.tmp.cleanup()

    def test_legacy_list_is_unchanged_for_an_old_binary(self):
        self.assertEqual(REAL.clean_env({"bin": self.old}, "claude"),
                         "env " + " ".join("-u " + n for n in OLD_8 + NEW_5))
        self.assertEqual(REAL.clean_env({"bin": self.old}, "codex"), REAL._LEGACY_CLEAN_ENV)

    def test_new_binary_claude_uses_exec_clean(self):
        self.assertEqual(REAL.clean_env({"bin": self.new}, "claude"),
                         f"{shlex.quote(self.new)} exec-clean --agent claude --")

    def test_new_binary_codex_uses_the_generated_static_prefix(self):
        # codex is not verified yet: exact names only, and they come FROM THE BINARY
        self.assertEqual(REAL.clean_env({"bin": self.new}, "codex"), "env -u FAKE_A -u FAKE_B")

    def test_no_value_in_any_prefix(self):
        for b in (self.old, self.new):
            for k in ("claude", "codex"):
                p = REAL.clean_env({"bin": b}, k)
                self.assertNotIn("=", p)
                self.assertNotIn("FAKE-", p)

    def test_clean_env_attribute_still_resolves(self):
        # the recovery scripts read R.CLEAN_ENV; with no daemon reachable it is the legacy list
        orig = REAL.discover
        REAL.discover = lambda s: (_ for _ in ()).throw(SystemExit("no daemon in a test"))
        try:
            self.assertEqual(REAL.CLEAN_ENV, REAL._LEGACY_CLEAN_ENV)
        finally:
            REAL.discover = orig


class RealSpawnStringIsScrubbed(unittest.TestCase):
    """fleetctl.start_agent's REAL spawn string, run under `sh -c` from scratch."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = self.tmp.name
        self.fanout = os.path.join(t, "fanout")
        self.launch = os.path.join(t, "launch")
        for d in (self.fanout, self.launch):
            os.mkdir(d)
        # the agent stub: an absolute path, NOT named claude. It
        # prints SET/UNSET per name via ${NAME:+x} — never a value.
        self.stub = os.path.join(t, "agent-env-probe")
        body = "#!/bin/sh\necho AGENT_STUB_RAN\n"
        for n in NEW_5 + ["FOO_TOKEN", "RYSH_PANE", "FAKE_CONTROL"]:
            body += f'if [ -n "${{{n}:+x}}" ]; then echo {n}=SET; else echo {n}=UNSET; fi\n'
        _write(self.stub, body)
        R = types.ModuleType("ryshfan")
        R.__dict__.update({k: v for k, v in vars(REAL).items() if not k.startswith("__")})
        R.clean_env = REAL.clean_env
        self.calls = []
        R.cmd_to_pane = lambda env, pane, command: (self.calls.append(command), "recorded")[1]
        R.fanout_dir = lambda env: self.fanout

        def refuse(*a, **k):
            raise AssertionError("rysh_exec reached in a test")
        R.rysh_exec = refuse
        sys.path.insert(0, HERE)
        from fleetctl_testing import load_fleetctl_with_stub
        self.fleetctl = load_fleetctl_with_stub(R, "fleetctl__test_clean_env_aws")

    def tearDown(self):
        self.tmp.cleanup()

    def _env(self):
        env = {"PATH": SYS_PATH, "FAKE_CONTROL": "1", "RYSH_PANE": "FAKE-pane", "FOO_TOKEN": "FAKE-tok"}
        env.update({n: f"FAKE-SENTINEL-{n}" for n in NEW_5})
        return env

    def _run(self, captured):
        # swap the AGENT word (not exec-clean's own `--agent claude`) for the stub
        line = captured.replace("--agent claude", "--agent \x00C")
        line = re.sub(r"(^|[\s;&|])claude(\s)", r"\1" + self.stub.replace("\\", "\\\\") + r"\2", line)
        line = line.replace("\x00C", "claude")
        r = subprocess.run(["sh", "-c", line], env=self._env(), cwd=self.tmp.name,
                           capture_output=True, text=True, timeout=60)
        self.assertNotIn("FAKE-", r.stdout + r.stderr)
        return r

    def _spawn(self, binpath):
        REAL._STATIC_PREFIX.clear()
        self.fleetctl.start_agent({"workspace": self.launch, "fanout": self.fanout, "bin": binpath},
                                  {"id": PANE_ID, "name": "t"}, "fake prompt",
                                  self.launch, "--dangerously-skip-permissions",
                                  agent="claude", fleet="t")
        self.assertEqual(len(self.calls), 1, "exactly one command to the pane")
        return self.calls[-1]

    def test_old_binary_spawn_unsets_the_legacy_names(self):
        old = os.path.join(self.tmp.name, "rysh-old-stub")
        _write(old, "#!/bin/sh\nexit 2\n")
        captured = self._spawn(old)
        self.assertTrue(captured.startswith(f"cd {shlex.quote(self.launch)} && {REAL._LEGACY_CLEAN_ENV} claude "), captured)
        out = self._run(captured).stdout.split()
        self.assertIn("AGENT_STUB_RAN", out)
        for n in NEW_5:
            self.assertIn(f"{n}=UNSET", out)
        self.assertIn("FAKE_CONTROL=SET", out)

    @unittest.skipUnless(os.environ.get("RYSH_EXEC_CLEAN_BIN"), "set RYSH_EXEC_CLEAN_BIN to a rysh binary with exec-clean")
    def test_real_exec_clean_spawn_drops_names_and_patterns(self):
        binpath = os.environ["RYSH_EXEC_CLEAN_BIN"]
        captured = self._spawn(binpath)
        self.assertIn(f"{shlex.quote(binpath)} exec-clean --agent claude -- claude ", captured)
        r = self._run(captured)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = r.stdout.split()
        self.assertIn("AGENT_STUB_RAN", out)
        for n in NEW_5 + ["FOO_TOKEN"]:
            self.assertIn(f"{n}=UNSET", out)          # exact names AND the *_TOKEN pattern
        for n in ("RYSH_PANE", "FAKE_CONTROL"):
            self.assertIn(f"{n}=SET", out)
        # (d) on the root side: the codex prefix IS the binary's generated list
        gen = subprocess.run([binpath, "exec-clean", "--print-static"], capture_output=True, text=True,
                             env={"PATH": SYS_PATH}, timeout=10).stdout.strip()
        self.assertTrue(gen.startswith("env -u "))
        self.assertEqual(REAL.clean_env({"bin": binpath}, "codex"), gen)

    def test_control_without_any_prefix_would_be_set(self):
        # Positive control: the stub with no prefix sees every fake.
        r = subprocess.run(["sh", "-c", self.stub], env=self._env(), cwd=self.tmp.name,
                           capture_output=True, text=True, timeout=30)
        for n in NEW_5 + ["FOO_TOKEN", "FAKE_CONTROL"]:
            self.assertIn(f"{n}=SET", r.stdout.split())


if __name__ == "__main__":
    unittest.main(verbosity=2)
