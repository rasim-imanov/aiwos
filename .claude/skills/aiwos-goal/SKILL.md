---
name: aiwos-goal
description: Turn a rough intention into a confirmed AI Work OS goal (understand, ask only high-value questions, research what decisions depend on, propose, get confirmation). Use when the user describes something they want to build, achieve, research or change that is multi-step, ambiguous, costly or will involve several sessions — before planning or writing code. Also use when an existing goal's intent changes.
argument-hint: "[rough description of what you want]"
---

# Goal intake

Request: $ARGUMENTS

You are converting ambiguity into a usable goal. **Do not create source files, architecture, agents
or tasks yet.** Output of this skill: one goal in state with status CONFIRMED (or PROPOSED awaiting the user).

## 1. Orient (cheap, no questions yet)
- `aiwos status` — is there already a goal this belongs to? If yes, update it instead of creating a new one
  (`aiwos goal show GOAL-00N`).
- Look only at what the request touches (README, the relevant directory). Do not read the whole repository.

## 2. Understand
Determine, silently first: what is the user trying to achieve, why, what success means, constraints,
scope, non-goals, unknowns, and **which decisions materially change the result**.

## 3. Ask only high-value questions
- For each candidate question ask yourself: *would a different answer change what gets built?* If not, infer
  it and record it as an ASSUMPTION.
- Use AskUserQuestion with A/B/C options (max 4 questions per round, grouped by topic, recommended option first).
- Never ask what the repository or research can answer.

## 4. Research what decisions depend on
- Only for consequential decisions that depend on external facts (APIs, pricing, standards, limits, current
  versions). Keep it focused; for anything non-trivial delegate to the `aiwos-researcher` subagent with one precise question.
- Findings go to `workspace/research/<topic>.md` with sources; never invent sources or capabilities.

## 5. Draft and record the goal
Write the goal JSON (compact — omit empty fields) and create it:

```bash
aiwos goal new --in - <<'EOF'
{"title": "...", "intent": "why the user wants this",
 "outcome": "observable end state",
 "success_criteria": ["measurable / checkable statements"],
 "scope": ["..."], "non_goals": ["..."], "constraints": ["..."],
 "assumptions": ["ASSUMPTION: ... (inferred because ...)"],
 "unknowns": ["..."], "risks": ["..."], "research": ["workspace/research/x.md"],
 "status": "PROPOSED"}
EOF
```
Allowed fields: title, intent, outcome, success_criteria, scope, non_goals, constraints, assumptions,
unknowns, risks, research, decisions, owners, validation, status, notes.

## 6. Confirmation checkpoint (required)
Show the user `aiwos goal show <ID>` output in a short, readable form: outcome, success criteria, scope /
non-goals, and the assumptions you made. Ask them to confirm or correct (one AskUserQuestion).
- Confirmed → `aiwos goal confirm <ID> --by "<user name>" --note "<what they said>"`
- Corrected → `aiwos goal update <ID> --in -` with the patch, then re-confirm.
Never record a confirmation the user did not give.

## 7. Next
Tell the user the goal id and that the next step is `/aiwos-plan <ID>` (or do it now if they asked you to proceed).

If new information later changes the outcome, success criteria, scope or constraints, update the goal —
it automatically returns to PROPOSED and needs re-confirmation.
