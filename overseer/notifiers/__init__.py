def make_notifier(cfg, state):
    """cfg['notifier'] = {type: telegram|discord, ...}"""
    n = cfg.get("notifier") or {}
    if n.get("type") == "discord":
        from .discord_bot import Discord
        return Discord(n, state)
    from .telegram import Telegram
    return Telegram(n, state)
