import os
import json
import sqlite3
import asyncio
from datetime import datetime, timedelta, timezone
from functools import wraps
from typing import Optional, Dict, List
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup
from telegram.error import Forbidden
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
from telegram.constants import ParseMode

# ==================== CONFIGURATION ====================
TOKEN = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
ADMIN_IDS = [int(x) for x in os.environ.get("ADMIN_IDS", "7892255798").split(",")]
CHANNELS = ["@SRK_ERA", "@SRKING000001", "@SRK_IMP1"]

DB_PATH = "bot_data.db"

# Optional: set this in Railway's Variables tab (not just via the admin panel)
# so the channel ID survives a full filesystem wipe - that's the one thing
# that isn't stored inside bot_data.db itself, since it's needed BEFORE that
# file exists again after a wipe.
DB_CHANNEL_ENV = os.environ.get("DB_CHANNEL_ID")

# Telegram "message effects" - the animated overlay you get from long-pressing
# the send button (🔥 fire, ❤️ heart, 🎉 confetti etc). These only work in
# private chats, which is all this bot ever sends to, so they're used across
# most of the bot's replies to make it feel more alive.
EFFECT_FIRE = "5104841245755180586"
EFFECT_THUMBS_UP = "5107584321108051014"
EFFECT_THUMBS_DOWN = "5104858069142078462"
EFFECT_HEART = "5159385139981059251"
EFFECT_PARTY = "5046509860389126442"
EFFECT_POOP = "5046589136895476101"

# The server this bot runs on may be set to UTC, but all users are in India.
# Every "current time" the bot stores or displays (redeem expiry, join dates,
# purchase times, etc.) goes through this so it always matches real IST clock
# time - previously it used the server's raw clock, which showed expiry times
# hours off from what people actually saw on their phones.
IST = timezone(timedelta(hours=5, minutes=30))

def now_ist():
    return datetime.now(timezone.utc).astimezone(IST).replace(tzinfo=None)

# ==================== DATABASE ====================
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        credits INTEGER DEFAULT 0,
        referrer_id INTEGER,
        joined_date TEXT,
        total_referrals INTEGER DEFAULT 0,
        banned INTEGER DEFAULT 0
    )''')

    # Migration for DBs created before the 'banned' column existed.
    try:
        c.execute("ALTER TABLE users ADD COLUMN banned INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    
    c.execute('''CREATE TABLE IF NOT EXISTS referrals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        referrer_id INTEGER,
        referee_id INTEGER,
        points_earned INTEGER,
        timestamp TEXT,
        status TEXT DEFAULT 'pending'
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS inventory (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        item_id INTEGER,
        item_name TEXT,
        guest_uid TEXT,
        guest_pass TEXT,
        timestamp TEXT,
        sold INTEGER DEFAULT 0
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS purchases (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        item_id INTEGER,
        item_name TEXT,
        guest_uid TEXT,
        guest_pass TEXT,
        purchase_date TEXT
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS daily_bonus (
        user_id INTEGER PRIMARY KEY,
        last_claim_date TEXT
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS admin_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        admin_id INTEGER,
        action TEXT,
        target TEXT,
        details TEXT,
        timestamp TEXT
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS redeem_codes (
        code TEXT PRIMARY KEY,
        credits INTEGER NOT NULL,
        expiry TEXT,
        max_uses INTEGER DEFAULT 1,
        used_count INTEGER DEFAULT 0,
        created_by INTEGER,
        created_at TEXT,
        active INTEGER DEFAULT 1
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS redeem_code_uses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT,
        user_id INTEGER,
        timestamp TEXT
    )''')
    
    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('refer_points', '10')")
    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('daily_bonus', '20')")

    # Migration for item-specific redeem codes (existing DBs won't have these columns).
    try:
        c.execute("ALTER TABLE redeem_codes ADD COLUMN item_id INTEGER")
    except sqlite3.OperationalError:
        pass
    try:
        c.execute("ALTER TABLE redeem_codes ADD COLUMN item_name TEXT")
    except sqlite3.OperationalError:
        pass
    
    special_items = [
        (203054011, "DARK DESIRE BUNDLE", 50),
        (909054007, "Volcanic Fury", 30),
        (909854001, "Pride Judgment", 30),
        (907105450, "Gloo Wall - Slothful Desire", 30),
        (911005401, "Boat of Luxury", 30),
        (907104730, "Katana - Spiky Desire", 25),
        (907105424, "Katana - Loving Desire", 25),
        (907105405, "M82B - Envious Desire", 35),
        (903054002, "Loot Box - Envious Desire", 20),
        (903054003, "Loot Box - Furious Desire", 20),
        (903054004, "Loot Box - Gluttonous Desire", 20),
        (903054005, "Loot Box - Luxurious Desire", 20),
        (903054006, "Loot Box - Slothful Desire", 20)
    ]
    for item_id, name, price in special_items:
        c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (f"price_{item_id}", str(price)))
    
    conn.commit()
    conn.close()

def get_setting(key):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT value FROM settings WHERE key = ?", (key,))
    result = c.fetchone()
    conn.close()
    return result[0] if result else None

def update_setting(key, value):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
    conn.commit()
    conn.close()

# ==================== DATABASE AUTO-BACKUP (Telegram channel) ====================

def get_db_channel():
    """Env var wins (survives a full disk wipe); falls back to whatever the
    admin set at runtime via the admin panel."""
    if DB_CHANNEL_ENV:
        return DB_CHANNEL_ENV
    return get_setting("db_channel_id")

async def restore_db_from_channel(bot):
    """Runs once at startup, before init_db(). If bot_data.db already exists
    locally, it's left untouched. If it's missing (fresh deploy, no
    persistent volume), pulls the most recent backup from the pinned message
    in the database channel and uses that as the starting DB."""
    if os.path.exists(DB_PATH):
        return
    channel_id = DB_CHANNEL_ENV
    if not channel_id:
        return
    try:
        chat = await bot.get_chat(channel_id)
        pinned = chat.pinned_message
        if pinned and pinned.document:
            file = await bot.get_file(pinned.document.file_id)
            await file.download_to_drive(DB_PATH)
            print("✅ Database restored from Telegram backup channel.")
        else:
            print("ℹ️ No pinned backup found in database channel, starting fresh.")
    except Exception as e:
        print(f"⚠️ Could not restore database from channel: {e}")

async def backup_db_to_channel(bot):
    channel_id = get_db_channel()
    if not channel_id or not os.path.exists(DB_PATH):
        return
    try:
        with open(DB_PATH, "rb") as f:
            msg = await bot.send_document(
                channel_id, document=f, filename="bot_data_backup.db",
                caption=f"🗄 Auto Backup — {now_ist().strftime('%Y-%m-%d %H:%M:%S')} IST"
            )
        try:
            await bot.unpin_all_chat_messages(channel_id)
        except Exception:
            pass
        try:
            await bot.pin_chat_message(channel_id, msg.message_id, disable_notification=True)
        except Exception:
            pass
    except Exception as e:
        print(f"⚠️ Backup to channel failed: {e}")

async def backup_loop(bot):
    while True:
        await asyncio.sleep(1800)  # every 30 minutes
        await backup_db_to_channel(bot)

async def daily_summary_loop(bot):
    """Sends admins a daily summary every day at 7:00 PM IST."""
    while True:
        now = now_ist()
        target = now.replace(hour=19, minute=0, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())
        try:
            date_str = now_ist().strftime("%Y-%m-%d")
            stats = get_daily_summary(date_str)
            text = (
                f"📊 Daily Summary — {date_str}\n\n"
                f"👥 New Users Today: {stats['new_users']}\n"
                f"🛒 Purchases Today: {stats['purchases']}\n"
                f"🎟 Redeem Codes Used Today: {stats['redeems']}\n"
                f"👥 Total Users (all time): {stats['total_users']}"
            )
            for admin_id in ADMIN_IDS:
                try:
                    await bot.send_message(admin_id, text, message_effect_id=EFFECT_PARTY)
                except Exception:
                    pass
        except Exception as e:
            print(f"⚠️ Daily summary failed: {e}")
        await asyncio.sleep(60)  # don't double-fire within the same minute

def get_daily_summary(date_str):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM users WHERE joined_date LIKE ?", (f"{date_str}%",))
    new_users = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM purchases WHERE purchase_date LIKE ?", (f"{date_str}%",))
    purchases = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM redeem_code_uses WHERE timestamp LIKE ?", (f"{date_str}%",))
    redeems = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM users")
    total_users = c.fetchone()[0]
    conn.close()
    return {"new_users": new_users, "purchases": purchases, "redeems": redeems, "total_users": total_users}

# ==================== LOW STOCK ALERTS ====================

async def notify_low_stock_if_needed(context: ContextTypes.DEFAULT_TYPE, item_id, item_name):
    """Alerts ALL users (not admin) the first time an item's stock drops
    below 5. Resets itself once the item is restocked past that, so the
    next dip alerts again instead of staying silent forever."""
    available = get_inventory_count(item_id)
    flag_key = f"low_stock_alerted_{item_id}"
    if available < 5:
        if get_setting(flag_key) == "1":
            return
        update_setting(flag_key, "1")
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT user_id FROM users WHERE banned = 0")
        users = c.fetchall()
        conn.close()
        text = (
            f"⚠️ Stock Alert!\n\n"
            f"🎯 {item_name}\n"
            f"Sirf {available} bache hain! Jaldi purchase kar lo warna miss ho jayega. 🔥"
        )
        for u in users:
            try:
                await context.bot.send_message(u[0], text, message_effect_id=EFFECT_FIRE)
            except Exception:
                pass
            await asyncio.sleep(0.05)
    else:
        if get_setting(flag_key) == "1":
            update_setting(flag_key, "0")

# ==================== MAINTENANCE MODE ====================
# Cached in memory so checking it on every single message doesn't cost a DB
# round-trip - only a toggle writes through to the DB and refreshes the cache.
_bot_enabled_cache = {"value": None}

def get_bot_enabled():
    if _bot_enabled_cache["value"] is None:
        val = get_setting("bot_enabled")
        _bot_enabled_cache["value"] = (val is None or val == "1")
    return _bot_enabled_cache["value"]

def set_bot_enabled(enabled: bool):
    update_setting("bot_enabled", "1" if enabled else "0")
    _bot_enabled_cache["value"] = enabled

MAINTENANCE_MESSAGE = (
    "🚧 *Bot Under Development* 🚧\n\n"
    "This bot is currently being upgraded with new features.\n"
    "We'll be back online very soon — thanks for your patience! 🙏\n\n"
    "✨ Stay tuned!"
)

LIVE_MESSAGE = (
    "✅ *We're Back Online!* ✅\n\n"
    "The bot is now live and fully working again.\n"
    "🎉 Go ahead and start using it!"
)

BANNED_MESSAGE = (
    "🚫 *Aap is bot se banned hain.*\n\n"
    "Agar aapko lagta hai ye galti se hua hai, to admin se contact karein."
)

def get_user(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    result = c.fetchone()
    conn.close()
    if result:
        return {
            "user_id": result[0],
            "username": result[1],
            "first_name": result[2],
            "credits": result[3],
            "referrer_id": result[4],
            "joined_date": result[5],
            "total_referrals": result[6],
            "banned": result[7] if len(result) > 7 else 0
        }
    return None

def is_banned(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT banned FROM users WHERE user_id = ?", (user_id,))
    result = c.fetchone()
    conn.close()
    return bool(result and result[0])

def ban_user(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE users SET banned = 1 WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()

def unban_user(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE users SET banned = 0 WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()

def create_user(user_id, username, first_name, referrer_id=None):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    joined_date = now_ist().strftime("%Y-%m-%d %H:%M:%S")
    c.execute("INSERT OR IGNORE INTO users (user_id, username, first_name, credits, referrer_id, joined_date, total_referrals) VALUES (?, ?, ?, ?, ?, ?, ?)",
              (user_id, username, first_name, 0, referrer_id, joined_date, 0))
    if referrer_id:
        points = int(get_setting("refer_points") or 5)
        c.execute("UPDATE users SET credits = credits + ?, total_referrals = total_referrals + 1 WHERE user_id = ?", (points, referrer_id))
        c.execute("INSERT INTO referrals (referrer_id, referee_id, points_earned, timestamp, status) VALUES (?, ?, ?, ?, ?)",
                  (referrer_id, user_id, points, joined_date, 'completed'))
    conn.commit()
    conn.close()
    return get_user(user_id)

def add_credits(user_id, amount):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE users SET credits = credits + ? WHERE user_id = ?", (amount, user_id))
    conn.commit()
    conn.close()

def deduct_credits(user_id, amount):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT credits FROM users WHERE user_id = ?", (user_id,))
    result = c.fetchone()
    if result and result[0] >= amount:
        c.execute("UPDATE users SET credits = credits - ? WHERE user_id = ?", (amount, user_id))
        conn.commit()
        conn.close()
        return True
    conn.close()
    return False

def get_inventory_count(item_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM inventory WHERE item_id = ? AND sold = 0", (item_id,))
    result = c.fetchone()
    conn.close()
    return result[0] if result else 0

def assign_inventory_to_user(user_id, item_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, item_id, item_name, guest_uid, guest_pass, timestamp FROM inventory WHERE item_id = ? AND sold = 0 LIMIT 1", (item_id,))
    result = c.fetchone()
    if result:
        db_id, inv_item_id, item_name, guest_uid, guest_pass, timestamp = result
        c.execute("UPDATE inventory SET sold = 1 WHERE id = ?", (db_id,))
        purchase_date = now_ist().strftime("%Y-%m-%d %H:%M:%S")
        c.execute("INSERT INTO purchases (user_id, item_id, item_name, guest_uid, guest_pass, purchase_date) VALUES (?, ?, ?, ?, ?, ?)",
                  (user_id, inv_item_id, item_name, guest_uid, guest_pass, purchase_date))
        conn.commit()
        conn.close()
        return {
            "guest_uid": guest_uid,
            "guest_pass": guest_pass,
            "item_id": inv_item_id,
            "item_name": item_name,
            "timestamp": timestamp
        }
    conn.close()
    return None

def get_item_price(item_id):
    price = get_setting(f"price_{item_id}")
    return int(price) if price else 20

def get_user_purchases(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT item_id, item_name, guest_uid, guest_pass, purchase_date FROM purchases WHERE user_id = ? ORDER BY purchase_date DESC", (user_id,))
    results = c.fetchall()
    conn.close()
    return [{"item_id": r[0], "item_name": r[1], "guest_uid": r[2], "guest_pass": r[3], "purchase_date": r[4]} for r in results]

def get_top_referrers(limit=10):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT user_id, username, first_name, total_referrals FROM users WHERE total_referrals > 0 ORDER BY total_referrals DESC LIMIT ?", (limit,))
    results = c.fetchall()
    conn.close()
    return [{"user_id": r[0], "username": r[1], "first_name": r[2], "total_referrals": r[3]} for r in results]

def check_daily_bonus(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT last_claim_date FROM daily_bonus WHERE user_id = ?", (user_id,))
    result = c.fetchone()
    now = now_ist()
    if result:
        last_claim = datetime.strptime(result[0], "%Y-%m-%d %H:%M:%S")
        if (now - last_claim).total_seconds() < 86400:
            next_claim = last_claim + timedelta(days=1)
            conn.close()
            return False, next_claim.strftime("%H:%M:%S")
    bonus = int(get_setting("daily_bonus") or 10)
    c.execute("INSERT OR REPLACE INTO daily_bonus (user_id, last_claim_date) VALUES (?, ?)", (user_id, now.strftime("%Y-%m-%d %H:%M:%S")))
    c.execute("UPDATE users SET credits = credits + ? WHERE user_id = ?", (bonus, user_id))
    conn.commit()
    conn.close()
    return True, str(bonus)

def add_inventory_bulk(items):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    added = 0
    skipped = 0
    added_names = []  # track exactly which items were added, in order
    for item in items:
        c.execute("SELECT id FROM inventory WHERE guest_uid = ? AND guest_pass = ? AND item_id = ?", 
                  (item.get("guestUid"), item.get("guestPass"), item.get("item_id")))
        if c.fetchone():
            skipped += 1
            continue
        c.execute("INSERT INTO inventory (item_id, item_name, guest_uid, guest_pass, timestamp, sold) VALUES (?, ?, ?, ?, ?, ?)",
                  (item.get("item_id"), item.get("item_name"), item.get("guestUid"), item.get("guestPass"), item.get("timestamp"), 0))
        added += 1
        added_names.append(item.get("item_name"))
    conn.commit()
    conn.close()
    # Restocked items should be able to alert again next time they run low.
    for item in items:
        iid = item.get("item_id")
        if iid is not None:
            update_setting(f"low_stock_alerted_{iid}", "0")
    return {"added": added, "skipped": skipped, "added_names": added_names}

def get_available_inventory_export():
    """Returns all unsold inventory accounts in the same format used for uploads,
    so the admin can pull back exactly what's left in stock."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT item_id, item_name, guest_uid, guest_pass, timestamp FROM inventory WHERE sold = 0")
    results = c.fetchall()
    conn.close()
    return [
        {
            "item_id": r[0],
            "item_name": r[1],
            "guestUid": r[2],
            "guestPass": r[3],
            "timestamp": r[4]
        }
        for r in results
    ]

def reset_inventory():
    """Deletes every row from the inventory table (sold + unsold) to free up
    database storage. Does NOT touch users, credits, referrals, or settings/prices."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM inventory")
    total = c.fetchone()[0]
    c.execute("DELETE FROM inventory")
    conn.commit()
    conn.close()
    return total


# ==================== USERS DATABASE (separate from inventory) ====================
# This is a completely separate backup/restore system from the inventory one
# above. It never touches inventory rows, prices, or other settings - only the
# users table and the per-user tables that hang off it (referrals, purchases,
# daily_bonus, redeem_code_uses).

def get_users_export():
    """Returns every user currently in the database, in a format that can be
    fed straight back into add_users_bulk()."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT user_id, username, first_name, credits, referrer_id, joined_date, total_referrals, banned FROM users")
    rows = c.fetchall()
    conn.close()
    return [
        {
            "user_id": r[0],
            "username": r[1],
            "first_name": r[2],
            "credits": r[3],
            "referrer_id": r[4],
            "joined_date": r[5],
            "total_referrals": r[6],
            "banned": r[7]
        }
        for r in rows
    ]

def add_users_bulk(users_list):
    """Adds new users from an uploaded backup. Existing user_ids are skipped
    (never overwritten), so this only ever ADDS to what's already there - it
    can't be used to silently wipe or change someone's current credits."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    added = 0
    skipped = 0
    for u in users_list:
        uid = u.get("user_id")
        if uid is None:
            skipped += 1
            continue
        c.execute("SELECT user_id FROM users WHERE user_id = ?", (uid,))
        if c.fetchone():
            skipped += 1
            continue
        c.execute(
            """INSERT INTO users (user_id, username, first_name, credits, referrer_id, joined_date, total_referrals, banned)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                uid,
                u.get("username"),
                u.get("first_name"),
                u.get("credits", 0) or 0,
                u.get("referrer_id"),
                u.get("joined_date") or now_ist().strftime("%Y-%m-%d %H:%M:%S"),
                u.get("total_referrals", 0) or 0,
                u.get("banned", 0) or 0
            )
        )
        added += 1
    conn.commit()
    conn.close()
    return {"added": added, "skipped": skipped}

def reset_users_db():
    """Deletes ALL users and everything keyed by user_id (referrals, purchases,
    daily bonus claims, redeem code redemption history). Does NOT touch
    inventory stock, item prices, referral/bonus settings, redeem code
    definitions, or admin logs."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM users")
    total = c.fetchone()[0]
    c.execute("DELETE FROM users")
    c.execute("DELETE FROM referrals")
    c.execute("DELETE FROM purchases")
    c.execute("DELETE FROM daily_bonus")
    c.execute("DELETE FROM redeem_code_uses")
    conn.commit()
    conn.close()
    return total


def get_all_special_items():
    special_items = [
        (203054011, "DARK DESIRE BUNDLE"),
        (909054007, "Volcanic Fury"),
        (909854001, "Pride Judgment"),
        (907105450, "Gloo Wall - Slothful Desire"),
        (911005401, "Boat of Luxury"),
        (907104730, "Katana - Spiky Desire"),
        (907105424, "Katana - Loving Desire"),
        (907105405, "M82B - Envious Desire"),
        (903054002, "Loot Box - Envious Desire"),
        (903054003, "Loot Box - Furious Desire"),
        (903054004, "Loot Box - Gluttonous Desire"),
        (903054005, "Loot Box - Luxurious Desire"),
        (903054006, "Loot Box - Slothful Desire")
    ]
    result = []
    for item_id, name in special_items:
        count = get_inventory_count(item_id)
        price = get_item_price(item_id)
        result.append({
            "item_id": item_id,
            "item_name": name,
            "available": count,
            "price": price
        })
    return result

def log_admin_action(admin_id, action, target, details):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO admin_logs (admin_id, action, target, details, timestamp) VALUES (?, ?, ?, ?, ?)",
              (admin_id, action, target, details, now_ist().strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()
    conn.close()

# ==================== REDEEM CODES ====================

def generate_redeem_code(length=8):
    import random, string
    chars = string.ascii_uppercase + string.digits
    return ''.join(random.choice(chars) for _ in range(length))

def create_redeem_code(credits, expiry_hours=None, max_uses=1, created_by=None, item_id=None, item_name=None):
    """expiry_hours=None (or 0) means the code never expires.
    If item_id is given, this becomes an item-specific code: redeeming it
    hands over that exact item straight from inventory instead of credits."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    while True:
        code = generate_redeem_code()
        c.execute("SELECT code FROM redeem_codes WHERE code = ?", (code,))
        if not c.fetchone():
            break
    created_at = now_ist().strftime("%Y-%m-%d %H:%M:%S")
    expiry = None
    if expiry_hours:
        expiry = (now_ist() + timedelta(hours=expiry_hours)).strftime("%Y-%m-%d %H:%M:%S")
    c.execute("""INSERT INTO redeem_codes (code, credits, expiry, max_uses, used_count, created_by, created_at, active, item_id, item_name)
                 VALUES (?, ?, ?, ?, 0, ?, ?, 1, ?, ?)""",
              (code, credits, expiry, max_uses, created_by, created_at, item_id, item_name))
    conn.commit()
    conn.close()
    return {"code": code, "credits": credits, "expiry": expiry, "max_uses": max_uses, "item_id": item_id, "item_name": item_name}

def get_redeem_code(code):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT code, credits, expiry, max_uses, used_count, created_by, created_at, active, item_id, item_name FROM redeem_codes WHERE code = ?", (code,))
    result = c.fetchone()
    conn.close()
    if result:
        return {
            "code": result[0], "credits": result[1], "expiry": result[2],
            "max_uses": result[3], "used_count": result[4], "created_by": result[5],
            "created_at": result[6], "active": result[7], "item_id": result[8], "item_name": result[9]
        }
    return None

def has_user_used_code(code, user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id FROM redeem_code_uses WHERE code = ? AND user_id = ?", (code, user_id))
    result = c.fetchone()
    conn.close()
    return result is not None

def redeem_code_for_user(code, user_id):
    """Attempts to redeem a code for a user.
    Returns (success: bool, kind: str, payload).
    kind is one of: "credits", "item", or a failure reason string.
    payload is the credits int for "credits", or the assigned-item dict for "item"."""
    redeem = get_redeem_code(code)
    if not redeem:
        return False, "invalid", None
    if not redeem["active"]:
        return False, "inactive", None
    if redeem["expiry"]:
        expiry_dt = datetime.strptime(redeem["expiry"], "%Y-%m-%d %H:%M:%S")
        if now_ist() > expiry_dt:
            return False, "expired", None
    if redeem["used_count"] >= redeem["max_uses"]:
        return False, "exhausted", None
    if has_user_used_code(code, user_id):
        return False, "already_used", None

    if redeem["item_id"]:
        if get_inventory_count(redeem["item_id"]) <= 0:
            return False, "out_of_stock", None
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("UPDATE redeem_codes SET used_count = used_count + 1 WHERE code = ?", (code,))
        c.execute("INSERT INTO redeem_code_uses (code, user_id, timestamp) VALUES (?, ?, ?)",
                  (code, user_id, now_ist().strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()
        conn.close()
        assigned = assign_inventory_to_user(user_id, redeem["item_id"])
        if not assigned:
            return False, "out_of_stock", None
        return True, "item", assigned

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE redeem_codes SET used_count = used_count + 1 WHERE code = ?", (code,))
    c.execute("INSERT INTO redeem_code_uses (code, user_id, timestamp) VALUES (?, ?, ?)",
              (code, user_id, now_ist().strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()
    conn.close()
    add_credits(user_id, redeem["credits"])
    return True, "credits", redeem["credits"]

def list_recent_codes(limit=10):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT code, credits, expiry, max_uses, used_count, active, item_id, item_name FROM redeem_codes ORDER BY created_at DESC LIMIT ?", (limit,))
    results = c.fetchall()
    conn.close()
    return [{"code": r[0], "credits": r[1], "expiry": r[2], "max_uses": r[3], "used_count": r[4],
              "active": r[5], "item_id": r[6], "item_name": r[7]} for r in results]

def get_code_redemptions(code):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""SELECT r.user_id, r.timestamp, u.username, u.first_name
                 FROM redeem_code_uses r LEFT JOIN users u ON r.user_id = u.user_id
                 WHERE r.code = ? ORDER BY r.timestamp""", (code,))
    results = c.fetchall()
    conn.close()
    return [{"user_id": r[0], "timestamp": r[1], "username": r[2], "first_name": r[3]} for r in results]

def get_user_by_username(username):
    """Case-insensitive lookup, with or without a leading @."""
    username = username.lstrip('@')
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE username = ? COLLATE NOCASE", (username,))
    result = c.fetchone()
    conn.close()
    if result:
        return {
            "user_id": result[0], "username": result[1], "first_name": result[2],
            "credits": result[3], "referrer_id": result[4], "joined_date": result[5],
            "total_referrals": result[6], "banned": result[7] if len(result) > 7 else 0
        }
    return None

# ==================== BOT HANDLERS ====================

def escape_md(text):
    """Escape Telegram's legacy Markdown special chars (_ * ` [) in user-controlled
    text (usernames, first names, free-typed broadcast text) before it goes into a
    ParseMode.MARKDOWN message. Without this, any username/name containing e.g. an
    underscore breaks Telegram's parser, send_message raises, and it was getting
    silently swallowed by bare except blocks — which is why join notifications and
    broadcasts were failing with no visible error."""
    if text is None:
        return ""
    text = str(text)
    for ch in ['_', '*', '`', '[']:
        text = text.replace(ch, '\\' + ch)
    return text

def format_user_ref(user_id, username, first_name):
    """Username with @ if available, otherwise a clickable profile link (no username needed)."""
    if username and username != "NoUsername":
        return f"@{escape_md(username)}"
    return f"[{escape_md(first_name or 'User')}](tg://user?id={user_id})"

def profile_link(user_id, first_name):
    """Always the user's name as a clickable link to their Telegram profile -
    no @username, and no dependency on them having one."""
    return f"[{escape_md(first_name or 'User')}](tg://user?id={user_id})"

def maintenance_gate(func):
    """Wraps a user-facing handler: while the bot is toggled OFF, every user
    except admins/owner gets the maintenance message instead of the handler
    running. Admins always pass through untouched."""
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user = update.effective_user
        if user and user.id not in ADMIN_IDS:
            if is_banned(user.id):
                try:
                    if update.callback_query:
                        await update.callback_query.answer()
                        await update.callback_query.message.reply_text(BANNED_MESSAGE, parse_mode=ParseMode.MARKDOWN, message_effect_id=EFFECT_THUMBS_DOWN)
                    elif update.effective_message:
                        await update.effective_message.reply_text(BANNED_MESSAGE, parse_mode=ParseMode.MARKDOWN, message_effect_id=EFFECT_THUMBS_DOWN)
                except:
                    pass
                return
            if not get_bot_enabled():
                try:
                    if update.callback_query:
                        await update.callback_query.answer()
                        await update.callback_query.message.reply_text(MAINTENANCE_MESSAGE, parse_mode=ParseMode.MARKDOWN, message_effect_id=EFFECT_HEART)
                    elif update.effective_message:
                        await update.effective_message.reply_text(MAINTENANCE_MESSAGE, parse_mode=ParseMode.MARKDOWN, message_effect_id=EFFECT_HEART)
                except:
                    pass
                return
        return await func(update, context, *args, **kwargs)
    return wrapper

async def notify_referrer_completed(context: ContextTypes.DEFAULT_TYPE, referrer_id, user_id, username, first_name):
    ref_user = get_user(referrer_id)
    if not ref_user:
        return
    display = format_user_ref(user_id, username, first_name)
    points = get_setting("refer_points") or "5"
    try:
        await context.bot.send_message(
            referrer_id,
            f"✅ *Referral Complete!*\n\n"
            f"👤 User: {display}\n"
            f"🆔 ID: `{user_id}`\n\n"
            f"Is user ne sabhi channels join kar liye hain.\n"
            f"💰 Aapko {points} points mil gaye hain!",
            parse_mode=ParseMode.MARKDOWN
        , message_effect_id=EFFECT_PARTY)
    except:
        pass

@maintenance_gate
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    username = user.username or "NoUsername"
    first_name = user.first_name or "User"
    
    existing_user = get_user(user_id)
    
    referrer_id = None
    deep_link_redeem = False
    redeem_code_from_link = None
    if context.args:
        if context.args[0].startswith("ref_"):
            try:
                referrer_id = int(context.args[0].split("_")[1])
                if referrer_id == user_id:
                    referrer_id = None
            except:
                pass
        elif context.args[0] == "redeem":
            deep_link_redeem = True
        elif context.args[0].startswith("redeem_"):
            # Carries the specific code created via Admin Panel -> Create Code,
            # so it can be auto-redeemed the instant the user lands here.
            deep_link_redeem = True
            redeem_code_from_link = context.args[0][len("redeem_"):]
    
    if not existing_user:
        member = True
        for channel in CHANNELS:
            try:
                chat_member = await context.bot.get_chat_member(channel, user_id)
                if chat_member.status in ["left", "kicked"]:
                    member = False
                    break
            except:
                member = False
                break
        
        if not member:
            # Referrer isn't credited yet (the referral only counts once this user
            # actually joins all channels), but stash it so check_joined() can pick
            # it up later - previously this was lost here and the referral silently
            # never counted for anyone who wasn't already in every channel.
            if referrer_id:
                context.user_data['pending_referrer'] = referrer_id
                ref_user = get_user(referrer_id)
                if ref_user:
                    display = format_user_ref(user_id, username, first_name)
                    try:
                        await context.bot.send_message(
                            referrer_id,
                            f"🔔 *Naya user aapke referral link se aaya hai!*\n\n"
                            f"👤 User: {display}\n"
                            f"🆔 ID: `{user_id}`\n\n"
                            f"⚠️ Is user ne abhi tak sabhi channels join nahi kiye hain, isliye referral credit *nahi* hua hai.\n"
                            f"Jaise hi ye sabhi channels join karega, aapko points mil jayenge.",
                            parse_mode=ParseMode.MARKDOWN
                        , message_effect_id=EFFECT_THUMBS_DOWN)
                    except:
                        pass
            
            # Remember that this /start came from a "Redeem Now" deep-link button
            # so check_joined() can drop the user straight into the redeem prompt
            # (or auto-redeem the carried code) once they finish joining, instead
            # of the main menu.
            if deep_link_redeem:
                context.user_data['pending_redeem'] = True
                if redeem_code_from_link:
                    context.user_data['pending_redeem_code'] = redeem_code_from_link

            # ONLY CHANNEL JOIN = INLINE BUTTONS WITH URL
            keyboard = []
            for channel in CHANNELS:
                channel_name = channel.replace('@', '')
                keyboard.append([InlineKeyboardButton(f"📢 Join {channel}", url=f"https://t.me/{channel_name}")])
            keyboard.append([InlineKeyboardButton("✅ I have joined", callback_data="check_joined")])
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await update.message.reply_text(
                "⚠️ *Please join all required channels first!*\n\n"
                "You must join these channels to use this bot:\n"
                + "\n".join([f"• {escape_md(ch)}" for ch in CHANNELS]),
                reply_markup=reply_markup,
                parse_mode=ParseMode.MARKDOWN
            , message_effect_id=EFFECT_THUMBS_DOWN)
            return
        
        user_data = create_user(user_id, username, first_name, referrer_id)
        
        if referrer_id:
            await notify_referrer_completed(context, referrer_id, user_id, username, first_name)
        
        for admin_id in ADMIN_IDS:
            try:
                ref_text = f"from @{escape_md(get_user(referrer_id)['username'])}" if referrer_id else "No referrer"
                msg = (
                    f"📥 *New User Joined!*\n"
                    f"🕐 Time: {now_ist().strftime('%Y-%m-%d %H:%M:%S')}\n"
                    f"👤 User: {escape_md(first_name)} (@{escape_md(username)}) [ID: {user_id}]\n"
                    f"📌 Type: {ref_text}\n"
                )
                await context.bot.send_message(admin_id, msg, parse_mode=ParseMode.MARKDOWN, message_effect_id=EFFECT_HEART)
            except:
                pass
    
    if deep_link_redeem:
        await handle_redeem_deep_link(update.message, context, user_id, redeem_code_from_link)
        return
    
    await show_main_menu(update, context)

@maintenance_gate
async def check_joined(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    
    all_joined = True
    for channel in CHANNELS:
        try:
            chat_member = await context.bot.get_chat_member(channel, user_id)
            if chat_member.status in ["left", "kicked"]:
                all_joined = False
                break
        except Exception as e:
            # If this fires for every user, the bot is most likely NOT an admin
            # in that channel yet - get_chat_member needs admin rights to work.
            print(f"⚠️ get_chat_member failed for {channel}: {e}")
            all_joined = False
            break
    
    if all_joined:
        await query.answer("✅ Verified!")
        existing_user = get_user(user_id)
        if not existing_user:
            username = query.from_user.username or "NoUsername"
            first_name = query.from_user.first_name or "User"
            # Pick up the referrer that was stashed in start() when this user
            # first clicked the link but hadn't joined all channels yet.
            referrer_id = context.user_data.pop('pending_referrer', None)
            create_user(user_id, username, first_name, referrer_id)
            if referrer_id:
                await notify_referrer_completed(context, referrer_id, user_id, username, first_name)
            
            # This was missing entirely - almost every real user finishes signup
            # here (not in start()'s immediate-member branch), so admins were
            # never getting notified about new joins at all.
            for admin_id in ADMIN_IDS:
                try:
                    ref_text = f"from @{escape_md(get_user(referrer_id)['username'])}" if referrer_id else "No referrer"
                    msg = (
                        f"📥 *New User Joined!*\n"
                        f"🕐 Time: {now_ist().strftime('%Y-%m-%d %H:%M:%S')}\n"
                        f"👤 User: {escape_md(first_name)} (@{escape_md(username)}) [ID: {user_id}]\n"
                        f"📌 Type: {ref_text}\n"
                    )
                    await context.bot.send_message(admin_id, msg, parse_mode=ParseMode.MARKDOWN, message_effect_id=EFFECT_HEART)
                except:
                    pass
        try:
            await query.edit_message_text("✅ *All channels joined!*\n\nWelcome to the bot!", parse_mode=ParseMode.MARKDOWN, message_effect_id=EFFECT_PARTY)
        except Exception:
            pass
        
        user = get_user(user_id)
        keyboard = [
            [KeyboardButton("🛒 Buy Items"), KeyboardButton("💰 My Credits")],
            [KeyboardButton("🎁 Daily Bonus"), KeyboardButton("📤 Referral")],
            [KeyboardButton("🏆 Leaderboard"), KeyboardButton("🎟 Redeem Code")],
            [KeyboardButton("❓ Help")]
        ]
        if user_id in ADMIN_IDS:
            keyboard.append([KeyboardButton("⚙️ Admin Panel")])
        reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        await query.message.reply_text(
            f"🏠 *Welcome back!*\n\n"
            f"👤 User: {user['first_name']}\n"
            f"💰 Credits: {user['credits']}\n"
            f"🏆 Referrals: {user['total_referrals']}\n\n"
            f"Select an option below:",
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN
        , message_effect_id=EFFECT_FIRE)
        
        # If this whole flow started from a "Redeem Now" deep-link button,
        # drop the user straight into the redeem prompt (or auto-redeem the
        # carried code) instead of stopping at the main menu.
        if context.user_data.pop('pending_redeem', False):
            code = context.user_data.pop('pending_redeem_code', None)
            await handle_redeem_deep_link(query.message, context, user_id, code)
    else:
        # Give visible feedback via a popup alert instead of silently trying
        # to re-edit an identical message (which Telegram rejects and used
        # to make this button look "dead").
        await query.answer(
            "⚠️ Aapne abhi tak sabhi channels join nahi kiye hain! Pehle sabhi channels join karein, phir dobara tap karein.",
            show_alert=True
        )
        keyboard = []
        for channel in CHANNELS:
            channel_name = channel.replace('@', '')
            keyboard.append([InlineKeyboardButton(f"📢 Join {channel}", url=f"https://t.me/{channel_name}")])
        keyboard.append([InlineKeyboardButton("✅ I have joined", callback_data="check_joined")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        try:
            await query.edit_message_text(
                "⚠️ *Please join all required channels first!*\n\n"
                "You must join these channels to use this bot:\n"
                + "\n".join([f"• {escape_md(ch)}" for ch in CHANNELS]),
                reply_markup=reply_markup,
                parse_mode=ParseMode.MARKDOWN
            , message_effect_id=EFFECT_THUMBS_DOWN)
        except Exception:
            pass

async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = get_user(user_id)
    if not user:
        await start(update, context)
        return
    
    keyboard = [
        [KeyboardButton("🛒 Buy Items"), KeyboardButton("💰 My Credits")],
        [KeyboardButton("🎁 Daily Bonus"), KeyboardButton("📤 Referral")],
        [KeyboardButton("🏆 Leaderboard"), KeyboardButton("🎟 Redeem Code")],
        [KeyboardButton("❓ Help")]
    ]
    if user_id in ADMIN_IDS:
        keyboard.append([KeyboardButton("⚙️ Admin Panel")])
    
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    await update.message.reply_text(
        f"🏠 *Welcome back!*\n\n"
        f"👤 User: {user['first_name']}\n"
        f"💰 Credits: {user['credits']}\n"
        f"🏆 Referrals: {user['total_referrals']}\n\n"
        f"Select an option below:",
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN
    , message_effect_id=EFFECT_FIRE)

@maintenance_gate
async def handle_menu_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    user_id = update.effective_user.id
    
    if text == "🛒 Buy Items":
        await show_buy_items(update, context)
    elif text == "💰 My Credits":
        await show_my_credits(update, context)
    elif text == "🎁 Daily Bonus":
        await claim_daily_bonus(update, context)
    elif text == "📤 Referral":
        await show_referral(update, context)
    elif text == "🏆 Leaderboard":
        await show_leaderboard(update, context)
    elif text == "🎟 Redeem Code":
        await start_redeem_code(update, context)
    elif text == "❓ Help":
        await show_help(update, context)
    elif text == "⚙️ Admin Panel" and user_id in ADMIN_IDS:
        await show_admin_panel(update, context)
    elif text == "🔙 Back" or text == "❌ Cancel":
        context.user_data['user_action'] = None
        context.user_data['admin_action'] = None
        await show_main_menu(update, context)
    else:
        await show_main_menu(update, context)

async def show_buy_items(update: Update, context: ContextTypes.DEFAULT_TYPE):
    items = get_all_special_items()
    user = get_user(update.effective_user.id)
    
    if not items:
        await update.message.reply_text("⚠️ *No items available for purchase!*", parse_mode=ParseMode.MARKDOWN)
        await show_main_menu(update, context)
        return
    
    keyboard = []
    for item in items:
        if item["available"] > 0:
            keyboard.append([KeyboardButton(f"{item['item_name']} - {item['price']}💰 ({item['available']} left)||{item['item_id']}")])
    keyboard.append([KeyboardButton("🔙 Back")])
    
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    await update.message.reply_text(
        f"🛒 *Available Items*\n\n"
        f"💰 Your Credits: {user['credits']}\n\n"
        f"Select an item to buy:",
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN
    , message_effect_id=EFFECT_FIRE)

@maintenance_gate
async def handle_buy_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    user_id = update.effective_user.id
    
    if text == "🔙 Back":
        await show_main_menu(update, context)
        return
    
    try:
        item_name, item_id_str = text.split("||")
        item_id = int(item_id_str)
        
        items = get_all_special_items()
        selected_item = None
        for item in items:
            if item["item_id"] == item_id:
                selected_item = item
                break
        
        if not selected_item or selected_item["available"] <= 0:
            await update.message.reply_text("⚠️ *This item is out of stock!*", parse_mode=ParseMode.MARKDOWN)
            await show_buy_items(update, context)
            return
        
        user = get_user(user_id)
        if user["credits"] < selected_item["price"]:
            keyboard = [[KeyboardButton("🔙 Back")]]
            reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
            await update.message.reply_text(
                f"⚠️ *Insufficient credits!*\n\n"
                f"Item Price: {selected_item['price']}💰\n"
                f"Your Credits: {user['credits']}💰\n\n"
                f"Earn more credits through referrals!",
                reply_markup=reply_markup,
                parse_mode=ParseMode.MARKDOWN
            , message_effect_id=EFFECT_THUMBS_DOWN)
            return
        
        keyboard = [
            [KeyboardButton(f"✅ Confirm||{item_id}")],
            [KeyboardButton("❌ Cancel")]
        ]
        reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        await update.message.reply_text(
            f"🛒 *Confirm Purchase*\n\n"
            f"Item: {selected_item['item_name']}\n"
            f"Price: {selected_item['price']}💰\n"
            f"Available: {selected_item['available']}\n\n"
            f"Your Credits: {user['credits']}💰\n"
            f"After Purchase: {user['credits'] - selected_item['price']}💰\n\n"
            f"*Are you sure?*",
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN
        , message_effect_id=EFFECT_FIRE)
    except:
        await update.message.reply_text("⚠️ *Invalid selection!*", parse_mode=ParseMode.MARKDOWN)
        await show_buy_items(update, context)

@maintenance_gate
async def handle_confirm_purchase(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    user_id = update.effective_user.id
    
    if text == "❌ Cancel":
        await show_buy_items(update, context)
        return
    
    try:
        _, item_id_str = text.split("||")
        item_id = int(item_id_str)
        
        available = get_inventory_count(item_id)
        if available <= 0:
            await update.message.reply_text("⚠️ *Item out of stock!*", parse_mode=ParseMode.MARKDOWN)
            await show_buy_items(update, context)
            return
        
        user = get_user(user_id)
        price = get_item_price(item_id)
        if user["credits"] < price:
            await update.message.reply_text("⚠️ *Insufficient credits!*", parse_mode=ParseMode.MARKDOWN, message_effect_id=EFFECT_THUMBS_DOWN)
            await show_buy_items(update, context)
            return
        
        if deduct_credits(user_id, price):
            assigned_item = assign_inventory_to_user(user_id, item_id)
            if assigned_item:
                item_data = {
                    "timestamp": assigned_item["timestamp"],
                    "guestUid": assigned_item["guest_uid"],
                    "guestPass": assigned_item["guest_pass"],
                    "item_id": assigned_item["item_id"],
                    "item_name": assigned_item["item_name"]
                }
                item_json = json.dumps(item_data, indent=4)
                
                keyboard = [
                    [KeyboardButton("🛒 Buy Items")],
                    [KeyboardButton("🔙 Back")]
                ]
                reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
                await update.message.reply_text(
                    f"✅ *Purchase Successful!*\n\n"
                    f"Item: {assigned_item['item_name']}\n"
                    f"Price: {price}💰\n"
                    f"Remaining Credits: {user['credits'] - price}💰\n\n"
                    f"*Your Item Details:*\n"
                    f"```json\n{item_json}\n```",
                    reply_markup=reply_markup,
                    parse_mode=ParseMode.MARKDOWN
                , message_effect_id=EFFECT_PARTY)
                
                for admin_id in ADMIN_IDS:
                    try:
                        display = profile_link(user_id, user['first_name'])
                        await context.bot.send_message(
                            admin_id,
                            f"🛒 *Purchase Made!*\n"
                            f"User: {display} [ID: {user_id}]\n"
                            f"Item: {escape_md(assigned_item['item_name'])}\n"
                            f"Price: {price}💰\n"
                            f"Remaining Stock: {get_inventory_count(item_id)}",
                            parse_mode=ParseMode.MARKDOWN
                        , message_effect_id=EFFECT_FIRE)
                    except:
                        pass
                await notify_low_stock_if_needed(context, item_id, assigned_item['item_name'])
            else:
                await update.message.reply_text("⚠️ *Error assigning item! Please contact admin.*", parse_mode=ParseMode.MARKDOWN)
        else:
            await update.message.reply_text("⚠️ *Error processing purchase!*", parse_mode=ParseMode.MARKDOWN)
        
        await show_main_menu(update, context)
    except Exception as e:
        await update.message.reply_text(f"⚠️ *Error: {str(e)}*", parse_mode=ParseMode.MARKDOWN)
        await show_buy_items(update, context)

async def show_my_credits(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = get_user(user_id)
    
    if not user:
        await start(update, context)
        return
    
    purchases = get_user_purchases(user_id)
    purchase_text = ""
    if purchases:
        purchase_text = "\n\n*Recent Purchases:*\n"
        for p in purchases[:5]:
            purchase_text += f"• {p['item_name']} ({p['purchase_date']})\n"
    
    keyboard = [[KeyboardButton("🔙 Back")]]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    await update.message.reply_text(
        f"💰 *My Credits*\n\n"
        f"Total Credits: {user['credits']}💰\n"
        f"Total Referrals: {user['total_referrals']}\n"
        f"Total Purchases: {len(purchases)}\n"
        f"{purchase_text}\n\n"
        f"🔹 *Earn More:* Share your referral link!\n"
        f"🔹 *Spend:* Buy special items!",
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN
    , message_effect_id=EFFECT_HEART)

async def claim_daily_bonus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    claimed, result = check_daily_bonus(user_id)
    keyboard = [[KeyboardButton("🔙 Back")]]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    if claimed:
        await update.message.reply_text(
            f"🎉 *Daily Bonus Claimed!*\n\n"
            f"You received: {result}💰\n"
            f"Come back tomorrow for more!",
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN
        , message_effect_id=EFFECT_PARTY)
    else:
        await update.message.reply_text(
            f"⏰ *Already Claimed Today!*\n\n"
            f"Next claim available at: {result}\n"
            f"Come back after this time!",
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN
        , message_effect_id=EFFECT_HEART)

async def show_referral(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = get_user(user_id)
    bot_username = context.bot.username
    
    referral_link = f"https://t.me/{bot_username}?start=ref_{user_id}"
    points_per_refer = get_setting("refer_points") or "5"
    
    keyboard = [[KeyboardButton("🔙 Back")]]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    await update.message.reply_text(
        f"📤 *Referral System*\n\n"
        f"💰 Points per Referral: {points_per_refer}\n"
        f"👥 Total Referrals: {user['total_referrals']}\n\n"
        f"*Your Referral Link:*\n"
        f"`{referral_link}`\n\n"
        f"🔹 Share this link with friends!\n"
        f"🔹 They must join all channels!\n"
        f"🔹 You earn points when they join!",
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN
    , message_effect_id=EFFECT_HEART)

async def send_redeem_prompt(message, context: ContextTypes.DEFAULT_TYPE):
    """Shared by the 'Redeem Code' menu button and the 'Redeem Now' deep-link
    button, so both land the user on the same prompt."""
    context.user_data['user_action'] = 'redeem_code'
    keyboard = [[KeyboardButton("🔙 Back")]]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    await message.reply_text(
        f"🎟 *Redeem Code*\n\n"
        f"Apna redeem code neeche bhejein.\n"
        f"Example: `AB12CD34`\n\n"
        f"Type 'cancel' to cancel.",
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN
    , message_effect_id=EFFECT_HEART)

async def start_redeem_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_redeem_prompt(update.message, context)

async def handle_redeem_deep_link(message, context: ContextTypes.DEFAULT_TYPE, user_id, code):
    """Reached via a 'Redeem Now' deep-link. When admin creates a code, the
    button's link now carries that exact code (?start=redeem_<CODE>), so
    pressing it redeems it immediately - no typing needed. The old generic
    link (?start=redeem, no code attached) still falls back to the manual
    type-the-code prompt."""
    if not code:
        await send_redeem_prompt(message, context)
        return

    code = code.strip().upper()
    success, kind, payload = redeem_code_for_user(code, user_id)

    if success and kind == "credits":
        user = get_user(user_id)
        await message.reply_text(
            f"✅ *Code Redeemed!*\n\n"
            f"🎟 Code: `{code}`\n"
            f"💰 Credits Added: {payload}\n"
            f"💰 New Balance: {user['credits']}💰",
            parse_mode=ParseMode.MARKDOWN
        , message_effect_id=EFFECT_PARTY)
    elif success and kind == "item":
        item_json = json.dumps({
            "timestamp": payload["timestamp"], "guestUid": payload["guest_uid"],
            "guestPass": payload["guest_pass"], "item_id": payload["item_id"], "item_name": payload["item_name"]
        }, indent=4)
        await message.reply_text(
            f"✅ *Code Redeemed!*\n\n"
            f"🎁 Item: {payload['item_name']}\n\n"
            f"*Your Item Details:*\n"
            f"```json\n{item_json}\n```",
            parse_mode=ParseMode.MARKDOWN
        , message_effect_id=EFFECT_PARTY)
        await notify_low_stock_if_needed(context, payload["item_id"], payload["item_name"])
    else:
        reason_text = {
            "invalid": "⚠️ *Ye code exist nahi karta!*",
            "inactive": "⚠️ *Ye code ab active nahi hai!*",
            "expired": "⚠️ *Ye code expire ho chuka hai!*",
            "exhausted": "⚠️ *Is code ki redemption limit khatam ho chuki hai!*",
            "already_used": "⚠️ *Aap ye code pehle hi redeem kar chuke hain!*",
            "out_of_stock": "⚠️ *Is item ka stock khatam ho chuka hai!*",
        }.get(kind, "⚠️ *Code redeem nahi ho paya!*")
        await message.reply_text(reason_text, parse_mode=ParseMode.MARKDOWN, message_effect_id=EFFECT_THUMBS_DOWN)

async def show_leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    top_referrers = get_top_referrers(10)
    is_admin = update.effective_user.id in ADMIN_IDS
    
    keyboard = [[KeyboardButton("🔙 Back")]]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    if not top_referrers:
        await update.message.reply_text("📊 *Leaderboard*\n\nNo referrals yet! Be the first!", reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN, message_effect_id=EFFECT_HEART)
        return
    
    text = "🏆 *Top Referrers*\n\n"
    emojis = ["🥇", "🥈", "🥉"]
    for i, ref in enumerate(top_referrers):
        emoji = emojis[i] if i < 3 else f"{i+1}."
        name = escape_md(ref['first_name'] or ref['username'] or f"User_{ref['user_id']}")
        id_suffix = f" `[{ref['user_id']}]`" if is_admin else ""
        text += f"{emoji} {name} - {ref['total_referrals']} referrals{id_suffix}\n"
    
    await update.message.reply_text(text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN, message_effect_id=EFFECT_HEART)

async def show_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[KeyboardButton("🔙 Back")]]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    await update.message.reply_text(
        f"📖 *Help & Commands*\n\n"
        f"🛒 *Buy Items* - Purchase special items\n"
        f"💰 *My Credits* - Check your balance\n"
        f"🎁 *Daily Bonus* - Claim free credits\n"
        f"📤 *Referral* - Share and earn points\n"
        f"🏆 *Leaderboard* - Top referrers\n\n"
        f"📢 *Required Channels:*\n" + "\n".join([f"• {escape_md(ch)}" for ch in CHANNELS]) + "\n\n"
        f"💡 *Tip:* More referrals = More credits = More items!",
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN
    , message_effect_id=EFFECT_HEART)

# ==================== ADMIN PANEL ====================

async def show_admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("⚠️ *Unauthorized!*", parse_mode=ParseMode.MARKDOWN)
        return
    
    keyboard = [
        [KeyboardButton("💰 Edit Prices"), KeyboardButton("🔄 Edit Points")],
        [KeyboardButton("🎁 Edit Bonus"), KeyboardButton("🎁 Gift Credits")],
        [KeyboardButton("🔻 Deduct Credits"), KeyboardButton("👤 User Info")],
        [KeyboardButton("📦 Upload Inventory"), KeyboardButton("🗄 Users Database")],
        [KeyboardButton("📢 Broadcast"), KeyboardButton("📨 Message User")],
        [KeyboardButton("🚫 Ban User"), KeyboardButton("✅ Unban User")],
        [KeyboardButton("🎟 Create Code"), KeyboardButton("🎁 Item Code")],
        [KeyboardButton("📋 List Codes"), KeyboardButton("👥 Code Users")],
        [KeyboardButton("🔌 Toggle Bot Status"), KeyboardButton("📊 Stats")],
        [KeyboardButton("🗄 Database Channel"), KeyboardButton("📥 Restore Database")],
        [KeyboardButton("🔙 Back")]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    await update.message.reply_text(
        f"🔧 *Admin Panel*\n\n"
        f"Select an action:",
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN
    , message_effect_id=EFFECT_HEART)

async def handle_admin_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    user_id = update.effective_user.id
    
    if user_id not in ADMIN_IDS:
        return
    
    if text == "🔙 Back":
        await show_main_menu(update, context)
    elif text == "💰 Edit Prices":
        await show_edit_prices(update, context)
    elif text == "🔄 Edit Points":
        context.user_data['admin_action'] = 'edit_points'
        await update.message.reply_text(
            f"🔄 *Edit Referral Points*\n\n"
            f"Current Points: {get_setting('refer_points') or '5'}\n\n"
            f"Send the new points value (number only).\n"
            f"Example: `10`\n\n"
            f"Type 'cancel' to cancel.",
            parse_mode=ParseMode.MARKDOWN
        , message_effect_id=EFFECT_HEART)
    elif text == "🎁 Edit Bonus":
        context.user_data['admin_action'] = 'edit_bonus'
        await update.message.reply_text(
            f"🎁 *Edit Daily Bonus*\n\n"
            f"Current Bonus: {get_setting('daily_bonus') or '10'}\n\n"
            f"Send the new bonus value (number only).\n"
            f"Example: `15`\n\n"
            f"Type 'cancel' to cancel.",
            parse_mode=ParseMode.MARKDOWN
        , message_effect_id=EFFECT_HEART)
    elif text == "🎁 Gift Credits":
        context.user_data['admin_action'] = 'gift'
        await update.message.reply_text(
            f"🎁 *Gift Credits*\n\n"
            f"Format: `user_id amount`\n"
            f"Example: `123456789 50`\n\n"
            f"Type 'cancel' to cancel.",
            parse_mode=ParseMode.MARKDOWN
        , message_effect_id=EFFECT_HEART)
    elif text == "🔻 Deduct Credits":
        context.user_data['admin_action'] = 'deduct'
        await update.message.reply_text(
            f"🔻 *Deduct Credits*\n\n"
            f"Format: `user_id amount`\n"
            f"Example: `123456789 10`\n\n"
            f"Type 'cancel' to cancel.",
            parse_mode=ParseMode.MARKDOWN
        , message_effect_id=EFFECT_HEART)
    elif text == "👤 User Info":
        context.user_data['admin_action'] = 'user_info'
        await update.message.reply_text(
            f"👤 *Get User Info*\n\n"
            f"Send the user ID *or* @username to fetch details.\n"
            f"Example: `123456789` or `@someuser`\n\n"
            f"Type 'cancel' to cancel.",
            parse_mode=ParseMode.MARKDOWN
        , message_effect_id=EFFECT_HEART)
    elif text == "📦 Upload Inventory":
        await show_upload_inventory_menu(update, context)
    elif text == "🗄 Users Database":
        await show_users_db_menu(update, context)
    elif text == "📢 Broadcast":
        context.user_data['admin_action'] = 'broadcast'
        await update.message.reply_text(
            f"📢 *Broadcast Message*\n\n"
            f"Send the message you want to broadcast to all users.\n"
            f"Type 'cancel' to cancel.",
            parse_mode=ParseMode.MARKDOWN
        , message_effect_id=EFFECT_HEART)
    elif text == "📨 Message User":
        context.user_data['admin_action'] = 'message_user_target'
        await update.message.reply_text(
            f"📨 *Message a Single User*\n\n"
            f"Send the user ID you want to message.\n"
            f"Example: `123456789`\n\n"
            f"Type 'cancel' to cancel.",
            parse_mode=ParseMode.MARKDOWN
        , message_effect_id=EFFECT_HEART)
    elif text == "🚫 Ban User":
        context.user_data['admin_action'] = 'ban_user'
        await update.message.reply_text(
            f"🚫 *Ban User*\n\n"
            f"Send the user ID to ban.\n"
            f"Example: `123456789`\n\n"
            f"Type 'cancel' to cancel.",
            parse_mode=ParseMode.MARKDOWN
        , message_effect_id=EFFECT_THUMBS_DOWN)
    elif text == "✅ Unban User":
        context.user_data['admin_action'] = 'unban_user'
        await update.message.reply_text(
            f"✅ *Unban User*\n\n"
            f"Send the user ID to unban.\n"
            f"Example: `123456789`\n\n"
            f"Type 'cancel' to cancel.",
            parse_mode=ParseMode.MARKDOWN
        , message_effect_id=EFFECT_HEART)
    elif text == "📊 Stats":
        await show_stats(update, context)
    elif text == "🎟 Create Code":
        context.user_data['admin_action'] = 'create_code'
        await update.message.reply_text(
            f"🎟 *Create Redeem Code*\n\n"
            f"Format: `credits expiry_hours [max_uses]`\n\n"
            f"🔹 `expiry_hours` = 0 ka matlab code kabhi expire nahi hoga\n"
            f"🔹 `max_uses` optional hai, default 1 (single use)\n\n"
            f"Examples:\n"
            f"`50 24` → 50 credits, 24 ghante mein expire, 1 baar use\n"
            f"`100 0 20` → 100 credits, kabhi expire nahi, 20 logo tak use\n\n"
            f"Type 'cancel' to cancel.",
            parse_mode=ParseMode.MARKDOWN
        , message_effect_id=EFFECT_HEART)
    elif text == "📋 List Codes":
        await show_code_list(update, context)
    elif text == "👥 Code Users":
        context.user_data['admin_action'] = 'view_code_users'
        await update.message.reply_text(
            f"👥 *Code Redemptions*\n\n"
            f"Send the redeem code to see who used it.\n"
            f"Example: `AB12CD34`\n\n"
            f"Type 'cancel' to cancel.",
            parse_mode=ParseMode.MARKDOWN
        , message_effect_id=EFFECT_HEART)
    elif text == "🎁 Item Code":
        await show_item_code_selection(update, context)
    elif text == "🗄 Database Channel":
        context.user_data['admin_action'] = 'set_db_channel'
        current = get_db_channel()
        await update.message.reply_text(
            f"🗄 *Database Backup Channel*\n\n"
            f"Current: `{current or 'Not set'}`\n\n"
            f"Bot ko us channel mein admin banao, phir yahan uska @username ya numeric ID bhejo.\n"
            f"Har 30 min mein database wahan auto-backup hoga.\n\n"
            f"Type 'cancel' to cancel.",
            parse_mode=ParseMode.MARKDOWN
        , message_effect_id=EFFECT_HEART)
    elif text == "📥 Restore Database":
        context.user_data['admin_action'] = 'restore_db_upload'
        await update.message.reply_text(
            f"📥 *Restore Database*\n\n"
            f"`.db` backup file bhejo — current database usse turant replace ho jayega.\n"
            f"⚠️ Ye action turant apply hoga, undo nahi ho sakta.\n\n"
            f"Type 'cancel' to cancel.",
            parse_mode=ParseMode.MARKDOWN
        , message_effect_id=EFFECT_THUMBS_DOWN)
    elif text == "🔌 Toggle Bot Status":
        await toggle_bot_status(update, context)

async def toggle_bot_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin_id = update.effective_user.id
    new_state = not get_bot_enabled()
    set_bot_enabled(new_state)
    log_admin_action(admin_id, "toggle_bot", "all_users", f"Bot enabled: {new_state}")
    
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT user_id FROM users")
    all_users = c.fetchall()
    conn.close()
    
    notify_text = LIVE_MESSAGE if new_state else MAINTENANCE_MESSAGE
    sent = 0
    for u in all_users:
        uid = u[0]
        if uid in ADMIN_IDS:
            continue
        try:
            await context.bot.send_message(uid, notify_text, parse_mode=ParseMode.MARKDOWN, message_effect_id=(EFFECT_PARTY if notify_text == LIVE_MESSAGE else EFFECT_THUMBS_DOWN))
            sent += 1
        except:
            pass
        await asyncio.sleep(0.05)
    
    status_text = "🟢 ON (live for everyone)" if new_state else "🔴 OFF (only you/admins can use it)"
    await update.message.reply_text(
        f"✅ *Bot status changed!*\n\n"
        f"Current status: {status_text}\n"
        f"📨 Notified: {sent} users",
        parse_mode=ParseMode.MARKDOWN
    , message_effect_id=EFFECT_PARTY)
    await show_admin_panel(update, context)

async def show_edit_prices(update: Update, context: ContextTypes.DEFAULT_TYPE):
    items = get_all_special_items()
    keyboard = []
    for item in items:
        keyboard.append([KeyboardButton(f"{item['item_name']} - {item['price']}💰||price_{item['item_id']}")])
    keyboard.append([KeyboardButton("🔙 Back")])
    
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    await update.message.reply_text(
        f"💰 *Edit Item Prices*\n\n"
        f"Select an item to change its price.\n"
        f"Then send the new price (number only).",
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN
    , message_effect_id=EFFECT_HEART)

async def handle_price_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    user_id = update.effective_user.id
    
    if user_id not in ADMIN_IDS:
        return
    
    if text == "🔙 Back":
        await show_admin_panel(update, context)
        return
    
    try:
        key = text.split("||")[1]
        context.user_data['admin_action'] = key
        await update.message.reply_text(
            f"💰 *Enter new price*\n\n"
            f"Send the new price as a number only.\n"
            f"Example: `50`\n\n"
            f"Type 'cancel' to cancel.",
            parse_mode=ParseMode.MARKDOWN
        , message_effect_id=EFFECT_HEART)
    except:
        await update.message.reply_text("⚠️ *Invalid selection!*", parse_mode=ParseMode.MARKDOWN, message_effect_id=EFFECT_THUMBS_DOWN)

async def show_code_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    codes = list_recent_codes(15)
    keyboard = [[KeyboardButton("🔙 Back")]]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    if not codes:
        await update.message.reply_text("📋 *No redeem codes created yet.*", reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN, message_effect_id=EFFECT_HEART)
        return
    
    text = "📋 *Recent Redeem Codes*\n\n"
    for cd in codes:
        status = "🟢 Active" if cd["active"] and cd["used_count"] < cd["max_uses"] else "🔴 Exhausted"
        expiry_text = cd["expiry"] if cd["expiry"] else "Never"
        reward = f"🎁 {escape_md(cd['item_name'])}" if cd.get("item_id") else f"{cd['credits']}💰"
        text += (
            f"`{cd['code']}` — {reward}\n"
            f"  Uses: {cd['used_count']}/{cd['max_uses']} | Expiry: {expiry_text} | {status}\n\n"
        )
    
    await update.message.reply_text(text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN, message_effect_id=EFFECT_HEART)

async def show_item_code_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    items = get_all_special_items()
    keyboard = []
    for item in items:
        keyboard.append([KeyboardButton(f"{item['item_name']} ({item['available']} left)||itemcode_{item['item_id']}")])
    keyboard.append([KeyboardButton("🔙 Back")])
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    await update.message.reply_text(
        f"🎁 *Create Item-Specific Redeem Code*\n\n"
        f"Select which item this code should give when redeemed:",
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN
    , message_effect_id=EFFECT_HEART)

async def handle_item_code_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        return
    if text == "🔙 Back":
        await show_admin_panel(update, context)
        return
    try:
        item_name_part, item_id_str = text.split("||itemcode_")
        item_id = int(item_id_str)
        items = get_all_special_items()
        selected = next((i for i in items if i["item_id"] == item_id), None)
        if not selected:
            await update.message.reply_text("⚠️ *Invalid item!*", parse_mode=ParseMode.MARKDOWN, message_effect_id=EFFECT_THUMBS_DOWN)
            return
        context.user_data['pending_item_code_id'] = item_id
        context.user_data['pending_item_code_name'] = selected['item_name']
        context.user_data['admin_action'] = 'create_item_code_details'
        await update.message.reply_text(
            f"🎁 *Item: {selected['item_name']}*\n\n"
            f"Format: `expiry_hours [max_uses]`\n"
            f"🔹 `expiry_hours` = 0 ka matlab kabhi expire nahi hoga\n"
            f"🔹 `max_uses` optional hai, default 1\n\n"
            f"Example: `24` or `0 10`\n\n"
            f"Type 'cancel' to cancel.",
            parse_mode=ParseMode.MARKDOWN
        , message_effect_id=EFFECT_HEART)
    except Exception:
        await update.message.reply_text("⚠️ *Invalid selection!*", parse_mode=ParseMode.MARKDOWN, message_effect_id=EFFECT_THUMBS_DOWN)

async def show_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    c.execute("SELECT COUNT(*) FROM users")
    total_users = c.fetchone()[0]
    
    c.execute("SELECT SUM(credits) FROM users")
    total_credits = c.fetchone()[0] or 0
    
    c.execute("SELECT COUNT(*) FROM referrals")
    total_referrals = c.fetchone()[0]
    
    c.execute("SELECT COUNT(*) FROM inventory WHERE sold = 0")
    total_inventory = c.fetchone()[0]
    
    c.execute("SELECT COUNT(*) FROM purchases")
    total_purchases = c.fetchone()[0]
    
    conn.close()
    
    keyboard = [[KeyboardButton("🔙 Back")]]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    await update.message.reply_text(
        f"📊 *Bot Statistics*\n\n"
        f"👥 Total Users: {total_users}\n"
        f"💰 Total Credits: {total_credits}\n"
        f"🔄 Total Referrals: {total_referrals}\n"
        f"📦 Inventory Available: {total_inventory}\n"
        f"🛒 Total Purchases: {total_purchases}",
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN
    , message_effect_id=EFFECT_FIRE)

@maintenance_gate
async def handle_admin_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    user_id = update.effective_user.id
    
    # Redeem-code flow is available to every user, not just admins, so it's
    # handled first regardless of who's typing.
    if context.user_data.get('user_action') == 'redeem_code':
        if text.lower() == 'cancel':
            context.user_data['user_action'] = None
            await show_main_menu(update, context)
            return
        
        code = text.strip().upper()
        success, kind, payload = redeem_code_for_user(code, user_id)
        context.user_data['user_action'] = None
        
        if success and kind == "credits":
            user = get_user(user_id)
            await update.message.reply_text(
                f"✅ *Code Redeemed!*\n\n"
                f"💰 Credits Added: {payload}\n"
                f"💰 New Balance: {user['credits']}💰",
                parse_mode=ParseMode.MARKDOWN
            , message_effect_id=EFFECT_PARTY)
        elif success and kind == "item":
            item_json = json.dumps({
                "timestamp": payload["timestamp"], "guestUid": payload["guest_uid"],
                "guestPass": payload["guest_pass"], "item_id": payload["item_id"], "item_name": payload["item_name"]
            }, indent=4)
            await update.message.reply_text(
                f"✅ *Code Redeemed!*\n\n"
                f"🎁 Item: {payload['item_name']}\n\n"
                f"*Your Item Details:*\n"
                f"```json\n{item_json}\n```",
                parse_mode=ParseMode.MARKDOWN
            , message_effect_id=EFFECT_PARTY)
            await notify_low_stock_if_needed(context, payload["item_id"], payload["item_name"])
        else:
            reason_text = {
                "invalid": "⚠️ *Ye code exist nahi karta!*",
                "inactive": "⚠️ *Ye code ab active nahi hai!*",
                "expired": "⚠️ *Ye code expire ho chuka hai!*",
                "exhausted": "⚠️ *Is code ki redemption limit khatam ho chuki hai!*",
                "already_used": "⚠️ *Aap ye code pehle hi redeem kar chuke hain!*",
                "out_of_stock": "⚠️ *Is item ka stock khatam ho chuka hai!*",
            }.get(kind, "⚠️ *Code redeem nahi ho paya!*")
            await update.message.reply_text(reason_text, parse_mode=ParseMode.MARKDOWN, message_effect_id=EFFECT_HEART)
        
        await show_main_menu(update, context)
        return
    
    if user_id not in ADMIN_IDS:
        return
    
    action = context.user_data.get('admin_action')
    
    if not action:
        return
    
    if text.lower() == 'cancel':
        context.user_data['admin_action'] = None
        await show_admin_panel(update, context)
        return
    
    try:
        if action == 'edit_points':
            value = int(text)
            update_setting("refer_points", str(value))
            log_admin_action(user_id, "edit_points", "refer_points", f"New value: {value}")
            await update.message.reply_text(f"✅ *Referral points updated!*\n\nNew: {value} per referral", parse_mode=ParseMode.MARKDOWN, message_effect_id=EFFECT_PARTY)
            context.user_data['admin_action'] = None
            await show_admin_panel(update, context)
            
        elif action == 'edit_bonus':
            value = int(text)
            update_setting("daily_bonus", str(value))
            log_admin_action(user_id, "edit_bonus", "daily_bonus", f"New value: {value}")
            await update.message.reply_text(f"✅ *Daily bonus updated!*\n\nNew: {value} per day", parse_mode=ParseMode.MARKDOWN, message_effect_id=EFFECT_PARTY)
            context.user_data['admin_action'] = None
            await show_admin_panel(update, context)
            
        elif action.startswith('price_'):
            item_id = int(action.split("_")[1])
            value = int(text)
            update_setting(f"price_{item_id}", str(value))
            log_admin_action(user_id, "edit_price", f"price_{item_id}", f"New price: {value}")
            await update.message.reply_text(f"✅ *Price updated successfully!*\n\nNew price: {value}💰", parse_mode=ParseMode.MARKDOWN, message_effect_id=EFFECT_PARTY)
            context.user_data['admin_action'] = None
            await show_admin_panel(update, context)
            
        elif action == 'create_code':
            parts = text.split()
            if len(parts) < 2 or len(parts) > 3:
                await update.message.reply_text(
                    "⚠️ *Invalid format! Use:* `credits expiry_hours [max_uses]`",
                    parse_mode=ParseMode.MARKDOWN
                , message_effect_id=EFFECT_THUMBS_DOWN)
                return
            
            credits_val = int(parts[0])
            expiry_hours = int(parts[1])
            max_uses = int(parts[2]) if len(parts) == 3 else 1
            
            if credits_val <= 0 or max_uses <= 0 or expiry_hours < 0:
                await update.message.reply_text("⚠️ *Values must be positive numbers!*", parse_mode=ParseMode.MARKDOWN, message_effect_id=EFFECT_THUMBS_DOWN)
                return
            
            redeem = create_redeem_code(credits_val, expiry_hours if expiry_hours > 0 else None, max_uses, created_by=user_id)
            log_admin_action(user_id, "create_code", redeem["code"], f"Credits: {credits_val}, Expiry hrs: {expiry_hours}, Max uses: {max_uses}")
            
            expiry_text = redeem["expiry"] if redeem["expiry"] else "Never"
            bot_username = context.bot.username
            redeem_keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("🎁 Redeem Now", url=f"https://t.me/{bot_username}?start=redeem_{redeem['code']}")]
            ])
            await update.message.reply_text(
                f"✅ *Redeem Code Created!*\n\n"
                f"🎟 Code: `{redeem['code']}`\n"
                f"💰 Credits: {credits_val}\n"
                f"⏰ Expiry: {expiry_text}\n"
                f"👥 Max Uses: {max_uses}\n\n"
                f"👉 Tapping *Redeem Now* below auto-redeems this exact code instantly.",
                reply_markup=redeem_keyboard,
                parse_mode=ParseMode.MARKDOWN
            , message_effect_id=EFFECT_PARTY)
            context.user_data['admin_action'] = None
            await show_admin_panel(update, context)
            
        elif action == 'gift' or action == 'deduct':
            parts = text.split()
            if len(parts) != 2:
                await update.message.reply_text("⚠️ *Invalid format! Use: user_id amount*", parse_mode=ParseMode.MARKDOWN, message_effect_id=EFFECT_THUMBS_DOWN)
                return
            
            target_id = int(parts[0])
            amount = int(parts[1])
            
            if amount < 0:
                await update.message.reply_text("⚠️ *Amount cannot be negative!*", parse_mode=ParseMode.MARKDOWN, message_effect_id=EFFECT_THUMBS_DOWN)
                return
            
            target_user = get_user(target_id)
            if not target_user:
                await update.message.reply_text(f"⚠️ *User {target_id} not found!*", parse_mode=ParseMode.MARKDOWN)
                return
            
            if action == 'gift':
                add_credits(target_id, amount)
                log_admin_action(user_id, "gift_credits", str(target_id), f"Amount: {amount}")
                await update.message.reply_text(
                    f"✅ *Credits gifted!*\n\n"
                    f"User: {escape_md(target_user['first_name'])} (@{escape_md(target_user['username'])})\n"
                    f"Amount: {amount}💰\n"
                    f"New Balance: {target_user['credits'] + amount}💰",
                    parse_mode=ParseMode.MARKDOWN
                , message_effect_id=EFFECT_PARTY)
            else:
                if target_user['credits'] < amount:
                    await update.message.reply_text(
                        f"⚠️ *Insufficient credits!*\n\n"
                        f"User: {escape_md(target_user['first_name'])}\n"
                        f"Balance: {target_user['credits']}💰\n"
                        f"Requested deduction: {amount}💰",
                        parse_mode=ParseMode.MARKDOWN
                    , message_effect_id=EFFECT_THUMBS_DOWN)
                    return
                deduct_credits(target_id, amount)
                log_admin_action(user_id, "deduct_credits", str(target_id), f"Amount: {amount}")
                await update.message.reply_text(
                    f"✅ *Credits deducted!*\n\n"
                    f"User: {escape_md(target_user['first_name'])} (@{escape_md(target_user['username'])})\n"
                    f"Amount: {amount}💰\n"
                    f"New Balance: {target_user['credits'] - amount}💰",
                    parse_mode=ParseMode.MARKDOWN
                , message_effect_id=EFFECT_PARTY)
            
            context.user_data['admin_action'] = None
            await show_admin_panel(update, context)
            
        elif action == 'user_info':
            query = text.strip()
            if query.lstrip('@').isdigit():
                target_user = get_user(int(query.lstrip('@')))
            else:
                target_user = get_user_by_username(query)
            if not target_user:
                await update.message.reply_text(f"⚠️ *User '{escape_md(query)}' not found!*", parse_mode=ParseMode.MARKDOWN)
                return
            target_id = target_user['user_id']
            
            purchases = get_user_purchases(target_id)
            purchase_text = ""
            if purchases:
                purchase_text = "\n\n*Purchases:*\n"
                for p in purchases[:10]:
                    purchase_text += f"• {p['item_name']} - {p['purchase_date']}\n"
                    purchase_text += f"  UID: {p['guest_uid']}\n"
            
            ban_status = "🚫 Banned" if target_user.get('banned') else "✅ Not banned"
            await update.message.reply_text(
                f"👤 *User Information*\n\n"
                f"ID: {target_user['user_id']}\n"
                f"Name: {escape_md(target_user['first_name'])}\n"
                f"Username: @{escape_md(target_user['username']) or 'None'}\n"
                f"Credits: {target_user['credits']}💰\n"
                f"Referrals: {target_user['total_referrals']}\n"
                f"Joined: {target_user['joined_date']}\n"
                f"Status: {ban_status}\n"
                f"Total Purchases: {len(purchases)}\n"
                f"{purchase_text}",
                parse_mode=ParseMode.MARKDOWN
            , message_effect_id=EFFECT_HEART)
            context.user_data['admin_action'] = None
            await show_admin_panel(update, context)

        elif action == 'ban_user':
            target_id = int(text)
            target_user = get_user(target_id)
            if not target_user:
                await update.message.reply_text(f"⚠️ *User {target_id} not found!*", parse_mode=ParseMode.MARKDOWN)
                return
            ban_user(target_id)
            log_admin_action(user_id, "ban_user", str(target_id), "Banned")
            await update.message.reply_text(
                f"🚫 *User Banned!*\n\n"
                f"User: {escape_md(target_user['first_name'])} (@{escape_md(target_user['username'])})\n"
                f"ID: {target_id}\n\n"
                f"This user can no longer use the bot until unbanned.",
                parse_mode=ParseMode.MARKDOWN
            , message_effect_id=EFFECT_THUMBS_DOWN)
            context.user_data['admin_action'] = None
            await show_admin_panel(update, context)

        elif action == 'unban_user':
            target_id = int(text)
            target_user = get_user(target_id)
            if not target_user:
                await update.message.reply_text(f"⚠️ *User {target_id} not found!*", parse_mode=ParseMode.MARKDOWN)
                return
            unban_user(target_id)
            log_admin_action(user_id, "unban_user", str(target_id), "Unbanned")
            await update.message.reply_text(
                f"✅ *User Unbanned!*\n\n"
                f"User: {escape_md(target_user['first_name'])} (@{escape_md(target_user['username'])})\n"
                f"ID: {target_id}\n\n"
                f"This user can use the bot again.",
                parse_mode=ParseMode.MARKDOWN
            , message_effect_id=EFFECT_PARTY)
            context.user_data['admin_action'] = None
            await show_admin_panel(update, context)

        elif action == 'message_user_target':
            target_id = int(text)
            target_user = get_user(target_id)
            if not target_user:
                await update.message.reply_text(f"⚠️ *User {target_id} not found!*", parse_mode=ParseMode.MARKDOWN)
                return
            context.user_data['single_broadcast_target'] = target_id
            context.user_data['admin_action'] = 'message_user_text'
            await update.message.reply_text(
                f"📨 *Message for {escape_md(target_user['first_name'])}*\n\n"
                f"Ab wo message bhejein jo sirf isi user (ID: {target_id}) ko jayega.\n\n"
                f"Type 'cancel' to cancel.",
                parse_mode=ParseMode.MARKDOWN
            , message_effect_id=EFFECT_HEART)

        elif action == 'message_user_text':
            target_id = context.user_data.get('single_broadcast_target')
            target_user = get_user(target_id) if target_id else None
            if not target_id or not target_user:
                await update.message.reply_text("⚠️ *Target user lost, please start again.*", parse_mode=ParseMode.MARKDOWN)
                context.user_data['admin_action'] = None
                await show_admin_panel(update, context)
                return
            try:
                # Plain text, same reason as the mass broadcast below - a stray
                # markdown character in free-typed text shouldn't be able to
                # make the send fail.
                await context.bot.send_message(target_id, f"📨 Message\n\n{text}", message_effect_id=EFFECT_FIRE)
                log_admin_action(user_id, "message_user", str(target_id), f"Sent to {target_id}")
                await update.message.reply_text(
                    f"✅ *Message sent!*\n\n"
                    f"To: {escape_md(target_user['first_name'])} (ID: {target_id})",
                    parse_mode=ParseMode.MARKDOWN
                , message_effect_id=EFFECT_PARTY)
            except Forbidden:
                await update.message.reply_text("🚫 *This user has blocked the bot — message not delivered.*", parse_mode=ParseMode.MARKDOWN, message_effect_id=EFFECT_THUMBS_DOWN)
            except Exception as e:
                await update.message.reply_text(f"⚠️ *Error sending message: {str(e)}*", parse_mode=ParseMode.MARKDOWN)
            context.user_data['admin_action'] = None
            context.user_data['single_broadcast_target'] = None
            await show_admin_panel(update, context)

        elif action == 'broadcast':
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute("SELECT user_id FROM users")
            users = c.fetchall()
            conn.close()
            
            sent = 0
            blocked = 0
            failed = 0
            for user in users:
                try:
                    # Sent as PLAIN TEXT on purpose. This used to use
                    # parse_mode=MARKDOWN on your raw typed text - a single stray
                    # _ / * / ` / [ in the message (extremely common in normal
                    # sentences) makes Telegram reject the message for EVERY
                    # recipient at once, since it's the same broken text each time.
                    # That's exactly why 1 test user worked but all real users failed.
                    await context.bot.send_message(user[0], f"📢 Announcement\n\n{text}", message_effect_id=EFFECT_FIRE)
                    sent += 1
                except Forbidden:
                    # User has blocked the bot / deleted their account - expected
                    # over time, not a bug, just skip them.
                    blocked += 1
                except Exception:
                    failed += 1
                await asyncio.sleep(0.05)
            
            log_admin_action(user_id, "broadcast", "all_users", f"Sent: {sent}, Blocked: {blocked}, Failed: {failed}")
            await update.message.reply_text(
                f"✅ *Broadcast Completed!*\n\n"
                f"Sent: {sent} users\n"
                f"🚫 Blocked bot: {blocked}\n"
                f"⚠️ Other failures: {failed}",
                parse_mode=ParseMode.MARKDOWN
            , message_effect_id=EFFECT_THUMBS_DOWN)
            context.user_data['admin_action'] = None
            await show_admin_panel(update, context)

        elif action == 'create_item_code_details':
            item_id = context.user_data.get('pending_item_code_id')
            item_name = context.user_data.get('pending_item_code_name')
            if not item_id:
                await update.message.reply_text("⚠️ *Item lost, please start again.*", parse_mode=ParseMode.MARKDOWN)
                context.user_data['admin_action'] = None
                await show_admin_panel(update, context)
                return
            parts = text.split()
            if len(parts) < 1 or len(parts) > 2:
                await update.message.reply_text("⚠️ *Invalid format! Use:* `expiry_hours [max_uses]`", parse_mode=ParseMode.MARKDOWN, message_effect_id=EFFECT_THUMBS_DOWN)
                return
            expiry_hours = int(parts[0])
            max_uses = int(parts[1]) if len(parts) == 2 else 1
            if max_uses <= 0 or expiry_hours < 0:
                await update.message.reply_text("⚠️ *Values must be positive numbers!*", parse_mode=ParseMode.MARKDOWN, message_effect_id=EFFECT_THUMBS_DOWN)
                return

            redeem = create_redeem_code(0, expiry_hours if expiry_hours > 0 else None, max_uses, created_by=user_id, item_id=item_id, item_name=item_name)
            log_admin_action(user_id, "create_item_code", redeem["code"], f"Item: {item_name}, Expiry hrs: {expiry_hours}, Max uses: {max_uses}")

            expiry_text = redeem["expiry"] if redeem["expiry"] else "Never"
            bot_username = context.bot.username
            redeem_keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("🎁 Redeem Now", url=f"https://t.me/{bot_username}?start=redeem_{redeem['code']}")]
            ])
            await update.message.reply_text(
                f"✅ *Item Redeem Code Created!*\n\n"
                f"🎟 Code: `{redeem['code']}`\n"
                f"🎁 Item: {item_name}\n"
                f"⏰ Expiry: {expiry_text}\n"
                f"👥 Max Uses: {max_uses}\n\n"
                f"👉 Tapping *Redeem Now* below auto-redeems this exact code instantly.",
                reply_markup=redeem_keyboard,
                parse_mode=ParseMode.MARKDOWN
            , message_effect_id=EFFECT_PARTY)
            context.user_data['admin_action'] = None
            context.user_data['pending_item_code_id'] = None
            context.user_data['pending_item_code_name'] = None
            await show_admin_panel(update, context)

        elif action == 'view_code_users':
            code = text.strip().upper()
            redeem = get_redeem_code(code)
            if not redeem:
                await update.message.reply_text(f"⚠️ *Code '{code}' not found!*", parse_mode=ParseMode.MARKDOWN)
                return
            rows = get_code_redemptions(code)
            reward = f"🎁 {escape_md(redeem['item_name'])}" if redeem.get("item_id") else f"{redeem['credits']}💰"
            out = f"🎟 *Code:* `{code}` — {reward}\n📊 Used: {redeem['used_count']}/{redeem['max_uses']}\n\n"
            if rows:
                out += "*Redeemed by:*\n"
                for r in rows:
                    name = escape_md(r['first_name'] or r['username'] or str(r['user_id']))
                    uname = f"@{escape_md(r['username'])}" if r['username'] else "no username"
                    out += f"• {name} ({uname}) [ID: {r['user_id']}] — {r['timestamp']}\n"
            else:
                out += "Abhi tak kisi ne bhi redeem nahi kiya."
            await update.message.reply_text(out, parse_mode=ParseMode.MARKDOWN, message_effect_id=EFFECT_HEART)
            context.user_data['admin_action'] = None
            await show_admin_panel(update, context)

        elif action == 'set_db_channel':
            channel_input = text.strip()
            try:
                await context.bot.send_message(channel_input, "✅ Database Channel Connected! Har 30 min mein yahan auto-backup aayega.")
                update_setting("db_channel_id", channel_input)
                log_admin_action(user_id, "set_db_channel", channel_input, "Database channel configured")
                await update.message.reply_text(
                    f"✅ *Channel Set!*\n\n"
                    f"`{escape_md(channel_input)}`\n\n"
                    f"💡 Permanent ke liye Railway → Variables mein bhi `DB_CHANNEL_ID={channel_input}` add kar dena, "
                    f"warna full data-loss ke case mein restore nahi ho payega.",
                    parse_mode=ParseMode.MARKDOWN
                , message_effect_id=EFFECT_PARTY)
            except Exception as e:
                await update.message.reply_text(
                    f"⚠️ *Channel se connect nahi ho paya:* {str(e)}\n\n"
                    f"Check karo bot us channel mein admin hai ya nahi.",
                    parse_mode=ParseMode.MARKDOWN
                , message_effect_id=EFFECT_THUMBS_DOWN)
            context.user_data['admin_action'] = None
            await show_admin_panel(update, context)

    except ValueError:
        await update.message.reply_text("⚠️ *Please enter a valid number!*", parse_mode=ParseMode.MARKDOWN, message_effect_id=EFFECT_THUMBS_DOWN)

async def show_upload_inventory_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['admin_action'] = None
    remaining = sum(item["available"] for item in get_all_special_items())
    keyboard = [
        [KeyboardButton("📥 Send New File")],
        [KeyboardButton("📤 Export Remaining Accounts")],
        [KeyboardButton("🗑 Reset Inventory")],
        [KeyboardButton("🔙 Back")]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    await update.message.reply_text(
        f"📦 *Inventory Management*\n\n"
        f"📊 Accounts currently in stock: {remaining}\n\n"
        f"🔹 *Send New File* — upload a JSON file with new stock\n"
        f"🔹 *Export Remaining Accounts* — get a JSON file of everything still unsold\n"
        f"🔹 *Reset Inventory* — permanently delete ALL stored accounts (sold + unsold)\n\n"
        f"✅ Credits, referral points and prices are *never* touched by any of these.",
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN
    , message_effect_id=EFFECT_PARTY)

async def show_users_db_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['admin_action'] = None
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM users")
    total_users = c.fetchone()[0]
    conn.close()
    keyboard = [
        [KeyboardButton("📥 Upload Users DB")],
        [KeyboardButton("📤 Export Users DB")],
        [KeyboardButton("🗑 Reset Users DB")],
        [KeyboardButton("🔙 Back")]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    await update.message.reply_text(
        f"🗄 *Users Database*\n\n"
        f"👥 Users currently in database: {total_users}\n\n"
        f"🔹 *Upload Users DB* — upload a JSON backup, new user_ids get added\n"
        f"🔹 *Export Users DB* — get a JSON file of the entire current users database\n"
        f"🔹 *Reset Users DB* — permanently delete ALL users (credits, referrals, purchase history, daily bonus, redeem history)\n\n"
        f"✅ This is completely separate from Inventory — item stock, prices, referral/bonus settings and redeem code definitions are *never* touched by any of these.",
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN
    , message_effect_id=EFFECT_PARTY)

async def handle_inventory_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    user_id = update.effective_user.id

    if user_id not in ADMIN_IDS:
        return

    if text == "📥 Upload Users DB":
        context.user_data['admin_action'] = 'upload_users_db'
        await update.message.reply_text(
            f"🗄 *Upload Users DB*\n\n"
            f"Send me a JSON file with the users backup data.\n"
            f"Existing user_ids in the database are skipped — this only *adds* new users.\n\n"
            f"Type 'cancel' to cancel.",
            parse_mode=ParseMode.MARKDOWN
        , message_effect_id=EFFECT_HEART)

    elif text == "📤 Export Users DB":
        export_data = get_users_export()
        if not export_data:
            await update.message.reply_text("⚠️ *No users in the database yet!*", parse_mode=ParseMode.MARKDOWN, message_effect_id=EFFECT_THUMBS_DOWN)
            return

        file_path = f"/tmp/users_db_export_{int(now_ist().timestamp())}.json"
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(export_data, f, indent=4, ensure_ascii=False)

        try:
            with open(file_path, "rb") as f:
                await update.message.reply_document(
                    document=f,
                    filename="users_db_export.json",
                    caption=f"📤 *Users Database Export*\n\nTotal users: {len(export_data)}",
                    parse_mode=ParseMode.MARKDOWN
                )
        finally:
            os.remove(file_path)

        log_admin_action(user_id, "export_users_db", "users", f"Exported: {len(export_data)} users")

    elif text == "🗑 Reset Users DB":
        context.user_data['admin_action'] = 'confirm_reset_users_db'
        keyboard = [
            [KeyboardButton("✅ Yes, Reset Users DB")],
            [KeyboardButton("❌ No, Cancel")]
        ]
        reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        await update.message.reply_text(
            f"⚠️ *Are you sure?*\n\n"
            f"This will permanently delete ALL users — credits, referrals, purchase history, daily bonus claims and redeem code history. "
            f"This action *cannot be undone*.\n\n"
            f"✅ Inventory stock, prices, referral/bonus settings and redeem code definitions will *NOT* be touched.",
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN
        , message_effect_id=EFFECT_THUMBS_DOWN)

    elif text == "✅ Yes, Reset Users DB" and context.user_data.get('admin_action') == 'confirm_reset_users_db':
        deleted_count = reset_users_db()
        log_admin_action(user_id, "reset_users_db", "users", f"Deleted: {deleted_count} users")
        context.user_data['admin_action'] = None
        await update.message.reply_text(
            f"✅ *Users Database Reset Complete!*\n\n"
            f"🗑 Deleted: {deleted_count} users\n\n"
            f"📦 Inventory and settings are untouched.",
            parse_mode=ParseMode.MARKDOWN
        , message_effect_id=EFFECT_PARTY)
        await show_admin_panel(update, context)

    elif text == "❌ No, Cancel" and context.user_data.get('admin_action') == 'confirm_reset_users_db':
        context.user_data['admin_action'] = None
        await update.message.reply_text("❌ *Reset cancelled.* Nothing was deleted.", parse_mode=ParseMode.MARKDOWN, message_effect_id=EFFECT_THUMBS_DOWN)
        await show_users_db_menu(update, context)

    elif text == "📥 Send New File":
        context.user_data['admin_action'] = 'upload_inventory'
        await update.message.reply_text(
            f"📦 *Upload Inventory*\n\n"
            f"Send me a JSON file with the inventory data.\n\n"
            f"Only special items will be stored.\n"
            f"Type 'cancel' to cancel.",
            parse_mode=ParseMode.MARKDOWN
        , message_effect_id=EFFECT_HEART)

    elif text == "📤 Export Remaining Accounts":
        export_data = get_available_inventory_export()
        if not export_data:
            await update.message.reply_text("⚠️ *No remaining accounts in inventory!*", parse_mode=ParseMode.MARKDOWN, message_effect_id=EFFECT_THUMBS_DOWN)
            return

        file_path = f"/tmp/remaining_inventory_{int(now_ist().timestamp())}.json"
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(export_data, f, indent=4, ensure_ascii=False)

        try:
            with open(file_path, "rb") as f:
                await update.message.reply_document(
                    document=f,
                    filename="remaining_inventory.json",
                    caption=f"📤 *Remaining Accounts Export*\n\nTotal accounts: {len(export_data)}",
                    parse_mode=ParseMode.MARKDOWN
                )
        finally:
            os.remove(file_path)

        log_admin_action(user_id, "export_inventory", "inventory", f"Exported: {len(export_data)} accounts")

    elif text == "🗑 Reset Inventory":
        context.user_data['admin_action'] = 'confirm_reset_inventory'
        keyboard = [
            [KeyboardButton("✅ Yes, Reset Inventory")],
            [KeyboardButton("❌ No, Cancel")]
        ]
        reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        await update.message.reply_text(
            f"⚠️ *Are you sure?*\n\n"
            f"This will permanently delete ALL stored accounts (sold + unsold) from the database. "
            f"This action *cannot be undone*.\n\n"
            f"✅ Credits, referral points, prices and user data will *NOT* be touched — only inventory accounts are removed.",
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN
        , message_effect_id=EFFECT_THUMBS_DOWN)

    elif text == "✅ Yes, Reset Inventory" and context.user_data.get('admin_action') == 'confirm_reset_inventory':
        deleted_count = reset_inventory()
        log_admin_action(user_id, "reset_inventory", "inventory", f"Deleted: {deleted_count} accounts")
        context.user_data['admin_action'] = None
        await update.message.reply_text(
            f"✅ *Inventory Reset Complete!*\n\n"
            f"🗑 Deleted: {deleted_count} accounts from the database\n\n"
            f"💰 Credits and referral points are untouched.",
            parse_mode=ParseMode.MARKDOWN
        , message_effect_id=EFFECT_PARTY)
        await show_admin_panel(update, context)

    elif text == "❌ No, Cancel" and context.user_data.get('admin_action') == 'confirm_reset_inventory':
        context.user_data['admin_action'] = None
        await update.message.reply_text("❌ *Reset cancelled.* Nothing was deleted.", parse_mode=ParseMode.MARKDOWN, message_effect_id=EFFECT_THUMBS_DOWN)
        await show_upload_inventory_menu(update, context)

async def handle_users_db_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    document = update.message.document
    if not document.file_name.endswith('.json'):
        await update.message.reply_text("⚠️ *Please send a JSON file!*", parse_mode=ParseMode.MARKDOWN)
        return

    try:
        file = await context.bot.get_file(document.file_id)
        file_content = await file.download_as_bytearray()
        data = json.loads(file_content)

        if not isinstance(data, list):
            await update.message.reply_text("⚠️ *Invalid format! Expected a list of users.*", parse_mode=ParseMode.MARKDOWN, message_effect_id=EFFECT_THUMBS_DOWN)
            return

        result = add_users_bulk(data)
        log_admin_action(user_id, "upload_users_db", "users", f"Added: {result['added']}, Skipped: {result['skipped']}")

        await update.message.reply_text(
            f"🗄 *Users DB Upload Summary*\n\n"
            f"✅ Added: {result['added']} users\n"
            f"⏭️ Skipped (already existed): {result['skipped']} users",
            parse_mode=ParseMode.MARKDOWN
        , message_effect_id=EFFECT_HEART)
        context.user_data['admin_action'] = None
        await show_admin_panel(update, context)

    except json.JSONDecodeError:
        await update.message.reply_text("⚠️ *Invalid JSON file!*", parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        await update.message.reply_text(f"⚠️ *Error: {str(e)}*", parse_mode=ParseMode.MARKDOWN)

async def handle_restore_db_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    document = update.message.document
    if not document.file_name.endswith('.db'):
        await update.message.reply_text("⚠️ *Please send a .db file!*", parse_mode=ParseMode.MARKDOWN)
        return
    try:
        file = await context.bot.get_file(document.file_id)
        tmp_path = DB_PATH + ".incoming"
        await file.download_to_drive(tmp_path)
        os.replace(tmp_path, DB_PATH)
        init_db()  # re-run migrations against the restored file, just in case it's from an older schema
        log_admin_action(user_id, "restore_db", "database", f"Restored from uploaded file: {document.file_name}")
        await update.message.reply_text("✅ *Database restored successfully!*", parse_mode=ParseMode.MARKDOWN, message_effect_id=EFFECT_PARTY)
        context.user_data['admin_action'] = None
        await show_admin_panel(update, context)
    except Exception as e:
        await update.message.reply_text(f"⚠️ *Error restoring database: {str(e)}*", parse_mode=ParseMode.MARKDOWN)

async def handle_inventory_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        return
    
    if not update.message.document:
        await update.message.reply_text("⚠️ *Please send a JSON file!*", parse_mode=ParseMode.MARKDOWN)
        return

    pending_action = context.user_data.get('admin_action')

    if pending_action == 'restore_db_upload':
        await handle_restore_db_upload(update, context)
        return

    if pending_action == 'upload_users_db':
        await handle_users_db_upload(update, context)
        return

    if pending_action != 'upload_inventory':
        return
    
    document = update.message.document
    if not document.file_name.endswith('.json'):
        await update.message.reply_text("⚠️ *Please send a JSON file!*", parse_mode=ParseMode.MARKDOWN)
        return
    
    try:
        file = await context.bot.get_file(document.file_id)
        file_content = await file.download_as_bytearray()
        data = json.loads(file_content)
        
        if not isinstance(data, list):
            await update.message.reply_text("⚠️ *Invalid format! Expected a list of items.*", parse_mode=ParseMode.MARKDOWN, message_effect_id=EFFECT_THUMBS_DOWN)
            return
        
        special_item_ids = [
            203054011, 909054007, 909854001, 907105450, 911005401,
            907104730, 907105424, 907105405, 903054002, 903054003,
            903054004, 903054005, 903054006
        ]
        
        filtered_items = []
        for item in data:
            if item.get("item_id") in special_item_ids:
                filtered_items.append(item)
        
        if not filtered_items:
            await update.message.reply_text("⚠️ *No special items found in the file!*", parse_mode=ParseMode.MARKDOWN, message_effect_id=EFFECT_THUMBS_DOWN)
            return
        
        result = add_inventory_bulk(filtered_items)
        
        # Count using the exact list of names that were actually added
        # (fixes wrong counts that could happen with the old guess-based logic)
        from collections import Counter
        item_counts = Counter(result['added_names'])
        
        summary = "📊 *Inventory Upload Summary*\n\n"
        summary += f"✅ Added: {result['added']} items\n"
        summary += f"⏭️ Skipped (duplicates): {result['skipped']} items\n\n"
        summary += "*Items Added:*\n"
        for name, count in item_counts.items():
            summary += f"• {name}: {count}\n"
        
        log_admin_action(user_id, "upload_inventory", "inventory", f"Added: {result['added']}, Skipped: {result['skipped']}")
        
        await update.message.reply_text(summary, parse_mode=ParseMode.MARKDOWN, message_effect_id=EFFECT_HEART)
        context.user_data['admin_action'] = None
        await show_admin_panel(update, context)
        
    except json.JSONDecodeError:
        await update.message.reply_text("⚠️ *Invalid JSON file!*", parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        await update.message.reply_text(f"⚠️ *Error: {str(e)}*", parse_mode=ParseMode.MARKDOWN)

# ==================== MAIN ====================

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Global safety net: if any handler above throws, this catches it so one
    bad update can't take down the bot or leave a user stuck with no reply -
    it logs the real error for debugging and gives the user a plain message."""
    import traceback
    print(f"⚠️ Unhandled exception: {context.error}")
    traceback.print_exception(type(context.error), context.error, context.error.__traceback__)
    try:
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text(
                "⚠️ Kuch gadbad ho gayi, please dobara try karein."
            , message_effect_id=EFFECT_THUMBS_DOWN)
    except:
        pass

async def post_init(application: Application):
    # Restore is only needed when bot_data.db doesn't exist locally at all
    # (fresh deploy, no persistent volume) - otherwise the local file is left alone.
    await restore_db_from_channel(application.bot)
    init_db()
    asyncio.create_task(backup_loop(application.bot))
    asyncio.create_task(daily_summary_loop(application.bot))

def main():
    application = Application.builder().token(TOKEN).post_init(post_init).build()
    application.add_error_handler(error_handler)
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(check_joined, pattern="check_joined"))
    
    application.add_handler(MessageHandler(filters.Regex(r'^🛒 Buy Items$|^💰 My Credits$|^🎁 Daily Bonus$|^📤 Referral$|^🏆 Leaderboard$|^🎟 Redeem Code$|^❓ Help$|^⚙️ Admin Panel$|^🔙 Back$|^❌ Cancel$'), handle_menu_buttons))
    # NOTE: handle_confirm_purchase's pattern (✅ Confirm||123) is a SUBSET of
    # handle_buy_selection's generic pattern (.*||123). It MUST be registered
    # first, otherwise the generic handler always wins and the Confirm button
    # never actually completes a purchase - it just re-shows the confirm screen.
    application.add_handler(MessageHandler(filters.Regex(r'^✅ Confirm\|\|\d+$'), handle_confirm_purchase))
    application.add_handler(MessageHandler(filters.Regex(r'^.*\|\|\d+$'), handle_buy_selection))
    
    application.add_handler(MessageHandler(filters.Regex(r'^💰 Edit Prices$|^🔄 Edit Points$|^🎁 Edit Bonus$|^🎁 Gift Credits$|^🔻 Deduct Credits$|^👤 User Info$|^📦 Upload Inventory$|^🗄 Users Database$|^📢 Broadcast$|^📨 Message User$|^🚫 Ban User$|^✅ Unban User$|^📊 Stats$|^🎟 Create Code$|^🎁 Item Code$|^📋 List Codes$|^👥 Code Users$|^🔌 Toggle Bot Status$|^🗄 Database Channel$|^📥 Restore Database$'), handle_admin_buttons))
    application.add_handler(MessageHandler(filters.Regex(r'^📥 Send New File$|^📤 Export Remaining Accounts$|^🗑 Reset Inventory$|^✅ Yes, Reset Inventory$|^📥 Upload Users DB$|^📤 Export Users DB$|^🗑 Reset Users DB$|^✅ Yes, Reset Users DB$|^❌ No, Cancel$'), handle_inventory_menu))
    application.add_handler(MessageHandler(filters.Regex(r'^.* - \d+💰\|\|price_\d+$'), handle_price_selection))
    application.add_handler(MessageHandler(filters.Regex(r'^.*\|\|itemcode_\d+$'), handle_item_code_select))
    
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_admin_text_input))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_inventory_upload))
    
    print("🤖 Bot started successfully!")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()