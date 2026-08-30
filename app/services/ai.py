from __future__ import annotations

import json
from typing import Iterable

import httpx

from app.core.config import Settings
from app.core.enums import IntentType, RiskLevel
from app.schemas.dtos import AiMessage


class PromptTemplates:
    @staticmethod
    def intent_prompt(history: list[AiMessage], user_input: str) -> list[AiMessage]:
        return [
            AiMessage(role="system", content=(
                "你是一个用户意图分类器，只做意图识别，不回答问题。"
                "只输出 CHAT、CONSULT、RISK 之一。CHAT 包含普通闲聊、学习、编程、作业、校园事务；"
                "CONSULT 包含压力、焦虑、低落、失眠、情绪倾诉；RISK 包含自杀、自残、伤人或即时危险信号。"
            )),
            AiMessage(role="user", content=f"最近上下文：\n{format_history(history)}\n\n当前输入：\n{user_input}"),
        ]

    @staticmethod
    def psychology_prompt(history: list[AiMessage], user_input: str) -> list[AiMessage]:
        return [
            AiMessage(role="system", content=(
                "你负责分析校园心理健康消息。只返回严格 JSON："
                '{"emotion":"NORMAL|ANXIETY|DEPRESSED|HIGH_RISK","emotionScore":0.0,'
                '"risk":"LOW|MEDIUM|HIGH","confidence":0.0,"summary":"short reason"}'
            )),
            AiMessage(role="user", content=f"最近上下文：\n{format_history(history)}\n\n当前输入：\n{user_input}"),
        ]

    @staticmethod
    def answer_system_prompt(intent: IntentType, risk: RiskLevel, context: str, display_name: str) -> AiMessage:
        if intent == IntentType.CHAT:
            content = (
                "你是 Xling，一个面向学生的日常陪伴与校园生活助手。"
                "普通学习、编程、校园事务和通用问题请自然、准确、直接地回答。"
                "不要主动做心理测评，不要输出风险等级、心理标签、诊断结论或报告口吻。"
                f"学生显示名：{display_name}"
            )
            return AiMessage(role="system", content=content)
        crisis_rule = ""
        if risk == RiskLevel.HIGH:
            crisis_rule = (
                "\n高风险处理规则：先回应情绪，再关注当前安全；鼓励用户立刻联系身边可信任的人、"
                "学校辅导员/心理中心或当地紧急救助；不提供任何危险操作细节。"
            )
        content = (
            "你是 Xling，一个面向学生的校园心理关怀智能体。"
            "回答要共情、谨慎、非评判，不诊断疾病，不开药，不替代持证心理咨询师。"
            "不要向学生输出风险等级、报告分数或后台标签。"
            "优先基于检索知识回答；知识不足时明确说明并给出安全通用建议。"
            f"\n学生显示名：{display_name}\n检索知识：\n{context}{crisis_rule}"
        )
        return AiMessage(role="system", content=content)

    @staticmethod
    def crisis_acknowledgment() -> str:
        """Fixed acknowledgment sent to the student while a high-risk message
        is pending counselor review (NOT an AI-generated reply)."""
        return (
            "我听到了你，你现在的感受很重要。你的消息已进入人工审核流程。"
            "如果你现在处于紧急情况，请立刻联系身边可信任的人、"
            "学校心理中心，或拨打 24 小时心理援助热线 400-161-9995。你不是一个人。"
        )

    @staticmethod
    def fallback_response() -> str:
        """Fixed safety response sent when a counselor rejects a high-risk message
        (NOT an AI-generated reply)."""
        return (
            "人工审核已收到并重视你的消息。如果你现在需要帮助，"
            "请立刻联系身边可信任的人、学校心理中心，或拨打 24 小时心理援助热线 400-161-9995。"
        )

    @staticmethod
    def sub_query_prompt(query: str, n: int = 3) -> list[AiMessage]:
        return [
            AiMessage(role="system", content=(
                "你是一个搜索查询改写器。给定学生问题，生成适合检索心理知识库的替代查询。"
                f"只返回 JSON 字符串数组，包含 {n} 个改写查询，不要解释。"
                '示例：["查询1","查询2","查询3"]'
            )),
            AiMessage(role="user", content=query),
        ]

    @staticmethod
    def rerank_prompt(query: str, candidates: list[dict]) -> list[AiMessage]:
        import json as _json
        docs = [{"index": i, "source": c["source"], "preview": c["content"][:200]} for i, c in enumerate(candidates)]
        return [
            AiMessage(role="system", content=(
                "你是一个搜索结果重排器。给定查询和候选文档，为每个候选打 0-10 的相关性分数。"
                '只返回 JSON 数组，每个元素含 index 和 score 字段。'
                '示例：[{"index":0,"score":8.5},{"index":1,"score":3.0}]'
            )),
            AiMessage(role="user", content=f"查询：{query}\n\n候选文档：\n{_json.dumps(docs, ensure_ascii=False)}"),
        ]

    @staticmethod
    def classifier_prompt(user_input: str) -> list[AiMessage]:
        return [
            AiMessage(role="system", content=(
                "你是校园心理情绪分类器。只输出一个标签词，不要解释、不要标点。"
                "可选标签：正常、焦虑、低落、高风险。"
                "正常：情绪平稳的日常表达；"
                "焦虑：紧张、担心、压力、未来导向的不安；"
                "低落：压抑、丧失兴趣、疲惫、情绪低沉；"
                "高风险：自伤、自杀、轻生意念或具体计划。"
            )),
            AiMessage(role="user", content=user_input),
        ]


class AiClient:
    def __init__(self, settings: Settings):
        self.settings = settings

    def complete(self, messages: list[AiMessage]) -> str:
        provider = self.settings.ai_provider.lower()
        if provider == "ollama":
            return self._ollama(messages, stream=False)
        if provider == "openai":
            return self._openai(messages, stream=False)
        return self._mock(messages)

    def classify(self, text: str) -> str:
        """Call the local fine-tuned classifier; returns a Chinese label word.

        The classifier is local-only (always via Ollama) regardless of
        AI_PROVIDER; mock provider uses rule-based simulation. Ollama failure
        propagates so the caller can fall back to the conservative path.
        """
        provider = self.settings.ai_provider.lower()
        if provider == "mock":
            return self._mock_classify(text)
        return self._ollama_classify(PromptTemplates.classifier_prompt(text))

    async def aclassify(self, text: str) -> str:
        provider = self.settings.ai_provider.lower()
        if provider == "mock":
            return self._mock_classify(text)
        return await self._ollama_classify_async(PromptTemplates.classifier_prompt(text))

    def risk_logits(self, text: str) -> list[float]:
        return _risk_logits_for_label(self.classify(text))

    async def arisk_logits(self, text: str) -> list[float]:
        return _risk_logits_for_label(await self.aclassify(text))

    def generate_sub_queries(self, query: str, n: int = 3) -> list[str]:
        """Use LLM to rewrite a student question into n sub-queries for multi-query retrieval."""
        messages = PromptTemplates.sub_query_prompt(query, n)
        raw = self.complete(messages)
        try:
            sub_queries = json.loads(raw)
            if isinstance(sub_queries, list) and all(isinstance(q, str) for q in sub_queries):
                return sub_queries[:n]
        except (json.JSONDecodeError, TypeError):
            pass
        return [query]

    def rerank(self, query: str, candidates: list[dict]) -> list[tuple[int, float]]:
        """Batch-score candidates with a single LLM call. Returns (index, score) pairs."""
        if not candidates:
            return []
        messages = PromptTemplates.rerank_prompt(query, candidates)
        raw = self.complete(messages)
        try:
            scored = json.loads(raw)
            if isinstance(scored, list):
                result = []
                for item in scored:
                    if isinstance(item, dict) and "index" in item and "score" in item:
                        idx = int(item["index"])
                        score = float(item["score"])
                        if 0 <= idx < len(candidates):
                            result.append((idx, score))
                if result:
                    return result
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
        return [(i, 0.0) for i in range(len(candidates))]

    async def acomplete(self, messages: list[AiMessage]) -> str:
        provider = self.settings.ai_provider.lower()
        if provider == "ollama":
            return await self._ollama_async(messages)
        if provider == "openai":
            return await self._openai_async(messages)
        return self._mock(messages)

    async def stream(self, messages: list[AiMessage]):
        provider = self.settings.ai_provider.lower()
        if provider == "ollama":
            async for token in self._ollama_stream(messages):
                yield token
            return
        if provider == "openai":
            async for token in self._openai_stream(messages):
                yield token
            return
        text = self._mock(messages)
        for chunk in split_text(text, 12):
            yield chunk

    def _ollama(self, messages: list[AiMessage], stream: bool) -> str:
        payload = {
            "model": self.settings.ollama_model,
            "messages": [m.model_dump() for m in messages],
            "stream": stream,
            "options": {"temperature": self.settings.ai_temperature, "num_predict": self.settings.ai_max_tokens},
        }
        response = httpx.post(f"{self.settings.ollama_base_url}/api/chat", json=payload, timeout=60)
        response.raise_for_status()
        return response.json()["message"]["content"]

    def _ollama_classify(self, messages: list[AiMessage]) -> str:
        payload = {
            "model": self.settings.ollama_classifier_model,
            "messages": [m.model_dump() for m in messages],
            "stream": False,
            "options": {"temperature": 0.1, "num_predict": 16},
        }
        response = httpx.post(f"{self.settings.ollama_base_url}/api/chat", json=payload, timeout=30)
        response.raise_for_status()
        return response.json()["message"]["content"].strip()

    async def _ollama_classify_async(self, messages: list[AiMessage]) -> str:
        payload = {
            "model": self.settings.ollama_classifier_model,
            "messages": [m.model_dump() for m in messages],
            "stream": False,
            "options": {"temperature": 0.1, "num_predict": 16},
        }
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(f"{self.settings.ollama_base_url}/api/chat", json=payload)
            response.raise_for_status()
            return response.json()["message"]["content"].strip()

    async def _ollama_async(self, messages: list[AiMessage]) -> str:
        payload = {
            "model": self.settings.ollama_model,
            "messages": [m.model_dump() for m in messages],
            "stream": False,
            "options": {"temperature": self.settings.ai_temperature, "num_predict": self.settings.ai_max_tokens},
        }
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(f"{self.settings.ollama_base_url}/api/chat", json=payload)
            response.raise_for_status()
            return response.json()["message"]["content"]

    async def _ollama_stream(self, messages: list[AiMessage]):
        payload = {
            "model": self.settings.ollama_model,
            "messages": [m.model_dump() for m in messages],
            "stream": True,
            "options": {"temperature": self.settings.ai_temperature, "num_predict": self.settings.ai_max_tokens},
        }
        async with httpx.AsyncClient(timeout=120) as client:
            async with client.stream("POST", f"{self.settings.ollama_base_url}/api/chat", json=payload) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    data = json.loads(line)
                    token = data.get("message", {}).get("content", "")
                    if token:
                        yield token

    def _openai(self, messages: list[AiMessage], stream: bool) -> str:
        headers = {"Authorization": f"Bearer {self.settings.openai_api_key}"}
        payload = {
            "model": self.settings.openai_model,
            "messages": [m.model_dump() for m in messages],
            "temperature": self.settings.ai_temperature,
            "max_tokens": self.settings.ai_max_tokens,
            "stream": stream,
        }
        response = httpx.post(f"{self.settings.openai_base_url}/chat/completions", headers=headers, json=payload, timeout=120)
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]

    async def _openai_async(self, messages: list[AiMessage]) -> str:
        headers = {"Authorization": f"Bearer {self.settings.openai_api_key}"}
        payload = {
            "model": self.settings.openai_model,
            "messages": [m.model_dump() for m in messages],
            "temperature": self.settings.ai_temperature,
            "max_tokens": self.settings.ai_max_tokens,
            "stream": False,
        }
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(f"{self.settings.openai_base_url}/chat/completions", headers=headers, json=payload)
            response.raise_for_status()
            return response.json()["choices"][0]["message"]["content"]

    async def _openai_stream(self, messages: list[AiMessage]):
        headers = {"Authorization": f"Bearer {self.settings.openai_api_key}"}
        payload = {
            "model": self.settings.openai_model,
            "messages": [m.model_dump() for m in messages],
            "temperature": self.settings.ai_temperature,
            "max_tokens": self.settings.ai_max_tokens,
            "stream": True,
        }
        async with httpx.AsyncClient(timeout=120) as client:
            async with client.stream("POST", f"{self.settings.openai_base_url}/chat/completions", headers=headers, json=payload) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    raw = line.removeprefix("data: ").strip()
                    if raw == "[DONE]":
                        break
                    data = json.loads(raw)
                    token = data["choices"][0].get("delta", {}).get("content", "")
                    if token:
                        yield token

    def _mock_classify(self, text: str) -> str:
        """Rule-based classifier simulation for mock provider."""
        if has_high_risk_signal(text):
            return "高风险"
        lowered = text.lower()
        if any(word in lowered for word in DEPRESSED_WORDS):
            return "低落"
        if has_consult_signal(text):
            return "焦虑"
        return "正常"

    def _mock(self, messages: list[AiMessage]) -> str:
        last = next((m.content for m in reversed(messages) if m.role == "user"), "")
        system = " ".join(m.content for m in messages if m.role == "system")
        if "搜索查询改写器" in system:
            return json.dumps(_mock_sub_queries(last), ensure_ascii=False)
        if "搜索结果重排器" in system:
            return json.dumps(_mock_rerank(last), ensure_ascii=False)
        if "认知行为四维追问" in system:
            return "Four-part extraction is not available in mock mode"
        if "心理行动规划" in system:
            return json.dumps({"items": [
                {"content": "写下三个最担心的事，选一个最小的步骤明天先做", "order": 0},
                {"content": "睡前 30 分钟放下手机，做缓慢呼吸练习", "order": 1},
                {"content": "明天安排一个 25 分钟专注时段，只做最重要的一件事", "order": 2},
            ]})
        if "严格 JSON" in system:
            if has_high_risk_signal(last):
                return '{"emotion":"HIGH_RISK","emotionScore":4.0,"risk":"HIGH","confidence":0.95,"summary":"检测到明确高风险表达"}'
            if has_consult_signal(last):
                return '{"emotion":"ANXIETY","emotionScore":2.5,"risk":"LOW","confidence":0.72,"summary":"检测到压力或情绪求助表达"}'
            return '{"emotion":"NORMAL","emotionScore":0.0,"risk":"LOW","confidence":0.66,"summary":"未检测到明显风险信号"}'
        if "意图分类器" in system:
            if has_high_risk_signal(last):
                return "RISK"
            if has_consult_signal(last):
                return "CONSULT"
            return "CHAT"
        if "当前由 CounselorAgent" in system:
            return "我听到你最近压力很大，还影响到了睡眠，这种状态确实会让人很消耗。你可以先做两件小事：今晚把最担心的事情写成清单，先只选一个最小步骤处理；睡前 30 分钟把手机和学习任务放远一点，用缓慢呼吸或热水澡帮身体降下来。如果这种失眠持续一周以上，建议联系学校心理中心或辅导员一起看一看。"
        if "评审员" in system:
            return '{"empathy":3,"safety":4,"actionability":3,"boundary":4,"empathy_reason":"mock judge","safety_reason":"mock judge","actionability_reason":"mock judge","boundary_reason":"mock judge"}'
        if "当前由 CompanionAgent" in system:
            return "我在。这个问题可以直接拆开来看，我们先从你最想解决的那一部分开始。"
        if "KnowledgeAgent" in system and "SUFFICIENT" in system:
            return "SUFFICIENT"
        if "KnowledgeAgent" in system:
            return last[:40] or "校园心理支持"
        return "我在。先把你现在最具体的困扰说出来，我们可以一步一步拆开。如果情况已经影响安全，请马上联系身边可信任的人或学校心理中心。"


def format_history(history: list[AiMessage]) -> str:
    if not history:
        return "无"
    return "\n".join(f"{m.role}: {m.content}" for m in history[-20:])


HIGH_RISK_WORDS = [
    "自杀", "自残", "不想活", "结束生命", "伤害自己", "伤害别人", "伤害他人",
    "轻生", "准备去死", "suicide", "kill myself", "self harm", "hurt others",
]
CONSULT_WORDS = [
    "焦虑", "抑郁", "低落", "压抑", "低沉", "提不起兴趣", "压力", "失眠",
    "睡不着", "心跳", "胸闷", "胃不舒服", "难过", "崩溃", "痛苦", "无助",
    "心理", "咨询", "担心", "害怕", "逃避", "拖延", "刷手机", "放弃复习",
    "anxious", "depress", "stress",
]

DEPRESSED_WORDS = ["抑郁", "低落", "崩溃", "难过", "丧失", "提不起", "行尸", "压抑", "无意义", "累赘", "想哭", "发呆", "机械", "低沉", "提不起兴趣"]


def has_high_risk_signal(text: str) -> bool:
    normalized = text.lower()
    return any(word in normalized for word in HIGH_RISK_WORDS)


def has_consult_signal(text: str) -> bool:
    normalized = text.lower()
    return any(word in normalized for word in CONSULT_WORDS)


def split_text(text: str, size: int) -> Iterable[str]:
    for index in range(0, len(text), size):
        yield text[index:index + size]


def _risk_logits_for_label(label: str) -> list[float]:
    return {
        "正常": [4.0, 0.0, -4.0],
        "焦虑": [3.0, 1.0, -4.0],
        "低落": [0.0, 4.0, -2.0],
        "高风险": [-4.0, 0.0, 5.0],
    }.get(label.strip(), [0.0, 0.0, 0.0])


_SYNONYM_PAIRS = [
    ("焦虑", "紧张"), ("失眠", "睡眠"), ("压力", "负担"), ("抑郁", "低落"),
    ("考试", "考核"), ("复习", "备考"), ("崩溃", "失控"), ("无助", "无力"),
]


def _mock_sub_queries(query: str) -> list[str]:
    """Generate 3 retrieval-oriented sub-queries without an LLM."""
    result = [query]
    swapped = query
    for old, new in _SYNONYM_PAIRS:
        swapped = swapped.replace(old, new)
    if swapped != query:
        result.append(swapped)
    import re
    concise = re.sub(r"[？?！!。\.]+$", "", query).strip()
    if concise and concise != query:
        result.append(concise)
    while len(result) < 3:
        result.append(query)
    return result[:3]


def _mock_rerank(user_content: str) -> list[dict]:
    """Score candidates by keyword overlap without an LLM."""
    import re
    query_match = re.search(r"查询：(.+?)(\n\n候选文档：)", user_content, re.DOTALL)
    docs_match = re.search(r"候选文档：\n(\[.+\])", user_content, re.DOTALL)
    if not query_match or not docs_match:
        return []
    query = query_match.group(1).strip()
    try:
        candidates = json.loads(docs_match.group(1))
    except json.JSONDecodeError:
        return []
    query_terms = [t for t in re.split(r"[\s，。！？、；：,.!?;:]+", query.lower()) if len(t) >= 2]
    scored = []
    for candidate in candidates:
        preview = (candidate.get("preview") or "").lower()
        matched = sum(1 for term in query_terms if term in preview)
        score = round(min(10.0, matched / max(1, len(query_terms)) * 10.0), 1) if query_terms else 0.0
        scored.append({"index": candidate.get("index", 0), "score": score})
    return scored
