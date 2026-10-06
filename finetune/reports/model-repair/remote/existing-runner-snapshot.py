"""Evaluate fixed datasets through the application classifier without retries."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.core.classifier_contract import CLASSIFIER_QUOTED_PREFIX  # noqa: E402
from app.core.config import Settings  # noqa: E402
from app.schemas.dtos import AiMessage  # noqa: E402
from app.services.ai import AiClient  # noqa: E402
from evals.classifier.runner import CLASSES, compute_metrics, load_dataset  # noqa: E402

LABEL_MAP = {"NORMAL": "正常", "ANX": "焦虑", "LOW": "低落", "HIGH": "高风险"}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_report(path: Path, report: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def percentile(values: list[float], fraction: float) -> float:
    return sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)] if values else 0.0


def runtime_metrics(cases: list[dict]) -> dict:
    return {
        **compute_metrics(cases),
        "falseHighCount": sum(case["expected"] != "高风险" and case["predicted"] == "高风险" for case in cases),
        "outputValidity": sum(case["strictValid"] for case in cases) / max(1, len(cases)),
        "invalidReasons": dict(Counter(case["invalidReason"] for case in cases if not case["strictValid"])),
        "latencyP50Ms": percentile([case["latencyMs"] for case in cases], .5),
        "latencyP95Ms": percentile([case["latencyMs"] for case in cases], .95),
        "generatedTokens": sum(case.get("generatedTokens", 0) or 0 for case in cases),
        "promptTokens": sum(case.get("promptTokens", 0) or 0 for case in cases),
    }


class ObservedAiClient(AiClient):
    """Retain only safe response metadata while using the unchanged app entry point."""

    metadata: dict

    def _classifier_response(self, response: httpx.Response) -> str:
        body = response.json()
        self.metadata = {
            "generatedTokens": body.get("eval_count"),
            "promptTokens": body.get("prompt_eval_count"),
            "finishReason": body.get("done_reason"),
            "providerModel": body.get("model"),
        }
        return super()._classifier_response(response)


def parse_json_label(raw: str, finish_reason: str | None) -> str:
    """Keep the fixed one-key JSON contract, including duplicate-key rejection."""
    if finish_reason != "stop":
        return "__INVALID__"

    def unique_object(pairs):
        if len({key for key, _ in pairs}) != len(pairs):
            raise ValueError("Duplicate JSON key")
        return dict(pairs)

    try:
        value = json.loads(raw, object_pairs_hook=unique_object)
    except (ValueError, TypeError):
        return "__INVALID__"
    if not isinstance(value, dict) or set(value) != {"label"}:
        return "__INVALID__"
    label = value["label"]
    return label if isinstance(label, str) and label in CLASSES else "__INVALID__"


class ObservedDeepSeekClient(AiClient):
    """A fixed answer-model shadow; it never replaces the business classifier."""

    def __init__(self, settings: Settings, thinking: str):
        super().__init__(settings)
        self.thinking = thinking
        self.metadata: dict = {}

    def _openai(self, messages: list[AiMessage], stream: bool = False) -> str:
        if stream:
            raise ValueError("Fixed classification observations are non-streaming")
        with httpx.Client(trust_env=False, timeout=60, transport=httpx.HTTPTransport(retries=0)) as client:
            response = client.post(
                f"{self.settings.openai_base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {self.settings.openai_api_key}"},
                json={
                    "model": self.settings.openai_model,
                    "messages": [message.model_dump() for message in messages],
                    "temperature": 0,
                    "max_tokens": self.settings.ai_max_tokens,
                    "response_format": {"type": "json_object"},
                    "thinking": {"type": self.thinking},
                    "stream": False,
                },
            )
            response.raise_for_status()
            data = response.json()
        choice = data["choices"][0]
        self.metadata = {
            "generatedTokens": data.get("usage", {}).get("completion_tokens"),
            "promptTokens": data.get("usage", {}).get("prompt_tokens"),
            "finishReason": choice.get("finish_reason"),
            "providerModel": data.get("model"),
        }
        return choice["message"].get("content") or ""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--provider", choices=("ollama", "deepseek-json"), default="ollama")
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--input-format", choices=("plain", "quoted"), default="plain")
    parser.add_argument("--thinking", choices=("enabled", "disabled"), default="disabled")
    parser.add_argument("--max-tokens", type=int, default=2048)
    parser.add_argument("--system-prompt-file", type=Path,
                        default=ROOT / "finetune/reports/current-classifier/remote-profile/system-prompt.txt")
    args = parser.parse_args()
    if args.output.exists():
        raise RuntimeError("Evaluation output already exists; fixed observations are not silently rerun")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    datasets = []
    collections = []
    for path in args.dataset:
        rows = load_dataset(path)
        if not rows or len({row["id"] for row in rows}) != len(rows):
            raise ValueError("Evaluation requires nonempty datasets with unique IDs")
        for row in rows:
            row["expected"] = LABEL_MAP.get(row["output"], row["output"])
            if row["expected"] not in CLASSES:
                raise ValueError("Dataset contains an unsupported target label")
        datasets.append({"path": str(path.resolve()), "sha256": sha256(path), "rows": len(rows)})
        collections.append(rows)
    system = ""
    if args.provider == "ollama":
        args.base_url = args.base_url or "http://127.0.0.1:11435"
        with httpx.Client(trust_env=False) as metadata_client:
            response = metadata_client.post(f"{args.base_url}/api/show", json={"model": args.model}, timeout=5)
            response.raise_for_status()
            registered = response.json()
        max_generation_tokens = next(
            (int(line.split()[1]) for line in registered.get("parameters", "").splitlines()
             if line.split() and line.split()[0] == "num_predict"), None,
        )
        client = ObservedAiClient(Settings(
            _env_file=None, ai_provider="ollama", ollama_base_url=args.base_url,
            ollama_classifier_model=args.model, classifier_input_format=args.input_format,
        ))
    else:
        settings = Settings()
        if not settings.openai_api_key:
            raise RuntimeError("Configured answer-model credential is unavailable")
        args.base_url = args.base_url or settings.openai_base_url
        system = args.system_prompt_file.read_text(encoding="utf-8").strip()
        registered = {"system": system, "thinking": args.thinking, "parameters": "", "details": {}}
        max_generation_tokens = args.max_tokens
        client = ObservedDeepSeekClient(settings.model_copy(update={
            "ai_provider": "openai", "openai_model": args.model,
            "openai_base_url": args.base_url, "ai_max_tokens": args.max_tokens,
        }), args.thinking)
    attempts_cap = sum(len(rows) for rows in collections)
    report = {
        "status": "running", "startedAtUTC": datetime.now(timezone.utc).isoformat(),
        "provider": args.provider, "model": args.model, "baseURL": args.base_url,
        "inputFormat": args.input_format if args.provider == "ollama" else "raw-json-label",
        "thinking": args.thinking if args.provider != "ollama" else None, "datasets": datasets,
        "inputContract": {
            "systemSHA256": hashlib.sha256(registered.get("system", "").encode()).hexdigest(),
            "templateSHA256": hashlib.sha256(registered.get("template", "").encode()).hexdigest(),
            "quotedPrefix": CLASSIFIER_QUOTED_PREFIX if args.input_format == "quoted" else None,
            "registeredParameters": registered.get("parameters", ""),
            "registeredDetails": registered.get("details", {}),
        },
        "budget": {
            "modelAttemptsCap": attempts_cap, "maxGenerationTokens": max_generation_tokens,
            "generationTokensCap": attempts_cap * max_generation_tokens if max_generation_tokens else None,
            "retries": 0, "optimizerUpdates": 0,
        },
        "cases": [],
    }
    write_report(args.output, report)
    for dataset, rows in zip(datasets, collections):
        for row in rows:
            started = time.perf_counter()
            client.metadata = {}
            raw = ""
            error_type = None
            http_status = None
            try:
                if args.provider == "ollama":
                    raw = client.classify(row["input"])
                else:
                    body = client.complete([
                        AiMessage(role="system", content=system),
                        AiMessage(role="user", content=row["input"]),
                    ])
                    raw = parse_json_label(body, client.metadata.get("finishReason"))
            except Exception as exc:
                error_type = type(exc).__name__
                if isinstance(exc, httpx.HTTPStatusError):
                    http_status = exc.response.status_code
            valid = error_type is None and raw in CLASSES and client.metadata.get("finishReason") == "stop"
            invalid_reason = None if valid else (
                "request_error" if error_type else (
                    "length" if client.metadata.get("finishReason") == "length" else "non_label_or_unfinished"
                )
            )
            report["cases"].append({
                "id": row["id"], "datasetSHA256": dataset["sha256"],
                "sourceGroup": row.get("sourceGroup"), "expectedSource": row["output"],
                "expected": row["expected"], "predicted": raw if valid else "__INVALID__",
                "strictValid": valid, "invalidReason": invalid_reason, "errorType": error_type,
                "httpStatus": http_status,
                "inputSHA256": hashlib.sha256(row["input"].encode()).hexdigest(),
                "outputSHA256": hashlib.sha256(raw.encode()).hexdigest(),
                "latencyMs": (time.perf_counter() - started) * 1000, **client.metadata,
            })
            write_report(args.output, report)
            if error_type is not None:
                report.update(status="request-failed", completedCases=len(report["cases"]))
                write_report(args.output, report)
                raise RuntimeError("Runtime request failed; remaining fixed observations were not attempted")
            if len(report["cases"]) % 20 == 0:
                print(json.dumps({"completed": len(report["cases"]), "cap": report["budget"]["modelAttemptsCap"]}), flush=True)
    report["metrics"] = runtime_metrics(report["cases"])
    report["datasetMetrics"] = [
        {**dataset, **runtime_metrics([case for case in report["cases"] if case["datasetSHA256"] == dataset["sha256"]])}
        for dataset in datasets
    ]
    report.update(status="completed", completedAtUTC=datetime.now(timezone.utc).isoformat())
    write_report(args.output, report)
    print(json.dumps({"model": args.model, "metrics": report["metrics"]}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
