"""ACP v1 — Agent Communication Protocol (internal to AI Work OS; see docs/acp.md).

Envelope (all kinds):
  protocol  "acp/1"            major version must match; minor additions are ignored
  kind      command | event | query | response
  type      UPPER_SNAKE name from the registries below
  id        unique message id (events: EV-<utc>-<rand>)
  ts        UTC ISO-8601
  from      {"session": "S-…"?, "actor": "…", "actor_type": "AI|HUMAN|AUTOMATION|SYSTEM"}
  to        optional audience for directed commands: "session:S-…"
  scope     ["goal:GOAL-001", "work:WP-003", "session:S-…"]  (used for filtering)
  priority  DEBUG | INFO | IMPORTANT | WARNING | BLOCKING
  data      type-specific payload — references, never copied artifact content
  refs      optional artifact paths / ids the receiver may fetch lazily
"""

from . import PROTOCOL, PROTOCOL_MAJOR
from .util import AiwosError, event_id, event_ts, iso, rand_id

PRIORITIES = ["DEBUG", "INFO", "IMPORTANT", "WARNING", "BLOCKING"]
KINDS = ["command", "event", "query", "response"]

EVENT_TYPES = {
    "GOAL_CREATED", "GOAL_UPDATED", "GOAL_CONFIRMED", "GOAL_COMPLETED",
    "WORK_CREATED", "WORK_UPDATED", "WORK_CLAIMED", "WORK_STARTED", "WORK_BLOCKED",
    "WORK_UNBLOCKED", "WORK_SUBMITTED", "WORK_COMPLETED", "WORK_FAILED", "WORK_RELEASED",
    "CLAIM_ACQUIRED", "CLAIM_RELEASED",
    "ARTIFACT_CREATED", "ARTIFACT_CHANGED",
    "DECISION_PROPOSED", "DECISION_ACCEPTED", "DECISION_REJECTED", "DECISION_SUPERSEDED",
    "CONTRACT_CREATED", "CONTRACT_CHANGED",
    "VALIDATION_STARTED", "VALIDATION_PASSED", "VALIDATION_FAILED",
    "CONFLICT_DETECTED",
    "HANDOFF_CREATED",
    "SESSION_STARTED", "SESSION_PAUSED", "SESSION_COMPLETED", "SESSION_FAILED",
    "NOTIFICATION",
}

# Commands executed by the coordinator (mutate state; answered with a response).
EXEC_COMMANDS = {
    "CREATE_GOAL", "UPDATE_GOAL", "CONFIRM_GOAL", "COMPLETE_GOAL",
    "ADD_WORK", "CLAIM_WORK", "START_WORK", "BLOCK_WORK", "UNBLOCK_WORK", "SUBMIT_WORK",
    "COMPLETE_WORK", "FAIL_WORK", "RELEASE_WORK",
    "ACQUIRE_CLAIM", "RELEASE_CLAIM", "CREATE_HANDOFF", "EMIT_EVENT",
}
# Commands delivered to another session's inbox (that session decides and acts).
DIRECTED_COMMANDS = {"ASSIGN_WORK", "REQUEST_RELEASE", "REQUEST_REVIEW", "REQUEST_INPUT", "REQUEST_CHANGE"}

QUERY_TYPES = {"STATUS", "NEXT_WORK", "CONTEXT", "INBOX", "CLAIMS", "CHECK_WRITE", "GOAL", "WORK", "TRACE"}

WORK_EVENT_TYPES = {t for t in EVENT_TYPES if t.startswith("WORK_")}


def priority_rank(p):
    try:
        return PRIORITIES.index((p or "INFO").upper())
    except ValueError:
        return 1


def make_event(etype, sender, scope=(), data=None, priority="INFO", refs=None):
    if etype not in EVENT_TYPES:
        raise AiwosError("unknown event type %s" % etype)
    ev = {
        "protocol": PROTOCOL, "kind": "event", "type": etype, "id": event_id(), "ts": event_ts(),
        "from": sender, "scope": sorted(set(scope)), "priority": priority, "data": data or {},
    }
    if refs:
        ev["refs"] = list(refs)
    return ev


def make_command(ctype, sender, to=None, scope=(), data=None, priority="IMPORTANT", refs=None):
    msg = {
        "protocol": PROTOCOL, "kind": "command", "type": ctype, "id": rand_id("CMD", 12), "ts": event_ts(),
        "from": sender, "scope": sorted(set(scope)), "priority": priority, "data": data or {},
    }
    if to:
        msg["to"] = to
    if refs:
        msg["refs"] = list(refs)
    return msg


def response(req, ok=True, data=None, code=None, message=None):
    r = {"protocol": PROTOCOL, "kind": "response", "type": (req or {}).get("type", "UNKNOWN"),
         "reply_to": (req or {}).get("id"), "ts": iso(), "ok": ok}
    if ok:
        r["data"] = data if data is not None else {}
    else:
        r["error"] = {"code": code or "ERROR", "message": message or ""}
    return r


def validate(msg):
    """Return a list of problems (empty = valid). Unknown extra fields are tolerated (forward compat)."""
    errs = []
    if not isinstance(msg, dict):
        return ["message must be a JSON object"]
    proto = msg.get("protocol")
    if not isinstance(proto, str) or not proto.startswith("acp/"):
        errs.append("protocol must be 'acp/<major>[.<minor>]'")
    else:
        try:
            major = int(proto.split("/", 1)[1].split(".")[0])
            if major != PROTOCOL_MAJOR:
                errs.append("INCOMPATIBLE_VERSION: got %s, this coordinator speaks %s" % (proto, PROTOCOL))
        except ValueError:
            errs.append("unparseable protocol version %r" % proto)
    kind = msg.get("kind")
    if kind not in KINDS:
        errs.append("kind must be one of %s" % KINDS)
    t = msg.get("type")
    if not isinstance(t, str) or not t:
        errs.append("type is required")
    elif kind == "event" and t not in EVENT_TYPES:
        errs.append("unknown event type %s" % t)
    elif kind == "command" and t not in EXEC_COMMANDS | DIRECTED_COMMANDS:
        errs.append("unknown command type %s" % t)
    elif kind == "query" and t not in QUERY_TYPES:
        errs.append("unknown query type %s" % t)
    if kind == "command" and t in DIRECTED_COMMANDS and not str(msg.get("to", "")).startswith("session:"):
        errs.append("directed command %s requires to='session:<id>'" % t)
    if "priority" in msg and str(msg["priority"]).upper() not in PRIORITIES:
        errs.append("priority must be one of %s" % PRIORITIES)
    if "scope" in msg and not (isinstance(msg["scope"], list) and all(isinstance(s, str) and ":" in s for s in msg["scope"])):
        errs.append("scope must be a list of 'kind:id' strings")
    if "data" in msg and not isinstance(msg["data"], dict):
        errs.append("data must be an object")
    return errs
