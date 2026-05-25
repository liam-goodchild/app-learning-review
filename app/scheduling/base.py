from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class ScheduleState:
    interval_days: float
    stability: float
    difficulty: float
    lapse_count: int


@dataclass(frozen=True)
class ScheduleDecision:
    due_at: datetime
    interval_days: float
    stability: float
    difficulty: float
    lapse_count: int


class Scheduler:
    def schedule(self, state: ScheduleState, rating: str, reviewed_at: datetime) -> ScheduleDecision:
        raise NotImplementedError

