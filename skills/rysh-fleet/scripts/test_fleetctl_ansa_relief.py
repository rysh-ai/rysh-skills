#!/usr/bin/env python3
"""ANSA relief: fleetctl stops loading the one WorkspaceActor mailbox every
ANSA send queues in.

`collect` / `wait` resolve their target with ONE own-tab listing, not
    `##tab list` + one `##pane list` per tab, and then poll by `vt_screen`.
    The idle/busy verdicts are untouched: op_wait is not changed.

Run: python3 test_fleetctl_ansa_relief.py
"""

import contextlib
import importlib.util
import io
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

# An earlier test module may have left a STUB `ryshfan` behind fleetctl (the
# 18 known failures). Never rely on F.R: load the real ryshfan by path
# and point fleetctl at it for the duration of each test.
_spec = importlib.util.spec_from_file_location(
    "ryshfan_real_for_relief", os.path.join(HERE, "..", "..", "rysh-fanout", "scripts", "ryshfan.py"))
REAL_R = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(REAL_R)
REAL_TAB_SCOPED_LIST = REAL_R.list_panes          # the original, one `##pane list`

BUSY = ["working…", "✻ Thinking… (esc to interrupt)", ""]
IDLE = ["done.", "─" * 40, "> ", "─" * 40]
NO_TUI = ["$ ", ""]


class FakeSession:
    """A session of `tabs` tabs × 3 panes that records every daemon call.

    Daemon calls are the two transports fleetctl uses: `rysh_exec` (a `##`
    command through the workspace's ws.inbox) and `vt_screen` (a pane-subject
    request). Each call is recorded in order, so a test can count how many
    happen before the first screen read.
    """

    def __init__(self, tabs=4, screens=None):
        self.calls = []
        self.tabs = [{"id": f"tab-{t}", "title": f"t{t}",
                      "panes": [f"p{t}{i}-0000-0000-0000-000000000000" for i in range(3)]}
                     for t in range(tabs)]
        self.screens = list(screens or [])

    def target(self):
        return self.tabs[-1]["panes"][1]          # a pane in the LAST tab

    def tab_of(self, pane_id):
        return next(t for t in self.tabs if pane_id in t["panes"])

    def listing(self, tab):
        lines = [f'[rysh] panes in tab "{tab["title"]}"  id={tab["id"]}  (1 lanes, 3 panes)',
                 "  lane-1 (flex=10)", "    group-1"]
        for i, pid in enumerate(tab["panes"], 1):
            lines.append(f"      [{i}] name-{pid[:3]}  id={pid}  running=2.1.288  meta:fleet.name=t")
        return "\n".join(lines) + "\n"

    def rysh_exec(self, env, command, pane_id=None, timeout=30):
        self.calls.append(("rysh_exec", command))
        if command == "##tab list":
            return "".join(f"  [{i}] {t['title']}  id={t['id']}  panes=3\n" for i, t in enumerate(self.tabs, 1))
        if command.startswith("##pane list --tab "):
            tid = command.split()[3]
            return self.listing(next(t for t in self.tabs if t["id"] == tid))
        if command.startswith("##pane list"):
            # tab-scoped: the tab of the pane it runs in (the first tab if none)
            return self.listing(self.tab_of(pane_id) if pane_id else self.tabs[0])
        raise AssertionError(f"unexpected daemon command {command!r}")

    def vt_screen(self, env, pane_id):
        self.calls.append(("vt_screen", pane_id))
        lines = self.screens.pop(0) if self.screens else IDLE
        return {"lines": list(lines), "interactive": True}

    def before_first_screen(self):
        n = 0
        for kind, _ in self.calls:
            n += 1
            if kind == "vt_screen":
                return n
        return n


def make_workspace(fs: FakeSession) -> tuple[tempfile.TemporaryDirectory, dict]:
    tmp = tempfile.TemporaryDirectory()
    ws = tmp.name
    os.makedirs(os.path.join(ws, ".rysh", "fleet"))
    members = [
        {"label": "lead", "role": "roadmap-manager", "unit": "-", "pane": fs.tabs[0]["panes"][0]},
        {"label": "wkr", "role": "worker", "unit": "1", "pane": fs.target(),
         "parent": fs.tabs[0]["panes"][0]},
    ]
    man = {"fleet": "t", "members": members, "msg_seq": 0, "log": [], "workspace": ws}
    with open(os.path.join(ws, ".rysh", "fleet", "t.json"), "w") as fh:
        json.dump(man, fh)
    env = {"workspace": ws, "session": "s", "bin": "rysh", "nats_host": "127.0.0.1", "nats_port": 1}
    return tmp, env


@contextlib.contextmanager
def wired(fs: FakeSession, env: dict):
    """fleetctl wired to the REAL ryshfan, whose transports are the fake's.

    Production patches `R.list_panes` to fleetctl's all-tabs union and keeps
    the original as `_ryshfan_list_panes`; reproduce exactly that wiring.
    """
    with mock.patch.object(F, "R", REAL_R), \
         mock.patch.object(F, "_ryshfan_list_panes", REAL_TAB_SCOPED_LIST), \
         mock.patch.object(REAL_R, "list_panes", F._list_panes), \
         mock.patch.object(REAL_R, "rysh_exec", fs.rysh_exec), \
         mock.patch.object(REAL_R, "vt_screen", fs.vt_screen), \
         mock.patch.object(REAL_R, "discover", lambda session: env), \
         mock.patch.object(REAL_R.time, "sleep", lambda s: None), \
         mock.patch.dict(os.environ, {"RYSH_PANE": ""}, clear=False):
        yield


def run_main(*argv) -> object:
    buf = io.StringIO()
    with mock.patch.object(sys, "argv", ["fleetctl.py", "--fleet", "t", *argv]), \
         contextlib.redirect_stdout(buf):
        F.main()
    return json.loads(buf.getvalue())


class F8PollWithoutWalkingEveryTab(unittest.TestCase):
    """One poll of collect/wait costs at most 2 daemon calls: one listing of the
    TARGET's own tab (it carries the pane meta op_wait reads) and one screen."""

    def check(self, cmd):
        fs = FakeSession(tabs=4, screens=[BUSY, IDLE, IDLE, IDLE])
        tmp, env = make_workspace(fs)
        self.addCleanup(tmp.cleanup)
        with wired(fs, env):
            res = run_main(cmd, "wkr", "--timeout", "60")
        r = res[0] if isinstance(res, list) else res
        self.assertEqual(r["state"], "idle", res)
        first = fs.before_first_screen()
        self.assertLessEqual(first, 2,
                             f"{cmd}: {first} daemon calls up to the first poll: {fs.calls[:first]}")
        self.assertFalse(any(c == ("rysh_exec", "##tab list") for c in fs.calls),
                         f"{cmd} walked every tab: {fs.calls}")
        listings = [c for c in fs.calls if c[0] == "rysh_exec"]
        self.assertEqual(len(listings), 1, listings)
        screens = sum(1 for c in fs.calls if c[0] == "vt_screen")
        self.assertEqual(screens, 4, "one screen read per poll, as before")

    def test_collect(self):
        self.check("collect")

    def test_wait(self):
        self.check("wait")

    def test_a_target_the_own_tab_listing_cannot_see_still_resolves(self):
        # Defence in depth: if the tab-scoped listing does not contain the
        # target (a pane moved tabs mid-call), fall back to the full walk
        # rather than failing a collect.
        fs = FakeSession(tabs=3, screens=[BUSY, IDLE, IDLE, IDLE])
        tmp, env = make_workspace(fs)
        self.addCleanup(tmp.cleanup)
        orig = fs.rysh_exec

        def blind(env_, command, pane_id=None, timeout=30):
            if command == "##pane list --meta":
                fs.calls.append(("rysh_exec", command))
                return fs.listing(fs.tabs[0])          # the wrong tab
            return orig(env_, command, pane_id, timeout)
        with wired(fs, env), mock.patch.object(REAL_R, "rysh_exec", blind):
            res = run_main("collect", "wkr", "--timeout", "60")
        self.assertEqual(res[0]["state"], "idle", res)


class F8IdleDetectionIsUnchanged(unittest.TestCase):
    """The same screens give the same verdicts: op_wait itself is not touched,
    and this table pins what it returns through both commands."""

    TABLE = [
        ("busy then settled idle", [BUSY, IDLE, IDLE, IDLE], "idle"),
        ("busy, a blip of idle, busy again, then settled", [BUSY, IDLE, BUSY, IDLE, IDLE, IDLE], "idle"),
        ("never busy, idle (untracked, inside warm-up)", [IDLE] * 6, "timeout"),
        ("no TUI at all", [NO_TUI] * 6, "timeout"),
    ]

    def test_table(self):
        for name, screens, want in self.TABLE:
            for cmd in ("collect", "wait"):
                with self.subTest(case=name, cmd=cmd):
                    fs = FakeSession(tabs=2, screens=list(screens))
                    tmp, env = make_workspace(fs)
                    self.addCleanup(tmp.cleanup)
                    # Bound the run by screens: once they are used up every
                    # poll returns the last one, and the deadline ends it.
                    last = screens[-1]
                    fs.screens = list(screens) + [last] * 4
                    clock = iter(range(0, 10_000))
                    with wired(fs, env), \
                         mock.patch.object(REAL_R.time, "time", lambda: float(next(clock))):
                        res = run_main(cmd, "wkr", "--timeout", str(len(screens) + 3))
                    r = res[0] if isinstance(res, list) else res
                    self.assertEqual(r["state"], want, (name, cmd, r))

    def test_predicates(self):
        self.assertTrue(REAL_R.is_busy(BUSY))
        self.assertFalse(REAL_R.has_composer(BUSY))
        self.assertTrue(REAL_R.has_composer(IDLE))
        self.assertFalse(REAL_R.is_busy(IDLE))
        self.assertFalse(REAL_R.has_composer(NO_TUI) or REAL_R.is_busy(NO_TUI))



# ---------------------------------------------------------------------------
# the send path
# ---------------------------------------------------------------------------

PROBE_OK = "[unknown_target] no pane matches 'fleetctl-ansa-probe-1'\n"


class FakeCLI:
    """Stands in for `rysh ansa send` as fleetctl runs it (subprocess.run).

    Records each call as 'probe' or 'send'. `send_result` decides how the send
    ends: ("ok", text) → rc 0; ("fail", text) → rc 1; ("expire", None) → the
    subprocess timeout fires.
    """

    def __init__(self, probe=("ok", PROBE_OK), send=("ok", "delivered to x (pane p)\n")):
        self.calls, self.timeouts, self.lines = [], [], []
        self.probe, self.send = probe, send

    def run(self, argv, **kw):
        kind = "probe" if any(str(a).startswith("fleetctl-ansa-probe-") for a in argv) else "send"
        self.calls.append(kind)
        self.timeouts.append((kind, kw.get("timeout")))
        if kind == "send":
            self.lines.append(argv[-1])
        how, text = self.probe if kind == "probe" else self.send
        if how == "expire":
            raise F.subprocess.TimeoutExpired(argv, kw.get("timeout"))
        return F.subprocess.CompletedProcess(argv, 0 if how == "ok" else 1, stdout=text, stderr="")


def session_workspace(pid=4242, bin_hash="b1"):
    tmp = tempfile.TemporaryDirectory()
    ws = tmp.name
    os.makedirs(os.path.join(ws, ".rysh", "sessions"))
    os.makedirs(os.path.join(ws, ".rysh", "fleet"))
    rec = {"name": "s", "pid": pid, "bin_hash": bin_hash, "version": "v0.2.10-99-ga53bf8c",
           "nats_port": 24242, "state": "running"}
    with open(os.path.join(ws, ".rysh", "sessions", "s.json"), "w") as fh:
        json.dump(rec, fh)
    return tmp, {"workspace": ws, "session": "s", "bin": "rysh"}


def fresh_process():
    """What a new fleetctl process starts with: no in-memory probe result."""
    F._ansa_capable = None
    F._ansa_probe_detail = ""


class F5OneProbePerSession(unittest.TestCase):
    """Every fleetctl send used to probe first — a second full `ansa send` in the
    one queue. The verdict is cached on disk per session and daemon identity."""

    def setUp(self):
        fresh_process()
        self.addCleanup(fresh_process)
        self.tmp, self.env = session_workspace()
        self.addCleanup(self.tmp.cleanup)
        self.cli = FakeCLI()
        p = mock.patch.object(F.subprocess, "run", self.cli.run)
        p.start()
        self.addCleanup(p.stop)

    def send_from_a_new_process(self):
        fresh_process()
        F.fleet_send(self.env, {"label": "wkr", "pane": "p-1"}, "hello")

    def test_two_processes_probe_once(self):
        self.send_from_a_new_process()
        self.send_from_a_new_process()
        self.assertEqual(self.cli.calls, ["probe", "send", "send"], self.cli.calls)

    def test_a_restarted_daemon_is_probed_again(self):
        self.send_from_a_new_process()
        with open(os.path.join(self.env["workspace"], ".rysh", "sessions", "s.json"), "w") as fh:
            json.dump({"name": "s", "pid": 9999, "bin_hash": "b1", "version": "v", "nats_port": 24242}, fh)
        self.send_from_a_new_process()
        self.assertEqual(self.cli.calls.count("probe"), 2, self.cli.calls)

    def test_an_upgraded_daemon_is_probed_again(self):
        self.send_from_a_new_process()
        with open(os.path.join(self.env["workspace"], ".rysh", "sessions", "s.json"), "w") as fh:
            json.dump({"name": "s", "pid": 4242, "bin_hash": "b2", "version": "v", "nats_port": 24242}, fh)
        self.send_from_a_new_process()
        self.assertEqual(self.cli.calls.count("probe"), 2, self.cli.calls)

    def test_an_old_verdict_expires(self):
        self.send_from_a_new_process()
        with mock.patch.object(F.time, "time", lambda: 1e12):
            self.send_from_a_new_process()
        self.assertEqual(self.cli.calls.count("probe"), 2, self.cli.calls)

    def test_a_negative_verdict_is_never_cached(self):
        self.cli.probe = ("fail", "unknown command: ansa\n")
        for _ in range(2):
            with self.assertRaises(SystemExit):
                self.send_from_a_new_process()
        self.assertEqual(self.cli.calls, ["probe", "probe"], self.cli.calls)

    def test_a_capability_error_on_send_forgets_the_verdict(self):
        self.send_from_a_new_process()
        self.cli.send = ("fail", "unknown command: ansa\n")
        with self.assertRaises(SystemExit):
            self.send_from_a_new_process()
        self.cli.send = ("ok", "delivered to x (pane p)\n")
        self.send_from_a_new_process()
        self.assertEqual(self.cli.calls, ["probe", "send", "send", "probe", "send"], self.cli.calls)

    def test_no_session_record_means_no_cache(self):
        os.remove(os.path.join(self.env["workspace"], ".rysh", "sessions", "s.json"))
        self.send_from_a_new_process()
        self.send_from_a_new_process()
        self.assertEqual(self.cli.calls.count("probe"), 2, "fail closed: no identity, no cache")

    def test_the_cache_is_runtime_state_git_ignores(self):
        self.send_from_a_new_process()
        path = F.ansa_probe_cache_path(self.env)
        self.assertTrue(os.path.isfile(path), path)
        rel = os.path.relpath(path, self.env["workspace"])
        self.assertTrue(rel.startswith(os.path.join(".rysh", "fleet") + os.sep), rel)
        self.assertFalse(rel.endswith(".json"), "a .json here would be listed as a fleet by `fleetctl fleets`")
        # Ask the .gitignore of the workspace these skills are installed in
        # (<workspace>/.claude/skills/rysh-fleet/scripts). Only meaningful inside
        # a rysh workspace: a standalone copy of the skills has none to ask.
        # (subprocess.run is faked here, os.system is not.)
        repo = os.path.abspath(os.path.join(HERE, "..", "..", "..", ".."))
        if not (os.path.isdir(os.path.join(repo, ".rysh"))
                and os.system(f"git -C {repo} rev-parse --is-inside-work-tree >/dev/null 2>&1") == 0):
            self.skipTest("not installed inside a rysh workspace git checkout")
        ok = os.system(f"git -C {repo} check-ignore -q {rel}") == 0
        self.assertTrue(ok, f"{rel} is not ignored in {repo}")


IN_WS = object()   # deliver_env(IN_WS) puts the sender's worktree UNDER the workspace


def deliver_env(sender_worktree):
    tmp = tempfile.TemporaryDirectory()
    ws = tmp.name
    os.makedirs(os.path.join(ws, ".rysh", "fleet"))
    if sender_worktree is IN_WS:
        sender_worktree = os.path.join(ws, "worktrees", "wt-x")
        os.makedirs(sender_worktree)
    lead = {"label": "lead", "role": "roadmap-manager", "unit": "-", "pane": "aaaaaaaa-1"}
    wkr = {"label": "wkr-1", "role": "worker", "unit": "1", "pane": "bbbbbbbb-2", "parent": "aaaaaaaa-1"}
    if sender_worktree:
        lead["worktree"] = sender_worktree
    man = {"fleet": "f", "members": [lead, wkr], "msg_seq": 0, "log": []}
    with open(os.path.join(ws, ".rysh", "fleet", "f.json"), "w") as fh:
        json.dump(man, fh)
    return tmp, {"workspace": ws, "session": "s", "bin": "rysh"}, man, lead, wkr


class F6LongBodiesTravelAsAFile(unittest.TestCase):
    """A typed line over ANSA_LINE_MAX (350) hits claude's paste
    handling and the confirm loop (7b: the tail-kept truncation, the 12–28 s
    holds). The body goes in a file and the typed line is the envelope plus a
    pointer, WORKSPACE-RELATIVE."""

    def setUp(self):
        self.cli = FakeCLI()
        for p in (mock.patch.object(F.subprocess, "run", self.cli.run),
                  mock.patch.object(F, "require_ansa_capable", lambda env: None),
                  mock.patch.object(F, "board_mirror", lambda *a, **k: None)):
            p.start()
            self.addCleanup(p.stop)

    def send(self, body, worktree):
        tmp, env, man, lead, wkr = deliver_env(worktree)
        self.addCleanup(tmp.cleanup)
        rec = F.deliver(env, man, lead, wkr, body, "WORK ORDER", None)
        return env, rec, self.cli.lines[-1]

    def test_a_900_char_body_types_only_the_envelope_and_a_pointer(self):
        body = ("line one of a long order\n" + "x" * 900)
        env, rec, line = self.send(body, IN_WS)
        self.assertTrue(line.startswith(rec["envelope"]), "the envelope travels intact, first")
        self.assertNotIn("x" * 50, line, "the body must not be typed")
        path = rec["body_file"]
        self.assertFalse(os.path.isabs(path), "workspace-relative")
        self.assertEqual(line, f"{rec['envelope']} Read {path} in full.")
        with open(os.path.join(env["workspace"], path)) as fh:
            self.assertEqual(fh.read(), body, "the file holds the body verbatim, newlines kept")
        self.assertTrue(path.startswith(os.path.join("worktrees", "wt-x", "out") + os.sep), path)
        self.assertEqual(os.path.basename(path), f"{rec['msg_id']}.md",
                         "the short name, msg-NNNN.md, in the sender's own out/msgs")

    def test_a_sender_with_no_worktree_writes_under_worktrees_not_tmp(self):
        env, rec, line = self.send("y" * 900, None)
        path = rec["body_file"]
        want = os.path.join("worktrees", ".f-msgs") + os.sep
        self.assertTrue(path.startswith(want), path)
        self.assertEqual(os.path.basename(path), f"{rec['msg_id']}.md")
        self.assertFalse(os.path.isabs(path), "workspace-relative")
        self.assertFalse(path.startswith("/private/tmp/claude") or "/scratchpad/" in path, path)
        self.assertTrue(os.path.isfile(os.path.join(env["workspace"], path)))

    def test_two_sends_never_share_a_file(self):
        tmp, env, man, lead, wkr = deliver_env(IN_WS)
        self.addCleanup(tmp.cleanup)
        a = F.deliver(env, man, lead, wkr, "a" * 900, "WORK ORDER", None)
        b = F.deliver(env, man, lead, wkr, "b" * 900, "WORK ORDER", None)
        self.assertNotEqual(a["body_file"], b["body_file"])
        with open(os.path.join(env["workspace"], a["body_file"])) as fh:
            self.assertEqual(fh.read(), "a" * 900)

    def test_a_short_line_still_travels_inline(self):
        tmp, env, man, lead, wkr = deliver_env(None)
        self.addCleanup(tmp.cleanup)
        rec = F.deliver(env, man, lead, wkr, "ok", "UPDATE", None)
        line = self.cli.lines[-1]
        # short fleet, short labels, no worktree: the whole line fits
        self.assertLessEqual(len(line), F.ANSA_LINE_MAX, line)
        self.assertIsNone(rec["body_file"])
        self.assertIn(" ok -- reply opening with your own envelope line naming yourself.", line)


# The longest names in the live manifests on 2026-10-09 (read-only survey of
# $HOME/root/github/rysh-ai/.rysh/fleet/*.json): fleet, label, role,
# and the longest member worktree path (100 chars).
LONG_FLEET = "epic-21-e21-chamberlai"
LONG_LABEL = "epic-21-e21-chamberlai-roadmap"
LONG_ROLE = "roadmap-manager"
# The ceiling worst case is the 100-char live path: a shorter fixture would test less.
LONG_WT_LEN = 100
LONG_WT = "$HOME/fixturepad/root/github/rysh-ai/worktrees/epic-21-e21-chamberlai-epic-21-e21-chamberlai-roadmap"


class OnePointerLineWithinTheCeiling(unittest.TestCase):
    """One pointer line, at most ANSA_LINE_MAX (350) chars
    INCLUDING the envelope, body in a file. The envelope drops the
    worktree path (the pointer itself is an absolute path into it) and keeps
    FLEET, kind, FROM role+label, TO role+label, 8-char pane ids and the msg-id."""

    def setUp(self):
        self.cli = FakeCLI()
        for p in (mock.patch.object(F.subprocess, "run", self.cli.run),
                  mock.patch.object(F, "require_ansa_capable", lambda env: None),
                  mock.patch.object(F, "board_mirror", lambda *a, **k: None)):
            p.start()
            self.addCleanup(p.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = self.tmp.name
        os.makedirs(os.path.join(self.ws, ".rysh", "fleet"))
        self.env = {"workspace": self.ws, "session": "s", "bin": "rysh"}

    def longest_names(self, sender_wt):
        lead = {"label": LONG_LABEL, "role": LONG_ROLE, "unit": "21", "pane": "e2302c92-aaaa", "worktree": sender_wt}
        peer = {"label": LONG_LABEL, "role": LONG_ROLE, "unit": "21", "pane": "03a4b27b-bbbb"}
        man = {"fleet": LONG_FLEET, "members": [lead, peer], "msg_seq": 122, "log": []}
        with open(os.path.join(self.ws, ".rysh", "fleet", f"{LONG_FLEET}.json"), "w") as fh:
            json.dump(man, fh)
        return man, lead, peer

    def test_the_constant_is_350_root_0183(self):
        self.assertEqual(F.ANSA_LINE_MAX, 350)

    def test_the_fixture_worktree_is_the_100_char_worst_case(self):
        self.assertEqual(len(LONG_WT), LONG_WT_LEN)

    def test_the_pointer_line_with_the_longest_live_names_is_within_the_ceiling(self):
        # The real worktree path is 100 chars; recreate one of the same length
        # under the temp dir so the body file can actually be written.
        wt = os.path.join(self.ws, "w" * (len(LONG_WT) - len(self.ws) - 1))
        self.assertEqual(len(wt), len(LONG_WT))
        os.makedirs(wt)
        man, lead, peer = self.longest_names(wt)
        body = "THE-BODY " * 120
        rec = F.deliver(self.env, man, lead, peer, body, "WORK ORDER", None)
        line = self.cli.lines[-1]
        self.assertLessEqual(len(line), F.ANSA_LINE_MAX, f"{len(line)}: {line}")
        self.assertNotIn("THE-BODY", line, "no body text in the typed line")
        self.assertNotIn(f" at {wt}", line, "the envelope no longer carries the worktree")
        rel_wt = os.path.relpath(os.path.realpath(wt), os.path.realpath(self.ws))
        self.assertIn(f" Read {rel_wt}{os.sep}", line, "the pointer names the sender's tree, workspace-relative")
        self.assertNotIn(wt, line, "never the absolute path")
        for keep in (f"[FLEET {LONG_FLEET} |", "| WORK ORDER |", f"FROM {LONG_ROLE} {LONG_LABEL}",
                     f"TO {LONG_ROLE} {LONG_LABEL}", "pane e2302c92", "pane 03a4b27b", rec["msg_id"]):
            self.assertIn(keep, line)

    def test_350_is_typed_inline_and_351_is_a_pointer(self):
        man, lead, peer = self.longest_names(None)
        # find the body length that makes the inline line exactly N chars
        probe = F.deliver(self.env, man, lead, peer, "x", "UPDATE", None)
        overhead = len(self.cli.lines[-1]) - 1   # the line with a 1-char body
        for n, inline in ((F.ANSA_LINE_MAX, True), (F.ANSA_LINE_MAX + 1, False)):
            with self.subTest(n=n):
                body = "y" * (n - overhead)
                rec = F.deliver(self.env, man, lead, peer, body, "UPDATE", None)
                line = self.cli.lines[-1]
                if inline:
                    self.assertEqual(len(line), n, line)
                    self.assertIsNone(rec["body_file"])
                    self.assertIn(body, line)
                else:
                    self.assertIsNotNone(rec["body_file"])
                    self.assertNotIn(body, line)
        self.assertIsNone(probe["body_file"])


class F7TimeoutsAreAboveTheCLIAndSayPending(unittest.TestCase):
    """fleetctl killed `rysh ansa send` at 20 s (probe 15 s) while the CLI's own
    budget is 30 s and the daemon still delivers queued requests (7b: msg-0056
    arrived 2 s after "nothing was delivered")."""

    def setUp(self):
        fresh_process()
        self.addCleanup(fresh_process)
        self.tmp, self.env = session_workspace()
        self.addCleanup(self.tmp.cleanup)

    def test_send_and_probe_budgets_exceed_the_cli(self):
        cli = FakeCLI()
        with mock.patch.object(F.subprocess, "run", cli.run):
            F.fleet_send(self.env, {"label": "wkr", "pane": "p-1"}, "hi")
        for kind, t in cli.timeouts:
            self.assertGreater(t, 33, f"{kind} timeout {t}s is not above the CLI's 3 s connect + 30 s request")

    def check_pending(self, send):
        cli = FakeCLI(send=send)
        buf = io.StringIO()
        with mock.patch.object(F.subprocess, "run", cli.run), contextlib.redirect_stdout(buf), \
             self.assertRaises(SystemExit):
            F.fleet_send(self.env, {"label": "wkr", "pane": "p-1"}, "hi")
        msg = json.loads(buf.getvalue())["error"]
        self.assertIn("PENDING: may still arrive; check the target's screen before resending", msg)
        self.assertNotIn("nothing was delivered", msg.lower())

    def test_our_own_timeout_says_pending(self):
        self.check_pending(("expire", None))

    def test_the_clis_timeout_says_pending(self):
        self.check_pending(("fail", "request: timeout waiting for reply on s.ws.inbox\n"))

    def test_a_probe_that_times_out_says_busy_not_incapable(self):
        cli = FakeCLI(probe=("expire", None))
        buf = io.StringIO()
        with mock.patch.object(F.subprocess, "run", cli.run), contextlib.redirect_stdout(buf), \
             self.assertRaises(SystemExit):
            F.fleet_send(self.env, {"label": "wkr", "pane": "p-1"}, "hi")
        msg = json.loads(buf.getvalue())["error"]
        self.assertIn("BUSY", msg)
        self.assertIn("NOT sent", msg)
        self.assertNotIn("predates ANSA", msg)
        self.assertEqual(cli.calls, ["probe"], "nothing is sent after a failed probe")

    def test_a_refusal_is_still_a_failure(self):
        cli = FakeCLI(send=("fail", "[unknown_target] no pane matches 'p-1'\n"))
        buf = io.StringIO()
        with mock.patch.object(F.subprocess, "run", cli.run), contextlib.redirect_stdout(buf), \
             self.assertRaises(SystemExit):
            F.fleet_send(self.env, {"label": "wkr", "pane": "p-1"}, "hi")
        msg = json.loads(buf.getvalue())["error"]
        self.assertIn("DELIVERY FAILED", msg)
        self.assertNotIn("PENDING", msg)


if __name__ == "__main__":
    unittest.main()
