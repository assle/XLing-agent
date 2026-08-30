from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from app.core.versioning import ArtifactVersionResolver
from app.services.ai import AiClient
from app.services.assessment import PsychologicalAssessmentService
from evals.config import EvalSettings, get_eval_settings

logger = logging.getLogger(__name__)

RISK_CLASSES = ["LOW", "MEDIUM", "HIGH"]


def compute_metrics(results: list[dict]) -> dict:
    """Compute per-class precision/recall/F1, macro-F1, accuracy, confusion matrix.

    Each result dict must have ``expected_risk`` and ``predicted_risk`` keys
    with values in RISK_CLASSES.
    """
    classes = RISK_CLASSES
    total = max(1, len(results))

    confusion = {exp: {pred: 0 for pred in classes} for exp in classes}
    for r in results:
        exp = r["expected_risk"]
        pred = r["predicted_risk"]
        if exp in confusion and pred in confusion[exp]:
            confusion[exp][pred] += 1

    per_class: dict[str, dict[str, float]] = {}
    for cls in classes:
        tp = confusion[cls][cls]
        fp = sum(confusion[other][cls] for other in classes if other != cls)
        fn = sum(confusion[cls][other] for other in classes if other != cls)
        precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
        recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
        f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0
        per_class[cls] = {"precision": precision, "recall": recall, "f1": f1}

    macro_f1 = sum(per_class[cls]["f1"] for cls in classes) / len(classes)
    correct = sum(1 for r in results if r["expected_risk"] == r["predicted_risk"])
    accuracy = correct / total

    return {
        "totalCases": len(results),
        "accuracy": accuracy,
        "macroF1": macro_f1,
        "perClass": per_class,
        "confusionMatrix": confusion,
    }


async def _run_cases(
    cases: list[dict], service: PsychologicalAssessmentService
) -> list[dict]:
    results: list[dict] = []
    for case in cases:
        assessment = await service.aassess(case["text"])
        predicted = assessment.risk.value
        expected = case["expected_risk"]
        results.append({
            "id": case["id"],
            "text": case["text"],
            "expectedRisk": expected,
            "predictedRisk": predicted,
            "category": case.get("category", ""),
            "notes": case.get("notes", ""),
            "hit": expected == predicted,
            "emotion": assessment.emotion.value,
            "emotionScore": assessment.emotion_score,
            "confidence": assessment.confidence,
            "summary": assessment.summary,
        })
    return results


def evaluate(
    settings: EvalSettings | None = None, provider: str | None = None
) -> dict:
    """Run risk evaluation against the labeled dataset.

    Reads cases from ``risk_eval_dataset``, calls
    ``PsychologicalAssessmentService.aassess()`` for each, computes metrics,
    and writes full report + compact summary to disk.
    """
    settings = settings or get_eval_settings()
    provider = provider or settings.risk_eval_ai_provider

    dataset_path = Path(settings.risk_eval_dataset)
    cases = json.loads(dataset_path.read_text(encoding="utf-8"))

    ai_settings = settings.model_copy(update={
        "ai_provider": provider,
        "openai_api_key": settings.risk_eval_api_key or settings.openai_api_key,
        "openai_base_url": settings.risk_eval_base_url or settings.openai_base_url,
        "openai_model": settings.risk_eval_model or settings.openai_model,
        "ai_max_tokens": 4096,
    })
    ai = AiClient(ai_settings)
    service = PsychologicalAssessmentService(ai)

    results = asyncio.run(_run_cases(cases, service))
    metrics = compute_metrics(
        [{"expected_risk": r["expectedRisk"], "predicted_risk": r["predictedRisk"]} for r in results]
    )

    report = {
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "provider": provider,
        "dataset": str(dataset_path),
        "artifactVersion": ArtifactVersionResolver(ai_settings).current(dataset_path).to_dict(),
        **metrics,
        "results": results,
    }

    report_path = Path(settings.risk_eval_output)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    summary = {k: v for k, v in report.items() if k != "results"}
    summary_path = Path(settings.risk_eval_summary_output)
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    return report


def build_summary(report: dict) -> dict:
    """Compact summary without per-case detail, for cross-run comparison."""
    return {k: v for k, v in report.items() if k != "results"}


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
    print(f"Risk evaluation completed (provider={provider or 'default'}).")
    print(f"totalCases={report['totalCases']}")
    print(f"accuracy={report['accuracy']:.4f}")
    print(f"macroF1={report['macroF1']:.4f}")
    for cls in RISK_CLASSES:
        m = report["perClass"][cls]
        print(f"  {cls}: precision={m['precision']:.4f} recall={m['recall']:.4f} f1={m['f1']:.4f}")
    print(f"summary={get_eval_settings().risk_eval_summary_output}")
