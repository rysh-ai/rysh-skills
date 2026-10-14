#!/usr/bin/env python3
"""reviewpool — pick a reviewer at random for a task, and talk to that reviewer.

A fleet with ONE reviewer (the `ceo` role) makes that reviewer the bottleneck:
every unit waits in one queue. reviewpool spreads review across every member
whose role is `ceo` (the review pool). The choice is made HERE, in code, with
the OS random source, never by an agent's judgement.

STICKY PER TASK. The first `pick` for a task id chooses a reviewer at random and
records it. Every later `pick`/`send` for the same task returns the SAME
reviewer, so a resubmission after a REJECT goes back to the reviewer who wrote
the findings. `--reroll` forces a new random choice (e.g. the reviewer is gone).

    reviewpool.py --fleet shop pick    --task T-62
    reviewpool.py --fleet shop send    --task T-62 --file /abs/path/unit.md
    reviewpool.py --fleet shop send    --task T-62 --text 'one line'
    reviewpool.py --fleet shop list
    reviewpool.py --fleet shop release --task T-62      # after ACCEPT

Output is JSON. `pick` and `send` print the reviewer's label and pane id. That
is the identity to use in the envelope and in board posts ("→ with <label>").

`send` delivers through fleetctl's own deliver(), so the envelope reads
FROM <you> TO <reviewer> exactly like `fleetctl report`. It exists because
`fleetctl report` can only reach your ONE registered parent. Reviewers answer
with `fleetctl msg <you>` (the ceo role may message down to anyone) and report
up to their own parent as before.

State: <fleet dir>/<fleet>.reviewpool.json, read-modify-written under an
exclusive lock, so two concurrent picks for one task cannot pick twice.
stdlib only.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as _dt
import fcntl
import json
import os
import secrets
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fleetctl as FC  # noqa: E402  (same directory; reuses manifest + deliver)

REVIEWER_ROLE = "ceo"
_rng = secrets.SystemRandom()


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def state_path(env: dict, fleet: str) -> str:
    return os.path.join(FC.fleet_dir(env), f"{fleet}.reviewpool.json")


@contextlib.contextmanager
def state_txn(env: dict, fleet: str):
    """Exclusive read-modify-write of the assignment file (same pattern as
    fleetctl.manifest_txn: the READ is inside the lock)."""
    path = state_path(env, fleet)
    fd = os.open(path + ".lock", os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        st = {"fleet": fleet, "assignments": {}, "log": []}
        if os.path.isfile(path):
            with open(path) as fh:
                st = json.load(fh)
        yield st
        tmp = f"{path}.tmp.{os.getpid()}"
        with open(tmp, "w") as fh:
            json.dump(st, fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def reviewers(man: dict) -> list[dict]:
    pool = [m for m in FC.roster(man) if m.get("role") == REVIEWER_ROLE]
    if not pool:
        FC.die(f"fleet {man['fleet']!r} has no member with role {REVIEWER_ROLE!r}")
    return pool


def _ident(m: dict) -> dict:
    return {"label": m["label"], "pane": m["pane"]}


def pick(env: dict, man: dict, task: str, reroll: bool = False) -> dict:
    task = task.strip()
    if not task:
        FC.die("--task must not be empty")
    pool = reviewers(man)
    by_pane = {m["pane"]: m for m in pool}
    with state_txn(env, man["fleet"]) as st:
        cur = st["assignments"].get(task)
        if cur and not reroll and cur["pane"] in by_pane:
            return {"task": task, "new": False, **_ident(by_pane[cur["pane"]]),
                    "assigned_at": cur["assigned_at"], "pool_size": len(pool)}
        choices = pool
        if cur and len(pool) > 1:   # a reroll must actually move the task
            choices = [m for m in pool if m["pane"] != cur["pane"]] or pool
        chosen = _rng.choice(choices)
        rec = {**_ident(chosen), "assigned_at": _now()}
        st["assignments"][task] = rec
        st["log"].append({"at": rec["assigned_at"], "task": task,
                          "event": "reroll" if cur else "pick", **_ident(chosen)})
        return {"task": task, "new": True, **_ident(chosen),
                "assigned_at": rec["assigned_at"], "pool_size": len(pool)}


def release(env: dict, man: dict, task: str) -> dict:
    with state_txn(env, man["fleet"]) as st:
        rec = st["assignments"].pop(task, None)
        st["log"].append({"at": _now(), "task": task, "event": "release",
                          **({k: rec[k] for k in ("label", "pane")} if rec else {})})
    return {"task": task, "released": bool(rec), "was": rec}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="reviewpool", description=__doc__.split("\n")[0])
    ap.add_argument("--session")
    ap.add_argument("--fleet", required=True)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("pick", help="random reviewer for a task (sticky)")
    p.add_argument("--task", required=True)
    p.add_argument("--reroll", action="store_true")
    s = sub.add_parser("send", help="pick, then deliver the unit to that reviewer")
    s.add_argument("--task", required=True)
    s.add_argument("--file")
    s.add_argument("--text")
    s.add_argument("--kind", default="REVIEW")
    s.add_argument("--as", dest="sender", help="sender label/pane (default: this pane)")
    s.add_argument("--reroll", action="store_true")
    sub.add_parser("list", help="current assignments")
    r = sub.add_parser("release", help="drop a task's assignment (after ACCEPT)")
    r.add_argument("--task", required=True)
    sub.add_parser("pool", help="the reviewers in the pool")
    a = ap.parse_args(argv)

    env = FC.R.discover(a.session)
    man = FC.load_manifest(env, a.fleet)

    if a.cmd == "pool":
        res = [_ident(m) for m in reviewers(man)]
    elif a.cmd == "pick":
        res = pick(env, man, a.task, a.reroll)
    elif a.cmd == "list":
        with state_txn(env, man["fleet"]) as st:
            res = st["assignments"]
    elif a.cmd == "release":
        res = release(env, man, a.task)
    else:  # send
        if bool(a.file) == bool(a.text):
            FC.die("give exactly one of --file <abs path> or --text '<one line>'")
        body_file = None
        if a.file:
            body_file = os.path.abspath(a.file)
            if not os.path.isfile(body_file) or body_file.startswith(("/dev/", "/proc/")):
                FC.die(f"--file must be a regular file on disk, got {a.file!r}")
        sender = FC.resolve(man, a.sender) if a.sender else FC.me(env, man)
        chosen = pick(env, man, a.task, a.reroll)
        rcpt = FC.resolve(man, chosen["pane"])
        body = a.text or ""
        prefix = f"[review task {a.task}] "
        if body:
            body = prefix + body
        res = {"reviewer": chosen,
               "delivery": FC.deliver(env, man, sender, rcpt, body, a.kind, body_file)}
    print(json.dumps(res, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
