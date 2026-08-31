"""生成通用心理支持四分类数据，并按模板来源整组切分。"""
from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = ROOT / "finetune" / "data"
REPORT_PATH = ROOT / "target" / "general-classifier-data-report.json"
SEED = 42
INSTRUCTION = "判断用户当前表达的路由标签，只输出：正常、焦虑、低落、高风险。分类只用于心理支持分流，不作诊断。"

SCENARIOS = {
    "anxiety": "对未来和很多事情感到不安",
    "low_mood": "情绪和动力持续受到影响",
    "sleep": "睡眠和白天状态最近不稳定",
    "work": "工作任务和沟通带来压力",
    "family": "家庭期待和相处让我为难",
    "relationships": "亲密关系或朋友相处出现困难",
    "adjustment": "新的环境和生活变化让我不适应",
    "exam": "考试和准备进度让我有压力",
    "ordinary": "日常安排和普通事务",
    "safety": "现实安全和强烈痛苦",
}

TEMPLATES = {
    "正常": {
        "train": [
            "今天谈到{topic}，整体还能应付，只是想整理一下安排。",
            "关于{topic}，我现在状态平稳，想听听普通建议。",
            "最近有{topic}这件事，不过没有明显困扰，我只是来聊聊。",
            "我正在处理{topic}，进度还可以，想确认下一步怎么安排。",
            "今天的{topic}没有影响吃饭睡觉，我只是顺手记录一下。",
        ],
        "val": [
            "虽然有{topic}，但我目前能正常生活，只想简单交流。",
            "我对{topic}有些想法，情绪总体稳定，不需要危机支持。",
        ],
        "test": [
            "说到{topic}，我没有持续难受，今天只是想做个常规咨询。",
            "目前{topic}都在可控范围内，我想获得一点一般性信息。",
        ],
    },
    "焦虑": {
        "train": [
            "一想到{topic}我就紧张，脑子停不下来，总担心会搞砸。",
            "最近因为{topic}反复担心，身体也绷得很紧，很难放松。",
            "关于{topic}的不确定让我心慌，我总是在预想最坏结果。",
            "这几天{topic}带来的压力越来越大，我注意力很难集中。",
            "我为{topic}焦虑了好几天，晚上也一直想着下一步怎么办。",
        ],
        "val": [
            "只要提到{topic}我就坐立不安，担忧像循环一样停不住。",
            "我对{topic}有明显的未来担心，胸口发紧但没有伤害自己的想法。",
        ],
        "test": [
            "最近{topic}让我一直处于警觉状态，很怕失控或失败。",
            "我知道{topic}还没发生，但还是反复害怕，已经影响休息。",
        ],
    },
    "低落": {
        "train": [
            "因为{topic}我持续提不起劲，原来喜欢的事情也没兴趣。",
            "最近面对{topic}总觉得很沉，做什么都没有动力。",
            "这段时间{topic}让我情绪低落，连起床和吃饭都变得费力。",
            "我对{topic}越来越麻木，常常想哭，也不想见人。",
            "经历{topic}之后我一直很疲惫，感觉自己什么都做不好。",
        ],
        "val": [
            "最近{topic}让我失去兴趣，白天也很难完成基本事情。",
            "我因为{topic}低沉了很久，觉得没有动力，但没有具体伤害计划。",
        ],
        "test": [
            "面对{topic}我每天都很空，原本在意的事情现在毫无感觉。",
            "这阵子{topic}让我持续难过和退缩，生活功能明显下降。",
        ],
    },
    "高风险": {
        "train": [
            "因为{topic}我不想活了，已经在考虑结束生命。",
            "我被{topic}压得撑不住，最近反复想自杀。",
            "面对{topic}我控制不住想伤害自己，现在很不安全。",
            "我已经为结束生命做好准备，{topic}只是最后的导火索。",
            "{topic}让我决定今晚轻生，我手边已经准备了工具。",
        ],
        "val": [
            "我因{topic}写好了告别信息，准备很快结束自己的生命。",
            "现在{topic}让我有明确自残计划，我无法保证自己安全。",
        ],
        "test": [
            "我为了摆脱{topic}已经选好时间地点，打算伤害自己。",
            "{topic}之后我准备去死，工具就在旁边，需要立即有人来。",
        ],
    },
}


def _ngrams(text: str, size: int = 3) -> set[str]:
    compact = "".join(text.split())
    return {compact[index:index + size] for index in range(max(1, len(compact) - size + 1))}


def _similarity(left: str, right: str) -> float:
    a, b = _ngrams(left), _ngrams(right)
    return len(a & b) / max(1, len(a | b))


def build_rows() -> list[dict]:
    rows: list[dict] = []
    for label, splits in TEMPLATES.items():
        template_number = 0
        for split, templates in splits.items():
            for template in templates:
                template_number += 1
                source_group = f"{label}-template-{template_number:02d}"
                for scenario, topic in SCENARIOS.items():
                    text = template.format(topic=topic)
                    row_id = hashlib.sha256(f"{source_group}:{scenario}:{SEED}".encode()).hexdigest()[:16]
                    rows.append({
                        "id": row_id,
                        "instruction": INSTRUCTION,
                        "input": text,
                        "output": label,
                        "scenario": scenario,
                        "sourceGroup": source_group,
                        "provenance": "deterministic-template-v1",
                        "split": split,
                        "humanReviewStatus": "pending",
                    })
    return rows


def validate(rows: list[dict]) -> dict:
    ids = [row["id"] for row in rows]
    texts = [row["input"] for row in rows]
    if len(ids) != len(set(ids)):
        raise ValueError("样本 ID 重复")
    if len(texts) != len(set(texts)):
        raise ValueError("存在精确重复文本")

    group_splits: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        group_splits[row["sourceGroup"]].add(row["split"])
    leaked_groups = sorted(group for group, splits in group_splits.items() if len(splits) > 1)
    if leaked_groups:
        raise ValueError(f"来源组跨集合泄漏：{leaked_groups}")

    cross_split_near_duplicates = []
    for left_index, left in enumerate(rows):
        for right in rows[left_index + 1:]:
            if left["split"] == right["split"]:
                continue
            score = _similarity(left["input"], right["input"])
            if score >= 0.86:
                cross_split_near_duplicates.append({"left": left["id"], "right": right["id"], "score": score})
    if cross_split_near_duplicates:
        raise ValueError(f"跨集合近重复：{cross_split_near_duplicates[:5]}")

    distribution = {}
    for split in ("train", "val", "test"):
        split_rows = [row for row in rows if row["split"] == split]
        distribution[split] = {
            "cases": len(split_rows),
            "labels": dict(Counter(row["output"] for row in split_rows)),
            "scenarios": dict(Counter(row["scenario"] for row in split_rows)),
            "sourceGroups": len({row["sourceGroup"] for row in split_rows}),
        }
    return {
        "datasetVersion": "general-routing-v1",
        "seed": SEED,
        "totalCases": len(rows),
        "exactDuplicates": 0,
        "crossSplitNearDuplicates": 0,
        "sourceGroupLeakage": 0,
        "humanReviewComplete": False,
        "distribution": distribution,
    }


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


def main() -> None:
    rows = build_rows()
    report = validate(rows)
    for split in ("train", "val", "test"):
        write_jsonl(OUT_DIR / f"general-{split}.jsonl", [row for row in rows if row["split"] == split])
    write_jsonl(OUT_DIR / "general-source.jsonl", rows)
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
