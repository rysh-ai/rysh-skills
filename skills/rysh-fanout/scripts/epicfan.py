#!/usr/bin/env python3
"""epicfan — ryshfan, but able to see across tabs.

Why this exists
---------------
``ryshfan`` resolves a pane by parsing ``##pane list``, and that listing is
TAB-SCOPED: it reports the tab of whichever pane the command ran in. A manager
claude living in the ``rysh-epics`` tab therefore cannot address — let alone
spawn into — its worker stack over in ``rysh-dev``::

    python3 ryshfan.py spawn --anchor <dev-pane-id>   # -> no pane with id ...

This wrapper patches exactly one function, ``ryshfan.list_panes``, so that it
returns the union of the panes in EVERY tab of the session. Everything built on
top of it — spawn, start, send, wait, result, close, children — then works on a
pane in any tab, unchanged. ``qualified()`` already emits ``--tab`` explicitly,
and every rysh selector resolves an id before an index, so cross-tab selectors
stay correct even as stacks are created underneath them.

Usage is ryshfan's, with the same subcommands::

    python3 epicfan.py panes                              # ALL tabs, not just ours
    python3 epicfan.py spawn --count 3 --anchor <pane>    # anchor may be in another tab
    python3 epicfan.py start <pane> --prompt-file f.md
    python3 epicfan.py wait <pane> --timeout 1800
    python3 epicfan.py result <pane>
    python3 epicfan.py send <pane> 'one-line follow-up'
    python3 epicfan.py close <pane>

Plus one addition ryshfan has no equivalent for::

    python3 epicfan.py tabs        # every tab: id, title, pane count
"""
from __future__ import annotations

import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ryshfan as R  # noqa: E402

TAB_ROW = re.compile(r"^\s*>?\s*\[(\d+)\]\s+(\S+)\s+id=(\S+)\s+panes=(\d+)")


def tabs(env: dict) -> list[dict]:
    """Every tab in the session. ``##tab list`` is the only view that is global."""
    out = R.rysh_exec(env, "##tab list")
    found = []
    for line in out.splitlines():
        m = TAB_ROW.match(line)
        if m:
            found.append(
                {
                    "index": int(m.group(1)),
                    "title": m.group(2),
                    "id": m.group(3),
                    "panes": int(m.group(4)),
                }
            )
    return found


_ORIGINAL_LIST_PANES = R.list_panes


def list_panes_all_tabs(env: dict, pane_id: str | None = None) -> list[dict]:
    """Union of every tab's panes.

    ``##pane list`` answers for the tab of the pane it ran in, so we ask once per
    tab, using a pane we already know lives there as the vantage point. The first
    call is the cheap one — it also tells us the tab we are already in.

    A caller that passes ``pane_id`` wants that pane's own tab specifically
    (ryshfan does this when it needs a single authoritative listing); honour it
    rather than widening, so the original semantics survive.
    """
    if pane_id:
        return _ORIGINAL_LIST_PANES(env, pane_id)

    here = _ORIGINAL_LIST_PANES(env)
    seen_tabs = {p["tab"] for p in here}
    merged = list(here)

    for tab in tabs(env):
        if tab["id"] in seen_tabs:
            continue
        # `##pane list --tab <id>` reports another tab without needing a pane in
        # it, which is the only way to bootstrap a tab we hold no handle on.
        out = R.rysh_exec(env, f"##pane list --tab {tab['id']} --meta")
        merged.extend(_parse_listing(out))
        seen_tabs.add(tab["id"])

    return merged


def _parse_listing(out: str) -> list[dict]:
    """Parse one ``##pane list`` block. Mirrors ryshfan.list_panes' parser.

    Kept as a separate function rather than reusing ryshfan's because that one
    couples parsing to the exec call; this takes text that is already in hand.
    """
    tab_title = tab_id = ""
    lane = group = 0
    panes: list[dict] = []
    for line in out.splitlines():
        m = R.TAB_LINE.search(line)
        if m:
            tab_title, tab_id = m.group(1), m.group(2)
            continue
        m = R.LANE_LINE.match(line)
        if m:
            lane, group = int(m.group(1)), 0
            continue
        m = R.GROUP_LINE.match(line)
        if m:
            group = int(m.group(1))
            continue
        m = R.PANE_LINE.match(line)
        if m:
            prog = R.RUNNING_RE.search(line)
            meta_pairs = {}
            for chunk in R.META_RE.findall(line):
                if "=" in chunk:
                    k, v = chunk.split("=", 1)
                    meta_pairs[k] = v
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
                    "program": prog.group(1) if prog else "",
                    "meta": meta_pairs,
                    "self": False,
                }
            )
    return panes


# The patch. Everything in ryshfan that resolves a pane goes through this.
R.list_panes = list_panes_all_tabs


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "tabs":
        env = R.discover(None)
        print(json.dumps(tabs(env), indent=2))
        return
    R.main()


if __name__ == "__main__":
    main()
