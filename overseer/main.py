"""Overseer main loop: collect -> (LLM triage when needed) -> policy -> notify/act."""
import argparse, collections, logging, os, threading, time
import yaml, urllib3

from . import brain, policy, actions
from .collectors import Switch, snapshot
from .hypervisors import make_hypervisor
from .notifiers import make_notifier
from .state import State

urllib3.disable_warnings()
log = logging.getLogger("overseer")
ICON = {0: "·", 1: "🟡", 2: "🟠", 3: "🔴", 4: "🚨"}


def normalize_cfg(cfg):
    """Accept older config layouts so upgrades never break a working install."""
    cfg = dict(cfg)
    if "notifier" not in cfg:
        if "telegram" in cfg:
            cfg["notifier"] = dict(cfg.pop("telegram"), type="telegram")
        elif "discord" in cfg:
            cfg["notifier"] = dict(cfg.pop("discord"), type="discord")
    if "hypervisor" not in cfg and "proxmox" in cfg:
        cfg["hypervisor"] = dict(cfg.pop("proxmox"), type="proxmox")
    cfg.pop("whatsapp", None)
    t = cfg.get("targets") or {}
    for old, new in (("proxmox", "host"),):          # old name for the hypervisor host target
        if old in t and new not in t:
            t[new] = t.pop(old)
            for c in cfg.get("checks") or []:
                if c.get("target") == old:
                    c["target"] = new
    return cfg


class Engine:
    def __init__(self, cfg):
        cfg = normalize_cfg(cfg)
        self.cfg = cfg
        self.targets = cfg.get("targets") or {}
        self.state = State(cfg["db_path"])
        self.pve = make_hypervisor(cfg)
        self.switch = Switch(cfg["switch"]) if (cfg.get("switch") or {}).get("host") else None
        brain.set_lab(cfg.get("lab_description"))
        self.backend = brain.make_backend(cfg["llm"])
        self.wa = make_notifier(cfg, self.state)
        self.grace = cfg.get("grace_minutes", 5) * 60
        self.lock = threading.Lock()
        if self.state.meta_get("dry_run") is None:
            self.state.meta_set("dry_run", int(cfg.get("dry_run", True)))
        self.last_failing = set()
        self.last_snap = None
        self.history = collections.deque(maxlen=60)   # ~1h of compact per-tick metrics
        self.briefing = threading.Lock()
        self.llm = threading.Lock()   # one LLM job at a time — CPU inference can't share cores
        self.last_review = time.time()  # first full review after an interval, not at boot

    @property
    def dry_run(self):
        return self.state.meta_get("dry_run", "1") == "1"

    # ---------- inbound commands ----------
    def on_command(self, verb, iid, raw):
        with self.lock:
            if verb == "STATUS":
                self.wa.send(self.status_text())
                return self._start_brief()
            if verb in (None, "HELP"):
                return self.wa.send("Commands: /status · /auto · /watch · /go <id> · /stop <id>")
            if verb in ("WATCH", "AUTO"):
                self.state.meta_set("dry_run", int(verb == "WATCH"))
                return self.wa.send("🤖 AUTO — fixes you approve with Go will actually run" if not self.dry_run else "👀 WATCH — alerts only; Go just simulates")
            inc = self.state.get(iid) if iid else None
            if not inc or inc["status"] not in ("scheduled", "pending_approval"):
                return self.wa.send(f"No pending action {iid or ''}".strip())
            if verb == "STOP":
                self.state.set_status(inc["id"], "cancelled", "owner said stop")
                return self.wa.send(f"✋ {inc['id']} cancelled. Still watching {inc['target']}.")
            if verb == "GO":
                self._run(inc)

    def status_text(self):
        """Instant overview from the last snapshot — no LLM involved."""
        s, opens = self.last_snap, self.state.open_incidents()
        L = [f"👁 Overseer · {'WATCH' if self.dry_run else 'AUTO'} · {self.backend.name()}"]
        if not s:
            return L[0] + "\nNo snapshot yet — first sweep still running."
        age = int(time.time() - s["ts"])
        checks = s["checks"]
        bad = [c for c in checks if not c["ok"]]
        L.append(f"{'🟢' if not bad else '🔴'} {len(checks) - len(bad)}/{len(checks)} checks OK · {age}s ago")
        n = s.get("facts", {}).get("node")
        nodes = s.get("facts", {}).get("nodes") or []
        if n:
            L.append(f"🖥 {n.get('name', 'host')}: CPU {n['cpu_pct']}% · RAM {n['mem_pct']}% · up {n['uptime_h'] / 24:.1f}d")
        for nd in nodes:
            L.append(f"   · {nd['name']}: CPU {nd['cpu_pct']}% · RAM {nd['mem_pct']}%")
        g = s.get("facts", {}).get("guests", {})
        if g:
            down = [f"{k} {v['name']}" for k, v in sorted(g.items()) if v["status"] != "running"]
            L.append(f"📦 Guests: {len(g) - len(down)}/{len(g)} running" + (f" (down: {', '.join(down)})" if down else ""))
            top = sorted(g.values(), key=lambda v: -(v.get("cpu_pct") or 0))[:3]
            L.append("🔥 Top CPU: " + ", ".join(f"{v['name']} {v['cpu_pct']}%" for v in top))
            topm = sorted(g.values(), key=lambda v: -(v.get("mem_pct") or 0))[:3]
            L.append("🧮 Top RAM: " + ", ".join(f"{v['name']} {min(v['mem_pct'], 100):g}%" for v in topm if v.get("mem_pct") is not None))
        stor = [c["detail"].replace(" used", "") for c in checks if c["name"].startswith("storage-")]
        if stor:
            L.append(f"💾 {' · '.join(stor)}")
        lag = [c for c in checks if c["name"].startswith("sw-")]
        if lag:
            L.append(f"🔌 Switch: {sum(c['ok'] for c in lag)}/{len(lag)} watched ports healthy")
        wan = [c["detail"].split("/")[-3] if "rtt" in c["detail"] else "down" for c in checks if c["name"].startswith("wan-")]
        if wan:
            L.append(f"🌐 WAN ping: {' / '.join(w + 'ms' if w != 'down' else w for w in wan)}")
        for c in bad:
            L.append(f"  ✗ {c['name']}: {c['detail'][:80]}")
        day = self.state.recent()
        L.append(f"🧾 {len(opens)} open · {len(day)} in last 24h" if (opens or day) else "🧾 No incidents in last 24h")
        for i in opens:
            L.append(f"  {ICON[i['tier']]} {i['id']} {i['target']}: {i['summary']} [{i['status']}]")
        L.append(f"🧠 Last LLM review {int((time.time() - self.last_review) / 60)} min ago")
        return "\n".join(L)

    def _start_brief(self):
        if not self.last_snap:
            return
        if not self.briefing.acquire(blocking=False):
            return self.wa.send("🧠 Analyst is already on it…")
        self.wa.send("🧠 Analyst thinking…")
        def run():
            try:
                hist = list(self.history)[::10][-6:]  # ~every 10 min, max 6 points
                if not self.llm.acquire(timeout=240):
                    return self.wa.send("🧠 Analyst skipped — incident triage has the LLM right now. Try again shortly.")
                try:
                    text, secs = brain.brief(self.backend, self.last_snap, hist, self.state.recent())
                finally:
                    self.llm.release()
                self.wa.send(f"🧠 Analyst ({secs}s):\n{text}" if text else f"🧠 Analyst gave up after {secs}s (LLM busy or slow) — overview above is current.")
            finally:
                self.briefing.release()
        threading.Thread(target=run, daemon=True).start()

    # ---------- core loop ----------
    def _run(self, inc):
        t = self.targets.get(inc["target"], {})
        ok, detail = actions.execute(inc["action"], inc["action_arg"], inc["target"], t, self.pve, self.dry_run)
        self.state.log_action(inc["id"], inc["target"], inc["action"], inc["action_arg"], self.dry_run, ok, detail)
        self.state.set_status(inc["id"], "executed" if ok else "failed", detail)
        self.wa.send(f"{'✅' if ok else '❌'} {inc['id']} {inc['action']} {inc['action_arg']} on {inc['target']}: {detail}")

    def handle(self, proposal):
        checks = sorted(proposal.get("checks") or [])
        fp = f"{proposal.get('target')}|{','.join(checks)}"
        if self.state.open_by_fingerprint(fp):
            return  # already tracking, don't spam
        rec = self.state.recent_actions(proposal.get("target", ""))
        d = policy.decide(proposal, self.targets, rec, self.cfg.get("max_actions_per_hour", 2))
        summary = proposal.get("summary", "")[:160]
        if d.mode == "log":
            log.info("T0 %s: %s", proposal.get("target"), summary)
            return
        status = {"grace": "scheduled", "approve": "pending_approval"}.get(d.mode, "open")
        iid = self.state.create(fp, proposal.get("target"), d.tier, summary, d.action, d.action_arg, d.mode, status,
                                time.time() + self.grace if d.mode == "grace" else None)
        act = f"{d.action} {d.action_arg}".strip()
        msg = f"{ICON[d.tier]} T{d.tier} [{iid}] {proposal.get('target')}: {summary}"
        if proposal.get("reasoning"):
            msg += f"\nWhy: {proposal['reasoning'][:200]}"
        if d.reason:
            msg += f"\nPolicy: {d.reason}"
        if d.mode == "grace":
            msg += "\nTap Go to run."
        elif d.mode == "approve":
            msg += f"\nProposed: {act}. Tap Go / Stop."
        elif d.mode == "notify_only":
            msg += "\nNot touching this. Your call."
        if self.dry_run and d.action != "none":
            msg += " (watch mode — won't run)"
        self.wa.send(msg, incident_id=iid, mode=d.mode)

    def tick(self):
        snap = snapshot(self.cfg, self.pve, self.switch)
        self.last_snap = snap
        n = snap.get("facts", {}).get("node", {})
        g = snap.get("facts", {}).get("guests", {})
        self.history.append({"t": time.strftime("%H:%M", time.localtime(snap["ts"])), "cpu": n.get("cpu_pct"), "ram": n.get("mem_pct"),
                             "failing": sorted(c["name"] for c in snap["checks"] if not c["ok"]),
                             "top_guests_cpu": sorted(((v["name"], v["cpu_pct"]) for v in g.values()), key=lambda x: -(x[1] or 0))[:3]})
        failing = {c["name"] for c in snap["checks"] if not c["ok"]}
        with self.lock:
            # resolve incidents whose checks all pass again
            for inc in self.state.open_incidents():
                inc_checks = set(filter(None, inc["fingerprint"].split("|", 1)[1].split(",")))
                if inc_checks and not (inc_checks & failing):
                    self.state.set_status(inc["id"], "resolved")
                    self.wa.send(f"✅ {inc['id']} {inc['target']} recovered.")
            review_due = time.time() - self.last_review > self.cfg.get("review_interval_minutes", 60) * 60
            new = failing - self.last_failing
            self.last_failing = failing
            open_now = self.state.open_incidents()
        # LLM runs OUTSIDE the lock — CPU inference takes minutes and must not block Telegram commands
        props = []
        if new or review_due:
            self.last_review = time.time()
            with self.llm:
                props = brain.triage(self.backend, snap, self.targets, open_now,
                                     self.cfg["decision_log"], self.cfg["llm"].get("claude", {}).get("escalate_at_tier", 2))
        with self.lock:
            for p in props:
                self.handle(p)
            for inc in self.state.due():
                self._run(inc)
        log.info("tick: %d checks, %d failing, took %ss", len(snap["checks"]), len(failing), snap["took_s"])
        hb = self.cfg.get("heartbeat_url")
        if hb:  # dead-man switch: if the lab/WAN dies, the external service alerts you instead
            try:
                import requests
                requests.get(hb, timeout=5)
            except Exception:
                pass

    def loop(self):
        self.wa.send(f"👁 Overseer online ({self.backend.name()}, {'WATCH' if self.dry_run else 'AUTO'}).")
        while True:
            try:
                self.tick()
            except Exception:
                log.exception("tick failed")
            time.sleep(self.cfg.get("interval_seconds", 60))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-c", "--config", default="/etc/overseer/config.yaml")
    ap.add_argument("--once", action="store_true", help="one tick, print snapshot, exit")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    cfg = yaml.safe_load(open(a.config))
    os.makedirs(os.path.dirname(cfg["db_path"]), exist_ok=True)
    eng = Engine(cfg)
    if a.once:
        import json
        print(json.dumps(snapshot(eng.cfg, eng.pve, eng.switch), indent=2, default=str))
        return
    eng.wa.start(eng.on_command)
    eng.loop()


if __name__ == "__main__":
    main()
