from __future__ import annotations

import json
import os
import logging
from typing import Any, List

import httpx

from schemas import (
    StudyRequest,
    StudyPlanInfo,
    SummaryRequest,
    SummaryResponse,
    QuizRequest,
    QuizResponse,
    QuizQuestion,
    QuizResult,
)

log = logging.getLogger(__name__)

# --- Конфиг через ENV ---
N8N_WEBHOOK_URL = os.getenv("N8N_WEBHOOK_URL", "https://n8n.yumini.ru/webhook/asb-plan")
FLOWISE_SUMMARY_URL = os.getenv("FLOWISE_SUMMARY_URL", "https://flowise.yumini.ru/summary_chain")
FLOWISE_QUIZ_URL = os.getenv("FLOWISE_QUIZ_URL", "https://flowise.yumini.ru/quiz_chain")
HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "30.0"))


# =======================
# Планирование (n8n)
# =======================
async def plan(req: StudyRequest) -> StudyPlanInfo:
    """
    Подготовить план: сейчас просто прокидываем в n8n.
    При недоступности n8n возвращаем заглушку, чтобы не блокировать М2.
    """
    return await trigger_n8n_plan(req)


async def trigger_n8n_plan(req: StudyRequest) -> StudyPlanInfo:
    payload = req.model_dump()
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            resp = await client.post(N8N_WEBHOOK_URL, json=payload)
            resp.raise_for_status()
            data = _safe_json(resp)
            doc_url = (data or {}).get("doc_url")
            calendar_info = (data or {}).get("calendar_info")
            if not doc_url:
                # n8n ответил странно — вернём fallback
                return _stub_plan()
            return StudyPlanInfo(doc_url=doc_url, calendar_info=calendar_info)
    except Exception as e:
        log.warning("n8n plan webhook failed: %s", e)
        return _stub_plan()


def _stub_plan() -> StudyPlanInfo:
    return StudyPlanInfo(
        doc_url="https://docs.google.com/document/d/FAKE_M2_PLAN",
        calendar_info=json.dumps({"created": False, "reason": "stub"}, ensure_ascii=False),
    )


# =======================
# Flowise: summary
# =======================
async def call_flowise_summary(req: SummaryRequest) -> SummaryResponse:
    """
    Запросить конспект у Flowise. При ошибке — вернуть краткий заглушечный markdown.
    """
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            resp = await client.post(FLOWISE_SUMMARY_URL, json=req.model_dump())
            resp.raise_for_status()
            data = _safe_json(resp) or {}
            md = data.get("markdown") or data.get("text") or data.get("content")
            if not md:
                # Некоторые Flowise-флоу возвращают массив сообщений — попробуем собрать
                md = _extract_markdown_from_flowise(data)
            if not md:
                md = f"# {req.topic}\n_Заглушка summary для М2._"
            return SummaryResponse(markdown=md)
    except Exception as e:
        log.warning("flowise summary failed: %s", e)
        return SummaryResponse(markdown=f"# {req.topic}\n_Заглушка summary для М2._")


# =======================
# Flowise: quiz
# =======================
async def call_flowise_quiz(req: QuizRequest) -> QuizResponse:
    """
    Запросить квиз у Flowise. При ошибке — сгенерировать локальную заглушку.
    Ожидаемый формат ответа: {"questions": [{"q":..., "options":[...], "answer_index":0}, ...]}
    """
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            resp = await client.post(FLOWISE_QUIZ_URL, json=req.model_dump())
            resp.raise_for_status()
            data = _safe_json(resp) or {}
            questions_raw = data.get("questions")
            if not questions_raw:
                questions_raw = _extract_questions_from_flowise(data)
            if not questions_raw:
                questions_raw = _fake_questions(req.questions_count, req.topic)
            questions = _coerce_questions(questions_raw)
            return QuizResponse(questions=questions)
    except Exception as e:
        log.warning("flowise quiz failed: %s", e)
        return QuizResponse(questions=_coerce_questions(_fake_questions(req.questions_count, req.topic)))


# =======================
# Прогресс (пока no-op)
# =======================
async def persist_progress(result: QuizResult) -> None:
    """
    Заглушка: в М5 подключим БД и реальную запись прогресса.
    Пока просто логируем.
    """
    log.info(
        "quiz_result: topic=%s correct=%s total=%s weak=%s",
        result.topic, result.correct, result.total, result.weak_topics
    )


# =======================
# Вспомогательные утилиты
# =======================
def _safe_json(resp: httpx.Response) -> Any | None:
    try:
        return resp.json()
    except Exception:
        try:
            # Иногда Flowise присылает строку с JSON внутри
            return json.loads(resp.text)
        except Exception:
            return None


def _extract_markdown_from_flowise(data: Any) -> str | None:
    """
    Пытаемся вытащить текст из разных возможных структур Flowise.
    """
    if isinstance(data, list):
        # список сообщений/узлов
        for item in data:
            text = _extract_markdown_from_flowise(item)
            if text:
                return text
    if isinstance(data, dict):
        for key in ("markdown", "text", "content", "output"):
            if isinstance(data.get(key), str) and data.get(key).strip():
                return data[key]
        # вложенные структуры
        for v in data.values():
            if isinstance(v, (list, dict)):
                text = _extract_markdown_from_flowise(v)
                if text:
                    return text
    return None


def _extract_questions_from_flowise(data: Any) -> List[dict]:
    """
    Пытаемся собрать вопросы из произвольной структуры ответа.
    Ищем объекты с ключами 'q'/'question' и 'options'.
    """
    collected: List[dict] = []

    def walk(x: Any):
        if isinstance(x, dict):
            if ("q" in x or "question" in x) and "options" in x:
                q = x.get("q") or x.get("question")
                options = x.get("options") or []
                ans = x.get("answer_index")
                collected.append({"q": q, "options": options, "answer_index": ans if isinstance(ans, int) else 0})
            else:
                for v in x.values():
                    walk(v)
        elif isinstance(x, list):
            for it in x:
                walk(it)

    walk(data)
    return collected


def _fake_questions(n: int, topic: str) -> List[dict]:
    base = f"Тема: {topic}. Выберите верный вариант."
    return [
        {"q": f"{i+1}. {base}", "options": ["A", "B", "C", "D"], "answer_index": 0}
        for i in range(max(1, n))
    ]


def _coerce_questions(raw: List[dict]) -> List[QuizQuestion]:
    """
    Приводим произвольные словари к нашей схеме QuizQuestion.
    Гарантируем наличие 4 опций и корректный answer_index.
    """
    out: List[QuizQuestion] = []
    for item in raw:
        q = str(item.get("q") or item.get("question") or "Вопрос")
        options = item.get("options") or []
        # нормализуем опции
        options = [str(o) for o in options][:4]
        while len(options) < 4:
            options.append(f"Вариант {len(options)+1}")
        # индекс ответа
        idx = item.get("answer_index")
        if not isinstance(idx, int) or not (0 <= idx < 4):
            idx = 0
        out.append(QuizQuestion(q=q, options=options, answer_index=idx))
    return out