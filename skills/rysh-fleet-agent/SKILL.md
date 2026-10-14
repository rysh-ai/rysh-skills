---
name: rysh-fleet-agent
description: The status and communication contract every agent in a rysh fleet follows — declare your work (idle/progress/blocked/done) with progress (--pct/--eta) to the fleet actor, name who blocks you, answer check-ins and the goal executor's HURRY/REALIGN/WAIT/SHIELD/SEQUENCE directives, ack prompts, and never go silent or idle unnoticed. Use when you are a member of a rysh fleet (your brief says so, your pane has fleet.role meta, or a message arrives with a [FLEET …] envelope or a CHECK-IN from fleet-actor), when you start, block on, finish or hand off a task inside a fleet, or when the user asks how fleet agents should report status or coordinate.
version: 1.1.0
argument-hint: "status [<fleet>] | <idle|progress|blocked|done> --task '…' [--blocked-by <who>]"
---

$ARGUMENTS

# The fleet agent contract (design 054)

You are one agent in a fleet. The daemon runs a **fleet actor** that keeps a
status ledger of every member and writes to the fleet's board. It knows two
things about you, and it checks one against the other:

| | who writes it | values |
|---|---|---|
| **work** | **you** — `fleet status <work>` | `idle` · `progress` · `blocked` · `done` |
| **activity** | the actor — it watches your pane | `working` · `idle` |

From both it derives your **health**, and that is what your parent and the
human see:

| health | means | what happens |
|---|---|---|
| `ok` | what you say matches what you do | nothing |
| `stalled` | you say `progress`, your pane has been still ≥ 5 min | you get a CHECK-IN at once, then once per interval |
| `silent` | you never declared a status, and you have been idle 10 min | you get a CHECK-IN |
| `unblocked` | you say `blocked`, every blocker is now done | you get a CHECK-IN at once telling you to resume |
| `available` | you say `idle`/`done` and you are idle | **your parent** is told to give you work |
| `deadlock` | you are in a `blocked_by` cycle | the cycle's parent is escalated to |
| `unresponsive` | 2 check-ins unanswered | **your parent** is escalated to |

The interval and stall threshold are per fleet (`rysh fleet watch`); 10 min /
5 min are the defaults.

## The one command

Your brief gives you the binary (`$BIN`), your pane id and your fleet. Every
example below writes the binary as `rysh`; use the absolute path from your brief.

```sh
rysh fleet status progress --as $RYSH_PANE --fleet <fleet> --task 'build the parser (E7 §3)'
rysh fleet status blocked  --as $RYSH_PANE --fleet <fleet> --blocked-by mgr-04 --note 'need the API spec'
rysh fleet status done     --as $RYSH_PANE --fleet <fleet> --task 'parser' --note 'branch feat-parser, 42 tests green'
rysh fleet status idle     --as $RYSH_PANE --fleet <fleet>
```

- `--as` defaults to `$RYSH_PANE`; pass it anyway — some agent shells do not
  carry it.
- `--blocked-by` takes a **member** (label like `mgr-04`, `wkr-11`, or a pane id
  prefix), **`human`**, or **`ext:<what>`** (`ext:ci`, `ext:D-14`). Several:
  `--blocked-by mgr-04,human`. It is only valid with `blocked`, and `blocked`
  requires it.
- `blocked` and `done` keep your current task when you omit `--task`.
- **Never declare `blocker_for`.** The actor derives it from everyone's
  `blocked_by` — whoever you name as your blocker sees you under "blocking"
  automatically, and stops seeing you the moment you declare anything else.

Read the ledger: `rysh fleet status` (your own fleet when run in a fleet pane),
`rysh fleet status <fleet>`, `--json` for scripts. In a rysh pane: `##fleet status`.

## When to declare — the rules

1. **On start of every task: `progress --task '…'`.** Including the first one
   in your brief. A task is one line a human can read in the flow diagram.
2. **The moment you wait on someone: `blocked --blocked-by <who>`.** Waiting for
   a reply, a review, a merge, a decision, credentials — all blocked. Then
   *also* send the message that asks them (fleetctl `msg`/`report`); the ledger
   records the dependency, it does not deliver your question.
3. **When unblocked, declare `progress` again** before you resume. If the actor
   sent you an `unblocked` check-in, your blockers are already done — re-read
   their result, then declare.
4. **On finishing: `done --note '<where the result is>'`**, with absolute paths
   and branch (protocol: absolute paths only). Then send your report as usual.
5. **Before ending a turn with nothing left to do: `idle` or `done`.** That is
   what tells your parent you need work (`available`). Being `blocked` and idle
   is fine — you are waiting, as declared — but a stale `progress` while idle is
   `stalled`, and never declaring at all is `silent`.
6. **Answer every CHECK-IN with exactly one `fleet status` command** — it records
   your status *and* closes the check-in (your answer lands on its thread). Do
   not answer a check-in with prose only; prose does not update the ledger.
7. **If you are a parent and get an ESCALATION**, act on it in the same turn: a
   work order for an `available` child, an answer for a blocked one, a decision
   for a `deadlock`. Then declare your own status.
8. Declare when the status *changes*, not as chatter. Re-declaring the same
   status to look busy is noise on the board.

## Messages you will receive

```
[rysh board <fleet> | command ci-… | from fleet (fleet)]
[FLEET <fleet> | CHECK-IN | FROM fleet-actor | TO worker wkr-04 (pane cc31bb3b) | ci-…]
Status check (stalled). The fleet ledger has you as: progress (build the parser), declared 12m ago; …
Answer with ONE command — it records your status and answers this check-in:
  /abs/path/rysh fleet status <idle|progress|blocked|done> --as <pane> --fleet <fleet> --session <s> --task '<what>' …
```

- `CHECK-IN` from `fleet-actor` → rule 6. You do not need to `board ack` it; the
  `fleet status` answer is the ack.
- `ESCALATION` from `fleet-actor` → rule 7 (parents only).
- Work orders from your parent → `board ack` first (as the protocol says), then
  `fleet status progress --task …`, then work.

## Goals and the goal executor (design 055)

Every fleet that is up has a **goal executor** beside the fleet actor. The
watch above asks *is each agent alive and honest?*; the executor asks *will the
fleet and each agent reach their goals, in what order?* It holds:

- the **fleet goal** and **your goal** — `rysh fleet goal show <fleet>` (set by the
  human, the head or your parent: `rysh fleet goal set <fleet> [--member <you>]
  --goal '…' --done-when '…' --due 4h`; closed with `goal met|drop`);
- your **progress** toward your current task — add it to the one command you
  already use: `rysh fleet status progress --task '…' --pct 40 --eta 20m`. A new
  `--task` resets pct and ETA; give them again when you can. An honest ETA is the
  single most useful thing you can tell a fleet that waits on you.

It reads the same team view and dependency tree the human reads, and judges you
as one of: `progressing`, `blocker` (someone waits on you), `blocked`,
`unblocked`, `idle`, `stalled`, **`stressed`** (others wait on you *and*
prompts pile up on you), **`strayed`** (busy but not converging: no pct
movement, task after task, past your own ETA, or working while you said
idle/done), `deadlock`, `met`. The team view shows it in its GOAL column.

It **serialises** the fleet's goal-level talk through itself, so you will
receive at most one of these per cycle, and none while you are on track:

| directive | means | your move |
|---|---|---|
| `GOAL HURRY` | agents wait on you and you gave no ETA (or passed it) | finish that first; answer with ONE `fleet status … --eta <d>` |
| `GOAL WAIT` | you wait on someone; their ETA; **do not message them** | don't ask them; advance what does not depend on them; `board ack` closes it |
| `GOAL SHIELD` | you are stressed; the executor holds its messages to you | do the one thing it names; give an `--eta` when you can |
| `GOAL REALIGN` | you look off course (why is stated) | ONE `fleet status`: a task that serves your goal, or keep yours and say how it does in `--note` |
| `GOAL SEQUENCE` | deadlock; one breaker | breaker: proceed on a stated assumption (in `--note`), declare `progress`; others: hold |
| `GOAL PULL` (parents) | your child is idle while the goal is open; maybe pair it with the bottleneck | give it work in this turn |
| `GOAL RISK` / `CONFIRM` (heads) | the fleet got worse / every member goal is met | re-plan, re-assign, move the due — or `rysh fleet goal met` |

Unanswered asks climb a ladder: your parent is copied, then the head and the
board (for the human).

## The rest of the fleet protocol still applies

Envelopes, chain-only messaging, worktrees, absolute paths, committing your own
roadmap file: `rysh-fleet/references/protocol.md`. This skill adds the status
contract; it replaces nothing there.

## Seeing it

- `rysh fleet status <fleet>` — the ledger table: work, activity, health, task,
  blocked by / blocking, unanswered check-ins.
- The board's **flow** view (`f` in the board pane, the desktop app's flow tab,
  `rysh board tail --board <fleet> --view flow`) draws it: a
  `{work·activity}` badge on every agent (`{progress·STALLED}` when unhealthy),
  a `⧗ blocked by` edge per dependency, and the fleet actor's check-ins as
  prompts from the `fleet` node.
- Alerts (`fleet-alert` posts) on the board's status view.
