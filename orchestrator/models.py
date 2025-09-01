from pydantic import BaseModel
from typing import Literal, Optional

class StudyRequest(BaseModel):
    topic: str
    depth: Literal["beginner", "intermediate", "advanced"] = "beginner"
    duration_days: int = 7
    daily_time_minutes: int = 30
    telegram_user_id: Optional[int] = None