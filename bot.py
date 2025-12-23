import os
import re
import time
import sqlite3
import sys
import asyncio
import threading
from collections import defaultdict, deque
from typing import Optional, Tuple, Set, Dict

from flask import Flask

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ChatPermissions,
)
from telegram.constants import ChatMemberStatus, ParseMode, ChatType
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
    ApplicationHandlerStop,
)

# -------------------- CONFIG --------------------
OWNER_ID = 5631512980
BOT_USERNAME = "swipeemanagerbot"  # your bot username without @

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DB_PATH = os.getenv("DB_PATH", "group_manager.db")

WELCOME_START_PREFIX = "wel_"  # /start wel_<chat_id>

# Admin cache: chat_id -> {"ids": set(int), "ts": float}
ADMIN_CACHE: Dict[int, Dict[str, object]] = {}
ADMIN_CACHE_TTL = 10 * 60  # 10 minutes

# In-memory antiflood buckets: (chat_id, user_id) -> deque[timestamps]
FLOOD_BUCKETS = defaultdict(lambda: deque(maxlen=20))

LINK_RE = re.compile(r"(https?://|t\.me/|www\.)", re.IGNORECASE)

# -------------------- WEB (for UptimeRobot / Render) --------------------
web_app = Flask(__name__)

@web_app.get("/")
def home():
    return "OK", 200

@web_app.get("/health")
def health():
    return "healthy", 200


def run_web():
    port = int(os.getenv("PORT", "10000"))  # Render sets PORT automatically for Web Services
    web_app.run(host="0.0.0.0", port=port)


# -------------------- PYTHON 3.14 EVENT LOOP FIX --------------------
def ensure_event_loop():
    if sys.platform.startswith("win"):
        try:
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        except Exception:
            pass

    try:
        asyncio.get_running_loop()
        return
    except RuntimeError:
        pass

    try:
        asyncio.get_event_loop()
        return
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)


# -------------------- DB --------------------
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def init_db():
    conn = db()
    cur = conn.cursor()

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS chat_settings (
            chat_id INTEGER PRIMARY KEY,

            antiflood_enabled INTEGER DEFAULT 1,
            flood_limit INTEGER DEFAULT 6,
            flood_window_sec INTEGER DEFAULT 8,
            flood_action_mute_sec INTEGER DEFAULT 60,

            link_lock_enabled INTEGER DEFAULT 0,
            blocklist_enabled INTEGER DEFAULT 1,

            greetings_enabled INTEGER DEFAULT 1,
            welcome_text TEXT DEFAULT 'Welcome, {mention}!',

            clean_commands_enabled INTEGER DEFAULT 0
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS warnings (
            chat_id INTEGER,
            user_id INTEGER,
            count INTEGER DEFAULT 0,
            PRIMARY KEY(chat_id, user_id)
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS blocklist (
            chat_id INTEGER,
            phrase TEXT,
            PRIMARY KEY(chat_id, phrase)
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS notes (
            chat_id INTEGER,
            name TEXT,
            content TEXT,
            PRIMARY KEY(chat_id, name)
        )
        """
    )

    conn.commit()
    conn.close()


def ensure_chat_row(chat_id: int):
    conn = db()
    conn.execute("INSERT OR IGNORE INTO chat_settings(chat_id) VALUES(?)", (chat_id,))
    conn.commit()
    conn.close()


def get_settings(chat_id: int) -> dict:
    ensure_chat_row(chat_id)
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM chat_settings WHERE chat_id=?", (chat_id,))
    row = cur.fetchone()
    cols = [d[0] for d in cur.description]
    conn.close()
    return dict(zip(cols, row))


def set_setting(chat_id: int, key: str, value):
    ensure_chat_row(chat_id)
    conn = db()
    conn.execute(f"UPDATE chat_settings SET {key}=? WHERE chat_id=?", (value, chat_id))
    conn.commit()
    conn.close()


# -------------------- Helpers / Access --------------------
def is_private(update: Update) -> bool:
    return bool(update.effective_chat and update.effective_chat.type == ChatType.PRIVATE)


def is_group(update: Update) -> bool:
    return bool(update.effective_chat and update.effective_chat.type in (ChatType.GROUP, ChatType.SUPERGROUP))


def is_owner(user_id: int) -> bool:
    return user_id == OWNER_ID


async def refresh_admin_cache(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> Set[int]:
    admins = await context.bot.get_chat_administrators(chat_id)
    admin_ids = {a.user.id for a in admins}
    ADMIN_CACHE[chat_id] = {"ids": admin_ids, "ts": time.time()}
    return admin_ids


async def get_admin_ids(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> Set[int]:
    cached = ADMIN_CACHE.get(chat_id)
    now = time.time()
    if cached and (now - float(cached["ts"])) < ADMIN_CACHE_TTL:
        return set(cached["ids"])  # type: ignore
    try:
        return await refresh_admin_cache(context, chat_id)
    except Exception:
        if cached:
            return set(cached["ids"])  # type: ignore
        return set()


async def is_group_admin(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    if is_owner(user_id):
        return True
    chat = update.effective_chat
    if not chat:
        return False
    if chat.type == ChatType.PRIVATE:
        return True
    admin_ids = await get_admin_ids(context, chat.id)
    return user_id in admin_ids


def extract_command(text: str) -> str:
    if not text or not text.startswith("/"):
        return ""
    cmd = text.split()[0]
    cmd = cmd.split("@")[0]
    return cmd[1:].lower()


# -------------------- PM HELP BUTTON (LIKE SCREENSHOT) --------------------
def pm_help_button() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("Click me for help!", url=f"https://t.me/{BOT_USERNAME}?start=help")]]
    )


# -------------------- GLOBAL GROUP COMMAND GATE --------------------
async def group_command_gate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    In groups: block ALL commands for non-admins/non-owner.
    Exceptions allowed for everyone:
      /admin, /info, /help, /start
    """
    if not is_group(update):
        return

    msg = update.effective_message
    user = update.effective_user
    if not msg or not user or not msg.text:
        return

    cmd = extract_command(msg.text)

    # Commands allowed for ALL members:
    if cmd in ("admin", "info", "help", "start"):
        return

    # Everything else: admins/owner only
    if not await is_group_admin(update, context, user.id):
        await msg.reply_text("You are not an admin.\nUse /admin to check the admins of this group.")
        raise ApplicationHandlerStop


# -------------------- HELP --------------------
def help_text_private() -> str:
    return (
        "Commands (Admins in groups):\n"
        "• /settings\n"
        "• /ban (reply), /unban (reply)\n"
        "• /mute <min> (reply), /unmute (reply)\n"
        "• /warn (reply), /warnings, /resetwarns (reply)\n"
        "• /block <phrase>, /unblock <phrase>, /blocklist\n"
        "• /save <name> <content>, /get <name>, /notes, /clear <name>\n\n"
        "Welcome message:\n"
        "• In group (admins): /setup\n"
        "• Then open PM via button and send welcome text\n\n"
        "Everyone can use in group:\n"
        "• /admin\n"
        "• /info (reply)\n"
        "• /help\n"
        "• /start\n\n"
        "Placeholders:\n"
        "• {mention} {first} {last} {username}\n\n"
        "Warn system:\n"
        "• 3 warnings → 4th warning = BAN\n"
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_group(update):
        await update.effective_message.reply_text(
            "Contact me in PM for help!",
            reply_markup=pm_help_button(),
            disable_web_page_preview=True,
        )
        return

    await update.effective_message.reply_text(help_text_private())


# -------------------- /start (deep links) --------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # In groups: any user gets PM button
    if is_group(update):
        await update.effective_message.reply_text(
            "Contact me in PM for help!",
            reply_markup=pm_help_button(),
            disable_web_page_preview=True,
        )
        return

    # In PM
    if context.args:
        arg = context.args[0].strip()

        if arg.lower() == "help":
            await update.effective_message.reply_text(help_text_private())
            return

        if arg.startswith(WELCOME_START_PREFIX):
            raw = arg[len(WELCOME_START_PREFIX):]
            try:
                chat_id = int(raw)
            except ValueError:
                await update.effective_message.reply_text("Invalid welcome setup link.")
                return

            user = update.effective_user
            if not user:
                return

            # Verify admin for that group (unless OWNER_ID)
            if not is_owner(user.id):
                try:
                    member = await context.bot.get_chat_member(chat_id, user.id)
                    if member.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
                        await update.effective_message.reply_text("You are not an admin of that group.")
                        return
                except Exception:
                    await update.effective_message.reply_text(
                        "I can't access that group right now.\nMake sure I'm still in the group."
                    )
                    return

            context.user_data["awaiting_welcome_for"] = chat_id
            current = get_settings(chat_id).get("welcome_text", "Welcome, {mention}!")
            await update.effective_message.reply_text(
                "Send me the new welcome message now.\n\n"
                "Placeholders:\n"
                "• {mention} {first} {last} {username}\n\n"
                f"Current:\n{current}"
            )
            return

    await update.effective_message.reply_text(
        "Hi! Send /help for commands.\nTo setup welcome: run /setup in your group (admins only)."
    )


# -------------------- /admin (allowed for everyone; owner id NOT shown) --------------------
async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_group(update):
        await update.effective_message.reply_text("Use /admin inside a group to see the admins.")
        return

    chat = update.effective_chat
    try:
        admins = await context.bot.get_chat_administrators(chat.id)
        lines = ["✅ <b>Group admins:</b>"]
        for a in admins:
            u = a.user
            lines.append(f"• {u.mention_html()} (<code>{u.id}</code>)")

        await update.effective_message.reply_text(
            "\n".join(lines),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except Exception as e:
        await update.effective_message.reply_text(f"Failed to fetch admins: {e}")


# -------------------- /info (allowed for everyone) --------------------
async def cmd_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_group(update):
        await update.effective_message.reply_text("Use /info inside a group (reply to a user).")
        return

    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat:
        return

    # Reply target, else self
    if msg.reply_to_message and msg.reply_to_message.from_user:
        u = msg.reply_to_message.from_user
    else:
        u = update.effective_user

    username = f"@{u.username}" if u.username else "—"
    full_name = " ".join([p for p in [u.first_name, u.last_name] if p]) or "—"

    text = (
        f"👤 <b>User Info</b>\n"
        f"• Name: {full_name}\n"
        f"• Username: {username}\n"
        f"• User ID: <code>{u.id}</code>\n\n"
        f"💬 <b>Chat Info</b>\n"
        f"• Group: {chat.title or '—'}\n"
        f"• Chat ID: <code>{chat.id}</code>\n"
    )

    await msg.reply_text(text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


# -------------------- Settings --------------------
def settings_keyboard(s: dict) -> InlineKeyboardMarkup:
    def onoff(v: int) -> str:
        return "✅ ON" if v else "❌ OFF"

    buttons = [
        [InlineKeyboardButton(f"Antiflood: {onoff(s['antiflood_enabled'])}", callback_data="tog:antiflood_enabled")],
        [InlineKeyboardButton(f"Link Lock: {onoff(s['link_lock_enabled'])}", callback_data="tog:link_lock_enabled")],
        [InlineKeyboardButton(f"Blocklist: {onoff(s['blocklist_enabled'])}", callback_data="tog:blocklist_enabled")],
        [InlineKeyboardButton(f"Greetings: {onoff(s['greetings_enabled'])}", callback_data="tog:greetings_enabled")],
        [InlineKeyboardButton(f"Clean Cmds: {onoff(s['clean_commands_enabled'])}", callback_data="tog:clean_commands_enabled")],
    ]
    return InlineKeyboardMarkup(buttons)


async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    s = get_settings(chat_id)
    await update.effective_message.reply_text(
        "⚙️ *Group Settings*",
        reply_markup=settings_keyboard(s),
        parse_mode=ParseMode.MARKDOWN,
    )


async def on_settings_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    chat_id = update.effective_chat.id
    data = q.data
    s = get_settings(chat_id)

    if data.startswith("tog:"):
        key = data.split(":", 1)[1]
        new_val = 0 if s[key] else 1
        set_setting(chat_id, key, new_val)
        s = get_settings(chat_id)
        await q.edit_message_reply_markup(reply_markup=settings_keyboard(s))


# -------------------- Welcome setup (/setup) --------------------
def setup_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("Configure welcome in PM", url=f"https://t.me/{BOT_USERNAME}?start={WELCOME_START_PREFIX}{chat_id}")]]
    )


async def cmd_setup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_group(update):
        await update.effective_message.reply_text("Use this command inside a group.")
        return
    chat = update.effective_chat
    await update.effective_message.reply_text(
        "Click the button below to set the welcome message in PM.",
        reply_markup=setup_keyboard(chat.id),
        disable_web_page_preview=True,
    )


async def handle_private_welcome_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_private(update):
        return

    chat_id = context.user_data.get("awaiting_welcome_for")
    if not chat_id:
        return

    user = update.effective_user
    if not user:
        return

    # Verify again
    if not is_owner(user.id):
        try:
            member = await context.bot.get_chat_member(int(chat_id), user.id)
            if member.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
                await update.effective_message.reply_text("You are not an admin of that group.")
                context.user_data.pop("awaiting_welcome_for", None)
                return
        except Exception:
            await update.effective_message.reply_text("I can't access that group. Make sure I'm still in it.")
            context.user_data.pop("awaiting_welcome_for", None)
            return

    text = (update.effective_message.text or "").strip()
    if not text:
        await update.effective_message.reply_text("Send a valid text.")
        return

    set_setting(int(chat_id), "welcome_text", text)
    set_setting(int(chat_id), "greetings_enabled", 1)

    context.user_data.pop("awaiting_welcome_for", None)
    await update.effective_message.reply_text("✅ Welcome message updated and enabled for that group.")


def render_welcome(template: str, user) -> str:
    mention = user.mention_html()
    first = (user.first_name or "")
    last = (user.last_name or "")
    username = f"@{user.username}" if user.username else ""

    out = template or "Welcome, {mention}!"
    out = out.replace("{mention}", mention)
    out = out.replace("{first}", first)
    out = out.replace("{last}", last)
    out = out.replace("{username}", username)
    return out


async def on_new_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_group(update):
        return
    chat_id = update.effective_chat.id
    s = get_settings(chat_id)
    if not s.get("greetings_enabled", 1):
        return

    for member in update.effective_message.new_chat_members:
        msg = render_welcome(s.get("welcome_text", "Welcome, {mention}!"), member)
        await update.effective_message.reply_html(msg)


# -------------------- Warnings (3 -> 4th BAN) --------------------
def get_warn_count(chat_id: int, user_id: int) -> int:
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT count FROM warnings WHERE chat_id=? AND user_id=?", (chat_id, user_id))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else 0


def set_warn_count(chat_id: int, user_id: int, count: int):
    conn = db()
    conn.execute(
        "INSERT INTO warnings(chat_id, user_id, count) VALUES(?,?,?) "
        "ON CONFLICT(chat_id, user_id) DO UPDATE SET count=excluded.count",
        (chat_id, user_id, count),
    )
    conn.commit()
    conn.close()


def reset_warns(chat_id: int, user_id: int):
    conn = db()
    conn.execute("DELETE FROM warnings WHERE chat_id=? AND user_id=?", (chat_id, user_id))
    conn.commit()
    conn.close()


def parse_target_user(update: Update) -> Optional[int]:
    msg = update.effective_message
    if msg and msg.reply_to_message and msg.reply_to_message.from_user:
        return msg.reply_to_message.from_user.id
    return None


def parse_reason_and_arg(text: str) -> Tuple[str, str]:
    parts = text.split(maxsplit=2)
    if len(parts) == 1:
        return "", ""
    if len(parts) == 2:
        return parts[1], ""
    return parts[1], parts[2]


async def cmd_warn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_group(update):
        return

    chat_id = update.effective_chat.id
    target = parse_target_user(update)
    if not target:
        await update.effective_message.reply_text("Reply to a user with /warn [reason].")
        return

    warn_limit = 4  # 4th = ban
    _, reason = parse_reason_and_arg(update.effective_message.text)
    count = get_warn_count(chat_id, target) + 1
    set_warn_count(chat_id, target, count)

    if count >= warn_limit:
        reset_warns(chat_id, target)
        try:
            await context.bot.ban_chat_member(chat_id, target)
            await update.effective_message.reply_text("🚫 4th warning reached — user banned.")
        except Exception as e:
            await update.effective_message.reply_text(f"Failed to ban: {e}")
    else:
        await update.effective_message.reply_text(
            f"⚠️ Warned. ({count}/3)" + (f"\nReason: {reason}" if reason else "")
        )


async def cmd_warnings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    target = parse_target_user(update) or update.effective_user.id
    count = get_warn_count(chat_id, target)
    await update.effective_message.reply_text(f"Warnings: {count} (4th = ban)")


async def cmd_resetwarns(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    target = parse_target_user(update)
    if not target:
        await update.effective_message.reply_text("Reply to a user with /resetwarns.")
        return
    reset_warns(chat_id, target)
    await update.effective_message.reply_text("✅ Warnings reset.")


# -------------------- Admin actions --------------------
async def cmd_ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    target = parse_target_user(update)
    if not target:
        await update.effective_message.reply_text("Reply to a user with /ban [reason].")
        return
    _, reason = parse_reason_and_arg(update.effective_message.text)
    try:
        await context.bot.ban_chat_member(chat_id, target)
        await update.effective_message.reply_text(f"🚫 Banned. {('Reason: ' + reason) if reason else ''}".strip())
    except Exception as e:
        await update.effective_message.reply_text(f"Failed to ban: {e}")


async def cmd_unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    target = parse_target_user(update)
    if not target:
        await update.effective_message.reply_text("Reply to a user with /unban.")
        return
    try:
        await context.bot.unban_chat_member(chat_id, target, only_if_banned=True)
        await update.effective_message.reply_text("✅ Unbanned.")
    except Exception as e:
        await update.effective_message.reply_text(f"Failed: {e}")


async def cmd_mute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    target = parse_target_user(update)
    if not target:
        await update.effective_message.reply_text("Reply to a user with /mute <minutes> [reason].")
        return

    arg, reason = parse_reason_and_arg(update.effective_message.text)
    try:
        minutes = int(arg) if arg else 10
    except ValueError:
        minutes = 10

    until = int(time.time() + minutes * 60)
    perms = ChatPermissions(can_send_messages=False)
    try:
        await context.bot.restrict_chat_member(chat_id, target, permissions=perms, until_date=until)
        await update.effective_message.reply_text(
            f"🔇 Muted for {minutes} min. {('Reason: ' + reason) if reason else ''}".strip()
        )
    except Exception as e:
        await update.effective_message.reply_text(f"Failed: {e}")


async def cmd_unmute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    target = parse_target_user(update)
    if not target:
        await update.effective_message.reply_text("Reply to a user with /unmute.")
        return

    perms = ChatPermissions(
        can_send_messages=True,
        can_send_audios=True,
        can_send_documents=True,
        can_send_photos=True,
        can_send_videos=True,
        can_send_video_notes=True,
        can_send_voice_notes=True,
        can_send_polls=True,
        can_send_other_messages=True,
        can_add_web_page_previews=True,
        can_invite_users=True,
    )
    try:
        await context.bot.restrict_chat_member(chat_id, target, permissions=perms)
        await update.effective_message.reply_text("🔊 Unmuted.")
    except Exception as e:
        await update.effective_message.reply_text(f"Failed: {e}")


async def cmd_del(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat_id = update.effective_chat.id
    if not msg.reply_to_message:
        await msg.reply_text("Reply to a message with /del.")
        return
    try:
        await context.bot.delete_message(chat_id, msg.reply_to_message.message_id)
        await context.bot.delete_message(chat_id, msg.message_id)
    except Exception as e:
        await msg.reply_text(f"Failed: {e}")


# -------------------- Anti-flood & Filters --------------------
async def antiflood_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not msg or not chat or not user:
        return False

    s = get_settings(chat.id)
    if not s["antiflood_enabled"]:
        return False

    if await is_group_admin(update, context, user.id):
        return False

    key = (chat.id, user.id)
    bucket = FLOOD_BUCKETS[key]
    now = time.time()
    bucket.append(now)

    window = s["flood_window_sec"]
    limit = s["flood_limit"]
    count = sum(1 for t in bucket if now - t <= window)

    if count >= limit:
        mute_sec = s["flood_action_mute_sec"]
        until = int(now + mute_sec)
        try:
            await context.bot.restrict_chat_member(chat.id, user.id, ChatPermissions(can_send_messages=False), until_date=until)
        except Exception:
            pass
        try:
            await context.bot.delete_message(chat.id, msg.message_id)
        except Exception:
            pass
        return True

    return False


def add_block(chat_id: int, phrase: str):
    phrase = phrase.strip().lower()
    if not phrase:
        return
    conn = db()
    conn.execute("INSERT OR IGNORE INTO blocklist(chat_id, phrase) VALUES(?,?)", (chat_id, phrase))
    conn.commit()
    conn.close()


def remove_block(chat_id: int, phrase: str):
    phrase = phrase.strip().lower()
    conn = db()
    conn.execute("DELETE FROM blocklist WHERE chat_id=? AND phrase=?", (chat_id, phrase))
    conn.commit()
    conn.close()


def list_block(chat_id: int):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT phrase FROM blocklist WHERE chat_id=? ORDER BY phrase", (chat_id,))
    rows = [r[0] for r in cur.fetchall()]
    conn.close()
    return rows


async def cmd_block(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    parts = update.effective_message.text.split(maxsplit=1)
    if len(parts) < 2:
        await update.effective_message.reply_text("Usage: /block <word_or_phrase>")
        return
    add_block(chat_id, parts[1])
    await update.effective_message.reply_text("✅ Added to blocklist.")


async def cmd_unblock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    parts = update.effective_message.text.split(maxsplit=1)
    if len(parts) < 2:
        await update.effective_message.reply_text("Usage: /unblock <word_or_phrase>")
        return
    remove_block(chat_id, parts[1])
    await update.effective_message.reply_text("✅ Removed from blocklist.")


async def cmd_blocklist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    words = list_block(chat_id)
    if not words:
        await update.effective_message.reply_text("Blocklist is empty.")
        return
    await update.effective_message.reply_text("🧱 Blocklist:\n- " + "\n- ".join(words))


async def filter_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not msg or not chat or not user or not msg.text:
        return False

    s = get_settings(chat.id)
    if await is_group_admin(update, context, user.id):
        return False

    text = msg.text.lower()

    if s["link_lock_enabled"] and LINK_RE.search(text):
        try:
            await context.bot.delete_message(chat.id, msg.message_id)
        except Exception:
            pass
        return True

    if s["blocklist_enabled"]:
        blocked = list_block(chat.id)
        for phrase in blocked:
            if phrase and phrase in text:
                try:
                    await context.bot.delete_message(chat.id, msg.message_id)
                except Exception:
                    pass
                return True

    return False


# -------------------- Notes --------------------
def save_note(chat_id: int, name: str, content: str):
    conn = db()
    conn.execute(
        "INSERT INTO notes(chat_id, name, content) VALUES(?,?,?) "
        "ON CONFLICT(chat_id, name) DO UPDATE SET content=excluded.content",
        (chat_id, name.lower(), content),
    )
    conn.commit()
    conn.close()


def get_note(chat_id: int, name: str) -> Optional[str]:
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT content FROM notes WHERE chat_id=? AND name=?", (chat_id, name.lower()))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None


def list_notes(chat_id: int):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT name FROM notes WHERE chat_id=? ORDER BY name", (chat_id,))
    rows = [r[0] for r in cur.fetchall()]
    conn.close()
    return rows


def clear_note(chat_id: int, name: str):
    conn = db()
    conn.execute("DELETE FROM notes WHERE chat_id=? AND name=?", (chat_id, name.lower()))
    conn.commit()
    conn.close()


async def cmd_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    parts = update.effective_message.text.split(maxsplit=2)
    if len(parts) < 3:
        await update.effective_message.reply_text("Usage: /save <name> <content>")
        return
    save_note(chat_id, parts[1], parts[2])
    await update.effective_message.reply_text("✅ Note saved.")


async def cmd_get(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    parts = update.effective_message.text.split(maxsplit=1)
    if len(parts) < 2:
        await update.effective_message.reply_text("Usage: /get <name>")
        return
    note = get_note(chat_id, parts[1])
    if not note:
        await update.effective_message.reply_text("Note not found.")
        return
    await update.effective_message.reply_text(note)


async def cmd_notes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    names = list_notes(chat_id)
    if not names:
        await update.effective_message.reply_text("No notes yet.")
        return
    await update.effective_message.reply_text("🗒 Notes:\n- " + "\n- ".join(names))


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    parts = update.effective_message.text.split(maxsplit=1)
    if len(parts) < 2:
        await update.effective_message.reply_text("Usage: /clear <name>")
        return
    clear_note(chat_id, parts[1])
    await update.effective_message.reply_text("✅ Note cleared.")


# -------------------- Moderation pipeline --------------------
async def on_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_group(update):
        return
    if await antiflood_check(update, context):
        return
    if await filter_check(update, context):
        return


# -------------------- MAIN --------------------
def main():
    ensure_event_loop()

    if not BOT_TOKEN:
        raise RuntimeError("Set BOT_TOKEN env var first (BOT_TOKEN).")

    init_db()

    # Start web server for Render/UptimeRobot in background thread
    threading.Thread(target=run_web, daemon=True).start()

    app = Application.builder().token(BOT_TOKEN).build()

    # Command gate FIRST
    app.add_handler(
        MessageHandler(filters.ChatType.GROUPS & filters.COMMAND, group_command_gate),
        group=-1
    )

    # basics
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))

    # public commands
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CommandHandler("info", cmd_info))

    # settings
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CallbackQueryHandler(on_settings_click, pattern=r"^tog:"))

    # welcome setup
    app.add_handler(CommandHandler("setup", cmd_setup))
    app.add_handler(CommandHandler("welcome_setup", cmd_setup))  # alias

    # DM welcome text receiver
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, handle_private_welcome_text))

    # welcome join handler
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, on_new_members))

    # warns
    app.add_handler(CommandHandler("warn", cmd_warn))
    app.add_handler(CommandHandler("warnings", cmd_warnings))
    app.add_handler(CommandHandler("resetwarns", cmd_resetwarns))

    # admin actions
    app.add_handler(CommandHandler("ban", cmd_ban))
    app.add_handler(CommandHandler("unban", cmd_unban))
    app.add_handler(CommandHandler("mute", cmd_mute))
    app.add_handler(CommandHandler("unmute", cmd_unmute))
    app.add_handler(CommandHandler("del", cmd_del))

    # filters
    app.add_handler(CommandHandler("block", cmd_block))
    app.add_handler(CommandHandler("unblock", cmd_unblock))
    app.add_handler(CommandHandler("blocklist", cmd_blocklist))

    # notes
    app.add_handler(CommandHandler("save", cmd_save))
    app.add_handler(CommandHandler("get", cmd_get))
    app.add_handler(CommandHandler("notes", cmd_notes))
    app.add_handler(CommandHandler("clear", cmd_clear))

    # moderation for normal messages
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & filters.TEXT & ~filters.COMMAND, on_text_message))

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
