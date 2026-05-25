from __future__ import annotations

import re

_DRAFT_ANSWER_PREFIX = re.compile(
    r"^\s*(?:draft\s+(?:answer|response|model answer)\s*(?::|-|\n)|draft\s*(?::|-))\s*",
    re.IGNORECASE,
)


def strip_draft_answer_prefix(answer: str) -> str:
    return _DRAFT_ANSWER_PREFIX.sub("", answer).strip()
