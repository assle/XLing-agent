import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

from finetune.scripts import generate_general_classifier_data as generator
from finetune.scripts.generate_general_classifier_data import SCENARIOS, build_rows, validate


def test_general_classifier_data_is_grouped_balanced_and_leak_free() -> None:
    """生成全部模板样本并运行数据校验。

    核对总数、分集合数量、四标签十场景覆盖及无来源组交叉，同时保留人工审核未完成标志。
    """
    report = validate(build_rows())
    assert report["totalCases"] == 360
    assert report["sourceGroupLeakage"] == 0
    assert report["crossSplitNearDuplicates"] == 0
    assert report["humanReviewComplete"] is False
    assert report["distribution"]["train"]["cases"] == 200
    assert report["distribution"]["val"]["cases"] == 80
    assert report["distribution"]["test"]["cases"] == 80
    for split in ("train", "val", "test"):
        assert set(report["distribution"][split]["labels"]) == {"正常", "焦虑", "低落", "高风险"}
        assert len(report["distribution"][split]["scenarios"]) == 10


def test_normal_examples_request_ordinary_tasks_without_asserting_distress() -> None:
    rows = [row for row in build_rows() if row["output"] == "正常"]
    assert len(rows) == 90
    distress_signals = (
        "压力", "不安", "焦虑", "紧张", "难过", "低落", "痛苦", "不适应",
        "失去兴趣", "没有动力", "自杀", "自残", "伤害自己",
    )
    asserted_signals = [
        (row["id"], signal)
        for row in rows
        for signal in distress_signals
        if signal in row["input"]
    ]
    assert not asserted_signals
    task_signals = ("整理", "安排", "清单", "表格", "偏好", "备忘", "笔记", "要点", "标题")
    assert all(any(signal in row["input"] for signal in task_signals) for row in rows)


def test_normal_facts_have_distinct_identities_from_legacy_normal_templates() -> None:
    legacy_ids = {
        hashlib.sha256(f"正常-template-{number:02d}:{scenario}:42".encode()).hexdigest()[:16]
        for number in range(1, 10)
        for scenario in SCENARIOS
    }
    rows = [row for row in build_rows() if row["output"] == "正常"]
    assert not {row["id"] for row in rows} & legacy_ids
    assert all(row["provenance"] == "deterministic-normal-facts-v2" for row in rows)


def test_other_three_classes_preserve_their_existing_content_and_metadata() -> None:
    rows = [row for row in build_rows() if row["output"] != "正常"]
    assert len(rows) == 270
    canonical = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    assert hashlib.sha256(canonical).hexdigest() == "5961e26214f34aa3c49c71e7de91e8ffaf26758aadb68d62019c642a8234668b"


def test_generator_entrypoint_preserves_legacy_snapshots(tmp_path: Path) -> None:
    script = tmp_path / "finetune" / "scripts" / "generate_general_classifier_data.py"
    script.parent.mkdir(parents=True)
    shutil.copyfile(generator.__file__, script)
    data_dir = tmp_path / "finetune" / "data"
    data_dir.mkdir()
    legacy = {}
    for split in ("train", "val", "test", "source"):
        path = data_dir / f"general-{split}.jsonl"
        legacy[path] = f"frozen-v1-{split}\n".encode()
        path.write_bytes(legacy[path])

    completed = subprocess.run(
        [sys.executable, str(script)], capture_output=True, text=True, check=True, timeout=30,
    )
    report = json.loads(completed.stdout)
    assert report["datasetVersion"] == "general-routing-v2"
    assert all(path.read_bytes() == content for path, content in legacy.items())
    for split, count in (("train", 200), ("val", 80), ("test", 80), ("source", 360)):
        current = data_dir / "general-routing-v2" / f"general-{split}.jsonl"
        rows = [json.loads(line) for line in current.read_text().splitlines()]
        assert len(rows) == count
        assert all(row["provenance"] == "deterministic-normal-facts-v2" for row in rows if row["output"] == "正常")
