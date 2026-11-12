"""生成「参数先验」Excel 模板。

模板只预填**名称/分组/单位**这三样定义性信息；
物理范围、先验范围、初值一律留空——这些必须由领域专家确认后填写，
程序不替人编数字（本项目的硬规则：无证据的数值不进入推荐卡）。

用法（在 backend 目录下）：
    python -m scripts.make_param_priors_template
    python -m scripts.make_param_priors_template --out templates/param_priors_template.xlsx
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from openpyxl import Workbook  # noqa: E402
from openpyxl.styles import Alignment, Font, PatternFill  # noqa: E402
from openpyxl.utils import get_column_letter  # noqa: E402
from openpyxl.worksheet.datavalidation import DataValidation  # noqa: E402

from app.services.graph.ontology.params import COLUMNS, HEADERS  # noqa: E402

DEFAULT_OUT = BASE_DIR / "templates" / "param_priors_template.xlsx"

# 参数清单：名称 / 中文名 / 分组 / 单位（只填定义性信息，不含任何取值范围）
PARAMETERS: tuple[tuple[str, str, str, str], ...] = (
    ("ke", "蒸散发折算系数", "蒸散发", "-"),
    ("c", "深层蒸散发系数", "蒸散发", "-"),
    ("wum", "上层蓄水容量", "产流", "mm"),
    ("wlm", "下层蓄水容量", "产流", "mm"),
    ("wdm", "深层蓄水容量", "产流", "mm"),
    ("a_imp", "不透水面积占比", "产流", "-"),
    ("b", "蓄水容量曲线指数", "产流", "-"),
    ("sm", "自由水蓄水能力", "水源划分", "mm"),
    ("ex", "自由水蓄水容量曲线指数", "水源划分", "-"),
    ("ki", "壤中流出流系数", "水源划分", "-"),
    ("kg", "地下水出流系数", "水源划分", "-"),
    ("ci", "壤中流消退系数", "汇流", "-"),
    ("cg", "地下水消退系数", "汇流", "-"),
)

_GROUP_OPTIONS = "蒸散发,产流,水源划分,汇流,其他"
_SENS_OPTIONS = "高,中,低"

_HELP_TEXT = """【这张表是干什么的】
它填的是「参数先验知识」：某个参数大致取多少、什么条件下可以收窄、依据是什么。
导入后进入本体库（PostgreSQL），供本体 RAG 和自动建模 agent 查询。

【硬规则】
1. 没有依据的数值不要填。填了却没写「依据」，导入会告警，且该数值不会出现在推荐卡里。
2. 拿不准的单元格留空，或写「待确认」。程序不会替你猜。
3. 数值只填数字（如 0.35），不要带单位、不要写「约」「左右」。

【列说明】
参数名      必填。程序内部用的英文代号，如 ke / sm / cg。
中文名      给人看的名字，会作为别名一起进图谱。
分组        必填，只能选：蒸散发 / 产流 / 水源划分 / 汇流 / 其他。
单位        无量纲填 -。蓄水容量类是 mm。
物理下限/上限   物理上不可能越过的边界（如系数不可能大于 1）。没有把握就留空。
通用下限/上限   一般情况下建议的取值范围。
条件范围    按条件收窄的范围，写法见下。
初值        率定的起点值。
敏感度      高 / 中 / 低，表示该参数对结果的影响程度。
依据类型    教材 / 规范 / 文献 / 经验 / 待确认。
依据        一句话说明这个数值从哪来（人看的）。
依据文档    关键列：填写知识库里**已入库文档的文件名**（如「水力学第5版.pdf」）。
            导入时会去知识库核对，找得到就把 doc_id 写进证据，找不到会告警并标「未溯源」。
依据位置    页码/章节，如「第53页」「4.2 节」。能解析出页码时会去定位具体切片。
原文摘录    把原文里支撑这句话的**原文**粘一段过来（20 字以上即可）。
            有摘录且能匹配上切片 → 证据等级为「已溯源」，可以点回原文。
备注        任何补充说明。

【证据等级说明】
已溯源   有 doc_id + chunk_id，能点回原文
仅到文档 只找到文档，没定位到具体页码/切片
仅说明   只有「依据」那一句话，知识库里找不到出处
无       完全没填依据 → 该参数的数值不会出现在推荐卡里

【条件范围怎么写】
一行一条，格式：条件 => 下限~上限
多个条件用 ; 连接（表示同时满足）：
    climate=湿润;flood_type=单峰 => 0.15~0.30
    climate=半干旱 => 0.20~0.40
可选的条件键：climate（湿润/半湿润/半干旱/干旱）、flood_type（单峰/多峰）。
多条之间用换行分隔。

【导入命令】
    python -m scripts.import_param_priors --dry-run     # 先看会写什么
    python -m scripts.import_param_priors --apply       # 真写库
"""


def _style_header(ws, headers: tuple[str, ...]) -> None:
    fill = PatternFill("solid", fgColor="1F4E78")
    font = Font(color="FFFFFF", bold=True)
    for idx, title in enumerate(headers, start=1):
        cell = ws.cell(row=1, column=idx, value=title)
        cell.fill = fill
        cell.font = font
        cell.alignment = Alignment(horizontal="center", vertical="center")
    ws.freeze_panes = "A2"
    ws.row_dimensions[1].height = 24


def _set_widths(ws, headers: tuple[str, ...]) -> None:
    widths = {
        "参数名": 12, "中文名": 22, "分组": 12, "单位": 8,
        "物理下限": 10, "物理上限": 10, "通用下限": 10, "通用上限": 10,
        "条件范围": 38, "初值": 10, "敏感度": 10, "依据类型": 12, "依据": 30,
        "依据文档": 26, "依据位置": 16, "原文摘录": 32, "备注": 26,
    }
    for idx, title in enumerate(headers, start=1):
        ws.column_dimensions[get_column_letter(idx)].width = widths.get(title, 14)


def build(out: Path) -> Path:
    wb = Workbook()
    ws = wb.active
    ws.title = "参数先验"
    headers = HEADERS

    _style_header(ws, headers)
    _set_widths(ws, headers)

    col_index = {name: idx for idx, (name, _) in enumerate(COLUMNS, start=1)}
    for r, (name, zh, group, unit) in enumerate(PARAMETERS, start=2):
        ws.cell(row=r, column=col_index["参数名"], value=name)
        ws.cell(row=r, column=col_index["中文名"], value=zh)
        ws.cell(row=r, column=col_index["分组"], value=group)
        ws.cell(row=r, column=col_index["单位"], value=unit)
        ws.cell(row=r, column=col_index["依据类型"], value="待确认")
        ws.cell(
            row=r,
            column=col_index["备注"],
            value="范围/初值待专家确认",
        )

    last_row = len(PARAMETERS) + 200
    group_validator = DataValidation(
        type="list", formula1=f'"{_GROUP_OPTIONS}"', allow_blank=True, showDropDown=False
    )
    group_validator.error = "分组只能是：蒸散发 / 产流 / 水源划分 / 汇流 / 其他"
    ws.add_data_validation(group_validator)
    group_validator.add(f"{get_column_letter(col_index['分组'])}2:{get_column_letter(col_index['分组'])}{last_row}")

    sens_validator = DataValidation(
        type="list", formula1=f'"{_SENS_OPTIONS}"', allow_blank=True, showDropDown=False
    )
    sens_validator.error = "敏感度只能是：高 / 中 / 低"
    ws.add_data_validation(sens_validator)
    sens_validator.add(
        f"{get_column_letter(col_index['敏感度'])}2:{get_column_letter(col_index['敏感度'])}{last_row}"
    )

    help_ws = wb.create_sheet("填写说明")
    help_ws.column_dimensions["A"].width = 110
    for i, line in enumerate(_HELP_TEXT.splitlines(), start=1):
        cell = help_ws.cell(row=i, column=1, value=line)
        if line.startswith("【"):
            cell.font = Font(bold=True, color="1F4E78")

    out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="生成参数先验 Excel 模板")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="输出路径")
    args = parser.parse_args()
    path = build(Path(args.out))
    print(f"已生成：{path}")
    print(f"预填 {len(PARAMETERS)} 个参数名称/分组/单位；范围与初值留空，等专家确认后填写。")


if __name__ == "__main__":
    main()
