"""Watch the machine the overseer runs on: CPU, RAM, disks, load, and chosen systemd services.
Used for plain Ubuntu/Debian servers with no hypervisor."""
import json, os, shutil, subprocess, time

# services worth pre-ticking in the wizard (anything else running is still offered, just unticked)
COMMON = {"nginx", "apache2", "httpd", "caddy", "haproxy", "traefik", "docker", "containerd", "podman",
          "mysql", "mariadb", "postgresql", "redis-server", "redis", "mongod", "memcached",
          "ssh", "sshd", "smbd", "nmbd", "nfs-server", "vsftpd", "pihole-FTL", "unbound", "named", "bind9", "dnsmasq",
          "grafana-server", "influxdb", "prometheus", "loki", "node_exporter", "plexmediaserver", "jellyfin", "emby-server",
          "home-assistant", "mosquitto", "tailscaled", "wg-quick@wg0", "cloudflared", "fail2ban", "ufw", "icecast2",
          "minecraft", "php8.1-fpm", "php8.2-fpm", "php8.3-fpm", "gitea", "nextcloud", "vaultwarden", "uptime-kuma", "ollama"}
# plumbing nobody wants alerts about
BORING = ("systemd-", "dbus", "getty@", "serial-getty", "user@", "polkit", "udisks", "upower", "rsyslog", "cron", "atd",
          "multipathd", "packagekit", "snapd", "unattended-upgrades", "networkd-dispatcher", "irqbalance", "thermald",
          "ModemManager", "accounts-daemon", "qemu-guest-agent", "open-vm-tools", "lxcfs", "apparmor", "blk-availability",
          "overseer", "wpa_supplicant", "avahi", "cups", "chrony", "ntp", "fwupd", "rtkit", "colord", "kerneloops", "iscsid")


def running_services():
    try:
        out = subprocess.run(["systemctl", "list-units", "--type=service", "--state=running", "--no-legend", "--plain"],
                             capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return []
    names = [l.split()[0].removesuffix(".service") for l in out.splitlines() if l.strip()]
    return sorted(n for n in names if not n.startswith(BORING))


_prev_stat = None


def _cpu_pct(interval=0.5):
    """Average CPU since the previous call (i.e. over the whole sweep). Only the very first call sleeps `interval`."""
    global _prev_stat
    def snap():
        v = [int(x) for x in open("/proc/stat").readline().split()[1:]]
        return sum(v), v[3] + (v[4] if len(v) > 4 else 0)
    if _prev_stat is None:
        _prev_stat = snap()
        time.sleep(interval)
    (t1, i1), (t2, i2) = _prev_stat, snap()
    _prev_stat = (t2, i2)
    return round(100 * (1 - (i2 - i1) / max(1, t2 - t1)), 1)


def _mem_pct():
    m = {l.split(":")[0]: int(l.split()[1]) for l in open("/proc/meminfo")}
    return round(100 * (1 - m["MemAvailable"] / m["MemTotal"]), 1)


def _uptime_h():
    return round(float(open("/proc/uptime").read().split()[0]) / 3600, 1)


def docker_bin():
    return shutil.which("docker")


def list_containers(use_sudo=True):
    """[{name, image, state, health}] for every container, or [] if Docker isn't here/usable."""
    d = docker_bin()
    if not d:
        return []
    cmd = (["sudo", "-n"] if use_sudo and os.geteuid() != 0 else []) + [d, "ps", "-a", "--no-trunc", "--format", "{{json .}}"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except Exception:
        return None
    if out.returncode != 0:
        return None                      # None = docker present but unreadable (permissions) -> reported as a failing check
    res = []
    for line in out.stdout.splitlines():
        try:
            j = json.loads(line)
        except ValueError:
            continue
        status = j.get("Status", "")
        health = "unhealthy" if "(unhealthy)" in status else "starting" if "(health: starting)" in status else \
                 "healthy" if "(healthy)" in status else ""
        res.append({"name": j.get("Names", "").split(",")[0], "image": j.get("Image", ""), "state": j.get("State", ""),
                    "status": status, "health": health})
    return res


def _active(units):
    """{unit: state} from one systemctl call (it prints one line per unit, in order; exit code is nonzero if any is down)."""
    r = subprocess.run(["systemctl", "is-active", *units], capture_output=True, text=True, timeout=10)
    states = r.stdout.split()
    return {u: states[i] if i < len(states) else "unknown" for i, u in enumerate(units)}


class LocalHost:
    def __init__(self, cfg):
        self.cfg = cfg or {}
        self.name = self.cfg.get("name") or os.uname().nodename

    def collect(self):
        checks, facts = [], {}
        try:
            cpu, mem = _cpu_pct(), _mem_pct()
            load1 = os.getloadavg()[0]
            ncpu = os.cpu_count() or 1
            facts["node"] = {"name": self.name, "cpu_pct": cpu, "mem_pct": mem, "uptime_h": _uptime_h(), "load1": round(load1, 2)}
            checks.append({"name": "local-mem", "target": "this-server", "ok": mem < self.cfg.get("mem_warn", 92), "detail": f"RAM {mem}%"})
            checks.append({"name": "local-load", "target": "this-server", "ok": load1 < ncpu * self.cfg.get("load_warn_per_cpu", 2),
                           "detail": f"load {load1:.2f} on {ncpu} CPUs"})
            for path in self.cfg.get("disks", ["/"]):
                try:
                    du = shutil.disk_usage(path)
                    pct = du.used / du.total * 100
                    checks.append({"name": f"storage-{path.strip('/').replace('/', '-') or 'root'}", "target": "this-server",
                                   "ok": pct < self.cfg.get("disk_warn", 85), "detail": f"{path} {pct:.1f}% used"})
                except OSError as e:
                    checks.append({"name": f"storage-{path}", "target": "this-server", "ok": False, "detail": f"{path}: {e}"})
        except Exception as e:
            checks.append({"name": "local-metrics", "target": "this-server", "ok": False, "detail": f"metrics error: {e}"[:200]})
        watch = self.cfg.get("containers") or []
        if watch:
            cs = list_containers()
            if cs is None:
                checks.append({"name": "docker", "target": "docker", "ok": False, "detail": "can't read Docker (is it running? permissions?)"})
            else:
                by = {c["name"]: c for c in cs}
                facts["containers"] = {c["name"]: {"state": c["state"], "health": c["health"], "image": c["image"]} for c in cs if c["name"] in watch}
                for name in watch:
                    c = by.get(name)
                    ok = bool(c) and c["state"] == "running" and c["health"] != "unhealthy"
                    detail = f"{name} " + (c["status"] if c else "missing (removed or renamed?)")
                    checks.append({"name": f"ctr-{name}", "target": self.cfg.get("container_targets", {}).get(name, name), "ok": ok, "detail": detail[:120]})
        services = self.cfg.get("services", [])
        try:
            states = _active(services) if services else {}
        except Exception as e:
            states = {s: f"error {e}" for s in services}
        for svc in services:
            state = states[svc]
            checks.append({"name": f"svc-{svc}", "target": svc, "ok": state == "active", "detail": f"{svc} {state}"})
        return checks, facts
