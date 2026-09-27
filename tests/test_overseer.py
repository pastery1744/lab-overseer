import json, time, yaml, pathlib
from overseer import policy, brain
from overseer.notifiers import telegram
from overseer.main import Engine

CFG = yaml.safe_load(open(pathlib.Path(__file__).parent.parent / "config.example.yaml"))
# test inventory (the shipped example is intentionally near-empty)
CFG["targets"] = {
    "pfsense": {"kind": "vm", "vmid": 100, "floor_tier": 3, "actions": ["start_vm"]},
    "pihole": {"kind": "vm", "vmid": 120, "actions": ["restart_service:pihole-FTL", "start_vm", "reboot_vm"],
               "ssh": {"host": "10.0.0.53", "user": "overseer"}},
}
T = CFG["targets"]


def P(**kw):
    base = dict(target="pihole", checks=["pihole-dns"], summary="x", tier=2, security=False, action="restart_service",
                action_arg="pihole-FTL", reasoning="")
    base.update(kw)
    return base


# ---- policy: the LLM can't talk its way past the floors ----
def test_every_fix_needs_approval():
    for tier in (1, 2, 3):
        d = policy.decide(P(tier=tier), T, 0)
        assert (d.mode, d.action) == ("approve", "restart_service")

def test_pfsense_floor_forces_approval():
    d = policy.decide(P(target="pfsense", tier=1, action="start_vm", action_arg=""), T, 0)
    assert (d.tier, d.mode) == (3, "approve")

def test_non_whitelisted_action_stripped():
    d = policy.decide(P(target="pfsense", tier=3, action="reboot_vm", action_arg=""), T, 0)
    assert d.action == "none" and d.mode == "notify"

def test_arbitrary_service_rejected():
    d = policy.decide(P(action_arg="ssh"), T, 0)
    assert d.action == "none"

def test_security_never_acts():
    d = policy.decide(P(tier=1, security=True), T, 0)
    assert (d.tier, d.mode, d.action) == (4, "notify_only", "none")

def test_llm_tier4_never_acts():
    assert policy.decide(P(tier=4), T, 0).action == "none"

def test_unknown_target():
    d = policy.decide(P(target="nuclear_reactor"), T, 0)
    assert d.action == "none"

def test_flapping_escalates():
    d = policy.decide(P(), T, recent_actions=2)
    assert (d.tier, d.mode) == (3, "approve")

def test_tier_clamped():
    assert policy.decide(P(tier=-5, action="none"), T, 0).tier == 0
    assert policy.decide(P(tier=99), T, 0).tier == 4


# ---- engine end-to-end with fake brain + no network ----
class FakeBackend:
    def __init__(self, out): self.out = out
    def name(self): return "fake"
    def triage(self, prompt, escalate=False): return self.out


def make_engine(tmp_path, monkeypatch, proposals, failing):
    cfg = dict(CFG, confirm_sweeps=1, notifier={'type': 'telegram', 'chat_id': '42'}, hypervisor={'type': 'none'}, db_path=str(tmp_path / "o.db"), decision_log=str(tmp_path / "d.jsonl"),
               switch=None, checks=[], grace_minutes=0)
    eng = Engine(cfg)
    eng.backend = FakeBackend({"incidents": proposals})
    sent = []
    eng.wa.send = lambda t, **k: sent.append(t)
    snap = {"ts": 1, "took_s": 0, "facts": {}, "checks": [{"name": n, "target": "x", "ok": False, "detail": "down"} for n in failing]}
    monkeypatch.setattr("overseer.main.snapshot", lambda *a: snap)
    return eng, sent, snap


def test_tier2_waits_for_go(tmp_path, monkeypatch):
    eng, sent, _ = make_engine(tmp_path, monkeypatch, [P()], ["pihole-dns"])
    eng.tick(); eng.tick()
    inc = eng.state._q("SELECT * FROM incidents")[0]
    assert inc["status"] == "pending_approval"            # never auto-runs, even with grace=0
    eng.on_command("GO", inc["id"], "")
    assert eng.state.get(inc["id"])["status"] == "executed"
    assert any("WATCH mode" in s for s in sent)

def test_stop_cancels(tmp_path, monkeypatch):
    eng, sent, _ = make_engine(tmp_path, monkeypatch, [P()], ["pihole-dns"])
    eng.grace = 999
    eng.tick()
    iid = eng.state._q("SELECT id FROM incidents")[0]["id"]
    eng.on_command("STOP", iid, "")
    assert eng.state.get(iid)["status"] == "cancelled"

def test_pfsense_waits_for_go(tmp_path, monkeypatch):
    eng, sent, _ = make_engine(tmp_path, monkeypatch, [P(target="pfsense", checks=["pfsense-gw-v20"], action="start_vm", action_arg="")], ["pfsense-gw-v20"])
    eng.tick(); eng.tick()
    iid = eng.state._q("SELECT id FROM incidents")[0]["id"]
    assert eng.state.get(iid)["status"] == "pending_approval"
    eng.on_command("GO", iid, "")
    assert eng.state.get(iid)["status"] == "executed"

def test_dedupe_and_recovery(tmp_path, monkeypatch):
    eng, sent, snap = make_engine(tmp_path, monkeypatch, [P(action="none")], ["pihole-dns"])
    eng.tick(); eng.last_failing = set(); eng.tick()
    assert len(eng.state._q("SELECT * FROM incidents")) == 1
    snap["checks"] = []
    eng.tick()
    assert any("recovered" in s for s in sent)

def test_llm_crash_falls_back(tmp_path, monkeypatch):
    eng, sent, _ = make_engine(tmp_path, monkeypatch, [], ["pihole-dns"])
    def boom(*a, **k): raise TimeoutError("ollama slow")
    eng.backend.triage = boom
    eng.tick()
    assert any("T1" in s for s in sent)
    rec = json.loads(open(tmp_path / "d.jsonl").readline())
    assert rec["error"]

def test_telegram_owner_only(tmp_path, monkeypatch):
    eng, sent, _ = make_engine(tmp_path, monkeypatch, [], [])
    got = []
    eng.wa._call = lambda *a, **k: {"ok": True}
    h = lambda *a: got.append(a)
    eng.wa._handle_update({"message": {"chat": {"id": 999}, "text": "/go A11"}}, h)
    eng.wa._handle_update({"message": {"chat": {"id": 42}, "text": "/stop a11"}}, h)
    eng.wa._handle_update({"callback_query": {"id": "x", "data": "GO B22", "message": {"chat": {"id": 42}}}}, h)
    assert [g[:2] for g in got] == [("STOP", "A11"), ("GO", "B22")]

def test_tg_parse():
    assert telegram.parse_command("/status") == ("STATUS", None)
    assert telegram.parse_command("/go c07") == ("GO", "C07")
    assert telegram.parse_command("/start") == ("HELP", None)

def test_status_overview(tmp_path, monkeypatch):
    eng, sent, snap = make_engine(tmp_path, monkeypatch, [], [])
    assert "No snapshot" in eng.status_text()
    snap["facts"] = {"node": {"cpu_pct": 12.0, "mem_pct": 40.0, "uptime_h": 552, "load": None},
                     "guests": {100: {"name": "pfSense", "status": "running", "cpu_pct": 2}, 127: {"name": "Minecraft", "status": "stopped", "cpu_pct": 0}}}
    snap["checks"] = [{"name": "wan-1111", "target": "internet", "ok": True, "detail": "rtt min/avg/max/mdev = 12.5/12.6/12.7/0.1 ms"},
                      {"name": "storage-backups", "target": "proxmox", "ok": True, "detail": "backups 65.4% used"},
                      {"name": "pihole-dns", "target": "pihole", "ok": False, "detail": "timeout"}]
    eng.tick()
    t = eng.status_text()
    print(t)
    assert "2/3 checks" in t and "RAM 40.0%" in t and "1/2 running" in t and "Minecraft" in t and "12.6ms" in t and "pihole-dns" in t

def test_status_triggers_analyst(tmp_path, monkeypatch):
    eng, sent, snap = make_engine(tmp_path, monkeypatch, [], [])
    eng.backend.brief = lambda prompt: "All quiet. Watch: backups at 65%."
    eng.tick()
    eng.on_command("STATUS", None, "/status")
    for _ in range(50):
        if any("Analyst (" in s for s in sent): break
        time.sleep(0.02)
    assert any("Guests" in s or "checks OK" in s for s in sent)
    assert any("Watch: backups" in s for s in sent)
    # second request while busy is refused politely, not queued
    eng.briefing.acquire(); eng.on_command("STATUS", None, "/status"); eng.briefing.release()
    assert any("already on it" in s for s in sent)

def test_analyst_failure_is_graceful(tmp_path, monkeypatch):
    eng, sent, snap = make_engine(tmp_path, monkeypatch, [], [])
    def boom(p): raise TimeoutError()
    eng.backend.brief = boom
    eng.tick(); eng.on_command("STATUS", None, "/status")
    for _ in range(50):
        if any("gave up" in s for s in sent): break
        time.sleep(0.02)
    assert any("gave up" in s for s in sent)


# ---- package-level pieces ----
def test_example_config_boots(tmp_path, monkeypatch):
    cfg = yaml.safe_load(open(pathlib.Path(__file__).parent.parent / "config.example.yaml"))
    cfg.update(db_path=str(tmp_path / "x.db"), decision_log=str(tmp_path / "d.jsonl"), checks=[])
    from overseer.main import Engine
    eng = Engine(cfg)
    assert eng.pve is None and eng.switch is None and "host" in eng.targets


def test_notifier_factory(tmp_path):
    from overseer.notifiers import make_notifier
    from overseer.state import State
    st = State(str(tmp_path / "s.db"))
    assert type(make_notifier({"notifier": {"type": "telegram"}}, st)).__name__ == "Telegram"
    d = make_notifier({"notifier": {"type": "discord", "channel_id": "1", "owner_id": "2", "guild_id": "3"}}, st)
    assert type(d).__name__ == "Discord" and d.owner_id == 2


def test_discord_registers_commands(tmp_path, monkeypatch):
    """Boot the Discord notifier with a fake login and check slash commands + button routing wire up."""
    import discord, asyncio, threading
    from overseer.notifiers.discord_bot import Discord, BTN
    from overseer.state import State
    captured = {}
    async def fake_start(self, token):
        captured["tree"] = self._connection._command_tree
    monkeypatch.setattr(discord.Client, "start", fake_start)
    monkeypatch.setenv("DISCORD_TOKEN", "x")
    d = Discord({"channel_id": "1", "owner_id": "2", "guild_id": "3"}, State(str(tmp_path / "s.db")))
    t = threading.Thread(target=d._run, args=(lambda *a: None,)); t.start(); t.join(5)
    names = {c.name for c in captured["tree"].get_commands(guild=discord.Object(id=3))}
    assert names == {"status", "auto", "watch", "go", "stop"}
    assert BTN.match("GO A12") and not BTN.match("GO a12; rm -rf")


def test_actions_route_power_to_hypervisor():
    from overseer import actions
    class HV:
        def power(self, a, t): return f"{a} {t['vmid']}"
    ok, d = actions.execute("reboot_vm", "", "x", {"vmid": 5, "kind": "vm"}, HV(), dry_run=False)
    assert ok and d == "reboot 5"
    ok, d = actions.execute("start_vm", "", "x", {"vmid": 5}, None, dry_run=False)
    assert not ok and "no hypervisor" in d


def test_esxi_collect_with_fake_vsphere(monkeypatch):
    """Exercise the ESXi backend against fake pyVmomi objects."""
    from types import SimpleNamespace as N
    from overseer.hypervisors.esxi import ESXi
    # rows shaped like ESXi._retrieve() output: property path -> value, plus the managed object
    vm = N(_moId="vm-1", name="router", guest=N(toolsRunningStatus="guestToolsRunning"), RebootGuest=lambda: None)
    vm_row = {"_obj": vm, "name": "router", "summary.config.memorySizeMB": 1024, "summary.runtime.powerState": "poweredOn",
              "summary.runtime.maxCpuUsage": 2000, "summary.quickStats.overallCpuUsage": 500, "summary.quickStats.guestMemoryUsage": 512}
    host = {"name": "esx1", "summary.quickStats": N(overallCpuUsage=1000, overallMemoryUsage=4096, uptime=7200),
            "summary.hardware": N(cpuMhz=2000, numCpuCores=4, memorySize=16 * 1024 ** 3)}
    e = ESXi.__new__(ESXi); e.cfg = {}; e.si = None
    monkeypatch.setattr(e, "_vm_rows", lambda probe=False: [vm_row]); monkeypatch.setattr(e, "_host_rows", lambda: [host])
    monkeypatch.setattr(e, "_ds_rows", lambda: [{"name": "datastore1", "summary.capacity": 100, "summary.freeSpace": 10}])
    checks, facts = e.collect({"router": {"kind": "vm", "vmname": "router"}})
    byname = {c["name"]: c for c in checks}
    assert facts["node"]["cpu_pct"] == 12.5 and facts["guests"]["vm-1"]["cpu_pct"] == 25.0
    assert byname["guest-router-running"]["ok"] and not byname["storage-datastore1"]["ok"]   # 90% used
    assert "guest reboot" in e.power("reboot", {"vmname": "router"})


def test_wizard_yaml_roundtrip():
    import sys; sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
    import setup as S
    cfg = yaml.safe_load(open(pathlib.Path(__file__).parent.parent / "config.example.yaml"))
    cfg["targets"] = T; cfg["notifier"]["chat_id"] = "123"; cfg["lab_description"] = "lab: #1, with 'quotes'"
    assert yaml.safe_load(S.to_yaml(cfg)) == cfg


def test_wizard_end_to_end_plain(tmp_path, monkeypatch):
    """Walk the whole wizard in plain mode with scripted answers and a fake network."""
    import sys, builtins, getpass, json as _j
    sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
    monkeypatch.setenv("OVERSEER_PLAIN", "1")
    for k in ("PVE_URL", "PVE_TOKEN_ID", "PVE_TOKEN", "PVE_NODE", "OVERSEER_LLM"):
        monkeypatch.delenv(k, raising=False)
    import importlib, setup as S
    importlib.reload(S)

    def fake_http(method, url, headers=None, body=None, verify=True, timeout=15):
        if url.endswith("/nodes"):
            return 200, {"data": [{"node": "pve"}]}
        if url.endswith("/cluster/resources"):
            return 200, {"data": [{"type": "node", "node": "pve"},
                                  {"type": "qemu", "vmid": 100, "name": "pfSense", "status": "running", "node": "pve"},
                                  {"type": "qemu", "vmid": 121, "name": "web", "status": "running", "node": "pve"},
                                  {"type": "qemu", "vmid": 9000, "name": "tmpl", "status": "stopped", "node": "pve", "template": 1},
                                  {"type": "lxc", "vmid": 200, "name": "radio", "status": "running", "node": "pve"}]}
        if "getMe" in url:
            return 200, {"ok": True, "result": {"username": "test_bot"}}
        if "getUpdates" in url and "timeout=10" in url:
            return 200, {"ok": True, "result": [{"update_id": 5, "message": {"chat": {"id": 777, "type": "private"}, "from": {"username": "me"}}}]}
        return 200, {"ok": True, "result": []}
    monkeypatch.setattr(S, "http", fake_http)
    monkeypatch.setattr(S, "default_gateway", lambda: "10.0.0.1")
    monkeypatch.setattr(S, "dns_servers", lambda: ["10.0.0.53"])
    answers = iter([
        "",            # welcome [Enter]
        "2",           # what to watch: proxmox
        "https://10.0.0.10:8006", "overseer@pve!t",   # url, token id  (secret via getpass)
        "",            # proxmox token msg [Enter] (msg comes first — consumed above order-insensitively)
        "",            # watch checklist: keep defaults
        "",            # critical checklist: keep suggested (pfSense)
        "1",           # telegram
        "",            # botfather msg
        "",            # pair msg
        "",            # paired msg
        "1",           # ollama local
        "n",           # extra checks
        "n",           # switch
        "n",           # heartbeat
        "",            # done msg
    ] + [""] * 10)
    monkeypatch.setattr(builtins, "input", lambda p="": next(answers))
    secrets = iter(["pve-secret", "123:TOKEN"])
    monkeypatch.setattr(getpass, "getpass", lambda p="": next(secrets))
    monkeypatch.setattr(sys, "argv", ["setup.py", "--out-dir", str(tmp_path)])
    S.main()
    cfg = yaml.safe_load(open(tmp_path / "config.yaml"))
    sec = open(tmp_path / "secrets.env").read()
    assert cfg["notifier"] == {"type": "telegram", "token_env": "TG_TOKEN", "chat_id": "777"}
    assert cfg["hypervisor"]["node"] == "pve" and cfg["targets"]["pfsense"]["floor_tier"] == 3
    assert cfg["lab_description"].startswith("a Proxmox homelab with 3 watched") and "pfsense" in cfg["lab_description"]
    assert "tmpl" not in cfg["targets"]           # templates are never targets
    assert "reboot_ct" in cfg["targets"]["radio"]["actions"] and cfg["checks"][0]["host"] == "10.0.0.1"
    assert "PVE_TOKEN=pve-secret" in sec and "TG_TOKEN=123:TOKEN" in sec
    assert oct((tmp_path / "secrets.env").stat().st_mode)[-3:] == "600"
    from overseer.main import Engine   # the written config must boot the engine
    cfg.update(db_path=str(tmp_path / "e.db"), decision_log=str(tmp_path / "d.jsonl"))
    Engine(cfg)


def test_proxmox_cluster(monkeypatch):
    from overseer.hypervisors.proxmox import Proxmox
    res = [
        {"type": "node", "node": "a", "status": "online", "cpu": 0.5, "maxcpu": 8, "mem": 8, "maxmem": 16, "uptime": 3600},
        {"type": "node", "node": "b", "status": "online", "cpu": 0.1, "maxcpu": 8, "mem": 15, "maxmem": 16, "uptime": 7200},
        {"type": "node", "node": "c", "status": "offline"},
        {"type": "qemu", "vmid": 100, "name": "fw", "status": "running", "node": "b", "cpu": 0.02, "mem": 1, "maxmem": 2},
        {"type": "lxc", "vmid": 200, "name": "ct", "status": "stopped", "node": "a", "maxmem": 1},
        {"type": "storage", "storage": "ceph", "node": "a", "shared": 1, "status": "available", "disk": 50, "maxdisk": 100},
        {"type": "storage", "storage": "ceph", "node": "b", "shared": 1, "status": "available", "disk": 50, "maxdisk": 100},
        {"type": "storage", "storage": "local-lvm", "node": "a", "status": "available", "disk": 90, "maxdisk": 100},
        {"type": "storage", "storage": "local-lvm", "node": "b", "status": "available", "disk": 10, "maxdisk": 100},
    ]
    p = Proxmox({"url": "https://x:8006", "token_id": "t"})
    posted = []
    monkeypatch.setattr(p, "get", lambda path: res)
    monkeypatch.setattr(p, "post", lambda path: posted.append(path))
    checks, facts = p.collect({"fw": {"kind": "vm", "vmid": 100}, "ct": {"kind": "ct", "vmid": 200}})
    by = {c["name"]: c for c in checks}
    assert facts["node"]["name"] == "cluster (2 nodes)" and facts["node"]["cpu_pct"] == 30.0 and len(facts["nodes"]) == 2
    assert not by["node-c-online"]["ok"] and not by["node-b-mem"]["ok"]            # offline node, 94% RAM node
    assert by["guest-100-running"]["ok"] and "on b" in by["guest-100-running"]["detail"]
    assert not by["guest-200-running"]["ok"]
    assert sum(1 for c in checks if c["name"].startswith("storage-ceph")) == 1      # shared storage counted once
    assert not by["storage-a-local-lvm"]["ok"] and by["storage-b-local-lvm"]["ok"]
    assert "on b" in p.power("reboot", {"kind": "vm", "vmid": 100}) and posted[-1] == "/nodes/b/qemu/100/status/reboot"
    res[3]["node"] = "a"; p._where.pop(100)                                         # migrated since last sweep
    p.power("start", {"kind": "vm", "vmid": 100})
    assert posted[-1] == "/nodes/a/qemu/100/status/start"


def test_esxi_multi_host(monkeypatch):
    from types import SimpleNamespace as N
    from overseer.hypervisors.esxi import ESXi
    def host(name, cpu, state="connected"):
        return {"name": name, "runtime.connectionState": state,
                "summary.quickStats": N(overallCpuUsage=cpu, overallMemoryUsage=1024, uptime=3600),
                "summary.hardware": N(cpuMhz=1000, numCpuCores=4, memorySize=8 * 1024 ** 3)}
    e = ESXi.__new__(ESXi); e.cfg = {}; e.si = None
    monkeypatch.setattr(e, "_vm_rows", lambda probe=False: [])
    monkeypatch.setattr(e, "_ds_rows", lambda: [{"name": "shared-nfs", "summary.capacity": 100, "summary.freeSpace": 50}])
    monkeypatch.setattr(e, "_host_rows", lambda: [host("esx1", 1000), host("esx2", 3000), host("esx3", 0, "disconnected")])
    checks, facts = e.collect({})
    by = {c["name"]: c for c in checks}
    assert facts["node"]["name"] == "2 ESXi hosts" and facts["node"]["cpu_pct"] == 50.0
    assert not by["host-esx3-online"]["ok"]
    assert sum(1 for c in checks if c["name"] == "storage-shared-nfs") == 1


def test_checks_run_in_parallel(monkeypatch):
    import time as _t
    from overseer import collectors as C
    monkeypatch.setattr(C, "_ping", lambda h: (_t.sleep(0.5), (False, "timeout"))[1])
    t0 = _t.time()
    out = C.run_checks([{"name": f"c{i}", "type": "ping", "host": "x"} for i in range(40)] + [{"name": "off", "type": "ping", "host": "x", "disabled": True}])
    assert _t.time() - t0 < 2 and [c["name"] for c in out] == [f"c{i}" for i in range(40)]


def test_messages_override(tmp_path, monkeypatch):
    from overseer.messages import Messages
    mp = tmp_path / "messages.yaml"
    mp.write_text('recovered: "🎉 {target} is back ({id}) {bogus}"\nicons: {2: "⚠️"}\nai_style: "Be sarcastic."\nnot_a_key: "x"\n')
    M = Messages(str(mp))
    assert M("recovered", id="K41", target="pihole") == "🎉 pihole is back (K41) {bogus}"   # typo'd placeholder doesn't crash
    assert M.icon(2) == "⚠️" and M.icon(4) == "🚨" and M.ai_style == "Be sarcastic."
    assert Messages(str(tmp_path / "missing.yaml"))("help").startswith("Commands:")
    (tmp_path / "broken.yaml").write_text("recovered: [unclosed\n")
    assert Messages(str(tmp_path / "broken.yaml"))("recovered", id="A", target="b") == "✅ A b recovered."   # broken file → defaults


def test_messages_reach_alerts_and_ai(tmp_path, monkeypatch):
    mp = tmp_path / "m.yaml"
    mp.write_text('alert: "HEY {target}: {summary}"\nalert_ask_go: "Fix with {action}?"\nai_style: "Talk like a pirate."\n')
    monkeypatch.setitem(CFG, "messages_path", str(mp))
    eng, sent, _ = make_engine(tmp_path, monkeypatch, [P()], ["pihole-dns"])
    eng.tick()
    assert any(s.startswith("HEY pihole: x") and "Fix with restart_service pihole-FTL?" in s for s in sent)
    from overseer import brain
    assert "Talk like a pirate." in brain._sys(brain.SYSTEM) and "Talk like a pirate." in brain._sys(brain.BRIEF_SYSTEM)
    brain.set_lab("a homelab", "")


def test_wizard_back_button(tmp_path, monkeypatch):
    """'back' at the hypervisor menu returns to the lab question; the new answer wins."""
    import sys, builtins, getpass, importlib
    sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
    monkeypatch.setenv("OVERSEER_PLAIN", "1")
    for k in ("PVE_URL", "PVE_TOKEN_ID", "PVE_TOKEN", "PVE_NODE", "OVERSEER_LLM"):
        monkeypatch.delenv(k, raising=False)
    import setup as S
    importlib.reload(S)
    monkeypatch.setattr(S, "http", lambda m, url, *a, **k: (200, {"ok": True, "result": {"username": "b"}}) if "getMe" in url else
                        (200, {"ok": True, "result": [{"update_id": 1, "message": {"chat": {"id": 5, "type": "private"}, "from": {}}}]}) if "timeout=10" in url
                        else (200, {"ok": True, "result": []}))
    monkeypatch.setattr(S, "default_gateway", lambda: None)
    monkeypatch.setattr(S, "dns_servers", lambda: [])
    answers = iter(["", "back",                       # welcome, BACK at the what-to-watch menu
                    "", "4",                          # welcome again, then: just the network
                    "1", "", "", "",                  # telegram + its three info screens
                    "back",                           # BACK at the AI menu -> notifier step again
                    "1", "", "", "",                  # telegram again
                    "1", "n", "n", "n", "", ""] + [""] * 5)
    monkeypatch.setattr(builtins, "input", lambda p="": next(answers))
    monkeypatch.setattr(getpass, "getpass", lambda p="": "1:TOK")
    monkeypatch.setattr(sys, "argv", ["setup.py", "--out-dir", str(tmp_path)])
    S.main()
    cfg = yaml.safe_load(open(tmp_path / "config.yaml"))
    assert cfg["lab_description"] == "a home network" and cfg["hypervisor"]["type"] == "none" and cfg["notifier"]["chat_id"] == "5"



def test_this_server_mode(tmp_path, monkeypatch):
    """Plain Ubuntu server, no VMs: wizard discovers services; collector watches host + services; restarts run locally."""
    import sys, builtins, getpass, importlib, subprocess as sp
    sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
    monkeypatch.setenv("OVERSEER_PLAIN", "1")
    for k in ("PVE_URL", "PVE_TOKEN_ID", "PVE_TOKEN", "PVE_NODE", "OVERSEER_LLM"):
        monkeypatch.delenv(k, raising=False)
    import setup as S
    from overseer import localhost as LH
    importlib.reload(S)
    monkeypatch.setattr(LH, "running_services", lambda: ["cron-ish", "docker", "nginx", "ssh"])
    monkeypatch.setattr(S, "real_disks", lambda: ["/", "/srv"])
    monkeypatch.setattr(S, "http", lambda m, url, *a, **k: (200, {"ok": True, "result": {"username": "b"}}) if "getMe" in url else
                        (200, {"ok": True, "result": [{"update_id": 1, "message": {"chat": {"id": 9, "type": "private"}, "from": {}}}]}) if "timeout=10" in url
                        else (200, {"ok": True, "result": []}))
    monkeypatch.setattr(S, "default_gateway", lambda: "10.0.0.1")
    monkeypatch.setattr(S, "dns_servers", lambda: [])
    answers = iter(["", "1",        # welcome; watch: this server
                    "",             # services checklist: keep pre-ticked (docker, nginx, ssh)
                    "1", "", "", "", "1", "n", "n", "n", "", ""] + [""] * 5)
    monkeypatch.setattr(builtins, "input", lambda p="": next(answers))
    monkeypatch.setattr(getpass, "getpass", lambda p="": "1:TOK")
    monkeypatch.setattr(sys, "argv", ["setup.py", "--out-dir", str(tmp_path)])
    S.main()
    cfg = yaml.safe_load(open(tmp_path / "config.yaml"))
    t = cfg["targets"]
    assert cfg["local"] == {"services": ["docker", "nginx", "ssh"], "disks": ["/", "/srv"]} and "cron-ish" not in t
    assert t["nginx"] == {"kind": "service", "local": True, "actions": ["restart_service:nginx"]}
    assert t["docker"]["floor_tier"] == 3 and t["ssh"]["actions"] == []          # ssh: alert only
    assert cfg["hypervisor"]["type"] == "none" and "running docker, nginx, ssh" in cfg["lab_description"]

    # collector
    monkeypatch.setattr(LH, "_cpu_pct", lambda interval=0.5: 12.5)
    monkeypatch.setattr(LH, "_mem_pct", lambda: 95.0)
    monkeypatch.setattr(LH, "_active", lambda us: {u: "active" if u != "nginx" else "failed" for u in us})
    checks, facts = LH.LocalHost(cfg["local"]).collect()
    by = {c["name"]: c for c in checks}
    assert facts["node"]["cpu_pct"] == 12.5 and not by["local-mem"]["ok"] and by["storage-root"]["ok"] is not None
    assert not by["svc-nginx"]["ok"] and by["svc-nginx"]["target"] == "nginx" and by["svc-docker"]["ok"]

    # local restart goes through sudo, never ssh
    from overseer import actions
    calls = []
    def fake_run(cmd, **kw):
        calls.append(cmd)
        return sp.CompletedProcess(cmd, 0, "active\n", "")
    monkeypatch.setattr(actions.subprocess, "run", fake_run)
    ok, _ = actions.execute("restart_service", "nginx", "nginx", t["nginx"], None, dry_run=False)
    assert ok and calls[0] == ["sudo", "-n", "/usr/bin/systemctl", "restart", "nginx"]


def test_docker_containers(tmp_path, monkeypatch):
    import subprocess as sp, json as _j
    from overseer import localhost as LH, policy as POL
    rows = [{"Names": "web", "Image": "nginx:1.27", "State": "running", "Status": "Up 3 hours (healthy)"},
            {"Names": "db", "Image": "postgres:16", "State": "running", "Status": "Up 3 hours (unhealthy)"},
            {"Names": "old", "Image": "busybox", "State": "exited", "Status": "Exited (0) 2 days ago"}]
    calls = []
    def fake_run(cmd, **kw):
        calls.append(cmd)
        return sp.CompletedProcess(cmd, 0, "\n".join(_j.dumps(r) for r in rows), "")
    monkeypatch.setattr(LH, "docker_bin", lambda: "/usr/bin/docker")
    monkeypatch.setattr(LH.subprocess, "run", fake_run)
    monkeypatch.setattr(LH.os, "geteuid", lambda: 1000)
    cs = LH.list_containers()
    assert calls[0][:3] == ["sudo", "-n", "/usr/bin/docker"] and calls[0][-1] == "{{json .}}"   # matches the sudoers rule exactly
    assert {c["name"]: c["health"] for c in cs} == {"web": "healthy", "db": "unhealthy", "old": ""}
    monkeypatch.setattr(LH, "_cpu_pct", lambda interval=0.5: 1.0)
    checks, facts = LH.LocalHost({"containers": ["web", "db", "gone"], "disks": []}).collect()
    by = {c["name"]: c for c in checks}
    assert by["ctr-web"]["ok"] and not by["ctr-db"]["ok"] and not by["ctr-gone"]["ok"] and "missing" in by["ctr-gone"]["detail"]
    assert set(facts["containers"]) == {"web", "db"}
    # policy: container restart only when whitelisted for that exact container
    T2 = {"web": {"kind": "container", "local": True, "actions": ["restart_container:web"]}}
    assert POL.decide({"target": "web", "tier": 2, "action": "restart_container", "action_arg": "web"}, T2, 0).action == "restart_container"
    assert POL.decide({"target": "web", "tier": 2, "action": "restart_container", "action_arg": "db"}, T2, 0).action == "none"


def test_wizard_docker_step(tmp_path, monkeypatch):
    import sys, builtins, getpass, importlib
    sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
    monkeypatch.setenv("OVERSEER_PLAIN", "1")
    for k in ("PVE_URL", "PVE_TOKEN_ID", "PVE_TOKEN", "PVE_NODE", "OVERSEER_LLM"):
        monkeypatch.delenv(k, raising=False)
    import setup as S
    from overseer import localhost as LH
    importlib.reload(S)
    monkeypatch.setattr(LH, "running_services", lambda: ["docker", "nginx"])
    monkeypatch.setattr(LH, "list_containers", lambda use_sudo=True: [
        {"name": "nginx", "image": "nginx:1.27", "state": "running", "health": "", "status": "Up"},
        {"name": "db", "image": "postgres:16", "state": "running", "health": "", "status": "Up"},
        {"name": "old", "image": "busybox", "state": "exited", "health": "", "status": "Exited"}])
    monkeypatch.setattr(S, "real_disks", lambda: ["/"])
    monkeypatch.setattr(S, "http", lambda m, url, *a, **k: (200, {"ok": True, "result": {"username": "b"}}) if "getMe" in url else
                        (200, {"ok": True, "result": [{"update_id": 1, "message": {"chat": {"id": 9, "type": "private"}, "from": {}}}]}) if "timeout=10" in url
                        else (200, {"ok": True, "result": []}))
    monkeypatch.setattr(S, "default_gateway", lambda: None)
    monkeypatch.setattr(S, "dns_servers", lambda: [])
    answers = iter(["", "1", "", "",        # welcome; this server; services keep; containers keep (running ones)
                    "1", "", "", "", "1", "n", "n", "n", "", ""] + [""] * 5)
    monkeypatch.setattr(builtins, "input", lambda p="": next(answers))
    monkeypatch.setattr(getpass, "getpass", lambda p="": "1:TOK")
    monkeypatch.setattr(sys, "argv", ["setup.py", "--out-dir", str(tmp_path)])
    S.main()
    cfg = yaml.safe_load(open(tmp_path / "config.yaml"))
    t, loc = cfg["targets"], cfg["local"]
    assert loc["containers"] == ["nginx", "db"] and loc["container_targets"] == {"nginx": "nginx-container"}   # name clash with the nginx service
    assert t["nginx-container"]["actions"] == ["restart_container:nginx"] and t["db"]["floor_tier"] == 3
    assert "Docker containers: nginx, db" in cfg["lab_description"]


def test_wizard_discord_autodetect(tmp_path, monkeypatch):
    """Discord pairing: waits for the bot to join, then picks server/channel/owner without typing any IDs."""
    import sys, builtins, getpass, importlib
    sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
    monkeypatch.setenv("OVERSEER_PLAIN", "1")
    for k in ("PVE_URL", "PVE_TOKEN_ID", "PVE_TOKEN", "PVE_NODE", "OVERSEER_LLM"):
        monkeypatch.delenv(k, raising=False)
    import setup as S
    importlib.reload(S)
    state = {"guild_polls": 0, "posted": None}
    G = "111111111111111111"
    def fake_http(method, url, headers=None, body=None, verify=True, timeout=15):
        if url.endswith("/users/@me"):
            return 200, {"id": "999999999999999999", "username": "overseer-bot"}
        if url.endswith("/users/@me/guilds"):
            state["guild_polls"] += 1
            return 200, ([] if state["guild_polls"] == 1 else [{"id": G, "name": "Homelab"}])   # joins after the invite
        if url.endswith(f"/guilds/{G}/channels"):
            return 200, [{"id": "2", "name": "general", "type": 0, "position": 0}, {"id": "3", "name": "voice", "type": 2},
                         {"id": "4", "name": "lab-alerts", "type": 0, "position": 1}]
        if url.endswith(f"/guilds/{G}"):
            return 200, {"owner_id": "555555555555555555"}
        if "/users/555555555555555555" in url:
            return 200, {"username": "pastery"}
        if "/messages" in url:
            state["posted"] = url
            return 200, {}
        return 200, {}
    monkeypatch.setattr(S, "http", fake_http)
    monkeypatch.setattr(S, "default_gateway", lambda: None)
    monkeypatch.setattr(S, "dns_servers", lambda: [])
    answers = iter(["", "4",          # welcome; just the network
                    "2", "",          # Discord; bot instructions
                    "",               # channel menu: accept the suggested #lab-alerts
                    "",               # "Is pastery you?" -> yes (default)
                    "",               # test message sent
                    "1", "n", "n", "n", "", ""] + [""] * 5)
    monkeypatch.setattr(builtins, "input", lambda p="": next(answers))
    monkeypatch.setattr(getpass, "getpass", lambda p="": "tok")
    monkeypatch.setattr(sys, "argv", ["setup.py", "--out-dir", str(tmp_path)])
    S.main()
    n = yaml.safe_load(open(tmp_path / "config.yaml"))["notifier"]
    assert n == {"type": "discord", "token_env": "DISCORD_TOKEN", "guild_id": G, "channel_id": "4", "owner_id": "555555555555555555"}
    assert state["posted"].endswith("/channels/4/messages")



def test_blip_is_debounced(tmp_path, monkeypatch):
    """One failed sweep = no AI, no alert. Two in a row = triage."""
    eng, sent, snap = make_engine(tmp_path, monkeypatch, [P()], ["pihole-dns"])
    eng.cfg["confirm_sweeps"] = 2
    calls = []
    orig = eng.backend.triage
    eng.backend.triage = lambda *a, **k: (calls.append(1), orig(*a, **k))[1]
    eng.tick()
    assert calls == [] and not eng.state.open_incidents()                 # blip: nothing
    snap["checks"] = []
    eng.tick()                                                             # recovered: streak resets
    snap["checks"] = [{"name": "pihole-dns", "target": "pihole", "ok": False, "detail": "timeout"}]
    eng.tick()
    assert calls == []                                                      # 1st failure again, still a blip
    eng.tick()
    assert calls == [1] and len(eng.state.open_incidents()) == 1           # 2nd in a row: confirmed


def test_ai_incident_without_evidence_is_discarded(tmp_path, monkeypatch):
    """The 5:59 PM bug: an all-green review where the model invents 'pfSense is down'."""
    hallucination = P(target="pfsense", checks=[], summary="pfSense appears down", action="start_vm", action_arg="", tier=3)
    eng, sent, snap = make_engine(tmp_path, monkeypatch, [hallucination], [])
    eng.last_review = 0                                                     # hourly review is due
    calls = []
    eng.backend.triage = lambda *a, **k: (calls.append(1), {"incidents": [hallucination]})[1]
    eng.tick()
    assert calls == []                                                      # all green: the AI isn't even asked
    # and even if it runs with a real failure elsewhere, claims about non-failing checks are dropped
    snap["checks"] = [{"name": "pihole-dns", "target": "pihole", "ok": False, "detail": "timeout"}]
    eng.backend.triage = lambda *a, **k: {"incidents": [dict(hallucination, checks=["pfsense-gw-v20"]), P()]}
    eng.last_failing = set(); eng.tick()
    targets = [i["target"] for i in eng.state.open_incidents()]
    assert targets == ["pihole"] and not any("pfSense" in s for s in sent)


def test_critical_target_goes_straight_to_strong_model(tmp_path, monkeypatch):
    """Failing floor_tier>=escalate_at_tier infra: one call on the strong model, not cheap-then-strong."""
    eng, sent, snap = make_engine(tmp_path, monkeypatch, [], [])
    calls = []
    eng.backend.triage = lambda prompt, escalate=False: (calls.append(escalate), {"incidents": []})[1]
    snap["checks"] = [{"name": "pihole-dns", "target": "pihole", "ok": False, "detail": "x"}]
    eng.tick()
    snap["checks"] = [{"name": "pfsense-ping", "target": "pfsense", "ok": False, "detail": "x"}]   # floor_tier 3
    eng.tick()
    assert calls == [False, True]


def test_background_triage_keeps_sweeping_and_uses_fresh_evidence(tmp_path, monkeypatch):
    import threading
    eng, sent, snap = make_engine(tmp_path, monkeypatch, [], [])
    eng.background = True
    snap["checks"] = [{"name": "pihole-dns", "target": "pihole", "ok": False, "detail": "x"}]
    go, started = threading.Event(), threading.Event()
    def slow(prompt, escalate=False):
        started.set(); go.wait(5)
        return {"incidents": [P()]}
    eng.backend.triage = slow
    eng.tick(); started.wait(5)
    snap["checks"] = []
    eng.tick()                              # model still thinking: this sweep must not block
    go.set()
    for _ in range(50):
        if not eng.llm.locked():
            break
        time.sleep(0.05)
    time.sleep(0.05)
    assert not eng.state.open_incidents()   # check recovered meanwhile -> AI incident discarded


def test_busy_llm_defers_new_failures(tmp_path, monkeypatch):
    eng, sent, snap = make_engine(tmp_path, monkeypatch, [P()], ["pihole-dns"])
    eng.llm.acquire()                       # e.g. the /status analyst holds the model
    eng.tick()
    assert not eng.state.open_incidents() and eng.last_failing == set()
    eng.llm.release()
    eng.tick()                              # still new -> triaged now
    assert len(eng.state.open_incidents()) == 1


def test_systemctl_batched(monkeypatch):
    import subprocess as sp
    from overseer import localhost as LH
    calls = []
    monkeypatch.setattr(LH.subprocess, "run", lambda cmd, **k: (calls.append(cmd), sp.CompletedProcess(cmd, 3, "active\nfailed\n", ""))[1])
    assert LH._active(["nginx", "redis"]) == {"nginx": "active", "redis": "failed"}
    assert len(calls) == 1


def test_decision_log_rotates_and_db_prunes(tmp_path):
    from overseer.state import State
    p = str(tmp_path / "d.jsonl")
    open(p, "w").write("x" * 2_000_000)
    brain._append_log(p, {"a": 1}, max_mb=1)
    assert (tmp_path / "d.jsonl.1").exists() and open(p).read() == '{"a": 1}\n'
    st = State(str(tmp_path / "s.db"))
    old = st.create("f", "t", 1, "s", "none", "", "notify", "resolved")
    keep = st.create("g", "t", 1, "s", "none", "", "notify", "open")
    st._q("UPDATE incidents SET updated=0")
    st.prune(90)
    assert st.get(old) is None and st.get(keep)
