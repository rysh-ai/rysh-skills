# rysh-skills

Agent skills that teach **Claude Code** and **Codex** to use [rysh](https://rysh.ai) —
the agentic terminal multiplexer — from inside the session they are running in.

A coding agent that can only edit files is one worker. A coding agent that can drive
rysh can open panes and tabs, start more agents next to itself, hand each one a prompt,
watch them work, and collect the answers. These skills are what make that second thing
possible: they carry the `##` command surface, the pane/lane addressing rules, and the
sharp edges you only learn by hitting them.

What they cover:

1. **Agent board and agent fleet** — stand up a three-tier org chart of agents (one CEO,
   N managers, N workers), each in its own pane and its own git worktree, talking to each
   other through an addressed envelope, with every delivered message mirrored onto rysh's
   agents board.
2. **Panes, tabs and prompts** — create panes and tabs, launch a fresh agent session in
   each one, send prompts and follow-up turns into a sibling session, read back what it
   said, and tear it all down — all via `rysh` `##` commands.

Everything the skills spawn is a **real interactive session** in a visible pane. The user
can watch it, type into it, or take it over at any point. Nothing runs headless.

---

## The skills

| Skill | What it does |
| --- | --- |
| [`rysh`](skills/rysh) | Operate the current session: tabs, lanes, stacks, panes, input modes, secrets and variables, LLM model bindings, spend, policy, the governance proxy, cron, worktrees, MCP servers, sharing, the web UI. Any `##` command, with the binary/session/cwd resolution that trips everyone up already handled. |
| [`rysh-fanout`](skills/rysh-fanout) | Fan work out to sibling panes: spawn N panes, start an agent in each with its own prompt, wait for them, read their answers, send follow-up turns, close them. Panes are named after their task, so `##pane list` reads like a task board. |
| [`rysh-fleet`](skills/rysh-fleet) | A persistent org chart on top of `rysh-fanout`: CEO → managers → workers, paired by unit, one worktree each. Adds cross-tab addressing, a from-me message envelope, chain-of-command routing (`msg` down, `report` up), and the agents-board mirror. Units come from a directory of docs, one doc split by heading, or the shape of the codebase. |

`rysh-fleet` builds on `rysh-fanout` as a library. It finds it as a sibling directory
first, then falls back to the standard skill locations, so a split install still works —
but installing the pair together is the simple path.

---

## Install

Each skill is a directory with a `SKILL.md` and its helper scripts. The helpers are
**stdlib-only Python 3** — nothing to install, no dependencies.

### Claude Code

Per project:

```sh
git clone https://github.com/rysh-ai/rysh-skills.git
mkdir -p .claude/skills
cp -R rysh-skills/skills/rysh rysh-skills/skills/rysh-fanout rysh-skills/skills/rysh-fleet .claude/skills/
```

Or for every project, into your personal skills directory:

```sh
mkdir -p ~/.claude/skills
cp -R rysh-skills/skills/* ~/.claude/skills/
```

Then `/rysh`, `/rysh-fanout`, `/rysh-fleet` — or just describe what you want
("open four panes and give each one an epic") and the skill triggers on its own.

### Codex

Codex reads the same `SKILL.md` format:

```sh
mkdir -p "${CODEX_HOME:-$HOME/.codex}/skills"
cp -R rysh-skills/skills/* "${CODEX_HOME:-$HOME/.codex}/skills/"
```

### Anywhere else

Install location is not baked in anywhere. Each `SKILL.md` opens by resolving its own
helper, probing — in order — `$RYSH_SKILLS_DIR`, `$CLAUDE_PLUGIN_ROOT/skills`,
`.claude/skills`, `~/.claude/skills`, and `$CODEX_HOME/skills` (default `~/.codex`):

```sh
for d in "${RYSH_SKILLS_DIR:-}" "${CLAUDE_PLUGIN_ROOT:-}/skills" .claude/skills "$HOME/.claude/skills" "${CODEX_HOME:-$HOME/.codex}/skills"; do
  if [ -f "$d/rysh/scripts/ryshctl.py" ]; then C="$d/rysh/scripts/ryshctl.py"; break; fi
done
```

For a layout none of those predict, put the skills wherever you like and export
`RYSH_SKILLS_DIR=/path/to/skills`. The helper scripts locate themselves and each other
from `__file__`, so `rysh-fleet` finds `rysh-fanout` as a sibling no matter where the
pair lives; `RYSHFAN_DIR` overrides that one lookup on its own. Fleet briefs are
rendered with absolute paths, so every agent in a fleet gets a working invocation
regardless of its own working directory.

---

## Requirements

- [rysh](https://rysh.ai) installed, and the agent running **inside a rysh pane** —
  the pane exports `$RYSH_SESSION` and `$RYSH_PANE`, which is how the skills know which
  session and which pane they are. rysh state is project-local, so the working directory
  matters too; the helpers resolve it for you.
- Python 3 (stdlib only).
- A daemon from **2026-08-05** or later for `$RYSH_PANE` self-identification. Older
  daemons still work, with the caveats each `SKILL.md` spells out.

The child sessions that `rysh-fanout` and `rysh-fleet` launch today are `claude`
sessions. The skills themselves are agent-agnostic — Claude Code and Codex both read
them and can drive rysh equally — but spawning a Codex child is not wired up yet.

---

## Try it

```sh
# resolve the helpers wherever you installed them
S=$(for d in "${RYSH_SKILLS_DIR:-}" "${CLAUDE_PLUGIN_ROOT:-}/skills" .claude/skills \
             "$HOME/.claude/skills" "${CODEX_HOME:-$HOME/.codex}/skills"; do
      if [ -d "$d/rysh-fanout" ]; then printf %s "$d"; break; fi
    done)

# what session am I in, what's open, what has it cost?
python3 "$S/rysh/scripts/ryshctl.py" dashboard

# three panes, three agents, three prompts, in parallel
R="$S/rysh-fanout/scripts/ryshfan.py"
python3 "$R" spawn --count 3
python3 "$R" start <child-id> --prompt-file /tmp/task-a.md
python3 "$R" wait  <child-id> --timeout 900

# a whole fleet from a roadmap directory
F="$S/rysh-fleet/scripts/fleetctl.py"
python3 "$F" units --from roadmap/            # see the units first — always
python3 "$F" --fleet epics up --from roadmap/ --workers 1 --worktrees
python3 "$F" --fleet epics tree
```

Every helper prints JSON (except `tree` and `screen`), so the agent parses instead of
scraping, and exit status is trustworthy.

---

## A word on cost

A fan-out of N children is N full agent sessions, and a fleet of 22 units with one worker
each is 45 of them. Both helpers check the session's token ceiling before spawning and
refuse when it is spent — `--force` overrides. Read that number before you scale up.

---

## License

Apache-2.0 — see [LICENSE](LICENSE).
