import json
from types import SimpleNamespace

from torch import nn

from app.core.config import Settings
from app.services import decision_classifier
from app.services.decision_classifier import LABELS, DecisionHeadClassifier


def test_classifier_loads_artifacts_from_the_configured_bundle(tmp_path, monkeypatch):
    bundle_dir = tmp_path / "bundle-v1"
    (bundle_dir / "base").mkdir(parents=True)
    (bundle_dir / "adapter").mkdir()
    (bundle_dir / "head.pt").write_bytes(b"test-only")
    (bundle_dir / "calibration.json").write_text(
        json.dumps({"labels": list(LABELS), "temperature": 1.25}), encoding="utf-8"
    )
    (bundle_dir / "system-prompt.txt").write_text("classify the message", encoding="utf-8")
    loaded = {}
    transformer = SimpleNamespace(eval=lambda: loaded.setdefault("evaluated", True))

    class BaseModel:
        def __init__(self):
            self.config = SimpleNamespace(use_cache=True)
            self.model = SimpleNamespace(model=transformer)

        def to(self, device):
            loaded["device"] = device
            return self

    def load_tokenizer(path, **kwargs):
        loaded["base_for_tokenizer"] = path
        return SimpleNamespace()

    def load_base(path, **kwargs):
        loaded["base_for_model"] = path
        return BaseModel()

    def load_adapter(base, path):
        loaded["adapter"] = path
        return base

    def load_head(path, **kwargs):
        loaded["head"] = path
        return {
            "labels": list(LABELS),
            "hiddenSize": 2,
            "head_state_dict": head.state_dict(),
        }

    head = nn.Linear(2, len(LABELS), dtype=decision_classifier.torch.float32)
    monkeypatch.setattr(decision_classifier.AutoTokenizer, "from_pretrained", load_tokenizer)
    monkeypatch.setattr(decision_classifier.AutoModelForCausalLM, "from_pretrained", load_base)
    monkeypatch.setattr(decision_classifier.PeftModel, "from_pretrained", load_adapter)
    monkeypatch.setattr(decision_classifier.torch, "load", load_head)
    settings = Settings(
        _env_file=None,
        decision_head_bundle_dir=str(bundle_dir),
        decision_head_device="cpu",
    )

    DecisionHeadClassifier(settings)

    assert loaded == {
        "base_for_tokenizer": bundle_dir / "base",
        "base_for_model": bundle_dir / "base",
        "device": "cpu",
        "adapter": bundle_dir / "adapter",
        "head": bundle_dir / "head.pt",
        "evaluated": True,
    }
