"""单次前向决策头分类器的运行时加载与推理。

加载训练产物（冻结基座 + LoRA 适配器 + 固定标签线性决策头 + 温度校准），
对输入文本做一次前向，输出四类校准概率。不做自回归生成，不存在无效标签；
所有权重只读本地文件，推理在调用线程内串行执行。
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path

import torch
from peft import PeftModel
from torch import nn
from transformers import AutoModelForCausalLM, AutoTokenizer

from app.core.classifier_contract import classifier_user_content
from app.core.config import Settings, get_settings

LABELS = ("正常", "焦虑", "低落", "高风险")


@dataclass(frozen=True)
class DecisionPrediction:
    """一次决策头前向的完整结果；置信度与 margin 均基于温度校准后的概率。"""

    label: str
    confidence: float
    margin: float
    high_risk_probability: float
    probabilities: dict[str, float]
    model_version: str


class DecisionHeadClassifier:
    """在应用进程内持有决策头模型，提供同步单次前向分类。

    构造时加载全部权重并固定为评估模式；predict 内部加锁串行推理，
    避免并发前向在半精度设备上产生不可预期的资源竞争。
    """

    def __init__(self, settings: Settings):
        """按配置加载基座、适配器、决策头与温度校准；任何缺失直接抛错由上层回退。"""
        root = settings.project_root
        device = settings.decision_head_device or (
            "cuda" if torch.cuda.is_available() else "mps" if torch.backends.mps.is_available() else "cpu"
        )
        self._device = device
        self._max_length = settings.decision_head_max_length
        self._input_format = settings.classifier_input_format
        self._system_prompt = (root / settings.decision_head_system_prompt_file).read_text(encoding="utf-8").strip()
        calibration = json.loads((root / settings.decision_head_calibration_path).read_text(encoding="utf-8"))
        if calibration["labels"] != list(LABELS):
            raise ValueError("温度校准文件的标签顺序与运行时四类定义不一致")
        self._temperature = float(calibration["temperature"])
        if self._temperature <= 0:
            raise ValueError("温度校准常数必须大于 0")
        self._model_version = f"decision-head@{Path(settings.decision_head_adapter_dir).parent.name}+t{self._temperature:.3f}"
        dtype = torch.float16 if device in {"mps", "cuda"} else torch.float32
        self._tokenizer = AutoTokenizer.from_pretrained(
            root / settings.decision_head_model_dir, trust_remote_code=True, local_files_only=True,
        )
        base = AutoModelForCausalLM.from_pretrained(
            root / settings.decision_head_model_dir,
            trust_remote_code=True,
            torch_dtype=dtype,
            low_cpu_mem_usage=True,
            local_files_only=True,
        ).to(device)
        base.config.use_cache = False
        peft_model = PeftModel.from_pretrained(base, root / settings.decision_head_adapter_dir)
        # 与训练脚本一致的访问路径：PEFT 包装下的因果语言模型，再取其 transformer 主体。
        self._transformer = peft_model.model.model
        head_payload = torch.load(root / settings.decision_head_head_path, map_location="cpu", weights_only=True)
        if head_payload["labels"] != list(LABELS):
            raise ValueError("决策头权重标签与运行时四类定义不一致")
        self._head = nn.Linear(head_payload["hiddenSize"], len(LABELS), dtype=torch.float32).to(device)
        self._head.load_state_dict(head_payload["head_state_dict"])
        self._head.eval()
        self._transformer.eval()
        self._lock = threading.Lock()

    def predict(self, text: str) -> DecisionPrediction:
        """对单条文本做一次前向，返回校准概率、置信度与前两名概率差。"""
        prompt = self._tokenizer.apply_chat_template(
            [
                {"role": "system", "content": self._system_prompt},
                {"role": "user", "content": classifier_user_content(text, self._input_format)},
            ],
            tokenize=False,
            add_generation_prompt=True,
        )
        encoded = self._tokenizer(
            prompt, add_special_tokens=False, truncation=True,
            max_length=self._max_length, return_tensors="pt",
        )
        input_ids = encoded["input_ids"].to(self._device)
        attention_mask = encoded["attention_mask"].to(self._device)
        with self._lock, torch.no_grad():
            hidden = self._transformer(input_ids=input_ids, attention_mask=attention_mask).last_hidden_state
            last_position = int(attention_mask.sum(dim=1)[0].item()) - 1
            logits = self._head(hidden[0, last_position].to(torch.float32)) / self._temperature
            probs = torch.softmax(logits, dim=-1).cpu().tolist()
        ordered = sorted(probs, reverse=True)
        best = max(range(len(LABELS)), key=lambda index: probs[index])
        return DecisionPrediction(
            label=LABELS[best],
            confidence=probs[best],
            margin=ordered[0] - ordered[1],
            high_risk_probability=probs[LABELS.index("高风险")],
            probabilities={label: probs[index] for index, label in enumerate(LABELS)},
            model_version=self._model_version,
        )


_INSTANCE: DecisionHeadClassifier | None = None
_INSTANCE_LOCK = threading.Lock()


def get_decision_classifier(settings: Settings | None = None) -> DecisionHeadClassifier:
    """返回进程级共享的决策头分类器，首次调用时加载权重。"""
    global _INSTANCE
    if _INSTANCE is None:
        with _INSTANCE_LOCK:
            if _INSTANCE is None:
                _INSTANCE = DecisionHeadClassifier(settings or get_settings())
    return _INSTANCE
