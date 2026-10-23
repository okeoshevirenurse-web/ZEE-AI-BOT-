import os
import sqlite3
import random
import threading
import time
import requests
import telebot
from telebot import types

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
REQUIRED_CHANNEL = os.getenv("REQUIRED_CHANNEL", "@ZEEMANDOMAIN").strip()
CHANNEL_URL = os.getenv("CHANNEL_URL", "https://t.me/ZEEMANDOMAIN").strip()
DB_PATH = os.getenv("DB_PATH", "zee_ai_bot.db")

OWNER_ID = 7946587480
SENIOR_ADMIN_ID = 7328699671
STARTING_ZC = 0
RPS_REWARD = 50
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

if not BOT_TOKEN:
    raise RuntimeError("Missing BOT_TOKEN environment variable.")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML", threaded=True)
db_lock = threading.RLock()
pending_pvp = {}  # challenge_id -> in-memory pending moves; wallet/results remain in SQLite


def connect_db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    with db_lock, connect_db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            zc INTEGER NOT NULL DEFAULT 0,
            joined_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS admins (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            role TEXT NOT NULL,
            added_by INTEGER NOT NULL,
            added_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            amount INTEGER NOT NULL,
            reason TEXT NOT NULL,
            reference TEXT UNIQUE,
            created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS games (
            game_id TEXT PRIMARY KEY,
            mode TEXT NOT NULL,
            player1 INTEGER NOT NULL,
            player2 INTEGER,
            status TEXT NOT NULL,
            winner_id INTEGER,
            reward_paid INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL,
            finished_at INTEGER
        );
        """)
        con.execute(
            "INSERT OR IGNORE INTO admins(user_id, username, role, added_by, added_at) VALUES(?,?,?,?,?)",
            (OWNER_ID, "wickedmd", "owner", OWNER_ID, int(time.time()))
        )
        con.execute(
            "INSERT OR IGNORE INTO admins(user_id, username, role, added_by, added_at) VALUES(?,?,?,?,?)",
            (SENIOR_ADMIN_ID, "wickedmd1", "senior", OWNER_ID, int(time.time()))
        )


def upsert_user(user):
    if not user or getattr(user, "is_bot", False):
        return
    with db_lock, connect_db() as con:
        con.execute("""
            INSERT INTO users(user_id, username, first_name, joined_at)
            VALUES(?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, first_name=excluded.first_name
        """, (user.id, user.username, user.first_name or "", int(time.time())))


def role_of(user_id):
    with db_lock, connect_db() as con:
        row = con.execute("SELECT role FROM admins WHERE user_id=?", (int(user_id),)).fetchone()
    return row["role"] if row else None


def is_admin(user_id):
    return role_of(user_id) is not None


def is_owner(user_id):
    return int(user_id) == OWNER_ID


def member_ok(user_id):
    try:
        member = bot.get_chat_member(REQUIRED_CHANNEL, int(user_id))
        return member.status in ("creator", "administrator", "member") or (
            member.status == "restricted" and getattr(member, "is_member", False)
        )
    except Exception:
        # Fail closed: don't unlock paid/member-gated commands if membership cannot be checked.
        return False


def join_keyboard():
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("📢 Join LORD ZEEMAN DOMAIN", url=CHANNEL_URL))
    kb.add(types.InlineKeyboardButton("✅ I've Joined — Check", callback_data="check_membership"))
    return kb


def require_membership(message_or_call):
    user = message_or_call.from_user
    if member_ok(user.id):
        return True
    text = ("🚫 <b>Forbidden — please join first.</b>\n\n"
            "Join LORD ZEEMAN DOMAIN, then press <b>I've Joined — Check</b>.")
    if hasattr(message_or_call, "message"):  # callback query
        try:
            bot.answer_callback_query(message_or_call.id)
            bot.send_message(user.id, text, reply_markup=join_keyboard())
        except Exception:
            pass
    else:
        bot.reply_to(message_or_call, text, reply_markup=join_keyboard())
    return False


def main_menu():
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("🎮 Games", callback_data="menu_games"),
        types.InlineKeyboardButton("💰 Economy", callback_data="menu_economy"),
        types.InlineKeyboardButton("⚙️ Core", callback_data="menu_core"),
        types.InlineKeyboardButton("🛡 Admin", callback_data="menu_admin"),
        types.InlineKeyboardButton("❓ Help", callback_data="menu_help"),
        types.InlineKeyboardButton("➕ Add to Group", url="https://t.me/" + bot.get_me().username + "?startgroup=true")
    )
    return kb


def balance(user_id):
    with db_lock, connect_db() as con:
        row = con.execute("SELECT zc FROM users WHERE user_id=?", (int(user_id),)).fetchone()
    return int(row["zc"]) if row else STARTING_ZC


def add_zc(user_id, amount, reason, reference):
    """Atomic, idempotent ZC update. Returns False if this reference was already processed."""
    with db_lock, connect_db() as con:
        try:
            con.execute(
                "INSERT INTO transactions(user_id, amount, reason, reference, created_at) VALUES(?,?,?,?,?)",
                (int(user_id), int(amount), reason, reference, int(time.time()))
            )
        except sqlite3.IntegrityError:
            return False
        con.execute("UPDATE users SET zc=zc+? WHERE user_id=?", (int(amount), int(user_id)))
    return True


def wallet_text(user_id):
    with db_lock, connect_db() as con:
        row = con.execute("SELECT zc FROM users WHERE user_id=?", (int(user_id),)).fetchone()
        txs = con.execute(
            "SELECT amount, reason, created_at FROM transactions WHERE user_id=? ORDER BY id DESC LIMIT 8",
            (int(user_id),)
        ).fetchall()
    zc = int(row["zc"]) if row else 0
    lines = [f"👑 <b>ZEE CURRENCY WALLET</b>", f"Balance: <b>{zc:,} ZC</b>", "", "<b>Recent history</b>"]
    if not txs:
        lines.append("No transactions yet.")
    else:
        for tx in txs:
            sign = "+" if tx["amount"] > 0 else ""
            lines.append(f"• {sign}{tx['amount']} ZC — {telebot.util.escape(tx['reason'])}")
    return "\n".join(lines)


def rps_keyboard(game_id):
    kb = types.InlineKeyboardMarkup(row_width=3)
    for move, label in (("rock", "🪨 Rock"), ("paper", "📄 Paper"), ("scissors", "✂️ Scissors")):
        kb.add(types.InlineKeyboardButton(label, callback_data=f"rps:{game_id}:{move}"))
    return kb


def winner_for(a, b):
    if a == b:
        return 0
    if (a, b) in (("rock", "scissors"), ("paper", "rock"), ("scissors", "paper")):
        return 1
    return 2


def start_bot_rps(chat_id, user):
    import uuid
    game_id = uuid.uuid4().hex[:12]
    with db_lock, connect_db() as con:
        con.execute("INSERT INTO games(game_id,mode,player1,player2,status,created_at) VALUES(?,?,?,?,?,?)",
                    (game_id, "bot", user.id, None, "waiting", int(time.time())))
    bot.send_message(
        chat_id,
        f"🪨 📄 ✂️ <b>ROCK-PAPER-SCISSORS</b>\n\n"
        f"Contestant: {telebot.util.escape(user.first_name or 'Player')} vs ZEE AI BOT\n"
        f"Reward: <b>{RPS_REWARD} ZC</b> · Loser gets nothing\n\nChoose your move:",
        reply_markup=rps_keyboard(game_id)
    )


def start_pvp(chat_id, challenger, target):
    import uuid
    game_id = uuid.uuid4().hex[:12]
    with db_lock, connect_db() as con:
        con.execute("INSERT INTO games(game_id,mode,player1,player2,status,created_at) VALUES(?,?,?,?,?,?)",
                    (game_id, "pvp", challenger.id, target.id, "challenged", int(time.time())))
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("✅ Accept challenge", callback_data=f"pvpaccept:{game_id}"))
    kb.add(types.InlineKeyboardButton("❌ Decline", callback_data=f"pvpdecline:{game_id}"))
    bot.send_message(
        chat_id,
        f"⚔️ <b>RPS CHALLENGE</b>\n{telebot.util.escape(challenger.first_name)} challenged "
        f"{telebot.util.escape(target.first_name)}!\nReward: <b>{RPS_REWARD} ZC</b> to the winner.",
        reply_markup=kb
    )


@bot.message_handler(commands=["start"])
def start(message):
    upsert_user(message.from_user)
    if not require_membership(message):
        return
    bot.send_message(
        message.chat.id,
        f"👑 <b>Welcome to ZEE AI BOT, {telebot.util.escape(message.from_user.first_name or 'friend')}!</b>\n\n"
        "Your hub for AI chat, games, Zee Currency and community tools.\nChoose an option below or use /help.",
        reply_markup=main_menu()
    )


@bot.message_handler(commands=["help"])
def help_command(message):
    upsert_user(message.from_user)
    if not require_membership(message):
        return
    bot.reply_to(message,
        "<b>ZEE AI BOT — COMMANDS</b>\n"
        "/start — Open the bot\n/help — Command guide\n/about — About this bot\n/cg — Community links\n"
        "/rps — Play RPS against the bot\n/rps @username — Challenge a player (they must have started the bot)\n"
        "/wallet — View ZC balance and history\n/zc — Check your ZC balance\n"
        "/addadmin ID — Owner: add admin\n/removeadmin ID — Owner: remove admin\n/listadmin — List admins\n\n"
        "More features will be added in future updates."
    )


@bot.message_handler(commands=["about"])
def about(message):
    upsert_user(message.from_user)
    if not require_membership(message):
        return
    bot.reply_to(message,
        "👑 <b>ZEE AI BOT</b>\nA community bot for AI conversations, interactive games, Zee Currency and group utilities.\n"
        "Built for LORD ZEEMAN DOMAIN. Features are being added gradually."
    )


@bot.message_handler(commands=["cg"])
def cg(message):
    upsert_user(message.from_user)
    if not require_membership(message):
        return
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("📢 LORD ZEEMAN DOMAIN", url=CHANNEL_URL))
    kb.add(types.InlineKeyboardButton("➕ Add bot to a group", url="https://t.me/" + bot.get_me().username + "?startgroup=true"))
    bot.reply_to(message, "🌐 <b>CHANNELS & GROUPS</b>\nUse the links below to join or add the bot.", reply_markup=kb)


@bot.message_handler(commands=["zc", "wallet"])
def wallet(message):
    upsert_user(message.from_user)
    if not require_membership(message):
        return
    bot.reply_to(message, wallet_text(message.from_user.id))


@bot.message_handler(commands=["rps"])
def rps(message):
    upsert_user(message.from_user)
    if not require_membership(message):
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) == 1:
        start_bot_rps(message.chat.id, message.from_user)
        return
    target_name = parts[1].strip().lstrip("@").lower()
    if not target_name:
        bot.reply_to(message, "Use /rps to play me, or /rps @username to challenge a player.")
        return
    with db_lock, connect_db() as con:
        target = con.execute("SELECT user_id, username, first_name FROM users WHERE lower(username)=?", (target_name,)).fetchone()
    if not target:
        bot.reply_to(message, "I can't find that player yet. They must open ZEE AI BOT and press /start first. You can also reply to their message with /rps.")
        return
    if int(target["user_id"]) == message.from_user.id:
        bot.reply_to(message, "You can't challenge yourself 😄")
        return
    target_user = types.User(int(target["user_id"]), False, target["first_name"] or target["username"], username=target["username"])
    start_pvp(message.chat.id, message.from_user, target_user)


@bot.message_handler(commands=["addadmin"])
def addadmin(message):
    upsert_user(message.from_user)
    if not is_owner(message.from_user.id):
        bot.reply_to(message, "🚫 Owner-only command.")
        return
    parts = message.text.split(maxsplit=1)
    target_id = None
    username = None
    if message.reply_to_message:
        target = message.reply_to_message.from_user
        target_id, username = target.id, target.username
        upsert_user(target)
    elif len(parts) > 1:
        raw = parts[1].strip()
        if raw.isdigit():
            target_id = int(raw)
            with db_lock, connect_db() as con:
                row = con.execute("SELECT username FROM users WHERE user_id=?", (target_id,)).fetchone()
                username = row["username"] if row else None
        else:
            username = raw.lstrip("@")
            with db_lock, connect_db() as con:
                row = con.execute("SELECT user_id FROM users WHERE lower(username)=?", (username.lower(),)).fetchone()
                target_id = int(row["user_id"]) if row else None
    if not target_id:
        bot.reply_to(message, "Usage: /addadmin <numeric_ID>, or reply to the user's message with /addadmin. A username works only if the user has used this bot before.")
        return
    if target_id == OWNER_ID:
        bot.reply_to(message, "The owner is already protected as owner.")
        return
    with db_lock, connect_db() as con:
        con.execute("INSERT INTO admins(user_id, username, role, added_by, added_at) VALUES(?,?,?,?,?) "
                    "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, role='admin', added_by=excluded.added_by, added_at=excluded.added_at",
                    (target_id, username, "admin", message.from_user.id, int(time.time())))
    bot.reply_to(message, f"✅ Admin added: <code>{target_id}</code>" + (f" (@{telebot.util.escape(username)})" if username else ""))


@bot.message_handler(commands=["removeadmin"])
def removeadmin(message):
    upsert_user(message.from_user)
    if not is_owner(message.from_user.id):
        bot.reply_to(message, "🚫 Owner-only command.")
        return
    parts = message.text.split(maxsplit=1)
    target_id = None
    if message.reply_to_message:
        target_id = message.reply_to_message.from_user.id
    elif len(parts) > 1:
        raw = parts[1].strip()
        if raw.isdigit():
            target_id = int(raw)
        else:
            username = raw.lstrip("@").lower()
            with db_lock, connect_db() as con:
                row = con.execute("SELECT user_id FROM admins WHERE lower(username)=?", (username,)).fetchone()
                if row:
                    target_id = int(row["user_id"])
    if not target_id:
        bot.reply_to(message, "Usage: /removeadmin <numeric_ID>, or reply to an admin's message with /removeadmin.")
        return
    if target_id in (OWNER_ID, SENIOR_ADMIN_ID):
        bot.reply_to(message, "🚫 The configured owner and senior admin are protected in this starter version.")
        return
    with db_lock, connect_db() as con:
        con.execute("DELETE FROM admins WHERE user_id=?", (target_id,))
    bot.reply_to(message, f"✅ Admin role removed for <code>{target_id}</code>.")


@bot.message_handler(commands=["listadmin"])
def listadmin(message):
    upsert_user(message.from_user)
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "🚫 Admin-only command.")
        return
    with db_lock, connect_db() as con:
        rows = con.execute("SELECT user_id, username, role FROM admins ORDER BY CASE role WHEN 'owner' THEN 0 WHEN 'senior' THEN 1 ELSE 2 END, user_id").fetchall()
    lines = ["👑 <b>ZEE AI BOT ADMIN LIST</b>"]
    for row in rows:
        name = f"@{telebot.util.escape(row['username'])}" if row["username"] else "(no username)"
        lines.append(f"• <b>{row['role'].upper()}</b> — {name} — <code>{row['user_id']}</code>")
    bot.reply_to(message, "\n".join(lines))


@bot.callback_query_handler(func=lambda c: c.data == "check_membership")
def check_membership(call):
    if member_ok(call.from_user.id):
        bot.answer_callback_query(call.id, "Membership verified!")
        bot.send_message(call.from_user.id, "✅ You're verified. Use /start to open ZEE AI BOT.", reply_markup=main_menu())
    else:
        bot.answer_callback_query(call.id, "Please join the channel first.", show_alert=True)


@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("menu_"))
def menu_callback(call):
    if not member_ok(call.from_user.id):
        bot.answer_callback_query(call.id, "Please join LORD ZEEMAN DOMAIN first.", show_alert=True)
        return
    pages = {
        "menu_games": "🎮 <b>GAMES</b>\n/rps — Play Rock-Paper-Scissors\nMore games and quiz rewards are planned.",
        "menu_economy": "💰 <b>ECONOMY</b>\n/wallet — Balance and history\n/zc — Check your ZC balance",
        "menu_core": "⚙️ <b>CORE</b>\n/start · /help · /about · /cg",
        "menu_admin": "🛡 <b>ADMIN</b>\n/addadmin · /removeadmin · /listadmin (owner/admin permissions apply)",
        "menu_help": "❓ Use /help to see the command list."
    }
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, pages.get(call.data, "Menu"), reply_markup=main_menu())


@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("rps:"))
def rps_move(call):
    try:
        _, game_id, move = call.data.split(":")
    except ValueError:
        bot.answer_callback_query(call.id, "Invalid game.")
        return
    if move not in ("rock", "paper", "scissors"):
        bot.answer_callback_query(call.id, "Invalid move.")
        return
    if not member_ok(call.from_user.id):
        bot.answer_callback_query(call.id, "Join the channel first.", show_alert=True)
        return
    upsert_user(call.from_user)
    with db_lock, connect_db() as con:
        game = con.execute("SELECT * FROM games WHERE game_id=?", (game_id,)).fetchone()
        if not game or game["status"] != "waiting" or game["player1"] != call.from_user.id:
            bot.answer_callback_query(call.id, "This game is finished or belongs to another player.", show_alert=True)
            return
        # Mark completed before awarding; only the first callback can pass this check.
        con.execute("UPDATE games SET status='finished', finished_at=? WHERE game_id=? AND status='waiting'",
                    (int(time.time()), game_id))
    bot.answer_callback_query(call.id)
    bot_move = random.choice(("rock", "paper", "scissors"))
    result = winner_for(move, bot_move)
    if result == 1:
        add_zc(call.from_user.id, RPS_REWARD, "RPS win vs bot", f"rps:{game_id}:win")
        outcome = f"🎉 You win! <b>+{RPS_REWARD} ZC</b>"
    elif result == 2:
        outcome = "😅 ZEE AI BOT wins. No ZC awarded."
    else:
        outcome = "🤝 Draw! No ZC awarded."
    labels = {"rock":"🪨 Rock", "paper":"📄 Paper", "scissors":"✂️ Scissors"}
    try:
        bot.edit_message_text(
            f"🪨 📄 ✂️ <b>ROCK-PAPER-SCISSORS</b>\n\nYou chose: {labels[move]}\nZEE AI BOT chose: {labels[bot_move]}\n\n{outcome}\n\nWallet: <b>{balance(call.from_user.id):,} ZC</b>",
            call.message.chat.id, call.message.message_id
        )
    except Exception:
        bot.send_message(call.message.chat.id, f"{outcome}\nWallet: {balance(call.from_user.id):,} ZC")


@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("pvpaccept:"))
def pvp_accept(call):
    game_id = call.data.split(":", 1)[1]
    with db_lock, connect_db() as con:
        game = con.execute("SELECT * FROM games WHERE game_id=?", (game_id,)).fetchone()
        if not game or game["mode"] != "pvp" or game["status"] != "challenged":
            bot.answer_callback_query(call.id, "Challenge expired.")
            return
        if call.from_user.id != game["player2"]:
            bot.answer_callback_query(call.id, "Only the challenged player can accept.", show_alert=True)
            return
        con.execute("UPDATE games SET status='playing' WHERE game_id=? AND status='challenged'", (game_id,))
    bot.answer_callback_query(call.id, "Challenge accepted!")
    # Moves are stored in process memory for the starter. A production multi-worker deployment should persist these in SQLite.
    pending_pvp[game_id] = {"moves": {}, "chat_id": call.message.chat.id, "players": (game["player1"], game["player2"])}
    bot.edit_message_text("⚔️ <b>Challenge accepted!</b>\nBoth players, choose your moves below.", call.message.chat.id, call.message.message_id,
                          reply_markup=pvp_moves_keyboard(game_id))


def pvp_moves_keyboard(game_id):
    kb = types.InlineKeyboardMarkup(row_width=3)
    for move, label in (("rock","🪨 Rock"),("paper","📄 Paper"),("scissors","✂️ Scissors")):
        kb.add(types.InlineKeyboardButton(label, callback_data=f"pvpmove:{game_id}:{move}"))
    return kb


@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("pvpdecline:"))
def pvp_decline(call):
    game_id = call.data.split(":", 1)[1]
    with db_lock, connect_db() as con:
        game = con.execute("SELECT * FROM games WHERE game_id=?", (game_id,)).fetchone()
        if not game or game["status"] != "challenged" or call.from_user.id != game["player2"]:
            bot.answer_callback_query(call.id, "Only the challenged player can decline.")
            return
        con.execute("UPDATE games SET status='declined', finished_at=? WHERE game_id=?", (int(time.time()), game_id))
    bot.answer_callback_query(call.id, "Challenge declined.")
    bot.edit_message_text("Challenge declined.", call.message.chat.id, call.message.message_id)


@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("pvpmove:"))
def pvp_move(call):
    _, game_id, move = call.data.split(":")
    if move not in ("rock", "paper", "scissors"):
        bot.answer_callback_query(call.id, "Invalid move.")
        return
    with db_lock, connect_db() as con:
        game = con.execute("SELECT * FROM games WHERE game_id=?", (game_id,)).fetchone()
        if not game or game["status"] != "playing":
            bot.answer_callback_query(call.id, "This game is no longer active.")
            return
        if call.from_user.id not in (game["player1"], game["player2"]):
            bot.answer_callback_query(call.id, "You are not a player in this match.", show_alert=True)
            return
        state = pending_pvp.get(game_id)
        if not state:
            bot.answer_callback_query(call.id, "This match needs to be restarted; try /rps again.", show_alert=True)
            return
        if call.from_user.id in state["moves"]:
            bot.answer_callback_query(call.id, "Your move is already locked.")
            return
        state["moves"][call.from_user.id] = move
        bot.answer_callback_query(call.id, "Move locked!")
        if len(state["moves"]) < 2:
            bot.send_message(call.message.chat.id, "✅ One player's move is locked. Waiting for the other player.")
            return
        move1, move2 = state["moves"][game["player1"]], state["moves"][game["player2"]]
        result = winner_for(move1, move2)
        winner_id = game["player1"] if result == 1 else game["player2"] if result == 2 else None
        con.execute("UPDATE games SET status='finished', winner_id=?, finished_at=? WHERE game_id=? AND status='playing'",
                    (winner_id, int(time.time()), game_id))
    pending_pvp.pop(game_id, None)
    labels = {"rock":"🪨 Rock", "paper":"📄 Paper", "scissors":"✂️ Scissors"}
    if winner_id:
        add_zc(winner_id, RPS_REWARD, "RPS PvP win", f"rps:{game_id}:win")
        result_text = f"🏆 Winner: <code>{winner_id}</code> — <b>+{RPS_REWARD} ZC</b>"
    else:
        result_text = "🤝 Draw! No ZC awarded."
    try:
        bot.edit_message_text(
            f"🪨 📄 ✂️ <b>RPS RESULT</b>\nPlayer 1: {labels[move1]}\nPlayer 2: {labels[move2]}\n\n{result_text}",
            call.message.chat.id, call.message.message_id
        )
    except Exception:
        bot.send_message(call.message.chat.id, result_text)


@bot.message_handler(func=lambda m: bool(m.text) and not m.text.startswith("/"))
def ai_chat(message):
    upsert_user(message.from_user)
    if not require_membership(message):
        return
    if not GEMINI_API_KEY:
        bot.reply_to(message, "🤖 AI chat is not configured yet. The owner needs to add GEMINI_API_KEY to the hosting environment.")
        return
    try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
        response = requests.post(
            url,
            params={"key": GEMINI_API_KEY},
            json={"contents":[{"parts":[{"text":message.text[:12000]}]}]},
            timeout=35
        )
        response.raise_for_status()
        data = response.json()
        answer = data["candidates"][0]["content"]["parts"][0]["text"].strip()
        # Telegram messages have a length limit; keep a safe margin.
        for i in range(0, len(answer), 3900):
            bot.send_message(message.chat.id, telebot.util.escape(answer[i:i+3900]), parse_mode="HTML")
    except Exception:
        bot.reply_to(message, "⚠️ I couldn't get an AI reply just now. Please try again later.")


@bot.message_handler(content_types=["new_chat_members"])
def added_to_group(message):
    me = bot.get_me()
    if any(member.id == me.id for member in message.new_chat_members):
        bot.send_message(message.chat.id, "👑 ZEE AI BOT is here! Use /help to see available commands.")


def main():
    init_db()
    print("ZEE AI BOT starting...")
    bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=30)


if __name__ == "__main__":
    main()
