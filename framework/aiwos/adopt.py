"""Existing-project adoption: read-only discovery -> project model -> capability map -> verification.

Analyze first, modify second. `scan` never changes project files; it writes its model to
workspace/migration/ so later sessions do not rediscover the repository.
"""

import json
import os
import re
from collections import defaultdict

from .util import AiwosError, dumps, iso, read_json, write_text_atomic

CAP_STATUSES = ["PRESERVED", "IMPROVED", "REPLACED", "DEPRECATED", "REMOVED", "PARTIAL", "UNKNOWN"]
INSTRUCTION_FILES = ("CLAUDE.md", "CLAUDE.local.md", "AGENTS.md", ".cursorrules", "GEMINI.md",
                     ".github/copilot-instructions.md")
SKIP = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", ".ai", "target", ".next"}


def _frontmatter(text):
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", text, re.S)
    if not m:
        return {}, text
    meta = {}
    key = None
    for line in m.group(1).splitlines():
        kv = re.match(r"^([A-Za-z0-9_-]+):\s*(.*)$", line)
        if kv:
            key = kv.group(1)
            meta[key] = kv.group(2).strip().strip('"').strip("'")
        elif key and line.strip().startswith("-"):
            meta[key] = (meta[key] + " " if meta[key] else "") + line.strip()[1:].strip()
        elif key and line.startswith(" "):
            meta[key] = (meta[key] + " " + line.strip()).strip()
    return meta, m.group(2)


def _read(path, limit=200000):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read(limit)
    except OSError:
        return ""


def _words(text):
    return set(w for w in re.findall(r"[a-z]{4,}", (text or "").lower()))


def _jaccard(a, b):
    return len(a & b) / float(len(a | b)) if a and b else 0.0


def scan(root):
    """Inventory every AI-facing mechanism in the project. Pure read."""
    root = os.path.abspath(root)
    model = {"root": root, "scanned_at": iso(), "instruction_files": [], "agents": [], "skills": [],
             "commands": [], "hooks": [], "settings": [], "mcp_servers": [], "rules": [], "scripts": [],
             "problems": [], "duplicates": [], "references": [], "unknowns": []}

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP and not (d == "worktrees" and dirpath.endswith(".claude"))]
        rel_dir = os.path.relpath(dirpath, root).replace("\\", "/")
        rel_dir = "" if rel_dir == "." else rel_dir + "/"
        for f in filenames:
            rel = rel_dir + f
            full = os.path.join(dirpath, f)
            if f in INSTRUCTION_FILES or rel in INSTRUCTION_FILES:
                text = _read(full)
                model["instruction_files"].append({"path": rel, "lines": text.count("\n") + 1,
                                                   "imports": re.findall(r"(?m)^@(\S+)", text)})
            if "/.claude/" in "/" + rel or rel.startswith(".claude/"):
                _classify_claude_file(model, rel, full)
    _parse_settings(model, root)
    mcp = read_json(os.path.join(root, ".mcp.json"), {}) or {}
    for name, cfg in (mcp.get("mcpServers") or {}).items():
        model["mcp_servers"].append({"name": name, "type": cfg.get("type", "stdio"),
                                     "command": cfg.get("command") or cfg.get("url")})
    _find_duplicates(model)
    _find_references(model, root)
    _find_instruction_conflicts(model, root)
    return model


def _classify_claude_file(model, rel, full):
    parts = rel.split("/")
    i = parts.index(".claude")
    sub = parts[i + 1:] if i + 1 < len(parts) else []
    if not sub:
        return
    if sub[0] == "agents" and rel.endswith(".md"):
        meta, body = _frontmatter(_read(full))
        model["agents"].append({"path": rel, "name": meta.get("name") or os.path.splitext(parts[-1])[0],
                                "description": meta.get("description", "")[:300], "tools": meta.get("tools"),
                                "model": meta.get("model"), "body_lines": body.count("\n") + 1})
    elif sub[0] == "skills" and parts[-1] == "SKILL.md":
        meta, body = _frontmatter(_read(full))
        model["skills"].append({"path": rel, "name": meta.get("name") or parts[-2],
                                "description": meta.get("description", "")[:300],
                                "user_invocable": meta.get("user-invocable", "true"),
                                "manual_only": meta.get("disable-model-invocation", "false"),
                                "context": meta.get("context"), "body_lines": body.count("\n") + 1})
    elif sub[0] == "commands" and rel.endswith(".md"):
        meta, body = _frontmatter(_read(full))
        model["commands"].append({"path": rel, "name": os.path.splitext(parts[-1])[0],
                                  "description": meta.get("description", "")[:300], "body_lines": body.count("\n") + 1})
    elif sub[0] == "rules" and rel.endswith(".md"):
        meta, _ = _frontmatter(_read(full))
        model["rules"].append({"path": rel, "paths": meta.get("paths")})
    elif sub[0] in ("hooks", "scripts") or rel.endswith((".sh", ".py", ".js", ".ps1", ".mjs", ".ts")):
        model["scripts"].append({"path": rel})


def _parse_settings(model, root):
    for rel in (".claude/settings.json", ".claude/settings.local.json"):
        doc = read_json(os.path.join(root, *rel.split("/")))
        if doc is None:
            continue
        model["settings"].append({"path": rel, "keys": sorted(doc.keys()),
                                  "permissions": doc.get("permissions"), "env": sorted((doc.get("env") or {}).keys())})
        for event, groups in (doc.get("hooks") or {}).items():
            for g in groups or []:
                for h in g.get("hooks", []):
                    model["hooks"].append({"settings": rel, "event": event, "matcher": g.get("matcher"),
                                           "type": h.get("type"), "command": h.get("command"),
                                           "aiwos": "aiwos" in str(h.get("command", ""))})


def _find_duplicates(model):
    items = [("agent", a) for a in model["agents"]] + [("skill", s) for s in model["skills"]] + \
            [("command", c) for c in model["commands"]]
    by_name = defaultdict(list)
    for kind, it in items:
        by_name[it["name"].lower()].append("%s:%s" % (kind, it["path"]))
    for name, where in by_name.items():
        if len(where) > 1:
            model["duplicates"].append({"kind": "same-name", "name": name, "where": where})
    for i, (ka, a) in enumerate(items):
        for kb, b in items[i + 1:]:
            if a["name"].lower() == b["name"].lower():
                continue
            sim = _jaccard(_words(a.get("description")), _words(b.get("description")))
            if sim >= 0.6:
                model["duplicates"].append({"kind": "similar-purpose", "similarity": round(sim, 2),
                                            "where": ["%s:%s" % (ka, a["path"]), "%s:%s" % (kb, b["path"])]})


def _find_references(model, root):
    """Invocation relationships and broken references (hook scripts, @imports, agent/skill mentions)."""
    names = {a["name"]: "agent" for a in model["agents"]}
    names.update({s["name"]: "skill" for s in model["skills"]})
    for h in model["hooks"]:
        cmd = h.get("command") or ""
        for token in re.findall(r"[\w./${}\-]+\.(?:sh|py|js|mjs|ps1|ts)", cmd):
            p = token.replace("${CLAUDE_PROJECT_DIR}", "").replace("$CLAUDE_PROJECT_DIR", "").lstrip("/")
            if not os.path.exists(os.path.join(root, *p.split("/"))) and not token.startswith("~"):
                model["problems"].append({"kind": "broken-hook-script", "event": h["event"], "path": token})
            model["references"].append({"from": "hook:%s" % h["event"], "to": p})
    for f in model["instruction_files"]:
        for imp in f["imports"]:
            base = os.path.dirname(os.path.join(root, *f["path"].split("/")))
            if not imp.startswith("~") and not os.path.exists(os.path.normpath(os.path.join(base, imp))):
                model["problems"].append({"kind": "broken-import", "file": f["path"], "import": imp})
    for coll in ("agents", "skills", "commands"):
        for it in model[coll]:
            text = _read(os.path.join(root, *it["path"].split("/")))
            for n, kind in names.items():
                if n != it["name"] and len(n) > 3 and re.search(r"(?<![\w-])%s(?![\w-])" % re.escape(n), text):
                    model["references"].append({"from": "%s:%s" % (coll[:-1], it["name"]), "to": "%s:%s" % (kind, n)})


_RULE = re.compile(r"(?im)^\s*[-*]?\s*(?:\*\*)?(always|never|must not|must|do not|don't)\b(.{5,160})$")


def _find_instruction_conflicts(model, root):
    """Heuristic: the same subject told to 'always' X in one file and 'never' X in another."""
    stmts = []
    paths = [f["path"] for f in model["instruction_files"]] + [r["path"] for r in model["rules"]] + \
            [a["path"] for a in model["agents"]] + [s["path"] for s in model["skills"]]
    for p in paths:
        for m in _RULE.finditer(_read(os.path.join(root, *p.split("/")))):
            pol = "neg" if m.group(1).lower() in ("never", "must not", "do not", "don't") else "pos"
            stmts.append((p, pol, _words(m.group(2)), m.group(0).strip()[:160]))
    seen = set()
    for i, a in enumerate(stmts):
        for b in stmts[i + 1:]:
            if a[1] != b[1] and _jaccard(a[2], b[2]) >= 0.5:
                key = (a[3], b[3])
                if key not in seen:
                    seen.add(key)
                    model["problems"].append({"kind": "possible-instruction-conflict",
                                              "a": "%s: %s" % (a[0], a[3]), "b": "%s: %s" % (b[0], b[3])})
            elif a[0] != b[0] and a[1] == b[1] and _jaccard(a[2], b[2]) >= 0.8:
                model["duplicates"].append({"kind": "duplicated-instruction", "where": [a[0], b[0]], "text": a[3]})


def capability_seed(model):
    """One capability per discovered mechanism, status UNKNOWN until a human/agent maps it."""
    caps = []
    for kind in ("agents", "skills", "commands", "hooks", "mcp_servers", "rules"):
        for it in model[kind]:
            if kind == "hooks" and it.get("aiwos"):
                continue
            name = it.get("name") or ("%s %s" % (it.get("event"), it.get("matcher") or "*") if kind == "hooks" else it.get("path"))
            caps.append({"id": "CAP-%03d" % (len(caps) + 1), "capability": "%s: %s" % (kind[:-1], name),
                         "old_implementation": it.get("path") or it.get("settings") or it.get("command"),
                         "new_implementation": None, "status": "UNKNOWN", "behavioral_differences": None,
                         "validation": None})
    for f in model["instruction_files"]:
        caps.append({"id": "CAP-%03d" % (len(caps) + 1), "capability": "instructions: %s" % f["path"],
                     "old_implementation": f["path"], "new_implementation": None, "status": "UNKNOWN",
                     "behavioral_differences": None, "validation": None})
    return caps


def write_model(root, model):
    out_dir = os.path.join(root, "workspace", "migration")
    os.makedirs(out_dir, exist_ok=True)
    write_text_atomic(os.path.join(out_dir, "project-model.json"), dumps(model))
    cmap_path = os.path.join(out_dir, "capability-map.json")
    existing = read_json(cmap_path)
    if existing is None:
        existing = {"version": 1, "level": "analyze", "capabilities": capability_seed(model)}
        write_text_atomic(cmap_path, dumps(existing))
    else:  # add newly discovered capabilities, never overwrite human/agent mapping work
        known = {c["old_implementation"] for c in existing["capabilities"]}
        for c in capability_seed(model):
            if c["old_implementation"] not in known:
                c["id"] = "CAP-%03d" % (len(existing["capabilities"]) + 1)
                existing["capabilities"].append(c)
        write_text_atomic(cmap_path, dumps(existing))
    write_text_atomic(os.path.join(out_dir, "project-model.md"), render_model(model, existing))
    return out_dir


def render_model(model, cmap):
    L = ["# Project model (AI mechanisms)", "",
         "Generated by `aiwos adopt scan` on %s. Read-only inventory; edit `capability-map.json`, not this file." % model["scanned_at"], "",
         "| Mechanism | Count |", "|---|---|"]
    for k in ("instruction_files", "agents", "skills", "commands", "rules", "hooks", "mcp_servers", "scripts"):
        L.append("| %s | %d |" % (k.replace("_", " "), len(model[k])))
    L += ["", "## Agents", ""] + ["- `%s` — %s (model: %s, tools: %s)" % (a["name"], a["description"][:120], a.get("model") or "inherit", a.get("tools") or "all") for a in model["agents"]]
    L += ["", "## Skills / commands", ""] + ["- `/%s` — %s" % (s["name"], s["description"][:120]) for s in model["skills"] + model["commands"]]
    L += ["", "## Hooks", ""] + ["- %s `%s` → `%s`" % (h["event"], h.get("matcher") or "*", (h.get("command") or "")[:100]) for h in model["hooks"]]
    probs = ["- **%s** %s" % (p["kind"], json.dumps({k: v for k, v in p.items() if k != "kind"}, ensure_ascii=False)[:240])
             for p in model["problems"]]
    dups = ["- **%s** %s" % (d["kind"], ", ".join(d.get("where", []))) for d in model["duplicates"]]
    L += ["", "## Problems", ""] + (probs or ["- none found"])
    L += ["", "## Duplicates", ""] + (dups or ["- none found"])
    counts = defaultdict(int)
    for c in cmap["capabilities"]:
        counts[c["status"]] += 1
    L += ["", "## Capability map status", "", ", ".join("%s: %d" % kv for kv in sorted(counts.items())) or "empty", ""]
    return "\n".join(L)


def verify(root):
    """Migration gate: no capability may be UNKNOWN, and kept ones must still exist."""
    cmap = read_json(os.path.join(root, "workspace", "migration", "capability-map.json"))
    if cmap is None:
        raise AiwosError("no capability map — run `aiwos adopt scan` first")
    problems = []
    for c in cmap["capabilities"]:
        st = c.get("status")
        if st not in CAP_STATUSES:
            problems.append("%s invalid status %r" % (c["id"], st))
        elif st == "UNKNOWN":
            problems.append("%s %s is unaccounted for" % (c["id"], c["capability"]))
        elif st in ("PRESERVED", "IMPROVED", "REPLACED", "PARTIAL"):
            impl = c.get("new_implementation") or (c.get("old_implementation") if st == "PRESERVED" else None)
            if not impl:
                problems.append("%s %s has no implementation reference" % (c["id"], st))
            elif "/" in str(impl) and not os.path.exists(os.path.join(root, *str(impl).split("/"))):
                problems.append("%s implementation %s does not exist" % (c["id"], impl))
            if st != "PRESERVED" and not c.get("validation"):
                problems.append("%s %s without validation evidence" % (c["id"], st))
        if st == "PARTIAL":
            problems.append("%s is PARTIAL — migration cannot be called complete" % c["id"])
    return problems
