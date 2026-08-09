#!/usr/bin/env python3
"""fleetctl — build and drive a three-tier fleet of claude sessions in rysh.

    ceo  ──prompts──▶  managers  ──prompts──▶  workers
         ◀──reports──            ◀──reports──

One CEO pane, N manager panes stacked in one lane, N worker panes stacked in
another, paired 1:1 by unit. Every pane is a real interactive claude the user can
watch and take over — nothing here is headless `-p`.

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
import contextlib
import fcntl
import json
import os
import re
import subprocess
import sys
import time
import uuid

# --------------------------------------------------------------------------
# ryshfan is the substrate. Find it next door in the rysh-fanout skill.
# --------------------------------------------------------------------------

_HERE = os.path.dirname(os.path.abspath(__file__))
_CANDIDATES = [
    os.environ.get("RYSHFAN_DIR", ""),
    os.path.join(_HERE, "..", "..", "rysh-fanout", "scripts"),
    os.path.join(_HERE, "..", "..", "..", "skills", "rysh-fanout", "scripts"),
]
for _c in _CANDIDATES:
    if _c and os.path.isfile(os.path.join(_c, "ryshfan.py")):
        sys.path.insert(0, os.path.abspath(_c))
        break
try:
    import ryshfan as R  # noqa: E402
except ImportError:  # pragma: no cover
    sys.stderr.write(
        "fleetctl: cannot import ryshfan. It ships with the rysh-fanout skill; "
        "point RYSHFAN_DIR at the directory holding ryshfan.py.\n")
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
            meta = {}
            if mm := R.META_RE.search(line):
                for entry in mm.group(1).split(","):
                    key, _, value = entry.partition("=")
                    if key:
                        meta[key] = value
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
META_ROLE = "fleet.role"          # ceo | manager | worker
META_UNIT = "fleet.unit"
META_PARENT = "fleet.parent"      # pane id of the boss
META_ROADMAP = "fleet.roadmap"    # path to this agent's own roadmap file
META_WORKTREE = "fleet.worktree"
META_LABEL = "fleet.label"        # human name, same as the pane's given-name

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
    """One unit per heading section of a single document."""
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


def new_lane(env: dict, tab: str | None) -> dict:
    before = {p["id"] for p in R.list_panes(env)}
    R.rysh_exec(env, f"##new lane {tab}" if tab else "##new lane")
    return wait_for_new(env, before, 1)[0]


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


def start_claude(env: dict, pane: dict, prompt: str, cwd: str | None,
                 claude_args: str) -> dict:
    """Launch an interactive claude with the prompt as argv.

    argv, not typed: a prompt typed into a claude that is still booting is
    silently dropped, and there is no ready signal to wait for. The session id is
    pinned here because claude only reports the id it chose when it exits, which
    is exactly too late to follow a child that is still running.
    """
    m = R.SESSION_ID_ARG.search(claude_args)
    session_id = m.group(1) if m else str(uuid.uuid4())
    args = claude_args if m else f"--session-id {session_id} {claude_args}"
    path = os.path.join(R.fanout_dir(env),
                        f"fleet-{pane['id'][:8]}-{int(time.time())}.txt")
    with open(path, "w") as fh:
        fh.write(prompt)
    cd = f"cd {cwd} && " if cwd else ""
    out = R.cmd_to_pane(env, pane, f'{cd}{R.CLEAN_ENV} claude {args} "$(cat {path})"')
    return {"session_id": session_id, "prompt_file": path, "output": out.strip()}


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
    return (f"[FLEET {man['fleet']} | {kind} | FROM {sender['role']} "
            f"{sender['label']} (unit {sender.get('unit', '-')}, pane "
            f"{short(sender['pane'])}) | TO {recipient['role']} "
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
    out = fleet_send(env, recipient, line)
    rec = {"msg_id": msg_id, "kind": kind, "from": sender["label"],
           "to": recipient["label"], "body_file": body_file,
           "submitted": bool(out.get("ok", True)), "at": int(time.time()),
           "via": out.get("via", "type")}
    if out.get("fallback_from"):
        rec["fallback_from"] = out["fallback_from"]
        rec["ansa_error"] = out.get("ansa_error")
    # Append under the lock, again on a fresh read, so a concurrent sender's
    # record cannot be erased by writing back a copy that predates it.
    with manifest_txn(env, man["fleet"]) as fresh:
        fresh.setdefault("log", []).append(rec)
        man["log"] = fresh["log"]
        man["msg_seq"] = fresh.get("msg_seq", man.get("msg_seq"))
    # Mirror to the agents board (design 025 §4.2). LAST, and best-effort: the
    # message is already delivered and logged by this point, so nothing below
    # can cost us the control channel. See board_mirror's own docstring.
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


def ansa_enabled() -> bool:
    """ON by default since W4-3a. `RYSH_FLEET_ANSA=0` returns to typing.

    The flip, and ONLY the flip: the typing path is still here and still the
    fallback. Deleting it is W4-3b, which is separate code reviewed as its own
    change, because after it there is no second path to catch a mistake.

    The evidence for flipping is the ANSA path exercised ENABLED in 5 of 6 tests
    plus a live proof against a real binary that lacks the subcommand -- not the
    evidence for deleting, which does not exist yet and must not be inherited.
    """
    return os.environ.get("RYSH_FLEET_ANSA", "1") not in ("0", "false", "no")


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

    The fallback is the whole point. If ANSA fails we type instead, and the
    result says so -- `via` is always present, and `fallback_from` records what
    failed. An operator reading the manifest can tell a real ANSA delivery from
    one that quietly went the old way, which is the distinction that makes a
    migration auditable rather than hopeful.
    """
    if not ansa_enabled():
        pane = R.find_pane(R.list_panes(env), recipient["pane"])
        return {**R.op_send(env, pane, line), "via": "type"}

    res = ansa_send(env, recipient, line)
    if res.get("ok"):
        return res

    # ANSA failed. Fall back -- LOUDLY, never silently.
    try:
        pane = R.find_pane(R.list_panes(env), recipient["pane"])
        typed = R.op_send(env, pane, line)
    except Exception as exc:
        # Both paths are gone. Raising is correct: a work order that cannot be
        # delivered must not return a receipt.
        die(f"DELIVERY FAILED on both paths for {recipient['label']}: "
            f"ansa={res.get('error') or res.get('output')!r}; type={exc!r}")
    return {**typed, "via": "type", "fallback_from": "ansa",
            "ansa_error": res.get("error") or res.get("output")}


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
    out = [man["ceo"]]
    for u in man["units"]:
        out.append(u["manager"])
        out.extend(u["workers"])
    return out


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
    if ref in ("ceo", "CEO"):
        return man["ceo"]
    people = roster(man)
    for p in people:
        if ref in (p["pane"], p["label"]):
            return p
    m = re.fullmatch(r"(manager|mgr|worker|wkr)[:/-]?(\w+)", ref, re.I)
    if m:
        role = "manager" if m.group(1).lower() in ("manager", "mgr") else "worker"
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
    if member["role"] == "ceo":
        die("the CEO has no parent in the fleet; it reports to the human")
    return resolve(man, member["parent"])


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


# --------------------------------------------------------------------------
# up — build the whole fleet
# --------------------------------------------------------------------------

def op_up(env: dict, args) -> dict:
    units = discover_units(args)
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

    total = 1 + len(units) * (1 + args.workers)
    if args.dry_run:
        return {"fleet": fleet, "dry_run": True, "units": units,
                "panes_that_would_open": total,
                "roadmap_dir": args.roadmap_dir}

    # ---- panes -----------------------------------------------------------
    panes = R.list_panes(env)
    anchor = R.self_pane(panes) or R.active_pane(panes)
    tab = anchor["tab"]

    ceo_pane = anchor if args.ceo_here else new_lane(env, tab)
    mgr_head = new_lane(env, tab)
    mgr_panes = [mgr_head] + stack_onto(env, mgr_head, len(units) - 1)
    wkr_head = new_lane(env, tab)
    wkr_panes = [wkr_head] + stack_onto(env, wkr_head, len(units) * args.workers - 1)

    # Record the lanes we opened, by FULL uuid. Teardown needs them: a
    # per-pane `##pane delete` reports "deleted" for a pane in another lane and
    # silently does nothing, so the only teardown that works is deleting the
    # lane — and `##lane delete` accepts nothing but the full uuid, which
    # `##lane list`, `##panegroup list` and `##pane info` all truncate to eight
    # characters. `##lane info` run AS a pane in the lane is the one view that
    # prints it whole.
    lanes = {"manager": lane_uuid(env, mgr_head["id"]),
             "worker": lane_uuid(env, wkr_head["id"]),
             "ceo": None if args.ceo_here else lane_uuid(env, ceo_pane["id"])}

    # ---- identities ------------------------------------------------------
    taken: set[str] = {p["name"] for p in R.list_panes(env)}
    roadmap_dir = args.roadmap_dir
    os.makedirs(os.path.join(env["workspace"], roadmap_dir, "managers"), exist_ok=True)
    os.makedirs(os.path.join(env["workspace"], roadmap_dir, "workers"), exist_ok=True)

    ceo = {"role": "ceo", "unit": "-", "pane": ceo_pane["id"],
           "label": unique(taken, f"{fleet}-ceo"),
           "roadmap": os.path.join(roadmap_dir, "ceo.md"), "parent": None}

    man = {"fleet": fleet, "tab": tab, "created": int(time.time()),
           "workspace": env["workspace"], "roadmap_dir": roadmap_dir,
           "source": args.source, "ceo": ceo, "units": [], "msg_seq": 0,
           "lanes": lanes, "log": []}

    wi = 0
    for u, mp in zip(units, mgr_panes):
        s = slug(u["title"])
        mgr = {"role": "manager", "unit": u["unit"], "pane": mp["id"],
               "label": unique(taken, f"mgr-{u['unit']}-{s}"),
               "roadmap": os.path.join(roadmap_dir, "managers", f"unit-{u['unit']}.md"),
               "parent": ceo["pane"], "sources": u["sources"], "title": u["title"]}
        workers = []
        for k in range(args.workers):
            wp = wkr_panes[wi]; wi += 1
            workers.append({
                "role": "worker", "unit": u["unit"], "pane": wp["id"],
                "label": unique(taken, f"wkr-{u['unit']}-{s}" + (f"-{k+1}" if args.workers > 1 else "")),
                "roadmap": os.path.join(roadmap_dir, "workers",
                                        f"unit-{u['unit']}-w{k+1}.md"),
                "parent": mgr["pane"], "title": u["title"]})
        man["units"].append({"unit": u["unit"], "title": u["title"],
                             "sources": u["sources"], "manager": mgr,
                             "workers": workers})

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
        if p.get("parent"):
            set_meta(env, p["pane"], META_PARENT, p["parent"])
        if p.get("worktree"):
            set_meta(env, p["pane"], META_WORKTREE, p["worktree"])

    save_manifest(env, man)
    launched = []
    for p in roster(man):
        prompt = build_brief(env, man, p, args)
        res = start_claude(env, R.find_pane(R.list_panes(env), p["pane"]),
                           prompt, p.get("worktree"), args.claude_args)
        p["session_id"] = res["session_id"]
        R.set_meta(env, {"id": p["pane"]}, R.META_SESSION, res["session_id"])
        R.set_meta(env, {"id": p["pane"]}, R.META_TASK, p["label"])
        launched.append({"label": p["label"], "role": p["role"],
                         "pane": p["pane"], "session_id": res["session_id"]})
    save_manifest(env, man)

    return {"fleet": fleet, "units": len(units), "panes": total,
            "roadmap_dir": roadmap_dir, "manifest": manifest_path(env, fleet),
            "launched": launched,
            "next": f"fleetctl verify --fleet {fleet}   # judge launches by screen, not exit code"}


def build_brief(env: dict, man: dict, p: dict, args) -> str:
    fleet = man["fleet"]
    common = dict(
        FLEET=fleet, LABEL=p["label"], ROLE=p["role"], PANE=p["pane"],
        UNIT=p.get("unit", "-"), ROADMAP=p["roadmap"], WORKSPACE=env["workspace"],
        WORKTREE=p.get("worktree") or "(none yet — create one before any code work)",
        FLEETCTL=os.path.join(_HERE, "fleetctl.py"),
        PROTOCOL=os.path.abspath(os.path.join(_HERE, "..", "references", "protocol.md")),
        MISSION=args.mission or "(the human will state the mission in the first work order)",
    )
    if p["role"] == "ceo":
        rows = "\n".join(
            f"- unit `{u['unit']}` — **{u['title']}** — manager `{u['manager']['label']}` "
            f"(pane `{u['manager']['pane']}`), worker(s) "
            + ", ".join(f"`{w['label']}`" for w in u["workers"])
            for u in man["units"])
        return render("brief-ceo", ROSTER=rows, UNITS=len(man["units"]), **common)
    if p["role"] == "manager":
        unit = next(u for u in man["units"] if u["manager"]["pane"] == p["pane"])
        wrows = "\n".join(
            f"- `{w['label']}` — pane `{w['pane']}` — roadmap `{w['roadmap']}`"
            for w in unit["workers"])
        srcs = "\n".join(f"- `{os.path.relpath(s, env['workspace'])}`"
                         for s in unit["sources"])
        return render("brief-manager", TITLE=unit["title"], SOURCES=srcs,
                      WORKERS=wrows, CEO=man["ceo"]["label"],
                      CEO_PANE=man["ceo"]["pane"], **common)
    unit = next(u for u in man["units"]
                if any(w["pane"] == p["pane"] for w in u["workers"]))
    srcs = "\n".join(f"- `{os.path.relpath(s, env['workspace'])}`"
                     for s in unit["sources"])
    return render("brief-worker", TITLE=unit["title"], SOURCES=srcs,
                  MANAGER=unit["manager"]["label"],
                  MANAGER_PANE=unit["manager"]["pane"], **common)


# --------------------------------------------------------------------------
# Operations
# --------------------------------------------------------------------------

def op_tree(env: dict, man: dict) -> str:
    panes = {p["id"]: p for p in R.list_panes(env)}

    def mark(m):
        live = panes.get(m["pane"], {}).get("program") or ""
        return "●" if live else "○"

    out = [f"fleet {man['fleet']}  ({len(man['units'])} units, "
           f"{len(roster(man))} agents)  roadmaps: {man['roadmap_dir']}",
           f"{mark(man['ceo'])} ceo  {man['ceo']['label']}  {short(man['ceo']['pane'])}"]
    for u in man["units"]:
        m = u["manager"]
        out.append(f"  {mark(m)} mgr  [{u['unit']}] {m['label']}  {short(m['pane'])}"
                   f"   {u['title'][:52]}")
        for w in u["workers"]:
            out.append(f"      {mark(w)} wkr  {w['label']}  {short(w['pane'])}")
    out.append("● running   ○ no program")
    return "\n".join(out)


def op_verify(env: dict, man: dict) -> dict:
    """Judge a launch by the pane's screen, never by an exit code.

    `start` returns 0 whether claude came up or died on a bad flag; the screen is
    the only honest signal.
    """
    rows = []
    for p in roster(man):
        pane = R.find_pane_opt(R.list_panes(env), p["pane"])
        if not pane:
            rows.append({"label": p["label"], "state": "pane-gone"})
            continue
        try:
            scr = R.vt_screen(env, p["pane"])
            lines = [R.visible_solid(x) for x in scr.get("lines", [])]
            state = "busy" if R.is_busy(lines) else (
                "idle" if R.has_composer(lines) else "no-tui")
        except Exception as exc:
            state = f"unreadable: {exc}"
        rows.append({"label": p["label"], "role": p["role"],
                     "pane": short(p["pane"]),
                     "program": pane.get("program") or "", "state": state})
    bad = [r for r in rows if r.get("state") in ("no-tui", "pane-gone")]
    return {"fleet": man["fleet"], "agents": rows, "not_running": bad,
            "ok": not bad}


def op_commit(env: dict, man: dict, member: dict, message: str) -> dict:
    """Commit only this agent's own roadmap file, by path.

    Path-limited so a shared file dirtied by a sibling is never swept in, and
    text-only because a fleet that commits renders and captures turns the repo
    into an artifact store.
    """
    rel = member["roadmap"]
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
            return {"committed": True, "sha": sha, "file": rel, "root": root,
                    "branch": branch, "subject": subject,
                    "attempts": attempt + 1}
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
        if args.worktrees:
            wt = make_worktree(env, f"{man['fleet']}-{w['label']}", args.base_branch)
            w["worktree"] = wt.get("path")
        unit["workers"].append(w)
        prompt = build_brief(env, man, w, args)
        res = start_claude(env, R.find_pane(R.list_panes(env), w["pane"]),
                           prompt, w.get("worktree"), args.claude_args)
        w["session_id"] = res["session_id"]
        R.set_meta(env, {"id": w["pane"]}, R.META_SESSION, res["session_id"])
        added.append({"label": w["label"], "pane": w["pane"]})
    save_manifest(env, man)
    return {"fleet": man["fleet"], "unit": unit["unit"], "added": added}


def op_down(env: dict, man: dict, keep_ceo: bool, keep_worktrees: bool) -> dict:
    closed, kept = [], []
    for p in roster(man):
        if keep_ceo and p["role"] == "ceo":
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
            role = "ceo" if p["role"] == "ceo" else (
                "manager" if p["role"] == "manager" else "worker")
            if role not in known and p["pane"] in live:
                known[role] = lane_uuid(env, p["pane"])
    for role, lane_id in known.items():
        if not lane_id or (keep_ceo and role == "ceo"):
            continue
        out = R.rysh_exec(env, f"##lane delete {lane_id}")
        (lanes_gone if "deleted" in out else lanes_left).append(
            {"role": role, "lane": lane_id, "output": out.strip()})
    survivors = [p["label"] for p in roster(man)
                 if R.find_pane_opt(R.list_panes(env), p["pane"])
                 and not (keep_ceo and p["role"] == "ceo")]
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
    return {"fleet": man["fleet"], "closed": closed, "kept": kept,
            "lanes_deleted": lanes_gone, "lanes_left": lanes_left,
            "panes_surviving": survivors,
            "worktrees_removed": removed, "worktrees_held": held,
            "note": "sessions are pinned: `claude -r <session_id>` from the "
                    "agent's working directory resumes any of them. Worktrees "
                    "with uncommitted changes are kept — remove them by hand "
                    "with `git worktree remove --force <path>` once reviewed."}


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def add_build_args(p):
    p.add_argument("--from", dest="source", required=True,
                   help="directory of unit docs, one doc to split, or a source root")
    p.add_argument("--pattern", default="*.md", help="dir mode: filename glob")
    p.add_argument("--split-on", default=r"^#{1,2} ",
                   help="file mode: heading regex that starts a unit")
    p.add_argument("--scan-source", action="store_true",
                   help="derive units from buildable components under --from")
    p.add_argument("--max-units", type=int, default=30)
    p.add_argument("--limit", type=int, default=0, help="use only the first N units")
    p.add_argument("--workers", type=int, default=1, help="workers per manager")
    p.add_argument("--mission", default="", help="one line: what this fleet is for")
    p.add_argument("--roadmap-dir", default="new_roadmap/fleet",
                   help="where each agent's own roadmap file lives (text only)")
    p.add_argument("--worktrees", action="store_true",
                   help="give every agent its own git worktree up front")
    p.add_argument("--base-branch", default="dev")
    p.add_argument("--ceo-here", action="store_true",
                   help="make THIS pane the CEO instead of opening a new lane")
    p.add_argument("--claude-args", default="--dangerously-skip-permissions")
    p.add_argument("--force", action="store_true", help="ignore a spent token ceiling")


def main() -> None:
    ap = argparse.ArgumentParser(prog="fleetctl", description=__doc__)
    ap.add_argument("--session")
    ap.add_argument("--fleet", default="fleet")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("discover")
    sub.add_parser("tabs")
    sub.add_parser("panes")
    sub.add_parser("fleets")

    p = sub.add_parser("units", help="show what would become units, and stop")
    add_build_args(p)

    p = sub.add_parser("up", help="build the fleet and start every claude")
    add_build_args(p)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--replace", action="store_true")

    sub.add_parser("tree", help="the org chart, live")
    sub.add_parser("verify", help="screen-check every agent actually came up")

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

    for name in ("status", "screen", "result"):
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
    if args.cmd == "up":
        return out(op_up(env, args))

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
    if args.cmd in ("status", "screen", "result", "wait"):
        who = resolve(man, args.who)
        pane = R.find_pane(R.list_panes(env), who["pane"])
        if args.cmd == "screen":
            scr = R.vt_screen(env, who["pane"])
            return out("\n".join(R.visible_solid(x) for x in scr.get("lines", [])))
        if args.cmd == "status":
            scr = R.vt_screen(env, who["pane"])
            lines = [R.visible_solid(x) for x in scr.get("lines", [])]
            return out({"label": who["label"], "role": who["role"],
                        "pane": who["pane"],
                        "state": "busy" if R.is_busy(lines) else
                                 ("idle" if R.has_composer(lines) else "no-tui")})
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
        if not args.keep_ceo:
            os.replace(manifest_path(env, man["fleet"]),
                       manifest_path(env, man["fleet"]) + ".closed")
        return out(r)


if __name__ == "__main__":
    main()
