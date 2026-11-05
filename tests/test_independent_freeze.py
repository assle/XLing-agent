import json
from types import SimpleNamespace

from app.core.config import Settings
from evals.independent import freeze_dataset


def test_freeze_records_artifacts_from_the_runtime_bundle(tmp_path, monkeypatch):
    base = tmp_path / "evals/independent"
    data = base / "data"
    data.mkdir(parents=True)
    (base / "data-validation.json").write_text(
        json.dumps({"status": "passed", "counts": {"LOW": 0}, "totalCases": 0}), encoding="utf-8"
    )
    bundle_dir = tmp_path / "bundle"
    (bundle_dir / "base").mkdir(parents=True)
    (bundle_dir / "adapter").mkdir()
    for relative_path in (
        "base/config.json",
        "adapter/adapter_config.json",
        "adapter/adapter_model.safetensors",
        "head.pt",
        "calibration.json",
        "system-prompt.txt",
    ):
        (bundle_dir / relative_path).write_text(relative_path, encoding="utf-8")
    settings = Settings(_env_file=None, decision_head_bundle_dir=str(bundle_dir))
    monkeypatch.setattr(freeze_dataset, "ROOT", tmp_path)
    monkeypatch.setattr(freeze_dataset, "BASE", base)
    monkeypatch.setattr(freeze_dataset, "DATA", data)
    monkeypatch.setattr(freeze_dataset, "OUTPUT", base / "frozen-manifest.json")
    monkeypatch.setattr(freeze_dataset, "Settings", lambda: settings)
    monkeypatch.setattr(
        freeze_dataset.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(stdout=b""),
    )
    monkeypatch.setattr(
        freeze_dataset.subprocess,
        "check_output",
        lambda command, **kwargs: "abc123\n" if "rev-parse" in command else "",
    )

    freeze_dataset.main()

    manifest = json.loads(freeze_dataset.OUTPUT.read_text(encoding="utf-8"))
    assert manifest["classifier"]["bundleDir"] == str(bundle_dir)
    assert [artifact["path"] for artifact in manifest["classifier"]["artifacts"]] == [
        "bundle/head.pt",
        "bundle/calibration.json",
        "bundle/adapter/adapter_config.json",
        "bundle/adapter/adapter_model.safetensors",
        "bundle/base/config.json",
        "bundle/system-prompt.txt",
    ]
