---
name: aiwos-validate
description: Validate a submitted AI Work OS work package in layers (deterministic checks, independent review, requirement and goal fit), diagnose and repair failures, integrate (merge/PR) and complete it only with evidence. Use after `aiwos work submit`, when checking whether work is really done, or when a goal's success criteria must be verified.
argument-hint: "WP-00N | GOAL-00N"
---

# Validate and complete

Target: $ARGUMENTS

## A. Work package (WP-00N)

1. **Technical (deterministic)** — `aiwos validate WP-00N`. This runs the package's declared commands plus
   structural checks (scope, declared outputs) and records the result. You interpret results; you never invent
   them. `INCOMPLETE` means there were no runnable checks: add real ones (`aiwos work update`) — do not proceed without.
   If it refuses because commands were "not written or approved on this machine" (they came from a teammate via
   `aiwos sync`), show the user the exact commands and ask. Only after the user approves, run
   `aiwos validate WP-00N --approve`. Never approve commands on your own judgment; they run as code on this machine.
2. **On FAIL** — diagnose from the recorded output, repair inside the package scope, commit, `aiwos work submit`
   again, re-validate. If the failure shows the plan is wrong, replan (`/aiwos-plan`) instead of patching around it.
   After 3 failed repair cycles, block the package with a clear reason and escalate to the user.
3. **Independent review** (required for risk medium/high or a `review` validation entry) — spawn the
   `aiwos-reviewer` subagent (use the model from `aiwos route WP-00N --kind review`) with only:
   the WP id, the branch/worktree, and the instruction to run `aiwos context WP-00N`. Do **not** give it your
   reasoning or conclusions — it must not inherit the builder's assumptions. It records its own verdict with
   `aiwos review`. A failed review is handled like a failed check (step 2).
4. **Requirement fit** — walk through every success criterion of the package and point to the evidence
   (test name, file, validation id). Any criterion without evidence → not done.
5. **Integrate** — merge the work branch into the base branch (or open a PR: `gh pr create` with the goal,
   WP id, validation and review ids in the body; follow the project's review/CI rules). Merging to a shared
   branch or pushing is an outward-facing action: confirm with the user unless they already authorized it.
6. **Complete** — `aiwos work gate WP-00N` shows anything still missing; `aiwos work complete WP-00N` enforces it.
   Dependents become READY and their owners are notified automatically.

Status words to use when reporting: COMPLETE, READY_FOR_REVIEW, VALIDATION_FAILED, PARTIAL, BLOCKED. Never "done" without the gate.

## B. Goal (GOAL-00N)

When every package is COMPLETE: check each goal success criterion against real evidence, run any goal-level
validation the goal lists, and ask: *does this actually achieve the original outcome?* A passing build is not proof.
Then complete with evidence per criterion index:
```bash
aiwos goal complete GOAL-00N --in '{"0": "V-1a2b (e2e checkout test)", "1": "workspace/delivery/report.md"}'
```
Afterwards run `/aiwos-cleanup` for the milestone audit.
