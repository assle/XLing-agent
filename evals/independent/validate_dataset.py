from __future__ import annotations

import json
import re
import unicodedata
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "evals/independent/data"
OUTPUT = ROOT / "evals/independent/data-validation.json"


def read_json(path: Path):
    if path.suffix == ".jsonl":
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return json.loads(path.read_text(encoding="utf-8"))


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).lower()
    return "".join(char for char in text if unicodedata.category(char)[0] in {"L", "N"})


def ngrams(text: str, size: int = 3) -> set[str]:
    if len(text) < size:
        return {text} if text else set()
    return {text[index:index + size] for index in range(len(text) - size + 1)}


def similarity(left: str, right: str) -> float:
    a, b = ngrams(left), ngrams(right)
    return len(a & b) / len(a | b) if a and b else 0.0


def texts_from_record(record: dict) -> list[str]:
    texts = []
    for key in ("input", "text", "question"):
        value = record.get(key)
        if isinstance(value, str):
            texts.append(value)
    messages = record.get("messages")
    if isinstance(messages, list):
        texts.extend(message for message in messages if isinstance(message, str))
    return texts


def historical_texts() -> list[dict]:
    roots = [
        ROOT / "finetune/data",
        ROOT / "evals/classifier",
        ROOT / "evals/risk",
        ROOT / "evals/rag",
        ROOT / "evals/quality",
        ROOT / "evals/e2e",
    ]
    items = []
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            if path.suffix not in {".json", ".jsonl"} or not path.is_file():
                continue
            try:
                records = read_json(path)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if not isinstance(records, list):
                continue
            for record in records:
                if isinstance(record, dict):
                    for text in texts_from_record(record):
                        items.append({"text": text, "normalized": normalize(text), "path": str(path.relative_to(ROOT)), "id": record.get("id", "")})
    return items


def main() -> None:
    datasets = {
        "classifier": read_json(DATA / "classifier.jsonl"),
        "risk": read_json(DATA / "risk.json"),
        "rag": read_json(DATA / "rag.jsonl"),
        "quality": read_json(DATA / "quality.json"),
        "e2e": read_json(DATA / "e2e.jsonl"),
    }
    errors = []
    counts = {name: len(records) for name, records in datasets.items()}
    expected_counts = {"classifier": 48, "risk": 36, "rag": 30, "quality": 20, "e2e": 24}
    if counts != expected_counts:
        errors.append({"type": "count", "expected": expected_counts, "actual": counts})

    all_ids = []
    new_texts = []
    for name, records in datasets.items():
        for record in records:
            all_ids.append(record["id"])
            for text in texts_from_record(record):
                new_texts.append({"dataset": name, "id": record["id"], "text": text, "normalized": normalize(text)})
    duplicates = [item for item, count in Counter(all_ids).items() if count > 1]
    if duplicates:
        errors.append({"type": "duplicate_ids", "ids": duplicates})
    new_duplicates = []
    seen = {}
    for item in new_texts:
        if item["normalized"] in seen:
            new_duplicates.append({"left": seen[item["normalized"]], "right": item["id"]})
        seen[item["normalized"]] = item["id"]
    if new_duplicates:
        errors.append({"type": "duplicate_new_texts", "items": new_duplicates})

    label_counts = {
        "classifier": dict(Counter(record["output"] for record in datasets["classifier"])),
        "risk": dict(Counter(record["expected_risk"] for record in datasets["risk"])),
        "quality": dict(Counter(record["category"] for record in datasets["quality"])),
        "e2e": dict(Counter(record["category"] for record in datasets["e2e"])),
    }
    if label_counts["classifier"] != {"正常": 12, "焦虑": 12, "低落": 12, "高风险": 12}:
        errors.append({"type": "classifier_balance", "actual": label_counts["classifier"]})
    if label_counts["risk"] != {"LOW": 12, "MEDIUM": 12, "HIGH": 12}:
        errors.append({"type": "risk_balance", "actual": label_counts["risk"]})

    source_errors = []
    for record in datasets["rag"]:
        for source in record["expectedSources"]:
            source_path = ROOT / "app/knowledge" / source
            if not source_path.is_file():
                source_errors.append({"id": record["id"], "source": source})
                continue
            content = source_path.read_text(encoding="utf-8").lower()
            missing_terms = [term for term in record["expectedTerms"] if term.lower() not in content]
            if missing_terms:
                source_errors.append({"id": record["id"], "source": source, "missingTerms": missing_terms})
        if not record["expectedTerms"]:
            source_errors.append({"id": record["id"], "error": "expectedTerms is empty"})
    if source_errors:
        errors.append({"type": "rag_sources", "items": source_errors})

    history = historical_texts()
    history_by_normalized = {}
    for item in history:
        history_by_normalized.setdefault(item["normalized"], item)
    exact_matches = []
    near_matches = []
    for new in new_texts:
        if new["normalized"] in history_by_normalized:
            old = history_by_normalized[new["normalized"]]
            exact_matches.append({"new": {key: new[key] for key in ("dataset", "id", "text")}, "old": {key: old[key] for key in ("path", "id", "text")}})
        scored = [(similarity(new["normalized"], old["normalized"]), old) for old in history]
        score, old = max(scored, key=lambda pair: pair[0])
        near_matches.append({"score": round(score, 4), "new": {key: new[key] for key in ("dataset", "id", "text")}, "old": {key: old[key] for key in ("path", "id", "text")}})
    near_matches.sort(key=lambda item: item["score"], reverse=True)
    if exact_matches:
        errors.append({"type": "exact_leakage", "count": len(exact_matches)})

    report = {
        "status": "failed" if errors else "passed",
        "counts": counts,
        "totalCases": sum(counts.values()),
        "labelCounts": label_counts,
        "historicalTextCount": len(history),
        "newTextCount": len(new_texts),
        "exactMatches": exact_matches,
        "topNearMatches": near_matches[:30],
        "nearMatchThresholdForReview": 0.55,
        "pairsAtOrAboveThreshold": [item for item in near_matches if item["score"] >= 0.55],
        "errors": errors,
        "notes": [
            "Exact and near-duplicate checks compare normalized user-facing text only.",
            "Near similarity is character 3-gram Jaccard; it is a screening aid, not a semantic label audit.",
            "All labels are agent-assigned engineering targets, pending human review; they are not medical gold labels.",
        ],
    }
    OUTPUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": report["status"], "counts": counts, "historicalTextCount": len(history), "exactMatches": len(exact_matches), "topNearMatches": near_matches[:10], "errors": errors}, ensure_ascii=False, indent=2))
    if errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
