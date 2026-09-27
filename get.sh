#!/usr/bin/env bash
# Lab Overseer — one-line installer.
#
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/pastery1744/lab-overseer/primary/get.sh)"
#
# On a Proxmox host  → builds a container and installs everything (deploy-proxmox.sh)
# On Debian/Ubuntu   → installs right here (install.sh)
# Options (env vars): OVERSEER_REF=v1.2.0 (tag/branch, default primary)   OVERSEER_REPO=owner/repo
set -euo pipefail
REPO="${OVERSEER_REPO:-pastery1744/lab-overseer}"
REF="${OVERSEER_REF:-primary}"
B="\033[1m"; C="\033[1;36m"; R="\033[0m"

[ "$(id -u)" = 0 ] || { echo "Please run as root, e.g.:  sudo bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/$REPO/$REF/get.sh)\""; exit 1; }
for t in curl tar; do command -v $t >/dev/null || { apt-get update -qq && apt-get install -y -qq curl tar >/dev/null; }; done

echo -e "${C}== Lab Overseer${R} — downloading ${B}$REPO@$REF${R}"
DIR=$(mktemp -d /tmp/lab-overseer.XXXX)
trap 'rm -rf "$DIR"' EXIT
URL="${OVERSEER_TARBALL:-https://codeload.github.com/$REPO/tar.gz/$REF}"   # OVERSEER_TARBALL: test a local build
curl -fsSL "$URL" | tar -xz -C "$DIR" --strip-components=1 \
  || { echo "Download failed — check the repo name/branch, or your internet."; exit 1; }
chmod +x "$DIR"/*.sh "$DIR"/setup.py "$DIR"/tools/* 2>/dev/null || true

# Menus need a real keyboard even when this script arrived through a pipe
TTY=/dev/stdin; { : </dev/tty; } 2>/dev/null && TTY=/dev/tty

if command -v pct >/dev/null 2>&1; then
  echo -e "Proxmox host detected → creating an overseer container."
  # deploy copies the app into the container, so keep it outside the temp dir we clean up
  KEEP=/root/lab-overseer; rm -rf "$KEEP.new"; cp -a "$DIR" "$KEEP.new"; rm -rf "$KEEP"; mv "$KEEP.new" "$KEEP"
  rm -rf "$DIR"; trap - EXIT
  cd "$KEEP" && exec ./deploy-proxmox.sh <"$TTY"
elif [ -r /etc/os-release ] && grep -qiE '^ID(_LIKE)?=.*(debian|ubuntu)' /etc/os-release; then
  echo -e "Debian/Ubuntu detected → installing on this machine."
  bash "$DIR/install.sh" <"$TTY"
else
  echo "This machine isn't Proxmox or Debian/Ubuntu."
  echo "On ESXi (or anything else): create a Debian 12 VM, then run this same command inside it."
  exit 1
fi
