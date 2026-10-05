---
name: aiwos-adopt
description: Adopt an existing AI / Claude Code project into AI Work OS without losing capabilities — discover and inventory agents, skills, commands, hooks, rules and instructions, build a project model and capability map, then migrate incrementally (analyze, normalize, optimize, modernize) with validation at each stage. Use when installing AI Work OS into a project that already has .claude/ config, CLAUDE.md/AGENTS.md instructions, or custom AI workflows.
argument-hint: "[analyze|normalize|optimize|modernize]"
---

# Adopt an existing AI project

Level requested: $ARGUMENTS (default **analyze** — read-only).
Principle: *the existing project may contain valuable capabilities.* Analyze first, modify second. Never rewrite blindly.

## Levels
| Level | Changes | Allowed actions |
|---|---|---|
| analyze | none (writes only `workspace/migration/`) | discover, inventory, model, map |
| normalize | structure only, behavior preserved | move/rename/dedupe with adapters; validate equivalence |
| optimize | behavior improved | context, duplication, agents, routing, validation |
| modernize | full framework adoption | goals/work packages/ACP for all workflows |
Each level above analyze requires a goal (`/aiwos-goal`) and user confirmation of the migration plan.

## 1. Safety
- `git status` — require a clean tree or a commit/branch before anything beyond analyze. No git → ask to `git init` and commit.
- The installer already backed up anything it touched in `.ai/backups/<timestamp>/`.

## 2. Discover and inventory (deterministic)
`aiwos adopt scan` → `workspace/migration/project-model.json`, `project-model.md`, `capability-map.json`.
It finds instruction files, agents, skills, commands, rules, hooks (and broken hook scripts), MCP servers,
same-name and similar-purpose duplicates, duplicated instructions, possible always/never conflicts, and references.

## 3. Understand (model)
Read `project-model.md`, then only the files needed to understand each mechanism. Complete the model with
what a scan cannot see: purpose, entry points, implicit workflows (e.g. "skill A is always run after agent B"),
environment assumptions, external integrations, generated files, hidden coupling, **invariants** (behaviors
that must not change). Write it to `workspace/migration/understanding.md` (short; tables).

## 4. Capability map
Edit `workspace/migration/capability-map.json`. For every capability set `status` to one of
PRESERVED, IMPROVED, REPLACED, DEPRECATED, REMOVED, PARTIAL, UNKNOWN, plus `new_implementation`,
`behavioral_differences` and `validation` (how equivalence was or will be checked).
Resolve conflicts and duplicates explicitly (keep one canonical source, map the rest).
Use compatibility adapters instead of renames for taste: an existing command can stay and simply invoke the
new workflow (legacy capability → adapter → canonical capability → ACP).

## 5. Migration plan → user checkpoint
Write `workspace/migration/plan.md`: ordered, incremental stages; each with changes, rollback, validation.
Structural normalization first, behavioral changes later, validated separately. Present it; get confirmation.
Then turn it into a goal + work packages (`/aiwos-plan`) so migration runs through the normal gates.

## 6. Migrate incrementally
One stage per work package, each validated (run the old and new path where possible and compare). Never delete
a capability's old implementation until its replacement is validated and the map says REPLACED/REMOVED.

## 7. Verify
`aiwos adopt verify` must report every capability accounted for (no UNKNOWN, no PARTIAL, implementations exist,
replacements validated). Do not call the migration complete otherwise. Re-run `aiwos adopt scan` to refresh the model.
