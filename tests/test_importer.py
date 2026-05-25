from __future__ import annotations

import shutil
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.importer.frontmatter import parse_frontmatter
from app.importer.markdown import parse_markdown_file
from app.importer.scanner import scan_vault
from app.models import Base, Question, SourceNote


def make_session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def make_settings(vault_path: Path) -> Settings:
    return Settings(
        app_host="127.0.0.1",
        app_port=8080,
        database_url="sqlite:///:memory:",
        vault_path=vault_path,
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
    )


def copy_fixture(tmp_path: Path, fixture_name: str, folder: str = "00 - Inbox", name: str | None = None) -> Path:
    target_dir = tmp_path / folder
    target_dir.mkdir(parents=True, exist_ok=True)
    source = Path(__file__).parent / "fixtures" / fixture_name
    target = target_dir / (name or fixture_name)
    shutil.copyfile(source, target)
    return target


def test_frontmatter_and_markdown_parsing():
    path = Path(__file__).parent / "fixtures" / "marked.md"
    parsed = parse_markdown_file(path)
    assert parsed.learning_status == "needs-questions"
    assert parsed.title == "Terraform Variables and Outputs"
    assert parsed.tags == ["terraform", "iac"]
    assert len(parsed.practice_questions) == 2


def test_markdown_without_frontmatter_is_not_marked():
    path = Path(__file__).parent / "fixtures" / "no_frontmatter.md"
    parsed = parse_markdown_file(path)
    assert parsed.frontmatter == {}
    assert parsed.learning_status is None
    assert parsed.practice_questions == ["What is missing from this file?"]


def test_malformed_frontmatter_reports_error():
    raw = (Path(__file__).parent / "fixtures" / "malformed.md").read_text()
    parsed = parse_frontmatter(raw)
    assert parsed.error is not None


def test_scan_imports_marked_notes_idempotently(tmp_path):
    copy_fixture(tmp_path, "marked.md")
    copy_fixture(tmp_path, "unmarked.md")
    db = make_session(tmp_path)
    settings = make_settings(tmp_path)

    first = scan_vault(db, settings)
    second = scan_vault(db, settings)

    assert first.imported == 1
    assert first.created == 1
    assert first.draft_questions_created == 2
    assert second.imported == 1
    assert second.created == 0
    assert db.scalar(select(SourceNote).where(SourceNote.title == "Terraform Variables and Outputs")) is not None
    assert len(db.scalars(select(Question)).all()) == 2


def test_scan_updates_content_hash_without_duplicate(tmp_path):
    path = copy_fixture(tmp_path, "marked.md")
    db = make_session(tmp_path)
    settings = make_settings(tmp_path)
    scan_vault(db, settings)
    original = db.scalar(select(SourceNote))
    original_hash = original.content_hash

    path.write_text(path.read_text() + "\nAdded line.\n")
    scan_vault(db, settings)

    notes = db.scalars(select(SourceNote)).all()
    assert len(notes) == 1
    assert notes[0].content_hash != original_hash


def test_moved_note_detection_by_hash(tmp_path):
    original_path = copy_fixture(tmp_path, "marked.md", "00 - Inbox", "moved.md")
    db = make_session(tmp_path)
    settings = make_settings(tmp_path)
    scan_vault(db, settings)

    moved_dir = tmp_path / "02 - Notes"
    moved_dir.mkdir(parents=True, exist_ok=True)
    moved_path = moved_dir / "moved.md"
    shutil.move(str(original_path), str(moved_path))
    scan_vault(db, settings)

    notes = db.scalars(select(SourceNote)).all()
    assert len(notes) == 1
    assert notes[0].path == "02 - Notes/moved.md"
