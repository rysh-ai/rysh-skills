#!/usr/bin/env python3
"""The harness that found `F-22`. Preserved so the fix can be judged against it.

    python3 harness_fleetctl_concurrency.py --fleet <name> --sender <label> \
        --recipients demo-w1,demo-w2 [--procs 12] [--ansa]

WHY THIS IS A FILE AND NOT A COMMAND SOMEBODY REMEMBERS
-------------------------------------------------------
`F-22`'s definition of done says the fix must be verified under concurrency
**against the same harness that found the bug**, not a new one written to suit
the fix. That requirement is unmeetable if the harness lives in /tmp and dies
with the session that wrote it — a rule that lives in a message dies with the
conversation, and so does apparatus.

WHAT IT DOES
------------
Fires N `fleetctl msg` processes at once, then inspects the manifest. It asserts
nothing and fixes nothing: it reports, because the interesting outcomes are the
ones nobody predicted.

WHAT IT FOUND (2026-08-09, 12 processes, isolated 5-pane demo fleet)
--------------------------------------------------------------------
    processes reporting delivered : 12 of 12
    records in manifest log       : 1
    msg_id collisions             : {'msg-0001': 12}
    manifest msg_seq              : 1

All twelve minted `msg-0001`; eleven records were lost; every process reported
success; and the messages really did arrive (verified by reading a recipient's
pane, not by trusting the receipt). Delivery WITHOUT a record — the
receipt-without-delivery failure inverted, which makes it the same disease.

Cause: `deliver()` load-modify-saves the whole manifest. `next_msg_id()`
increments an in-memory copy and `save_manifest()` rewrites the file wholesale,
so twelve readers of `msg_seq=0` all mint `msg-0001` and the last writer wins.

RUN THE CONTROL. IT IS THE POINT.
---------------------------------
Run it BOTH with and without `--ansa`. The identical result on both paths is
what proved this is a pre-existing fleetctl defect rather than something ANSA
introduced — and that distinction is the difference between "W4 is blocked" and
"W4 is fine, but the log has a hole". A finding without a control is a
suspicion.

NEVER RUN THIS AGAINST A FLEET YOU CARE ABOUT. It sends real messages to real
panes. Use an isolated session, and remember that RYSH_* env vars from the
operator's own pane override rysh.config.yaml (design 025 §8a) -- scrub them, or
you will point this at the live bus.
"""

import argparse
import collections
import glob
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
FLEETCTL = os.path.join(HERE, "fleetctl.py")

# The RYSH_* vars that leak from an operator's pane and silently re-point a
# subprocess at the live session.
SCRUB = ("RYSH_SESSION", "RYSH_NATS_PORT", "RYSH_NATS_MODE", "RYSH_NATS_DATA_DIR",
         "RYSH_PANE", "RYSH_TAB", "RYSH_LANE", "RYSH_STACK", "RYSH_SESSION_SOURCE")


def child_env(session: str, ansa: bool) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in SCRUB}
    env["RYSH_SESSION"] = session
    env["RYSH_FLEET_ANSA"] = "1" if ansa else "0"
    env.setdefault("RYSHFAN_DIR",
                   os.path.join(HERE, "..", "..", "rysh-fanout", "scripts"))
    return env


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fleet", required=True)
    ap.add_argument("--session", required=True)
    ap.add_argument("--sender", required=True)
    ap.add_argument("--recipients", required=True,
                    help="comma-separated labels; messages round-robin across them")
    ap.add_argument("--procs", type=int, default=12)
    ap.add_argument("--ansa", action="store_true", help="deliver via ANSA (else the typing path)")
    ap.add_argument("--manifest", required=True, help="path to .rysh/fleet/<name>.json")
    args = ap.parse_args()

    rcpts = [r.strip() for r in args.recipients.split(",") if r.strip()]
    outdir = tempfile.mkdtemp(prefix="f22-")
    env = child_env(args.session, args.ansa)

    procs = []
    for i in range(1, args.procs + 1):
        to = rcpts[i % len(rcpts)]
        out = open(os.path.join(outdir, f"out-{i:03d}.json"), "w")
        procs.append(subprocess.Popen(
            [sys.executable, FLEETCTL, "--fleet", args.fleet, "msg", to,
             "--as", args.sender, "--text", f"concurrent-{i}"],
            stdout=out, stderr=subprocess.STDOUT, env=env))
    for p in procs:
        p.wait()

    reported = []
    for f in sorted(glob.glob(os.path.join(outdir, "out-*.json"))):
        try:
            d = json.load(open(f))
            if d.get("submitted"):
                reported.append(d.get("msg_id"))
        except Exception:
            pass

    print(f"path                          : {'ansa' if args.ansa else 'type'}")
    print(f"processes reporting delivered : {len(reported)} of {args.procs}")
    try:
        man = json.load(open(args.manifest))
    except Exception as exc:
        # A manifest that will not parse is the OTHER half of F-22:
        # save_manifest truncates before it writes, so a process dying mid-dump
        # corrupts rather than staling the file.
        print(f"manifest parses               : NO -> {exc}")
        return 1
    log = man.get("log", [])
    ids = [r.get("msg_id") for r in log]
    dupes = {k: v for k, v in collections.Counter(reported).items() if v > 1}
    print(f"manifest parses               : YES")
    print(f"records in manifest log       : {len(log)}")
    print(f"manifest msg_seq              : {man.get('msg_seq')}")
    print(f"msg_id collisions (reported)  : {dupes or 'none'}")
    print(f"receipts with no log record   : {sorted(set(reported) - set(ids)) or 'none'}")
    print(f"\nraw process output kept in    : {outdir}")
    print("\nRUN THE CONTROL: repeat with --ansa toggled. Identical results on both "
          "paths mean the defect is fleetctl's, not the delivery layer's.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
