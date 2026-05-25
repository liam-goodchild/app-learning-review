from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.importer.frontmatter import parse_frontmatter

HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)
QUESTION_ITEM_RE = re.compile(r"^\s*(?:[-*+]\s+|\d+[.)]\s+|\[[ xX]\]\s*)(.+?)\s*$")
URL_RE = re.compile(r"https?://[^\s)>\]}]+")


@dataclass(frozen=True)
class Heading:
    level: int
    text: str


@dataclass(frozen=True)
class ParsedMarkdown:
    path: Path
    raw: str
    content_hash: str
    frontmatter: dict[str, Any]
    frontmatter_error: str | None
    body: str
    title: str
    tags: list[str]
    source_urls: list[str]
    status: str | None
    confidence: str | None
    learning_status: str | None
    headings: list[Heading]
    practice_questions: list[str]
    retrieval_schedule: str | None


def content_hash(markdown: str) -> str:
    return hashlib.sha256(markdown.encode("utf-8")).hexdigest()


def normalize_tags(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [tag.strip().lstrip("#") for tag in re.split(r"[, ]+", value) if tag.strip()]
    if isinstance(value, list):
        tags: list[str] = []
        for item in value:
            if isinstance(item, str):
                tags.extend(normalize_tags(item))
        return list(dict.fromkeys(tags))
    return []


def extract_urls(frontmatter: dict[str, Any], body: str = "") -> list[str]:
    urls: list[str] = []
    for key in ("source", "sources", "source_url", "source_urls", "url", "urls"):
        value = frontmatter.get(key)
        values = value if isinstance(value, list) else [value]
        for item in values:
            if isinstance(item, str):
                urls.extend(URL_RE.findall(item))
            elif isinstance(item, dict):
                for nested in item.values():
                    if isinstance(nested, str):
                        urls.extend(URL_RE.findall(nested))
    urls.extend(URL_RE.findall(body))
    return list(dict.fromkeys(urls))


def extract_headings(body: str) -> list[Heading]:
    return [Heading(level=len(match.group(1)), text=match.group(2).strip()) for match in HEADING_RE.finditer(body)]


def first_h1(body: str) -> str | None:
    for match in HEADING_RE.finditer(body):
        if len(match.group(1)) == 1:
            return match.group(2).strip()
    return None


def extract_section(body: str, section_name: str) -> str | None:
    matches = list(HEADING_RE.finditer(body))
    for index, match in enumerate(matches):
        heading = match.group(2).strip().lower().rstrip(":")
        if heading != section_name.lower():
            continue
        level = len(match.group(1))
        start = match.end()
        end = len(body)
        for next_match in matches[index + 1 :]:
            if len(next_match.group(1)) <= level:
                end = next_match.start()
                break
        return body[start:end].strip()
    return None


def extract_practice_questions(body: str) -> list[str]:
    section = extract_section(body, "Practice questions")
    if not section:
        return []

    questions: list[str] = []
    current: list[str] = []

    def flush() -> None:
        if not current:
            return
        text = " ".join(part.strip() for part in current if part.strip()).strip()
        if text and len(text) > 3:
            questions.append(text)
        current.clear()

    for line in section.splitlines():
        stripped = line.strip()
        if not stripped:
            flush()
            continue
        item_match = QUESTION_ITEM_RE.match(line)
        if item_match:
            flush()
            current.append(item_match.group(1))
        elif current and (line.startswith(" ") or line.startswith("\t")):
            current.append(stripped)
        elif stripped.endswith("?"):
            flush()
            current.append(stripped)
            flush()
    flush()
    return list(dict.fromkeys(questions))


def parse_markdown_file(path: Path) -> ParsedMarkdown:
    raw = path.read_text(encoding="utf-8")
    frontmatter_result = parse_frontmatter(raw)
    fm = frontmatter_result.frontmatter
    body = frontmatter_result.body
    title = str(fm.get("title") or first_h1(body) or path.stem)
    return ParsedMarkdown(
        path=path,
        raw=raw,
        content_hash=content_hash(raw),
        frontmatter=fm,
        frontmatter_error=frontmatter_result.error,
        body=body,
        title=title,
        tags=normalize_tags(fm.get("tags")),
        source_urls=extract_urls(fm, body),
        status=str(fm.get("status")) if fm.get("status") is not None else None,
        confidence=str(fm.get("confidence")) if fm.get("confidence") is not None else None,
        learning_status=str(fm.get("learning_status")) if fm.get("learning_status") is not None else None,
        headings=extract_headings(body),
        practice_questions=extract_practice_questions(body),
        retrieval_schedule=extract_section(body, "Retrieval schedule"),
    )

