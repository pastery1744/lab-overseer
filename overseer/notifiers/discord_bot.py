"""Discord bot: slash commands + Go/Stop buttons. Uses only non-privileged intents (no message content)."""
import asyncio, logging, os, re, threading

log = logging.getLogger("discord")
BTN = re.compile(r"^(GO|STOP) ([A-Z]\d{2})$")


class Discord:
    def __init__(self, cfg, state):
        self.token = os.environ.get(cfg.get("token_env", "DISCORD_TOKEN"), "")
        self.channel_id = int(cfg.get("channel_id") or 0)
        self.owner_id = int(cfg.get("owner_id") or 0)
        self.guild_id = int(cfg.get("guild_id") or 0)
        self.state = state
        self.loop = None
        self.client = None
        self.ready = threading.Event()

    # ---- outbound (called from any thread) ----
    def send(self, text, incident_id=None, mode=None):
        log.info("DC -> %s", text.replace("\n", " | "))
        if not self.ready.wait(timeout=30):
            log.error("discord not ready; dropped message")
            return False

        async def _send():
            import discord
            ch = self.client.get_channel(self.channel_id) or await self.client.fetch_channel(self.channel_id)
            view = None
            if incident_id and mode in ("grace", "approve"):
                view = discord.ui.View(timeout=None)
                if mode == "approve":
                    view.add_item(discord.ui.Button(label="Go", style=discord.ButtonStyle.success, custom_id=f"GO {incident_id}"))
                view.add_item(discord.ui.Button(label="Stop", style=discord.ButtonStyle.danger, custom_id=f"STOP {incident_id}"))
            await ch.send(text[:1990], view=view)

        try:
            asyncio.run_coroutine_threadsafe(_send(), self.loop).result(timeout=20)
            return True
        except Exception as e:
            log.error("discord send failed: %s", e)
            return False

    # ---- inbound ----
    def start(self, on_command):
        if not self.token:
            log.error("DISCORD_TOKEN not set")
            return
        threading.Thread(target=self._run, args=(on_command,), daemon=True).start()

    def _run(self, on_command):
        import discord
        from discord import app_commands

        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        client = discord.Client(intents=discord.Intents.default())
        tree = app_commands.CommandTree(client)
        self.client = client
        guild = discord.Object(id=self.guild_id) if self.guild_id else None

        def allowed(inter):
            return inter.user.id == self.owner_id

        async def dispatch(inter, verb, iid=None):
            if not allowed(inter):
                return await inter.response.send_message("Not yours.", ephemeral=True)
            await inter.response.send_message("👍", ephemeral=True)
            # on_command can block (locks, SSH); keep it off the event loop
            await self.loop.run_in_executor(None, on_command, verb, iid, f"/{verb.lower()} {iid or ''}")

        @tree.command(name="status", description="Lab overview + analyst read", guild=guild)
        async def _status(inter: discord.Interaction):
            await dispatch(inter, "STATUS")

        @tree.command(name="auto", description="Armed: approved fixes actually run", guild=guild)
        async def _auto(inter: discord.Interaction):
            await dispatch(inter, "AUTO")

        @tree.command(name="watch", description="Safe: approved fixes are only simulated", guild=guild)
        async def _watch(inter: discord.Interaction):
            await dispatch(inter, "WATCH")

        @tree.command(name="go", description="Approve an incident's fix", guild=guild)
        @app_commands.describe(incident="Incident ID, e.g. A12")
        async def _go(inter: discord.Interaction, incident: str):
            await dispatch(inter, "GO", incident.upper())

        @tree.command(name="stop", description="Cancel an incident's fix", guild=guild)
        @app_commands.describe(incident="Incident ID, e.g. A12")
        async def _stop(inter: discord.Interaction, incident: str):
            await dispatch(inter, "STOP", incident.upper())

        @client.event
        async def on_ready():
            try:
                await tree.sync(guild=guild)
            except Exception as e:
                log.error("slash command sync failed: %s", e)
            log.info("discord ready as %s", client.user)
            self.ready.set()

        @client.event
        async def on_interaction(inter: discord.Interaction):
            # buttons survive restarts because we route by custom_id here instead of per-message View callbacks
            if inter.type != discord.InteractionType.component:
                return
            m = BTN.match((inter.data or {}).get("custom_id", ""))
            if not m:
                return
            if not allowed(inter):
                return await inter.response.send_message("Not yours.", ephemeral=True)
            await inter.response.defer()
            await self.loop.run_in_executor(None, on_command, m.group(1), m.group(2), m.group(0))

        self.loop.run_until_complete(client.start(self.token))
