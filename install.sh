#!/usr/bin/env bash
# Lab Overseer installer — run as root on any Debian 12+/Ubuntu 22.04+ box (VM, LXC, bare metal).
#
#   sudo ./install.sh                 # install + run the setup wizard + start
#   sudo ./install.sh --no-ollama     # using Claude API or a remote Ollama
#   sudo ./install.sh --no-wizard     # config already in /etc/overseer (deploy-proxmox.sh uses this)
#
# Safe to re-run: upgrades code in place, keeps /etc/overseer config + secrets. Deletes nothing.
set -euo pipefail
export LC_ALL=C DEBIAN_FRONTEND=noninteractive
SRC="$(cd "$(dirname "$0")" && pwd)"
OLLAMA=auto; WIZARD=1
for a in "$@"; do case "$a" in
  --no-ollama) OLLAMA=0 ;; --ollama) OLLAMA=1 ;; --no-wizard) WIZARD=0 ;;
  -h|--help) sed -n '2,9p' "$0"; exit 0 ;; *) echo "unknown option $a"; exit 1 ;; esac; done
[ "$(id -u)" = 0 ] || { echo "run as root (sudo)"; exit 1; }
say(){ echo -e "\n\033[1;36m== $*\033[0m"; }

say "System packages"
apt-get update -qq
apt-get install -y -qq python3 python3-venv iputils-ping iproute2 snmp openssh-client curl ca-certificates zstd whiptail nano >/dev/null
setcap cap_net_raw+ep "$(readlink -f "$(command -v ping)")" 2>/dev/null || true   # unprivileged containers

say "App → /opt/overseer"
id overseer &>/dev/null || useradd -r -m -d /opt/overseer -s /usr/sbin/nologin overseer
mkdir -p /opt/overseer /etc/overseer /var/lib/overseer
if [ "$SRC" != /opt/overseer ]; then
  tar -C "$SRC" --exclude=venv --exclude=__pycache__ --exclude=.git -cf - . | tar -C /opt/overseer -xf -
fi
cd /opt/overseer
[ -d venv ] || python3 -m venv venv
venv/bin/pip install -q --upgrade pip
venv/bin/pip install -q -r requirements.txt
chown -R overseer /opt/overseer /var/lib/overseer
chmod +x /opt/overseer/tools/*.sh /opt/overseer/tools/*.py /opt/overseer/*.sh 2>/dev/null || true
ln -sf /opt/overseer/tools/overseer-menu.py /usr/local/bin/overseer
[ -f /etc/overseer/id_ed25519 ] || ssh-keygen -q -t ed25519 -f /etc/overseer/id_ed25519 -N "" -C overseer
chown overseer /etc/overseer/id_ed25519*

if [ "$WIZARD" = 1 ]; then
  say "Setup wizard"
  if [ -f /etc/overseer/config.yaml ]; then echo "Existing config found — keeping it (change it later with: overseer)"; else /opt/overseer/venv/bin/python /opt/overseer/setup.py --out-dir /etc/overseer; fi
fi
[ -f /etc/overseer/config.yaml ] || { echo "No /etc/overseer/config.yaml — run: python3 /opt/overseer/setup.py"; exit 1; }
[ -f /etc/overseer/messages.yaml ] || cp /opt/overseer/messages.example.yaml /etc/overseer/messages.yaml
chown overseer /etc/overseer/config.yaml /etc/overseer/secrets.env /etc/overseer/messages.yaml 2>/dev/null || true
chmod 600 /etc/overseer/secrets.env 2>/dev/null || true

# Ollama: install when the config wants a local one
if [ "$OLLAMA" = auto ]; then
  OLLAMA=$(/opt/overseer/venv/bin/python -c 'import yaml;l=yaml.safe_load(open("/etc/overseer/config.yaml"))["llm"];u=l["ollama"]["url"];print(int(l["backend"]=="ollama" and ("127.0.0.1" in u or "localhost" in u)))')
fi
if [ "$OLLAMA" = 1 ]; then
  say "Ollama (downloads ~7 GB of models)"
  command -v ollama >/dev/null || curl -fsSL https://ollama.com/install.sh | sh
  mkdir -p /etc/systemd/system/ollama.service.d
  printf '[Service]\nEnvironment=OLLAMA_KEEP_ALIVE=-1\nEnvironment=OLLAMA_CONTEXT_LENGTH=8192\n' > /etc/systemd/system/ollama.service.d/overseer.conf
  systemctl daemon-reload; systemctl enable --now ollama; systemctl restart ollama; sleep 3
  for m in $(/opt/overseer/venv/bin/python -c 'import yaml;o=yaml.safe_load(open("/etc/overseer/config.yaml"))["llm"]["ollama"];print(*sorted({o["model"],o.get("brief_model",o["model"])}))'); do
    echo "pulling $m"; ollama pull "$m" >/dev/null
    curl -s localhost:11434/api/generate -d "{\"model\":\"$m\",\"keep_alive\":-1}" >/dev/null || true
  done
  ollama ps || true
fi

say "Service"
cp deploy/overseer.service /etc/systemd/system/overseer.service
systemctl daemon-reload

say "Smoke test (one sweep)"
sudo -u overseer bash -c 'set -a; . /etc/overseer/secrets.env 2>/dev/null; cd /opt/overseer; venv/bin/python -m overseer.main -c /etc/overseer/config.yaml --once 2>/dev/null' \
 | python3 -c 'import sys,json
s=json.load(sys.stdin); cs=s["checks"]
[print(("  ok   " if c["ok"] else "  FAIL "), c["name"], "-", c["detail"][:80]) for c in cs]
print(); print(sum(1 for c in cs if c["ok"]), "/", len(cs), "checks passing")' || echo "  (smoke test couldn't run — check: journalctl -u overseer)"

systemctl enable --now overseer >/dev/null 2>&1; systemctl restart overseer; sleep 3
systemctl is-active --quiet overseer && echo -e "\n\033[1;32m✓ Overseer running.\033[0m You should get an 'Overseer online' message. Try /status." \
  || { echo "service failed:"; journalctl -u overseer -n 30 --no-pager; exit 1; }
echo -e "\nChange settings any time by typing:  \033[1moverseer\033[0m"
