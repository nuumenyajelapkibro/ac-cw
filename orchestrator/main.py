from fastapi import FastAPI
from models import StudyRequest

app = FastAPI(title="AI Study Buddy Orchestrator (MVP)")

@app.get("/health")
async def health():
    return {"status": "ok"}

@app.post("/study")
async def study(req: StudyRequest):
    return {"message": f"🎓 План по теме «{req.topic}» создаётся. Пришлю конспект и квиз позже."}

@app.get("/summary")
async def summary():
    return {"message": "📝 Конспект будет доступен после импорта материалов."}

@app.get("/quiz")
async def quiz():
    return {"message": "❓ Квиз появится после первичного обучения (MVP-заглушка)."}

@app.get("/progress")
async def progress():
    return {"message": "📈 Пока нет данных о прогрессе. Начните со /study."}