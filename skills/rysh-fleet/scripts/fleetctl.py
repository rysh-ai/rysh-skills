#!/usr/bin/env python3
"""fleetctl — build and drive a three-tier fleet of agent sessions in rysh.

    ceo  ──prompts──▶  managers  ──prompts──▶  workers
         ◀──reports──            ◀──reports──

One CEO pane, N manager panes stacked in one lane, N worker panes stacked in
another, paired 1:1 by unit. Every pane is a real interactive agent the user can
watch and take over — nothing here is headless `-p`.

WHICH agent is a fleet parameter: `--agent claude` (the default) or
`--agent codex`, one for the whole fleet. See AGENTS below for what differs
between them — launch and resume, and nothing else.

Built on `ryshfan` (the rysh-fanout skill) as a library, with three additions:

* ``list_panes`` is patched to union every tab, because ``##pane list`` is
  TAB-SCOPED and reports only the tab of whichever pane it ran in. Without this a
  CEO in one tab cannot even see, let alone address, a worker in another.
* an addressing layer: units, roles and pane ids resolve to the same pane, and
  the org chart lives on the panes themselves (``##pane meta``) so it survives a
  daemon restart and is readable by any tool.
* a message envelope, so every prompt says who it is from and every report says
  who it is from. An agent that cannot tell its CEO's instruction from its
  worker's answer routes work to the wrong place.

stdlib only. Everything prints JSON except ``screen`` and ``tree``.
"""
from __future__ import annotations

import argparse
import fnmatch
import contextlib
import fcntl
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import uuid

# --------------------------------------------------------------------------
# ryshfan is the substrate. Find it next door in the rysh-fanout skill.
#
# Nothing here may assume where the skill was installed. The sibling lookup
# off __file__ covers the normal case (both skills in one skills directory,
# wherever that is); the rest cover a split install — fanout in ~/.claude or
# $CODEX_HOME, fleet in the project — and RYSHFAN_DIR is the escape hatch for
# a layout none of them predicted.
# --------------------------------------------------------------------------

_HERE = os.path.dirname(os.path.abspath(__file__))
_HOME = os.path.expanduser("~")
_SKILL_ROOTS = [
    os.environ.get("RYSH_SKILLS_DIR", ""),
    os.path.join(os.environ["CLAUDE_PLUGIN_ROOT"], "skills") if os.environ.get("CLAUDE_PLUGIN_ROOT") else "",
    os.path.join(".claude", "skills"),
    os.path.join(_HOME, ".claude", "skills"),
    os.path.join(os.environ.get("CODEX_HOME") or os.path.join(_HOME, ".codex"), "skills"),
]
_CANDIDATES = [
    os.environ.get("RYSHFAN_DIR", ""),
    os.path.join(_HERE, "..", "..", "rysh-fanout", "scripts"),
    os.path.join(_HERE, "..", "..", "..", "skills", "rysh-fanout", "scripts"),
] + [os.path.join(r, "rysh-fanout", "scripts") for r in _SKILL_ROOTS if r]
for _c in _CANDIDATES:
    if _c and os.path.isfile(os.path.join(_c, "ryshfan.py")):
        sys.path.insert(0, os.path.abspath(_c))
        break
try:
    import ryshfan as R  # noqa: E402
except ImportError:  # pragma: no cover
    sys.stderr.write(
        "fleetctl: cannot import ryshfan. It ships with the rysh-fanout skill, "
        "normally installed beside this one. Searched:\n  " +
        "\n  ".join(os.path.abspath(c) for c in _CANDIDATES if c) +
        "\nPoint RYSHFAN_DIR at the directory holding ryshfan.py, or "
        "RYSH_SKILLS_DIR at the skills directory holding both.\n")
    raise SystemExit(2)


# --------------------------------------------------------------------------
# Cross-tab panes. `##tab list` is the only globally-scoped view.
# --------------------------------------------------------------------------

TAB_ROW = re.compile(r"^\s*>?\s*\[(\d+)\]\s+(\S+)\s+id=(\S+)\s+panes=(\d+)")
_ryshfan_list_panes = R.list_panes


def tabs(env: dict) -> list[dict]:
    out = R.rysh_exec(env, "##tab list")
    found = []
    for line in out.splitlines():
        m = TAB_ROW.match(line)
        if m:
            found.append({"index": int(m.group(1)), "title": m.group(2),
                          "id": m.group(3), "panes": int(m.group(4))})
    return found


def parse_pane_list(out: str) -> list[dict]:
    """Parse one `##pane list` listing into pane records.

    A copy of ryshfan's parse loop, reusing its compiled row regexes, so it can
    be pointed at `--tab <id>` output. ryshfan's own `list_panes` hardcodes the
    unqualified command, which is what makes it tab-scoped.

    lane-N / group-N / [N] are 1-based INDICES — exactly what --lane/--pg accept.
    """
    tab_title = tab_id = ""
    lane = group = 0
    panes: list[dict] = []
    for line in out.splitlines():
        if m := R.TAB_LINE.search(line):
            tab_title, tab_id = m.group(1), m.group(2)
            continue
        if m := R.LANE_LINE.match(line):
            lane, group = int(m.group(1)), 0
            continue
        if m := R.GROUP_LINE.match(line):
            group = int(m.group(1))
            continue
        if m := R.PANE_LINE.match(line):
            # ryshfan.parse_meta: a value with spaces (agent.args) used to drop
            # every key after it. Fallback for an older ryshfan on PATH.
            meta = R.parse_meta(line) if hasattr(R, "parse_meta") else {}
            run = R.RUNNING_RE.search(line)
            panes.append({
                "tab": tab_id, "tab_title": tab_title, "lane": lane, "pg": group,
                "index": int(m.group(2)), "name": m.group(3), "id": m.group(4),
                "active": ">" in m.group(1),
                "program": run.group(1) if run else "", "meta": meta,
            })
    return panes


def _list_panes(env: dict, pane_id: str | None = None) -> list[dict]:
    """Union of every tab's panes, deduped by id.

    `##pane list` is TAB-SCOPED: it reports the tab of whichever pane it ran in.
    A CEO in one tab therefore cannot see, let alone address, a worker in
    another. `##tab list` is the only globally-scoped view, so walk it and list
    each tab explicitly.
    """
    by_id: dict[str, dict] = {}
    for p in _ryshfan_list_panes(env, pane_id):
        by_id[p["id"]] = p
    for t in tabs(env):
        if any(p.get("tab") == t["id"] for p in by_id.values()):
            continue
        try:
            out = R.rysh_exec(env, f"##pane list --tab {t['id']} --meta")
        except Exception:
            continue
        for p in parse_pane_list(out):
            by_id.setdefault(p["id"], p)
    panes = list(by_id.values())
    mine = os.environ.get("RYSH_PANE", "").strip()
    for p in panes:
        p["self"] = bool(mine) and p["id"] == mine
    R.annotate_ids(panes)
    return panes


R.list_panes = _list_panes


# --------------------------------------------------------------------------
# Fleet manifest — the org chart, on disk and on the panes
# --------------------------------------------------------------------------

META_FLEET = "fleet.name"
META_ROLE = "fleet.role"          # free-form; see TEMPLATES for the shipped sets
META_BOARD = "board.id"           # which agents-board this pane posts to (028)
META_UNIT = "fleet.unit"
META_PARENT = "fleet.parent"      # pane id of the boss
META_ROADMAP = "fleet.roadmap"    # path to this agent's own roadmap file
META_WORKTREE = "fleet.worktree"
META_LABEL = "fleet.label"        # human name, same as the pane's given-name
# Whether the pane's repaints stream to web clients (rysh-cli `##pane sync`).
# "off" pauses it; the pane keeps running. See web_sync_off().
META_WEB_SYNC = "web.sync"
WEB_SYNC_POLICIES = ("root", "all", "none")


def web_sync_off(man: dict, member: dict) -> bool:
    """Whether `member` launches with its web repaint stream paused.

    A browser showing a fleet stalls: twenty-plus agents each repaint several
    times a second, and every repaint is streamed to it. The fleet is driven
    through its head and its board, so by default (`root`) only the head of the
    chain — the orchestrator the human talks to — keeps streaming; every member
    below it launches with web.sync=off. The fleet's board pane is not a member
    and is never paused. `all` keeps every pane live (the old behaviour); `none`
    pauses the head too. A manifest from before this policy existed has no key
    and keeps the old behaviour, so a subfleet added to it does not suddenly go
    dark.

    Paused panes are resumed with `##pane sync on --all` (or per pane, or by
    clicking SYNC OFF in the pane's title bar).
    """
    policy = man.get("web_sync", "all")
    if policy == "none":
        return True
    if policy == "root":
        return bool(member.get("parent"))
    return False

MEDIA = {".png", ".jpg", ".jpeg", ".gif", ".mp4", ".mov", ".webm", ".mkv",
         ".pdf", ".zip", ".tar", ".gz", ".mp3", ".wav", ".ico", ".webp"}


def fleet_dir(env: dict) -> str:
    d = os.path.join(env["workspace"], ".rysh", "fleet")
    os.makedirs(d, exist_ok=True)
    return d


def manifest_path(env: dict, fleet: str) -> str:
    return os.path.join(fleet_dir(env), f"{fleet}.json")


def load_manifest(env: dict, fleet: str) -> dict:
    p = manifest_path(env, fleet)
    if not os.path.isfile(p):
        die(f"no fleet named {fleet!r} (looked in {p}). `fleetctl fleets` lists them.")
    with open(p) as fh:
        return json.load(fh)


def save_manifest(env: dict, man: dict) -> None:
    """Write the manifest ATOMICALLY: temp file, fsync, rename.

    `open(path, "w")` truncates BEFORE it writes, so a process dying mid-dump
    left a CORRUPT manifest rather than a stale one -- the fleet's whole org
    chart, unparseable, with no backup. os.replace is atomic on POSIX, so a
    reader sees either the old file or the new one and never a half-written one.
    """
    path = manifest_path(env, man["fleet"])
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w") as fh:
        json.dump(man, fh, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


@contextlib.contextmanager
def manifest_txn(env: dict, fleet: str):
    """Read-modify-write the manifest under an exclusive lock.

    THE READ IS INSIDE THE CRITICAL SECTION, and that is the entire fix for
    F-22. A lock around the write alone is not enough: every process would still
    read msg_seq=0 first, all mint msg-0001, and the last writer would win. That
    is exactly what happened -- twelve concurrent sends produced twelve
    `msg-0001` and ONE surviving record, on both delivery paths.

    The caller's `man` is deliberately NOT the object mutated here. Whatever it
    loaded may be minutes stale; the fresh copy read under the lock is the only
    safe basis for an increment.

    Callers must NOT do anything slow inside this block -- above all not a
    network send. The lock exists to serialise a file update, not a delivery.
    """
    lock_path = manifest_path(env, fleet) + ".lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        fresh = load_manifest(env, fleet)
        yield fresh
        save_manifest(env, fresh)
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)



# --------------------------------------------------------------------------
# The daemon's fleet registry (design 028 §6.5) — the AUTHORITY.
#
# The manifest under .rysh/fleet/ stays, but as a CACHE. It is an untracked
# file shared by every fleet in the workspace, and two live failures came from
# exactly that: a sibling fleet's teardown deleted a live fleet's manifest, and
# a worktree step reported created:true for a directory that did not exist.
#
# Every call here is BEST EFFORT and never fatal. A daemon that predates the
# registry answers "unknown command", and a fleet must still stand up against
# it -- the manifest is what makes that possible. What is NOT acceptable is
# pretending the registry recorded something it did not, so failures are
# collected and reported in the command's own output rather than swallowed.
# --------------------------------------------------------------------------


def registry(env: dict, *argv: str) -> dict:
    """Run `rysh fleet ...` and return its JSON, or {"error": ...}."""
    cmd = [env["bin"], "fleet", *argv, "--session", env["session"], "--json"]
    try:
        proc = subprocess.run(cmd, cwd=env["workspace"], capture_output=True,
                              text=True, timeout=15)
    except Exception as exc:  # noqa: BLE001 - a broken registry must not kill a fleet
        return {"error": f"{exc}"}
    out = (proc.stdout or "").strip()
    # PREFER THE JSON REPLY, even on a non-zero exit. `rysh fleet` prints the
    # structured refusal to stdout AND an error line to stderr, so gluing the
    # two together (which this did until 2026-08-11, found live) produced a
    # `reason` that carried the message twice and could not be parsed at all.
    if out:
        try:
            parsed = json.loads(out)
            if isinstance(parsed, dict) and parsed.get("err"):
                return {"error": parsed["err"]}
            if proc.returncode == 0:
                return parsed
        except json.JSONDecodeError:
            pass
    if proc.returncode != 0:
        return {"error": ((proc.stderr or "") + " " + out).strip() or f"exit {proc.returncode}"}
    return {}


def registry_sync(env: dict, man: dict, state: str) -> dict:
    """Mirror a manifest into the daemon's registry.

    Returns a report rather than raising: the caller prints it, so a fleet that
    is running while the registry does not know about it is VISIBLE instead of
    being an assumption nobody checks.
    """
    errors = []
    res = registry(env, "register", man["fleet"],
                   "--source", man.get("source") or "",
                   "--roadmap", man.get("roadmap_dir") or "",
                   "--tab", man.get("tab") or "")
    if res.get("error"):
        errors.append(res["error"])
        # Nothing else can land if the fleet itself is not there.
        return {"registered": False, "members": 0, "errors": errors}

    members = 0
    for p in roster(man):
        r = registry(env, "member", "add", man["fleet"],
                     "--pane", p["pane"],
                     "--role", p.get("role") or "",
                     "--label", p.get("label") or "",
                     "--unit", str(p.get("unit") or ""),
                     "--parent", p.get("parent") or "",
                     "--worktree", p.get("worktree") or "",
                     "--session-id", p.get("session_id") or "")
        if r.get("error"):
            errors.append(f"{p['label']}: {r['error']}")
            continue
        members += 1
    state_error = None
    if state:
        r = registry(env, "state", man["fleet"], state)
        if r.get("error"):
            errors.append(r["error"])
            state_error = r["error"]
    return {"registered": True, "members": members, "errors": errors,
            "state_error": state_error}


# --------------------------------------------------------------------------
# Truthful fleet state (founder ruling R11, 2026-10-07)
#
# D-14's three-fleet cap is retired. The registry records state; it does not
# ration it. `up` is written only after a launch that succeeded, a failed launch
# never says `up`, `down` writes `down`, and the limit on spend is the token
# ceiling `budget_blocked` checks BEFORE anything launches.
#
# A DAEMON STILL ON A PRE-R11 BINARY refuses `up` past three fleets. That
# refusal is a fact about the RECORD, not the fleet: the agents are running, so
# it is reported and the panes are left alone. Taking working agents down to
# make a row agree with an obsolete cap would be the worse lie.
# --------------------------------------------------------------------------

def launch_outcome(env: dict, man: dict, planned: int) -> dict:
    """Did the launch succeed? Judged on three concrete signals, none of them an
    exit code: every planned member got a pane, every member's pane still exists
    in a fresh pane listing, and no member's worktree failed (an agent with no
    worktree edits the shared main checkout).

    Deliberately NOT the agent's screen: a claude typed into a shell is still
    booting for seconds after this returns, and `verify` is the screen check.
    """
    problems = []
    members = roster(man)
    if len(members) < planned:
        problems.append(f"{len(members)} of {planned} planned members got a pane")
    live = {p["id"] for p in R.list_panes(env)}
    for p in members:
        if p["pane"] not in live:
            problems.append(f"{p['label']}: pane {short(p['pane'])} is gone after launch")
        if p.get("worktree_error"):
            problems.append(f"{p['label']}: worktree failed: {p['worktree_error']}")
    return {"ok": not problems, "problems": problems}


def record_launch_state(env: dict, man: dict, outcome: dict) -> dict:
    """Write what the launch earned: `up` on success, `registered` otherwise."""
    wanted = "up" if outcome.get("ok") else "registered"
    reg = registry_sync(env, man, wanted)
    st = {"wanted": wanted, "recorded": not reg.get("state_error"), "registry": reg}
    if reg.get("state_error"):
        st["reason"] = reg["state_error"]
        st["note"] = ("the registry refused the state; the panes were left running. A "
                      "daemon still on a pre-R11 binary refuses `up` past three fleets "
                      "until it is restarted — then `fleetctl reconcile --apply` fixes "
                      "the rows from the live panes.")
    return st


def launch_roster(env: dict, man: dict, args, agent: str, launch_args: str) -> list:
    """Start every member's agent. A launch that dies part-way (start_agent
    `die`s) records `registered` before it re-raises: a fleet whose launch
    failed must never be left reading `up`, nor be missing from the registry."""
    launched, trusted = [], []
    try:
        for p in roster(man):
            prompt = build_brief(env, man, p, args)
            res = start_agent(env, R.find_pane(R.list_panes(env), p["pane"]),
                              prompt, p.get("worktree"), launch_args, agent, man["fleet"],
                              comm=fleet_comm(man))
            p["session_id"] = res["session_id"]
            stamp_launch(env, p["pane"], res, agent, res.get("launch_args", launch_args),
                         task=p["label"])
            if res.get("trusted", {}).get("added"):
                trusted.append(res["trusted"]["path"])
            launched.append({"label": p["label"], "role": p["role"],
                             "pane": p["pane"], "session_id": res["session_id"]})
    except SystemExit:
        save_manifest(env, man)
        registry_sync(env, man, "registered")
        raise
    if trusted:
        man["codex_trusted"] = trusted
    save_manifest(env, man)
    return launched


def fleet_liveness(fleet: dict, panes: list[dict]) -> dict:
    """How much of a registry row is actually running: member panes that exist,
    and of those, panes with a foreground program (an agent, not a bare shell)."""
    by_id = {p["id"]: p for p in panes}
    ids = [m.get("pane_id") for m in (fleet.get("members") or []) if m.get("pane_id")]
    present = [by_id[i] for i in ids if i in by_id]
    running = [p for p in present if (p.get("program") or "").strip()]
    return {"members": len(ids), "panes_live": len(present), "agents_running": len(running)}


def op_reconcile(env: dict, apply: bool = False) -> dict:
    """One-shot: set each fleet's registry state from whether its panes are live.

    Any live member pane -> `up`. No live pane and recorded `up` -> `down`. A
    fleet never brought up stays `registered`. DRY RUN unless `apply`; with
    `apply`, only the rows that change are written, and a refusal (a pre-R11
    daemon) is reported per fleet rather than retried.
    """
    listing = registry(env, "ls")
    if listing.get("error"):
        die(f"cannot read the fleet registry: {listing['error']}")
    panes = R.list_panes(env)
    rows = []
    for f in listing.get("fleets") or []:
        live = fleet_liveness(f, panes)
        now = f.get("state") or "registered"
        if live["panes_live"]:
            to = "up"
        elif now == "up":
            to = "down"
        else:
            to = now
        row = {"fleet": f["name"], "from": now, "to": to if to != now else None, **live}
        if apply and row["to"]:
            r = registry(env, "state", f["name"], row["to"])
            if r.get("error"):
                row["error"] = r["error"]
        rows.append(row)
    changes = [r for r in rows if r["to"]]
    return {"dry_run": not apply, "fleets": rows,
            "summary": {"fleets": len(rows), "changes": len(changes),
                        "to_up": sum(1 for r in changes if r["to"] == "up"),
                        "to_down": sum(1 for r in changes if r["to"] == "down"),
                        "refused": sum(1 for r in rows if r.get("error"))},
            "apply_with": "fleetctl reconcile --apply"}


def die(msg: str) -> None:
    print(json.dumps({"error": msg}, indent=2))
    raise SystemExit(1)


# --------------------------------------------------------------------------
# Unit discovery: a directory of docs, one doc split by heading, or source code
# --------------------------------------------------------------------------

UNIT_KEY = re.compile(r"^([A-Za-z]*?)(\d+)")
BUILD_FILES = ("go.mod", "package.json", "pyproject.toml", "Cargo.toml",
               "pom.xml", "build.gradle", "CMakeLists.txt", "Gemfile")


def _title_of(path: str) -> str:
    try:
        with open(path, errors="replace") as fh:
            for line in fh:
                if line.startswith("#"):
                    return line.lstrip("#").strip()
    except OSError:
        pass
    return os.path.splitext(os.path.basename(path))[0]


def units_from_dir(path: str, pattern: str) -> list[dict]:
    """One unit per file, with same-prefix files merged into one unit.

    `epic07-a16z-pitch.md`, `-memo.md`, `-claims.md` and `-objections.md` are one
    epic in four files. Keying on the leading `<word><number>` merges them; a
    naive one-file-one-unit split would have spawned four managers for one epic
    and left three of them fighting over the same doc.
    """
    import fnmatch
    files = sorted(f for f in os.listdir(path)
                   if fnmatch.fnmatch(f, pattern)
                   and os.path.isfile(os.path.join(path, f)))
    groups: dict[str, list[str]] = {}
    for f in files:
        stem = os.path.splitext(f)[0]
        m = UNIT_KEY.match(stem)
        key = (m.group(1) + m.group(2)) if m else stem
        groups.setdefault(key, []).append(os.path.join(path, f))
    units = []
    for i, (key, members) in enumerate(sorted(groups.items()), 1):
        m = UNIT_KEY.match(key)
        uid = m.group(2) if m else f"{i:02d}"
        units.append({"unit": uid, "key": key, "title": _title_of(members[0]),
                      "sources": sorted(members)})
    return units


def units_from_file(path: str, split_on: str) -> list[dict]:
    """One unit per heading section of a single document.

    AN EMPTY PATTERN IS REFUSED, and this is not defensive tidiness: `re.compile("")`
    matches EVERY line, so `--from <file>` with no `--split-on` silently produced
    one unit per line. Found live 2026-08-11 when the epic driver promoted a
    fleet and `up` set out to open **248 panes** for a single epic doc. Nothing
    downstream would have caught it — the pane limit refuses late, after the
    session is already wedged.
    """
    if not split_on.strip():
        die(f"--from {path!r} names a FILE, so --split-on is required "
            f"(e.g. --split-on '^## '). An empty pattern matches every line and "
            f"would make one unit — and one agent — per line of the document.")
    rx = re.compile(split_on)
    with open(path, errors="replace") as fh:
        lines = fh.readlines()
    starts = [i for i, ln in enumerate(lines) if rx.match(ln)]
    if not starts:
        die(f"no headings matching {split_on!r} in {path}")
    units = []
    for n, s in enumerate(starts, 1):
        e = starts[n] if n < len(starts) else len(lines)
        title = lines[s].lstrip("#").strip()
        m = UNIT_KEY.match(title)
        uid = m.group(2) if m else f"{n:02d}"
        units.append({"unit": uid, "key": f"unit{uid}", "title": title,
                      "sources": [path], "lines": [s + 1, e]})
    return units


def units_from_source(root: str, max_units: int) -> list[dict]:
    """Derive units from the shape of the codebase: one per buildable component.

    A component is a directory carrying a build manifest (`go.mod`,
    `package.json`, …). That is the only definition of "module" every language in
    a polyglot monorepo agrees on, and it is checkable rather than guessed.
    """
    found = []
    skip = {".git", "node_modules", "vendor", "worktrees", ".claude", "dist",
            "build", "target", ".venv", "__pycache__"}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip and not d.startswith(".")]
        if dirpath == root:
            continue
        if any(b in filenames for b in BUILD_FILES):
            rel = os.path.relpath(dirpath, root)
            if rel.count(os.sep) > 2:
                continue
            found.append(rel)
            dirnames[:] = []
    found.sort()
    if not found:
        die(f"no buildable component found under {root} "
            f"(looked for {', '.join(BUILD_FILES)})")
    units = []
    for i, rel in enumerate(found[:max_units], 1):
        units.append({"unit": f"{i:02d}", "key": f"unit{i:02d}",
                      "title": f"{rel} — component audit and work",
                      "sources": [os.path.join(root, rel)]})
    return units


def discover_units(args) -> list[dict]:
    units = _discover_units(args)
    excludes = getattr(args, "exclude", None) or []
    if not excludes:
        return units
    # A unit is dropped only when EVERY source file matches an --exclude glob:
    # a unit built from two files (E26's epic + its decision packet) is not lost
    # because one of them matched.
    return [u for u in units
            if not u.get("sources")
            or not all(any(fnmatch.fnmatch(os.path.basename(sp), g) for g in excludes)
                       for sp in u["sources"])]


def _discover_units(args) -> list[dict]:
    src = args.source
    if args.scan_source:
        return units_from_source(src, args.max_units)
    if os.path.isdir(src):
        return units_from_dir(src, args.pattern)
    if os.path.isfile(src):
        return units_from_file(src, args.split_on)
    die(f"--from {src!r} is neither a file nor a directory")


# --------------------------------------------------------------------------
# Naming
# --------------------------------------------------------------------------

def slug(text: str, words: int = 3, cap: int = 20) -> str:
    """A readable label from a unit title.

    Bare numbers and single letters are dropped: titles here look like
    "Epic 01 — Phase 0: launch readiness" and "G-8 · G-9: the OSS launch engine",
    so keeping digits yields `01-0-launch` and keeping single letters yields
    `g-g-oss`. The unit number is already in the label; the words are what make
    it readable.
    """
    skipw = {"the", "a", "an", "in", "of", "to", "and", "for", "on", "at", "is",
             "epic", "unit", "phase", "wave", "track"}
    toks = [w for w in re.findall(r"[A-Za-z0-9]+", text.lower())
            if w not in skipw and not w.isdigit() and len(w) > 1]
    return "-".join(toks[:words])[:cap].strip("-") or "unit"


def unique(existing: set[str], base: str) -> str:
    """Given-names are unique per lane, so collisions must be resolved up front."""
    name, n = base, 2
    while name in existing:
        name = f"{base}-{n}"
        n += 1
    existing.add(name)
    return name


# --------------------------------------------------------------------------
# Panes: lanes, stacks, starting claude
# --------------------------------------------------------------------------

def wait_for_new(env: dict, before: set[str], count: int, tries: int = 24) -> list[dict]:
    for _ in range(tries):
        time.sleep(0.5)
        panes = R.list_panes(env)
        fresh = [p for p in panes if p["id"] not in before]
        if len(fresh) >= count:
            return fresh
    die(f"expected {count} new pane(s); the daemon did not create them in time")


def new_tab(env: dict, title: str) -> str | None:
    """Open a tab for this fleet and return its id, or None.

    ONE TAB PER FLEET is what makes several fleets watchable at once (design 028
    §6.8): lanes per role inside it, plus the fleet's board pane. It waits for
    the tab to APPEAR rather than trusting the command's exit code — `##new tab`
    reports success the instant the message is published, and the tab is created
    one actor hop later.
    """
    before = {t["id"] for t in tabs(env)}
    R.rysh_exec(env, "##new tab")
    for _ in range(20):
        found = [t for t in tabs(env) if t["id"] not in before]
        if found:
            tab = found[0]["id"]
            R.rysh_exec(env, f"##tab name {tab} {title}")
            return tab
        time.sleep(0.25)
    return None


def new_lane(env: dict, tab: str | None) -> dict:
    before = {p["id"] for p in R.list_panes(env)}
    R.rysh_exec(env, f"##new lane {tab}" if tab else "##new lane")
    return wait_for_new(env, before, 1)[0]


GROUP_UUID_IN_REFUSAL = re.compile(r"pane group ([0-9a-f-]{36})")


def open_fleet_board(env: dict, tab: str, fleet: str) -> dict | None:
    """Open the fleet's agents-board in its own right-most lane (the
    oss-export setup, 2026-08-14).

    `##board open --board <fleet>` appends the board pane as a NEW group at the
    bottom of the tab's LAST lane — it ignores which pane exec'd it, and there
    is no pane-move verb. So: make the lane first (it arrives holding a shell
    pane), open the board (lands in that lane as its own group), then delete
    the shell pane's group. `##pane delete` refuses the only pane of a group,
    but the refusal prints the FULL group uuid, which `pane-group delete`
    accepts.

    Finally stamp `board.id` on the board pane itself: a board pane with no
    `board.id` meta renders the DEFAULT session board (F-54) — a 2-agent fleet
    board once showed 12 registered agents exactly this way.
    """
    shell = new_lane(env, tab)
    before = {p["id"] for p in R.list_panes(env)}
    R.rysh_exec(env, f"##board open --board {fleet}", pane_id=shell["id"])
    board_pane = None
    for _ in range(20):
        fresh = [p for p in R.list_panes(env) if p["id"] not in before]
        if fresh:
            board_pane = fresh[0]
            break
        time.sleep(0.25)
    out = R.rysh_exec(env, f"##pane delete {shell['id']}")
    m = GROUP_UUID_IN_REFUSAL.search(out or "")
    if m:
        subprocess.run(
            [env["bin"], "pane-group", "delete", "--session", env["session"],
             "--id", m.group(1)],
            cwd=env["workspace"], capture_output=True, text=True)
    if board_pane is None:
        return None
    R.rysh_exec(env, f"##pane meta --pane {board_pane['id']} set board.id {fleet}")
    return {"pane": board_pane["id"], "board": fleet}


def delete_lanes_of(env: dict, panes: list[dict]) -> None:
    """Delete the lanes holding the given panes (the default shell lanes a new
    tab is born with). `##lane delete` accepts nothing but the full uuid, which
    only `##lane info` run AS a pane in the lane prints whole."""
    for p in panes:
        lu = lane_uuid(env, p["id"])
        if lu:
            R.rysh_exec(env, f"##lane delete {lu}")


def stack_onto(env: dict, anchor: dict, count: int) -> list[dict]:
    """Add `count` panes to the anchor's own pane group."""
    if count <= 0:
        return []
    before = {p["id"] for p in R.list_panes(env)}
    R.rysh_exec(env, f"##new stack {count}", pane_id=anchor["id"])
    fresh = wait_for_new(env, before, count)
    return sorted(fresh, key=lambda p: p.get("index", 0))


def new_group_in_lane(env: dict, tab: str, lane, count: int) -> list[dict]:
    """A fresh pane GROUP in an existing lane, then stacked to `count`.

    `##new pane` opens a new group; `##new stack` adds to an existing one. A
    manager building its own sub-fleet wants the former, so its workers sit in
    their own block instead of interleaving with everyone else's.
    """
    before = {p["id"] for p in R.list_panes(env)}
    R.rysh_exec(env, f"##new pane {tab} {lane}")
    head = wait_for_new(env, before, 1)[0]
    return [head] + stack_onto(env, head, count - 1)


LANE_ID_LINE = re.compile(r"^\s*id\s*:\s*([0-9a-f-]{36})\s*$", re.M)


def lane_uuid(env: dict, pane_id: str) -> str | None:
    """The full uuid of the lane holding `pane_id`.

    Every other view truncates lane ids to eight characters, and `##lane delete`
    rejects the truncated form with "not found" — so a fleet that only recorded
    what `##lane list` printed could never tear itself down.
    """
    m = LANE_ID_LINE.search(R.rysh_exec(env, "##lane info", pane_id=pane_id))
    return m.group(1) if m else None


def rename(env: dict, pane_id: str, name: str) -> None:
    """`##pane name` takes a POSITIONAL id.

    It does not understand `--pane`: given the flag it silently renames the pane
    the command ran in and drops the target. Silent, so always read it back.
    """
    R.rysh_exec(env, f"##pane name {pane_id} {name}")


def set_meta(env: dict, pane_id: str, key: str, value: str) -> None:
    """`##pane meta` takes `--pane` and rejects a qualified --tab/--lane selector."""
    R.rysh_exec(env, f"##pane meta set {key} {value} --pane {pane_id}")


def clear_meta(env: dict, pane_id: str, key: str) -> None:
    """Delete one pane meta key. A key that is not there is reported by the
    daemon and changes nothing, which is what a clear wants."""
    R.rysh_exec(env, f"##pane meta delete {key} --pane {pane_id}")


# --------------------------------------------------------------------------
# Which agent a fleet runs — `--agent claude|codex|rysh`
#
# TABLE-DRIVEN ON PURPOSE. Two things about an agent are agent-specific: how it
# is LAUNCHED and how it is RESUMED. Everything else in the fleet is agnostic —
# the org chart, the briefs, the boards, the addressing, the roadmap files.
#
# `rysh` is the row that proves the table was worth building, because it is not
# a CLI at all. A rysh agent is the pane's OWN LLMPromptExecutionActor running
# inside the daemon: nothing is spawned, nothing owns the PTY, and the pane's
# foreground program stays empty for the entire life of the agent. That one fact
# changes every column:
#
#   |            | claude / codex            | rysh                             |
#   |------------|---------------------------|----------------------------------|
#   | launch     | spawn a CLI, brief as argv | deliver the brief as a PROMPT    |
#   | where      | a subprocess in the pane   | the daemon, in the pane's actor  |
#   | stop       | SIGTERM the process group  | cancel the turn (state kept)     |
#   | verified   | no foreground program      | the agent says it is not in flight|
#   | resume     | re-launch + a nudge        | `continue` from the checkpoint   |
#   | loses      | the in-flight response     | nothing — the turn is checkpointed|
#
# The stop/resume half lives in rysh-cli (`killNativeTurn`, `ansaResumeNative`);
# this side stamps `fleet.agent` so that code knows which branch to take.
# --------------------------------------------------------------------------

AGENTS = {
    # claude: the session id is pinned at launch, so resume needs no lookup.
    "claude": {"default_args": "--dangerously-skip-permissions",
               "pins_session_id": True, "native": False},
    # codex: NO launch-time id pin — codex chooses its own id and only records it
    # on disk afterwards. Resume keys off the working directory instead
    # (`codex resume --last` is cwd-filtered), which is why a codex fleet wants
    # `--worktrees`: see the collision warning in codex_trust/op_up.
    "codex": {"default_args": "--dangerously-bypass-approvals-and-sandbox",
              "pins_session_id": False, "native": False},
    # rysh: no process, no flags, no id. The brief is delivered as a prompt and
    # the pane's own agent runs it — with rysh's full tool set (bash, file_read,
    # edit, git_commit, sub_agent, …), so a native worker does the same KIND of
    # work as a claude one. There are no launch args because there is no command
    # line to put them on; permission mode is the pane's policy and its approval
    # settings, which is a different mechanism, not a missing one.
    "rysh": {"default_args": "", "pins_session_id": False, "native": True},
}

META_AGENT = "fleet.agent"        # which agent this pane runs: claude|codex|rysh
META_AGENT_ARGS = "agent.args"    # its launch args, so resume restores the mode
# A native agent's permission mode is not a command-line flag, so it needs its
# own key — and resume needs it for the same reason the CLI agents need
# `agent.args`: a fleet stop must not quietly change the mode an agent runs in.
# A cancel DISARMS the run budget, which clears auto-approve with it, so a
# resumed rysh agent that was not re-armed stalls at its next tool call on an
# approval prompt nobody is watching. That is f8824f5's defect in native dress.
META_AUTO_APPROVE = "rysh.auto_approve"


def is_native(agent: str) -> bool:
    """True for an agent that runs INSIDE rysh rather than as a subprocess."""
    return bool(AGENTS.get(agent, {}).get("native"))


CODEX_TRUST_MARK = "# added by fleetctl for fleet "
CODEX_PROJECT_HEADER = re.compile(r'^\[projects\."(.*)"\]\s*$')


def codex_home() -> str:
    return os.environ.get("CODEX_HOME") or os.path.join(os.path.expanduser("~"), ".codex")


def codex_trust(cwd: str, fleet: str = "") -> dict:
    """Pre-trust `cwd` in `~/.codex/config.toml`, non-interactively.

    On interactive launch codex asks *"Do you trust the contents of this
    directory?"* and BLOCKS until answered. A fleet agent has no one to answer
    it, so it would hang there forever looking exactly like a slow start. The
    grant is per-cwd, so every worktree needs its own entry.

    This WRITES TO THE USER'S CONFIG — a real, persistent grant. It is marked
    with the fleet that made it so `down` can take it back (see codex_untrust),
    and reported in `up`'s output rather than done silently.
    """
    path = os.path.join(codex_home(), "config.toml")
    entry = f'[projects."{os.path.abspath(cwd)}"]'
    existing = ""
    if os.path.isfile(path):
        with open(path) as fh:
            existing = fh.read()
    if entry in existing:
        return {"path": cwd, "config": path, "added": False}
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a") as fh:
        fh.write(f"\n{CODEX_TRUST_MARK}{fleet}\n{entry}\ntrust_level = \"trusted\"\n")
    return {"path": cwd, "config": path, "added": True}


def codex_untrust(fleet: str) -> list[str]:
    """Remove the trust grants THIS fleet added, and only those.

    Keyed on the marker comment, never on the path: a path the fleet found
    ALREADY trusted was somebody else's grant and stays (codex_trust reports
    added=False for it and writes no marker). Teardown that revokes more than it
    granted is the same defect as teardown that revokes nothing.
    """
    path = os.path.join(codex_home(), "config.toml")
    if not os.path.isfile(path):
        return []
    with open(path) as fh:
        lines = fh.read().splitlines()
    kept: list[str] = []
    removed: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith(CODEX_TRUST_MARK) and line[len(CODEX_TRUST_MARK):].strip() == fleet:
            i += 1
            if i < len(lines):
                m = CODEX_PROJECT_HEADER.match(lines[i])
                if m:
                    removed.append(m.group(1))
                    i += 1
                    # the block runs to the next table header (or EOF)
                    while i < len(lines) and not lines[i].startswith("["):
                        if lines[i].startswith(CODEX_TRUST_MARK):
                            break
                        i += 1
            continue
        kept.append(line)
        i += 1
    with open(path, "w") as fh:
        fh.write("\n".join(kept).strip("\n") + "\n")
    return removed


def start_native_agent(env: dict, pane: dict, prompt: str, cwd: str | None,
                       path: str) -> dict:
    """Give the pane's OWN agent its brief. Nothing is spawned.

    Two steps, and the order matters:

    1. Put the pane's shell in the agent's worktree. A rysh agent's tools run in
       the PANE's working directory (the executor asks the pane for it every
       turn), so a fleet started `--worktrees` needs this or every agent edits
       the shared checkout — the isolation the worktree exists to provide, lost
       silently. A CLI agent gets the same thing from `cd <wt> && claude …`.
    2. Publish the brief as a PROMPT, straight to the pane's inbox.

    THE BRIEF GOES OVER THE BUS, NOT THROUGH A `##` LINE. `##ansa prompt` would
    be the obvious route and it is the wrong one: a `##` command is a single
    line, and a brief is forty of them with backticks and quotes. Publishing
    MsgPaneExecPrompt is the same message ANSA itself sends after its F-25
    routing decision, so this is that path minus the line-oriented parser — the
    whole brief arrives verbatim, exactly as argv delivers it to a claude.

    AND THE APPROVALS ARE ARMED FIRST, WHICH IS NOT OPTIONAL AND IS ORDERED.
    A rysh agent's tool calls are gated by default: left alone, every agent in
    the fleet stops at its first `bash` on `[y]es [Y]es always [n]o` and waits
    for a human who is not coming — the whole fleet reads as booted and does
    nothing. `MsgSetRunBudget{auto_approve}` is the native equivalent of
    `--dangerously-skip-permissions`, and it must land BEFORE the prompt: the
    flag is copied onto each orchestrator when the LEG SPAWNS, so arming a run
    that is already in flight changes nothing (verified live 2026-08-12 — three
    agents stayed on `waiting_approval` through an arm sent mid-run). ryshfan's
    publish round-trips a PING, so the two publishes below are ordered on the
    wire rather than hopefully ordered.
    """
    if cwd:
        R.cmd_to_pane(env, pane, f"cd {cwd}")
    nc = R.Nats(env["nats_host"], env["nats_port"])
    try:
        # auto_approve_persist makes the grant ACTOR-level: without it the
        # daemon cleared auto-approve on EVERY terminal outcome (a clean finish
        # included), so a fleet agent was approval-free for exactly one turn
        # and its second work order stalled gated. An older daemon that does
        # not know the field ignores it and keeps the old one-turn behaviour —
        # the ANSA per-prompt re-arm still covers that binary.
        nc.publish(f"{env['session']}.pane.{pane['id']}.llm_prompt_execution.inbox",
                   R.envelope("MsgSetRunBudget",
                              {"auto_continue": False, "auto_approve": True,
                               "auto_approve_persist": True}))
        nc.publish(f"{env['session']}.pane.{pane['id']}.inbox",
                   R.envelope("MsgPaneExecPrompt", {"prompt": prompt}))
    finally:
        nc.close()
    return {"session_id": "", "prompt_file": path, "agent": "rysh",
            "auto_approve": True,
            "output": "brief delivered as a prompt (no process)"}


def native_run_status(env: dict, pane_id: str) -> dict:
    """Ask a pane's own agent what it is doing.

    The same request `##ansa kill` uses to decide whether a program-less pane is
    idle or working — one fact, one source. A pane that does not answer has
    nothing running in it, which is why a timeout returns the idle shape rather
    than raising: silence here is an answer, not a failure.
    """
    nc = None
    try:
        nc = R.Nats(env["nats_host"], env["nats_port"])
        raw = nc.request(f"{env['session']}.pane.{pane_id}.llm_prompt_execution.inbox",
                         R.envelope("MsgGetRunStatus", {}), timeout=1.5)
        body = json.loads(raw).get("p", {}) or {}
    except Exception:
        return {"in_flight": False, "paused": False, "answered": False}
    finally:
        if nc is not None:
            nc.close()
    return {"in_flight": bool(body.get("in_flight")),
            "paused": bool(body.get("paused")),
            "answered": True}


# --------------------------------------------------------------------------
# Comm modes (design 050): how one fleet agent's message reaches another
# --------------------------------------------------------------------------
#
#   input-stream    ANSA types the message into the recipient's pane and presses
#                   Enter. The pane's composer is shared by the human and every
#                   agent that messages it -- and ANSA clears it first (ctrl+U x8),
#                   so a message arriving while the human types ERASES the human's
#                   draft. That is not a race to tune; it is the mechanism.
#   polling-stream  The message is a COMMAND on the fleet's agents board
#                   (`rysh board send`). The daemon's courier delivers it into the
#                   recipient's own session -- a claude's inbox socket, `codex
#                   queue` -- beside the keyboard, never through it. The composer
#                   belongs to the human alone. Founder default since 2026-10-11.
#
# A manifest written before design 050 has no `comm` and was input-stream:
# fleet_comm() reads it that way, so a live fleet never changes mode under an
# upgrade of this file.

# Design 051 split the board mode in two and named them:
#   queue  (default) every agent runs its OWN listener on the board (`rysh board
#          listen`) and takes the prompts addressed to it; no central pusher.
#   grid   point-to-point: the daemon's courier writes each prompt into the
#          recipient's inbox. "polling-stream" is its pre-051 name and is
#          accepted, and read, as grid.
COMM_QUEUE = "queue"
COMM_GRID = "grid"
COMM_POLLING = "polling-stream"   # legacy name of grid
COMM_INPUT = "input-stream"
COMMS = (COMM_QUEUE, COMM_GRID, COMM_INPUT, COMM_POLLING)
DEFAULT_COMM = COMM_QUEUE
BOARD_COMMS = (COMM_QUEUE, COMM_GRID)
META_COMM = "fleet.comm"
META_CLAUDE_INBOX = "claude.inbox"
META_CODEX_SESSION = "codex.session_id"
CODEX_DISCOVER_RETRIES = 150   # x 2s in the daemon: five minutes


def normalize_comm(comm: str | None) -> str:
    c = (comm or "").strip()
    return COMM_GRID if c == COMM_POLLING else c


def fleet_comm(man: dict) -> str:
    return normalize_comm(man.get("comm")) or COMM_INPUT


def is_board_comm(comm: str | None) -> bool:
    """queue and grid both carry prompts on the board; only delivery differs."""
    return normalize_comm(comm) in BOARD_COMMS


def listen_state_dir(env: dict) -> str:
    """<rysh_dir>/listen of the session: where `rysh board listen` keeps its
    per-pane lock, pid and delivered record (its own default, made explicit so
    fleetctl and the listener can never disagree)."""
    rec = R.session_record(env.get("workspace") or os.getcwd(), env["session"])
    base = rec.get("rysh_dir") or os.path.join(env.get("workspace") or os.getcwd(), ".rysh")
    return os.path.join(base, "listen")


def start_listener(env: dict, man: dict, pane_id: str) -> dict:
    """Start the agent's queue listener: one detached `rysh board listen` per pane.

    Detached (its own session) and owned by nobody: it must survive fleetctl
    exiting and the agent restarting, and it stops by itself when its pane is
    gone or leaves queue messaging. Idempotent — a pane that already has a
    listener keeps it (the listener's own lock answers "already listening").
    """
    sd = listen_state_dir(env)
    os.makedirs(sd, exist_ok=True)
    argv = [agent_bin(env), "board", "listen", "--as", pane_id, "--board", man["fleet"],
            "--session", env["session"], "--state-dir", sd]
    log = open(os.path.join(sd, f"{pane_id}.log"), "a")
    try:
        proc = subprocess.Popen(argv, cwd=env.get("workspace") or None, stdin=subprocess.DEVNULL,
                                stdout=log, stderr=log, start_new_session=True)
    finally:
        log.close()
    return {"pane": pane_id, "pid": proc.pid, "state_dir": sd}


def listener_alive(env: dict, pane_id: str) -> int:
    """The pid of the pane's live listener, 0 when there is none."""
    try:
        with open(os.path.join(listen_state_dir(env), f"{pane_id}.pid")) as fh:
            pid = int(fh.read().strip() or 0)
        os.kill(pid, 0)
        return pid
    except (OSError, ValueError):
        return 0


def claude_comm_settings(env: dict, pane: dict) -> str:
    """Write the `--settings` file a polling-stream claude is launched with.

    Two keys, both load-bearing, both proven live in design 050 P2:

    * `crossSessionInbound: accept`. Fleet claudes bypass permissions, and in
      that mode Claude Code HOLDS an inbound message from a sender that does not
      attest bypass -- behind an approval dialog in the composer, which is
      exactly the surface polling-stream exists to keep clear.
    * a SessionStart hook that stamps `claude.inbox` on the pane with the
      session's own inbox socket. The courier also finds claude by its pid, but
      Claude Code binds elsewhere on a collision, and the stamp is the session
      saying where it is. SessionStart fires on start, resume and /clear, so a
      restarted claude re-stamps its new socket.

    The hook runs `rysh exec` FROM THE WORKSPACE with --config when there is
    one: a bare exec from a worktree has been seen to start a duplicate daemon
    under the session's name. Its output goes to /dev/null (a SessionStart
    hook's stdout becomes model context) and it can never fail the launch.
    """
    ws = env.get("workspace") or os.getcwd()
    cfg = os.path.join(ws, "rysh.config.yaml")
    exec_argv = [agent_bin(env)]
    if os.path.isfile(cfg):
        exec_argv += ["--config", cfg]
    exec_argv += ["exec", "--session", env["session"], "--"]
    hook = (f"cd {shlex.quote(ws)} && "
            + " ".join(shlex.quote(a) for a in exec_argv)
            + f' "##pane meta set --pane $RYSH_PANE {META_CLAUDE_INBOX} $CLAUDE_CODE_MESSAGING_SOCKET"'
            + " >/dev/null 2>&1 || true")
    settings = {
        "crossSessionInbound": "accept",
        "hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": hook}]}]},
    }
    path = os.path.join(R.fanout_dir(env), f"fleet-settings-{pane['id'][:8]}.json")
    with open(path, "w") as fh:
        json.dump(settings, fh, indent=1)
    return path


def require_board_send_capable(env: dict) -> None:
    """Refuse a polling-stream fleet the session cannot carry -- before any pane opens.

    polling-stream needs the CLI's `board send/inbox` AND a daemon that answers
    them (MsgCLIBoardResolve, and the courier beside it). Either one missing
    fails only at the first message otherwise -- after a tab, lanes, worktrees
    and briefs were made -- which is gap B again (see require_ansa_capable). A
    session started before design 050 keeps its old daemon however new the
    binary on disk is, so the probe asks the DAEMON, with a real request.
    """
    me = os.environ.get("RYSH_PANE", "").strip()
    if not me:
        panes = R.list_panes(env)
        me = panes[0]["id"] if panes else ""
    argv = [env["bin"], "board", "inbox", "--as", me or "-", "--session", env["session"]]
    try:
        proc = subprocess.run(argv, cwd=env.get("workspace") or None,
                              capture_output=True, text=True, timeout=15)
        ok, detail = proc.returncode == 0, ((proc.stdout or "") + (proc.stderr or "")).strip()
    except Exception as exc:
        ok, detail = False, f"{type(exc).__name__}: {exc}"
    if not ok:
        die(f"--comm polling-stream (the default) needs a rysh daemon with board commands and "
            f"the courier (design 050), and session {env['session']!r} did not answer one: "
            f"{detail.splitlines()[0] if detail else 'no output'!r}. Restart the session on a "
            f"binary built from rysh-cli dev at or after f380d25, or build this fleet with "
            f"--comm input-stream. Nothing has been opened.")


def require_queue_capable(env: dict) -> None:
    """Refuse a queue fleet on a daemon whose courier predates design 051.

    Such a courier does not know to leave queue panes to their listeners, so
    every prompt would arrive TWICE — once from the courier, once from the
    agent's listener. `rysh board caps` asks the running daemon; an old binary
    has no `caps` verb and an old daemon reports none, and both are refused.
    """
    me = os.environ.get("RYSH_PANE", "").strip()
    if not me:
        panes = R.list_panes(env)
        me = panes[0]["id"] if panes else ""
    argv = [env["bin"], "board", "caps", "--session", env["session"]]
    if me:
        argv += ["--as", me]
    try:
        proc = subprocess.run(argv, cwd=env.get("workspace") or None,
                              capture_output=True, text=True, timeout=15)
        caps = (proc.stdout or "").split() if proc.returncode == 0 else []
        detail = ((proc.stdout or "") + (proc.stderr or "")).strip()
    except Exception as exc:
        caps, detail = [], f"{type(exc).__name__}: {exc}"
    if "queue" not in caps:
        die(f"--comm queue (the default) needs a daemon whose courier leaves queue panes to "
            f"their own listeners (design 051), and session {env['session']!r} reports "
            f"{'no such capability' if not detail else repr(detail.splitlines()[0])}. Restart the "
            f"session on a newer rysh, or build this fleet with --comm grid. Nothing has been opened.")


def arm_codex_discovery(env: dict, pane_id: str, cwd: str, launched_at: int) -> None:
    """Ask the daemon to find the codex thread this pane just opened.

    The courier queues to a codex by its thread id (`codex queue --thread`), and
    codex picks that id itself and only records it on disk. The daemon already
    discovers it for `##codex` panes (handleDiscoverCodexSession) and stamps
    `codex.session_id`; a fleet launches through `##cmd`, so it arms the same
    discovery by hand. Best-effort: `verify` reports a pane that never got one.
    """
    nc = R.Nats(env["nats_host"], env["nats_port"])
    try:
        nc.publish(f"{env['session']}.ws.inbox",
                   R.envelope("MsgDiscoverCodexSession",
                              {"pane_id": pane_id, "cwd": cwd,
                               "not_before": launched_at - 5,
                               "retries": CODEX_DISCOVER_RETRIES}))
    finally:
        nc.close()


def apply_watch(env: dict, fleet: str, args) -> dict:
    """Set the fleet actor's check-in policy when `up` was given one (design
    054). Unset flags keep the daemon's defaults: smart, every 10m, stalled
    after 5m."""
    argv = []
    for flag, val in (("--policy", getattr(args, "watch_policy", "")),
                      ("--interval", getattr(args, "watch_interval", "")),
                      ("--stall", getattr(args, "watch_stall", ""))):
        if val:
            argv += [flag, val]
    if not argv:
        return {"policy": "default"}
    return registry(env, "watch", fleet, *argv)


def apply_goal(env: dict, fleet: str, args) -> dict:
    """Set the fleet goal and the goal executor's cadence when `up` was given
    them (design 055). Unset flags leave the daemon's defaults: no goal (the
    executor still coordinates blockers and asks for one), adaptive 5m-30m."""
    out: dict = {}
    goal = getattr(args, "goal", "") or ""
    if goal:
        argv = ["goal", "set", fleet, "--goal", goal]
        for flag, val in (("--done-when", getattr(args, "goal_done_when", "")),
                          ("--due", getattr(args, "goal_due", ""))):
            if val:
                argv += [flag, val]
        out["goal"] = registry(env, *argv)
    argv = []
    for flag, val in (("--mode", getattr(args, "goal_exec_mode", "")),
                      ("--min", getattr(args, "goal_exec_min", "")),
                      ("--max", getattr(args, "goal_exec_max", ""))):
        if val:
            argv += [flag, val]
    out["executor"] = registry(env, "goal", "exec", fleet, *argv) if argv else {"mode": "default"}
    return out


def status_section(env: dict, man: dict, p: dict) -> str:
    """The brief paragraph that binds every agent to the status contract
    (design 054, the rysh-fleet-agent skill).

    Appended to every brief, of every template, like comm_section: the fleet
    actor's ledger is only as good as the declarations it gets, and a role
    whose brief forgot the contract is the agent that goes silent."""
    b = agent_bin(env)
    sess = env.get("session") or ""
    base = f"{b} fleet status"
    tail = f"--as {p['pane']} --fleet {man['fleet']}" + (f" --session {sess}" if sess else "")
    skill = os.path.abspath(os.path.join(_HERE, "..", "..", "rysh-fleet-agent", "SKILL.md"))
    return (
        "\n\n## Your status: the fleet ledger (design 054)\n\n"
        "The daemon's fleet actor keeps a status ledger of every member and watches every "
        "pane. It compares what you DECLARE with what it SEES, and it checks in with — or "
        "escalates to your parent about — any agent that is stalled, silent, idle with "
        f"nothing to do, or unblocked. The full contract is `{skill}`; read it once.\n\n"
        "Declare with exactly these commands (the binary, ids and session are spelled out "
        "because your shell may not carry them):\n\n"
        f"```sh\n{base} progress {tail} --task '<one line: what you are doing>'\n"
        f"{base} blocked  {tail} --blocked-by <member label | human | ext:<what>> --note '<why>'\n"
        f"{base} done     {tail} --note '<absolute path / branch of the result>'\n"
        f"{base} idle     {tail}\n```\n\n"
        "- **First act of this session:** `progress` with your first task. Every new task: "
        "`progress` again.\n"
        "- **Waiting on anyone — a reply, a review, a decision, the human:** `blocked "
        "--blocked-by <who>`, then send your question as usual. Never declare `blocker_for`; "
        "the actor derives it.\n"
        "- **Finished:** `done` with where the result is, then report. **Nothing left:** "
        "`idle`. Never end a turn idle without declaring it.\n"
        "- **A `CHECK-IN` from `fleet-actor`:** answer with ONE `fleet status` command — it "
        "records your status and closes the check-in. **An `ESCALATION`** (you are a parent): "
        "act on it in the same turn.\n"
        "- **Progress toward your task:** add `--pct <0-100> --eta <20m>` to `progress` "
        "whenever you can; a new `--task` resets them. The fleet's **goal executor** (design 055) "
        "holds the fleet and you to your goals with it — see your goal: "
        f"`{b} fleet goal show {man['fleet']}`" + (f" --session {sess}" if sess else "") + ".\n"
        "- **A `GOAL HURRY` or `GOAL REALIGN` from `goal-executor`:** answer with ONE `fleet status` "
        "command (with `--eta`, or a task that serves the goal). **`GOAL WAIT`:** do not message "
        "whoever you wait on — the executor tells you when they are done; meanwhile advance what "
        "does not depend on them. **`GOAL SHIELD`:** do the one thing it names. **`SEQUENCE`:** "
        "the breaker proceeds on a stated assumption, the rest hold.\n"
        f"- See the whole fleet: `{base} {man['fleet']}`" + (f" --session {sess}" if sess else "") + ".\n")


def comm_section(env: dict, man: dict, p: dict) -> str:
    """The brief paragraph that says how messages reach this agent."""
    comm = fleet_comm(man)
    if not is_board_comm(comm):
        return ""
    b = agent_bin(env)
    return (
        "\n\n## How messages reach you: the board, never your keyboard\n\n"
        
        + (f"This fleet runs **queue messaging** (design 051). Every prompt an agent sends is "
           f"posted on the fleet board, and every agent — you too — has its own listener on "
           f"that board: yours takes the prompts addressed to you and lets the rest pass, so "
           f"a prompt sent while you are busy or restarting waits on the board for you. "
           if comm == COMM_QUEUE else
           f"This fleet runs **grid messaging** (design 050/051). Every prompt an agent sends "
           f"is a command on the fleet board, and rysh's courier pushes it straight into the "
           f"recipient's session. ")
        + "Prompts reach you as a message that starts `[rysh board <board> | command <id> | "
        "from <who>]` followed by the usual `[FLEET …]` envelope — never typed into your "
        "pane. Your keyboard is the human's: anything typed there IS the human, and is not "
        "a fleet message.\n\n"
        "- When a command arrives, **first** run the `board ack` line it ends with "
        "(that tells the sender you have it), then do the work, then answer with "
        "`fleetctl report` / `msg` as the protocol says.\n"
        f"- To see what you were asked earlier: `{b} board inbox --as {p['pane']} "
        f"--board {man['fleet']} --all`. Read it when you want context; do **not** "
        "poll, loop on, or watch the board — every command is delivered to you.\n"
        "- Never type into another agent's pane, and never use `ansa send` to reach "
        "one: `fleetctl msg`/`report` already go through the board.\n")


def start_agent(env: dict, pane: dict, prompt: str, cwd: str | None,
                agent_args: str, agent: str = "claude", fleet: str = "",
                comm: str = COMM_INPUT) -> dict:
    """Start one agent on its brief.

    For a CLI agent this launches a process with the prompt as argv — argv, not
    typed, because a prompt typed into an agent that is still booting is silently
    dropped and there is no ready signal to wait for. Both shipped CLIs take the
    prompt positionally (`claude [OPTIONS] [PROMPT]`, `codex [OPTIONS] [PROMPT]`),
    which is what makes one launcher possible.

    For a RYSH agent there is no process to start: the pane already has an agent
    — its own LLMPromptExecutionActor — and "launching" it means giving it its
    first prompt. So the brief is DELIVERED rather than exec'd, and the boot race
    the argv rule exists to avoid cannot happen: the actor was created with the
    pane and is listening before fleetctl has finished naming it.

    Returns the session id where the agent HAS one at launch time (claude), and
    `""` where it does not (codex, rysh) — an empty id is the honest answer.
    """
    if agent not in AGENTS:
        die(f"unknown --agent {agent!r}; known: {', '.join(sorted(AGENTS))}")
    path = os.path.join(R.fanout_dir(env),
                        f"fleet-{pane['id'][:8]}-{int(time.time())}.txt")
    with open(path, "w") as fh:
        fh.write(prompt)
    cd = f"cd {cwd} && " if cwd else ""

    if is_native(agent):
        return start_native_agent(env, pane, prompt, cwd, path)

    if agent == "codex":
        # No id to pin, and the trust gate to clear first — for the cwd the agent
        # will actually run in, which is the worktree when it has one.
        run_in = cwd or env.get("workspace") or os.getcwd()
        trust = codex_trust(run_in, fleet)
        launched_at = int(time.time())
        out = R.cmd_to_pane(env, pane, f'{cd}{R.CLEAN_ENV} codex {agent_args} "$(cat {path})"')
        res = {"session_id": "", "prompt_file": path, "agent": agent,
               "trusted": trust, "output": out.strip()}
        if is_board_comm(comm):
            try:
                arm_codex_discovery(env, pane["id"], os.path.abspath(run_in), launched_at)
                res["codex_discovery"] = "armed"
            except Exception as exc:
                res["codex_discovery"] = f"not armed: {exc}"
        return res

    m = R.SESSION_ID_ARG.search(agent_args)
    # The session id IS the pane id (design 2026-08-11): the fleet-stop verb
    # kills the claude process and `##ansa resume` brings it back with
    # `claude --resume <session-id>`. Resume needs the id, and the one id
    # rysh always has for a pane is the pane's own — so pinning it here means
    # resume never has to look it up, and there is one fact, not two to keep in
    # step. A caller-supplied --session-id still wins, for the rare resume.
    session_id = m.group(1) if m else pane["id"]
    # polling-stream: the --settings file joins the LAUNCH ARGS, not just this
    # one command line -- they are stamped as `agent.args`, which is what
    # `##ansa resume` relaunches with. A resumed claude without them would
    # hold every courier delivery behind an approval dialog.
    if is_board_comm(comm) and "--settings" not in agent_args:
        agent_args = f"{agent_args} --settings {shlex.quote(claude_comm_settings(env, pane))}".strip()
    args = agent_args if m else f"--session-id {session_id} {agent_args}"
    # ALWAYS START IN A KNOWN DIRECTORY (R12). claude files a session under the
    # directory it started in, and the restart sweep resumes with
    # `cd <agent.cwd> && claude --resume <id>` — so the directory has to be a
    # fact, not "wherever the pane's shell happened to be". The member's
    # worktree when it has one, the workspace otherwise.
    launch_dir = cwd or env.get("workspace")
    cd_line = f"cd {shlex.quote(launch_dir)} && " if launch_dir else ""
    out = R.cmd_to_pane(env, pane, f'{cd_line}{R.CLEAN_ENV} claude {args} "$(cat {path})"')
    return {"session_id": session_id, "prompt_file": path, "agent": agent,
            "cwd": launch_dir, "launch_args": agent_args, "output": out.strip()}


def agent_args_for(args) -> tuple[str, str]:
    """The (agent, launch args) pair a build command was asked for.

    `--claude-args` predates `--agent` and stays working as the override it
    always was; `--agent-args` is the neutral spelling. Whichever the caller
    passed wins over the agent's default — and if they passed neither, the
    DEFAULT IS PER AGENT, because codex's full-auto flag is not claude's
    (`--dangerously-bypass-approvals-and-sandbox`, not
    `--dangerously-skip-permissions`). Handing codex claude's flag is an
    immediate hard error at the pane; handing it nothing is the approval prompt
    that hangs a fleet agent forever.
    """
    agent = getattr(args, "agent", "claude") or "claude"
    if agent not in AGENTS:
        die(f"unknown --agent {agent!r}; known: {', '.join(sorted(AGENTS))}")
    explicit = (getattr(args, "agent_args", "") or "").strip()
    claude_args = (getattr(args, "claude_args", "") or "").strip()
    if not explicit and claude_args and claude_args != AGENTS["claude"]["default_args"]:
        explicit = claude_args           # a deliberate override, whatever the agent
    if is_native(agent):
        # A native agent has NO COMMAND LINE. Silently accepting flags for one
        # would stamp `agent.args` on the pane and put a permission-mode string
        # in a receipt for a launch that never had one — a fact that is not a
        # fact. Refuse the flag rather than ignore it: an ignored flag is how a
        # fleet runs in a mode nobody chose.
        if explicit:
            die(f"--agent {agent} takes no launch args ({explicit!r}): a rysh agent is not a "
                f"process and has no command line. Its permissions come from the pane's policy "
                f"and approval settings.")
        return agent, ""
    return agent, explicit or AGENTS[agent]["default_args"]


def stamp_launch(env: dict, pane_id: str, res: dict, agent: str,
                 agent_args: str, task: str = "") -> None:
    """Record on the PANE what `##ansa resume` needs to bring this agent back.

    One function for both launch sites deliberately. The defect this whole
    invariant exists to prevent (f8824f5) was a stamp present at the roster site
    and MISSING at the subfleet site, so a killed subfleet worker came back in
    the wrong permission mode. Two call sites of one function cannot drift the
    way two copies of four lines did.

    What resume reads:
      * `fleet.agent`  — WHICH CLI, so it picks `claude --resume <id>` or
        `codex resume --last`. Without it a codex pane resumes by trying to run
        a claude, which is not a wrong permission mode but a wrong PROGRAM.
      * `agent.args`   — the launch args, so the resumed agent has the same
        permission mode it was started with.
      * `claude.session_id` — only where the agent has an id at launch. Codex
        has none (§3 of the handoff); an empty stamp would be a fact that isn't.

    `claude.args` is written alongside `agent.args` for one release: a pane
    launched by this fleetctl must still resume correctly under a rysh-cli built
    before the rename.
    """
    if res.get("auto_approve"):
        R.set_meta(env, {"id": pane_id}, META_AUTO_APPROVE, "true")
    if res.get("session_id"):
        R.set_meta(env, {"id": pane_id}, R.META_SESSION, res["session_id"])
    if task:
        R.set_meta(env, {"id": pane_id}, R.META_TASK, task)
    R.set_meta(env, {"id": pane_id}, META_AGENT, agent)
    if agent_args.strip():
        R.set_meta(env, {"id": pane_id}, META_AGENT_ARGS, agent_args.strip())
        R.set_meta(env, {"id": pane_id}, "claude.args", agent_args.strip())
    arm_restart_sweep(env, pane_id, agent, res.get("cwd"))


# The keys rysh-cli's startup sweep reads (nativeResumePlan, design 029 §7),
# spelled and meant exactly as stampNativeAgentMeta writes them.
META_AGENT_NATIVE = "agent.native"   # presence arms the sweep; value "1"
META_AGENT_KIND = "agent.kind"       # "claude" (codex is not armed — see below)
META_AGENT_CWD = "agent.cwd"         # the directory the agent STARTED in


def arm_restart_sweep(env: dict, pane_id: str, agent: str, cwd: str | None) -> None:
    """Make a daemon restart resume this agent from its pinned session (R12).

    Only claude is armed, and only with a known start directory. A fleet codex
    has no session id the sweep can resume — fleetctl types it, and only
    `##codex` discovers one — so arming it would start a FRESH, briefless codex
    on every restart; and a rysh agent is not a process. Nothing is cleared for
    them: every fleet pane is created fresh by `up`/`subfleet`, so none carries
    an earlier stamp. The arming key goes LAST: a pane whose stamp is
    half-written must never be armed, or the sweep would resume it with no
    kind or no cwd.
    """
    if agent != "claude" or not cwd:
        return
    R.set_meta(env, {"id": pane_id}, META_AGENT_KIND, "claude")
    R.set_meta(env, {"id": pane_id}, META_AGENT_CWD, cwd)
    R.set_meta(env, {"id": pane_id}, META_AGENT_NATIVE, "1")


def transcript_cwd(path: str) -> str | None:
    """The directory a claude session started in, from its own transcript —
    the authoritative answer for a pane launched before fleetctl recorded it."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh):
                if i > 200:
                    break
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict) and rec.get("cwd"):
                    return rec["cwd"]
    except OSError:
        return None
    return None


def op_stamp_resume(env: dict, apply: bool = False) -> dict:
    """One-shot backfill (R12): arm the restart sweep on fleet panes launched
    before fleetctl stamped them. DRY RUN unless `apply`.

    Arms only a fleet pane whose CLAUDE IS RUNNING NOW (an exited or killed
    agent must stay stopped), which is not stamped yet, and whose session
    transcript says where it started. Writes kind and cwd, then the arming key.
    """
    rows = []
    for p in R.list_panes(env):
        meta = p.get("meta") or {}
        if not meta.get(META_FLEET):
            continue
        agent = meta.get(META_AGENT) or "claude"
        row = {"pane": p["id"], "label": meta.get(META_LABEL) or p.get("name"),
               "fleet": meta[META_FLEET], "agent": agent}
        if agent != "claude":
            row.update(action="skip", reason=f"{agent}: no pinned session the sweep can resume")
        elif meta.get(META_AGENT_NATIVE):
            row.update(action="already-stamped")
        elif not (p.get("program") or "").strip():
            row.update(action="skip", reason="agent not running (exited or stopped); "
                                            "arming it would resurrect a stopped agent")
        else:
            sid = meta.get("claude.session_id") or p["id"]
            path = R.transcript_path(sid)
            cwd = transcript_cwd(path) if path else None
            if not path:
                row.update(action="skip", reason=f"no transcript for session {sid}")
            elif not cwd:
                row.update(action="skip", reason=f"transcript {path} records no cwd")
            else:
                row.update(action="stamp", session_id=sid, cwd=cwd)
                if apply:
                    set_meta(env, p["id"], META_AGENT_KIND, "claude")
                    set_meta(env, p["id"], META_AGENT_CWD, cwd)
                    set_meta(env, p["id"], META_AGENT_NATIVE, "1")
        rows.append(row)
    counts: dict = {}
    for r in rows:
        counts[r["action"]] = counts.get(r["action"], 0) + 1
    return {"dry_run": not apply, "panes": rows, "summary": counts,
            "apply_with": "fleetctl stamp-resume --apply"}


# --------------------------------------------------------------------------
# Worktrees
# --------------------------------------------------------------------------

def git(env: dict, *a: str, cwd: str | None = None) -> subprocess.CompletedProcess:
    """Run git in `cwd`, defaulting to the main checkout.

    Worktree work must pass its own path. Defaulting everything to the workspace
    is what made agents write their roadmap into the SHARED main checkout just to
    get `commit` to find it — which is exactly the isolation the worktree exists
    to provide.
    """
    return subprocess.run(["git", "-C", cwd or env["workspace"], *a],
                          capture_output=True, text=True)


def workspace_relative(env: dict, path: str, what: str) -> str:
    """A path inside the workspace, expressed RELATIVE to it.

    An agent's roadmap path is joined onto its worktree root to find its own
    file — and `os.path.join(root, <absolute>)` returns the absolute path,
    dropping the root on the floor. So one absolute `--roadmap-dir` sent every
    agent's path-limited commit into the SHARED main checkout while its own
    branch never moved, and left the agent's real work sitting untracked in the
    worktree: F-28, and the exact isolation the worktree exists to provide.

    Normalised ONCE, here, rather than at each join. Two representations of one
    path that must be kept in step is how F-23 happened; the callers below take
    a relative path because that is the only kind that composes.

    A path outside the workspace is REFUSED rather than relativised into
    `../..`, because no agent worktree can hold it and guessing which tree was
    meant is how this defect read as working in the first place.
    """
    if not os.path.isabs(path):
        return os.path.normpath(path)
    rel = os.path.relpath(path, env["workspace"])
    if rel == os.pardir or rel.startswith(os.pardir + os.sep):
        die(f"{what} {path!r} is outside the workspace {env['workspace']!r}, "
            f"so no agent worktree can hold it")
    return rel


def make_worktree(env: dict, name: str, base: str) -> dict:
    """`worktrees/<name>` off the repo root — never `.claude/worktrees/`.

    That other path silently occupies the branch name and blocks the main
    checkout from ever using it.
    """
    rel = os.path.join("worktrees", name)
    path = os.path.join(env["workspace"], rel)
    if os.path.isdir(path):
        return {"path": path, "rel": rel, "branch": name, "created": False}
    r = git(env, "worktree", "add", "-b", name, rel, base)
    if r.returncode != 0 and "already exists" in (r.stderr or ""):
        r = git(env, "worktree", "add", rel, name)
    if r.returncode != 0:
        return {"path": None, "rel": rel, "branch": name, "created": False,
                "error": (r.stderr or r.stdout).strip()}
    return {"path": path, "rel": rel, "branch": name, "created": True}


# --------------------------------------------------------------------------
# The message envelope
# --------------------------------------------------------------------------

def short(pane_id: str | None) -> str:
    return pane_id[:8] if pane_id else "keyboard"


def envelope_line(man: dict, sender: dict, recipient: dict, kind: str,
                  msg_id: str) -> str:
    """The stamp on the front of every delivered message.

    IT NAMES THE SENDER'S WORKTREE (`F-33`). Every agent works in its own, so a
    relative path in a report — `out/note-1.md` — names one file per agent, and
    the workspace copy a human opens is usually empty. The protocol tells agents
    to cite absolute paths; this is the half that does not depend on them
    remembering, because the recipient can resolve any relative path the sender
    used against the tree the sender was standing in.

    Omitted entirely when the sender has no worktree — the human, or a fleet
    built without `--worktrees` — rather than printed as an empty field: a blank
    where a path belongs reads as "the workspace", which is the very confusion
    this exists to remove.
    """
    wt = sender.get("worktree") or ""
    at = f" at {wt}" if wt else ""
    return (f"[FLEET {man['fleet']} | {kind} | FROM {sender['role']} "
            f"{sender['label']} (unit {sender.get('unit', '-')}, pane "
            f"{short(sender['pane'])}{at}) | TO {recipient['role']} "
            f"{recipient['label']} (pane {short(recipient['pane'])}) | {msg_id}]")


def next_msg_id(man: dict) -> str:
    man["msg_seq"] = man.get("msg_seq", 0) + 1
    return f"msg-{man['msg_seq']:04d}"


def deliver(env: dict, man: dict, sender: dict, recipient: dict, body: str,
            kind: str, body_file: str | None) -> dict:
    """Send one message, envelope first.

    A `##` command line is tokenised on whitespace and re-joined with single
    spaces, and newlines cannot survive it at all — so anything with structure
    goes in a FILE and the typed line points at it. The envelope always travels
    inline, because a recipient must be able to see who is talking without
    opening anything.
    """
    # Allocate the id under the lock, on a FRESH read. Two short critical
    # sections rather than one long one: the send happens between them, because
    # holding a file lock across a network delivery would serialise the fleet on
    # its slowest recipient.
    with manifest_txn(env, man["fleet"]) as fresh:
        msg_id = next_msg_id(fresh)
        man["msg_seq"] = fresh["msg_seq"]  # keep the caller's view current
    env_line = envelope_line(man, sender, recipient, kind, msg_id)
    if body_file:
        line = (f"{env_line} {kind.title()} from your {sender['role']} "
                f"{sender['label']}. Read {body_file} in full and act on it. "
                f"Reply per the fleet protocol: open your answer with your own "
                f"envelope line naming yourself.")
    else:
        flat = " ".join(body.split())
        line = (f"{env_line} {flat} -- reply opening with your own envelope line "
                f"naming yourself.")
    if is_board_comm(fleet_comm(man)):
        out = board_send(env, man, sender, recipient, line, msg_id, body_file)
    else:
        out = fleet_send(env, recipient, line)
    rec = {"msg_id": msg_id, "kind": kind, "from": sender["label"],
           "to": recipient["label"], "body_file": body_file,
           "submitted": bool(out.get("ok", True)), "at": int(time.time()),
           # `via` records WHICH PATH CARRIED THIS, and it survives the W4-3b
           # cutover to a single path on purpose: it is the only way to answer
           # *how was this delivered* after the fact, and it is the exact field
           # that caught the live binary lacking `ansa` while every message was
           # silently falling back. A forensic field earns its keep only if it
           # cannot be wrong, so an unreported path is recorded as UNKNOWN --
           # never defaulted to a name. The default here used to be "type", the
           # typing path deleted at W4-3b: a sender that reported nothing was
           # credited with a mechanism that no longer exists, which is worse
           # than a blank, because the next reader trusts it.
           "via": out.get("via") or "unknown"}
    # Append under the lock, again on a fresh read, so a concurrent sender's
    # record cannot be erased by writing back a copy that predates it.
    with manifest_txn(env, man["fleet"]) as fresh:
        fresh.setdefault("log", []).append(rec)
        man["log"] = fresh["log"]
        man["msg_seq"] = fresh.get("msg_seq", man.get("msg_seq"))
    # Mirror to the agents board (design 025 §4.2). LAST, and best-effort: the
    # message is already delivered and logged by this point, so nothing below
    # can cost us the control channel. See board_mirror's own docstring.
    # Not under polling-stream: there the message IS a board post already, and
    # a mirror would put every order on the board twice.
    if not is_board_comm(fleet_comm(man)):
        board_mirror(env, man, sender, recipient, rec, body, body_file)
    return {**rec, "envelope": env_line, "send": out}


# --------------------------------------------------------------------------
# Delivery: ANSA (design 026 §6) or the legacy typing path
# --------------------------------------------------------------------------
#
# W4 step 1. OFF by default: `RYSH_FLEET_ANSA=1` opts in. The typing path stays
# the default and untouched until step 2 has been proven in an ISOLATED session
# and step 3 is merged by the CEO -- fleetctl is invoked from the main checkout
# by every live fleet on this machine, so a bad cutover costs everyone the
# ability to say so.
#
# THE ONE RULE: a work order must never vanish. A board post is worth less than
# the control channel; a work order is worth more than either. So ANSA delivery
# either lands, or falls back to typing, or raises -- and it records WHICH, so a
# silent success can never be mistaken for a real one. This fleet lost three
# work orders to a confident `ok: True`; the receipt is the dangerous part, not
# the failure.

ANSA_TIMEOUT = 20.0


# W4-3b: the typing path is GONE. There is no ansa_enabled() any more -- a flag
# with one legal value is a decision nobody made, and leaving it would invite
# someone to set it back to a path that no longer exists.


_ansa_capable: bool | None = None
_ansa_probe_detail: str = ""

# The ANSA router's own verdict codes (msg/messages_ansa.go, AnsaErr*). The
# daemon renders a refusal as `[<code>] <prose>`, so one of these appearing in
# the probe's output is PROOF THE ROUTE RAN — it can only have been written by a
# daemon that has the ANSA handler compiled in. That is the whole discriminator.
ANSA_ROUTE_CODES = ("no_target", "no_text", "unknown_target", "ambiguous_target",
                    "unreachable", "bad_mode", "publish_failed", "not_an_id",
                    "directory_unavailable")


def ansa_capable(env: dict) -> bool:
    """Does the RUNNING SESSION actually route ANSA?

    A DEPLOYMENT prerequisite, not a code one, and it is the thing that stopped
    this cutover: `macmini` had ANSA merged while the running sidecar was built
    before it existed, so every message since the flip quietly took the
    fallback. Source-merged is not deployed.

    E9 Q2, GAP A — WHAT THIS USED TO MEASURE. The old probe ran `<bin> ansa` and
    asked whether the string "unknown command" came back, i.e. *does this file on
    disk have an `ansa` subcommand*. That is a different question from the one the
    function is named for, and the difference is the entire hazard: a session is a
    long-lived process, so a fresh binary on disk says nothing about the daemon
    already in memory. New binary + old daemon passed the old check and then
    failed EVERY send. The check existed; it measured the wrong thing.

    SO THE PROBE IS A ROUND TRIP. It asks the live session to route to a sentinel
    target that cannot exist, and reads the answer:

      - a live daemon refuses it with one of its own ANSA codes
        (`[unknown_target] no pane matches ...`) — the route ran, so ANSA is live;
      - an old binary answers "unknown command" locally, never reaching a daemon;
      - an old daemon has no handler for MsgCLIAnsaSend, drops the request, and
        the CLI's request/reply dies at the transport with no verdict at all.

    Only the first is capable, and NOTHING IS EVER DELIVERED by the probe: the
    target is refused before any pane is written to. Fail-closed by construction
    — anything that is not a routing verdict counts as not capable, because for a
    prerequisite whose failure mode is silence, guessing "probably fine" is the
    error that cost this fleet the cutover.

    Probed once per process. The answer can change under us in principle (a
    session restart), but not within one fleetctl invocation.
    """
    global _ansa_capable, _ansa_probe_detail
    if _ansa_capable is None:
        # An id-shaped sentinel: pane ids are uuids and given-names need a
        # leading `@`, so this can match neither, and the pid keeps two
        # concurrent probes from ever being confused for each other.
        target = f"fleetctl-ansa-probe-{os.getpid()}"
        argv = [env["bin"], "ansa", "send", target,
                "--session", env["session"], "--", "ansa capability probe"]
        try:
            proc = subprocess.run(argv, cwd=env.get("workspace") or None,
                                  capture_output=True, text=True, timeout=15)
            output = (proc.stdout or "") + (proc.stderr or "")
            _ansa_probe_detail = output.strip()[:400]
            # rc 0 would mean the sentinel somehow resolved — impossible in
            # practice, but it is still a routing verdict, so it still counts.
            _ansa_capable = proc.returncode == 0 or any(
                f"[{code}]" in output for code in ANSA_ROUTE_CODES)
        except Exception as exc:
            _ansa_probe_detail = f"{type(exc).__name__}: {exc}"
            _ansa_capable = False
    return _ansa_capable


def require_ansa_capable(env: dict) -> None:
    """Refuse clearly, rather than failing obscurely per message.

    Without this, deploying 3b onto an old binary produces a delivery failure on
    every send and leaves the reader to work out why from a router error. Naming
    the cause once, up front, is the difference between an outage you can fix
    and one you have to diagnose.

    E9 Q2, GAP B — WHERE THIS IS CALLED FROM. `fleet_send` was the only caller,
    so the refusal arrived at the FIRST MESSAGE: after `up` had opened a tab,
    a lane per role, a pane per unit, cut a worktree each and written a brief
    each. The check was correct and landed after the entire cost, leaving a
    standing fleet nobody could reach and a manual teardown. `op_up` now calls
    this before it opens anything, which is the only point at which refusing is
    still free.

    The message names the BINARY and the SESSION because after GAP A the cause
    may be either, and they have different fixes: replace the file, or restart
    the session.
    """
    if not ansa_capable(env):
        detail = f" The probe answered: {_ansa_probe_detail!r}." if _ansa_probe_detail else ""
        die(f"fleet delivery requires a live ANSA route, and session "
            f"{env['session']!r} did not answer one: running the `ansa` subcommand "
            f"of {env['bin']!r} against it produced no routing verdict.{detail} "
            f"Either the binary predates ANSA, or — the case a check on the binary "
            f"alone cannot see — the RUNNING DAEMON does, since a session started "
            f"before ANSA landed keeps its old daemon in memory however new the "
            f"file on disk is. Since W4-3b there is no typing fallback: restart the "
            f"session on a binary built from macmini at or after ANSA landed. "
            f"Nothing has been delivered and no panes were opened.")


def ansa_send(env: dict, recipient: dict, line: str) -> dict:
    """Deliver one line through ANSA. Returns a result dict, never raises.

    Addresses by PANE ID, never by name: given-names are unique per LANE, not
    per session, so a name is a label and an id is an address (design 026 §5.1).
    """
    argv = [env["bin"], "ansa", "send", recipient["pane"],
            "--session", env["session"], "--", line]
    try:
        proc = subprocess.run(argv, cwd=env.get("workspace") or None,
                              capture_output=True, text=True, timeout=ANSA_TIMEOUT)
    except Exception as exc:
        return {"ok": False, "via": "ansa", "error": f"{type(exc).__name__}: {exc}"}
    output = (proc.stdout or "") + (proc.stderr or "")
    return {"ok": proc.returncode == 0, "via": "ansa",
            "rc": proc.returncode, "output": output.strip()}


def fleet_send(env: dict, recipient: dict, line: str) -> dict:
    """Deliver one message, by whichever path is enabled.

    ANSA only since W4-3b. `via` STAYS in the manifest even though "ansa" is now
    the only possible value: after the cutover it is the sole way to answer *how
    was this delivered* after the fact, and it is the exact field that caught the
    live binary lacking `ansa` when every message was silently falling back.
    The moment to lose an audit trail is never, and least of all the moment it
    stops being redundant.
    """
    require_ansa_capable(env)

    res = ansa_send(env, recipient, line)
    if res.get("ok"):
        return res

    # There is NO second path any more. A work order that cannot be delivered
    # must not return a receipt -- that failure cost this fleet three orders.
    die(f"DELIVERY FAILED for {recipient['label']}: "
        f"ansa={res.get('error') or res.get('output')!r}. There is no typing "
        f"fallback since W4-3b; nothing was delivered.")


BOARD_SEND_TIMEOUT = 20.0


def board_send(env: dict, man: dict, sender: dict, recipient: dict, line: str,
               msg_id: str, body_file: str | None) -> dict:
    """Deliver one message under polling-stream: a COMMAND on the fleet board.

    The fleet's msg id IS the command id, so the manifest log, the envelope and
    the board thread all name the same thing. The courier in the daemon does
    the delivery; this call only has to get the command onto the board, and it
    dies -- never returns a receipt -- when it cannot, the same rule as
    fleet_send: a work order that silently vanished cost this fleet three.

    The human has no pane of their own in the roster, so a human-sent message
    is sent from the pane fleetctl is running in ($RYSH_PANE).
    """
    as_pane = sender.get("pane") or os.environ.get("RYSH_PANE", "").strip()
    if not as_pane:
        die("polling-stream sends from a pane: run this inside a rysh pane (so "
            "$RYSH_PANE is set) or pass --as <member>")
    if not recipient.get("pane"):
        die(f"{recipient['label']} has no pane to deliver to. Under polling-stream "
            f"the human reads the board itself: `{agent_bin(env)} board tail --board "
            f"{man['fleet']}`")
    argv = [env["bin"], "board", "send", "--as", as_pane, "--to", recipient["pane"],
            "--board", man["fleet"], "--id", msg_id, "--session", env["session"]]
    if body_file:
        argv += ["--file", body_file]
    argv += ["--", line]
    try:
        proc = subprocess.run(argv, cwd=env.get("workspace") or None,
                              capture_output=True, text=True, timeout=BOARD_SEND_TIMEOUT)
    except Exception as exc:
        die(f"DELIVERY FAILED for {recipient['label']}: board send: "
            f"{type(exc).__name__}: {exc}. Nothing was delivered.")
    output = ((proc.stdout or "") + (proc.stderr or "")).strip()
    if proc.returncode != 0:
        die(f"DELIVERY FAILED for {recipient['label']}: board send: {output!r}. "
            f"Nothing was delivered.")
    return {"ok": True, "via": "board", "rc": 0, "output": output}


# --------------------------------------------------------------------------
# agents-board mirror (design 025 §4.2)
# --------------------------------------------------------------------------

# The board is a monitoring view. The fleet's ability to talk to itself is not.
# If those two ever come into conflict, the board loses — every failure mode
# below is swallowed, and `deliver()` returns exactly what it returned before
# this hook existed.
BOARD_MIRROR_TIMEOUT = 2.0  # seconds; a wedged daemon must not stall delivery


def board_mirror_enabled() -> bool:
    """OFF by default. `RYSH_FLEET_BOARD=1` opts in.

    Default-off is a deliberate reading of the founder's working default for
    "what counts as a milestone": *the agent decides, and there is an explicit
    call it makes — posting is something an agent does deliberately, not
    something inferred from its output.* An automatic publish inside deliver()
    would make EVERY fleet message a post, which is the opposite of that.

    So the mirror ships built and tested but dormant: if the founder later
    prefers an auto-fed board, it is one environment variable, not a patch.
    The switch also serves the operational case — this function runs inside the
    path every `msg`, `report` and `broadcast` in every live fleet on this
    machine goes through, so turning it off must never require editing code.
    """
    return os.environ.get("RYSH_FLEET_BOARD", "0") in ("1", "true", "yes")


def board_post_argv(env: dict, as_pane: str, kind: str, text: str,
                    thread: str | None) -> list[str]:
    """Build the `rysh` invocation that posts to the board.

    Uses `rysh board post`, the AGENT door, not `rysh exec -- '##board post'`.

    F-21: the `##` form is the HUMAN door. It routes through runRyshCommand,
    which echoes the command line into a pane's output buffers -- and with no
    `--pane-id` the target is the workspace's AMBIENT active pane, so every
    mirrored fleet message printed into whichever pane a human happened to be
    looking at. (Focus is NOT stolen: `focusPaneByID` is only reached on the
    --pane-id/--tab-id branches, which this never supplied. I reported focus
    theft too and was wrong about that half.)

    `rysh board post --as <pane-id>` exists precisely to avoid that door: it
    sends MsgCLIBoardPost, which never focuses and never echoes. The irony worth
    keeping is that the mirror was already careful about attribution -- it
    passed `--as` -- and inherited the echo anyway by taking the wrong route.
    """
    argv = [env["bin"], "board", "post", "--as", as_pane, "--kind", kind,
            "--session", env["session"]]
    if thread:
        argv += ["--thread", thread]
    argv += ["--", text]
    return argv


def board_mirror(env: dict, man: dict, sender: dict, recipient: dict,
                 rec: dict, body: str, body_file: str | None) -> None:
    """Mirror one delivered fleet message onto the agents board.

    This is the hook that makes posting free rather than merely cheap: the
    fleet's log record is already board-shaped (msg_id / kind / from / to /
    body_file / at), so every existing `msg`, `report` and `broadcast` call site
    becomes a board post without changing any of them. It is OFF by default --
    see board_mirror_enabled.

    GATE 4: the fleet envelope does not enter the board, and must not be
    smuggled back in through the post text either. What goes across is the
    speaker (--as, a full pane uuid) and what they said. Not who it was
    addressed to, not the msg id, not the FROM/TO line.

    NOTHING here may raise, and nothing here may block for long. A board post is
    worth strictly less than the fleet's control channel, so every failure —
    no daemon, no `##board` verb (it is newer than this function), a timeout, a
    malformed manifest — is swallowed silently. The caller has already delivered
    the message and saved the manifest before we are called.
    """
    if not board_mirror_enabled():
        return
    try:
        text = body_file if (body_file and not body) else (body or body_file or "")
        text = " ".join(str(text).split())
        if not text:
            text = f"{rec['kind']} {rec['msg_id']}"
        argv = board_post_argv(
            env, as_pane=sender["pane"], kind=rec["kind"],
            text=text, thread=None)
        subprocess.run(argv, cwd=env.get("workspace") or None,
                       capture_output=True, text=True,
                       timeout=BOARD_MIRROR_TIMEOUT)
    except Exception:
        # Intentionally total. See the docstring: there is no board failure
        # worth failing a fleet message over, and no useful place to report it
        # from inside a delivery that has already succeeded.
        return


# --------------------------------------------------------------------------
# Roster helpers
# --------------------------------------------------------------------------

def roster(man: dict) -> list[dict]:
    """Every member of the fleet, in chain order.

    `members` is the authority since `E-42`. THE LEGACY BRANCH IS NOT DEAD CODE:
    manifests written before this change are on disk right now, with `ceo` as an
    object and the tiers nested under `units`, and a fleet that has been running
    for a day must not stop being addressable because the tool learned a new
    shape. Reading both and writing one is what makes that upgrade invisible.
    """
    # THE KEY'S PRESENCE marks the new shape, not its truthiness. `members: []`
    # is a legitimate state — a fleet whose panes have all closed, or one being
    # built — and testing for a non-empty list sent it down the legacy branch,
    # where it died on a `ceo` key the new shape does not have.
    if "members" in man:
        return man["members"]
    out = [man["ceo"]]
    for u in man.get("units", []):
        out.append(u["manager"])
        out.extend(u["workers"])
    return out


def members_by_role(man: dict, role: str) -> list[dict]:
    return [p for p in roster(man) if p.get("role") == role]


def children_of(man: dict, member: dict) -> list[dict]:
    return [p for p in roster(man) if p.get("parent") == member["pane"]]


def unit_meta(man: dict, unit) -> dict:
    """The unit of work a member is assigned to: its title and source docs.

    Unit metadata lives in `man["units"]` and the ROSTER lives in
    `man["members"]`; before `E-42` they were the same object, which is why a
    fourth role was a schema change rather than a list entry.
    """
    for u in man.get("units", []):
        if str(u.get("unit")) == str(unit):
            return u
    return {"unit": unit, "title": "", "sources": []}


HUMAN = {"role": "human", "label": "human", "unit": "-", "pane": None,
         "parent": None}


def resolve(man: dict, ref: str) -> dict:
    """Resolve a role/unit/name/pane-id to one fleet member.

    `human` is a synthetic member with no pane. Without it the only way to relay
    an instruction from the keyboard is to forge it as the CEO, and the envelope
    then reads FROM ceo TO ceo — which tells the recipient the opposite of the
    truth about where its orders came from.
    """
    if ref in ("human", "founder", "user"):
        return dict(HUMAN)
    people = roster(man)
    for p in people:
        if ref in (p["pane"], p["label"]):
            return p

    # A bare role name addresses the one member holding it — `ceo`, `roadmap`,
    # `rm`. It is REFUSED when the role has several holders rather than
    # returning the first: on this tool an ambiguous address delivers a work
    # order to the wrong agent, and refusing is the rule the whole stack uses
    # (design 026 §5.1).
    role = ROLE_ALIASES.get(ref.lower())
    if role:
        holders = members_by_role(man, role)
        if len(holders) == 1:
            return holders[0]
        if len(holders) > 1:
            die(f"{ref!r} is ambiguous in fleet {man['fleet']!r}: "
                + ", ".join(h["label"] for h in holders))

    # NON-GREEDY on the role and DIGITS for the unit. A greedy `[a-z-]+` eats
    # the separator too — "mgr-01" resolves the role as "mgr-", which is in no
    # alias table, and the address silently falls through to "matches nobody".
    m = re.fullmatch(r"([a-z][a-z-]*?)[:/-]?(\d+)", ref, re.I)
    if m and ROLE_ALIASES.get(m.group(1).lower()):
        role = ROLE_ALIASES[m.group(1).lower()]
        want = m.group(2).lstrip("0") or "0"
        for p in people:
            if p["role"] == role and str(p.get("unit", "")).lstrip("0") == want:
                return p
    for p in people:
        if p["pane"].startswith(ref):
            return p
    die(f"{ref!r} matches nobody in fleet {man['fleet']!r}. `fleetctl tree` lists everyone.")


def me(env: dict, man: dict) -> dict:
    """Which fleet member is calling? A pane exports $RYSH_PANE."""
    pid = os.environ.get("RYSH_PANE")
    if pid:
        for p in roster(man):
            if p["pane"] == pid:
                return p
    sp = R.self_pane(R.list_panes(env))
    if sp:
        for p in roster(man):
            if p["pane"] == sp["id"]:
                return p
    die("this pane is not a member of that fleet — pass --as <role|unit|pane>")


def parent_of(man: dict, member: dict) -> dict:
    if not member.get("parent"):
        die(f"{member['label']} is at the head of this fleet's chain; "
            f"it reports to the human, not upward")
    return resolve(man, member["parent"])



# --------------------------------------------------------------------------
# Role templates (design 028 §6.7, `E-42`) — THE SHAPE OF A FLEET IS DATA.
#
# It used to be structure: the manifest had `ceo` as an object and each unit had
# `manager` + `workers`, so a fourth role was a schema change and a different
# org chart was a fork of this file. A fleet is now a LIST OF ROLE SPECS and the
# roster is a flat `members` list, which is what lets an epic fleet — roadmap
# manager, CEO, fleet manager, workers — exist without touching the three-tier
# one.
#
# Each spec is:
#   role   the value written to `fleet.role` pane meta; free-form to ANSA
#   brief  the references/<brief>.md an agent of this role is started with
#   parent the role it reports to, or None for the top of the chain
#   scope  "fleet" (one for the whole fleet) or "unit" (one per unit of work)
#   count  how many per scope; "workers" means --workers N
#   label  the given-name pattern; {fleet} {unit} {slug} {n}
#
# ORDER IS THE CHAIN. Specs are listed top-down, and `msg` refuses to skip a
# link by walking `parent`, so getting the order right is getting the org chart
# right.
TEMPLATES = {
    "three-tier": [
        {"role": "ceo", "brief": "brief-ceo", "parent": None,
         "scope": "fleet", "count": 1, "label": "{fleet}-ceo"},
        {"role": "manager", "brief": "brief-manager", "parent": "ceo",
         "scope": "unit", "count": 1, "label": "mgr-{unit}-{slug}"},
        {"role": "worker", "brief": "brief-worker", "parent": "manager",
         "scope": "unit", "count": "workers", "label": "wkr-{unit}-{slug}{n}"},
    ],
    # One fleet per epic (design 028 §6.7). The roadmap manager is not
    # decoration: it is the single member allowed to write new_roadmap/, which
    # is the answer to a shared file dirtied by N fleets at once — the most
    # reliable way this org chart has found to lose a day's work.
    "epic": [
        {"role": "roadmap-manager", "brief": "brief-roadmap-manager", "parent": None,
         "scope": "fleet", "count": 1, "label": "{fleet}-roadmap"},
        {"role": "ceo", "brief": "brief-ceo", "parent": "roadmap-manager",
         "scope": "fleet", "count": 1, "label": "{fleet}-ceo"},
        {"role": "fleet-manager", "brief": "brief-fleet-manager", "parent": "ceo",
         "scope": "unit", "count": 1, "label": "fmgr-{unit}-{slug}"},
        {"role": "worker", "brief": "brief-worker", "parent": "fleet-manager",
         "scope": "unit", "count": "workers", "label": "wkr-{unit}-{slug}{n}"},
    ],
    # The flat epic shape, 2026-08-12: roadmap lead -> fleet manager -> 2 workers
    # + 1 misc. NO CEO — the roadmap lead owns the record AND the order of work,
    # and hands orders straight to the fleet manager. Five agents per fleet
    # instead of `epic`'s four, but one fewer link in the chain, so an order
    # reaches a worker in two hops rather than three.
    #
    # The briefs are FORKS, not the `epic` ones reused: brief-ceo and
    # brief-fleet-manager both name a CEO in the first paragraph, and a role that
    # does not exist is a false fact in the prompt an agent trusts most. Same
    # reason 028 gave for telling a codex it is a claude.
    "epic-flat": [
        {"role": "roadmap-manager", "brief": "brief-roadmap-lead", "parent": None,
         "scope": "fleet", "count": 1, "label": "{fleet}-roadmap"},
        {"role": "fleet-manager", "brief": "brief-fleet-manager-flat",
         "parent": "roadmap-manager",
         "scope": "unit", "count": 1, "label": "fmgr-{unit}-{slug}"},
        {"role": "worker", "brief": "brief-worker-flat", "parent": "fleet-manager",
         "scope": "unit", "count": "workers", "label": "wkr-{unit}-{slug}{n}"},
        {"role": "misc", "brief": "brief-misc", "parent": "fleet-manager",
         "scope": "unit", "count": 1, "label": "misc-{unit}-{slug}"},
    ],
    # The four-agent epic shape, 2026-09-24 (founder order): roadmap agent ->
    # fleet manager -> 2 workers. No CEO and NO MISC — `epic-flat` minus the
    # misc agent. The manager and worker briefs are FORKS for that reason and
    # not the flat ones reused: both flat briefs name the misc agent, and a role
    # that does not exist is a false fact in the prompt an agent trusts most.
    # brief-roadmap-lead names no role that is missing here, so it is shared.
    "epic-quad": [
        {"role": "roadmap-manager", "brief": "brief-roadmap-lead", "parent": None,
         "scope": "fleet", "count": 1, "label": "{fleet}-roadmap"},
        {"role": "fleet-manager", "brief": "brief-fleet-manager-quad",
         "parent": "roadmap-manager",
         "scope": "unit", "count": 1, "label": "fmgr-{unit}-{slug}"},
        {"role": "worker", "brief": "brief-worker-quad", "parent": "fleet-manager",
         "scope": "unit", "count": "workers", "label": "wkr-{unit}-{slug}{n}"},
    ],
    # The two-agent epic shape, 2026-08-14: roadmap agent -> 1 worker. No CEO,
    # no fleet manager, no misc — the roadmap agent owns the record AND the
    # order of work and hands orders straight to its single worker. One hop.
    # The briefs are FORKS of the flat pair (same rule as epic-flat): the flat
    # worker brief names a fleet manager and sibling workers, roles that do not
    # exist here, and a false fact in the first prompt is the one an agent
    # trusts most.
    # One lead over a whole roadmap, one worker per epic (2026-09-28, founder
    # request): "a fleet for each item in new_roadmap, one single worker in the
    # stacked pane for each epic". duo has that shape but its briefs say "exactly
    # two agents, one epic, no sibling worker" — false with N workers — so both
    # briefs are forks, not reuses. With the default --web-sync root the workers
    # are sync-paused and the lead is the pane the human watches.
    "portfolio": [
        {"role": "portfolio-lead", "brief": "brief-portfolio-lead", "parent": None,
         "scope": "fleet", "count": 1, "label": "{fleet}-lead"},
        {"role": "worker", "brief": "brief-worker-portfolio", "parent": "portfolio-lead",
         "scope": "unit", "count": "workers", "label": "wkr-{unit}-{slug}{n}"},
    ],
    "duo": [
        {"role": "roadmap-manager", "brief": "brief-roadmap-duo", "parent": None,
         "scope": "fleet", "count": 1, "label": "{fleet}-roadmap"},
        {"role": "worker", "brief": "brief-worker-duo", "parent": "roadmap-manager",
         "scope": "unit", "count": "workers", "label": "wkr-{unit}-{slug}{n}"},
    ],
}

# Short forms a human types: `mgr-04`, `wkr-11`, `fmgr-02`, `rm`.
ROLE_ALIASES = {
    "mgr": "manager", "manager": "manager",
    "wkr": "worker", "worker": "worker",
    "fmgr": "fleet-manager", "fleet-manager": "fleet-manager", "fm": "fleet-manager",
    "rm": "roadmap-manager", "roadmap-manager": "roadmap-manager", "roadmap": "roadmap-manager",
    "ceo": "ceo",
    "misc": "misc",
    "lead": "portfolio-lead", "portfolio-lead": "portfolio-lead",
}


def template_specs(name: str) -> list[dict]:
    if name not in TEMPLATES:
        die(f"unknown fleet template {name!r} — have: {', '.join(sorted(TEMPLATES))}")
    return TEMPLATES[name]


def spec_count(spec: dict, workers: int) -> int:
    return workers if spec["count"] == "workers" else int(spec["count"])


def top_role(man: dict) -> str:
    """The role at the head of the chain — it reports to the human, not upward."""
    for spec in template_specs(man.get("template", "three-tier")):
        if spec["parent"] is None:
            return spec["role"]
    return "ceo"


# --------------------------------------------------------------------------
# Templates
# --------------------------------------------------------------------------

def template(name: str) -> str:
    path = os.path.join(_HERE, "..", "references", f"{name}.md")
    with open(os.path.abspath(path)) as fh:
        return fh.read()


def render(name: str, **kw) -> str:
    text = template(name)
    for k, v in kw.items():
        text = text.replace("{{" + k + "}}", str(v))
    return text


def agent_bin(env: dict) -> str:
    """The rysh invocation to WRITE INTO A BRIEF.

    Every brief said the literal `rysh` until 2026-08-14, and on this machine
    there is no `rysh` — the installed binary is `rysh_local`. Design 025 §8d
    recorded the cause (three names, `rysh` / `ry` / `rysh_local`, across one
    install path, versioned in two repositories so no single diff ever showed
    them disagreeing) and filed it as an ONBOARDING failure. It is also an
    AGENT failure: a claude that follows its brief literally gets
    `bash: rysh: command not found`, exit 127, and the board stays empty while
    ABLA, ANSA, the store and the view are all working. Proved with two real
    agents in an isolated session, same pane and same second:

        rysh board post --as $RYSH_PANE -- '…'        -> exit 127, nothing
        rysh_local board post --as $RYSH_PANE -- '…'  -> exit 0, on the board

    NOT `env["bin"]` verbatim either. That is the daemon's argv[0] and may be
    relative (`./rysh_local`) — fine for fleetctl, which runs in the workspace,
    and wrong for an agent, which runs in its own WORKTREE. A brief is read in
    a cwd we do not control, so what goes in it has to be cwd-independent. That
    is the same class of bug, one level down.

    Falls back to the raw value rather than to a guessed name: `up` already
    proves this binary runs before a pane exists (require_ansa_capable), so a
    value that survives to here is one the daemon answered on.
    """
    raw = (env.get("bin") or "").strip() or "rysh"
    if os.path.isabs(raw) and os.access(raw, os.X_OK):
        return raw
    if found := shutil.which(os.path.basename(raw)):
        return found
    absolute = os.path.abspath(raw)
    if os.access(absolute, os.X_OK):
        return absolute
    return raw


def require_brief_binary(env: dict) -> None:
    """Refuse to build a fleet whose briefs would name a binary nobody can run.

    `require_ansa_capable` proves `env["bin"]` executes; it does not prove that
    what goes INTO THE BRIEF does, and those are allowed to differ (agent_bin
    absolutises, or falls back). This closes that gap in the one place where
    refusing is still free.

    E9 Q2 gap B, applied to a second prerequisite: a check that is right but
    LAST is a standing fleet nobody can reach. By the first board post there is
    a tab, a lane per role, a pane per unit, a worktree each and a brief each —
    and the failure is silent, because a board post that dies with exit 127 is
    swallowed by the agent's own shell and shows up only as a board that stays
    empty. That is the exact failure this whole change exists to end, so it is
    checked before the first pane, not discovered after the last one.
    """
    bin_ = agent_bin(env)
    if os.path.isabs(bin_) and os.access(bin_, os.X_OK):
        return
    if shutil.which(bin_):
        return
    die(f"refusing to build: the briefs would tell every agent to run {bin_!r}, "
        f"which is not executable and not on PATH. Agents follow a brief "
        f"literally — a wrong name here is `command not found`, exit 127, and a "
        f"board that stays empty while everything under it works. The daemon "
        f"reported its binary as {env.get('bin')!r}; check that the session was "
        f"started from a binary that still exists.")


# --------------------------------------------------------------------------
# up — build the whole fleet
# --------------------------------------------------------------------------

def op_up(env: dict, args, units: list[dict] | None = None) -> dict:
    # `units` lets a CALLER hand over the work it already discovered — the epic
    # driver does, because one epic is one unit and re-deriving it from the doc
    # invites the child to disagree with the parent about what the work is. That
    # disagreement is exactly what produced a 248-pane `up` (units_from_file).
    units = discover_units(args) if units is None else units
    if args.limit:
        units = units[:args.limit]
    if not units:
        die("no units discovered")

    cost = R.session_cost(env)
    if not args.force and (blocked := R.budget_blocked(cost)):
        die(blocked)

    fleet = args.fleet
    if os.path.isfile(manifest_path(env, fleet)) and not args.replace:
        die(f"fleet {fleet!r} already exists — `fleetctl down --fleet {fleet}` "
            f"first, or pass --replace to overwrite the manifest")

    specs_for_count = template_specs(args.template)
    total = sum(len(units) * spec_count(sp, args.workers) if sp["scope"] == "unit"
                else spec_count(sp, args.workers) for sp in specs_for_count)
    if args.dry_run:
        return {"fleet": fleet, "dry_run": True, "units": units,
                "template": args.template,
                "roles": [{"role": sp["role"],
                           "panes": len(units) * spec_count(sp, args.workers)
                           if sp["scope"] == "unit" else spec_count(sp, args.workers)}
                          for sp in specs_for_count],
                "panes_that_would_open": total,
                "roadmap_dir": args.roadmap_dir}

    # ---- the deployment gate, while refusing is still free ----------------
    #
    # E9 Q2 GAP B. Everything above this line is argument validation; everything
    # below it costs something that has to be undone by hand. ANSA is a hard
    # prerequisite for a fleet — there is no typing fallback since W4-3b — and
    # until now the only thing that checked it was `fleet_send`, so a dead route
    # was discovered at the first message: tab open, lane per role, pane per
    # unit, a worktree cut and a brief written for each, and every one of them
    # unreachable. The check was never wrong, it was just last.
    #
    # AFTER the --dry-run return, deliberately: a dry run opens nothing and
    # delivers nothing, so it has no business requiring a live daemon. This is
    # the last statement before the first pane exists.
    require_ansa_capable(env)
    require_brief_binary(env)
    _comm = normalize_comm(getattr(args, "comm", None) or DEFAULT_COMM)
    if is_board_comm(_comm):
        require_board_send_capable(env)
    if _comm == COMM_QUEUE:
        require_queue_capable(env)

    # ---- panes -----------------------------------------------------------
    #
    # ONE LANE PER ROLE, built from the template rather than from three
    # hard-coded tiers (design 028 §6.7). A fleet-scoped role gets a lane with
    # one pane; a unit-scoped role gets a lane stacked to units × count.
    panes = R.list_panes(env)
    anchor = R.self_pane(panes) or R.active_pane(panes)
    tab = anchor["tab"]

    # A TAB OF ITS OWN, and the manifest records that WE opened it — `down`
    # closes only a tab this fleet created. Tearing down a tab the human was
    # already using would take their panes with it.
    owns_tab = False
    default_shells: list[dict] = []
    if getattr(args, "new_tab", False) and not args.ceo_here:
        opened = new_tab(env, fleet)
        if opened:
            tab, owns_tab = opened, True
            # `##new tab` arrives with default shell lanes (observed: two).
            # Remember their panes now; they are deleted AFTER the role lanes
            # exist, because a tab refuses to lose its last lane.
            default_shells = [p for p in R.list_panes(env) if p.get("tab") == tab]
        else:
            die("--new-tab: the tab never appeared; refusing to build the fleet "
                "in whichever tab happened to be active")

    specs = template_specs(args.template)
    stacked_head = None          # --stacked: the one lane every role joins
    lanes: dict = {}
    role_panes: dict = {}
    for spec in specs:
        want = len(units) * spec_count(spec, args.workers) if spec["scope"] == "unit" \
            else spec_count(spec, args.workers)
        if want <= 0:
            role_panes[spec["role"]] = []
            continue
        # --ceo-here keeps the calling pane as the head of the chain, so the
        # human keeps the pane they are already in rather than watching a lane
        # open on top of them.
        if args.ceo_here and spec["parent"] is None and want == 1:
            role_panes[spec["role"]] = [anchor]
            lanes[spec["role"]] = None
            continue
        # --stacked: ONE lane for the whole chain, every agent a pane in the
        # same stack, so a tab reads top-to-bottom as the org chart and the
        # board keeps the right-most lane to itself. Without it each role gets
        # its own lane (the 028 §6.8 default).
        if getattr(args, "stacked", False) and stacked_head is not None:
            got = stack_onto(env, stacked_head, want)
        else:
            head = new_lane(env, tab)
            got = [head] + stack_onto(env, head, want - 1)
            if getattr(args, "stacked", False):
                stacked_head = head
        role_panes[spec["role"]] = got
        # Record the lane by FULL uuid. Teardown needs it: a per-pane
        # `##pane delete` reports "deleted" for a pane in another lane and
        # silently does nothing, so the only teardown that works is deleting the
        # lane — and `##lane delete` accepts nothing but the full uuid, which
        # `##lane list`, `##panegroup list` and `##pane info` all truncate to
        # eight characters. `##lane info` run AS a pane in the lane is the one
        # view that prints it whole.
        lanes[spec["role"]] = lane_uuid(env, got[0]["id"])

    # ---- tab finish: drop the default shell lanes, open the fleet board --
    #
    # The oss-export setup (2026-08-14): a fleet tab is role lanes plus the
    # fleet's agents-board in its own right-most lane, and nothing else. The
    # default shell lanes `##new tab` was born with are deleted only now,
    # because a tab refuses to lose its last lane.
    board_info = None
    if owns_tab:
        delete_lanes_of(env, default_shells)
        board_info = open_fleet_board(env, tab, fleet)

    # ---- identities ------------------------------------------------------
    taken: set[str] = {p["name"] for p in R.list_panes(env)}
    roadmap_dir = workspace_relative(env, args.roadmap_dir, "--roadmap-dir")
    for spec in specs:
        os.makedirs(os.path.join(env["workspace"], roadmap_dir, spec["role"] + "s"),
                    exist_ok=True)

    man = {"fleet": fleet, "tab": tab, "created": int(time.time()),
           "workspace": env["workspace"], "roadmap_dir": roadmap_dir,
           "source": args.source, "template": args.template, "owns_tab": owns_tab,
           "board": board_info,
           "global_board": getattr(args, "global_board", "") or "",
           "web_sync": getattr(args, "web_sync", "root") or "root",
           "comm": normalize_comm(getattr(args, "comm", None) or DEFAULT_COMM),
           "units": [{"unit": u["unit"], "title": u["title"], "sources": u["sources"]}
                     for u in units],
           "members": [], "msg_seq": 0, "lanes": lanes, "log": []}

    # THE ROSTER IS FLAT and each member names its parent BY PANE ID. Nesting is
    # what made a fourth role a schema change; a parent pointer is what lets
    # `msg` refuse to skip a link without knowing how many tiers there are.
    heads: dict = {}          # role -> the fleet-scoped member holding it
    per_unit: dict = {}       # (role, unit) -> member
    for spec in specs:
        pool = role_panes.get(spec["role"], [])
        idx = 0
        targets = units if spec["scope"] == "unit" else [None]
        for u in targets:
            for k in range(spec_count(spec, args.workers)):
                if idx >= len(pool):
                    break
                pane = pool[idx]; idx += 1
                unit = u["unit"] if u else "-"
                s_slug = slug(u["title"]) if u else fleet
                n = f"-{k+1}" if spec_count(spec, args.workers) > 1 else ""
                label = unique(taken, spec["label"].format(
                    fleet=fleet, unit=unit, slug=s_slug, n=n))
                parent = None
                if spec["parent"]:
                    up = per_unit.get((spec["parent"], unit)) or heads.get(spec["parent"])
                    parent = up["pane"] if up else None
                member = {
                    "role": spec["role"], "unit": unit, "pane": pane["id"],
                    "label": label, "parent": parent,
                    "roadmap": os.path.join(roadmap_dir, spec["role"] + "s", f"{label}.md"),
                    "title": u["title"] if u else "",
                    "sources": u["sources"] if u else [],
                }
                man["members"].append(member)
                if spec["scope"] == "unit":
                    per_unit[(spec["role"], unit)] = member
                else:
                    heads[spec["role"]] = member

    # ---- worktrees -------------------------------------------------------
    if args.worktrees:
        base = args.base_branch
        for p in roster(man):
            wt = make_worktree(env, f"{fleet}-{p['label']}", base)
            p["worktree"] = wt.get("path")
            p["worktree_error"] = wt.get("error")

    # ---- name, tag, launch ----------------------------------------------
    for p in roster(man):
        rename(env, p["pane"], p["label"])
        set_meta(env, p["pane"], META_FLEET, fleet)
        set_meta(env, p["pane"], META_ROLE, p["role"])
        set_meta(env, p["pane"], META_UNIT, str(p.get("unit", "-")))
        set_meta(env, p["pane"], META_LABEL, p["label"])
        set_meta(env, p["pane"], META_ROADMAP, p["roadmap"])
        # The fleet's OWN board (design 028 §6.1). Without this every agent
        # posts to the session board and twenty-five fleets share one stream —
        # which is the thing the board ids were built to end. A pane carrying it
        # needs no flag: `rysh board post` resolves the board from the poster.
        set_meta(env, p["pane"], META_BOARD, fleet)
        # The courier reads this: an input-stream pane is never delivered to
        # from the board (its messages go through the pane).
        set_meta(env, p["pane"], META_COMM, fleet_comm(man))
        if p.get("parent"):
            set_meta(env, p["pane"], META_PARENT, p["parent"])
        if p.get("worktree"):
            set_meta(env, p["pane"], META_WORKTREE, p["worktree"])
        # BEFORE the agent starts: its first seconds are the busiest repaint
        # burst it will ever have, twenty-plus times over.
        if web_sync_off(man, p):
            set_meta(env, p["pane"], META_WEB_SYNC, "off")

    save_manifest(env, man)
    # Membership BEFORE launch (design 054): every brief tells its agent to
    # declare `fleet status progress` as its first act, and the ledger refuses
    # a pane that is not yet a member. The sync after launch still runs — it
    # adds the session ids — and an upsert keeps whatever status landed.
    early_reg = registry_sync(env, man, "")
    agent, launch_args = agent_args_for(args)
    man["agent"] = agent
    man["agent_args"] = launch_args
    launched = launch_roster(env, man, args, agent, launch_args)

    # queue messaging (dev, design 051): every agent gets its own listener on the board.
    listeners = []
    if fleet_comm(man) == COMM_QUEUE:
        for p in roster(man):
            listeners.append(start_listener(env, man, p["pane"]))

    # TRUTHFUL STATE (R11). The daemon's registry is the authority and the
    # manifest the cache, but the registry is only as true as what is written to
    # it: `up` only for a launch that succeeded, `registered` for one that did
    # not, and a refusal of the write (a pre-R11 daemon's cap) reported rather
    # than acted on. Membership was registered BEFORE launch (design 054) above.
    outcome = launch_outcome(env, man, total)
    state = record_launch_state(env, man, outcome)
    if early_reg.get("errors"):
        state["errors_before_launch"] = early_reg["errors"]
    state["watch"] = apply_watch(env, fleet, args)
    state["goal"] = apply_goal(env, fleet, args)

    return {"fleet": fleet, "units": len(units), "panes": total,
            "roadmap_dir": roadmap_dir, "manifest": manifest_path(env, fleet),
            "ok": outcome["ok"], "launch": outcome, "state": state,
            "registry": state["registry"],
            "launched": launched, "comm": fleet_comm(man), "listeners": listeners,
            "next": f"fleetctl verify --fleet {fleet}   # judge launches by screen, not exit code"}


def build_brief(env: dict, man: dict, p: dict, args) -> str:
    """The prompt one agent is started with.

    GENERIC SINCE `E-42`: the brief is chosen by ROLE — references/brief-<role>.md
    — and every role gets the same variables. Before this there was a branch per
    tier, so a new role meant editing this function; now it means adding a file.

    A MISSING BRIEF IS FATAL rather than falling back to another role's. An
    agent started with the wrong brief does the wrong job confidently, which is
    strictly worse than a fleet that refuses to start.
    """
    fleet = man["fleet"]
    unit = unit_meta(man, p.get("unit", "-"))
    kids = children_of(man, p)
    parent = None
    if p.get("parent"):
        parent = next((m for m in roster(man) if m["pane"] == p["parent"]), None)

    rows = "\n".join(
        f"- `{m['label']}` — {m['role']} — unit `{m.get('unit', '-')}` — pane `{m['pane']}`"
        for m in roster(man))
    child_rows = "\n".join(
        f"- `{m['label']}` — {m['role']} — pane `{m['pane']}` — roadmap `{m['roadmap']}`"
        for m in kids) or "(none — you are at the bottom of this chain)"
    srcs = "\n".join(f"- `{os.path.relpath(sp, env['workspace'])}`"
                      for sp in (p.get("sources") or unit.get("sources") or [])) or "(none)"

    spec = next((sp for sp in template_specs(man.get("template", "three-tier"))
                 if sp["role"] == p["role"]), None)
    brief = spec["brief"] if spec else f"brief-{p['role']}"

    # COMPATIBILITY ALIASES for the briefs that shipped before roles were data.
    # brief-worker says {{MANAGER}}, brief-manager says {{CEO}} and {{WORKERS}};
    # those files are on disk, users edit them, and a variable this function
    # stops supplying does not error — it renders as the literal `{{MANAGER}}`
    # in an agent's first prompt. Aliasing them onto the generic names costs
    # three lines and keeps every existing brief working unchanged.
    ceo = next((m for m in roster(man) if m["role"] == "ceo"), None)
    text = render(
        brief,
        MANAGER=(parent or {}).get("label", "the human"),
        MANAGER_PANE=(parent or {}).get("pane", "-"),
        CEO=(ceo or {}).get("label", "-"),
        CEO_PANE=(ceo or {}).get("pane", "-"),
        WORKERS=child_rows,
        FLEET=fleet, LABEL=p["label"], ROLE=p["role"], PANE=p["pane"],
        # Which CLI this agent IS. A brief that opens "You are a claude" handed
        # to a codex is a false fact in the one prompt an agent trusts most.
        AGENT=man.get("agent", "claude"),
        UNIT=p.get("unit", "-"), ROADMAP=p["roadmap"], WORKSPACE=env["workspace"],
        WORKTREE=p.get("worktree") or "(none yet — create one before any code work)",
        FLEETCTL=os.path.join(_HERE, "fleetctl.py"),
        PROTOCOL=os.path.abspath(os.path.join(_HERE, "..", "references", "protocol.md")),
        MISSION=args.mission or "(the human will state the mission in the first work order)",
        TITLE=p.get("title") or unit.get("title") or fleet,
        SOURCES=srcs,
        ROSTER=rows,
        UNITS=len(man.get("units", [])),
        CHILDREN=child_rows,
        PARENT=(parent or {}).get("label", "the human"),
        PARENT_PANE=(parent or {}).get("pane", "-"),
        BOARD=fleet,
        # The binary an agent can actually RUN, not the name the docs use.
        # See agent_bin: `rysh` does not exist on this machine, and a brief
        # that says it produces exit 127 and an empty board.
        BIN=agent_bin(env),
    )

    # AN UNRENDERED VARIABLE IS A FATAL ERROR, not a cosmetic one. It reaches
    # the agent as the literal `{{THING}}` in the prompt that tells it who it is
    # and who it reports to — instructions it cannot follow, in a session that
    # otherwise looks like it started fine. Refusing here is the same rule as
    # refusing a missing brief.
    # THE GLOBAL BOARD (founder order 2026-09-25): one board every epic fleet
    # posts to besides its own, so the human reads all fleets in one pane.
    # Appended rather than templated so every brief, of every template, gets
    # it without a {{VAR}} that older briefs would leave unrendered.
    text += web_sync_section(man, p)
    text += comm_section(env, man, p)
    text += status_section(env, man, p)
    # Only the HEAD of each fleet writes the global board (founder order
    # 2026-10-03): it is a board of fleets, not of agents, and every post there
    # wakes every fleet reading it. Managers and workers keep to their own board.
    gb = man.get("global_board")
    if gb and p["role"] == top_role(man):
        text += (
            f"\n\n## The global fleet board: `{gb}`\n\n"
            f"Besides your fleet's own board (`{fleet}`), every epic fleet in this "
            f"session posts to ONE shared board, `{gb}`, which the human watches "
            f"to see all fleets at once. Post there with:\n\n"
            f"```sh\n{agent_bin(env)} board post --board {gb} --as {p['pane']} -- "
            f"'[{fleet}] <START | MILESTONE | BLOCKED | DONE>: <one line>'\n```\n\n"
            f"Post START once you are up, then only milestones, blockers and DONE — "
            f"never chatter; a post there wakes every fleet reading it. Always prefix "
            f"the line with `[{fleet}]` so the reader knows which fleet spoke. Read "
            f"what other fleets said with `{agent_bin(env)} board tail --board {gb}`; "
            f"a cross-fleet dependency is raised there, never by messaging another "
            f"fleet's panes directly.\n")

    leftover = sorted(set(re.findall(r"\{\{([A-Z_]+)\}\}", text)))
    if leftover:
        die(f"brief {brief!r} for role {p['role']!r} has unrendered variables: "
            + ", ".join(leftover))
    return text


# --------------------------------------------------------------------------
# Operations
# --------------------------------------------------------------------------
# --------------------------------------------------------------------------
# The epic driver (design 028 §6.8, `E-43`) — ONE FLEET PER EPIC, STAGED.
#
# `up` builds ONE fleet with a manager per unit. The driver inverts that: each
# epic doc becomes its OWN fleet, with its own board and its own org chart, and
# they are brought up A FEW AT A TIME.
#
# WHY STAGED RATHER THAN FANNED OUT. Twenty-five fleets is roughly 150 panes and
# 125 live claude sessions. The pane limit refuses first, the token ceiling
# second, and the load failures on this stack are already on record — the ##cmd
# pane dies under load and `start` returns 0 for launches that never happened.
# So the driver REGISTERS everything (free: a row and a board id) and brings up
# `--promote N` of them per run.
#
# THERE IS NO CAP (founder ruling R11, 2026-10-07, retired D-14's three). The
# limit on spend is the session token ceiling, checked before every launch;
# `--promote` is this driver's own staging flag, not a registry limit, and the
# registry records the state each launch actually earned.
# --------------------------------------------------------------------------


def epic_fleet_name(unit: dict) -> str:
    """The fleet name for one epic: its unit id and a slug of its title.

    It must also be a legal BOARD ID — lower-case, no dots — because a fleet's
    board id defaults to its name and a board id is a NATS subject token. The
    registry refuses an illegal one; this makes sure we never build one.
    """
    base = f"epic-{unit['unit']}-{slug(unit['title'], words=2, cap=14)}"
    cleaned = re.sub(r"[^a-z0-9_-]+", "-", base.lower()).strip("-")
    return re.sub(r"-+", "-", cleaned)[:32].strip("-")


def epics_budget_blocked(env: dict, args) -> str | None:
    """The token-ceiling check `up` makes, asked BEFORE each launch so a driver
    stops cleanly instead of `up` dying part-way through a run. `--force`
    bypasses it exactly as it does for `up`."""
    if getattr(args, "force", False):
        return None
    return R.budget_blocked(R.session_cost(env))


def op_epics(env: dict, args) -> dict:
    """Register one fleet per epic, then bring up to `--promote` of them up.

    NO CAP (founder ruling R11, 2026-10-07, retired D-14). `--promote N` is the
    DRIVER's staging flag — how many fleets this run launches — not a registry
    limit: no slot is reserved before a launch, because writing `up` before the
    panes exist is the lie R11 ends. Each launch is preceded by the token-ceiling
    check, and `op_up` records the state the launch actually earned.

    Reports the lists separately — registered, up, failed — plus the reason a
    run stopped early on the token ceiling.
    """
    units = discover_units(args)
    if args.limit:
        units = units[:args.limit]
    if not units:
        die("no epics discovered")

    plan = [{"fleet": epic_fleet_name(u), "unit": u["unit"], "title": u["title"],
             "sources": u["sources"]} for u in units]

    if args.dry_run:
        return {"dry_run": True, "epics": len(plan), "fleets": plan,
                "note": "registering opens no panes; --promote N brings that many up"}

    registered, failed = [], []
    for item in plan:
        src = item["sources"][0] if item["sources"] else ""
        res = registry(env, "register", item["fleet"], "--source", src,
                       "--roadmap", args.roadmap_dir)
        if res.get("error"):
            failed.append({"fleet": item["fleet"], "error": res["error"]})
            continue
        registered.append(item["fleet"])

    up, blocked = [], None
    # ONE ATTEMPT PER PROMOTION. A failed `up` consumes one, so a systemic
    # failure (a missing claude binary, a full pane limit) stops after
    # --promote attempts instead of walking every epic.
    attempts = 0
    for item in plan:
        if item["fleet"] not in registered:
            continue
        if len(up) >= args.promote or attempts >= args.promote:
            break
        if (blocked := epics_budget_blocked(env, args)):
            break
        sub = argparse.Namespace(**vars(args))
        sub.fleet = item["fleet"]
        # The child `up` runs on ONE epic doc, so its --from is that doc.
        sub.source = item["sources"][0] if item["sources"] else args.source
        sub.limit = 0
        sub.split_on = args.split_on
        sub.scan_source = False
        sub.replace = True
        attempts += 1
        try:
            # ONE EPIC IS ONE UNIT — handed over, never re-discovered. Letting
            # the child re-derive it is what produced a 248-pane `up` live on
            # 2026-08-11 (see op_up's `units` parameter and units_from_file).
            built = op_up(env, sub, units=[{"unit": item["unit"], "title": item["title"],
                                            "sources": item["sources"]}])
        except SystemExit:
            failed.append({"fleet": item["fleet"], "error": "up failed; state left registered"})
            continue
        if not built.get("ok", True):
            failed.append({"fleet": item["fleet"],
                           "error": "launch incomplete",
                           "problems": (built.get("launch") or {}).get("problems", [])})
            continue
        up.append({"fleet": item["fleet"], "panes": built.get("panes"),
                   "state": built.get("state")})

    return {"epics": len(plan), "registered": registered,
            "up": up, "failed": failed, "budget_blocked": blocked,
            "next": "python3 fleetctl.py epics-promote   # bring the next waiting fleet up"}


def op_epics_promote(env: dict, args) -> dict:
    """Bring the next waiting fleet up.

    NO CAP (R11). "Waiting" is a `registered` row whose member panes are NOT
    live: on a daemon still running a pre-R11 binary, a live fleet past the third
    reads `registered`, and promoting it would rebuild a running fleet over
    itself. The token ceiling is checked before the launch, as `up` does.
    """
    listing = registry(env, "ls")
    if listing.get("error"):
        die(f"cannot read the fleet registry: {listing['error']}")

    fleets = listing.get("fleets") or []
    panes = R.list_panes(env)
    waiting = [f for f in fleets if f.get("state") == "registered"
               and not fleet_liveness(f, panes)["panes_live"]]
    skipped_live = [f["name"] for f in fleets if f.get("state") == "registered"
                    and fleet_liveness(f, panes)["panes_live"]]
    if not waiting:
        return {"promoted": None, "reason": "no fleet is waiting to come up",
                "skipped_live": skipped_live,
                "up": [f["name"] for f in fleets if f.get("state") == "up"]}

    if (blocked := epics_budget_blocked(env, args)):
        return {"promoted": None, "reason": blocked, "skipped_live": skipped_live}

    target = waiting[0]
    sub = argparse.Namespace(**vars(args))
    sub.fleet = target["name"]
    sub.source = target.get("source") or ""
    sub.replace = True
    # ONE EPIC IS ONE UNIT. The registry records the doc a fleet was cut from,
    # so the unit is reconstructed here rather than re-discovered — see op_up's
    # `units` parameter for what re-discovery cost.
    unit = {"unit": "01", "title": _title_of(sub.source) if sub.source else target["name"],
            "sources": [sub.source] if sub.source else []}
    built = op_up(env, sub, units=[unit])
    return {"promoted": target["name"] if built.get("ok", True) else None,
            "panes": built.get("panes"), "state": built.get("state"),
            "launch": built.get("launch"), "skipped_live": skipped_live}



def op_tree(env: dict, man: dict) -> str:
    """The org chart, rendered from the parent pointers rather than from tiers.

    Depth comes from walking `parent`, so a four-role fleet draws itself without
    this function knowing what a fleet-manager is.
    """
    panes = {p["id"]: p for p in R.list_panes(env)}
    members = roster(man)
    by_pane = {m["pane"]: m for m in members}

    def mark(m):
        live = panes.get(m["pane"], {}).get("program") or ""
        return "●" if live else "○"

    def depth(m):
        d, cur, seen = 0, m, set()
        while cur.get("parent") and cur["pane"] not in seen:
            seen.add(cur["pane"])
            cur = by_pane.get(cur["parent"]) or {}
            d += 1
        return d

    out = [f"fleet {man['fleet']}  ({len(man.get('units', []))} units, "
           f"{len(members)} agents, template {man.get('template', 'three-tier')})  "
           f"board {man['fleet']}  roadmaps: {man['roadmap_dir']}"]
    for m in members:
        pad = "  " * depth(m)
        unit = m.get("unit", "-")
        title = (m.get("title") or "")[:44]
        out.append(f"{pad}{mark(m)} {m['role']:<15} [{unit}] {m['label']}  "
                   f"{short(m['pane'])}   {title}")
    return "\n".join(out)

def op_verify(env: dict, man: dict) -> dict:
    """Judge a launch by the pane's screen, never by an exit code.

    `start` returns 0 whether the agent came up or died on a bad flag; the screen
    is the only honest signal.

    For CODEX the signal is the PROCESS, not the screen: `is_busy`/`has_composer`
    read claude's TUI furniture ("esc to interrupt", the composer border), which
    a perfectly healthy codex does not draw. Judging a codex by them would report
    `no-tui` for every working agent — a red verify for a green fleet, which is
    worse than no verify at all. The foreground program answers the question this
    function is actually asking, and it is the same signal `##ansa kill` trusts.
    """
    rows = []
    agent = man.get("agent", "claude")
    for p in roster(man):
        pane = R.find_pane_opt(R.list_panes(env), p["pane"])
        if not pane:
            rows.append({"label": p["label"], "state": "pane-gone"})
            continue
        try:
            if is_native(agent):
                # ASK THE AGENT. A rysh agent has no foreground program and
                # draws no TUI of its own, so both of the other signals — the
                # program name and claude's composer furniture — are blank for a
                # perfectly healthy one. Its own run status is the only honest
                # answer, and it is the same fact `##ansa kill` verifies against.
                st = native_run_status(env, p["pane"])
                state = ("working" if st.get("in_flight") else
                         "paused" if st.get("paused") else "idle")
            elif agent == "codex":
                prog = (pane.get("program") or "").strip()
                # codex is a node process; an empty foreground is a bare shell,
                # which after a launch means it died or never started.
                state = "running" if prog else "no-tui"
            else:
                scr = R.vt_screen(env, p["pane"])
                lines = [R.visible_solid(x) for x in scr.get("lines", [])]
                state = "busy" if R.is_busy(lines) else (
                    "idle" if R.has_composer(lines) else "no-tui")
        except Exception as exc:
            state = f"unreadable: {exc}"
        row = {"label": p["label"], "role": p["role"],
               "pane": short(p["pane"]),
               "program": pane.get("program") or "", "state": state}
        if is_board_comm(fleet_comm(man)):
            row["inbox"] = comm_inbox(agent, pane.get("meta") or {})
        if fleet_comm(man) == COMM_QUEUE:
            pid = listener_alive(env, p["pane"])
            row["listener"] = f"pid {pid}" if pid else "missing: run `fleetctl --fleet <name> listen`"
        rows.append(row)
    bad = [r for r in rows if r.get("state") in ("no-tui", "pane-gone")]
    # polling-stream: an agent the courier cannot reach is not up, however
    # healthy its screen looks -- every message to it would come back
    # `undelivered`. Reported apart from not_running so the fix is obvious.
    unreachable = [r for r in rows if r.get("inbox", "").startswith("missing")
                   or r.get("listener", "").startswith("missing")]
    return {"fleet": man["fleet"], "comm": fleet_comm(man), "agents": rows,
            "not_running": bad, "unreachable": unreachable,
            "ok": not bad and not unreachable}


def comm_inbox(agent: str, meta: dict) -> str:
    """Where the courier will deliver to this agent, or why it cannot."""
    if is_native(agent):
        return "rysh-native"
    if agent == "codex":
        tid = (meta.get(META_CODEX_SESSION) or "").strip()
        return (f"codex-queue {tid[:13]}" if tid else
                "missing: no codex.session_id yet (discovery runs ~5 min after launch; "
                "re-run verify)")
    path = (meta.get(META_CLAUDE_INBOX) or "").strip()
    return (f"claude-inbox {path}" if path else
            "missing: no claude.inbox stamp (was it launched with the fleet's --settings "
            "file? the courier still tries /tmp/cc-socks/<pid>.sock)")


def op_commit(env: dict, man: dict, member: dict, message: str) -> dict:
    """Commit only this agent's own roadmap file, by path.

    Path-limited so a shared file dirtied by a sibling is never swept in, and
    text-only because a fleet that commits renders and captures turns the repo
    into an artifact store.
    """
    # Defended here as well as at `up`, because manifests written before F-28
    # was fixed are still on disk holding absolute roadmap paths, and this is
    # the function where an absolute one did the damage.
    rel = workspace_relative(env, member["roadmap"], "roadmap")
    root = member.get("worktree") or env["workspace"]
    ext = os.path.splitext(rel)[1].lower()
    if ext in MEDIA:
        die(f"refusing to commit {rel}: {ext} is a media/binary type. "
            f"Fleet roadmaps are text only.")
    full = os.path.join(root, rel)
    if not os.path.isfile(full):
        die(f"{rel} does not exist yet — write your roadmap before committing it")
    if os.path.getsize(full) > 2 << 20:
        die(f"refusing to commit {rel}: larger than 2 MiB, which is not prose")
    subject = message or f"docs(fleet): {member['label']} — roadmap update"
    # A path-limited commit cannot create a file's first commit: `git commit --
    # <path>` fails with "pathspec did not match any file(s) known to git" while
    # the path is untracked, which is every agent's FIRST commit. Stage that one
    # path first — still path-limited, so a sibling's dirty file is never caught.
    if git(env, "ls-files", "--error-unmatch", "--", rel, cwd=root).returncode != 0:
        a = git(env, "add", "--", rel, cwd=root)
        if a.returncode != 0:
            return {"committed": False, "file": rel,
                    "reason": (a.stderr or a.stdout).strip()}
    last = None
    for attempt in range(5):
        r = git(env, "commit", "-m", subject, "--", rel, cwd=root)
        out = (r.stdout or "") + (r.stderr or "")
        if r.returncode == 0:
            sha = git(env, "rev-parse", "--short", "HEAD", cwd=root).stdout.strip()
            branch = git(env, "rev-parse", "--abbrev-ref", "HEAD",
                         cwd=root).stdout.strip()
            res = {"committed": True, "sha": sha, "file": rel, "root": root,
                   "branch": branch, "subject": subject,
                   "attempts": attempt + 1}
            # SAY when the commit went into the tree every sibling shares.
            # `root` falls back to the workspace whenever the manifest holds no
            # worktree for this member — including the case that actually bit:
            # the brief tells an agent with no worktree to create one, and
            # nothing records it back, so the agent works in its own tree while
            # `commit` quietly writes to the shared one. The fallback is
            # legitimate for a fleet started without `--worktrees`; being
            # silent about it is not, because the receipt then reads identical
            # in both cases. A receipt that cannot be told apart from the wrong
            # outcome is the defect this whole track keeps re-finding.
            if os.path.realpath(root) == os.path.realpath(env["workspace"]):
                res["shared_tree"] = True
                res["note"] = ("committed into the SHARED workspace checkout, "
                               "not a per-agent worktree")
            return res
        if "nothing to commit" in out or "no changes added" in out:
            return {"committed": False, "reason": "nothing to commit", "file": rel}
        if "index.lock" in out or "Unable to create" in out:
            last = out.strip(); time.sleep(2 + attempt * 2); continue
        return {"committed": False, "reason": out.strip(), "file": rel}
    return {"committed": False, "reason": f"index.lock contention: {last}",
            "file": rel}


def op_subfleet(env: dict, man: dict, member: dict, count: int, args) -> dict:
    """A manager opens its own pane GROUP of extra workers and adopts them."""
    if member["role"] != "manager":
        die("only a manager builds a sub-fleet; the CEO delegates to managers")
    unit = next(u for u in man["units"] if u["manager"]["pane"] == member["pane"])
    anchor = R.find_pane(R.list_panes(env), unit["workers"][0]["pane"]) \
        if unit["workers"] else R.find_pane(R.list_panes(env), member["pane"])
    fresh = new_group_in_lane(env, man["tab"], anchor.get("lane", 1), count)
    taken = {p["name"] for p in R.list_panes(env)}
    added = []
    # A sub-fleet INHERITS its fleet's agent unless the manager names another.
    # `subfleet --agent` defaults to empty for exactly this: a manager in a codex
    # fleet that had to remember to re-type `--agent codex` would eventually
    # open a claude among the codexes, and mixed-agent fleets are out of scope
    # (§7.3) precisely because nothing downstream expects one.
    if not getattr(args, "agent", ""):
        args.agent = man.get("agent", "claude")
        if not getattr(args, "agent_args", "") and man.get("agent_args"):
            args.agent_args = man["agent_args"]
    agent, launch_args = agent_args_for(args)
    base = slug(unit["title"])
    for i, pane in enumerate(fresh, len(unit["workers"]) + 1):
        w = {"role": "worker", "unit": unit["unit"], "pane": pane["id"],
             "label": unique(taken, f"wkr-{unit['unit']}-{base}-{i}"),
             "roadmap": os.path.join(man["roadmap_dir"], "workers",
                                     f"unit-{unit['unit']}-w{i}.md"),
             "parent": member["pane"], "title": unit["title"]}
        rename(env, w["pane"], w["label"])
        for k, v in ((META_FLEET, man["fleet"]), (META_ROLE, "worker"),
                     (META_UNIT, unit["unit"]), (META_LABEL, w["label"]),
                     (META_ROADMAP, w["roadmap"]), (META_PARENT, member["pane"])):
            set_meta(env, w["pane"], k, v)
        if web_sync_off(man, w):
            set_meta(env, w["pane"], META_WEB_SYNC, "off")
        if args.worktrees:
            wt = make_worktree(env, f"{man['fleet']}-{w['label']}", args.base_branch)
            w["worktree"] = wt.get("path")
        unit["workers"].append(w)
        prompt = build_brief(env, man, w, args)
        set_meta(env, w["pane"], META_COMM, fleet_comm(man))
        if fleet_comm(man) == COMM_QUEUE:
            start_listener(env, man, w["pane"])
        res = start_agent(env, R.find_pane(R.list_panes(env), w["pane"]),
                          prompt, w.get("worktree"), launch_args, agent, man["fleet"],
                          comm=fleet_comm(man))
        w["session_id"] = res["session_id"]
        # Same stamp as the main roster, through the same function: a
        # kill/resume that only worked for the top three tiers is a trap.
        stamp_launch(env, w["pane"], res, agent, res.get("launch_args", launch_args))
        if res.get("trusted", {}).get("added"):
            man.setdefault("codex_trusted", []).append(res["trusted"]["path"])
        added.append({"label": w["label"], "pane": w["pane"]})
    save_manifest(env, man)
    return {"fleet": man["fleet"], "unit": unit["unit"], "added": added}


def op_down(env: dict, man: dict, keep_ceo: bool, keep_worktrees: bool) -> dict:
    # `--keep-ceo` keeps the HEAD OF THE CHAIN, whatever this template calls it
    # (design 028 §6.7). For a three-tier fleet that is the ceo, which is why the
    # flag is still spelled that way; for an epic fleet it is the roadmap
    # manager. Keying on the literal string "ceo" would silently keep nobody in
    # a fleet whose head has another name — a teardown that quietly exceeded
    # what was asked.
    head = top_role(man)
    closed, kept = [], []
    for p in roster(man):
        if keep_ceo and p["role"] == head:
            kept.append(p["label"]); continue
        pane = R.find_pane_opt(R.list_panes(env), p["pane"])
        if pane:
            R.rysh_exec(env, f"##pane delete {p['pane']}")
        closed.append({"label": p["label"], "pane": p["pane"],
                       "session_id": p.get("session_id")})

    # `##pane delete` reports success but will NOT remove the last pane of a
    # lane, so a per-pane teardown always strands one live claude per lane.
    # Delete the lanes this fleet opened — never a lane we did not create.
    lanes_gone, lanes_left = [], []
    known = man.get("lanes") or {}
    if not known:
        # A fleet built before lane ids were recorded: recover them from any
        # pane still alive, which is the only place the full uuid is visible.
        live = {p["id"] for p in R.list_panes(env)}
        known = {}
        for p in roster(man):
            role = p["role"]
            if role not in known and p["pane"] in live:
                known[role] = lane_uuid(env, p["pane"])
    for role, lane_id in known.items():
        if not lane_id or (keep_ceo and role == head):
            continue
        out = R.rysh_exec(env, f"##lane delete {lane_id}")
        (lanes_gone if "deleted" in out else lanes_left).append(
            {"role": role, "lane": lane_id, "output": out.strip()})
    # The fleet's OWN tab, closed last — after its lanes, so nothing is still
    # being torn down inside it. Only if this fleet opened it (`owns_tab`):
    # a fleet built in the human's tab must never close it.
    tab_closed = None
    if man.get("owns_tab") and man.get("tab") and not keep_ceo:
        out = R.rysh_exec(env, f"##tab delete {man['tab']}")
        tab_closed = {"tab": man["tab"], "output": out.strip()}

    survivors = [p["label"] for p in roster(man)
                 if R.find_pane_opt(R.list_panes(env), p["pane"])
                 and not (keep_ceo and p["role"] == head)]
    removed, held = [], []
    if not keep_worktrees:
        # Sweep by PREFIX, not just by what the manifest recorded: an agent that
        # ran `git worktree add` by hand instead of `fleetctl worktree` leaves a
        # tree nothing knows about, and those accumulated across every run.
        wanted = {p["worktree"] for p in roster(man) if p.get("worktree")}
        listing = git(env, "worktree", "list", "--porcelain").stdout or ""
        prefix = os.path.join(env["workspace"], "worktrees", f"{man['fleet']}-")
        for line in listing.splitlines():
            if line.startswith("worktree "):
                path = line.split(" ", 1)[1].strip()
                if path.startswith(prefix):
                    wanted.add(path)
        for path in sorted(wanted):
            # No --force: a worktree with uncommitted changes is somebody's
            # unsaved work, and tearing the fleet down must not discard it.
            r = git(env, "worktree", "remove", path)
            if r.returncode == 0:
                removed.append(path)
            else:
                held.append({"path": path,
                             "reason": (r.stderr or r.stdout).strip()[:160]})
        git(env, "worktree", "prune")
    # Give back what `up` took: a codex fleet writes `trust_level = "trusted"`
    # into the user's ~/.codex/config.toml for every cwd it launches in, and a
    # grant that outlives the fleet that needed it is a permission nobody
    # remembers giving. Only this fleet's own marked entries go.
    untrusted = codex_untrust(man["fleet"]) if man.get("codex_trusted") else []
    agent = man.get("agent", "claude")
    resume_hint = ("`codex resume --last` from the agent's working directory"
                   if agent == "codex"
                   else "no resume verb — a rysh agent's conversation is persisted under "
                        "its PANE id, so closing the pane orphans the transcript rather than "
                        "deleting it, and nothing reaches it afterwards; keep what matters by "
                        "committing it before teardown"
                   if is_native(agent)
                   else "`claude -r <session_id>` from the agent's working directory")
    return {"fleet": man["fleet"], "closed": closed, "kept": kept,
            "tab_closed": tab_closed,
            "lanes_deleted": lanes_gone, "lanes_left": lanes_left,
            "panes_surviving": survivors,
            "worktrees_removed": removed, "worktrees_held": held,
            "codex_trust_revoked": untrusted,
            "note": f"what survives this teardown: {resume_hint}. "
                    "Worktrees with uncommitted changes are kept — "
                    "remove them by hand with `git worktree remove --force "
                    "<path>` once reviewed."}


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def add_build_args(p):
    p.add_argument("--from", dest="source", required=True,
                   help="directory of unit docs, one doc to split, or a source root")
    p.add_argument("--pattern", default="*.md", help="dir mode: filename glob")
    p.add_argument("--exclude", action="append", default=[], metavar="GLOB",
                   help="dir mode: drop units whose source files all match this "
                        "filename glob (repeatable), e.g. --exclude 'E1-*'")
    p.add_argument("--split-on", default=r"^#{1,2} ",
                   help="file mode: heading regex that starts a unit")
    p.add_argument("--scan-source", action="store_true",
                   help="derive units from buildable components under --from")
    p.add_argument("--max-units", type=int, default=30)
    p.add_argument("--limit", type=int, default=0, help="use only the first N units")
    p.add_argument("--workers", type=int, default=1, help="workers per manager")
    p.add_argument("--template", default="three-tier", choices=sorted(TEMPLATES),
                   help="the org chart to build: three-tier (ceo/manager/worker) "
                        "or epic (roadmap-manager/ceo/fleet-manager/worker)")
    p.add_argument("--mission", default="", help="one line: what this fleet is for")
    p.add_argument("--roadmap-dir", default="new_roadmap/fleet",
                   help="where each agent's own roadmap file lives (text only)")
    p.add_argument("--worktrees", action="store_true",
                   help="give every agent its own git worktree up front")
    p.add_argument("--base-branch", default="dev")
    p.add_argument("--new-tab", action="store_true",
                   help="give this fleet a tab of its own (closed by `down`)")
    p.add_argument("--ceo-here", action="store_true",
                   help="make THIS pane the head of the chain instead of opening a lane")
    p.add_argument("--agent", default="claude", choices=sorted(AGENTS),
                   help="which agent every pane runs: claude, codex, or rysh "
                        "(rysh's own in-daemon agent — no subprocess). One per "
                        "fleet; a mixed fleet is not supported")
    p.add_argument("--agent-args", default="",
                   help="launch args for the agent CLI; defaults per agent "
                        "(claude --dangerously-skip-permissions, "
                        "codex --dangerously-bypass-approvals-and-sandbox)")
    p.add_argument("--claude-args", default="--dangerously-skip-permissions",
                   help="the old spelling of --agent-args; still an override")
    p.add_argument("--force", action="store_true", help="ignore a spent token ceiling")
    p.add_argument("--comm", choices=COMMS, default=DEFAULT_COMM,
                   help="how agents message each other (designs 050/051). queue (default): "
                        "every message is a prompt on the fleet board and each agent's own "
                        "listener takes the ones addressed to it. grid: the daemon's courier "
                        "pushes each prompt into the recipient's inbox (polling-stream is its "
                        "old name). Both deliver beside the keyboard, never into it. "
                        "input-stream: typed into the recipient's pane (pre-050)")
    add_web_sync_arg(p)
    p.add_argument("--watch-policy", default="", choices=["", "smart", "always", "off"],
                   help="the fleet actor's check-ins (design 054): smart (default) pings only "
                        "silent/stalled/unblocked agents; always pings every agent every interval; "
                        "off observes and alerts but never pings")
    p.add_argument("--watch-interval", default="",
                   help="check-in interval, e.g. 10m (the daemon's default; floor 30s)")
    p.add_argument("--watch-stall", default="",
                   help="how long an agent may declare progress while its pane is idle "
                        "before it is stalled, e.g. 5m (default)")
    p.add_argument("--goal", default="",
                   help="the fleet goal the goal executor (design 055) holds the fleet to")
    p.add_argument("--goal-done-when", default="", help="how anyone can tell the goal is met")
    p.add_argument("--goal-due", default="", help="when the goal is due, e.g. 4h")
    p.add_argument("--goal-exec-mode", default="", choices=["", "adaptive", "fixed", "off"],
                   help="the goal executor: adaptive (default) looks every --goal-exec-min while "
                        "anything is wrong and backs off to --goal-exec-max; fixed; off")
    p.add_argument("--goal-exec-min", default="", help="e.g. 5m (default; floor 60s)")
    p.add_argument("--goal-exec-max", default="", help="e.g. 30m (default)")


def web_sync_section(man: dict, p: dict) -> str:
    """The brief paragraph that says who can see this agent's pane.

    Appended to every brief rather than templated, for the same reason as the
    global board: it depends on the fleet's --web-sync policy, and a brief that
    says "nobody watches your pane" to an agent whose pane is live — or the
    reverse — is a false fact in the prompt the agent trusts most.
    """
    fleet = man["fleet"]
    if web_sync_off(man, p):
        return (
            "\n\n## Nobody is watching this pane\n\n"
            "Your pane is **sync-paused in the human's browser** (`##pane sync off`): "
            "you keep running, but your screen is frozen there on purpose — a browser "
            "showing every agent of a fleet live stalls. The human follows this fleet "
            "through the head of the chain and the fleet board. So **nothing you only "
            "show on screen reaches anyone.** Every result, question and blocker goes "
            "into your end-of-turn report, a `report` up your chain, or a board post "
            f"(`--board {fleet}`). A question typed on screen and left waiting is a "
            "question nobody will answer. Do not turn your own web sync back on."
        )
    paused = [m for m in roster(man) if web_sync_off(man, m)]
    if paused and not p.get("parent"):
        return (
            "\n\n## The human watches your pane and the board — not the others\n\n"
            f"{len(paused)} of this fleet's panes are **sync-paused in the human's "
            "browser** (`##pane sync off`): they keep running, but their screens are "
            "frozen there on purpose, because that many live agents stall the web UI. "
            "The human follows the fleet through **your pane and the fleet board**. "
            "So anything the human must know reaches them through you or the board; "
            "a screen nobody is looking at is not a report. To look at an agent "
            f"yourself use `python3 {os.path.join(_HERE, 'fleetctl.py')} --fleet "
            f"{fleet} screen <who>` or its answer (`result` / `collect`). Turning "
            "their web sync back on is the human's call, not yours."
        )
    return ""


def add_web_sync_arg(p):
    p.add_argument("--web-sync", default="root", choices=WEB_SYNC_POLICIES,
                   help="which members stream repaints to web clients: root "
                        "(default — only the head of the chain; everyone below "
                        "launches with `##pane sync off`), all, or none")


def main() -> None:
    ap = argparse.ArgumentParser(prog="fleetctl", description=__doc__)
    ap.add_argument("--session")
    ap.add_argument("--fleet", default="fleet")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("discover")
    sub.add_parser("tabs")
    sub.add_parser("panes")
    sub.add_parser("fleets")

    e = sub.add_parser("epics", help="one fleet per epic doc; --promote N of them brought up")
    # dest="source", like every other --from in this file: discover_units reads
    # args.source. Found live 2026-08-11 — a second dest made the driver see an
    # empty path and refuse a directory that was right there.
    e.add_argument("--from", dest="source", required=True)
    # `epic*.md`, NOT `*.md`. Found live 2026-08-11: a directory of epics also
    # holds MERGE-NOTES.md and README.md, and the driver stood a whole fleet up
    # for one — a cap slot, an org chart and a board spent on a notes file.
    # `up` keeps the looser default because it is given a directory on purpose;
    # the driver picks its own units and must not pick furniture.
    e.add_argument("--pattern", default="[Ee]*.md")
    e.add_argument("--exclude", action="append", default=[], metavar="GLOB")
    e.add_argument("--split-on", default="")
    e.add_argument("--scan-source", action="store_true")
    e.add_argument("--max-units", type=int, default=30)
    e.add_argument("--limit", type=int, default=0, help="use only the first N epics")
    e.add_argument("--promote", type=int, default=3,
                   help="how many fleets this run brings up (a driver staging flag; there is no registry cap since R11)")
    e.add_argument("--template", default="epic", choices=sorted(TEMPLATES))
    e.add_argument("--workers", type=int, default=1)
    e.add_argument("--mission", default="")
    e.add_argument("--roadmap-dir", default="new_roadmap/fleet")
    e.add_argument("--worktrees", action="store_true")
    e.add_argument("--base-branch", default="dev")
    e.add_argument("--new-tab", action="store_true", default=True)
    e.add_argument("--ceo-here", action="store_true")
    e.add_argument("--agent", default="claude", choices=sorted(AGENTS))
    e.add_argument("--agent-args", default="")
    e.add_argument("--claude-args", default="--dangerously-skip-permissions")
    e.add_argument("--force", action="store_true")
    e.add_argument("--dry-run", action="store_true")
    e.add_argument("--source", default="")
    e.add_argument("--stacked", action="store_true")
    e.add_argument("--global-board", default="",
                   help="a board every fleet also posts to (e.g. `fleet`)")
    add_web_sync_arg(e)

    pr = sub.add_parser("epics-promote", help="bring the next waiting fleet up")
    for a in ("--pattern", "--split-on", "--roadmap-dir", "--base-branch",
              "--claude-args", "--agent-args", "--mission", "--source"):
        pr.add_argument(a, default={"--pattern": "*.md", "--roadmap-dir": "new_roadmap/fleet",
                                    "--base-branch": "dev",
                                    "--claude-args": "--dangerously-skip-permissions"}.get(a, ""))
    pr.add_argument("--agent", default="claude", choices=sorted(AGENTS))
    pr.add_argument("--from", dest="source", default="")
    pr.add_argument("--template", default="epic", choices=sorted(TEMPLATES))
    pr.add_argument("--workers", type=int, default=1)
    pr.add_argument("--max-units", type=int, default=30)
    pr.add_argument("--limit", type=int, default=0)
    pr.add_argument("--scan-source", action="store_true")
    pr.add_argument("--worktrees", action="store_true")
    pr.add_argument("--new-tab", action="store_true", default=True)
    pr.add_argument("--ceo-here", action="store_true")
    pr.add_argument("--force", action="store_true")
    pr.add_argument("--dry-run", action="store_true")
    pr.add_argument("--stacked", action="store_true")
    pr.add_argument("--global-board", default="")
    add_web_sync_arg(pr)

    p = sub.add_parser("units", help="show what would become units, and stop")
    add_build_args(p)

    p = sub.add_parser("up", help="build the fleet and start every claude")
    add_build_args(p)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--stacked", action="store_true",
                   help="put every role in ONE lane as a single stack, instead "
                        "of a lane per role; the fleet board keeps the right-most lane")
    p.add_argument("--global-board", default="",
                   help="a board every agent also posts to, besides its fleet's own")
    p.add_argument("--replace", action="store_true")

    q = sub.add_parser("reconcile", help="set each fleet's registry state from its live panes (dry run unless --apply)")
    q.add_argument("--apply", action="store_true",
                   help="write the changes; without it nothing is written")
    q = sub.add_parser("stamp-resume", help="arm the restart sweep on fleet claudes launched before R12 (dry run unless --apply)")
    q.add_argument("--apply", action="store_true",
                   help="write the stamps; without it nothing is written")
    sub.add_parser("tree", help="the org chart, live")
    sub.add_parser("verify", help="screen-check every agent actually came up")
    sub.add_parser("listen", help="queue messaging: start every member's board listener "
                                  "that is not running (idempotent)")

    p = sub.add_parser("msg", help="send down the chain (ceo->manager, manager->worker)")
    p.add_argument("to")
    # The body may be given adjacent to the target (`msg who 'text'`) or, when
    # options come first, as --text. argparse cannot intermix a variable-nargs
    # positional with options under a subparser, so `msg who --as human 'text'`
    # dies with "unrecognized arguments" unless --text is used.
    p.add_argument("body", nargs="?", default="")
    p.add_argument("--text", help="message body (use when options precede it)")
    p.add_argument("--file", help="path to a long body; the typed line points at it")
    p.add_argument("--as", dest="sender",
                   help="send as this member, or `human` to relay from the "
                        "keyboard (default: this pane)")
    p.add_argument("--kind", default="WORK ORDER")

    p = sub.add_parser("report", help="send up the chain to your parent")
    p.add_argument("body", nargs="?", default="")
    p.add_argument("--text", help="message body (use when options precede it)")
    p.add_argument("--file")
    p.add_argument("--as", dest="sender")
    p.add_argument("--kind", default="UPDATE")

    p = sub.add_parser("broadcast", help="one message to every member of a role")
    p.add_argument("role", choices=["manager", "worker"])
    p.add_argument("body", nargs="?", default="")
    p.add_argument("--text", help="message body (use when options precede it)")
    p.add_argument("--file")
    p.add_argument("--as", dest="sender")
    p.add_argument("--kind", default="WORK ORDER")

    q = sub.add_parser("status", help="the fleet ledger (design 054); with <who>, one "
                                      "member's ledger row plus what its screen shows")
    q.add_argument("who", nargs="?")
    for name in ("screen", "result"):
        q = sub.add_parser(name)
        q.add_argument("who")
        if name == "result":
            q.add_argument("--max-chars", type=int, default=4000)

    q = sub.add_parser("wait")
    q.add_argument("who")
    q.add_argument("--timeout", type=float, default=1800)

    q = sub.add_parser("collect", help="wait on several members and return every answer")
    q.add_argument("who", nargs="+")
    q.add_argument("--timeout", type=float, default=1800)
    q.add_argument("--max-chars", type=int, default=2000)

    q = sub.add_parser("commit", help="commit YOUR roadmap file, by path, text only")
    q.add_argument("-m", "--message", default="")
    q.add_argument("--as", dest="sender")

    q = sub.add_parser("worktree", help="create this agent's worktree on demand")
    q.add_argument("--as", dest="sender")
    q.add_argument("--base-branch", default="dev")

    q = sub.add_parser("subfleet", help="manager: open extra workers of your own")
    q.add_argument("--count", type=int, default=1)
    q.add_argument("--as", dest="sender")
    q.add_argument("--worktrees", action="store_true")
    q.add_argument("--base-branch", default="dev")
    # Empty on purpose: op_subfleet reads the FLEET's agent when this is unset,
    # so a manager in a codex fleet opens codex workers without re-typing it.
    q.add_argument("--agent", default="", choices=[""] + sorted(AGENTS))
    q.add_argument("--agent-args", default="")
    q.add_argument("--claude-args", default="--dangerously-skip-permissions")
    q.add_argument("--mission", default="")
    q.add_argument("--roadmap-dir", default="new_roadmap/fleet")

    q = sub.add_parser("down", help="close the fleet's panes")
    q.add_argument("--keep-ceo", action="store_true")
    q.add_argument("--keep-worktrees", action="store_true")

    args = ap.parse_args()
    env = R.discover(args.session)

    def out(x):
        print(x if isinstance(x, str) else json.dumps(x, indent=2))

    if args.cmd == "discover":
        return out(env)
    if args.cmd == "tabs":
        return out(tabs(env))
    if args.cmd == "panes":
        return out(R.list_panes(env))
    if args.cmd == "fleets":
        d = fleet_dir(env)
        return out([f[:-5] for f in sorted(os.listdir(d)) if f.endswith(".json")])
    if args.cmd == "units":
        return out(discover_units(args))
    if args.cmd == "epics":
        return out(op_epics(env, args))
    if args.cmd == "epics-promote":
        return out(op_epics_promote(env, args))
    if args.cmd == "up":
        res = op_up(env, args)
        out(res)
        # A failed launch exits non-zero: a driver reading the exit code must
        # not count a fleet whose panes vanished as one that came up. A launch
        # that succeeded but whose `up` the registry refused (a pre-R11 daemon)
        # exits 0 — the agents ARE running, and a retry would launch them twice.
        if not res.get("ok", True):
            raise SystemExit(3)
        return None
    if args.cmd == "reconcile":
        return out(op_reconcile(env, apply=args.apply))
    if args.cmd == "stamp-resume":
        return out(op_stamp_resume(env, apply=args.apply))

    # --file must name a REGULAR FILE that still exists. Checked here, before
    # the fleet is even resolved, because this is argument validation and a
    # message that cannot be read should fail at the earliest possible moment
    # rather than after a manifest write.
    #
    # WHY THIS GUARD EXISTS: fleetctl never reads the --file body. deliver()
    # records the path and tells the recipient "Read <path> in full". So
    # `--file /dev/stdin` (a heredoc, a pipe, a process substitution) records a
    # path whose content was never stored anywhere -- the sender's bytes are
    # consumed at send time, and the recipient's /dev/stdin is a different
    # stream entirely. The send then reports SUCCESS while delivering an
    # unreadable order.
    #
    # Not hypothetical: msg-0071, msg-0073 and msg-0075 were lost exactly this
    # way -- three consecutive work orders, no error at either end, the bodies
    # unrecoverable because they never existed on disk.
    #
    # Copying stdin to a temp file would also "work" and is deliberately NOT
    # what this does: a body that only ever existed in a pipe is invisible to
    # anyone auditing the fleet log afterwards, and the manifest should point at
    # something that still exists tomorrow.
    if getattr(args, "file", None):
        _bf = os.path.abspath(args.file)
        # Two separate checks, because ONE IS NOT ENOUGH and the live run proved
        # it. os.path.isfile("/dev/stdin") is False when stdin is a pipe -- but
        # TRUE on macOS when stdin comes from a heredoc/herestring, because bash
        # backs those with a temp file. That temp file is unlinked when the
        # sender exits, so the recipient still cannot read it: isfile() alone
        # lets the exact bug through in the most common shape.
        _process_local = ("/dev/", "/proc/self/fd/", "/proc/")
        if any(_bf.startswith(pfx) for pfx in _process_local):
            die(f"--file must be a real path on disk, got {args.file!r}. That is a "
                f"PROCESS-LOCAL stream: fleetctl records the path and the recipient "
                f"opens it later, by which time it is gone or belongs to a different "
                f"process, so the message is silently lost. Write the body to a real "
                f"file first, then pass that path.")
        if not os.path.isfile(_bf):
            die(f"--file must be a regular file that exists, got {args.file!r}. "
                f"A pipe or a deleted path is NOT persisted, so the recipient cannot "
                f"read it and the message is silently lost. Write the body to a real "
                f"file first, then pass that path.")

    man = load_manifest(env, args.fleet)

    if args.cmd == "tree":
        return out(op_tree(env, man))
    if args.cmd == "listen":
        if fleet_comm(man) != COMM_QUEUE:
            die(f"fleet {man['fleet']!r} uses {fleet_comm(man)} messaging; listeners are for "
                f"queue messaging only")
        started, running = [], []
        for p in roster(man):
            pid = listener_alive(env, p["pane"])
            if pid:
                running.append({"label": p["label"], "pid": pid})
            else:
                started.append({"label": p["label"], **start_listener(env, man, p["pane"])})
        return out({"fleet": man["fleet"], "running": running, "started": started})
    if args.cmd == "verify":
        return out(op_verify(env, man))
    if args.cmd in ("msg", "report", "broadcast"):
        args.body = getattr(args, "text", None) or args.body or ""
        sender = resolve(man, args.sender) if args.sender else me(env, man)
        body_file = os.path.abspath(args.file) if args.file else None
        if not body_file and not args.body:
            die("give a one-line body or --file <path>")
        if args.cmd == "msg":
            rcpt = resolve(man, args.to)
            if (rcpt["parent"] != sender["pane"]
                    and sender["role"] not in ("ceo", "human")):
                die(f"{sender['label']} is not {rcpt['label']}'s manager — "
                    f"messages go down your own chain. Use `report` to go up.")
            return out(deliver(env, man, sender, rcpt, args.body, args.kind, body_file))
        if args.cmd == "report":
            return out(deliver(env, man, sender, parent_of(man, sender),
                               args.body, args.kind, body_file))
        targets = [p for p in roster(man) if p["role"] == args.role
                   and (sender["role"] in ("ceo", "human")
                        or p["parent"] == sender["pane"])]
        return out([deliver(env, man, sender, t, args.body, args.kind, body_file)
                    for t in targets])
    if args.cmd == "status" and not args.who:
        return out(registry(env, "status", man["fleet"]))
    if args.cmd in ("status", "screen", "result", "wait"):
        who = resolve(man, args.who)
        pane = R.find_pane(R.list_panes(env), who["pane"])
        if args.cmd == "screen":
            scr = R.vt_screen(env, who["pane"])
            return out("\n".join(R.visible_solid(x) for x in scr.get("lines", [])))
        if args.cmd == "status":
            scr = R.vt_screen(env, who["pane"])
            lines = [R.visible_solid(x) for x in scr.get("lines", [])]
            ledger = registry(env, "status", man["fleet"])
            row = next((m.get("status") or {} for f in ledger.get("fleets", [])
                        for m in f.get("members", []) if m.get("pane_id") == who["pane"]),
                       {"error": ledger["error"]} if ledger.get("error") else {})
            return out({"label": who["label"], "role": who["role"],
                        "pane": who["pane"],
                        "state": "busy" if R.is_busy(lines) else
                                 ("idle" if R.has_composer(lines) else "no-tui"),
                        "ledger": row})
        if args.cmd == "result":
            r = R.op_result(env, R.list_panes(env), who["pane"])
            r["answer"] = R.clip(r.get("answer"), args.max_chars)
            r["label"] = who["label"]
            return out(r)
        r = R.op_wait(env, pane, args.timeout, settle=3)
        r["label"] = who["label"]
        return out(r)
    if args.cmd == "collect":
        res = []
        for ref in args.who:
            w = resolve(man, ref)
            pane = R.find_pane(R.list_panes(env), w["pane"])
            r = R.op_wait(env, pane, args.timeout, settle=3)
            r["answer"] = R.clip(r.get("answer"), args.max_chars)
            res.append({"label": w["label"], "role": w["role"], **r})
        return out(res)
    if args.cmd == "commit":
        who = resolve(man, args.sender) if args.sender else me(env, man)
        return out(op_commit(env, man, who, args.message))
    if args.cmd == "worktree":
        who = resolve(man, args.sender) if args.sender else me(env, man)
        wt = make_worktree(env, f"{man['fleet']}-{who['label']}", args.base_branch)
        who["worktree"] = wt.get("path")
        if wt.get("path"):
            set_meta(env, who["pane"], META_WORKTREE, wt["path"])
        save_manifest(env, man)
        return out(wt)
    if args.cmd == "subfleet":
        who = resolve(man, args.sender) if args.sender else me(env, man)
        return out(op_subfleet(env, man, who, args.count, args))
    if args.cmd == "down":
        r = op_down(env, man, args.keep_ceo, args.keep_worktrees)
        # The registry keeps the fleet and marks it DOWN rather than forgetting
        # it: the record is what says where the work was and which session ids
        # are still resumable (`claude -r`), and that is worth more after a
        # teardown than during one. Members drop out on the daemon's own
        # reconcile, because their panes have gone.
        r["registry"] = registry(env, "state", man["fleet"], "down")
        if not args.keep_ceo:
            os.replace(manifest_path(env, man["fleet"]),
                       manifest_path(env, man["fleet"]) + ".closed")
        return out(r)


if __name__ == "__main__":
    main()
