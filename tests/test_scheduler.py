from __future__ import annotations

from datetime import datetime, timezone

from app.scheduling.base import ScheduleState
from app.scheduling.simple import SimpleScheduler


def test_simple_scheduler_ratings():
    scheduler = SimpleScheduler()
    now = datetime(2026, 5, 25, tzinfo=timezone.utc)
    state = ScheduleState(interval_days=1.0, stability=1.0, difficulty=2.5, lapse_count=0)

    again = scheduler.schedule(state, "again", now)
    hard = scheduler.schedule(state, "hard", now)
    good = scheduler.schedule(state, "good", now)
    easy = scheduler.schedule(state, "easy", now)

    assert again.interval_days < hard.interval_days < good.interval_days < easy.interval_days
    assert again.lapse_count == 1
    assert easy.difficulty < state.difficulty
