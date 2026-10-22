from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Iterator


def utc_now() -> int:
    return int(time.time())


def as_int(value: Any) -> int:
    try:
        if value is None or value == "":
            return 0
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def deep_get(obj: Any, path: list[str], default: Any = None) -> Any:
    value = obj
    for key in path:
        if not isinstance(value, dict):
            return default
        value = value.get(key)
    return value if value is not None else default


def walk_dicts(obj: Any, path: str = "") -> Iterator[tuple[str, str, Any]]:
    if isinstance(obj, dict):
        for key, value in obj.items():
            current = f"{path}.{key}" if path else key
            yield current, key, value
            yield from walk_dicts(value, current)
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            yield from walk_dicts(value, f"{path}[{index}]")


def find_key(obj: Any, key_names: set[str]) -> Any:
    for _, key, value in walk_dicts(obj):
        if key in key_names and value not in (None, ""):
            return value
    return None


def model_name(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("modelID", "model_id", "model", "name", "id"):
            if value.get(key):
                return str(value[key])
    return None


def fingerprint(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()[:24]


def stable_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def epoch_from_any(value: Any) -> int | None:
    if isinstance(value, str):
        text = value.strip()
        if text:
            try:
                from datetime import datetime
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
                return int(parsed.timestamp())
            except ValueError:
                pass
    number = as_int(value)
    if number <= 0:
        return None
    if number > 10_000_000_000:
        number //= 1000
    return number


def display_model_id(value: Any) -> str:
    text = str(value or "未知模型")
    return text.rsplit("/", 1)[-1].rsplit("::", 1)[-1]
