# ACP v1 — Agent Communication Protocol

Internal coordination protocol of AI Work OS. Version string: `acp/1`.

> **Name.** "ACP" is also used by unrelated public protocols (IBM/BeeAI's Agent Communication Protocol and
> Zed's Agent Client Protocol). Here it is internal and always carries the version string `acp/<major>`, so
> there is no wire-level ambiguity. Rename if this framework is ever exposed publicly.

## Envelope

```json
{
  "protocol": "acp/1",
  "kind": "event",
  "type": "WORK_COMPLETED",
  "id": "EV-20261005221501-3f9a1c2b",
  "ts": "2026-10-05T22:15:01.123456Z",
  "from": {"session": "S-a17f2c90", "actor": "alice", "actor_type": "AI"},
  "scope": ["goal:GOAL-001", "work:WP-014"],
  "priority": "IMPORTANT",
  "data": {"work_id": "WP-014", "validation": "V-1b2c3d4e"},
  "refs": ["workspace/specs/payment-api.md"]
}
```

| Field | Rule |
|---|---|
| `protocol` | `acp/<major>[.<minor>]`. Major must equal 1, else `INCOMPATIBLE_VERSION`. Unknown extra fields are ignored (forward compatible). |
| `kind` | `command` (request an action), `event` (something happened), `query`, `response` |
| `type` | from the registries below |
| `id` | unique; re-delivering an event with a known id is a no-op (`duplicate: true`) |
| `from` | set by the coordinator from the caller; a mismatching `from.session` is rejected (`SENDER_MISMATCH`) |
| `to` | directed commands only: `session:<id>` |
| `scope` | `kind:id` strings used for filtering (`goal:`, `work:`, `session:`, `contract:`) |
| `priority` | `DEBUG < INFO < IMPORTANT < WARNING < BLOCKING` — inboxes default to ≥ IMPORTANT |
| `data` | ids, statuses, short reasons. **Never copied artifact content** — use `refs`. |

## Registries

**Events:** GOAL_CREATED, GOAL_UPDATED, GOAL_CONFIRMED, GOAL_COMPLETED · WORK_CREATED, WORK_UPDATED,
WORK_CLAIMED, WORK_STARTED, WORK_BLOCKED, WORK_UNBLOCKED, WORK_SUBMITTED, WORK_COMPLETED, WORK_FAILED,
WORK_RELEASED · CLAIM_ACQUIRED, CLAIM_RELEASED · ARTIFACT_CREATED, ARTIFACT_CHANGED · DECISION_PROPOSED,
DECISION_ACCEPTED, DECISION_REJECTED, DECISION_SUPERSEDED · CONTRACT_CREATED, CONTRACT_CHANGED ·
VALIDATION_STARTED, VALIDATION_PASSED, VALIDATION_FAILED · CONFLICT_DETECTED · HANDOFF_CREATED ·
SESSION_STARTED, SESSION_PAUSED, SESSION_COMPLETED, SESSION_FAILED · NOTIFICATION

**Executed commands** (coordinator acts, returns a response): CREATE_GOAL, UPDATE_GOAL, CONFIRM_GOAL,
COMPLETE_GOAL, ADD_WORK, CLAIM_WORK, START_WORK, BLOCK_WORK, UNBLOCK_WORK, SUBMIT_WORK, COMPLETE_WORK,
FAIL_WORK, RELEASE_WORK, ACQUIRE_CLAIM, RELEASE_CLAIM, CREATE_HANDOFF, EMIT_EVENT

**Directed commands** (delivered to the target session's inbox; it decides): ASSIGN_WORK, REQUEST_RELEASE,
REQUEST_REVIEW, REQUEST_INPUT, REQUEST_CHANGE

**Queries:** STATUS, NEXT_WORK, CONTEXT, INBOX, CLAIMS, CHECK_WRITE, GOAL, WORK, TRACE

## Responses

```json
{"protocol": "acp/1", "kind": "response", "type": "CLAIM_WORK", "reply_to": "C-1", "ok": false,
 "error": {"code": "REJECTED", "message": "WP-014 is owned by live session S-b2…"}}
```
Error codes: `INVALID_MESSAGE`, `INCOMPATIBLE_VERSION`, `SENDER_MISMATCH`, `REJECTED`, `UNSUPPORTED`.

## Transport

* `aiwos acp` — one JSON message on stdin (or as argument), one response on stdout; exit 0 ok, 2 error.
* The CLI subcommands are convenience wrappers over the same operations.
* Storage: `.ai/state/events/<writer>.jsonl`, one writer per file; ordering by `(ts, id)` with
  microsecond, process-monotonic timestamps.

## Semantics worth knowing

* Work-package status is a fold over WORK_*/VALIDATION_*/HANDOFF events. Events about a package from a
  session that is not its current owner are **stale** and ignored (reported by `aiwos doctor`).
* `WORK_SUBMITTED` resets validation and review for that package.
* Directed commands are requests, not orders: the receiver answers with a NOTIFICATION scoped to the sender.

## Versioning

Additive changes (new optional fields, new types) bump the minor version and stay readable by v1
coordinators. Removing/renaming fields or changing semantics requires `acp/2`; v1 coordinators reject such
messages explicitly instead of misinterpreting them.
