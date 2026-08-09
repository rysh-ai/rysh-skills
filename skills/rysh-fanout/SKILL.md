---
name: rysh-fanout
description: Fan work out to sibling claude sessions running in other rysh panes. Use when the user types "/rysh-fanout" or asks to run a prompt in another pane, spawn a second (or third, or tenth) claude next to this one, delegate a task to a sibling pane, parallelise work across panes, check what another pane's claude is doing, or send a follow-up turn to a claude already running in a pane. Works from inside a rysh session; each child is a normal interactive claude the user can watch and take over, never headless `-p`.
version: 1.0.0
argument-hint: "[<prompt>] | spawn <n> | send <pane> <text> | status [<pane>] | screen <pane> | close <pane>"
---

$ARGUMENTS

# /rysh-fanout — run claude in sibling rysh panes

Spawns panes next to this one, starts a real interactive claude in each, feeds it a
prompt, and reports back. The children stay on screen: the user can watch them work,
type into them, or take one over at any point. Nothing here uses headless `-p`.

**Helper:** `scripts/ryshfan.py`, next to this file — stdlib only, no install.
Run it with `python3`. Every subcommand prints JSON except `screen`.

---

## Before anything else: which session?

Usually nobody has to say. A pane exports `$RYSH_SESSION`, so `discover` resolves the
session this conversation is *running in* — a fact about where we are, not a guess about
what was meant. It reports which rule fired as `resolved_by`.

Resolve the helper once, from wherever this skill is installed — a project's
`.claude/skills`, `~/.claude/skills`, `$CODEX_HOME/skills`, or a plugin. Set
`RYSH_SKILLS_DIR` to override. Every later block assumes `$R` is set:

```sh
for d in "${RYSH_SKILLS_DIR:-}" "${CLAUDE_PLUGIN_ROOT:-}/skills" .claude/skills "$HOME/.claude/skills" "${CODEX_HOME:-$HOME/.codex}/skills"; do
  if [ -f "$d/rysh-fanout/scripts/ryshfan.py" ]; then R="$d/rysh-fanout/scripts/ryshfan.py"; break; fi
done
```

```sh
python3 "$R" discover                       # RYSH_SESSION, or the only daemon running
python3 "$R" --session <name> discover      # override, and required if neither applies
python3 "$R" panes                          # every pane, fully qualified
```

When there is no `$RYSH_SESSION` (a daemon older than **2026-08-05**, or ryshfan run
outside a pane) and more than one daemon is up, `discover` refuses and lists what it
found. **That list can include other people's sessions on a shared dev box**, so ask —
never pass `--session` for a session the user did not name.

You are in a pane, and it says so: a pane's shell exports `$RYSH_PANE`, so `panes`
marks that one `"self": true` and `spawn` anchors there without being told. `discover`
echoes it back as `self_pane`.

`"active": true` is a different thing — where the user's *focus* is, which moves. Under
a daemon older than **2026-08-05** (rysh-cli `e137b82`) `$RYSH_PANE` is unset, `self` is
false everywhere, and `spawn` falls back to the active pane. That fallback drifts: open
a pane and the focus follows it, so the next `spawn` anchors somewhere new. Pass
`--anchor <pane>` if you need it pinned.

---

## The normal flow

```sh
# $R resolved as above
S=""   # empty when discover resolves the session itself; else "--session <name>"

# 1. one pane per parallel task, stacked next to this one
python3 "$R" $S spawn --count 3

# 2. start each child on its own prompt (prompt goes in at launch)
python3 "$R" $S start <child-id> --prompt-file /tmp/task-a.md

# 3. block until it stops working — this returns the child's answer
python3 "$R" $S wait <child-id> --timeout 900

# 4. clean up
python3 "$R" $S close <child-id>
```

Spawn all the children first, start them all, and only then `wait` on each in turn —
otherwise they run one at a time and you have gained nothing.

`start` names each pane after its task, so `##pane list` reads like a task board
(`audit-secrets`, `audit-tests`) instead of a zoo (`humorous-falcon`). The name is also
a selector: `wait audit-secrets` works.

### Getting results back

`wait` returns what the child said. `start` pins a `--session-id` for every child, so
its transcript is a known file and the last thing it said can be read back as text —
`wait` and `result` both do that, and `result` re-reads it at any time afterwards.

```sh
python3 "$R" $S result <child-id>                 # whole answer + transcript path
python3 "$R" $S result <child-id> --max-chars 500 # clipped, flagged `truncated`
```

`wait` clips its answer at 4000 chars by default (`--max-chars`, 0 = unlimited) so one
verbose child cannot flood this conversation; the transcript path is always returned in
full, so nothing is lost.

For a **structured** result — a diff, JSON, a file the next step consumes — still tell
the child to write a file, and read the file. The transcript gives you its prose; a
file gives you an artefact.

Do not try to read results off the screen. A pane's shell buffer holds nothing once
claude takes the alternate screen, and the screen itself is a picture: wrapped, styled,
and only as tall as the pane. `screen <pane>` is for looking at a child or diagnosing a
stuck one, not for parsing.

### Follow-up turns

```sh
python3 "$R" $S send <child-id> 'now also check the tests and update the file'
```

`send` types into the running claude and confirms it was submitted, pressing Enter
itself if rysh's own Enter did not take (see *stale daemon* below).

---

## Four things that will bite you

**1. A `##` command line is tokenised on whitespace and re-joined with single spaces.**
Runs of spaces collapse; newlines cannot survive at all. `start` sidesteps this by
passing the prompt through a file, so `--prompt-file` takes anything. `send` cannot —
it types directly into the TUI — so keep follow-ups to one line, or write the long
version to a file and send `read /tmp/next.md and do it`.

**2. `--pane` only resolves inside the caller's pane group.** `##cmd pane --pane <id>`
with no other selector searches the *active* pane's group only, so it fails on a
child in another group and silently follows the user's focus as it moves. The helper
always emits `--tab T --lane L --pg G --pane ID`, using `$RYSH_LANE`/`$RYSH_STACK` for
our own lane and stack and positions elsewhere — `##pane list` only reports positions,
and a position stops being true the moment a lane or stack is opened or closed, which
a fan-out does to itself. Every selector resolves an id before an index, so the two mix
in one line. If you hand-write a `##cmd`, qualify it the same way. `spawn` uses `##new stack`, which puts children in the anchor's own group; plain
`##new pane` would open a new group instead.

**3. A prompt typed into a claude that is still starting up is dropped.** There is no
reliable ready signal, which is why `start` hands the prompt to the process as argv
instead of typing it. Only use `send` on a child that has already answered something.

**4. `wait` reports idle, not correct.** It returns once the pane is idle *and* the
child has spoken since it was prompted — so a child still booting is never mistaken for
a finished one, and a second turn never returns the first turn's answer. But a child
that asked a *question* has also spoken and gone idle: read the answer it returns
before trusting it. `state` can also come back `never-started` (claude never came up in
that pane — usually a bad `--claude-args`) or `timeout`.

**5. `--claude-args` needs `=` when its value starts with a dash.**
`--claude-args --model opus` makes argparse complain that it "expected one argument";
write `--claude-args='--model opus --dangerously-skip-permissions'`. Note that passing
it replaces the default, so repeat `--dangerously-skip-permissions` if you still want
it.

---

## Stale daemon (the LF bug)

A rysh daemon older than **2026-07-29** (commit `18eae6e`) terminates typed commands
with LF instead of CR. claude treats LF as "insert a newline", so prompts pile up in
its composer and never submit. `rysh list-sessions` flags such a daemon `[outdated]`.

`send` handles it: it checks the composer afterwards and presses Enter over NATS if
the text is still sitting there (`"submitted_by": "ryshfan-cr"` in the output).
`start` is immune, since the prompt never goes through the composer.

The real fix is restarting the daemon on a current binary — **but do not do that
yourself.** Live PTY processes are lost on restart, and on this machine the claude you
are running in may itself live inside that daemon. Tell the user; let them choose.

---

## Command reference

| Command | Does |
|---|---|
| `discover` | resolve session, workspace, binary, NATS port |
| `panes` | every pane with `tab`/`lane`/`pg`/`index`/`name`/`id`/`active`/`self`, plus `lane_id`/`pg_id` where known |
| `spawn --count N [--anchor P]` | stack N panes in the anchor's group (default: our own pane, else the active one); refuses if a token ceiling is spent (`--force`) |
| `start <pane> --prompt-file F` | launch interactive claude with the prompt as argv; prints its `session_id` |
| `send <pane> <text>` | type a follow-up turn and confirm it submitted |
| `screen <pane>` | the child's current screen, ANSI-stripped |
| `status <pane>` | `busy` / `idle` / `no-tui`, the composer, session id, last answer time |
| `wait <pane> [--timeout S]` | block until the child stops working; returns its answer |
| `result <pane> [--max-chars N]` | the child's last answer + transcript path, any time after |
| `close <pane>` \| `close --all` | delete one pane, or every child this supervisor started |
| `children` | every pane running a claude we started: task, session id, whether it is still running |
| `watch <pane>` | block until the pane's program EXITS (a daemon event, not a screen poll) |
| `cost` | session spend, and any token ceiling close to being hit |

`start` takes `--claude-args` (default `--dangerously-skip-permissions`) if a child
needs a different model, effort, or permission mode.

## What the daemon knows about a child

A child's bookkeeping lives on the PANE, in `##pane meta` — `claude.session_id`,
`claude.task`, `claude.parent`, `claude.prompted_at` — not in files beside this script.
So `##pane list --meta` shows it, any tool can read it, and it survives a daemon
restart. `##pane list` also reports each pane's live foreground program
(`running=claude`), straight from the daemon rather than scraped off a screen.

That is what `children` and `close --all` are built on, and why they can tell OUR
children from another supervisor's (`claude.parent`).

`watch <pane>` blocks on the daemon's own process event, which is the one thing `wait`
cannot see: a child that crashed, was killed, or was taken over and quit looks exactly
like a child thinking hard when all you have is its screen.

## Housekeeping

Children run in the pane's working directory and inherit the daemon's environment; the
helper unsets the parent's `CLAUDE_CODE_*` markers so each child is a top-level session
rather than a nested one. Prompt files, and the pane→session-id map that makes
`result` work, live under `<workspace>/.rysh/fanout/`. Close the panes when the work is
done — every open child is a live claude process.

Because each child's session id is pinned and recorded, a child outlives its pane. Run
`claude -r <session_id>` **from the child's working directory** to pick its conversation
up yourself — transcripts are filed per directory. `result` still answers after the pane
is closed, addressed by pane id or session id (the name goes with the pane); `status`
and `start` both print the id, so keep it if you plan to close the pane.
