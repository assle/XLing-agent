from __future__ import annotations

import re


class PrivacySanitizer:
    """按格式清理部分直接身份信息，并整理供授权审核人员阅读的必要摘要。"""

    # 可识别并替换的身份信息格式。
    PHONE_PATTERN = re.compile(r"1[3-9]\d{9}")
    EMAIL_PATTERN = re.compile(r"[\w.-]+@[\w.-]+\.\w+")
    ID_CARD_PATTERN = re.compile(r"\d{17}[\dXx]")
    NAME_HINT_PATTERN = re.compile(r"(?:我叫|我是|我的名字是|姓名)\s*[^\s,，。.!！?？]{2,4}")

    @classmethod
    def sanitize(cls, text: str) -> str:
        """按预设格式替换手机号、邮箱、身份证号和部分姓名提示文本。

        空文本返回空字符串；使用模式匹配而非完整身份识别，不能保证清除所有个人信息。
        """
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
        """把必要的困境、风险趋势、四维摘要和行动状态整理成人工审核摘要。

        困境与四维摘要经过脱敏；趋势和行动状态按传入文本使用，应由调用方提供适当内容。
        没有任何内容时返回固定占位说明，不自动复制整段对话。
        """
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
