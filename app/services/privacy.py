from __future__ import annotations

import re


class PrivacySanitizer:
    """Sanitize context for human review (issue 11).

    Removes direct identity information while keeping relevant context
    for reviewer decision-making. Does NOT copy complete conversations,
    screening answers, or direct identity info.
    """

    # Patterns to redact
    PHONE_PATTERN = re.compile(r"1[3-9]\d{9}")
    EMAIL_PATTERN = re.compile(r"[\w.-]+@[\w.-]+\.\w+")
    ID_CARD_PATTERN = re.compile(r"\d{17}[\dXx]")
    NAME_HINT_PATTERN = re.compile(r"(?:我叫|我是|我的名字是|姓名)\s*[^\s,，。.!！?？]{2,4}")

    @classmethod
    def sanitize(cls, text: str) -> str:
        if not text:
            return ""
        result = text
        result = cls.PHONE_PATTERN.sub("[手机号已隐藏]", result)
        result = cls.EMAIL_PATTERN.sub("[邮箱已隐藏]", result)
        result = cls.ID_CARD_PATTERN.sub("[身份证已隐藏]", result)
        result = cls.NAME_HINT_PATTERN.sub("[姓名已隐藏]", result)
        return result

    @classmethod
    def build_review_summary(
        cls,
        current_difficulty: str = "",
        risk_trend: str = "",
        cbt_summary: str = "",
        action_plan_status: str = "",
    ) -> str:
        """Build a desensitized summary for human review."""
        parts = []
        if current_difficulty:
            parts.append(f"当前困境：{cls.sanitize(current_difficulty)}")
        if risk_trend:
            parts.append(f"风险轨迹趋势：{risk_trend}")
        if cbt_summary:
            parts.append(f"认知行为四维追问摘要：{cls.sanitize(cbt_summary)}")
        if action_plan_status:
            parts.append(f"行动计划状态：{action_plan_status}")
        return "\n".join(parts) if parts else "无可用上下文摘要"
