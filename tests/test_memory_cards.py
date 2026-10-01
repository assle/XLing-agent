"""Tests for issue 04: Memory cards + no-memory session.

Covers:
  - MemoryCardService CRUD (create, list, update, delete)
  - Suggestion + confirmation flow (unconfirmed not in context)
  - User isolation
  - API endpoints (GET/POST/PUT/DELETE/confirm)
  - Confirmed context for agent runtime

Run: python -m pytest tests/test_memory_cards.py
"""
from __future__ import annotations

from app.agents.langgraph_runtime import LangGraphAgentRuntimeService
from app.api.routes import router
from app.core.config import Settings
from app.core.security import hash_password
from app.models.entities import ChatSession, MemoryCard, UserAccount
from app.schemas.dtos import AiMessage
from app.services.memory_cards import MemoryCardService
from tests.support import ApiHarness, build_runtime

_harness = ApiHarness(router)
_TestSession = _harness.sessions
client = _harness.client

def _seed():
    """创建两个不同用户，供记忆卡片访问隔离测试。

    提交账户后关闭连接。
    """
    db = _TestSession()
    try:
        s1 = UserAccount(username="student", display_name="S1", password_hash=hash_password("student123"))
        s1.roles = {"ROLE_USER"}
        s2 = UserAccount(username="student2", display_name="S2", password_hash=hash_password("pass2"))
        s2.roles = {"ROLE_USER"}
        db.add_all([s1, s2])
        db.commit()
    finally:
        db.close()

_seed()

def _token(u="student", p="student123"):
    """使用指定测试账户取得登录凭证。

    默认返回第一位普通用户的凭证。
    """
    r = client.post("/api/auth/login", json={"username": u, "password": p})
    return r.json()["accessToken"]

def _auth(t):
    """生成当前请求需要的凭证头。

    只封装格式，不代替真实身份验证。
    """
    return {"Authorization": f"Bearer {t}"}

def _clean():
    """清空记忆卡片记录并确认保存。

    保留账户，避免前一测试内容干扰下一例。
    """
    db = _TestSession()
    try:
        db.query(MemoryCard).delete()
        db.commit()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Service: CRUD
# ---------------------------------------------------------------------------

def test_create_card():
    """主动创建一条卡片。

    检查取得编号、内容正确且用户主动创建的卡片默认已确认。
    """
    _clean()
    db = _TestSession()
    try:
        svc = MemoryCardService(db)
        card = svc.create_card(1, "和室友关系紧张影响复习")
        assert card.id is not None
        assert card.confirmed is True
        assert card.content == "和室友关系紧张影响复习"
    finally:
        db.close()

def test_list_cards():
    """给同一用户建立两张卡片。

    检查列表数量与创建数量一致。
    """
    _clean()
    db = _TestSession()
    try:
        svc = MemoryCardService(db)
        svc.create_card(1, "card 1")
        svc.create_card(1, "card 2")
        cards = svc.list_cards(1)
        assert len(cards) == 2
    finally:
        db.close()

def test_update_card():
    """创建卡片后修改其内容。

    检查返回对象使用新正文。
    """
    _clean()
    db = _TestSession()
    try:
        svc = MemoryCardService(db)
        card = svc.create_card(1, "original")
        updated = svc.update_card(1, card.id, "updated content")
        assert updated.content == "updated content"
    finally:
        db.close()

def test_delete_card():
    """创建再删除一张自有卡片。

    检查列表不再包含记录。
    """
    _clean()
    db = _TestSession()
    try:
        svc = MemoryCardService(db)
        card = svc.create_card(1, "to delete")
        svc.delete_card(1, card.id)
        assert len(svc.list_cards(1)) == 0
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Suggestion + confirmation
# ---------------------------------------------------------------------------

def test_suggested_card_not_in_context():
    """创建一张系统建议卡片但不确认。

    检查管理列表可见，而长期记忆上下文仍为空。
    """
    _clean()
    db = _TestSession()
    try:
        svc = MemoryCardService(db)
        svc.suggest_card(1, "system suggestion")
        # Unconfirmed cards should not appear in context
        assert svc.get_confirmed_context(1) == ""
        # But should appear in list with include_pending
        all_cards = svc.list_cards(1)
        assert len(all_cards) == 1
        assert all_cards[0].confirmed is False
    finally:
        db.close()

def test_confirm_card_adds_to_context():
    """先检查建议卡片不进入上下文，再主动确认。

    检查确认后正文才出现在长期记忆中。
    """
    _clean()
    db = _TestSession()
    try:
        svc = MemoryCardService(db)
        card = svc.suggest_card(1, "important context")
        assert svc.get_confirmed_context(1) == ""
        svc.confirm_card(1, card.id)
        context = svc.get_confirmed_context(1)
        assert "important context" in context
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 不同用户的数据访问隔离。
# ---------------------------------------------------------------------------

def test_user_isolation():
    """分别给两位用户建卡，再尝试跨用户修改。

    检查列表独立且越权修改抛错。
    """
    _clean()
    db = _TestSession()
    try:
        svc = MemoryCardService(db)
        svc.create_card(1, "user 1 card")
        svc.create_card(2, "user 2 card")
        assert len(svc.list_cards(1)) == 1
        assert len(svc.list_cards(2)) == 1
        # User 1 can't access user 2's card
        try:
            svc.update_card(1, svc.list_cards(2)[0].id, "hacked")
            assert False, "Should have raised"
        except ValueError:
            pass
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 通过接口验证请求与响应。
# ---------------------------------------------------------------------------

def test_api_create_and_list():
    """经接口新建卡片后读取列表。

    检查保存内容能由列表接口返回。
    """
    _clean()
    t = _token()
    r = client.post("/api/memory-cards", json={"content": "API card"}, headers=_auth(t))
    assert r.status_code == 200
    r = client.get("/api/memory-cards", headers=_auth(t))
    assert r.status_code == 200
    assert len(r.json()) == 1
    assert r.json()[0]["content"] == "API card"

def test_api_update():
    """通过接口修改已创建卡片。

    检查响应成功且正文更新。
    """
    _clean()
    t = _token()
    r = client.post("/api/memory-cards", json={"content": "original"}, headers=_auth(t))
    card_id = r.json()["id"]
    r = client.put(f"/api/memory-cards/{card_id}", json={"content": "updated"}, headers=_auth(t))
    assert r.status_code == 200
    assert r.json()["content"] == "updated"

def test_api_delete():
    """通过接口删除已有卡片再查询列表。

    检查服务端实际不再返回该记录。
    """
    _clean()
    t = _token()
    r = client.post("/api/memory-cards", json={"content": "to delete"}, headers=_auth(t))
    card_id = r.json()["id"]
    r = client.delete(f"/api/memory-cards/{card_id}", headers=_auth(t))
    assert r.status_code == 200
    r = client.get("/api/memory-cards", headers=_auth(t))
    assert len(r.json()) == 0

def test_api_confirm():
    """先准备系统建议卡片，再通过接口确认。

    检查返回的确认标志为真。
    """
    _clean()
    db = _TestSession()
    try:
        svc = MemoryCardService(db)
        card = svc.suggest_card(1, "needs confirmation")
        card_id = card.id
    finally:
        db.close()
    t = _token()
    r = client.post(f"/api/memory-cards/{card_id}/confirm", headers=_auth(t))
    assert r.status_code == 200
    assert r.json()["confirmed"] is True

def test_api_user_isolation():
    """第一位用户建卡，第二位用户查询自己的列表。

    检查看不到第一位用户的内容。
    """
    _clean()
    t1 = _token("student", "student123")
    t2 = _token("student2", "pass2")
    client.post("/api/memory-cards", json={"content": "user1"}, headers=_auth(t1))
    r = client.get("/api/memory-cards", headers=_auth(t2))
    assert len(r.json()) == 0

def test_api_requires_auth():
    """未登录查询记忆卡片。

    检查接口返回 401。
    """
    r = client.get("/api/memory-cards")
    assert r.status_code == 401


def test_no_memory_session_does_not_load_long_term_context(monkeypatch):
    """为无记忆会话注入可记录调用的背景与卡片服务。

    检查两者完全未创建，跨会话上下文保持为空，同时本会话历史仍可读取。
    """
    calls = {"profile": 0, "cards": 0}

    class ProfileSpy:
        def __init__(self, db):
            """记录支持背景服务是否被创建。通过外层计数判断执行器是否访问跨会话背景。
            """
            calls["profile"] += 1

        def get_support_context(self, user_id):  # noqa: ANN001
            """返回固定支持背景作为可识别标记。测试据此检查内容是否进入模型上下文。
            """
            return "当前关注：工作压力"

    class CardSpy:
        def __init__(self, db):
            """记录记忆卡片服务是否被创建。让无记忆和普通会话测试比较访问次数。
            """
            calls["cards"] += 1

        def get_confirmed_context(self, user_id):  # noqa: ANN001
            """返回预设的跨会话记忆文本。仅用于识别执行器是否读取了卡片背景。
            """
            return "secret card"

    class MemorySpy:
        def load_recent(self, session_id):  # noqa: ANN001
            """返回同会话的一条既有消息。使测试只关注长期记忆开关，不进入数据库历史恢复。
            """
            return [AiMessage(role="user", content="本次会话内容")]

        def replace(self, session_id, messages):  # noqa: ANN001
            """提供不执行操作的缓存替换入口。

            该测试已提供非空历史，无需实际回填。
            """
            pass

    runtime = build_runtime(
        LangGraphAgentRuntimeService,
        db=object(),
        settings=Settings(ai_provider="mock", langgraph_checkpoint_backend="memory", knowledge_vector_enabled=False),
        memory=MemorySpy(),
    )
    # 匿名替身返回可等待的固定摘要，排除模型请求对长期记忆访问测试的干扰。
    runtime._summarize_memory = lambda history, current: _async_value("本次会话摘要")
    monkeypatch.setattr("app.agents.langgraph_runtime.UserProfileService", ProfileSpy)
    monkeypatch.setattr("app.agents.langgraph_runtime.MemoryCardService", CardSpy)
    import asyncio
    result = asyncio.run(runtime.run(
        UserAccount(id=1, display_name="测试用户"),
        ChatSession(id=1, public_id="no-memory-session", user_id=1, no_memory=True),
        "帮我写代码", "帮我写代码",
    ))

    assert calls == {"profile": 0, "cards": 0}
    prompt = "\n".join(message.content for message in result.response_messages)
    assert "本次会话摘要" in prompt
    assert "secret card" not in prompt
    assert "当前关注：工作压力" not in prompt


def test_normal_session_loads_long_term_context(monkeypatch):
    """为普通会话注入背景和卡片观察替身。

    检查各读取服务创建一次，两个上下文字段都获得预设内容。
    """
    calls = {"profile": 0, "cards": 0}

    class ProfileSpy:
        def __init__(self, db):
            """记录支持背景服务是否被创建。通过外层计数判断执行器是否访问跨会话背景。
            """
            calls["profile"] += 1

        def get_support_context(self, user_id):  # noqa: ANN001
            """返回固定支持背景作为可识别标记。测试据此检查内容是否进入模型上下文。
            """
            return "当前关注：工作压力"

    class CardSpy:
        def __init__(self, db):
            """记录记忆卡片服务是否被创建。让无记忆和普通会话测试比较访问次数。
            """
            calls["cards"] += 1

        def get_confirmed_context(self, user_id):  # noqa: ANN001
            """返回预设的跨会话记忆文本。仅用于识别执行器是否读取了卡片背景。
            """
            return "known context"

    class MemorySpy:
        def load_recent(self, session_id):  # noqa: ANN001
            """返回同会话的一条既有消息。使测试只关注长期记忆开关，不进入数据库历史恢复。
            """
            return [AiMessage(role="user", content="本次会话内容")]

    runtime = build_runtime(
        LangGraphAgentRuntimeService,
        db=object(),
        settings=Settings(ai_provider="mock", langgraph_checkpoint_backend="memory", knowledge_vector_enabled=False),
        memory=MemorySpy(),
    )
    # 返回可等待的固定摘要，隔离模型调用对记忆权限测试的影响。
    runtime._summarize_memory = lambda history, current: _async_value("本次会话摘要")
    monkeypatch.setattr("app.agents.langgraph_runtime.UserProfileService", ProfileSpy)
    monkeypatch.setattr("app.agents.langgraph_runtime.MemoryCardService", CardSpy)
    import asyncio
    result = asyncio.run(runtime.run(
        UserAccount(id=1, display_name="测试用户"),
        ChatSession(id=1, public_id="normal-session", user_id=1, no_memory=False),
        "帮我写代码", "帮我写代码",
    ))

    assert calls == {"profile": 1, "cards": 1}
    prompt = "\n".join(message.content for message in result.response_messages)
    assert "当前关注：工作压力" in prompt
    assert "known context" in prompt


async def _async_value(value):
    """把给定值包装为可等待的结果。

    供同步匿名替身替代原异步摘要方法。
    """
    return value
