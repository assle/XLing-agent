from __future__ import annotations

import json
from importlib import import_module

from app.core.config import Settings
from app.core.time import utc_now
from app.models.entities import ChatMessage
from app.schemas.dtos import AiMessage
from app.services.privacy import PrivacySanitizer


class RedisShortTermMemoryStore:
    _fallback: dict[str, list[str]] = {}  # 当前进程的不同请求实例共享这份备用数据。

    def __init__(self, settings: Settings):
        """准备短期会话缓存、隐私处理器和缓存客户端。

        settings 提供容量、超时和连接地址；缺少缓存库依赖时初始化失败，连接操作错误由各读写入口处理。
        """
        self.settings = settings
        self.privacy = PrivacySanitizer()
        self.client = self._connect()

    def load_recent(self, session_public_id: str) -> list[AiMessage]:
        """读取指定公开会话编号的最近消息。

        消息数量上限来自配置；读取时会对正文进行脱敏处理。
        """
        return self._read(session_public_id, self.settings.redis_memory_max_messages)

    def delete_sessions(self, session_public_ids: list[str]) -> int:
        """清除指定会话的消息、四维状态和进程内备用数据。

        删除失败向调用方抛出，不能用备用路径把外部数据尚未删除当作成功。
        """
        keys = [
            key
            for session_public_id in session_public_ids
            for key in (self._key(session_public_id), self._cbt_key(session_public_id))
        ]
        if not keys:
            return 0
        deleted = self.client.delete(*keys)
        for key in keys:
            self._fallback.pop(key, None)
        return deleted

    def messages_from_rows(self, rows: list[ChatMessage]) -> list[AiMessage]:
        """把数据库消息记录转换成供模型使用的消息列表。

        保持传入顺序，对角色名称转小写，并通过隐私处理器清理正文中的匹配信息。
        """
        return [self._message_from_row(row) for row in rows]

    def append(self, session_public_id: str, role: str, content: str) -> None:
        """追加一条短期消息，并限制缓存长度和有效期。

        session_public_id 确定缓存键，role 和 content 提供消息；缓存服务报错时写入当前进程共享的备用字典。
        备用字典只保存在进程内，不具备外部缓存的持久性或过期机制。
        """
        key = self._key(session_public_id)
        payload = self._serialize(role, content)
        try:
            self.client.rpush(key, payload)
            self.client.ltrim(key, -self.settings.redis_memory_max_messages, -1)
            # 每次追加都会刷新外部缓存有效期；进程内备用列表没有对应过期机制。
            self.client.expire(key, self.settings.redis_memory_ttl_seconds)
        except Exception:
            self._fallback.setdefault(key, []).append(payload)
            self._fallback[key] = self._fallback[key][-self.settings.redis_memory_max_messages:]

    def replace(self, session_public_id: str, messages: list[AiMessage]) -> None:
        """用给定消息列表重建一个会话的短期缓存。

        将删除、追加、裁剪和设置有效期放入客户端批处理；失败时改写进程内备用列表。
        消息为空时清除外部缓存键，备用路径也会得到空列表。
        """
        key = self._key(session_public_id)
        try:
            # 把同一会话的替换操作组合提交，避免逐次往返缓存服务。
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
        """读取最近消息并跳过无法解析的条目。

        limit 指定外部缓存读取数量；只有读取抛错时才取备用数据，正常返回空列表不会自动切换。
        角色和内容都非空时才转换成消息对象，正文在此脱敏后交给模型。
        """
        key = self._key(session_public_id)
        try:
            raw_items = self.client.lrange(key, -limit, -1)
        except Exception:
            # 备用数据来自本进程的共享字典，无法保证其他进程能看到相同内容。
            raw_items = self._fallback.get(key, [])
        messages = []
        for raw in raw_items:
            try:
                data = json.loads(raw)
            # 单条缓存文本损坏时跳过该条，不因此丢掉其余可用历史。
            except json.JSONDecodeError:
                continue
            role = str(data.get("role", "")).lower()
            content = str(data.get("content", ""))
            # 只把具备角色和正文的消息送入上下文，并在返回前对正文进行脱敏。
            if role and content:
                messages.append(AiMessage(role=role, content=self.privacy.sanitize(content)))
        return messages

    def _connect(self):
        """创建配置了读取和连接超时的 Redis 客户端。

        Redis 是用于快速保存短期会话数据的缓存服务；创建客户端不等于已成功完成一次读写。
        """
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
        """将一条数据库消息转换为模型消息并清理正文。

        返回仅包含小写角色和脱敏内容的对象，不更改数据库原记录。
        """
        return AiMessage(role=row.role.lower(), content=self.privacy.sanitize(row.content))

    def _serialize(self, role: str, content: str) -> str:
        """把角色、原始消息内容和当前时间编码成缓存文本。

        保留中文字符便于查看；此处不脱敏，隐私处理发生在读取和数据库消息转换时。
        """
        return json.dumps(
            {
                "role": role.lower(),
                "content": content,
                "createdAt": utc_now().isoformat(),
            },
            ensure_ascii=False,
        )

    def _key(self, session_public_id: str) -> str:
        """根据公开会话编号构造短期消息缓存键。

        固定前缀将对话消息与四维状态等其他缓存内容分开。
        """
        return f"xling:short-term-memory:{session_public_id}"

    @staticmethod
    def _cbt_key(session_public_id: str) -> str:
        return f"xling:cbt-state:{session_public_id}"

    # ------------------------------------------------------------------
    # 保存和读取会话内四维追问进度。
    # ------------------------------------------------------------------

    def save_cbt_state(self, session_public_id: str, state: dict) -> None:
        """保存一个会话跨消息累计的四维追问状态。

        state 是状态字典，外部缓存设置配置中的有效期；写入报错时存入进程内备用字典。
        """
        key = self._cbt_key(session_public_id)
        try:
            self.client.set(key, json.dumps(state, ensure_ascii=False), ex=self.settings.redis_memory_ttl_seconds)
        except Exception:
            self._fallback[key] = [json.dumps(state, ensure_ascii=False)]

    def load_cbt_state(self, session_public_id: str) -> dict:
        """恢复会话的四维追问状态，缺少记录时返回空字典。

        外部读取或解析抛错后才读取备用项；正常读到空值不会读取备用项，备用内容解析失败仍会抛错。
        """
        key = self._cbt_key(session_public_id)
        try:
            raw = self.client.get(key)
            if raw:
                return json.loads(raw)
        except Exception:
            items = self._fallback.get(key, [])
            if items:
                return json.loads(items[-1])
        return {}
