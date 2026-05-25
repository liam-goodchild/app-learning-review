from __future__ import annotations

import json
from typing import Any


def loads_json(text: str | None, default: Any) -> Any:
    if text is None or str(text).strip() == "":
        return default
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return default


def dumps_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)

