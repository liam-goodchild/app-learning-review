from __future__ import annotations

from datetime import datetime, timedelta

from app.scheduling.base import ScheduleDecision, ScheduleState, Scheduler


class SimpleScheduler(Scheduler):
    """Small, swappable fallback scheduler for the MVP."""

    def schedule(self, state: ScheduleState, rating: str, reviewed_at: datetime) -> ScheduleDecision:
        rating = rating.lower()
        interval = max(0.0, state.interval_days)
        stability = max(0.0, state.stability)
        difficulty = min(5.0, max(1.0, state.difficulty))
        lapse_count = state.lapse_count

        if rating == "again":
            interval = 10 / (24 * 60)
            stability = max(0.1, stability * 0.5)
            difficulty = min(5.0, difficulty + 0.35)
            lapse_count += 1
        elif rating == "hard":
            interval = 1.0 if interval < 1.0 else interval * 1.25
            stability = max(0.5, stability * 1.15 if stability else 0.8)
            difficulty = min(5.0, difficulty + 0.15)
        elif rating == "easy":
            interval = 3.0 if interval < 1.0 else interval * 3.0
            stability = max(2.5, stability * 2.4 if stability else 2.5)
            difficulty = max(1.0, difficulty - 0.25)
        else:
            interval = 1.0 if interval < 1.0 else interval * 2.2
            stability = max(1.0, stability * 1.8 if stability else 1.0)
            difficulty = max(1.0, difficulty - 0.05)

        return ScheduleDecision(
            due_at=reviewed_at + timedelta(days=interval),
            interval_days=interval,
            stability=stability,
            difficulty=difficulty,
            lapse_count=lapse_count,
        )

