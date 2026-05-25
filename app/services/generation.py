from __future__ import annotations

import json
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.config import Settings
from app.importer.markdown import parse_markdown_file
from app.models import GenerationJob, Question, SourceNote, utcnow
from app.services.json_tools import dumps_json, loads_json

QUESTION_TYPES = {"multiple-choice", "short-answer", "rubric", "categorisation"}
DEFAULT_TYPES = ["multiple-choice", "short-answer", "rubric", "categorisation"]
JOB_STATUSES = {"pending", "running", "succeeded", "failed"}


def _frontmatter(source: SourceNote) -> dict[str, Any]:
    data = loads_json(source.frontmatter_json, {})
    return data if isinstance(data, dict) else {}


def _normalise_types(value: Any) -> list[str]:
    if isinstance(value, str):
        raw = [part.strip() for part in re.split(r"[, ]+", value) if part.strip()]
    elif isinstance(value, list):
        raw = [str(item).strip() for item in value if str(item).strip()]
    else:
        raw = []
    selected = [item for item in raw if item in QUESTION_TYPES]
    return selected or DEFAULT_TYPES


def _question_count(source: SourceNote, requested: int | None) -> int:
    if requested is not None:
        return max(1, min(20, requested))
    value = _frontmatter(source).get("learning_question_goal", 8)
    try:
        return max(1, min(20, int(value)))
    except (TypeError, ValueError):
        return 8


def question_types_for_source(source: SourceNote) -> list[str]:
    return _normalise_types(_frontmatter(source).get("learning_question_types"))


def enqueue_generation_job(
    db: Session,
    source: SourceNote,
    *,
    provider: str = "codex",
    question_count: int | None = None,
    question_types: list[str] | None = None,
) -> GenerationJob:
    types = question_types or question_types_for_source(source)
    job = GenerationJob(
        source_note_id=source.id,
        provider=provider,
        status="pending",
        question_count=_question_count(source, question_count),
        question_types_json=dumps_json(types),
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def jobs_for_source(db: Session, source_id: int) -> list[GenerationJob]:
    return (
        db.execute(
            select(GenerationJob)
            .where(GenerationJob.source_note_id == source_id)
            .order_by(GenerationJob.created_at.desc())
        )
        .scalars()
        .all()
    )


def recent_jobs(db: Session, limit: int = 50) -> list[GenerationJob]:
    return (
        db.execute(
            select(GenerationJob)
            .options(joinedload(GenerationJob.source_note))
            .order_by(GenerationJob.created_at.desc())
            .limit(limit)
        )
        .unique()
        .scalars()
        .all()
    )


def read_source_body(source: SourceNote, settings: Settings) -> str:
    if source.body.strip():
        return source.body
    vault_root = settings.vault_path.resolve()
    candidate = (vault_root / source.path).resolve()
    try:
        candidate.relative_to(vault_root)
    except ValueError:
        return ""
    if not candidate.exists() or candidate.suffix.lower() != ".md":
        return ""
    try:
        return parse_markdown_file(candidate).body
    except Exception:
        return ""


def build_generation_prompt(source: SourceNote, job: GenerationJob, settings: Settings) -> str:
    frontmatter = _frontmatter(source)
    body = read_source_body(source, settings)
    tags = loads_json(source.tags_json, [])
    source_urls = loads_json(source.source_urls_json, [])
    generation_notes = frontmatter.get("learning_generation_notes", "")
    question_types = loads_json(job.question_types_json, DEFAULT_TYPES)
    json_shape = {
        "questions": [
            {
                "type": "multiple-choice | short-answer | rubric | categorisation",
                "prompt": "question prompt",
                "answer": "model answer or correct option id",
                "options": {
                    "options": [{"id": "A", "text": "...", "correct": False, "feedback": "..."}],
                    "categories": [],
                    "items": [],
                },
                "rubric": [{"criterion": "...", "points": 1}],
                "feedback": {"why": "...", "common_misconception": "..."},
                "difficulty": 1,
                "source_reference": source.path,
            }
        ]
    }
    categorisation_shape = {"options": [], "categories": ["..."], "items": [{"text": "...", "category": "..."}]}
    return "\n".join(
        [
            "You are creating draft retrieval-practice questions for a self-hosted learning review app.",
            "",
            "Return JSON only. Do not include Markdown fences, prose, comments, or code blocks.",
            f"Create exactly {job.question_count} questions. Use these question types across the set: {json.dumps(question_types)}.",
            "",
            "Learning principles to apply:",
            "- retrieval practice, not recognition-only",
            "- generation before feedback",
            "- interleaving-ready prompts",
            "- scenario, explain-why, misconception, and application prompts",
            "- useful feedback that explains wrong thinking",
            "",
            "Quality rules:",
            "- Avoid trivial definition-only questions.",
            "- Avoid ambiguous multiple-choice options.",
            "- Every question needs a model answer and feedback.",
            "- Multiple-choice questions need 3-5 options, exactly one correct option, and feedback on every option.",
            "- Rubric questions need criteria with points.",
            "- Categorisation questions need categories and tap-select items.",
            "- Generated questions must be drafts; do not claim they are approved.",
            "",
            "JSON shape:",
            json.dumps(json_shape, indent=2),
            "",
            "The options field is always an object with keys options, categories, and items.",
            "For multiple-choice questions, put choices in options.options and use empty categories/items arrays.",
            "For short-answer and rubric questions, use empty arrays for all three options keys.",
            "For non-rubric questions, use an empty rubric array.",
            "For categorisation questions, put this object in options instead of an array:",
            json.dumps(categorisation_shape, indent=2),
            "",
            "Source note metadata:",
            f"Title: {source.title}",
            f"Path: {source.path}",
            f"Tags: {json.dumps(tags)}",
            f"Source URLs: {json.dumps(source_urls)}",
            f"Learning generation notes: {generation_notes}",
            "",
            "Source note body:",
            "---",
            body[:16000],
            "---",
        ]
    ).strip()


def claim_next_job(db: Session, settings: Settings) -> GenerationJob | None:
    job = (
        db.execute(
            select(GenerationJob)
            .options(joinedload(GenerationJob.source_note))
            .where(GenerationJob.status == "pending")
            .order_by(GenerationJob.created_at.asc())
            .limit(1)
        )
        .unique()
        .scalar_one_or_none()
    )
    if job is None:
        return None
    now = utcnow()
    job.status = "running"
    job.started_at = now
    job.prompt_text = build_generation_prompt(job.source_note, job, settings)
    job.updated_at = now
    db.commit()
    db.refresh(job)
    return job


def extract_json_payload(raw_output: str) -> dict[str, Any]:
    text = raw_output.strip()
    if not text:
        raise ValueError("AI output was empty")
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        return json.loads(fence.group(1))
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        return json.loads(text[start : end + 1])
    raise ValueError("AI output did not contain a JSON object")


def _require_text(item: dict[str, Any], key: str) -> str:
    value = item.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Question is missing {key}")
    return value.strip()


def _feedback(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ValueError("feedback must be an object")
    why = str(value.get("why", "")).strip()
    misconception = str(value.get("common_misconception", "")).strip()
    if not why:
        raise ValueError("feedback.why is required")
    return {"why": why, "common_misconception": misconception}


def validate_generated_payload(raw_output: str) -> list[dict[str, Any]]:
    payload = extract_json_payload(raw_output)
    questions = payload.get("questions")
    if not isinstance(questions, list) or not questions:
        raise ValueError("JSON must contain a non-empty questions array")
    validated: list[dict[str, Any]] = []
    for index, item in enumerate(questions, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"Question {index} must be an object")
        question_type = _require_text(item, "type")
        if question_type not in QUESTION_TYPES:
            raise ValueError(f"Question {index} has unsupported type {question_type}")
        prompt = _require_text(item, "prompt")
        answer = _require_text(item, "answer")
        feedback = _feedback(item.get("feedback"))
        difficulty = item.get("difficulty", 2)
        try:
            difficulty_int = max(1, min(5, int(difficulty)))
        except (TypeError, ValueError):
            difficulty_int = 2
        options = item.get("options", [])
        rubric = item.get("rubric", [])
        if question_type == "multiple-choice":
            if isinstance(options, dict):
                options = options.get("options", [])
            if not isinstance(options, list) or len(options) < 2:
                raise ValueError(f"Question {index} multiple-choice options must be a list with at least two options")
            correct_count = 0
            normalised_options = []
            for option in options:
                if not isinstance(option, dict):
                    raise ValueError(f"Question {index} has a non-object option")
                option_id = _require_text(option, "id")
                option_text = _require_text(option, "text")
                option_feedback = _require_text(option, "feedback")
                is_correct = bool(option.get("correct"))
                correct_count += 1 if is_correct else 0
                normalised_options.append({"id": option_id, "text": option_text, "correct": is_correct, "feedback": option_feedback})
            if correct_count != 1:
                raise ValueError(f"Question {index} must have exactly one correct multiple-choice option")
            options = normalised_options
            rubric = []
        elif question_type == "rubric":
            if not isinstance(rubric, list) or not rubric:
                raise ValueError(f"Question {index} rubric questions require rubric criteria")
            normalised_rubric = []
            for criterion in rubric:
                if not isinstance(criterion, dict):
                    raise ValueError(f"Question {index} has a non-object rubric criterion")
                normalised_rubric.append({"criterion": _require_text(criterion, "criterion"), "points": int(criterion.get("points", 1))})
            rubric = normalised_rubric
            options = []
        elif question_type == "categorisation":
            if not isinstance(options, dict):
                raise ValueError(f"Question {index} categorisation options must be an object")
            categories = options.get("categories")
            items = options.get("items")
            if not isinstance(categories, list) or not categories or not isinstance(items, list) or not items:
                raise ValueError(f"Question {index} categorisation needs categories and items")
            category_set = {str(category) for category in categories}
            normalised_items = []
            for categorisation_item in items:
                if not isinstance(categorisation_item, dict):
                    raise ValueError(f"Question {index} has a non-object categorisation item")
                text = _require_text(categorisation_item, "text")
                category = _require_text(categorisation_item, "category")
                if category not in category_set:
                    raise ValueError(f"Question {index} categorisation item uses an unknown category")
                normalised_items.append({"text": text, "category": category})
            options = {"categories": sorted(category_set), "items": normalised_items}
            rubric = []
        else:
            if not isinstance(options, list):
                options = []
            if not isinstance(rubric, list):
                rubric = []
        validated.append(
            {
                "type": question_type,
                "prompt": prompt,
                "answer": answer,
                "options": options,
                "rubric": rubric,
                "feedback": feedback,
                "difficulty": difficulty_int,
                "source_reference": str(item.get("source_reference") or "").strip(),
            }
        )
    return validated


def import_generated_questions(db: Session, job: GenerationJob, raw_output: str) -> int:
    generated = validate_generated_payload(raw_output)
    existing_prompts = {
        prompt
        for (prompt,) in db.execute(select(Question.prompt).where(Question.source_note_id == job.source_note_id)).all()
    }
    created = 0
    for item in generated:
        if item["prompt"] in existing_prompts:
            continue
        db.add(
            Question(
                source_note_id=job.source_note_id,
                type=item["type"],
                prompt=item["prompt"],
                answer=item["answer"],
                options_json=dumps_json(item["options"]),
                rubric_json=dumps_json(item["rubric"]),
                feedback_json=dumps_json(item["feedback"]),
                source_reference=item["source_reference"] or job.source_note.path,
                difficulty=item["difficulty"],
                status="draft",
            )
        )
        created += 1
    if created and job.source_note.learning_status != "active":
        job.source_note.learning_status = "questions-drafted"
    return created


def complete_generation_job(db: Session, job: GenerationJob, raw_output: str) -> GenerationJob:
    now = utcnow()
    created = import_generated_questions(db, job, raw_output)
    job.status = "succeeded"
    job.raw_output = raw_output
    job.error = ""
    job.draft_questions_created = created
    job.completed_at = now
    job.updated_at = now
    db.commit()
    db.refresh(job)
    return job


def fail_generation_job(db: Session, job: GenerationJob, error: str, raw_output: str = "") -> GenerationJob:
    now = utcnow()
    job.status = "failed"
    job.error = error[:8000]
    job.raw_output = raw_output[:200000]
    job.completed_at = now
    job.updated_at = now
    db.commit()
    db.refresh(job)
    return job
