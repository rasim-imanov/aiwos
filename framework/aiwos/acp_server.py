"""ACP endpoint: validate one message, dispatch it, return one response (never raises)."""

from . import acp, ops
from .model import View
from .util import AiwosError


def _d(msg, key, default=None):
    return (msg.get("data") or {}).get(key, default)


def _exec(store, msg, ctx):
    t = msg["type"]
    wid = _d(msg, "work_id")
    if t == "CREATE_GOAL":
        return ops.goal_create(ctx, _d(msg, "goal", {}))
    if t == "UPDATE_GOAL":
        return ops.goal_update(ctx, _d(msg, "goal_id"), _d(msg, "patch", {}))
    if t == "CONFIRM_GOAL":
        return ops.goal_confirm(ctx, _d(msg, "goal_id"), _d(msg, "by"), _d(msg, "note"))
    if t == "COMPLETE_GOAL":
        return ops.goal_complete(ctx, _d(msg, "goal_id"), _d(msg, "evidence", {}))
    if t == "ADD_WORK":
        new, warnings = ops.work_add(ctx, _d(msg, "goal_id"), _d(msg, "work", []))
        return {"added": [w["id"] for w in new], "warnings": warnings}
    if t == "CLAIM_WORK":
        return ops.work_claim(ctx, wid, bool(_d(msg, "force")))
    if t == "START_WORK":
        return ops.work_start(ctx, wid, _d(msg, "worktree"))
    if t == "BLOCK_WORK":
        ops.work_block(ctx, wid, _d(msg, "reason", ""), bool(_d(msg, "needs_input")))
        return {"work_id": wid}
    if t == "UNBLOCK_WORK":
        ops.work_unblock(ctx, wid)
        return {"work_id": wid}
    if t == "SUBMIT_WORK":
        return ops.work_submit(ctx, wid, bool(_d(msg, "allow_scope")))
    if t == "COMPLETE_WORK":
        return ops.work_complete(ctx, wid)
    if t == "FAIL_WORK":
        ops.work_fail(ctx, wid, _d(msg, "reason", ""))
        return {"work_id": wid}
    if t == "RELEASE_WORK":
        ops.work_release(ctx, wid, _d(msg, "reason"))
        return {"work_id": wid}
    if t == "ACQUIRE_CLAIM":
        made, conflicts = ops.claim_acquire(ctx, _d(msg, "resources", []), _d(msg, "mode", "WRITE"), wid)
        if conflicts:
            raise AiwosError("CONFLICT: %s" % conflicts)
        return {"claims": made}
    if t == "RELEASE_CLAIM":
        return {"released": ops.claim_release(ctx, work=wid, resource=_d(msg, "resource"), everything=bool(_d(msg, "all")))}
    if t == "CREATE_HANDOFF":
        return ops.handoff_create(ctx, wid, _d(msg, "completed", ""), _d(msg, "remaining", ""), _d(msg, "problems", ""),
                                  _d(msg, "next_action", ""), _d(msg, "decisions", []))
    if t == "EMIT_EVENT":
        ev = _d(msg, "event") or {}
        return ops.emit(ctx, ev.get("type"), ev.get("scope", []), ev.get("data", {}), ev.get("priority", "INFO"), ev.get("refs"))
    raise AiwosError("unhandled command %s" % t)


def _query(store, msg, ctx):
    from .context import projection, status, trace
    t = msg["type"]
    if t == "STATUS":
        return {"text": status(store, ctx.session, _d(msg, "goal_id"))}
    if t == "NEXT_WORK":
        return {"work": ops.next_work(ctx, _d(msg, "expertise"), _d(msg, "limit", 5))}
    if t == "CONTEXT":
        return projection(store, _d(msg, "work_id"), ctx.session)
    if t == "INBOX":
        return {"messages": ops.inbox(ctx, _d(msg, "min_priority"), bool(_d(msg, "ack")))}
    if t == "CLAIMS":
        return {"claims": [c for c in View(store).claims if c["alive"]]}
    if t == "CHECK_WRITE":
        d, reason = ops.check_write(ctx, _d(msg, "path", ""))
        return {"decision": d, "reason": reason}
    if t == "GOAL":
        return store.load_goal(_d(msg, "goal_id"))
    if t == "WORK":
        v = View(store)
        w = _d(msg, "work_id")
        return dict(store.find_work(w), state=dict(v.state[w], status=v.status(w)))
    if t == "TRACE":
        return trace(store, _d(msg, "path", ""))
    raise AiwosError("unhandled query %s" % t)


def handle(store, msg, ctx):
    errs = acp.validate(msg)
    if errs:
        code = "INCOMPATIBLE_VERSION" if any(e.startswith("INCOMPATIBLE_VERSION") for e in errs) else "INVALID_MESSAGE"
        return acp.response(msg if isinstance(msg, dict) else {}, False, code=code, message="; ".join(errs))
    sender = msg.get("from") or {}
    if sender.get("session") and ctx.session and sender["session"] != ctx.session:
        return acp.response(msg, False, code="SENDER_MISMATCH",
                            message="message claims session %s but caller is %s" % (sender["session"], ctx.session))
    if sender.get("session") and not ctx.session:
        ctx.session = sender["session"]
    try:
        kind = msg["kind"]
        if kind == "event":
            known = {e["id"] for e in store.events()} if msg.get("id") else set()
            if msg.get("id") in known:
                return acp.response(msg, True, {"duplicate": True, "id": msg["id"]})
            ev = acp.make_event(msg["type"], ctx.sender, msg.get("scope", []), msg.get("data"),
                                msg.get("priority", "INFO").upper(), msg.get("refs"))
            if msg.get("id"):
                ev["id"] = msg["id"]  # keep sender id so redelivery stays idempotent
            store.append_event(ev, ctx.writer)
            return acp.response(msg, True, {"id": ev["id"]})
        if kind == "command" and msg["type"] in acp.DIRECTED_COMMANDS:
            sent = ops.send(ctx, msg["type"], msg["to"].split(":", 1)[1], msg.get("scope", []), msg.get("data"),
                            msg.get("priority", "IMPORTANT").upper(), msg.get("refs"))
            return acp.response(msg, True, {"id": sent["id"], "delivered_to": msg["to"]})
        if kind == "command":
            return acp.response(msg, True, _exec(store, msg, ctx))
        if kind == "query":
            return acp.response(msg, True, _query(store, msg, ctx))
        return acp.response(msg, False, code="UNSUPPORTED", message="responses are not accepted as input")
    except AiwosError as e:
        return acp.response(msg, False, code="REJECTED", message=str(e))
