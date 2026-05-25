from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.models import Attempt, Base, Question, Schedule, SourceNote
from app.services.questions import approve_question
from app.services.reviews import due_questions, record_attempt_and_schedule


def make_session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def seed_question(db, source_title="Source", prompt="Prompt"):
    source = db.scalar(select(SourceNote).where(SourceNote.title == source_title))
    if source is None:
        source = SourceNote(
            title=source_title,
            path=f"00 - Inbox/{source_title}.md",
            content_hash=source_title,
            learning_status="questions-drafted",
        )
        db.add(source)
        db.flush()
    question = Question(source_note_id=source.id, type="short-answer", prompt=prompt, answer="Answer", status="draft")
    db.add(question)
    db.commit()
    db.refresh(question)
    return question


def test_question_approval_creates_schedule(tmp_path):
    db = make_session(tmp_path)
    question = seed_question(db)

    approve_question(db, question)

    refreshed = db.get(Question, question.id)
    assert refreshed.status == "active"
    assert refreshed.schedule is not None
    assert refreshed.source_note.learning_status == "active"


def test_review_attempt_recording_updates_schedule(tmp_path):
    db = make_session(tmp_path)
    question = seed_question(db)
    approve_question(db, question)

    attempt = record_attempt_and_schedule(
        db,
        db.get(Question, question.id),
        submitted_answer="Answer",
        rating="good",
        confidence="medium",
        score=1.0,
    )

    schedule = db.scalar(select(Schedule).where(Schedule.question_id == question.id))
    assert attempt.id is not None
    assert db.scalar(select(Attempt)) is not None
    assert schedule.last_reviewed_at is not None
    assert schedule.interval_days >= 1.0


def test_due_queue_interleaves_sources(tmp_path):
    db = make_session(tmp_path)
    questions = [seed_question(db, "A", "A1"), seed_question(db, "A", "A2"), seed_question(db, "B", "B1")]
    for question in questions:
        approve_question(db, question)

    queue = due_questions(db, limit=3, now=datetime(2030, 1, 1, tzinfo=timezone.utc))

    assert [item.source_note.title for item in queue[:3]] == ["A", "B", "A"]
