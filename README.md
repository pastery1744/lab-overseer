# Lab Overseer

Lab Overseer watches your homelab and messages you on **Telegram** or **Discord** when something breaks. It tells you what's wrong and offers a fix with **Go** and **Stop** buttons. Nothing changes unless you tap Go.

Every minute it:
1. Pings your gateway, DNS, websites and services
2. Asks your hypervisor (Proxmox or ESXi) how the host and every VM are doing
3. Optionally checks your switch ports

When something fails, an AI reads the situation, figures out the likely cause, and suggests **one** safe fix.

---

## Quick start

Run this **one line** as root. It works on a Proxmox host or on any Debian/Ubuntu machine:

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/pastery1744/lab-overseer/main/get.sh)"
```

It works out where it's running and does the right thing:

**On a Proxmox host** it builds a dedicated container for the overseer. Everything happens in simple on-screen menus (arrow keys, Space, Enter):
1. Pick your AI (local or Claude).
2. It shows **recommended settings**, picked automatically for your server. Press **Create it**.
3. The setup wizard asks you to connect Telegram or Discord and tick which VMs to watch.

When it's done you'll get **"👁 Overseer online"** in your chat. Already have one installed? It notices and asks whether to **upgrade it**, **install a separate one**, or **quit**. It never changes anything without asking.

**On a Debian/Ubuntu server** (no VMs needed) it installs right there. Pick **"This server itself"** in the wizard and it watches the machine's CPU, RAM, disks and load, plus whichever services you tick (nginx, docker, databases…). It can restart those services for you, and only those.

**On ESXi:** ESXi can't run containers. Create a small **Debian 12** VM, run the same line inside it, and pick **ESXi** in the wizard.

<details><summary>Prefer not to pipe from the internet?</summary>

```bash
git clone https://github.com/pastery1744/lab-overseer.git && cd lab-overseer
./deploy-proxmox.sh      # on a Proxmox host
sudo ./install.sh        # on Debian/Ubuntu
```
Pin a version with `OVERSEER_REF=v1.0.0` in front of the one-liner.
</details>

---

## What you'll need

- **A place to run it.** The Proxmox script makes one for you. Otherwise use any Debian or Ubuntu machine, including the server you want to watch.
  - With a **local AI** (Ollama): 8+ cores, 12 GB RAM, 32 GB disk
  - With **Claude** (cloud AI): 1–2 cores, 1 GB RAM, 8 GB disk
- **Telegram or Discord.** For Discord, a server you're an admin of.
- **Optional:** a Proxmox or ESXi host, a managed switch with SNMP, an Anthropic API key.

The setup wizard walks you through the few things only you can do: creating the bot, inviting it to Discord, pasting tokens and IDs, and typing passwords.

---

## The setup wizard

The wizard runs automatically during install. Use arrow keys to move, **Space** to tick boxes and **Enter** to confirm. **Back** goes to the previous step. **Esc** pauses, so you can go back, keep going, or quit. It asks, in order:

1. **What to watch.**
   - **This server itself:** for a plain Ubuntu/Debian box. It lists the services running on it with common apps (nginx, docker, databases…) pre-ticked, and watches CPU, RAM, disks and load. Services that could lock you out if restarted (like SSH) are alert-only.
     If **Docker** is installed, it also lists your containers, with running ones pre-ticked. It alerts when a container stops or goes *unhealthy* and can restart it for you. Database containers (Postgres, MySQL, Redis…) are treated as critical.
   - **Proxmox / ESXi:** it connects and shows your VMs with tick boxes, running ones pre-ticked. Then it asks which are **critical** (router, firewall, domain controllers, NAS) and pre-ticks its guesses from their names. Critical ones always need your approval and are never rebooted casually.
   - **Just the network:** pings, websites and ports only.
2. **Chat app.**
   - **Telegram:** make a bot with **@BotFather**, paste the token, then send `/start` to your bot. The wizard grabs your chat ID and locks the bot to you.
   - **Discord:** create a bot at discord.com/developers and paste the token. The wizard prints an invite link you can click, then waits while you add the bot to your server. It lists your channels to pick from and confirms you're the server owner, so there are no IDs to copy. It sends a test message.
3. **AI.** Local Ollama or Claude (it tests your key).
4. **Checks.** It adds your gateway, DNS servers and internet pings automatically. You can add websites and `host:port` services.
5. **Switch** (optional): IP, read-only community string, and which ports to watch.
6. **Heartbeat** (optional): a free [healthchecks.io](https://healthchecks.io) link that warns you if the overseer itself goes offline.

The AI gets a description of your lab built automatically from your answers. Nothing is saved until you confirm on the final review screen, where **No** takes you back to change things. To run it again later, use the `overseer` menu → *Re-run setup*. Your old config is kept as a backup.

---

## Everyday use

### The `overseer` menu
Log into the overseer machine and type **`overseer`**. On Proxmox, get there with `pct enter <CT-ID>`. You get a menu to:
- **Check status now:** every check, pass or fail, plus which AI models are loaded
- **Switch between WATCH and AUTO** mode
- **Send a test alert** to your phone
- **Show what it watches** and what it's allowed to do
- **Let it restart a service** on another machine (walks you through it)
- **Change what the bot says:** its messages, and the AI's tone
- **View logs**, **restart** it, **re-run setup**, or **edit the config file**

### From your phone

| Command | What it does |
|---|---|
| `/status` | Instant overview of the whole lab, then an AI summary of trends and what to keep an eye on |
| `/watch` | **Safe mode (default).** You get alerts; tapping Go only *pretends* to fix |
| `/auto` | **Armed.** Tapping Go really runs the fix |
| `/go A12` / `/stop A12` | Approve or cancel an incident (same as the buttons) |

An alert looks like this:
```
🟠 T2 [K41] pihole: DNS not answering
Why: DNS check failing but the VM is running — likely the DNS service crashed
Tap Go to run.                          [ Go ] [ Stop ]
```
When it's healthy again you'll get **✅ K41 pihole recovered.**

**Severity (T0–T4):**
- **T0:** harmless blips, logged only
- **T1–T3:** real problems, from minor to core infrastructure. Any fix waits for your Go.
- **T4:** looks like a security issue. You're alerted, and it will **never** act on its own, even if you tap Go.

It starts in **watch mode**. Let it run for a few days, see what it *would* have done, then send `/auto`.

---

## How it keeps you safe

- **The AI can't run commands.** It can only pick from a short list of fixes you allowed for each machine. Plain code, not the AI, decides whether anything happens.
- **Only four kinds of fix exist:** start a VM, reboot a VM, restart one named service, or restart one named Docker container. There's no delete, shutdown or config change anywhere in the code.
- **You approve every fix**, and watch mode means even approved fixes are simulated until you switch to `/auto`.
- **Critical machines** (`floor_tier: 3`) are always treated as serious, no matter what the AI thinks.
- **Security-looking issues** are hands-off, always.
- **Repeat protection:** if the same machine needs fixing more than twice an hour, it stops and escalates to you.
- **Locked to you:** the bot ignores everyone except your chat (Telegram) or your user ID (Discord).
- **Minimal access:** the Proxmox account can only view and power VMs on and off. Service access is limited to restarting the exact services you list. For Docker it gets a sudo rule for one read-only listing command plus restarting only your chosen containers. It's never added to the `docker` group, since that's effectively root.
- **Everything is logged:** every AI decision and every action, real or simulated.

---

## Letting it restart services inside a VM

Starting and rebooting VMs works right away. To let it restart a **service** inside a machine (like `nginx` or `pihole-FTL`), the easiest way is the `overseer` menu → *Let it restart a service on another machine*. Or run it directly:

```bash
sudo /opt/overseer/tools/add-service-target.sh <admin-user>@<ip> <service>[,<service>] [name]

# example
sudo /opt/overseer/tools/add-service-target.sh pi@192.168.1.53 pihole-FTL pihole
```

You type that machine's password once. The script creates an `overseer` user there that can **only** restart those services, tests it, updates your config and restarts the overseer.

---

## Changing what the bot says

Every message the bot sends lives in **`/etc/overseer/messages.yaml`**: alerts, `/status`, command replies and icons. The easy way to edit it is the `overseer` menu → *Change what the bot says*.

Everything in the file starts commented out, so you get the built-in wording. To change a message, delete the `# ` in front of it and edit the text:
```yaml
recovered: "🎉 {target} is back up! ({id})"
alert_ask_go: "Want me to {action}?"
icons: {3: "🔥", 4: "☠️"}
```
- Words in `{curly braces}` are filled in automatically. The file lists which ones each message can use. A misspelled one just shows up as-is and won't break anything.
- **`ai_style`** changes how the AI talks, e.g. `ai_style: "Be casual and a bit sarcastic."` or `"Reply in Spanish."` It affects tone only. It can't change the safety rules or the alert format.
- Updates never overwrite this file. If you break its formatting, the bot ignores it and uses the defaults (the menu warns you).

---

## Changing settings

For most things, just use the **`overseer` menu**. For finer control, settings live in `/etc/overseer/config.yaml`. Passwords and tokens live separately in `/etc/overseer/secrets.env`. After editing, run `sudo systemctl restart overseer`.

Every option is explained in [`config.example.yaml`](config.example.yaml). The ones you're most likely to touch:

**Targets** are the machines it's allowed to fix:
```yaml
targets:
  router:
    kind: vm
    vmid: 100              # Proxmox ID (on ESXi use vmname: "router")
    floor_tier: 3          # always treat problems here as serious
    actions: [start_vm]    # the ONLY fixes allowed
    notes: "pfSense — everything depends on it"
```
Possible actions: `start_vm`, `reboot_vm`, `start_ct`, `reboot_ct`, `restart_service:<name>`. For VMs that are normally off, add `expect_running: false`.

**Checks** are the things it pings:
```yaml
checks:
  - {name: gateway, type: ping, host: 192.168.1.1,  target: router}
  - {name: dns,     type: dns,  server: 192.168.1.53, query: cloudflare.com, target: pihole}
  - {name: site,    type: http, url: https://example.com, target: web}
  - {name: ldap,    type: tcp,  host: 192.168.1.30, port: 389, target: dc01}
```
`target` tells it which machine to blame when that check fails. Add `disabled: true` to pause a check. Full disks (over 85%), low host memory and stopped VMs are detected automatically.

**This server (no VMs):**
```yaml
local:
  services: [nginx, docker, postgresql]   # systemd units to watch
  disks: ["/", "/srv"]                   # warn above 85% full
  containers: [web, db]                  # Docker containers to watch
targets:
  nginx: {kind: service, local: true, actions: ["restart_service:nginx"]}
  web:   {kind: container, local: true, actions: ["restart_container:web"]}
```
After editing local services by hand, run `overseer` → *Restart*. That also refreshes the rule that lets it restart exactly those services.

**Switch:** see the `switch:` section of the example config. Port names must match what the switch reports, like `Gi1/0/1` or `Po1`.

---

## Local AI or Claude?

| | Ollama (local) | Claude (cloud) |
|---|---|---|
| Cost | Free | Usually a few dollars a month |
| Privacy | Stays on your network | Lab status is sent to Anthropic |
| Speed | 15 s to 2 min on CPU | A few seconds |
| Smarts | Good | Better with tricky problems |

Locally, it uses two small models that stay loaded in memory: `qwen2.5:7b-instruct` for diagnosing problems and `llama3.2:3b` for `/status` summaries. More CPU cores means faster answers.

**Not sure? Compare them on your own incidents.** Every decision is logged, so you can replay them through Claude and see how it would have answered:
```bash
cd /opt/overseer && sudo -u overseer bash -c 'set -a; . /etc/overseer/secrets.env; venv/bin/python -m overseer.replay -c /etc/overseer/config.yaml --backend claude --last 50'
```
To switch, set `llm: backend: claude` in the config, add `ANTHROPIC_API_KEY=` to secrets.env, and restart.

---

## Maintenance

- **Update:** run the same one-line command again. On Proxmox, choose **Upgrade**. Your settings are kept, and older config formats are upgraded automatically.
- **Logs:** `journalctl -u overseer -f`. On Proxmox: `pct exec <CT-ID> -- journalctl -u overseer -f`
- **Re-run setup:** `overseer` → *Re-run setup*
- **Uninstall:**
  - On Proxmox, delete the container, then run `pveum user delete overseer@pve` and `pveum role delete OverseerRole`.
  - Elsewhere, run `systemctl disable --now overseer ollama` and delete `/opt/overseer`, `/etc/overseer` and `/var/lib/overseer`.
  - On any machine you added with `add-service-target.sh`, remove the `overseer` user and `/etc/sudoers.d/overseer`.

---

## Troubleshooting

- **Bot doesn't reply:** check the logs.
  - *401 Unauthorized* means the token got cut off when pasted. Re-paste it into secrets.env.
  - If the chat ID is empty, message the bot. It will reply with your ID.
  - Quick check: `overseer` → *Send a test alert*.
- **Replies are slow, or "Analyst gave up":** the AI is busy. Give the container more CPU cores, or use Claude. `ollama ps` should show both models with *Forever*.
- **"hypervisor-api: 401":** the Proxmox token is wrong. Make a new one (`pveum user token add overseer@pve new --privsep 0`) and update `token_id` and `PVE_TOKEN`.
- **VM memory shows ~100%:** normal for VMs without guest tools. It's how the host sees them.
- **Port-channel shows unhealthy:** some switches report its speed as 0. Remove `expect_speed` for that port.
- **Discord slash commands missing:** re-invite the bot with the wizard's link and double-check the server ID.
- **Discord says "Not yours.":** `owner_id` must be *your* user ID, not the bot's.

---

## How big can it go?

It's built for homelabs, and scales comfortably to:
- **Proxmox clusters:** every node and every VM, found automatically. Fixes are sent to whichever node the VM is on right now, even after a migration.
- **Several ESXi hosts** behind vCenter.
- **Hundreds of checks:** they all run at the same time, so even 200 unreachable hosts only take a few seconds.
- **100+ VMs with a local AI:** on big labs it only shows the AI what matters (failing machines, critical ones, the busiest few), so answers stay quick.

One overseer watches one lab. For a second site, install a second overseer.

---

## Known limits

- **It can't warn you if your router or internet is down**, because it needs them to send messages. Set up the free heartbeat so an outside service warns you instead.
- **ESXi support is newer** and less battle-tested than Proxmox.
- **It doesn't read logs yet.** It spots problems from checks and metrics, so security alerts are only as good as those checks.
- **It watches your router from the outside.** It doesn't inspect firewall rules or DHCP leases.

---

## What's in this folder

| File | Purpose |
|---|---|
| `get.sh` | The one-line installer (downloads this repo, picks the right installer) |
| `deploy-proxmox.sh` | One-command Proxmox install (run on the host) |
| `install.sh` | Installer for any Debian/Ubuntu machine |
| `setup.py` | The setup wizard |
| `tools/overseer-menu.py` | The `overseer` control menu |
| `config.example.yaml` | Every setting, explained |
| `messages.example.yaml` | Every message the bot sends, ready to customize |
| `tools/add-service-target.sh` | Allow restarting a service on another machine |
| `overseer/` | The app itself (`policy.py` holds the safety rules) |
| `tests/` | Automated tests: `python -m pytest tests` |

---

MIT licensed — see [LICENSE](LICENSE).
