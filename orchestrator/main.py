from __future__ import annotations

import logging
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse

from .schemas import (
    StudyRequest,
    StudyPlanInfo,
    SummaryRequest,
    SummaryResponse,
    QuizRequest,
    QuizResponse,
    QuizResult,
    ProgressResponse,
)
from .fsm import (
    State,
    get_state,
    set_state,
    ensure_state,
    set_ctx,
    get_ctx,
    set_quiz,
    get_quiz,
    clear_quiz,
    fsm_guard_transition,
)
from .nodes import (
    plan,
    call_flowise_summary,
    call_flowise_quiz,
    persist_progress,
)

log = logging.getLogger(__name__)

app = FastAPI(title="ASB Orchestrator", version="0.2.0")


@app.get("/health")
def health():
    return {"ok": True}


# ---------------------------
# /study — старт планирования
# ---------------------------
@app.post("/study", response_model=StudyPlanInfo)
async def study(req: StudyRequest):
    uid = req.user_id or 0

    # запрещаем параллельные процессы
    st = get_state(uid)
    if st in (State.PLANNING, State.QUIZZING):
        raise HTTPException(status_code=409, detail=f"Сейчас состояние: {st}. Завершите текущий процесс.")

    # переход в PLANNING
    set_state(uid, State.PLANNING)

    try:
        info = await plan(req)
        # сохраняем контекст и переводим в READY
        set_ctx(uid, topic=req.topic, level=req.depth, doc_url=info.doc_url, calendar_info=info.calendar_info)
        set_state(uid, State.READY)
        return info
    except HTTPException:
        # пробрасываем как есть
        set_state(uid, State.IDLE)
        raise
    except Exception as e:
        set_state(uid, State.IDLE)
        log.exception("study failed")
        raise HTTPException(status_code=500, detail=f"Ошибка планирования: {e}")


# ---------------------------
# /summary — получить конспект
# ---------------------------
@app.get("/summary", response_model=SummaryResponse)
async def summary(
    user_id: int = Query(..., description="telegram_id пользователя"),
    topic: Optional[str] = Query(None),
):
    try:
        ensure_state(user_id, {State.READY})
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))

    ctx = get_ctx(user_id)
    req = SummaryRequest(
        topic=topic or str(ctx.get("topic") or ""),
        level=str(ctx.get("level") or "basic"),
    )
    if not req.topic:
        raise HTTPException(status_code=400, detail="Тема не определена. Сначала вызовите /study.")

    return await call_flowise_summary(req)


# ---------------------------
# /quiz — начать квиз-сессию
# ---------------------------
@app.get("/quiz", response_model=QuizResponse)
async def quiz(
    user_id: int = Query(..., description="telegram_id пользователя"),
    topic: Optional[str] = Query(None),
    questions_count: int = Query(6, ge=3, le=10),
):
    try:
        # атомарный (best-effort) переход READY -> QUIZZING
        new_state = fsm_guard_transition(user_id, {State.READY}, State.QUIZZING)
        if new_state != State.QUIZZING:
            raise HTTPException(status_code=409, detail=f"Нужно состояние READY, сейчас {new_state}. Сначала /study.")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Ошибка состояния: {e}")

    ctx = get_ctx(user_id)
    req = QuizRequest(
        topic=topic or str(ctx.get("topic") or ""),
        level=str(ctx.get("level") or "basic"),
        questions_count=questions_count,
    )
    if not req.topic:
        # возвращаемся в READY, чтобы не застрять
        set_state(user_id, State.READY)
        raise HTTPException(status_code=400, detail="Тема не определена. Сначала вызовите /study.")

    q = await call_flowise_quiz(req)
    set_quiz(user_id, {"questions": [qq.dict() for qq in q.questions], "current_index": 0, "topic": req.topic, "level": req.level})
    return q


# --------------------------------
# /quiz/result — завершить и записать
# --------------------------------
@app.post("/quiz/result")
async def quiz_result(
    user_id: int,
    result: QuizResult,
):
    try:
        # очищаем активную сессию
        clear_quiz(user_id)
        # возвращаем в READY
        set_state(user_id, State.READY)
        await persist_progress(result)
        return JSONResponse({"ok": True})
    except Exception as e:
        log.exception("quiz_result failed")
        raise HTTPException(status_code=500, detail=f"Не удалось сохранить результат: {e}")


# ---------------------------
# /progress — посмотреть прогресс
# ---------------------------
@app.get("/progress", response_model=ProgressResponse)
async def progress(user_id: int):
    ctx = get_ctx(user_id)
    # В М2 — заглушка. В М5 подключим БД и реальные метрики.
    return ProgressResponse(
        completion_percent=20.0,
        avg_score=0.0,
        weak_topics=[],
        doc_url=ctx.get("doc_url"),
    )