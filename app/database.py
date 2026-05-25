from __future__ import annotations

from pathlib import Path
from typing import Iterator
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.models import Base


settings = get_settings()


def _ensure_sqlite_parent(database_url: str) -> None:
    if not database_url.startswith("sqlite:///"):
        return
    path_text = database_url.removeprefix("sqlite:///")
    if path_text in {"", ":memory:"}:
        return
    Path(path_text).parent.mkdir(parents=True, exist_ok=True)


_ensure_sqlite_parent(settings.database_url)

connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, connect_args=connect_args, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def run_lightweight_migrations() -> None:
    inspector = inspect(engine)
    if "source_notes" not in inspector.get_table_names():
        return
    column_names = {column["name"] for column in inspector.get_columns("source_notes")}
    if "body" not in column_names:
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE source_notes ADD COLUMN body TEXT NOT NULL DEFAULT ''"))


def init_db() -> None:
    Base.metadata.create_all(bind=engine)
    run_lightweight_migrations()


def get_session() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()

