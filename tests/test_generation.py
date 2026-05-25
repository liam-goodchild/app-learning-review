from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.models import Base, GenerationJob, Question, SourceNote
from app.services.generation import (
    build_generation_prompt,
    claim_next_job,
    complete_generation_job,
    enqueue_generation_job,
    validate_generated_payload,
)


def make_session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        app_host="127.0.0.1",
        app_port=8080,
        database_url="sqlite:///:memory:",
        vault_path=tmp_path,
        vault_import_folders=["00 - Inbox", "02 - Notes"],
        app_secret_key="test-secret",
        app_username="admin",
        app_password="change-me",
        app_password_hash=None,
        session_cookie_name="test_session",
        secure_cookies=False,
        scan_interval_seconds=300,
        min_file_age_seconds=0,
        review_session_size=10,
        enable_periodic_scan=False,
        worker_token="worker-token",
    )


def seed_source(db) -> SourceNote:
    source = SourceNote(
        title="Terraform Variables and Outputs",
        path="00 - Inbox/Terraform Variables and Outputs.md",
        content_hash="abc",
        frontmatter_json=json.dumps({"learning_question_goal": 4, "learning_question_types": ["multiple-choice", "short-answer", "rubric", "categorisation"]}),
        tags_json=json.dumps(["terraform", "iac"]),
        source_urls_json=json.dumps(["https://developer.hashicorp.com/terraform/language/values"]),
        learning_status="needs-questions",
        body="Variables are inputs. Locals are internal named expressions. Outputs expose values.",
    )
    db.add(source)
    db.commit()
    db.refresh(source)
    return source


def fixture_output() -> str:
    return (Path(__file__).parent / "fixtures" / "generated_questions.json").read_text()


def test_generation_prompt_contains_source_body_and_schema(tmp_path):
    db = make_session(tmp_path)
    settings = make_settings(tmp_path)
    source = seed_source(db)
    job = enqueue_generation_job(db, source, question_count=4)

    prompt = build_generation_prompt(source, job, settings)

    assert "Return JSON only" in prompt
    assert "Variables are inputs" in prompt
    assert "multiple-choice" in prompt


def test_validate_and_import_generated_questions(tmp_path):
    db = make_session(tmp_path)
    source = seed_source(db)
    job = enqueue_generation_job(db, source, question_count=4)

    questions = validate_generated_payload(fixture_output())
    completed = complete_generation_job(db, job, fixture_output())

    assert len(questions) == 4
    assert completed.status == "succeeded"
    assert completed.draft_questions_created == 4
    assert db.get(SourceNote, source.id).learning_status == "questions-drafted"
    assert len(db.scalars(select(Question).where(Question.source_note_id == source.id)).all()) == 4


def test_claim_next_job_builds_prompt(tmp_path):
    db = make_session(tmp_path)
    settings = make_settings(tmp_path)
    source = seed_source(db)
    enqueue_generation_job(db, source, question_count=2)

    claimed = claim_next_job(db, settings)

    assert claimed is not None
    assert claimed.status == "running"
    assert "Terraform Variables and Outputs" in claimed.prompt_text


def test_invalid_multiple_choice_rejected(tmp_path):
    bad_output = {
        "questions": [
            {
                "type": "multiple-choice",
                "prompt": "Bad MC",
                "answer": "A",
                "options": [
                    {"id": "A", "text": "One", "correct": True, "feedback": "ok"},
                    {"id": "B", "text": "Two", "correct": True, "feedback": "also ok"}
                ],
                "rubric": [],
                "feedback": {"why": "bad", "common_misconception": ""},
                "difficulty": 1,
                "source_reference": "test.md"
            }
        ]
    }

    try:
        validate_generated_payload(json.dumps(bad_output))
    except ValueError as exc:
        assert "exactly one correct" in str(exc)
    else:
        raise AssertionError("invalid MC question should fail validation")
