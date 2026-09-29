"""Every sentence the bot says, in one place. Override any of them in /etc/overseer/messages.yaml.

Placeholders like {id} are filled in automatically — see messages.example.yaml for what each message can use.
Unknown or misspelled placeholders are left as-is instead of crashing.
"""
import logging, os
import yaml

log = logging.getLogger("messages")

DEFAULTS = {
    # ---- tier icons (0 = noise … 4 = security) ----
    "icons": {0: "·", 1: "🟡", 2: "🟠", 3: "🔴", 4: "🚨"},

    # ---- lifecycle ----
    "online": "👁 Overseer online ({ai}, {mode}).",
    "help": "Commands: /status · /auto · /watch · /go <id> · /stop <id>",
    "mode_auto": "🤖 AUTO — fixes you approve with Go will actually run",
    "mode_watch": "👀 WATCH — alerts only; Go just simulates",

    # ---- incidents ----
    "alert": "{icon} T{tier} [{id}] {target}: {summary}",
    "alert_why": "Why: {reason}",
    "alert_policy": "Policy: {policy}",
    "alert_ask_go": "Proposed: {action}. Tap Go / Stop.",
    "alert_hands_off": "Not touching this. Your call.",
    "alert_watch_note": " (watch mode — won't run)",
    "recovered": "✅ {id} {target} recovered.",
    "action_ok": "✅ {id} {action} on {target}: {detail}",
    "action_failed": "❌ {id} {action} on {target}: {detail}",
    "cancelled": "✋ {id} cancelled. Still watching {target}.",
    "no_pending": "No pending action {id}",

    # ---- /status ----
    "status_header": "👁 Overseer · {mode} · {ai}",
    "status_no_data": "No snapshot yet — first sweep still running.",
    "status_checks": "{dot} {ok}/{total} checks OK · {age}s ago",
    "status_host": "🖥 {name}: CPU {cpu}% · RAM {ram}% · up {days}d",
    "status_node": "   · {name}: CPU {cpu}% · RAM {ram}%",
    "status_guests": "📦 Guests: {running}/{total} running{down}",
    "status_top_cpu": "🔥 Top CPU: {list}",
    "status_top_ram": "🧮 Top RAM: {list}",
    "status_storage": "💾 {list}",
    "status_containers": "🐳 Containers: {running}/{total} healthy{bad}",
    "status_switch": "🔌 Switch: {ok}/{total} watched ports healthy",
    "status_wan": "🌐 WAN ping: {list}",
    "status_failing": "  ✗ {name}: {detail}",
    "status_incidents": "🧾 {open} open · {day} in last 24h",
    "status_no_incidents": "🧾 No incidents in last 24h",
    "status_incident": "  {icon} {id} {target}: {summary} [{status}]",
    "status_llm": "🧠 Last analyst run {minutes} min ago",
    "status_llm_none": "🧠 No analyst run yet (send /status to ask)",

    # ---- analyst ----
    "analyst_thinking": "🧠 Analyst thinking…",
    "analyst_busy": "🧠 Analyst is already on it…",
    "analyst_skipped": "🧠 Analyst skipped — incident triage has the LLM right now. Try again shortly.",
    "analyst_reply": "🧠 Analyst ({secs}s):\n{text}",
    "analyst_failed": "🧠 Analyst gave up after {secs}s (LLM busy or slow) — overview above is current.",

    # ---- AI personality (added to the AI's instructions; safety rules can't be changed here) ----
    "ai_style": "",          # e.g. "Be casual and a little sarcastic." or "Reply in Spanish."
}


class _Safe(dict):
    def __missing__(self, k):
        return "{" + k + "}"


class Messages:
    def __init__(self, path=None):
        self.m = {k: (dict(v) if isinstance(v, dict) else v) for k, v in DEFAULTS.items()}
        self.path = path
        if path and os.path.exists(path):
            try:
                user = yaml.safe_load(open(path)) or {}
                for k, v in user.items():
                    if k not in DEFAULTS:
                        log.warning("messages.yaml: unknown key %r ignored", k)
                    elif k == "icons" and isinstance(v, dict):
                        self.m["icons"].update({int(i): str(x) for i, x in v.items()})
                    elif v is not None:
                        self.m[k] = str(v)
            except Exception as e:
                log.error("messages.yaml unreadable, using defaults: %s", e)

    def __call__(self, key, **kw):
        try:
            return self.m[key].format_map(_Safe(kw))
        except Exception:
            return self.m[key]

    def icon(self, tier):
        return self.m["icons"].get(int(tier), "•")

    @property
    def ai_style(self):
        return (self.m.get("ai_style") or "").strip()
