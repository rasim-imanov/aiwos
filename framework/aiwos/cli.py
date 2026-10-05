"""`aiwos` command line. Human-readable by default; `--json` for machine consumers."""

import argparse
import json
import os
import re
import sys

from . import __version__, acp
from .util import AiwosError, dumps, iso, read_text, sha256_file, slug, write_text_atomic


# ---------------------------------------------------------------- helpers

def _store(require=True):
    from .store import Store
    return Store(require=require)


def _ctx(args, store):
    from .ops import Ctx
    return Ctx(store, getattr(args, "session", None) or None,
               actor_type="HUMAN" if getattr(args, "human", False) else None)


def _emit_out(args, data, text=None):
    if getattr(args, "json", False) or text is None:
        sys.stdout.write(dumps(data))
    else:
        print(text)


def _load_json_arg(value):
    if value in (None, "-"):
        raw = sys.stdin.read()
    elif os.path.isfile(value):
        raw = read_text(value)
    else:
        raw = value
    try:
        return json.loads(raw)
    except ValueError as e:
        raise AiwosError("invalid JSON: %s" % e)


def _split(v):
    return [x.strip() for x in (v or "").split(",") if x.strip()]


# ---------------------------------------------------------------- install

def cmd_init(args):
    from .installer import Installer, install_user_skill
    if args.user_skill:
        print("installed personal skill: %s" % install_user_skill(args.dry_run))
        if not args.target:
            return 0
    target = args.target or os.getcwd()
    log = Installer(target, dry_run=args.dry_run).install(force=args.force)
    print("\n".join(log))
    print("\nAI Work OS %s installed in %s%s" % (__version__, os.path.abspath(target), " (dry run)" if args.dry_run else ""))
    print("Next: restart Claude Code in that project so hooks load, then describe your goal or run /aiwos-goal.")
    return 0


def cmd_uninstall(args):
    from .installer import Installer
    print("\n".join(Installer(args.target or os.getcwd(), dry_run=args.dry_run).uninstall()))
    return 0


def cmd_where(args):
    st = _store()
    _emit_out(args, {"root": st.root, "main_root": st.main_root, "state": st.state, "git": st.is_git,
                     "session": st.current_session_id(), "version": __version__},
              "root:  %s\nmain:  %s\nstate: %s\nsession: %s" % (st.root, st.main_root, st.state, st.current_session_id()))
    return 0


# ---------------------------------------------------------------- views

def cmd_status(args):
    from .context import status
    st = _store()
    if args.json:
        from .model import View
        v = View(st)
        _emit_out(args, {"goals": list(v.goals.values()),
                         "work": {w: dict(v.state[w], status=v.status(w), stale_owner=v.stale_owner(w)) for w in v.work},
                         "sessions_alive": sorted(v.alive), "contested": v.contested})
    else:
        print(status(st, st.current_session_id(), args.goal))
    return 0


def cmd_context(args):
    from .context import projection
    st = _store()
    _emit_out(args, projection(st, args.work_id, st.current_session_id()))
    return 0


def cmd_next(args):
    from .ops import next_work
    st = _store()
    items = next_work(_ctx(args, st), args.expertise, args.limit)
    text = "\n".join("%s %s%s (goal %s, risk %s, unblocks %d)" % (
        i["work_id"], "[RECOVER] " if i["recover"] else "", i["title"], i["goal"], i["risk"], i["unblocks"]) for i in items)
    _emit_out(args, items, text or "no work is ready for you (see `aiwos status`)")
    return 0


def cmd_doctor(args):
    from .context import doctor
    problems = doctor(_store())
    _emit_out(args, problems, "\n".join("- " + p for p in problems) or "state is consistent")
    return 1 if problems and args.strict else 0


def cmd_audit(args):
    from .context import cleanup_audit
    f = cleanup_audit(_store())
    _emit_out(args, f, "\n".join("- %-26s %s%s" % (x["kind"], x["path"], ("  (" + x["advice"] + ")") if x.get("advice") else "")
                                 for x in f) or "workspace looks clean")
    return 0


def cmd_metrics(args):
    from .context import metrics
    _emit_out(args, metrics(_store()))
    return 0


def cmd_trace(args):
    from .context import trace
    st = _store()
    _emit_out(args, trace(st, st.rel(args.path) or args.path))
    return 0


# ---------------------------------------------------------------- goals

def cmd_goal(args):
    from . import ops
    st = _store()
    ctx = _ctx(args, st)
    a = args.action
    if a == "new":
        doc = _load_json_arg(args.json_in) if args.json_in else {}
        if args.title:
            doc["title"] = args.title
        g = ops.goal_create(ctx, doc)
        _emit_out(args, g, "created %s: %s [%s]" % (g["id"], g["title"], g["status"]))
    elif a == "show":
        _emit_out(args, st.load_goal(args.id) if args.id else [st.load_goal(g) for g in st.list_goal_ids()],
                  _render_goal(st.load_goal(args.id)) if args.id else None)
    elif a == "list":
        gs = [st.load_goal(g) for g in st.list_goal_ids()]
        _emit_out(args, gs, "\n".join("%s [%s] %s" % (g["id"], g["status"], g["title"]) for g in gs) or "no goals")
    elif a == "update":
        g = ops.goal_update(ctx, args.id, _load_json_arg(args.json_in))
        _emit_out(args, g, "updated %s [%s]" % (g["id"], g["status"]))
    elif a == "propose":
        g = ops.goal_update(ctx, args.id, {"status": "PROPOSED"})
        print(_render_goal(g))
    elif a == "confirm":
        g = ops.goal_confirm(ctx, args.id, args.by, args.note)
        _emit_out(args, g, "%s CONFIRMED by %s" % (g["id"], args.by or ctx.actor))
    elif a == "complete":
        g = ops.goal_complete(ctx, args.id, _load_json_arg(args.json_in) if args.json_in else {})
        _emit_out(args, g, "%s COMPLETE" % g["id"])
    elif a == "abandon":
        g = ops.goal_update(ctx, args.id, {"status": "ABANDONED", "notes": args.note or ""})
        _emit_out(args, g, "%s ABANDONED" % g["id"])
    return 0


def _render_goal(g):
    L = ["%s — %s  [%s]" % (g["id"], g["title"], g["status"])]
    for k in ("intent", "outcome"):
        if g.get(k):
            L.append("%s: %s" % (k.capitalize(), g[k]))
    for k in ("success_criteria", "scope", "non_goals", "constraints", "assumptions", "unknowns", "risks", "research", "decisions"):
        if g.get(k):
            L.append("%s:" % k.replace("_", " ").capitalize())
            for i, x in enumerate(g[k]):
                L.append("  %s %s" % (("%d." % i) if k == "success_criteria" else "-", x if isinstance(x, str) else json.dumps(x)))
    if g.get("approvals"):
        L.append("Approvals: " + ", ".join("%s @ %s" % (a["by"], a["ts"]) for a in g["approvals"]))
    return "\n".join(L)


# ---------------------------------------------------------------- work

def cmd_work(args):
    from . import ops
    from .model import View
    st = _store()
    ctx = _ctx(args, st)
    a = args.action
    if a == "add":
        new, warns = ops.work_add(ctx, args.id, _load_json_arg(args.json_in))
        lines = ["added %s %s" % (w["id"], w["title"]) for w in new]
        lines += ["WARNING potential parallel conflict %s on %s -> %s" % (w["work"], w["resources"], w["advice"]) for w in warns]
        _emit_out(args, {"added": new, "warnings": warns}, "\n".join(lines))
    elif a == "update":
        wp, warns = ops.work_update(ctx, args.id, _load_json_arg(args.json_in))
        _emit_out(args, {"work": wp, "warnings": warns}, "updated %s" % wp["id"] + "".join(
            "\nWARNING potential conflict %s on %s" % (w["work"], w["resources"]) for w in warns))
    elif a in ("list", "graph"):
        v = View(st)
        rows = [{"id": w, "goal": wp["goal"], "title": wp["title"], "status": v.status(w),
                 "owner": v.state[w]["owner"], "depends_on": wp.get("depends_on", [])}
                for w, wp in v.work.items() if not args.id or wp["goal"] == args.id]
        text = "\n".join("%s %-17s %s%s" % (r["id"], r["status"], r["title"],
                                            ("  <- " + ",".join(r["depends_on"])) if r["depends_on"] else "") for r in rows)
        _emit_out(args, rows, text or "no work packages")
    elif a == "show":
        v = View(st)
        wp = st.find_work(args.id)
        _emit_out(args, dict(wp, state=dict(v.state[args.id], status=v.status(args.id))))
    elif a == "claim":
        r = ops.work_claim(ctx, args.id, args.force)
        msg = "claimed %s%s" % (args.id, (" (taken over from stale %s)" % r["takeover_from"]) if r.get("takeover_from") else "")
        if r.get("handoff"):
            msg += "\nread the handoff first: .ai/state/handoffs/%s.md" % r["handoff"]
        _emit_out(args, r, msg)
    elif a == "start":
        r = ops.work_start(ctx, args.id, None if args.worktree is None else args.worktree == "yes")
        msg = "started %s" % args.id
        if r.get("branch"):
            msg += " on branch %s" % r["branch"]
        if r.get("worktree"):
            msg += "\nwork in: %s  (cd there before editing)" % os.path.join(st.main_root, *r["worktree"].split("/"))
        _emit_out(args, r, msg)
    elif a == "block":
        ops.work_block(ctx, args.id, args.reason or "unspecified", args.needs_input)
        print("%s %s" % (args.id, "NEEDS_INPUT" if args.needs_input else "BLOCKED"))
    elif a == "unblock":
        ops.work_unblock(ctx, args.id)
        print("%s IN_PROGRESS" % args.id)
    elif a == "submit":
        r = ops.work_submit(ctx, args.id, args.allow_scope)
        _emit_out(args, r, "%s READY_FOR_REVIEW (%d files, %d commits). Next: `aiwos validate %s`" % (
            args.id, len(r["changed"]), len(r["commits"]), args.id))
    elif a == "complete":
        r = ops.work_complete(ctx, args.id, args.keep_worktree)
        _emit_out(args, r, "%s COMPLETE%s" % (args.id, ("; now READY: " + ", ".join(r["now_ready"])) if r["now_ready"] else ""))
    elif a == "fail":
        ops.work_fail(ctx, args.id, args.reason or "unspecified")
        print("%s FAILED" % args.id)
    elif a == "release":
        ops.work_release(ctx, args.id, args.reason)
        print("%s released" % args.id)
    elif a == "gate":
        v = View(st)
        problems = ops.completion_gate(st, v, args.id)
        _emit_out(args, problems, "\n".join("- " + p for p in problems) or "%s may be completed" % args.id)
    return 0


# ---------------------------------------------------------------- sessions, claims, events

def cmd_session(args):
    from . import ops
    st = _store()
    a = args.action
    if a == "start":
        s = ops.session_start(st, label=args.label, actor_type="HUMAN" if args.human else "AI", sid=args.id)
        if args.json:
            _emit_out(args, s)
        else:
            print('export AIWOS_SESSION="%s"' % s["id"])
            print("# session %s registered; export the line above in your shell" % s["id"], file=sys.stderr)
    elif a == "end":
        s = ops.session_end(_ctx(args, st))
        print("session %s %s" % (s["id"], s["status"]) if s else "no such session")
    elif a == "heartbeat":
        ops.heartbeat(st, _ctx(args, st).need_session(), min_interval=0)
        print("ok")
    elif a == "list":
        from .model import View
        v = View(st)
        rows = [dict(s, alive=s["id"] in v.alive) for s in v.sessions.values()]
        _emit_out(args, rows, "\n".join("%s %-8s %-6s %s %s %s" % (s["id"], s.get("status"), "live" if s["alive"] else "-",
                                                                   s.get("actor"), s.get("work") or "-", s.get("last_seen"))
                                        for s in rows) or "no sessions")
    return 0


def cmd_claim(args):
    from . import ops
    from .model import View
    st = _store()
    ctx = _ctx(args, st)
    a = args.action
    if a == "add":
        made, conflicts = ops.claim_acquire(ctx, args.resources, args.mode, args.work)
        if conflicts:
            _emit_out(args, {"conflicts": conflicts}, "CONFLICT:\n" + "\n".join(
                "  %s overlaps %s (%s by %s, work %s)" % (c["resource"], c["held"], c["mode"], c["session"], c.get("work"))
                for c in conflicts))
            return 3
        _emit_out(args, made, "claimed: " + ", ".join(c["resource"] for c in made) if made else "already held")
    elif a == "release":
        rel = ops.claim_release(ctx, work=args.work, resource=args.resource, everything=args.all)
        print("released: " + (", ".join(rel) or "nothing"))
    elif a == "list":
        v = View(st)
        rows = [c for c in v.claims if args.all or c["alive"]]
        _emit_out(args, rows, "\n".join("%s %-9s %-40s %s %s%s" % (c["session"], c["mode"], c["resource"], c.get("work") or "-",
                                                                    c.get("acquired_at"), "" if c["alive"] else " (dead)")
                                        for c in rows) or "no claims")
    elif a == "check":
        d, reason = ops.check_write(ctx, args.resources[0])
        _emit_out(args, {"decision": d, "reason": reason}, "%s: %s" % (d, reason))
        return 0 if d != "deny" else 3
    return 0


def cmd_event(args):
    from . import ops
    st = _store()
    ctx = _ctx(args, st)
    data = _load_json_arg(args.data) if args.data else {}
    ev = ops.emit(ctx, args.type, args.scope or [], data, args.priority.upper(), args.ref)
    _emit_out(args, ev, ev["id"])
    return 0


def cmd_events(args):
    from .model import View
    v = View(_store())
    floor = acp.priority_rank(args.min_priority)
    rows = [e for e in v.events if acp.priority_rank(e.get("priority")) >= floor
            and (not args.scope or set(args.scope) & set(e.get("scope", [])))
            and (not args.type or e.get("type") == args.type)]
    rows = rows[-args.limit:]
    _emit_out(args, rows, "\n".join("%s %-9s %-20s %-10s %s" % (e["ts"][:19], e.get("priority"), e["type"],
                                                                 (e.get("from") or {}).get("session") or (e.get("from") or {}).get("actor"),
                                                                 json.dumps(e.get("data"), ensure_ascii=False)[:110]) for e in rows) or "no events")
    return 0


def cmd_inbox(args):
    from . import ops
    st = _store()
    rows = ops.inbox(_ctx(args, st), args.min_priority, mark_read=args.ack, include_read=args.all)
    _emit_out(args, rows, "\n".join("%s %s %s from %s: %s" % (e["ts"][:19], e.get("priority"), e["type"],
                                                               (e.get("from") or {}).get("session") or (e.get("from") or {}).get("actor"),
                                                               json.dumps(e.get("data"), ensure_ascii=False)[:140]) for e in rows) or "inbox empty")
    return 0


def cmd_send(args):
    from . import ops
    st = _store()
    ctx = _ctx(args, st)
    data = {"note": args.note} if args.note else {}
    if args.work:
        data["work_id"] = args.work
    scope = ["work:" + args.work] if args.work else []
    msg = ops.send(ctx, args.type, args.to, scope, data, args.priority.upper())
    _emit_out(args, msg, "sent %s to %s (%s)" % (args.type, args.to, msg["id"]))
    return 0


def cmd_handoff(args):
    from . import ops
    st = _store()
    r = ops.handoff_create(_ctx(args, st), args.work_id, args.completed, args.remaining, args.problems, args.next,
                           _split(args.decisions), release=not args.keep)
    _emit_out(args, r, "handoff %s written: %s" % (r["handoff"], r["path"]))
    return 0


def cmd_recover(args):
    from . import ops
    r = ops.recover(_ctx(args, _store()))
    _emit_out(args, r, "expired sessions: %s\nwork returned to pool: %s\nclaims released: %d" % (
        ", ".join(r["expired_sessions"]) or "none", ", ".join(r["released_work"]) or "none", r["released_claims"]))
    return 0


def cmd_sync(args):
    from .sync import record_sync, sync
    from . import ops
    st = _store()
    r = sync(st, push=not args.no_push, remote=args.remote)
    record_sync(st, r)
    for c in r["conflicts"]:
        ops.emit(_ctx(args, st), "CONFLICT_DETECTED", ["goal:" + c["path"].split("/")[1]], dict(c, stage="sync"), "BLOCKING")
    from .model import View
    contested = View(st).contested
    _emit_out(args, dict(r, contested=len(contested)),
              "sync: fetched=%s merged=%d committed=%s pushed=%s conflicts=%d contested-claims=%d" % (
                  r["fetched"], len(r["merged_files"]), r["committed"], r["pushed"], len(r["conflicts"]), len(contested)))
    return 0


# ---------------------------------------------------------------- validation, review, routing

def cmd_validate(args):
    from .validation import contradictions, run_checks
    st = _store()
    rec = run_checks(_ctx(args, st), args.work_id, _split(args.only) or None)
    lines = ["%s %s %s" % (rec["id"], "PASSED" if rec["passed"] else ("INCOMPLETE" if rec["incomplete"] else "FAILED"), args.work_id)]
    for r in rec["results"]:
        lines.append("  [%s] %-9s %s%s" % ("ok" if r["passed"] else "FAIL", r["layer"], r["name"],
                                          "" if r["passed"] else "\n" + "\n".join("      " + l for l in str(r.get("output_tail") or r.get("detail")).splitlines()[-15:])))
    if rec["incomplete"]:
        lines.append("  no runnable checks: add validation commands to the work package (`work update`)")
    if rec["manual_pending"]:
        lines.append("  manual checks still pending: " + ", ".join(rec["manual_pending"]))
    for c in contradictions(st, args.work_id):
        lines.append("  CONTRADICTION: " + c)
    _emit_out(args, rec, "\n".join(lines))
    return 0 if rec["passed"] else 1


def cmd_review(args):
    from .validation import record_review
    st = _store()
    findings = _load_json_arg(args.findings) if args.findings else None
    rec = record_review(_ctx(args, st), args.work_id, args.verdict, args.reviewer, args.notes, findings)
    _emit_out(args, rec, "%s review %s by %s" % (rec["id"], "PASSED" if rec["passed"] else "FAILED", args.reviewer))
    return 0


def route(config, risk=None, complexity=None, kind=None):
    r = config["routing"]
    facts = {"risk": risk, "complexity": complexity, "kind": kind}
    tier = r["default_tier"]
    for rule in r["rules"]:
        if all(facts.get(k) == v for k, v in rule["if"].items()):
            tier = rule["tier"]
            break
    return dict(r["tiers"].get(tier, {}), tier=tier)


def cmd_route(args):
    st = _store()
    if args.work_id:
        wp = st.find_work(args.work_id)
        res = route(st.config, wp.get("risk"), wp.get("complexity"), args.kind or wp.get("kind"))
    else:
        res = route(st.config, args.risk, args.complexity, args.kind)
    _emit_out(args, res, "tier=%s model=%s effort=%s" % (res["tier"], res.get("model", "-"), res.get("effort", "-")))
    return 0


# ---------------------------------------------------------------- knowledge: decisions, contracts, artifacts

def _decisions_dir(st):
    return os.path.join(st.root, "knowledge", "decisions")


def _find_decision(st, did):
    d = _decisions_dir(st)
    for name in os.listdir(d) if os.path.isdir(d) else []:
        if name.startswith(did + "-"):
            return os.path.join(d, name)
    raise AiwosError("unknown decision %s" % did)


def cmd_decision(args):
    from . import ops
    st = _store()
    ctx = _ctx(args, st)
    d = _decisions_dir(st)
    if args.action == "new":
        os.makedirs(d, exist_ok=True)
        nums = [int(m.group(1)) for m in (re.match(r"^D-(\d+)-", n) for n in os.listdir(d)) if m]
        did = "D-%03d" % (max(nums, default=0) + 1)
        tpl = read_text(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates", "decision.md"))
        text = tpl.replace("{{ID}}", did).replace("{{TITLE}}", args.title).replace("{{DATE}}", iso()[:10]) \
            .replace("{{GOAL}}", args.goal or "—").replace("{{WORK}}", args.work or "—").replace("{{BY}}", ctx.actor)
        path = os.path.join(d, "%s-%s.md" % (did, slug(args.title)))
        write_text_atomic(path, text)
        scope = (["goal:" + args.goal] if args.goal else []) + (["work:" + args.work] if args.work else [])
        ops.emit(ctx, "DECISION_PROPOSED", scope, {"decision": did, "title": args.title,
                                                   "path": st.rel(path)}, "IMPORTANT", refs=[st.rel(path)])
        _emit_out(args, {"id": did, "path": path}, "%s created: %s (fill it in, then `aiwos decision accept %s`)" % (did, st.rel(path), did))
        return 0
    path = _find_decision(st, args.id)
    text = read_text(path)
    new_status = {"accept": "ACCEPTED", "reject": "REJECTED", "supersede": "SUPERSEDED"}[args.action]
    label = new_status + (" by %s" % args.by if args.action == "supersede" and args.by else "")
    text, n = re.subn(r"(?im)^(\**status\**:\s*\**\s*)[A-Z_]+[^\n]*", lambda m: m.group(1) + label, text, count=1)
    if not n:
        raise AiwosError("%s has no 'Status:' line" % path)
    write_text_atomic(path, text)
    etype = {"accept": "DECISION_ACCEPTED", "reject": "DECISION_REJECTED", "supersede": "DECISION_SUPERSEDED"}[args.action]
    goals = sorted(set(re.findall(r"GOAL-\d+", text[:1500])))
    ops.emit(ctx, etype, ["goal:" + g for g in goals], {"decision": args.id, "superseded_by": args.by,
                                                        "path": st.rel(path)}, "IMPORTANT", refs=[st.rel(path)])
    print("%s %s" % (args.id, label))
    return 0


def cmd_contract(args):
    from . import ops
    from .context import contracts
    st = _store()
    ctx = _ctx(args, st)
    if args.action == "add":
        rel = st.rel(args.path)
        if not rel or not os.path.exists(os.path.join(st.root, *rel.split("/"))):
            raise AiwosError("contract file %s does not exist (write it first)" % args.path)
        data = {"name": args.name, "path": rel, "hash": sha256_file(os.path.join(st.root, *rel.split("/"))),
                "goal": args.goal, "provider": args.provider, "consumers": _split(args.consumers)}
        scope = ["contract:" + args.name] + (["goal:" + args.goal] if args.goal else []) + ["work:" + w for w in data["consumers"]]
        ops.emit(ctx, "CONTRACT_CREATED", scope, data, "IMPORTANT", refs=[rel])
        print("contract %s registered (%s)" % (args.name, rel))
    elif args.action == "list":
        cs = contracts(st)
        _emit_out(args, cs, "\n".join("%s v%d %s provider=%s consumers=%s" % (n, c["version"], c.get("path"), c.get("provider"),
                                                                            ",".join(c.get("consumers", []))) for n, c in cs.items()) or "no contracts")
    elif args.action == "check":
        changed = []
        for n, c in contracts(st).items():
            p = os.path.join(st.root, *c["path"].split("/"))
            h = sha256_file(p) if os.path.exists(p) else None
            if h != c.get("hash"):
                changed.append(n)
                scope = ["contract:" + n] + ["work:" + w for w in c.get("consumers", [])] + (["goal:" + c["goal"]] if c.get("goal") else [])
                ops.emit(ctx, "CONTRACT_CHANGED", scope, {"name": n, "path": c["path"], "hash": h, "previous": c.get("hash")},
                         "BLOCKING", refs=[c["path"]])
        print("changed: " + (", ".join(changed) or "none"))
        return 1 if changed else 0
    return 0


def cmd_artifact(args):
    from . import ops
    st = _store()
    rel = st.rel(args.path) or args.path
    kind = args.kind.upper()
    if kind not in ("AUTHORITATIVE", "DERIVED", "TEMPORARY", "ARCHIVED"):
        raise AiwosError("kind must be AUTHORITATIVE, DERIVED, TEMPORARY or ARCHIVED")
    scope = (["goal:" + args.goal] if args.goal else []) + (["work:" + args.work] if args.work else [])
    ops.emit(_ctx(args, st), "ARTIFACT_CREATED" if args.action == "add" else "ARTIFACT_CHANGED", scope,
             {"path": rel, "kind": kind, "work_id": args.work, "goal_id": args.goal, "note": args.note}, "INFO", refs=[rel])
    print("%s %s recorded" % (rel, kind))
    return 0


# ---------------------------------------------------------------- adoption

def cmd_adopt(args):
    from . import adopt
    root = os.path.abspath(args.target or os.getcwd())
    if args.action == "scan":
        model = adopt.scan(root)
        out = adopt.write_model(root, model)
        print("project model written to %s" % out)
        print("agents=%d skills=%d commands=%d hooks=%d instruction-files=%d problems=%d duplicates=%d" % (
            len(model["agents"]), len(model["skills"]), len(model["commands"]), len(model["hooks"]),
            len(model["instruction_files"]), len(model["problems"]), len(model["duplicates"])))
    elif args.action == "verify":
        problems = adopt.verify(root)
        print("\n".join("- " + p for p in problems) or "every capability is accounted for")
        return 1 if problems else 0
    return 0


# ---------------------------------------------------------------- ACP endpoint

def cmd_acp(args):
    from .acp_server import handle
    st = _store()
    msg = _load_json_arg(args.message or "-")
    resp = handle(st, msg, _ctx(args, st))
    sys.stdout.write(dumps(resp))
    return 0 if resp.get("ok") else 2


def cmd_hook(args):
    from .hooks import run
    return run(args.name)


# ---------------------------------------------------------------- parser

def build_parser():
    p = argparse.ArgumentParser(prog="aiwos", description="AI Work OS — goal-oriented coordination for AI sessions")
    p.add_argument("--version", action="version", version="aiwos " + __version__)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="machine-readable output")
    common.add_argument("--session", help="act as this aiwos session (default: $AIWOS_SESSION)")
    common.add_argument("--human", action="store_true", help="record the actor as HUMAN")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", parents=[common], help="install/upgrade the framework into a project")
    s.add_argument("target", nargs="?")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--force", action="store_true", help="overwrite locally modified framework files")
    s.add_argument("--user-skill", action="store_true", help="also install personal /aiwos-init skill")
    s.set_defaults(fn=cmd_init)
    s = sub.add_parser("uninstall", parents=[common], help="remove framework files (keeps your state/knowledge)")
    s.add_argument("target", nargs="?")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(fn=cmd_uninstall)
    sub.add_parser("where", parents=[common], help="show resolved paths and session").set_defaults(fn=cmd_where)

    s = sub.add_parser("status", parents=[common], help="compact project status")
    s.add_argument("--goal")
    s.set_defaults(fn=cmd_status)
    s = sub.add_parser("context", parents=[common], help="minimal context projection for a work package")
    s.add_argument("work_id")
    s.set_defaults(fn=cmd_context)
    s = sub.add_parser("next", parents=[common], help="work this session can safely take")
    s.add_argument("--expertise")
    s.add_argument("--limit", type=int, default=5)
    s.set_defaults(fn=cmd_next)
    s = sub.add_parser("doctor", parents=[common], help="consistency checks")
    s.add_argument("--strict", action="store_true")
    s.set_defaults(fn=cmd_doctor)
    sub.add_parser("audit", parents=[common], help="read-only workspace cleanup audit").set_defaults(fn=cmd_audit)
    sub.add_parser("metrics", parents=[common], help="efficiency/diagnostic metrics").set_defaults(fn=cmd_metrics)
    s = sub.add_parser("trace", parents=[common], help="why does this artifact exist?")
    s.add_argument("path")
    s.set_defaults(fn=cmd_trace)

    s = sub.add_parser("goal", parents=[common], help="goals: new|show|list|update|propose|confirm|complete|abandon")
    s.add_argument("action", choices=["new", "show", "list", "update", "propose", "confirm", "complete", "abandon"])
    s.add_argument("id", nargs="?")
    s.add_argument("--title")
    s.add_argument("--in", dest="json_in", help="JSON file, inline JSON, or - for stdin")
    s.add_argument("--by", help="who approved (confirm)")
    s.add_argument("--note")
    s.set_defaults(fn=cmd_goal)

    s = sub.add_parser("work", parents=[common], help="work packages")
    s.add_argument("action", choices=["add", "update", "list", "graph", "show", "claim", "start", "block", "unblock",
                                      "submit", "complete", "fail", "release", "gate"])
    s.add_argument("id", nargs="?", help="GOAL id for add/list, WP id otherwise")
    s.add_argument("--in", dest="json_in")
    s.add_argument("--reason")
    s.add_argument("--needs-input", action="store_true")
    s.add_argument("--force", action="store_true")
    s.add_argument("--worktree", choices=["yes", "no"])
    s.add_argument("--allow-scope", action="store_true")
    s.add_argument("--keep-worktree", action="store_true")
    s.set_defaults(fn=cmd_work)

    s = sub.add_parser("session", parents=[common], help="sessions: start|end|heartbeat|list")
    s.add_argument("action", choices=["start", "end", "heartbeat", "list"])
    s.add_argument("--label")
    s.add_argument("--id")
    s.set_defaults(fn=cmd_session)

    s = sub.add_parser("claim", parents=[common], help="resource claims: add|release|list|check")
    s.add_argument("action", choices=["add", "release", "list", "check"])
    s.add_argument("resources", nargs="*")
    s.add_argument("--mode", default="WRITE")
    s.add_argument("--work")
    s.add_argument("--resource")
    s.add_argument("--all", action="store_true")
    s.set_defaults(fn=cmd_claim)

    s = sub.add_parser("event", parents=[common], help="emit an ACP event")
    s.add_argument("type")
    s.add_argument("--scope", action="append")
    s.add_argument("--data")
    s.add_argument("--priority", default="INFO")
    s.add_argument("--ref", action="append")
    s.set_defaults(fn=cmd_event)
    s = sub.add_parser("events", parents=[common], help="list events (filtered)")
    s.add_argument("--scope", action="append")
    s.add_argument("--type")
    s.add_argument("--min-priority", default="DEBUG")
    s.add_argument("--limit", type=int, default=40)
    s.set_defaults(fn=cmd_events)
    s = sub.add_parser("inbox", parents=[common], help="relevant unread events and directed commands")
    s.add_argument("--min-priority")
    s.add_argument("--ack", action="store_true", help="mark everything up to now as read")
    s.add_argument("--all", action="store_true")
    s.set_defaults(fn=cmd_inbox)
    s = sub.add_parser("send", parents=[common], help="send a directed ACP command to another session")
    s.add_argument("to")
    s.add_argument("type", choices=sorted(acp.DIRECTED_COMMANDS))
    s.add_argument("--work")
    s.add_argument("--note")
    s.add_argument("--priority", default="IMPORTANT")
    s.set_defaults(fn=cmd_send)
    s = sub.add_parser("handoff", parents=[common], help="hand a work package to another session")
    s.add_argument("work_id")
    s.add_argument("--completed", default="")
    s.add_argument("--remaining", default="")
    s.add_argument("--problems", default="")
    s.add_argument("--next", default="")
    s.add_argument("--decisions", default="")
    s.add_argument("--keep", action="store_true", help="keep ownership (checkpoint only)")
    s.set_defaults(fn=cmd_handoff)
    sub.add_parser("recover", parents=[common], help="expire dead sessions and free their work").set_defaults(fn=cmd_recover)
    s = sub.add_parser("sync", parents=[common], help="share coordination state via git")
    s.add_argument("--remote")
    s.add_argument("--no-push", action="store_true")
    s.set_defaults(fn=cmd_sync)

    s = sub.add_parser("validate", parents=[common], help="run deterministic validation for a work package")
    s.add_argument("work_id")
    s.add_argument("--only")
    s.set_defaults(fn=cmd_validate)
    s = sub.add_parser("review", parents=[common], help="record an independent review verdict")
    s.add_argument("work_id")
    s.add_argument("--verdict", required=True, choices=["pass", "fail"])
    s.add_argument("--reviewer", required=True)
    s.add_argument("--notes", help="path to review notes")
    s.add_argument("--findings", help="JSON list of findings")
    s.set_defaults(fn=cmd_review)
    s = sub.add_parser("route", parents=[common], help="recommend model tier for a work package/task")
    s.add_argument("work_id", nargs="?")
    s.add_argument("--risk")
    s.add_argument("--complexity")
    s.add_argument("--kind")
    s.set_defaults(fn=cmd_route)

    s = sub.add_parser("decision", parents=[common], help="decision records: new|accept|reject|supersede")
    s.add_argument("action", choices=["new", "accept", "reject", "supersede"])
    s.add_argument("id_or_title", metavar="ID|TITLE")
    s.add_argument("--goal")
    s.add_argument("--work")
    s.add_argument("--by", help="superseding decision id")
    s.set_defaults(fn=cmd_decision)
    s = sub.add_parser("contract", parents=[common], help="contracts: add|list|check")
    s.add_argument("action", choices=["add", "list", "check"])
    s.add_argument("name", nargs="?")
    s.add_argument("path", nargs="?")
    s.add_argument("--goal")
    s.add_argument("--provider")
    s.add_argument("--consumers")
    s.set_defaults(fn=cmd_contract)
    s = sub.add_parser("artifact", parents=[common], help="record artifact provenance: add|change")
    s.add_argument("action", choices=["add", "change"])
    s.add_argument("path")
    s.add_argument("--kind", default="AUTHORITATIVE")
    s.add_argument("--goal")
    s.add_argument("--work")
    s.add_argument("--note")
    s.set_defaults(fn=cmd_artifact)

    s = sub.add_parser("adopt", parents=[common], help="existing project adoption: scan|verify")
    s.add_argument("action", choices=["scan", "verify"])
    s.add_argument("target", nargs="?")
    s.set_defaults(fn=cmd_adopt)
    s = sub.add_parser("acp", parents=[common], help="ACP endpoint: one JSON message in, one response out")
    s.add_argument("message", nargs="?", help="JSON (default: stdin)")
    s.set_defaults(fn=cmd_acp)
    s = sub.add_parser("hook", help="Claude Code hook entry point")
    s.add_argument("name")
    s.set_defaults(fn=cmd_hook)
    return p


def main(argv=None):
    # Hook payloads and piped JSON are UTF-8; Windows would otherwise decode them with the ANSI code page.
    for stream in (sys.stdin, sys.stdout):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8")
            except (ValueError, OSError):
                pass
    args = build_parser().parse_args(argv)
    if args.cmd == "decision":
        if args.action == "new":
            args.title = args.id_or_title
        else:
            args.id = args.id_or_title
    try:
        return args.fn(args) or 0
    except AiwosError as e:
        print("aiwos: %s" % e, file=sys.stderr)
        return 2
