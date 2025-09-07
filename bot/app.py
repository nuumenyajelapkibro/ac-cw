from __future__ import annotations

import os
import asyncio
import logging
from typing import Any, Dict, Iterable
from urllib.parse import urlparse

from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Message
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web
import httpx

from aiogram.client.default import DefaultBotProperties

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# --- ENV ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
# Prefer ORCHESTRATOR_URL, fallback to legacy ORCH_URL, then default to service hostname
ORCHESTRATOR_URL = (
    os.getenv("ORCHESTRATOR_URL")
    or os.getenv("ORCH_URL")
    or "http://asb-orchestrator:8000"
)
BASE_URL = os.getenv("BASE_URL")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
APP_ENV = os.getenv("APP_ENV", "prod")
ORCH_CLIENT_TIMEOUT = float(os.getenv("ORCH_CLIENT_TIMEOUT", "200.0"))

if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN is not set")

# Derive webhook path from WEBHOOK_URL (or fallback)
def _derive_webhook_path(url: str | None) -> str:
    if not url:
        return f"/webhook/{TELEGRAM_TOKEN}"
    return urlparse(url).path or f"/webhook/{TELEGRAM_TOKEN}"

WEBHOOK_PATH = _derive_webhook_path(WEBHOOK_URL)

# If WEBHOOK_URL is not explicitly provided but BASE_URL exists, build it
if not WEBHOOK_URL and BASE_URL:
    WEBHOOK_URL = f"{BASE_URL}{WEBHOOK_PATH}"

bot = Bot(
    token=TELEGRAM_TOKEN,
    default=DefaultBotProperties(parse_mode="HTML")
)
dp = Dispatcher()


# ------------- helpers -------------
TG_MESSAGE_LIMIT = 4096

async def _post_json(path: str, json: Dict[str, Any]) -> Dict[str, Any]:
    async with httpx.AsyncClient(timeout=ORCH_CLIENT_TIMEOUT) as client:
        resp = await client.post(f"{ORCHESTRATOR_URL}{path}", json=json)
        if resp.status_code == 409:
            detail = resp.json().get("detail", "Конфликт состояний")
            raise RuntimeError(f"409: {detail}")
        resp.raise_for_status()
        return resp.json()

async def _get(path: str, params: Dict[str, Any]) -> Dict[str, Any]:
    async with httpx.AsyncClient(timeout=ORCH_CLIENT_TIMEOUT) as client:
        resp = await client.get(f"{ORCHESTRATOR_URL}{path}", params=params)
        if resp.status_code == 409:
            detail = resp.json().get("detail", "Конфликт состояний")
            raise RuntimeError(f"409: {detail}")
        resp.raise_for_status()
        return resp.json()

def _err_text(detail: str) -> str:
    return f"⚠️ <b>Ошибка:</b> {detail}"

def _chunk(text: str, size: int = TG_MESSAGE_LIMIT) -> Iterable[str]:
    # сохранение границ строк где возможно
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            nl = text.rfind("\n", start, end)
            if nl != -1 and nl > start + size // 2:
                end = nl
        yield text[start:end]
        start = end

async def send_long(message: Message, text: str):
    for part in _chunk(text):
        await message.answer(part)


# ------------- commands -------------

@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        "Привет! Я <b>AI Study Buddy</b> 🤖\n"
        "Команды:\n"
        "• /study &lt;тема&gt; — создать план\n"
        "• /summary — конспект по текущей теме\n"
        "• /quiz — квиз 5–7 вопросов\n"
        "• /progress — прогресс"
    )

@dp.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "Подсказка:\n"
        "• /study Машинное обучение — начнём план\n"
        "• /summary — верну конспект\n"
        "• /quiz — запущу квиз\n"
        "• /progress — покажу прогресс"
    )

@dp.message(Command("study"))
async def cmd_study(message: Message):
    text = (message.text or "").strip()
    parts = text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Укажи тему: <code>/study квантовые вычисления</code>")
        return

    topic = parts[1]
    user_id = message.from_user.id

    payload = {
        "topic": topic,
        "depth": "basic",
        "duration_days": 7,
        "daily_time_minutes": 20,
        "user_id": user_id,
    }

    await message.answer("🧠 Планирую обучение…")
    try:
        data = await _post_json("/study", payload)
        doc_url = data.get("doc_url") or "—"
        await message.answer(
            f"✅ План готов!\n"
            f"Документ: {doc_url}\n\n"
            f"Теперь можно вызвать /summary или /quiz"
        )
    except RuntimeError as e:
        if str(e).startswith("409:"):
            await message.answer(_err_text(str(e)[5:]))
        else:
            await message.answer(_err_text(str(e)))
    except httpx.HTTPError as e:
        await message.answer(_err_text(f"Оркестратор недоступен: {e}"))
    except Exception as e:
        log.exception("study failed")
        await message.answer(_err_text(f"Непредвиденная ошибка: {e}"))

@dp.message(Command("summary"))
async def cmd_summary(message: Message):
    user_id = message.from_user.id
    try:
        data = await _get("/summary", {"user_id": user_id})
        md = data.get("markdown", "")
        if not md:
            await message.answer("Конспект пока пуст.")
            return
        # Отправим как pre: это безопасно и читабельно
        await send_long(message, "📝 <b>Конспект</b>:\n\n<pre>" + md + "</pre>")
    except RuntimeError as e:
        if str(e).startswith("409:"):
            await message.answer(_err_text(str(e)[5:]))
        else:
            await message.answer(_err_text(str(e)))
    except httpx.HTTPError as e:
        await message.answer(_err_text(f"Оркестратор недоступен: {e}"))

@dp.message(Command("quiz"))
async def cmd_quiz(message: Message):
    user_id = message.from_user.id
    try:
        data = await _get("/quiz", {"user_id": user_id, "questions_count": 6})
        qs = data.get("questions", [])
        if not qs:
            await message.answer("Пока не удалось получить вопросы.")
            return
        blocks = []
        for i, q in enumerate(qs, 1):
            opts = q.get("options", [])
            block = [
                f"<b>Вопрос {i}:</b> {q.get('q')}",
                *(f"{j+1}) {opt}" for j, opt in enumerate(opts[:4]))
            ]
            blocks.append("\n".join(block))
        await send_long(message, "🎯 <b>Квиз</b>\n\n" + "\n\n".join(blocks))
        await message.answer(
            "Когда будешь готов(а), пришли команду вида: "
            "<code>/quiz_result тема|правильных|всего</code>\n"
            "Например: <code>/quiz_result ML|4|6</code>"
        )
    except RuntimeError as e:
        if str(e).startswith("409:"):
            await message.answer(_err_text(str(e)[5:]))
        else:
            await message.answer(_err_text(str(e)))
    except httpx.HTTPError as e:
        await message.answer(_err_text(f"Оркестратор недоступен: {e}"))

@dp.message(Command("quiz_result"))
async def cmd_quiz_result(message: Message):
    text = (message.text or "").strip()
    parts = text.split(maxsplit=1)
    if len(parts) < 2 or "|" not in parts[1]:
        await message.answer("Формат: <code>/quiz_result тема|правильных|всего</code>")
        return

    try:
        topic, correct_s, total_s = parts[1].split("|")
        payload = {
            "topic": topic.strip(),
            "correct": int(correct_s),
            "total": int(total_s),
            "weak_topics": [],
        }
    except Exception:
        await message.answer("Не удалось разобрать параметры. Пример: <code>/quiz_result ML|4|6</code>")
        return

    user_id = message.from_user.id
    try:
        await _post_json("/quiz/result", {"user_id": user_id, **payload})
        await message.answer("✅ Результат сохранён. Можно продолжать в /summary или /quiz")
    except RuntimeError as e:
        if str(e).startswith("409:"):
            await message.answer(_err_text(str(e)[5:]))
        else:
            await message.answer(_err_text(str(e)))
    except httpx.HTTPError as e:
        await message.answer(_err_text(f"Оркестратор недоступен: {e}"))

@dp.message(Command("progress"))
async def cmd_progress(message: Message):
    user_id = message.from_user.id
    try:
        data = await _get("/progress", {"user_id": user_id})
        doc = data.get("doc_url") or "—"
        await message.answer(
            "📈 <b>Прогресс</b>\n"
            f"Готово: {data.get('completion_percent', 0)}%\n"
            f"Средний балл: {data.get('avg_score', 0)}\n"
            f"Слабые темы: {', '.join(data.get('weak_topics', [])) or '—'}\n"
            f"Документ: {doc}"
        )
    except RuntimeError as e:
        if str(e).startswith("409:"):
            await message.answer(_err_text(str(e)[5:]))
        else:
            await message.answer(_err_text(str(e)))
    except httpx.HTTPError as e:
        await message.answer(_err_text(f"Оркестратор недоступен: {e}"))


# ------------- webhook server -------------

async def on_startup(app: web.Application):
    # Регистрируем вебхук у Telegram, если задан WEBHOOK_URL
    if WEBHOOK_URL:
        await bot.set_webhook(WEBHOOK_URL, drop_pending_updates=True)
        log.info("Webhook set to %s", WEBHOOK_URL)
    else:
        # Локальный режим без внешнего URL — на всякий случай чистим вебхук
        await bot.delete_webhook(drop_pending_updates=True)
        log.info("Webhook deleted (no WEBHOOK_URL).")

async def on_cleanup(app: web.Application):
    await bot.session.close()

def build_app() -> web.Application:
    app = web.Application()
    # Регистрируем обработчик на путь WEBHOOK_PATH
    SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, on_startup=on_startup, on_shutdown=None, on_cleanup=on_cleanup)
    return app


# ------------- entrypoint -------------

if __name__ == "__main__":
    # Локальный dev: можно запустить aiohttp-сервер (за прокси отвечает Caddy/Nginx)
    port = int(os.getenv("PORT", "8080"))
    web.run_app(build_app(), host="0.0.0.0", port=port)
