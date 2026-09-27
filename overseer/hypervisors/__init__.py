def make_hypervisor(cfg):
    """cfg['hypervisor'] = {type: proxmox|esxi|none, ...}"""
    h = (cfg or {}).get("hypervisor") or {}
    t = h.get("type", "none")
    if t == "proxmox":
        from .proxmox import Proxmox
        return Proxmox(h)
    if t == "esxi":
        from .esxi import ESXi
        return ESXi(h)
    return None
