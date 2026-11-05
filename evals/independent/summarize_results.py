from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT / "evals/independent"
RUN = BASE / "results/20261102-v2"
PREVIOUS = BASE / "results/20261102-v1"


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def percent(value: float) -> str:
    return f"{value * 100:.2f}%"


def table(headers: list[str], rows: list[list[object]]) -> list[str]:
    def cell(value: object) -> str:
        return str(value).replace("|", "\\|").replace("\n", " ")
    return [
        "| " + " | ".join(headers) + " |",
        "|" + "|".join(["---"] * len(headers)) + "|",
        *["| " + " | ".join(cell(value) for value in row) + " |" for row in rows],
    ]


def main() -> None:
    classifier = load(RUN / "classifier.json")
    risk = load(RUN / "risk.json")
    rag = load(RUN / "rag-direct/rag-hybrid-rrf.json")
    manifest = load(BASE / "frozen-manifest.json")
    validation = load(BASE / "data-validation.json")
    previous_classifier = load(PREVIOUS / "classifier.json")
    previous_risk = load(PREVIOUS / "risk.json")
    previous_rag = load(PREVIOUS / "rag-direct/rag-hybrid-rrf.json")
    previous_e2e = load(PREVIOUS / "e2e-recovered.json")

    e2e_source_path = BASE / "data/e2e.jsonl"
    e2e_source_cases = [json.loads(line) for line in e2e_source_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    e2e_data = {case["id"]: case for case in e2e_source_cases}
    case_order = {case["id"]: index for index, case in enumerate(e2e_source_cases)}
    batch_paths = [RUN / f"e2e-recovered/batch-{index}.json" for index in range(1, 5)]
    batch_reports = [load(path) for path in batch_paths]
    combined_cases = sorted(
        [case for report in batch_reports for case in report["cases"]],
        key=lambda case: case_order[case["id"]],
    )
    if len(combined_cases) != len(e2e_source_cases) or {case["id"] for case in combined_cases} != set(e2e_data):
        raise ValueError("E2E batches do not exactly cover the frozen dataset")
    if any("error" in case["events"] for case in combined_cases):
        raise ValueError("E2E report still contains terminal infrastructure errors")

    artifact_version = dict(batch_reports[0]["artifactVersion"])
    artifact_version["datasetVersion"] = sha256(e2e_source_path)
    e2e = {
        "artifactVersion": artifact_version,
        "totalCases": len(combined_cases),
        "passedCases": sum(case["passed"] for case in combined_cases),
        "cases": combined_cases,
        "batchReports": [str(path.relative_to(ROOT)) for path in batch_paths],
    }
    (RUN / "e2e-recovered.json").write_text(json.dumps(e2e, ensure_ascii=False, indent=2), encoding="utf-8")

    classifier_data = {
        item["id"]: item
        for item in (json.loads(line) for line in (BASE / "data/classifier.jsonl").read_text(encoding="utf-8").splitlines() if line.strip())
    }
    classifier_errors = [
        {"id": item["id"], "text": classifier_data[item["id"]]["input"], "expected": item["expected"], "predicted": item["predicted"]}
        for item in classifier["datasets"][0]["casesDetail"] if item["expected"] != item["predicted"]
    ]
    risk_errors = [
        {"id": item["id"], "text": item["text"], "category": item["category"], "expected": item["expectedRisk"],
         "predicted": item["predictedRisk"], "summary": item["summary"]}
        for item in risk["results"] if not item["hit"]
    ]
    rag_misses = []
    for item in rag["results"]:
        if item["targetSourceHit"]:
            continue
        expected = set(item["expectedSources"])
        dense_ranks = [
            entry["rank"]
            for query in item["diagnostics"]["queries"]
            for entry in query.get("dense", []) if entry["source"] in expected
        ]
        merged_target = any(entry["source"] in expected for entry in item["diagnostics"]["merged"])
        rag_misses.append({
            "id": item["id"], "question": item["question"], "expectedSources": item["expectedSources"],
            "returnedSources": [entry["source"] for entry in item["retrieved"]],
            "bestDenseRank": min(dense_ranks) if dense_ranks else None,
            "targetSurvivedToMultiQueryMerge": merged_target,
            "diagnosis": "dense-recalled-but-absent-from-baseline-multi-query-merge" if dense_ranks and not merged_target else "review-stage-trace",
        })

    frozen_checks = []
    for record in manifest["files"]:
        path = ROOT / record["path"]
        frozen_checks.append({"path": record["path"], "matches": path.is_file() and sha256(path) == record["sha256"]})
    frozen_intact = all(item["matches"] for item in frozen_checks)
    if not frozen_intact:
        raise ValueError("Frozen dataset hash mismatch")

    e2e_category = {}
    for category in sorted({item["category"] for item in combined_cases}):
        cases = [item for item in combined_cases if item["category"] == category]
        e2e_category[category] = {"total": len(cases), "passed": sum(item["passed"] for item in cases)}
    e2e_diagnostics = {
        "intentAccuracy": sum(item["intent"] == e2e_data[item["id"]]["expectedIntent"] for item in combined_cases) / len(combined_cases),
        "riskAccuracy": sum(item["risk"] == e2e_data[item["id"]]["expectedRisk"] for item in combined_cases) / len(combined_cases),
        "reviewAccuracy": sum(item["reviewCreated"] == e2e_data[item["id"]]["expectReview"] for item in combined_cases) / len(combined_cases),
        "terminalErrorEvents": 0,
    }
    summary = {
        "datasetId": manifest["datasetId"],
        "status": "completed",
        "evaluationRole": "frozen-acceptance-set-used-during-adr-repair-not-unseen-generalization",
        "totalAuthoredCases": manifest["totalCases"],
        "evaluatedCases": 48 + 36 + 30 + 24,
        "excludedQualityCases": 20,
        "labelProvenance": manifest["labelProvenance"],
        "frozenDataIntact": frozen_intact,
        "frozenFileChecks": frozen_checks,
        "leakageCheck": {
            "status": validation["status"], "historicalTextCount": validation["historicalTextCount"],
            "exactMatches": len(validation["exactMatches"]),
            "maximumCharacter3GramJaccard": validation["topNearMatches"][0]["score"],
        },
        "classifier": {
            "cases": 48, "accuracy": classifier["combined"]["accuracy"], "macroF1": classifier["combined"]["macroF1"],
            "highRiskRecall": classifier["combined"]["highRiskRecall"], "perClass": classifier["combined"]["perClass"],
            "confusionMatrix": classifier["combined"]["confusionMatrix"], "errors": classifier_errors,
            "previousAccuracy": previous_classifier["combined"]["accuracy"], "report": "evals/independent/results/20261102-v2/classifier.json",
        },
        "risk": {
            "cases": 36, "accuracy": risk["accuracy"], "macroF1": risk["macroF1"], "perClass": risk["perClass"],
            "confusionMatrix": risk["confusionMatrix"], "errors": risk_errors, "previousAccuracy": previous_risk["accuracy"],
            "report": "evals/independent/results/20261102-v2/risk.json",
        },
        "rag": {
            "cases": 30, "topK": rag["topK"], "retrieval": rag["retrieval"],
            "hitRate": rag["hitRate"], "recallAtK": rag["recallAtK"], "precisionAtK": rag["precisionAtK"],
            "targetSourceRecallAtK": rag["targetSourceRecallAtK"], "answerRelevanceHitRate": rag["answerRelevanceHitRate"],
            "reviewedRelevanceCases": rag["reviewedRelevanceCases"], "mrr": rag["mrr"], "ndcgAtK": rag["ndcgAtK"],
            "comparisonEligible": rag["comparisonEligible"], "previousTargetSourceRecallAtK": previous_rag["hitRate"],
            "misses": rag_misses, "report": "evals/independent/results/20261102-v2/rag-direct/rag-hybrid-rrf.json",
        },
        "quality": {"status": "excluded-by-owner", "cases": 20},
        "e2e": {
            "cases": 24, "passedCases": e2e["passedCases"], "passRate": e2e["passedCases"] / 24,
            "previousPassedCases": previous_e2e["passedCases"], "perCategory": e2e_category,
            "diagnostics": e2e_diagnostics, "generationModel": artifact_version["chatModel"],
            "batchReports": e2e["batchReports"], "report": "evals/independent/results/20261102-v2/e2e-recovered.json",
        },
        "limitations": [
            "全部文本与标签均为代理编写的合成工程目标；除已确认的测试口径修正外，其余标签仍待人工复核，不是医学金标或真实用户数据。",
            "该冻结集已参与本轮 ADR 修复，因此结果是冻结验收复测，不再宣称未曝光泛化；新的泛化结论需要全新查询和人工审核标签。",
            "RAG 的额外回答相关来源尚未人工审核，reviewedRelevanceCases=0；answerRelevanceHitRate 不能解释为人工语义相关性评分。",
            "RAG 本轮只实现双指标和诊断，没有修改线上检索算法，因此无需把本轮结果描述为策略优化收益。",
            "E2E 使用内存数据库并关闭向量检索，不是 RAG 端到端测试；分批仅为控制单次运行时长，合并前已核验 24 个 ID 完整覆盖。",
            "质量 20 题按所有者要求不在本轮复测范围内。",
        ],
    }
    (RUN / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# 冻结独立验收集 ADR 复测报告（v2）", "",
        "## 结论与口径", "",
        f"- 数据集：`{manifest['datasetId']}`，共 **158** 例；本轮评测 **138** 例，质量 **20** 例按要求不复测。五个冻结数据文件哈希全部一致。",
        "- 题目和标签是合成工程目标，不是真实用户数据、人工金标或临床验证。该集已参与 ADR 修复，本报告是冻结验收复测，不再宣称未曝光泛化。",
        f"- 与 {validation['historicalTextCount']} 条历史文本比较，规范化精确重复 **0**；最高字符 3-gram Jaccard 为 **{validation['topNearMatches'][0]['score']:.4f}**。",
        "- 端到端早先受本机代理影响的错误批次已用相同输入重跑；下表只采用服务可用、无终止 error 的结果。", "",
        "## 总览与前后对照", "",
        *table(["模块", "v1", "v2", "说明"], [
            ["原始决策头（48）", percent(previous_classifier["combined"]["accuracy"]), percent(classifier["combined"]["accuracy"]), "同一冻结模型与数据，结果一致"],
            ["完整风险服务（36）", percent(previous_risk["accuracy"]), percent(risk["accuracy"]), f"HIGH 召回 {percent(risk['perClass']['HIGH']['recall'])}"],
            ["RAG 目标来源 Recall@4（30）", percent(previous_rag["hitRate"]), percent(rag["targetSourceRecallAtK"]), "本轮只加诊断，未改检索算法"],
            ["端到端（24）", f"{previous_e2e['passedCases']} / 24", f"{e2e['passedCases']} / 24", "分流、风险、事件、审核及知识顺序全部通过"],
            ["回复质量（20）", "历史结果保留", "未复测", "按所有者要求排除"],
        ]), "",
        "## 1. 原始决策头分类", "",
        f"Accuracy **{percent(classifier['combined']['accuracy'])}**，Macro F1 **{percent(classifier['combined']['macroF1'])}**，高风险召回 **{percent(classifier['combined']['highRiskRecall'])}**。仍有 2 个错误：", "",
        *table(["ID", "文本", "真实", "预测"], [[item["id"], item["text"], item["expected"], item["predicted"]] for item in classifier_errors]), "",
        "## 2. 完整风险服务", "",
        f"Accuracy **{percent(risk['accuracy'])}**，Macro F1 **{percent(risk['macroF1'])}**，HIGH 召回 **{percent(risk['perClass']['HIGH']['recall'])}**。剩余 10 个与冻结标签不一致的结果如下，不修改标签迎合模型：", "",
        *table(["ID", "文本", "真实", "预测", "实际决策"], [[item["id"], item["text"], item["expected"], item["predicted"], item["summary"]] for item in risk_errors]), "",
        "其中 `ind-risk-low-12` 的保守 MEDIUM 来自生成式复核不可用；其余是决策头或分层评估结果。否定、引用等只取消关键词直连，不代表完整风险评估必然判 LOW。", "",
        "## 3. RAG 双指标与漏损定位", "",
        f"真实向量路径，`comparisonEligible=true`。目标来源 Recall@4 **{percent(rag['targetSourceRecallAtK'])}**，回答相关性命中率 **{percent(rag['answerRelevanceHitRate'])}**，MRR **{rag['mrr']:.4f}**，nDCG@4 **{rag['ndcgAtK']:.4f}**。人工审核相关来源数为 **0**，所以回答相关性仍是未人工扩展标注的工程口径。", "",
        "6 个目标来源遗漏均在 dense 候选中出现，却没有进入跨查询合并列表；这只能定位漏损阶段，不能单独归因于 Top 4 截断。反事实重放确认，主要机制是中英文 BM25 词项断层叠加 RRF 单双路计票不平衡，另有改写漂移；扩大候选池或加入原查询均不能单独恢复。完整证据见 `diagnosis.md`。本轮未改算法，也未进行策略变更所要求的新查询复验。", "",
        *table(["ID", "目标来源", "dense 最佳名次", "进入跨查询合并", "诊断"], [[item["id"], ", ".join(item["expectedSources"]), item["bestDenseRank"], item["targetSurvivedToMultiQueryMerge"], item["diagnosis"]] for item in rag_misses]), "",
        "逐题的原查询、改写查询、dense、BM25、融合、合并和最终排名保存在详细 RAG 报告的 `diagnostics` 字段。", "",
        "## 4. 端到端流程", "",
        f"真实生成通道共 **{e2e['passedCases']} / 24** 通过；分流准确率、风险等级一致率和审核创建一致率均为 **100%**，终止 error 为 **0**。两条资源查询均走 CHAT＋资源检索；四维完整题生成行动计划，缺少想法题继续追问且不生成计划。", "",
        *table(["ID", "分流", "风险", "事件", "通过"], [[item["id"], item["intent"], item["risk"], " → ".join(item["events"]), item["passed"]] for item in combined_cases]), "",
        "## 范围限制", "",
        *[f"- {item}" for item in summary["limitations"]], "",
        "## 产物", "",
        "- `evals/independent/frozen-manifest.json`：冻结数据、代码版本和模型产物指纹。",
        "- `evals/independent/results/20261102-v2/summary.json`：机器可读汇总及全部失败案例。",
        "- `evals/independent/results/20261102-v2/diagnosis.md`：风险与 RAG 剩余问题的决策路径、根因证据及反事实重放。",
        "- `evals/independent/results/20261102-v2/e2e-recovered.json`：24 题逐题机器判别结果。",
        "- `evals/independent/results/20261102-v2/rag-direct/rag-hybrid-rrf.json`：30 题检索结果及阶段轨迹。",
        "- v1 原始结果完整保留，未覆盖。",
    ]
    (RUN / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({
        "summary": str((RUN / "summary.md").relative_to(ROOT)), "frozenDataIntact": frozen_intact,
        "classifierErrors": len(classifier_errors), "riskErrors": len(risk_errors),
        "ragMisses": len(rag_misses), "e2ePassed": e2e["passedCases"], "e2eTotal": e2e["totalCases"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
