"""Proxmox VE backend — works on a single node or a whole cluster (REST API, API token)."""
import os
import requests


class Proxmox:
    kind = "proxmox"

    def __init__(self, cfg):
        self.base = cfg["url"].rstrip("/") + "/api2/json"
        self.h = {"Authorization": f"PVEAPIToken={cfg['token_id']}={os.environ.get(cfg.get('token_secret_env', 'PVE_TOKEN'), '')}"}
        self.verify = cfg.get("verify_ssl", False)
        self.node = cfg.get("node")          # optional; only used as a fallback label
        self._where = {}                     # vmid -> node, refreshed every sweep

    def get(self, path):
        r = requests.get(self.base + path, headers=self.h, verify=self.verify, timeout=10)
        r.raise_for_status()
        return r.json()["data"]

    def post(self, path):
        r = requests.post(self.base + path, headers=self.h, verify=self.verify, timeout=15)
        r.raise_for_status()
        return r.json()["data"]

    def _resources(self):
        return self.get("/cluster/resources")

    def list_guests(self):
        out = []
        for r in self._resources():
            if r["type"] in ("qemu", "lxc") and not r.get("template"):
                out.append({"id": int(r["vmid"]), "name": r.get("name") or str(r["vmid"]),
                            "kind": "vm" if r["type"] == "qemu" else "ct", "status": r["status"], "node": r["node"]})
        return sorted(out, key=lambda g: g["id"])

    def collect(self, targets):
        checks, facts = [], {}
        try:
            res = self._resources()
            nodes = [r for r in res if r["type"] == "node"]
            per_node, cpu_used, cpu_tot, mem_used, mem_tot = [], 0.0, 0, 0, 0
            for n in nodes:
                online = n.get("status") == "online"
                checks.append({"name": f"node-{n['node']}-online", "target": "host", "ok": online,
                               "detail": f"{n['node']} {n.get('status')}"})
                if not online or not n.get("maxmem"):
                    continue
                mem = n["mem"] / n["maxmem"] * 100
                checks.append({"name": f"node-{n['node']}-mem", "target": "host", "ok": mem < 92, "detail": f"{n['node']} mem {mem:.1f}%"})
                per_node.append({"name": n["node"], "cpu_pct": round(n.get("cpu", 0) * 100, 1), "mem_pct": round(mem, 1),
                                 "uptime_h": round(n.get("uptime", 0) / 3600, 1)})
                cpu_used += n.get("cpu", 0) * n.get("maxcpu", 1)
                cpu_tot += n.get("maxcpu", 1)
                mem_used += n["mem"]
                mem_tot += n["maxmem"]
            if per_node:
                facts["node"] = per_node[0] if len(per_node) == 1 else {
                    "name": f"cluster ({len(per_node)} nodes)", "cpu_pct": round(cpu_used / cpu_tot * 100, 1),
                    "mem_pct": round(mem_used / mem_tot * 100, 1), "uptime_h": min(p["uptime_h"] for p in per_node)}
                if len(per_node) > 1:
                    facts["nodes"] = per_node
            guests = {}
            self._where = {}
            for r in res:
                if r["type"] in ("qemu", "lxc") and not r.get("template"):
                    gid = int(r["vmid"])
                    self._where[gid] = r["node"]
                    guests[gid] = {"kind": "vm" if r["type"] == "qemu" else "ct", "name": r.get("name"), "status": r["status"],
                                   "node": r["node"], "cpu_pct": round(r.get("cpu", 0) * 100, 1),
                                   "mem_pct": round(r.get("mem", 0) / r["maxmem"] * 100, 1) if r.get("maxmem") else None}
            facts["guests"] = guests
            for tname, t in targets.items():
                gid = t.get("vmid")
                if gid is not None and t.get("expect_running", True):
                    g = guests.get(int(gid))
                    checks.append({"name": f"guest-{gid}-running", "target": tname, "ok": bool(g) and g["status"] == "running",
                                   "detail": f"{gid} status={g['status'] if g else 'missing'}" + (f" on {g['node']}" if g and len(nodes) > 1 else "")})
            seen_shared = set()
            for r in res:
                if r["type"] != "storage" or not r.get("maxdisk") or r.get("status") != "available":
                    continue
                if r.get("shared"):
                    if r["storage"] in seen_shared:
                        continue                      # shared storage shows once per node; count it once
                    seen_shared.add(r["storage"])
                    label = r["storage"]
                else:
                    label = r["storage"] if len(nodes) == 1 else f"{r['node']}/{r['storage']}"
                pct = r["disk"] / r["maxdisk"] * 100
                checks.append({"name": f"storage-{label.replace('/', '-')}", "target": "host", "ok": pct < 85,
                               "detail": f"{label} {pct:.1f}% used"})
        except Exception as e:
            checks.append({"name": "hypervisor-api", "target": "host", "ok": False, "detail": f"API error: {e}"[:200]})
        return checks, facts

    def _node_of(self, vmid):
        vmid = int(vmid)
        if vmid not in self._where:                 # guest may have migrated since the last sweep
            for r in self._resources():
                if r["type"] in ("qemu", "lxc"):
                    self._where[int(r["vmid"])] = r["node"]
        return self._where.get(vmid) or self.node

    def power(self, action, target):
        kind = "qemu" if target.get("kind") == "vm" else "lxc"
        verb = "start" if action == "start" else "reboot"
        node = self._node_of(target["vmid"])
        self.post(f"/nodes/{node}/{kind}/{int(target['vmid'])}/status/{verb}")
        return f"{verb} {kind} {target['vmid']} on {node} requested"
