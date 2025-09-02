import asyncio
from aiohttp import web
from aiogram import Bot, Dispatcher, Router
from aiogram.filters import Command
from aiogram.types import Message
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiogram.client.default import DefaultBotProperties 
from settings import settings
import httpx

router = Router()

@router.message(Command("start"))
async def start_cmd(m: Message):
    await m.answer(
        "Привет, теперь мы умеем в CI/CD! Я на вебхуке. Команды: /study &lt;тема&gt;, /summary, /quiz, /progress"
    )

@router.message(Command("study"))
async def study_cmd(m: Message):
    topic = (m.text or "").split(maxsplit=1)
    topic = topic[1].strip() if len(topic) > 1 else ""
    if not topic:
        await m.answer("Укажи тему: /study <тема>")
        return
    payload = {
        "topic": topic,
        "depth": "beginner",
        "duration_days": 7,
        "daily_time_minutes": 30,
        "telegram_user_id": m.from_user.id,
    }
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(f"{settings.ORCH_URL}/study", json=payload)
            r.raise_for_status()
            data = r.json()
        await m.answer(data.get("message", "🎓 План формируется..."))
    except Exception as e:
        await m.answer(f"⚠️ Ошибка: {e}")

@router.message(Command("summary"))
async def summary_cmd(m: Message):
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{settings.ORCH_URL}/summary")
            r.raise_for_status()
        await m.answer(r.json().get("message", "📝 Конспект скоро будет."))
    except Exception as e:
        await m.answer(f"⚠️ Ошибка: {e}")

@router.message(Command("quiz"))
async def quiz_cmd(m: Message):
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{settings.ORCH_URL}/quiz")
            r.raise_for_status()
        await m.answer(r.json().get("message", "❓ Квиз в разработке."))
    except Exception as e:
        await m.answer(f"⚠️ Ошибка: {e}")

@router.message(Command("progress"))
async def progress_cmd(m: Message):
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{settings.ORCH_URL}/progress")
            r.raise_for_status()
        await m.answer(r.json().get("message", "📈 Прогресс пока не сохранён."))
    except Exception as e:
        await m.answer(f"⚠️ Ошибка: {e}")

async def on_startup(app: web.Application):
    bot: Bot = app["bot_instance"]
    await bot.set_webhook(
        url=f"{settings.BASE_URL}/webhook/{settings.TELEGRAM_TOKEN}",
        drop_pending_updates=True,
    )

def create_app() -> web.Application:
    bot = Bot(
        token=settings.TELEGRAM_TOKEN,
        default=DefaultBotProperties(parse_mode="HTML"),
    )
    dp = Dispatcher()
    dp.include_router(router)

    app = web.Application()
    app["bot_instance"] = bot

    SimpleRequestHandler(dp, bot).register(app, path=f"/webhook/{settings.TELEGRAM_TOKEN}")
    setup_application(app, dp, bot=bot)

    app.on_startup.append(on_startup)
    return app

if __name__ == "__main__":
    web.run_app(create_app(), host="0.0.0.0", port=8080)