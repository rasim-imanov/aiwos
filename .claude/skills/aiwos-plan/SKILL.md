---
name: aiwos-plan
description: Decompose a confirmed AI Work OS goal into a dependency graph of bounded work packages with resources, contracts, success criteria and validation commands; detect parallel-work conflicts before execution. Use after a goal is confirmed, or when validation/research shows the plan must change (replanning).
argument-hint: "GOAL-001"
---

# Plan a goal

Goal: $ARGUMENTS

## 1. Load only what planning needs
`aiwos goal show $ARGUMENTS` and `aiwos work list $ARGUMENTS`. The goal must be CONFIRMED/ACTIVE; if not, go back
to `/aiwos-goal`. Inspect only the parts of the repository the goal touches.

## 2. Decompose from the actual goal
- Derive work packages from *this* goal's outcome and success criteria — never from a generic template.
- Each package must be independently understandable and, where possible, independently executable.
- Prefer few, meaningful packages (typically 2–8). A trivial goal may be one package.
- Every package needs: `title`, `purpose`, `success_criteria`, and usually `resources` (the files/globs it may
  write — the narrower, the better), `depends_on`, `validation`, `risk` (low|medium|high), `complexity`,
  `kind` (code|doc|research|design|ops), `expertise`, `outputs`, `contracts`.
- `validation` entries are deterministic commands first: `{"name": "tests", "run": "npm test -- payment"}`; add
  `{"name": "review", "layer": "review"}` when an independent review is required, `{"name": "...", "layer": "manual"}`
  for checks only a human can do.
- Definition of done covers everything relevant, not the happy path: error handling, edge cases, security,
  tests, accessibility, docs, cleanup.

## 3. Enable parallelism with contracts
When two packages depend on a shared interface (API, schema, event, component props, file format), add a small
first package that writes the contract into `workspace/specs/` or `workspace/contracts/`, then register it:
`aiwos contract add <name> <path> --goal <G> --provider WP-a --consumers WP-b,WP-c`. Dependents may then run in parallel.

## 4. Add the plan (one batch, keys for local references)
```bash
aiwos work add $ARGUMENTS --in - <<'EOF'
[
 {"key": "api", "title": "Payment API contract", "purpose": "...", "kind": "doc", "risk": "low",
  "resources": ["workspace/specs/payment-api.md"], "success_criteria": ["..."],
  "validation": [{"name": "lint-spec", "run": "..."}]},
 {"key": "be", "title": "Backend payment service", "depends_on": ["api"], "resources": ["workspace/backend/payment/**"],
  "purpose": "...", "success_criteria": ["..."], "risk": "high", "complexity": "high", "expertise": ["backend"],
  "contracts": ["payment-api"], "validation": [{"name": "unit", "run": "..."}, {"name": "review", "layer": "review"}]}
]
EOF
```
The coordinator rejects cycles and unknown dependencies, and warns about packages that could run concurrently
while writing overlapping resources. **Resolve every warning**: add `depends_on` (serialize), narrow `resources`
(split), or introduce a contract. Use `aiwos work update WP-00N --in -` to amend.

## 5. Record decisions
Plan-shaping choices (architecture, technology, scope cuts) become decision records:
`aiwos decision new "Use Stripe Billing" --goal <G>`; fill in options/evidence; accept after the user agrees
if it is a major/irreversible choice. Location for code: an appropriate `workspace/<domain>/` subdirectory, never the root.

## 6. Checkpoint
Show `aiwos work graph $ARGUMENTS` and, per package, the recommended model tier (`aiwos route WP-00N`).
Ask for confirmation only if the plan contains major scope/architecture decisions the user has not seen;
otherwise proceed to `/aiwos-work`.

Replanning: when evidence invalidates the plan, update or add packages (never silently delete history);
fail obsolete ones with `aiwos work fail WP-00N --reason "superseded by WP-0XX"`.
