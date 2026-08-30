from __future__ import annotations

import json
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models.entities import ScreeningResult

# ---------------------------------------------------------------------------
# Question definitions (standard Chinese versions)
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

DISCLAIMER = "本量表仅用于筛查和趋势参考，不作诊断。如有需要请咨询专业人士。"
QUESTION_VERSION = "standard-zh-2024"

# PHQ-9 Q9 (index 8) asks about self-harm/suicide
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
    if scale_type.upper() == "PHQ-9":
        return ScreeningScale(
            scale_type="PHQ-9",
            purpose="帮助你了解最近两周的抑郁相关感受和变化趋势。",
            questions=PHQ9_QUESTIONS,
            answer_options=ANSWER_OPTIONS,
            disclaimer=DISCLAIMER,
            scoring_rules="0-4=极少, 5-9=轻度, 10-14=中度, 15-19=中重度, 20-27=重度",
        )
    if scale_type.upper() == "GAD-7":
        return ScreeningScale(
            scale_type="GAD-7",
            purpose="帮助你了解最近两周的焦虑相关感受和变化趋势。",
            questions=GAD7_QUESTIONS,
            answer_options=ANSWER_OPTIONS,
            disclaimer=DISCLAIMER,
            scoring_rules="0-4=极少, 5-9=轻度, 10-14=中度, 15-21=重度",
        )
    raise ValueError(f"Unknown scale type: {scale_type}. Valid: PHQ-9, GAD-7")


def score_to_severity(scale_type: str, score: int) -> str:
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
    # GAD-7
    if score <= 4:
        return "minimal"
    if score <= 9:
        return "mild"
    if score <= 14:
        return "moderate"
    return "severe"


class ScreeningService:
    """Voluntary explicit screening for PHQ-9 / GAD-7 (issue 05)."""

    def __init__(self, db: Session):
        self.db = db

    def get_scale_info(self, scale_type: str) -> dict:
        scale = get_scale(scale_type)
        return {
            "scaleType": scale.scale_type,
            "purpose": scale.purpose,
            "questions": scale.questions,
            "answerOptions": scale.answer_options,
            "scoringRules": scale.scoring_rules,
            "disclaimer": scale.disclaimer,
            "questionVersion": QUESTION_VERSION,
        }

    def submit_screening(
        self,
        user_id: int,
        scale_type: str,
        answers: list[int],
        trigger_source: str = "voluntary",
    ) -> ScreeningResult:
        scale = get_scale(scale_type)
        if len(answers) != len(scale.questions):
            raise ValueError(
                f"Expected {len(scale.questions)} answers for {scale_type}, got {len(answers)}"
            )
        if any(a < 0 or a > 3 for a in answers):
            raise ValueError("Each answer must be 0-3")

        total_score = sum(answers)
        severity = score_to_severity(scale_type, total_score)

        # Detect high-risk answers (PHQ-9 Q9 about self-harm)
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
        return (
            self.db.query(ScreeningResult)
            .filter(ScreeningResult.user_id == user_id)
            .order_by(ScreeningResult.created_at.desc())
            .all()
        )

    def get_result(self, user_id: int, result_id: int) -> ScreeningResult | None:
        result = self.db.get(ScreeningResult, result_id)
        if result is None or result.user_id != user_id:
            return None
        return result

    def to_response(self, result: ScreeningResult) -> dict:
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
            "createdAt": result.created_at.isoformat(),
        }
