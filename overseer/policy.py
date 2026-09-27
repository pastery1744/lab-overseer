"""Severity gating. Hard-coded. The LLM can raise a tier, never lower it below a floor.

Tiers:
  0 info      -> log only
  1-3         -> any proposed fix waits for explicit GO (owner approves every abnormality)
  4 critical  -> notify only, never act (security / data risk)
"""
from dataclasses import dataclass

ALLOWED_ACTIONS = {"none", "start_vm", "reboot_vm", "start_ct", "reboot_ct", "restart_service"}


@dataclass
class Decision:
    tier: int
    action: str          # validated action or "none"
    action_arg: str      # service name for restart_service, else ""
    mode: str            # log | notify | grace | approve | notify_only
    reason: str          # why policy changed anything (empty if untouched)


def decide(incident: dict, targets: dict, recent_actions: int, max_actions_per_hour: int = 2) -> Decision:
    notes = []
    tier = int(incident.get("tier", 1))
    tier = max(0, min(4, tier))
    action = incident.get("action") or "none"
    arg = incident.get("action_arg") or ""
    tname = incident.get("target", "unknown")
    t = targets.get(tname)

    if incident.get("security"):
        if tier < 4:
            notes.append("security flag -> tier 4")
        tier = 4

    if t is None:
        if action != "none":
            notes.append(f"unknown target '{tname}' -> no action")
        action, arg = "none", ""
    else:
        floor = int(t.get("floor_tier", 0))
        if tier < floor:
            notes.append(f"floor for {tname} is {floor}")
            tier = floor
        allowed = t.get("actions", [])
        key = f"restart_service:{arg}" if action == "restart_service" else action
        if action not in ALLOWED_ACTIONS or (action != "none" and key not in allowed):
            notes.append(f"action '{key}' not whitelisted for {tname}")
            action, arg = "none", ""

    if action != "none" and recent_actions >= max_actions_per_hour and tier < 3:
        notes.append(f"{recent_actions} auto-actions on {tname} in last hour (flapping) -> tier 3")
        tier = 3

    if tier == 0:
        mode = "log"
    elif tier < 4:
        mode = "approve" if action != "none" else "notify"
    else:
        mode = "notify_only"
        action, arg = "none", ""

    return Decision(tier, action, arg, mode, "; ".join(notes))
