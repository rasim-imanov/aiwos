---
name: aiwos-cleanup
description: Milestone workspace audit for AI Work OS projects — find duplicates, stale plans, unbounded TEMPORARY code, orphaned or scattered files, broken references and stale decisions, then clean carefully with evidence. Use after completing a goal or major work package, or when the project feels cluttered.
---

# Workspace audit and cleanup

1. Gather facts (read-only):
   - `aiwos audit` — root clutter, suspect duplicate names (final2.md…), empty dirs, TEMPORARY markers without a replacement condition.
   - `aiwos doctor` — graph problems, stale owners, missing worktrees/contracts, decisions without status.
   - `aiwos status` — finished goals whose temporary artifacts can go.
2. For each finding decide with evidence: keep (and allow-list), merge into the canonical file, move into
   `workspace/<domain>/` or `knowledge/`, archive, or delete. Use `aiwos trace <path>` to learn why a file exists.
3. Delete only when there is sufficient evidence it is obsolete (superseded, merged, or a temporary artifact
   of a completed package). Anything uncertain → list it for the user instead of deleting.
4. Superseded decisions: `aiwos decision supersede D-00X --by D-00Y` (never edit history away).
5. Do the cleanup on a branch, commit with a summary of what moved/removed and why, and report.
