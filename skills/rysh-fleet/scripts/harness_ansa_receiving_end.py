#!/usr/bin/env python3
"""E9 / Q1: prove ANSA delivery at the RECEIVING end, against a live daemon.

    python3 scripts/harness_ansa_receiving_end.py --session <name> --pane <pane-id>

WHY THIS IS NOT A UNIT TEST. Everything in test_fleetctl_via_audit.py is a claim
about what fleetctl RECORDS. This is the other half, and it cannot be stubbed:
did the bytes actually land on the recipient's screen? A harness, not a test,
because it needs a live daemon and a real scratch pane -- so it is run by hand
and its output pasted, rather than run in a suite that would fail on any machine
without a session.

THE RULE IT ENFORCES: `fleetctl` exiting 0 is SENDING-END evidence and proves
nothing. The cutover that motivated all of this failed exactly here -- a binary
with no `ansa` subcommand returned success while every message silently took a
path that has since been deleted. So this harness reads the recipient pane's VT
screen back over NATS and looks for a nonce it just sent. Both facts are printed
side by side, because the interesting case is when they disagree.

Point it at a SCRATCH pane you own. Sending to a sibling agent's pane injects a
turn into somebody else's session.
"""

import argparse
import os
import sys
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
FANOUT = os.path.abspath(os.path.join(HERE, "..", "..", "rysh-fanout", "scripts"))
sys.path.insert(0, HERE)
sys.path.insert(0, FANOUT)
os.environ.setdefault("RYSHFAN_DIR", FANOUT)

import ryshfan          # noqa: E402  live substrate: NATS + daemon
import fleetctl         # noqa: E402


def screen_text(env: dict, pane_id: str) -> str:
    vt = ryshfan.vt_screen(env, pane_id)
    return "\n".join(vt.get("lines") or [])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", required=True)
    ap.add_argument("--pane", required=True, help="a SCRATCH pane you own")
    ap.add_argument("--bin", default=os.path.abspath(os.path.join(
        HERE, "..", "..", "..", "..", "rysh-cli-app", "sidecar", "rysh-darwin-arm64")))
    ap.add_argument("--settle", type=float, default=2.0)
    args = ap.parse_args()

    # discover() resolves the LIVE daemon rather than assuming a port: the
    # session under test must be the one actually running, or the VT read below
    # would query a different daemon and report a clean screen as a failure.
    env = dict(ryshfan.discover(args.session))
    env["bin"] = args.bin
    rcpt = {"role": "scratch", "unit": "-", "pane": args.pane, "label": "scratch"}

    # A nonce, so a hit cannot be scrollback from an earlier run. This is the
    # whole reason the assertion is trustworthy: the string did not exist
    # anywhere in the world until this line ran.
    nonce = f"E9-Q1-{uuid.uuid4().hex[:12]}"
    # Self-describing, because the only panes that can witness a delivery are
    # panes running an agent -- so this line WILL be read by one. It says what it
    # is, so a recipient does not act on a probe.
    line = (f"E9-Q1 ANSA receiving-end probe {nonce} "
            f"-- test artifact, no action required, do not reply")

    # A pane with no live terminal returns an EMPTY VT snapshot, and an empty
    # screen looks exactly like a message that never arrived. Measured 2026-08-12:
    # a freshly created pane, and the fleet's own board pane, both report
    # interactive=False with zero lines, while every pane running claude reports
    # a full screen. So refuse to run rather than emit a FAIL that would be read
    # as "ANSA is broken" -- absence of evidence from a screen that cannot draw
    # is not evidence of absence.
    pre = ryshfan.vt_screen(env, args.pane)
    if not [ln for ln in (pre.get("lines") or []) if ln.strip()]:
        print(f"UNUSABLE TARGET: pane {args.pane} has an empty VT snapshot "
              f"(interactive={pre.get('interactive')}). A pane with no live "
              f"terminal cannot witness a delivery. Point this at a pane running "
              f"an interactive program; nothing was sent.")
        return 2

    before = screen_text(env, args.pane)
    if nonce in before:
        print(f"FAIL: nonce {nonce} was on screen BEFORE sending -- impossible; abort")
        return 2

    # NEGATIVE CONTROL, run before the real send. A screen reader that returned
    # the query, or an `in` test against the wrong buffer, would make the final
    # assertion pass no matter what. This nonce is never sent, so it must never
    # be found.
    never_sent = f"E9-Q1-NEVERSENT-{uuid.uuid4().hex[:12]}"

    fleetctl._ansa_capable = None            # force a real capability probe
    out = fleetctl.fleet_send(env, rcpt, line)   # the REAL delivery path
    sending_end_ok = bool(out.get("ok"))

    time.sleep(args.settle)
    after = screen_text(env, args.pane)

    arrived = nonce in after
    control_clean = never_sent not in after

    print("-" * 70)
    print(f"pane            : {args.pane}")
    print(f"nonce           : {nonce}")
    print(f"SENDING end     : fleet_send ok={sending_end_ok} via={out.get('via')!r} "
          f"rc={out.get('rc')}   <- proves nothing on its own")
    print(f"RECEIVING end   : nonce on recipient VT screen = {arrived}   <- the evidence")
    print(f"negative control: unsent nonce absent = {control_clean}")
    print("-" * 70)
    tail = [ln for ln in after.splitlines() if ln.strip()][-6:]
    print("recipient screen tail:")
    for ln in tail:
        print(f"  | {ln}")
    print("-" * 70)

    if arrived and control_clean:
        print("PASS: bytes verified at the receiving end, and via recorded "
              f"{out.get('via')!r}")
        return 0
    print("FAIL: delivery not confirmed at the receiving end")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
