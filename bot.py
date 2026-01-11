# -*- coding: utf-8 -*-
"""
Кабак – Telegram‑бот для многопользовательской игры.
"""

# ──────────────────────  Библиотеки  ──────────────────────
import os
import json
import random
import datetime
import pathlib
import logging
import html
import asyncio
from functools import wraps

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ──────────────────────  Логирование  ──────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# ──────────────────────  Конфигурация  ──────────────────────
BASE = pathlib.Path(__file__).parent
PHOTOS_DIR = BASE / "places"
RULES_PATH = BASE / "rules.txt"

load_dotenv(BASE / ".env")          # .env → BOT_TOKEN, ADMIN_ID
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

CFG_PATH = BASE / "config.json"
STATE_PATH = BASE / "game_state.json"

# Список слов (нормализуется к нижнему регистру)
WORDS = {
    w.strip().lower()
    for w in (BASE / "New_rus.txt").read_text(encoding="utf-8").split()
    if w.strip()
}

# Текст правил – разбит на сообщения по 4096 символов
if RULES_PATH.is_file():
    raw = RULES_PATH.read_text(encoding="utf-8")
    RULES_CHUNKS = [raw[i : i + 4000] for i in range(0, len(raw), 4000)]
else:
    RULES_CHUNKS = [
        "📜 *Правила пока не заданы.*\n"
        "Создайте файл `rules.txt` рядом с `bot.py`, чтобы добавить правила."
    ]

# ──────────────────────  Утилиты  ──────────────────────
def load_json(path: pathlib.Path) -> dict:
    """Читает JSON‑файл, при отсутствии создаёт пустой словарь."""
    return json.load(path.open(encoding="utf-8")) if path.is_file() else {}


def save_state(state: dict) -> None:
    """Атомарно сохраняет состояние игры."""
    tmp = STATE_PATH.with_suffix(".tmp")
    json.dump(state, tmp.open("w", encoding="utf-8"), ensure_ascii=False, indent=2)
    tmp.replace(STATE_PATH)


def compare_word(guess: str, target: str) -> list[str]:
    """Возвращает список цветов (green, yellow, gray) для сравнения guess‑target."""
    guess, target = guess.upper(), target.upper()
    res = ["gray"] * len(guess)
    remaining = []

    for i, ch in enumerate(guess):
        if ch == target[i]:
            res[i] = "green"
        else:
            remaining.append(target[i])

    for i, ch in enumerate(guess):
        if res[i] == "gray" and ch in remaining:
            res[i] = "yellow"
            remaining.remove(ch)
    return res


def score_from(colours: list[str]) -> int:
    """Считает очки: 10 за green, 5 за yellow, 0 за gray."""
    return sum(10 if c == "green" else 5 if c == "yellow" else 0 for c in colours)


# ──────────────────────  Ответ клиенту  ──────────────────────
async def reply(update: Update, text: str, **kwargs) -> None:
    """Отправляет сообщение, независимо от типа `update`."""
    if update.effective_message:
        await update.effective_message.reply_text(text, **kwargs)
    elif update.callback_query:
        await update.callback_query.answer(text, show_alert=True)
    else:
        logger.warning("reply() called without a target: %s", text)


# ──────────────────────  Декораторы  ──────────────────────
def admin_only(func):
    """Разрешает вызов функции только администратору."""
    @wraps(func)
    async def wrapper(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if upd.effective_user.id != ctx.bot_data["admin_id"]:
            await reply(upd, "❌ Доступ только у админа")
            return
        return await func(upd, ctx)

    return wrapper


def active_player(func):
    """Требует, чтобы пользователь был в игре и игра запущена."""
    @wraps(func)
    async def wrapper(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
        uid = str(upd.effective_user.id)
        if uid not in ctx.bot_data["players"]:
            await reply(upd, "❗️ Сначала /join")
            return
        if not ctx.bot_data.get("state_game_active"):
            await reply(upd, "⚠️ Игра ещё не началась")
            return
        return await func(upd, ctx)

    return wrapper


def player_or_admin(func):
    """
    Позволяет вызвать функцию либо администратору,
    либо обычному игроку (в проверке используется ``active_player``).
    """
    @wraps(func)
    async def wrapper(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if upd.effective_user.id == ctx.bot_data["admin_id"]:
            return await func(upd, ctx)

        # fallback к проверке active_player
        uid = str(upd.effective_user.id)
        if uid not in ctx.bot_data["players"]:
            await reply(upd, "❗️ Сначала /join")
            return
        if not ctx.bot_data.get("state_game_active"):
            await reply(upd, "⚠️ Игра ещё не началась")
            return
        return await func(upd, ctx)

    return wrapper


# ──────────────────────  Очередь и уведомления  ──────────────────────
def advance_queue(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Перемещает текущего игрока в конец очереди и уведомляет следующего."""
    q = ctx.bot_data["queue"]
    if not q:
        return
    q.append(q.pop(0))
    ctx.bot_data["state_game_active"] = True
    next_uid = q[0]
    username = ctx.bot_data["players"][next_uid]["username"]
    ctx.application.create_task(
        ctx.application.bot.send_message(
            chat_id=int(next_uid),
            text=f"⏳ Ваш ход, @{username}! /menu – ваши возможности",
        )
    )


def notify_current_player(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Отправляет личное сообщение текущему игроку (не меняя очередь)."""
    if not ctx.bot_data.get("queue"):
        return
    uid = ctx.bot_data["queue"][0]
    username = ctx.bot_data["players"][uid]["username"]
    ctx.application.create_task(
        ctx.application.bot.send_message(
            chat_id=int(uid),
            text=f"⏳ Ваш ход, @{username}! /menu – ваши возможности",
        )
    )


def check_inactivity(uid: str, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Если игрок был неактивен >7 дней – отправляем предупреждение."""
    player = ctx.bot_data["players"].get(uid)
    if not player:
        return
    last = datetime.datetime.fromisoformat(player["last_active"])
    if datetime.datetime.utcnow() - last > datetime.timedelta(days=7):
        ctx.application.create_task(
            ctx.application.bot.send_message(
                chat_id=int(uid),
                text=(
                    "⚠️ Вы не делали ход более недели. "
                    "Появится риск исключения из игры."
                ),
            )
        )


# ──────────────────────  Таблица лидеров  ──────────────────────
def format_score_table(ctx: ContextTypes.DEFAULT_TYPE) -> str:
    """Возвращает таблицу лидеров в виде markdown‑блока."""
    players = ctx.bot_data["players"]
    if not players:
        return "⚙️ Пока никто не играет"

    sorted_players = sorted(players.values(), key=lambda p: p["score"], reverse=True)
    cur_uid = ctx.bot_data["queue"][0] if ctx.bot_data.get("state_game_active") else None

    name_w = max(len(p["username"]) for p in sorted_players)
    score_w = max(len(str(p["score"])) for p in sorted_players)

    lines = []
    for p in sorted_players:
        marker = "⏳ " if cur_uid and p["username"] == ctx.bot_data["players"][cur_uid]["username"] else "   "
        lines.append(
            f"{p['username'].ljust(name_w)}  {str(p['score']).rjust(score_w)} {marker}"
        )
    return "```\n" + "\n".join(lines) + "\n```"


# ──────────────────────  Команды  ──────────────────────
async def start(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Приветственное сообщение и список команд."""
    await reply(
        upd,
        "👋 Привет! Я — бот‑помощник в игре *Кабак*.\n"
        "/join – присоединиться к игре\n"
        "/begin – (только админ) запустить партию\n"
        "/menu – открыть главное меню\n"
        "/rules – посмотреть правила",
        parse_mode="Markdown",
    )


async def join(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Добавляет пользователя в список игроков и очередь (админ исключён)."""
    uid = str(upd.effective_user.id)

    # админ не участвует в игре
    if upd.effective_user.id == ctx.bot_data["admin_id"]:
        await reply(upd, "❌ Администратор не участвует в игре")
        return

    if uid in ctx.bot_data["players"]:
        await reply(upd, "✅ Вы уже в игре")
        return

    ctx.bot_data["players"][uid] = {
        "username": upd.effective_user.username or upd.effective_user.full_name,
        "score": 0,
        "last_active": datetime.datetime.utcnow().isoformat(),
    }
    ctx.bot_data["queue"].append(uid)
    await reply(upd, "🤝 Вы присоединились к партии!")


@admin_only
async def begin(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Инициализирует игру: перемешивает очередь, загружает здания."""
    players = list(ctx.bot_data["players"].keys())
    if not players:
        await reply(upd, "❌ Нет игроков")
        return

    random.shuffle(players)
    ctx.bot_data["queue"] = players

    state = load_json(STATE_PATH)
    cfg = load_json(CFG_PATH)

    # создаём список зданий, если его нет в состоянии
    if not state.get("buildings"):
        state["buildings"] = [
            {"id": b["id"], "last_attempt": None, "is_closed": False}
            for b in cfg.get("buildings", [])
        ]
    state["queue"], state["game_state"] = players, "active"
    save_state(state)

    ctx.bot_data["state_game_active"] = True
    first = ctx.bot_data["players"][players[0]]["username"]
    await reply(upd, f"🚀 Игра началась! Ходит @{first}.")

    # личное сообщение первому игроку
    notify_current_player(ctx)


async def menu(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    Главное меню.

    • Администратор – только карта и таблица лидеров.
    • Обычный игрок – стандартные игровые кнопки.
    """
    uid = str(upd.effective_user.id)
    is_admin = upd.effective_user.id == ctx.bot_data["admin_id"]

    # ---------- администратор ----------
    if is_admin:
        rows = [
            [InlineKeyboardButton("🗺️ Карта", callback_data="show_board")],
            [InlineKeyboardButton("📊 Таблица", callback_data="score")],
        ]
        await reply(upd, "📋 Главное меню (админ):", reply_markup=InlineKeyboardMarkup(rows))
        return

    # ---------- обычный игрок ----------
    if uid not in ctx.bot_data["players"]:
        await reply(upd, "❗️ Сначала /join")
        return
    if not ctx.bot_data.get("state_game_active"):
        await reply(upd, "⚠️ Игра ещё не началась")
        return

    is_my_turn = ctx.bot_data["queue"] and ctx.bot_data["queue"][0] == uid

    # условия для кнопки «Грабить»
    others = any(pl != uid for pl in ctx.bot_data["players"])
    my_balance = ctx.bot_data["players"][uid]["score"]
    rich_other = any(p["score"] >= 10 for pid, p in ctx.bot_data["players"].items() if pid != uid)
    can_steal = is_my_turn and others and my_balance >= 2 and rich_other

    # кнопки игрока
    if is_my_turn:
        rows = [[InlineKeyboardButton("💡 Угадать слово", callback_data="guess")]]
        if can_steal:
            rows.append([InlineKeyboardButton("💰 Грабить", callback_data="steal")])
    else:
        rows = [[InlineKeyboardButton("🕒 Ожидаю ход", callback_data="wait")]]

    rows += [
        [InlineKeyboardButton("🗺️ Карта", callback_data="show_board")],
        [InlineKeyboardButton("📊 Таблица", callback_data="score")],
    ]

    await reply(upd, "📋 Главное меню:", reply_markup=InlineKeyboardMarkup(rows))


async def score(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    Таблица лидеров.

    Администратор может увидеть её до начала игры,
    обычные игроки – только после старта.
    """
    if not ctx.bot_data.get("state_game_active"):
        if upd.effective_user.id != ctx.bot_data["admin_id"]:
            await reply(upd, "⚠️ Игра ещё не началась")
            return
    await reply(upd, format_score_table(ctx), parse_mode="Markdown")


@player_or_admin
async def show_board(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Кнопка «🗺️ Карта» – список всех зданий (доступна админа и игрокам)."""
    cfg = load_json(CFG_PATH)

    rows = [
        [
            InlineKeyboardButton(
                f"{b['name']} (ID {b['id']})",
                callback_data=f"building:{b['id']}",
            )
        ]
        for b in cfg.get("buildings", [])
    ]
    rows.append([InlineKeyboardButton("↩️ В меню", callback_data="menu")])

    await upd.callback_query.message.reply_text(
        "Выберите место:",
        reply_markup=InlineKeyboardMarkup(rows),
    )
    await upd.callback_query.answer()


@player_or_admin
async def building_info(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Отправляет информацию о выбранном здании (фото + подпись)."""
    query = upd.callback_query
    try:
        b_id = int(query.data.split(":")[1])
    except (IndexError, ValueError):
        await query.answer("❌ Некорректный запрос", show_alert=True)
        return

    cfg = load_json(CFG_PATH)
    st = load_json(STATE_PATH)

    building = next((b for b in cfg.get("buildings", []) if b["id"] == b_id), None)
    if not building:
        await query.answer("🏚️ Здание не найдено", show_alert=True)
        return

    dyn = next((d for d in st.get("buildings", []) if d["id"] == b_id), {})

    # базовое описание
    caption = f"<b>{html.escape(building['name'])}</b>\n{html.escape(building['story_text'])}"

    # статус закрытого здания
    if dyn.get("is_closed"):
        caption = "🔒 Здание закрыто\n" + caption

    # админ‑вид (загаданное слово)
    if upd.effective_user.id == ctx.bot_data["admin_id"]:
        caption += f"\n\n🔑 <b>Загаданное слово:</b> <code>{html.escape(building['target_word'])}</code>"

    # последняя попытка
    last = dyn.get("last_attempt")
    if last:
        dt = datetime.datetime.fromisoformat(last["time"]).strftime("%d.%m.%Y %H:%M")
        verdict = "".join(
            "🟩" if c == "green" else "🟨" if c == "yellow" else "⬜"
            for c in compare_word(last["word"], building["target_word"])
        )
        caption += (
            f"\n\n❗️ Последняя попытка:\n"
            f"👤 @{html.escape(last['username'])}\n"
            f"🕒 {dt}\n"
            f"🗣️ <code>{html.escape(last['word'])}</code>\n"
            f"{verdict}"
        )
    else:
        caption += "\n\n❗️ Последних попыток нет."

    # отправка фото (если есть) или только текста
    photo_name = building.get("photo_file")
    if photo_name and (PHOTOS_DIR / photo_name).is_file():
        await query.message.reply_photo(
            photo=str(PHOTOS_DIR / photo_name),
            caption=caption,
            parse_mode="HTML",
        )
    else:
        await query.message.reply_text(caption, parse_mode="HTML")

    await query.answer()


@active_player
async def handle_guess_word(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает ввод слова после выбора здания."""
    if "guess_building_id" not in ctx.user_data:
        return

    uid = str(upd.effective_user.id)

    # проверка очереди
    if ctx.bot_data["queue"][0] != uid:
        await reply(upd, "⚠️ Ожидайте своей очереди.")
        return

    b_id = ctx.user_data.pop("guess_building_id")
    word = upd.message.text.strip().upper()

    # проверка длины и наличия в словаре
    if len(word) != 5:
        await reply(upd, "❗️ Слово должно быть ровно из 5 букв.")
        return
    if word.lower() not in WORDS:
        await reply(upd, "❗️ Слова нет в словаре.")
        return

    cfg = load_json(CFG_PATH)
    building = next((b for b in cfg.get("buildings", []) if b["id"] == b_id), None)
    if not building:
        await reply(upd, "🏚️ Здание не найдено")
        return

    state = load_json(STATE_PATH)
    dyn = next((d for d in state.get("buildings", []) if d["id"] == b_id), {})

    # повторная попытка
    last = dyn.get("last_attempt")
    if last and last["word"].upper() == word:
        await reply(upd, "❗️ Вы уже пробовали это слово.")
        return

    # закрытое здание
    if dyn.get("is_closed"):
        await reply(upd, "🔒 Здание уже закрыто")
        return

    # сравнение и начисление очков
    colours = compare_word(word, building["target_word"])
    points = score_from(colours)

    player = ctx.bot_data["players"][uid]
    player["score"] += points
    player["last_active"] = datetime.datetime.utcnow().isoformat()
    dyn["last_attempt"] = {
        "user_id": uid,
        "username": player["username"],
        "time": datetime.datetime.utcnow().isoformat(),
        "word": word,
    }

    visual = "".join(
        "🟩" if c == "green" else "🟨" if c == "yellow" else "⬜"
        for c in colours
    )

    if word.upper() == building["target_word"].upper():
        dyn["is_closed"] = True
        await reply(
            upd,
            f"🎉 Вы полностью отгадали **{building['target_word']}**!\n{visual}",
            parse_mode="Markdown",
        )
    else:
        await reply(upd, f"{visual}\n+{points} 🪙")

    # сохраняем состояние и передаём ход
    state["buildings"] = [
        d if d["id"] != b_id else dyn for d in state.get("buildings", [])
    ]
    save_state(state)
    advance_queue(ctx)
    await menu(upd, ctx)


@active_player
async def steal_target_kb(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Показывает список потенциальных жертв для грабежа."""
    uid = str(upd.effective_user.id)

    other_players = [
        (pid, p) for pid, p in ctx.bot_data["players"].items() if pid != uid
    ]

    rows = [
        [InlineKeyboardButton(f"@{p['username']}", callback_data=f"steal:{pid}")]
        for pid, p in other_players
    ]
    rows.append([InlineKeyboardButton("↩️ В меню", callback_data="menu")])

    await upd.callback_query.message.reply_text(
        "Выберите цель грабежа:",
        reply_markup=InlineKeyboardMarkup(rows),
    )
    await upd.callback_query.answer()


@active_player
async def steal_handler(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает грабёж: бросок кубика, перенос монет, смена хода."""
    uid = str(upd.effective_user.id)
    data = upd.callback_query.data

    # нажата сама кнопка «Грабить» – показываем список целей
    if data == "steal":
        if ctx.bot_data["queue"][0] != uid:
            await reply(upd, "⚠️ Грабить можно только в свой ход")
            await upd.callback_query.answer()
            return
        await steal_target_kb(upd, ctx)
        return

    # выбран конкретный игрок
    if data.startswith("steal:"):
        target_id = data.split(":")[1]
        victim = ctx.bot_data["players"].get(target_id)
        thief = ctx.bot_data["players"][uid]

        if not victim:
            await upd.callback_query.answer("❌ Игрок не найден", show_alert=True)
            return

        dice = random.randint(1, 6)
        if dice >= 5:                      # успех
            thief["score"] += 10
            victim["score"] -= 10
            outcome = f"🎲 Выпало {dice}. Удача на вашей стороне! Плюс 10 очков"
            note = f"🚨 @{thief['username']} грабит вас! Минус 10 очков"
        else:                               # неудача
            thief["score"] -= 2
            victim["score"] += 2
            outcome = f"🎲 Выпало {dice}. Не повезло. Минус 2 очка"
            note = f"🚨 @{thief['username']} попытался вас ограбить, но вы не из робкого десятка! Плюс 2 очка"

        # уведомляем жертву
        ctx.application.create_task(
            ctx.application.bot.send_message(chat_id=int(target_id), text=note)
        )

        await reply(upd, outcome)
        advance_queue(ctx)
        await menu(upd, ctx)
        await upd.callback_query.answer()
        return

    await upd.callback_query.answer()


@admin_only
async def reset_game(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Полностью очищает состояние игры (только админ)."""
    if STATE_PATH.is_file():
        STATE_PATH.unlink()
    ctx.bot_data["players"] = {}
    ctx.bot_data["queue"] = []
    ctx.bot_data["state_game_active"] = False
    await reply(upd, "🔄 Игра сброшена. /join – снова в игру")


async def rules_command(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    for chunk in RULES_CHUNKS:
        await reply(upd, chunk, parse_mode="Markdown")
        await asyncio.sleep(0.2)

    map_path = BASE / "MapNewYork.png"
    if map_path.is_file():
        await ctx.application.bot.send_photo(
            chat_id=upd.effective_chat.id,
            photo=str(map_path),
            caption="🗺️ Карта игрового города",
        )
    else:
        logger.warning("Файл карты не найден: %s", map_path)
        await reply(upd, "⚠️ Картинка карты не найдена.")


# ──────────────────────  Callback‑handler  ──────────────────────
async def callback_handler(upd: Update, ctx: ContextTypes.DEFAULT_TYPE):
    data = upd.callback_query.data

    # если пользователь переключился из режима «угадывать слово», сбрасываем маркер
    if ctx.user_data.get("guess_building_id") and not (data in ("guess",) or data.startswith("guess:")):
        ctx.user_data.pop("guess_building_id", None)

    handlers = {
        "reset_game": reset_game,
        "menu": menu,
        "score": score,
        "show_board": show_board,
        "steal": steal_handler,
        "building": building_info,
    }

    # ---------- УГАДЫВАНИЕ ----------
    if data == "guess":
        cfg = load_json(CFG_PATH)
        state = load_json(STATE_PATH)
        rows = []
        for b in cfg.get("buildings", []):
            dyn = next((d for d in state.get("buildings", []) if d["id"] == b["id"]), {})
            if dyn.get("is_closed"):
                continue
            rows.append(
                [InlineKeyboardButton(f"{b['name']} (ID {b['id']})", callback_data=f"guess:{b['id']}")]
            )
        if not rows:
            await reply(upd, "❌ Все здания уже закрыты")
            await upd.callback_query.answer()
            return
        rows.append([InlineKeyboardButton("↩️ В меню", callback_data="menu")])
        await upd.callback_query.message.reply_text(
            "Выберите место для угадывания:",
            reply_markup=InlineKeyboardMarkup(rows),
        )
        await upd.callback_query.answer()
        return

    if data.startswith("guess:"):
        ctx.user_data["guess_building_id"] = int(data.split(":")[1])
        await upd.callback_query.message.reply_text(
            "Введите слово (5 букв):",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("↩️ В меню", callback_data="menu")]]
            ),
        )
        await upd.callback_query.answer()
        return

    # ---------- ГРАБЁЖ ----------
    if data.startswith("steal:"):
        await steal_handler(upd, ctx)
        return

    # ---------- ЗДАНИЕ ----------
    if data.startswith("building:"):
        await building_info(upd, ctx)
        await upd.callback_query.answer()
        return

    # ---------- ОСТАЛЬНОЕ ----------
    if data in handlers:
        await handlers[data](upd, ctx)
        await upd.callback_query.answer()
        return

    # неизвестный запрос – просто answer, чтобы Telegram не ругался
    await upd.callback_query.answer()


# ──────────────────────  Точка входа  ──────────────────────
def main() -> None:
    """Запуск бота."""
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # глобальные данные
    app.bot_data["admin_id"] = ADMIN_ID
    app.bot_data["players"] = {}
    app.bot_data["queue"] = []
    app.bot_data["state_game_active"] = False

    # команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("join", join))
    app.add_handler(CommandHandler("begin", begin))
    app.add_handler(CommandHandler("menu", menu))
    app.add_handler(CommandHandler("score", score))
    app.add_handler(CommandHandler("rules", rules_command))

    # обработчики
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_guess_word))

    logger.info("✅ Бот запущен")
    app.run_polling()


if __name__ == "__main__":
    main()
