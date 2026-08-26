from __future__ import annotations

import json
from datetime import datetime
from importlib import import_module

from app.core.config import Settings
from app.models.entities import ChatMessage
from app.schemas.dtos import AiMessage
from app.services.privacy import PrivacySanitizer


class RedisShortTermMemoryStore:
    _fallback: dict[str, list[str]] = {}  # class-level: survives across per-request instances

    def __init__(self, settings: Settings):
        self.settings = settings
        self.privacy = PrivacySanitizer()
        self.client = self._connect()

    def load_recent(self, session_public_id: str) -> list[AiMessage]:
        return self._read(session_public_id, self.settings.redis_memory_max_messages)

    def messages_from_rows(self, rows: list[ChatMessage]) -> list[AiMessage]:
        return [self._message_from_row(row) for row in rows]

    def append(self, session_public_id: str, role: str, content: str) -> None:
        key = self._key(session_public_id)
        payload = self._serialize(role, content)
        try:
            self.client.rpush(key, payload)
            self.client.ltrim(key, -self.settings.redis_memory_max_messages, -1)
            self.client.expire(key, self.settings.redis_memory_ttl_seconds)
        except Exception:
            self._fallback.setdefault(key, []).append(payload)
            self._fallback[key] = self._fallback[key][-self.settings.redis_memory_max_messages:]

    def replace(self, session_public_id: str, messages: list[AiMessage]) -> None:
        key = self._key(session_public_id)
        try:
            pipe = self.client.pipeline()
            pipe.delete(key)
            if messages:
                pipe.rpush(key, *[self._serialize(message.role, message.content) for message in messages])
                pipe.ltrim(key, -self.settings.redis_memory_max_messages, -1)
                pipe.expire(key, self.settings.redis_memory_ttl_seconds)
            pipe.execute()
        except Exception:
            self._fallback[key] = [self._serialize(m.role, m.content) for m in messages][-self.settings.redis_memory_max_messages:]

    def _read(self, session_public_id: str, limit: int) -> list[AiMessage]:
        key = self._key(session_public_id)
        try:
            raw_items = self.client.lrange(key, -limit, -1)
        except Exception:
            raw_items = self._fallback.get(key, [])
        messages = []
        for raw in raw_items:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue
            role = str(data.get("role", "")).lower()
            content = str(data.get("content", ""))
            if role and content:
                messages.append(AiMessage(role=role, content=self.privacy.sanitize(content)))
        return messages

    def _connect(self):
        try:
            redis_module = import_module("redis")
        except ModuleNotFoundError as exc:
            raise RuntimeError("请先安装 requirements.txt 中的 redis 依赖") from exc
        return redis_module.Redis.from_url(
            self.settings.redis_url,
            decode_responses=True,
            socket_timeout=self.settings.redis_socket_timeout_seconds,
            socket_connect_timeout=self.settings.redis_socket_timeout_seconds,
        )

    def _message_from_row(self, row: ChatMessage) -> AiMessage:
        return AiMessage(role=row.role.lower(), content=self.privacy.sanitize(row.content))

    def _serialize(self, role: str, content: str) -> str:
        return json.dumps(
            {
                "role": role.lower(),
                "content": content,
                "createdAt": datetime.utcnow().isoformat(),
            },
            ensure_ascii=False,
        )

    def _key(self, session_public_id: str) -> str:
        return f"xling:short-term-memory:{session_public_id}"

    # ------------------------------------------------------------------
    # Four-part questioning state persistence (issue 08 runtime integration)
    # ------------------------------------------------------------------

    def save_cbt_state(self, session_public_id: str, state: dict) -> None:
        """Persist four-part questioning state across messages in a session."""
        key = f"xling:cbt-state:{session_public_id}"
        try:
            self.client.set(key, json.dumps(state, ensure_ascii=False), ex=self.settings.redis_memory_ttl_seconds)
        except Exception:
            self._fallback[key] = [json.dumps(state, ensure_ascii=False)]

    def load_cbt_state(self, session_public_id: str) -> dict:
        """Load four-part questioning state for a session."""
        key = f"xling:cbt-state:{session_public_id}"
        try:
            raw = self.client.get(key)
            if raw:
                return json.loads(raw)
        except Exception:
            items = self._fallback.get(key, [])
            if items:
                return json.loads(items[-1])
        return {}
