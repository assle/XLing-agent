from __future__ import annotations

import argparse
import json
from pathlib import Path

from evals.config import get_eval_settings
from evals.e2e.runner import evaluate

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "evals/independent/data/e2e.jsonl"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("batch", type=int, choices=range(1, 5))
    parser.add_argument("--run", choices=("20261102-v1", "20261102-v2"), default="20261102-v2")
    args = parser.parse_args()
    output_dir = ROOT / f"evals/independent/results/{args.run}/e2e-recovered"
    cases = [json.loads(line) for line in SOURCE.read_text(encoding="utf-8").splitlines() if line.strip()]
    batch_cases = [case for index, case in enumerate(cases) if index % 4 + 1 == args.batch]
    output_dir.mkdir(parents=True, exist_ok=True)
    dataset_path = output_dir / f"batch-{args.batch}.jsonl"
    dataset_path.write_text("".join(json.dumps(case, ensure_ascii=False) + "\n" for case in batch_cases), encoding="utf-8")
    base = get_eval_settings()
    settings = base.model_copy(update={
        "e2e_eval_dataset": str(dataset_path.relative_to(ROOT)),
        "e2e_eval_output": f"evals/independent/results/{args.run}/e2e-recovered/batch-{args.batch}.json",
        "e2e_eval_ai_provider": "openai",
    })
    report = evaluate(settings)
    print(json.dumps({"batch": args.batch, "totalCases": report["totalCases"], "passedCases": report["passedCases"], "ids": [case["id"] for case in batch_cases]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
