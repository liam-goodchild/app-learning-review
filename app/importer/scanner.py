from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.importer.markdown import ParsedMarkdown, parse_markdown_file
from app.models import Concept, Question, SourceNote, utcnow

RECOGNIZED_STATUSES = {"needs-questions", "questions-drafted", "active", "skip", "stale"}
IGNORED_STATUSES = {"skip"}


@dataclass
class ScanResult:
    scanned: int = 0
    imported: int = 0
    created: int = 0
    updated: int = 0
    skipped_recent: int = 0
    skipped_unmarked: int = 0
    skipped_errors: int = 0
    draft_questions_created: int = 0
    generation_jobs_enqueued: int = 0
    errors: list[str] = field(default_factory=list)


def safe_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _relative_path(path: Path, vault_path: Path) -> str:
    try:
        return str(path.relative_to(vault_path))
    except ValueError:
        return str(path)


def _query_existing(parsed: ParsedMarkdown, relative_path: str) -> Select[tuple[SourceNote]]:
    clauses = [SourceNote.path == relative_path, SourceNote.content_hash == parsed.content_hash]
    learning_app_id = parsed.frontmatter.get("learning_app_id")
    if learning_app_id:
        clauses.append(SourceNote.learning_app_id == str(learning_app_id))
    if parsed.source_urls:
        source_json = safe_json(parsed.source_urls)
        clauses.append((SourceNote.title == parsed.title) & (SourceNote.source_urls_json == source_json))
    return select(SourceNote).where(or_(*clauses)).limit(1)


def _status_for_source(parsed: ParsedMarkdown, existing: SourceNote | None) -> str:
    incoming = parsed.learning_status or "needs-questions"
    if existing and existing.learning_status == "active":
        return "active"
    if incoming == "needs-questions" and existing and existing.questions:
        return "active"
    return incoming


def _upsert_concept(db: Session, source: SourceNote, parsed: ParsedMarkdown) -> None:
    existing = db.scalar(select(Concept).where(Concept.source_note_id == source.id).limit(1))
    if existing:
        existing.name = parsed.title
        existing.description = parsed.frontmatter.get("summary") if isinstance(parsed.frontmatter.get("summary"), str) else existing.description
        existing.tags_json = safe_json(parsed.tags)
        existing.updated_at = utcnow()
        return
    db.add(
        Concept(
            source_note_id=source.id,
            name=parsed.title,
            description=parsed.frontmatter.get("summary") if isinstance(parsed.frontmatter.get("summary"), str) else None,
            tags_json=safe_json(parsed.tags),
        )
    )




def import_markdown_file(db: Session, path: Path, settings: Settings, now: datetime | None = None) -> tuple[SourceNote | None, int]:
    now = now or utcnow()
    parsed = parse_markdown_file(path)
    if parsed.frontmatter_error:
        raise ValueError(f"{path}: malformed frontmatter: {parsed.frontmatter_error}")
    if parsed.learning_status not in RECOGNIZED_STATUSES or parsed.learning_status in IGNORED_STATUSES:
        return None, 0

    relative_path = _relative_path(path, settings.vault_path)
    source = db.scalar(_query_existing(parsed, relative_path))
    created = source is None
    if source is None:
        source = SourceNote(
            learning_app_id=str(parsed.frontmatter.get("learning_app_id")) if parsed.frontmatter.get("learning_app_id") else None,
            title=parsed.title,
            path=relative_path,
            content_hash=parsed.content_hash,
            learning_status=parsed.learning_status or "needs-questions",
        )
        db.add(source)
        db.flush()

    source.learning_app_id = str(parsed.frontmatter.get("learning_app_id")) if parsed.frontmatter.get("learning_app_id") else source.learning_app_id
    source.title = parsed.title
    source.path = relative_path
    source.content_hash = parsed.content_hash
    source.frontmatter_json = safe_json(parsed.frontmatter)
    source.tags_json = safe_json(parsed.tags)
    source.source_urls_json = safe_json(parsed.source_urls)
    source.status = parsed.status
    source.confidence = parsed.confidence
    source.learning_status = _status_for_source(parsed, source)
    source.body = parsed.body
    source.last_imported_at = now
    source.last_seen_at = now
    source.updated_at = now

    _upsert_concept(db, source, parsed)
    db.commit()
    return source, 0


def scan_vault(db: Session, settings: Settings, now: datetime | None = None) -> ScanResult:
    now = now or utcnow()
    result = ScanResult()
    vault_path = settings.vault_path
    if not vault_path.exists():
        result.errors.append(f"Vault path does not exist: {vault_path}")
        return result

    cutoff = now.timestamp() - settings.min_file_age_seconds
    for folder in settings.vault_import_folders:
        root = vault_path / folder
        if not root.exists():
            continue
        for path in root.rglob("*.md"):
            result.scanned += 1
            try:
                if path.stat().st_mtime > cutoff:
                    result.skipped_recent += 1
                    continue
                parsed = parse_markdown_file(path)
                if parsed.frontmatter_error:
                    result.skipped_errors += 1
                    result.errors.append(f"{path}: {parsed.frontmatter_error}")
                    continue
                if parsed.learning_status not in RECOGNIZED_STATUSES or parsed.learning_status in IGNORED_STATUSES:
                    result.skipped_unmarked += 1
                    continue
                relative_path = _relative_path(path, settings.vault_path)
                existed = db.scalar(_query_existing(parsed, relative_path)) is not None
                source, _ = import_markdown_file(db, path, settings, now=now)
                if source is None:
                    continue
                result.imported += 1
                if existed:
                    result.updated += 1
                else:
                    result.created += 1
            except Exception as exc:  # pragma: no cover - defensive logging path
                db.rollback()
                result.skipped_errors += 1
                result.errors.append(f"{path}: {exc}")

    from app.models import GenerationJob
    from app.services import generation as generation_service

    needs = db.scalars(
        select(SourceNote).where(SourceNote.learning_status == "needs-questions")
    ).all()
    for source in needs:
        has_questions = db.scalar(select(func.count(Question.id)).where(Question.source_note_id == source.id))
        if has_questions:
            continue
        has_pending = db.scalar(
            select(func.count(GenerationJob.id)).where(
                GenerationJob.source_note_id == source.id,
                GenerationJob.status.in_(("pending", "running")),
            )
        )
        if has_pending:
            continue
        generation_service.enqueue_generation_job(db, source)
        result.generation_jobs_enqueued += 1
    return result

