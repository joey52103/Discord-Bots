import os
import asyncio
from datetime import datetime, timezone
from typing import Optional, List

import discord
from discord import app_commands
from dotenv import load_dotenv
import aiosqlite

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID", "0"))
ALERT_CHANNEL_ID = int(os.getenv("ALERT_CHANNEL_ID", "0"))  # optional
STAFF_ROLE_ID = int(os.getenv("STAFF_ROLE_ID", "0"))        # 0 = allow all
RAID_EMOJI = os.getenv("RAID_EMOJI", "🦁")

DB_PATH = "raidbot.db"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def is_staff(member: discord.Member) -> bool:
    if STAFF_ROLE_ID == 0:
        return True
    return any(r.id == STAFF_ROLE_ID for r in member.roles)


def chunk_mentions(user_ids: List[int], max_chars: int = 1800) -> List[str]:
    """Return chunks of mention strings that fit under Discord message limits."""
    chunks = []
    current = ""
    for uid in user_ids:
        m = f"<@{uid}> "
        if len(current) + len(m) > max_chars:
            chunks.append(current.strip())
            current = m
        else:
            current += m
    if current.strip():
        chunks.append(current.strip())
    return chunks


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS tracked_posts (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            guild_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            emoji TEXT NOT NULL
        )
        """)
        await db.execute("""
        CREATE TABLE IF NOT EXISTS subscribers (
            user_id INTEGER PRIMARY KEY,
            added_at_utc TEXT NOT NULL
        )
        """)
        await db.commit()


async def set_tracked_post(guild_id: int, channel_id: int, message_id: int, emoji: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO tracked_posts (id, guild_id, channel_id, message_id, emoji)
            VALUES (1, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                guild_id=excluded.guild_id,
                channel_id=excluded.channel_id,
                message_id=excluded.message_id,
                emoji=excluded.emoji
        """, (guild_id, channel_id, message_id, emoji))
        await db.commit()


async def get_tracked_post() -> Optional[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT guild_id, channel_id, message_id, emoji FROM tracked_posts WHERE id = 1")
        row = await cur.fetchone()
        if not row:
            return None
        return {"guild_id": row[0], "channel_id": row[1], "message_id": row[2], "emoji": row[3]}


async def add_subscriber(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT OR IGNORE INTO subscribers (user_id, added_at_utc)
            VALUES (?, ?)
        """, (user_id, utc_now_iso()))
        await db.commit()


async def remove_subscriber(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM subscribers WHERE user_id = ?", (user_id,))
        await db.commit()


async def list_subscribers() -> List[int]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT user_id FROM subscribers ORDER BY user_id ASC")
        rows = await cur.fetchall()
        return [r[0] for r in rows]


class RaidBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.reactions = True
        intents.guilds = True
        # members intent not required because we mention by ID (<@id>)
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

        # simple cooldown to prevent spam
        self._raid_lock = asyncio.Lock()
        self._last_raid_ts: float = 0.0
        self._cooldown_seconds = 120  # 2 minutes

    async def setup_hook(self):
        await init_db()

        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

    async def can_fire_raid(self) -> bool:
        async with self._raid_lock:
            now = asyncio.get_event_loop().time()
            if now - self._last_raid_ts < self._cooldown_seconds:
                return False
            self._last_raid_ts = now
            return True


bot = RaidBot()


@bot.event
async def on_ready():
    print(f"Raid Bot logged in as {bot.user} (ID: {bot.user.id})")


@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    tracked = await get_tracked_post()
    if not tracked:
        return

    if payload.guild_id != tracked["guild_id"]:
        return
    if payload.channel_id != tracked["channel_id"]:
        return
    if payload.message_id != tracked["message_id"]:
        return

    # Compare emoji
    emoji_str = str(payload.emoji)
    if emoji_str != tracked["emoji"]:
        return

    # Ignore bot reactions
    if payload.user_id == bot.user.id:
        return

    await add_subscriber(payload.user_id)


@bot.event
async def on_raw_reaction_remove(payload: discord.RawReactionActionEvent):
    tracked = await get_tracked_post()
    if not tracked:
        return

    if payload.guild_id != tracked["guild_id"]:
        return
    if payload.channel_id != tracked["channel_id"]:
        return
    if payload.message_id != tracked["message_id"]:
        return

    emoji_str = str(payload.emoji)
    if emoji_str != tracked["emoji"]:
        return

    await remove_subscriber(payload.user_id)


raid_group = app_commands.Group(name="raid", description="Raid alert commands")


@raid_group.command(name="track", description="Tell Raid Bot which message to watch for reactions")
@app_commands.describe(
    message_id="The message ID of your base photo post",
    emoji=f"Emoji to react with (default: {RAID_EMOJI})"
)
async def raid_track(interaction: discord.Interaction, message_id: str, emoji: Optional[str] = None):
    if not isinstance(interaction.user, discord.Member) or not is_staff(interaction.user):
        await interaction.response.send_message("You don't have permission to set the tracked message.", ephemeral=True)
        return

    if not message_id.isdigit():
        await interaction.response.send_message("message_id must be a number. (Right-click message → Copy ID)", ephemeral=True)
        return

    tracked_emoji = (emoji or RAID_EMOJI).strip()

    # Store channel+message for this interaction's channel
    await set_tracked_post(interaction.guild_id, interaction.channel_id, int(message_id), tracked_emoji)

    # Optional: try to add the reaction so people see what to click
    try:
        msg = await interaction.channel.fetch_message(int(message_id))
        await msg.add_reaction(tracked_emoji)
    except Exception:
        pass

    # Clear current subscriber list (optional behavior)
    # If you want to keep old subs even when switching message, comment this block out.
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM subscribers")
        await db.commit()

    await interaction.response.send_message(
        f"✅ Tracking message **{message_id}** in this channel.\n"
        f"Members should react with **{tracked_emoji}** to get raid alerts.\n"
        f"(Subscriber list was reset when tracking was updated.)",
        ephemeral=True
    )


@raid_group.command(name="alert", description="Ping everyone who reacted to the tracked base post")
@app_commands.describe(reason="Optional reason/details")
async def raid_alert(interaction: discord.Interaction, reason: Optional[str] = None):
    if not isinstance(interaction.user, discord.Member) or not is_staff(interaction.user):
        await interaction.response.send_message("You don't have permission to trigger a raid alert.", ephemeral=True)
        return

    tracked = await get_tracked_post()
    if not tracked:
        await interaction.response.send_message(
            "No tracked message set yet. Use `/raid track <message_id>` in the base channel first.",
            ephemeral=True
        )
        return

    if not await bot.can_fire_raid():
        await interaction.response.send_message("Raid alert is on cooldown. Try again in a bit.", ephemeral=True)
        return

    subs = await list_subscribers()
    if not subs:
        await interaction.response.send_message("No one has opted in yet (no reactions recorded).", ephemeral=True)
        return

    # Decide where to post alert
    channel: discord.abc.Messageable
    if ALERT_CHANNEL_ID:
        ch = bot.get_channel(ALERT_CHANNEL_ID)
        channel = ch if ch else interaction.channel
    else:
        channel = interaction.channel

    header = "🚨 **RAID ALERT** 🚨"
    details = f"\n**Reason:** {reason}" if reason else ""
    base_link = f"\n**Base Post:** https://discord.com/channels/{tracked['guild_id']}/{tracked['channel_id']}/{tracked['message_id']}"

    await interaction.response.send_message("✅ Raid alert sent.", ephemeral=True)

    # Post header + chunks of mentions
    await channel.send(f"{header}{details}{base_link}")
    for chunk in chunk_mentions(subs):
        await channel.send(chunk)


@raid_group.command(name="count", description="See how many people are subscribed (reacted)")
async def raid_count(interaction: discord.Interaction):
    subs = await list_subscribers()
    await interaction.response.send_message(f"🦁 Subscribers: **{len(subs)}**", ephemeral=True)


@raid_group.command(name="clear", description="Clear the subscriber list (people will need to react again)")
async def raid_clear(interaction: discord.Interaction):
    if not isinstance(interaction.user, discord.Member) or not is_staff(interaction.user):
        await interaction.response.send_message("You don't have permission to clear the list.", ephemeral=True)
        return

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM subscribers")
        await db.commit()

    await interaction.response.send_message("✅ Subscriber list cleared.", ephemeral=True)


bot.tree.add_command(raid_group)

if not TOKEN:
    raise RuntimeError("Missing DISCORD_TOKEN in environment variables")

bot.run(TOKEN)
