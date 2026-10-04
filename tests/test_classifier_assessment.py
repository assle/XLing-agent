"""Tests for the fine-tuned classifier path in PsychologicalAssessmentService.

Ticket 02: classifier replaces the second risk-assessment layer. The classifier
outputs only a Chinese emotion label; the service maps it to EmotionLabel and
fills score/risk/confidence/summary via rules. Keyword short-circuit (layer 1)
and conservative fallback (layer 3) remain unchanged.

Run: python -m pytest tests/test_classifier_assessment.py
"""
from __future__ import annotations

import asyncio

from app.core.enums import EmotionLabel, RiskLevel
from app.services.assessment import (
    PsychologicalAssessmentService,
    assessment_from_label,
    label_to_emotion,
)


class FakeClassifierAiClient:
    """Test double recording classifier and full-generation calls."""

    def __init__(self, label: str = "", error: bool = False):
        """保存预设分类标签和是否模拟故障。

        分别记录分类调用与完整回复调用的次数。
        """
        self._label = label
        self._error = error
        self.classify_count = 0
        self.complete_count = 0

    def classify(self, text: str) -> str:
        """增加分类调用计数，按配置抛错或返回标签。

        不会读取输入文本做真实模型分类。
        """
        self.classify_count += 1
        if self._error:
            raise RuntimeError("simulated classifier outage")
        return self._label

    async def aclassify(self, text: str) -> str:
        """通过异步入口复用分类替身。

        与同步调用共享计数和错误设置。
        """
        return self.classify(text)

    def complete(self, messages):  # noqa: ANN001
        """记录完整生成调用，确保分类评估不会使用该入口。"""
        self.complete_count += 1
        return "unexpected full-generation call"

    async def acomplete(self, messages):  # noqa: ANN001
        """记录异步完整生成调用。"""
        return self.complete(messages)


def _svc(label: str = "", error: bool = False) -> PsychologicalAssessmentService:
    """为标签映射或故障测试创建评估服务。

    用可控分类器替换外部模型请求。
    """
    return PsychologicalAssessmentService(FakeClassifierAiClient(label, error))


# ---------------------------------------------------------------------------
# 中文标签与内部情绪类别的映射。
# ---------------------------------------------------------------------------

def test_label_to_emotion_maps_all_four():
    """逐个传入四个合法中文标签。

    检查它们一一对应内部情绪枚举。
    """
    assert label_to_emotion("正常") == EmotionLabel.NORMAL
    assert label_to_emotion("焦虑") == EmotionLabel.ANXIETY
    assert label_to_emotion("低落") == EmotionLabel.DEPRESSED
    assert label_to_emotion("高风险") == EmotionLabel.HIGH_RISK


def test_label_to_emotion_unknown_returns_none():
    """输入未知和空标签。

    检查映射返回 None，让调用方能够进入保守备用路径。
    """
    assert label_to_emotion("不知道") is None
    assert label_to_emotion("") is None


def test_assessment_from_label_anxiety():
    """由焦虑标签生成完整评估。

    核对标签、分数、风险及摘要中的标签来源。
    """
    a = assessment_from_label("焦虑")
    assert a.emotion == EmotionLabel.ANXIETY
    assert a.emotion_score == 2.0
    assert a.risk == RiskLevel.LOW
    assert a.confidence > 0.5
    assert "焦虑" in a.summary


def test_assessment_from_label_depressed():
    """由低落标签生成完整评估。

    检查固定三分对应中风险。
    """
    a = assessment_from_label("低落")
    assert a.emotion == EmotionLabel.DEPRESSED
    assert a.emotion_score == 3.0
    assert a.risk == RiskLevel.MEDIUM


def test_assessment_from_label_high_risk():
    """由高风险标签生成完整评估。

    检查四分和高风险保持一致。
    """
    a = assessment_from_label("高风险")
    assert a.emotion == EmotionLabel.HIGH_RISK
    assert a.emotion_score == 4.0
    assert a.risk == RiskLevel.HIGH


def test_assessment_from_label_normal():
    """由正常标签生成完整评估。

    检查分数为零且风险为低。
    """
    a = assessment_from_label("正常")
    assert a.emotion == EmotionLabel.NORMAL
    assert a.emotion_score == 0.0
    assert a.risk == RiskLevel.LOW


# ---------------------------------------------------------------------------
# 明确高风险关键词优先返回，跳过分类器。
# ---------------------------------------------------------------------------

def test_keyword_skips_classifier_suicide():
    """向预设正常标签的分类器输入明确风险表达。

    检查规则直接返回高风险，分类调用次数为零。
    """
    svc = _svc(label="正常")
    result = svc.assess("我不想活了，想自杀")
    assert result.risk == RiskLevel.HIGH
    assert result.emotion == EmotionLabel.HIGH_RISK
    assert svc.ai.classify_count == 0


def test_keyword_skips_classifier_selfharm():
    """输入明确自伤表达。

    检查高风险规则优先，分类器不被调用。
    """
    svc = _svc(label="正常")
    result = svc.assess("我想伤害自己")
    assert result.risk == RiskLevel.HIGH
    assert svc.ai.classify_count == 0


# ---------------------------------------------------------------------------
# 分类器标签通过固定规则转换成评估对象。
# ---------------------------------------------------------------------------

def test_classifier_anxiety_maps_low():
    """让分类器返回焦虑标签，运行当前主评估入口。

    检查它按固定规则映射为两分和低风险。
    """
    result = _svc(label="焦虑").assess("最近考试压力很大")
    assert result.emotion == EmotionLabel.ANXIETY
    assert result.risk == RiskLevel.LOW
    assert result.emotion_score == 2.0


def test_classifier_depressed_maps_medium():
    """让分类器返回低落标签。

    核对主入口得到三分和中风险。
    """
    result = _svc(label="低落").assess("最近一直很低落")
    assert result.emotion == EmotionLabel.DEPRESSED
    assert result.risk == RiskLevel.MEDIUM
    assert result.emotion_score == 3.0


def test_classifier_high_risk_maps_high():
    """在没有测试关键词的表达上让分类器给出高风险。

    检查主入口仍接受高风险标签并给出四分。
    """
    result = _svc(label="高风险").assess("感觉很危险")
    assert result.emotion == EmotionLabel.HIGH_RISK
    assert result.risk == RiskLevel.HIGH
    assert result.emotion_score == 4.0


def test_classifier_normal_maps_low():
    """让分类器返回正常标签。

    检查主入口得到零分和低风险。
    """
    result = _svc(label="正常").assess("今天天气不错")
    assert result.emotion == EmotionLabel.NORMAL
    assert result.risk == RiskLevel.LOW
    assert result.emotion_score == 0.0


def test_classifier_strips_whitespace_in_label():
    """返回带首尾空白和换行的焦虑标签。

    检查去空白后仍能正确映射。
    """
    result = _svc(label="  焦虑\n").assess("有点紧张")
    assert result.emotion == EmotionLabel.ANXIETY


# ---------------------------------------------------------------------------
# 分类失败或标签不合法时采用保守结果。
# ---------------------------------------------------------------------------

def test_classifier_outage_falls_back_to_medium():
    """模拟分类服务异常。

    检查结果为保守中风险且置信度较低。
    """
    svc = _svc(error=True)
    result = svc.assess("有点担心")
    assert result.risk == RiskLevel.MEDIUM
    assert result.confidence <= 0.5


def test_classifier_invalid_label_falls_back_to_medium():
    """让分类器输出不认识的标签。

    检查主入口不会猜测低风险，而是采用保守结果。
    """
    svc = _svc(label="不知道")
    result = svc.assess("有点担心")
    assert result.risk == RiskLevel.MEDIUM


def test_classifier_empty_label_falls_back_to_medium():
    """模拟分类器空回复。

    检查同样进入中风险备用处理。
    """
    svc = _svc(label="")
    result = svc.assess("有点担心")
    assert result.risk == RiskLevel.MEDIUM


# ---------------------------------------------------------------------------
# 异步入口的对应场景。
# ---------------------------------------------------------------------------

def test_async_classifier_anxiety():
    """对异步主入口提供焦虑标签。

    核对情绪和风险映射与同步入口一致。
    """
    result = asyncio.run(_svc(label="焦虑").aassess("最近考试压力很大"))
    assert result.emotion == EmotionLabel.ANXIETY
    assert result.risk == RiskLevel.LOW


def test_async_classifier_outage_fallback():
    """让异步分类入口模拟服务失败。

    检查异常转为保守中风险评估。
    """
    result = asyncio.run(_svc(error=True).aassess("有点担心"))
    assert result.risk == RiskLevel.MEDIUM


def test_async_keyword_skips_classifier():
    """向异步评估输入明确风险表达。

    检查直接高风险且无需调用分类器。
    """
    svc = _svc(label="正常")
    result = asyncio.run(svc.aassess("我想自杀"))
    assert result.risk == RiskLevel.HIGH
    assert svc.ai.classify_count == 0


def test_classifier_does_not_generate_full_assessment():
    svc = _svc(label="正常")
    assert svc.assess("今天天气不错").risk == RiskLevel.LOW
    assert asyncio.run(svc.aassess("今天天气不错")).risk == RiskLevel.LOW
    assert svc.ai.classify_count == 2
    assert svc.ai.complete_count == 0
