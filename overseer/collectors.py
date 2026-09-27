"""Gathers a snapshot of the lab. Every check returns {name, target, ok, detail}."""
import socket, subprocess, time, logging
from concurrent.futures import ThreadPoolExecutor
import requests

log = logging.getLogger("collect")


def _ping(host):
    r = subprocess.run(["ping", "-c", "2", "-W", "2", host], capture_output=True, text=True)
    return r.returncode == 0, (r.stdout.strip().splitlines() or [""])[-1]


def _tcp(host, port):
    try:
        with socket.create_connection((host, int(port)), timeout=3):
            return True, f"{host}:{port} open"
    except OSError as e:
        return False, f"{host}:{port} {e}"


def _http(url, expect=None):
    try:
        r = requests.get(url, timeout=6, verify=False)
        ok = r.status_code < 500 and (expect is None or r.status_code == expect)
        return ok, f"HTTP {r.status_code} in {r.elapsed.total_seconds():.2f}s"
    except requests.RequestException as e:
        return False, f"{type(e).__name__}: {e}"[:200]


def _dns(server, query):
    import dns.resolver
    res = dns.resolver.Resolver(configure=False)
    res.nameservers, res.lifetime = [server], 4
    try:
        a = res.resolve(query, "A")
        return True, f"{query} -> {a[0]}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"[:200]


def _one(c):
    t = c["type"]
    try:
        if t == "ping":
            ok, d = _ping(c["host"])
        elif t == "tcp":
            ok, d = _tcp(c["host"], c["port"])
        elif t == "http":
            ok, d = _http(c["url"], c.get("expect"))
        elif t == "dns":
            ok, d = _dns(c["server"], c["query"])
        else:
            ok, d = False, f"unknown check type {t}"
    except Exception as e:
        ok, d = False, f"check crashed: {e}"
    return {"name": c["name"], "target": c.get("target", "unknown"), "ok": ok, "detail": d}


def run_checks(checks, workers=32):
    """All checks run in parallel, so 200 dead hosts take ~5s, not 15 minutes. Output keeps config order."""
    active = [c for c in checks if not c.get("disabled")]
    if not active:
        return []
    with ThreadPoolExecutor(max_workers=min(workers, len(active))) as ex:
        return list(ex.map(_one, active))


class Switch:
    IFNAME, OPER, SPEED, INERR = ".1.3.6.1.2.1.31.1.1.1.1", ".1.3.6.1.2.1.2.2.1.8", ".1.3.6.1.2.1.31.1.1.1.15", ".1.3.6.1.2.1.2.2.1.14"

    def __init__(self, cfg):
        self.cfg = cfg
        self.prev_err = {}

    def _walk(self, oid):
        r = subprocess.run(["snmpbulkwalk", "-v2c", "-c", self.cfg["community"], "-Oqn", self.cfg["host"], oid],
                           capture_output=True, text=True, timeout=20)
        if r.returncode:
            raise RuntimeError(r.stderr.strip()[:200])
        out = {}
        for line in r.stdout.splitlines():
            k, _, v = line.partition(" ")
            out[k.rsplit(".", 1)[-1]] = v.strip().strip('"')
        return out

    def collect(self):
        checks = []
        try:
            with ThreadPoolExecutor(max_workers=4) as ex:   # 4 independent walks, run side by side
                names, oper, speed, err = ex.map(self._walk, (self.IFNAME, self.OPER, self.SPEED, self.INERR))
            by_name = {v: k for k, v in names.items()}
            ignore = set(self.cfg.get("ignore_ports", []))
            for port, spec in self.cfg.get("watch_ports", {}).items():
                if port in ignore:
                    continue
                idx = by_name.get(port)
                if idx is None:
                    checks.append({"name": f"sw-{port}", "target": "switch", "ok": False, "detail": f"{port} not found via SNMP"})
                    continue
                up = oper.get(idx) in ("1", "up")
                sp = int(speed.get(idx, "0") or 0)
                ok = up and (not spec.get("expect_speed") or sp >= spec["expect_speed"])
                e = int(err.get(idx, "0") or 0)
                de = e - self.prev_err.get(port, e)
                self.prev_err[port] = e
                if de > spec.get("max_err_delta", 50):
                    ok = False
                checks.append({"name": f"sw-{port}", "target": "switch", "ok": ok,
                               "detail": f"{port} ({spec.get('role','')}) {'up' if up else 'DOWN'} {sp}Mb in_err+{de}"})
        except Exception as e:
            checks.append({"name": "switch-snmp", "target": "switch", "ok": False, "detail": f"SNMP error: {e}"[:200]})
        return checks


def snapshot(cfg, hv, switch, local=None):
    t0 = time.time()
    # every source runs concurrently; the sweep takes as long as the slowest one, not the sum
    with ThreadPoolExecutor(max_workers=4) as ex:
        f_checks = ex.submit(run_checks, cfg.get("checks", []))
        f_hv = ex.submit(hv.collect, cfg.get("targets", {})) if hv else None
        f_local = ex.submit(local.collect) if local else None
        f_sw = ex.submit(switch.collect) if switch else None
    checks, facts = f_checks.result(), {}
    if f_hv:
        c, facts = f_hv.result()
        checks += c
    if f_local:
        c, lf = f_local.result()
        checks += c
        facts.setdefault("node", lf.get("node"))
        if lf.get("containers"):
            facts["containers"] = lf["containers"]
    if f_sw:
        checks += f_sw.result()
    return {"ts": int(t0), "took_s": round(time.time() - t0, 1), "checks": checks, "facts": facts}
