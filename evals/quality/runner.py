from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, stdev

from app.core.enums import IntentType, RiskLevel
from app.core.versioning import ArtifactVersionResolver
from app.schemas.dtos import AiMessage
from app.services.ai import AiClient, PromptTemplates
from evals.config import EvalSettings, get_eval_settings
from evals.quality.judge_prompt import build_judge_messages, parse_judge_response

logger = logging.getLogger(__name__)

QUALITY_DIMENSIONS = ["empathy", "safety", "actionability", "boundary"]


async def _generate_reply(ai_client: AiClient, text: str) -> str:
    """为质量评估样本构造统一的低风险心理支持请求并生成回复。

    不执行完整消息分流或知识检索，因此评估的是此固定条件下的回复。
    """
    system_prompt = PromptTemplates.answer_system_prompt(
        IntentType.CONSULT, RiskLevel.LOW, "", "同学"
    )
    messages = [
        system_prompt,
        AiMessage(
            role="system",
            content=(
                "当前由 CounselorAgent 负责回复。\n记忆摘要：无\n"
                "KnowledgeAgent 检索 query：无\n"
                "回复策略：先共情，再给出具体支持步骤"
            ),
        ),
        AiMessage(role="user", content=text),
    ]
    return await ai_client.acomplete(messages)


async def _judge_reply(
    judge_client: AiClient, user_text: str, reply: str
) -> dict:
    """请评审模型对一条生成回复评分，并解析其输出。

    user_text 和 reply 分别为原问题和回答；调用失败向上传递，格式问题由解析函数给出占位结果。
    """
    messages = build_judge_messages(user_text, reply)
    raw = await judge_client.acomplete(messages)
    return parse_judge_response(raw)


async def _evaluate_async(settings: EvalSettings, provider: str) -> list[dict]:
    """逐个生成样本回复，并对每个回复重复评分后汇总各维度均值和波动。

    生成与评审使用分别构造的配置；模拟生成时也强制模拟评审，避免误调用真实评审服务。
    样本和重复评分均顺序执行，重复次数需由配置保证为正。
    """
    dataset_path = Path(settings.quality_eval_dataset)
    cases = json.loads(dataset_path.read_text(encoding="utf-8"))

    gen_settings = settings.model_copy(update={
        "ai_provider": provider,
        "openai_api_key": settings.quality_eval_gen_api_key or settings.openai_api_key,
        "openai_base_url": settings.quality_eval_gen_base_url or settings.openai_base_url,
        "openai_model": settings.quality_eval_gen_model or settings.openai_model,
    })
    gen_client = AiClient(gen_settings)

    if provider == "mock":
        judge_provider = "mock"
    else:
        judge_provider = settings.quality_eval_judge_provider
    judge_settings = settings.model_copy(update={
        "ai_provider": judge_provider,
        "openai_base_url": settings.quality_eval_judge_base_url,
        "openai_api_key": settings.quality_eval_judge_api_key or settings.openai_api_key,
        "openai_model": settings.quality_eval_judge_model,
        "ai_temperature": settings.quality_eval_judge_temperature,
        "ai_max_tokens": 4096,
    })
    judge_client = AiClient(judge_settings)

    results: list[dict] = []
    for case in cases:
        reply = await _generate_reply(gen_client, case["text"])

        judge_runs: list[dict] = []
        # 同一条生成回复重复评分，不在每次评审前重新生成回答。
        for _ in range(settings.quality_eval_judge_runs):
            scores = await _judge_reply(judge_client, case["text"], reply)
            judge_runs.append(scores)

        dim_stats: dict[str, dict[str, float]] = {}
        for dim in QUALITY_DIMENSIONS:
            # 将同一维度的多次评分汇集后计算均值和波动，观察评审稳定性。
            vals = [run.get(dim, 0) for run in judge_runs]
            dim_stats[dim] = {
                "mean": round(mean(vals), 4),
                "std": round(stdev(vals), 4) if len(vals) > 1 else 0.0,
            }

        results.append({
            "id": case["id"],
            "text": case["text"],
            "category": case.get("category", ""),
            "notes": case.get("notes", ""),
            "reply": reply,
            "judgeRuns": judge_runs,
            "dimensionStats": dim_stats,
        })

    return results


def compute_quality_summary(results: list[dict]) -> dict:
    """对各案例的四维平均分及评分波动进行汇总。

    std 字段是每例标准差的平均值，不是全体评分的总体标准差；平均波动超过 0.5 标记不稳定。
    """
    per_dimension: dict[str, dict] = {}
    for dim in QUALITY_DIMENSIONS:
        all_means = [r["dimensionStats"][dim]["mean"] for r in results]
        all_stds = [r["dimensionStats"][dim]["std"] for r in results]
        avg_std = mean(all_stds) if all_stds else 0.0
        per_dimension[dim] = {
            "mean": round(mean(all_means), 4) if all_means else 0.0,
            "std": round(avg_std, 4),
            "unstable": avg_std > 0.5,
        }
    return {
        "perDimension": per_dimension,
        "totalCases": len(results),
    }


def build_summary(report: dict) -> dict:
    """从完整质量报告中移除逐例 results，生成简要摘要。

    其余元数据和总体统计原样保留，不重新计算指标。
    """
    return {k: v for k, v in report.items() if k != "results"}


def evaluate(
    settings: EvalSettings | None = None, provider: str | None = None
) -> dict:
    """运行回复生成与重复评分，并保存完整报告和摘要。

    创建输出目录，记录模型与数据版本；它只产出离线评估文件，不改变应用的线上配置。
    """
    settings = settings or get_eval_settings()
    provider = provider or settings.quality_eval_gen_provider

    results = asyncio.run(_evaluate_async(settings, provider))
    summary = compute_quality_summary(results)

    report = {
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "genProvider": provider,
        "judgeProvider": "mock" if provider == "mock" else settings.quality_eval_judge_provider,
        "judgeModel": settings.quality_eval_judge_model,
        "judgeRuns": settings.quality_eval_judge_runs,
        "artifactVersion": ArtifactVersionResolver(
            settings.model_copy(update={"ai_provider": provider})
        ).current(settings.quality_eval_dataset).to_dict(),
        **summary,
        "results": results,
    }

    report_path = Path(settings.quality_eval_output)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    summary_data = build_summary(report)
    summary_path = Path(settings.quality_eval_summary_output)
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(
        json.dumps(summary_data, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    return report


if __name__ == "__main__":
    import sys

    provider = None
    if len(sys.argv) > 1:
        arg = sys.argv[1]
        if arg == "--provider" and len(sys.argv) > 2:
            provider = sys.argv[2]
        else:
            provider = arg

    report = evaluate(provider=provider)
    print(
        f"Quality evaluation completed "
        f"(genProvider={report['genProvider']}, "
        f"judgeProvider={report['judgeProvider']})."
    )
    print(f"totalCases={report['totalCases']}")
    for dim in QUALITY_DIMENSIONS:
        d = report["perDimension"][dim]
        unstable = " [UNSTABLE]" if d["unstable"] else ""
        print(f"  {dim}: mean={d['mean']:.4f} std={d['std']:.4f}{unstable}")
    print(f"summary={get_eval_settings().quality_eval_summary_output}")
