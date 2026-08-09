#!/usr/bin/env python3
"""ryshctl — run a ## command against the rysh session we are sitting in.

`rysh exec` needs three things a caller normally supplies by hand, and gets
wrong in three different ways:

  * the binary — it may be `rysh`, `ry`, or a locally built `rysh_local`;
  * the session — `--session` is required as soon as a second daemon exists;
  * a working directory holding that session's registry. rysh state is
    project-local, so the command that works in the workspace fails with
    `session "X" not found` one directory away. This is the one that wastes
    the most time, because the error names the session and not the cwd.

All three are knowable from the pane we are running in, so this asks for none
of them. It is a convenience over `rysh exec`, not a replacement: every
argument after the flags is passed through untouched, and the exit status is
rysh's own.

    ryshctl.py '##pane info'
    ryshctl.py --json '##lane list'
    ryshctl.py --pane <id> '##mode list'   # answer AS that pane
    ryshctl.py --focus '##pane info'       # the pane the USER is looking at
    ryshctl.py where                       # what this process thinks it is
    ryshctl.py dashboard                   # session, layout, what is running, spend
"""

from __future__ import annotations

import json
import os
import subprocess
import sys


def die(msg: str) -> None:
    print(f"ryshctl: {msg}", file=sys.stderr)
    sys.exit(2)


def _find_daemon_proc(session: str) -> dict | None:
    """Linux: locate the daemon for `session` in /proc."""
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                argv = [a.decode(errors="replace") for a in fh.read().split(b"\0") if a]
        except OSError:
            continue
        if len(argv) < 3 or argv[1] != "daemon" or argv[2] != session:
            continue
        try:
            return {
                "pid": int(pid),
                "session": session,
                "workspace": os.readlink(f"/proc/{pid}/cwd"),
                "bin": os.readlink(f"/proc/{pid}/exe").replace(" (deleted)", ""),
            }
        except OSError:
            continue
    return None


def _find_daemon_ps(session: str) -> dict | None:
    """macOS/BSD: no /proc, so ask ps for the argv and lsof for the cwd.

    The cwd is not optional — the daemon's working directory IS the workspace,
    and every command below is run from it — so a daemon whose cwd cannot be
    read is not usable and is skipped rather than returned half-known.
    """
    try:
        out = subprocess.run(
            ["ps", "-axo", "pid=,args="], capture_output=True, text=True, timeout=10
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.splitlines():
        pid_s, _, cmd = line.strip().partition(" ")
        if not pid_s.isdigit():
            continue
        argv = cmd.split()
        if len(argv) < 3 or argv[1] != "daemon" or argv[2] != session:
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
        return {"pid": int(pid_s), "session": session, "workspace": cwd, "bin": argv[0]}
    return None


def find_daemon(session: str) -> dict:
    """Locate the daemon for `session` and read its identity from the OS.

    The daemon's own working directory IS the workspace — that is where its
    registry, KV data and config live — so this answers the cwd question at the
    same time as the binary one. /proc where there is one, ps + lsof where there
    is not: rysh's desktop app runs on macOS, which has no /proc at all.
    """
    found = (_find_daemon_proc(session) if os.path.isdir("/proc")
             else _find_daemon_ps(session))
    if found:
        return found
    die(f"no running daemon for session {session!r} — `rysh list-sessions` in its workspace")


def run_command(env: dict, session: str, pane: str, command: str) -> str:
    """One ## command, output only. Used by the dashboard, which cares about
    what came back rather than about the exit status of each part."""
    argv = [env["bin"], "exec", "--session", session]
    if pane:
        argv += ["--pane-id", pane]
    argv += ["--", command]
    proc = subprocess.run(argv, cwd=env["workspace"], capture_output=True, text=True)
    return (proc.stdout or "") + (proc.stderr or "")


def dashboard(env: dict, session: str) -> None:
    """Everything you ask first, in one call.

    Four round-trips instead of four commands typed one after another: what the
    session is, what its panes are doing, what model is in effect, what it has
    cost. Assembling this by hand every time is how you end up not checking.
    """
    pane = os.environ.get("RYSH_PANE", "").strip()
    parts = [
        ("session", "##session"),
        ("panes", "##pane list --meta"),
        ("model", "##llm status"),
        ("spend", "##cost"),
    ]
    for title, command in parts:
        body = run_command(env, session, pane, command).strip()
        print(f"\n=== {title} " + "=" * max(0, 60 - len(title)))
        # `##pane list --meta` is the only one worth trimming: its rules and
        # footer are noise next to the pane rows.
        for line in body.splitlines():
            if line.strip().startswith("---") or line.strip().startswith("==="):
                continue
            print(line)


def main() -> None:
    args = sys.argv[1:]
    as_json = False
    focus = False
    session = os.environ.get("RYSH_SESSION", "").strip()
    pane = os.environ.get("RYSH_PANE", "").strip()

    while args and args[0].startswith("--"):
        flag = args.pop(0)
        if flag == "--json":
            as_json = True
        elif flag == "--focus":
            focus = True
        elif flag == "--session":
            session = args.pop(0) if args else die("--session needs a value")
        elif flag == "--pane":
            # Ask AS another pane. Pane-scoped commands (##pane info, ##mode
            # list, ##grounding) have no selector of their own — they answer for
            # the caller's pane — so this is the only way to ask about one.
            pane = args.pop(0) if args else die("--pane needs a pane id")
        elif flag == "--":
            break
        else:
            die(f"unknown flag {flag}")

    if not session:
        die("no RYSH_SESSION in this environment — pass --session <name>, or run "
            "from inside a pane of a daemon new enough to export it (2026-08-05)")
    env = find_daemon(session)

    if args and args[0] == "dashboard":
        dashboard(env, session)
        return

    if args and args[0] == "where":
        env["pane"] = pane or None
        env["tab"] = os.environ.get("RYSH_TAB") or None
        env["lane"] = os.environ.get("RYSH_LANE") or None
        env["stack"] = os.environ.get("RYSH_STACK") or None
        print(json.dumps(env, indent=2))
        return

    if not args:
        die("nothing to run: ryshctl.py [--json] [--focus] '##<command>'")

    argv = [env["bin"], "exec", "--session", session]
    # Pane-scoped commands ("the active pane") otherwise act on wherever the
    # USER's focus is, which moves while we work. Pinning our own pane makes
    # them deterministic; --focus is how you ask about the user's pane instead.
    if pane and not focus:
        argv += ["--pane-id", pane]
    if as_json:
        argv.append("--json")
    argv += ["--", " ".join(args)]

    # cwd, not --config: the workspace layout varies (rysh.config.yaml or
    # .rysh/rysh.config.yaml) and rysh already searches both from its cwd.
    proc = subprocess.run(argv, cwd=env["workspace"])
    sys.exit(proc.returncode)


if __name__ == "__main__":
    main()
