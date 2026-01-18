import os
import time
from dataclasses import dataclass
from typing import Optional, Dict, List, Tuple

import aiohttp
import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

# Load .env from this folder
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
TWITCH_CLIENT_ID = os.getenv("TWITCH_CLIENT_ID")
TWITCH_CLIENT_SECRET = os.getenv("TWITCH_CLIENT_SECRET")
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "75"))
PANEL_UPDATE_SECONDS = int(os.getenv("PANEL_UPDATE_SECONDS", "60"))

if not DISCORD_TOKEN or not TWITCH_CLIENT_ID or not TWITCH_CLIENT_SECRET:
    raise RuntimeError("Missing DISCORD_TOKEN or TWITCH_CLIENT_ID or TWITCH_CLIENT_SECRET in .env")

DB_PATH = os.path.join(os.path.dirname(__file__), "twitch_alerts.db")

DEFAULT_TEMPLATE = (
    "@everyone 🔴 **{name}** is LIVE!\n"
    "**{title}**\n"
    "Playing: **{game}**\n"
    "{url}"
)

# ---------------- Twitch API ----------------

class TwitchAPI:
    def __init__(self, client_id: str, client_secret: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self._token: Optional[str] = None
        self._token_exp: float = 0.0

    async def _get_token(self, session: aiohttp.ClientSession) -> str:
        now = time.time()
        if self._token and now < self._token_exp - 60:
            return self._token

        url = "https://id.twitch.tv/oauth2/token"
        params = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "grant_type": "client_credentials",
        }
        async with session.post(url, params=params) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Twitch token error {resp.status}: {await resp.text()}")
            data = await resp.json()
            self._token = data["access_token"]
            self._token_exp = now + int(data.get("expires_in", 3600))
            return self._token

    async def get_streams(self, session: aiohttp.ClientSession, logins: List[str]) -> List[dict]:
        if not logins:
            return []
        token = await self._get_token(session)
        url = "https://api.twitch.tv/helix/streams"
        params = [("user_login", login) for login in logins]
        headers = {
            "Client-ID": self.client_id,
            "Authorization": f"Bearer {token}",
        }
        async with session.get(url, params=params, headers=headers) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Twitch helix error {resp.status}: {await resp.text()}")
            data = await resp.json()
            return data.get("data", [])

# ---------------- DB ----------------

@dataclass
class Settings:
    guild_id: int
    alert_channel_id: Optional[int]
    streamer_role_id: Optional[int]
    default_template: str
    panel_channel_id: Optional[int]
    panel_message_id: Optional[int]

@dataclass
class Link:
    guild_id: int
    discord_user_id: int
    twitch_login: str
    custom_template: Optional[str]
    last_live: int
    last_stream_id: Optional[str]

class DB:
    def __init__(self, path: str):
        self.path = path

    async def init(self):
        async with aiosqlite.connect(self.path) as db:
            await db.execute("""
              CREATE TABLE IF NOT EXISTS settings (
                guild_id INTEGER PRIMARY KEY,
                alert_channel_id INTEGER,
                streamer_role_id INTEGER,
                default_template TEXT,
                panel_channel_id INTEGER,
                panel_message_id INTEGER
              )
            """)
            await db.execute("""
              CREATE TABLE IF NOT EXISTS streamer_links (
                guild_id INTEGER,
                discord_user_id INTEGER,
                twitch_login TEXT,
                custom_template TEXT,
                last_live INTEGER DEFAULT 0,
                last_stream_id TEXT,
                PRIMARY KEY (guild_id, discord_user_id)
              )
            """)
            await db.commit()

    async def get_settings(self, guild_id: int) -> Settings:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM settings WHERE guild_id=?", (guild_id,)) as cur:
                row = await cur.fetchone()

            if row is None:
                await db.execute(
                    "INSERT INTO settings (guild_id, default_template) VALUES (?, ?)",
                    (guild_id, DEFAULT_TEMPLATE),
                )
                await db.commit()
                return await self.get_settings(guild_id)

            return Settings(
                guild_id=guild_id,
                alert_channel_id=row["alert_channel_id"],
                streamer_role_id=row["streamer_role_id"],
                default_template=row["default_template"] or DEFAULT_TEMPLATE,
                panel_channel_id=row["panel_channel_id"],
                panel_message_id=row["panel_message_id"],
            )

    async def update_settings(self, guild_id: int, alert_channel_id=None, streamer_role_id=None, default_template=None,
                              panel_channel_id=None, panel_message_id=None):
        cur = await self.get_settings(guild_id)

        new_alert = alert_channel_id if alert_channel_id is not None else cur.alert_channel_id
        new_role = streamer_role_id if streamer_role_id is not None else cur.streamer_role_id
        new_template = default_template if default_template is not None else cur.default_template
        new_panel_channel = panel_channel_id if panel_channel_id is not None else cur.panel_channel_id
        new_panel_msg = panel_message_id if panel_message_id is not None else cur.panel_message_id

        async with aiosqlite.connect(self.path) as db:
            await db.execute("""
              UPDATE settings
              SET alert_channel_id=?, streamer_role_id=?, default_template=?,
                  panel_channel_id=?, panel_message_id=?
              WHERE guild_id=?
            """, (new_alert, new_role, new_template, new_panel_channel, new_panel_msg, guild_id))
            await db.commit()

    async def set_link(self, guild_id: int, discord_user_id: int, twitch_login: str):
        async with aiosqlite.connect(self.path) as db:
            await db.execute("""
              INSERT INTO streamer_links (guild_id, discord_user_id, twitch_login)
              VALUES (?, ?, ?)
              ON CONFLICT(guild_id, discord_user_id)
              DO UPDATE SET twitch_login=excluded.twitch_login
            """, (guild_id, discord_user_id, twitch_login.lower()))
            await db.commit()

    async def set_custom_template(self, guild_id: int, discord_user_id: int, text: Optional[str]):
        async with aiosqlite.connect(self.path) as db:
            await db.execute("""
              UPDATE streamer_links
              SET custom_template=?
              WHERE guild_id=? AND discord_user_id=?
            """, (text, guild_id, discord_user_id))
            await db.commit()

    async def clear_custom_template(self, guild_id: int, discord_user_id: int):
        await self.set_custom_template(guild_id, discord_user_id, None)

    async def get_links(self, guild_id: int) -> List[Link]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM streamer_links WHERE guild_id=?", (guild_id,)) as cur:
                rows = await cur.fetchall()

            return [
                Link(
                    guild_id=row["guild_id"],
                    discord_user_id=row["discord_user_id"],
                    twitch_login=row["twitch_login"],
                    custom_template=row["custom_template"],
                    last_live=row["last_live"],
                    last_stream_id=row["last_stream_id"],
                )
                for row in rows
            ]

    async def set_state(self, guild_id: int, discord_user_id: int, last_live: int, last_stream_id: Optional[str]):
        async with aiosqlite.connect(self.path) as db:
            await db.execute("""
              UPDATE streamer_links
              SET last_live=?, last_stream_id=?
              WHERE guild_id=? AND discord_user_id=?
            """, (last_live, last_stream_id, guild_id, discord_user_id))
            await db.commit()

db = DB(DB_PATH)
twitch = TwitchAPI(TWITCH_CLIENT_ID, TWITCH_CLIENT_SECRET)

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

def apply_template(template: str, **kwargs) -> str:
    out = template
    for k, v in kwargs.items():
        out = out.replace("{" + k + "}", "" if v is None else str(v))
    return out

async def member_has_role(guild: discord.Guild, user_id: int, role_id: Optional[int]) -> bool:
    if not role_id:
        return False
    role = guild.get_role(role_id)
    if role is None:
        return False
    member = guild.get_member(user_id)
    if member is None:
        try:
            member = await guild.fetch_member(user_id)
        except Exception:
            return False
    return role in member.roles

def stream_thumbnail(url_template: Optional[str]) -> Optional[str]:
    if not url_template:
        return None
    return url_template.replace("{width}", "1280").replace("{height}", "720")

# ---------------- Panel helpers ----------------

async def get_or_create_panel(guild: discord.Guild, settings: Settings) -> Optional[discord.Message]:
    ch = guild.get_channel(settings.panel_channel_id) if settings.panel_channel_id else None
    if ch is None or not isinstance(ch, discord.TextChannel):
        return None

    if settings.panel_message_id:
        try:
            return await ch.fetch_message(settings.panel_message_id)
        except Exception:
            pass

    msg = await ch.send("📺 Live panel starting...")
    await db.update_settings(guild.id, panel_message_id=msg.id)
    return msg

async def render_panel_embed(
    guild: discord.Guild,
    settings: Settings,
    live_streams: List[dict],
    active_links: List[Tuple[Link, discord.Member]]
) -> discord.Embed:
    embed = discord.Embed(title="📺 Twitch Live Panel")
    embed.set_footer(text=f"Updates every {PANEL_UPDATE_SECONDS}s • Streamers use /twitch_set")

    live_map = {s["user_login"].lower(): s for s in live_streams}

    live_lines = []
    off_lines = []
    for link, member in active_links:
        s = live_map.get(link.twitch_login.lower())
        if s:
            name = s.get("user_name", link.twitch_login)
            login = s.get("user_login", link.twitch_login)
            title = s.get("title", "")
            game = s.get("game_name", "—")
            url = f"https://twitch.tv/{login}"
            live_lines.append(f"🔴 **{name}** — *{game}*\n{title}\n{url}")
        else:
            off_lines.append(f"⚫ **{member.display_name}** — {link.twitch_login}")

    embed.add_field(
        name="Live Now",
        value=("\n\n".join(live_lines)[:1024] if live_lines else "Nobody is live right now."),
        inline=False
    )
    if off_lines:
        embed.add_field(name="Offline", value=("\n".join(off_lines)[:1024]), inline=False)
    return embed

@bot.event
async def on_ready():
    await db.init()
    await bot.tree.sync()
    print(f"✅ Logged in as {bot.user}")
    poll_loop.start()
    panel_loop.start()

# ---------------- Slash Commands ----------------

@bot.tree.command(name="setup", description="(Admin) Set alert channel and Streamer role.")
@app_commands.describe(channel="Channel to post alerts in", role="Role that marks someone as a streamer")
async def setup(interaction: discord.Interaction, channel: discord.TextChannel, role: discord.Role):
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message("You need **Manage Server** to use this.", ephemeral=True)
        return
    await db.update_settings(interaction.guild_id, alert_channel_id=channel.id, streamer_role_id=role.id)
    await interaction.response.send_message(f"✅ Saved.\nAlerts: {channel.mention}\nStreamer role: {role.mention}")

@bot.tree.command(name="panel_set", description="(Admin) Set a channel for the live panel (auto-updated message).")
@app_commands.describe(channel="Channel to keep an updated live panel in")
async def panel_set(interaction: discord.Interaction, channel: discord.TextChannel):
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message("You need **Manage Server** to use this.", ephemeral=True)
        return
    await db.update_settings(interaction.guild_id, panel_channel_id=channel.id, panel_message_id=None)
    await interaction.response.send_message(f"✅ Live panel channel set to {channel.mention}.")

@bot.tree.command(name="template_default", description="(Admin) Set default alert message template.")
@app_commands.describe(text="Use {name} {title} {game} {url} {viewers}")
async def template_default(interaction: discord.Interaction, text: str):
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message("You need **Manage Server** to use this.", ephemeral=True)
        return
    await db.update_settings(interaction.guild_id, default_template=text)
    await interaction.response.send_message("✅ Default template updated.")

@bot.tree.command(name="twitch_set", description="(Streamers) Link your Twitch login.")
@app_commands.describe(login="Your Twitch channel name (login), e.g. jprod")
async def twitch_set(interaction: discord.Interaction, login: str):
    settings = await db.get_settings(interaction.guild_id)
    if not await member_has_role(interaction.guild, interaction.user.id, settings.streamer_role_id):
        await interaction.response.send_message("You need the Streamer role to link a Twitch account.", ephemeral=True)
        return
    await db.set_link(interaction.guild_id, interaction.user.id, login)
    await interaction.response.send_message(f"✅ Linked Twitch login: **{login.lower()}**", ephemeral=True)

@bot.tree.command(name="template_me", description="(Streamers) Set a custom alert message just for you.")
@app_commands.describe(text="Use {name} {title} {game} {url} {viewers}")
async def template_me(interaction: discord.Interaction, text: str):
    settings = await db.get_settings(interaction.guild_id)
    if not await member_has_role(interaction.guild, interaction.user.id, settings.streamer_role_id):
        await interaction.response.send_message("You need the Streamer role to set a custom template.", ephemeral=True)
        return
    await db.set_custom_template(interaction.guild_id, interaction.user.id, text)
    await interaction.response.send_message("✅ Your custom template is set.", ephemeral=True)

@bot.tree.command(name="template_me_clear", description="(Streamers) Clear your custom alert message (revert to default).")
async def template_me_clear(interaction: discord.Interaction):
    settings = await db.get_settings(interaction.guild_id)
    if not await member_has_role(interaction.guild, interaction.user.id, settings.streamer_role_id):
        await interaction.response.send_message("You need the Streamer role to do that.", ephemeral=True)
        return
    await db.clear_custom_template(interaction.guild_id, interaction.user.id)
    await interaction.response.send_message("✅ Cleared. You now use the server default template.", ephemeral=True)

@bot.tree.command(name="live", description="Show who is live right now.")
async def live(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    settings = await db.get_settings(interaction.guild_id)
    links = await db.get_links(interaction.guild_id)

    active: List[Tuple[Link, discord.Member]] = []
    for link in links:
        if await member_has_role(interaction.guild, link.discord_user_id, settings.streamer_role_id):
            member = interaction.guild.get_member(link.discord_user_id)
            if member is None:
                try:
                    member = await interaction.guild.fetch_member(link.discord_user_id)
                except Exception:
                    continue
            active.append((link, member))

    if not active:
        await interaction.followup.send("No streamers are linked yet (or none currently have the Streamer role).")
        return

    async with aiohttp.ClientSession() as session:
        streams = await twitch.get_streams(session, [link.twitch_login for link, _ in active])

    if not streams:
        await interaction.followup.send("Nobody is live right now.")
        return

    embed = discord.Embed(title="🔴 Live now")
    lines = []
    for s in streams:
        name = s.get("user_name")
        login = s.get("user_login")
        title = s.get("title", "")
        game = s.get("game_name", "—")
        url = f"https://twitch.tv/{login}"
        lines.append(f"**{name}** — *{game}*\n{title}\n{url}")
    embed.description = "\n\n".join(lines)[:4096]
    await interaction.followup.send(embed=embed)

# ---------------- Alert Polling Loop ----------------

@tasks.loop(seconds=POLL_SECONDS)
async def poll_loop():
    async with aiohttp.ClientSession() as session:
        for guild in bot.guilds:
            try:
                settings = await db.get_settings(guild.id)
                if not settings.alert_channel_id or not settings.streamer_role_id:
                    continue

                channel = guild.get_channel(settings.alert_channel_id)
                if channel is None or not isinstance(channel, discord.TextChannel):
                    continue

                links = await db.get_links(guild.id)

                active: List[Tuple[Link, discord.Member]] = []
                for link in links:
                    if await member_has_role(guild, link.discord_user_id, settings.streamer_role_id):
                        member = guild.get_member(link.discord_user_id)
                        if member is None:
                            try:
                                member = await guild.fetch_member(link.discord_user_id)
                            except Exception:
                                continue
                        active.append((link, member))

                if not active:
                    continue

                logins = [link.twitch_login for link, _ in active]
                streams = await twitch.get_streams(session, logins)
                live_map: Dict[str, dict] = {s["user_login"].lower(): s for s in streams}

                for link, member in active:
                    s = live_map.get(link.twitch_login.lower())
                    is_live_now = s is not None

                    if is_live_now:
                        stream_id = s.get("id")
                        was_live = link.last_live == 1
                        is_new = (link.last_stream_id != stream_id)

                        if (not was_live) or is_new:
                            name = s.get("user_name", link.twitch_login)
                            login = s.get("user_login", link.twitch_login)
                            title = s.get("title", "—")
                            game = s.get("game_name", "—")
                            viewers = s.get("viewer_count", "—")
                            url = f"https://twitch.tv/{login}"

                            template = link.custom_template.strip() if link.custom_template else settings.default_template
                            content = apply_template(
                                template,
                                name=name,
                                login=login,
                                title=title,
                                game=game,
                                viewers=viewers,
                                url=url,
                            )

                            embed = discord.Embed(
                                title=f"🔴 {name} is live!",
                                url=url,
                                description=title if title else None,
                            )
                            embed.add_field(name="Game", value=str(game), inline=True)
                            embed.add_field(name="Viewers", value=str(viewers), inline=True)

                            thumb = stream_thumbnail(s.get("thumbnail_url"))
                            if thumb:
                                embed.set_image(url=thumb)

                            allowed = discord.AllowedMentions(everyone=True)
                            if "@everyone" not in content:
                                content = "@everyone\n" + content

                            await channel.send(content=content, embed=embed, allowed_mentions=allowed)
                            await db.set_state(guild.id, link.discord_user_id, 1, stream_id)
                    else:
                        if link.last_live == 1:
                            await db.set_state(guild.id, link.discord_user_id, 0, link.last_stream_id)

            except Exception as e:
                print(f"Poll error in guild {guild.id}: {e}")

@poll_loop.before_loop
async def before_poll():
    await bot.wait_until_ready()

# ---------------- Live Panel Loop ----------------

@tasks.loop(seconds=PANEL_UPDATE_SECONDS)
async def panel_loop():
    async with aiohttp.ClientSession() as session:
        for guild in bot.guilds:
            try:
                settings = await db.get_settings(guild.id)
                if not settings.panel_channel_id or not settings.streamer_role_id:
                    continue

                links = await db.get_links(guild.id)

                active_links: List[Tuple[Link, discord.Member]] = []
                for link in links:
                    if await member_has_role(guild, link.discord_user_id, settings.streamer_role_id):
                        member = guild.get_member(link.discord_user_id)
                        if member is None:
                            try:
                                member = await guild.fetch_member(link.discord_user_id)
                            except Exception:
                                continue
                        active_links.append((link, member))

                if not active_links:
                    continue

                logins = [link.twitch_login for link, _ in active_links]
                streams = await twitch.get_streams(session, logins)

                panel_msg = await get_or_create_panel(guild, settings)
                if panel_msg is None:
                    continue

                settings = await db.get_settings(guild.id)  # refresh panel ids
                embed = await render_panel_embed(guild, settings, streams, active_links)
                await panel_msg.edit(content=None, embed=embed)

            except Exception as e:
                print(f"Panel error in guild {guild.id}: {e}")

@panel_loop.before_loop
async def before_panel():
    await bot.wait_until_ready()

# Run
bot.run(DISCORD_TOKEN)
