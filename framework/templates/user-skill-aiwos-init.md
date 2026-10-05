---
name: aiwos-init
description: Install or upgrade AI Work OS (goal-oriented multi-session coordination framework) in the current project. Use when the user asks to set up, install, add, or upgrade AI Work OS / aiwos in a project.
disable-model-invocation: true
---

# Install AI Work OS into this project

Framework source: `{{FRAMEWORK_DIR}}`

1. Inspect first. Run `git status --short 2>/dev/null | head` and list `.claude/`, `CLAUDE.md`, `AGENTS.md`.
   - No git repo: recommend `git init` + an initial commit (needed for branches/worktrees/sync). Ask before doing it.
   - Uncommitted changes: tell the user; the installer backs up what it touches to `.ai/backups/`, but a commit is the real safety net.
2. If the project already has agents, skills, commands, hooks or long CLAUDE.md files, it is an **existing AI project**:
   preview with `"{{PYTHON}}" "{{FRAMEWORK_DIR}}/aiwos_main.py" init --dry-run .` and show the user what will change.
3. Install: `"{{PYTHON}}" "{{FRAMEWORK_DIR}}/aiwos_main.py" init .`
   (merges hooks into `.claude/settings.json`, appends a marked block to `CLAUDE.md`, adds `aiwos-*` skills/agents,
   vendors the runtime into `.ai/runtime`). Report the installer log.
4. Existing AI project: continue with `/aiwos-adopt` after the restart (analyze before changing anything).
5. Tell the user to restart Claude Code in this project so the hooks load, then describe their goal (or run `/aiwos-goal`).
