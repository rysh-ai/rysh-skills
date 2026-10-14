---
name: rysh-fleet
description: Build and drive a three-tier fleet of claude sessions in a rysh session — one CEO, N managers, N workers, paired by unit, each in its own pane and its own worktree, talking to each other with a from-me envelope. Use when the user types "/rysh-fleet" or asks to stand up a fleet of claudes, run an epic/module/component per agent, create manager and worker claudes, build an org chart of agents, fan a roadmap out across many panes, or drive/inspect/tear down a fleet that already exists. Units come from a directory of docs, one doc split by heading, or from the shape of the codebase.
version: 1.0.0
argument-hint: "up --from <dir|file> [--template epic] [--workers N] | tree | msg <who> | broadcast manager | collect <who…> | down"
---

$ARGUMENTS

# /rysh-fleet — an org chart of claude sessions

```
        human                          human
          │                              │
         ceo                      roadmap-manager   owns new_roadmap/, alone
          │                              │
      managers                          ceo         owns scope
          │                              │
       workers                    fleet-managers    own the work orders
                                         │
                                     workers
   --template three-tier          --template epic
```

**The shape of a fleet is data** (design 028 §6.7). `TEMPLATES` in `fleetctl.py`
is a list of role specs — role, brief, parent, scope, count, label — and the
manifest's roster is a flat `members` list where each member names its parent by
pane id. Adding a role is a list entry plus a `references/brief-<role>.md`, not a
change to the manifest schema. One lane per role; `--template epic` is the
one-fleet-per-epic org chart.

Every node is a **real interactive claude** in a rysh pane — the user can watch it,
type into it, or take it over. Nothing here is headless `-p`.

**Helper:** `scripts/fleetctl.py` — stdlib only. It builds on `ryshfan` from the
**rysh-fanout** skill (required, sits next door) and adds three things fanout has
no answer for: cross-tab pane addressing, a persistent org chart, and a message
envelope so every agent knows who is talking to it.

Resolve the helper once, from wherever this skill is installed — a project's
`.claude/skills`, `~/.claude/skills`, `$CODEX_HOME/skills`, or a plugin. Set
`RYSH_SKILLS_DIR` to override. Every later block assumes `$F` is set:

```sh
for d in "${RYSH_SKILLS_DIR:-}" "${CLAUDE_PLUGIN_ROOT:-}/skills" .claude/skills "$HOME/.claude/skills" "${CODEX_HOME:-$HOME/.codex}/skills"; do
  if [ -f "$d/rysh-fleet/scripts/fleetctl.py" ]; then F="$d/rysh-fleet/scripts/fleetctl.py"; break; fi
done
```

```sh
python3 "$F" --fleet <name> <subcommand>
```

Everything prints JSON except `tree` and `screen`.

---

## Standing one up

```sh
# 0. see what would become units — always do this first
python3 "$F" units --from new_roadmap/epics            # a directory of docs
python3 "$F" units --from ROADMAP.md --split-on '^## '        # one doc, split by heading
python3 "$F" units --from . --scan-source                     # from the codebase itself

# 1. build it (add --dry-run to count panes without opening any)
python3 "$F" --fleet epics up --from new_roadmap/epics \
    --workers 1 --worktrees --mission 'ship the launch'

# 2. verify — start returns 0 even when a launch failed
python3 "$F" --fleet epics verify
python3 "$F" --fleet epics tree
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

Key flags: `--template three-tier|epic|epic-flat|epic-quad|duo|portfolio` ·
`--exclude GLOB` (repeatable; drop units whose files all match) · `--workers N` (per manager) ·
`--worktrees` + `--base-branch` · `--roadmap-dir` (default `new_roadmap/fleet`) ·
`--mission` · `--limit N` · `--ceo-here` (make the calling pane the head of the
chain instead of opening a lane) · `--agent claude|codex` · `--agent-args`
(`--claude-args` is the older spelling and still overrides).

## One lead, one worker per epic — `--template portfolio`

For "an agent on every epic of the roadmap, all at once": one **portfolio lead**
(the head — owns `new_roadmap/`, orders work across every epic, the pane the human
talks to) and **one worker per epic**, all workers stacked in one lane.

```sh
python3 "$F" --fleet roadmap-epics up --from new_roadmap/epics --pattern 'E*.md' \
    --exclude 'E1-*' --exclude 'E2-*' --exclude 'E7-*' --exclude 'E27-*' \
    --template portfolio --workers 1 --worktrees --base-branch dev \
    --mission '<one line>'
```

With the default `--web-sync root` the workers are sync-paused in the browser and
their briefs say so: nothing they only show on screen reaches anyone, so every
result goes to the lead or the board. The lead's brief says the reverse — it is
the human's window. (`duo` has the same shape but its briefs say "exactly two
agents, one epic", which is false with N workers; that is why this template's
briefs are forks.) Address a worker by epic: `msg wkr-11`, `screen wkr-11`;
the lead is `lead`.

## Which agent the fleet runs

```sh
python3 "$F" --fleet epics up --from new_roadmap/epics --agent codex --worktrees
```

`--agent` picks what every pane runs — `claude` (default), `codex`, or **`rysh`**
— for the **whole fleet**; a mixed fleet is not supported. For the two CLIs, two
things differ and nothing else does:

* **Launch args.** Full-auto is `--dangerously-skip-permissions` for claude and
  `--dangerously-bypass-approvals-and-sandbox` for codex. The default follows the
  agent, so you normally pass neither.
* **Resume.** claude resumes by the session id fleetctl pinned at launch; codex
  issues no id at launch, so `##ansa resume` runs `codex resume --last`, which is
  filtered by working directory. **A codex fleet should therefore use
  `--worktrees`** — two codex agents sharing one cwd would both resume whichever
  ran last.

`##ansa kill` signals the pane's foreground process group, whatever the program
is — and for `--agent rysh` it does something else entirely, below.

### `--agent rysh` — rysh's own agents, no subprocess

```sh
python3 "$F" --fleet rx up --from new_roadmap/epics --agent rysh --worktrees
```

A rysh agent is the pane's **own** agentic loop running inside the daemon, with
rysh's full tool set (bash, file_read, edit, git_commit, sub_agent, …). Nothing
is spawned, so **the pane's foreground program is empty for the agent's whole
life** — which changes what every verb means:

* **Launch** is a delivery, not an exec: the brief is published to the pane as a
  prompt, and the pane's shell is moved into the worktree first (a native agent's
  tools run in the PANE's cwd).
* **Approvals** are armed at launch — `--agent rysh` takes no launch args, and
  will refuse them, because there is no command line. Left un-armed, every agent
  stops at its first `bash` waiting for a human who is not coming.
* **`##ansa kill`** cancels the in-flight TURN and reports
  `paused … (rysh agent (native turn) — checkpointed)`. Never "killed": there was
  no process, and unlike a signalled CLI a cancelled turn loses nothing.
* **`##ansa resume`** continues that turn from its checkpoint — no CLI to
  relaunch, no session id, no nudge needed — and puts the approval mode back.
* **`##ansa interrupt` does NOT work on a native fleet** (it sends ESC to a PTY
  no agent reads). Use `kill`; it is the native-aware verb.
* **`down` is final.** The transcript is persisted under the PANE id, so closing
  the pane orphans it and no resume verb reaches it afterwards.

**A codex fleet writes to your `~/.codex/config.toml`.** Codex blocks on a
"do you trust this directory?" prompt that a fleet agent has nobody to answer, so
`up` pre-grants `trust_level = "trusted"` for each cwd it launches in, marked with
the fleet's name. `down` removes the entries that fleet added — and only those.

## Every fleet gets its own board

`up` stamps `board.id = <fleet>` on every pane it creates, so an agent's
`rysh board post --as $RYSH_PANE -- '…'` lands on ITS fleet's board with no flag
(design 028). Read one back with `rysh board tail --board <fleet>`, open it with
`##board open --fleet <fleet>`, and stop one fleet without touching another with
`##ansa interrupt --fleet <fleet>` — a bare `--fleet` is refused, and
`--all-fleets` is the session-wide form.

## Messages go through the board, not the keyboard (`--comm`)

Every `msg` / `report` / `broadcast` (and `reviewpool.py send`) becomes a PROMPT on
the fleet's board (`rysh board send`) and is delivered into the recipient's own
session — a claude's inbox socket, `codex queue` — beside its keyboard, never
through it. The human can type into any agent's pane at any time and nothing an
agent sends will erase it. The recipient acks on the board (`rysh board ack`); a
prompt that cannot be handed over shows up on its thread as `undelivered`.

Two ways to deliver, chosen per fleet at `up` (designs 050/051):

| `--comm` | who delivers | if the recipient is down |
|---|---|---|
| **`queue`** (default) | each agent's **own listener** (`rysh board listen`, one per pane, started by `up`) watches the board and takes the prompts addressed to it; every listener sees every prompt and lets the others' pass | the prompt waits open on the board; the listener catches it up when it starts |
| `grid` (old name `polling-stream`) | the daemon's courier pushes each prompt point-to-point into the recipient's inbox | the courier retries, then posts `undelivered` |
| `input-stream` | ANSA types into the recipient's pane (pre-050) | — |

```sh
python3 "$F" --fleet X up --from <doc>                        # queue (default)
python3 "$F" --fleet X up --from <doc> --comm grid            # courier push
python3 "$F" --fleet X up --from <doc> --comm input-stream    # typed into the pane
python3 "$F" --fleet X verify     # board modes: is every agent reachable (+ listener alive, queue)
python3 "$F" --fleet X listen     # queue: (re)start any member's listener that is not running
```

What `up` does for a board mode: stamps `fleet.comm`; launches claude with a
`--settings` file that accepts inbound messages and stamps its inbox socket as
`claude.inbox` (kept in `agent.args`, so `##ansa resume` keeps it); arms codex
thread discovery; for `queue`, starts one detached listener per agent
(`<rysh_dir>/listen/<pane>.{pid,lock,log,delivered}`). Listeners outlive `fleetctl`
and agent restarts and exit when their pane is gone; after a machine reboot run
`listen`. `up` refuses a board mode the running daemon cannot carry (queue needs
`rysh board caps` → `queue`, i.e. a daemon from design 051 on). **input-stream
types into the recipient's composer and clears it first (ctrl+U), so it erases
whatever the human was typing there.** A manifest built before 050 has no `comm`
and stays input-stream.

**Watching it:** the board pane shows **status** messages by default; press `p`
for prompt messages (commands agents send each other, with their acks), `a` for
all, `s` for status again, `f` for the flow diagram, and **`t` for the team view**:
every agent's work · activity · health · task in one table (who is progressing,
idle or blocked, and on whom), with the dependency tree under it — always drawn,
"none blocked" when nobody waits. `rysh board tail --view status|prompt|all|flow|team`
is the same from a shell.

## Only the head and the board repaint in the browser

Twenty-plus agents each repaint several times a second, and a browser that
streams all of them stalls. So `up` stamps `web.sync=off` (`##pane sync off`) on
every member **below the head of the chain** before starting it: those panes keep
running and keep their slot, but web clients stop receiving their repaints and
show them dimmed with a **SYNC OFF** badge over the last screen received. The head
— the orchestrator you talk to — and the fleet's board pane stay live, which is
how the fleet is meant to be driven.

```sh
python3 "$F" --fleet X up --from <doc> --web-sync all    # old behaviour: everyone live
python3 "$F" --fleet X up --from <doc> --web-sync none   # the head paused too
```

Look at one agent with `##pane sync on --pane <label>` (or click its SYNC OFF
badge); `##pane sync off --all` pauses everything but your pane and the boards
again, `##pane sync on --all` resumes everything, `##pane sync list` shows what is
paused. `subfleet` workers follow the fleet's policy. A fleet built before the
policy existed has no `web_sync` in its manifest and is left as it was.

## A tab of its own

```sh
python3 "$F" --fleet epic-07 up --from <doc> --template epic --new-tab
python3 "$F" --fleet epic-07 down          # closes that tab too
```

`--new-tab` gives the fleet its own tab, named after it, with a lane per role
inside — which is what makes several fleets watchable at once. The manifest
records `owns_tab`, and `down` closes the tab **only if this fleet opened it**: a
fleet built in your tab must never take your panes with it. `--keep-ceo` leaves
the tab alone for the same reason — somebody still lives there. `epics` passes
`--new-tab` by default, so each epic's fleet lands on its own screen.

## The daemon knows what every agent is doing (design 054)

The fleet actor keeps a **status ledger**: each member's declared work
(`idle|progress|blocked|done`, a task, `blocked_by`), its observed activity
(`working|idle`, read from the pane), and a derived health (`ok`, `stalled`,
`silent`, `unblocked`, `available`, `deadlock`, `unresponsive`). `blocker_for`
is derived from everyone's `blocked_by`, never declared. Every brief binds its
agent to the contract in the **rysh-fleet-agent** skill.

```sh
python3 "$F" --fleet X status                   # the ledger (= rysh fleet status X --json)
python3 "$F" --fleet X status wkr-04            # one member: ledger row + what its screen shows
rysh fleet status X                             # the table: work · activity · health · blockers
rysh fleet watch X --policy smart --interval 10m --stall 5m
python3 "$F" --fleet X up --from <doc> --watch-policy always --watch-interval 15m
```

**Check-ins** are board commands from `fleet-actor`, delivered like any work
order. `smart` (default) pings only silent, stalled and newly unblocked agents —
a working agent with a fresh status is never interrupted; `always` pings every
agent every interval; `off` observes and alerts only. An agent `available` (done
or idle, and idle on screen), `unresponsive` (two check-ins unanswered) or in a
`deadlock` (a `blocked_by` cycle) is **escalated to its parent**. Every change is
a `fleet-alert` post on the board, and the flow view (`f`) draws the ledger:
`{work·activity}` badges, `⧗ blocked by` edges, the `fleet` node's check-ins.

## The daemon knows your fleets

`up` registers the fleet and its members with the session's fleet registry and
`down` marks it down; `##fleet list|show <name>` and `rysh fleet ls --json` read
it. **The manifest under `.rysh/fleet/` is now a cache, not the truth** — it is
untracked, shared by every fleet in the workspace, and a sibling's teardown has
deleted a live fleet's copy before. The registry sync is reported in `up`'s own
output: if it says otherwise, the fleet is running and the daemon does not know
it.

---

## Twenty-five epics, three at a time

```sh
python3 "$F" epics --from new_roadmap/epics --dry-run   # what would become a fleet
python3 "$F" epics --from new_roadmap/epics --promote 3 --worktrees
python3 "$F" epics-promote        # when one goes down, bring the next up
```

`epics` makes **one fleet per epic doc**, each with its own board and its own
org chart, registers all of them (free — a row and a board id, no panes) and
brings up only `--promote`. **The cap is the daemon's**: the registry refuses the
fourth `up` (founder gate `D-14`, ruled at 3) and this driver respects the
refusal rather than keeping its own count, which is what makes the cap hold when
somebody also runs `fleetctl up` by hand.

It reports four lists and they are not interchangeable: `registered`, `up`,
`refused_by_cap`, `failed`. A fleet refused by the cap opened nothing; a fleet
that failed to come up has had its slot handed back.

## Driving it

```sh
python3 "$F" --fleet epics msg mgr-04 --file /tmp/order.md      # down the chain
python3 "$F" --fleet epics broadcast manager --file /tmp/all.md # one order to every manager
python3 "$F" --fleet epics report 'BLOCKED: no credentials'     # up the chain, from a pane
python3 "$F" --fleet epics collect mgr-01 mgr-02 --timeout 1800 # wait on several, get answers
python3 "$F" --fleet epics status [wkr-04] | screen wkr-04 | result wkr-04
python3 "$F" --fleet epics subfleet --count 3 --worktrees       # a manager grows its own team
python3 "$F" --fleet epics commit -m 'docs(fleet): …'           # commit YOUR roadmap only
python3 "$F" --fleet epics down [--keep-ceo] [--keep-worktrees]
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
2. **`report`,** which pushes to the parent — through the board and its courier
   under polling-stream, into the parent's pane under input-stream. For a blocker
   hit after the parent stopped waiting, or a milestone it needs now. Every push
   starts a turn in a working agent, so it is not for chatter.

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
