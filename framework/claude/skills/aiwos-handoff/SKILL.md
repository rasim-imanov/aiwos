---
name: aiwos-handoff
description: Hand an in-progress AI Work OS work package to another session (or a future you) with a compact, reference-based handoff, releasing ownership and claims. Use when stopping mid-task, when context is getting large, when the user switches sessions or people, or when another session should continue.
argument-hint: "WP-00N"
---

# Hand off WP $ARGUMENTS

1. Commit work in progress on the work branch (`WP-00N: wip <what>`) so nothing lives only in the worktree.
2. If useful, run `aiwos validate $ARGUMENTS` so the handoff carries a real validation state.
3. Create the handoff — short sentences, references not copies (paths, decision ids, validation ids):
```bash
aiwos handoff $ARGUMENTS \
  --completed "what is done (with file paths)" \
  --remaining "what is left, in order" \
  --problems "known failures, risks, surprises" \
  --next "the single recommended next action" \
  --decisions "D-004,D-007"
```
   Changed files and validation status are added automatically. Ownership and claims are released, so the
   package appears in `aiwos next` for any session. Use `--keep` for a checkpoint without releasing.
4. Tell the user the handoff id and how to continue: in any session, `/aiwos-work $ARGUMENTS`.
