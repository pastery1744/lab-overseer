#!/usr/bin/env bash
# Lab Overseer — one-command deploy for Proxmox VE. Run as root ON THE PROXMOX HOST, from this folder:
#
#   ./deploy-proxmox.sh
#
# Picks sensible settings automatically, makes a least-privilege API token and a Debian container,
# runs the setup wizard, installs, and starts. Never touches an existing install without asking. Deletes nothing.
set -euo pipefail
export LC_ALL=C
SRC="$(cd "$(dirname "$0")" && pwd)"
T="Lab Overseer"
command -v pct >/dev/null || { echo "This isn't a Proxmox host. On ESXi or anything else: make a Debian VM and run ./install.sh inside it."; exit 1; }
[ "$(id -u)" = 0 ] || { echo "Please run as root."; exit 1; }
command -v whiptail >/dev/null || apt-get install -y -qq whiptail >/dev/null 2>&1 || true
WT=$(command -v whiptail || true)

# ---------- tiny dialog helpers (whiptail, or plain prompts) ----------
msg(){ if [ -n "$WT" ]; then whiptail --title "$T" --msgbox "$1" 16 74; else echo -e "\n$1\n"; read -rp "[Enter] " _; fi; }
yesno(){ if [ -n "$WT" ]; then whiptail --title "$T" ${3:+--yes-button "$3"} ${4:+--no-button "$4"} --yesno "$1" "${2:-14}" 74; else read -rp "$1 (y/n): " a; [[ "$a" =~ ^[Yy] ]]; fi; }
input(){ if [ -n "$WT" ]; then whiptail --title "$T" --inputbox "$1" 10 74 "$2" 3>&1 1>&2 2>&3; else read -rp "$1 [$2]: " v; echo "${v:-$2}"; fi; }
menu(){ local q=$1; shift; if [ -n "$WT" ]; then whiptail --title "$T" --menu "$q" 18 74 8 "$@" 3>&1 1>&2 2>&3; else
  echo "$q"; local i=1 keys=(); while [ $# -gt 0 ]; do echo "  $i) $2"; keys+=("$1"); shift 2; i=$((i+1)); done
  read -rp "choice [1]: " c; echo "${keys[$(( ${c:-1} - 1 ))]}"; fi; }
say(){ echo -e "\n\033[1;36m== $*\033[0m"; }

# ---------- existing installs? ----------
EXISTING=()
for id in $(pct list | awk 'NR>1{print $1}'); do
  pct config "$id" 2>/dev/null | grep -qE '^hostname: overseer|Lab Overseer|AI overseer' && EXISTING+=("$id")
done
MODE=new
if [ ${#EXISTING[@]} -gt 0 ]; then
  OPTS=(); for id in "${EXISTING[@]}"; do OPTS+=("up-$id" "Upgrade existing overseer in CT $id (keeps its settings)"); done
  OPTS+=("new" "Install a brand-new, separate overseer" "quit" "Quit — change nothing")
  CH=$(menu "Found an overseer already installed. What would you like to do?" "${OPTS[@]}") || exit 0
  case "$CH" in quit) exit 0 ;; up-*) MODE=upgrade; CTID=${CH#up-} ;; esac
fi

# ---------- new install: pick everything automatically ----------
if [ "$MODE" = new ]; then
  LLM=$(menu "Which AI should do the thinking?" \
      ollama "Local (Ollama) — free & private, needs ~12 GB RAM" \
      claude "Claude API — smarter & faster, a few \$/month" \
      remote "An Ollama server I already run elsewhere") || exit 0
  CTID=$(pvesh get /cluster/nextid)
  N=""; while pct list | awk '{print $3}' | grep -qx "overseer$N"; do N=$(( ${N:-1} + 1 )); done; NAME="overseer$N"
  STORAGE=$(pvesm status --content rootdir 2>/dev/null | awk 'NR>1 && $3=="active"{print $6, $1}' | sort -rn | head -1 | awk '{print $2}')
  DEFDEV=$(ip route show default | awk '{print $5; exit}')
  BRIDGE=${DEFDEV%%.*}; VLAN=""; [[ "$DEFDEV" == *.* ]] && VLAN=${DEFDEV#*.}
  [ -d "/sys/class/net/$BRIDGE/bridge" ] || BRIDGE=vmbr0
  NCPU=$(nproc); FREE_MB=$(awk '/MemAvailable/{print int($2/1024)}' /proc/meminfo)
  if [ "$LLM" = ollama ]; then CORES=$(( NCPU / 2 )); [ $CORES -gt 16 ] && CORES=16; [ $CORES -lt 4 ] && CORES=$NCPU; MEM=12288; DISK=32
  else CORES=2; MEM=1024; DISK=8; fi
  IPCFG=dhcp; GW=""
  SUMMARY="Recommended settings:\n\n  Container:  $CTID  ($NAME)\n  CPU / RAM:  $CORES cores / $((MEM/1024)) GB\n  Disk:       ${DISK} GB on $STORAGE\n  Network:    $BRIDGE${VLAN:+ (VLAN $VLAN)}, DHCP\n  AI:         $LLM"
  [ "$LLM" = ollama ] && [ "$FREE_MB" -lt $((MEM + 2048)) ] && SUMMARY="$SUMMARY\n\n⚠ Only $((FREE_MB/1024)) GB RAM free on this host — Claude may suit better."
  if ! yesno "$SUMMARY" 20 "Create it" "Customize"; then
    CTID=$(input "Container ID" "$CTID"); NAME=$(input "Container name" "$NAME")
    STORAGE=$(input "Storage for the disk" "$STORAGE"); BRIDGE=$(input "Network bridge" "$BRIDGE")
    VLAN=$(input "VLAN tag (leave blank for none)" "$VLAN"); IPCFG=$(input "IP: 'dhcp' or an address like 192.168.1.40/24" "dhcp")
    [ "$IPCFG" != dhcp ] && GW=$(input "Gateway" "$(echo "$IPCFG" | cut -d/ -f1 | cut -d. -f1-3).1")
    CORES=$(input "CPU cores" "$CORES"); MEM=$(input "RAM (MB)" "$MEM"); DISK=$(input "Disk (GB)" "$DISK")
  fi

  say "API user + token (can only view and power VMs — nothing else)"
  pveum role add OverseerRole -privs "VM.Audit VM.PowerMgmt Sys.Audit Datastore.Audit" 2>/dev/null || true
  pveum user add overseer@pve --comment "Lab Overseer" 2>/dev/null || true
  pveum aclmod / -user overseer@pve -role OverseerRole
  TN="ov$(date +%y%m%d%H%M%S)"
  PVE_TOKEN=$(pveum user token add overseer@pve "$TN" --privsep 0 --output-format json | python3 -c 'import sys,json;print(json.load(sys.stdin)["value"])')
  HOSTIP=$(ip -4 -o addr show "$DEFDEV" 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | head -1); HOSTIP=${HOSTIP:-$(hostname -I | awk '{print $1}')}
  export PVE_URL="https://$HOSTIP:8006" PVE_NODE="$(hostname)" PVE_TOKEN_ID="overseer@pve!$TN" PVE_TOKEN
  export OVERSEER_LLM=$([ "$LLM" = claude ] && echo claude || ([ "$LLM" = remote ] && echo remote || echo ollama))

  say "Setup wizard"
  TMPC=$(mktemp -d); trap 'rm -rf "$TMPC"' EXIT
  python3 "$SRC/setup.py" --hypervisor proxmox --out-dir "$TMPC" || { echo "Setup cancelled — nothing was created except the API token ($TN)."; exit 1; }

  say "Debian 12 template"
  pveam update >/dev/null
  TPL=$(pveam available --section system | awk '/debian-12-standard/{print $2}' | sort -V | tail -1)
  pveam list local | grep -q "$TPL" || pveam download local "$TPL"

  say "Creating container $CTID"
  NET="name=eth0,bridge=$BRIDGE,ip=$IPCFG"; [ -n "$VLAN" ] && NET="$NET,tag=$VLAN"; [ -n "$GW" ] && NET="$NET,gw=$GW"
  pct create "$CTID" "local:vztmpl/$TPL" --hostname "$NAME" --cores "$CORES" --memory "$MEM" --swap 1024 \
    --rootfs "$STORAGE:$DISK" --net0 "$NET" --unprivileged 1 --features nesting=1 --onboot 1 \
    --tags "monitoring" --description "Lab Overseer — AI monitoring"
  pct start "$CTID"
  echo -n "waiting for network"; for i in $(seq 1 45); do pct exec "$CTID" -- ping -c1 -W1 1.1.1.1 &>/dev/null && break; echo -n .; sleep 2; done; echo
  pct exec "$CTID" -- ping -c1 -W2 1.1.1.1 &>/dev/null || { msg "The container has no internet yet.\nCheck the bridge/VLAN/DHCP, then re-run this script and choose Upgrade."; exit 1; }
fi

say "Copying the app into CT $CTID"
tar -C "$SRC" --exclude=venv --exclude=__pycache__ --exclude=.git -czf /tmp/overseer-src.tgz .
pct exec "$CTID" -- mkdir -p /opt/overseer /etc/overseer
pct push "$CTID" /tmp/overseer-src.tgz /tmp/overseer-src.tgz && rm -f /tmp/overseer-src.tgz
pct exec "$CTID" -- tar -xzf /tmp/overseer-src.tgz -C /opt/overseer
if [ "$MODE" = new ]; then
  pct push "$CTID" "$TMPC/config.yaml" /etc/overseer/config.yaml
  pct push "$CTID" "$TMPC/secrets.env" /etc/overseer/secrets.env --perms 600
fi

say "Installing inside CT $CTID (this is the slow part — AI models are a few GB)"
pct exec "$CTID" -- bash /opt/overseer/install.sh --no-wizard

IP=$(pct exec "$CTID" -- hostname -I 2>/dev/null | awk '{print $1}')
msg "✓ Done! Container $CTID is running the overseer${IP:+ at $IP}.\n\nYou should have an 'Overseer online' message.\nTry sending it:  /status\n\nTo change settings later:\n  pct enter $CTID   then type:  overseer"
