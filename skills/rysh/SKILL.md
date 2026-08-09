---
name: rysh
description: Operate the rysh session this conversation is running in. Use when the user types "/rysh" or asks to run any `##` command, or to inspect or change anything about the session — tabs, lanes, stacks, panes, input modes (`##mode`), secrets and variables (`##secret`, `##var`), LLM model bindings (`##llm`, `##pane model`), spend (`##cost`), policy, the governance proxy, agents and humanoids, cron, git worktrees, MCP servers, sharing/upstream, the web UI, or session state itself (`##rysh`, `##session`). Also for "what panes do I have", "rename this tab", "what model am I on", "how much have I spent", "open 4 panes".
version: 1.0.0
argument-hint: "[<##command>] | status | panes | cost | model | secrets"
---

$ARGUMENTS

# /rysh — operate this rysh session

Every `##` command, driven from inside the session it acts on. This is the live
session the user is sitting in: their panes, their spend, their secrets. Read
before you write, and treat anything that changes what is on their screen as
something to say out loud.

**Helper:** `.claude/skills/rysh/scripts/ryshctl.py` — stdlib only.

---

## The one invocation

```sh
C=.claude/skills/rysh/scripts/ryshctl.py
python3 $C '##session'                # any ## command
python3 $C --json '##pane list'       # {"ok","status","command","output"} for parsing
python3 $C --pane <id> '##mode list'  # answer AS another pane
python3 $C where                      # session, workspace, binary, our own tab/lane/stack/pane
python3 $C dashboard                  # session + layout + what is running + spend, in one call
```

Pane-scoped commands — `##pane info`, `##mode list`, `##grounding`, `##hop status`
— have no selector of their own: they answer for the *caller's* pane. `--pane <id>`
is how you ask about a different one.

It resolves three things that are easy to get wrong: the binary (`rysh`, `ry`,
`rysh_local`), the session (`$RYSH_SESSION`), and the working directory. That
last one costs the most time — rysh state is **project-local**, so `rysh exec`
run one directory away fails with `session "X" not found`, an error that names
the session and never mentions the cwd.

It also pins `--pane-id $RYSH_PANE`, so commands about "the active pane" mean
**our** pane instead of wherever the user's focus has drifted. Pass `--focus`
when you genuinely want to ask about the pane the user is looking at.

The raw form, if you need it: `cd <workspace> && rysh exec --session <name> --
'##...'`.

Exit status is trustworthy — every `##` command reports failure properly, so
`set -e` and `if` work. Check it rather than grepping the text.

---

## Look before you touch

```sh
python3 $C '##session'        # name, state, daemon pid, nats/web ports, working dir
python3 $C '##tab list'       # tabs
python3 $C '##pane list'      # panes of the active tab, with ids
python3 $C '##lane list'      # lanes, with ids and pane counts
python3 $C '##pane info'      # our own pane: name, mode, provider, status
python3 $C '##llm status'     # the model actually in effect
python3 $C '##cost'           # spend so far
```

`##help` prints the complete list of commands — reach for it rather than
guessing a subcommand, and `##<family>` with a bad subcommand prints that
family's usage.

---

## Six rules

**1. Never send a command to your own pane.** `##cmd pane --pane $RYSH_PANE …`
types into *this* claude's composer and presses Enter, so the "command" arrives
as a message from the user. Same for `##native on`, which hands the terminal to
bash underneath us. Target other panes, or use `ryshctl` — which talks to the
daemon, not to a keyboard.

**2. Qualify every selector.** `--pane <id>` alone is resolved inside the
*active* pane's group, so it silently follows the user's focus and fails on a
pane in another group. Use the whole path, and prefer ids — `##pane list`
reports lane and stack as **positions**, which stop being true the moment a lane
or stack is opened or closed:

```sh
python3 $C "##cmd pane --tab $RYSH_TAB --lane $RYSH_LANE --pg $RYSH_STACK --pane <id> pwd"
```

Every selector resolves an id before an index or a name, so `$RYSH_*` values
drop straight in.

**3. `##cmd` output does not come back** — unless you ask. It runs in the target
*panes*; what returns is a dispatch summary (`ran "pwd" in 3 pane(s)`). `--capture`
redirects each pane's output to a file and prints where, ending with
`__rysh_capture_done:<status>`; poll for that line, then read the file. The waiting is
yours to do: a `##` command runs on the mailbox every other pane queues behind, so
blocking there would freeze the session.

**3b. Target only the panes that can take a command.** `##cmd` types into whatever is
in the pane — a shell command sent to a pane running claude becomes a message to claude.
`--running shell` restricts a broadcast to panes at a prompt; `--running claude` does
the opposite. `##pane list` shows what each pane is running.

**4. Secrets: list names, never fetch values.** `##secret list` prints names and
which tier they come from — safe. `##secret get <NAME>` prints the **real
value**, which lands in this conversation and therefore in the model's context;
this session holds real provider keys and app passwords. Only run it when the
user explicitly asks for that value. `##variable` (`##var`) is the deliberately
LLM-visible flavour — use it for anything that is not a credential. `##snat off`
disables the translation that keeps real secrets out of provider traffic; leave
it on unless the user says otherwise.

**5. Do not take the session down.** This claude runs *inside* the daemon:
`rysh stop`, `delete-session`, `attach --upgrade` and anything that restarts it
kill this conversation and every live PTY in it. `attach --upgrade` now lists what
would die and refuses without `--force`, but that guard protects the user's work, not
this conversation's — the answer is still to let them run it. `##session switch` moves the
user elsewhere. Tell them what to run; let them run it.

**6. Some commands reach outside this machine.** `##share` / `##upstream`
publish pane content to the collaboration server. `##rysh web start` opens an
HTTP port. `##humanoid channel start` connects Slack / WhatsApp / Telegram to a
live agent. `##cron add` schedules work that runs when nobody is watching.
Confirm before any of them, every time — approval for one is not approval for
the next.

And: deletes are irreversible and take processes with them. `##pane delete`,
`##tab delete`, `##lane delete`, `##panegroup delete` end whatever was running
in there. `##worktree remove` preserves a dirty tree but drops a clean one.

---

## The command map

| Family | For | Most used |
|---|---|---|
| `##session` | this session | `##session`, `##session list`, `##session reload` |
| `##rysh` | the session's own surfaces | `##rysh web start\|stop\|status\|token`, `##rysh tab name`, `##rysh lane name` |
| `##new` | layout | `##new tab`, `##new lane`, `##new pane`, `##new stack <N>`, `##new grid <L>x<P>` |
| `##tab` `##lane` `##panegroup`/`##pg` `##pane` | inspect / rename / delete each level | `… list`, `… info`, `##pane name`, `##pane delete <id>` |
| `##pane meta` | per-pane notes for whoever drives it | `##pane meta list`, `##pane meta set <k> <v>`, `##pane list --meta` |
| `##pane new --claude` | a pane running claude, session id recorded | `##pane new --claude review the diff` |
| `##cmd` | run bash across a scope | `##cmd stack pwd`, `##cmd ws --running shell git status -s`, `##cmd stack --capture make test` |
| `##mode` | a pane's input modes | `##mode list`, `##mode new prompt`, `##mode delete chat` |
| `##llm` / `##<scope> model` | model selection and its hierarchy | `##llm status`, `##llm list`, `##llm scopes`, `##pane model <p>/<n>` |
| `##secret` `##variable` `##snat` | named values, and what the provider may see | `##secret list`, `##var list`, `##snat status` |
| `##cost` `##policy` | spend and guardrails | `##cost`, `##cost week`, `##cost budget <n>`, `##policy` |
| `##proxy` | governance of wrapped CLIs | `##proxy status`, `##proxy audit`, `##proxy check <cli>` |
| `##agent` `##humanoid` `@name` | autonomous workers | `##agent list`, `##humanoid list`, `@name <prompt>` |
| `##worktree` | isolated git work | `##worktree list`, `##worktree new <branch>`, `##worktree merge <b>` |
| `##mcp` `##forge` `##integration` | external tools | `##mcp list`, `##mcp tools <name>` |
| `##share` `##unshare` `##upstream` | collaboration (**outward-facing**) | `##share status`, `##share list` |
| `##replay` `##snap` `##public/##private pane print` | captured output | `##replay status`, `##snap public` |
| `##cron` | scheduled inputs (**runs unattended**) | `##cron list`, `##cron logs` |
| `##hop` `##grounding` `##image` `##auto` `##pipe` | per-pane behaviour | `##hop status`, `##grounding` |

---

## Recipes

**Where am I, and what is running?**

```sh
python3 $C where && python3 $C '##session' && python3 $C '##pane list'
```

**What model is this pane actually on, and why?**

```sh
python3 $C '##llm status'   # the effective model
python3 $C '##llm scopes'   # session > workspace > tab > lane > stack > pane — narrowest wins
```

**Spend.** `##cost` for this session, `##cost week` for 7 days, `##cost budget 500k`
to set a ceiling. Worth checking before a big fan-out, not after.

**Open working panes.** `##new stack 3` adds three to the active group;
`##new grid 2x3` builds a lane×pane grid; `##pane new --worktree <branch>` opens
one in its own git worktree (branch `pane/<alias>`, removed on close if clean).

**Rename for legibility.** `##pane name <id> <name>` and `##rysh tab name <name>`
— names work as selectors afterwards, which beats copying uuids around.

**Isolated work.** `##worktree new <branch>` → `##worktree cwd <branch>` (new panes
in the group start there) → `##worktree merge <branch>` shows a diff and needs
`--confirm`. This is the right shape for parallel edits: one writer per tree.

**Proxy state.** `##proxy status`, `##proxy audit`. Caution: an audit of this
package flagged — unverified — that `##proxy check` may clear the session's proxy
endpoint when it finishes, silently disabling injection for panes opened after it.
Re-check `##proxy status` afterwards.

---

## Driving panes, and when not to

`rysh send <session> <text> [--pane <id>]` is fire-and-forget. `rysh prompt
--pane-id <id> -- '<text>'` blocks until the agentic turn ends. `##prompt` does
the same inside a `.rysh` script, leaving the answer in `$RYSH_OUT`.

When the work is "run a whole task in another pane and get an answer back", use
**`/rysh-fanout`** instead — it spawns panes, starts real claudes in them, waits
on the right signals, and returns what each one said.

## Scripting

`rysh script <file.rysh>` runs bash whose `##`-prefixed lines are rysh commands;
`$RYSH_OUT` holds the last command's output and `$RYSH_STATUS` its exit code. A
script running inside a pane inherits that pane's `$RYSH_SESSION/TAB/LANE/STACK/PANE`,
so it targets itself by default. `--check` verifies the file is valid as both
rysh and plain bash.
