# AI Work OS — Architecture (v0.1)

This is the architecture proposal required by spec §90, updated to match what is actually built.
Spec: [`instructions.md`](../instructions.md). Protocol: [`acp.md`](acp.md).

## 1. Shape of the system

```
 Claude Code session ──skills──▶ aiwos CLI ──▶ coordination state (.ai/state, JSON/JSONL)
        │  ▲                        │                    │
        │  └──hooks (session-start, │                    └── aiwos sync ──▶ git branch `aiwos-state` (remote)
        │      prompt, pre/post-    │
        │      write, session-end)  └──▶ git (branches, worktrees, merges)  ──▶ GitHub PRs/CI
        ▼
 human-facing artifacts: workspace/  knowledge/  knowledge/decisions/
```

Three parts, deliberately small:

| Part | What it is | Why |
|---|---|---|
| **`aiwos` coordinator** | Python 3.9+ CLI, standard library only, vendored into each project at `.ai/runtime/` | Deterministic state transitions, gates and conflict checks belong in code, not prompts. No install step, no server, works offline. |
| **Claude Code layer** | 8 skills (`/aiwos-goal`, `-plan`, `-work`, `-validate`, `-handoff`, `-coordinate`, `-adopt`, `-cleanup`), 3 subagents (`aiwos-reviewer`, `-researcher`, `-worker`), 5 hooks, a short CLAUDE.md block | Skills carry the judgment-heavy lifecycle and load only when used. Hooks enforce what must not depend on the model remembering. |
| **Git** | Branches + worktrees per work package; a separate state branch for cross-machine sync | Git is the source-control infrastructure; the framework doesn't rebuild it. |

## 2. Canonical model → implementation

| Concept (spec §9) | Implementation |
|---|---|
| Goal | `state/goals/GOAL-NNN/goal.json` (intent, outcome, success criteria, scope, non-goals, constraints, assumptions, unknowns, risks, approvals) |
| Objective / Milestone | optional `objective` label on work packages (no separate entity until needed) |
| Work Package | `state/goals/GOAL-NNN/work/WP-NNN.json` (definition). **Status is derived** from events. |
| Task | lives inside a session/subagent; not persisted (transient context) |
| Actor | `from.actor` + `actor_type` (HUMAN, AI, AUTOMATION, SYSTEM) on every event |
| Session | `state/sessions/S-xxxxxxxx.json`, lease = `last_seen + lease_ttl_seconds` |
| Claim | `state/claims/<session>.json` — `READ` / `WRITE` / `EXCLUSIVE` on path globs or `work:WP-NNN` |
| Event / Command | ACP v1 messages in `state/events/<writer>.jsonl` (append-only) |
| Handoff | `state/handoffs/H-*.md` + `HANDOFF_CREATED` event |
| Validation | `state/validation/V-*.json` (+ `VALIDATION_PASSED/FAILED` events) |
| Decision | `knowledge/decisions/D-NNN-*.md` (ADR with `Status:` line; supersede, never rewrite) |
| Contract | any file (usually `workspace/specs/…`) registered by `CONTRACT_CREATED` with a content hash |
| Artifact | files in `workspace/` / `knowledge/`; provenance via `ARTIFACT_*` events, `aiwos trace` |
| Agent / Skill / Workflow | Claude Code subagents / skills; the lifecycle is the workflow |
| Rule | CLAUDE.md block (framework rules) + project CLAUDE.md (project rules) + hooks (enforced rules) |
| Context | `aiwos context WP` projection; `session_brief` at start; inbox filtering |

Other runtimes map onto the same CLI + ACP; nothing in the state model is Claude-specific
except the hook adapter (`hooks.py`).

## 3. Lifecycle

```
/aiwos-goal   understand → high-value questions → focused research → goal PROPOSED → user confirms → CONFIRMED
/aiwos-plan   decompose → work graph (cycle check, parallel-overlap check) → contracts → decisions
/aiwos-work   aiwos next → claim (atomic) → start (branch+worktree) → context projection → execute in scope → submit
/aiwos-validate  deterministic checks → independent review → requirement fit → merge/PR → gated complete
                 └─ FAIL → diagnose → repair/replan → resubmit (validation state resets)
goal complete    every package COMPLETE + evidence for every success criterion
/aiwos-cleanup   milestone audit
```

Loops are first-class: a goal change sends it back to PROPOSED; a resubmission invalidates old validation;
failed packages can be replanned; dead sessions' work returns to the pool.

## 4. State and sources of truth

| Information | Authority | Location |
|---|---|---|
| Goal intent, success criteria | goal record | `.ai/state/goals/` |
| Current work status, ownership | event log (folded) | `.ai/state/events/` |
| Who may write what | claims + session leases | `.ai/state/claims/`, `sessions/` |
| Source code / documents | Git | the repository |
| Decisions | decision records | `knowledge/decisions/` |
| Research | research artifacts | `workspace/research/` |
| Validation results | validation records | `.ai/state/validation/` |
| Human approval | `approvals[]` on the goal + `GOAL_CONFIRMED` event | goal record |
| Framework configuration | `.ai/config.json` (committed) | |

**Where `.ai/state` lives.** In the *main* worktree, git-ignored, so all local worktrees/sessions share one
state without polluting code branches. (Resolved without spawning git by reading `.git` / `commondir`.)

**Single-writer rule.** Every mutable file has one writer: a session's own events/claims/session file.
Goal and work-package definitions are the exception and merge newest-wins with `updated_at`. This is what
makes cross-machine merging conflict-free.

**What is not stored:** conversations, reasoning traces, copied file contents, research bodies. Events
carry ids, statuses and references only.

## 5. Multi-session and multi-machine coordination

* **Claims with leases.** Claims are valid only while the owning session's lease is alive (heartbeat on every
  prompt and write, throttled to 1/min). A crashed session's claims lapse automatically after
  `lease_ttl_seconds` (default 30 min); `aiwos recover` also returns its packages to the pool.
* **Atomicity on one machine:** an `O_EXCL` lock file around check-and-claim; stale locks are broken after 30 s.
* **Across machines:** `aiwos sync` commits the state directory with Git plumbing to `refs/aiwos/state` and
  pushes it to branch `aiwos-state`. Merge is semantic: union of events by id, newest JSON document wins,
  goal-id collisions are parked as `*.conflict-*.json` and reported (BLOCKING). A rejected push re-fetches
  and retries — Git's ref update is the compare-and-swap.
* **Offline races:** two machines claiming overlapping resources before syncing produce a *contested* claim.
  Resolution is deterministic on every machine: the earlier `acquired_at` wins; the hook denies the loser's writes.
* **Conflict prevention order** (spec §20): at planning (`plan_check` warns about unordered packages with
  overlapping resources) → at claim time (atomic refusal) → at write time (PreToolUse hook) → at submit
  (scope check over the branch diff) → at merge (Git).
* **Pull-based scheduling:** `aiwos next` = confirmed goal ∧ READY ∧ unowned (or stale owner) ∧ no
  conflicting live claims ∧ expertise match; ordered by recoverable-first, then how much work it unblocks.
* **Event-driven readiness:** completing a package emits IMPORTANT notifications scoped to now-READY
  dependents; sessions see them in `aiwos inbox` and via the prompt hook — no polling of the full log.

## 6. Context strategy

1. Session start injects < 800 characters (active goals, owned work, stale work) — not the project.
2. `aiwos context WP` returns the projection: goal essentials, the package, dependency outputs as paths,
   relevant non-superseded decisions, contracts as path+hash, others' claims, unread important events.
   Typically 1–3 KB. Content is fetched lazily by the agent.
3. Inbox filtering: by scope (my session, my packages and their dependencies, my goal) and priority ≥ IMPORTANT.
4. Subagents receive references (WP id, paths), never pasted content; the reviewer receives *only* the WP id.
5. Skills load on demand; the always-loaded CLAUDE.md block is ~30 lines.

## 7. Validation strategy

| Layer | Mechanism |
|---|---|
| Structural | built-in: changed files ⊆ resources; declared outputs exist |
| Technical | the package's `validation[].run` commands, executed by the coordinator, real exit codes and output tails recorded |
| Requirement / quality | `aiwos-reviewer` subagent with fresh context records a verdict (`aiwos review`) |
| Goal | `aiwos goal complete` requires all packages COMPLETE + evidence per success criterion |

The **completion gate** (`aiwos work gate`) refuses unless: submitted; a passing validation since the last
submission; independent review passed for risk medium/high (reviewer ≠ builder session); branch merged into
base. "No runnable checks" is INCOMPLETE, never PASS. Contradictory evidence (tests pass, review fails) blocks completion.

## 8. Existing-project adoption

`aiwos adopt scan` (read-only) inventories instruction files, agents, skills, commands, rules, hooks, MCP
servers and scripts; finds broken hook scripts and imports, same-name and similar-purpose duplicates,
duplicated instructions, and possible always/never conflicts; maps references. Output: `project-model.json/.md`
and a seeded `capability-map.json` (every capability UNKNOWN). `/aiwos-adopt` drives understanding,
invariants, the migration plan (structural before behavioral), and incremental migration through normal
work packages. `aiwos adopt verify` is the gate: no UNKNOWN or PARTIAL, implementations exist,
replacements carry validation evidence. The installer itself is non-destructive (backups, settings merge,
marked CLAUDE.md block, `aiwos-` name prefix, `uninstall`).

## 9. Failure and recovery

| Failure | Behaviour |
|---|---|
| Claude session killed | lease expires → claims inert → `aiwos next` offers `[RECOVER]` → new owner resumes the same branch/worktree; late events from the dead session are ignored as stale |
| Machine restart | all state is on disk; sessions re-register on start |
| Torn write / crash mid-append | atomic replace for JSON; unparseable trailing JSONL line skipped |
| Hook error | fail-open (never breaks the session), logged to `.ai/state/.local/hook-errors.log` |
| Network / push failure | sync is retried by the user; local work continues; nothing is lost |
| Validation failure | VALIDATION_FAILED, gate closed, repair/replan loop |
| Inconsistent state | `aiwos doctor` lists graph errors, stale owners, missing worktrees/contracts, contested claims |

## 10. Security and trust

Instruction hierarchy is written into the CLAUDE.md block (framework > project > user requirements > agent
suggestions > external content; external text is data). Protected paths (`.ai/state`, `.ai/runtime`,
`.ai/bin`) can only be changed through the CLI. ACP rejects messages whose `from.session` differs from the
caller.

**Shared state is untrusted input.** Anyone who can push to the `aiwos-state` branch controls what `aiwos sync`
writes locally. Hardening after the v0.1 security review:

| Risk | Control |
|---|---|
| Validation commands (run with `shell=True`, by design) arriving through sync | A command runs only if its SHA-256 is in `.ai/state/.local/approved-commands.json`. That file is machine-local and never synced. `work add` / `work update` on this machine approve their own commands; anything else is refused until `aiwos validate WP --approve`, which the skills require the user to authorize |
| Path traversal from a crafted remote tree (`..`, Windows `..\`, drive letters, hidden or unknown folders) | `sync.safe_rel` allows only `[A-Za-z0-9._-]` names under `goals/ sessions/ claims/ events/ validation/ handoffs/`; only regular blobs ≤ 2 MB; a resolved-path check guards against symlinked folders. Rejected paths are reported and raise a BLOCKING event |
| Permission allowlist used to bypass prompts | Only read-only subcommands are pre-approved (`status`, `next`, `context`, `inbox`, `doctor`, `trace`, …). `validate`, `sync`, `init`, `uninstall`, `acp`, `work add/update` prompt as usual; upgrades remove the old broad `Bash(.ai/bin/aiwos *)` grant |
| Prompt injection through other sessions' messages | The prompt hook labels them as data and requests, never instructions |
| Local path leakage | The committed `.ai/manifest.json` no longer records the framework's absolute source path |

Covered by `tests/test_security.py`.

## 11. Certain / assumed / unknown

**Certain (verified in the docs on 2026-10-05 and/or by tests):** hook events, stdin fields and the
PreToolUse `permissionDecision` contract; SessionStart `additionalContext` and `CLAUDE_ENV_FILE`; skill
frontmatter (`description`, `argument-hint`, `disable-model-invocation`, `$ARGUMENTS`); subagent frontmatter
(`tools`, `model` aliases); shell-form hooks run in Git Bash on Windows; all 57 framework tests pass on
Windows with `core.autocrlf` both true and false.

**Assumed:** `CLAUDECODE=1` is present in Claude Code's Bash environment (used only for a session-id fallback
when `AIWOS_SESSION` is missing); `UserPromptSubmit` honours `hookSpecificOutput.additionalContext`;
GitHub accepts pushes of the `aiwos-state` branch under normal branch protection rules.

**Unknown / not yet verified:** behaviour of the hooks in a live Claude Code session on macOS/Linux (tested
on Windows via simulated hook payloads); `gh` PR creation (gh not installed here — the skill instructs it,
the CLI does not wrap it); performance on very large event logs (folding is O(events); fine for thousands).

## 12. Known limitations (honest list)

* Bash-tool writes (`echo > file`, `sed -i`) are not intercepted by the write hook; they *are* caught by the
  scope check at submit for branch-based work.
* Subagents inside one Claude session share that session's claims; true isolation between parallel workers
  comes from separate sessions/worktrees.
* Reviewer independence is procedural (fresh-context subagent, recorded reviewer identity), not cryptographic.
* Cross-machine coordination is eventually consistent: it is as fresh as the last `aiwos sync`.
* Model routing recommends tiers (configurable in `.ai/config.json`); skills apply them when spawning subagents.
  The main session's model is the user's choice.
* Metrics are derived from events (conflicts prevented, validation failures, handoffs, completion rate); file-read
  counts and token usage are not measured yet.

## 13. Roadmap

v0.2 candidates: a `PostToolUse(Bash)` git-status scope check; `aiwos pr` wrapping `gh` with goal/WP/validation
links; automatic periodic sync in hooks (opt-in); read-count/context-size metrics from transcripts; an MCP
server exposing ACP to other runtimes.
