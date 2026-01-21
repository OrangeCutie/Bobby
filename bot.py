import os
import discord
from discord.ext import commands
from discord import app_commands
import aiosqlite
import hashlib
import secrets
from datetime import datetime, timezone

# ================= CONFIG =================

OWNER_ID = 739411481342509059

DB_DIR = "/app/data"
DB_FILE = f"{DB_DIR}/keys.db"

ROLE_MAP = {
    "premium": "Premium",
    "vip": "VIP",
    "lifetime": "Lifetime"
}

# If True, logs channel will see the FULL key.
# If False, it will be masked like ABCD...WXYZ in the logs channel.
SHOW_FULL_KEY_IN_LOG_CHANNEL = False

# =========================================

os.makedirs(DB_DIR, exist_ok=True)

TOKEN = os.environ.get("TOKEN")
if not TOKEN:
    raise RuntimeError("TOKEN not found in environment variables")

intents = discord.Intents.default()
intents.members = True  # REQUIRED to add roles reliably in many servers
bot = commands.Bot(command_prefix="!", intents=intents)

# ---------- DATABASE ----------

async def init_db():
    async with aiosqlite.connect(DB_FILE) as db:
        # Keys table (stores only hashes; keys themselves are never recoverable)
        await db.execute("""
        CREATE TABLE IF NOT EXISTS keys (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key_hash TEXT UNIQUE NOT NULL,
            product TEXT NOT NULL,
            redeemed INTEGER DEFAULT 0
        )
        """)

        # Redemption log table (who/when/what)
        await db.execute("""
        CREATE TABLE IF NOT EXISTS redemptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key_hash TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            guild_id INTEGER NOT NULL,
            product TEXT NOT NULL,
            redeemed_at_utc TEXT NOT NULL
        )
        """)

        # Settings table (per guild)
        await db.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            guild_id INTEGER PRIMARY KEY,
            log_channel_id INTEGER
        )
        """)

        await db.commit()

async def get_log_channel_id(guild_id: int) -> int | None:
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute("SELECT log_channel_id FROM settings WHERE guild_id = ?", (guild_id,))
        row = await cur.fetchone()
        return row[0] if row and row[0] else None

async def set_log_channel_id(guild_id: int, channel_id: int | None):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("""
        INSERT INTO settings (guild_id, log_channel_id)
        VALUES (?, ?)
        ON CONFLICT(guild_id) DO UPDATE SET log_channel_id=excluded.log_channel_id
        """, (guild_id, channel_id))
        await db.commit()

# ---------- UTILS ----------

def is_owner(interaction: discord.Interaction) -> bool:
    return interaction.user.id == OWNER_ID

def utc_now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

def sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()

def generate_key():
    raw = secrets.token_urlsafe(32)
    key = raw.upper()
    key_hash = sha256_hex(key)
    return key, key_hash

def mask_key(key: str) -> str:
    k = key.strip().upper()
    if len(k) <= 10:
        return k
    return f"{k[:4]}...{k[-4:]}"

async def safe_respond(interaction: discord.Interaction, content: str, ephemeral: bool = True):
    """
    Handles 'already responded' edge cases.
    """
    try:
        if interaction.response.is_done():
            await interaction.followup.send(content, ephemeral=ephemeral)
        else:
            await interaction.response.send_message(content, ephemeral=ephemeral)
    except Exception:
        # last resort: ignore response errors
        pass

# ---------- EVENTS ----------

@bot.event
async def on_ready():
    await init_db()
    # Sync once on ready. If you run multiple shards/instances, you can guard this.
    try:
        await bot.tree.sync()
    except Exception as e:
        print("⚠️ tree.sync failed:", e)
    print(f"✅ Bot is online as {bot.user}")

# ---------- COMMANDS ----------

@bot.tree.command(name="redeem", description="Redeem a one-time license key")
@app_commands.describe(key="Your license key")
@app_commands.guild_only()
async def redeem(interaction: discord.Interaction, key: str):
    if not interaction.guild:
        await safe_respond(interaction, "❌ This command must be used in a server.", True)
        return

    key_clean = key.strip().upper()
    key_hash = sha256_hex(key_clean)

    # Attempt redemption atomically-ish
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute(
            "SELECT product, redeemed FROM keys WHERE key_hash = ?",
            (key_hash,)
        )
        row = await cur.fetchone()

        if not row:
            await safe_respond(interaction, "❌ Invalid key.", True)
            return

        product, redeemed = row
        if redeemed == 1:
            await safe_respond(interaction, "❌ This key has already been used.", True)
            return

        await db.execute("UPDATE keys SET redeemed = 1 WHERE key_hash = ?", (key_hash,))
        await db.execute(
            "INSERT INTO redemptions (key_hash, user_id, guild_id, product, redeemed_at_utc) VALUES (?, ?, ?, ?, ?)",
            (key_hash, interaction.user.id, interaction.guild.id, product, utc_now_str())
        )
        await db.commit()

    # Assign role
    role_name = ROLE_MAP.get(product.lower())
    role_added = False

    if role_name:
        role = discord.utils.get(interaction.guild.roles, name=role_name)
        if role:
            try:
                await interaction.user.add_roles(role, reason=f"Key redeemed: {product}")
                role_added = True
            except discord.Forbidden:
                role_added = False
            except discord.HTTPException:
                role_added = False

    msg = f"✅ You redeemed **{product.upper()}**"
    if role_name and role_added:
        msg += f"\n🎭 Role added: **{role_name}**"
    elif role_name and not role_added:
        msg += f"\n⚠️ I couldn’t add the role (**{role_name}**). Check my permissions and role hierarchy."

    await safe_respond(interaction, msg, True)

    # ---------- LOGGING ----------
    log_channel_id = await get_log_channel_id(interaction.guild.id)
    if log_channel_id:
        log_channel = interaction.guild.get_channel(log_channel_id) or bot.get_channel(log_channel_id)
    else:
        log_channel = None

    if log_channel and isinstance(log_channel, discord.abc.Messageable):
        shown_key = key_clean if SHOW_FULL_KEY_IN_LOG_CHANNEL else mask_key(key_clean)

        embed = discord.Embed(title="🔑 Key Redeemed", color=discord.Color.green())
        embed.add_field(name="User", value=f"{interaction.user} ({interaction.user.id})", inline=False)
        embed.add_field(name="Product", value=product.upper(), inline=False)
        embed.add_field(name="Key", value=f"`{shown_key}`", inline=False)
        embed.add_field(name="Key Hash (sha256)", value=f"`{key_hash}`", inline=False)
        embed.add_field(name="Time", value=utc_now_str(), inline=False)
        embed.set_footer(text=f"Guild: {interaction.guild.name} ({interaction.guild.id})")

        try:
            await log_channel.send(embed=embed)
        except Exception:
            pass

# ---------- OWNER / ADMIN COMMANDS ----------

@bot.tree.command(name="setlogchannel", description="Set the log channel for this server (OWNER ONLY)")
@app_commands.describe(channel="Channel to send redemption logs to (or leave empty to disable)")
@app_commands.guild_only()
async def setlogchannel(interaction: discord.Interaction, channel: discord.TextChannel | None):
    if not is_owner(interaction):
        await safe_respond(interaction, "❌ No permission.", True)
        return

    guild_id = interaction.guild.id
    if channel is None:
        await set_log_channel_id(guild_id, None)
        await safe_respond(interaction, "✅ Log channel disabled for this server.", True)
        return

    await set_log_channel_id(guild_id, channel.id)
    await safe_respond(interaction, f"✅ Log channel set to {channel.mention}", True)

@bot.tree.command(name="addkey", description="Generate one-time-use keys (OWNER ONLY)")
@app_commands.describe(product="Product name (premium / vip / lifetime)", amount="Number of keys to generate")
@app_commands.guild_only()
async def addkey(interaction: discord.Interaction, product: str, amount: int):
    if not is_owner(interaction):
        await safe_respond(interaction, "❌ No permission.", True)
        return

    product_clean = product.strip().lower()
    if product_clean not in ROLE_MAP:
        await safe_respond(interaction, f"❌ Unknown product. Use one of: {', '.join(ROLE_MAP.keys())}", True)
        return

    if amount < 1 or amount > 50:
        await safe_respond(interaction, "❌ Amount must be between 1 and 50.", True)
        return

    keys = []
    async with aiosqlite.connect(DB_FILE) as db:
        for _ in range(amount):
            key, key_hash = generate_key()
            await db.execute(
                "INSERT INTO keys (key_hash, product, redeemed) VALUES (?, ?, 0)",
                (key_hash, product_clean)
            )
            keys.append(key)
        await db.commit()

    await safe_respond(
        interaction,
        f"🔐 **{amount} ONE-TIME KEY(S) GENERATED**\n"
        f"📦 Product: **{product_clean.upper()}**\n\n"
        f"```{chr(10).join(keys)}```\n"
        f"⚠️ Save these now. They cannot be recovered (only hashes are stored).",
        True
    )

@bot.tree.command(name="keystats", description="View key usage stats (OWNER ONLY)")
@app_commands.guild_only()
async def keystats(interaction: discord.Interaction):
    if not is_owner(interaction):
        await safe_respond(interaction, "❌ No permission.", True)
        return

    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute("""
            SELECT product,
                   SUM(CASE WHEN redeemed=1 THEN 1 ELSE 0 END) AS used,
                   SUM(CASE WHEN redeemed=0 THEN 1 ELSE 0 END) AS unused,
                   COUNT(*) AS total
            FROM keys
            GROUP BY product
            ORDER BY product
        """)
        rows = await cur.fetchall()

    if not rows:
        await safe_respond(interaction, "No keys in database.", True)
        return

    lines = ["PRODUCT | USED | UNUSED | TOTAL"]
    for product, used, unused, total in rows:
        lines.append(f"{product.upper():8} | {used:4} | {unused:6} | {total:5}")

    await safe_respond(interaction, f"```{chr(10).join(lines)}```", True)

@bot.tree.command(name="lookupkey", description="Lookup a key redemption by providing the key (OWNER ONLY)")
@app_commands.describe(key="The license key to lookup")
@app_commands.guild_only()
async def lookupkey(interaction: discord.Interaction, key: str):
    if not is_owner(interaction):
        await safe_respond(interaction, "❌ No permission.", True)
        return

    key_clean = key.strip().upper()
    key_hash = sha256_hex(key_clean)

    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute("SELECT product, redeemed FROM keys WHERE key_hash = ?", (key_hash,))
        krow = await cur.fetchone()

        if not krow:
            await safe_respond(interaction, "❌ Key not found in database.", True)
            return

        product, redeemed = krow

        cur2 = await db.execute("""
            SELECT user_id, redeemed_at_utc, guild_id
            FROM redemptions
            WHERE key_hash = ?
            ORDER BY id DESC
            LIMIT 1
        """, (key_hash,))
        rrow = await cur2.fetchone()

    if redeemed == 0:
        await safe_respond(
            interaction,
            f"✅ Key exists.\nProduct: **{product.upper()}**\nStatus: **UNUSED**\nHash: `{key_hash}`",
            True
        )
        return

    if not rrow:
        # Shouldn't happen, but handle it
        await safe_respond(
            interaction,
            f"⚠️ Key is marked as USED but no redemption log was found.\nProduct: **{product.upper()}**\nHash: `{key_hash}`",
            True
        )
        return

    user_id, redeemed_at_utc, guild_id = rrow
    await safe_respond(
        interaction,
        f"✅ Key lookup result:\n"
        f"Product: **{product.upper()}**\n"
        f"Status: **USED**\n"
        f"Redeemed by: `{user_id}`\n"
        f"Redeemed at: **{redeemed_at_utc}**\n"
        f"Guild ID: `{guild_id}`\n"
        f"Hash: `{key_hash}`",
        True
    )

@bot.tree.command(name="recentredemptions", description="Show recent redemptions (OWNER ONLY)")
@app_commands.describe(limit="How many to show (max 20)")
@app_commands.guild_only()
async def recentredemptions(interaction: discord.Interaction, limit: int = 10):
    if not is_owner(interaction):
        await safe_respond(interaction, "❌ No permission.", True)
        return

    if limit < 1:
        limit = 1
    if limit > 20:
        limit = 20

    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute("""
            SELECT redeemed_at_utc, product, user_id, guild_id, key_hash
            FROM redemptions
            ORDER BY id DESC
            LIMIT ?
        """, (limit,))
        rows = await cur.fetchall()

    if not rows:
        await safe_respond(interaction, "No redemptions yet.", True)
        return

    lines = []
    for redeemed_at_utc, product, user_id, guild_id, key_hash in rows:
        lines.append(f"{redeemed_at_utc} | {product.upper():8} | user:{user_id} | guild:{guild_id} | {key_hash[:12]}...")

    await safe_respond(interaction, f"```{chr(10).join(lines)}```", True)

# ---------- RUN ----------

bot.run(TOKEN)
