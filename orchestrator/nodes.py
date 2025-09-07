from __future__ import annotations

import json
import os
import logging
from typing import Any, List

import httpx
import asyncio
from fastapi import HTTPException

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
N8N_BASIC_USER = os.getenv("N8N_BASIC_USER")
N8N_BASIC_PASS = os.getenv("N8N_BASIC_PASS")
FLOWISE_SUMMARY_URL = os.getenv("FLOWISE_SUMMARY_URL", "https://flowise.yumini.ru/summary_chain")
FLOWISE_QUIZ_URL = os.getenv("FLOWISE_QUIZ_URL", "https://flowise.yumini.ru/quiz_chain")
# Таймаут HTTP-запросов (по умолчанию увеличен из-за долгих флоу n8n)
HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "180.0"))


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
    attempts = 2
    backoff_base = 0.6
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, auth=(N8N_BASIC_USER, N8N_BASIC_PASS)) as client:
            last_exc: Exception | None = None
            for attempt in range(1, attempts + 1):
                try:
                    resp = await client.post(N8N_WEBHOOK_URL, json=payload)
                    # 5xx считаем сетевой/инфраструктурной ошибкой — ретраим
                    if 500 <= resp.status_code < 600:
                        raise httpx.HTTPStatusError("server error", request=resp.request, response=resp)

                    # 4xx — бизнес-ошибка от n8n: пробуем достать сообщение
                    if 400 <= resp.status_code < 500:
                        msg = _extract_error_message(resp)
                        _log_n8n_business_error(req, None, msg)
                        raise HTTPException(status_code=resp.status_code, detail=msg or "n8n returned client error")

                    # 2xx — разбираем тело
                    data = _safe_json(resp)
                    item = _pick_ok_item(data)
                    # Если явно ok=false или нет годного элемента — 400
                    if not item or (isinstance(item, dict) and item.get("ok") is False):
                        msg = (item or {}).get("error") if isinstance(item, dict) else None
                        _log_n8n_business_error(req, _safe_get(item, "request_id"), msg)
                        raise HTTPException(status_code=400, detail=msg or "n8n: план не сформирован")

                    doc_url = _safe_get(item, "doc_url")
                    if not doc_url:
                        _log_n8n_business_error(req, _safe_get(item, "request_id"), "doc_url missing")
                        raise HTTPException(status_code=422, detail="n8n: не вернул doc_url")

                    cal_raw = _safe_get(item, "calendar_info")
                    calendar_info = _coerce_calendar_info(cal_raw)

                    # Логирование успеха
                    log.info(
                        "n8n plan success: request_id=%s topic=%s user_id=%s doc_url=%s event_count=%s",
                        _safe_get(item, "request_id"), req.topic, req.user_id, doc_url,
                        (calendar_info or {}).get("event_count") if isinstance(calendar_info, dict) else None,
                    )

                    return StudyPlanInfo(doc_url=doc_url, calendar_info=calendar_info)

                except (httpx.TimeoutException, httpx.ConnectError, httpx.RequestError, httpx.HTTPStatusError) as e:
                    last_exc = e
                    if attempt < attempts:
                        await asyncio.sleep(backoff_base * (2 ** (attempt - 1)))
                        continue
                    # Все попытки исчерпаны — вернём заглушку
                    log.warning(
                        "n8n plan webhook failed (network/server): %s | topic=%s user_id=%s",
                        e, req.topic, req.user_id,
                    )
                    return _stub_plan()

            # Теоретически недостижимо
            if last_exc:
                raise last_exc
    except HTTPException:
        # бизнес-ошибки пробрасываем дальше, их обработает FastAPI слой
        raise
    except Exception as e:
        # Непредвиденная ошибка клиента/парсинга — безопасный fallback
        log.warning("n8n plan webhook unexpected failure: %s | topic=%s user_id=%s", e, req.topic, req.user_id)
        return _stub_plan()


def _stub_plan() -> StudyPlanInfo:
    return StudyPlanInfo(
        doc_url="https://docs.google.com/document/d/FAKE_M2_PLAN",
        calendar_info={"created": False, "reason": "stub"},
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


def _pick_ok_item(data: Any) -> dict | None:
    """Поддержка двух форматов ответа: объект или массив объектов.
    Возвращает первый элемент с ok=true, либо сам объект, если он не массив.
    """
    if isinstance(data, list):
        for it in data:
            if isinstance(it, dict) and it.get("ok") is True:
                return it
        # если нет ok=true, но массив непустой — вернём первый для диагностики
        return data[0] if data else None
    if isinstance(data, dict):
        return data
    return None


def _coerce_calendar_info(raw: Any) -> dict | None:
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            v = json.loads(raw)
            return v if isinstance(v, dict) else None
        except Exception:
            return None
    return None


def _extract_error_message(resp: httpx.Response) -> str | None:
    data = _safe_json(resp)
    if isinstance(data, dict):
        return str(data.get("error") or data.get("message") or data.get("detail") or "").strip() or None
    if isinstance(data, list) and data:
        first = data[0]
        if isinstance(first, dict):
            return str(first.get("error") or first.get("message") or first.get("detail") or "").strip() or None
    return None


def _safe_get(d: Any, key: str) -> Any:
    return d.get(key) if isinstance(d, dict) else None


def _log_n8n_business_error(req: StudyRequest, request_id: Any, msg: str | None) -> None:
    log.warning(
        "n8n plan business error: request_id=%s topic=%s user_id=%s detail=%s",
        request_id, req.topic, req.user_id, (msg or "")
    )


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
