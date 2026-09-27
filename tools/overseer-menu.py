#!/opt/overseer/venv/bin/python
"""`overseer` — friendly control panel over SSH. Run as root: sudo overseer"""
import os, sqlite3, subprocess, sys, json

APP = "/opt/overseer"
CFG = "/etc/overseer/config.yaml"
SEC = "/etc/overseer/secrets.env"
sys.path.insert(0, APP)
from overseer.tui import get_ui  # noqa: E402

UI = get_ui()


def sh(cmd, **kw):
    return subprocess.run(cmd, shell=True, text=True, capture_output=True, **kw)


def load_cfg():
    import yaml
    return yaml.safe_load(open(CFG))


def secrets_env():
    env = dict(os.environ)
    if os.path.exists(SEC):
        env.update(dict(l.strip().split("=", 1) for l in open(SEC) if "=" in l and not l.startswith("#")))
    return env


def mode(cfg):
    try:
        db = sqlite3.connect(cfg["db_path"])
        r = db.execute("SELECT v FROM meta WHERE k='dry_run'").fetchone()
        return "WATCH" if (r is None and cfg.get("dry_run", True)) or (r and r[0] == "1") else "AUTO"
    except Exception:
        return "WATCH" if cfg.get("dry_run", True) else "AUTO"


def set_mode(cfg, watch):
    db = sqlite3.connect(cfg["db_path"])
    db.execute("CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)")
    db.execute("INSERT OR REPLACE INTO meta VALUES('dry_run', ?)", ("1" if watch else "0",))
    db.commit()
    sh("chown overseer " + cfg["db_path"])


def status(cfg):
    UI.info("Running a health sweep… (a few seconds)")
    r = subprocess.run([f"{APP}/venv/bin/python", "-m", "overseer.main", "-c", CFG, "--once"], cwd=APP,
                       env=secrets_env(), text=True, capture_output=True, timeout=120)
    svc = sh("systemctl is-active overseer").stdout.strip()
    oll = sh("ollama ps 2>/dev/null").stdout.strip()
    lines = [f"Service: {svc}    Mode: {mode(cfg)}", ""]
    try:
        s = json.loads(r.stdout)
        cs = s["checks"]
        lines.append(f"{sum(c['ok'] for c in cs)}/{len(cs)} checks passing\n")
        for c in sorted(cs, key=lambda c: c["ok"]):
            lines.append(f"  {'OK  ' if c['ok'] else 'FAIL'}  {c['name']:<26} {c['detail'][:70]}")
    except Exception:
        lines.append("Couldn't run a sweep:\n" + (r.stderr or r.stdout)[-1500:])
    if oll:
        lines += ["", "AI models loaded:", oll]
    UI.text("Lab status", "\n".join(lines))


def test_alert(cfg):
    code = f"""
import sys; sys.path.insert(0, {APP!r})
import yaml, time
from overseer.state import State
from overseer.notifiers import make_notifier
cfg = yaml.safe_load(open({CFG!r}))
n = make_notifier(cfg, State('/tmp/overseer-test.db'))
if cfg.get('notifier', {{}}).get('type') == 'discord':
    n.start(lambda *a: None)
print('sent' if n.send('🧪 Test alert from the overseer menu. If you see this, alerts work.') else 'failed')
"""
    UI.info("Sending a test alert…")
    r = subprocess.run([f"{APP}/venv/bin/python", "-c", code], text=True, capture_output=True, env=secrets_env(), timeout=60)
    ok = "sent" in r.stdout
    UI.msg("✓ Sent — check your phone." if ok else "✗ Couldn't send. Details:\n\n" + (r.stderr or r.stdout)[-800:], "Test alert")


def add_service():
    UI.msg("This lets the overseer restart specific services (like nginx or pihole-FTL) on another Linux machine.\n\n"
           "You'll need an admin login for that machine. The overseer gets a user there that can ONLY restart the services you list.",
           "Add a service restart")
    dest = UI.ask("Admin login for that machine, as user@address (e.g. pi@192.168.1.53)")
    svcs = UI.ask("Service name(s), comma separated (e.g. pihole-FTL)")
    name = UI.ask("Short name for it in alerts (e.g. pihole)", dest.split("@")[-1].replace(".", "-"))
    if not (dest and svcs):
        return
    os.system("clear")
    print("You'll be asked for that machine's password (and possibly its sudo password).\n")
    rc = os.system(f"{APP}/tools/add-service-target.sh {dest!r} {svcs!r} {name!r}")
    input("\n[Enter] to return to the menu")
    UI.msg("✓ Done." if rc == 0 else "Something went wrong — see the messages above.", "Add a service restart")


def targets_view(cfg):
    lines = []
    for k, t in (cfg.get("targets") or {}).items():
        crit = " (critical)" if t.get("floor_tier", 0) >= 3 else ""
        lines.append(f"{k}{crit}\n    can: {', '.join(t.get('actions') or []) or 'nothing (alerts only)'}")
    UI.text("What it watches & what it may do", "\n".join(lines) or "No targets yet.")


def main():
    if os.geteuid() != 0:
        os.execvp("sudo", ["sudo", sys.executable, *sys.argv])
    if not os.path.exists(CFG):
        if UI.yes("The overseer isn't set up yet. Run setup now?"):
            os.system(f"{APP}/venv/bin/python {APP}/setup.py && systemctl restart overseer")
        return
    while True:
        cfg = load_cfg()
        m = mode(cfg)
        choice = UI.menu(f"Mode: {m}    ({'alerts only, Go simulates' if m == 'WATCH' else 'Go really runs fixes'})\n\nWhat would you like to do?", [
            ("status", "Check status now"),
            ("mode", f"Switch to {'AUTO (armed)' if m == 'WATCH' else 'WATCH (safe)'} mode"),
            ("test", "Send a test alert to my phone"),
            ("targets", "Show what it watches"),
            ("service", "Let it restart a service on another machine"),
            ("logs", "View recent logs"),
            ("restart", "Restart the overseer"),
            ("wording", "Change what the bot says (messages & AI tone)"),
            ("setup", "Re-run setup (change chat app, AI, VMs…)"),
            ("edit", "Edit config file (advanced)"),
            ("quit", "Quit")], "status")
        if choice == "quit":
            break
        elif choice == "status":
            status(cfg)
        elif choice == "mode":
            to_watch = m == "AUTO"
            if to_watch or UI.yes("Switch to AUTO?\n\nFixes you approve with Go will REALLY run (start/reboot VMs, restart services).\n"
                                  "You still approve every single one.", False):
                set_mode(cfg, to_watch)
                UI.msg(f"Now in {'WATCH' if to_watch else 'AUTO'} mode.", "Mode")
        elif choice == "test":
            test_alert(cfg)
        elif choice == "targets":
            targets_view(cfg)
        elif choice == "service":
            add_service()
        elif choice == "logs":
            UI.text("Recent logs", sh("journalctl -u overseer -n 150 --no-pager -o short-iso | sed -E 's/bot[0-9]+:[A-Za-z0-9_-]+/bot<TOKEN>/g'").stdout)
        elif choice == "restart":
            sh("systemctl restart overseer")
            UI.msg(f"Restarted — service is {sh('systemctl is-active overseer').stdout.strip()}.", "Restart")
        elif choice == "setup":
            if UI.yes("Re-run the full setup? Your current config is backed up first."):
                os.system(f"{APP}/venv/bin/python {APP}/setup.py && chown overseer /etc/overseer/config.yaml /etc/overseer/secrets.env && systemctl restart overseer")
        elif choice == "wording":
            mp = "/etc/overseer/messages.yaml"
            if not os.path.exists(mp):
                sh(f"cp {APP}/messages.example.yaml {mp} && chown overseer {mp}")
            UI.msg("You'll see every message the bot sends.\n\nTo change one: delete the '# ' at the start of its line, edit the text, "
                   "then save (Ctrl-O, Enter) and exit (Ctrl-X).\n\nWords in {curly braces} are filled in automatically.", "Change wording")
            os.system(f"${{EDITOR:-nano}} {mp}")
            r = subprocess.run([f"{APP}/venv/bin/python", "-c", f"import sys;sys.path.insert(0,{APP!r});import yaml;"
                                f"yaml.safe_load(open({mp!r}));from overseer.messages import Messages;Messages({mp!r});print('ok')"],
                               text=True, capture_output=True)
            if "ok" not in r.stdout:
                UI.msg("That file has a typo, so the bot will ignore it and use the defaults.\n\n" + r.stderr[-400:], "Wording")
            elif UI.yes("Restart the overseer so the new wording takes effect?"):
                sh("systemctl restart overseer")
        elif choice == "edit":
            os.system(f"${{EDITOR:-nano}} {CFG}")
            if UI.yes("Restart the overseer to apply your changes?"):
                sh("systemctl restart overseer")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
