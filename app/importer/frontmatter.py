from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import yaml


@dataclass(frozen=True)
class FrontmatterResult:
    frontmatter: dict[str, Any]
    body: str
    error: str | None = None


def parse_frontmatter(markdown: str) -> FrontmatterResult:
    if not markdown.startswith("---"):
        return FrontmatterResult(frontmatter={}, body=markdown)

    lines = markdown.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return FrontmatterResult(frontmatter={}, body=markdown)

    closing_index: int | None = None
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() in {"---", "..."}:
            closing_index = index
            break

    if closing_index is None:
        return FrontmatterResult(frontmatter={}, body=markdown, error="Missing closing frontmatter delimiter")

    yaml_text = "".join(lines[1:closing_index])
    body = "".join(lines[closing_index + 1 :])
    try:
        parsed = yaml.safe_load(yaml_text) if yaml_text.strip() else {}
    except yaml.YAMLError as exc:
        return FrontmatterResult(frontmatter={}, body=body, error=str(exc))

    if parsed is None:
        parsed = {}
    if not isinstance(parsed, dict):
        return FrontmatterResult(frontmatter={}, body=body, error="Frontmatter must be a mapping")
    return FrontmatterResult(frontmatter=parsed, body=body)

