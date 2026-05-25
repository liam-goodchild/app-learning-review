from __future__ import annotations

import json
from collections import OrderedDict, defaultdict, deque
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session, joinedload

from app.models import Attempt, Question, Schedule, SourceNote, utcnow
from app.scheduling.base import ScheduleState
from app.scheduling.simple import SimpleScheduler
from app.services.json_tools import loads_json

VALID_RATINGS = {"again", "hard", "good", "easy"}
VALID_CONFIDENCE = {"low", "medium", "high"}


def _aware(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def option_list(question: Question) -> list[dict[str, Any]]:
    data = loads_json(question.options_json, [])
    if isinstance(data, dict):
        data = data.get("options", [])
    return data if isinstance(data, list) else []


def feedback_dict(question: Question) -> dict[str, Any]:
    data = loads_json(question.feedback_json, {})
    return data if isinstance(data, dict) else {}


def rubric_items(question: Question) -> list[dict[str, Any]]:
    data = loads_json(question.rubric_json, [])
    return data if isinstance(data, list) else []


def categorisation_data(question: Question) -> dict[str, Any]:
    data = loads_json(question.options_json, {"categories": [], "items": []})
    return data if isinstance(data, dict) else {"categories": [], "items": []}


def interleave_questions(questions: list[Question], limit: int) -> list[Question]:
    groups: "OrderedDict[int, deque[Question]]" = OrderedDict()
    for question in questions:
        groups.setdefault(question.source_note_id, deque()).append(question)

    mixed: list[Question] = []
    while groups and len(mixed) < limit:
        for source_id in list(groups.keys()):
            queue = groups[source_id]
            if queue:
                mixed.append(queue.popleft())
                if len(mixed) >= limit:
                    break
            if not queue:
                del groups[source_id]
    return mixed


def due_questions(db: Session, *, limit: int, now: datetime | None = None) -> list[Question]:
    now = now or utcnow()
    rows = (
        db.execute(
            select(Question)
            .options(joinedload(Question.source_note), joinedload(Question.schedule))
            .join(Schedule)
            .where(Question.status == "active", Schedule.due_at <= now)
            .order_by(Schedule.due_at.asc(), Question.source_note_id.asc())
            .limit(max(limit * 4, limit))
        )
        .unique()
        .scalars()
        .all()
    )
    return interleave_questions(rows, limit)


def due_count(db: Session, now: datetime | None = None) -> int:
    now = now or utcnow()
    return int(
        db.scalar(
            select(func.count(Question.id))
            .join(Schedule)
            .where(Question.status == "active", Schedule.due_at <= now)
        )
        or 0
    )


def evaluate_answer(question: Question, submitted_answer: str) -> dict[str, Any]:
    feedback = feedback_dict(question)
    result: dict[str, Any] = {
        "result": "self-scored",
        "score": None,
        "expected_answer": question.answer,
        "why": feedback.get("why", ""),
        "common_misconception": feedback.get("common_misconception", ""),
        "source_reference": question.source_reference or question.source_note.path,
        "option_feedback": [],
        "rubric": rubric_items(question),
    }

    if question.type == "multiple-choice":
        selected = submitted_answer.strip()
        options = option_list(question)
        correct_ids = {str(option.get("id")) for option in options if option.get("correct")}
        score = 1.0 if selected in correct_ids else 0.0
        result["score"] = score
        result["result"] = "correct" if score == 1.0 else "incorrect"
        result["expected_answer"] = question.answer or ", ".join(sorted(correct_ids))
        result["option_feedback"] = options
        return result

    if question.type == "categorisation":
        try:
            selected_map = json.loads(submitted_answer)
        except json.JSONDecodeError:
            selected_map = {}
        items = categorisation_data(question).get("items", [])
        total = len(items)
        correct = 0
        expected: list[str] = []
        for index, item in enumerate(items):
            expected_category = str(item.get("category", ""))
            expected.append(f"{item.get('text', '')}: {expected_category}")
            if selected_map.get(str(index)) == expected_category:
                correct += 1
        score = (correct / total) if total else 0.0
        result["score"] = score
        result["result"] = "correct" if score == 1.0 else "partially correct" if score > 0 else "incorrect"
        result["expected_answer"] = "\n".join(expected)
        return result

    return result


def score_from_rating(rating: str) -> float:
    return {"again": 0.0, "hard": 0.4, "good": 0.75, "easy": 1.0}.get(rating, 0.75)


def record_attempt_and_schedule(
    db: Session,
    question: Question,
    *,
    submitted_answer: str,
    rating: str,
    confidence: str,
    score: float | None,
    response_time_ms: int | None = None,
) -> Attempt:
    if rating not in VALID_RATINGS:
        raise ValueError("Invalid rating")
    if confidence not in VALID_CONFIDENCE:
        raise ValueError("Invalid confidence")

    schedule = question.schedule
    if schedule is None:
        schedule = Schedule(question_id=question.id, due_at=utcnow())
        db.add(schedule)
        db.flush()

    reviewed_at = utcnow()
    if score is None:
        score = score_from_rating(rating)
    attempt = Attempt(
        question_id=question.id,
        submitted_answer=submitted_answer,
        score=score,
        rating=rating,
        confidence=confidence,
        response_time_ms=response_time_ms,
        feedback_shown=True,
    )
    db.add(attempt)

    decision = SimpleScheduler().schedule(
        ScheduleState(
            interval_days=schedule.interval_days,
            stability=schedule.stability,
            difficulty=schedule.difficulty,
            lapse_count=schedule.lapse_count,
        ),
        rating,
        reviewed_at,
    )
    schedule.due_at = decision.due_at
    schedule.last_reviewed_at = reviewed_at
    schedule.interval_days = decision.interval_days
    schedule.stability = decision.stability
    schedule.difficulty = decision.difficulty
    schedule.lapse_count = decision.lapse_count
    schedule.updated_at = reviewed_at

    db.commit()
    db.refresh(attempt)
    return attempt


def recent_attempts(db: Session, limit: int = 10) -> list[Attempt]:
    return (
        db.execute(
            select(Attempt)
            .options(joinedload(Attempt.question).joinedload(Question.source_note))
            .order_by(desc(Attempt.created_at))
            .limit(limit)
        )
        .unique()
        .scalars()
        .all()
    )


def weak_areas(db: Session, limit: int = 5) -> list[tuple[SourceNote, int]]:
    rows = db.execute(
        select(SourceNote, func.count(Attempt.id).label("misses"))
        .join(Question, Question.source_note_id == SourceNote.id)
        .join(Attempt, Attempt.question_id == Question.id)
        .where((Attempt.rating.in_(["again", "hard"])) | (Attempt.score < 0.6))
        .group_by(SourceNote.id)
        .order_by(desc("misses"))
        .limit(limit)
    ).all()
    return [(row[0], int(row[1])) for row in rows]

