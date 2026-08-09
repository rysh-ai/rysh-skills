---
name: rysh-fleet
description: Build and drive a three-tier fleet of claude sessions in a rysh session — one CEO, N managers, N workers, paired by unit, each in its own pane and its own worktree, talking to each other with a from-me envelope. Use when the user types "/rysh-fleet" or asks to stand up a fleet of claudes, run an epic/module/component per agent, create manager and worker claudes, build an org chart of agents, fan a roadmap out across many panes, or drive/inspect/tear down a fleet that already exists. Units come from a directory of docs, one doc split by heading, or from the shape of the codebase.
version: 1.0.0
argument-hint: "up --from <dir|file> [--workers N] | tree | msg <who> | broadcast manager | collect <who…> | down"
---

$ARGUMENTS

# /rysh-fleet — an org chart of claude sessions

```
        human
          │
         ceo            one pane, its own lane
          │  work orders down ↓        ↑ reports up
      managers          one lane, stacked, one per unit
          │  work orders down ↓        ↑ reports up
       workers          one lane, stacked, one or more per manager
```

Every node is a **real interactive claude** in a rysh pane — the user can watch it,
type into it, or take it over. Nothing here is headless `-p`.

**Helper:** `scripts/fleetctl.py` — stdlib only. It builds on `ryshfan` from the
**rysh-fanout** skill (required, sits next door) and adds three things fanout has
no answer for: cross-tab pane addressing, a persistent org chart, and a message
envelope so every agent knows who is talking to it.

```sh
F=.claude/skills/rysh-fleet/scripts/fleetctl.py
python3 $F --fleet <name> <subcommand>
```

Everything prints JSON except `tree` and `screen`.

---

## Standing one up

```sh
# 0. see what would become units — always do this first
python3 $F units --from new_roadmap/tracks/fleet            # a directory of docs
python3 $F units --from ROADMAP.md --split-on '^## '        # one doc, split by heading
python3 $F units --from . --scan-source                     # from the codebase itself

# 1. build it (add --dry-run to count panes without opening any)
python3 $F --fleet epics up --from new_roadmap/tracks/fleet \
    --workers 1 --worktrees --mission 'ship the launch'

# 2. verify — start returns 0 even when a launch failed
python3 $F --fleet epics verify
python3 $F --fleet epics tree
```

`up` opens three lanes in the current tab (CEO, managers, workers), names every
pane after its role and unit, writes the org chart onto the panes as `##pane meta`,
optionally creates a worktree per agent, and starts a claude in each with a brief
that already knows its own pane id, its parent, its children and its roadmap file.

**Unit discovery, three ways.** A directory (one unit per file, files sharing a
`<word><number>` prefix merged into one unit — `epic07-pitch.md` + `epic07-memo.md`
is one unit, not two); one document split on a heading regex; or `--scan-source`,
which walks the tree for buildable components (`go.mod`, `package.json`,
`pyproject.toml`, `Cargo.toml`, …) and makes one unit per module.

Key flags: `--workers N` (per manager) · `--worktrees` + `--base-branch` ·
`--roadmap-dir` (default `new_roadmap/fleet`) · `--mission` · `--limit N` ·
`--ceo-here` (make the calling pane the CEO instead of opening a lane) ·
`--claude-args`.

---

## Driving it

```sh
python3 $F --fleet epics msg mgr-04 --file /tmp/order.md      # down the chain
python3 $F --fleet epics broadcast manager --file /tmp/all.md # one order to every manager
python3 $F --fleet epics report 'BLOCKED: no credentials'     # up the chain, from a pane
python3 $F --fleet epics collect mgr-01 mgr-02 --timeout 1800 # wait on several, get answers
python3 $F --fleet epics status wkr-04 | screen wkr-04 | result wkr-04
python3 $F --fleet epics subfleet --count 3 --worktrees       # a manager grows its own team
python3 $F --fleet epics commit -m 'docs(fleet): …'           # commit YOUR roadmap only
python3 $F --fleet epics down [--keep-ceo] [--keep-worktrees]
```

Addressing accepts a label (`mgr-04-sell-revenue`), a role+unit (`mgr-04`,
`wkr-11`), a pane id or an id prefix. `--as <who>` sends on another member's
behalf; without it, the calling pane is the sender (a pane exports `$RYSH_PANE`).
`--as human` relays an instruction from the keyboard — use it when you are
passing on what the user asked for, so the envelope says `FROM human` instead of
forging the CEO as the source of its own orders.

`msg` refuses to skip the chain: a manager cannot message another manager's
worker. Use `report` to go up, `msg` to go down.

---

## The envelope — why every message names its sender

`fleetctl` stamps this on the front of everything it delivers:

```
[FLEET epics | WORK ORDER | FROM manager mgr-04-sell-revenue (unit 04, pane 57484670)
 | TO worker wkr-04-sell-revenue (pane cc31bb3b) | msg-0007]
```

Panes get prompted by their parent, by the human taking the pane over, and
occasionally by a stray keystroke. An agent that cannot tell its manager's
instruction from a mistake does the wrong work and reports success. Every brief
tells the agent to trust the envelope and to open its own replies with one — *"I
am worker `wkr-04-…`, unit 04. This is my update."*

The full contract is `references/protocol.md`; every brief points its agent there.

---

## Two reporting channels, and they are not interchangeable

1. **The transcript.** An agent's final message in its pane *is* its answer;
   `wait`/`result`/`collect` read the transcript, not the screen. This is the
   default and it is why briefs demand a self-contained closing report.
2. **`report`,** which pushes into the parent's pane. For a blocker hit after the
   parent stopped waiting, or a milestone it needs now. Every push interrupts a
   working agent, so it is not for chatter.

Do not parse a pane's screen for results. A pane's shell buffer holds nothing once
claude takes the alternate screen, and the screen is a picture — wrapped, styled,
and only as tall as the pane. `screen` is for looking at an agent or diagnosing a
stuck one.

---

## Worktrees and commits

Every agent — CEO, manager, worker — works in `worktrees/<fleet>-<label>` off the
repo root, created by `up --worktrees` or on demand with `fleetctl worktree`.
Never `.claude/worktrees/`: it silently occupies the branch name and blocks the
main checkout. The main checkout is shared by the whole fleet, so an agent editing
it directly destroys everyone else's uncommitted work.

Every agent keeps **one roadmap file of its own** (`fleet.roadmap` on its pane,
under `--roadmap-dir`), and `fleetctl commit` commits **only that file, by path** —
so a shared file dirtied by a sibling is never swept in — retrying through the
`index.lock` contention you get when a dozen agents commit at once.

**Text only.** `commit` refuses `.png .jpg .gif .mp4 .mov .webm .pdf .zip …` and
anything over 2 MiB. If the work produces media, the file records where it lives
and how it was made: the repo keeps the recipe, not the render.

---

## Six things that will bite you

1. **`start` reports success even when a launch failed.** Always `fleetctl verify`
   after `up`; it screen-checks every agent. An exit code proves nothing.
2. **Shared files must be serialised by the CEO.** N managers editing one status
   file concurrently corrupts it. Have them write before/after proposal hunks and
   apply them yourself, one at a time. This is the most reliable way to lose a
   fleet's work.
3. **A prompt typed into a booting claude is dropped** — there is no ready signal.
   `up` passes the first prompt as argv to dodge this; only `msg`/`report` an
   agent that has already answered once.
4. **`wait` returns idle, not correct.** An agent that asked a *question* is also
   idle. Read what it returned before trusting it.
5. **Teardown is by LANE, not by pane.** `##pane delete <id>` prints
   `[rysh] pane <id> deleted` for a pane in another lane and does *nothing* —
   a per-pane teardown leaves every agent alive while reporting success. Only
   `##lane delete` works, and only with the **full uuid**, which `##lane list`,
   `##panegroup list` and `##pane info` all truncate to eight characters
   (`##lane delete <short-id>` → "not found"). `##lane info` run *as* a pane in
   the lane is the one view that prints it whole; `up` captures it there and
   stores it in the manifest so `down` can use it.
6. **`##pane meta` takes `--pane <id>`, `##pane name` takes a positional id.**
   Passing `--pane` to `name` does not error — it silently renames *your own*
   pane. `fleetctl` gets both right; hand-written `##` lines often do not.

---

## Scale

Every agent is a live claude burning tokens. `1 + units × (1 + workers)` panes:
22 units with one worker each is 45 sessions. `up` checks the session's token
ceiling before opening anything (`--force` overrides) and `--dry-run` counts panes
without opening any. Start with `--limit 3` on a fleet you have not run before.

Tear down with `down`; sessions are pinned, so `claude -r <session_id>` from the
agent's working directory resumes any of them afterwards.

## Layout

```
scripts/fleetctl.py          the helper
references/protocol.md       the contract every agent reads
references/brief-ceo.md      role briefs, {{VAR}} templates — edit to retune the fleet
references/brief-manager.md
references/brief-worker.md
```

The manifest lives at `<workspace>/.rysh/fleet/<name>.json`, and the same facts
are mirrored onto each pane's `##pane meta` (`fleet.role`, `fleet.unit`,
`fleet.parent`, `fleet.roadmap`, `fleet.worktree`) so the chart survives a daemon
restart and any tool can read it.
