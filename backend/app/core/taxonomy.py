"""知识分类字典（单元 2）：业务域 + 文档类型。

设计（2026-10-28 与用户确认）：
- **业务域挂在知识库上**：一个知识库 = 一个业务域，库内文档自动继承；
  单元 3 建 6 个业务知识库时天然一一对应。
- **文档类型挂在文档上**：上传时选择，随检索结果返回，供引用判断来源性质。
- 这里是**唯一权威清单**：后端校验、前端下拉、清单展示、检索过滤都从这里取，
  避免「后端一套值、前端一套值」长期漂移。

改分类 = 改这个文件 + 前端 `frontend/src/constants/taxonomy.ts`（两边保持同名同值），
再跑一次迁移或回填脚本（旧数据靠脚本归类，不靠数据库约束）。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Category:
    """一个分类项：value 落库（英文稳定值），label 展示（中文）。"""

    value: str
    label: str
    description: str = ""


# ---------------- 业务域（6 个，用户 2026-10-28 确认的「推荐清单 A」） ----------------
BUSINESS_DOMAINS: tuple[Category, ...] = (
    Category("general", "综合与基础", "教材、通用规范、标准化、暂未归类资料"),
    Category("flood", "防洪减灾", "防汛抗旱、洪水风险、防洪标准、应急预案"),
    Category("water", "水资源与供水", "水资源规划配置、节约用水、供水与给排水工程"),
    Category("construction", "水利工程建设", "勘测设计、施工、监理、质量安全、造价与验收"),
    Category("operation", "工程运行管理", "水库、水闸、泵站、堤防的运行维护与安全监测"),
    Category("ecology", "河湖生态与农村水利", "河湖管理、水生态、水土保持、灌区与农村供水"),
)

DEFAULT_DOMAIN = "general"
DOMAIN_VALUES: frozenset[str] = frozenset(c.value for c in BUSINESS_DOMAINS)
DOMAIN_LABELS: dict[str, str] = {c.value: c.label for c in BUSINESS_DOMAINS}

# ---------------- 文档类型（10 类） ----------------
# 前 4 个是历史值（textbook/standard/manual/other），**值保持不变**，
# 避免旧数据与对外契约（document_type）出现两套含义。
DOC_TYPES: tuple[Category, ...] = (
    Category("standard", "规范", "国家标准/行业标准（GB、SL、DL 等）"),
    Category("regulation", "规程", "操作/运行/检测/验收规程"),
    Category("textbook", "教材", "高校教材、专业书籍"),
    Category("manual", "手册", "设计手册、技术手册、产品手册"),
    Category("emergency_plan", "预案", "防汛抗旱、应急处置等预案"),
    Category("scheme", "方案", "施工方案、专项方案、实施方案"),
    Category("report", "报告", "设计报告、评估报告、检测报告、工作总结"),
    Category("case", "案例", "工程案例、事故案例、经验做法"),
    Category("ledger", "台账", "方案台账、设备清单、检查记录等数据表"),
    Category("other", "其他", "无法归类或待补充"),
)

DEFAULT_DOC_TYPE = "other"
DOC_TYPE_VALUES: frozenset[str] = frozenset(c.value for c in DOC_TYPES)
DOC_TYPE_LABELS: dict[str, str] = {c.value: c.label for c in DOC_TYPES}

# 扩展名 → 建议文档类型（上传时前端可据此预选，后端不强制）
EXT_DOC_TYPE_HINT: dict[str, str] = {
    "xlsx": "ledger",
    "csv": "ledger",
}


def is_valid_domain(value: str | None) -> bool:
    return bool(value) and value in DOMAIN_VALUES


def is_valid_doc_type(value: str | None) -> bool:
    return bool(value) and value in DOC_TYPE_VALUES


def normalize_domain(value: str | None) -> str:
    """非法/空 → general（不抛错，历史数据与旧客户端都能跑）。"""
    return value if is_valid_domain(value) else DEFAULT_DOMAIN


def normalize_doc_type(value: str | None) -> str:
    """非法/空 → other（与扩展前的默认行为一致）。"""
    return value if is_valid_doc_type(value) else DEFAULT_DOC_TYPE


def domain_label(value: str | None) -> str:
    return DOMAIN_LABELS.get(normalize_domain(value), DEFAULT_DOMAIN)


def doc_type_label(value: str | None) -> str:
    return DOC_TYPE_LABELS.get(normalize_doc_type(value), "其他")


def as_options(items: tuple[Category, ...]) -> list[dict[str, str]]:
    """给接口返回下拉选项：[{value,label,description}]。"""
    return [{"value": c.value, "label": c.label, "description": c.description} for c in items]
