"""The only things the overseer can physically do. No stop, no delete, no config edits. Ever."""
import logging, shlex, subprocess, re

log = logging.getLogger("actions")
SERVICE_RE = re.compile(r"^[a-zA-Z0-9@._-]+$")
CONTAINER_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")
POWER = {"start_vm": "start", "reboot_vm": "reboot", "start_ct": "start", "reboot_ct": "reboot"}


def execute(action, arg, target_name, target, hv, dry_run):
    if dry_run:
        return True, f"WATCH mode: would {action} {arg} on {target_name}".replace("  ", " ")
    try:
        if action in POWER:
            if hv is None:
                return False, "no hypervisor configured"
            return True, hv.power(POWER[action], target)
        if action == "restart_container":
            if not CONTAINER_RE.match(arg):
                return False, "bad container name"
            import shutil
            d = shutil.which("docker") or "/usr/bin/docker"
            r = subprocess.run(["sudo", "-n", d, "restart", arg], capture_output=True, text=True, timeout=120)
            return r.returncode == 0, (r.stdout + r.stderr).strip()[-200:] or "restarted"
        if action == "restart_service":
            if not SERVICE_RE.match(arg):
                return False, "bad service name"
            if target.get("local"):          # a service on this very machine (sudoers allows only listed units)
                r = subprocess.run(["sudo", "-n", "/usr/bin/systemctl", "restart", arg], capture_output=True, text=True, timeout=60)
                ok = r.returncode == 0 and subprocess.run(["systemctl", "is-active", arg], capture_output=True, text=True).stdout.strip() == "active"
                return ok, (r.stdout + r.stderr).strip()[-200:] or ("restarted, active" if ok else "restarted but not active")
            ssh = target["ssh"]
            cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "-i", ssh.get("key", "/etc/overseer/id_ed25519"),
                   f"{ssh['user']}@{ssh['host']}", f"sudo systemctl restart {shlex.quote(arg)} && systemctl is-active {shlex.quote(arg)}"]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            return r.returncode == 0, (r.stdout + r.stderr).strip()[-200:]
        return False, f"unknown action {action}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"[:200]
