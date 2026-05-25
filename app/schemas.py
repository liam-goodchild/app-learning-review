from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ReviewSubmission:
    answer: str
    rating: str
    confidence: str
