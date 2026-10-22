from __future__ import annotations

import json
from typing import Any

from .config import DATA_DIR

PATH = DATA_DIR / "preferences.json"


def load() -> dict[str, Any]:
    try:
        value = json.loads(PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def get(key: str, default: Any = None) -> Any:
    return load().get(key, default)


def update(**values: Any) -> None:
    data = load()
    data.update(values)
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        temporary = PATH.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(PATH)
    except OSError:
        pass
