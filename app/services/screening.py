from __future__ import annotations

import json
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.core.time import utc_isoformat
from app.models.entities import ScreeningResult

# ---------------------------------------------------------------------------
# 当前量表题目与选项定义。
# ---------------------------------------------------------------------------

PHQ9_QUESTIONS = [
    "做事时提不起劲或没有兴趣",
    "感到心情低落、沮丧或绝望",
    "入睡困难、睡不安稳，或睡眠过多",
    "感觉疲倦或没有活力",
    "食欲不振或吃太多",
    "觉得自己很糟糕，或觉得自己很失败，或让自己/家人失望",
    "对事物专注有困难，例如阅读报纸或看电视时",
    "动作或说话速度缓慢到他人已经察觉，或正好相反，烦躁或坐立不安",
    "有不如死掉或用某种方式伤害自己的念头",
]

GAD7_QUESTIONS = [
    "感觉紧张、焦虑或急切",
    "无法停止或控制担忧",
    "对各种各样的事情担忧过多",
    "很难放松下来",
    "由于不安而难以静坐",
    "变得容易烦恼或急躁",
    "感到害怕，似乎有可怕的事情发生",
]

ANSWER_OPTIONS = [
    {"value": 0, "label": "完全没有"},
    {"value": 1, "label": "有几天"},
    {"value": 2, "label": "超过一半的天数"},
    {"value": 3, "label": "几乎每天"},
]

DISCLAIMER = "本量表完全自愿；题目、作答和计分结果仅供你查看筛查与趋势，不作诊断，也不提供治疗方案。"
SCORING_METHOD = "各题所选分数相加得到总分；总分分档："
QUESTION_VERSION = "standard-zh-2024"

# 九题量表的最后一题涉及自伤相关想法，列表索引从零开始，因此使用八。
PHQ9_HIGH_RISK_INDEX = 8


@dataclass
class ScreeningScale:
    scale_type: str
    purpose: str
    questions: list[str]
    answer_options: list[dict]
    disclaimer: str
    scoring_rules: str


def get_scale(scale_type: str) -> ScreeningScale:
    """返回支持的自愿量表筛查题目、选项、用途和计分说明。

    支持 PHQ-9（九题低落相关感受筛查）和 GAD-7（七题焦虑相关感受筛查），不作诊断。
    查询量表时不区分大小写，其他名称抛出 ValueError。
    """
    if scale_type.upper() == "PHQ-9":
        return ScreeningScale(
            scale_type="PHQ-9",
            purpose="帮助你了解最近两周的抑郁相关感受和变化趋势。",
            questions=PHQ9_QUESTIONS,
            answer_options=ANSWER_OPTIONS,
            disclaimer=DISCLAIMER,
            scoring_rules=SCORING_METHOD + "0-4=极少, 5-9=轻度, 10-14=中度, 15-19=中重度, 20-27=重度",
        )
    if scale_type.upper() == "GAD-7":
        return ScreeningScale(
            scale_type="GAD-7",
            purpose="帮助你了解最近两周的焦虑相关感受和变化趋势。",
            questions=GAD7_QUESTIONS,
            answer_options=ANSWER_OPTIONS,
            disclaimer=DISCLAIMER,
            scoring_rules=SCORING_METHOD + "0-4=极少, 5-9=轻度, 10-14=中度, 15-21=重度",
        )
    raise ValueError(f"Unknown scale type: {scale_type}. Valid: PHQ-9, GAD-7")


def score_to_severity(scale_type: str, score: int) -> str:
    """按当前量表规则把总分映射成筛查程度标签。

    PHQ-9 使用五档，其他传入值走七题量表的四档规则；调用方应先校验量表名称。
    """
    if scale_type == "PHQ-9":
        if score <= 4:
            return "minimal"
        if score <= 9:
            return "mild"
        if score <= 14:
            return "moderate"
        if score <= 19:
            return "moderately_severe"
        return "severe"
    # 其余值使用七题焦虑量表的分档，调用方需先校验量表类型。
    if score <= 4:
        return "minimal"
    if score <= 9:
        return "mild"
    if score <= 14:
        return "moderate"
    return "severe"


class ScreeningService:
    """保存用户自愿完成的标准化量表答案及筛查结果，不作疾病诊断。"""

    def __init__(self, db: Session):
        """保存量表结果读写使用的数据库会话。

        初始化不开始筛查或生成结果。
        """
        self.db = db

    def get_scale_info(self, scale_type: str) -> dict:
        """向网页提供题目、选项、计分规则、版本及自愿作答说明。

        scale_type 决定量表，内容来自统一定义，避免网页重复维护计分规则。
        """
        scale = get_scale(scale_type)
        return {
            "scaleType": scale.scale_type,
            "purpose": scale.purpose,
            "questions": scale.questions,
            "answerOptions": scale.answer_options,
            "scoringRules": scale.scoring_rules,
            "disclaimer": scale.disclaimer,
            "consent": "开始前请确认：你自愿作答，并了解结果只用于筛查、趋势和求助建议。",
            "questionVersion": QUESTION_VERSION,
        }

    def submit_screening(
        self,
        user_id: int,
        scale_type: str,
        answers: list[int],
        trigger_source: str = "voluntary",
    ) -> ScreeningResult:
        """检查答案数量和零到三的取值范围，计算并保存筛查结果。

        保存逐题答案、题目版本和触发来源；九题量表特定答案大于零时标记需立即关注。
        后续计分和风险检查直接比较传入的标准量表名称，因此调用方应传 PHQ-9 或 GAD-7 的标准写法。
        """
        scale = get_scale(scale_type)
        if len(answers) != len(scale.questions):
            raise ValueError(
                f"Expected {len(scale.questions)} answers for {scale_type}, got {len(answers)}"
            )
        if any(a < 0 or a > 3 for a in answers):
            raise ValueError("Each answer must be 0-3")

        total_score = sum(answers)
        severity = score_to_severity(scale_type, total_score)

        # 单独检查九题量表最后一题，不让总分较低掩盖需立即关注的答案。
        high_risk = False
        if scale_type == "PHQ-9" and len(answers) > PHQ9_HIGH_RISK_INDEX:
            if answers[PHQ9_HIGH_RISK_INDEX] > 0:
                high_risk = True

        result = ScreeningResult(
            user_id=user_id,
            scale_type=scale_type,
            question_version=QUESTION_VERSION,
            answers_json=json.dumps(answers),
            total_score=total_score,
            severity=severity,
            trigger_source=trigger_source,
            high_risk_flagged=high_risk,
        )
        self.db.add(result)
        self.db.commit()
        self.db.refresh(result)
        return result

    def list_results(self, user_id: int) -> list[ScreeningResult]:
        """按创建时间倒序列出指定用户的筛查结果。

        只查自身归属，不把结果自动写入记忆卡片。
        """
        return (
            self.db.query(ScreeningResult)
            .filter(ScreeningResult.user_id == user_id)
            .order_by(ScreeningResult.created_at.desc())
            .all()
        )

    def get_result(self, user_id: int, result_id: int) -> ScreeningResult | None:
        """按结果编号验证用户归属后读取记录。

        缺失或属于其他用户时返回 None。
        """
        result = self.db.get(ScreeningResult, result_id)
        if result is None or result.user_id != user_id:
            return None
        return result

    def to_response(self, result: ScreeningResult) -> dict:
        """把结果中的答案文本还原为列表并整理显示字段。

        同时附上筛查非诊断说明和题目版本，保留分数与高风险答案标志的区别。
        """
        return {
            "id": result.id,
            "scaleType": result.scale_type,
            "questionVersion": result.question_version,
            "answers": json.loads(result.answers_json),
            "totalScore": result.total_score,
            "severity": result.severity,
            "triggerSource": result.trigger_source,
            "highRiskFlagged": result.high_risk_flagged,
            "disclaimer": DISCLAIMER,
            "createdAt": utc_isoformat(result.created_at),
        }
