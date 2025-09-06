# orchestrator/fsm.py
from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional, TypedDict

import redis

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")

r = redis.Redis.from_url(REDIS_URL, decode_responses=True)


# --- Ключи и TTL ---
FSM_KEY = "asb:fsm:{uid}"       # текущее состояние
CTX_KEY = "asb:ctx:{uid}"       # контекст текущей темы / плана
QUIZ_KEY = "asb:quiz:{uid}"     # активная квиз-сессия

TTL_FSM = 48 * 3600             # 48h
TTL_CTX = 7 * 24 * 3600         # 7d
TTL_QUIZ = 2 * 3600             # 2h


class State:
    """Допустимые состояния конечного автомата."""
    IDLE = "IDLE"           # по умолчанию
    PLANNING = "PLANNING"   # /study запущен, ждём план
    READY = "READY"         # план готов, доступны /summary и /quiz
    QUIZZING = "QUIZZING"   # активная квиз-сессия


class QuizPayload(TypedDict, total=False):
    """Схема хранения активного квиза в Redis (упрощённо)."""
    questions: list[dict[str, Any]]
    current_index: int
    topic: str
    level: str


# --- FSM helpers ---

def get_state(uid: int) -> str:
    """Получить состояние пользователя, если нет — вернуть IDLE."""
    st = r.get(FSM_KEY.format(uid=uid))
    return st or State.IDLE


def set_state(uid: int, state: str) -> None:
    """Установить состояние с TTL."""
    r.setex(FSM_KEY.format(uid=uid), TTL_FSM, state)


def ensure_state(uid: int, allowed: set[str]) -> None:
    """Бросить ValueError, если текущее состояние не в allowed."""
    st = get_state(uid)
    if st not in allowed:
        raise ValueError(f"Нужно состояние {allowed}, сейчас {st}")


# --- Контекст обучения ---

def set_ctx(uid: int, **kwargs: Any) -> None:
    """
    Обновить контекст (hash) для пользователя.
    Не-строковые значения сериализуются в JSON.
    """
    key = CTX_KEY.format(uid=uid)
    if kwargs:
        mapping: Dict[str, str] = {}
        for k, v in kwargs.items():
            if isinstance(v, str):
                mapping[k] = v
            else:
                mapping[k] = json.dumps(v, ensure_ascii=False)
        r.hset(key, mapping=mapping)
    r.expire(key, TTL_CTX)


def get_ctx(uid: int) -> Dict[str, Any]:
    """Получить контекст и распарсить JSON-значения обратно в Python-типы."""
    key = CTX_KEY.format(uid=uid)
    data = r.hgetall(key)
    out: Dict[str, Any] = {}
    for k, v in data.items():
        try:
            out[k] = json.loads(v)
        except Exception:
            out[k] = v
    return out


def clear_ctx(uid: int) -> None:
    """Полностью очистить контекст пользователя."""
    r.delete(CTX_KEY.format(uid=uid))


# --- Квиз-сессия ---

def set_quiz(uid: int, payload: QuizPayload) -> None:
    """Сохранить активную квиз-сессию целиком (JSON) с TTL."""
    r.setex(QUIZ_KEY.format(uid=uid), TTL_QUIZ, json.dumps(payload, ensure_ascii=False))


def get_quiz(uid: int) -> Optional[QuizPayload]:
    """Получить активную квиз-сессию (или None)."""
    raw = r.get(QUIZ_KEY.format(uid=uid))
    return json.loads(raw) if raw else None


def update_quiz(uid: int, **patch: Any) -> Optional[QuizPayload]:
    """
    Частично обновить активную квиз-сессию.
    Возвращает обновлённый payload или None, если квиза нет.
    """
    current = get_quiz(uid)
    if not current:
        return None
    current.update(patch)  # type: ignore[arg-type]
    set_quiz(uid, current)  # продлевает TTL
    return current


def clear_quiz(uid: int) -> None:
    """Удалить активную квиз-сессию."""
    r.delete(QUIZ_KEY.format(uid=uid))


# --- Утилиты безопасности/идемпотентности ---

def fsm_guard_transition(uid: int, from_states: set[str], to_state: str) -> str:
    """
    Атомарная проверка и установка нового состояния (best-effort).
    Возвращает новое состояние (или фактическое текущее, если не поменялось).
    Примечание: для абсолютной атомарности нужен Lua-скрипт или блокировка.
    """
    current = get_state(uid)
    if current not in from_states:
        return current
    set_state(uid, to_state)
    return to_state
