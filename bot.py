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

LOG_CHANNEL_ID = 123456789012345678  # 👈 CHANGE THIS TO YOUR LOG CHANNEL

DB_DIR = "/app/data"
DB_FILE = f"{DB_DIR}/keys.db"

ROLE_MAP = {
    "premium": "Premium",
    "vip": "VIP",
    "lifetime": "Lifetime"
}

# =========================================

os.makedirs(DB_DIR, exist_ok=True)

TOKEN = os.environ.get("TOKEN")
if not TOKEN:
    raise RuntimeError("TOKEN not found in Railway Variables")

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

# ---------- DATABASE ----------

async def init_db():
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS keys (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key_hash TEXT UNIQUE NOT NULL,
            product TEXT NOT NULL,
            redeemed INTEGER DEFAULT 0
        )
        """)
        await db.commit()

# ---------- UTILS ----------

def generate_key():
    raw = secrets.token_urlsafe(32)
    key = raw.upper()
    key_hash = hashlib.sha256(key.encode()).hexdigest()
    return key, key_hash

def is_owner(interaction: discord.Interaction):
    return interaction.user.id == OWNER_ID

def utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

# ---------- EVENTS ----------

@bot.event
async def on_ready():
    await init_db()
    await bot.tree.sync()
    print("✅ Bot is online")

# ---------- USER COMMAND ----------

@bot.tree.command(name="redeem", description="Redeem a one-time license key")
@app_commands.describe(key="Your license key")
async def redeem(interaction: discord.Interaction, key: str):

    key_clean = key.upper()
    key_hash = hashlib.sha256(key_clean.encode()).hexdigest()

    async with aiosqlite.connect(DB_FILE) as db:
        cursor = await db.execute(
            "SELECT product, redeemed FROM keys WHERE key_hash = ?",
            (key_hash,)
        )
        row = await cursor.fetchone()

        if not row:
            await interaction.response.send_message("❌ Invalid key.", ephemeral=True)
            return

        product, redeemed = row

        if redeemed == 1:
            await interaction.response.send_message("❌ This key has already been used.", ephemeral=True)
            return

        await db.execute(
            "UPDATE keys SET redeemed = 1 WHERE key_hash = ?",
            (key_hash,)
        )
        await db.commit()

    # Assign role
    role_name = ROLE_MAP.get(product.lower())
    if role_name:
        role = discord.utils.get(interaction.guild.roles, name=role_name)
        if role:
            await interaction.user.add_roles(role)

    await interaction.response.send_message(
        f"✅ You redeemed **{product.upper()}**",
        ephemeral=True
    )

    # ---------- LOGGING ----------

    log_channel = bot.get_channel(LOG_CHANNEL_ID)
    if log_channel:
        embed = discord.Embed(
            title="🔑 Key Redeemed",
            color=discord.Color.green()
        )
        embed.add_field(name="User", value=f"{interaction.user} ({interaction.user.id})", inline=False)
        embed.add_field(name="Key", value=f"`{key_clean}`", inline=False)
        embed.add_field(name="Product", value=product.upper(), inline=False)
        embed.add_field(name="Time", value=utc_now(), inline=False)

        await log_channel.send(embed=embed)

# ---------- ADMIN COMMANDS ----------

@bot.tree.command(
    name="addkey",
    description="Generate one-time-use keys (OWNER ONLY)"
)
@app_commands.describe(
    product="Product name (premium / vip / lifetime)",
    amount="Number of keys to generate"
)
async def addkey(interaction: discord.Interaction, product: str, amount: int):

    if not is_owner(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True)
        return

    if amount < 1 or amount > 50:
        await interaction.response.send_message("❌ Amount must be between 1 and 50.", ephemeral=True)
        return

    keys = []

    async with aiosqlite.connect(DB_FILE) as db:
        for _ in range(amount):
            key, key_hash = generate_key()
            await db.execute(
                "INSERT INTO keys (key_hash, product) VALUES (?, ?)",
                (key_hash, product.lower())
            )
            keys.append(key)
        await db.commit()

    await interaction.response.send_message(
        f"🔐 **{amount} ONE-TIME KEY(S) GENERATED**\n"
        f"📦 Product: **{product.upper()}**\n\n"
        f"```{chr(10).join(keys)}```\n"
        f"⚠️ Save these now. They cannot be recovered.",
        ephemeral=True
    )

@bot.tree.command(
    name="listkeys",
    description="View key usage stats (OWNER ONLY)"
)
async def listkeys(interaction: discord.Interaction):

    if not is_owner(interaction):
        await interaction.response.send_message("❌ No permission.", ephemeral=True)
        return

    async with aiosqlite.connect(DB_FILE) as db:
        cursor = await db.execute(
            "SELECT product, redeemed FROM keys"
        )
        rows = await cursor.fetchall()

    if not rows:
        await interaction.response.send_message("No keys in database.", ephemeral=True)
        return

    text = "\n".join(
        f"{product.upper()} | {'USED' if redeemed else 'UNUSED'}"
        for product, redeemed in rows
    )

    await interaction.response.send_message(f"```{text}```", ephemeral=True)

# ---------- RUN ----------

bot.run(TOKEN)
