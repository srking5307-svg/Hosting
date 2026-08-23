import os
import json
import sqlite3
import asyncio
from datetime import datetime, timedelta, timezone
from functools import wraps
from typing import Optional, Dict, List
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup
from telegram.error import Forbidden
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, ChatMemberHandler, filters, ContextTypes
from telegram.constants import ParseMode

# ==================== CONFIGURATION ====================
TOKEN = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
ADMIN_IDS = [int(x) for x in os.environ.get("ADMIN_IDS", "7892255798").split(",")]
CHANNELS_DEFAULT = ["@SRK_ERA", "@SRKING000001", "@SRK_IMP1"]

DB_PATH = "bot_data.db"

# Optional: set this in Railway's Variables tab (not just via the admin panel)
# so the channel ID survives a full filesystem wipe - that's the one thing
# that isn't stored inside bot_data.db itself, since it's needed BEFORE that
# file exists again after a wipe.
DB_CHANNEL_ENV = os.environ.get("DB_CHANNEL_ID")

# --- Shared persistent DB connection -------------------------------------
# Opening a brand-new sqlite3 connection on every single query (the original
# design) is the single biggest source of latency in a bot like this - each
# open/close is a real filesystem round-trip. Reusing one connection for the
# life of the process, with WAL mode on, is dramatically faster and also
# avoids "database is locked" errors under concurrent load.
_db_conn = None

def get_db():
    global _db_conn
    if _db_conn is None:
        _db_conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
        _db_conn.execute("PRAGMA journal_mode=WAL")
        _db_conn.execute("PRAGMA synchronous=NORMAL")
        _db_conn.execute("PRAGMA busy_timeout=30000")
    return _db_conn

def reset_db_connection():
    """Call this immediately after the bot_data.db file on disk is replaced
    wholesale (restore from channel / manual upload) - otherwise the old
    connection keeps reading/writing the now-detached original file handle
    instead of the new one, silently."""
    global _db_conn
    if _db_conn is not None:
        try:
            _db_conn.close()
        except Exception:
            pass
        _db_conn = None

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
    conn = get_db()
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
    try:
        c.execute("ALTER TABLE redeem_codes ADD COLUMN message TEXT")
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

    # --- New tables / columns for the newer feature set ---
    try:
        c.execute("ALTER TABLE users ADD COLUMN shadow_banned INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    try:
        c.execute("ALTER TABLE users ADD COLUMN channel_left_notify INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    try:
        c.execute("ALTER TABLE users ADD COLUMN last_purchase_ts TEXT")
    except sqlite3.OperationalError:
        pass

    c.execute('''CREATE TABLE IF NOT EXISTS welcome_images (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        file_id TEXT,
        added_at TEXT
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS item_channel_routes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        item_id INTEGER,
        item_name TEXT,
        channel_id TEXT
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS preorders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        item_id INTEGER,
        item_name TEXT,
        price INTEGER,
        timestamp TEXT,
        fulfilled INTEGER DEFAULT 0
    )''')

    # Seed the one routing rule that's already known about (Dark Desire Bundle
    # -> @SRK_ERA) but only once - admin can add/remove/edit more from the
    # panel afterwards, this is just a sane starting point.
    c.execute("SELECT COUNT(*) FROM item_channel_routes")
    if c.fetchone()[0] == 0:
        c.execute("INSERT INTO item_channel_routes (item_id, item_name, channel_id) VALUES (?, ?, ?)",
                  (203054011, "DARK DESIRE BUNDLE", "@SRK_ERA"))

    # Indexes - the hot paths (inventory lookups, purchase history, code use
    # checks) were doing full table scans before.
    c.execute("CREATE INDEX IF NOT EXISTS idx_inventory_item_sold ON inventory(item_id, sold)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_purchases_user ON purchases(user_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_purchases_item ON purchases(item_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_purchases_date ON purchases(purchase_date)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_redeem_uses_code ON redeem_code_uses(code)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_redeem_uses_user ON redeem_code_uses(user_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_referrals_referrer ON referrals(referrer_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_users_banned ON users(banned)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_preorders_item ON preorders(item_id, fulfilled)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_preorders_user ON preorders(user_id)")

    conn.commit()
def get_setting(key):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT value FROM settings WHERE key = ?", (key,))
    result = c.fetchone()
    return result[0] if result else None

def update_setting(key, value):
    conn = get_db()
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
    conn.commit()

# ==================== REQUIRED CHANNELS (force-subscribe list) ====================

_channels_cache = {"value": None, "raw": None}

def get_required_channels():
    raw = get_setting("required_channels")
    if raw == _channels_cache["raw"] and _channels_cache["value"] is not None:
        return _channels_cache["value"]
    if not raw:
        result = list(CHANNELS_DEFAULT)
    else:
        try:
            parsed = json.loads(raw)
            result = parsed if isinstance(parsed, list) and parsed else list(CHANNELS_DEFAULT)
        except Exception:
            result = list(CHANNELS_DEFAULT)
    _channels_cache["raw"] = raw
    _channels_cache["value"] = result
    return result

def save_required_channels(channels):
    update_setting("required_channels", json.dumps(channels))
    _channels_cache["raw"] = None  # force a fresh read next call, not the stale cached list
    _channels_cache["value"] = None

async def get_missing_channels(bot, user_id):
    """Checks every required channel AT ONCE (concurrently) instead of one
    at a time in a loop - with 3+ channels this turns 3 sequential network
    round-trips into 1. Returns the list of channels the user is actually
    NOT currently a member of (empty list = fully verified)."""
    channels = get_required_channels()
    if not channels:
        return []

    async def check_one(channel):
        try:
            member = await bot.get_chat_member(channel, user_id)
            if member.status in ("left", "kicked"):
                return channel
            return None
        except Exception as e:
            # Fires for every user if the bot isn't admin in that channel -
            # treat it as "not joined" (fail safe) rather than silently
            # letting everyone through.
            print(f"⚠️ get_chat_member failed for {channel} / user {user_id}: {e}")
            return channel

    results = await asyncio.gather(*(check_one(ch) for ch in channels), return_exceptions=False)
    return [ch for ch in results if ch]

def build_join_keyboard(missing_channels):
    keyboard = []
    for channel in missing_channels:
        channel_name = channel.replace('@', '')
        keyboard.append([InlineKeyboardButton(f"📢 Join {channel}", url=f"https://t.me/{channel_name}", style="primary")])
    keyboard.append([InlineKeyboardButton("✅ I have joined", callback_data="check_joined", style="success")])
    return InlineKeyboardMarkup(keyboard)

async def verify_bot_can_check_channel(bot, channel_id, probe_user_id):
    """Before a channel is accepted as a required channel, actually confirm
    the bot can query membership there (i.e. it's an admin in that channel).
    Without this, adding a channel where the bot ISN'T admin silently breaks
    verification for every single user - get_chat_member() would fail for
    everyone and they'd be stuck unable to pass the join check no matter
    what they do."""
    try:
        await bot.get_chat_member(channel_id, probe_user_id)
        return True, None
    except Exception as e:
        return False, str(e)



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
        # WAL mode keeps recent writes in a separate -wal file until a
        # checkpoint merges them back - force that now so the bytes we're
        # about to copy actually include everything up to this moment.
        try:
            get_db().execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception as e:
            print(f"⚠️ WAL checkpoint before backup failed: {e}")
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
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM users WHERE joined_date LIKE ?", (f"{date_str}%",))
    new_users = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM purchases WHERE purchase_date LIKE ?", (f"{date_str}%",))
    purchases = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM redeem_code_uses WHERE timestamp LIKE ?", (f"{date_str}%",))
    redeems = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM users")
    total_users = c.fetchone()[0]
    return {"new_users": new_users, "purchases": purchases, "redeems": redeems, "total_users": total_users}

# ==================== LOW STOCK ALERTS ====================

# ==================== PURCHASE RECEIPT (Text/Image toggle) ====================

def get_receipt_mode():
    return get_setting("receipt_mode") or "text"

def generate_receipt_image(item_name, price, buyer_label, purchase_date, purchase_id=None):
    """Draws a large, crisp, branded receipt card with Pillow. Renders at 2x
    resolution then downsamples with LANCZOS so text/edges stay sharp even
    when the user zooms in on mobile - the earlier version rendered directly
    at final size, which is why it looked small/blurry."""
    from PIL import Image, ImageDraw, ImageFont
    import io

    SCALE = 2
    W, H = 1600 * SCALE, 900 * SCALE
    gold = (255, 200, 60)
    gold_bright = (255, 224, 120)
    bg_top = (18, 12, 30)
    bg_bottom = (55, 18, 70)

    img = Image.new("RGB", (W, H), bg_top)
    draw = ImageDraw.Draw(img)
    for y in range(H):
        ratio = y / H
        r = int(bg_top[0] + (bg_bottom[0] - bg_top[0]) * ratio)
        g = int(bg_top[1] + (bg_bottom[1] - bg_top[1]) * ratio)
        b = int(bg_top[2] + (bg_bottom[2] - bg_top[2]) * ratio)
        draw.line([(0, y), (W, y)], fill=(r, g, b))

    def font(size, bold=False):
        size = size * SCALE
        candidates = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ]
        for path in candidates:
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
        return ImageFont.load_default()

    def centered_text(y, text, f, fill):
        bbox = draw.textbbox((0, 0), text, font=f)
        w = bbox[2] - bbox[0]
        draw.text(((W - w) / 2, y), text, font=f, fill=fill)

    margin = 40 * SCALE
    # Outer decorative double border
    draw.rounded_rectangle([margin, margin, W - margin, H - margin], radius=28 * SCALE, outline=gold, width=4 * SCALE)
    draw.rounded_rectangle([margin + 12 * SCALE, margin + 12 * SCALE, W - margin - 12 * SCALE, H - margin - 12 * SCALE], radius=22 * SCALE, outline=(120, 90, 40), width=2 * SCALE)

    # Brand header
    centered_text(margin + 40 * SCALE, "♛ SR KING GUEST ID STORE BOT ♛", font(46, True), gold_bright)
    centered_text(margin + 105 * SCALE, "✓ PURCHASE RECEIPT", font(32, True), (255, 255, 255))

    line_y = margin + 165 * SCALE
    draw.line([(margin + 60 * SCALE, line_y), (W - margin - 60 * SCALE, line_y)], fill=gold, width=2 * SCALE)

    # Details block
    label_x = margin + 90 * SCALE
    value_x = margin + 400 * SCALE
    y = line_y + 60 * SCALE
    row_gap = 82 * SCALE
    rows = [
        ("Item", item_name),
        ("Price", f"{price} Credits"),
        ("Buyer", buyer_label),
        ("Date", purchase_date),
    ]
    if purchase_id:
        rows.append(("Txn ID", f"#{purchase_id}"))

    for label, value in rows:
        draw.text((label_x, y), f"{label}", font=font(30, True), fill=(200, 170, 220))
        draw.text((value_x, y), str(value), font=font(30), fill=(255, 255, 255))
        y += row_gap

    y += 20 * SCALE
    draw.line([(margin + 60 * SCALE, y), (W - margin - 60 * SCALE, y)], fill=(120, 90, 40), width=2 * SCALE)
    y += 50 * SCALE

    centered_text(y, "★ Thank you for shopping with SR KING! ★", font(30, True), gold_bright)
    centered_text(y + 55 * SCALE, "Fast • Trusted • Instant Delivery", font(22), (210, 200, 220))

    img = img.resize((1600, 900), Image.LANCZOS)

    buf = io.BytesIO()
    buf.name = "receipt.png"
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf

async def send_purchase_receipt(context: ContextTypes.DEFAULT_TYPE, user_id, assigned_item, price):
    """Sends an EXTRA styled receipt if the admin has turned image-mode on.
    In text mode this is a no-op - the normal success message already
    covers it, so nothing duplicate gets sent."""
    if get_receipt_mode() != "image":
        return
    try:
        user = get_user(user_id)
        buyer_label = f"@{user['username']}" if user and user.get('username') and user['username'] != "NoUsername" else f"ID {user_id}"
        buf = generate_receipt_image(assigned_item['item_name'], price, buyer_label, now_ist().strftime("%Y-%m-%d %H:%M"), assigned_item.get('purchase_id'))
        await context.bot.send_photo(user_id, photo=buf, caption="🧾 SR KING GUEST ID STORE BOT — Your Purchase Receipt", protect_content=True)
    except Exception as e:
        print(f"⚠️ Receipt image generation failed: {e}")

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
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT user_id FROM users WHERE banned = 0")
        user_ids = [u[0] for u in c.fetchall()]
        text = (
            f"⚠️ Stock Alert!\n\n"
            f"🎯 {item_name}\n"
            f"Sirf {available} bache hain! Jaldi purchase kar lo warna miss ho jayega. 🔥"
        )
        await send_broadcast_concurrent(context.bot, user_ids, text)
    else:
        if get_setting(flag_key) == "1":
            update_setting(flag_key, "0")

# ==================== USER INFO INLINE ACTIONS ====================

def build_user_actions_keyboard(target_user):
    uid = target_user['user_id']
    ban_btn = (
        InlineKeyboardButton("✅ Unban", callback_data=f"uact_unban_{uid}") if target_user.get('banned')
        else InlineKeyboardButton("🚫 Ban", callback_data=f"uact_ban_{uid}")
    )
    ghost_btn = (
        InlineKeyboardButton("👁 Un-Ghost", callback_data=f"uact_unghost_{uid}") if target_user.get('shadow_banned')
        else InlineKeyboardButton("🕶 Ghost Ban", callback_data=f"uact_ghost_{uid}")
    )
    return InlineKeyboardMarkup([
        [ban_btn, ghost_btn],
        [InlineKeyboardButton("➕ Add Credits", callback_data=f"uact_addcred_{uid}"),
         InlineKeyboardButton("➖ Deduct Credits", callback_data=f"uact_subcred_{uid}")],
        [InlineKeyboardButton("🎁 Gift Bundle", callback_data=f"uact_gift_{uid}")]
    ])

async def user_action_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    admin_id = query.from_user.id
    if admin_id not in ADMIN_IDS:
        await query.answer()
        return

    data = query.data  # uact_<action>_<uid>
    parts = data.split("_")
    sub_action = parts[1]
    target_id = int(parts[2])
    target_user = get_user(target_id)
    if not target_user:
        await query.answer("User not found!", show_alert=True)
        return

    if sub_action == "ban":
        ban_user(target_id)
        log_admin_action(admin_id, "ban_user", str(target_id), "via User Info card")
        await query.answer("🚫 User banned!")
    elif sub_action == "unban":
        unban_user(target_id)
        log_admin_action(admin_id, "unban_user", str(target_id), "via User Info card")
        await query.answer("✅ User unbanned!")
    elif sub_action == "ghost":
        ghost_ban_user(target_id)
        log_admin_action(admin_id, "ghost_ban_user", str(target_id), "via User Info card")
        await query.answer("🕶 Ghost ban applied!")
    elif sub_action == "unghost":
        unban_user(target_id)
        log_admin_action(admin_id, "un_ghost_ban_user", str(target_id), "via User Info card")
        await query.answer("👁 Ghost ban removed!")
    elif sub_action == "addcred":
        context.user_data['admin_action'] = 'quick_add_credits'
        context.user_data['pending_target_id'] = target_id
        await query.answer()
        await query.message.reply_text(f"➕ Kitne credits add karne hain user `{target_id}` ko?", parse_mode=ParseMode.MARKDOWN)
        return
    elif sub_action == "subcred":
        context.user_data['admin_action'] = 'quick_sub_credits'
        context.user_data['pending_target_id'] = target_id
        await query.answer()
        await query.message.reply_text(f"➖ Kitne credits deduct karne hain user `{target_id}` se?", parse_mode=ParseMode.MARKDOWN)
        return
    elif sub_action == "gift":
        await query.answer()
        context.user_data['pending_gift_target_id'] = target_id
        items = get_all_special_items()
        keyboard = []
        for item in items:
            keyboard.append([InlineKeyboardButton(f"{item['item_name']} ({item['available']} left)", callback_data=f"giftitem_{target_id}_{item['item_id']}")])
        await query.message.reply_text(
            f"🎁 User `{target_id}` ko kaunsa item gift karna hai?",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN
        )
        return

    # refresh the card in place for the toggle-style actions
    updated_user = get_user(target_id)
    if updated_user.get('shadow_banned'):
        ban_status = "🕶 Ghost Banned"
    elif updated_user.get('banned'):
        ban_status = "🚫 Banned"
    else:
        ban_status = "✅ Not banned"
    try:
        await query.edit_message_reply_markup(reply_markup=build_user_actions_keyboard(updated_user))
    except Exception:
        pass

async def gift_item_confirm_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """First tap on a gift-item choice: shows a Yes/No confirmation before actually handing it over."""
    query = update.callback_query
    admin_id = query.from_user.id
    if admin_id not in ADMIN_IDS:
        await query.answer()
        return
    await query.answer()
    _, target_id_s, item_id_s = query.data.split("_")
    target_id, item_id = int(target_id_s), int(item_id_s)
    items = get_all_special_items()
    selected = next((i for i in items if i['item_id'] == item_id), None)
    if not selected:
        await query.message.reply_text("⚠️ Item not found / out of stock now.")
        return
    confirm_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Yes, Gift It", callback_data=f"giftconfirm_{target_id}_{item_id}"),
         InlineKeyboardButton("❌ No", callback_data="giftconfirm_cancel")]
    ])
    await query.message.reply_text(
        f"🎁 Confirm: user `{target_id}` ko *{escape_md(selected['item_name'])}* gift karna hai (free, credits nahi katenge)?",
        reply_markup=confirm_kb, parse_mode=ParseMode.MARKDOWN
    )

async def gift_item_finalize(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    admin_id = query.from_user.id
    if admin_id not in ADMIN_IDS:
        await query.answer()
        return
    if query.data == "giftconfirm_cancel":
        await query.answer("Cancelled")
        try:
            await query.edit_message_text("❌ Gift cancelled.")
        except Exception:
            pass
        await restore_admin_keyboard(context.bot, admin_id)
        return
    await query.answer()
    _, target_id_s, item_id_s = query.data.split("_")
    target_id, item_id = int(target_id_s), int(item_id_s)
    assigned = assign_inventory_to_user(target_id, item_id)
    if not assigned:
        try:
            await query.edit_message_text("⚠️ Out of stock now, gift nahi ho paya.")
        except Exception:
            pass
        await restore_admin_keyboard(context.bot, admin_id)
        return
    log_admin_action(admin_id, "gift_bundle", str(target_id), f"Item: {assigned['item_name']}")
    try:
        await query.edit_message_text(f"✅ Gifted *{escape_md(assigned['item_name'])}* to `{target_id}`!", parse_mode=ParseMode.MARKDOWN)
    except Exception:
        pass
    item_json = json.dumps({
        "timestamp": assigned["timestamp"], "guestUid": assigned["guest_uid"],
        "guestPass": assigned["guest_pass"], "item_id": assigned["item_id"], "item_name": assigned["item_name"]
    }, indent=4)
    try:
        await context.bot.send_message(
            target_id,
            f"🎁 *Aapko admin ki taraf se ek gift mila hai!*\n\n"
            f"🎯 Item: {assigned['item_name']}\n\n"
            f"*Your Item Details:*\n```json\n{item_json}\n```",
            parse_mode=ParseMode.MARKDOWN, message_effect_id=EFFECT_PARTY, protect_content=True
        )
    except Exception:
        pass
    await notify_low_stock_if_needed(context, item_id, assigned['item_name'])
    await route_item_purchase_notification(context, item_id, assigned['item_name'], target_id)
    await restore_admin_keyboard(context.bot, admin_id)

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
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    result = c.fetchone()
    if result:
        return {
            "user_id": result[0],
            "username": result[1],
            "first_name": result[2],
            "credits": result[3],
            "referrer_id": result[4],
            "joined_date": result[5],
            "total_referrals": result[6],
            "banned": result[7] if len(result) > 7 else 0,
            "shadow_banned": result[8] if len(result) > 8 else 0,
            "channel_left_notify": result[9] if len(result) > 9 else 0,
        }
    return None

def is_banned(user_id):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT banned FROM users WHERE user_id = ?", (user_id,))
    result = c.fetchone()
    return bool(result and result[0])

def ban_user(user_id):
    conn = get_db()
    c = conn.cursor()
    c.execute("UPDATE users SET banned = 1, shadow_banned = 0 WHERE user_id = ?", (user_id,))
    conn.commit()
def unban_user(user_id):
    conn = get_db()
    c = conn.cursor()
    c.execute("UPDATE users SET banned = 0, shadow_banned = 0 WHERE user_id = ?", (user_id,))
    conn.commit()
def ghost_ban_user(user_id):
    """Bot behaves completely normally for this user - buttons work, menus
    load - but purchases/redeems silently do nothing on the backend. Unlike
    a regular ban, they're never told they've been flagged, so they don't
    immediately go make a new account."""
    conn = get_db()
    c = conn.cursor()
    c.execute("UPDATE users SET shadow_banned = 1, banned = 0 WHERE user_id = ?", (user_id,))
    conn.commit()
def is_shadow_banned(user_id):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT shadow_banned FROM users WHERE user_id = ?", (user_id,))
    r = c.fetchone()
    return bool(r and r[0])
def create_user(user_id, username, first_name, referrer_id=None):
    conn = get_db()
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
    return get_user(user_id)

def add_credits(user_id, amount):
    conn = get_db()
    c = conn.cursor()
    c.execute("UPDATE users SET credits = credits + ? WHERE user_id = ?", (amount, user_id))
    conn.commit()
def deduct_credits(user_id, amount):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT credits FROM users WHERE user_id = ?", (user_id,))
    result = c.fetchone()
    if result and result[0] >= amount:
        c.execute("UPDATE users SET credits = credits - ? WHERE user_id = ?", (amount, user_id))
        conn.commit()
        return True
    return False

def get_inventory_count(item_id):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM inventory WHERE item_id = ? AND sold = 0", (item_id,))
    result = c.fetchone()
    return result[0] if result else 0

def assign_inventory_to_user(user_id, item_id):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT id, item_id, item_name, guest_uid, guest_pass, timestamp FROM inventory WHERE item_id = ? AND sold = 0 LIMIT 1", (item_id,))
    result = c.fetchone()
    if result:
        db_id, inv_item_id, item_name, guest_uid, guest_pass, timestamp = result
        c.execute("UPDATE inventory SET sold = 1 WHERE id = ?", (db_id,))
        purchase_date = now_ist().strftime("%Y-%m-%d %H:%M:%S")
        c.execute("INSERT INTO purchases (user_id, item_id, item_name, guest_uid, guest_pass, purchase_date) VALUES (?, ?, ?, ?, ?, ?)",
                  (user_id, inv_item_id, item_name, guest_uid, guest_pass, purchase_date))
        purchase_id = c.lastrowid
        conn.commit()
        return {
            "guest_uid": guest_uid,
            "guest_pass": guest_pass,
            "item_id": inv_item_id,
            "item_name": item_name,
            "timestamp": timestamp,
            "purchase_id": purchase_id
        }
    return None

def get_item_price(item_id):
    price = get_setting(f"price_{item_id}")
    return int(price) if price else 20

def get_purchase_by_id(purchase_id):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT id, user_id, item_id, item_name, guest_uid, guest_pass, purchase_date FROM purchases WHERE id = ?", (purchase_id,))
    r = c.fetchone()
    if r:
        return {"id": r[0], "user_id": r[1], "item_id": r[2], "item_name": r[3], "guest_uid": r[4], "guest_pass": r[5], "purchase_date": r[6]}
    return None

async def reveal_credentials_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Tap-to-reveal: the credential message starts hidden behind a button so
    it isn't sitting in plain text where anyone glancing at the phone (or a
    screenshot taken before it's needed) can read it."""
    query = update.callback_query
    requester_id = query.from_user.id
    purchase_id = int(query.data.split("_", 1)[1])
    purchase = get_purchase_by_id(purchase_id)
    if not purchase:
        await query.answer("⚠️ Not found!", show_alert=True)
        return
    if purchase["user_id"] != requester_id and requester_id not in ADMIN_IDS:
        await query.answer("⚠️ Ye aapka nahi hai!", show_alert=True)
        return
    await query.answer()
    item_json = json.dumps({
        "timestamp": purchase["purchase_date"], "guestUid": purchase["guest_uid"],
        "guestPass": purchase["guest_pass"], "item_id": purchase["item_id"], "item_name": purchase["item_name"]
    }, indent=4)
    try:
        await query.edit_message_text(
            f"✅ *Purchase Successful!*\n\n"
            f"Item: {purchase['item_name']}\n\n"
            f"*Your Item Details:*\n"
            f"```json\n{item_json}\n```",
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception:
        pass
    await restore_user_keyboard(context.bot, requester_id)

def get_user_purchases(user_id):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT item_id, item_name, guest_uid, guest_pass, purchase_date FROM purchases WHERE user_id = ? ORDER BY purchase_date DESC", (user_id,))
    results = c.fetchall()
    return [{"item_id": r[0], "item_name": r[1], "guest_uid": r[2], "guest_pass": r[3], "purchase_date": r[4]} for r in results]

def get_top_referrers(limit=10):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT user_id, username, first_name, total_referrals FROM users WHERE total_referrals > 0 ORDER BY total_referrals DESC LIMIT ?", (limit,))
    results = c.fetchall()
    return [{"user_id": r[0], "username": r[1], "first_name": r[2], "total_referrals": r[3]} for r in results]

def check_daily_bonus(user_id):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT last_claim_date FROM daily_bonus WHERE user_id = ?", (user_id,))
    result = c.fetchone()
    now = now_ist()
    if result:
        last_claim = datetime.strptime(result[0], "%Y-%m-%d %H:%M:%S")
        if (now - last_claim).total_seconds() < 86400:
            next_claim = last_claim + timedelta(days=1)
            return False, next_claim.strftime("%H:%M:%S")
    bonus = int(get_setting("daily_bonus") or 10)
    c.execute("INSERT OR REPLACE INTO daily_bonus (user_id, last_claim_date) VALUES (?, ?)", (user_id, now.strftime("%Y-%m-%d %H:%M:%S")))
    c.execute("UPDATE users SET credits = credits + ? WHERE user_id = ?", (bonus, user_id))
    conn.commit()
    return True, str(bonus)

def add_inventory_bulk(items):
    conn = get_db()
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
    # Restocked items should be able to alert again next time they run low.
    for item in items:
        iid = item.get("item_id")
        if iid is not None:
            update_setting(f"low_stock_alerted_{iid}", "0")
    return {"added": added, "skipped": skipped, "added_names": added_names}

def get_available_inventory_export():
    """Returns all unsold inventory accounts in the same format used for uploads,
    so the admin can pull back exactly what's left in stock."""
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT item_id, item_name, guest_uid, guest_pass, timestamp FROM inventory WHERE sold = 0")
    results = c.fetchall()
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
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM inventory")
    total = c.fetchone()[0]
    c.execute("DELETE FROM inventory")
    conn.commit()
    return total


# ==================== USERS DATABASE (separate from inventory) ====================
# This is a completely separate backup/restore system from the inventory one
# above. It never touches inventory rows, prices, or other settings - only the
# users table and the per-user tables that hang off it (referrals, purchases,
# daily_bonus, redeem_code_uses).

def get_users_export():
    """Returns every user currently in the database, in a format that can be
    fed straight back into add_users_bulk()."""
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT user_id, username, first_name, credits, referrer_id, joined_date, total_referrals, banned FROM users")
    rows = c.fetchall()
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
    conn = get_db()
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
    return {"added": added, "skipped": skipped}

def reset_users_db():
    """Deletes ALL users and everything keyed by user_id (referrals, purchases,
    daily bonus claims, redeem code redemption history). Does NOT touch
    inventory stock, item prices, referral/bonus settings, redeem code
    definitions, or admin logs."""
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM users")
    total = c.fetchone()[0]
    c.execute("DELETE FROM users")
    c.execute("DELETE FROM referrals")
    c.execute("DELETE FROM purchases")
    c.execute("DELETE FROM daily_bonus")
    c.execute("DELETE FROM redeem_code_uses")
    conn.commit()
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
    result.sort(key=lambda x: x["price"], reverse=True)
    return result

# ==================== ITEM -> CHANNEL ROUTING ====================
# Generalized version of "Dark Desire Bundle purchases get forwarded to
# @SRK_ERA" - admin can map ANY item to ANY channel(s) from the panel,
# add/remove/change freely, without touching code.

def get_item_routes():
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT id, item_id, item_name, channel_id FROM item_channel_routes ORDER BY id")
    return [{"id": r[0], "item_id": r[1], "item_name": r[2], "channel_id": r[3]} for r in c.fetchall()]

def add_item_route(item_id, item_name, channel_id):
    conn = get_db()
    c = conn.cursor()
    c.execute("INSERT INTO item_channel_routes (item_id, item_name, channel_id) VALUES (?, ?, ?)", (item_id, item_name, channel_id))
    conn.commit()

def remove_item_route(route_id):
    conn = get_db()
    c = conn.cursor()
    c.execute("DELETE FROM item_channel_routes WHERE id = ?", (route_id,))
    conn.commit()

async def route_item_purchase_notification(context: ContextTypes.DEFAULT_TYPE, item_id, item_name, buyer_id):
    """Whenever a routed item is bought/gifted/preordered-and-fulfilled,
    forward a clean announcement to whatever channel(s) are mapped to it."""
    routes = [r for r in get_item_routes() if r["item_id"] == item_id]
    if not routes:
        return
    buyer = get_user(buyer_id)
    buyer_label = f"@{buyer['username']}" if buyer and buyer.get('username') and buyer['username'] != "NoUsername" else f"ID {buyer_id}"
    text = (
        f"🔥 *{item_name}* just got purchased!\n"
        f"👤 by {buyer_label}\n\n"
        f"Stock limited hai — jaldi apna le lo! 🚀"
    )
    for route in routes:
        try:
            await context.bot.send_message(route["channel_id"], text, parse_mode=ParseMode.MARKDOWN)
        except Exception as e:
            print(f"⚠️ Failed to route purchase notification to {route['channel_id']}: {e}")

def log_admin_action(admin_id, action, target, details):
    conn = get_db()
    c = conn.cursor()
    c.execute("INSERT INTO admin_logs (admin_id, action, target, details, timestamp) VALUES (?, ?, ?, ?, ?)",
              (admin_id, action, target, details, now_ist().strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()
# ==================== REDEEM CODES ====================

def generate_redeem_code(length=8):
    import random, string
    chars = string.ascii_uppercase + string.digits
    return ''.join(random.choice(chars) for _ in range(length))

def create_redeem_code(credits, expiry_hours=None, max_uses=1, created_by=None, item_id=None, item_name=None, message=None):
    """expiry_hours=None (or 0) means the code never expires.
    If item_id is given, this becomes an item-specific code: redeeming it
    hands over that exact item straight from inventory instead of credits.
    message, if given, is shown to whoever successfully redeems the code."""
    conn = get_db()
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
    c.execute("""INSERT INTO redeem_codes (code, credits, expiry, max_uses, used_count, created_by, created_at, active, item_id, item_name, message)
                 VALUES (?, ?, ?, ?, 0, ?, ?, 1, ?, ?, ?)""",
              (code, credits, expiry, max_uses, created_by, created_at, item_id, item_name, message))
    conn.commit()
    return {"code": code, "credits": credits, "expiry": expiry, "max_uses": max_uses, "item_id": item_id, "item_name": item_name, "message": message}

def get_redeem_code(code):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT code, credits, expiry, max_uses, used_count, created_by, created_at, active, item_id, item_name, message FROM redeem_codes WHERE code = ?", (code,))
    result = c.fetchone()
    if result:
        return {
            "code": result[0], "credits": result[1], "expiry": result[2],
            "max_uses": result[3], "used_count": result[4], "created_by": result[5],
            "created_at": result[6], "active": result[7], "item_id": result[8],
            "item_name": result[9], "message": result[10]
        }
    return None

def has_user_used_code(code, user_id):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT id FROM redeem_code_uses WHERE code = ? AND user_id = ?", (code, user_id))
    result = c.fetchone()
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
        conn = get_db()
        c = conn.cursor()
        c.execute("UPDATE redeem_codes SET used_count = used_count + 1 WHERE code = ?", (code,))
        c.execute("INSERT INTO redeem_code_uses (code, user_id, timestamp) VALUES (?, ?, ?)",
                  (code, user_id, now_ist().strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()
        assigned = assign_inventory_to_user(user_id, redeem["item_id"])
        if not assigned:
            return False, "out_of_stock", None
        assigned["message"] = redeem.get("message")
        return True, "item", assigned

    conn = get_db()
    c = conn.cursor()
    c.execute("UPDATE redeem_codes SET used_count = used_count + 1 WHERE code = ?", (code,))
    c.execute("INSERT INTO redeem_code_uses (code, user_id, timestamp) VALUES (?, ?, ?)",
              (code, user_id, now_ist().strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()
    add_credits(user_id, redeem["credits"])
    return True, "credits", {"amount": redeem["credits"], "message": redeem.get("message")}

def list_recent_codes(limit=10):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT code, credits, expiry, max_uses, used_count, active, item_id, item_name, message FROM redeem_codes ORDER BY created_at DESC LIMIT ?", (limit,))
    results = c.fetchall()
    return [{"code": r[0], "credits": r[1], "expiry": r[2], "max_uses": r[3], "used_count": r[4],
              "active": r[5], "item_id": r[6], "item_name": r[7], "message": r[8]} for r in results]

def get_code_redemptions(code):
    conn = get_db()
    c = conn.cursor()
    c.execute("""SELECT r.user_id, r.timestamp, u.username, u.first_name
                 FROM redeem_code_uses r LEFT JOIN users u ON r.user_id = u.user_id
                 WHERE r.code = ? ORDER BY r.timestamp""", (code,))
    results = c.fetchall()
    return [{"user_id": r[0], "timestamp": r[1], "username": r[2], "first_name": r[3]} for r in results]

def get_user_by_username(username):
    """Case-insensitive lookup, with or without a leading @."""
    username = username.lstrip('@')
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE username = ? COLLATE NOCASE", (username,))
    result = c.fetchone()
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
    
    # Checked for EVERY /start, not just brand-new users - an existing user
    # who left a required channel after signing up used to sail straight
    # through to the main menu here with no re-check at all.
    missing_channels = await get_missing_channels(context.bot, user_id)
    
    if missing_channels:
        # Referrer isn't credited yet (the referral only counts once this user
        # actually joins all channels), but stash it so check_joined() can pick
        # it up later - previously this was lost here and the referral silently
        # never counted for anyone who wasn't already in every channel.
        if not existing_user and referrer_id:
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
        
        reply_markup = build_join_keyboard(missing_channels)
        await update.message.reply_text(
            "⚠️ *Please join these channel(s) first!*\n\n"
            "You're not verified in:\n"
            + "\n".join([f"• {escape_md(ch)}" for ch in missing_channels]),
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN
        , message_effect_id=EFFECT_THUMBS_DOWN)
        return
    
    if not existing_user:
        user_data = create_user(user_id, username, first_name, referrer_id)
        await send_random_welcome_image(context.bot, user_id)
        
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

async def send_broadcast_concurrent(bot, user_ids, message_text):
    """Sends to many users at once instead of one-by-one - bounded by a
    semaphore so it stays under Telegram's ~30 msg/sec limit instead of
    tripping flood control, but is far faster than a strict sequential loop."""
    sem = asyncio.Semaphore(25)
    counts = {"sent": 0, "blocked": 0, "failed": 0}

    async def send_one(uid):
        async with sem:
            try:
                await bot.send_message(uid, f"📢 Announcement\n\n{message_text}", message_effect_id=EFFECT_FIRE)
                counts["sent"] += 1
            except Forbidden:
                counts["blocked"] += 1
            except Exception:
                counts["failed"] += 1

    await asyncio.gather(*(send_one(uid) for uid in user_ids), return_exceptions=True)
    return counts

async def broadcast_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    admin_id = query.from_user.id
    if admin_id not in ADMIN_IDS:
        await query.answer()
        return
    await query.answer()

    if query.data == "broadcast_no":
        context.user_data['pending_broadcast_text'] = None
        try:
            await query.edit_message_text("❌ Broadcast cancelled.")
        except Exception:
            pass
        await restore_admin_keyboard(context.bot, admin_id)
        return

    message_text = context.user_data.pop('pending_broadcast_text', None)
    if not message_text:
        try:
            await query.edit_message_text("⚠️ Broadcast message lost, please try again.")
        except Exception:
            pass
        await restore_admin_keyboard(context.bot, admin_id)
        return

    try:
        await query.edit_message_text("📤 Sending broadcast...")
    except Exception:
        pass

    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT user_id FROM users WHERE banned = 0")
        user_ids = [r[0] for r in c.fetchall()]
        counts = await send_broadcast_concurrent(context.bot, user_ids, message_text)
        log_admin_action(admin_id, "broadcast", "all_users", f"Sent: {counts['sent']}, Blocked: {counts['blocked']}, Failed: {counts['failed']}")
        await context.bot.send_message(
            admin_id,
            f"✅ *Broadcast Completed!*\n\n"
            f"Sent: {counts['sent']} users\n"
            f"🚫 Blocked bot: {counts['blocked']}\n"
            f"⚠️ Other failures: {counts['failed']}",
            parse_mode=ParseMode.MARKDOWN, message_effect_id=EFFECT_PARTY,
            reply_markup=get_admin_keyboard_markup()
        )
    except Exception as e:
        print(f"⚠️ Broadcast failed: {e}")
        try:
            await context.bot.send_message(admin_id, f"⚠️ Broadcast failed: {e}", reply_markup=get_admin_keyboard_markup())
        except Exception:
            pass

async def high_value_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    admin_id = query.from_user.id
    if admin_id not in ADMIN_IDS:
        await query.answer()
        return
    await query.answer()
    if query.data == "highvalue_no":
        context.user_data['pending_confirm_action'] = None
        context.user_data['pending_target_id'] = None
        try:
            await query.edit_message_text("❌ Cancelled.")
        except Exception:
            pass
        await restore_admin_keyboard(context.bot, admin_id)
        return

    action = context.user_data.get('pending_confirm_action')
    amount = context.user_data.get('pending_confirm_amount')
    target_id = context.user_data.get('pending_target_id')
    if not action or not amount or not target_id:
        try:
            await query.edit_message_text("⚠️ Details lost, please try again.")
        except Exception:
            pass
        await restore_admin_keyboard(context.bot, admin_id)
        return

    if action == 'quick_add_credits':
        add_credits(target_id, amount)
        log_admin_action(admin_id, "quick_add_credits", str(target_id), f"+{amount}")
        result_text = f"✅ {amount} credits added to `{target_id}`!"
    else:
        deduct_credits(target_id, amount)
        log_admin_action(admin_id, "quick_sub_credits", str(target_id), f"-{amount}")
        result_text = f"✅ {amount} credits deducted from `{target_id}`!"

    context.user_data['pending_confirm_action'] = None
    context.user_data['pending_confirm_amount'] = None
    context.user_data['pending_target_id'] = None
    try:
        await query.edit_message_text(result_text, parse_mode=ParseMode.MARKDOWN)
    except Exception:
        pass
    await restore_admin_keyboard(context.bot, admin_id)

@maintenance_gate
async def check_joined(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    
    missing_channels = await get_missing_channels(context.bot, user_id)
    all_joined = not missing_channels
    
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
            await send_random_welcome_image(context.bot, user_id)
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
            [KeyboardButton("🛒 Buy Items", style="primary"), KeyboardButton("💰 My Credits", style="primary")],
            [KeyboardButton("🎁 Daily Bonus", style="success"), KeyboardButton("📤 Referral", style="primary")],
            [KeyboardButton("🏆 Leaderboard", style="primary"), KeyboardButton("🎟 Redeem Code", style="success")],
            [KeyboardButton("❓ Help", style="primary")]
        ]
        if user_id in ADMIN_IDS:
            keyboard.append([KeyboardButton("⚙️ Admin Panel", style="primary")])
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
        reply_markup = build_join_keyboard(missing_channels)
        try:
            await query.edit_message_text(
                "⚠️ *Please join these channel(s) first!*\n\n"
                "You're not verified in:\n"
                + "\n".join([f"• {escape_md(ch)}" for ch in missing_channels]),
                reply_markup=reply_markup,
                parse_mode=ParseMode.MARKDOWN
            , message_effect_id=EFFECT_THUMBS_DOWN)
        except Exception:
            pass

def get_user_keyboard_markup(user_id):
    keyboard = [
        [KeyboardButton("🛒 Buy Items", style="primary"), KeyboardButton("💰 My Credits", style="primary")],
        [KeyboardButton("🎁 Daily Bonus", style="success"), KeyboardButton("📤 Referral", style="primary")],
        [KeyboardButton("🏆 Leaderboard", style="primary"), KeyboardButton("🎟 Redeem Code", style="success")],
        [KeyboardButton("📈 Trending", style="primary"), KeyboardButton("📖 How To Use", style="primary")],
        [KeyboardButton("❓ Help", style="primary")]
    ]
    if user_id in ADMIN_IDS:
        keyboard.append([KeyboardButton("⚙️ Admin Panel", style="primary")])
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

async def restore_user_keyboard(bot, chat_id):
    """Same idea as restore_admin_keyboard, but for regular users - used
    after callback-driven flows (tap-to-reveal, preorder confirm) that only
    ever edit an inline keyboard and never touch the bottom one."""
    try:
        await bot.send_message(chat_id, "⌨️", reply_markup=get_user_keyboard_markup(chat_id))
    except Exception:
        pass

async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = get_user(user_id)
    if not user:
        await start(update, context)
        return

    if user.get('channel_left_notify'):
        conn = get_db()
        c = conn.cursor()
        c.execute("UPDATE users SET channel_left_notify = 0 WHERE user_id = ?", (user_id,))
        conn.commit()
        try:
            await update.message.reply_text(
                "⚠️ *Aapne humara channel leave kiya tha, isliye aapke credits reset ho chuke hain.*\n\n"
                "Dobara active hone ke liye channels join karke rakho.",
                parse_mode=ParseMode.MARKDOWN, message_effect_id=EFFECT_THUMBS_DOWN
            )
        except Exception:
            pass
        user = get_user(user_id)
    
    reply_markup = get_user_keyboard_markup(user_id)
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
    elif text == "📈 Trending":
        await show_trending(update, context)
    elif text == "📖 How To Use":
        await show_how_to_use(update, context)
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

async def send_random_welcome_image(bot, user_id):
    """Sent to the NEW user only, once, right at signup - never included in
    the admin's own 'New User Joined' notification, which stays plain text."""
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT file_id FROM welcome_images ORDER BY RANDOM() LIMIT 1")
    row = c.fetchone()
    if not row:
        return
    try:
        await bot.send_photo(
            user_id, photo=row[0],
            caption="🌿 Welcome! Sit back, relax, and enjoy. 🌸"
        )
    except Exception as e:
        print(f"⚠️ Welcome image send failed: {e}")

async def show_trending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = get_db()
    c = conn.cursor()
    c.execute("""SELECT item_id, item_name, COUNT(*) as cnt FROM purchases
                 GROUP BY item_id, item_name ORDER BY cnt DESC LIMIT 15""")
    rows = c.fetchall()
    if not rows:
        await update.message.reply_text("📈 *Abhi tak koi purchase nahi hua hai.*", parse_mode=ParseMode.MARKDOWN, message_effect_id=EFFECT_HEART)
        return
    text = "📈 *Trending Items*\n\n"
    medals = ["🥇", "🥈", "🥉"]
    for i, (item_id, item_name, cnt) in enumerate(rows):
        prefix = medals[i] if i < 3 else f"{i+1}."
        text += f"{prefix} {escape_md(item_name)} — {cnt} sold\n"
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, message_effect_id=EFFECT_FIRE)

async def show_how_to_use(update: Update, context: ContextTypes.DEFAULT_TYPE):
    video_id = get_setting("how_to_use_video_id")
    if not video_id:
        await update.message.reply_text("📖 *How To Use video abhi set nahi hua hai.*", parse_mode=ParseMode.MARKDOWN)
        return
    try:
        await update.message.reply_video(video_id, caption="📖 How To Use", protect_content=False)
    except Exception as e:
        await update.message.reply_text(f"⚠️ Video load nahi ho paya: {e}")

@maintenance_gate
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
            keyboard.append([KeyboardButton(f"{item['item_name']} - {item['price']}💰 ({item['available']} left)||{item['item_id']}", style="primary")])
        else:
            keyboard.append([KeyboardButton(f"🔴 {item['item_name']} - OUT OF STOCK (Preorder)||preorder_{item['item_id']}", style="danger")])
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
async def handle_preorder_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    user_id = update.effective_user.id
    try:
        _, item_id_str = text.split("||preorder_")
        item_id = int(item_id_str)
        items = get_all_special_items()
        selected = next((i for i in items if i['item_id'] == item_id), None)
        if not selected:
            await update.message.reply_text("⚠️ *Item not found!*", parse_mode=ParseMode.MARKDOWN)
            return
        user = get_user(user_id)
        if not user:
            await update.message.reply_text("⚠️ *Pehle /start bhejo!*", parse_mode=ParseMode.MARKDOWN)
            return
        if user['credits'] < selected['price']:
            await update.message.reply_text("⚠️ *Insufficient credits for preorder!*", parse_mode=ParseMode.MARKDOWN, message_effect_id=EFFECT_THUMBS_DOWN)
            return
        confirm_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Yes, Preorder", callback_data=f"preorder_yes_{item_id}"),
             InlineKeyboardButton("❌ No", callback_data="preorder_no")]
        ])
        await update.message.reply_text(
            f"⚠️ *Preorder Confirmation*\n\n"
            f"Item: {selected['item_name']}\n"
            f"Price: {selected['price']}💰\n\n"
            f"Agar aap preorder karte hain to {selected['price']}💰 turant aapke balance se kat jayenge. "
            f"Jaise hi stock aayega, item automatically aapko mil jayega (jo pehle preorder karega usko pehle milega).",
            reply_markup=confirm_kb,
            parse_mode=ParseMode.MARKDOWN
        , message_effect_id=EFFECT_HEART)
    except Exception as e:
        await update.message.reply_text(f"⚠️ *Error: {str(e)}*", parse_mode=ParseMode.MARKDOWN)

async def preorder_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    await query.answer()
    try:
        if query.data == "preorder_no":
            try:
                await query.edit_message_text("❌ Preorder cancelled.")
            except Exception:
                pass
            return
        try:
            item_id = int(query.data.split("_")[-1])
            items = get_all_special_items()
            selected = next((i for i in items if i['item_id'] == item_id), None)
            if not selected:
                await query.edit_message_text("⚠️ Item not found.")
                return
            user = get_user(user_id)
            if not user:
                await query.edit_message_text("⚠️ Pehle /start bhejo.")
                return
            if user['credits'] < selected['price']:
                await query.edit_message_text("⚠️ Insufficient credits.")
                return
            if selected['available'] > 0:
                await query.edit_message_text("✅ Stock aa gaya hai! Seedha 🛒 Buy Items se khareedo.")
                return
            if not deduct_credits(user_id, selected['price']):
                await query.edit_message_text("⚠️ Credit deduction failed.")
                return
            conn = get_db()
            c = conn.cursor()
            c.execute("INSERT INTO preorders (user_id, item_id, item_name, price, timestamp, fulfilled) VALUES (?, ?, ?, ?, ?, 0)",
                      (user_id, item_id, selected['item_name'], selected['price'], now_ist().strftime("%Y-%m-%d %H:%M:%S")))
            conn.commit()
            log_admin_action(user_id, "preorder", str(item_id), f"Price: {selected['price']}")
            await query.edit_message_text(f"✅ *Preorder confirmed!*\n\nItem: {selected['item_name']}\nAapke {selected['price']}💰 kat gaye. Stock aate hi item aapko mil jayega.", parse_mode=ParseMode.MARKDOWN)
        except Exception as e:
            print(f"⚠️ Preorder confirm failed: {e}")
            try:
                await query.edit_message_text(f"⚠️ Error: {e}")
            except Exception:
                pass
    finally:
        await restore_user_keyboard(context.bot, user_id)

async def fulfill_preorders(context: ContextTypes.DEFAULT_TYPE, item_id):
    """FIFO: whenever new stock lands for an item, serve waiting preorders
    first, automatically, before the item is even shown in the public list."""
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT id, user_id, item_name FROM preorders WHERE item_id = ? AND fulfilled = 0 ORDER BY timestamp ASC", (item_id,))
    pending = c.fetchall()
    for pre_id, pre_user_id, item_name in pending:
        if get_inventory_count(item_id) <= 0:
            break
        assigned = assign_inventory_to_user(pre_user_id, item_id)
        if not assigned:
            break
        c.execute("UPDATE preorders SET fulfilled = 1 WHERE id = ?", (pre_id,))
        conn.commit()
        item_json = json.dumps({
            "timestamp": assigned["timestamp"], "guestUid": assigned["guest_uid"],
            "guestPass": assigned["guest_pass"], "item_id": assigned["item_id"], "item_name": assigned["item_name"]
        }, indent=4)
        try:
            await context.bot.send_message(
                pre_user_id,
                f"🎉 *Aapka preorder fulfil ho gaya!*\n\n"
                f"🎯 Item: {item_name}\n\n"
                f"*Your Item Details:*\n```json\n{item_json}\n```",
                parse_mode=ParseMode.MARKDOWN, message_effect_id=EFFECT_PARTY, protect_content=True
            )
        except Exception:
            pass
        await route_item_purchase_notification(context, item_id, item_name, pre_user_id)

async def remove_item_route_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    admin_id = query.from_user.id
    if admin_id not in ADMIN_IDS:
        await query.answer()
        return
    route_id = int(query.data.split("_")[1])
    remove_item_route(route_id)
    log_admin_action(admin_id, "remove_item_route", str(route_id), "")
    await query.answer("✅ Removed!")
    try:
        await query.edit_message_text("✅ Route removed.")
    except Exception:
        pass
    await restore_admin_keyboard(context.bot, admin_id)

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
        if not user:
            await update.message.reply_text("⚠️ *Pehle /start bhejo!*", parse_mode=ParseMode.MARKDOWN)
            return
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
            [KeyboardButton(f"✅ Confirm||{item_id}", style="success")],
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

    # --- Anti-spam cooldown: block rapid double-taps of Confirm ---
    now_ts = asyncio.get_event_loop().time()
    last_ts = context.bot_data.setdefault('purchase_cooldown', {}).get(user_id, 0)
    if now_ts - last_ts < 4:
        await update.message.reply_text("⏳ *Thoda ruko, pichla purchase abhi process ho raha hai.*", parse_mode=ParseMode.MARKDOWN)
        return
    context.bot_data['purchase_cooldown'][user_id] = now_ts

    try:
        _, item_id_str = text.split("||")
        item_id = int(item_id_str)

        # --- Re-verify channel membership right at the moment of purchase ---
        missing_channels = await get_missing_channels(context.bot, user_id)
        if missing_channels:
            reply_markup = build_join_keyboard(missing_channels)
            await update.message.reply_text(
                "⚠️ *Aap in channel(s) mein nahi ho!*\n\n"
                "Purchase karne ke liye pehle inhe join karo:\n"
                + "\n".join([f"• {escape_md(ch)}" for ch in missing_channels]),
                reply_markup=reply_markup,
                parse_mode=ParseMode.MARKDOWN
            , message_effect_id=EFFECT_THUMBS_DOWN)
            await show_main_menu(update, context)
            return

        available = get_inventory_count(item_id)
        if available <= 0:
            await update.message.reply_text("⚠️ *Item out of stock!*", parse_mode=ParseMode.MARKDOWN)
            await show_buy_items(update, context)
            return
        
        user = get_user(user_id)
        if not user:
            await update.message.reply_text("⚠️ *Pehle /start bhejo!*", parse_mode=ParseMode.MARKDOWN)
            return
        price = get_item_price(item_id)
        if user["credits"] < price:
            await update.message.reply_text("⚠️ *Insufficient credits!*", parse_mode=ParseMode.MARKDOWN, message_effect_id=EFFECT_THUMBS_DOWN)
            await show_buy_items(update, context)
            return

        # --- Shadow/ghost-banned users: everything LOOKS normal, but nothing
        # actually happens on the backend. No credits move, no stock moves. ---
        if user.get('shadow_banned'):
            await update.message.reply_text("⚠️ *Server thoda busy hai, thodi der baad try karo.*", parse_mode=ParseMode.MARKDOWN)
            await show_main_menu(update, context)
            return
        
        if deduct_credits(user_id, price):
            assigned_item = assign_inventory_to_user(user_id, item_id)
            if assigned_item:
                keyboard = [
                    [KeyboardButton("🛒 Buy Items", style="primary")],
                    [KeyboardButton("🔙 Back")]
                ]
                reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
                reveal_kb = InlineKeyboardMarkup([[InlineKeyboardButton("👁 Tap to View Credentials", callback_data=f"reveal_{assigned_item['purchase_id']}")]])
                await update.message.reply_text(
                    f"✅ *Purchase Successful!*\n\n"
                    f"Item: {assigned_item['item_name']}\n"
                    f"Price: {price}💰\n"
                    f"Remaining Credits: {user['credits'] - price}💰\n\n"
                    f"👇 Tap the button below to reveal your account details.",
                    reply_markup=reply_markup,
                    parse_mode=ParseMode.MARKDOWN
                , message_effect_id=EFFECT_PARTY)
                await update.message.reply_text(
                    "🔒 Protected delivery:", reply_markup=reveal_kb, protect_content=True
                )
                await send_purchase_receipt(context, user_id, assigned_item, price)
                
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
                await route_item_purchase_notification(context, item_id, assigned_item['item_name'], user_id)
            else:
                add_credits(user_id, price)  # refund - stock vanished between the check and the assignment
                await update.message.reply_text("⚠️ *Error assigning item! Credits refunded, please contact admin.*", parse_mode=ParseMode.MARKDOWN)
        else:
            await update.message.reply_text("⚠️ *Error processing purchase!*", parse_mode=ParseMode.MARKDOWN)
        
        await show_main_menu(update, context)
    except Exception as e:
        print(f"⚠️ Purchase error: {e}")
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
            f"💰 Credits Added: {payload['amount']}\n"
            f"💰 New Balance: {user['credits']}💰",
            parse_mode=ParseMode.MARKDOWN
        , message_effect_id=EFFECT_PARTY)
        if payload.get("message"):
            await message.reply_text(f"📩 *Message from Admin:*\n\n{escape_md(payload['message'])}", parse_mode=ParseMode.MARKDOWN)
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
        if payload.get("message"):
            await message.reply_text(f"📩 *Message from Admin:*\n\n{escape_md(payload['message'])}", parse_mode=ParseMode.MARKDOWN)
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
        f"📢 *Required Channels:*\n" + "\n".join([f"• {escape_md(ch)}" for ch in get_required_channels()]) + "\n\n"
        f"💡 *Tip:* More referrals = More credits = More items!",
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN
    , message_effect_id=EFFECT_HEART)

# ==================== ADMIN PANEL ====================

def get_admin_keyboard_markup():
    keyboard = [
        [KeyboardButton("💰 Edit Prices", style="primary"), KeyboardButton("🔄 Edit Points", style="primary")],
        [KeyboardButton("🎁 Edit Bonus", style="primary"), KeyboardButton("🎁 Gift Credits", style="success")],
        [KeyboardButton("🔻 Deduct Credits", style="danger"), KeyboardButton("👤 User Info", style="primary")],
        [KeyboardButton("📦 Upload Inventory", style="primary"), KeyboardButton("🗄 Users Database", style="primary")],
        [KeyboardButton("📢 Broadcast", style="primary"), KeyboardButton("📨 Message User", style="primary")],
        [KeyboardButton("🚫 Ban User", style="danger"), KeyboardButton("✅ Unban User", style="success")],
        [KeyboardButton("🎟 Create Code", style="success"), KeyboardButton("🎁 Item Code", style="success")],
        [KeyboardButton("📋 List Codes", style="primary"), KeyboardButton("👥 Code Users", style="primary")],
        [KeyboardButton("🔌 Toggle Bot Status", style="danger"), KeyboardButton("📊 Stats", style="primary")],
        [KeyboardButton("🗄 Database Channel", style="primary"), KeyboardButton("📥 Restore Database", style="danger")],
        [KeyboardButton("📦 Stock", style="primary"), KeyboardButton("📢 Channels", style="primary")],
        [KeyboardButton("📊 Leaderboard", style="primary"), KeyboardButton("🔀 Item Routes", style="primary")],
        [KeyboardButton("🖼 Welcome Images", style="primary"), KeyboardButton("📖 How To Use Video", style="primary")],
        [KeyboardButton("🧾 Receipt Mode", style="primary")],
        [KeyboardButton("🔙 Back")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

async def restore_admin_keyboard(bot, chat_id):
    """Callback-driven admin flows (edit_message_text on an inline keyboard)
    never touch the persistent bottom keyboard - on some clients that leaves
    it collapsed/hidden until something explicitly resends it. Call this
    after any such flow finishes so the admin panel keyboard reliably comes
    back without needing /start."""
    try:
        await bot.send_message(chat_id, "⌨️", reply_markup=get_admin_keyboard_markup())
    except Exception:
        pass

async def show_admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("⚠️ *Unauthorized!*", parse_mode=ParseMode.MARKDOWN)
        return
    
    reply_markup = get_admin_keyboard_markup()
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
    elif text == "📦 Stock":
        await show_stock_summary(update, context)
    elif text == "📢 Channels":
        await show_channels_management(update, context)
    elif text == "📊 Leaderboard":
        await show_leaderboard_menu(update, context)
    elif text == "🔀 Item Routes":
        await show_item_routes(update, context)
    elif text == "🖼 Welcome Images":
        context.user_data['admin_action'] = 'awaiting_welcome_image'
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM welcome_images")
        count = c.fetchone()[0]
        await update.message.reply_text(
            f"🖼 *Welcome Images*\n\n"
            f"Abhi pool mein: {count} images\n\n"
            f"Naya image bhejo (jitna chaho utna bhej sakte ho, ek ek karke) - naye users ko in mein se random ek milega.\n"
            f"`clear` bhejo pool khali karne ke liye.\n"
            f"Type 'cancel' jab ho jaye.",
            parse_mode=ParseMode.MARKDOWN
        , message_effect_id=EFFECT_HEART)
    elif text == "📖 How To Use Video":
        context.user_data['admin_action'] = 'set_how_to_use_video'
        await update.message.reply_text(
            f"📖 *How To Use Video*\n\n"
            f"Video bhejo - users jab '📖 How To Use' button dabayenge unhe yahi video milega.\n"
            f"Type 'cancel' to cancel.",
            parse_mode=ParseMode.MARKDOWN
        , message_effect_id=EFFECT_HEART)
    elif text == "🧾 Receipt Mode":
        current = get_receipt_mode()
        keyboard = [
            [KeyboardButton("📝 Text Mode", style="primary" if current != "image" else None), KeyboardButton("🖼 Image Mode", style="primary" if current == "image" else None)],
            [KeyboardButton("🔙 Back")]
        ]
        await update.message.reply_text(
            f"🧾 *Receipt Mode*\n\nAbhi: *{current.upper()}*\n\nChuno:",
            reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True),
            parse_mode=ParseMode.MARKDOWN
        , message_effect_id=EFFECT_HEART)
    elif text == "📝 Text Mode":
        update_setting("receipt_mode", "text")
        await update.message.reply_text("✅ Receipt mode: TEXT", message_effect_id=EFFECT_PARTY)
        await show_admin_panel(update, context)
    elif text == "🖼 Image Mode":
        update_setting("receipt_mode", "image")
        await update.message.reply_text("✅ Receipt mode: IMAGE", message_effect_id=EFFECT_PARTY)
        await show_admin_panel(update, context)

async def toggle_bot_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin_id = update.effective_user.id
    new_state = not get_bot_enabled()
    set_bot_enabled(new_state)
    log_admin_action(admin_id, "toggle_bot", "all_users", f"Bot enabled: {new_state}")
    
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT user_id FROM users")
    all_users = c.fetchall()
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
        keyboard.append([KeyboardButton(f"{item['item_name']} - {item['price']}💰||price_{item['item_id']}", style="primary")])
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

async def show_stock_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = get_db()
    c = conn.cursor()
    c.execute("""SELECT item_id, item_name,
                        COUNT(*) AS total,
                        SUM(CASE WHEN sold = 0 THEN 1 ELSE 0 END) AS available
                 FROM inventory
                 GROUP BY item_id, item_name
                 ORDER BY item_id""")
    rows = c.fetchall()
    keyboard = [[KeyboardButton("🔙 Back")]]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

    if not rows:
        await update.message.reply_text("📦 *Inventory is empty!*", reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN, message_effect_id=EFFECT_HEART)
        return

    total_all = sum(r[2] for r in rows)
    available_all = sum(r[3] for r in rows)
    sold_all = total_all - available_all

    text = f"📦 *Stock Summary*\n\n"
    for item_id, item_name, total, available in rows:
        sold = total - available
        flag = "⚠️" if available < 5 else "✅"
        text += (
            f"{flag} *{escape_md(item_name)}* (ID: `{item_id}`)\n"
            f"  Available: {available} | Sold: {sold} | Total: {total}\n\n"
        )
    text += f"— — —\n📊 Overall: {available_all} available | {sold_all} sold | {total_all} total"

    await update.message.reply_text(text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN, message_effect_id=EFFECT_HEART)

# ==================== CHANNEL MANAGEMENT ====================

async def show_channels_management(update: Update, context: ContextTypes.DEFAULT_TYPE):
    channels = get_required_channels()
    text = "📢 *Required Channels (Force Subscribe)*\n\n"
    if channels:
        for ch in channels:
            text += f"• {escape_md(ch)}\n"
    else:
        text += "_Koi channel set nahi hai._\n"
    text += "\nNaya channel add karne ke liye @username bhejo (bot us channel mein *admin* hona chahiye).\nHatane ke liye niche button dabao."
    keyboard = []
    for ch in channels:
        keyboard.append([InlineKeyboardButton(f"❌ Remove {ch}", callback_data=f"rmchannel_{ch}")])
    if keyboard:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN, message_effect_id=EFFECT_HEART)
    else:
        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, message_effect_id=EFFECT_HEART)
    context.user_data['admin_action'] = 'add_channel'

async def remove_channel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    admin_id = query.from_user.id
    if admin_id not in ADMIN_IDS:
        await query.answer()
        return
    channel = query.data.split("_", 1)[1]
    channels = get_required_channels()
    if channel in channels:
        channels.remove(channel)
        save_required_channels(channels)
        log_admin_action(admin_id, "remove_channel", channel, "")
    await query.answer(f"✅ {channel} removed!")
    try:
        await query.edit_message_text(f"✅ {channel} removed from required channels.")
    except Exception:
        pass
    await restore_admin_keyboard(context.bot, admin_id)

# ==================== ADMIN LEADERBOARD ====================

async def show_leaderboard_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🤝 Top Referrals", callback_data="lb_referrals")],
        [InlineKeyboardButton("🛒 Top Buyers", callback_data="lb_buyers")],
        [InlineKeyboardButton("💰 Top Credits", callback_data="lb_credits")],
    ])
    await update.message.reply_text("📊 *Leaderboard* — kaunsa dekhna hai?", reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN, message_effect_id=EFFECT_HEART)

async def leaderboard_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    admin_id = query.from_user.id
    if admin_id not in ADMIN_IDS:
        await query.answer()
        return
    await query.answer()
    kind = query.data.split("_", 1)[1]
    conn = get_db()
    c = conn.cursor()

    if kind == "referrals":
        # total_referrals is only ever incremented once a referred user has
        # actually completed the channel-join verification (see create_user
        # call sites) - so this is already "verified referrals only".
        c.execute("SELECT user_id, username, first_name, total_referrals FROM users WHERE total_referrals > 0 ORDER BY total_referrals DESC LIMIT 15")
        rows = c.fetchall()
        title = "🤝 Top Referrals (verified joins only)"
        lines = [f"{i+1}. {escape_md(r[2] or r[1] or str(r[0]))} — {r[3]} referrals" for i, r in enumerate(rows)]
    elif kind == "buyers":
        c.execute("""SELECT u.user_id, u.username, u.first_name, COUNT(p.id) as cnt
                     FROM purchases p JOIN users u ON p.user_id = u.user_id
                     GROUP BY p.user_id ORDER BY cnt DESC LIMIT 15""")
        rows = c.fetchall()
        title = "🛒 Top Buyers"
        lines = [f"{i+1}. {escape_md(r[2] or r[1] or str(r[0]))} — {r[3]} purchases" for i, r in enumerate(rows)]
    else:
        c.execute("SELECT user_id, username, first_name, credits FROM users ORDER BY credits DESC LIMIT 15")
        rows = c.fetchall()
        title = "💰 Top Credits"
        lines = [f"{i+1}. {escape_md(r[2] or r[1] or str(r[0]))} — {r[3]}💰" for i, r in enumerate(rows)]

    text = f"*{title}*\n\n" + ("\n".join(lines) if lines else "_Abhi tak koi data nahi hai._")
    try:
        await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN)
    except Exception:
        await query.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
    await restore_admin_keyboard(context.bot, admin_id)

# ==================== ITEM ROUTES MANAGEMENT ====================

async def show_item_routes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    routes = get_item_routes()
    text = "🔀 *Item → Channel Routes*\n\nJab bhi ye item bikta/gift/preorder-fulfil hota hai, announcement us channel mein jayega.\n\n"
    keyboard = []
    if routes:
        for r in routes:
            text += f"• {escape_md(r['item_name'])} → {escape_md(r['channel_id'])}\n"
            keyboard.append([InlineKeyboardButton(f"❌ Remove: {r['item_name']} → {r['channel_id']}", callback_data=f"rmroute_{r['id']}")])
    else:
        text += "_Koi route set nahi hai._\n"
    text += "\nNaya route add karne ke liye bhejo: `item_id @channel`"
    context.user_data['admin_action'] = 'add_item_route'
    if keyboard:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN, message_effect_id=EFFECT_HEART)
    else:
        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, message_effect_id=EFFECT_HEART)

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
        msg_line = f"  💬 {escape_md(cd['message'])}\n" if cd.get("message") else ""
        text += (
            f"`{cd['code']}` — {reward}\n"
            f"  Uses: {cd['used_count']}/{cd['max_uses']} | Expiry: {expiry_text} | {status}\n"
            f"{msg_line}\n"
        )
    
    await update.message.reply_text(text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN, message_effect_id=EFFECT_HEART)

async def show_item_code_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    items = get_all_special_items()
    keyboard = []
    for item in items:
        keyboard.append([KeyboardButton(f"{item['item_name']} ({item['available']} left)||itemcode_{item['item_id']}", style="primary")])
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
    conn = get_db()
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
                f"💰 Credits Added: {payload['amount']}\n"
                f"💰 New Balance: {user['credits']}💰",
                parse_mode=ParseMode.MARKDOWN
            , message_effect_id=EFFECT_PARTY)
            if payload.get("message"):
                await update.message.reply_text(f"📩 *Message from Admin:*\n\n{escape_md(payload['message'])}", parse_mode=ParseMode.MARKDOWN)
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
            if payload.get("message"):
                await update.message.reply_text(f"📩 *Message from Admin:*\n\n{escape_md(payload['message'])}", parse_mode=ParseMode.MARKDOWN)
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
            
            context.user_data['pending_code_credits'] = credits_val
            context.user_data['pending_code_expiry_hours'] = expiry_hours
            context.user_data['pending_code_max_uses'] = max_uses
            context.user_data['admin_action'] = 'create_code_message'
            await update.message.reply_text(
                f"💬 *Custom Message (optional)*\n\n"
                f"Koi message set karna hai jo redeem karne wale ko dikhega? Bhej do.\n"
                f"Nahi chahiye to `skip` bhejo.",
                parse_mode=ParseMode.MARKDOWN
            , message_effect_id=EFFECT_HEART)

        elif action == 'create_code_message':
            custom_message = None if text.strip().lower() in ('skip', '-') else text.strip()
            credits_val = context.user_data.get('pending_code_credits')
            expiry_hours = context.user_data.get('pending_code_expiry_hours')
            max_uses = context.user_data.get('pending_code_max_uses')

            redeem = create_redeem_code(credits_val, expiry_hours if expiry_hours > 0 else None, max_uses, created_by=user_id, message=custom_message)
            log_admin_action(user_id, "create_code", redeem["code"], f"Credits: {credits_val}, Expiry hrs: {expiry_hours}, Max uses: {max_uses}")
            
            expiry_text = redeem["expiry"] if redeem["expiry"] else "Never"
            bot_username = context.bot.username
            redeem_keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("🎁 Redeem Now", url=f"https://t.me/{bot_username}?start=redeem_{redeem['code']}", style="success")]
            ])
            has_msg_note = " (with a custom message set)" if custom_message else ""
            await update.message.reply_text(
                f"✅ *Redeem Code Created!*\n\n"
                f"🎟 Code: `{redeem['code']}`\n"
                f"💰 Credits: {credits_val}\n"
                f"⏰ Expiry: {expiry_text}\n"
                f"👥 Max Uses: {max_uses}{has_msg_note}\n\n"
                f"👉 Tapping *Redeem Now* below auto-redeems this exact code instantly.",
                reply_markup=redeem_keyboard,
                parse_mode=ParseMode.MARKDOWN
            , message_effect_id=EFFECT_PARTY)
            context.user_data['admin_action'] = None
            context.user_data['pending_code_credits'] = None
            context.user_data['pending_code_expiry_hours'] = None
            context.user_data['pending_code_max_uses'] = None
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
            
            if target_user.get('shadow_banned'):
                ban_status = "🕶 Ghost Banned"
            elif target_user.get('banned'):
                ban_status = "🚫 Banned"
            else:
                ban_status = "✅ Not banned"
            action_keyboard = build_user_actions_keyboard(target_user)
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
                reply_markup=action_keyboard,
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
            context.user_data['pending_broadcast_text'] = text
            context.user_data['admin_action'] = None
            confirm_keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Yes, Send", callback_data="broadcast_yes"),
                 InlineKeyboardButton("❌ No, Cancel", callback_data="broadcast_no")]
            ])
            await update.message.reply_text(
                f"📢 Preview:\n\n{text}\n\n"
                f"⚠️ Ye sabhi users ko jayega. Confirm karo:",
                reply_markup=confirm_keyboard
            , message_effect_id=EFFECT_HEART)

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

            context.user_data['pending_item_code_expiry_hours'] = expiry_hours
            context.user_data['pending_item_code_max_uses'] = max_uses
            context.user_data['admin_action'] = 'create_item_code_message'
            await update.message.reply_text(
                f"💬 *Custom Message (optional)*\n\n"
                f"Koi message set karna hai jo redeem karne wale ko dikhega? Bhej do.\n"
                f"Nahi chahiye to `skip` bhejo.",
                parse_mode=ParseMode.MARKDOWN
            , message_effect_id=EFFECT_HEART)

        elif action == 'create_item_code_message':
            item_id = context.user_data.get('pending_item_code_id')
            item_name = context.user_data.get('pending_item_code_name')
            expiry_hours = context.user_data.get('pending_item_code_expiry_hours')
            max_uses = context.user_data.get('pending_item_code_max_uses')
            if not item_id:
                await update.message.reply_text("⚠️ *Item lost, please start again.*", parse_mode=ParseMode.MARKDOWN)
                context.user_data['admin_action'] = None
                await show_admin_panel(update, context)
                return
            custom_message = None if text.strip().lower() in ('skip', '-') else text.strip()

            redeem = create_redeem_code(0, expiry_hours if expiry_hours > 0 else None, max_uses, created_by=user_id, item_id=item_id, item_name=item_name, message=custom_message)
            log_admin_action(user_id, "create_item_code", redeem["code"], f"Item: {item_name}, Expiry hrs: {expiry_hours}, Max uses: {max_uses}")

            expiry_text = redeem["expiry"] if redeem["expiry"] else "Never"
            bot_username = context.bot.username
            redeem_keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("🎁 Redeem Now", url=f"https://t.me/{bot_username}?start=redeem_{redeem['code']}", style="success")]
            ])
            has_msg_note = " (with a custom message set)" if custom_message else ""
            await update.message.reply_text(
                f"✅ *Item Redeem Code Created!*\n\n"
                f"🎟 Code: `{redeem['code']}`\n"
                f"🎁 Item: {item_name}\n"
                f"⏰ Expiry: {expiry_text}\n"
                f"👥 Max Uses: {max_uses}{has_msg_note}\n\n"
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

        elif action in ('quick_add_credits', 'quick_sub_credits'):
            target_id = context.user_data.get('pending_target_id')
            amount = int(text.strip())
            if amount <= 0 or not target_id:
                await update.message.reply_text("⚠️ *Invalid amount!*", parse_mode=ParseMode.MARKDOWN)
                return
            if amount >= 500:
                context.user_data['pending_confirm_action'] = action
                context.user_data['pending_confirm_amount'] = amount
                confirm_kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ Yes, Confirm", callback_data="highvalue_yes"),
                     InlineKeyboardButton("❌ No", callback_data="highvalue_no")]
                ])
                verb = "add" if action == 'quick_add_credits' else "deduct"
                await update.message.reply_text(f"⚠️ Ye bada adjustment hai — {amount} credits {verb} karna hai user `{target_id}` ke liye?", reply_markup=confirm_kb, parse_mode=ParseMode.MARKDOWN)
                return
            if action == 'quick_add_credits':
                add_credits(target_id, amount)
                log_admin_action(user_id, "quick_add_credits", str(target_id), f"+{amount}")
                await update.message.reply_text(f"✅ {amount} credits add ho gaye!", message_effect_id=EFFECT_PARTY)
            else:
                deduct_credits(target_id, amount)
                log_admin_action(user_id, "quick_sub_credits", str(target_id), f"-{amount}")
                await update.message.reply_text(f"✅ {amount} credits deduct ho gaye!", message_effect_id=EFFECT_PARTY)
            context.user_data['admin_action'] = None
            context.user_data['pending_target_id'] = None

        elif action == 'add_item_route':
            parts = text.split()
            if len(parts) != 2:
                await update.message.reply_text("⚠️ *Format:* `item_id @channel`", parse_mode=ParseMode.MARKDOWN, message_effect_id=EFFECT_THUMBS_DOWN)
                return
            item_id = int(parts[0])
            channel_id = parts[1]
            items = get_all_special_items()
            selected = next((i for i in items if i['item_id'] == item_id), None)
            item_name = selected['item_name'] if selected else f"Item {item_id}"
            add_item_route(item_id, item_name, channel_id)
            log_admin_action(user_id, "add_item_route", str(item_id), f"-> {channel_id}")
            await update.message.reply_text(f"✅ *Route Added!*\n\n{item_name} → {channel_id}", parse_mode=ParseMode.MARKDOWN, message_effect_id=EFFECT_PARTY)
            context.user_data['admin_action'] = None
            await show_admin_panel(update, context)

        elif action == 'set_how_to_use_video':
            await update.message.reply_text("⚠️ *Video bhejo, text nahi!*", parse_mode=ParseMode.MARKDOWN)
            return

        elif action == 'awaiting_welcome_image':
            if text.strip().lower() == 'clear':
                conn = get_db()
                c = conn.cursor()
                c.execute("DELETE FROM welcome_images")
                conn.commit()
                log_admin_action(user_id, "clear_welcome_images", "all", "")
                await update.message.reply_text("✅ *Welcome images pool cleared!*", parse_mode=ParseMode.MARKDOWN, message_effect_id=EFFECT_PARTY)
            else:
                await update.message.reply_text("⚠️ *Sirf image bhejo, ya `clear`/`cancel` type karo.*", parse_mode=ParseMode.MARKDOWN)
                return
            context.user_data['admin_action'] = None
            await show_admin_panel(update, context)

        elif action == 'add_channel':
            channel_input = text.strip()
            if channel_input.lower() == 'skip':
                context.user_data['admin_action'] = None
                await show_admin_panel(update, context)
                return
            probe_admin = user_id
            ok, err = await verify_bot_can_check_channel(context.bot, channel_input, probe_admin)
            if not ok:
                await update.message.reply_text(
                    f"⚠️ *Bot is channel ki membership check nahi kar paya!*\n\n"
                    f"Error: `{escape_md(str(err))}`\n\n"
                    f"Pehle bot ko `{escape_md(channel_input)}` mein **admin** banao, phir dobara try karo.\n"
                    f"Ye channel *add nahi* kiya gaya, taaki verification kisi ke liye bhi kabhi na atke.",
                    parse_mode=ParseMode.MARKDOWN
                , message_effect_id=EFFECT_THUMBS_DOWN)
                return
            channels = get_required_channels()
            if channel_input not in channels:
                channels.append(channel_input)
                save_required_channels(channels)
                log_admin_action(user_id, "add_channel", channel_input, "")
            await update.message.reply_text(
                f"✅ *Channel added and verified working!*\n\n`{escape_md(channel_input)}`\n\n"
                f"Naya channel add karna hai to aur bhejo, ya `skip` bhejo khatam karne ke liye.",
                parse_mode=ParseMode.MARKDOWN
            , message_effect_id=EFFECT_PARTY)
            return


    except ValueError:
        await update.message.reply_text("⚠️ *Please enter a valid number!*", parse_mode=ParseMode.MARKDOWN, message_effect_id=EFFECT_THUMBS_DOWN)

async def show_upload_inventory_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['admin_action'] = None
    remaining = sum(item["available"] for item in get_all_special_items())
    keyboard = [
        [KeyboardButton("📥 Send New File", style="primary")],
        [KeyboardButton("📤 Export Remaining Accounts", style="primary")],
        [KeyboardButton("🗑 Reset Inventory", style="danger")],
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
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM users")
    total_users = c.fetchone()[0]
    keyboard = [
        [KeyboardButton("📥 Upload Users DB", style="primary")],
        [KeyboardButton("📤 Export Users DB", style="primary")],
        [KeyboardButton("🗑 Reset Users DB", style="danger")],
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
            [KeyboardButton("✅ Yes, Reset Users DB", style="danger")],
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
            [KeyboardButton("✅ Yes, Reset Inventory", style="danger")],
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

async def handle_welcome_image_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS or context.user_data.get('admin_action') != 'awaiting_welcome_image':
        return
    photo = update.message.photo[-1]  # largest size
    conn = get_db()
    c = conn.cursor()
    c.execute("INSERT INTO welcome_images (file_id, added_at) VALUES (?, ?)", (photo.file_id, now_ist().strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()
    c.execute("SELECT COUNT(*) FROM welcome_images")
    count = c.fetchone()[0]
    await update.message.reply_text(f"✅ Image added! Pool mein ab {count} images hain. Aur bhejo ya `cancel`/`skip` bhejo.", message_effect_id=EFFECT_PARTY)

async def handle_how_to_use_video_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS or context.user_data.get('admin_action') != 'set_how_to_use_video':
        return
    video = update.message.video
    if not video:
        return
    update_setting("how_to_use_video_id", video.file_id)
    log_admin_action(user_id, "set_how_to_use_video", "video", "")
    await update.message.reply_text("✅ *How To Use video set ho gaya!*", parse_mode=ParseMode.MARKDOWN, message_effect_id=EFFECT_PARTY)
    context.user_data['admin_action'] = None
    await show_admin_panel(update, context)

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
        reset_db_connection()  # drop the old connection BEFORE swapping the file - it's holding a handle to the old (about-to-be-replaced) inode
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

        # Auto-fulfill anyone waiting on a preorder for these items, FIFO,
        # before anything else touches the fresh stock.
        for item_id in item_counts_by_id(filtered_items):
            await fulfill_preorders(context, item_id)

        # Forward-ready announcement, ready to paste straight into a channel.
        item_names_line = "\n".join(f"🎯 {name} — {count} available" for name, count in item_counts.items())
        announcement = (
            f"🔥 *STOCK ADDED!* 🔥\n\n"
            f"{item_names_line}\n\n"
            f"⚡ Jaldi jaake bot mein khareed lo, stock limited hai!"
        )
        await update.message.reply_text(
            f"📣 *Forward-ready announcement* (copy karke channel mein daal do):\n\n{announcement}",
            parse_mode=ParseMode.MARKDOWN
        )

        context.user_data['admin_action'] = None
        await show_admin_panel(update, context)
        
    except json.JSONDecodeError:
        await update.message.reply_text("⚠️ *Invalid JSON file!*", parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        await update.message.reply_text(f"⚠️ *Error: {str(e)}*", parse_mode=ParseMode.MARKDOWN)

def item_counts_by_id(items):
    seen = []
    for item in items:
        iid = item.get("item_id")
        if iid is not None and iid not in seen:
            seen.append(iid)
    return seen

# ==================== MAIN ====================

async def track_channel_leave(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Real-time: fires the moment someone leaves/gets kicked from any
    required channel (bot must be admin there to receive this). Zeroes
    their credits immediately and flags them so the next time they open the
    bot they're told why."""
    try:
        result = update.chat_member
        if not result:
            return
        channel_username = f"@{result.chat.username}" if result.chat.username else str(result.chat.id)
        required = get_required_channels()
        if channel_username not in required and str(result.chat.id) not in required:
            return

        old_status = result.old_chat_member.status if result.old_chat_member else None
        new_status = result.new_chat_member.status if result.new_chat_member else None
        left_now = old_status in ("member", "administrator", "creator", "restricted") and new_status in ("left", "kicked")
        if not left_now:
            return

        if not result.new_chat_member or not result.new_chat_member.user:
            return
        target_user_id = result.new_chat_member.user.id
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT credits FROM users WHERE user_id = ?", (target_user_id,))
        row = c.fetchone()
        if not row:
            return
        if row[0] > 0:
            c.execute("UPDATE users SET credits = 0, channel_left_notify = 1 WHERE user_id = ?", (target_user_id,))
        else:
            c.execute("UPDATE users SET channel_left_notify = 1 WHERE user_id = ?", (target_user_id,))
        conn.commit()
        print(f"ℹ️ User {target_user_id} left {channel_username} — credits zeroed.")
    except Exception as e:
        print(f"⚠️ track_channel_leave failed (non-fatal, ignored): {e}")

_error_notify_state = {"count": 0, "last_notify_ts": 0}

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Global safety net: if any handler above throws, this catches it so one
    bad update can't take down the bot or leave a user stuck with no reply -
    it logs the real error for debugging and gives the user a plain message.
    Also pings the admin if errors start happening repeatedly (not on every
    single one - a transient network blip shouldn't flood the admin's DMs)."""
    import traceback
    error_text = str(context.error)
    error_type = type(context.error).__name__
    print(f"⚠️ Unhandled exception: {error_type}: {error_text}")
    tb_lines = []
    try:
        tb_lines = traceback.format_exception(type(context.error), context.error, context.error.__traceback__)
        print("".join(tb_lines))
    except Exception:
        pass
    # Last real frame from OUR code (bot.py), not library internals - this is
    # what actually tells us which function/line to look at.
    our_frame = ""
    for line in reversed(tb_lines):
        if "bot.py" in line:
            our_frame = line.strip()
            break

    try:
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text(
                "⚠️ Kuch gadbad ho gayi, please dobara try karein."
            , message_effect_id=EFFECT_THUMBS_DOWN)
    except Exception:
        pass

    # Rate-limited admin alert: at most once every 5 minutes, so a burst of
    # identical failures (e.g. Telegram having a bad minute) doesn't spam.
    try:
        now_ts = asyncio.get_event_loop().time()
        _error_notify_state["count"] += 1
        if now_ts - _error_notify_state["last_notify_ts"] > 300:
            _error_notify_state["last_notify_ts"] = now_ts
            count_since = _error_notify_state["count"]
            _error_notify_state["count"] = 0
            for admin_id in ADMIN_IDS:
                try:
                    await context.bot.send_message(
                        admin_id,
                        f"⚠️ Bot mein errors aa rahe hain ({count_since} pichle 5 min mein).\n"
                        f"Type: {error_type}\n"
                        f"Latest: {error_text[:300]}\n"
                        f"Where: {our_frame[:200]}"
                    )
                except Exception:
                    pass
    except Exception:
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
    application.add_handler(CallbackQueryHandler(broadcast_confirm, pattern="^broadcast_(yes|no)$"))
    application.add_handler(CallbackQueryHandler(user_action_callback, pattern="^uact_"))
    application.add_handler(CallbackQueryHandler(gift_item_confirm_prompt, pattern="^giftitem_"))
    application.add_handler(CallbackQueryHandler(gift_item_finalize, pattern="^giftconfirm_"))
    application.add_handler(CallbackQueryHandler(high_value_confirm, pattern="^highvalue_(yes|no)$"))
    application.add_handler(CallbackQueryHandler(reveal_credentials_callback, pattern="^reveal_"))
    application.add_handler(CallbackQueryHandler(preorder_confirm_callback, pattern="^preorder_"))
    application.add_handler(CallbackQueryHandler(remove_item_route_callback, pattern="^rmroute_"))
    application.add_handler(CallbackQueryHandler(remove_channel_callback, pattern="^rmchannel_"))
    application.add_handler(CallbackQueryHandler(leaderboard_callback, pattern="^lb_"))
    
    application.add_handler(MessageHandler(filters.Regex(r'^🛒 Buy Items$|^💰 My Credits$|^🎁 Daily Bonus$|^📤 Referral$|^🏆 Leaderboard$|^🎟 Redeem Code$|^📈 Trending$|^📖 How To Use$|^❓ Help$|^⚙️ Admin Panel$|^🔙 Back$|^❌ Cancel$'), handle_menu_buttons))
    # NOTE: handle_confirm_purchase's pattern (✅ Confirm||123) is a SUBSET of
    # handle_buy_selection's generic pattern (.*||123). It MUST be registered
    # first, otherwise the generic handler always wins and the Confirm button
    # never actually completes a purchase - it just re-shows the confirm screen.
    application.add_handler(MessageHandler(filters.Regex(r'^✅ Confirm\|\|\d+$'), handle_confirm_purchase))
    application.add_handler(MessageHandler(filters.Regex(r'^.*\|\|preorder_\d+$'), handle_preorder_selection))
    application.add_handler(MessageHandler(filters.Regex(r'^.*\|\|\d+$'), handle_buy_selection))
    
    application.add_handler(MessageHandler(filters.Regex(r'^💰 Edit Prices$|^🔄 Edit Points$|^🎁 Edit Bonus$|^🎁 Gift Credits$|^🔻 Deduct Credits$|^👤 User Info$|^📦 Upload Inventory$|^🗄 Users Database$|^📢 Broadcast$|^📨 Message User$|^🚫 Ban User$|^✅ Unban User$|^📊 Stats$|^🎟 Create Code$|^🎁 Item Code$|^📋 List Codes$|^👥 Code Users$|^🔌 Toggle Bot Status$|^🗄 Database Channel$|^📥 Restore Database$|^📦 Stock$|^📢 Channels$|^📊 Leaderboard$|^🔀 Item Routes$|^🖼 Welcome Images$|^📖 How To Use Video$|^🧾 Receipt Mode$|^📝 Text Mode$|^🖼 Image Mode$'), handle_admin_buttons))
    application.add_handler(MessageHandler(filters.Regex(r'^📥 Send New File$|^📤 Export Remaining Accounts$|^🗑 Reset Inventory$|^✅ Yes, Reset Inventory$|^📥 Upload Users DB$|^📤 Export Users DB$|^🗑 Reset Users DB$|^✅ Yes, Reset Users DB$|^❌ No, Cancel$'), handle_inventory_menu))
    application.add_handler(MessageHandler(filters.Regex(r'^.* - \d+💰\|\|price_\d+$'), handle_price_selection))
    application.add_handler(MessageHandler(filters.Regex(r'^.*\|\|itemcode_\d+$'), handle_item_code_select))
    
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_admin_text_input))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_inventory_upload))
    application.add_handler(MessageHandler(filters.PHOTO, handle_welcome_image_upload))
    application.add_handler(MessageHandler(filters.VIDEO, handle_how_to_use_video_upload))
    application.add_handler(ChatMemberHandler(track_channel_leave, ChatMemberHandler.CHAT_MEMBER))
    
    print("🤖 Bot started successfully!")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()