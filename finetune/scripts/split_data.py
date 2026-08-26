"""切分情绪四分类合成数据为训练集和验证集。

9:1 切分，每类 60 条验证（共 240），训练 2160。固定随机种子保证可复现。
标签保持中文不动。同时生成 LLaMA-Factory 可识别的 dataset_info.json。

用法:
    python finetune/scripts/split_data.py
"""
from __future__ import annotations

import json
import random
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "psychqa_synthetic.jsonl"
OUT_DIR = ROOT / "finetune" / "data"
TRAIN_FILE = OUT_DIR / "train.jsonl"
VAL_FILE = OUT_DIR / "val.jsonl"
INFO_FILE = OUT_DIR / "dataset_info.json"

SEED = 42
VAL_PER_CLASS = 60


def load_data(path: Path) -> list[dict]:
    rows = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def split_by_label(rows: list[dict], val_per_class: int, seed: int) -> tuple[list[dict], list[dict]]:
    by_label: dict[str, list[dict]] = {}
    for r in rows:
        by_label.setdefault(r["output"], []).append(r)

    rng = random.Random(seed)
    train, val = [], []
    for label in sorted(by_label):
        pool = list(by_label[label])
        rng.shuffle(pool)
        val.extend(pool[:val_per_class])
        train.extend(pool[val_per_class:])
    return train, val


def write_jsonl(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def write_dataset_info(path: Path) -> None:
    info = {
        "psychqa_cls_train": {
            "file_name": "train.jsonl",
            "formatting": "alpaca",
        },
        "psychqa_cls_val": {
            "file_name": "val.jsonl",
            "formatting": "alpaca",
        },
    }
    with path.open("w", encoding="utf-8") as f:
        json.dump(info, f, ensure_ascii=False, indent=2)


def main() -> None:
    rows = load_data(SRC)
    print(f"源数据: {len(rows)} 条")
    print(f"标签分布: {dict(Counter(r['output'] for r in rows))}")

    train, val = split_by_label(rows, VAL_PER_CLASS, SEED)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    write_jsonl(TRAIN_FILE, train)
    write_jsonl(VAL_FILE, val)
    write_dataset_info(INFO_FILE)

    print(f"\n训练集: {len(train)} 条 -> {TRAIN_FILE.relative_to(ROOT)}")
    print(f"  标签分布: {dict(Counter(r['output'] for r in train))}")
    print(f"验证集: {len(val)} 条 -> {VAL_FILE.relative_to(ROOT)}")
    print(f"  标签分布: {dict(Counter(r['output'] for r in val))}")
    print(f"数据集注册: {INFO_FILE.relative_to(ROOT)}")
    print(f"随机种子: {SEED} (可复现)")


if __name__ == "__main__":
    main()
