# -*- coding: utf-8 -*-
"""
error_handler.py – универсальный обработчик ошибок для python‑telegram‑bot.

Подключается в bot.py:

    from error_handler import universal_error_handler
    app.add_error_handler(universal_error_handler)

"""

import os
import logging
import traceback
import asyncio
from telegram import Update
from telegram.ext import ContextTypes
from telegram.error import Conflict, TelegramError

# ----------------------------------------------------------------------
logger = logging.getLogger(__name__)          # будет наследовать конфиг из bot.py
# ----------------------------------------------------------------------


async def universal_error_handler(
    update: Update | None,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """
    Обрабатывает любые исключения, возникшие в обработчиках бота.
    • Conflict – обычное для Render‑free‑service, просто игнорируем.
    • TelegramError – логируем как ошибку бота.
    • Всё остальное – логируем полную трассировку и (по желанию) оповещаем админа.
    """
    exc = context.error                       # тип – Exception
    tb_str = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))

    # --------------------------------------------------------------
    # 1️⃣ Конфликт getUpdates (одновременно запущено 2+ процесса)
    # --------------------------------------------------------------
    if isinstance(exc, Conflict):
        # Это обычный «мельтешный» конфликт в Render, когда старый процесс
        # ещё держит запрос, а уже запущен новый.
        logger.warning(
            "⚠️ Conflict while getUpdates – another instance is probably still "
            "running. Ignoring and will retry. Details:\n%s",
            tb_str,
        )
        # Пауза даёт старому процессу шанс корректно завершиться.
        await asyncio.sleep(2)
        return  # НЕ пробрасываем дальше → polling продолжится

    # --------------------------------------------------------------
    # 2️⃣ Ошибки Telegram API (таймауты, 429, BadRequest и т.п.)
    # --------------------------------------------------------------
    if isinstance(exc, TelegramError):
        logger.error(
            "❌ TelegramError: %s\n%s",
            getattr(exc, "message", "<no message>"),
            tb_str,
        )
    else:
        # --------------------------------------------------------------
        # 3️⃣ Любые другие (код‑баги, ошибки в наших функциях)
        # --------------------------------------------------------------
        logger.error("🚨 Unhandled exception in handler:\n%s", tb_str)

    # --------------------------------------------------------------
    # 4️⃣ Оповещение администратора (по желанию)
    # --------------------------------------------------------------
    admin_id = int(os.getenv("ADMIN_ID", "0"))
    if admin_id:
        try:
            await context.bot.send_message(
                chat_id=admin_id,
                text=f"🚨 <b>Bot error</b>:\n<pre>{tb_str}</pre>",
                parse_mode="HTML",
            )
        except Exception as send_exc:
            # Если не удалось отправить – просто залогируем.
            logger.error("Failed to notify admin about the error: %s", send_exc)
