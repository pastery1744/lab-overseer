"""VMware ESXi / vCenter backend via pyVmomi (vSphere API). Targets use `moid` or `vmname` instead of vmid.

Every attribute read on a pyVmomi managed object is its own SOAP round-trip, so collection fetches all the
properties it needs per object type in one PropertyCollector call (3 calls per sweep, whatever the lab size)."""
import os, ssl, atexit

HOST_PROPS = ["name", "runtime.connectionState", "summary.quickStats", "summary.hardware"]
VM_PROPS = ["name", "summary.config.memorySizeMB", "summary.runtime.powerState", "summary.runtime.maxCpuUsage",
            "summary.quickStats.overallCpuUsage", "summary.quickStats.guestMemoryUsage"]
DS_PROPS = ["name", "summary.capacity", "summary.freeSpace"]


class ESXi:
    kind = "esxi"

    def __init__(self, cfg):
        from pyVim.connect import SmartConnect, Disconnect
        self._SmartConnect, self._Disconnect = SmartConnect, Disconnect
        self.cfg = cfg
        self.si = None
        self.content = None
        atexit.register(self._close)                 # once, not per reconnect

    def _close(self):
        if self.si is not None:
            try:
                self._Disconnect(self.si)
            except Exception:
                pass

    def _conn(self, probe=False):
        """Reuse the session; collect() drops it on any API error. `probe` re-checks it (for rare power actions)."""
        if self.si is not None:
            if not probe:
                return self.si
            try:
                self.si.CurrentTime()
                return self.si
            except Exception:
                self.si = None
        ctx = None if self.cfg.get("verify_ssl") else ssl._create_unverified_context()
        self.si = self._SmartConnect(host=self.cfg["host"], user=self.cfg["user"],
                                     pwd=os.environ.get(self.cfg.get("password_env", "ESXI_PASSWORD"), ""),
                                     sslContext=ctx, port=int(self.cfg.get("port", 443)))
        self.content = self.si.RetrieveContent()
        return self.si

    def _retrieve(self, vimtype, paths, probe=False):
        """[{path: value, ..., '_obj': managed object}] for every object of `vimtype`, in a single round-trip."""
        from pyVmomi import vim, vmodl
        self._conn(probe)
        pc = vmodl.query.PropertyCollector
        view = self.content.viewManager.CreateContainerView(self.content.rootFolder, [vimtype], True)
        try:
            spec = pc.FilterSpec(
                objectSet=[pc.ObjectSpec(obj=view, skip=True, selectSet=[
                    pc.TraversalSpec(name="view", path="view", skip=False, type=vim.view.ContainerView)])],
                propSet=[pc.PropertySpec(type=vimtype, pathSet=paths)])
            res = self.content.propertyCollector.RetrieveContents([spec])
        finally:
            view.Destroy()
        return [dict({p.name: p.val for p in o.propSet}, _obj=o.obj) for o in res]   # unset paths are simply absent

    def _host_rows(self):
        from pyVmomi import vim
        return self._retrieve(vim.HostSystem, HOST_PROPS)

    def _vm_rows(self, probe=False):
        from pyVmomi import vim
        return self._retrieve(vim.VirtualMachine, VM_PROPS, probe)

    def _ds_rows(self):
        from pyVmomi import vim
        return self._retrieve(vim.Datastore, DS_PROPS)

    def _find(self, target):
        for r in self._vm_rows(probe=True):
            if r["_obj"]._moId == str(target.get("moid")) or r.get("name") == target.get("vmname"):
                return r["_obj"]
        raise LookupError(f"VM {target.get('vmname') or target.get('moid')} not found")

    def list_guests(self):
        return sorted(({"id": r["_obj"]._moId, "name": r.get("name", ""), "kind": "vm",
                        "status": "running" if str(r.get("summary.runtime.powerState")) == "poweredOn" else "stopped"}
                       for r in self._vm_rows(probe=True)), key=lambda g: g["name"].lower())

    def collect(self, targets):
        checks, facts = [], {}
        try:
            per_host, cpu_used, cpu_tot, mem_used, mem_tot = [], 0.0, 0.0, 0.0, 0.0
            for h in self._host_rows():
                name, conn = h.get("name"), str(h.get("runtime.connectionState", "connected"))
                up = conn == "connected"
                checks.append({"name": f"host-{name}-online", "target": "host", "ok": up, "detail": f"{name} {conn}"})
                if not up:
                    continue
                qs, hw = h["summary.quickStats"], h["summary.hardware"]
                mhz = hw.cpuMhz * hw.numCpuCores
                mb = hw.memorySize / 1048576
                mem = qs.overallMemoryUsage / mb * 100
                checks.append({"name": f"host-{name}-mem", "target": "host", "ok": mem < 92, "detail": f"{name} mem {mem:.1f}%"})
                per_host.append({"name": name, "cpu_pct": round(qs.overallCpuUsage / mhz * 100, 1), "mem_pct": round(mem, 1),
                                 "uptime_h": round((qs.uptime or 0) / 3600, 1)})
                cpu_used += qs.overallCpuUsage; cpu_tot += mhz; mem_used += qs.overallMemoryUsage; mem_tot += mb
            if per_host:
                facts["node"] = per_host[0] if len(per_host) == 1 else {
                    "name": f"{len(per_host)} ESXi hosts", "cpu_pct": round(cpu_used / cpu_tot * 100, 1),
                    "mem_pct": round(mem_used / mem_tot * 100, 1), "uptime_h": min(p["uptime_h"] for p in per_host)}
                if len(per_host) > 1:
                    facts["nodes"] = per_host
            guests, byname = {}, {}
            for r in self._vm_rows():
                maxmem = r.get("summary.config.memorySizeMB") or 0
                g = {"kind": "vm", "name": r.get("name"),
                     "status": "running" if str(r.get("summary.runtime.powerState")) == "poweredOn" else "stopped",
                     "cpu_pct": round((r.get("summary.quickStats.overallCpuUsage") or 0) / max(1, r.get("summary.runtime.maxCpuUsage") or 1) * 100, 1),
                     "mem_pct": round((r.get("summary.quickStats.guestMemoryUsage") or 0) / maxmem * 100, 1) if maxmem else None}
                guests[r["_obj"]._moId] = g
                byname[g["name"]] = g
            facts["guests"] = guests
            for tname, t in targets.items():
                if t.get("kind") == "vm" and t.get("expect_running", True) and (t.get("moid") or t.get("vmname")):
                    g = guests.get(str(t.get("moid"))) or byname.get(t.get("vmname"))
                    checks.append({"name": f"guest-{tname}-running", "target": tname, "ok": bool(g) and g["status"] == "running",
                                   "detail": f"{tname} status={g['status'] if g else 'missing'}"})
            for ds in self._ds_rows():               # each datastore once, even when shared by every host
                cap, free = ds.get("summary.capacity"), ds.get("summary.freeSpace") or 0
                if cap:
                    pct = (cap - free) / cap * 100
                    checks.append({"name": f"storage-{ds.get('name')}", "target": "host", "ok": pct < 85,
                                   "detail": f"{ds.get('name')} {pct:.1f}% used"})
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
