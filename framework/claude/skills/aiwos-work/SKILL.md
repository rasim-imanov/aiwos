---
name: aiwos-work
description: Pick up and execute an AI Work OS work package inside its claimed scope — pull ready work, claim it, start a branch/worktree, load the minimal context projection, implement, commit and submit for validation. Use when continuing a planned goal, when asked "what can I work on", or when resuming/recovering work in a new session.
argument-hint: "[WP-00N]"
---

# Execute a work package

Requested: $ARGUMENTS (empty = choose the best available work)

## 1. Select (pull-based)
- `aiwos inbox` — handle BLOCKING messages and directed requests first.
- If no WP was given: `aiwos next` and take the first suggestion (items marked `[RECOVER]` were abandoned by a
  dead session — prefer finishing them). If nothing is ready, report why (`aiwos status`) and stop.

## 2. Claim and start
```bash
aiwos work claim WP-00N      # atomically claims the package + its resources; fails on conflict
aiwos work start WP-00N      # code packages: creates branch ai/wp-00n-… and worktree .ai/worktrees/WP-00N
```
- If claim reports a conflict, do **not** work around it: choose other work, or `aiwos send <session> REQUEST_RELEASE --work <WP>`.
- If `start` printed a worktree path, do all edits and commands inside that directory.
- If the claim mentions a handoff, read that handoff file first.

## 3. Load minimal context
`aiwos context WP-00N` — goal essentials, the package, dependency outputs, decisions, contracts, others' claims,
unread important events. Open referenced files **only** when the current step needs them. Do not read the
whole repository or other sessions' history.

## 4. Execute within scope
- Edit only paths inside `resources`. The pre-write hook denies edits to other sessions' claims and (by
  default) outside yours; if the scope is genuinely wrong, `aiwos work update WP-00N --in -` (resources) or
  `aiwos claim add '<glob>' --work WP-00N` and say why.
- Shared contract changes are coordination events: avoid them; if unavoidable, change the contract file,
  record a decision, and expect consumers to be notified (BLOCKING).
- Delegate bounded sub-tasks to subagents with the model tier from `aiwos route WP-00N`; pass references, not pasted content.
- Blocked by something external → `aiwos work block WP-00N --reason "..."`; need a human decision →
  `--needs-input`. Never silently continue around a blocker.
- Temporary code must be marked `TEMPORARY: <reason>; scope: <...>; replace when: <condition>`.
- Commit in the work branch with messages referencing the package: `WP-00N: <what>`.

## 5. Submit
`aiwos work submit WP-00N` — rejects uncommitted changes and out-of-scope files. Then run `/aiwos-validate WP-00N`.

## 6. If you must stop
Use `/aiwos-handoff WP-00N` so another session (or you later) can continue without rereading everything.
Never report work as complete here; completion happens only through the validation gate.
