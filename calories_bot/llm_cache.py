from __future__ import annotations

import hashlib
import json
from typing import Any, Protocol


class LLMResponseCache(Protocol):
    """Durable exact-match cache used by every OpenAI-backed operation."""

    def get_cached_llm_response(self, operation: str, cache_key: str) -> str | None: ...

    def store_cached_llm_response(
        self, operation: str, cache_key: str, response_json: str
    ) -> None: ...


def llm_cache_key(operation: str, payload: dict[str, Any]) -> str:
    """Return a stable privacy-preserving key for a complete model request."""

    canonical = json.dumps(
        {"operation": operation, **payload},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def content_sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()
