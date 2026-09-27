#!/opt/overseer/venv/bin/python
"""Let the overseer restart ONLY the local services named in config (targets with local: true). Run as root."""
import re, shutil, subprocess, sys, yaml

CFG, OUT = "/etc/overseer/config.yaml", "/etc/sudoers.d/overseer-local"
cfg = yaml.safe_load(open(CFG)) or {}
units = sorted({a.split(":", 1)[1] for t in (cfg.get("targets") or {}).values() if t.get("local")
                for a in t.get("actions", []) if a.startswith("restart_service:")})
units = [u for u in units if re.fullmatch(r"[A-Za-z0-9@._-]+", u)]
ctrs = sorted({a.split(":", 1)[1] for t in (cfg.get("targets") or {}).values() if t.get("local")
               for a in t.get("actions", []) if a.startswith("restart_container:")})
ctrs = [c for c in ctrs if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", c)]
docker = shutil.which("docker") or "/usr/bin/docker"
body = "# Managed by Lab Overseer (tools/sync-sudoers.py) — regenerated on install/setup\n" + \
       "".join(f"overseer ALL=(root) NOPASSWD: /usr/bin/systemctl restart {u}\n" for u in units)
if (cfg.get("local") or {}).get("containers"):
    # read-only listing (exact args) + restart of the chosen containers only — never the docker group (that's root)
    body += f'overseer ALL=(root) NOPASSWD: {docker} ps -a --no-trunc --format {{{{json .}}}}\n'
    body += "".join(f"overseer ALL=(root) NOPASSWD: {docker} restart {c}\n" for c in ctrs)
tmp = OUT + ".new"
open(tmp, "w").write(body)
if subprocess.run(["visudo", "-cf", tmp], capture_output=True).returncode != 0:
    print("sudoers check failed; leaving existing rules untouched", file=sys.stderr)
    sys.exit(1)
subprocess.run(["chmod", "440", tmp], check=True)
subprocess.run(["mv", tmp, OUT], check=True)
print(f"local restart rules: services={', '.join(units) or 'none'} containers={', '.join(ctrs) or 'none'}")
