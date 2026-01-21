import os
import re
import json
import discord
import aiosqlite
import hashlib
import secrets
import aiohttp
from datetime import datetime, timezone
from discord.ext import commands
from discord import app_commands

# ================== CONFIG ==================

OWNER_ID = 739411481342509059

DB_DIR = "/app/data"
DB_FILE = f"{DB_DIR}/keys.db"

# Security: if set, bot will only stay in these servers.
# Put your server IDs here (recommended). Example: {123, 456}
# If empty set(), bot will allow any server (but still OWNER-only commands).
ALLOWED_GUILD_IDS = set()  # <-- add your server IDs here

# If False, the log channel only sees masked keys (recommended)
SHOW_FULL_KEY_IN_LOG_CHANNEL = False

# SellAuth (optional)
SELLAUTH_TOKEN = os.environ.get("SELLAUTH_TOKEN")   # your SellAuth API key
SELLAUTH_SHOP_ID = os.environ.get("SELLAUTH_SHOP_ID")  # numeric/internal shop id (NOT the shop name)

# ============================================

os.makedirs(DB_DIR, exist_ok=True)

TOKEN = os.environ.get("TOKEN")
if not TOKEN:
    raise RuntimeError("TOKEN not found in environment variables")

intents = discord.Intents.default()  # no privileged intents -> no crash
bot = commands.Bot(command_prefix="!", intents=intents)


# ------------------ DB INIT ------------------

async def init_db():
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS products (
            name TEXT PRIMARY KEY,
            role_id INTEGER,
            created_at_utc TEXT NOT NULL
        )
        """)

        await db.execute("""
        CREATE TABLE IF NOT EXISTS keys (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key_hash TEXT UNIQUE NOT NULL,
            product_name TEXT NOT NULL,
            redeemed INTEGER DEFAULT 0,
            created_at_utc TEXT NOT NULL,
            FOREIGN KEY(product_name) REFERENCES products(name)
        )
        """)

        await db.execute("""
        CREATE TABLE IF NOT EXISTS redemptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key_hash TEXT NOT NULL,
            product_name TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            guild_id INTEGER NOT NULL,
            redeemed_at_utc TEXT NOT NULL
        )
        """)

        await db.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            guild_id INTEGER PRIMARY KEY,
            log_channel_id INTEGER
        )
        """)

        await db.execute("""
        CREATE TABLE IF NOT EXISTS sellauth_map (
            product_name TEXT PRIMARY KEY,
            sellauth_product_id TEXT NOT NULL,
            sellauth_variant_id TEXT NOT NULL
        )
        """)

        await db.commit()


# ------------------ UTILS ------------------

def utc_now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

def sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()

def generate_key():
    raw = secrets.token_urlsafe(32)
    key = raw.upper()
    return key, sha256_hex(key)

def mask_key(key: str) -> str:
    k = key.strip().upper()
    if len(k) <= 10:
        return k
    return f"{k[:4]}...{k[-4:]}"

def normalize_product_name(name: str) -> str:
    name = name.strip().lower()
    name = re.sub(r"\s+", "-", name)
    name = re.sub(r"[^a-z0-9\-_]", "", name)
    return name

async def safe_send(i: discord.Interaction, content: str, ephemeral: bool = True):
    try:
        if i.response.is_done():
            await i.followup.send(content, ephemeral=ephemeral)
        else:
            await i.response.send_message(content, ephemeral=ephemeral)
    except Exception:
        pass

async def set_log_channel_id(guild_id: int, channel_id: int | None):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("""
        INSERT INTO settings (guild_id, log_channel_id)
        VALUES (?, ?)
        ON CONFLICT(guild_id) DO UPDATE SET log_channel_id=excluded.log_channel_id
        """, (guild_id, channel_id))
        await db.commit()

async def get_log_channel_id(guild_id: int) -> int | None:
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute("SELECT log_channel_id FROM settings WHERE guild_id=?", (guild_id,))
        row = await cur.fetchone()
        return row[0] if row and row[0] else None

async def get_product(product_name: str):
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute("SELECT name, role_id FROM products WHERE name=?", (product_name,))
        return await cur.fetchone()

async def log_redeem(guild: discord.Guild, user: discord.abc.User, product_name: str, key_clean: str, key_hash: str):
    log_channel_id = await get_log_channel_id(guild.id)
    if not log_channel_id:
        return

    channel = guild.get_channel(log_channel_id) or bot.get_channel(log_channel_id)
    if not channel:
        return

    shown_key = key_clean if SHOW_FULL_KEY_IN_LOG_CHANNEL else mask_key(key_clean)

    embed = discord.Embed(title="🔑 Key Redeemed", color=discord.Color.green())
    embed.add_field(name="User", value=f"{user} ({user.id})", inline=False)
    embed.add_field(name="Product", value=product_name, inline=False)
    embed.add_field(name="Key", value=f"`{shown_key}`", inline=False)
    embed.add_field(name="Key Hash (sha256)", value=f"`{key_hash}`", inline=False)
    embed.add_field(name="Time", value=utc_now_str(), inline=False)
    embed.set_footer(text=f"Guild: {guild.name} ({guild.id})")

    try:
        await channel.send(embed=embed)
    except Exception:
        pass


# ------------------ GLOBAL OWNER LOCK ------------------
# This makes EVERY slash command only usable by OWNER_ID.
# Also blocks usage in non-allowed guilds (if you set ALLOWED_GUILD_IDS).

@bot.tree.interaction_check
async def global_owner_lock(interaction: discord.Interaction) -> bool:
    # Block usage outside allowed guilds (if allowlist is set)
    if interaction.guild and ALLOWED_GUILD_IDS:
        if interaction.guild.id not in ALLOWED_GUILD_IDS:
            return False

    # Only you can use commands
    return interaction.user.id == OWNER_ID


# ------------------ SELLAUTH HELPERS ------------------

async def sellauth_append_deliverables(product_id: str, variant_id: str, deliverables: list[str]):
    """
    Append deliverables to a SellAuth product variant.
    This requires:
      - SELLAUTH_TOKEN
      - SELLAUTH_SHOP_ID (internal numeric/id, not shop name)
    """
    if not SELLAUTH_TOKEN or not SELLAUTH_SHOP_ID:
        raise RuntimeError("Missing SELLAUTH_TOKEN or SELLAUTH_SHOP_ID in environment variables")

    url = f"https://api.sellauth.com/v1/shops/{SELLAUTH_SHOP_ID}/products/{product_id}/deliverables/append/{variant_id}"
    headers = {
        "Authorization": f"Bearer {SELLAUTH_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {"deliverables": deliverables}

    async with aiohttp.ClientSession() as session:
        async with session.put(url, headers=headers, data=json.dumps(payload)) as resp:
            text = await resp.text()
            if resp.status >= 300:
                raise RuntimeError(f"SellAuth API error {resp.status}: {text}")
            return text


# ------------------ EVENTS ------------------

@bot.event
async def on_ready():
    await init_db()
    try:
        await bot.tree.sync()
    except Exception as e:
        print("⚠️ tree.sync failed:", e)

    print(f"✅ Bot is online as {bot.user}")

@bot.event
async def on_guild_join(guild: discord.Guild):
    # If allowlist set and this guild isn't allowed -> leave immediately
    if ALLOWED_GUILD_IDS and guild.id not in ALLOWED_GUILD_IDS:
        try:
            await guild.leave()
        except Exception:
            pass


# ------------------ COMMANDS ------------------

@bot.tree.command(name="redeem", description="Redeem a one-time license key (OWNER ONLY)")
@app_commands.describe(key="Your license key")
@app_commands.guild_only()
async def redeem(i: discord.Interaction, key: str):
    if not i.guild:
        await safe_send(i, "❌ Use this in a server.", True)
        return

    key_clean = key.strip().upper()
    key_hash = sha256_hex(key_clean)

    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute(
            "SELECT product_name, redeemed FROM keys WHERE key_hash=?",
            (key_hash,)
        )
        row = await cur.fetchone()
        if not row:
            await safe_send(i, "❌ Invalid key.", True)
            return

        product_name, redeemed_flag = row
        if redeemed_flag == 1:
            await safe_send(i, "❌ This key has already been used.", True)
            return

        await db.execute("UPDATE keys SET redeemed=1 WHERE key_hash=?", (key_hash,))
        await db.execute("""
            INSERT INTO redemptions (key_hash, product_name, user_id, guild_id, redeemed_at_utc)
            VALUES (?, ?, ?, ?, ?)
        """, (key_hash, product_name, i.user.id, i.guild.id, utc_now_str()))
        await db.commit()

    # Role assignment (if product has role_id)
    role_added = False
    prod = await get_product(product_name)
    role_id = prod[1] if prod else None

    if role_id:
        role = i.guild.get_role(int(role_id))
        if role:
            try:
                member = i.user if isinstance(i.user, discord.Member) else i.guild.get_member(i.user.id)
                if member:
                    await member.add_roles(role, reason=f"Redeemed product: {product_name}")
                    role_added = True
            except Exception:
                role_added = False

    msg = f"✅ Redeemed: **{product_name}**"
    if role_id and role_added:
        msg += f"\n🎭 Role added: <@&{role_id}>"
    elif role_id and not role_added:
        msg += "\n⚠️ Couldn’t add role. Check bot permissions + role hierarchy."

    await safe_send(i, msg, True)
    await log_redeem(i.guild, i.user, product_name, key_clean, key_hash)


@bot.tree.command(name="setlogchannel", description="Set redemption log channel (OWNER ONLY)")
@app_commands.describe(channel="Channel for logs (empty = disable)")
@app_commands.guild_only()
async def setlogchannel(i: discord.Interaction, channel: discord.TextChannel | None):
    await set_log_channel_id(i.guild.id, channel.id if channel else None)
    await safe_send(i, f"✅ Log channel set to {channel.mention}" if channel else "✅ Log channel disabled.", True)


@bot.tree.command(name="product_add", description="Create/update a product name (OWNER ONLY)")
@app_commands.describe(name="Any product name you want", role="Optional role to grant on redeem")
@app_commands.guild_only()
async def product_add(i: discord.Interaction, name: str, role: discord.Role | None = None):
    pname = normalize_product_name(name)
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("""
            INSERT INTO products (name, role_id, created_at_utc)
            VALUES (?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET role_id=excluded.role_id
        """, (pname, role.id if role else None, utc_now_str()))
        await db.commit()
    await safe_send(i, f"✅ Product saved: **{pname}**" + (f" → role <@&{role.id}>" if role else ""), True)


@bot.tree.command(name="product_remove", description="Delete a product (OWNER ONLY)")
@app_commands.describe(name="Product name")
@app_commands.guild_only()
async def product_remove(i: discord.Interaction, name: str):
    pname = normalize_product_name(name)
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("DELETE FROM products WHERE name=?", (pname,))
        await db.execute("DELETE FROM sellauth_map WHERE product_name=?", (pname,))
        await db.commit()
    await safe_send(i, f"✅ Product removed: **{pname}**", True)


@bot.tree.command(name="products", description="List products (OWNER ONLY)")
@app_commands.guild_only()
async def products(i: discord.Interaction):
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute("SELECT name, role_id FROM products ORDER BY name")
        rows = await cur.fetchall()

    if not rows:
        await safe_send(i, "No products yet. Use `/product_add`.", True)
        return

    lines = []
    for name, role_id in rows:
        lines.append(f"{name:25} | role: {role_id if role_id else 'none'}")

    await safe_send(i, "```" + "\n".join(lines) + "```", True)


@bot.tree.command(name="genkeys", description="Generate keys for ANY product (OWNER ONLY)")
@app_commands.describe(product="Product name", amount="1-50 keys")
@app_commands.guild_only()
async def genkeys(i: discord.Interaction, product: str, amount: int):
    pname = normalize_product_name(product)
    if amount < 1 or amount > 50:
        await safe_send(i, "❌ Amount must be 1-50.", True)
        return

    prod = await get_product(pname)
    if not prod:
        await safe_send(i, f"❌ Product not found: **{pname}**. Create it with `/product_add` first.", True)
        return

    keys = []
    async with aiosqlite.connect(DB_FILE) as db:
        for _ in range(amount):
            k, kh = generate_key()
            await db.execute("""
                INSERT INTO keys (key_hash, product_name, redeemed, created_at_utc)
                VALUES (?, ?, 0, ?)
            """, (kh, pname, utc_now_str()))
            keys.append(k)
        await db.commit()

    await safe_send(
        i,
        f"🔐 Generated **{amount}** key(s) for **{pname}**\n"
        f"```{chr(10).join(keys)}```\n"
        f"⚠️ Save now (plaintext keys aren’t stored).",
        True
    )


@bot.tree.command(name="keystats", description="Key usage stats (OWNER ONLY)")
@app_commands.guild_only()
async def keystats(i: discord.Interaction):
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute("""
            SELECT product_name,
                   SUM(CASE WHEN redeemed=1 THEN 1 ELSE 0 END) AS used,
                   SUM(CASE WHEN redeemed=0 THEN 1 ELSE 0 END) AS unused,
                   COUNT(*) AS total
            FROM keys
            GROUP BY product_name
            ORDER BY product_name
        """)
        rows = await cur.fetchall()

    if not rows:
        await safe_send(i, "No keys in database.", True)
        return

    lines = ["PRODUCT                 | USED | UNUSED | TOTAL"]
    for p, used, unused, total in rows:
        lines.append(f"{p:22} | {used:4} | {unused:6} | {total:5}")

    await safe_send(i, "```" + "\n".join(lines) + "```", True)


@bot.tree.command(name="recentredemptions", description="Show recent redemptions (OWNER ONLY)")
@app_commands.describe(limit="1-20")
@app_commands.guild_only()
async def recentredemptions(i: discord.Interaction, limit: int = 10):
    limit = max(1, min(20, limit))

    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute("""
            SELECT redeemed_at_utc, product_name, user_id, guild_id, key_hash
            FROM redemptions
            ORDER BY id DESC
            LIMIT ?
        """, (limit,))
        rows = await cur.fetchall()

    if not rows:
        await safe_send(i, "No redemptions yet.", True)
        return

    lines = []
    for t, p, uid, gid, kh in rows:
        lines.append(f"{t} | {p:20} | user:{uid} | guild:{gid} | {kh[:12]}...")

    await safe_send(i, "```" + "\n".join(lines) + "```", True)


@bot.tree.command(name="lookupkey", description="Lookup a key (OWNER ONLY)")
@app_commands.describe(key="Plaintext key to lookup")
@app_commands.guild_only()
async def lookupkey(i: discord.Interaction, key: str):
    key_clean = key.strip().upper()
    key_hash = sha256_hex(key_clean)

    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute("SELECT product_name, redeemed FROM keys WHERE key_hash=?", (key_hash,))
        krow = await cur.fetchone()

        if not krow:
            await safe_send(i, "❌ Key not found.", True)
            return

        product_name, redeemed_flag = krow

        cur2 = await db.execute("""
            SELECT user_id, guild_id, redeemed_at_utc
            FROM redemptions
            WHERE key_hash=?
            ORDER BY id DESC LIMIT 1
        """, (key_hash,))
        rrow = await cur2.fetchone()

    if redeemed_flag == 0:
        await safe_send(i, f"✅ Key exists: **{product_name}** | **UNUSED**\nHash: `{key_hash}`", True)
        return

    if not rrow:
        await safe_send(i, f"⚠️ Marked USED but no log found.\nProduct: **{product_name}**\nHash: `{key_hash}`", True)
        return

    uid, gid, t = rrow
    await safe_send(
        i,
        f"✅ Key is **USED**\nProduct: **{product_name}**\nRedeemed by: `{uid}`\nGuild: `{gid}`\nTime: **{t}**\nHash: `{key_hash}`",
        True
    )


# ------------------ SELLAUTH COMMANDS (OPTIONAL) ------------------

@bot.tree.command(name="sellauth_link", description="Link your product name to SellAuth product/variant (OWNER ONLY)")
@app_commands.describe(product="Your product name", sellauth_product_id="SellAuth productId", sellauth_variant_id="SellAuth variantId")
@app_commands.guild_only()
async def sellauth_link(i: discord.Interaction, product: str, sellauth_product_id: str, sellauth_variant_id: str):
    pname = normalize_product_name(product)
    prod = await get_product(pname)
    if not prod:
        await safe_send(i, f"❌ Product not found: **{pname}**. Create it with `/product_add` first.", True)
        return

    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("""
            INSERT INTO sellauth_map (product_name, sellauth_product_id, sellauth_variant_id)
            VALUES (?, ?, ?)
            ON CONFLICT(product_name) DO UPDATE SET
                sellauth_product_id=excluded.sellauth_product_id,
                sellauth_variant_id=excluded.sellauth_variant_id
        """, (pname, sellauth_product_id.strip(), sellauth_variant_id.strip()))
        await db.commit()

    await safe_send(i, f"✅ Linked **{pname}** → SellAuth product `{sellauth_product_id}` variant `{sellauth_variant_id}`", True)


@bot.tree.command(name="sellauth_pushkeys", description="Generate keys and append to SellAuth deliverables (OWNER ONLY)")
@app_commands.describe(product="Your product name", amount="1-50 keys")
@app_commands.guild_only()
async def sellauth_pushkeys(i: discord.Interaction, product: str, amount: int):
    pname = normalize_product_name(product)
    if amount < 1 or amount > 50:
        await safe_send(i, "❌ Amount must be 1-50.", True)
        return

    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute("SELECT sellauth_product_id, sellauth_variant_id FROM sellauth_map WHERE product_name=?", (pname,))
        m = await cur.fetchone()

    if not m:
        await safe_send(i, f"❌ Not linked. Use `/sellauth_link {pname} <productId> <variantId>` first.", True)
        return

    s_product_id, s_variant_id = m

    deliverables = []
    async with aiosqlite.connect(DB_FILE) as db:
        for _ in range(amount):
            k, kh = generate_key()
            await db.execute("""
                INSERT INTO keys (key_hash, product_name, redeemed, created_at_utc)
                VALUES (?, ?, 0, ?)
            """, (kh, pname, utc_now_str()))
            deliverables.append(k)
        await db.commit()

    try:
        await sellauth_append_deliverables(s_product_id, s_variant_id, deliverables)
    except Exception as e:
        await safe_send(i, f"❌ SellAuth push failed: `{e}`\nKeys were still generated and saved in DB.", True)
        return

    await safe_send(
        i,
        f"✅ Pushed **{amount}** key(s) to SellAuth deliverables for **{pname}**\n"
        f"(SellAuth product `{s_product_id}` variant `{s_variant_id}`)",
        True
    )


# ------------------ RUN ------------------
bot.run(TOKEN)
