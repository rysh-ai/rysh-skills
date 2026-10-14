#!/usr/bin/env python3
"""Tests for fleet comm modes (design 050): input-stream vs polling-stream.

Run (from the rysh-fleet skill directory):  python3 scripts/test_fleetctl_comm.py

The headline is the first class: under polling-stream a fleet message goes onto
the board as a command and NEVER through ANSA, whose delivery types into the
recipient's pane and erases whatever the human was writing there. The second
headline is backward compatibility: a manifest written before design 050 has no
`comm` and must keep delivering through ANSA, because live fleets run this file.

stdlib unittest with a stub ryshfan, like the other fleetctl tests.
"""

import json
import os
import subprocess
import sys
import tempfile
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))


def _install_ryshfan_stub():
    m = types.ModuleType("ryshfan")
    m.calls = []
    m.list_panes = lambda env, pane_id=None: []
    m.find_pane = lambda panes, pane_id: {"id": pane_id, "name": "stub"}
    m.rysh_exec = lambda env, command, pane_id=None, timeout=30: ""
    m.annotate_ids = lambda panes: None
    m.TAB_LINE = m.LANE_LINE = m.GROUP_LINE = m.PANE_LINE = m.META_RE = m.RUNNING_RE = None
    m.vt_screen = lambda env, pane: {"lines": []}
    m.visible_solid = lambda x: x
    m.is_busy = lambda lines: False
    m.has_composer = lambda lines: True
    m.CLEAN_ENV = "env -u CLAUDECODE"
    import re
    m.SESSION_ID_ARG = re.compile(r"--session-id[= ]([0-9a-fA-F-]{36})")

    def cmd_to_pane(env, pane, command):
        m.calls.append(("cmd_to_pane", pane["id"], command))
        return "ran"
    m.cmd_to_pane = cmd_to_pane
    m.fanout_dir = lambda env: env["fanout"]
    m.session_record = lambda workspace, session: {}
    sys.modules["ryshfan"] = m
    return m


STUB = _install_ryshfan_stub()
os.environ.setdefault("RYSHFAN_DIR", HERE)
sys.path.insert(0, HERE)
import fleetctl  # noqa: E402

SENDER = {"role": "manager", "unit": "01", "pane": "pane-sender-uuid", "label": "mgr-01"}
RECIPIENT = {"role": "worker", "unit": "01", "pane": "pane-recipient-uuid", "label": "wkr-01"}


def make_env(tmp, comm):
    env = {"workspace": tmp, "session": "test-session", "bin": "/nonexistent/rysh",
           "fanout": os.path.join(tmp, ".rysh", "fanout")}
    os.makedirs(env["fanout"], exist_ok=True)
    man = {"fleet": "alpha", "msg_seq": 0, "log": [], "units": [], "members": []}
    if comm is not None:
        man["comm"] = comm
    os.makedirs(os.path.join(tmp, ".rysh", "fleet"), exist_ok=True)
    with open(os.path.join(tmp, ".rysh", "fleet", "alpha.json"), "w") as fh:
        json.dump(man, fh)
    return env, man


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        STUB.calls.clear()
        self.runs = []
        self.ansa = []
        self.rc = 0
        self._run, self._ansa = subprocess.run, fleetctl.ansa_send
        fleetctl._ansa_capable = True

        def fake_run(argv, **kw):
            self.runs.append(list(argv))
            return subprocess.CompletedProcess(argv, self.rc, stdout="sent command", stderr="")
        subprocess.run = fake_run
        fleetctl.ansa_send = lambda env, rcpt, line: (
            self.ansa.append((rcpt["pane"], line)) or {"ok": True, "via": "ansa"})
        os.environ.pop("RYSH_FLEET_BOARD", None)

    def tearDown(self):
        subprocess.run, fleetctl.ansa_send = self._run, self._ansa
        os.environ.pop("RYSH_FLEET_BOARD", None)

    def board_sends(self):
        return [a for a in self.runs if a[1:3] == ["board", "send"]]


class PollingStreamGoesThroughTheBoard(_Base):
    def test_message_is_a_board_command_and_never_typed(self):
        env, man = make_env(self.tmp, "polling-stream")
        os.environ["RYSH_FLEET_BOARD"] = "1"   # even with the mirror on
        res = fleetctl.deliver(env, man, SENDER, RECIPIENT, "build the parser", "WORK ORDER", None)

        self.assertEqual(self.ansa, [], "polling-stream must never reach ANSA: it types into the pane")
        sends = self.board_sends()
        self.assertEqual(len(sends), 1, f"want exactly one board send, got {self.runs}")
        argv = sends[0]
        for flag, want in (("--as", SENDER["pane"]), ("--to", RECIPIENT["pane"]),
                           ("--board", "alpha"), ("--id", "msg-0001"), ("--session", "test-session")):
            self.assertEqual(argv[argv.index(flag) + 1], want, f"{flag} in {argv}")
        self.assertIn("[FLEET alpha | WORK ORDER", argv[-1], "the envelope travels as the command text")
        self.assertNotIn("post", [a[2] for a in self.runs if len(a) > 2 and a[1] == "board"],
                         "no mirror post: the command already is the board post")
        self.assertEqual(res["via"], "board")
        self.assertTrue(res["submitted"])
        with open(os.path.join(self.tmp, ".rysh", "fleet", "alpha.json")) as fh:
            self.assertEqual(json.load(fh)["log"][0]["via"], "board")

    def test_file_body_rides_as_file(self):
        env, man = make_env(self.tmp, "polling-stream")
        body = os.path.join(self.tmp, "order.md")
        with open(body, "w") as fh:
            fh.write("long order")
        fleetctl.deliver(env, man, SENDER, RECIPIENT, "", "WORK ORDER", body)
        argv = self.board_sends()[0]
        self.assertEqual(argv[argv.index("--file") + 1], body)

    def test_a_refused_send_dies_and_logs_nothing(self):
        env, man = make_env(self.tmp, "polling-stream")
        self.rc = 1
        with self.assertRaises(SystemExit):
            fleetctl.deliver(env, man, SENDER, RECIPIENT, "x", "WORK ORDER", None)
        with open(os.path.join(self.tmp, ".rysh", "fleet", "alpha.json")) as fh:
            self.assertEqual(json.load(fh)["log"], [], "a refused send must not be logged as delivered")

    def test_human_sends_from_the_calling_pane(self):
        env, man = make_env(self.tmp, "polling-stream")
        human = dict(fleetctl.HUMAN)
        os.environ["RYSH_PANE"] = "pane-of-the-human"
        try:
            fleetctl.deliver(env, man, human, RECIPIENT, "x", "WORK ORDER", None)
        finally:
            os.environ.pop("RYSH_PANE", None)
        argv = self.board_sends()[0]
        self.assertEqual(argv[argv.index("--as") + 1], "pane-of-the-human")

    def test_human_outside_a_pane_is_refused_not_typed(self):
        env, man = make_env(self.tmp, "polling-stream")
        os.environ.pop("RYSH_PANE", None)
        with self.assertRaises(SystemExit):
            fleetctl.deliver(env, man, dict(fleetctl.HUMAN), RECIPIENT, "x", "WORK ORDER", None)
        self.assertEqual(self.ansa, [])


class InputStreamIsUnchanged(_Base):
    def test_a_pre_050_manifest_keeps_ansa(self):
        env, man = make_env(self.tmp, None)        # no "comm" key at all
        self.assertEqual(fleetctl.fleet_comm(man), "input-stream")
        res = fleetctl.deliver(env, man, SENDER, RECIPIENT, "x", "WORK ORDER", None)
        self.assertEqual(len(self.ansa), 1)
        self.assertEqual(self.board_sends(), [])
        self.assertEqual(res["via"], "ansa")

    def test_explicit_input_stream_keeps_ansa(self):
        env, man = make_env(self.tmp, "input-stream")
        fleetctl.deliver(env, man, SENDER, RECIPIENT, "x", "WORK ORDER", None)
        self.assertEqual(len(self.ansa), 1)
        self.assertEqual(self.board_sends(), [])


class Launch(_Base):
    def test_polling_claude_gets_the_settings_file(self):
        env, _ = make_env(self.tmp, "polling-stream")
        with open(os.path.join(self.tmp, "rysh.config.yaml"), "w") as fh:
            fh.write("rysh: {}\n")
        res = fleetctl.start_agent(env, {"id": "pane-claude-uuid"}, "brief", None,
                                   "--dangerously-skip-permissions", "claude", "alpha",
                                   comm="polling-stream")
        launch = [c for c in STUB.calls if c[0] == "cmd_to_pane"][0][2]
        self.assertIn("--settings", launch)
        self.assertIn("--settings", res["launch_args"],
                      "the settings must be in the STAMPED args, or a resumed claude loses them")
        path = res["launch_args"].split("--settings ", 1)[1].strip("'\"")
        with open(path) as fh:
            settings = json.load(fh)
        self.assertEqual(settings["crossSessionInbound"], "accept")
        hook = settings["hooks"]["SessionStart"][0]["hooks"][0]["command"]
        for want in (f"cd {self.tmp}", "--config", "exec --session test-session",
                     "##pane meta set --pane $RYSH_PANE claude.inbox $CLAUDE_CODE_MESSAGING_SOCKET",
                     ">/dev/null 2>&1", "|| true"):
            self.assertIn(want, hook)

    def test_input_claude_is_launched_as_before(self):
        env, _ = make_env(self.tmp, "input-stream")
        res = fleetctl.start_agent(env, {"id": "pane-claude-uuid"}, "brief", None,
                                   "--dangerously-skip-permissions", "claude", "alpha",
                                   comm="input-stream")
        launch = [c for c in STUB.calls if c[0] == "cmd_to_pane"][0][2]
        self.assertNotIn("--settings", launch)
        self.assertEqual(res["launch_args"], "--dangerously-skip-permissions")

    def test_polling_codex_arms_thread_discovery(self):
        env, _ = make_env(self.tmp, "polling-stream")
        armed = []
        real = fleetctl.arm_codex_discovery
        real_trust = fleetctl.codex_trust
        fleetctl.arm_codex_discovery = lambda env, pane_id, cwd, at: armed.append((pane_id, cwd))
        fleetctl.codex_trust = lambda cwd, fleet="": {"added": False}
        try:
            wt = os.path.join(self.tmp, "wt")
            res = fleetctl.start_agent(env, {"id": "pane-codex-uuid"}, "brief", wt,
                                       "--dangerously-bypass-approvals-and-sandbox", "codex",
                                       "alpha", comm="polling-stream")
        finally:
            fleetctl.arm_codex_discovery, fleetctl.codex_trust = real, real_trust
        self.assertEqual(armed, [("pane-codex-uuid", wt)])
        self.assertEqual(res["codex_discovery"], "armed")


class CapabilityProbe(_Base):
    def test_old_daemon_is_refused_before_anything_opens(self):
        env, _ = make_env(self.tmp, "polling-stream")
        self.rc = 1
        with self.assertRaises(SystemExit):
            fleetctl.require_board_send_capable(env)
        argv = self.runs[-1]
        self.assertEqual(argv[1:3], ["board", "inbox"])

    def test_new_daemon_passes(self):
        env, _ = make_env(self.tmp, "polling-stream")
        os.environ["RYSH_PANE"] = "pane-x"
        try:
            fleetctl.require_board_send_capable(env)
        finally:
            os.environ.pop("RYSH_PANE", None)
        self.assertIn("pane-x", self.runs[-1])


class QueueListeners(_Base):
    def test_queue_fleet_delivers_through_the_board(self):
        env, man = make_env(self.tmp, "queue")
        res = fleetctl.deliver(env, man, SENDER, RECIPIENT, "x", "WORK ORDER", None)
        self.assertEqual(self.ansa, [])
        self.assertEqual(len(self.board_sends()), 1)
        self.assertEqual(res["via"], "board")

    def test_start_listener_launches_a_detached_board_listen(self):
        env, man = make_env(self.tmp, "queue")
        launched = []
        real = subprocess.Popen

        class FakePopen:
            def __init__(self, argv, **kw):
                launched.append((argv, kw))
                self.pid = 4242
        subprocess.Popen = FakePopen
        try:
            res = fleetctl.start_listener(env, man, "pane-y")
        finally:
            subprocess.Popen = real
        argv, kw = launched[0]
        self.assertEqual(argv[1:3], ["board", "listen"])
        for flag, want in (("--as", "pane-y"), ("--board", "alpha"), ("--session", "test-session"),
                           ("--state-dir", os.path.join(self.tmp, ".rysh", "listen"))):
            self.assertEqual(argv[argv.index(flag) + 1], want)
        self.assertTrue(kw.get("start_new_session"), "a listener must outlive fleetctl")
        self.assertEqual(res["pid"], 4242)

    def test_listener_alive_reads_the_pid_file(self):
        env, _ = make_env(self.tmp, "queue")
        sd = os.path.join(self.tmp, ".rysh", "listen")
        os.makedirs(sd, exist_ok=True)
        self.assertEqual(fleetctl.listener_alive(env, "pane-y"), 0)
        with open(os.path.join(sd, "pane-y.pid"), "w") as fh:
            fh.write(f"{os.getpid()}\n")
        self.assertEqual(fleetctl.listener_alive(env, "pane-y"), os.getpid())
        with open(os.path.join(sd, "pane-y.pid"), "w") as fh:
            fh.write("999999\n")
        self.assertEqual(fleetctl.listener_alive(env, "pane-y"), 0, "a dead pid is no listener")

    def test_queue_needs_a_051_daemon(self):
        env, _ = make_env(self.tmp, "queue")
        os.environ["RYSH_PANE"] = "pane-x"
        real_run = subprocess.run
        try:
            for out, ok in (("queue\n", True), ("", False)):
                subprocess.run = lambda argv, **kw: subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")
                if ok:
                    fleetctl.require_queue_capable(env)
                else:
                    with self.assertRaises(SystemExit):
                        fleetctl.require_queue_capable(env)
        finally:
            subprocess.run = real_run
            os.environ.pop("RYSH_PANE", None)


class BriefAndVerify(unittest.TestCase):
    def test_comm_section(self):
        env = {"bin": "/opt/rysh", "session": "s", "workspace": "/w"}
        self.assertEqual(fleetctl.comm_section(env, {"fleet": "alpha"}, {"pane": "p"}), "",
                         "a pre-050 fleet's briefs must not change")
        grid = fleetctl.comm_section(env, {"fleet": "alpha", "comm": "polling-stream"}, {"pane": "p"})
        queue = fleetctl.comm_section(env, {"fleet": "alpha", "comm": "queue"}, {"pane": "p"})
        self.assertIn("grid messaging", grid, "polling-stream is grid's old name")
        self.assertIn("queue messaging", queue)
        self.assertIn("its own listener", queue)
        for text in (grid, queue):
            for want in ("board ack", "do **not**", "board inbox --as p --board alpha", "human's"):
                self.assertIn(want, text)

    def test_comm_inbox(self):
        self.assertTrue(fleetctl.comm_inbox("claude", {}).startswith("missing"))
        self.assertEqual(fleetctl.comm_inbox("claude", {"claude.inbox": "/tmp/cc-socks/1.sock"}),
                         "claude-inbox /tmp/cc-socks/1.sock")
        self.assertTrue(fleetctl.comm_inbox("codex", {}).startswith("missing"))
        self.assertTrue(fleetctl.comm_inbox("codex", {"codex.session_id": "01a1284e-2775"}).startswith("codex-queue"))
        self.assertEqual(fleetctl.comm_inbox("rysh", {}), "rysh-native")

    def test_default_comm_is_queue(self):
        import argparse
        p = argparse.ArgumentParser()
        fleetctl.add_build_args(p)
        self.assertEqual(p.parse_args(["--from", "x"]).comm, "queue")
        for c in ("grid", "input-stream", "polling-stream"):
            self.assertEqual(p.parse_args(["--from", "x", "--comm", c]).comm, c)
        with self.assertRaises(SystemExit):
            p.parse_args(["--from", "x", "--comm", "carrier-pigeon"])

    def test_comm_names(self):
        self.assertEqual(fleetctl.normalize_comm("polling-stream"), "grid")
        self.assertEqual(fleetctl.fleet_comm({"comm": "polling-stream"}), "grid")
        self.assertEqual(fleetctl.fleet_comm({}), "input-stream", "a pre-050 manifest stays input-stream")
        self.assertTrue(fleetctl.is_board_comm("queue") and fleetctl.is_board_comm("grid")
                        and fleetctl.is_board_comm("polling-stream"))
        self.assertFalse(fleetctl.is_board_comm("input-stream"))


class MetaParsing(unittest.TestCase):
    """The REAL ryshfan parser (not the stub): a meta value with spaces must not
    swallow the keys after it. agent.args carries `--settings <file>` under
    polling-stream, and sorts first -- this is how P4's verify read a stamped
    agent as unreachable."""

    def test_value_with_spaces_keeps_later_keys(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "ryshfan_real", os.path.join(HERE, "..", "..", "rysh-fanout", "scripts", "ryshfan.py"))
        real = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(real)
        line = ("      [1] hello-roadmap   id=b70a0a95  running=2.1.296  meta:agent.args="
                "--dangerously-skip-permissions --settings /x/fleet settings.json,board.id=hello,"
                "claude.inbox=/tmp/cc-socks/80133.sock,fleet.comm=polling-stream")
        self.assertEqual(real.parse_meta(line), {
            "agent.args": "--dangerously-skip-permissions --settings /x/fleet settings.json",
            "board.id": "hello",
            "claude.inbox": "/tmp/cc-socks/80133.sock",
            "fleet.comm": "polling-stream",
        })
        self.assertEqual(real.parse_meta("  [1] x  id=1  running=zsh"), {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
