from __future__ import annotations

from typing import List, Optional, Literal, Dict, Any
from pydantic import BaseModel, Field, HttpUrl


LevelStr = Literal["basic", "intermediate", "advanced"]


class StudyRequest(BaseModel):
    """Запрос на генерацию учебного плана."""
    topic: str = Field(..., min_length=2, description="Тема обучения")
    depth: LevelStr = Field("basic", description="Уровень: basic|intermediate|advanced")
    duration_days: int = Field(7, ge=1, le=60, description="Длительность в днях")
    daily_time_minutes: int = Field(20, ge=5, le=180, description="Ежедневное время, минут")
    user_id: Optional[int] = Field(None, description="telegram_id пользователя")


class StudyPlanInfo(BaseModel):
    """Результат планирования от n8n: ссылка на Doc и инфо по календарю."""
    doc_url: Optional[HttpUrl] = None
    calendar_info: Optional[Dict[str, Any]] = None


class SummaryRequest(BaseModel):
    topic: str
    level: LevelStr = "basic"
    materials_ids: Optional[List[str]] = None


class SummaryResponse(BaseModel):
    markdown: str


class QuizRequest(BaseModel):
    topic: str
    level: LevelStr = "basic"
    questions_count: int = Field(6, ge=3, le=10)


class QuizQuestion(BaseModel):
    q: str
    options: List[str]
    answer_index: int = Field(..., ge=0, le=3)


class QuizResponse(BaseModel):
    questions: List[QuizQuestion]


class QuizResult(BaseModel):
    topic: str
    correct: int = Field(..., ge=0)
    total: int = Field(..., ge=1)
    weak_topics: Optional[List[str]] = None


class QuizResultIn(QuizResult):
    """Тело запроса для /quiz/result, включает user_id."""
    user_id: int


class ProgressResponse(BaseModel):
    completion_percent: float = Field(..., ge=0, le=100)
    avg_score: float = Field(..., ge=0, le=100)
    weak_topics: List[str]
    doc_url: Optional[HttpUrl] = None


__all__ = [
    "StudyRequest",
    "StudyPlanInfo",
    "SummaryRequest",
    "SummaryResponse",
    "QuizRequest",
    "QuizQuestion",
    "QuizResponse",
    "QuizResult",
    "QuizResultIn",
    "ProgressResponse",
]
