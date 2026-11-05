from __future__ import annotations

import argparse
import json
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.comments import Comment
from openpyxl.formatting.rule import CellIsRule, ColorScaleRule
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT / "evals/independent"
RUN = BASE / "results/20261102-v1"
OUTPUT = RUN / "independent-annotation-and-machine-results.xlsx"

ACCENT = "175E63"
ACCENT_LIGHT = "E8F2F2"
HUMAN_FILL = PatternFill("solid", fgColor="FFF4CC")
MACHINE_FILL = PatternFill("solid", fgColor="EDF6EC")
FROZEN_FILL = PatternFill("solid", fgColor="EAF0F8")
HEADER_FILL = PatternFill("solid", fgColor=ACCENT)
HEADER_FONT = Font(color="FFFFFF", bold=True, size=10)
TITLE_FONT = Font(color=ACCENT, bold=True, size=18)
SUBTITLE_FONT = Font(color="52636A", size=10)
THIN_GREY = Side(style="thin", color="DCE3E6")


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def yes_no(value: bool) -> str:
    return "是" if value else "否"


def set_cell(cell, value) -> None:
    cell.value = value
    if isinstance(value, str) and value.startswith("="):
        cell.data_type = "s"


def add_data_sheet(
    wb: Workbook,
    name: str,
    title: str,
    headers: list[str],
    rows: list[list],
    widths: list[float],
    human_columns: list[int],
    machine_columns: list[int],
    frozen_columns: list[int],
    wrap_columns: list[int],
) -> object:
    ws = wb.create_sheet(name)
    ws.sheet_view.showGridLines = False
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=len(headers))
    set_cell(ws.cell(1, 1), title)
    ws.cell(1, 1).font = Font(color=ACCENT, bold=True, size=14)
    ws.cell(1, 1).alignment = Alignment(vertical="center")
    ws.row_dimensions[1].height = 28
    ws.row_dimensions[2].height = 8
    for column, header in enumerate(headers, start=1):
        cell = ws.cell(3, column)
        set_cell(cell, header)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = Border(bottom=Side(style="medium", color=ACCENT))
    ws.row_dimensions[3].height = 34
    for row_index, values in enumerate(rows, start=4):
        for column, value in enumerate(values, start=1):
            cell = ws.cell(row_index, column)
            set_cell(cell, value)
            cell.alignment = Alignment(
                vertical="top",
                wrap_text=column in wrap_columns,
            )
            cell.border = Border(bottom=THIN_GREY)
            cell.font = Font(size=10, color="17242A")
            if column in human_columns:
                cell.fill = HUMAN_FILL
            elif column in machine_columns:
                cell.fill = MACHINE_FILL
            elif column in frozen_columns:
                cell.fill = FROZEN_FILL
        ws.row_dimensions[row_index].height = 64 if wrap_columns else 22
    for index, width in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(index)].width = width
    ws.freeze_panes = "A4"
    ws.auto_filter.ref = f"A3:{get_column_letter(len(headers))}{len(rows) + 3}"
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.print_title_rows = "1:3"
    return ws


def add_list_validation(ws, column: int, values: list[str], row_count: int) -> None:
    validation = DataValidation(type="list", formula1='"' + ",".join(values) + '"', allow_blank=True)
    validation.error = "请选择下拉列表中的有效标签。"
    validation.errorTitle = "标签无效"
    validation.prompt = "请选择人工标注结果；留空表示尚未标注。"
    validation.promptTitle = "人工标注"
    validation.showErrorMessage = True
    validation.showInputMessage = True
    ws.add_data_validation(validation)
    letter = get_column_letter(column)
    validation.add(f"{letter}4:{letter}{row_count + 3}")


def add_score_validation(ws, columns: list[int], row_count: int) -> None:
    for column in columns:
        validation = DataValidation(type="whole", operator="between", formula1="1", formula2="5", allow_blank=True)
        validation.error = "评分必须是 1 到 5 的整数。"
        validation.showErrorMessage = True
        ws.add_data_validation(validation)
        letter = get_column_letter(column)
        validation.add(f"{letter}4:{letter}{row_count + 3}")


def add_mismatch_format(ws, column: int, row_count: int) -> None:
    letter = get_column_letter(column)
    ws.conditional_formatting.add(
        f"{letter}4:{letter}{row_count + 3}",
        CellIsRule(operator="equal", formula=['"否"'], fill=PatternFill("solid", fgColor="FCE8E6")),
    )


def build() -> None:
    manifest = load_json(BASE / "frozen-manifest.json")
    summary = load_json(RUN / "summary.json")
    classifier_data = load_jsonl(BASE / "data/classifier.jsonl")
    risk_data = load_json(BASE / "data/risk.json")
    rag_data = load_jsonl(BASE / "data/rag.jsonl")
    quality_data = load_json(BASE / "data/quality.json")
    e2e_data = load_jsonl(BASE / "data/e2e.jsonl")
    classifier_report = load_json(RUN / "classifier.json")
    risk_report = load_json(RUN / "risk.json")
    rag_report = load_json(RUN / "rag-direct/rag-hybrid-rrf.json")
    quality_report = load_json(RUN / "quality-final.json")
    e2e_report = load_json(RUN / "e2e-recovered.json")

    classifier_machine = {item["id"]: item for item in classifier_report["datasets"][0]["casesDetail"]}
    risk_machine = {item["id"]: item for item in risk_report["results"]}
    rag_machine = {item["id"]: item for item in rag_report["results"]}
    quality_machine = {item["id"]: item for item in quality_report["results"]}
    e2e_machine = {item["id"]: item for item in e2e_report["cases"]}

    wb = Workbook()
    cover = wb.active
    cover.title = "总览"
    cover.sheet_view.showGridLines = False
    cover.column_dimensions["A"].width = 3
    cover.column_dimensions["B"].width = 28
    cover.column_dimensions["C"].width = 88
    cover.column_dimensions["D"].width = 20
    cover.merge_cells("B2:D2")
    set_cell(cover["B2"], "独立测试集｜人工标注与逐题机器结果")
    cover["B2"].font = TITLE_FONT
    cover.row_dimensions[2].height = 34
    cover.merge_cells("B3:D3")
    set_cell(cover["B3"], f"数据集 {manifest['datasetId']}｜共 {manifest['totalCases']} 例｜冻结后未修改题目或标签")
    cover["B3"].font = SUBTITLE_FONT
    instructions = [
        "黄色列：由你填写人工标签或评分；留空表示尚未标注。",
        "蓝色列：冻结数据与原始代理标签，仅供对照，请勿直接覆盖。",
        "绿色列：机器实际输出，包括预测、概率、检索、回复、评分与流程事件。",
        "分类、风险、端到端的人工标签已设置下拉选项；质量人工评分为 1–5 的整数。",
        "本工作簿不是医学金标；人工复核前，所有冻结标签均为代理编写的工程目标。",
    ]
    for index, text in enumerate(instructions, start=5):
        cover.merge_cells(start_row=index, start_column=2, end_row=index, end_column=4)
        set_cell(cover.cell(index, 2), "• " + text)
        cover.cell(index, 2).alignment = Alignment(wrap_text=True, vertical="top")
        cover.row_dimensions[index].height = 24
    cover.cell(11, 2, "工作表").font = HEADER_FONT
    cover.cell(11, 2).fill = HEADER_FILL
    cover.cell(11, 3, "内容").font = HEADER_FONT
    cover.cell(11, 3).fill = HEADER_FILL
    cover.cell(11, 4, "题数").font = HEADER_FONT
    cover.cell(11, 4).fill = HEADER_FILL
    sheet_index = [
        ("冻结清单", "冻结时间、代码版本、数据与模型产物 SHA-256", "—"),
        ("分类_48", "四分类原文、冻结标签、预测、置信度与四类概率", 48),
        ("风险_36", "三级风险原文、冻结标签、完整风险服务预测与摘要", 36),
        ("RAG_30", "期望来源、Top 4 实际检索来源与片段、逐题排序指标", 30),
        ("质量_20", "完整机器回复、四维评分及评分理由、人工评分列", 20),
        ("端到端_24", "实际分流、风险、审核、事件、知识访问和失败原因", 24),
    ]
    for row, values in enumerate(sheet_index, start=12):
        for column, value in enumerate(values, start=2):
            set_cell(cover.cell(row, column), value)
            cover.cell(row, column).border = Border(bottom=THIN_GREY)
            cover.cell(row, column).alignment = Alignment(vertical="top", wrap_text=True)
        cover.row_dimensions[row].height = 28
    cover.freeze_panes = "B11"

    ws = wb.create_sheet("冻结清单")
    ws.sheet_view.showGridLines = False
    ws.column_dimensions["A"].width = 34
    ws.column_dimensions["B"].width = 102
    ws.column_dimensions["C"].width = 18
    ws.merge_cells("A1:C1")
    set_cell(ws["A1"], "冻结实验清单")
    ws["A1"].font = TITLE_FONT
    metadata = [
        ("数据集 ID", manifest["datasetId"]),
        ("冻结时间（UTC）", manifest["frozenAt"]),
        ("冻结状态", manifest["status"]),
        ("标签来源", "代理编写与标注，未经人工复核，不是医学金标"),
        ("总题数", manifest["totalCases"]),
        ("各模块题数", json.dumps(manifest["counts"], ensure_ascii=False)),
        ("代码提交", manifest["code"]["commit"]),
        ("Tracked diff SHA-256", manifest["code"]["trackedDiffSha256"]),
        ("冻结时工作区状态", "\n".join(manifest["code"]["statusPorcelain"])),
        ("分类器后端", manifest["classifier"]["backend"]),
        ("基座模型目录", manifest["classifier"]["baseModelDir"]),
        ("适配器目录", manifest["classifier"]["adapterDir"]),
        ("输入格式", manifest["classifier"]["inputFormat"]),
    ]
    for row, (key, value) in enumerate(metadata, start=3):
        set_cell(ws.cell(row, 1), key)
        set_cell(ws.cell(row, 2), value)
        ws.cell(row, 1).font = Font(bold=True, color=ACCENT)
        ws.cell(row, 1).fill = PatternFill("solid", fgColor=ACCENT_LIGHT)
        ws.cell(row, 2).alignment = Alignment(wrap_text=True, vertical="top")
        ws.cell(row, 1).border = ws.cell(row, 2).border = Border(bottom=THIN_GREY)
        ws.row_dimensions[row].height = 36 if key == "冻结时工作区状态" else 24
    scope_start = 18
    ws.cell(scope_start, 1, "评测范围").font = HEADER_FONT
    ws.cell(scope_start, 1).fill = HEADER_FILL
    ws.cell(scope_start, 2, "冻结口径").font = HEADER_FONT
    ws.cell(scope_start, 2).fill = HEADER_FILL
    for row, (key, value) in enumerate(manifest["scope"].items(), start=scope_start + 1):
        set_cell(ws.cell(row, 1), key)
        set_cell(ws.cell(row, 2), value)
        ws.cell(row, 2).alignment = Alignment(wrap_text=True, vertical="top")
        ws.row_dimensions[row].height = 38
    artifact_start = scope_start + 7
    for column, value in enumerate(["类型", "文件路径", "SHA-256 / 字节数"], start=1):
        set_cell(ws.cell(artifact_start, column), value)
        ws.cell(artifact_start, column).font = HEADER_FONT
        ws.cell(artifact_start, column).fill = HEADER_FILL
    artifact_rows = [("冻结数据", item) for item in manifest["files"]] + [("模型与提示产物", item) for item in manifest["classifier"]["artifacts"]]
    for row, (kind, item) in enumerate(artifact_rows, start=artifact_start + 1):
        set_cell(ws.cell(row, 1), kind)
        set_cell(ws.cell(row, 2), item["path"])
        set_cell(ws.cell(row, 3), f"{item['sha256']}\n{item['bytes']} bytes")
        ws.cell(row, 3).alignment = Alignment(wrap_text=True, vertical="top")
        ws.row_dimensions[row].height = 34
        for column in range(1, 4):
            ws.cell(row, column).border = Border(bottom=THIN_GREY)
    ws.freeze_panes = "A3"

    rows = []
    for item in classifier_data:
        machine = classifier_machine[item["id"]]
        probabilities = machine["probabilities"]
        rows.append([
            item["id"], item["scenario"], item["input"], item["output"], None, None,
            machine["predicted"], yes_no(machine["expected"] == machine["predicted"]), machine["confidence"],
            probabilities["正常"], probabilities["焦虑"], probabilities["低落"], probabilities["高风险"], machine["latencyMs"],
        ])
    ws = add_data_sheet(
        wb, "分类_48", "分类独立测试集｜48 例",
        ["ID", "场景", "输入文本", "冻结标签", "人工标签", "人工备注", "机器预测", "冻结与机器一致", "机器置信度", "正常概率", "焦虑概率", "低落概率", "高风险概率", "延迟（ms）"],
        rows, [24, 24, 58, 12, 12, 30, 12, 16, 14, 14, 14, 14, 14, 14], [5, 6], list(range(7, 15)), [3, 4], [3, 6],
    )
    add_list_validation(ws, 5, ["正常", "焦虑", "低落", "高风险"], len(rows))
    add_mismatch_format(ws, 8, len(rows))
    for row in range(4, len(rows) + 4):
        for column in [9, 10, 11, 12, 13]:
            ws.cell(row, column).number_format = "0.000000"
        ws.cell(row, 14).number_format = "0.00"

    rows = []
    for item in risk_data:
        machine = risk_machine[item["id"]]
        rows.append([
            item["id"], item["category"], item["text"], item["expected_risk"], item["notes"], None, None,
            machine["predictedRisk"], yes_no(machine["hit"]), machine["emotion"], machine["emotionScore"], machine["confidence"], machine["summary"],
        ])
    ws = add_data_sheet(
        wb, "风险_36", "完整风险服务独立测试集｜36 例",
        ["ID", "类别", "输入文本", "冻结风险", "标签说明", "人工风险", "人工备注", "机器风险", "冻结与机器一致", "机器情绪", "情绪分", "风险置信度", "机器摘要"],
        rows, [24, 24, 58, 12, 36, 12, 30, 12, 16, 12, 12, 14, 58], [6, 7], list(range(8, 14)), [3, 4, 5], [3, 5, 7, 13],
    )
    add_list_validation(ws, 6, ["LOW", "MEDIUM", "HIGH"], len(rows))
    add_mismatch_format(ws, 9, len(rows))
    for row in range(4, len(rows) + 4):
        ws.cell(row, 11).number_format = "0.000000"
        ws.cell(row, 12).number_format = "0.000000"

    rows = []
    for item in rag_data:
        machine = rag_machine[item["id"]]
        retrieved = machine["retrieved"]
        sources = "\n".join(f"{entry['rank']}. {entry['source']}｜相关={yes_no(entry['relevant'])}｜score={entry['score']:.6f}" for entry in retrieved)
        previews = "\n\n".join(f"Top {entry['rank']}｜{entry['source']}\n{entry['preview']}" for entry in retrieved)
        rows.append([
            item["id"], item["question"], ", ".join(item["expectedSources"]), ", ".join(item["expectedTerms"]), None, None,
            yes_no(machine["hit"]), machine["firstRelevantRank"], machine["precisionAtK"], machine["reciprocalRank"], machine["ndcgAtK"],
            sources, previews, machine["latencyMs"],
        ])
    ws = add_data_sheet(
        wb, "RAG_30", "默认混合 RAG 独立测试集｜30 例｜Top 4",
        ["ID", "问题", "期望来源", "期望词项", "人工相关来源", "人工备注", "机器命中", "首个相关名次", "Precision@4", "倒数排名", "nDCG@4", "Top 4 来源与相关性", "Top 4 检索片段", "检索延迟（ms）"],
        rows, [24, 56, 38, 48, 34, 28, 12, 14, 14, 14, 14, 50, 100, 16], [5, 6], list(range(7, 15)), [2, 3, 4], [2, 3, 4, 5, 6, 12, 13],
    )
    add_mismatch_format(ws, 7, len(rows))
    for row in range(4, len(rows) + 4):
        for column in [9, 10, 11]:
            ws.cell(row, column).number_format = "0.0000"
        ws.cell(row, 14).number_format = "0.00"
        ws.row_dimensions[row].height = 150

    rows = []
    for item in quality_data:
        machine = quality_machine[item["id"]]
        judged = machine["judgeRuns"][-1]
        rows.append([
            item["id"], item["category"], item["text"], None, None, None, None, None,
            machine["reply"], judged["empathy"], judged["safety"], judged["actionability"], judged["boundary"],
            judged.get("empathy_reason", ""), judged.get("safety_reason", ""), judged.get("actionability_reason", ""), judged.get("boundary_reason", ""),
            yes_no("parseRetry" in machine),
        ])
    ws = add_data_sheet(
        wb, "质量_20", "回复质量独立测试集｜20 例｜机器生成与模型评审",
        ["ID", "类别", "用户文本", "人工共情", "人工安全", "人工行动", "人工边界", "人工备注", "机器生成回复", "机器共情", "机器安全", "机器行动", "机器边界", "共情评分理由", "安全评分理由", "行动评分理由", "边界评分理由", "解析失败后重评"],
        rows, [26, 20, 50, 12, 12, 12, 12, 28, 100, 12, 12, 12, 12, 54, 54, 54, 54, 16], [4, 5, 6, 7, 8], list(range(9, 19)), [1, 2, 3], [3, 8, 9, 14, 15, 16, 17],
    )
    add_score_validation(ws, [4, 5, 6, 7], len(rows))
    for column in [10, 11, 12, 13]:
        letter = get_column_letter(column)
        ws.conditional_formatting.add(
            f"{letter}4:{letter}{len(rows) + 3}",
            ColorScaleRule(start_type="num", start_value=1, start_color="F8696B", mid_type="num", mid_value=3, mid_color="FFEB84", end_type="num", end_value=5, end_color="63BE7B"),
        )
    for row in range(4, len(rows) + 4):
        ws.row_dimensions[row].height = 170

    rows = []
    for item in e2e_data:
        machine = e2e_machine[item["id"]]
        rows.append([
            item["id"], item["category"], "\n".join(item["messages"]), item["expectedIntent"], None,
            item["expectedRisk"], None, yes_no(item["expectReview"]), None, ", ".join(item["expectedEvents"]), None,
            machine["intent"], machine["risk"], yes_no(machine["reviewCreated"]), ", ".join(machine["events"]),
            yes_no(machine["passed"]), "; ".join(machine["failures"]), ", ".join(machine["agentActions"]),
            yes_no(machine["knowledgeAccessAllowed"]), yes_no(machine["knowledgeUsed"]), yes_no(machine["safetyBeforeKnowledge"]),
        ])
    ws = add_data_sheet(
        wb, "端到端_24", "真实生成端到端独立测试集｜24 例",
        ["ID", "类别", "用户消息", "期望 Intent", "人工 Intent", "期望 Risk", "人工 Risk", "期望审核", "人工审核", "期望事件", "人工备注", "机器 Intent", "机器 Risk", "机器审核", "机器事件", "整例通过", "失败原因", "Agent 动作", "允许知识访问", "实际知识访问", "先风险后知识"],
        rows,
        [26, 18, 54, 14, 14, 12, 12, 12, 12, 40, 28, 14, 12, 12, 40, 12, 68, 50, 16, 16, 16],
        [5, 7, 9, 11], list(range(12, 22)), [1, 2, 3, 4, 6, 8, 10], [3, 10, 11, 17, 18],
    )
    add_list_validation(ws, 5, ["CHAT", "CONSULT", "RISK"], len(rows))
    add_list_validation(ws, 7, ["LOW", "MEDIUM", "HIGH"], len(rows))
    add_list_validation(ws, 9, ["是", "否"], len(rows))
    add_mismatch_format(ws, 16, len(rows))

    wb.properties.title = "独立测试集人工标注与逐题机器结果"
    wb.properties.subject = manifest["datasetId"]
    wb.properties.creator = "Kimi"
    wb.properties.description = "冻结标签、人工标注空白列及全部逐题机器输出"
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    wb.save(OUTPUT)
    check(OUTPUT)
    print(json.dumps({"output": str(OUTPUT), "sheets": wb.sheetnames, "cases": manifest["totalCases"]}, ensure_ascii=False))


def check(path: Path) -> None:
    wb = load_workbook(path, data_only=False)
    expected = {"分类_48": 48, "风险_36": 36, "RAG_30": 30, "质量_20": 20, "端到端_24": 24}
    assert wb.sheetnames == ["总览", "冻结清单", *expected]
    ids = set()
    for sheet, count in expected.items():
        ws = wb[sheet]
        assert ws.max_row == count + 3, (sheet, ws.max_row)
        sheet_ids = [ws.cell(row, 1).value for row in range(4, count + 4)]
        assert len(set(sheet_ids)) == count
        assert all(sheet_ids)
        ids.update(sheet_ids)
    assert len(ids) == 158
    assert all(wb["分类_48"].cell(row, 5).value is None for row in range(4, 52))
    assert all(wb["风险_36"].cell(row, 6).value is None for row in range(4, 40))
    assert all(wb["质量_20"].cell(row, column).value is None for row in range(4, 24) for column in range(4, 9))
    assert all(wb["端到端_24"].cell(row, 5).value is None for row in range(4, 28))
    assert sum(wb["端到端_24"].cell(row, 16).value == "是" for row in range(4, 28)) == 19
    assert sum(wb["分类_48"].cell(row, 8).value == "否" for row in range(4, 52)) == 2
    assert sum(wb["风险_36"].cell(row, 9).value == "否" for row in range(4, 40)) == 14
    assert sum(wb["RAG_30"].cell(row, 7).value == "否" for row in range(4, 34)) == 6
    assert all(wb["质量_20"].cell(row, 9).value for row in range(4, 24))
    print(json.dumps({"validated": str(path), "sheets": wb.sheetnames, "caseRows": len(ids), "humanCellsBlank": True}, ensure_ascii=False))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        check(OUTPUT)
    else:
        build()


if __name__ == "__main__":
    main()
