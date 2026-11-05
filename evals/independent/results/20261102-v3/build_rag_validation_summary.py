import json
from pathlib import Path

from evals.rag.runner import build_eval_summary, compute_report

ROOT = Path("evals/independent/results/20261102-v3")
REGRESSION = ROOT / "rag-regression-dense"
CURRENT_SOURCES = {path.name for path in Path("app/knowledge").glob("*.md")}


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


reports = [
    read_json(REGRESSION / f"batch-{index}-dense-primary.json")
    for index in range(1, 5)
]
results = []
for report in reports:
    results.extend(report["results"])

for result in results:
    expected = set(result.get("expectedSources", []))
    result["sourceEvaluationStatus"] = (
        "current-source" if expected & CURRENT_SOURCES else "stale-source"
    )

label = "multi-query + Chroma dense primary (local hybrid fallback)"
full = compute_report(results, "evals/rag/xling-rag-eval.jsonl", 4, label)
full["comparisonEligible"] = all(
    read_json(REGRESSION / f"batch-{index}-dense-primary-summary.json")["comparisonEligible"]
    for index in range(1, 5)
)
full["staleSourceIds"] = [
    result["id"] for result in results if result["sourceEvaluationStatus"] == "stale-source"
]
full["currentSourceMissIds"] = [
    result["id"] for result in results
    if result["sourceEvaluationStatus"] == "current-source" and not result["targetSourceHit"]
]
write_json(REGRESSION / "combined.json", full)
write_json(REGRESSION / "combined-summary.json", build_eval_summary(full))

comparable_results = [
    result for result in results if result["sourceEvaluationStatus"] == "current-source"
]
comparable = compute_report(
    comparable_results,
    "evals/rag/xling-rag-eval.jsonl#current-source-subset",
    4,
    label,
)
comparable["comparisonEligible"] = full["comparisonEligible"]
comparable["excludedStaleSourceIds"] = full["staleSourceIds"]
comparable["missIds"] = [
    result["id"] for result in comparable_results if not result["targetSourceHit"]
]
comparable["safetyCriticalMissIds"] = [
    result["id"] for result in comparable_results
    if result["safetyCritical"] and not result["targetSourceHit"]
]
write_json(REGRESSION / "comparable.json", comparable)
write_json(REGRESSION / "comparable-summary.json", build_eval_summary(comparable))

validation = {
    "strategy": label,
    "topK": 4,
    "frozen": build_eval_summary(read_json(ROOT / "rag-dense/rag-dense-primary.json")),
    "regressionAll": build_eval_summary(full),
    "regressionCurrentSourceSubset": build_eval_summary(comparable),
    "regressionStaleSourceIds": full["staleSourceIds"],
    "regressionCurrentSourceMissIds": full["currentSourceMissIds"],
    "regressionCurrentSourceSafetyMissIds": comparable["safetyCriticalMissIds"],
    "holdout": build_eval_summary(read_json(ROOT / "rag-holdout/rag-dense-primary.json")),
    "holdoutMissIds": [
        result["id"]
        for result in read_json(ROOT / "rag-holdout/rag-dense-primary.json")["results"]
        if not result["targetSourceHit"]
    ],
}
write_json(ROOT / "rag-validation.json", validation)
print(json.dumps({
    "all": {"n": full["totalCases"], "recall": full["targetSourceRecallAtK"]},
    "comparable": {
        "n": comparable["totalCases"],
        "recall": comparable["targetSourceRecallAtK"],
        "safetyMissRate": comparable["safetyCriticalMissRate"],
    },
    "stale": full["staleSourceIds"],
    "misses": full["currentSourceMissIds"],
    "holdoutMisses": validation["holdoutMissIds"],
}, ensure_ascii=False, indent=2))
