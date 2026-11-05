from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from app.core.config import Settings

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT / "evals/independent"
DATA = BASE / "data"
OUTPUT = BASE / "frozen-manifest.json"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_record(path: Path) -> dict:
    path = path.resolve() if path.is_absolute() else (ROOT / path).resolve()
    return {"path": str(path.relative_to(ROOT)), "sha256": sha256(path), "bytes": path.stat().st_size}


def main() -> None:
    validation_path = BASE / "data-validation.json"
    validation = json.loads(validation_path.read_text(encoding="utf-8"))
    if validation.get("status") != "passed":
        raise SystemExit("Dataset validation has not passed; refusing to freeze")

    settings = Settings()
    artifact_paths = [
        Path(settings.decision_head_head_path),
        Path(settings.decision_head_calibration_path),
        Path(settings.decision_head_adapter_dir) / "adapter_config.json",
        Path(settings.decision_head_adapter_dir) / "adapter_model.safetensors",
        Path(settings.decision_head_model_dir) / "config.json",
        ROOT / "finetune/reports/current-classifier/training/system-prompt.txt",
    ]
    missing = [str(path) for path in artifact_paths if not path.is_file()]
    if missing:
        raise SystemExit(f"Missing model artifacts: {missing}")

    git_diff = subprocess.run(["git", "diff", "--binary"], cwd=ROOT, check=True, capture_output=True).stdout
    manifest = {
        "datasetId": "ind-20261102-v2",
        "supersedes": "ind-20261102-v1",
        "frozenAt": datetime.now(timezone.utc).isoformat(),
        "status": "frozen-for-adr-implementation",
        "labelProvenance": "agent-authored; resource-routing and action-plan oracle corrections approved in ADR-0021/0022; remaining labels pending human review; not medical gold",
        "counts": validation["counts"],
        "totalCases": validation["totalCases"],
        "files": [file_record(path) for path in sorted(DATA.iterdir()) if path.is_file()],
        "validation": file_record(validation_path),
        "code": {
            "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
            "statusPorcelain": subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True).splitlines(),
            "trackedDiffSha256": hashlib.sha256(git_diff).hexdigest(),
        },
        "classifier": {
            "backend": settings.classifier_backend,
            "baseModelDir": str(settings.decision_head_model_dir),
            "adapterDir": str(settings.decision_head_adapter_dir),
            "inputFormat": "quoted",
            "artifacts": [file_record(path) for path in artifact_paths],
        },
        "scope": {
            "classifier": "Raw default decision head with frozen adapter, head and calibration.",
            "risk": "Full PsychologicalAssessmentService using the default decision head.",
            "rag": "Answerable retrieval questions only; refusal and unanswerable-question behavior are out of scope.",
            "quality": "Fixed CONSULT/LOW prompt path with real generation and model-based judging; not a human evaluation.",
            "e2e": "Real chat flow with an in-memory database and vector retrieval disabled; not a RAG end-to-end test.",
        },
    }
    OUTPUT.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"manifest": str(OUTPUT.relative_to(ROOT)), "datasetId": manifest["datasetId"], "totalCases": manifest["totalCases"], "fileHashes": manifest["files"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
