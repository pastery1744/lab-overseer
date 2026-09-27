"""VMware ESXi / vCenter backend via pyVmomi (vSphere API). Targets use `moid` or `vmname` instead of vmid."""
import os, ssl, atexit


class ESXi:
    kind = "esxi"

    def __init__(self, cfg):
        from pyVim.connect import SmartConnect, Disconnect
        self._SmartConnect, self._Disconnect = SmartConnect, Disconnect
        self.cfg = cfg
        self.si = None

    def _conn(self):
        if self.si is not None:
            try:
                self.si.CurrentTime()
                return self.si
            except Exception:
                self.si = None
        ctx = None if self.cfg.get("verify_ssl") else ssl._create_unverified_context()
        self.si = self._SmartConnect(host=self.cfg["host"], user=self.cfg["user"],
                                     pwd=os.environ.get(self.cfg.get("password_env", "ESXI_PASSWORD"), ""),
                                     sslContext=ctx, port=int(self.cfg.get("port", 443)))
        atexit.register(self._Disconnect, self.si)
        return self.si

    def _vms(self):
        from pyVmomi import vim
        content = self._conn().RetrieveContent()
        view = content.viewManager.CreateContainerView(content.rootFolder, [vim.VirtualMachine], True)
        try:
            return list(view.view)
        finally:
            view.Destroy()

    def _hosts(self):
        from pyVmomi import vim
        content = self._conn().RetrieveContent()
        view = content.viewManager.CreateContainerView(content.rootFolder, [vim.HostSystem], True)
        try:
            return list(view.view)
        finally:
            view.Destroy()

    def _find(self, target):
        for vm in self._vms():
            if vm._moId == str(target.get("moid")) or vm.name == target.get("vmname"):
                return vm
        raise LookupError(f"VM {target.get('vmname') or target.get('moid')} not found")

    def list_guests(self):
        return sorted(({"id": vm._moId, "name": vm.name, "kind": "vm",
                        "status": "running" if str(vm.runtime.powerState) == "poweredOn" else "stopped"} for vm in self._vms()),
                      key=lambda g: g["name"].lower())

    def collect(self, targets):
        checks, facts = [], {}
        try:
            hosts = self._hosts()
            per_host, cpu_used, cpu_tot, mem_used, mem_tot = [], 0.0, 0.0, 0.0, 0.0
            for h in hosts:
                conn = str(getattr(h.runtime, "connectionState", "connected")) if getattr(h, "runtime", None) else "connected"
                up = conn == "connected"
                checks.append({"name": f"host-{h.name}-online", "target": "host", "ok": up, "detail": f"{h.name} {conn}"})
                if not up:
                    continue
                qs, hw = h.summary.quickStats, h.summary.hardware
                mhz = hw.cpuMhz * hw.numCpuCores
                mb = hw.memorySize / 1048576
                mem = qs.overallMemoryUsage / mb * 100
                checks.append({"name": f"host-{h.name}-mem", "target": "host", "ok": mem < 92, "detail": f"{h.name} mem {mem:.1f}%"})
                per_host.append({"name": h.name, "cpu_pct": round(qs.overallCpuUsage / mhz * 100, 1), "mem_pct": round(mem, 1),
                                 "uptime_h": round((qs.uptime or 0) / 3600, 1)})
                cpu_used += qs.overallCpuUsage; cpu_tot += mhz; mem_used += qs.overallMemoryUsage; mem_tot += mb
            if per_host:
                facts["node"] = per_host[0] if len(per_host) == 1 else {
                    "name": f"{len(per_host)} ESXi hosts", "cpu_pct": round(cpu_used / cpu_tot * 100, 1),
                    "mem_pct": round(mem_used / mem_tot * 100, 1), "uptime_h": min(p["uptime_h"] for p in per_host)}
                if len(per_host) > 1:
                    facts["nodes"] = per_host
            guests, byname = {}, {}
            for vm in self._vms():
                s = vm.summary
                maxmem = s.config.memorySizeMB or 0
                g = {"kind": "vm", "name": vm.name,
                     "status": "running" if str(s.runtime.powerState) == "poweredOn" else "stopped",
                     "cpu_pct": round((s.quickStats.overallCpuUsage or 0) / max(1, (s.runtime.maxCpuUsage or 1)) * 100, 1),
                     "mem_pct": round((s.quickStats.guestMemoryUsage or 0) / maxmem * 100, 1) if maxmem else None}
                guests[vm._moId] = g
                byname[vm.name] = g
            facts["guests"] = guests
            for tname, t in targets.items():
                if t.get("kind") == "vm" and t.get("expect_running", True) and (t.get("moid") or t.get("vmname")):
                    g = guests.get(str(t.get("moid"))) or byname.get(t.get("vmname"))
                    checks.append({"name": f"guest-{tname}-running", "target": tname, "ok": bool(g) and g["status"] == "running",
                                   "detail": f"{tname} status={g['status'] if g else 'missing'}"})
            seen = set()
            for h in hosts:
                for ds in getattr(h, "datastore", []) or []:
                    if ds.name in seen:          # shared datastores show up on every host
                        continue
                    seen.add(ds.name)
                    cap, free = ds.summary.capacity, ds.summary.freeSpace
                    if cap:
                        pct = (cap - free) / cap * 100
                        checks.append({"name": f"storage-{ds.name}", "target": "host", "ok": pct < 85, "detail": f"{ds.name} {pct:.1f}% used"})
        except Exception as e:
            self.si = None
            checks.append({"name": "hypervisor-api", "target": "host", "ok": False, "detail": f"API error: {e}"[:200]})
        return checks, facts

    def power(self, action, target):
        vm = self._find(target)
        if action == "start":
            vm.PowerOnVM_Task()
            return f"power on {vm.name} requested"
        # graceful guest reboot when VMware Tools is running, hard reset otherwise
        if str(vm.guest.toolsRunningStatus) == "guestToolsRunning":
            vm.RebootGuest()
            return f"guest reboot {vm.name} requested"
        vm.ResetVM_Task()
        return f"hard reset {vm.name} requested (no VMware Tools)"
