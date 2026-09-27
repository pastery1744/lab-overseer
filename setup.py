#!/usr/bin/env python3
"""Lab Overseer setup wizard. Stdlib only, so it runs anywhere (a Proxmox host, a fresh Debian VM, ...).

  python3 setup.py                      # interactive; writes /etc/overseer/config.yaml + secrets.env
  python3 setup.py --out-dir /tmp/ov    # write somewhere else (deploy-proxmox.sh uses this)

Pre-fill (skips questions) with env vars: PVE_URL PVE_NODE PVE_TOKEN_ID PVE_TOKEN, OVERSEER_LLM=ollama|claude
"""
import argparse, json, os, re, ssl, subprocess, sys, time, urllib.error, urllib.parse, urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from overseer.tui import get_ui, Back  # noqa: E402

UI = None


def ok(t):
    UI.info("✓ " + t) if UI.gui else print("✓ " + t)


def warn(t):
    UI.msg("⚠ " + t, "Heads up")


# ---------------- tiny HTTP helper ----------------
_insecure = ssl._create_unverified_context()


def http(method, url, headers=None, body=None, verify=True, timeout=15):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=None if verify else _insecure) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except Exception:
            return e.code, {}


# ---------------- tiny YAML writer (flow style for small leaf dicts) ----------------
def _scalar(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if v is None:
        return '""'
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v)
    return json.dumps(s) if (s == "" or re.search(r"[:#{}\[\],&*!|>'\"%@`]|^\s|\s$|^(true|false|yes|no|null|~|\d[\d.]*)$", s, re.I)) else s


def _flow(v):
    if isinstance(v, dict):
        return "{" + ", ".join(f"{k}: {_flow(x)}" for k, x in v.items()) + "}"
    if isinstance(v, list):
        return "[" + ", ".join(_flow(x) for x in v) + "]"
    return _scalar(v)


def _leafy(v):
    return isinstance(v, (dict, list)) and all(not isinstance(x, (dict, list)) or (isinstance(x, list) and all(not isinstance(y, (dict, list)) for y in x))
                                             for x in (v.values() if isinstance(v, dict) else v)) and len(_flow(v)) < 110


def to_yaml(d, ind=0):
    out, pad = [], "  " * ind
    for k, v in d.items():
        if isinstance(v, dict) and v and not _leafy(v):
            out.append(f"{pad}{k}:")
            out.append(to_yaml(v, ind + 1))
        elif isinstance(v, list) and v and any(isinstance(x, dict) for x in v):
            out.append(f"{pad}{k}:")
            out += [f"{pad}  - {_flow(x)}" for x in v]
        else:
            out.append(f"{pad}{k}: {_flow(v)}")
    return "\n".join(out)


# ---------------- discovery ----------------
CRITICAL_HINT = re.compile(r"pfsense|opnsense|router|firewall|\bfw\b|gateway|\bdc\d*\b|domain|\bad\d*\b|truenas|\bnas\b|vyos|mikrotik", re.I)


def default_gateway():
    try:
        out = subprocess.run(["ip", "route", "show", "default"], capture_output=True, text=True).stdout
        m = re.search(r"default via (\S+)", out)
        return m.group(1) if m else None
    except Exception:
        return None


def dns_servers():
    try:
        return [l.split()[1] for l in open("/etc/resolv.conf") if l.startswith("nameserver") and not l.split()[1].startswith("127.")]
    except Exception:
        return []


def setup_proxmox(sec):
    url, tid, secret = os.environ.get("PVE_URL"), os.environ.get("PVE_TOKEN_ID"), os.environ.get("PVE_TOKEN")
    if not (url and tid and secret):
        UI.msg("The overseer needs a read + power-only Proxmox API token.\n\nOn the Proxmox host run:\n"
               "  pveum role add OverseerRole -privs 'VM.Audit VM.PowerMgmt Sys.Audit Datastore.Audit'\n"
               "  pveum user add overseer@pve\n  pveum aclmod / -user overseer@pve -role OverseerRole\n"
               "  pveum user token add overseer@pve overseer --privsep 0\n\n"
               "(deploy-proxmox.sh does this for you.)", "Proxmox API token")
        gw = default_gateway() or "192.168.1.1"
        url = url or UI.ask("Proxmox address", "https://" + gw.rsplit(".", 1)[0] + ".10:8006")
        tid = tid or UI.ask("Token ID", "overseer@pve!overseer")
        secret = secret or UI.ask("Token secret (the long UUID)", secret=True)
    hdr = {"Authorization": f"PVEAPIToken={tid}={secret}"}
    base = url.rstrip("/") + "/api2/json"
    UI.info("Connecting to Proxmox…")
    code, j = http("GET", base + "/nodes", hdr, verify=False)
    base_cfg = {"type": "proxmox", "url": url, "token_id": tid, "token_secret_env": "PVE_TOKEN", "verify_ssl": False}
    sec["PVE_TOKEN"] = secret
    if code != 200:
        warn(f"Proxmox answered HTTP {code}, so VM discovery is skipped.\nYou can fix the token later with: overseer → Re-run setup.")
        return dict(base_cfg, node=os.environ.get("PVE_NODE") or "pve"), []
    nodes = [n["node"] for n in j["data"]]
    node = os.environ.get("PVE_NODE") or nodes[0]
    if len(nodes) > 1:
        UI.msg(f"Cluster detected: {', '.join(nodes)}.\nThe overseer will watch every node and every VM across the cluster.", "Proxmox cluster")
    _, rj = http("GET", base + "/cluster/resources", hdr, verify=False)
    guests = [{"id": int(g["vmid"]), "name": g.get("name") or str(g["vmid"]), "kind": "vm" if g["type"] == "qemu" else "ct",
               "status": g["status"]}
              for g in rj.get("data", []) if g.get("type") in ("qemu", "lxc") and not g.get("template")
              and not (g.get("name") or "").startswith("overseer")]
    return dict(base_cfg, node=node), sorted(guests, key=lambda g: g["id"])


def setup_esxi(sec):
    UI.msg("Works with a standalone ESXi host or vCenter.\n\nBest practice: make a dedicated user whose role only has\n"
           "Virtual machine > Interaction > Power on / Reset / Guest restart\n(plus read-only everywhere else).", "ESXi")
    host = UI.ask("ESXi or vCenter address (IP or hostname)")
    user = UI.ask("Username", "root")
    pwd = UI.ask(f"Password for {user}", secret=True)
    sec["ESXI_PASSWORD"] = pwd
    cfg = {"type": "esxi", "host": host, "user": user, "password_env": "ESXI_PASSWORD", "verify_ssl": False}
    try:
        os.environ["ESXI_PASSWORD"] = pwd
        from overseer.hypervisors.esxi import ESXi
        UI.info("Connecting to ESXi…")
        return cfg, ESXi(cfg).list_guests()
    except ImportError:
        warn("VM discovery needs pyVmomi, which install.sh sets up. Skipping discovery for now.")
    except Exception as e:
        warn(f"Couldn't connect ({e}).\nSaving anyway — fix it later with: overseer → Re-run setup.")
    return cfg, []


NO_RESTART = {"ssh", "sshd", "networking", "systemd-networkd", "NetworkManager", "ufw", "fail2ban"}   # restarting these can lock you out


def real_disks():
    keep, seen = [], set()
    try:
        for line in open("/proc/mounts"):
            dev, mnt, fs = line.split()[:3]
            if fs in ("ext4", "ext3", "xfs", "btrfs", "zfs", "f2fs") and not mnt.startswith(("/boot", "/snap", "/var/lib/docker")) and dev not in seen:
                seen.add(dev)
                keep.append(mnt)
    except OSError:
        pass
    return sorted(keep) or ["/"]


def setup_local():
    from overseer.localhost import running_services, COMMON
    svcs = running_services()
    targets = {"this-server": {"kind": "host", "floor_tier": 3, "actions": [], "notes": "the machine the overseer runs on"},
               "internet": {"kind": "external", "actions": []}}
    chosen = []
    if svcs:
        chosen = UI.checklist("Which services on this server should it watch?\n(Common apps are pre-ticked. Space = toggle, Enter = done)",
                              [(n, n, n in COMMON) for n in svcs])
    for n in chosen:
        if n in NO_RESTART:
            targets[n] = {"kind": "service", "actions": [], "notes": "alert only — restarting it could lock you out"}
        else:
            targets[n] = {"kind": "service", "local": True, "actions": [f"restart_service:{n}"]}
            if n in ("docker", "containerd", "podman", "mysql", "mariadb", "postgresql"):
                targets[n]["floor_tier"] = 3          # restarting these takes other things down with them
    local = {"services": chosen, "disks": real_disks()}
    return local, targets


def auto_description(st):
    """What the AI is told about the lab — built from the answers instead of asking."""
    hv = st.get("hv_type")
    t = st.get("targets") or {}
    crit = [k for k, v in t.items() if v.get("floor_tier", 0) >= 3 and k not in ("host", "this-server")]
    if hv == "this":
        svcs = (st.get("local") or {}).get("services") or []
        d = f"a single Linux server ({os.uname().nodename})" + (f" running {', '.join(svcs[:8])}" if svcs else "")
    elif hv in ("proxmox", "esxi"):
        n = len([v for v in t.values() if v.get("vmid") is not None or v.get("vmname")])
        d = f"a {'Proxmox' if hv == 'proxmox' else 'VMware ESXi'} homelab with {n} watched VMs/containers"
    else:
        d = "a home network"
    if crit:
        d += f"; critical: {', '.join(crit[:6])}"
    if st.get("switch"):
        d += "; managed switch monitored over SNMP"
    return d


def build_targets(hv_type, guests):
    targets = {"host": {"kind": "host", "floor_tier": 3, "actions": [], "notes": "the hypervisor itself"},
               "internet": {"kind": "external", "actions": []}}
    if not guests:
        return targets
    items = [(str(g["id"]), f"{g['name']:<24} {g['kind']}  {g['status']}", g["status"] == "running") for g in guests]
    watch = set(UI.checklist("Which VMs/containers should it watch?\n(Space = toggle, Enter = done)", items))
    chosen = [g for g in guests if str(g["id"]) in watch]
    if not chosen:
        return targets
    crit_items = [(str(g["id"]), g["name"], bool(CRITICAL_HINT.search(g["name"]))) for g in chosen]
    crit = set(UI.checklist("Which ones are CRITICAL? (router, firewall, domain controllers, NAS…)\n"
                            "Critical machines always need your approval and are never rebooted casually.", crit_items))
    for g in chosen:
        key = re.sub(r"[^a-z0-9]+", "-", g["name"].lower()).strip("-") or f"guest-{g['id']}"
        start = "start_ct" if g["kind"] == "ct" else "start_vm"
        t = {"kind": g["kind"], "actions": [start]}
        if hv_type == "esxi":
            t["vmname"] = g["name"]
        else:
            t["vmid"] = g["id"]
        if g["status"] != "running":
            t["expect_running"] = False
        if str(g["id"]) in crit:
            t["floor_tier"] = 3
        else:
            t["actions"].append("reboot_ct" if g["kind"] == "ct" else "reboot_vm")
        targets[key] = t
    return targets


def setup_telegram(sec):
    UI.msg("Let's make your Telegram bot (takes 1 minute):\n\n"
           "1. Open Telegram and search for  @BotFather\n"
           "2. Send it:  /newbot\n"
           "3. Give it any name, then a username ending in 'bot'\n"
           "4. BotFather replies with a token like  123456789:AAH...\n\n"
           "Copy that token — you'll paste it next.", "Telegram bot")
    while True:
        tok = UI.ask("Paste the bot token", secret=True).strip()
        code, j = http("GET", f"https://api.telegram.org/bot{tok}/getMe")
        if code == 200 and j.get("ok"):
            break
        warn("Telegram didn't accept that token.\nMost common cause: part of it got cut off when pasting. Try again.")
    bot = j["result"]["username"]
    sec["TG_TOKEN"] = tok
    http("GET", f"https://api.telegram.org/bot{tok}/deleteWebhook")
    _, j = http("GET", f"https://api.telegram.org/bot{tok}/getUpdates?timeout=0")
    offset = (max(u["update_id"] for u in j["result"]) + 1) if j.get("result") else 0
    UI.msg(f"Now open Telegram, find  @{bot}  and send it:  /start\n\nPress OK, then send the message.", "Pair your phone")
    chat = who = None
    t_end = time.time() + 180
    while time.time() < t_end and not chat:
        UI.info(f"Waiting for your /start to @{bot}…  ({int(t_end - time.time())}s left)")
        _, j = http("GET", f"https://api.telegram.org/bot{tok}/getUpdates?timeout=10&offset={offset}", timeout=20)
        for u in j.get("result", []):
            offset = u["update_id"] + 1
            m = u.get("message") or {}
            if m.get("chat", {}).get("type") == "private":
                chat = str(m["chat"]["id"])
                who = m.get("from", {}).get("username") or m.get("from", {}).get("first_name")
    http("GET", f"https://api.telegram.org/bot{tok}/getUpdates?offset={offset}&timeout=0")
    if not chat:
        warn("Didn't see a message. No problem: after install, message the bot and it will reply with your chat ID.\n"
             "Then: overseer → Re-run setup.")
        return {"type": "telegram", "token_env": "TG_TOKEN", "chat_id": ""}
    http("POST", f"https://api.telegram.org/bot{tok}/sendMessage", body={"chat_id": chat, "text": "👋 Lab Overseer paired. You'll get alerts here."})
    UI.msg(f"Paired with {who}. Check Telegram — you should have a hello message.", "Telegram ✓")
    return {"type": "telegram", "token_env": "TG_TOKEN", "chat_id": chat}


def setup_discord(sec):
    UI.msg("Let's make your Discord bot:\n\n"
           "1. Go to  discord.com/developers/applications\n"
           "2. New Application → any name → Create\n"
           "3. Left menu: Bot → Reset Token → Copy\n\n"
           "Paste that token next.", "Discord bot")
    hdr = lambda t: {"Authorization": f"Bot {t}", "User-Agent": "LabOverseer (setup, 1)"}
    while True:
        tok = UI.ask("Paste the bot token", secret=True).strip()
        code, j = http("GET", "https://discord.com/api/v10/users/@me", hdr(tok))
        if code == 200:
            break
        warn("Discord didn't accept that token. Try copying it again (Reset Token makes a new one).")
    sec["DISCORD_TOKEN"] = tok
    invite = f"https://discord.com/oauth2/authorize?client_id={j['id']}&scope=bot+applications.commands&permissions=3072"
    UI.msg(f"Invite the bot to your server — open this link in a browser:\n\n{invite}\n\n"
           "Then in Discord: Settings → Advanced → turn ON Developer Mode.\n"
           "That lets you right-click things and 'Copy ID'.", "Invite the bot")
    ids = r"\d{15,21}"
    def get_id(q):
        while True:
            v = UI.ask(q).strip()
            if re.fullmatch(ids, v):
                return v
            warn("That doesn't look like a Discord ID (a long number). Right-click → Copy ID.")
    guild = get_id("Server ID  (right-click your server icon → Copy Server ID)")
    chan = get_id("Channel ID for alerts  (right-click the channel → Copy Channel ID)")
    owner = get_id("YOUR user ID  (right-click your own name → Copy User ID)")
    code, _ = http("POST", f"https://discord.com/api/v10/channels/{chan}/messages", hdr(tok), {"content": "👋 Lab Overseer paired. Alerts will land here."})
    if code in (200, 201):
        UI.msg("Test message sent — check the channel.", "Discord ✓")
    else:
        warn(f"Couldn't post to that channel yet (HTTP {code}).\nMake sure you opened the invite link and the bot can see that channel.")
    return {"type": "discord", "token_env": "DISCORD_TOKEN", "guild_id": guild, "channel_id": chan, "owner_id": owner}


def ram_gb():
    try:
        return int(open("/proc/meminfo").read().split()[1]) / 1048576
    except Exception:
        return 0


def setup_llm(sec):
    llm = {"backend": "ollama",
           "ollama": {"url": "http://127.0.0.1:11434", "model": "qwen2.5:7b-instruct", "brief_model": "llama3.2:3b", "timeout": 300},
           "claude": {"api_key_env": "ANTHROPIC_API_KEY", "triage_model": "claude-haiku-4-5-20251001",
                      "escalate_model": "claude-sonnet-5", "escalate_at_tier": 2}}
    choice = os.environ.get("OVERSEER_LLM") or UI.menu(
        "Which AI should do the thinking?",
        [("ollama", "Local (Ollama) — free & private, needs ~10 GB RAM, slower"),
         ("claude", "Claude API — smarter & fast, a few $/month, needs a key"),
         ("remote", "An Ollama server I already have elsewhere")], "ollama")
    if choice == "remote":
        llm["ollama"]["url"] = UI.ask("Ollama server address", "http://192.168.1.50:11434")
    elif choice == "claude":
        llm["backend"] = "claude"
        UI.msg("Get an API key at  console.anthropic.com  → API Keys → Create Key.", "Claude API key")
        while True:
            key = UI.ask("Paste the API key", secret=True).strip()
            code, _ = http("POST", "https://api.anthropic.com/v1/messages", {"x-api-key": key, "anthropic-version": "2023-06-01"},
                           {"model": llm["claude"]["triage_model"], "max_tokens": 5, "messages": [{"role": "user", "content": "hi"}]})
            if code == 200 or not UI.yes(f"The key didn't work (HTTP {code}). Try a different key?"):
                break
        sec["ANTHROPIC_API_KEY"] = key
    return llm


def build_checks(targets):
    checks = []
    gw = default_gateway()
    router = next((k for k, t in targets.items() if t.get("floor_tier") == 3 and CRITICAL_HINT.search(k)), "internet")
    if gw:
        checks.append({"name": "gateway", "type": "ping", "host": gw, "target": router})
    checks += [{"name": "wan-1111", "type": "ping", "host": "1.1.1.1", "target": "internet"},
               {"name": "wan-9999", "type": "ping", "host": "9.9.9.9", "target": "internet"}]
    for i, d in enumerate(dns_servers()[:2]):
        checks.append({"name": f"dns-{i + 1}", "type": "dns", "server": d, "query": "cloudflare.com", "target": "internet"})
    found = ", ".join(c.get("host") or c.get("server") for c in checks)
    if UI.yes(f"Already watching: {found}\n\nAdd websites or other services to watch too?", False):
        urls = UI.ask("Website addresses, comma separated (e.g. https://mysite.com, http://192.168.1.80)")
        for u in filter(None, (x.strip() for x in urls.split(","))):
            name = re.sub(r"[^a-z0-9]+", "-", urllib.parse.urlparse(u if "://" in u else "http://" + u).netloc.lower()).strip("-")
            checks.append({"name": f"http-{name}", "type": "http", "url": u if "://" in u else "http://" + u, "target": "internet"})
        ports = UI.ask("Other services as address:port, comma separated (e.g. 192.168.1.30:389) — blank to skip")
        for hp in filter(None, (x.strip() for x in ports.split(","))):
            host, _, port = hp.rpartition(":")
            if port.isdigit():
                checks.append({"name": f"tcp-{host}-{port}", "type": "tcp", "host": host, "port": int(port), "target": "internet"})
    return checks


def setup_switch():
    if not UI.yes("Do you have a managed switch with SNMP you'd like it to watch?\n(Skip this if you're not sure.)", False):
        return {}
    sw = {"host": UI.ask("Switch IP address"), "community": UI.ask("SNMP read-only community string"), "watch_ports": {}, "ignore_ports": []}
    for p in filter(None, (x.strip() for x in UI.ask("Important ports, comma separated (e.g. Gi1/0/1,Po1)").split(","))):
        sw["watch_ports"][p] = {"role": "important port"}
    return sw


def write_files(out_dir, cfg, sec):
    os.makedirs(out_dir, exist_ok=True)
    cpath, spath = os.path.join(out_dir, "config.yaml"), os.path.join(out_dir, "secrets.env")
    if os.path.exists(cpath):
        os.rename(cpath, cpath + f".bak-{int(time.time())}")
    with open(cpath, "w") as f:
        f.write("# Written by setup.py — change settings with the `overseer` menu, or edit then: systemctl restart overseer\n"
                + to_yaml(cfg) + "\n")
    old = {}
    if os.path.exists(spath):
        old = dict(l.strip().split("=", 1) for l in open(spath) if "=" in l and not l.startswith("#"))
    old.update(sec)
    fd = os.open(spath, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("# Lab Overseer secrets — keep private\n" + "".join(f"{k}={v}\n" for k, v in old.items()))
    return cpath, spath


def main():
    global UI
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", default="/etc/overseer")
    ap.add_argument("--hypervisor", choices=["proxmox", "esxi", "none"])
    a = ap.parse_args()
    UI = get_ui()
    sec, st = {}, {}

    # Each step fills `st`. Pressing Back anywhere inside a step returns to the previous step.
    def s_welcome():
        UI.msg("Welcome! This takes about 3 minutes.\n\nArrow keys move, Space ticks boxes, Enter confirms.\n"
               "Back goes to the previous step. Esc pauses.\nNothing is saved until the very end.", "Lab Overseer setup")

    def s_hypervisor():
        hv_type = a.hypervisor or UI.menu("What should it watch?", [
            ("this", "This server itself — no VMs (Ubuntu/Debian server)"),
            ("proxmox", "Proxmox VE — VMs & containers"),
            ("esxi", "VMware ESXi / vCenter — VMs"),
            ("none", "Just the network (pings, websites, services)")],
            st.get("hv_type", "proxmox" if os.path.exists("/etc/pve") else "this"))
        hv, guests, local = {"type": "none"}, [], None
        if hv_type == "proxmox":
            hv, guests = setup_proxmox(sec)
        elif hv_type == "esxi":
            hv, guests = setup_esxi(sec)
        if hv_type == "this":
            local, targets = setup_local()
        else:
            targets = build_targets(hv_type, guests)
        st.update(hv_type=hv_type, hv=hv, local=local, targets=targets)

    def s_notifier():
        kind = UI.menu("Where should alerts go?", [("telegram", "Telegram (easiest)"), ("discord", "Discord")], st.get("kind", "telegram"))
        st.update(kind=kind, notifier=setup_telegram(sec) if kind == "telegram" else setup_discord(sec))

    def s_llm():
        st["llm"] = setup_llm(sec)

    def s_checks():
        st["checks"] = build_checks(st["targets"])

    def s_switch():
        st["switch"] = setup_switch()

    def s_heartbeat():
        st["hb"] = ""
        if UI.yes("Optional: get warned if the overseer itself goes offline?\n\n"
                  "Make a free check at healthchecks.io and paste its ping URL.", False):
            st["hb"] = UI.ask("healthchecks.io ping URL", st.get("hb", ""))

    def s_review():
        n_watch = len([t for t in st["targets"].values() if t.get("vmid") is not None or t.get("vmname")])
        ai = {"ollama": "local Ollama", "claude": "Claude API"}[st["llm"]["backend"]]
        watching = {"this": f"this server + {len((st['local'] or {}).get('services') or [])} services",
                    "proxmox": f"Proxmox — {n_watch} VMs/containers", "esxi": f"ESXi — {n_watch} VMs",
                    "none": "the network only"}[st["hv_type"]]
        if not UI.yes(f"Ready to save:\n\n  • Watching: {watching}\n"
                      f"  • Alerts: {st['kind'].capitalize()}\n  • AI: {ai}\n  • Network checks: {len(st['checks'])}\n"
                      f"  • Switch: {'yes' if st['switch'] else 'no'}   • Heartbeat: {'yes' if st['hb'] else 'no'}\n\n"
                      "Save this? (No = go back and change something)"):
            raise Back

    steps = [s_welcome, s_hypervisor, s_notifier, s_llm, s_checks, s_switch, s_heartbeat, s_review]
    i = 0
    while i < len(steps):
        try:
            steps[i]()
            i += 1
        except Back:
            if i == 0:
                if UI.yes("Quit setup? Nothing has been saved.", False):
                    raise KeyboardInterrupt
            else:
                i -= 1

    cfg = {"lab_description": auto_description(st), "dry_run": True, "interval_seconds": 60, "review_interval_minutes": 60,
           "max_actions_per_hour": 2, "db_path": "/var/lib/overseer/overseer.db", "decision_log": "/var/lib/overseer/decisions.jsonl",
           "heartbeat_url": st["hb"], "llm": st["llm"], "notifier": st["notifier"], "hypervisor": st["hv"], "switch": st["switch"],
           "targets": st["targets"], "checks": st["checks"]}
    if st.get("local"):
        cfg["local"] = st["local"]
    write_files(a.out_dir, cfg, sec)
    llm = st["llm"]
    json.dump({"llm": llm["backend"], "local_ollama": llm["backend"] == "ollama" and "127.0.0.1" in llm["ollama"]["url"],
               "notifier": st["kind"], "hypervisor": st["hv_type"]}, open(os.path.join(a.out_dir, ".setup-summary.json"), "w"))
    UI.msg("All set!\n\nIt starts in SAFE mode: it alerts you, and tapping Go only simulates the fix.\n"
           "Send /auto when you trust it.\n\nChange anything later by typing:  overseer", "Setup complete")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nSetup cancelled — nothing was saved.")
        sys.exit(130)
