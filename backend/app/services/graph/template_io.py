"""防洪链台账模板的生成与读取（Excel）。

模板四张表 + 「说明」+「填写示例」；写入与读取放在同一模块，
保证「模板列名」与「导入解析」永远一致（改一处即可）。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

HEADER_FILL = PatternFill("solid", fgColor="1F4E79")
HEADER_FONT = Font(color="FFFFFF", bold=True)

SHEET_PROJECTS = "工程表"
SHEET_CONDITIONS = "工况表"
SHEET_REACHES = "河段表"
SHEET_IMPACTS = "影响对象表"
REQUIRED_SHEETS: tuple[str, ...] = (SHEET_PROJECTS, SHEET_CONDITIONS, SHEET_REACHES, SHEET_IMPACTS)

SHEETS: dict[str, list[str]] = {
    SHEET_PROJECTS: [
        "工程名称", "工程类型", "所属水库", "管理单位", "所在乡镇",
        "坝高(m)", "总库容(万m³)", "备注",
    ],
    SHEET_CONDITIONS: [
        "工况名", "工况类型", "频率", "库水位(m)", "下泄流量(m³/s)",
        "依据条款", "别名", "备注",
    ],
    SHEET_REACHES: [
        "河段名", "所属水库", "起点", "终点", "长度(km)",
        "安全泄量(m³/s)", "依据条款", "备注",
    ],
    SHEET_IMPACTS: [
        "工况", "河段", "乡镇", "村", "是否预警", "预警阈值",
        "是否转移", "安置点", "转移路线", "依据条款", "责任人",
    ],
}

SAMPLES: dict[str, list[list[str]]] = {
    SHEET_PROJECTS: [
        ["鲤鱼塘水库", "reservoir", "", "XX县水利局", "XX镇", "58.5", "1200", "示例行，导入时跳过"],
        ["鲤鱼塘水库大坝", "dam", "鲤鱼塘水库", "", "", "58.5", "", "示例行，导入时跳过"],
    ],
    SHEET_CONDITIONS: [
        ["设计工况", "design", "100年一遇", "412.30", "1230", "汛期调度规程 4.2", "设计洪水工况", "示例行"],
        ["校核工况", "check", "1000年一遇", "413.80", "1680", "汛期调度规程 4.3", "", "示例行"],
    ],
    SHEET_REACHES: [
        ["鲤鱼塘水库下游河段1", "鲤鱼塘水库", "坝下", "XX桥", "6.5", "800", "防洪抢险应急预案 附表3", "示例行"],
        ["鲤鱼塘水库下游河段2", "鲤鱼塘水库", "XX桥", "YY汇口", "9.2", "600", "防洪抢险应急预案 附表3", "示例行"],
    ],
    SHEET_IMPACTS: [
        ["设计工况", "鲤鱼塘水库下游河段1", "XX镇", "A村", "是", "800 m³/s", "是", "XX镇中心小学", "沿河路→镇道", "预案 5.3", "XX镇防汛责任人", "示例行"],
        ["设计工况", "鲤鱼塘水库下游河段1", "XX镇", "B村", "是", "800 m³/s", "否", "", "", "预案 5.3", "", "示例行"],
    ],
}

NOTES = [
    "防洪链台账填写说明（第一期）",
    "",
    "一、这份表用来做什么",
    "  把《防洪抢险应急预案》《汛期调度规程》里的关键事实，先人工整理成四张表；",
    "  导入后会变成知识图谱的节点和关系，问答时用来「取全清单、给准数字」。",
    "",
    "二、四张表分别填什么",
    "  1) 工程表：水库本身，以及大坝/溢洪道/闸门等建筑物（建筑物要填「所属水库」）",
    "  2) 工况表：设计/校核/汛限等工况，各自的下泄流量、库水位、依据条款",
    "  3) 河段表：水库下游各河段，重点填「安全泄量(m³/s)」（各河段不同）",
    "  4) 影响对象表：某工况下，哪些乡镇/村要预警、要转移、安置在哪、依据哪条",
    "",
    "三、填写规则",
    "  • 表头不要改名、不要删列（程序按表头取值）；",
    "  • 「是否预警」「是否转移」填 是/否（也认 y/√/1）；",
    "  • 数值列只填数字（如 1230、58.5），单位已写在表头里；",
    "  • 「依据条款」尽量写（如 预案 5.3、调度规程 4.2），答案会据此标出处；",
    "  • 一行只写一个对象；同一乡镇多个村就写多行；",
    "  • 不确定的格子留空，比填错好。",
    "",
    "四、导入方式",
    "  cd backend",
    "  .venv\\Scripts\\python.exe -X utf8 scripts\\import_flood_chain.py --file templates/flood_chain_template.xlsx --kb-name 你的知识库名",
    "  先看预览报告（dry-run），确认后再加 --apply 写库。",
    "",
    "五、注意",
    "  「填写示例」工作表只是参考，程序不会读取它；正式填写请用前四张空白表。",
    "  备注里带「示例行」的行会被自动跳过，不会导入。",
]


def build_workbook() -> Workbook:
    wb = Workbook()
    wb.remove(wb.active)

    notes = wb.create_sheet("说明")
    for row_idx, line in enumerate(NOTES, start=1):
        cell = notes.cell(row=row_idx, column=1, value=line)
        cell.alignment = Alignment(vertical="top")
        if row_idx == 1:
            cell.font = Font(bold=True, size=14)
    notes.column_dimensions["A"].width = 100

    for sheet_name, headers in SHEETS.items():
        ws = wb.create_sheet(sheet_name)
        ws.append(headers)
        for col_idx, header in enumerate(headers, start=1):
            cell = ws.cell(row=1, column=col_idx)
            cell.fill = HEADER_FILL
            cell.font = HEADER_FONT
            cell.alignment = Alignment(horizontal="center", vertical="center")
            ws.column_dimensions[get_column_letter(col_idx)].width = max(12, len(header) + 6)
        ws.freeze_panes = "A2"

    sample_ws = wb.create_sheet("填写示例")
    sample_ws.column_dimensions["A"].width = 24
    row = 1
    for sheet_name, samples in SAMPLES.items():
        sample_ws.cell(row=row, column=1, value=f"【{sheet_name}】").font = Font(bold=True)
        row += 1
        headers = SHEETS[sheet_name]
        for col_idx, header in enumerate(headers, start=1):
            cell = sample_ws.cell(row=row, column=col_idx, value=header)
            cell.fill = HEADER_FILL
            cell.font = HEADER_FONT
        row += 1
        for sample in samples:
            for col_idx, value in enumerate(sample, start=1):
                sample_ws.cell(row=row, column=col_idx, value=value)
            row += 1
        row += 1

    return wb


def write_template(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    build_workbook().save(path)
    return path


def read_sheet(workbook, name: str) -> list[dict[str, Any]]:
    """把一张表读成 [{表头: 值}]；跳过空行与备注含「示例行」的行。"""
    if name not in workbook.sheetnames:
        return []
    rows = list(workbook[name].iter_rows(values_only=True))
    if not rows:
        return []
    headers = [str(h).strip() if h is not None else "" for h in rows[0]]
    out: list[dict[str, Any]] = []
    for raw in rows[1:]:
        if not raw or all(v in (None, "") for v in raw):
            continue
        values = ["" if v is None else str(v).strip() for v in raw]
        if any("示例行" in v for v in values):
            continue
        out.append({h: v for h, v in zip(headers, values) if h})
    return out


def read_template(path: Path) -> dict[str, list[dict[str, Any]]]:
    """读取四张表；缺表直接报错（避免「以为导入了其实没读」）。"""
    workbook = load_workbook(path, data_only=True)
    missing = [s for s in REQUIRED_SHEETS if s not in workbook.sheetnames]
    if missing:
        raise ValueError(f"缺少工作表：{'、'.join(missing)}")
    return {
        "projects": read_sheet(workbook, SHEET_PROJECTS),
        "conditions": read_sheet(workbook, SHEET_CONDITIONS),
        "reaches": read_sheet(workbook, SHEET_REACHES),
        "impacts": read_sheet(workbook, SHEET_IMPACTS),
    }
