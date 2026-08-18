import os
import json
import sqlite3
import asyncio
from datetime import datetime, timedelta
from typing import Optional, Dict, List
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
from telegram.constants import ParseMode

# ==================== CONFIGURATION ====================
TOKEN = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
ADMIN_IDS = [int(x) for x in os.environ.get("ADMIN_IDS", "7892255798").split(",")]
CHANNELS = ["@SRK_ERA", "@SRKING000001", "@SRK_IMP1"]

DB_PATH = "bot_data.db"

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
        total_referrals INTEGER DEFAULT 0
    )''')
    
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
    
    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('refer_points', '5')")
    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('daily_bonus', '10')")
    
    special_items = [
        (203054011, "DARK DESIRE BUNDLE", 50),
        (909054007, "Volcanic Fury", 40),
        (909854001, "Pride Judgment", 45),
        (907105450, "Gloo Wall - Slothful Desire", 35),
        (911005401, "Boat of Luxury", 30),
        (907104730, "Katana - Spiky Desire", 25),
        (907105424, "Katana - Loving Desire", 25),
        (907105405, "M82B - Envious Desire", 30),
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
            "total_referrals": result[6]
        }
    return None

def create_user(user_id, username, first_name, referrer_id=None):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    joined_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
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
        purchase_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
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
    now = datetime.now()
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
              (admin_id, action, target, details, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()
    conn.close()

# ==================== BOT HANDLERS ====================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    username = user.username or "NoUsername"
    first_name = user.first_name or "User"
    
    existing_user = get_user(user_id)
    
    referrer_id = None
    if context.args and context.args[0].startswith("ref_"):
        try:
            referrer_id = int(context.args[0].split("_")[1])
            if referrer_id == user_id:
                referrer_id = None
        except:
            pass
    
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
            # ONLY CHANNEL JOIN = INLINE BUTTONS WITH URL
            keyboard = []
            for channel in CHANNELS:
                channel_name = channel.replace('@', '')
                keyboard.append([InlineKeyboardButton(f"📢 Join {channel}", url=f"https://t.me/{channel_name}", style="primary")])
            keyboard.append([InlineKeyboardButton("✅ I have joined", callback_data="check_joined", style="success")])
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await update.message.reply_text(
                "⚠️ *Please join all required channels first!*\n\n"
                "You must join these channels to use this bot:\n"
                + "\n".join([f"• {ch}" for ch in CHANNELS]),
                reply_markup=reply_markup,
                parse_mode=ParseMode.MARKDOWN
            )
            return
        
        user_data = create_user(user_id, username, first_name, referrer_id)
        
        for admin_id in ADMIN_IDS:
            try:
                ref_text = f"from @{get_user(referrer_id)['username']}" if referrer_id else "No referrer"
                msg = (
                    f"📥 *New User Joined!*\n"
                    f"🕐 Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                    f"👤 User: {first_name} (@{username}) [ID: {user_id}]\n"
                    f"📌 Type: {ref_text}\n"
                )
                await context.bot.send_message(admin_id, msg, parse_mode=ParseMode.MARKDOWN)
            except:
                pass
    
    await show_main_menu(update, context)

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
            create_user(user_id, username, first_name, None)
        try:
            await query.edit_message_text("✅ *All channels joined!*\n\nWelcome to the bot!", parse_mode=ParseMode.MARKDOWN)
        except Exception:
            pass
        
        user = get_user(user_id)
        keyboard = [
            [KeyboardButton("🛒 Buy Items", style="primary"), KeyboardButton("💰 My Credits", style="success")],
            [KeyboardButton("🎁 Daily Bonus", style="success"), KeyboardButton("📤 Referral", style="primary")],
            [KeyboardButton("🏆 Leaderboard", style="primary"), KeyboardButton("❓ Help", style="primary")]
        ]
        if user_id in ADMIN_IDS:
            keyboard.append([KeyboardButton("⚙️ Admin Panel", style="danger")])
        reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        await query.message.reply_text(
            f"🏠 *Welcome back!*\n\n"
            f"👤 User: {user['first_name']}\n"
            f"💰 Credits: {user['credits']}\n"
            f"🏆 Referrals: {user['total_referrals']}\n\n"
            f"Select an option below:",
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN
        )
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
            keyboard.append([InlineKeyboardButton(f"📢 Join {channel}", url=f"https://t.me/{channel_name}", style="primary")])
        keyboard.append([InlineKeyboardButton("✅ I have joined", callback_data="check_joined", style="success")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        try:
            await query.edit_message_text(
                "⚠️ *Please join all required channels first!*\n\n"
                "You must join these channels to use this bot:\n"
                + "\n".join([f"• {ch}" for ch in CHANNELS]),
                reply_markup=reply_markup,
                parse_mode=ParseMode.MARKDOWN
            )
        except Exception:
            pass

async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = get_user(user_id)
    if not user:
        await start(update, context)
        return
    
    keyboard = [
        [KeyboardButton("🛒 Buy Items", style="primary"), KeyboardButton("💰 My Credits", style="success")],
        [KeyboardButton("🎁 Daily Bonus", style="success"), KeyboardButton("📤 Referral", style="primary")],
        [KeyboardButton("🏆 Leaderboard", style="primary"), KeyboardButton("❓ Help", style="primary")]
    ]
    if user_id in ADMIN_IDS:
        keyboard.append([KeyboardButton("⚙️ Admin Panel", style="danger")])
    
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    await update.message.reply_text(
        f"🏠 *Welcome back!*\n\n"
        f"👤 User: {user['first_name']}\n"
        f"💰 Credits: {user['credits']}\n"
        f"🏆 Referrals: {user['total_referrals']}\n\n"
        f"Select an option below:",
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN
    )

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
    elif text == "❓ Help":
        await show_help(update, context)
    elif text == "⚙️ Admin Panel" and user_id in ADMIN_IDS:
        await show_admin_panel(update, context)
    elif text == "🔙 Back" or text == "❌ Cancel":
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
            keyboard.append([KeyboardButton(f"{item['item_name']} - {item['price']}💰 ({item['available']} left)||{item['item_id']}", style="primary")])
    keyboard.append([KeyboardButton("🔙 Back")])
    
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    await update.message.reply_text(
        f"🛒 *Available Items*\n\n"
        f"💰 Your Credits: {user['credits']}\n\n"
        f"Select an item to buy:",
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN
    )

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
            )
            return
        
        keyboard = [
            [KeyboardButton(f"✅ Confirm||{item_id}", style="success")],
            [KeyboardButton("❌ Cancel", style="danger")]
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
        )
    except:
        await update.message.reply_text("⚠️ *Invalid selection!*", parse_mode=ParseMode.MARKDOWN)
        await show_buy_items(update, context)

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
            await update.message.reply_text("⚠️ *Insufficient credits!*", parse_mode=ParseMode.MARKDOWN)
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
                    [KeyboardButton("🛒 Buy Items", style="primary")],
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
                )
                
                for admin_id in ADMIN_IDS:
                    try:
                        await context.bot.send_message(
                            admin_id,
                            f"🛒 *Purchase Made!*\n"
                            f"User: @{user['username']} [ID: {user_id}]\n"
                            f"Item: {assigned_item['item_name']}\n"
                            f"Price: {price}💰\n"
                            f"Remaining Stock: {get_inventory_count(item_id)}",
                            parse_mode=ParseMode.MARKDOWN
                        )
                    except:
                        pass
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
    )

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
        )
    else:
        await update.message.reply_text(
            f"⏰ *Already Claimed Today!*\n\n"
            f"Next claim available at: {result}\n"
            f"Come back after this time!",
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN
        )

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
    )

async def show_leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    top_referrers = get_top_referrers(10)
    
    keyboard = [[KeyboardButton("🔙 Back")]]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    if not top_referrers:
        await update.message.reply_text("📊 *Leaderboard*\n\nNo referrals yet! Be the first!", reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
        return
    
    text = "🏆 *Top Referrers*\n\n"
    emojis = ["🥇", "🥈", "🥉"]
    for i, ref in enumerate(top_referrers):
        emoji = emojis[i] if i < 3 else f"{i+1}."
        name = ref['first_name'] or ref['username'] or f"User_{ref['user_id']}"
        text += f"{emoji} {name} - {ref['total_referrals']} referrals\n"
    
    await update.message.reply_text(text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)

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
        f"📢 *Required Channels:*\n" + "\n".join([f"• {ch}" for ch in CHANNELS]) + "\n\n"
        f"💡 *Tip:* More referrals = More credits = More items!",
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN
    )

# ==================== ADMIN PANEL ====================

async def show_admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("⚠️ *Unauthorized!*", parse_mode=ParseMode.MARKDOWN)
        return
    
    keyboard = [
        [KeyboardButton("💰 Edit Prices", style="primary"), KeyboardButton("🔄 Edit Points", style="primary")],
        [KeyboardButton("🎁 Edit Bonus", style="success"), KeyboardButton("🎁 Gift Credits", style="success")],
        [KeyboardButton("🔻 Deduct Credits", style="danger"), KeyboardButton("👤 User Info", style="primary")],
        [KeyboardButton("📦 Upload Inventory", style="primary"), KeyboardButton("📢 Broadcast", style="primary")],
        [KeyboardButton("📊 Stats", style="primary"), KeyboardButton("🔙 Back")]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    await update.message.reply_text(
        f"🔧 *Admin Panel*\n\n"
        f"Select an action:",
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN
    )

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
        )
    elif text == "🎁 Edit Bonus":
        context.user_data['admin_action'] = 'edit_bonus'
        await update.message.reply_text(
            f"🎁 *Edit Daily Bonus*\n\n"
            f"Current Bonus: {get_setting('daily_bonus') or '10'}\n\n"
            f"Send the new bonus value (number only).\n"
            f"Example: `15`\n\n"
            f"Type 'cancel' to cancel.",
            parse_mode=ParseMode.MARKDOWN
        )
    elif text == "🎁 Gift Credits":
        context.user_data['admin_action'] = 'gift'
        await update.message.reply_text(
            f"🎁 *Gift Credits*\n\n"
            f"Format: `user_id amount`\n"
            f"Example: `123456789 50`\n\n"
            f"Type 'cancel' to cancel.",
            parse_mode=ParseMode.MARKDOWN
        )
    elif text == "🔻 Deduct Credits":
        context.user_data['admin_action'] = 'deduct'
        await update.message.reply_text(
            f"🔻 *Deduct Credits*\n\n"
            f"Format: `user_id amount`\n"
            f"Example: `123456789 10`\n\n"
            f"Type 'cancel' to cancel.",
            parse_mode=ParseMode.MARKDOWN
        )
    elif text == "👤 User Info":
        context.user_data['admin_action'] = 'user_info'
        await update.message.reply_text(
            f"👤 *Get User Info*\n\n"
            f"Send the user ID to fetch details.\n"
            f"Example: `123456789`\n\n"
            f"Type 'cancel' to cancel.",
            parse_mode=ParseMode.MARKDOWN
        )
    elif text == "📦 Upload Inventory":
        await show_upload_inventory_menu(update, context)
    elif text == "📢 Broadcast":
        context.user_data['admin_action'] = 'broadcast'
        await update.message.reply_text(
            f"📢 *Broadcast Message*\n\n"
            f"Send the message you want to broadcast to all users.\n"
            f"Type 'cancel' to cancel.",
            parse_mode=ParseMode.MARKDOWN
        )
    elif text == "📊 Stats":
        await show_stats(update, context)

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
    )

async def handle_price_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    
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
        )
    except:
        await update.message.reply_text("⚠️ *Invalid selection!*", parse_mode=ParseMode.MARKDOWN)

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
    )

async def handle_admin_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    user_id = update.effective_user.id
    
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
            await update.message.reply_text(f"✅ *Referral points updated!*\n\nNew: {value} per referral", parse_mode=ParseMode.MARKDOWN)
            context.user_data['admin_action'] = None
            await show_admin_panel(update, context)
            
        elif action == 'edit_bonus':
            value = int(text)
            update_setting("daily_bonus", str(value))
            log_admin_action(user_id, "edit_bonus", "daily_bonus", f"New value: {value}")
            await update.message.reply_text(f"✅ *Daily bonus updated!*\n\nNew: {value} per day", parse_mode=ParseMode.MARKDOWN)
            context.user_data['admin_action'] = None
            await show_admin_panel(update, context)
            
        elif action.startswith('price_'):
            item_id = int(action.split("_")[1])
            value = int(text)
            update_setting(f"price_{item_id}", str(value))
            log_admin_action(user_id, "edit_price", f"price_{item_id}", f"New price: {value}")
            await update.message.reply_text(f"✅ *Price updated successfully!*\n\nNew price: {value}💰", parse_mode=ParseMode.MARKDOWN)
            context.user_data['admin_action'] = None
            await show_admin_panel(update, context)
            
        elif action == 'gift' or action == 'deduct':
            parts = text.split()
            if len(parts) != 2:
                await update.message.reply_text("⚠️ *Invalid format! Use: user_id amount*", parse_mode=ParseMode.MARKDOWN)
                return
            
            target_id = int(parts[0])
            amount = int(parts[1])
            
            if amount < 0:
                await update.message.reply_text("⚠️ *Amount cannot be negative!*", parse_mode=ParseMode.MARKDOWN)
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
                    f"User: {target_user['first_name']} (@{target_user['username']})\n"
                    f"Amount: {amount}💰\n"
                    f"New Balance: {target_user['credits'] + amount}💰",
                    parse_mode=ParseMode.MARKDOWN
                )
            else:
                if target_user['credits'] < amount:
                    await update.message.reply_text(
                        f"⚠️ *Insufficient credits!*\n\n"
                        f"User: {target_user['first_name']}\n"
                        f"Balance: {target_user['credits']}💰\n"
                        f"Requested deduction: {amount}💰",
                        parse_mode=ParseMode.MARKDOWN
                    )
                    return
                deduct_credits(target_id, amount)
                log_admin_action(user_id, "deduct_credits", str(target_id), f"Amount: {amount}")
                await update.message.reply_text(
                    f"✅ *Credits deducted!*\n\n"
                    f"User: {target_user['first_name']} (@{target_user['username']})\n"
                    f"Amount: {amount}💰\n"
                    f"New Balance: {target_user['credits'] - amount}💰",
                    parse_mode=ParseMode.MARKDOWN
                )
            
            context.user_data['admin_action'] = None
            await show_admin_panel(update, context)
            
        elif action == 'user_info':
            target_id = int(text)
            target_user = get_user(target_id)
            if not target_user:
                await update.message.reply_text(f"⚠️ *User {target_id} not found!*", parse_mode=ParseMode.MARKDOWN)
                return
            
            purchases = get_user_purchases(target_id)
            purchase_text = ""
            if purchases:
                purchase_text = "\n\n*Purchases:*\n"
                for p in purchases[:10]:
                    purchase_text += f"• {p['item_name']} - {p['purchase_date']}\n"
                    purchase_text += f"  UID: {p['guest_uid']}\n"
            
            await update.message.reply_text(
                f"👤 *User Information*\n\n"
                f"ID: {target_user['user_id']}\n"
                f"Name: {target_user['first_name']}\n"
                f"Username: @{target_user['username'] or 'None'}\n"
                f"Credits: {target_user['credits']}💰\n"
                f"Referrals: {target_user['total_referrals']}\n"
                f"Joined: {target_user['joined_date']}\n"
                f"Total Purchases: {len(purchases)}\n"
                f"{purchase_text}",
                parse_mode=ParseMode.MARKDOWN
            )
            context.user_data['admin_action'] = None
            await show_admin_panel(update, context)
            
        elif action == 'broadcast':
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute("SELECT user_id FROM users")
            users = c.fetchall()
            conn.close()
            
            sent = 0
            failed = 0
            for user in users:
                try:
                    await context.bot.send_message(user[0], f"📢 *Announcement*\n\n{text}", parse_mode=ParseMode.MARKDOWN)
                    sent += 1
                    await asyncio.sleep(0.1)
                except:
                    failed += 1
            
            log_admin_action(user_id, "broadcast", "all_users", f"Sent: {sent}, Failed: {failed}")
            await update.message.reply_text(
                f"✅ *Broadcast Completed!*\n\n"
                f"Sent: {sent} users\n"
                f"Failed: {failed} users",
                parse_mode=ParseMode.MARKDOWN
            )
            context.user_data['admin_action'] = None
            await show_admin_panel(update, context)
            
    except ValueError:
        await update.message.reply_text("⚠️ *Please enter a valid number!*", parse_mode=ParseMode.MARKDOWN)

async def show_upload_inventory_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['admin_action'] = None
    remaining = sum(item["available"] for item in get_all_special_items())
    keyboard = [
        [KeyboardButton("📥 Send New File", style="primary")],
        [KeyboardButton("📤 Export Remaining Accounts", style="success")],
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
    )

async def handle_inventory_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    user_id = update.effective_user.id

    if user_id not in ADMIN_IDS:
        return

    if text == "📥 Send New File":
        context.user_data['admin_action'] = 'upload_inventory'
        await update.message.reply_text(
            f"📦 *Upload Inventory*\n\n"
            f"Send me a JSON file with the inventory data.\n\n"
            f"Only special items will be stored.\n"
            f"Type 'cancel' to cancel.",
            parse_mode=ParseMode.MARKDOWN
        )

    elif text == "📤 Export Remaining Accounts":
        export_data = get_available_inventory_export()
        if not export_data:
            await update.message.reply_text("⚠️ *No remaining accounts in inventory!*", parse_mode=ParseMode.MARKDOWN)
            return

        file_path = f"/tmp/remaining_inventory_{int(datetime.now().timestamp())}.json"
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
            [KeyboardButton("❌ No, Cancel", style="primary")]
        ]
        reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        await update.message.reply_text(
            f"⚠️ *Are you sure?*\n\n"
            f"This will permanently delete ALL stored accounts (sold + unsold) from the database. "
            f"This action *cannot be undone*.\n\n"
            f"✅ Credits, referral points, prices and user data will *NOT* be touched — only inventory accounts are removed.",
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN
        )

    elif text == "✅ Yes, Reset Inventory" and context.user_data.get('admin_action') == 'confirm_reset_inventory':
        deleted_count = reset_inventory()
        log_admin_action(user_id, "reset_inventory", "inventory", f"Deleted: {deleted_count} accounts")
        context.user_data['admin_action'] = None
        await update.message.reply_text(
            f"✅ *Inventory Reset Complete!*\n\n"
            f"🗑 Deleted: {deleted_count} accounts from the database\n\n"
            f"💰 Credits and referral points are untouched.",
            parse_mode=ParseMode.MARKDOWN
        )
        await show_admin_panel(update, context)

    elif text == "❌ No, Cancel" and context.user_data.get('admin_action') == 'confirm_reset_inventory':
        context.user_data['admin_action'] = None
        await update.message.reply_text("❌ *Reset cancelled.* Nothing was deleted.", parse_mode=ParseMode.MARKDOWN)
        await show_upload_inventory_menu(update, context)

async def handle_inventory_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        return
    
    if not update.message.document:
        await update.message.reply_text("⚠️ *Please send a JSON file!*", parse_mode=ParseMode.MARKDOWN)
        return
    
    if context.user_data.get('admin_action') != 'upload_inventory':
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
            await update.message.reply_text("⚠️ *Invalid format! Expected a list of items.*", parse_mode=ParseMode.MARKDOWN)
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
            await update.message.reply_text("⚠️ *No special items found in the file!*", parse_mode=ParseMode.MARKDOWN)
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
        
        await update.message.reply_text(summary, parse_mode=ParseMode.MARKDOWN)
        context.user_data['admin_action'] = None
        await show_admin_panel(update, context)
        
    except json.JSONDecodeError:
        await update.message.reply_text("⚠️ *Invalid JSON file!*", parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        await update.message.reply_text(f"⚠️ *Error: {str(e)}*", parse_mode=ParseMode.MARKDOWN)

# ==================== MAIN ====================

def main():
    init_db()
    
    application = Application.builder().token(TOKEN).build()
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(check_joined, pattern="check_joined"))
    
    application.add_handler(MessageHandler(filters.Regex(r'^🛒 Buy Items$|^💰 My Credits$|^🎁 Daily Bonus$|^📤 Referral$|^🏆 Leaderboard$|^❓ Help$|^⚙️ Admin Panel$|^🔙 Back$|^❌ Cancel$'), handle_menu_buttons))
    # NOTE: handle_confirm_purchase's pattern (✅ Confirm||123) is a SUBSET of
    # handle_buy_selection's generic pattern (.*||123). It MUST be registered
    # first, otherwise the generic handler always wins and the Confirm button
    # never actually completes a purchase - it just re-shows the confirm screen.
    application.add_handler(MessageHandler(filters.Regex(r'^✅ Confirm\|\|\d+$'), handle_confirm_purchase))
    application.add_handler(MessageHandler(filters.Regex(r'^.*\|\|\d+$'), handle_buy_selection))
    
    application.add_handler(MessageHandler(filters.Regex(r'^💰 Edit Prices$|^🔄 Edit Points$|^🎁 Edit Bonus$|^🎁 Gift Credits$|^🔻 Deduct Credits$|^👤 User Info$|^📦 Upload Inventory$|^📢 Broadcast$|^📊 Stats$'), handle_admin_buttons))
    application.add_handler(MessageHandler(filters.Regex(r'^📥 Send New File$|^📤 Export Remaining Accounts$|^🗑 Reset Inventory$|^✅ Yes, Reset Inventory$|^❌ No, Cancel$'), handle_inventory_menu))
    application.add_handler(MessageHandler(filters.Regex(r'^.* - \d+💰\|\|price_\d+$'), handle_price_selection))
    
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_admin_text_input))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_inventory_upload))
    
    print("🤖 Bot started successfully!")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()