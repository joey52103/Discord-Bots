# rounds-bot/bot.py
# Lion's Crown Rounds Bot (discord.py + SQLite)
# Features:
# - /round start, /round add (proof image OR proof link), /round finalize
# - /round stats, /round export
# - Permission: ONLY members with the ROUNDS role can use these commands
#
# .env / Railway Variables needed:
#   DISCORD_TOKEN=...
#   GUILD_ID=...               (server id for fast slash sync; optional but recommended)
#   LOG_CHANNEL_ID=...         (channel id where finalized rounds are posted)
#   ROUNDS_ROLE_ID=...         (role id allowed to run rounds; REQUIRED)
#
# Optional:
#   (none)

import os
import csv
import io
from datetime import datetime, timezone
from typing import Optional, List, Tuple

import discord
from discord import app_commands
from dotenv import load_dotenv
import aiosqlite

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID", "0"))  # server ID for fast sync
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "0"))

# Only members with this role can run rounds commands
ROUNDS_ROLE_ID = int(os.getenv("ROUNDS_ROLE_ID", "0"))

DB_PATH = "rounds.db"

CUT_OWNER = 0.70
CUT_RUNNER = 0.30


def money(n: int) -> str:
    return f"${n:,}"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def has_rounds_role(member: discord.Member) -> bool:
    # If not configured, default to "deny" to be safe
    if ROUNDS_ROLE_ID == 0:
        return False
    return any(r.id == ROUNDS_ROLE_ID for r in member.roles)


async def require_rounds_role(interaction: discord.Interaction) -> bool:
    """Returns True if allowed, otherwise sends an ephemeral error and returns False."""
    if not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("❌ This command must be used in a server.", ephemeral=True)
        return False

    if not has_rounds_role(interaction.user):
        await interaction.response.send_message("❌ You need the **Rounds** role to use rounds commands.", ephemeral=True)
        return False

    return True


class LionsCrownRoundsBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True  # role checks require members intent (enable in Dev Portal)
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await init_db()

        # Fast guild-only sync (recommended while developing)
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            # Global sync can take longer to propagate
            await self.tree.sync()


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS round_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at_utc TEXT NOT NULL,
            finalized_at_utc TEXT,
            runner_id INTEGER NOT NULL,
            runner_name TEXT NOT NULL,
            status TEXT NOT NULL  -- 'ACTIVE' or 'FINAL'
        )
        """)

        # proof_url is nullable: can store uploaded image URL OR a user-provided link
        await db.execute("""
        CREATE TABLE IF NOT EXISTS round_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            round_id INTEGER NOT NULL,
            created_at_utc TEXT NOT NULL,
            owner_id INTEGER NOT NULL,
            owner_name TEXT NOT NULL,
            amount INTEGER NOT NULL,     -- amount collected for this owner (base amount before split)
            proof_url TEXT,              -- can be NULL
            FOREIGN KEY(round_id) REFERENCES round_sessions(id)
        )
        """)
        await db.commit()


bot = LionsCrownRoundsBot()


@bot.event
async def on_ready():
    print(f"Rounds Bot logged in as {bot.user} (ID: {bot.user.id})")


round_group = app_commands.Group(name="round", description="Printer round logging tools")


async def get_active_round_id(runner_id: int) -> Optional[int]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT id FROM round_sessions
            WHERE runner_id = ? AND status = 'ACTIVE'
            ORDER BY id DESC LIMIT 1
        """, (runner_id,))
        row = await cur.fetchone()
        return row[0] if row else None


async def fetch_round_entries(round_id: int) -> List[Tuple[int, str, int, Optional[str]]]:
    # returns: [(owner_id, owner_name, amount, proof_url), ...]
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT owner_id, owner_name, amount, proof_url
            FROM round_entries
            WHERE round_id = ?
            ORDER BY id ASC
        """, (round_id,))
        return await cur.fetchall()


@round_group.command(name="start", description="Start your round session")
async def round_start(interaction: discord.Interaction):
    if not await require_rounds_role(interaction):
        return

    runner = interaction.user  # discord.Member
    existing = await get_active_round_id(runner.id)
    if existing:
        await interaction.response.send_message(
            f"You already have an active round (**#{existing}**). Use `/round add` or `/round finalize`.",
            ephemeral=True
        )
        return

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            INSERT INTO round_sessions (started_at_utc, runner_id, runner_name, status)
            VALUES (?, ?, ?, 'ACTIVE')
        """, (utc_now_iso(), runner.id, runner.display_name))
        await db.commit()
        round_id = cur.lastrowid

    await interaction.response.send_message(
        f"✅ Round **#{round_id}** started.\n"
        f"Log payouts with: `/round add owner amount proof_image/proof_link`",
        ephemeral=True
    )


@round_group.command(name="add", description="Add ONE payout entry to your active round (proof: image OR link)")
@app_commands.describe(
    owner="Who you paid (the AFK owner)",
    amount="How much you collected for them (base amount before split)",
    proof_image="Upload a screenshot proof (optional if you provide a link instead)",
    proof_link="Paste a proof link (optional if you upload an image instead)"
)
async def round_add(
    interaction: discord.Interaction,
    owner: discord.Member,
    amount: app_commands.Range[int, 1, 2_000_000_000],
    proof_image: Optional[discord.Attachment] = None,
    proof_link: Optional[str] = None
):
    if not await require_rounds_role(interaction):
        return

    runner = interaction.user  # discord.Member
    round_id = await get_active_round_id(runner.id)
    if not round_id:
        await interaction.response.send_message("You don’t have an active round. Run `/round start` first.", ephemeral=True)
        return

    # Require at least one proof method
    if proof_image is None and (proof_link is None or not proof_link.strip()):
        await interaction.response.send_message(
            "Proof is required. Upload an image in **proof_image** OR paste a URL in **proof_link**.",
            ephemeral=True
        )
        return

    proof_url = proof_image.url if proof_image is not None else proof_link.strip()
    amt = int(amount)

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO round_entries (round_id, created_at_utc, owner_id, owner_name, amount, proof_url)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (round_id, utc_now_iso(), owner.id, owner.display_name, amt, proof_url))
        await db.commit()

    owner_payout = int(round(amt * CUT_OWNER))
    runner_cut = amt - owner_payout

    await interaction.response.send_message(
        f"✅ Added to Round **#{round_id}**:\n"
        f"- Owner: {owner.mention}\n"
        f"- Collected: **{money(amt)}**\n"
        f"- Owner gets (70%): **{money(owner_payout)}**\n"
        f"- Runner cut (30%): **{money(runner_cut)}**\n"
        f"- Proof: {proof_url}",
        ephemeral=True
    )


@round_group.command(name="finalize", description="Finalize your active round and post the breakdown to the log channel")
async def round_finalize(interaction: discord.Interaction):
    if not await require_rounds_role(interaction):
        return

    runner = interaction.user  # discord.Member
    round_id = await get_active_round_id(runner.id)
    if not round_id:
        await interaction.response.send_message("No active round found. Use `/round start` first.", ephemeral=True)
        return

    entries = await fetch_round_entries(round_id)
    if not entries:
        await interaction.response.send_message(
            f"Round **#{round_id}** has no entries. Add payouts with `/round add`.",
            ephemeral=True
        )
        return

    # Mark final in DB
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE round_sessions
            SET status = 'FINAL', finalized_at_utc = ?
            WHERE id = ?
        """, (utc_now_iso(), round_id))
        await db.commit()

    # Totals
    total_collected = sum(e[2] for e in entries)
    total_paid_out = sum(int(round(e[2] * CUT_OWNER)) for e in entries)
    total_runner_cut = total_collected - total_paid_out

    # Log channel
    log_channel = bot.get_channel(LOG_CHANNEL_ID)
    if not isinstance(log_channel, discord.TextChannel):
        await interaction.response.send_message(
            "Finalized in the database, but I can't find the log channel. Check LOG_CHANNEL_ID.",
            ephemeral=True
        )
        return

    embed = discord.Embed(
        title=f"🦁 Lion’s Crown — Round #{round_id} Finalized",
        description="Per-owner breakdown (proof links included when provided).",
        timestamp=datetime.now(timezone.utc)
    )
    embed.add_field(name="Runner", value=runner.mention, inline=False)
    embed.add_field(name="Total Collected", value=money(total_collected), inline=True)
    embed.add_field(name="Total Paid Out (70%)", value=money(total_paid_out), inline=True)
    embed.add_field(name="Runner Cut (30%)", value=money(total_runner_cut), inline=True)

    lines = []
    for owner_id, owner_name, amount, proof_url in entries:
        owner_payout = int(round(amount * CUT_OWNER))
        runner_cut = amount - owner_payout
        proof_part = f"[proof]({proof_url})" if proof_url else "*no proof*"
        lines.append(
            f"<@{owner_id}>: collected **{money(amount)}** → paid **{money(owner_payout)}** | cut **{money(runner_cut)}** {proof_part}"
        )

    # Chunk into embed fields to avoid value limits
    chunk, length = [], 0
    for line in lines:
        if length + len(line) + 1 > 950:
            embed.add_field(name="Entries", value="\n".join(chunk), inline=False)
            chunk, length = [], 0
        chunk.append(line)
        length += len(line) + 1
    if chunk:
        embed.add_field(name="Entries", value="\n".join(chunk), inline=False)

    embed.set_footer(text="Ledger saved to rounds.db")

    await interaction.response.send_message(f"✅ Round **#{round_id}** finalized and posted.", ephemeral=True)
    await log_channel.send(embed=embed)


@round_group.command(name="stats", description="Show stats (how many rounds + totals) for a runner")
@app_commands.describe(member="Optional member (defaults to you)")
async def round_stats(interaction: discord.Interaction, member: Optional[discord.Member] = None):
    if not await require_rounds_role(interaction):
        return

    target = member or interaction.user  # discord.Member

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT COUNT(*) FROM round_sessions
            WHERE runner_id = ? AND status = 'FINAL'
        """, (target.id,))
        rounds_final = (await cur.fetchone())[0]

        cur = await db.execute("""
            SELECT COALESCE(SUM(e.amount), 0)
            FROM round_entries e
            JOIN round_sessions s ON s.id = e.round_id
            WHERE s.runner_id = ? AND s.status = 'FINAL'
        """, (target.id,))
        total_collected = (await cur.fetchone())[0] or 0

    paid_out = int(round(int(total_collected) * CUT_OWNER))
    runner_cut = int(total_collected) - paid_out

    embed = discord.Embed(title="📊 Round Stats")
    embed.add_field(name="Member", value=target.mention, inline=False)
    embed.add_field(name="Finalized Rounds", value=str(rounds_final), inline=True)
    embed.add_field(name="Total Collected", value=money(int(total_collected)), inline=True)
    embed.add_field(name="Est. Paid Out (70%)", value=money(paid_out), inline=True)
    embed.add_field(name="Est. Runner Cut (30%)", value=money(runner_cut), inline=True)

    await interaction.response.send_message(embed=embed, ephemeral=True)


@round_group.command(name="export", description="Export all rounds + entries as CSV (Rounds role required)")
async def round_export(interaction: discord.Interaction):
    if not await require_rounds_role(interaction):
        return

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT
                s.id AS round_id,
                s.started_at_utc,
                s.finalized_at_utc,
                s.runner_id,
                s.runner_name,
                s.status,
                e.id AS entry_id,
                e.created_at_utc,
                e.owner_id,
                e.owner_name,
                e.amount,
                e.proof_url
            FROM round_sessions s
            LEFT JOIN round_entries e ON e.round_id = s.id
            ORDER BY s.id DESC, e.id ASC
        """)
        rows = await cur.fetchall()
        cols = [d[0] for d in cur.description]

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(cols)
    writer.writerows(rows)
    output.seek(0)

    data = output.getvalue().encode("utf-8")
    file = discord.File(fp=io.BytesIO(data), filename="lionscrown_rounds_export.csv")

    await interaction.response.send_message("✅ Export ready:", file=file, ephemeral=True)


bot.tree.add_command(round_group)

if not TOKEN:
    raise RuntimeError("Missing DISCORD_TOKEN in environment variables")

if LOG_CHANNEL_ID == 0:
    raise RuntimeError("Missing LOG_CHANNEL_ID in environment variables")

if ROUNDS_ROLE_ID == 0:
    raise RuntimeError("Missing ROUNDS_ROLE_ID in environment variables (required)")

bot.run(TOKEN)
