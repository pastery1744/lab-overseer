#!/usr/bin/env bash
# Let the overseer restart specific systemd services on another Linux box, and nothing else.
# Run as root on the overseer machine:
#
#   sudo /opt/overseer/tools/add-service-target.sh <admin-user>@<host> <service>[,<service>...] [target-name]
#   e.g. sudo /opt/overseer/tools/add-service-target.sh pi@192.168.1.53 pihole-FTL pihole
#
# You type the admin user's password (and sudo password) once. The script:
#   1. creates an 'overseer' user on the target with the overseer's SSH key
#   2. adds a sudoers rule allowing ONLY `systemctl restart <those services>`
#   3. verifies it, then adds/updates the target in /etc/overseer/config.yaml (a .bak copy is kept)
set -euo pipefail
[ $# -ge 2 ] || { sed -n '3,8p' "$0"; exit 1; }
DEST=$1; SVCS=$2; HOST=${DEST#*@}; NAME=${3:-$(echo "$HOST" | tr '.' '-')}
PUB=$(cat /etc/overseer/id_ed25519.pub)
RULES=""; for s in ${SVCS//,/ }; do
  [[ "$s" =~ ^[A-Za-z0-9@._-]+$ ]] || { echo "bad service name: $s"; exit 1; }
  RULES+="overseer ALL=(root) NOPASSWD: /usr/bin/systemctl restart $s\n"; done
echo "Connecting to $DEST (you'll be asked for its password, then sudo's)..."
ssh -t -o StrictHostKeyChecking=accept-new "$DEST" "sudo bash -c '
  id overseer >/dev/null 2>&1 || useradd -m -s /bin/bash overseer
  mkdir -p /home/overseer/.ssh && echo \"$PUB\" > /home/overseer/.ssh/authorized_keys
  chown -R overseer:overseer /home/overseer/.ssh && chmod 700 /home/overseer/.ssh && chmod 600 /home/overseer/.ssh/authorized_keys
  printf \"$RULES\" > /etc/sudoers.d/overseer && chmod 440 /etc/sudoers.d/overseer && visudo -cf /etc/sudoers.d/overseer'"
sudo -u overseer ssh -i /etc/overseer/id_ed25519 -o BatchMode=yes -o StrictHostKeyChecking=accept-new "overseer@$HOST" 'sudo -n -l | grep restart'
cp /etc/overseer/config.yaml "/etc/overseer/config.yaml.bak-$(date +%s)"
/opt/overseer/venv/bin/python - "$NAME" "$HOST" "$SVCS" <<'PY'
import sys, yaml
name, host, svcs = sys.argv[1:]
p = "/etc/overseer/config.yaml"; cfg = yaml.safe_load(open(p))
t = cfg.setdefault("targets", {}).setdefault(name, {"kind": "service", "actions": []})
acts = [a for a in t.get("actions", []) if not a.startswith("restart_service:")]
t["actions"] = [f"restart_service:{s}" for s in svcs.split(",")] + acts
t["ssh"] = {"host": host, "user": "overseer"}
yaml.safe_dump(cfg, open(p, "w"), sort_keys=False, default_flow_style=None, width=140)
print(f"target '{name}' can now restart: {svcs}")
PY
systemctl restart overseer && echo "✓ overseer restarted"
