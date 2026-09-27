"""Telegram bot: outbound alerts + long-polling for commands. No public webhook needed."""
import logging, os, re, threading, time
import requests

log = logging.getLogger("telegram")
CMD = re.compile(r"^/?(GO|YES|STOP|NO|STATUS|DRYRUN|LIVE|AUTO|WATCH|HELP|START)\b\s*([A-Za-z]\d{2})?", re.I)


def parse_command(text):
    m = CMD.match((text or "").strip())
    if not m:
        return None, None
    verb = m.group(1).upper()
    verb = {"YES": "GO", "NO": "STOP", "START": "HELP", "LIVE": "AUTO", "DRYRUN": "WATCH"}.get(verb, verb)
    return verb, (m.group(2) or "").upper() or None


class Telegram:
    def __init__(self, cfg, state):
        self.token = os.environ.get(cfg.get("token_env", "TG_TOKEN"), "")
        self.chat_id = str(cfg.get("chat_id", ""))
        self.base = f"https://api.telegram.org/bot{self.token}"
        self.state = state

    def _call(self, method, **params):
        if not self.token:
            return None
        try:
            r = requests.post(f"{self.base}/{method}", json=params, timeout=40)
            j = r.json()
            if not j.get("ok"):
                log.error("TG %s failed: %s", method, str(j)[:300])
            return j
        except requests.RequestException as e:
            log.error("TG %s error: %s", method, e)
            return None

    def send(self, text, incident_id=None, mode=None):
        log.info("TG -> %s", text.replace("\n", " | "))
        if not self.chat_id:
            return False
        p = {"chat_id": self.chat_id, "text": text[:4000], "disable_web_page_preview": True}
        if incident_id and mode in ("grace", "approve"):
            btns = [{"text": "🛑 Stop", "callback_data": f"STOP {incident_id}"}]
            if mode == "approve":
                btns.insert(0, {"text": "✅ Go", "callback_data": f"GO {incident_id}"})
            p["reply_markup"] = {"inline_keyboard": [btns]}
        j = self._call("sendMessage", **p)
        return bool(j and j.get("ok"))

    def _handle_update(self, u, on_command):
        if "callback_query" in u:
            cq = u["callback_query"]
            self._call("answerCallbackQuery", callback_query_id=cq["id"])
            chat, text = str(cq.get("message", {}).get("chat", {}).get("id")), cq.get("data", "")
        else:
            msg = u.get("message") or {}
            chat, text = str(msg.get("chat", {}).get("id")), msg.get("text", "")
        if not self.chat_id:
            # first-run helper: log whoever messages so you can copy the chat_id into config
            log.warning("Message from chat_id %s — set telegram.chat_id to this to claim the bot", chat)
            self._call("sendMessage", chat_id=chat, text=f"Unclaimed. Your chat_id is {chat} — set it as telegram.chat_id and restart.")
            return
        if chat != self.chat_id:
            log.warning("ignored message from unknown chat %s", chat)
            return
        verb, iid = parse_command(text)
        on_command(verb, iid, text)

    def poll_forever(self, on_command):
        offset = int(self.state.meta_get("tg_offset", 0))
        while True:
            j = self._call("getUpdates", offset=offset, timeout=30, allowed_updates=["message", "callback_query"])
            if not j or not j.get("ok"):
                time.sleep(10)
                continue
            for u in j["result"]:
                offset = u["update_id"] + 1
                self.state.meta_set("tg_offset", offset)
                try:
                    self._handle_update(u, on_command)
                except Exception:
                    log.exception("update handling failed")

    def start(self, on_command):
        if self.token:
            self._call("setMyCommands", commands=[
                {"command": "status", "description": "Lab overview"},
                {"command": "auto", "description": "Armed: approved fixes actually run"},
                {"command": "watch", "description": "Safe: even approved fixes are only simulated"},
                {"command": "go", "description": "Approve an incident: /go A12"},
                {"command": "stop", "description": "Cancel an incident: /stop A12"}])
            threading.Thread(target=self.poll_forever, args=(on_command,), daemon=True).start()
