from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any
import json


@dataclass(slots=True)
class UsageEvent:
    event_key: str
    app_type: str
    model: str | None = None
    session_id: str | None = None
    turn_id: str | None = None
    request_id: str | None = None
    occurred_at: int | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0
    input_token_semantics: str | None = None
    source_kind: str = "unknown"
    source_path: str | None = None
    source_offset: int | None = None
    source_event_id: str | None = None
    capture_mode: str = "file"
    raw_usage_json: str | None = None

    def as_db_tuple(self) -> tuple[Any, ...]:
        return (
            self.event_key,
            self.app_type,
            self.model,
            self.session_id,
            self.turn_id,
            self.request_id,
            self.occurred_at,
            self.input_tokens,
            self.output_tokens,
            self.cache_read_tokens,
            self.cache_creation_tokens,
            self.reasoning_tokens,
            self.total_tokens,
            self.input_token_semantics,
            self.source_kind,
            self.source_path,
            self.source_offset,
            self.source_event_id,
            self.capture_mode,
            self.raw_usage_json,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @staticmethod
    def json_usage(value: Any) -> str | None:
        if value is None:
            return None
        try:
            return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError):
            return None
