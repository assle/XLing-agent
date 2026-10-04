"""生成通用心理支持四分类数据，并按模板来源整组切分。"""
from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATASET_VERSION = "general-routing-v2"
OUT_DIR = ROOT / "finetune" / "data" / DATASET_VERSION
REPORT_PATH = ROOT / "target" / f"{DATASET_VERSION}-data-report.json"
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

NORMAL_SCENARIOS = {
    "anxiety": "明天的日程安排",
    "low_mood": "周末的家务清单",
    "sleep": "卧室灯光和休息时间",
    "work": "工作任务的先后顺序",
    "family": "全家聚餐的采购清单",
    "relationships": "和朋友约见的时间地点",
    "adjustment": "搬家后的物品收纳",
    "exam": "复习资料的目录整理",
    "ordinary": "周六买菜和准备便当",
    "safety": "通讯录中联系人的整理",
}

TEMPLATES = {
    "正常": {
        "train": [
            "今天想整理{topic}，请帮我列出最先做的两步。",
            "{topic}已经定好了，我想把它记成简短的备忘。",
            "这是我下周的{topic}，想按先后顺序整理一下。",
            "我准备更新{topic}，你能给我一个简洁的表格格式吗？",
            "请记住我喜欢用小清单处理{topic}，以后照这个偏好提醒就好。",
        ],
        "val": [
            "关于{topic}，我偏好一次只列三项，请按这个格式写。",
            "{topic}可以先给我一个标题和几个要点吗？我要保存到笔记里。",
        ],
        "test": [
            "帮我把{topic}分成今天和明天两栏，方便照着安排。",
            "我想把{topic}整理给朋友看，能写成一句明白的说明吗？",
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
    """去除空白后提取连续 size 个字符组成的片段集合。

    默认每片三个字符，用于近重复比较；短于片长的文本仍生成一次切片。
    """
    compact = "".join(text.split())
    return {compact[index:index + size] for index in range(max(1, len(compact) - size + 1))}


def _similarity(left: str, right: str) -> float:
    """计算两段文本的字符片段交集占并集的比例。

    返回零到一之间的相似度，分母至少为一；这是字符重叠比较，不使用语义模型。
    """
    a, b = _ngrams(left), _ngrams(right)
    return len(a & b) / max(1, len(a | b))


def build_rows() -> list[dict]:
    """把各标签模板与场景组合成带来源分组和集合归属的合成样本。

    样本编号由模板组、场景和固定种子计算得到；标明人工审核待完成，不把生成等同于审核。
    """
    rows: list[dict] = []
    for label, splits in TEMPLATES.items():
        template_number = 0
        for split, templates in splits.items():
            for template in templates:
                template_number += 1
                prefix = "正常-facts-v2" if label == "正常" else label
                source_group = f"{prefix}-template-{template_number:02d}"
                topics = NORMAL_SCENARIOS if label == "正常" else SCENARIOS
                for scenario, topic in topics.items():
                    text = template.format(topic=topic)
                    row_id = hashlib.sha256(f"{source_group}:{scenario}:{SEED}".encode()).hexdigest()[:16]
                    rows.append({
                        "id": row_id,
                        "instruction": INSTRUCTION,
                        "input": text,
                        "output": label,
                        "scenario": scenario,
                        "sourceGroup": source_group,
                        "provenance": "deterministic-normal-facts-v2" if label == "正常" else "deterministic-template-v1",
                        "split": split,
                        "humanReviewStatus": "pending",
                    })
    return rows


def validate(rows: list[dict]) -> dict:
    """检查样本编号、精确文本、来源组和跨集合近重复情况。

    同模板组不能跨训练、验证和测试集合，近重复相似度达到 0.86 时抛错。
    全部检查通过后返回各集合的标签及场景分布，仍保留人工审核未完成标志。
    """
    ids = [row["id"] for row in rows]
    texts = [row["input"] for row in rows]
    if len(ids) != len(set(ids)):
        raise ValueError("样本 ID 重复")
    if len(texts) != len(set(texts)):
        raise ValueError("存在精确重复文本")

    # 把同模板的改写视为同一来源组，检查它们是否泄漏到不同用途的数据集合。
    group_splits: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        group_splits[row["sourceGroup"]].add(row["split"])
    leaked_groups = sorted(group for group, splits in group_splits.items() if len(splits) > 1)
    if leaked_groups:
        raise ValueError(f"来源组跨集合泄漏：{leaked_groups}")

    cross_split_near_duplicates = []
    for left_index, left in enumerate(rows):
        for right in rows[left_index + 1:]:
            # 只检查不同集合之间的近重复，避免把训练内部的相似模板当作跨集合泄漏。
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
        "datasetVersion": DATASET_VERSION,
        "seed": SEED,
        "totalCases": len(rows),
        "exactDuplicates": 0,
        "crossSplitNearDuplicates": 0,
        "sourceGroupLeakage": 0,
        "humanReviewComplete": False,
        "distribution": distribution,
    }


def write_jsonl(path: Path, rows: list[dict]) -> None:
    """创建父目录并以每行一个对象的格式写入样本。

    保留中文，覆盖指定文件；不会追加到已有数据末尾。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


def main() -> None:
    """生成样本、完成重复检查，再输出分集合文件、总源文件和检查报告。

    只有 validate 成功后才开始写文件，脚本会覆盖配置的输出位置。
    """
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
