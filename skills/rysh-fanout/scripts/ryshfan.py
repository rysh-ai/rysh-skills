#!/usr/bin/env python3
"""ryshfan — drive sibling claude panes from inside a rysh session.

Four primitives the rysh CLI does not expose on its own:

  * pane discovery that records each pane's FULL path (tab/lane/group), so every
    later command is fully qualified and immune to focus drift;
  * reading a pane's live VT screen (``MsgGetPaneVT`` over NATS), which is the
    only way to see an inline TUI such as claude — a pane's shell buffer holds
    nothing once the program takes the alternate screen;
  * sending raw keystrokes (``MsgRawKeyInput``), used to guarantee the Enter
    that submits a typed prompt;
  * a handle on what the child actually SAID: every child is launched with a
    pinned ``--session-id``, so its transcript can be read back as text instead
    of scraped off a screen that only ever shows the last few lines.

Everything else goes through ``rysh exec -- '##...'`` exactly as a human would
type it, so the pane routing, sharing and pipeline rules still apply.

Subcommands: discover, panes, spawn, start, send, screen, status, wait, result,
close. Every subcommand prints JSON unless noted otherwise.
"""

from __future__ import annotations

import argparse
import base64
import glob
import json
import os
import re
import socket
import subprocess
import sys
import time
import uuid
from datetime import datetime

# --------------------------------------------------------------------------
# NATS — minimal text-protocol client (stdlib only, no dependency to install)
# --------------------------------------------------------------------------


class Nats:
    def __init__(self, host: str, port: int, timeout: float = 5.0):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        self.buf = b""
        self._readline()  # INFO
        self.sock.sendall(
            b'CONNECT {"verbose":false,"pedantic":false,"tls_required":false,'
            b'"name":"ryshfan","lang":"python","version":"1"}\r\n'
        )

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass

    def _fill(self) -> None:
        chunk = self.sock.recv(65536)
        if not chunk:
            raise ConnectionError("NATS connection closed")
        self.buf += chunk

    def _readline(self) -> bytes:
        while b"\r\n" not in self.buf:
            self._fill()
        line, self.buf = self.buf.split(b"\r\n", 1)
        return line

    def _read_exact(self, n: int) -> bytes:
        while len(self.buf) < n:
            self._fill()
        data, self.buf = self.buf[:n], self.buf[n:]
        return data

    def publish(self, subject: str, payload: bytes) -> None:
        self.sock.sendall(
            b"PUB %s %d\r\n%s\r\n" % (subject.encode(), len(payload), payload)
        )
        # Round-trip a PING so the publish is on the wire before we return.
        self.sock.sendall(b"PING\r\n")
        while True:
            line = self._readline()
            if line.startswith(b"PONG"):
                return
            if line.startswith(b"PING"):
                self.sock.sendall(b"PONG\r\n")
            elif line.startswith(b"-ERR"):
                raise RuntimeError(line.decode(errors="replace"))

    def request(self, subject: str, payload: bytes, timeout: float = 5.0) -> bytes:
        inbox = "_INBOX." + uuid.uuid4().hex
        self.sock.sendall(b"SUB %s 1\r\n" % inbox.encode())
        self.sock.sendall(
            b"PUB %s %s %d\r\n%s\r\n"
            % (subject.encode(), inbox.encode(), len(payload), payload)
        )
        deadline = time.time() + timeout
        while True:
            self.sock.settimeout(max(0.1, deadline - time.time()))
            line = self._readline()
            if line.startswith(b"MSG "):
                nbytes = int(line.split()[-1])
                data = self._read_exact(nbytes)
                self._read_exact(2)  # trailing CRLF
                return data
            if line.startswith(b"PING"):
                self.sock.sendall(b"PONG\r\n")
            elif line.startswith(b"-ERR"):
                raise RuntimeError(line.decode(errors="replace"))
            if time.time() > deadline:
                raise TimeoutError("no reply on " + subject)


def envelope(tag: str, payload: dict) -> bytes:
    return json.dumps({"t": tag, "p": payload}).encode()


# --------------------------------------------------------------------------
# Session discovery
# --------------------------------------------------------------------------

DAEMON_RE = re.compile(r"(?:^|/)(rysh\w*|ry)\s")


def _daemon_from_argv(pid: int, argv: list[str]) -> dict | None:
    """The daemon record for one process, or None if it is not a rysh daemon.

    Shared by both scanners so "what counts as a daemon" is decided once:
    `<binary> daemon <session-name> [...]`, binary named rysh* or ry.
    """
    if len(argv) < 3 or argv[1] != "daemon":
        return None
    exe_name = os.path.basename(argv[0])
    if not (exe_name.startswith("rysh") or exe_name == "ry"):
        return None
    return {"pid": pid, "session": argv[2], "workspace": "", "bin": argv[0]}


def _running_daemons_proc() -> list[dict]:
    """Linux: read the process table out of /proc."""
    found = []
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                argv = fh.read().split(b"\0")
        except OSError:
            continue
        argv = [a.decode(errors="replace") for a in argv if a]
        rec = _daemon_from_argv(int(pid), argv)
        if rec is None:
            continue
        try:
            rec["workspace"] = os.readlink(f"/proc/{pid}/cwd")
            rec["bin"] = os.readlink(f"/proc/{pid}/exe").replace(" (deleted)", "")
        except OSError:
            continue
        found.append(rec)
    return found


def _running_daemons_ps() -> list[dict]:
    """macOS/BSD: no /proc, so ask ps for the argv and lsof for the cwd.

    A daemon's WORKSPACE is its working directory, and that is what keys the
    session records — so a scanner that cannot report cwd is not a scanner at
    all. lsof is the only portable way to read another process's cwd here; it
    is asked once per candidate daemon, not once per process.
    """
    try:
        out = subprocess.run(
            ["ps", "-axo", "pid=,args="], capture_output=True, text=True, timeout=10
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    found = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        pid_s, _, cmd = line.partition(" ")
        if not pid_s.isdigit():
            continue
        rec = _daemon_from_argv(int(pid_s), cmd.split())
        if rec is None:
            continue
        try:
            cwd_out = subprocess.run(
                ["lsof", "-a", "-p", pid_s, "-d", "cwd", "-Fn"],
                capture_output=True, text=True, timeout=10,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        cwd = next((l[1:] for l in cwd_out.splitlines() if l.startswith("n/")), "")
        if not cwd:
            continue
        rec["workspace"] = cwd
        found.append(rec)
    return found


def running_daemons() -> list[dict]:
    """Every live rysh daemon on this machine.

    /proc where there is one, ps + lsof where there is not — the helper has to
    work on the machine the daemon is running on, and rysh runs on macOS.
    """
    scan = _running_daemons_proc if os.path.isdir("/proc") else _running_daemons_ps
    return sorted(scan(), key=lambda d: d["session"])


def session_record(workspace: str, session: str) -> dict:
    path = os.path.join(workspace, ".rysh", "sessions", f"{session}.json")
    try:
        with open(path) as fh:
            return json.load(fh)
    except OSError:
        return {}


def discover(session: str | None) -> dict:
    """Resolve which daemon to talk to, without ever picking one for the user.

    Three ways, in order: the session named on the command line; the session
    whose pane we are running in (RYSH_SESSION — a fact about this process, not
    a guess about intent); the only one running. Several daemons and no signal
    is the case that must refuse, because on a shared box the other names can
    belong to other people.
    """
    daemons = running_daemons()
    if not daemons:
        die("no running rysh daemon found")
    how = "--session"
    if session:
        daemons = [d for d in daemons if d["session"] == session]
        if not daemons:
            die(f"no running daemon for session {session!r}")
    else:
        me = os.environ.get("RYSH_SESSION", "").strip()
        # Matched against the live daemons rather than trusted outright: a stale
        # RYSH_SESSION naming a dead session must fall through to the rules
        # below, not fail as though the user had asked for it.
        mine = [d for d in daemons if d["session"] == me] if me else []
        if mine:
            daemons, how = mine, "RYSH_SESSION"
        elif len(daemons) > 1:
            names = ", ".join(d["session"] for d in daemons)
            die(
                f"{len(daemons)} rysh sessions are running ({names}) and nothing says "
                "which one we are in (RYSH_SESSION is unset or names no running daemon); "
                "pass --session to choose one — never guess, other people's sessions may "
                "be among them"
            )
        else:
            how = "only-daemon"
    d = dict(daemons[0])
    d["resolved_by"] = how
    rec = session_record(d["workspace"], d["session"])
    d["nats_port"] = rec.get("nats_port", 24242)
    d["nats_host"] = "127.0.0.1"
    # Where this process itself is running, when the pane env says so. Reported
    # because it is the answer to "why did spawn anchor there?", and its absence
    # is the answer to "why did it follow the user instead?".
    d["self_session"] = os.environ.get("RYSH_SESSION") or None
    d["self_lane"] = os.environ.get("RYSH_LANE") or None
    d["self_stack"] = os.environ.get("RYSH_STACK") or None
    d["self_pane"] = os.environ.get("RYSH_PANE") or None
    return d


# --------------------------------------------------------------------------
# rysh CLI plumbing
# --------------------------------------------------------------------------


def rysh_exec(env: dict, command: str, pane_id: str | None = None, timeout: int = 30) -> str:
    argv = [env["bin"], "exec", "--session", env["session"]]
    if pane_id:
        argv += ["--pane-id", pane_id]
    argv += ["--", command]
    proc = subprocess.run(
        argv, cwd=env["workspace"], capture_output=True, text=True, timeout=timeout
    )
    return (proc.stdout or "") + (proc.stderr or "")


PANE_LINE = re.compile(r"^(\s*>?\s*)\[(\d+)\]\s+(\S+)\s+id=(\S+)")
RUNNING_RE = re.compile(r"running=(\S+)")
META_RE = re.compile(r"meta:(\S+)")
LANE_LINE = re.compile(r"^\s*lane-(\d+)\s")
GROUP_LINE = re.compile(r"^\s*group-(\d+)\s*$")
TAB_LINE = re.compile(r'panes in tab "(.*)"\s+id=(\S+)')


def list_panes(env: dict, pane_id: str | None = None) -> list[dict]:
    """Parse ##pane list into fully-qualified pane records.

    lane-N / group-N / [N] in that listing are 1-based INDICES, which is exactly
    what --lane / --pg accept as selectors.
    """
    # --meta so a supervisor learns what every pane is in ONE call; without it
    # the same answer costs a round-trip per pane.
    out = rysh_exec(env, "##pane list --meta", pane_id=pane_id)
    tab_title = tab_id = ""
    lane = group = 0
    panes: list[dict] = []
    for line in out.splitlines():
        m = TAB_LINE.search(line)
        if m:
            tab_title, tab_id = m.group(1), m.group(2)
            continue
        m = LANE_LINE.match(line)
        if m:
            lane, group = int(m.group(1)), 0
            continue
        m = GROUP_LINE.match(line)
        if m:
            group = int(m.group(1))
            continue
        m = PANE_LINE.match(line)
        if m:
            meta = {}
            if mm := META_RE.search(line):
                for entry in mm.group(1).split(","):
                    key, _, value = entry.partition("=")
                    if key:
                        meta[key] = value
            run = RUNNING_RE.search(line)
            panes.append(
                {
                    "tab": tab_id,
                    "tab_title": tab_title,
                    "lane": lane,
                    "pg": group,
                    "index": int(m.group(2)),
                    "name": m.group(3),
                    "id": m.group(4),
                    "active": ">" in m.group(1),
                    # What the pane is running, straight from the daemon — no
                    # screen-scraping, and true for a program that prints
                    # nothing.
                    "program": run.group(1) if run else "",
                    "meta": meta,
                }
            )
    if not panes:
        die("could not parse ##pane list:\n" + out)
    # "self" is where WE are; "active" is where the USER is looking. The two
    # coincide often enough to hide the difference, and diverge exactly when it
    # matters, so both are reported.
    me = os.environ.get("RYSH_PANE", "").strip()
    for p in panes:
        p["self"] = bool(me) and p["id"] == me
    annotate_ids(panes)
    return panes


def annotate_ids(panes: list[dict]) -> None:
    """Attach the real lane and stack ids to the panes we can name them for.

    `##pane list` reports lane and stack as POSITIONS, and a position is only
    true until the layout changes: open a lane above ours, close a stack beside
    it — something a fan-out does to itself — and `--lane 1` now names a
    different lane, so a selector captured a moment ago addresses the wrong pane
    rather than failing. The pane env carries our own lane and stack as ids,
    which do not move.

    Only our own, because that is all the environment knows: a pane elsewhere in
    the tab keeps its positional selector.
    """
    me = next((p for p in panes if p["self"]), None)
    if not me:
        return
    lane_id = os.environ.get("RYSH_LANE", "").strip()
    stack_id = os.environ.get("RYSH_STACK", "").strip()
    for p in panes:
        if p["tab"] != me["tab"] or p["lane"] != me["lane"]:
            continue
        if lane_id:
            p["lane_id"] = lane_id
        if stack_id and p["pg"] == me["pg"]:
            p["pg_id"] = stack_id


def self_pane(panes: list[dict]) -> dict | None:
    """The pane this process is running in, if the daemon told it.

    A pane's shell exports its own identity (RYSH_PANE), which is the only
    signal meaning "here" rather than "wherever the user is looking". It is
    absent under a daemon predating that change, or when ryshfan is run outside
    any pane — hence a caller that must cope with None.
    """
    for p in panes:
        if p["self"]:
            return p
    return None


def find_pane_opt(panes: list[dict], ref: str) -> dict | None:
    for p in panes:
        if p["id"] == ref or p["name"] == ref:
            return p
    return None


def find_pane(panes: list[dict], ref: str) -> dict:
    pane = find_pane_opt(panes, ref)
    if pane is None:
        die(f"no pane named or with id {ref!r}")
    return pane


def active_pane(panes: list[dict]) -> dict:
    for p in panes:
        if p["active"]:
            return p
    return panes[0]


def qualified(pane: dict) -> str:
    """Selector string that pins tab, lane, group and pane.

    --pane alone resolves ONLY inside the caller's pane group, and the caller is
    whatever pane happens to be active, so an unqualified selector silently
    follows the user's focus.

    Ids wherever we have them (annotate_ids), positions otherwise. Every
    selector resolves an id before an index, so the two mix freely in one line.
    """
    lane = pane.get("lane_id") or pane["lane"]
    pg = pane.get("pg_id") or pane["pg"]
    return f"--tab {pane['tab']} --lane {lane} --pg {pg} --pane {pane['id']}"


def cmd_to_pane(env: dict, pane: dict, command: str) -> str:
    return rysh_exec(env, f"##cmd pane {qualified(pane)} {command}")


# --------------------------------------------------------------------------
# Reading a child's screen
# --------------------------------------------------------------------------

CLEAN_ENV = "env " + " ".join(
    "-u " + v
    for v in (
        "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_EXECPATH",
        "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_CODE_SESSION_ID",
        "CLAUDE_SESSION_ID", "CLAUDE_PID", "CLAUDE_EFFORT",
    )
)

ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")
BORDER = re.compile(r"^\s*─{10,}\s*$")


def vt_screen(env: dict, pane_id: str) -> dict:
    nc = Nats(env["nats_host"], env["nats_port"])
    try:
        raw = nc.request(
            f"{env['session']}.pane.{pane_id}.snapshot",
            envelope("MsgGetPaneVT", {}),
        )
    finally:
        nc.close()
    env_reply = json.loads(raw)
    vt = env_reply.get("p") or {}
    raw_lines = list(vt.get("screen") or [])
    return {
        "interactive": bool(vt.get("interactive")),
        "lines": [ANSI.sub("", ln) for ln in raw_lines],
        "raw": raw_lines,
    }


SGR = re.compile(r"\x1b\[([0-9;]*)m")


def visible_solid(raw_line: str) -> str:
    """Visible text of a line, dropping anything drawn dim (SGR 2).

    claude renders its own suggestion — the greyed-out continuation it offers
    for your next message — inside the input box. Stripped of colour it is
    indistinguishable from text we typed, and mistaking it for an unsubmitted
    prompt would make us press Enter and send claude its own suggestion.
    """
    out, dim, pos = [], False, 0
    for m in SGR.finditer(raw_line):
        if not dim:
            out.append(raw_line[pos : m.start()])
        for param in (m.group(1) or "0").split(";"):
            if param in ("", "0", "22"):
                dim = False
            elif param == "2":
                dim = True
        pos = m.end()
    if not dim:
        out.append(raw_line[pos:])
    return ANSI.sub("", "".join(out))


def composer_text(screen: dict) -> str:
    """Text sitting in claude's input box, excluding its dim suggestion.

    The box is the region between the LAST TWO border rows. Matching on the "❯"
    prompt glyph instead would also match claude's echo of every message it has
    already accepted, and report submitted prompts as still pending.
    """
    lines, raw = screen["lines"], screen["raw"]
    borders = [i for i, ln in enumerate(lines) if BORDER.match(ln)]
    if len(borders) < 2:
        return ""
    body = raw[borders[-2] + 1 : borders[-1]]
    text = "\n".join(visible_solid(ln).lstrip().lstrip("❯").strip() for ln in body)
    return text.strip()


def is_busy(lines: list[str]) -> bool:
    return "esc to interrupt" in "\n".join(lines)


def has_composer(lines: list[str]) -> bool:
    return len([i for i, ln in enumerate(lines) if BORDER.match(ln)]) >= 2


def send_keys(env: dict, pane_id: str, data: bytes) -> None:
    nc = Nats(env["nats_host"], env["nats_port"])
    try:
        nc.publish(
            f"{env['session']}.pane.{pane_id}.rawinput",
            envelope(
                "MsgRawKeyInput",
                {"pane_id": pane_id, "data": base64.b64encode(data).decode()},
            ),
        )
    finally:
        nc.close()


# --------------------------------------------------------------------------
# Operations
# --------------------------------------------------------------------------


def fanout_dir(env: dict) -> str:
    """Where prompt files live. Prompts are the only thing still on disk: they
    are content, not state, and the pane's shell has to be able to `cat` one."""
    path = os.path.join(env["workspace"], ".rysh", "fanout")
    os.makedirs(path, exist_ok=True)
    return path


# --------------------------------------------------------------------------
# Child bookkeeping — kept on the PANE, not beside it
#
# What a child is (its claude session, its task, who started it, when it was
# last prompted) used to live in sidecar files under .rysh/fanout/, keyed by the
# first 8 characters of a pane id. That made it private to this script: the pane
# list could not show it, no other tool could read it, and a workspace move
# orphaned it. rysh now stores metadata on the pane itself (`##pane meta`), so
# put it there — one place, visible to everything, persisted with the pane.
# --------------------------------------------------------------------------

META_SESSION = "claude.session_id"
META_TASK = "claude.task"
META_PARENT = "claude.parent"
META_PROMPTED = "claude.prompted_at"


def set_meta(env: dict, pane: dict, key: str, value: str) -> None:
    rysh_exec(env, f"##pane meta --pane {pane['id']} set {key} {value}")


def pane_meta(pane: dict) -> dict:
    return pane.get("meta") or {}


def mark_prompted(env: dict, pane: dict) -> None:
    """Record when a pane was last given a prompt, for wait()'s warm-up."""
    set_meta(env, pane, META_PROMPTED, str(int(time.time())))


def prompted_at(pane: dict) -> float | None:
    try:
        return float(pane_meta(pane)[META_PROMPTED])
    except (KeyError, ValueError):
        return None


def record_session(env: dict, pane: dict, session_id: str, task: str) -> None:
    set_meta(env, pane, META_SESSION, session_id)
    if task:
        set_meta(env, pane, META_TASK, task)
    # Whose child this is. Without it "close everything I started" would also
    # close the panes a sibling supervisor started, which is the sort of help
    # nobody wants.
    me = os.environ.get("RYSH_PANE", "").strip()
    if me:
        set_meta(env, pane, META_PARENT, me)


def child_session(pane: dict) -> str | None:
    return pane_meta(pane).get(META_SESSION) or None


def children(panes: list[dict], mine_only: bool = True) -> list[dict]:
    """Panes running a claude we started.

    mine_only compares claude.parent against our own pane, so two supervisors
    fanning out in the same session do not collect each other's children.
    """
    me = os.environ.get("RYSH_PANE", "").strip()
    out = []
    for p in panes:
        meta = pane_meta(p)
        if META_SESSION not in meta:
            continue
        if mine_only and me and meta.get(META_PARENT) not in (None, me):
            continue
        out.append(p)
    return out


def task_slug(prompt: str) -> str:
    """A short, pane-name-shaped label taken from the prompt's first words.

    A pane list of `humorous-falcon, cuddly-tarpon, shining-mosquito` says
    nothing about what is running; `audit-secrets, audit-controls, audit-tests`
    reads like a task board. Names are also selectors, so this doubles as a
    handle: `ryshfan wait audit-secrets`.
    """
    words = re.findall(r"[A-Za-z0-9]+", prompt.lower())
    skip = {"the", "a", "an", "in", "of", "to", "and", "for", "on", "at", "is",
            "read", "please", "then", "your", "this", "that", "with"}
    keep = [w for w in words if w not in skip][:3]
    slug = "-".join(keep)[:24].strip("-")
    return slug or "task"


def unique_name(panes: list[dict], base: str) -> str:
    """A given-name is unique per lane, so a second `audit` becomes `audit-2`."""
    taken = {p["name"] for p in panes} | {p.get("given_name") for p in panes}
    if base not in taken:
        return base
    for n in range(2, 100):
        candidate = f"{base}-{n}"
        if candidate not in taken:
            return candidate
    return base


# --------------------------------------------------------------------------
# Spend
# --------------------------------------------------------------------------

COST_LINE = re.compile(r"session\s+\w+\s+([\d.]+)([kKmM]?)\s*tok\s+\$([\d.]+)")
BUDGET_LINE = re.compile(r"budget pane\s+(\S+)\s+(\S+)\s+/\s+(\S+)\s+\((\d+)%\)")


def parse_tokens(value: str, suffix: str) -> float:
    scale = {"k": 1e3, "m": 1e6}.get(suffix.lower(), 1.0)
    return float(value) * scale


def session_cost(env: dict) -> dict:
    """Spend so far, and any pane ceiling that is close to being hit.

    A fan-out of N children is N full claude sessions; the bill is the one part
    of it nobody sees until later. Cheap to read, so read it.
    """
    out = rysh_exec(env, "##cost")
    cost = {"tokens": None, "dollars": None, "ceilings": []}
    if m := COST_LINE.search(out):
        cost["tokens"] = parse_tokens(m.group(1), m.group(2))
        cost["dollars"] = float(m.group(3))
    for m in BUDGET_LINE.finditer(out):
        cost["ceilings"].append({"pane": m.group(1), "spent": m.group(2),
                                 "ceiling": m.group(3), "pct": int(m.group(4))})
    return cost


def budget_blocked(cost: dict) -> str | None:
    """The reason to refuse a fan-out, or None.

    Only a ceiling the session itself declares can block: guessing a limit from
    spend alone would refuse work nobody asked us to refuse.
    """
    for c in cost["ceilings"]:
        if c["pct"] >= 100:
            return (f"pane {c['pane']} is at {c['pct']}% of its token ceiling "
                    f"({c['spent']} / {c['ceiling']}) — raise it with ##cost budget, "
                    "or pass --force")
    return None


def projects_root() -> str:
    """Where claude keeps its per-project transcript directories.

    CLAUDE_CONFIG_DIR relocates the whole ~/.claude tree. A child inherits the
    daemon's environment, and so did we, so our own value is the child's value.
    """
    base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    return os.path.join(base, "projects")


def transcript_path(session_id: str) -> str | None:
    """The child's JSONL, wherever claude filed it.

    Transcripts are filed under a directory named after the session's working
    directory, and a child runs in ITS pane's directory — not ours. Globbing for
    the id finds it without having to reconstruct a path we would only get right
    when the two happen to match.
    """
    hits = sorted(glob.glob(os.path.join(projects_root(), "*", f"{session_id}.jsonl")))
    return hits[0] if hits else None


def tail_records(path: str, nbytes: int = 1 << 20) -> list[dict]:
    """Parse the last `nbytes` of a transcript into records.

    Not the whole file: transcripts reach tens of megabytes and `wait` re-reads
    every couple of seconds. Parsing is deliberately forgiving — the first line
    of the window is normally cut in half, and the child may be mid-write on the
    last one.
    """
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - nbytes))
            chunk = fh.read()
    except OSError:
        return []
    lines = chunk.split(b"\n")
    if size > nbytes:
        lines = lines[1:]
    out = []
    for line in lines:
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def iso_epoch(ts: str | None) -> float:
    if not ts:
        return 0.0
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def final_answer(records: list[dict]) -> dict:
    """The newest thing the child said, as text.

    Sidechain records are a subagent's own conversation — what something the
    child spawned said to itself, not the child's answer — so they are skipped.
    A turn whose blocks are all tool calls has no text to report, and the search
    continues into earlier turns; `wait` only asks once the pane is idle, so
    what comes back is the last thing said before it stopped.
    """
    for rec in reversed(records):
        if rec.get("type") != "assistant" or rec.get("isSidechain"):
            continue
        content = (rec.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        text = "\n".join(
            b.get("text", "")
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        ).strip()
        if text:
            return {"text": text, "timestamp": rec.get("timestamp"),
                    "at": iso_epoch(rec.get("timestamp"))}
    return {}


def child_answer(env: dict, pane: dict) -> dict:
    """What the child has said so far, plus the plumbing state behind it.

    `tracked` distinguishes "this child has no session id, so nothing can be
    read" from "it has one and has not spoken yet" — the two look identical
    from the outside and mean opposite things to a caller that is waiting.
    """
    sid = child_session(pane)
    if not sid:
        return {"tracked": False, "session_id": None, "transcript": None, "answer": None}
    path = transcript_path(sid)
    if not path:
        return {"tracked": True, "session_id": sid, "transcript": None, "answer": None}
    answer = final_answer(tail_records(path))
    return {"tracked": True, "session_id": sid, "transcript": path,
            "answer": answer or None}


UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F-]{27}")


def op_result(env: dict, panes: list[dict], ref: str) -> dict:
    """An answer lookup that outlives the pane.

    A transcript is a file; a pane is a window onto a process. Closing the window
    to tidy the screen should not put the answer out of reach, so `ref` may be a
    pane name, a pane id, or the session id itself. A closed pane's id is still a
    valid key — the map lives on disk — and only if that misses do we read `ref`
    as a session id, which keeps a live pane's own id from being misread as one.
    """
    pane = find_pane_opt(panes, ref)
    found = child_answer(env, pane) if pane else {"tracked": False, "session_id": None,
                                                  "transcript": None, "answer": None}
    if not found["tracked"] and UUID_RE.fullmatch(ref):
        path = transcript_path(ref)
        found = {"tracked": True, "session_id": ref, "transcript": path,
                 "answer": (final_answer(tail_records(path)) or None) if path else None}
    return {"pane": pane["name"] if pane else ref, **found}


def clip(answer: dict | None, max_chars: int) -> dict | None:
    """Bound what a single answer can inject into the caller's context."""
    if not answer or max_chars <= 0 or len(answer["text"]) <= max_chars:
        return answer
    return {**answer, "text": answer["text"][:max_chars], "truncated": True,
            "full_chars": len(answer["text"])}


def op_spawn(env: dict, count: int, anchor_ref: str | None, force: bool = False) -> list[dict]:
    """Stack `count` panes beside the anchor, and report the new ones.

    Anchor precedence: what the caller named, else the pane we are running in,
    else the active pane. The middle one is the point — anchoring on the active
    pane means anchoring on the user's focus, so children landed wherever they
    happened to be looking when spawn ran, and moved if they looked elsewhere.
    """
    # A fan-out of N children is N full claude sessions. Check the session's own
    # ceiling before opening them, not after the bill arrives.
    cost = session_cost(env)
    if not force and (blocked := budget_blocked(cost)):
        die(blocked)

    panes = list_panes(env)
    anchor = (find_pane(panes, anchor_ref) if anchor_ref
              else self_pane(panes) or active_pane(panes))
    before = {p["id"] for p in panes}
    # ##new stack adds panes to the ANCHOR's pane group. Children therefore share
    # the caller's group, which keeps them addressable and visually adjacent.
    rysh_exec(env, f"##new stack {count}", pane_id=anchor["id"])
    for _ in range(20):
        time.sleep(0.5)
        panes = list_panes(env)
        fresh = [p for p in panes if p["id"] not in before]
        if len(fresh) >= count:
            return fresh
    die("panes were not created in time")


SESSION_ID_ARG = re.compile(r"--session-id[= ]([0-9a-fA-F-]{36})")


def op_start(env: dict, pane: dict, prompt: str, claude_args: str, panes: list[dict] | None = None) -> dict:
    """Launch claude in the pane with the prompt already supplied as argv.

    argv, not typing: a prompt typed into a claude that is still drawing its
    splash screen is silently dropped, and there is no reliable "ready" signal to
    wait for. Handing the prompt to the process removes the race entirely — and
    it is still a normal interactive session, not `-p` headless mode.

    The prompt travels via a file because a ## command line is tokenised on
    whitespace and re-joined with single spaces, which would mangle any prompt
    containing runs of spaces or newlines.

    The session id is pinned here rather than read back afterwards: claude only
    reports the id it chose when it exits, which is exactly too late to follow a
    child that is still running.
    """
    caller = SESSION_ID_ARG.search(claude_args)
    session_id = caller.group(1) if caller else str(uuid.uuid4())
    args = claude_args if caller else f"--session-id {session_id} {claude_args}"

    path = os.path.join(fanout_dir(env), f"prompt-{pane['id'][:8]}-{int(time.time())}.txt")
    with open(path, "w") as fh:
        fh.write(prompt)

    # Name the pane after its task before starting it. The name is also a
    # selector, so this is a handle as well as a label: `wait audit-secrets`
    # beats `wait humorous-falcon` for anyone reading along — including the user.
    task = task_slug(prompt)
    name = unique_name(panes if panes is not None else list_panes(env), task)
    rysh_exec(env, f"##pane name {pane['id']} {name}")

    record_session(env, pane, session_id, task)
    mark_prompted(env, pane)
    out = cmd_to_pane(env, pane, f'{CLEAN_ENV} claude {args} "$(cat {path})"')
    return {"session_id": session_id, "name": name, "task": task, "output": out.strip()}


def op_send(env: dict, pane: dict, text: str, settle: float = 1.5) -> dict:
    """Type text into a running claude and make sure it is submitted.

    rysh types the text and presses Enter itself (a CR, written separately after
    a short pause so the TUI's paste detection cannot absorb it). A daemon older
    than the 2026-07-29 fix terminates with LF instead, which claude treats as
    "insert a newline" — the prompt then sits in the composer forever. So verify,
    and only press Enter ourselves if the text is still sitting there.
    """
    out = cmd_to_pane(env, pane, text)
    mark_prompted(env, pane)
    time.sleep(settle)
    screen = vt_screen(env, pane["id"])
    pending = composer_text(screen)
    probe = " ".join(text.split())[:40]
    if probe and probe[:24] in " ".join(pending.split()):
        send_keys(env, pane["id"], b"\r")
        time.sleep(settle)
        screen = vt_screen(env, pane["id"])
        pending = composer_text(screen)
        return {"ok": True, "submitted_by": "ryshfan-cr", "output": out.strip(),
                "composer": pending}
    return {"ok": True, "submitted_by": "rysh", "output": out.strip()}


def op_wait(env: dict, pane: dict, timeout: float, settle: int, warmup: float = 90,
            max_chars: int = 4000) -> dict:
    """Block until the child has stopped working, and report what it said.

    "Idle" alone is not enough to mean "finished": claude draws its input box
    while it is still starting up, so a poll taken between launch and the first
    token would call a child idle before it had begun. For a child whose session
    id we hold, the transcript settles this exactly — idle counts only once the
    child has spoken since it was prompted. Without one, fall back to the old
    heuristic: idle counts once we have seen work, or once `warmup` has elapsed.
    """
    pane_id = pane["id"]
    started = time.time()
    deadline = started + timeout
    # Measure warm-up from when the child was last given a prompt, not from this
    # call: waits are sequential, so a child that finished while we were blocked
    # on its sibling would otherwise sit out the full grace period for nothing.
    grace_from = prompted_at(pane) or started
    tracked = child_session(pane) is not None
    idle_streak, seen_busy = 0, False

    def report(state: str, extra: dict | None = None) -> dict:
        out = {"state": state, "saw_work": seen_busy,
               "waited": round(time.time() - started, 1)}
        if tracked:
            found = child_answer(env, pane)
            out.update({k: found[k] for k in ("session_id", "transcript")})
            out["answer"] = clip(found["answer"], max_chars)
        return {**out, **(extra or {})}

    while time.time() < deadline:
        screen = vt_screen(env, pane_id)
        if is_busy(screen["lines"]):
            seen_busy, idle_streak = True, 0
        elif has_composer(screen["lines"]):
            idle_streak += 1
            if idle_streak >= settle:
                if not tracked:
                    if seen_busy or time.time() - grace_from > warmup:
                        return report("idle")
                else:
                    answer = child_answer(env, pane)["answer"]
                    if answer and answer["at"] > grace_from:
                        return report("idle")
                    # Idle with nothing said yet: still booting, or the prompt
                    # never landed. Keep polling rather than calling it done.
                    idle_streak = settle
        else:
            idle_streak = 0
            # No TUI, no transcript, and past the grace period: claude never came
            # up in this pane. Say so now instead of burning the whole timeout.
            if (tracked and time.time() - grace_from > warmup
                    and not transcript_path(child_session(pane))):
                return report("never-started")
        time.sleep(2)
    return report("timeout")


def op_children(env: dict, mine_only: bool) -> list[dict]:
    """Every pane running a claude we started, with what it is doing."""
    out = []
    for pane in children(list_panes(env), mine_only):
        meta = pane_meta(pane)
        found = child_answer(env, pane)
        out.append({
            "name": pane["name"],
            "id": pane["id"],
            "task": meta.get(META_TASK, ""),
            "session_id": meta.get(META_SESSION, ""),
            # From the daemon, not from the screen: "claude" while it is up,
            # empty once it has exited.
            "running": pane["program"],
            "answered_at": (found["answer"] or {}).get("timestamp"),
            "transcript": found["transcript"],
        })
    return out


def op_close_all(env: dict, mine_only: bool) -> dict:
    """Close every child we started. Each open child is a live claude process."""
    closed = []
    for pane in children(list_panes(env), mine_only):
        rysh_exec(env, f"##pane delete {pane['id']}")
        closed.append({"name": pane["name"], "id": pane["id"]})
    return {"closed": closed, "count": len(closed)}


def op_watch(env: dict, pane: dict, timeout: float) -> dict:
    """Block until the pane's foreground program exits, using the daemon's own
    event rather than a poll.

    This is what `wait` cannot tell you: a child that CRASHED, was killed, or
    was taken over and exited looks exactly like a child thinking hard when all
    you have is a rendered screen.
    """
    nc = Nats(env["nats_host"], env["nats_port"], timeout=max(5.0, timeout))
    try:
        sub = f"{env['session']}.pane.{pane['id']}.process"
        nc.sock.sendall(b"SUB %s 90\r\n" % sub.encode())
        deadline = time.time() + timeout
        while time.time() < deadline:
            nc.sock.settimeout(max(0.1, deadline - time.time()))
            try:
                line = nc._readline()
            except (TimeoutError, OSError):
                break
            if line.startswith(b"PING"):
                nc.sock.sendall(b"PONG\r\n")
                continue
            if not line.startswith(b"MSG "):
                continue
            data = nc._read_exact(int(line.split()[-1]))
            nc._read_exact(2)
            payload = (json.loads(data).get("p") or {})
            if payload.get("event") == "exit":
                return {"state": "exited", "program": payload.get("program"),
                        "at": payload.get("at")}
        return {"state": "timeout"}
    finally:
        nc.close()


def die(msg: str) -> None:
    print(json.dumps({"error": msg}), file=sys.stderr)
    sys.exit(1)


def main() -> None:
    ap = argparse.ArgumentParser(prog="ryshfan")
    ap.add_argument("--session", help="rysh session name (required if several are running)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("discover")
    sub.add_parser("panes")

    p = sub.add_parser("spawn")
    p.add_argument("--count", type=int, default=1)
    p.add_argument("--anchor", help="pane whose group the children join (default: our own pane)")
    p.add_argument("--force", action="store_true", help="spawn even if a token ceiling is spent")

    p = sub.add_parser("start")
    p.add_argument("pane")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--prompt")
    g.add_argument("--prompt-file")
    p.add_argument("--claude-args", default="--dangerously-skip-permissions")

    p = sub.add_parser("send")
    p.add_argument("pane")
    p.add_argument("text")

    p = sub.add_parser("screen")
    p.add_argument("pane")

    p = sub.add_parser("status")
    p.add_argument("pane")

    p = sub.add_parser("wait")
    p.add_argument("pane")
    p.add_argument("--timeout", type=float, default=900)
    p.add_argument("--settle", type=int, default=3)
    p.add_argument("--warmup", type=float, default=90,
                   help="seconds to allow for start-up before an untracked, never-busy "
                        "child counts as done")
    p.add_argument("--max-chars", type=int, default=4000,
                   help="clip the reported answer (0 = no limit); the transcript path "
                        "is always returned in full")

    p = sub.add_parser("result")
    p.add_argument("pane")
    p.add_argument("--max-chars", type=int, default=0,
                   help="clip the answer (default: whole thing)")

    p = sub.add_parser("close")
    p.add_argument("pane", nargs="?", help="omit with --all")
    p.add_argument("--all", action="store_true",
                   help="close every child this supervisor started")
    p.add_argument("--any-parent", action="store_true",
                   help="with --all, include children started by another supervisor")

    p = sub.add_parser("children")
    p.add_argument("--any-parent", action="store_true",
                   help="include children started by another supervisor")

    p = sub.add_parser("watch")
    p.add_argument("pane")
    p.add_argument("--timeout", type=float, default=900)

    sub.add_parser("cost")

    args = ap.parse_args()
    env = discover(args.session)

    if args.cmd == "discover":
        print(json.dumps(env, indent=2))
        return
    if args.cmd == "panes":
        print(json.dumps(list_panes(env), indent=2))
        return
    if args.cmd == "spawn":
        print(json.dumps(op_spawn(env, args.count, args.anchor, args.force), indent=2))
        return
    if args.cmd == "cost":
        print(json.dumps(session_cost(env), indent=2))
        return
    if args.cmd == "children":
        print(json.dumps(op_children(env, not args.any_parent), indent=2))
        return
    if args.cmd == "close" and args.all:
        print(json.dumps(op_close_all(env, not args.any_parent), indent=2))
        return
    if args.cmd == "close" and not args.pane:
        die("close needs a pane, or --all")

    panes = list_panes(env)

    # Before the pane has to exist: `result` deliberately still answers for one
    # that has been closed.
    if args.cmd == "result":
        found = op_result(env, panes, args.pane)
        if not found["tracked"]:
            die(f"no session id recorded for {args.pane!r} — it was not started by "
                "`ryshfan start`, so there is no transcript to read; use `screen` if "
                "the pane is still open")
        if not found["transcript"]:
            die(f"claude has not created a transcript for session {found['session_id']} "
                "yet — it may still be starting, or it never started")
        print(json.dumps({**found, "answer": clip(found["answer"], args.max_chars)},
                         indent=2))
        return

    pane = find_pane(panes, args.pane)

    if args.cmd == "start":
        prompt = args.prompt
        if args.prompt_file:
            with open(args.prompt_file) as fh:
                prompt = fh.read()
        print(json.dumps({"pane": pane, **op_start(env, pane, prompt, args.claude_args, panes)}, indent=2))
    elif args.cmd == "send":
        print(json.dumps(op_send(env, pane, args.text), indent=2))
    elif args.cmd == "screen":
        screen = vt_screen(env, pane["id"])
        print("\n".join(screen["lines"]).rstrip())
    elif args.cmd == "status":
        screen = vt_screen(env, pane["id"])
        state = (
            "busy" if is_busy(screen["lines"])
            else "idle" if has_composer(screen["lines"])
            else "no-tui"
        )
        found = child_answer(env, pane)
        print(json.dumps({"pane": pane["name"], "state": state,
                          "composer": composer_text(screen),
                          "session_id": found["session_id"],
                          "transcript": found["transcript"],
                          "answered_at": (found["answer"] or {}).get("timestamp")}, indent=2))
    elif args.cmd == "wait":
        print(json.dumps(op_wait(env, pane, args.timeout, args.settle, args.warmup,
                                 args.max_chars), indent=2))
    elif args.cmd == "watch":
        print(json.dumps(op_watch(env, pane, args.timeout), indent=2))
    elif args.cmd == "close":
        print(json.dumps({"output": rysh_exec(env, f"##pane delete {pane['id']}").strip()}, indent=2))


if __name__ == "__main__":
    main()
