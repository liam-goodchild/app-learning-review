from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.importer.scanner import safe_json
from app.models import Question, Schedule, SourceNote, utcnow
from app.services.json_tools import loads_json

QUESTION_TYPES = {"multiple-choice", "short-answer", "rubric", "categorisation"}
QUESTION_STATUSES = {"draft", "approved", "active", "retired"}


def validate_json_field(text: str, default: object) -> str:
    if not text.strip():
        return safe_json(default)
    return safe_json(loads_json(text, default))


def attach_active_schedule(db: Session, question: Question, *, now=None) -> None:
    now = now or utcnow()
    db.add(
        Schedule(
            question_id=question.id,
            due_at=now,
            interval_days=0.0,
            stability=0.0,
            difficulty=float(question.difficulty),
            lapse_count=0,
        )
    )


def create_question(
    db: Session,
    source_note_id: int,
    *,
    question_type: str,
    prompt: str,
    answer: str = "",
    options_json: str = "",
    rubric_json: str = "",
    feedback_json: str = "",
) -> Question:
    if question_type not in QUESTION_TYPES:
        question_type = "short-answer"
    source = db.get(SourceNote, source_note_id)
    if source is None:
        raise ValueError("Source note not found")
    now = utcnow()
    question = Question(
        source_note_id=source_note_id,
        type=question_type,
        prompt=prompt.strip(),
        answer=answer.strip(),
        options_json=validate_json_field(options_json, []),
        rubric_json=validate_json_field(rubric_json, []),
        feedback_json=validate_json_field(feedback_json, {}),
        source_reference=source.path,
        status="active",
    )
    db.add(question)
    db.flush()
    attach_active_schedule(db, question, now=now)
    source.learning_status = "active"
    source.updated_at = now
    db.commit()
    db.refresh(question)
    return question


def update_question(
    db: Session,
    question: Question,
    *,
    question_type: str,
    prompt: str,
    answer: str,
    options_json: str,
    rubric_json: str,
    feedback_json: str,
    source_reference: str,
    difficulty: int,
    status: str,
) -> Question:
    if question_type not in QUESTION_TYPES:
        raise ValueError("Unsupported question type")
    if status not in QUESTION_STATUSES:
        raise ValueError("Unsupported question status")
    question.type = question_type
    question.prompt = prompt.strip()
    question.answer = answer.strip()
    question.options_json = validate_json_field(options_json, [] if question_type != "categorisation" else {"categories": [], "items": []})
    question.rubric_json = validate_json_field(rubric_json, [])
    question.feedback_json = validate_json_field(feedback_json, {})
    question.source_reference = source_reference.strip() or question.source_reference
    question.difficulty = max(1, min(5, int(difficulty)))
    question.status = status
    question.updated_at = utcnow()
    db.commit()
    db.refresh(question)
    return question


def approve_question(db: Session, question: Question) -> Question:
    now = utcnow()
    question.status = "active"
    question.updated_at = now
    if question.schedule is None:
        db.add(
            Schedule(
                question_id=question.id,
                due_at=now,
                interval_days=0.0,
                stability=0.0,
                difficulty=float(question.difficulty),
                lapse_count=0,
            )
        )
    else:
        question.schedule.due_at = min(question.schedule.due_at, now) if question.schedule.due_at else now
        question.schedule.updated_at = now

    source = db.get(SourceNote, question.source_note_id)
    if source is not None:
        source.learning_status = "active"
        source.updated_at = now
    db.commit()
    db.refresh(question)
    return question


def retire_question(db: Session, question: Question) -> Question:
    question.status = "retired"
    question.updated_at = utcnow()
    db.commit()
    db.refresh(question)
    return question


def due_schedule_for_question(db: Session, question_id: int) -> Schedule | None:
    return db.scalar(select(Schedule).where(Schedule.question_id == question_id))

