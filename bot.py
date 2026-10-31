import asyncio
import hashlib
import hmac
import html
import json
import logging
import os
import secrets
import sqlite3
import time

from contextlib import closing
from urllib.parse import parse_qsl, quote

from aiohttp import web

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ChatMemberStatus
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    MenuButtonWebApp,
    Message,
    ReplyKeyboardMarkup,
    WebAppInfo,
)


# ==================================================
# SOZLAMALAR — Render.com Environment Variables dan o'qiladi
# ==================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
DB_PATH = os.getenv("DB_PATH", "bot.db")

# WEBAPP_URL avtomatik aniqlanadi:
# 1) WEBAPP_URL env var berilgan bo'lsa — shu ishlatiladi
# 2) Aks holda RENDER_EXTERNAL_URL (Render.com avtomatik beradi)
WEBAPP_URL = os.getenv("WEBAPP_URL", "") or os.getenv("RENDER_EXTERNAL_URL", "")

# Tekshirish — BOT_TOKEN bo'lmasa ishga tushmaydi
if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN topilmadi!\n"
        "Render > Environment > BOT_TOKEN = sizning_token\n"
        "Yoki lokal: export BOT_TOKEN=sizning_token"
    )

router = Router()
ANIMATED_MAIL_EMOJI = "📨"
user_last_content = {}


# ==================================================
# MA'LUMOTLAR BAZASI
# ==================================================

def query(sql, args=(), one=False, commit=False):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(sql, args)
        if commit:
            conn.commit()
            return cur.lastrowid
        rows = cur.fetchall()
        return (rows[0] if rows else None) if one else rows


def init_db():
    query(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            full_name TEXT,
            code TEXT UNIQUE NOT NULL,
            created TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """,
        commit=True,
    )
    query(
        """
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_id INTEGER NOT NULL,
            to_id INTEGER NOT NULL,
            text TEXT,
            msg_type TEXT DEFAULT 'text',
            created TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """,
        commit=True,
    )
    query(
        """
        CREATE TABLE IF NOT EXISTS blocks (
            owner_id INTEGER NOT NULL,
            blocked_id INTEGER NOT NULL,
            PRIMARY KEY (owner_id, blocked_id)
        )
        """,
        commit=True,
    )
    query(
        """
        CREATE TABLE IF NOT EXISTS channels (
            user_id INTEGER,
            channel_id INTEGER,
            channel_title TEXT,
            PRIMARY KEY (user_id, channel_id)
        )
        """,
        commit=True,
    )


def save_user(user):
    row = query("SELECT code FROM users WHERE user_id=?", (user.id,), one=True)
    if row:
        query(
            "UPDATE users SET username=?, full_name=? WHERE user_id=?",
            (user.username, user.full_name, user.id),
            commit=True,
        )
        return row["code"]

    code = secrets.token_hex(7)
    query(
        "INSERT INTO users (user_id, username, full_name, code) VALUES (?, ?, ?, ?)",
        (user.id, user.username, user.full_name, code),
        commit=True,
    )
    return code


def owner_by_code(code: str):
    row = query("SELECT user_id FROM users WHERE code=?", (code,), one=True)
    return row["user_id"] if row else None


def is_blocked(owner_id: int, sender_id: int) -> bool:
    return bool(
        query("SELECT 1 FROM blocks WHERE owner_id=? AND blocked_id=?", (owner_id, sender_id), one=True)
    )


# ==================================================
# TELEGRAM MINI APP MA'LUMOTLARINI TEKSHIRISH
# ==================================================

def verify_telegram_web_app_data(init_data: str) -> dict | None:
    if not BOT_TOKEN or not init_data:
        return None
    try:
        parsed_data = dict(parse_qsl(init_data, keep_blank_values=True))
        if "hash" not in parsed_data:
            return None
        hash_check = parsed_data.pop("hash")
        data_check_string = "\n".join(f"{key}={value}" for key, value in sorted(parsed_data.items()))

        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

        if not hmac.compare_digest(calculated_hash, hash_check):
            return None

        auth_date = parsed_data.get("auth_date")
        if not auth_date or abs(int(time.time()) - int(auth_date)) > 86400:
            return None

        user_data = json.loads(parsed_data.get("user", "{}"))
        if not isinstance(user_data, dict) or not user_data.get("id"):
            return None

        return user_data
    except Exception:
        logging.exception("Mini App tekshiruvida xato")
        return None


# ==================================================
# FSM VA TUGMALAR
# ==================================================

class Flow(StatesGroup):
    writing = State()
    replying = State()


def link_text(username: str, user_id: int) -> str:
    row = query("SELECT code FROM users WHERE user_id=?", (user_id,), one=True)
    if not row:
        raise ValueError("Foydalanuvchi kodi topilmadi")
    return f"https://t.me/{username}?start={row['code']}"


def start_keyboard(link: str, bot_username: str) -> InlineKeyboardMarkup:
    share = f"https://t.me/share/url?url={quote(link)}&text={quote('Menga anonim xabar yuboring 👇')}"
    add_channel_url = f"https://t.me/{bot_username}?startgroup=true&startchannel=true&admin=post_messages"
    buttons = [
        [InlineKeyboardButton(text="Ulashish ↗", url=share)],
        [InlineKeyboardButton(text="Kanalga ulash", url=add_channel_url)],
    ]
    # Web App tugmasi — faqat WEBAPP_URL mavjud bo'lsa
    if WEBAPP_URL:
        buttons.append([InlineKeyboardButton(text="🗂 Boshqaruv Paneli", web_app=WebAppInfo(url=WEBAPP_URL))])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="❌ Bekor qilish", callback_data="cancel_action")]]
    )


def write_again_keyboard(owner_id: int, channel_owner_id: int | None = None) -> InlineKeyboardMarkup:
    channel_owner_id = channel_owner_id if channel_owner_id is not None else owner_id
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Yana xabar yuborish", callback_data=f"again:{owner_id}")],
            [InlineKeyboardButton(text="Kanalga yuborish", callback_data=f"sendchan:{channel_owner_id}")],
        ]
    )


def sender_keyboard(owner_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Yana xabar yuborish", callback_data=f"again:{owner_id}")]]
    )


def incoming_keyboard(msg_id: int, owner_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Javob berish", callback_data=f"reply:{msg_id}")],
            [InlineKeyboardButton(text="Kanalga yuborish", callback_data=f"sendchan:{owner_id}")],
            [InlineKeyboardButton(text="⚠️ Shikoyat", callback_data=f"report:{msg_id}")],
            [InlineKeyboardButton(text="🚫 Taqiqlash", callback_data=f"block:{msg_id}")],
        ]
    )


# ==================================================
# WEB APP HTML SAHIFA
# ==================================================

async def handle_web_app(request):
    html_content = """<!DOCTYPE html>
<html lang="uz">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta name="color-scheme" content="dark light">
    <title>Kabinet va Boshqaruv</title>
    <script src="https://telegram.org/js/telegram-web-app.js"></script>
    <style>
        :root {
            --bg-color: var(--tg-theme-bg-color, #1f2937);
            --text-color: var(--tg-theme-text-color, #f9fafb);
            --card-bg: var(--tg-theme-secondary-bg-color, #273444);
            --hint-color: var(--tg-theme-hint-color, #9ca3af);
            --border-color: rgba(255, 255, 255, 0.1);
            --accent-color: #3b82f6;
        }
        * { box-sizing: border-box; }
        body { margin: 0; padding: 16px; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: var(--bg-color); color: var(--text-color); }
        h2 { margin: 0 0 12px; font-size: 20px; }
        .toolbar { display: flex; gap: 8px; margin-bottom: 14px; flex-wrap: wrap; }
        button { border: 0; border-radius: 9px; padding: 10px 14px; background: var(--card-bg); color: var(--text-color); font-size: 14px; cursor: pointer; font-weight: 600; transition: background .2s; }
        button:active { opacity: .7; }
        button.active { background: var(--accent-color); color: #fff; }
        .card { background: var(--card-bg); border: 1px solid var(--border-color); border-radius: 12px; padding: 14px; margin-bottom: 12px; animation: fadeIn .3s ease; }
        @keyframes fadeIn { from { opacity: 0; transform: translateY(8px); } to { opacity: 1; transform: translateY(0); } }
        .card-header { display: flex; justify-content: space-between; margin-bottom: 6px; color: var(--hint-color); font-size: 12px; }
        .card-body { font-size: 14px; line-height: 1.4; overflow-wrap: anywhere; white-space: pre-wrap; }
        a { color: #60a5fa; text-decoration: none; }
        .loading, .empty, .error { text-align: center; padding: 32px 18px; color: var(--hint-color); }
        .error { color: #fca5a5; }
        .stats { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-bottom: 16px; }
        .stat-card { background: var(--card-bg); border-radius: 10px; padding: 12px; text-align: center; }
        .stat-num { font-size: 24px; font-weight: 700; color: var(--accent-color); }
        .stat-label { font-size: 12px; color: var(--hint-color); margin-top: 4px; }
    </style>
</head>
<body>
    <main>
        <h2>🗂 Boshqaruv Paneli</h2>
        <div class="stats" id="stats-block" style="display:none;">
            <div class="stat-card"><div class="stat-num" id="stat-users">0</div><div class="stat-label">Foydalanuvchilar</div></div>
            <div class="stat-card"><div class="stat-num" id="stat-msgs">0</div><div class="stat-label">Xabarlar</div></div>
        </div>
        <div class="toolbar" id="admin-tabs" style="display:none;">
            <button id="tab-msgs" class="active" onclick="switchTab('msgs')">📥 Xabarlar</button>
            <button id="tab-users" onclick="switchTab('users')">👥 Foydalanuvchilar</button>
        </div>
        <div class="toolbar"><button id="refresh-btn" type="button" onclick="loadData()">🔄 Yangilash</button></div>
        <section id="content-list"><div class="loading">Ma'lumotlar yuklanmoqda...</div></section>
    </main>
    <script>
        "use strict";
        const tg = window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;
        const listEl = document.getElementById("content-list");
        let currentTab = 'msgs';
        let cachedData = null;

        if (tg) { tg.ready(); tg.expand(); }

        function switchTab(tab) {
            currentTab = tab;
            document.getElementById('tab-msgs').className = tab === 'msgs' ? 'active' : '';
            document.getElementById('tab-users').className = tab === 'users' ? 'active' : '';
            renderData();
        }

        function escapeHtml(text) {
            const d = document.createElement('div');
            d.textContent = text;
            return d.innerHTML;
        }

        async function loadData() {
            if (!tg || !tg.initData) {
                listEl.innerHTML = '<div class="error">⚠️ Telegram ichidan oching!<br><br><small>Bu sahifa faqat botdagi tugma orqali ochiladi.</small></div>';
                return;
            }
            listEl.innerHTML = '<div class="loading">⏳ Yuklanmoqda...</div>';
            try {
                const response = await fetch('/api/data', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ initData: tg.initData })
                });
                const result = await response.json();
                if (!response.ok || !result.ok) {
                    listEl.innerHTML = '<div class="error">' + escapeHtml(result.error || 'Server xatosi') + '</div>';
                    return;
                }
                cachedData = result;
                if (result.is_admin) {
                    document.getElementById('admin-tabs').style.display = 'flex';
                    document.getElementById('stats-block').style.display = 'grid';
                    document.getElementById('stat-users').textContent = result.users ? result.users.length : 0;
                    document.getElementById('stat-msgs').textContent = result.messages ? result.messages.length : 0;
                }
                renderData();
            } catch (err) {
                listEl.innerHTML = '<div class="error">Serverga ulanib bo\\'lmadi.<br><small>' + escapeHtml(err.message) + '</small></div>';
            }
        }

        function renderData() {
            if (!cachedData) return;
            if (cachedData.is_admin && currentTab === 'users') {
                const users = cachedData.users || [];
                if (users.length === 0) {
                    listEl.innerHTML = '<div class="empty">Hali foydalanuvchilar yo\\'q 📭</div>';
                    return;
                }
                listEl.innerHTML = users.map((u, i) =>
                    '<div class="card">' +
                    '<div class="card-header"><span>👤 #' + (i+1) + ' · ID: ' + u.user_id + '</span><time>' + escapeHtml(u.created || '') + '</time></div>' +
                    '<div class="card-body">' +
                    '<b>Ism:</b> ' + escapeHtml(u.full_name || "Noma'lum") + '<br>' +
                    '<b>Username:</b> ' + (u.username ? '@' + escapeHtml(u.username) : 'Mavjud emas') + '<br>' +
                    '<b>Profil:</b> <a href="tg://user?id=' + u.user_id + '">Ochish</a>' +
                    '</div></div>'
                ).join('');
            } else {
                const messages = cachedData.messages || [];
                if (messages.length === 0) {
                    listEl.innerHTML = '<div class="empty">Hali xabarlar yo\\'q 📭</div>';
                    return;
                }
                listEl.innerHTML = messages.map((m, i) =>
                    '<div class="card">' +
                    '<div class="card-header"><span>📌 #' + (i+1) + '</span><time>' + escapeHtml(m.created || '') + '</time></div>' +
                    '<div class="card-body">' + escapeHtml(m.text || '') + '</div></div>'
                ).join('');
            }
        }
        loadData();
    </script>
</body>
</html>"""
    return web.Response(text=html_content, content_type="text/html")


# ==================================================
# API: MA'LUMOTLARNI QAYTARISH
# ==================================================

async def handle_get_data(request):
    try:
        data = await request.json()
        init_data = data.get("initData")
        tg_user = verify_telegram_web_app_data(init_data)

        if not tg_user:
            return web.json_response({"ok": False, "error": "Autentifikatsiyadan o'tmadi."}, status=401)

        user_id = int(tg_user["id"])
        is_admin = (user_id == ADMIN_ID)

        users_list = []
        if is_admin:
            users_raw = query("SELECT user_id, username, full_name, created FROM users ORDER BY created DESC")
            users_list = [
                {
                    "user_id": u["user_id"],
                    "username": u["username"],
                    "full_name": u["full_name"],
                    "created": u["created"]
                } for u in users_raw
            ]
            msgs = query(
                """
                SELECT m.id, m.from_id, m.to_id, m.text, m.msg_type, m.created,
                       u1.username AS from_user, u2.username AS to_user
                FROM messages m
                LEFT JOIN users u1 ON m.from_id = u1.user_id
                LEFT JOIN users u2 ON m.to_id = u2.user_id
                ORDER BY m.id DESC
                """
            )
            messages_list = []
            for m in msgs:
                sender = "@" + m["from_user"] if m["from_user"] else str(m["from_id"])
                receiver = "@" + m["to_user"] if m["to_user"] else str(m["to_id"])
                txt = f"Kimdan: {sender}\nKimga: {receiver}\nXabar: {m['text'] or '[Matn yo`q]'}"
                messages_list.append({"id": m["id"], "text": txt, "created": m["created"]})
        else:
            msgs = query("SELECT id, text, created FROM messages WHERE to_id=? ORDER BY id DESC", (user_id,))
            messages_list = [{"id": m["id"], "text": m["text"] or "", "created": m["created"]} for m in msgs]

        return web.json_response({
            "ok": True,
            "is_admin": is_admin,
            "users": users_list,
            "messages": messages_list
        })
    except Exception:
        logging.exception("API xatosi")
        return web.json_response({"ok": False, "error": "Server xatosi."}, status=500)


# ==================================================
# HEALTH CHECK — Render uxlamasligi uchun
# ==================================================

async def handle_health(request):
    return web.json_response({"status": "ok", "bot": "running"})


@web.middleware
async def cors_middleware(request, handler):
    if request.method == "OPTIONS":
        response = web.Response()
    else:
        response = await handler(request)
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "POST, GET, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response


# ==================================================
# KANALGA ADMIN QILINGANDA SAQLASH
# ==================================================

@router.my_chat_member()
async def on_bot_chat_member(event: ChatMemberUpdated):
    if event.chat.type in ("channel", "supergroup"):
        if event.new_chat_member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR):
            query(
                "INSERT OR REPLACE INTO channels (user_id, channel_id, channel_title) VALUES (?, ?, ?)",
                (event.from_user.id, event.chat.id, event.chat.title or "Kanal"),
                commit=True
            )


# ==================================================
# ADMIN PANEL
# ==================================================

@router.message(Command("admin"))
async def cmd_admin(message: Message):
    if not message.from_user or message.from_user.id != ADMIN_ID:
        await message.answer("Bu buyruq faqat admin uchun.")
        return
    if WEBAPP_URL:
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🗂 Boshqaruv Paneli", web_app=WebAppInfo(url=WEBAPP_URL))]
            ]
        )
        await message.answer("<b>👑 Admin panelga xush kelibsiz!</b>", reply_markup=kb)
    else:
        await message.answer(
            "<b>👑 Admin panel</b>\n\n"
            "⚠️ WEBAPP_URL aniqlanmadi.\n"
            "Render > Environment > WEBAPP_URL qo'shing."
        )


# ==================================================
# /START VA BOSHQA FUNKSIYALAR
# ==================================================

async def send_own_link(message: Message, bot: Bot, user=None):
    actual_user = user if user is not None else (message.from_user if message else None)
    if not actual_user:
        return
    save_user(actual_user)
    me = await bot.me()
    link = link_text(me.username, actual_user.id)
    await message.answer(
        f"<b>Shaxsiy havolangiz:</b>\n\n{link}\n\n<b>Havolani ulashib, anonim suhbatni boshlashingiz mumkin.</b>",
        reply_markup=start_keyboard(link, me.username),
        disable_web_page_preview=True,
    )


@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject, state: FSMContext, bot: Bot):
    if not message.from_user:
        return
    save_user(message.from_user)
    await state.clear()
    code = command.args
    if not code:
        await send_own_link(message, bot)
        return

    owner_id = owner_by_code(code)
    if owner_id is None or owner_id == message.from_user.id:
        await send_own_link(message, bot)
        return

    await state.set_state(Flow.writing)
    await state.update_data(owner_id=owner_id)
    await message.answer("<b>Murojaatingizni shu yerga yozing!</b>", reply_markup=cancel_keyboard())


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("<b>Bekor qilindi.</b>")


@router.callback_query(F.data == "cancel_action")
async def cb_cancel_action(cb: CallbackQuery, state: FSMContext, bot: Bot):
    await state.clear()
    if cb.message:
        try:
            await cb.message.delete()
        except Exception:
            pass
        await send_own_link(cb.message, bot, user=cb.from_user)
    await cb.answer("Bekor qilindi.")


async def deliver(bot: Bot, src: Message, to_id: int, header: str, markup):
    if src.text:
        await bot.send_message(to_id, f"{header}\n\n<b>{html.escape(src.text)}</b>", reply_markup=markup)
    else:
        await bot.send_message(to_id, header)
        await bot.copy_message(to_id, src.chat.id, src.message_id, reply_markup=markup)


@router.message(Flow.writing)
async def on_anonymous_message(message: Message, state: FSMContext, bot: Bot):
    if not message.from_user or (message.text and message.text.startswith("/")):
        return
    data = await state.get_data()
    owner_id = data.get("owner_id")
    if not owner_id:
        await state.clear()
        return

    sender_id = message.from_user.id
    if is_blocked(owner_id, sender_id):
        await state.clear()
        await message.answer("<b>Bu foydalanuvchi sizdan xabar qabul qilmaydi.</b>")
        return

    msg_type = "text" if message.text else "media"
    msg_text = message.text or message.caption or ""
    msg_id = query(
        "INSERT INTO messages (from_id, to_id, text, msg_type) VALUES (?, ?, ?, ?)",
        (sender_id, owner_id, msg_text, msg_type),
        commit=True,
    )
    user_last_content[owner_id] = {"msg": msg_text, "reply": ""}

    try:
        await deliver(bot, message, owner_id, f"{ANIMATED_MAIL_EMOJI} <b>Yangi anonim xabar!</b>", incoming_keyboard(msg_id, owner_id))
    except Exception:
        pass

    await state.clear()
    await message.answer("<b>Xabaringiz yuborildi!</b>", reply_markup=sender_keyboard(owner_id))
    await send_own_link(message, bot)


@router.callback_query(F.data.startswith("reply:"))
async def cb_reply(cb: CallbackQuery, state: FSMContext):
    try:
        msg_id = int(cb.data.split(":")[1])
    except Exception:
        return
    row = query("SELECT * FROM messages WHERE id=?", (msg_id,), one=True)
    if not row or not cb.from_user or row["to_id"] != cb.from_user.id:
        return
    await state.set_state(Flow.replying)
    await state.update_data(msg_id=row["id"])
    if cb.message:
        await cb.message.answer("<b>Javobingizni yozing:</b>", reply_markup=cancel_keyboard())
    await cb.answer()


@router.message(Flow.replying)
async def on_reply(message: Message, state: FSMContext, bot: Bot):
    if not message.from_user:
        return
    data = await state.get_data()
    row = query("SELECT * FROM messages WHERE id=?", (data.get("msg_id"),), one=True)
    await state.clear()
    if not row:
        return

    msg_type = "reply_text" if message.text else "reply_media"
    msg_text = message.text or message.caption or ""
    query(
        "INSERT INTO messages (from_id, to_id, text, msg_type) VALUES (?, ?, ?, ?)",
        (message.from_user.id, row["from_id"], msg_text, msg_type),
        commit=True,
    )
    user_last_content[row["to_id"]] = {"msg": row["text"] or "", "reply": msg_text}
    try:
        await deliver(bot, message, row["from_id"], f"{ANIMATED_MAIL_EMOJI} <b>Yangi xabar!</b>", sender_keyboard(row["to_id"]))
    except Exception:
        pass
    await message.answer("<b>Javobingiz yuborildi!</b>", reply_markup=write_again_keyboard(row["to_id"]))


@router.callback_query(F.data.startswith("again:"))
async def cb_again(cb: CallbackQuery, state: FSMContext):
    try:
        owner_id = int(cb.data.split(":")[1])
    except Exception:
        return
    await state.set_state(Flow.writing)
    await state.update_data(owner_id=owner_id)
    if cb.message:
        await cb.message.answer("<b>Xabaringizni yozing:</b>", reply_markup=cancel_keyboard())
    await cb.answer()


@router.callback_query(F.data.startswith("sendchan:"))
async def cb_send_channel(cb: CallbackQuery, bot: Bot):
    try:
        owner_id = int(cb.data.split(":")[1])
    except Exception:
        return
    if not cb.from_user or cb.from_user.id != owner_id:
        return
    chan = query("SELECT channel_id FROM channels WHERE user_id=? ORDER BY rowid DESC LIMIT 1", (owner_id,), one=True)
    if not chan:
        await cb.answer("Kanal topilmadi!", show_alert=True)
        return
    content_data = user_last_content.get(owner_id, {"msg": "", "reply": ""})
    content = f"{ANIMATED_MAIL_EMOJI} <b>Anonim xabar:</b>\n\n{html.escape(content_data['msg'])}"
    if content_data['reply']:
        content += f"\n\nJavob: {html.escape(content_data['reply'])}"
    try:
        await bot.send_message(chan["channel_id"], content)
        await cb.answer("Kanalga yuborildi!", show_alert=True)
    except Exception:
        await cb.answer("Xatolik yuz berdi.", show_alert=True)


@router.callback_query(F.data.startswith("block:"))
async def cb_block(cb: CallbackQuery):
    try:
        msg_id = int(cb.data.split(":")[1])
    except Exception:
        return
    row = query("SELECT * FROM messages WHERE id=?", (msg_id,), one=True)
    if row and cb.from_user and row["to_id"] == cb.from_user.id:
        query("INSERT OR IGNORE INTO blocks (owner_id, blocked_id) VALUES (?, ?)", (row["to_id"], row["from_id"]), commit=True)
        await cb.answer("🚫 Bloklandi.", show_alert=True)


@router.callback_query(F.data.startswith("report:"))
async def cb_report(cb: CallbackQuery, bot: Bot):
    try:
        msg_id = int(cb.data.split(":")[1])
    except Exception:
        return
    row = query("SELECT * FROM messages WHERE id=?", (msg_id,), one=True)
    if row:
        try:
            await bot.send_message(ADMIN_ID, f"<b>⚠️ Shikoyat</b>\nXabar ID: {row['id']}")
        except Exception:
            pass
    await cb.answer("Shikoyat yuborildi.", show_alert=True)


@router.message()
async def fallback(message: Message, bot: Bot):
    await send_own_link(message, bot)


# ==================================================
# ASOSIY DASTUR
# ==================================================

async def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    init_db()
    logging.info(f"WEBAPP_URL = {WEBAPP_URL or '(aniqlanmadi)'}")
    logging.info(f"ADMIN_ID = {ADMIN_ID}")

    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    # Web server
    app = web.Application(middlewares=[cors_middleware])
    app.router.add_get("/", handle_web_app)
    app.router.add_get("/health", handle_health)
    app.router.add_post("/api/data", handle_get_data)

    runner = web.AppRunner(app)
    await runner.setup()

    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logging.info(f"Web server {port}-portda ishga tushdi.")

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        logging.info("Bot polling boshlanmoqda...")
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        logging.info("Bot to'xtatilmoqda...")
        await runner.cleanup()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())