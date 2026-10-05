# AI Work OS

A reusable, goal-oriented operating layer for Claude Code. You say what you want to achieve. Claude turns
that into a confirmed **goal** and a graph of bounded **work packages**, then executes them in one or more
sessions. Every session claims what it works on, sees only the context it needs, and can only call work
complete after real validation.

Install it into any project, new or existing. The installer copies a small coordinator CLI
(Python 3.9+, standard library only) plus Claude Code skills, subagents and hooks. No server, no database,
no dependencies.

* **User manual: [docs/user-manual.md](docs/user-manual.md)** · Design: [docs/architecture.md](docs/architecture.md) · Protocol: [docs/acp.md](docs/acp.md) · Original spec: [instructions.md](instructions.md)

## Install into a project

```bash
# from anywhere; target is the project directory (a git repo with at least one commit is recommended)
python3 /path/to/work-os/framework/aiwos_main.py init /path/to/project
#   --dry-run       show what would change
#   --user-skill    also install a personal /aiwos-init skill, so you can type /aiwos-init in any project later
```

Then restart Claude Code in that project so the hooks load. Commit the new `.ai/`, `.claude/` and
`CLAUDE.md` changes so teammates and worktrees get them.

What the installer does (non-destructive: everything it modifies is backed up to `.ai/backups/`):

| Path | Content |
|---|---|
| `.ai/runtime/`, `.ai/bin/aiwos` | the coordinator (vendored, versioned) and launchers (`aiwos` for sh/Git Bash, `aiwos.cmd` for cmd/PowerShell) |
| `.ai/config.json` | lease time, enforcement mode, sync remote, review policy, model routing |
| `.ai/state/` (git-ignored) | coordination state: goals, work, sessions, claims, events, validation, handoffs |
| `.claude/skills/aiwos-*`, `.claude/agents/aiwos-*` | lifecycle skills and reviewer/researcher/worker subagents |
| `.claude/settings.json` | five hooks merged in (your existing hooks and settings are kept) + permission to run `.ai/bin/aiwos` |
| `CLAUDE.md` | a ~30-line rules block between `<!-- aiwos:begin/end -->` markers (your text is kept) |

Upgrade: run `init` again (files you modified are kept and reported). Remove: `aiwos uninstall` (keeps your
goals, decisions and workspace).

## Use it

Just describe what you want. For anything multi-step, Claude follows the lifecycle (you can also call the steps directly):

| Step | Command | What happens |
|---|---|---|
| Goal | `/aiwos-goal I want …` | understands, asks only the questions that change the result (A/B/C), researches what decisions depend on, proposes a goal → **you confirm** |
| Plan | `/aiwos-plan GOAL-001` | work packages with dependencies, write scopes, contracts and validation commands; overlaps are flagged before any work starts |
| Work | `/aiwos-work` | picks ready work, claims it, creates a branch + worktree, loads the minimal context, implements within scope, submits |
| Validate | `/aiwos-validate WP-003` | runs the real checks and an independent reviewer subagent, then repair or replan if needed, merge, and a gated completion |
| Hand off | `/aiwos-handoff WP-003` | compact, reference-based handoff; the package goes back to the pool |
| Team | `/aiwos-coordinate` | inbox, conflicts, crashed-session recovery, multi-machine sync |
| Adopt | `/aiwos-adopt` | turns an existing AI project into a compatible one without losing capabilities |
| Clean | `/aiwos-cleanup` | milestone audit: duplicates, unbounded TEMPORARY code, clutter, stale decisions |

Useful CLI commands (`aiwos` is on PATH inside Claude Code; otherwise `sh .ai/bin/aiwos` or `.ai\bin\aiwos.cmd`):

```
aiwos status                 goals, work, live sessions, claims, conflicts
aiwos next                   work you can safely take now
aiwos context WP-003         minimal context for one package
aiwos inbox                  messages relevant to you (≥ IMPORTANT, directed requests)
aiwos work gate WP-003       what still blocks completion
aiwos recover                free work held by dead sessions
aiwos sync                   share coordination state with teammates (git)
aiwos doctor | audit | metrics | trace <file>
```

## Several sessions, several people

* **Same machine:** open more Claude Code sessions. They share `.ai/state`, take different packages
  (`aiwos next`), and each works in its own worktree under `.ai/worktrees/`. The write hook blocks edits to
  another session's claimed files and gives a reason you can act on.
* **Several machines/people:** set `"sync": {"remote": "origin"}` in `.ai/config.json` and commit it. Then
  `aiwos sync` before and after claiming or completing work. State travels on the `aiwos-state` branch,
  separately from code, and always merges without conflicts. Code integrates through normal PRs.
* **Crashes:** a session that stops renewing its lease (30 min default) loses its claims. Its work shows up in
  `aiwos next` as `[RECOVER]` and continues on the same branch.

## Develop the framework

```bash
python3 -m unittest discover -s tests      # 52 tests; uses temporary git repos, needs git on PATH
```

The source lives in `framework/` (`aiwos/` package, `claude/` skills and agents, `templates/`, `bin/`). The
tests cover the spec §76 matrix and the §93 scenarios: new project, adoption, two people on two clones,
three or more sessions, session failure, conflicts, context bounds, and validation failure.

Known limitations are listed in [docs/architecture.md §12](docs/architecture.md#12-known-limitations-honest-list).
