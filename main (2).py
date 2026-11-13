"""
Cruxer - Helper Warning bot
Watches the backup tickets, reads the "Help Registered" messages, checks every helper's Roblox presence with your
Roblox cookie and posts warnings when a helper never actually joins.

  5 min after registering  -> not online on Roblox                 -> warning
  7 min after registering  -> not in the requested game            -> warning
  backup result is posted  -> never joined the game                -> warning + red summary embed

Requires: discord.py >= 2.6, aiohttp, python-dotenv
Run:      py main.py
Needs the MESSAGE CONTENT INTENT enabled in the Developer Portal (Bot tab).
"""
import asyncio
import json
import os
import re
import subprocess
import sys
import time


def _bootstrap():
    """Install missing libraries automatically (for hosts that skip requirements.txt)."""
    try:
        import aiohttp, dotenv  # noqa: F401
        import discord
        if hasattr(discord.ui, "LayoutView"):
            return
    except ImportError:
        pass
    pkgs = ["discord.py>=2.6.0", "aiohttp>=3.10", "python-dotenv>=1.0"]
    base = [sys.executable, "-m", "pip", "install", "--quiet", "--upgrade"] + pkgs
    print("Installing required libraries...")
    try:
        subprocess.check_call(base)
    except subprocess.CalledProcessError:
        subprocess.check_call(base + ["--break-system-packages"])


_bootstrap()

import aiohttp  # noqa: E402
import discord  # noqa: E402
from discord import app_commands  # noqa: E402
from discord.ext import tasks  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

load_dotenv()


# ============================================================ config
def env_int(name: str, default: int = 0) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
ROBLOX_COOKIE = os.getenv("ROBLOX_COOKIE", "").strip().strip('"')
if ROBLOX_COOKIE.lower().startswith(".roblosecurity="):
    ROBLOX_COOKIE = ROBLOX_COOKIE.split("=", 1)[1]

OWNER_ID = env_int("OWNER_ID")
TICKET_CATEGORY_ID = env_int("TICKET_CATEGORY_ID", 1507716131551707258)   # the category the tickets are made in
TRACKED_BOT_ID = env_int("TRACKED_BOT_ID", 1470577242676531372)           # the bot whose messages are read
RESULT_CHANNEL_ID = env_int("RESULT_CHANNEL_ID", 1562782174137159792)     # a result posted here = the backup ended
WARN_CHANNEL_ID = env_int("WARN_CHANNEL_ID", 0)                           # 0 = post warnings inside the ticket

OFFLINE_MINUTES = env_int("OFFLINE_MINUTES", 5)
NOT_JOINED_MINUTES = env_int("NOT_JOINED_MINUTES", 7)
MAX_WARNINGS = env_int("MAX_WARNINGS", 15)
POLL_SECONDS = max(15, env_int("POLL_SECONDS", 30))
MAX_TICKET_HOURS = env_int("MAX_TICKET_HOURS", 6)       # a ticket with no result after this long is dropped silently
RECOVER_HOURS = env_int("RECOVER_HOURS", 3)             # on start-up only help messages newer than this are picked up
TICKET_LABEL = os.getenv("TICKET_LABEL", "").strip() or "Backup-ticket"
BOT_NAME = os.getenv("BOT_NAME", "").strip() or "Cruxer"

YELLOW, RED = 0xF1C40F, 0xE74C3C
NO_PING = discord.AllowedMentions.none()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.getenv("DATA_DIR", "").strip() or os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "").strip() or BASE_DIR
os.makedirs(DATA_DIR, exist_ok=True)
STATE_FILE = os.path.join(DATA_DIR, "cruxer_state.json")


# ============================================================ saved state
# state["warnings"] = {discord_id: count}
# state["tickets"]  = {channel_id: {number, name, created, job, place, ended, helpers: {discord_id: {...}}}}
state = {"warnings": {}, "tickets": {}}
try:
    with open(STATE_FILE, "r", encoding="utf-8") as _fh:
        loaded = json.load(_fh)
    state["warnings"] = loaded.get("warnings") or {}
    state["tickets"] = loaded.get("tickets") or {}
except FileNotFoundError:
    pass
except Exception as exc:
    print(f"Could not read {STATE_FILE}, starting fresh: {exc!r}")


def save():
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh)
    os.replace(tmp, STATE_FILE)


LOCK = asyncio.Lock()


# ============================================================ roblox
class Roblox:
    """Username -> id lookups and presence checks. The cookie is only sent to roblox.com."""
    def __init__(self, session: aiohttp.ClientSession):
        self.s = session
        self.csrf = ""
        self.ids = {}

    def _headers(self):
        h = {"Content-Type": "application/json"}
        if ROBLOX_COOKIE:
            h["Cookie"] = f".ROBLOSECURITY={ROBLOX_COOKIE}"
        if self.csrf:
            h["x-csrf-token"] = self.csrf
        return h

    async def _post(self, url, body):
        for _ in range(2):   # the first try may only fetch the CSRF token
            try:
                async with self.s.post(url, json=body, headers=self._headers(),
                                       timeout=aiohttp.ClientTimeout(total=15)) as r:
                    if r.status == 403 and r.headers.get("x-csrf-token"):
                        self.csrf = r.headers["x-csrf-token"]
                        continue
                    if r.status != 200:
                        print(f"Roblox {url} answered {r.status}")
                        return None
                    return await r.json(content_type=None)
            except Exception as exc:
                print(f"Roblox request failed: {exc!r}")
                return None
        return None

    async def whoami(self):
        try:
            async with self.s.get("https://users.roblox.com/v1/users/authenticated", headers=self._headers(),
                                  timeout=aiohttp.ClientTimeout(total=15)) as r:
                if r.status == 200:
                    return (await r.json(content_type=None)).get("name")
        except Exception as exc:
            print(f"Roblox login check failed: {exc!r}")
        return None

    async def user_id(self, name: str):
        key = name.lower()
        if key in self.ids:
            return self.ids[key]
        data = await self._post("https://users.roblox.com/v1/usernames/users",
                                {"usernames": [name], "excludeBannedUsers": False})
        try:
            uid = int(data["data"][0]["id"])
        except Exception:
            return None
        self.ids[key] = uid
        return uid

    async def presence(self, ids):
        """{roblox_id: presence dict}, or None when Roblox could not be reached (never treated as offline)."""
        out = {}
        ids = list(dict.fromkeys(int(i) for i in ids))
        for i in range(0, len(ids), 100):
            data = await self._post("https://presence.roblox.com/v1/presence/users", {"userIds": ids[i:i + 100]})
            if not data:
                return None
            for p in data.get("userPresences", []):
                out[int(p["userId"])] = p
        return out


# ============================================================ reading the tracked bot's messages
def components_text(components) -> str:
    """All text (and button links) of a Components V2 message."""
    out = []

    def walk(c):
        content = getattr(c, "content", None)
        if isinstance(content, str):
            out.append(content)
        url = getattr(c, "url", None)
        if isinstance(url, str):
            out.append(url)
        for ch in getattr(c, "children", None) or []:
            walk(ch)
        acc = getattr(c, "accessory", None)
        if acc is not None:
            walk(acc)

    for c in components or []:
        walk(c)
    return "\n".join(out)


def message_text(message: discord.Message) -> str:
    parts = [message.content or ""]
    for e in message.embeds:
        parts += [e.title or "", e.description or "", e.url or ""]
        for f in e.fields:
            parts += [f.name or "", f.value or ""]
    parts.append(components_text(message.components))
    return "\n".join(p for p in parts if p).replace("\\", "")


HELP_RE = re.compile(r"help\s+registered", re.I)
MENTION_RE = re.compile(r"<@!?(\d+)>")
ROBLOX_NAME_RE = re.compile(r"Roblox\W{0,6}([A-Za-z0-9_]{3,20})", re.I)
ROBLOX_ID_RE = re.compile(r"roblox\.com/users/(\d+)", re.I)


def parse_help(text: str):
    """'@D3 | Piro has confirmed for help. Roblox: Dio_D3' -> (discord_id, roblox_name, roblox_id) or None."""
    if not HELP_RE.search(text):
        return None
    m = MENTION_RE.search(text)
    n = ROBLOX_NAME_RE.search(text)
    i = ROBLOX_ID_RE.search(text)
    if not m or not (n or i):
        return None
    return int(m.group(1)), (n.group(1) if n else None), (int(i.group(1)) if i else None)


def ticket_number(name: str):
    m = re.search(r"(\d{1,6})\D*$", name or "")
    return int(m.group(1)) if m else None


def numbers_in(text: str) -> set:
    """Ticket numbers mentioned in a result message: '#0332' first, any 3-5 digit number as a fallback."""
    nums = {int(x) for x in re.findall(r"#\s*(\d{3,6})(?!\d)", text)}
    if not nums:
        nums = {int(x) for x in re.findall(r"(?<![\d:])(\d{3,5})(?![\d:])", text)}
    return nums


def label(t) -> str:
    return f"{TICKET_LABEL} #{t['number']:04d}" if t.get("number") is not None else t["name"]


# ============================================================ tickets
def get_ticket(chan, create=True):
    key = str(chan.id)
    t = state["tickets"].get(key)
    if t or not create:
        return t
    t = {"channel_id": chan.id, "name": chan.name, "number": ticket_number(chan.name),
         "created": chan.created_at.timestamp(), "job": None, "place": None,
         "ended": False, "ended_at": 0, "helpers": {}}
    state["tickets"][key] = t
    save()
    return t


def learn_server(t, text: str):
    """Remember the requested server (job id / place id) when a link to it shows up in the ticket."""
    changed = False
    m = re.search(r"gameInstanceId=([0-9a-fA-F-]{32,40})", text)
    if m and not t.get("job"):
        t["job"], changed = m.group(1), True
    p = re.search(r"placeId=(\d+)", text)
    if p and not t.get("place"):
        t["place"], changed = p.group(1), True
    if changed:
        save()


def register_helper(t, uid: int, rb_name, rb_id, when: float) -> bool:
    key = str(uid)
    if key in t["helpers"]:
        return False
    t["helpers"][key] = {
        "discord_id": uid, "roblox": rb_name or "unknown", "roblox_id": rb_id, "tries": 0, "t": when,
        "seen_online": False, "seen_game": False, "w5": False, "w7": False, "wend": False,
    }
    save()
    print(f"[{label(t)}] helper registered: {uid} / {rb_name}")
    return True


def in_requested_server(t, p) -> bool:
    job, gid = (t.get("job") or "").lower(), (p.get("gameId") or "").lower()
    if job and gid and job != gid:
        return False
    place = str(t.get("place") or "")
    places = {str(p.get("placeId") or ""), str(p.get("rootPlaceId") or "")} - {""}
    if place and places and place not in places:
        return False
    return True


def apply_presence(t, h, p):
    ptype = int(p.get("userPresenceType") or 0)   # 0 offline, 1 online, 2 in game, 3 in studio
    if ptype >= 1:
        h["seen_online"] = True
    if ptype == 2 and in_requested_server(t, p):
        h["seen_game"] = True


# ============================================================ warnings
async def post_embed(t, embed: discord.Embed):
    cid = WARN_CHANNEL_ID or t["channel_id"]
    try:
        chan = client.get_channel(cid) or await client.fetch_channel(cid)
        await chan.send(embed=embed, allowed_mentions=NO_PING)
    except discord.HTTPException as exc:
        print(f"Could not post a warning in {cid}: {exc!r}")


async def warn(t, h, reason: str):
    uid = h["discord_id"]
    count = state["warnings"].get(str(uid), 0) + 1
    state["warnings"][str(uid)] = count
    save()
    text = (f"Member: <@{uid}>\n"
            f"Ticket: {label(t)}\n"
            f"Reason: {reason}\n"
            f"Warnings: `{count}/{MAX_WARNINGS}`.")
    if count >= MAX_WARNINGS:
        text += "\n**Warning limit reached.**"
    embed = discord.Embed(title="Helper Warning", description=text, colour=YELLOW)
    embed.set_footer(text=BOT_NAME)
    await post_embed(t, embed)
    print(f"[{label(t)}] warning {count}/{MAX_WARNINGS} for {uid}: {reason}")


async def check_helpers(t, helpers, fetched: dict, now: float):
    """Apply fresh presence, then fire the 5 minute and 7 minute warnings."""
    for h in helpers:
        p = fetched.get(h["roblox_id"])
        if p is None:
            continue
        apply_presence(t, h, p)
        elapsed = now - h["t"]
        if not h["w5"] and elapsed >= OFFLINE_MINUTES * 60:
            h["w5"] = True
            if not h["seen_online"]:
                await warn(t, h, "Offline on Roblox")
        if not h["w7"] and elapsed >= NOT_JOINED_MINUTES * 60:
            h["w7"] = True
            if not h["seen_game"]:
                await warn(t, h, f"Not in the requested Roblox server after {NOT_JOINED_MINUTES} minutes")


async def resolve_ids(helpers):
    for h in helpers:
        if h["roblox_id"] or not h["roblox"] or h["tries"] >= 10:
            continue
        h["tries"] += 1
        h["roblox_id"] = await client.roblox.user_id(h["roblox"])


async def end_ticket(t, silent=False):
    """The backup is over. Final check, final warnings, red summary embed."""
    async with LOCK:
        if t["ended"]:
            return
        t["ended"], t["ended_at"] = True, time.time()
        if silent:
            save()
            return
        pending = [h for h in t["helpers"].values() if not h["seen_game"] and not h["wend"]]
        await resolve_ids(pending)
        ids = [h["roblox_id"] for h in pending if h["roblox_id"]]
        fetched = await client.roblox.presence(ids) if ids else {}
        if fetched:
            for h in pending:
                p = fetched.get(h["roblox_id"])
                if p:
                    apply_presence(t, h, p)
        offenders = []
        for h in pending:
            if h["seen_game"]:
                continue
            if not h["roblox_id"]:
                print(f"[{label(t)}] could not check {h['roblox']} (unknown Roblox user), no warning given")
                continue
            h["wend"] = True
            reason = "Ticket ended before joining the requested Roblox server"
            if not h["seen_online"]:
                reason += " (offline)"
            await warn(t, h, reason)
            offenders.append(h)
        if offenders:
            lines = []
            for h in offenders:
                who = f"[{h['roblox']}](https://www.roblox.com/users/{h['roblox_id']}/profile)"
                state_text = "was online, but not in the requested server" if h["seen_online"] \
                    else "was offline, not in the requested server"
                lines.append(f"• <@{h['discord_id']}> – {who} {state_text}")
            embed = discord.Embed(
                title=f"Offline Helper Warning - {label(t)}",
                description="These helpers joined this backup but were still not in the game when it ended:\n"
                            + "\n".join(lines),
                colour=RED)
            embed.set_footer(text=BOT_NAME)
            await post_embed(t, embed)
        save()


# ============================================================ watching loop
@tasks.loop(seconds=POLL_SECONDS)
async def poll_loop():
    try:
        async with LOCK:
            now = time.time()
            for key in [k for k, t in state["tickets"].items() if t["ended"] and now - t["ended_at"] > 2 * 86400]:
                state["tickets"].pop(key, None)
            work = []
            for t in state["tickets"].values():
                if t["ended"]:
                    continue
                if now - t["created"] > MAX_TICKET_HOURS * 3600:
                    t["ended"], t["ended_at"] = True, now
                    continue
                pend = [h for h in t["helpers"].values() if not h["seen_game"] and not h["wend"]]
                if pend:
                    await resolve_ids(pend)
                    work.append((t, pend))
            ids = [h["roblox_id"] for _, pend in work for h in pend if h["roblox_id"]]
            if ids:
                fetched = await client.roblox.presence(ids)
                if fetched is not None:   # Roblox unreachable = nobody is judged this round
                    for t, pend in work:
                        await check_helpers(t, pend, fetched, now)
            save()
    except Exception as exc:
        print(f"Poll error: {exc!r}")


# ============================================================ discord side
def in_tickets(channel) -> bool:
    return getattr(channel, "category_id", None) == TICKET_CATEGORY_ID


def find_ticket(text: str):
    for cid in re.findall(r"<#(\d+)>", text):
        t = state["tickets"].get(cid)
        if t and not t["ended"]:
            return t
    nums = numbers_in(text)
    found = [t for t in state["tickets"].values() if not t["ended"] and t.get("number") in nums]
    return max(found, key=lambda t: t["created"]) if found else None


async def handle_message(message: discord.Message):
    if message.guild is None:
        return
    chan = message.channel
    tracked = message.author.id == TRACKED_BOT_ID

    if in_tickets(chan):
        text = message_text(message)
        has_link = "gameInstanceId=" in text
        if not (tracked or has_link):
            return
        t = get_ticket(chan)
        if t["ended"]:
            return
        learn_server(t, text)
        if tracked:
            reg = parse_help(text)
            if reg:
                register_helper(t, reg[0], reg[1], reg[2], message.created_at.timestamp())
    elif chan.id == RESULT_CHANNEL_ID and tracked:
        text = message_text(message)
        t = find_ticket(text)
        if t:
            print(f"Result posted for {label(t)}, ending it.")
            await end_ticket(t)
        else:
            print(f"A result was posted but no open ticket matched it: {text[:150]!r}")


class Cruxer(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.session = None
        self.roblox = None
        self._ready_once = False

    async def setup_hook(self):
        self.session = aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar())
        self.roblox = Roblox(self.session)
        try:
            await self.tree.sync()
        except discord.HTTPException as exc:
            print(f"Could not sync slash commands: {exc!r}")

    async def close(self):
        if self.session:
            await self.session.close()
        await super().close()

    async def on_ready(self):
        print(f"Cruxer online as {self.user} ({self.user.id})")
        if self._ready_once:
            return
        self._ready_once = True
        if not ROBLOX_COOKIE:
            print("WARNING: ROBLOX_COOKIE is empty, presence checks will not work.")
        else:
            who = await self.roblox.whoami()
            print(f"Roblox cookie OK, logged in as {who}." if who else
                  "WARNING: the Roblox cookie was rejected (expired or wrong). Get a fresh .ROBLOSECURITY value.")
        try:
            await recover()
        except Exception as exc:
            print(f"Start-up recovery failed: {exc!r}")
        poll_loop.start()

    async def on_message(self, message: discord.Message):
        try:
            await handle_message(message)
        except Exception as exc:
            print(f"Message handler error: {exc!r}")

    async def on_message_edit(self, before, after):
        try:
            await handle_message(after)
        except Exception as exc:
            print(f"Edit handler error: {exc!r}")

    async def on_guild_channel_delete(self, channel):
        t = state["tickets"].get(str(channel.id))
        if t and not t["ended"]:   # a deleted ticket is over, no end-of-backup warnings
            await end_ticket(t, silent=True)


client = Cruxer()


async def recover():
    """After a restart: pick up tickets and helper messages that were posted while the bot was off."""
    cat = client.get_channel(TICKET_CATEGORY_ID)
    if not isinstance(cat, discord.CategoryChannel):
        print("Ticket category not found. Check TICKET_CATEGORY_ID and that the bot can see it.")
        return
    ended = set()
    rchan = client.get_channel(RESULT_CHANNEL_ID)
    if rchan:
        try:
            async for m in rchan.history(limit=100):
                if m.author.id == TRACKED_BOT_ID:
                    ended |= numbers_in(message_text(m))
        except discord.HTTPException as exc:
            print(f"Could not read the result channel: {exc!r}")
    for t in list(state["tickets"].values()):   # a result came while the bot was off: close it silently
        if not t["ended"] and t.get("number") in ended:
            await end_ticket(t, silent=True)
    cutoff = time.time() - RECOVER_HOURS * 3600
    found = 0
    for chan in cat.text_channels:
        if str(chan.id) in state["tickets"] or ticket_number(chan.name) in ended:
            continue
        if chan.created_at.timestamp() < cutoff:
            continue
        try:
            async for m in chan.history(limit=200, oldest_first=True):
                text = message_text(m)
                if m.author.id == TRACKED_BOT_ID or "gameInstanceId=" in text:
                    t = get_ticket(chan)
                    learn_server(t, text)
                    reg = parse_help(text) if m.author.id == TRACKED_BOT_ID else None
                    if reg and m.created_at.timestamp() >= cutoff:
                        found += register_helper(t, reg[0], reg[1], reg[2], m.created_at.timestamp())
        except discord.HTTPException as exc:
            print(f"Could not read {chan.name}: {exc!r}")
    print(f"Recovery done: {len(state['tickets'])} tickets known, {found} helpers picked up.")


# ============================================================ slash commands
def allowed(interaction: discord.Interaction) -> bool:
    if OWNER_ID and interaction.user.id == OWNER_ID:
        return True
    perms = getattr(interaction.user, "guild_permissions", None)
    return bool(perms and perms.administrator)


@client.tree.command(name="check", description="Admin only: check a Roblox user's status (tests the cookie).")
@app_commands.describe(username="Roblox username")
async def check_cmd(interaction: discord.Interaction, username: str):
    if not allowed(interaction):
        return await interaction.response.send_message("Admins only.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    uid = await client.roblox.user_id(username.strip())
    if not uid:
        return await interaction.followup.send("That Roblox user was not found.", ephemeral=True)
    data = await client.roblox.presence([uid])
    p = (data or {}).get(uid)
    if not p:
        return await interaction.followup.send("Roblox could not be reached or the cookie is invalid.", ephemeral=True)
    kind = {0: "Offline", 1: "Online", 2: "In a game", 3: "In Roblox Studio"}.get(int(p.get("userPresenceType") or 0), "Unknown")
    extra = ""
    if p.get("placeId"):
        extra = f"\nPlace: `{p['placeId']}`  Server: `{p.get('gameId') or 'hidden'}`"
    await interaction.followup.send(f"**{username}** (`{uid}`): **{kind}**{extra}", ephemeral=True)


@client.tree.command(name="warnings", description="See how many helper warnings a member has.")
@app_commands.describe(member="Whose warnings to show (default: you)")
async def warnings_cmd(interaction: discord.Interaction, member: discord.User = None):
    target = member or interaction.user
    count = state["warnings"].get(str(target.id), 0)
    await interaction.response.send_message(f"{target.mention} has `{count}/{MAX_WARNINGS}` warnings.",
                                            ephemeral=True, allowed_mentions=NO_PING)


@client.tree.command(name="resetwarnings", description="Admin only: set a member's warnings back to 0.")
@app_commands.describe(member="Whose warnings to reset")
async def resetwarnings_cmd(interaction: discord.Interaction, member: discord.User):
    if not allowed(interaction):
        return await interaction.response.send_message("Admins only.", ephemeral=True)
    state["warnings"].pop(str(member.id), None)
    save()
    await interaction.response.send_message(f"{member.mention}'s warnings were reset.", ephemeral=True,
                                            allowed_mentions=NO_PING)


@client.tree.error
async def on_tree_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    print(f"Command error: {error!r}")
    try:
        if interaction.response.is_done():
            await interaction.followup.send("Something went wrong.", ephemeral=True)
        else:
            await interaction.response.send_message("Something went wrong.", ephemeral=True)
    except discord.HTTPException:
        pass


if __name__ == "__main__":
    if not TOKEN or TOKEN.startswith("PUT_"):
        raise SystemExit("Set DISCORD_TOKEN (in .env or your host's Variables) first.")
    try:
        client.run(TOKEN)
    except discord.PrivilegedIntentsRequired:
        raise SystemExit("Turn on MESSAGE CONTENT INTENT in the Discord Developer Portal "
                         "(your app > Bot > Privileged Gateway Intents), save, then restart the bot.")
