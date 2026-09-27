"""The LLM layer. It only *proposes*: incidents with tier + one whitelisted action.
policy.decide() has the final say. If the model fails, we fall back to deterministic alerts."""
import json, os, time, logging
import requests

log = logging.getLogger("brain")

SCHEMA = {
    "type": "object",
    "properties": {"incidents": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "target": {"type": "string"},
            "checks": {"type": "array", "items": {"type": "string"}},
            "summary": {"type": "string"},
            "tier": {"type": "integer", "minimum": 0, "maximum": 4},
            "security": {"type": "boolean"},
            "action": {"type": "string", "enum": ["none", "start_vm", "reboot_vm", "start_ct", "reboot_ct", "restart_service"]},
            "action_arg": {"type": "string"},
            "reasoning": {"type": "string"}},
        "required": ["target", "checks", "summary", "tier", "security", "action", "action_arg", "reasoning"]}}},
    "required": ["incidents"]}

LAB = "a homelab"
STYLE = ""


def set_lab(desc, style=""):
    """Lab description + optional owner style (from messages.yaml ai_style). Style shapes wording only;
    the output schema, tiers and safety rules below always win."""
    global LAB, STYLE
    LAB = desc or LAB
    STYLE = style or ""


def _sys(prompt):
    p = prompt.replace("{lab}", LAB)
    return p + (f"\n\nOwner's style preference for wording (never changes format, tiers or safety rules): {STYLE}" if STYLE else "")


SYSTEM = """You are the overseer of {lab}.
You get a snapshot of health checks and metrics. Group failing checks into incidents by root cause (one incident per root cause; e.g. pfSense down explains many unreachable services — report pfsense, not ten incidents).

Severity tiers:
0 info: expected/self-healing noise. 1 low: degraded, non-critical, no risk. 2 medium: a service down with a safe reversible fix.
3 high: core infra (router/firewall, switches, the hypervisor host, domain controllers, anything with floor_tier 3). 4 critical: suspected compromise or data risk -> set security=true.

Action: pick ONE from the target's allowed actions list, or "none". Prefer the least disruptive (restart_service before reboot). For restart_service put the service name in action_arg. Never invent targets; use target names exactly as given. If unsure, tier higher and action none.
Keep summary under 120 chars, plain language. Output JSON only matching the schema. If nothing is wrong, return {"incidents": []}."""


def build_prompt(snap, targets, open_incidents, big=40):
    """Everything relevant, nothing else — keeps CPU models fast even on 100+ VM clusters."""
    checks = snap["checks"]
    failing = [c for c in checks if not c["ok"]]
    passing = [c["name"] for c in checks if c["ok"]]
    hot_targets = {c["target"] for c in failing}
    facts = snap.get("facts", {})
    guests = facts.get("guests", {})
    if len(guests) > big or len(targets) > big:
        # big lab: send failing-related + critical targets, down guests, and the busiest few
        inv_keys = [k for k, v in targets.items() if k in hot_targets or v.get("floor_tier", 0) >= 3]
        vmids = {str(targets[k].get("vmid") or targets[k].get("vmname")) for k in inv_keys}
        keep = {gid: g for gid, g in guests.items()
                if g["status"] != "running" or str(gid) in vmids or g.get("name") in vmids}
        keep.update(dict(sorted(guests.items(), key=lambda kv: -(kv[1].get("cpu_pct") or 0))[:8]))
        facts = dict(facts, guests=keep, guests_total=len(guests), guests_running=sum(g["status"] == "running" for g in guests.values()))
        passing = {"count": len(passing), "related": [n for n in passing if any(c["name"] == n and c["target"] in hot_targets for c in checks)]}
        note = f"Large lab: showing {len(inv_keys)} of {len(targets)} targets (those with failures + critical ones)."
    else:
        inv_keys = list(targets)
        note = ""
    inv = {k: {"kind": targets[k].get("kind"), "id": targets[k].get("vmid") or targets[k].get("vmname"),
               "critical": targets[k].get("floor_tier", 0) >= 3, "allowed_actions": targets[k].get("actions", []),
               "notes": targets[k].get("notes", "")} for k in inv_keys}
    return json.dumps({
        "note": note,
        "failing_checks": failing,
        "passing_checks": passing,
        "metrics": facts,
        "targets": inv,
        "already_open_incidents": [{"id": i["id"], "target": i["target"], "summary": i["summary"], "status": i["status"]} for i in open_incidents],
    }, default=str, separators=(",", ":"))


BRIEF_SYSTEM = """You are the analyst for {lab}.
You get the current snapshot, a short metrics history, and recent incidents. Write a brief status read for the owner's phone:
- 1 line overall verdict.
- Up to 4 short bullets: notable trends or anomalies (rising CPU/RAM/storage, latency, flapping, a guest hogging resources), with numbers.
- 1 bullet 'Watch:' naming the single thing most likely to cause trouble next, or 'nothing notable'.
Baselines (these are NORMAL, don't flag them): VM RAM is host-side usage — values near 100% are normal for VMs without a balloon driver/guest tools; WAN ping under 30ms is good, 9.9.9.9 up to ~70ms is normal; storage under 80% is fine; node CPU under 60% is fine.
Only flag things outside those baselines or clearly trending toward them. If nothing is abnormal, say so plainly.
Plain text, no markdown, under 600 characters. Don't invent data; if history is short, say so. Be direct, no filler."""


class Ollama:
    def __init__(self, c):
        self.url, self.model, self.timeout = c["url"].rstrip("/"), c["model"], c.get("timeout", 300)
        self.brief_model = c.get("brief_model", self.model)

    def name(self):
        return f"ollama:{self.model}" + (f" + {self.brief_model}" if self.brief_model != self.model else "")

    def triage(self, prompt, escalate=False):
        r = requests.post(self.url + "/api/chat", timeout=self.timeout, json={
            "model": self.model, "stream": False, "format": SCHEMA, "keep_alive": -1, "options": {"temperature": 0.1, "num_ctx": 8192},
            "messages": [{"role": "system", "content": _sys(SYSTEM)}, {"role": "user", "content": prompt}]})
        r.raise_for_status()
        return json.loads(r.json()["message"]["content"])


    def brief(self, prompt):
        r = requests.post(self.url + "/api/chat", timeout=150, json={
            "model": self.brief_model, "stream": False, "keep_alive": -1,
            "options": {"temperature": 0.3, "num_ctx": 8192, "num_predict": 220},
            "messages": [{"role": "system", "content": _sys(BRIEF_SYSTEM)}, {"role": "user", "content": prompt}]})
        r.raise_for_status()
        return r.json()["message"]["content"].strip()


class Claude:
    def __init__(self, c):
        import anthropic
        self.client = anthropic.Anthropic(api_key=os.environ.get(c.get("api_key_env", "ANTHROPIC_API_KEY")))
        self.triage_model, self.escalate_model = c["triage_model"], c["escalate_model"]
        self.last_model = self.triage_model

    def name(self):
        return f"claude:{self.last_model}"

    def triage(self, prompt, escalate=False):
        self.last_model = self.escalate_model if escalate else self.triage_model
        msg = self.client.messages.create(
            model=self.last_model, max_tokens=2000,
            system=[{"type": "text", "text": _sys(SYSTEM), "cache_control": {"type": "ephemeral"}}],
            tools=[{"name": "report", "description": "Report incidents", "input_schema": SCHEMA}],
            tool_choice={"type": "tool", "name": "report"},
            messages=[{"role": "user", "content": prompt}])
        for b in msg.content:
            if b.type == "tool_use":
                return b.input
        raise ValueError("no tool_use in response")


    def brief(self, prompt):
        msg = self.client.messages.create(model=self.triage_model, max_tokens=600,
                                          system=_sys(BRIEF_SYSTEM), messages=[{"role": "user", "content": prompt}])
        return "".join(b.text for b in msg.content if b.type == "text").strip()


def digest(snap, history, recent_incidents):
    """Compact text digest (~300-500 tokens) — CPU prefill is the bottleneck, so keep it tight."""
    f = snap.get("facts", {})
    n, g = f.get("node", {}), f.get("guests", {})
    L = [f"node cpu {n.get('cpu_pct')}% ram {n.get('mem_pct')}% up {round((n.get('uptime_h') or 0)/24,1)}d"]
    ordered = sorted(g.values(), key=lambda v: -(v.get("cpu_pct") or 0))
    down = [v for v in ordered if v["status"] != "running"]
    shown = (ordered[:12] + [v for v in down if v not in ordered[:12]]) if len(ordered) > 15 else ordered
    L.append(f"guests ({len(g) - len(down)}/{len(g)} running; name cpu%/ram%): " + ", ".join(
        f"{v['name']} {v['cpu_pct']}/{min(v['mem_pct'] or 0, 100):g}" + ("" if v["status"] == "running" else " DOWN") for v in shown)
        + (f" (+{len(ordered) - len(shown)} quiet guests omitted)" if len(shown) < len(ordered) else ""))
    chk = snap.get("checks", [])
    L.append("storage: " + ", ".join(c["detail"] for c in chk if c["name"].startswith("storage-")))
    L.append("wan: " + ", ".join(c["detail"].split("=")[-1].strip() if c["ok"] else "DOWN" for c in chk if c["name"].startswith("wan-")))
    bad = [f"{c['name']}: {c['detail'][:60]}" for c in chk if not c["ok"]]
    L.append(f"checks: {len(chk)-len(bad)}/{len(chk)} ok" + (" | FAILING: " + "; ".join(bad) if bad else ""))
    if history:
        L.append("history (time cpu ram failing#): " + " | ".join(f"{h['t']} {h['cpu']} {h['ram']} {len(h['failing'])}" for h in history))
    else:
        L.append("history: none yet (just restarted)")
    L.append("incidents 24h: " + ("; ".join(f"{i['id']} T{i['tier']} {i['target']} {i['status']}: {i['summary'][:50]}" for i in recent_incidents[-8:]) or "none"))
    return "\n".join(L)


def brief(backend, snap, history, recent_incidents):
    prompt = digest(snap, history, recent_incidents)
    t0 = time.time()
    try:
        return backend.brief(prompt), round(time.time() - t0)
    except Exception as e:
        log.warning("brief failed: %s", e)
        return None, round(time.time() - t0)


def make_backend(llm_cfg):
    return Claude(llm_cfg["claude"]) if llm_cfg["backend"] == "claude" else Ollama(llm_cfg["ollama"])


def fallback(snap):
    """Model unavailable: one tier-1 alert per failing target, no actions."""
    by_t = {}
    for c in snap["checks"]:
        if not c["ok"]:
            by_t.setdefault(c["target"], []).append(c)
    return {"incidents": [{"target": t, "checks": [c["name"] for c in cs], "summary": "; ".join(c["detail"] for c in cs)[:120],
                           "tier": 1, "security": False, "action": "none", "action_arg": "", "reasoning": "LLM unavailable - raw alert"}
                          for t, cs in by_t.items()]}


def triage(backend, snap, targets, open_incidents, decision_log, escalate_at_tier=2):
    prompt = build_prompt(snap, targets, open_incidents)
    t0 = time.time()
    try:
        out = backend.triage(prompt)
        # Claude: re-run anything serious on the stronger model
        if isinstance(backend, Claude) and any(i.get("tier", 0) >= escalate_at_tier for i in out.get("incidents", [])):
            out = backend.triage(prompt, escalate=True)
        err = None
    except Exception as e:
        log.warning("LLM failed: %s", e)
        out, err = fallback(snap), str(e)[:300]
    rec = {"ts": snap["ts"], "backend": backend.name(), "latency_s": round(time.time() - t0, 1),
           "error": err, "prompt": prompt, "output": out}
    try:
        with open(decision_log, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except OSError as e:
        log.warning("decision log write failed: %s", e)
    return out.get("incidents", [])
