import asyncio
import json
import sqlite3
import uuid
import subprocess
from datetime import datetime, timedelta
from typing import Optional, List, Tuple, Dict, Any
from functools import wraps

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
)

# =========================
# НАСТРОЙКИ
# =========================
BOT_TOKEN = "7900977069:AAGGHgnf9GPFqQjCVyffs9uCw1Cu4oySXiI"
ADMIN_ID = 1991552751

XRAY_CONFIG = "/usr/local/etc/xray/config.json"

# VLESS Reality (твои значения)
SERVER_ADDR = "185.58.115.221"
PORT = 443
SNI = "www.cloudflare.com"
PUBLIC_KEY = "GNligj6Ji9XELMMgOQFN-3-IBdDWJShDmY_44CpIeX0"   # pbk
SHORT_ID = "345945fbd4038bf2"                            # sid
FLOW = "xtls-rprx-vision"
TRANSPORT = "tcp"

SUPPORT_TEXT = (
    "<b>🆘 Поддержка</b>\n"
    "Пиши в личку: <a href='https://t.me/moloko113'>@moloko113</a>\n"
)
HOWTO_TEXT = (
    "<b>ℹ️ Как подключиться</b>\n"
    "1) Установи клиент:\n"
    "   • Android — v2rayNG\n"
    "   • Windows — v2rayN\n"
    "   • iOS — Shadowrocket\n"
    "2) Нажми «🛠 Создать конфиг» — получишь персональную ссылку.\n"
    "3) В клиенте выбери «Импорт по ссылке/URI», вставь её.\n"
    "4) Включи VPN и проверь работу.\n"
)

# =========================
# ИНИЦИАЛИЗАЦИЯ
# =========================
bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
dp = Dispatcher()

# =========================
# БАЗА ДАННЫХ
# =========================
# check_same_thread=False — чтобы использовать соединение в разных обработчиках
conn = sqlite3.connect("users.db", check_same_thread=False)
cur = conn.cursor()

# users.approved: 0 (ожидает), 1 (одобрен), -1 (отклонён)
cur.execute("""
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    approved INTEGER NOT NULL DEFAULT 0
)
""")
cur.execute("""
CREATE TABLE IF NOT EXISTS configs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    uuid TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    is_trial INTEGER NOT NULL DEFAULT 0,
    created_by_admin INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    FOREIGN KEY(user_id) REFERENCES users(user_id)
)
""")
conn.commit()

# Простой стейт для диалогов админа
ADMIN_PENDING: Dict[int, Dict[str, Any]] = {}

# =========================
# UI — красивые клавиатуры
# =========================
def user_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛠 Создать конфиг", callback_data="user_create")],
        [InlineKeyboardButton(text="🧪 Протестить VPN (3 дня)", callback_data="user_trial")],
        [InlineKeyboardButton(text="ℹ️ Как подключиться", callback_data="user_howto")],
        [InlineKeyboardButton(text="🆘 Поддержка", callback_data="user_support")]
    ])

def admin_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📜 Список подключений", callback_data="admin_list")
        ],
        [
            InlineKeyboardButton(text="⏫ Продлить конфиг", callback_data="admin_extend"),
            InlineKeyboardButton(text="🗑 Удалить конфиг", callback_data="admin_delete")
        ],
        [
            InlineKeyboardButton(text="➕ Создать доп. конфиг", callback_data="admin_create_extra")
        ]
    ])

def approval_kb(for_user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Одобрить", callback_data=f"approve_{for_user_id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"decline_{for_user_id}")
        ]
    ])

# =========================
# XRAY HELPERS
# =========================
def get_reality_inbound(config: dict):
    for inbound in config.get("inbounds", []):
        if inbound.get("protocol") != "vless":
            continue
        st = inbound.get("streamSettings", {})
        if st.get("security") == "reality":
            return inbound
    return None

def xray_restart():
    # по желанию: subprocess.run(["xray", "run", "-test", "-config", XRAY_CONFIG])
    subprocess.run(["systemctl", "restart", "xray"])

def add_uuid_to_xray(user_uuid: str):
    with open(XRAY_CONFIG, "r") as f:
        cfg = json.load(f)
    inbound = get_reality_inbound(cfg)
    if not inbound:
        raise RuntimeError("Reality inbound не найден")
    clients = inbound.setdefault("settings", {}).setdefault("clients", [])
    if not any(c.get("id") == user_uuid for c in clients):
        clients.append({"id": user_uuid, "flow": FLOW})
    with open(XRAY_CONFIG, "w") as f:
        json.dump(cfg, f, indent=2)
    xray_restart()

def remove_uuid_from_xray(user_uuid: str):
    with open(XRAY_CONFIG, "r") as f:
        cfg = json.load(f)
    inbound = get_reality_inbound(cfg)
    if not inbound:
        return
    clients = inbound.get("settings", {}).get("clients", [])
    inbound["settings"]["clients"] = [c for c in clients if c.get("id") != user_uuid]
    with open(XRAY_CONFIG, "w") as f:
        json.dump(cfg, f, indent=2)
    xray_restart()

def build_vless_link(user_uuid: str) -> str:
    return (
        f"vless://{user_uuid}@{SERVER_ADDR}:{PORT}"
        f"?encryption=none&flow={FLOW}&security=reality"
        f"&sni={SNI}&fp=chrome&pbk={PUBLIC_KEY}&sid={SHORT_ID}"
        f"&type={TRANSPORT}#VPN"
    )

# =========================
# DB HELPERS
# =========================
def ensure_user_row(user_id: int):
    cur.execute("INSERT OR IGNORE INTO users(user_id) VALUES(?)", (user_id,))
    conn.commit()

def set_approved(user_id: int, approved: int):
    cur.execute("INSERT OR REPLACE INTO users(user_id, approved) VALUES(?, ?)", (user_id, approved))
    conn.commit()

def is_approved(user_id: int) -> int:
    cur.execute("SELECT approved FROM users WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    return int(row[0]) if row else 0

def get_active_user_configs(user_id: int) -> List[Tuple]:
    now = datetime.now().isoformat()
    cur.execute("""SELECT id, uuid, expires_at, is_trial, created_by_admin, created_at
                   FROM configs WHERE user_id=? AND expires_at > ?
                   ORDER BY id DESC""", (user_id, now))
    return cur.fetchall()

def get_all_configs(limit=50) -> List[Tuple]:
    cur.execute("""SELECT id, user_id, uuid, expires_at, is_trial, created_by_admin, created_at
                   FROM configs ORDER BY id DESC LIMIT ?""", (limit,))
    return cur.fetchall()

def create_config_record(user_id: int, user_uuid: str, days: int, is_trial=False, created_by_admin=False):
    expires_at = (datetime.now() + timedelta(days=days)).isoformat()
    cur.execute("""INSERT INTO configs(user_id, uuid, expires_at, is_trial, created_by_admin, created_at)
                   VALUES(?, ?, ?, ?, ?, ?)""",
                (user_id, user_uuid, expires_at, int(is_trial), int(created_by_admin), datetime.now().isoformat()))
    conn.commit()

def delete_config_record(config_id: int) -> Optional[str]:
    cur.execute("SELECT uuid FROM configs WHERE id=?", (config_id,))
    row = cur.fetchone()
    if not row:
        return None
    uuid_val = row[0]
    cur.execute("DELETE FROM configs WHERE id=?", (config_id,))
    conn.commit()
    return uuid_val

def extend_config_record(config_id: int, add_days: int) -> Optional[str]:
    cur.execute("SELECT expires_at FROM configs WHERE id=?", (config_id,))
    row = cur.fetchone()
    if not row:
        return None
    expires_at = row[0]
    new_exp = (datetime.fromisoformat(expires_at) + timedelta(days=add_days)).isoformat()
    cur.execute("UPDATE configs SET expires_at=? WHERE id=?", (new_exp, config_id))
    conn.commit()
    return new_exp

# =========================
# DECORATOR — доступ только одобренным
# =========================
def require_approved(func):
    @wraps(func)
    async def wrapper(cb: CallbackQuery, **data):
        if is_approved(cb.from_user.id) != 1:
            await cb.answer()
            await cb.message.answer("⏳ Доступ ещё не одобрен. Подожди решения админа.")
            return
        return await func(cb)
    return wrapper

# =========================
# ЮЗЕР: /start и меню
# =========================
@dp.message(F.text == "/start")
async def start_handler(message: Message):
    ensure_user_row(message.from_user.id)
    appr = is_approved(message.from_user.id)

    title = "<b>🔐 Добро пожаловать в VPN-бот</b>\n"
    if appr == 1:
        await message.answer(
            f"{title}Выбери действие ниже:",
            reply_markup=user_menu()
        )
        return
    elif appr == -1:
        await message.answer(f"{title}❌ Тебе отказано в доступе.")
        return

    # Ещё не одобрен — отправляем админу карточку
    uname = f"@{message.from_user.username}" if message.from_user.username else str(message.from_user.id)
    await bot.send_message(
        ADMIN_ID,
        f"<b>🔔 Новый запрос на доступ</b>\n"
        f"Пользователь: {uname}\n"
        f"ID: <code>{message.from_user.id}</code>",
        reply_markup=approval_kb(message.from_user.id)
    )
    await message.answer(
        f"{title}⏳ Заявка отправлена администратору. Пожалуйста, подожди одобрения."
    )

@dp.callback_query(F.data.startswith("approve_"))
async def approve_user(cb: CallbackQuery):
    await cb.answer()
    if cb.from_user.id != ADMIN_ID:
        return
    uid = int(cb.data.split("_")[1])
    set_approved(uid, 1)
    await cb.message.edit_text(f"✅ Пользователь <code>{uid}</code> одобрен")
    try:
        await bot.send_message(uid, "✅ Доступ одобрен. Открой меню:", reply_markup=user_menu())
    except:
        pass

@dp.callback_query(F.data.startswith("decline_"))
async def decline_user(cb: CallbackQuery):
    await cb.answer()
    if cb.from_user.id != ADMIN_ID:
        return
    uid = int(cb.data.split("_")[1])
    set_approved(uid, -1)
    await cb.message.edit_text(f"❌ Пользователь <code>{uid}</code> отклонён")
    try:
        await bot.send_message(uid, "❌ Админ отклонил заявку на доступ.")
    except:
        pass

# =========================
# ЮЗЕР: коллбэки
# =========================
@dp.callback_query(F.data == "user_support")
@require_approved
async def cb_user_support(cb: CallbackQuery):
    await cb.answer()
    await cb.message.answer(SUPPORT_TEXT, disable_web_page_preview=True)

@dp.callback_query(F.data == "user_howto")
@require_approved
async def cb_user_howto(cb: CallbackQuery):
    await cb.answer()
    await cb.message.answer(HOWTO_TEXT)

@dp.callback_query(F.data == "user_create")
@require_approved
async def cb_user_create(cb: CallbackQuery):
    await cb.answer()
    uid = cb.from_user.id

    # Один обычный активный конфиг для юзера (админ — без ограничения)
    active = get_active_user_configs(uid)
    has_regular = any(row[3] == 0 for row in active)  # is_trial == 0
    if has_regular and uid != ADMIN_ID:
        await cb.message.answer("⚠️ У тебя уже есть активный конфиг. Больше одного создавать нельзя.")
        return

    user_uuid = str(uuid.uuid4())
    try:
        add_uuid_to_xray(user_uuid)
        create_config_record(uid, user_uuid, days=30, is_trial=False, created_by_admin=(uid == ADMIN_ID))
    except Exception as e:
        await cb.message.answer(f"❌ Ошибка при создании: <code>{e}</code>")
        return

    link = build_vless_link(user_uuid)
    await cb.message.answer(
        "<b>✅ Конфиг создан на 30 дней</b>\n"
        "Твоя персональная ссылка:\n"
        f"<code>{link}</code>"
    )

@dp.callback_query(F.data == "user_trial")
@require_approved
async def cb_user_trial(cb: CallbackQuery):
    await cb.answer()
    uid = cb.from_user.id

    active = get_active_user_configs(uid)
    has_regular = any(row[3] == 0 for row in active)
    if has_regular:
        await cb.message.answer("⚠️ Тест недоступен: у тебя уже есть активный конфиг.")
        return

    has_trial = any(row[3] == 1 for row in active)
    if has_trial:
        await cb.message.answer("⚠️ У тебя уже есть активный тестовый доступ.")
        return

    user_uuid = str(uuid.uuid4())
    try:
        add_uuid_to_xray(user_uuid)
        create_config_record(uid, user_uuid, days=3, is_trial=True, created_by_admin=False)
    except Exception as e:
        await cb.message.answer(f"❌ Ошибка при создании теста: <code>{e}</code>")
        return

    link = build_vless_link(user_uuid)
    await cb.message.answer(
        "<b>🧪 Тестовый доступ на 3 дня активирован</b>\n"
        "Ссылка для подключения:\n"
        f"<code>{link}</code>"
    )

# =========================
# АДМИН: панель и диалоги
# =========================
def reset_admin_state():
    ADMIN_PENDING.pop(ADMIN_ID, None)

@dp.message(F.text == "/admin")
async def admin_handler(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    reset_admin_state()
    await message.answer(
        "<b>⚙️ Админ-панель</b>\nВыбери действие:",
        reply_markup=admin_menu()
    )

@dp.callback_query(F.data == "admin_list")
async def cb_admin_list(cb: CallbackQuery):
    await cb.answer()
    if cb.from_user.id != ADMIN_ID:
        return
    rows = get_all_configs(50)
    if not rows:
        await cb.message.answer("📭 Пока подключений нет.")
        return
    now = datetime.now()
    lines = []
    for cid, uid, uuid_val, exp, is_trial, cba, created_at in rows:
        status = "✅ активен" if datetime.fromisoformat(exp) > now else "⌛ истёк"
        tag = "TRIAL" if is_trial else "REG"
        lines.append(
            f"#{cid} • uid:{uid} • {tag} • до {exp} • {status}\n"
            f"<code>{uuid_val}</code>"
        )
    await cb.message.answer("<b>📜 Последние подключения (до 50)</b>\n\n" + "\n\n".join(lines))

@dp.callback_query(F.data == "admin_extend")
async def cb_admin_extend(cb: CallbackQuery):
    await cb.answer()
    if cb.from_user.id != ADMIN_ID:
        return
    ADMIN_PENDING[ADMIN_ID] = {"action": "extend", "step": 1}
    await cb.message.answer("⏫ Введите <b>ID конфига</b>, который нужно продлить:")

@dp.callback_query(F.data == "admin_delete")
async def cb_admin_delete(cb: CallbackQuery):
    await cb.answer()
    if cb.from_user.id != ADMIN_ID:
        return
    ADMIN_PENDING[ADMIN_ID] = {"action": "delete", "step": 1}
    await cb.message.answer("🗑 Введите <b>ID конфига</b>, который нужно удалить:")

@dp.callback_query(F.data == "admin_create_extra")
async def cb_admin_create_extra(cb: CallbackQuery):
    await cb.answer()
    if cb.from_user.id != ADMIN_ID:
        return
    ADMIN_PENDING[ADMIN_ID] = {"action": "create_extra", "step": 1}
    await cb.message.answer("➕ Укажите <b>USER_ID</b> пользователя, для которого создать доп. конфиг:")

# Шаги диалогов админа + команда /send
@dp.message()
async def admin_dialogs_and_broadcast(message: Message):
    # --- рассылка ---
    if message.from_user.id == ADMIN_ID and message.text and message.text.startswith("/send"):
        parts = message.text.split(maxsplit=1)
        if len(parts) < 2:
            await message.answer("Использование: <code>/send ТЕКСТ</code>")
            return
        text = parts[1]
        cur.execute("SELECT user_id FROM users WHERE approved=1")
        rows = cur.fetchall()
        sent, failed = 0, 0
        for (uid,) in rows:
            try:
                await bot.send_message(uid, f"📢 <b>Сообщение от администратора</b>\n\n{text}")
                sent += 1
                await asyncio.sleep(0.03)  # мягкий троттлинг
            except Exception:
                failed += 1
        await message.answer(f"✅ Рассылка завершена.\nУспешно: {sent}\nОшибок: {failed}")
        return

    # --- диалоги админа ---
    if message.from_user.id != ADMIN_ID:
        return
    if ADMIN_PENDING.get(ADMIN_ID) is None:
        return

    state = ADMIN_PENDING[ADMIN_ID]
    action = state.get("action")
    step = state.get("step", 0)

    # Удалить конфиг
    if action == "delete":
        if step == 1:
            try:
                cid = int(message.text.strip())
            except:
                await message.answer("❗ Введите правильный <b>ID</b> (число).")
                return
            uuid_val = delete_config_record(cid)
            if not uuid_val:
                reset_admin_state()
                await message.answer("Не найден CONFIG_ID.")
                return
            try:
                remove_uuid_from_xray(uuid_val)
                await message.answer(f"🗑 Конфиг <b>#{cid}</b> удалён и отключён.")
            except Exception as e:
                await message.answer(f"❌ Ошибка удаления: <code>{e}</code>")
            reset_admin_state()
            return

    # Продлить конфиг
    if action == "extend":
        if step == 1:
            try:
                cid = int(message.text.strip())
            except:
                await message.answer("❗ Введите правильный <b>ID</b> (число).")
                return
            state["config_id"] = cid
            state["step"] = 2
            await message.answer("На сколько дней продлить? Введите число (например, 30):")
            return
        if step == 2:
            try:
                add_days = int(message.text.strip())
            except:
                await message.answer("❗ Введите правильное число дней.")
                return
            cid = state.get("config_id")
            new_exp = extend_config_record(cid, add_days)
            reset_admin_state()
            if not new_exp:
                await message.answer("Не найден CONFIG_ID.")
                return
            await message.answer(f"⏫ Конфиг <b>#{cid}</b> продлён до <code>{new_exp}</code>.")
            return

    # Создать дополнительный конфиг
    if action == "create_extra":
        if step == 1:
            try:
                tgt = int(message.text.strip())
            except:
                await message.answer("❗ Введите правильный <b>USER_ID</b> (число).")
                return
            state["user_id"] = tgt
            state["step"] = 2
            await message.answer("На сколько дней создать доп. конфиг? Введите число (например, 30):")
            return
        if step == 2:
            try:
                days = int(message.text.strip())
            except:
                await message.answer("❗ Введите правильное число дней.")
                return
            tgt = state.get("user_id")
            reset_admin_state()

            user_uuid = str(uuid.uuid4())
            try:
                cur.execute("INSERT OR IGNORE INTO users(user_id) VALUES(?)", (tgt,))
                conn.commit()
                add_uuid_to_xray(user_uuid)
                create_config_record(tgt, user_uuid, days=days, is_trial=False, created_by_admin=True)
            except Exception as e:
                await message.answer(f"❌ Ошибка создания: <code>{e}</code>")
                return

            link = build_vless_link(user_uuid)
            await message.answer(
                f"➕ Доп. конфиг для <b>{tgt}</b> на <b>{days}</b> дн. создан:\n<code>{link}</code>"
            )
            try:
                await bot.send_message(
                    tgt,
                    f"👋 Админ создал для тебя дополнительный конфиг на {days} дн.:\n<code>{link}</code>"
                )
            except:
                pass
            return

# =========================
# КРОН: автоотключение истёкших
# =========================
async def check_expired_loop():
    while True:
        now = datetime.now()
        try:
            cur.execute("SELECT id, user_id, uuid, expires_at FROM configs")
            for cid, uid, uuid_val, exp in cur.fetchall():
                try:
                    if datetime.fromisoformat(exp) <= now:
                        remove_uuid_from_xray(uuid_val)
                        cur.execute("DELETE FROM configs WHERE id=?", (cid,))
                        conn.commit()
                        try:
                            await bot.send_message(uid, "⌛ Срок действия твоего конфига истёк. Доступ отключён.")
                        except:
                            pass
                except Exception as e:
                    print("expire-check item error:", e)
        except Exception as e:
            print("expire-check cycle error:", e)

        await asyncio.sleep(3600)  # раз в час

# =========================
# MAIN
# =========================
async def main():
    asyncio.create_task(check_expired_loop())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())